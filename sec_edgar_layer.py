"""
SEC EDGAR Data Layer for MiniTradeIQ
=====================================
Pulls US company financial statements directly from the SEC's official
EDGAR XBRL API — completely free, no API key, no rate-limit blocking like
yfinance. This is the SAME source FMP and other data vendors use.

Returns data in the SAME yfinance-compatible shape (income_df, balance_df,
cashflow_df + info dict) so dcf_endpoint.py / convergence_endpoint.py don't
need to change their find_row() logic.

Rate limit: SEC allows 10 req/sec. We need ~1 call per company (companyfacts
returns everything), so this is extremely efficient.

Requirements:
- A descriptive User-Agent header is MANDATORY (SEC blocks requests without it)
- CIK must be 10-digit zero-padded

Usage:
    info, income_df, balance_df, cashflow_df = get_sec_company_data("AAPL")
"""

import time
import httpx
import pandas as pd

# SEC REQUIRES a descriptive User-Agent with contact info, or it blocks you.
# Replace with your real email before production.
SEC_HEADERS = {
    "User-Agent": "yllodwrites04@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL   = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# SIC code ranges → sector (maps SEC's industry classification to our sectors)
def _sic_to_sector(sic: str) -> str:
    try:
        code = int(sic)
    except (ValueError, TypeError):
        return "Unknown"
    if 100   <= code <= 999:   return "Basic Materials"      # Agriculture
    if 1000  <= code <= 1499:  return "Energy"               # Mining
    if 1500  <= code <= 1799:  return "Industrials"          # Construction
    if 2000  <= code <= 2199:  return "Consumer Defensive"   # Food
    if 2200  <= code <= 2799:  return "Consumer Cyclical"    # Textiles/Apparel
    if 2800  <= code <= 2899:  return "Basic Materials"      # Chemicals
    if 2900  <= code <= 2999:  return "Energy"               # Petroleum
    if 3000  <= code <= 3999:  return "Industrials"          # Manufacturing
    if 4000  <= code <= 4899:  return "Industrials"          # Transport/Comms
    if 4900  <= code <= 4999:  return "Utilities"            # Utilities
    if 5000  <= code <= 5199:  return "Consumer Cyclical"    # Wholesale
    if 5200  <= code <= 5999:  return "Consumer Cyclical"    # Retail
    if 6000  <= code <= 6799:  return "Financial Services"   # Finance
    if 7000  <= code <= 8999:  return "Technology"           # Services/Tech
    return "Unknown"

# ── Ticker → CIK cache (loaded once, reused) ──────────────────────────────────
_TICKER_CIK_MAP: dict = {}
_CIK_CACHE: dict = {}
_CIK_CACHE_TTL = 86400  # 24 hours — financials change quarterly at most


def _load_ticker_cik_map():
    """Load and cache the SEC's ticker→CIK mapping (one-time, persisted to disk)."""
    global _TICKER_CIK_MAP
    if _TICKER_CIK_MAP:
        return _TICKER_CIK_MAP

    # Try loading from disk cache first (avoids re-fetching the 10MB file)
    import os, json
    cache_file = "/tmp/sec_ticker_cik_map.json"
    try:
        if os.path.exists(cache_file):
            age = time.time() - os.path.getmtime(cache_file)
            if age < 7 * 86400:  # 7 days
                with open(cache_file, "r") as f:
                    _TICKER_CIK_MAP = json.load(f)
                if _TICKER_CIK_MAP:
                    return _TICKER_CIK_MAP
    except Exception:
        pass

    headers = {"User-Agent": SEC_HEADERS["User-Agent"]}
    last_err = None
    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(2 ** attempt)  # 2s, 4s backoff
            with httpx.Client(timeout=20.0) as client:
                resp = client.get(SEC_TICKERS_URL, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                for entry in data.values():
                    ticker = entry.get("ticker", "").upper()
                    cik    = entry.get("cik_str")
                    title  = entry.get("title", "")
                    if ticker and cik:
                        _TICKER_CIK_MAP[ticker] = {"cik": cik, "title": title}
                # Persist to disk
                try:
                    with open(cache_file, "w") as f:
                        json.dump(_TICKER_CIK_MAP, f)
                except Exception:
                    pass
                return _TICKER_CIK_MAP
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = str(e)

    raise RuntimeError(f"SEC ticker map fetch failed: {last_err}")


def ticker_to_cik(ticker: str) -> str | None:
    """Resolve a ticker symbol to its 10-digit zero-padded CIK."""
    mapping = _load_ticker_cik_map()
    entry   = mapping.get(ticker.upper())
    if not entry:
        return None
    return str(entry["cik"]).zfill(10)


def company_title(ticker: str) -> str:
    mapping = _load_ticker_cik_map()
    entry   = mapping.get(ticker.upper())
    return entry["title"] if entry else ticker.upper()


# ── Fetch company facts (all XBRL data in one call) ────────────────────────────

def _fetch_company_facts(cik: str) -> dict:
    cache_key = f"sec_facts:{cik}"
    cached = _CIK_CACHE.get(cache_key)
    if cached and (time.time() - cached["time"]) < _CIK_CACHE_TTL:
        return cached["data"]

    url = SEC_FACTS_URL.format(cik=cik)
    time.sleep(0.12)  # respect 10 req/sec limit
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=SEC_HEADERS)
    if resp.status_code != 200:
        raise RuntimeError(f"SEC companyfacts failed for CIK {cik}: {resp.status_code}")

    data = resp.json()
    _CIK_CACHE[cache_key] = {"data": data, "time": time.time()}
    return data


# ── Extract a concept's annual values ──────────────────────────────────────────

def _get_concept_annual(facts: dict, *tags, unit: str = "USD") -> dict:
    """
    Extract annual (FY) values for the first matching XBRL tag.
    Returns {year: value} dict keyed by the PERIOD END YEAR (reliable),
    not the 'fy' filing-year field (unreliable — a 10-K tags multiple
    years of comparatives with the same fy).
    Tries multiple tag names since companies use different ones.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    from datetime import datetime

    for tag in tags:
        if tag not in us_gaap:
            continue
        units = us_gaap[tag].get("units", {})
        unit_data = units.get(unit) or (list(units.values())[0] if units else [])

        annual = {}
        for item in unit_data:
            form  = item.get("form", "")
            val   = item.get("val")
            start = item.get("start")
            end   = item.get("end")

            if form != "10-K" or val is None or not end:
                continue

            # Period must be a full year (~365 days) — skip quarterly chunks
            if start and end:
                try:
                    d0 = datetime.fromisoformat(start)
                    d1 = datetime.fromisoformat(end)
                    days = (d1 - d0).days
                    if days < 300 or days > 400:
                        continue
                except Exception:
                    continue
            else:
                continue

            # Key by the period END YEAR — this is the actual data year
            try:
                period_year = datetime.fromisoformat(end).year
            except Exception:
                continue

            # Keep the value; later filings overwrite earlier (latest restatement wins)
            annual[period_year] = val

        if annual:
            return dict(sorted(annual.items(), key=lambda x: x[0], reverse=True))

    return {}


def _get_concept_instant(facts: dict, *tags, unit: str = "USD") -> dict:
    """
    Extract instantaneous (balance sheet) values — point-in-time.
    Keyed by the period END YEAR (reliable), not the fy field.
    Returns {year: value} most recent first.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    from datetime import datetime

    for tag in tags:
        if tag not in us_gaap:
            continue
        units = us_gaap[tag].get("units", {})
        unit_data = units.get(unit) or (list(units.values())[0] if units else [])

        annual = {}
        for item in unit_data:
            form = item.get("form", "")
            val  = item.get("val")
            end  = item.get("end")
            if form != "10-K" or val is None or not end:
                continue
            try:
                period_year = datetime.fromisoformat(end).year
            except Exception:
                continue
            # Latest filing wins for each year-end
            annual[period_year] = val

        if annual:
            return dict(sorted(annual.items(), key=lambda x: x[0], reverse=True))

    return {}


# ── Build yfinance-shaped DataFrames ───────────────────────────────────────────

def _series_to_columns(*concept_dicts, limit=6):
    """Find common fiscal years across concepts, return sorted year list."""
    all_years = set()
    for d in concept_dicts:
        all_years.update(d.keys())
    years = sorted(all_years, reverse=True)[:limit]
    return years


def _build_dataframes(facts: dict):
    # ── Income statement concepts ─────────────────────────────────────────────
    revenue = _get_concept_annual(
        facts,
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    )
    cost_revenue = _get_concept_annual(facts, "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold")
    # CostsAndExpenses = TOTAL costs incl COGS + OpEx (what we want for NOP).
    # OperatingExpenses alone EXCLUDES COGS for most companies.
    costs_and_expenses = _get_concept_annual(facts, "CostsAndExpenses")
    operating_expenses = _get_concept_annual(facts, "OperatingExpenses")
    operating_income = _get_concept_annual(facts, "OperatingIncomeLoss")
    pretax       = _get_concept_annual(facts, "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments")
    tax_expense  = _get_concept_annual(facts, "IncomeTaxExpenseBenefit")
    interest_exp = _get_concept_annual(facts, "InterestExpense", "InterestExpenseDebt")
    net_income   = _get_concept_annual(facts, "NetIncomeLoss")
    depreciation = _get_concept_annual(facts, "DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortization")

    # ── Balance sheet concepts (instantaneous) ────────────────────────────────
    current_assets      = _get_concept_instant(facts, "AssetsCurrent")
    current_liabilities = _get_concept_instant(facts, "LiabilitiesCurrent")
    cash                = _get_concept_instant(facts, "CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")
    short_term_debt     = _get_concept_instant(facts, "LongTermDebtCurrent", "DebtCurrent")
    net_ppe             = _get_concept_instant(facts, "PropertyPlantAndEquipmentNet")
    long_term_debt      = _get_concept_instant(facts, "LongTermDebtNoncurrent", "LongTermDebt")
    total_equity        = _get_concept_instant(facts, "StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
    long_term_inv       = _get_concept_instant(facts, "LongTermInvestments")
    minority_interest   = _get_concept_instant(facts, "MinorityInterest")

    # ── Cash flow concepts ────────────────────────────────────────────────────
    capex               = _get_concept_annual(facts, "PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")
    operating_cf        = _get_concept_annual(facts, "NetCashProvidedByUsedInOperatingActivities")

    # ── Determine common years ────────────────────────────────────────────────
    years = _series_to_columns(revenue, current_assets, net_income, limit=6)
    if not years:
        raise RuntimeError("No annual financial data found in SEC filings.")

    col_labels = [str(y) for y in years]

    def row(concept_dict):
        return [concept_dict.get(y) for y in years]

    # ── Build "Total Expenses" = total costs including COGS ────────────────────
    # Priority:
    #   1. CostsAndExpenses (already total incl COGS + OpEx)
    #   2. CostOfRevenue + OperatingExpenses (sum them)
    #   3. Revenue - OperatingIncome (derive from EBIT)
    total_expenses_row = []
    for y in years:
        if costs_and_expenses.get(y) is not None:
            total_expenses_row.append(costs_and_expenses[y])
        elif cost_revenue.get(y) is not None or operating_expenses.get(y) is not None:
            cogs = cost_revenue.get(y, 0) or 0
            opex = operating_expenses.get(y, 0) or 0
            total_expenses_row.append(cogs + opex)
        elif revenue.get(y) is not None and operating_income.get(y) is not None:
            # Total Expenses = Revenue - Operating Income (EBIT)
            total_expenses_row.append(revenue[y] - operating_income[y])
        else:
            total_expenses_row.append(None)

    # Income statement DataFrame (index names match find_row searches)
    income_data = {
        "Total Revenue":           row(revenue),
        "Cost Of Revenue":         row(cost_revenue),
        "Total Expenses":          total_expenses_row,
        "Operating Income":        row(operating_income),
        "EBIT":                    row(operating_income),
        "Pretax Income":           row(pretax),
        "Tax Provision":           row(tax_expense),
        "Interest Expense":        row(interest_exp),
        "Net Income":              row(net_income),
        "Reconciled Depreciation": row(depreciation),
    }
    income_df = pd.DataFrame(income_data, index=col_labels).T

    # ── Total Debt = Long Term Debt + Current/Short Term Debt ──────────────────
    total_debt_row = []
    for y in years:
        ltd = long_term_debt.get(y, 0) or 0
        std = short_term_debt.get(y, 0) or 0
        total_debt_row.append(ltd + std if (ltd or std) else None)

    # Balance sheet DataFrame
    balance_data = {
        "Total Current Assets":              row(current_assets),
        "Total Current Liabilities":         row(current_liabilities),
        "Cash And Cash Equivalents":         row(cash),
        "Current Portion Of Long Term Debt": row(short_term_debt),
        "Net PPE":                           row(net_ppe),
        "Long Term Investments":             row(long_term_inv),
        "Minority Interest":                 row(minority_interest),
        "Total Debt":                        total_debt_row,
        "Total Stockholders Equity":         row(total_equity),
    }
    balance_df = pd.DataFrame(balance_data, index=col_labels).T

    # Cash flow DataFrame
    cashflow_data = {
        "Depreciation Amortization": row(depreciation),
        "Capital Expenditure":       row(capex),
        "Operating Cash Flow":       row(operating_cf),
    }
    cashflow_df = pd.DataFrame(cashflow_data, index=col_labels).T

    return income_df, balance_df, cashflow_df, net_income, total_equity


# ── Public entry point ──────────────────────────────────────────────────────────

def get_sec_company_data(ticker: str):
    """
    Returns (info, income_df, balance_df, cashflow_df) for a US ticker,
    using SEC EDGAR as the data source. Raises if ticker is not a US filer.

    Note: SEC provides financial statements but NOT live price/market cap.
    Those still need to come from a price source (yfinance quote, or a
    cheap price-only API). The `info` dict here fills financial fields and
    leaves price fields as None for the caller to populate.
    """
    cik = ticker_to_cik(ticker)
    if not cik:
        raise RuntimeError(f"{ticker} not found in SEC EDGAR (not a US filer?).")

    facts = _fetch_company_facts(cik)
    income_df, balance_df, cashflow_df, net_income, total_equity = _build_dataframes(facts)

    # Most recent year's values for the info dict
    def latest(df, row_name):
        try:
            return float(df.loc[row_name].iloc[0])
        except Exception:
            return None

    total_debt = latest(balance_df, "Total Debt") or 0.0
    total_cash = latest(balance_df, "Cash And Cash Equivalents") or 0.0
    equity_latest = latest(balance_df, "Total Stockholders Equity")

    # Shares outstanding from SEC (dei taxonomy)
    shares = _get_shares_outstanding(facts)

    # Sector from SEC submissions (SIC code) — needed for Relative Valuation
    sector = "Unknown"
    try:
        sub_url = SEC_SUBMISSIONS_URL.format(cik=cik)
        time.sleep(0.12)
        with httpx.Client(timeout=20.0) as client:
            sub_resp = client.get(sub_url, headers=SEC_HEADERS)
        if sub_resp.status_code == 200:
            sub_data = sub_resp.json()
            sic      = sub_data.get("sic", "")
            sector   = _sic_to_sector(sic)
    except Exception:
        pass

    info = {
        # Financial fields from SEC
        "longName":           company_title(ticker),
        "totalDebt":          total_debt,
        "totalCash":          total_cash,
        "netIncome":          latest(income_df, "Net Income"),
        "sharesOutstanding":  shares,
        # Price fields — filled by _enrich_with_price() below
        "currentPrice":       None,
        "regularMarketPrice": None,
        "beta":               None,
        "marketCap":          None,
        "trailingEps":        (latest(income_df, "Net Income") / shares) if (shares and latest(income_df, "Net Income")) else None,
        "trailingPE":         None,
        "priceToBook":        None,
        "bookValue":          (equity_latest / shares) if (shares and equity_latest) else None,
        "returnOnEquity":     (latest(income_df, "Net Income") / equity_latest) if (equity_latest and latest(income_df, "Net Income")) else None,
        "sector":             sector,
        "industry":           sector,
        "_data_source":       "sec_edgar",
        "_cik":               cik,
    }

    # Enrich with live price + beta + sector from a price source
    info = _enrich_with_price(ticker, info)

    return info, income_df, balance_df, cashflow_df


def _get_shares_outstanding(facts: dict):
    """Get shares outstanding from SEC dei taxonomy or us-gaap."""
    dei = facts.get("facts", {}).get("dei", {})
    for tag in ["EntityCommonStockSharesOutstanding"]:
        if tag in dei:
            units = dei[tag].get("units", {})
            shares_data = units.get("shares", [])
            if shares_data:
                # Most recent value
                latest = sorted(shares_data, key=lambda x: x.get("end", ""), reverse=True)
                for item in latest:
                    if item.get("val"):
                        return float(item["val"])

    # Fallback to us-gaap
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in ["CommonStockSharesOutstanding", "CommonStockSharesIssued"]:
        if tag in us_gaap:
            units = us_gaap[tag].get("units", {})
            shares_data = units.get("shares", [])
            if shares_data:
                latest = sorted(shares_data, key=lambda x: x.get("end", ""), reverse=True)
                for item in latest:
                    if item.get("val"):
                        return float(item["val"])
    return None


def _enrich_with_price(ticker: str, info: dict) -> dict:
    """
    SEC has no live price. Pull current price + market cap from yfinance
    fast_info — a lightweight, cached call that rarely rate-limits (it does
    NOT hit the heavy /quoteSummary endpoint that .info uses).

    We deliberately AVOID t.info here since that's the heavy call that gets
    rate-limited. Beta defaults to 1.0 if unavailable; sector stays Unknown.
    Price/PE are derived from fast_info + SEC-derived EPS.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        # fast_info is a lightweight object (NOT a dict) — use getattr only
        fast = t.fast_info

        price = None
        for attr in ("last_price", "lastPrice", "regular_market_price"):
            try:
                v = getattr(fast, attr, None)
                if v:
                    price = float(v)
                    break
            except Exception:
                continue

        if price:
            info["currentPrice"]       = price
            info["regularMarketPrice"] = price

            # Market cap from fast_info
            try:
                mc = getattr(fast, "market_cap", None)
                if mc:
                    info["marketCap"] = float(mc)
            except Exception:
                pass

            # Derive market cap from price × shares if not available
            if not info.get("marketCap") and info.get("sharesOutstanding"):
                info["marketCap"] = price * info["sharesOutstanding"]

            # Derive trailing P/E from price and SEC-derived EPS
            if info.get("trailingEps") and info["trailingEps"] > 0:
                info["trailingPE"] = price / info["trailingEps"]

            # Derive Price/Book from price and SEC-derived book value
            if info.get("bookValue") and info["bookValue"] > 0:
                info["priceToBook"] = price / info["bookValue"]

        # Beta: default to 1.0 (avoids the heavy .info call).
        # A more accurate beta can be computed from price history later.
        if info.get("beta") is None:
            info["beta"] = 1.0

    except Exception:
        # yfinance failed entirely — financials still work; default beta
        if info.get("beta") is None:
            info["beta"] = 1.0

    return info
