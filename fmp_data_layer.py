"""
FMP Data Layer for MiniTradeIQ
================================
Fetches financial statements from Financial Modeling Prep's /stable API
and converts them into the SAME shape that yfinance returns
(income_df, balance_df, cashflow_df as pandas DataFrames with line-item
names as the index, and an `info` dict) — so dcf_endpoint.py,
screener_endpoint.py etc. don't need to change their find_row() logic
at all. Only the data source changes.

Usage:
    info, income_df, balance_df, cashflow_df = get_company_data(
        ticker="AAPL", market="us", source="auto"
    )

source:
    "auto"     -> yfinance for India, FMP for US (the hybrid default)
    "yfinance" -> force yfinance regardless of market
    "fmp"      -> force FMP regardless of market (falls back to yfinance on failure)
"""

import os
import time
import httpx
import pandas as pd

FMP_BASE = "https://financialmodelingprep.com/stable"

# ── Simple in-memory cache ──────────────────────────────────────────────────────
# Avoids hitting yfinance / FMP repeatedly for the same ticker within a short
# window. This is the #1 fix for yfinance's "Too Many Requests" error, since
# DCF + Financials + Screener can all request the same ticker back-to-back.
_CACHE: dict = {}
_CACHE_TTL_SECONDS = 1800  # 30 minutes


def _cache_get(key: str):
    entry = _CACHE.get(key)
    if entry and (time.time() - entry["time"]) < _CACHE_TTL_SECONDS:
        return entry["data"]
    return None


def _cache_set(key: str, data):
    _CACHE[key] = {"data": data, "time": time.time()}


def _with_retry(func, max_retries: int = 3, base_delay: float = 1.5):
    """
    Retries a function with exponential backoff if it hits a rate-limit
    style error (Yahoo's "Too Many Requests", HTTP 429, etc).
    """
    last_err = None
    for attempt in range(max_retries):
        try:
            # Small base delay to be polite to Yahoo's servers
            if attempt > 0:
                time.sleep(base_delay * (2 ** (attempt - 1)))  # 1.5s, 3s, 6s
            return func()
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            is_rate_limit = (
                "too many requests" in msg
                or "429" in msg
                or "rate limit" in msg
                or "json" in msg  # yfinance sometimes returns JSON errors on rate limit
                or "connection" in msg
            )
            if is_rate_limit and attempt < max_retries - 1:
                continue
            raise
    raise last_err


def _fmp_key() -> str:
    key = os.environ.get("FMP_API_KEY", "")
    if not key:
        raise RuntimeError("FMP_API_KEY environment variable not set.")
    return key


def _fmp_get(endpoint: str, **params) -> list | dict:
    """Raw GET against FMP /stable API. Returns parsed JSON (usually a list)."""
    params["apikey"] = _fmp_key()
    url = f"{FMP_BASE}/{endpoint}"
    with httpx.Client(timeout=20.0) as client:
        resp = client.get(url, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"FMP {endpoint} returned {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if isinstance(data, dict) and data.get("Error Message"):
        raise RuntimeError(f"FMP error: {data['Error Message']}")
    return data


# ── Raw FMP fetchers (one per statement) ───────────────────────────────────────

def fmp_profile(ticker: str) -> dict:
    data = _fmp_get("profile", symbol=ticker)
    return data[0] if isinstance(data, list) and data else {}


def fmp_quote(ticker: str) -> dict:
    data = _fmp_get("quote", symbol=ticker)
    return data[0] if isinstance(data, list) and data else {}


def fmp_income_statement(ticker: str, limit: int = 6) -> list:
    return _fmp_get("income-statement", symbol=ticker, period="annual", limit=limit) or []


def fmp_balance_sheet(ticker: str, limit: int = 7) -> list:
    return _fmp_get("balance-sheet-statement", symbol=ticker, period="annual", limit=limit) or []


def fmp_cash_flow(ticker: str, limit: int = 6) -> list:
    return _fmp_get("cash-flow-statement", symbol=ticker, period="annual", limit=limit) or []


def fmp_ratios(ticker: str, limit: int = 1) -> list:
    return _fmp_get("ratios", symbol=ticker, period="annual", limit=limit) or []


def fmp_key_metrics(ticker: str, limit: int = 1) -> list:
    return _fmp_get("key-metrics", symbol=ticker, period="annual", limit=limit) or []


# ── Helpers ──────────────────────────────────────────────────────────────────

def _g(d: dict, *keys, default=None):
    """Try multiple possible key names (FMP has renamed fields across versions)."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _year_label(row: dict) -> str:
    date_str = row.get("date", "")
    fy = row.get("fiscalYear") or row.get("calendarYear")
    if fy:
        return str(fy)
    return date_str[:4] if date_str else "Y"


# ── Build yfinance-shaped DataFrames from FMP JSON ─────────────────────────────

def _build_income_df(rows: list) -> pd.DataFrame:
    """
    Index names match what dcf_endpoint.py's find_row() searches for:
    "total revenue", "total expenses", "pretax", "tax provision",
    "interest expense", "depreciation amortization"
    """
    if not rows:
        return pd.DataFrame()

    cols = [_year_label(r) for r in rows]
    data = {
        "Total Revenue":                [_g(r, "revenue", default=0.0) for r in rows],
        "Total Expenses":                [_g(r, "costAndExpenses", "operatingExpenses", default=0.0) for r in rows],
        "Pretax Income":                 [_g(r, "incomeBeforeTax", default=0.0) for r in rows],
        "Tax Provision":                 [_g(r, "incomeTaxExpense", default=0.0) for r in rows],
        "Interest Expense":              [_g(r, "interestExpense", default=0.0) for r in rows],
        "Reconciled Depreciation":       [_g(r, "depreciationAndAmortization", default=0.0) for r in rows],
        "EBIT":                          [_g(r, "operatingIncome", "ebit", default=0.0) for r in rows],
        "Net Income":                    [_g(r, "netIncome", default=0.0) for r in rows],
    }
    df = pd.DataFrame(data, index=cols).T
    return df


def _build_balance_df(rows: list) -> pd.DataFrame:
    """
    Index names match find_row() searches:
    "current assets", "current liabilities", "cash and cash equivalents",
    "current portion"+"long term", "net ppe", "long term investments",
    "minority interest"
    """
    if not rows:
        return pd.DataFrame()

    cols = [_year_label(r) for r in rows]
    data = {
        "Total Current Assets":          [_g(r, "totalCurrentAssets", default=0.0) for r in rows],
        "Total Current Liabilities":     [_g(r, "totalCurrentLiabilities", default=0.0) for r in rows],
        "Cash And Cash Equivalents":     [_g(r, "cashAndCashEquivalents", default=0.0) for r in rows],
        "Current Portion Of Long Term Debt": [_g(r, "shortTermDebt", "capitalLeaseObligationsCurrent", default=0.0) for r in rows],
        "Net PPE":                       [_g(r, "propertyPlantEquipmentNet", default=0.0) for r in rows],
        "Long Term Investments":         [_g(r, "longTermInvestments", "totalInvestments", default=0.0) for r in rows],
        "Minority Interest":             [_g(r, "minorityInterest", default=0.0) for r in rows],
        "Total Debt":                    [_g(r, "totalDebt", default=0.0) for r in rows],
        "Total Stockholders Equity":     [_g(r, "totalStockholdersEquity", "totalEquity", default=0.0) for r in rows],
    }
    df = pd.DataFrame(data, index=cols).T
    return df


def _build_cashflow_df(rows: list) -> pd.DataFrame:
    """
    Index names match find_row() searches:
    "depreciation amortization", "capital expenditure"
    """
    if not rows:
        return pd.DataFrame()

    cols = [_year_label(r) for r in rows]
    data = {
        "Depreciation Amortization":     [_g(r, "depreciationAndAmortization", default=0.0) for r in rows],
        "Capital Expenditure":           [_g(r, "capitalExpenditure", default=0.0) for r in rows],
        "Free Cash Flow":                [_g(r, "freeCashFlow", default=0.0) for r in rows],
        "Operating Cash Flow":           [_g(r, "operatingCashFlow", "netCashProvidedByOperatingActivities", default=0.0) for r in rows],
    }
    df = pd.DataFrame(data, index=cols).T
    return df


def _build_info_dict(ticker: str, profile: dict, quote: dict, ratios: dict, key_metrics: dict) -> dict:
    """
    Builds the same `info` dict shape that yfinance's stock.info returns,
    covering every field used across /dcf, /valuation, /financials, /screener.
    """
    return {
        "currentPrice":       _g(quote, "price", default=_g(profile, "price")),
        "regularMarketPrice": _g(quote, "price", default=_g(profile, "price")),
        "sharesOutstanding":  _g(quote, "sharesOutstanding"),
        "beta":               _g(profile, "beta", default=1.0),
        "marketCap":          _g(quote, "marketCap", default=_g(profile, "marketCap")),
        "totalDebt":          None,   # filled from balance sheet in get_company_data
        "totalCash":          None,   # filled from balance sheet in get_company_data
        "longName":           _g(profile, "companyName", default=ticker),
        "sector":             _g(profile, "sector", default="Unknown"),
        "industry":           _g(profile, "industry", default="Unknown"),
        "trailingPE":         _g(quote, "pe", default=_g(ratios, "priceToEarningsRatio")),
        "forwardPE":          None,
        "priceToBook":        _g(ratios, "priceToBookRatio"),
        "returnOnEquity":     _g(key_metrics, "returnOnEquity", default=_g(ratios, "returnOnEquity")),
        "debtToEquity":       _g(ratios, "debtToEquityRatio"),
        "dividendYield":      _g(ratios, "dividendYield"),
        "trailingEps":        _g(quote, "eps"),
        "bookValue":          None,
    }


# ── yfinance fallback (existing logic, unchanged) ──────────────────────────────

def _fetch_from_yfinance(ticker: str):
    cache_key = f"yf:{ticker}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    import yfinance as yf

    def _do_fetch():
        stock = yf.Ticker(ticker)
        info        = stock.info
        income_df   = stock.financials
        balance_df  = stock.balance_sheet
        cashflow_df = stock.cashflow
        if not info or (income_df is None or income_df.empty):
            raise RuntimeError("Too Many Requests")  # treat empty response as rate-limit
        return info, income_df, balance_df, cashflow_df

    result = _with_retry(_do_fetch, max_retries=3, base_delay=1.5)
    _cache_set(cache_key, result)
    return result


# ── FMP fetcher — builds the unified shape ──────────────────────────────────────

def _fetch_from_fmp(ticker: str):
    cache_key = f"fmp:{ticker}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    profile     = fmp_profile(ticker)
    quote       = fmp_quote(ticker)
    income_rows = fmp_income_statement(ticker, limit=6)
    balance_rows = fmp_balance_sheet(ticker, limit=7)
    cashflow_rows = fmp_cash_flow(ticker, limit=6)
    ratios_rows = fmp_ratios(ticker, limit=1)
    km_rows     = fmp_key_metrics(ticker, limit=1)

    ratios      = ratios_rows[0] if ratios_rows else {}
    key_metrics = km_rows[0] if km_rows else {}

    income_df   = _build_income_df(income_rows)
    balance_df  = _build_balance_df(balance_rows)
    cashflow_df = _build_cashflow_df(cashflow_rows)

    info = _build_info_dict(ticker, profile, quote, ratios, key_metrics)

    # Pull totalDebt / totalCash from most recent balance sheet row for the info dict
    if not balance_df.empty:
        info["totalDebt"] = float(balance_df.loc["Total Debt"].iloc[0]) if "Total Debt" in balance_df.index else 0.0
        info["totalCash"] = float(balance_df.loc["Cash And Cash Equivalents"].iloc[0]) if "Cash And Cash Equivalents" in balance_df.index else 0.0

    result = (info, income_df, balance_df, cashflow_df)
    _cache_set(cache_key, result)
    return result


# ── Public entry point ──────────────────────────────────────────────────────────

def get_company_data(ticker: str, market: str = "us", source: str = "auto"):
    """
    Returns (info, income_df, balance_df, cashflow_df, data_source_label).

    READ ORDER (this is the key to eliminating rate limits):
      1. YOUR STORE (Postgres) — instant, no rate limits, fundamentals
         + live price fetched separately (small fast call)
      2. If not in store → live fetch (SEC/yfinance/FMP) as fallback

    source:
        "auto"     -> store first, then India: yfinance | US: SEC EDGAR
        "store"    -> force store only (error if not present)
        "yfinance" -> force yfinance (bypass store)
        "fmp"      -> force FMP
        "sec"      -> force SEC EDGAR (US only)
    """
    raw_ticker = ticker.upper()
    is_india   = market.lower() == "india"

    if is_india and not raw_ticker.endswith(".NS") and source not in ("fmp", "sec"):
        raw_ticker += ".NS"

    # ── STORE FIRST (unless a specific live source is forced) ─────────────────
    if source in ("auto", "store"):
        try:
            from data_store import get_from_store, get_live_price
            stored = get_from_store(ticker, market)
            if stored:
                info, inc, bal, cf = stored
                # Fetch ONLY the live price (small, fast, rarely rate-limited)
                live_price = get_live_price(ticker, market)
                if live_price:
                    info["currentPrice"]       = live_price
                    info["regularMarketPrice"] = live_price
                    # Derive market cap, PE, PB from live price + stored fundamentals
                    if info.get("sharesOutstanding"):
                        info["marketCap"] = live_price * info["sharesOutstanding"]
                    if info.get("trailingEps") and info["trailingEps"] > 0:
                        info["trailingPE"] = live_price / info["trailingEps"]
                    if info.get("bookValue") and info["bookValue"] > 0:
                        info["priceToBook"] = live_price / info["bookValue"]
                return (info, inc, bal, cf, info.get("_data_source", "store"))
        except Exception:
            # Store unavailable (e.g. DATABASE_URL not set) — fall through to live
            pass

        if source == "store":
            raise RuntimeError(f"{ticker} not found in data store.")

    resolved_source = source
    if source == "auto":
        resolved_source = "yfinance" if is_india else "sec"

    # ── SEC EDGAR (US financials) ─────────────────────────────────────────────
    if resolved_source == "sec":
        try:
            from sec_edgar_layer import get_sec_company_data
            info, inc, bal, cf = get_sec_company_data(raw_ticker.replace(".NS", ""))
            return (info, inc, bal, cf, "sec_edgar")
        except Exception as sec_err:
            try:
                return (*_fetch_from_fmp(raw_ticker.replace(".NS", "")), "fmp (sec fallback)")
            except Exception:
                try:
                    return (*_fetch_from_yfinance(raw_ticker), "yfinance (sec+fmp fallback)")
                except Exception as yf_err:
                    raise RuntimeError(f"All sources failed. SEC: {sec_err} | yfinance: {yf_err}")

    # ── FMP ───────────────────────────────────────────────────────────────────
    if resolved_source == "fmp":
        try:
            return (*_fetch_from_fmp(raw_ticker.replace(".NS", "")), "fmp")
        except Exception as fmp_err:
            try:
                yf_ticker = raw_ticker if raw_ticker.endswith(".NS") or not is_india else raw_ticker
                if is_india and not yf_ticker.endswith(".NS"):
                    yf_ticker += ".NS"
                return (*_fetch_from_yfinance(yf_ticker), "yfinance (fmp fallback)")
            except Exception as yf_err:
                raise RuntimeError(f"Both FMP and yfinance failed. FMP: {fmp_err} | yfinance: {yf_err}")

    # ── yfinance (default for India) ──────────────────────────────────────────
    return (*_fetch_from_yfinance(raw_ticker), "yfinance")
