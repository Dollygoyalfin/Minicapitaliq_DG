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
    "User-Agent": "MiniTradeIQ contact@minitradeiq.com",
    "Accept-Encoding": "gzip, deflate",
    "Host": "data.sec.gov",
}

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL   = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# ── Ticker → CIK cache (loaded once, reused) ──────────────────────────────────
_TICKER_CIK_MAP: dict = {}
_CIK_CACHE: dict = {}
_CIK_CACHE_TTL = 86400  # 24 hours — financials change quarterly at most


def _load_ticker_cik_map():
    """Load and cache the SEC's ticker→CIK mapping (one-time)."""
    global _TICKER_CIK_MAP
    if _TICKER_CIK_MAP:
        return _TICKER_CIK_MAP

    headers = {"User-Agent": "MiniTradeIQ contact@minitradeiq.com"}
    with httpx.Client(timeout=20.0) as client:
        resp = client.get(SEC_TICKERS_URL, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"SEC ticker map fetch failed: {resp.status_code}")

    data = resp.json()
    # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    for entry in data.values():
        ticker = entry.get("ticker", "").upper()
        cik    = entry.get("cik_str")
        title  = entry.get("title", "")
        if ticker and cik:
            _TICKER_CIK_MAP[ticker] = {"cik": cik, "title": title}

    return _TICKER_CIK_MAP


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
    Returns {fiscal_year: value} dict, most recent first.
    Tries multiple tag names since companies use different ones.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    for tag in tags:
        if tag not in us_gaap:
            continue
        units = us_gaap[tag].get("units", {})
        # Find the right unit (USD, shares, etc.)
        unit_data = units.get(unit) or (list(units.values())[0] if units else [])

        annual = {}
        for item in unit_data:
            # Only annual data: form 10-K, fp FY, and a full-year period
            form = item.get("form", "")
            fp   = item.get("fp", "")
            fy   = item.get("fy")
            val  = item.get("val")
            start = item.get("start")
            end   = item.get("end")

            if form != "10-K" or fy is None or val is None:
                continue

            # For flow items (revenue), ensure it's a ~full year period
            if start and end:
                try:
                    from datetime import datetime
                    d0 = datetime.fromisoformat(start)
                    d1 = datetime.fromisoformat(end)
                    days = (d1 - d0).days
                    if days < 300:   # skip quarterly chunks
                        continue
                except Exception:
                    pass

            # Keep the latest filing's value for each fiscal year
            if fy not in annual:
                annual[fy] = val

        if annual:
            # Return sorted most-recent-first
            return dict(sorted(annual.items(), key=lambda x: x[0], reverse=True))

    return {}


def _get_concept_instant(facts: dict, *tags, unit: str = "USD") -> dict:
    """
    Extract instantaneous (balance sheet) values — point-in-time, not period.
    Returns {fiscal_year: value} most recent first.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    for tag in tags:
        if tag not in us_gaap:
            continue
        units = us_gaap[tag].get("units", {})
        unit_data = units.get(unit) or (list(units.values())[0] if units else [])

        annual = {}
        for item in unit_data:
            form = item.get("form", "")
            fy   = item.get("fy")
            val  = item.get("val")
            if form != "10-K" or fy is None or val is None:
                continue
            # For balance sheet, take period-end value
            if fy not in annual:
                annual[fy] = val

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
    opex_total   = _get_concept_annual(facts, "OperatingExpenses", "CostsAndExpenses")
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
    total_debt_lt       = _get_concept_instant(facts, "LongTermDebtNoncurrent", "LongTermDebt")
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

    # Income statement DataFrame (index names match find_row searches)
    income_data = {
        "Total Revenue":           row(revenue),
        "Cost Of Revenue":         row(cost_revenue),
        "Total Expenses":          row(opex_total) if opex_total else [
            (cost_revenue.get(y, 0) or 0) for y in years
        ],
        "Operating Income":        row(operating_income),
        "EBIT":                    row(operating_income),
        "Pretax Income":           row(pretax),
        "Tax Provision":           row(tax_expense),
        "Interest Expense":        row(interest_exp),
        "Net Income":              row(net_income),
        "Reconciled Depreciation": row(depreciation),
    }
    income_df = pd.DataFrame(income_data, index=col_labels).T

    # Balance sheet DataFrame
    balance_data = {
        "Total Current Assets":              row(current_assets),
        "Total Current Liabilities":         row(current_liabilities),
        "Cash And Cash Equivalents":         row(cash),
        "Current Portion Of Long Term Debt": row(short_term_debt),
        "Net PPE":                           row(net_ppe),
        "Long Term Investments":             row(long_term_inv),
        "Minority Interest":                 row(minority_interest),
        "Total Debt":                        row(total_debt_lt),
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
        "sector":             "Unknown",
        "industry":           "Unknown",
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
    SEC has no live price. Pull current price, beta, market cap, sector
    from yfinance quote (a SINGLE lightweight call, not the heavy
    financials calls). Falls back gracefully if yfinance is rate-limited.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        fast = getattr(t, "fast_info", None)

        price = None
        if fast:
            price = getattr(fast, "last_price", None) or fast.get("lastPrice") if hasattr(fast, "get") else getattr(fast, "last_price", None)

        if price:
            info["currentPrice"]       = float(price)
            info["regularMarketPrice"] = float(price)
            try:
                info["marketCap"] = float(getattr(fast, "market_cap", None) or 0) or None
            except Exception:
                pass

        # Beta and sector need full .info — only fetch if price succeeded
        # (keeps it to one extra call, and only when not already rate-limited)
        if price:
            try:
                full = t.info
                info["beta"]           = full.get("beta", info.get("beta"))
                info["sector"]         = full.get("sector", "Unknown")
                info["industry"]       = full.get("industry", "Unknown")
                info["trailingPE"]     = full.get("trailingPE", info.get("trailingPE"))
                info["priceToBook"]    = full.get("priceToBook", info.get("priceToBook"))
                if not info.get("marketCap"):
                    info["marketCap"]  = full.get("marketCap")
                if not info.get("sharesOutstanding"):
                    info["sharesOutstanding"] = full.get("sharesOutstanding")
            except Exception:
                pass

        # Derive PE from price and EPS if not set
        if info.get("currentPrice") and info.get("trailingEps") and not info.get("trailingPE"):
            try:
                info["trailingPE"] = info["currentPrice"] / info["trailingEps"]
            except Exception:
                pass
    except Exception:
        # yfinance failed entirely — financials still work, price is None
        pass

    return info
