"""
MiniTradeIQ — Own Indian Financials Pipeline (v2 — XBRL)
=========================================================
NSE publishes every financial-results filing with a structured Ind-AS
XBRL file (confirmed via live response). This pipeline:

  1. Lists annual filings per symbol  (corporates-financial-results API)
  2. Picks the Consolidated filing per fiscal year (Standalone fallback)
  3. Downloads each year's XBRL XML and extracts P&L + Balance Sheet +
     Cash Flow facts directly — no PDF parsing, no unit guessing
     (XBRL values are absolute rupees)
  4. Falls back to results-PDF + Groq extraction ONLY if XBRL lacks
     balance-sheet facts
  5. Writes everything to your Postgres store

This is the India equivalent of the SEC EDGAR pipeline.

Requirements: httpx, pdfplumber (fallback only)
Env: GROQ_API_KEY (fallback only)
"""

import os
import io
import re
import json
import time
import httpx
import xml.etree.ElementTree as ET
from datetime import datetime, date

# ── NSE session handling (unchanged — proven working) ──────────────────────────

NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

_nse_client = None
_nse_client_time = 0
_NSE_SESSION_TTL = 300


def _nse():
    global _nse_client, _nse_client_time
    now = time.time()
    if _nse_client is not None and (now - _nse_client_time) < _NSE_SESSION_TTL:
        return _nse_client
    client = httpx.Client(timeout=30.0, headers=NSE_HEADERS, follow_redirects=True)
    client.get("https://www.nseindia.com")
    time.sleep(0.5)
    _nse_client = client
    _nse_client_time = now
    return client


def _nse_get(url: str, retries: int = 3):
    last_err = None
    for attempt in range(retries):
        try:
            if attempt > 0:
                time.sleep(2 * attempt)
                global _nse_client
                _nse_client = None
            resp = _nse().get(url)
            if resp.status_code == 200:
                return resp
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"NSE request failed after {retries} tries: {last_err} — {url}")


def _nse_get_json(url: str, retries: int = 3):
    return _nse_get(url, retries).json()


# ── 1) List annual filings, pick best per fiscal year ──────────────────────────

def _q(symbol: str) -> str:
    """URL-encode a symbol — NSE names like M&M break query strings raw."""
    from urllib.parse import quote
    return quote(symbol, safe="")


def fetch_annual_filings(symbol: str) -> dict:
    """
    Returns {fiscal_year: {"xbrl": url, "consolidated": bool, "to_date": "31-Mar-2024"}}
    Prefers Consolidated over Standalone for each year.
    """
    url = (f"https://www.nseindia.com/api/corporates-financial-results"
           f"?index=equities&symbol={_q(symbol)}&period=Annual")
    data = _nse_get_json(url)
    rows = data if isinstance(data, list) else data.get("data", [])

    # NSE occasionally returns an empty list on a stale session — one retry
    # with a forced fresh cookie handshake
    if not rows:
        global _nse_client
        _nse_client = None
        time.sleep(2)
        data = _nse_get_json(url)
        rows = data if isinstance(data, list) else data.get("data", [])

    by_fy: dict = {}
    for r in rows:
        xbrl = r.get("xbrl")
        to_date = r.get("toDate", "")
        if not xbrl or not to_date:
            continue
        m = re.search(r"(\d{4})", to_date)
        if not m:
            continue
        fy = int(m.group(1))
        is_cons = str(r.get("consolidated", "")).strip().lower() == "consolidated"
        fmt     = str(r.get("format", "")).strip().lower()
        existing = by_fy.get(fy)
        if existing is None or (is_cons and not existing["consolidated"]):
            by_fy[fy] = {"xbrl": xbrl, "consolidated": is_cons,
                         "to_date": to_date, "format": fmt}
    return by_fy


# ── 1b) Sector classification from NSE quote API ───────────────────────────────

# NSE macro-sector → our SECTOR_PE_MEDIANS keys
_NSE_SECTOR_MAP = {
    "financial services":          "Financial Services",
    "information technology":      "Technology",
    "fast moving consumer goods":  "Consumer Defensive",
    "consumer discretionary":      "Consumer Cyclical",
    "consumer services":           "Consumer Cyclical",
    "healthcare":                  "Healthcare",
    "energy":                      "Energy",
    "oil gas & consumable fuels":  "Energy",
    "commodities":                 "Basic Materials",
    "metals & mining":             "Basic Materials",
    "chemicals":                   "Basic Materials",
    "industrials":                 "Industrials",
    "capital goods":               "Industrials",
    "construction":                "Industrials",
    "services":                    "Industrials",
    "diversified":                 "Industrials",
    "telecommunication":           "Communication Services",
    "utilities":                   "Utilities",
    "power":                       "Utilities",
    "realty":                      "Real Estate",
    # ── full NSE Nifty-500 CSV industry labels ──
    "automobile and auto components":      "Consumer Cyclical",
    "consumer durables":                   "Consumer Cyclical",
    "construction materials":              "Basic Materials",
    "forest materials":                    "Basic Materials",
    "media entertainment & publication":   "Communication Services",
    "textiles":                            "Consumer Cyclical",
}


_SECTOR_CSV_CACHE = None


def _load_sector_map() -> dict:
    """symbol -> Industry for all Nifty 500 names, from NSE's index CSV.
    One request covers every company; cached for the process lifetime."""
    global _SECTOR_CSV_CACHE
    if _SECTOR_CSV_CACHE is not None:
        return _SECTOR_CSV_CACHE
    m = {}
    try:
        import csv as _csv, io as _io
        resp = _nse_get(
            "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
        )
        for r in _csv.DictReader(_io.StringIO(resp.text)):
            sym = (r.get("Symbol") or "").strip().upper()
            ind = (r.get("Industry") or "").strip()
            if sym and ind:
                m[sym] = ind
        print(f"  sector map loaded: {len(m)} symbols from Nifty 500 CSV")
    except Exception as e:
        print(f"  sector map load failed ({e})")
    _SECTOR_CSV_CACHE = m
    return m


def fetch_nse_sector(symbol: str) -> tuple:
    """Returns (mapped_sector, raw_industry).
    Primary: Nifty-500 CSV Industry column (one cached request for all).
    Fallback: per-symbol quote API. ("Unknown","Unknown") if both fail."""
    # ── CSV primary ─────────────────────────────────────────────────────────
    ind = _load_sector_map().get(symbol.upper())
    if ind:
        mapped = _NSE_SECTOR_MAP.get(ind.strip().lower(), "Unknown")
        return mapped, ind

    # ── Quote-API fallback ──────────────────────────────────────────────────
    try:
        data = _nse_get_json(
            f"https://www.nseindia.com/api/quote-equity?symbol={_q(symbol)}"
        )
        info = data.get("industryInfo", {}) or {}
        raw_industry = (info.get("industry") or info.get("basicIndustry")
                        or "Unknown")
        for key in (info.get("macro"), info.get("sector"), info.get("industry")):
            if key and key.strip().lower() in _NSE_SECTOR_MAP:
                return _NSE_SECTOR_MAP[key.strip().lower()], raw_industry
        return "Unknown", raw_industry
    except Exception:
        return "Unknown", "Unknown"


# ── 2) XBRL parsing ─────────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    """Strip XML namespace: '{ns}RevenueFromOperations' → 'revenuefromoperations'."""
    return tag.split("}")[-1].lower()


def _parse_date(s: str):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_xbrl(xml_bytes: bytes, period_end: date) -> dict:
    """
    Extract facts from an Ind-AS XBRL instance.

    NSE quirk (confirmed on live RIL FY24 filing): ALL duration contexts carry
    Q4 dates (e.g. 2024-01-01 -> 2024-03-31), even the cumulative full-year
    one. Naming encodes the truth: OneD = single quarter, FourD = four
    quarters cumulative = the ANNUAL figures. Date-span filtering therefore
    fails. Instead we pick, among contexts ending at period_end, the one with
    the MOST numeric facts — the cumulative annual context is always the
    richest (RIL: FourD=144 facts vs OneD=54 vs segment contexts=1).
    Same max-facts rule selects the primary instant (balance sheet) context.
    """
    from collections import Counter
    root = ET.fromstring(xml_bytes)

    # ── Contexts: id -> (start, end, instant) ────────────────────────────────
    contexts: dict = {}
    for el in root.iter():
        if _local(el.tag) != "context":
            continue
        cid = el.get("id")
        start = end = instant = None
        for child in el.iter():
            lc = _local(child.tag)
            if lc == "startdate":
                start = _parse_date(child.text)
            elif lc == "enddate":
                end = _parse_date(child.text)
            elif lc == "instant":
                instant = _parse_date(child.text)
        contexts[cid] = (start, end, instant)

    # ── Numeric fact census per context ──────────────────────────────────────
    counts: Counter = Counter()
    fact_els = []
    for el in root.iter():
        ctx = el.get("contextRef")
        if ctx is None or el.text is None:
            continue
        txt = el.text.strip()
        if txt in ("", "-"):
            continue
        try:
            float(txt)
        except ValueError:
            continue
        counts[ctx] += 1
        fact_els.append(el)

    # ── Primary contexts: most facts among those anchored at period_end ─────
    dur_candidates  = [cid for cid, (s, e, i) in contexts.items() if e == period_end]
    inst_candidates = [cid for cid, (s, e, i) in contexts.items() if i == period_end]

    primary_dur  = max(dur_candidates,  key=lambda c: counts.get(c, 0), default=None)
    primary_inst = max(inst_candidates, key=lambda c: counts.get(c, 0), default=None)
    keep = {c for c in (primary_dur, primary_inst) if c is not None}

    # ── Facts from the primary contexts only ─────────────────────────────────
    facts: dict = {}
    for el in fact_els:
        if el.get("contextRef") not in keep:
            continue
        name = _local(el.tag)
        if name not in facts:
            facts[name] = float(el.text.strip())

    return facts


def _pick(facts: dict, *candidates, contains: str = None):
    """Find a fact by exact candidate names (lowercased), else substring."""
    for c in candidates:
        v = facts.get(c.lower())
        if v is not None:
            return v
    if contains:
        needle = contains.lower()
        for k, v in facts.items():
            if needle in k:
                return v
    return None


def extract_financials_from_xbrl(xml_bytes: bytes, period_end: date) -> dict:
    """Map raw XBRL facts → our store schema fields (absolute rupees)."""
    f = parse_xbrl(xml_bytes, period_end)

    revenue = _pick(f, "RevenueFromOperations", contains="revenuefromoperations")
    total_income = _pick(f, "Income", "TotalIncome")
    if revenue is None:
        revenue = total_income

    expenses = _pick(f, "Expenses", "TotalExpenses", contains="totalexpense")
    pbt      = _pick(f, "ProfitBeforeTax", "ProfitLossBeforeTax",
                     contains="profitbeforetax")
    tax      = _pick(f, "TaxExpense", "TotalTaxExpenses", contains="taxexpense")
    pat      = _pick(f, "ProfitLossForPeriod", "ProfitLossForThePeriod",
                     "NetProfitLossForThePeriod", contains="profitlossforperiod")
    eps      = _pick(f, "BasicEarningsLossPerShareFromContinuingOperations",
                     "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                     contains="basicearnings")
    depr     = _pick(f, "DepreciationDepletionAndAmortisationExpense",
                     contains="depreciation")
    fincost  = _pick(f, "FinanceCosts", contains="financecost")

    if tax is None and pbt is not None and pat is not None:
        tax = pbt - pat

    # Balance sheet (instant facts, present in annual/half-yearly filings)
    curr_assets = _pick(f, "CurrentAssets", "TotalCurrentAssets",
                        contains="currentassets")
    curr_liab   = _pick(f, "CurrentLiabilities", "TotalCurrentLiabilities",
                        contains="currentliabilities")
    cash        = _pick(f, "CashAndCashEquivalents", contains="cashandcashequivalents")
    ppe         = _pick(f, "PropertyPlantAndEquipment", contains="propertyplantandequipment")
    equity      = _pick(f, "Equity", "EquityAttributableToOwnersOfParent",
                        contains="equityattributable")
    # Borrowings: careful — "noncurrentborrowings" CONTAINS the substring
    # "currentborrowings", so current must exclude "non" explicitly
    borrow_nc  = _pick(f, "NonCurrentBorrowings", "LongTermBorrowings",
                       "BorrowingsNonCurrent", contains="noncurrentborrowings")
    borrow_cur = _pick(f, "CurrentBorrowings", "ShortTermBorrowings",
                       "BorrowingsCurrent")
    if borrow_cur is None:
        for k, v in f.items():
            if "borrowings" in k and "noncurrent" not in k and "current" in k:
                borrow_cur = v
                break
    # Some filings report a single combined "borrowings" figure
    if borrow_cur is None and borrow_nc is None:
        borrow_nc = _pick(f, "Borrowings")
    minority    = _pick(f, "NonControllingInterests", contains="noncontrolling")
    investments = _pick(f, "NonCurrentInvestments", contains="noncurrentinvestments")

    total_debt = None
    if borrow_cur is not None or borrow_nc is not None:
        total_debt = (borrow_cur or 0) + (borrow_nc or 0)

    # Cash flow (present in annual filings)
    ocf   = _pick(f, "NetCashFlowsFromUsedInOperatingActivities",
                  contains="operatingactivities")
    capex = _pick(f, "PurchaseOfPropertyPlantAndEquipment",
                  contains="purchaseofproperty")

    return {
        # P&L
        "revenue":          revenue,
        "total_expenses":   expenses,
        "pretax_income":    pbt,
        "tax_provision":    tax,
        "net_income":       pat,
        "eps":              eps,
        "depreciation":     depr,
        "interest_expense": fincost,
        # Balance sheet
        "current_assets":      curr_assets,
        "current_liabilities": curr_liab,
        "cash":                cash,
        "net_ppe":             ppe,
        "total_equity":        equity,
        "cpltd":               borrow_cur,
        "total_debt":          total_debt,
        "minority_interest":   minority,
        "long_term_investments": investments,
        # Cash flow
        "operating_cash_flow": ocf,
        "capex":               abs(capex) if capex is not None else None,
        # Diagnostics
        "_facts_found": len(f),
    }


# ── 3) PDF + Groq fallback (only if XBRL lacked balance sheet) ─────────────────

def fetch_latest_results_pdf_url(symbol: str) -> str | None:
    url = (f"https://www.nseindia.com/api/corporate-announcements"
           f"?index=equities&symbol={_q(symbol)}")
    try:
        data = _nse_get_json(url)
    except Exception:
        return None
    rows = data if isinstance(data, list) else data.get("data", [])
    for r in rows:
        desc = (str(r.get("desc", "")) + " " + str(r.get("attchmntText", ""))).lower()
        attach = r.get("attchmntFile") or r.get("attachmentFile") or r.get("pdfLink")
        if attach and "financial result" in desc and str(attach).lower().endswith(".pdf"):
            return attach
    return None


def _pdf_groq_balance_sheet(symbol: str) -> dict | None:
    """Fallback: extract BS from the latest results PDF via Groq."""
    try:
        import pdfplumber
        pdf_url = fetch_latest_results_pdf_url(symbol)
        if not pdf_url:
            return None
        resp = _nse_get(pdf_url)
        text_parts = []
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            for page in pdf.pages[:12]:
                text_parts.append(page.extract_text() or "")
        full_text = "\n".join(text_parts)

        lower = full_text.lower()
        for marker in ("statement of assets and liabilities", "balance sheet", "assets"):
            idx = lower.find(marker)
            if idx != -1:
                full_text = full_text[max(0, idx - 500):idx + 9000]
                break
        else:
            full_text = full_text[:9000]

        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            return None

        prompt = f"""Extract the most recent period's balance sheet from this Indian
financial results document. Values may be in lakhs or crores — detect from the
header and convert ALL monetary values to ABSOLUTE RUPEES (lakh=x1e5, crore=x1e7).
Use null when absent. Respond with ONLY JSON:
{{"fiscal_year": <year>, "current_assets": n, "current_liabilities": n,
"cash": n, "cpltd": n, "net_ppe": n, "long_term_investments": n,
"minority_interest": n, "total_debt": n, "total_equity": n,
"depreciation": n, "capex": n, "operating_cash_flow": n}}

DOCUMENT:
{full_text}"""

        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}"},
                json={"model": "llama-3.3-70b-versatile",
                      "max_tokens": 600, "temperature": 0.0,
                      "response_format": {"type": "json_object"},
                      "messages": [
                          {"role": "system", "content": "Respond with valid JSON only."},
                          {"role": "user",   "content": prompt}]},
            )
        if r.status_code != 200:
            return None
        parsed = json.loads(r.json()["choices"][0]["message"]["content"])
        ca = parsed.get("current_assets")
        if ca is not None and (ca < 0 or ca > 1e16):
            return None
        return parsed
    except Exception:
        return None


# ── 4) Full ingestion for one symbol ────────────────────────────────────────────

def ingest_india_own(ticker: str) -> bool:
    from data_store import upsert_company, upsert_statements
    import pandas as pd

    symbol = ticker.upper().replace(".NS", "")
    store_ticker = symbol + ".NS"

    filings = fetch_annual_filings(symbol)
    if not filings:
        print(f"  ⚠ {symbol}: no annual filings found on NSE")
        return False

    years = sorted(filings.keys(), reverse=True)[:6]
    yearly: dict = {}

    # Filings up to FY2022 use a legacy XBRL taxonomy our parser doesn't
    # cover (confirmed: RELIANCE/TCS/INFY all yield 2 facts pre-FY2023).
    # NSE's "format" field can't distinguish them (legacy filings are also
    # marked "New"), so we cut over by year. Skipped years are filled from
    # the yfinance merge below.
    LEGACY_XBRL_CUTOFF = 2022

    for fy in years:
        info_f = filings[fy]
        if fy <= LEGACY_XBRL_CUTOFF:
            print(f"  FY{fy}: legacy taxonomy, skipping XBRL (yfinance will fill)")
            continue
        period_end = _parse_date(info_f["to_date"])
        try:
            resp = _nse_get(info_f["xbrl"])
            data = extract_financials_from_xbrl(resp.content, period_end)
            if data.get("revenue"):
                yearly[fy] = data
                bs_flag = "BS ok" if data.get("current_assets") else "BS missing"
                print(f"  FY{fy}: rev={data['revenue']:.3e} "
                      f"({data['_facts_found']} facts, {bs_flag}, "
                      f"{'cons' if info_f['consolidated'] else 'standalone'})")
            else:
                print(f"  ⚠ FY{fy}: XBRL parsed but no revenue fact "
                      f"({data['_facts_found']} facts)")
        except Exception as e:
            print(f"  ⚠ FY{fy}: XBRL failed ({e})")
        time.sleep(1.0)

    if not yearly:
        print(f"  ❌ {symbol}: no usable years")
        return False

    # ── Fill older years from yfinance (XBRL years keep priority) ───────────
    if len(yearly) < 5:
        try:
            import yfinance as yf

            def _yfv(df, col_i, *keywords):
                if df is None or df.empty:
                    return None
                for idx in df.index:
                    low = idx.lower()
                    if all(k in low for k in keywords):
                        try:
                            v = df.loc[idx].iloc[col_i]
                            return None if (v is None or str(v) == "nan") else float(v)
                        except Exception:
                            return None
                return None

            inc = bal = cf = None
            for attempt in range(3):
                if attempt > 0:
                    time.sleep(6 * attempt)   # 6s, 12s — let Yahoo cool off
                t   = yf.Ticker(store_ticker)
                inc, bal, cf = t.financials, t.balance_sheet, t.cashflow
                if inc is not None and not inc.empty:
                    break
            if inc is None or inc.empty:
                print(f"  yfinance merge: empty response (throttled) — "
                      f"rerun backfill later to fill older years")
            added = []
            if inc is not None and not inc.empty:
                for i, col in enumerate(inc.columns):
                    try:
                        fy = int(col.year)
                    except Exception:
                        continue
                    if fy in yearly or len(yearly) >= 6:
                        continue
                    rev = _yfv(inc, i, "total revenue")
                    if not rev:
                        continue
                    yearly[fy] = {
                        "revenue":            rev,
                        "total_expenses":     _yfv(inc, i, "total expenses"),
                        "pretax_income":      _yfv(inc, i, "pretax"),
                        "tax_provision":      _yfv(inc, i, "tax", "provision"),
                        "net_income":         _yfv(inc, i, "net income"),
                        "eps":                _yfv(inc, i, "basic eps")
                                              or _yfv(inc, i, "diluted eps"),
                        "depreciation":       _yfv(inc, i, "reconciled", "depreciation")
                                              or _yfv(cf, i, "depreciation"),
                        "interest_expense":   _yfv(inc, i, "interest", "expense"),
                        "current_assets":     _yfv(bal, i, "current assets"),
                        "current_liabilities": _yfv(bal, i, "current liabilities"),
                        "cash":               _yfv(bal, i, "cash", "equivalents"),
                        "net_ppe":            _yfv(bal, i, "net ppe"),
                        "total_equity":       _yfv(bal, i, "stockholders equity"),
                        "cpltd":              _yfv(bal, i, "current", "long term debt")
                                              or _yfv(bal, i, "current debt"),
                        "total_debt":         _yfv(bal, i, "total debt"),
                        "minority_interest":  _yfv(bal, i, "minority interest"),
                        "long_term_investments": _yfv(bal, i, "long term", "investment"),
                        "operating_cash_flow": _yfv(cf, i, "operating cash flow"),
                        "capex":              abs(_yfv(cf, i, "capital expenditure") or 0) or None,
                        "_facts_found":       0,
                    }
                    added.append(fy)
            if added:
                print(f"  yfinance merge: added FY{sorted(added, reverse=True)}")
        except Exception as e:
            print(f"  yfinance merge skipped ({e})")

    got_years = sorted(yearly.keys(), reverse=True)
    cols = [str(y) for y in got_years]

    def row(field):
        return [yearly[y].get(field) for y in got_years]

    income_df = pd.DataFrame({
        "Total Revenue":           row("revenue"),
        "Total Expenses":          row("total_expenses"),
        "Pretax Income":           row("pretax_income"),
        "Tax Provision":           row("tax_provision"),
        "Net Income":              row("net_income"),
        "Interest Expense":        row("interest_expense"),
        "Reconciled Depreciation": row("depreciation"),
        "Operating Income":        [None] * len(got_years),
        "EBIT":                    [None] * len(got_years),
    }, index=cols).T

    # If the latest year lacks BS facts, try the PDF+Groq fallback for it
    latest = got_years[0]
    if not yearly[latest].get("current_assets"):
        print(f"  {symbol}: latest XBRL lacks BS — trying PDF fallback...")
        bs = _pdf_groq_balance_sheet(symbol)
        if bs:
            for k in ("current_assets", "current_liabilities", "cash", "cpltd",
                      "net_ppe", "long_term_investments", "minority_interest",
                      "total_debt", "total_equity", "depreciation", "capex",
                      "operating_cash_flow"):
                if yearly[latest].get(k) is None and bs.get(k) is not None:
                    yearly[latest][k] = bs[k]
            print(f"  PDF fallback merged")

    balance_df = pd.DataFrame({
        "Total Current Assets":              row("current_assets"),
        "Total Current Liabilities":         row("current_liabilities"),
        "Cash And Cash Equivalents":         row("cash"),
        "Current Portion Of Long Term Debt": row("cpltd"),
        "Net PPE":                           row("net_ppe"),
        "Long Term Investments":             row("long_term_investments"),
        "Minority Interest":                 row("minority_interest"),
        "Total Debt":                        row("total_debt"),
        "Total Stockholders Equity":         row("total_equity"),
    }, index=cols).T

    cashflow_df = pd.DataFrame({
        "Depreciation Amortization": row("depreciation"),
        "Capital Expenditure":       row("capex"),
        "Operating Cash Flow":       row("operating_cash_flow"),
    }, index=cols).T

    # ── Company info ────────────────────────────────────────────────────────
    L = yearly[latest]

    # EPS/shares derivation: the latest year is often a yfinance-merged one
    # whose EPS may be missing — scan newest→oldest for the first year that
    # has BOTH eps and net_income, and derive shares from that year.
    eps_best = None
    shares   = None
    for y in got_years:
        ye = yearly[y]
        if ye.get("eps") and ye.get("net_income"):
            try:
                cand = ye["net_income"] / ye["eps"]
                if 1e5 < abs(cand) < 1e12:
                    eps_best = ye["eps"]
                    shares   = cand
                    break
            except ZeroDivisionError:
                continue
    if eps_best is None:
        # keep the newest EPS even if shares couldn't be derived
        for y in got_years:
            if yearly[y].get("eps"):
                eps_best = yearly[y]["eps"]
                break

    sector, industry = fetch_nse_sector(symbol)
    info = {
        "longName":          symbol,
        "sector":            sector,
        "industry":          industry,
        "sharesOutstanding": shares,
        "beta":              1.0,
        "totalDebt":         L.get("total_debt"),
        "totalCash":         L.get("cash"),
        "bookValue":         None,
        "trailingEps":       eps_best,
        "_cik":              None,
    }
    if shares:
        eq = None
        for y in got_years:
            if yearly[y].get("total_equity"):
                eq = yearly[y]["total_equity"]
                break
        if eq:
            info["bookValue"] = eq / shares

    src = "nse_xbrl_pipeline" if all(
        yearly[y].get("_facts_found", 0) > 0 for y in got_years
    ) else "nse_xbrl+yfinance"
    upsert_company(info, store_ticker, "india", src)
    upsert_statements(store_ticker, income_df, balance_df, cashflow_df)
    print(f"  ✅ {symbol}: stored ({len(got_years)} yrs via NSE XBRL)")
    return True


# ── Standalone test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
    print(f"Fetching annual filings for {sym}...")
    filings = fetch_annual_filings(sym)
    print(f"  Years found: {sorted(filings.keys(), reverse=True)}")
    for fy in sorted(filings.keys(), reverse=True)[:3]:
        fi = filings[fy]
        print(f"\nFY{fy} ({'Consolidated' if fi['consolidated'] else 'Standalone'}) — downloading XBRL...")
        resp = _nse_get(fi["xbrl"])
        d = extract_financials_from_xbrl(resp.content, _parse_date(fi["to_date"]))
        print(f"  facts found:  {d['_facts_found']}")
        print(f"  revenue:      {d['revenue']}")
        print(f"  expenses:     {d['total_expenses']}")
        print(f"  PBT:          {d['pretax_income']}")
        print(f"  PAT:          {d['net_income']}")
        print(f"  EPS:          {d['eps']}")
        print(f"  curr assets:  {d['current_assets']}")
        print(f"  curr liab:    {d['current_liabilities']}")
        print(f"  net PPE:      {d['net_ppe']}")
        print(f"  total debt:   {d['total_debt']}")
        print(f"  capex:        {d['capex']}")
        time.sleep(1.0)
