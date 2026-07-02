"""
MiniTradeIQ — Own Indian Financials Pipeline
=============================================
Your own data source for Indian stocks, replacing yfinance dependence.

Architecture (mirrors the SEC EDGAR approach for US):
  1. INCOME STATEMENT  → NSE's structured financial-results API (clean JSON)
  2. BALANCE SHEET +
     CASH FLOW         → annual results PDF → text → Groq LLM extraction
  3. Both written to YOUR Postgres store via data_store.py

Run via ingest.py — this is a BACKGROUND ingestion source, not a live
user-request path. Users always read from the store.

Requirements (add to requirements.txt):
    httpx
    pdfplumber

Env vars:
    GROQ_API_KEY   (already set for AI Verdict)

NOTE: NSE requires browser-like headers + a cookie handshake (visit the
homepage first). Field names in NSE responses occasionally change — the
parsers below try multiple candidate keys and log what they find, so the
first live run tells you exactly what (if anything) to adjust.
"""

import os
import io
import re
import json
import time
import httpx

# ── NSE session handling ────────────────────────────────────────────────────────
# NSE blocks plain programmatic requests. The standard technique:
# 1) create a client with full browser headers
# 2) GET the homepage once to receive cookies
# 3) then call the JSON APIs with those cookies

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
_NSE_SESSION_TTL = 300  # refresh cookies every 5 minutes


def _nse():
    """Get an NSE client with valid cookies (handshake with homepage)."""
    global _nse_client, _nse_client_time
    now = time.time()
    if _nse_client is not None and (now - _nse_client_time) < _NSE_SESSION_TTL:
        return _nse_client

    client = httpx.Client(timeout=25.0, headers=NSE_HEADERS, follow_redirects=True)
    # Cookie handshake — required before API calls work
    client.get("https://www.nseindia.com")
    time.sleep(0.5)
    _nse_client = client
    _nse_client_time = now
    return client


def _nse_get_json(url: str, retries: int = 3):
    last_err = None
    for attempt in range(retries):
        try:
            if attempt > 0:
                time.sleep(2 * attempt)
                # force a fresh cookie handshake on retry
                global _nse_client
                _nse_client = None
            client = _nse()
            resp = client.get(url)
            if resp.status_code == 200:
                return resp.json()
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"NSE request failed after {retries} tries: {last_err} — {url}")


# ── 1) INCOME STATEMENT — NSE structured financial results ─────────────────────

def _num(v):
    """NSE returns numbers as strings with commas / '-' for nil."""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "--", "NA", "N.A."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_nse_income_statements(symbol: str, period: str = "Annual") -> list:
    """
    Fetch structured P&L results from NSE for a symbol.
    Returns a list of dicts (most recent first):
      { fiscal_year, revenue, total_expenses, pretax_income,
        tax_provision, net_income, eps, audited, period_end }

    NSE results are in ₹ LAKHS for most filers — we convert to absolute ₹
    (multiply by 1e5) so units match the rest of the store.
    """
    url = (f"https://www.nseindia.com/api/corporates-financial-results"
           f"?index=equities&symbol={symbol}&period={period}")
    data = _nse_get_json(url)

    rows = data if isinstance(data, list) else data.get("data", [])
    results = []

    for r in rows:
        # NSE field names vary across result vintages — try candidates
        def g(*keys):
            for k in keys:
                if k in r and r[k] not in (None, "", "-"):
                    return r[k]
            return None

        income   = _num(g("income", "totalIncome", "re_total_income", "ti"))
        expend   = _num(g("expenditure", "totalExpenditure", "re_total_expenditure", "te"))
        pbt      = _num(g("proLossBefTax", "profitBeforeTax", "re_pro_loss_bef_tax", "pbt"))
        tax      = _num(g("tax", "taxExpense", "re_tax", "taxAmt"))
        pat      = _num(g("proLossAftTax", "reProLossAftTax", "profitAfterTax",
                          "re_pro_loss_aft_tax", "pat", "netProfitLoss"))
        eps      = _num(g("eps", "basicEPS", "re_basic_eps", "epsAfterExtraOrdinary"))
        to_date  = g("toDate", "to_date", "reToDate", "period_ended", "relatingTo")
        audited  = g("audited", "re_audited", "auditedUnaudited")

        # Derive tax if missing
        if tax is None and pbt is not None and pat is not None:
            tax = pbt - pat

        # Fiscal year from the period-end date (e.g. "31-Mar-2025" → 2025)
        fy = None
        if to_date:
            m = re.search(r"(\d{4})", str(to_date))
            if m:
                fy = int(m.group(1))

        if fy is None or income is None:
            continue

        LAKH = 1e5  # NSE results are reported in lakhs
        results.append({
            "fiscal_year":    fy,
            "revenue":        income * LAKH,
            "total_expenses": expend * LAKH if expend is not None else None,
            "pretax_income":  pbt * LAKH if pbt is not None else None,
            "tax_provision":  tax * LAKH if tax is not None else None,
            "net_income":     pat * LAKH if pat is not None else None,
            "eps":            eps,   # per-share, no scaling
            "period_end":     to_date,
            "audited":        audited,
        })

    # Deduplicate by fiscal year (keep first = most recent filing per FY)
    seen, unique = set(), []
    for r in sorted(results, key=lambda x: x["fiscal_year"], reverse=True):
        if r["fiscal_year"] not in seen:
            seen.add(r["fiscal_year"])
            unique.append(r)
    return unique


# ── 2) BALANCE SHEET + CASH FLOW — results PDF → Groq extraction ────────────────

def fetch_latest_results_pdf_url(symbol: str) -> str | None:
    """
    Find the latest annual/half-yearly financial-results PDF from NSE
    corporate announcements (these contain the balance sheet).
    """
    url = (f"https://www.nseindia.com/api/corporate-announcements"
           f"?index=equities&symbol={symbol}")
    try:
        data = _nse_get_json(url)
    except Exception:
        return None

    rows = data if isinstance(data, list) else data.get("data", [])
    for r in rows:
        desc = (str(r.get("desc", "")) + " " + str(r.get("attchmntText", ""))).lower()
        attach = r.get("attchmntFile") or r.get("attachmentFile") or r.get("pdfLink")
        if not attach:
            continue
        if "financial result" in desc or "financial results" in desc:
            if str(attach).lower().endswith(".pdf"):
                return attach
    return None


def extract_pdf_text(pdf_url: str, max_pages: int = 12) -> str:
    """Download a results PDF and extract text from the first N pages."""
    import pdfplumber
    client = _nse()
    resp = client.get(pdf_url)
    if resp.status_code != 200:
        raise RuntimeError(f"PDF download failed: HTTP {resp.status_code}")

    text_parts = []
    with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
        for page in pdf.pages[:max_pages]:
            t = page.extract_text() or ""
            text_parts.append(t)
    return "\n".join(text_parts)


def _focus_balance_sheet_section(full_text: str, window: int = 9000) -> str:
    """Trim the PDF text to the region around the balance sheet to keep
    the LLM prompt small and focused."""
    lower = full_text.lower()
    for marker in ("statement of assets and liabilities", "balance sheet", "assets"):
        idx = lower.find(marker)
        if idx != -1:
            start = max(0, idx - 500)
            return full_text[start:start + window]
    return full_text[:window]


GROQ_EXTRACT_PROMPT = """You are a precise financial data extractor. Below is text
extracted from an Indian listed company's financial results PDF (SEBI format).
Extract the MOST RECENT period's standalone-or-consolidated (prefer consolidated)
balance sheet and cash flow figures.

IMPORTANT:
- Figures in these PDFs are usually in ₹ lakhs or ₹ crores — detect which from
  the document header and return ALL monetary values converted to ABSOLUTE RUPEES
  (lakh = ×100000, crore = ×10000000).
- If a value is genuinely not present, use null. Do NOT guess.
- Respond with ONLY a JSON object, no markdown, no commentary.

{{
  "unit_detected": "lakhs | crores | rupees",
  "fiscal_year": <4-digit year of period end>,
  "current_assets": <number|null>,
  "current_liabilities": <number|null>,
  "cash_and_equivalents": <number|null>,
  "current_portion_lt_debt": <number|null>,
  "net_ppe": <number|null>,
  "long_term_investments": <number|null>,
  "minority_interest": <number|null>,
  "total_debt": <number|null>,
  "total_equity": <number|null>,
  "depreciation": <number|null>,
  "capex": <number|null>,
  "operating_cash_flow": <number|null>
}}

DOCUMENT TEXT:
{doc}"""


def groq_extract_balance_sheet(pdf_text: str) -> dict | None:
    """Send the balance-sheet section to Groq and parse structured JSON back."""
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set — needed for PDF extraction.")

    focused = _focus_balance_sheet_section(pdf_text)
    prompt  = GROQ_EXTRACT_PROMPT.format(doc=focused)

    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "max_tokens": 800,
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "You extract financial data. Respond with valid JSON only."},
                    {"role": "user",   "content": prompt},
                ],
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Groq extraction failed: {resp.status_code} {resp.text[:200]}")

    text = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    try:
        parsed = json.loads(text)
    except Exception:
        return None

    # Sanity checks — reject obviously broken extractions rather than
    # poisoning the store with garbage
    ca, cl = parsed.get("current_assets"), parsed.get("current_liabilities")
    if ca is not None and (ca < 0 or ca > 1e16):
        return None
    if cl is not None and (cl < 0 or cl > 1e16):
        return None
    return parsed


# ── 3) Full ingestion for one Indian company ────────────────────────────────────

def ingest_india_own(ticker: str, include_balance_sheet: bool = True) -> bool:
    """
    Full own-pipeline ingestion for one NSE symbol:
      P&L from NSE structured API + BS/CF from results PDF via Groq.
    Writes to the Postgres store. Returns True on success.
    """
    from data_store import upsert_company, upsert_statements
    import pandas as pd

    symbol = ticker.upper().replace(".NS", "")
    store_ticker = symbol + ".NS"

    # ── Income statements (structured, multiple years) ─────────────────────────
    income_rows = fetch_nse_income_statements(symbol, period="Annual")
    if not income_rows:
        print(f"  ⚠ {symbol}: no NSE results found")
        return False

    years = [r["fiscal_year"] for r in income_rows][:6]
    col_labels = [str(y) for y in years]
    by_year = {r["fiscal_year"]: r for r in income_rows}

    def irow(field):
        return [by_year.get(y, {}).get(field) for y in years]

    income_df = pd.DataFrame({
        "Total Revenue":           irow("revenue"),
        "Total Expenses":          irow("total_expenses"),
        "Pretax Income":           irow("pretax_income"),
        "Tax Provision":           irow("tax_provision"),
        "Net Income":              irow("net_income"),
        # Not available from NSE structured results — filled from PDF below
        "Operating Income":        [None] * len(years),
        "EBIT":                    [None] * len(years),
        "Interest Expense":        [None] * len(years),
        "Reconciled Depreciation": [None] * len(years),
    }, index=col_labels).T

    # ── Balance sheet + cash flow from latest results PDF ──────────────────────
    bs = None
    if include_balance_sheet:
        try:
            pdf_url = fetch_latest_results_pdf_url(symbol)
            if pdf_url:
                pdf_text = extract_pdf_text(pdf_url)
                bs = groq_extract_balance_sheet(pdf_text)
                if bs:
                    print(f"  📄 {symbol}: PDF extracted (unit: {bs.get('unit_detected')})")
        except Exception as e:
            print(f"  ⚠ {symbol}: PDF extraction failed ({e}) — storing P&L only")

    bs_year = str((bs or {}).get("fiscal_year") or years[0])
    balance_df = pd.DataFrame({
        "Total Current Assets":              [(bs or {}).get("current_assets")],
        "Total Current Liabilities":         [(bs or {}).get("current_liabilities")],
        "Cash And Cash Equivalents":         [(bs or {}).get("cash_and_equivalents")],
        "Current Portion Of Long Term Debt": [(bs or {}).get("current_portion_lt_debt")],
        "Net PPE":                           [(bs or {}).get("net_ppe")],
        "Long Term Investments":             [(bs or {}).get("long_term_investments")],
        "Minority Interest":                 [(bs or {}).get("minority_interest")],
        "Total Debt":                        [(bs or {}).get("total_debt")],
        "Total Stockholders Equity":         [(bs or {}).get("total_equity")],
    }, index=[bs_year]).T

    cashflow_df = pd.DataFrame({
        "Depreciation Amortization": [(bs or {}).get("depreciation")],
        "Capital Expenditure":       [(bs or {}).get("capex")],
        "Operating Cash Flow":       [(bs or {}).get("operating_cash_flow")],
    }, index=[bs_year]).T

    # ── Company info ────────────────────────────────────────────────────────────
    latest = income_rows[0]
    equity = (bs or {}).get("total_equity")
    info = {
        "longName":          symbol,
        "sector":            "Unknown",       # can enrich later
        "industry":          "Unknown",
        "sharesOutstanding": None,            # derive: PAT / EPS if both present
        "beta":              1.0,
        "totalDebt":         (bs or {}).get("total_debt"),
        "totalCash":         (bs or {}).get("cash_and_equivalents"),
        "bookValue":         None,
        "trailingEps":       latest.get("eps"),
        "_cik":              None,
    }
    if latest.get("net_income") and latest.get("eps"):
        try:
            shares = latest["net_income"] / latest["eps"]
            if 1e5 < shares < 1e12:  # sanity band
                info["sharesOutstanding"] = shares
                if equity:
                    info["bookValue"] = equity / shares
        except ZeroDivisionError:
            pass

    upsert_company(info, store_ticker, "india", "nse_own_pipeline")
    upsert_statements(store_ticker, income_df, balance_df, cashflow_df)
    print(f"  ✅ {symbol}: stored via own pipeline ({len(years)} yrs P&L"
          f"{', BS ✓' if bs else ', BS ✗'})")
    return True


# ── Quick standalone test ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
    print(f"Testing NSE structured results for {sym}...")
    rows = fetch_nse_income_statements(sym)
    for r in rows[:6]:
        print(f"  FY{r['fiscal_year']}: revenue={r['revenue']:.0f} "
              f"PAT={r['net_income'] or 0:.0f} EPS={r['eps']}")
    print(f"\nLooking for results PDF...")
    url = fetch_latest_results_pdf_url(sym)
    print(f"  PDF: {url}")
