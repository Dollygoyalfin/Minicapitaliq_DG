import sys
import functools
print = functools.partial(print, flush=True)
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional  # kept for forward compatibility
import yfinance as yf
import os
import httpx
import json
import re
import math
from datetime import date as _date
from fmp_data_layer import get_company_data
from fmp_data_layer import _cache_get, _cache_set, _with_retry   # for convergence

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import pathlib


# Set yfinance to use a persistent session with headers to reduce rate limiting
try:
    import yfinance as yf
    import requests
    _yf_session = requests.Session()
    _yf_session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    })
    yf.utils._session = _yf_session
except Exception:
    pass


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── PWA static files (manifest, sw, icons) ───────────────────────────────────
_STATIC_DIR = pathlib.Path(__file__).parent  # same folder as main.py

@app.get("/manifest.json", include_in_schema=False)
def serve_manifest():
    return FileResponse(_STATIC_DIR / "manifest.json", media_type="application/manifest+json")

@app.get("/sw.js", include_in_schema=False)
def serve_sw():
    return FileResponse(_STATIC_DIR / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/"})

@app.get("/icon-192.png", include_in_schema=False)
def serve_icon192():
    return FileResponse(_STATIC_DIR / "icon-192.png", media_type="image/png")

@app.get("/icon-512.png", include_in_schema=False)
def serve_icon512():
    return FileResponse(_STATIC_DIR / "icon-512.png", media_type="image/png")

# ── API Keys ──────────────────────────────────────────────────────────────────
FMP_API_KEY  = os.getenv("FMP_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

if not FMP_API_KEY:
    print("WARNING: FMP_API_KEY is not set. All FMP endpoints will return empty data.")
if not GROQ_API_KEY:
    print("WARNING: GROQ_API_KEY is not set. AI Verdict will not work.")

# ── FMP Base URL ──────────────────────────────────────────────────────────────
FMP_BASE = "https://financialmodelingprep.com/stable"

# ─────────────────────────────────────────────────────────────────────────────
#  FMP HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def fmp_get(path: str, params: dict = None) -> dict | list:
    try:
        url = f"{FMP_BASE}{path}"
        p = dict(params) if params else {}
        p["apikey"] = FMP_API_KEY
        resp = httpx.get(url, params=p, timeout=15)
        print(f"[FMP DEBUG] {path} | status={resp.status_code} | body={resp.text[:300]!r}", flush=True)
        data = resp.json()
        if isinstance(data, dict) and ("Error Message" in data or "error" in data):
            return []
        return data
    except Exception as e:
        print(f"[FMP DEBUG] {path} | EXCEPTION: {type(e).__name__}: {e}", flush=True)
        return []


def get_fmp_profile(ticker: str) -> dict:
    data = fmp_get("/profile", {"symbol": ticker})
    if not data or not isinstance(data, list) or not data[0]:
        return {}
    d = data[0]

    bal = fmp_get("/balance-sheet-statement", {"symbol": ticker, "limit": 1})
    bal0 = bal[0] if isinstance(bal, list) and bal else {}
    total_debt = float(bal0.get("totalDebt") or bal0.get("longTermDebt") or 0)
    total_cash = float(bal0.get("cashAndShortTermInvestments") or bal0.get("cashAndCashEquivalents") or 0)

    shares = d.get("sharesOutstanding")
    if not shares:
        mktcap = d.get("mktCap") or 0
        price  = d.get("price") or 1
        shares = mktcap / price if price else None

    return {
        "currentPrice":       d.get("price"),
        "marketCap":          d.get("mktCap"),
        "trailingEps":        d.get("eps"),
        "trailingPE":         d.get("pe"),
        "forwardPE":          None,
        "priceToBook":        d.get("priceToBookRatio"),
        "beta":               d.get("beta") or 1.0,
        "returnOnEquity":     d.get("roe"),
        "debtToEquity":       d.get("debtToEquityRatio"),
        "bookValue":          d.get("bookValuePerShare"),
        "longName":           d.get("companyName"),
        "sector":             d.get("sector"),
        "industry":           d.get("industry"),
        "sharesOutstanding":  shares,
        "totalDebt":          total_debt,
        "totalCash":          total_cash,
        "dividendYield":      d.get("lastDiv"),
        "regularMarketPrice": d.get("price"),
        "description":        d.get("description", ""),
        "exchange":           d.get("exchangeShortName", ""),
    }


def get_fmp_income(ticker: str, limit: int = 6) -> list:
    data = fmp_get("/income-statement", {"symbol": ticker, "limit": limit})
    return data if isinstance(data, list) else []


def get_fmp_cashflow(ticker: str, limit: int = 6) -> list:
    data = fmp_get("/cash-flow-statement", {"symbol": ticker, "limit": limit})
    return data if isinstance(data, list) else []


def get_fmp_balance(ticker: str, limit: int = 7) -> list:
    data = fmp_get("/balance-sheet-statement", {"symbol": ticker, "limit": limit})
    return data if isinstance(data, list) else []

def resolve_ticker(ticker: str, market: str, advanced: bool = False) -> tuple[str, bool]:
    """
    Returns (resolved_ticker, use_fmp).
    - India + default  → yfinance (.NS suffix)
    - India + advanced → FMP (KEEP .NS suffix — FMP uses TICKER.NS for NSE listings)
    - US               → FMP always
    """
    t = ticker.upper()
    if market.lower() == "india":
        if not t.endswith(".NS"):
            t += ".NS"
        if advanced:
            return t, True   # FMP: keep .NS
        else:
            return t, False  # yfinance: keep .NS
    else:
        return t, True


# ─────────────────────────────────────────────────────────────────────────────
#  STATIC / ROOT
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def serve_frontend():
    return FileResponse("index.html")


# ─────────────────────────────────────────────────────────────────────────────
#  /valuation  — yfinance for India default, FMP for US / Advanced
# ─────────────────────────────────────────────────────────────────────────────

# ── STORE-FIRST /valuation and /financials (drop-in) ──────────────────────────
# Replace main.py lines for BOTH old blocks (get_valuation + get_financials)
# with this file's contents. Response shapes preserved for the frontend.
# Requires (already imported in main.py): get_company_data, math, Query

@app.get("/valuation")
def get_valuation(
    ticker: str = Query(...),
    market: str = Query("us"),
    advanced: bool = Query(False),          # kept for URL compatibility
    source: str = Query("auto"),
    risk_free_rate: float = Query(0.04),
    market_return: float = Query(0.10),
    growth_rate: float = Query(0.08),
):
    try:
        info, income_df, balance_df, cashflow_df, data_source = get_company_data(
            ticker=ticker, market=market, source=source
        )

        def _row(df, *keys):
            if df is None or df.empty:
                return None
            for idx in df.index:
                if all(k.lower() in idx.lower() for k in keys):
                    try:
                        v = df.loc[idx].iloc[0]
                        return None if v is None or str(v) == "nan" else float(v)
                    except Exception:
                        return None
            return None

        current_price = info.get("currentPrice")
        eps           = info.get("trailingEps") or 0.0
        beta          = info.get("beta") or 1.0
        beta          = max(0.5, min(float(beta), 2.5))  # sanity clamp
        book_value    = info.get("bookValue")
        shares        = info.get("sharesOutstanding")
        market_cap    = info.get("marketCap") or (
            current_price * shares if current_price and shares else None)
        pe_ratio      = info.get("trailingPE") or (
            current_price / eps if current_price and eps > 0 else None)
        pb_ratio      = info.get("priceToBook") or (
            current_price / book_value if current_price and book_value else None)

        # ROE and D/E from the store's own statements
        net_income = info.get("netIncome") or _row(income_df, "net income")
        equity     = _row(balance_df, "stockholders equity") or _row(balance_df, "total equity")
        total_debt = info.get("totalDebt") or _row(balance_df, "total debt") or 0.0
        roe        = info.get("returnOnEquity") or (
            net_income / equity if net_income and equity else None)
        de_ratio   = (total_debt / equity * 100) if equity else None

        # WACC — real debt when available, 80/20 otherwise
        cost_of_equity = risk_free_rate + beta * (market_return - risk_free_rate)
        cost_of_debt   = 0.06
        if market_cap:
            equity_value = market_cap
            debt_value   = total_debt if total_debt else market_cap * 0.2
        else:
            debt_value   = total_debt if total_debt else 1.0
            equity_value = debt_value * 4.0
        wacc = ((equity_value / (equity_value + debt_value)) * cost_of_equity +
                (debt_value / (equity_value + debt_value)) * cost_of_debt)

        # Gordon Growth with a denominator floor — growth is capped so the
        # spread (wacc - g) never drops below 4%, preventing absurd values
        # (e.g. the old ₹4,472 base case from a 1.3% denominator)
        # ── Gordon Growth on NORMALISED earnings ────────────────────────────
        # This previously capitalised the LATEST year's EPS in perpetuity. For
        # a cyclical at peak profitability that values a peak year forever —
        # the same defect corrected in the DCF. NALCO's ₹836 came from one
        # exceptional year's ₹31.56 EPS against a 5-year average near ₹19.
        eps_history = []
        if income_df is not None and not income_df.empty and shares:
            for idx in income_df.index:
                if "net income" in idx.lower():
                    for col in income_df.columns[:6]:
                        try:
                            v = float(income_df.loc[idx, col])
                            if v == v and v > 0:
                                eps_history.append(v / shares)
                        except Exception:
                            pass
                    break

        eps_used, eps_basis = eps, "latest year"
        if len(eps_history) >= 3 and eps:
            avg_eps = sum(eps_history) / len(eps_history)
            # More than 40% above the company's own multi-year average means
            # the latest year is an outlier, not a run rate.
            if avg_eps > 0 and eps > avg_eps * 1.4:
                eps_used = avg_eps
                eps_basis = (f"{len(eps_history)}-year average — the latest "
                             f"year was {eps/avg_eps:.1f}x that average")

        # Terminal growth cannot exceed the risk-free rate; no company
        # outgrows the economy in perpetuity. This replaces the arbitrary
        # "wacc - g >= 4%" floor with the reason it existed.
        g_eff = min(growth_rate, risk_free_rate)
        if wacc - g_eff < 0.02:
            g_eff = wacc - 0.02

        intrinsic_value = None
        if eps_used and eps_used > 0 and wacc > g_eff:
            intrinsic_value = (eps_used * (1 + g_eff)) / (wacc - g_eff)

        valuation_low = valuation_high = None
        if eps_used and eps_used > 0:
            g_low,  d_low  = g_eff - 0.02, wacc + 0.02
            g_high, d_high = g_eff + 0.01, max(wacc - 0.01, g_eff + 0.04)
            if d_low > g_low:
                valuation_low = (eps_used * (1 + g_low)) / (d_low - g_low)
            if d_high > g_high:
                valuation_high = (eps_used * (1 + g_high)) / (d_high - g_high)

        return {
            "ticker":          ticker.upper(),
            "market":          market,
            "data_source":     data_source,
            "current_price":   current_price,
            "eps":             round(eps, 2) if eps else 0.0,
            "pe_ratio":        round(pe_ratio, 2) if pe_ratio else None,
            "forward_pe":      info.get("forwardPE"),
            "beta":            beta,
            "pb_ratio":        round(pb_ratio, 2) if pb_ratio else None,
            "book_value":      round(book_value, 2) if book_value else None,
            "market_cap":      market_cap,
            "roe":             round(roe, 4) if roe else None,
            "de_ratio":        round(de_ratio, 2) if de_ratio else None,
            "intrinsic_value": round(intrinsic_value, 2) if intrinsic_value else None,
            "valuation_low":   round(valuation_low, 2) if valuation_low else None,
            "valuation_high":  round(valuation_high, 2) if valuation_high else None,
            "eps_used":           round(eps_used, 2) if eps_used else None,
            "eps_basis":          eps_basis,
            "eps_latest":         round(eps, 2) if eps else None,
            "growth_rate_used":   round(g_eff, 4),
            "discount_rate_used": round(wacc, 4),
            "wacc":               round(wacc, 4),
            "promoters_holding":  None,
            "fii_holding":        None,
            "dii_holding":        None,
            "retail_holding":     None,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/financials")
def get_financials(
    ticker: str = Query(...),
    market: str = Query("us"),
    advanced: bool = Query(False),          # kept for URL compatibility
    source: str = Query("auto"),
):
    try:
        info, income_df, balance_df, cashflow_df, data_source = get_company_data(
            ticker=ticker, market=market, source=source
        )
        shares = info.get("sharesOutstanding")

        def df_to_serializable(df, n=6):
            if df is None or df.empty:
                return {}
            out = {}
            for col in df.columns[:n]:
                try:
                    yr = str(col.year)
                except Exception:
                    yr = str(col)
                row = {}
                for idx in df.index:
                    try:
                        v = float(df.loc[idx, col])
                        row[idx] = None if (math.isnan(v) or math.isinf(v)) else v
                    except Exception:
                        row[idx] = None
                out[yr] = row
            return out

        income        = df_to_serializable(income_df)
        cashflow      = df_to_serializable(cashflow_df)
        balance_sheet = df_to_serializable(balance_df)

        # Per-year Basic EPS derived from NI / shares (store has no EPS rows)
        if shares:
            for yr, row in income.items():
                ni = row.get("Net Income")
                if ni and "Basic EPS" not in row:
                    row["Basic EPS"] = round(ni / shares, 2)

        # ROE per year: DuPont when total assets exist, plain NI/equity otherwise
        roe_dupont = {}
        for year, row in income.items():
            ni  = row.get("Net Income") or 0
            bal = balance_sheet.get(year, {})
            equity = (bal.get("Total Stockholders Equity")
                      or bal.get("Stockholders Equity") or 0)
            assets = bal.get("Total Assets")
            try:
                if assets and equity and row.get("Total Revenue"):
                    rev = row["Total Revenue"]
                    roe_val = (ni / rev) * (rev / assets) * (assets / equity)
                elif equity:
                    roe_val = ni / equity
                else:
                    roe_val = 0.0
                roe_dupont[year] = 0.0 if (math.isnan(roe_val) or math.isinf(roe_val)) else roe_val
            except (ZeroDivisionError, TypeError):
                roe_dupont[year] = 0.0

        return {
            "data_source":      data_source,
            "income_statement": income,
            "cash_flow":        cashflow,
            "balance_sheet":    balance_sheet,
            "dupont_roe":       roe_dupont,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/financials")
def get_financials(
    ticker: str = Query(...),
    market: str = Query("us"),
    advanced: bool = Query(False),          # kept for URL compatibility
    source: str = Query("auto"),
):
    try:
        info, income_df, balance_df, cashflow_df, data_source = get_company_data(
            ticker=ticker, market=market, source=source
        )
        shares = info.get("sharesOutstanding")

        def df_to_serializable(df, n=6):
            if df is None or df.empty:
                return {}
            out = {}
            for col in df.columns[:n]:
                try:
                    yr = str(col.year)
                except Exception:
                    yr = str(col)
                row = {}
                for idx in df.index:
                    try:
                        v = float(df.loc[idx, col])
                        row[idx] = None if (math.isnan(v) or math.isinf(v)) else v
                    except Exception:
                        row[idx] = None
                out[yr] = row
            return out

        income        = df_to_serializable(income_df)
        cashflow      = df_to_serializable(cashflow_df)
        balance_sheet = df_to_serializable(balance_df)

        # Per-year Basic EPS derived from NI / shares (store has no EPS rows)
        if shares:
            for yr, row in income.items():
                ni = row.get("Net Income")
                if ni and "Basic EPS" not in row:
                    row["Basic EPS"] = round(ni / shares, 2)

        # ROE per year: DuPont when total assets exist, plain NI/equity otherwise
        roe_dupont = {}
        for year, row in income.items():
            ni  = row.get("Net Income") or 0
            bal = balance_sheet.get(year, {})
            equity = (bal.get("Total Stockholders Equity")
                      or bal.get("Stockholders Equity") or 0)
            assets = bal.get("Total Assets")
            try:
                if assets and equity and row.get("Total Revenue"):
                    rev = row["Total Revenue"]
                    roe_val = (ni / rev) * (rev / assets) * (assets / equity)
                elif equity:
                    roe_val = ni / equity
                else:
                    roe_val = 0.0
                roe_dupont[year] = 0.0 if (math.isnan(roe_val) or math.isinf(roe_val)) else roe_val
            except (ZeroDivisionError, TypeError):
                roe_dupont[year] = 0.0

        return {
            "data_source":      data_source,
            "income_statement": income,
            "cash_flow":        cashflow,
            "balance_sheet":    balance_sheet,
            "dupont_roe":       roe_dupont,
        }
    except Exception as e:
        return {"error": str(e)}


# ── Sector P/E medians (hardcoded, update quarterly) ──────────────────────────
SECTOR_PE_MEDIANS = {
    # India sectors
    "Financial Services":        18.0,
    "Banking":                   14.0,
    "Insurance":                 22.0,
    "Technology":                28.0,
    "Consumer Defensive":        35.0,
    "Consumer Cyclical":         30.0,
    "Healthcare":                32.0,
    "Energy":                    12.0,
    "Basic Materials":           14.0,
    "Industrials":               22.0,
    "Communication Services":    20.0,
    "Real Estate":               25.0,
    "Utilities":                 16.0,
    # US sectors
    "Information Technology":    30.0,
    "Health Care":               25.0,
    "Financials":                14.0,
    "Consumer Discretionary":    28.0,
    "Consumer Staples":          22.0,
    "Industrials_US":            22.0,
    "Materials":                 16.0,
    "Energy_US":                 12.0,
    "Utilities_US":              18.0,
    "Real Estate_US":            35.0,
    "Communication Services_US": 20.0,
    # Default
    "Unknown":                   20.0,
}
 
 
@app.get("/convergence")
def get_convergence(
    ticker: str = Query(...),
    market: str = Query("us"),
    source: str = Query("auto", description="auto / yfinance / fmp"),
    risk_free_rate: float = Query(0.04),
    market_return: float = Query(0.10),
    terminal_growth_rate: float = Query(0.03),
    margin_of_safety: float = Query(0.25),
):
    """
    Convergence Engine — 5 valuation methods aggregated into one consensus signal.
 
    Methods:
    1. DCF (FCFF-based)         — cash flow intrinsic value
    2. Earnings Power Value     — zero-growth floor value (Bruce Greenwald)
    3. Graham Number            — Benjamin Graham's formula
    4. Relative Valuation       — sector P/E median × EPS
    5. Historical P/E Mean      — reversion to historical average multiple
 
    Outputs:
    - Individual intrinsic values per method
    - Upside/downside per method
    - Consensus intrinsic value (trimmed mean)
    - Confidence score (% of models agreeing on direction)
    - Model convergence signal
    - Margin of safety buy zone
    """
    try:
        # ── Fetch data ────────────────────────────────────────────────────────
        raw_ticker = ticker.upper()
        if market.lower() == "india" and not raw_ticker.endswith(".NS"):
            raw_ticker += ".NS"
 
        try:
            info, income_df, balance_df, cashflow_df, data_source = get_company_data(
                ticker=ticker, market=market, source=source
            )
        except Exception as e:
            return {"error": f"Data fetch failed: {e}"}
 
        # ── Info fields ───────────────────────────────────────────────────────
        current_price      = info.get("currentPrice") or info.get("regularMarketPrice")
        shares_outstanding = info.get("sharesOutstanding")
        beta               = info.get("beta", 1.0) or 1.0
        market_cap         = info.get("marketCap")
        total_debt         = info.get("totalDebt", 0) or 0
        total_cash         = info.get("totalCash", 0) or 0
        eps                = info.get("trailingEps")
        pe_ratio           = info.get("trailingPE")
        book_value         = info.get("bookValue")
        sector             = info.get("sector", "Unknown")
        company_name       = info.get("longName", raw_ticker)
        roe                = info.get("returnOnEquity")
 
        if not current_price:
            return {"error": "Current price not available for this ticker."}
        if not shares_outstanding or shares_outstanding == 0:
            return {"error": "Shares outstanding not available."}
 
        # ── Helpers ───────────────────────────────────────────────────────────
        def find_row(df, *keywords):
            for idx in df.index:
                if all(k.lower() in idx.lower() for k in keywords):
                    return idx
            return None
 
        def safe_float(df, row_key, col=0):
            if row_key is None or df is None or df.empty:
                return None
            try:
                val = df.loc[row_key].iloc[col]
                if val is None or str(val) == "nan":
                    return None
                return float(val)
            except Exception:
                return None
 
        def upside(intrinsic, price):
            if intrinsic is None or price is None or price == 0:
                return None
            return round((intrinsic - price) / price * 100, 2)
 
        def signal(up):
            if up is None:
                return "N/A"
            if up > 30:  return "Strong Buy"
            if up > 10:  return "Buy"
            if up > -10: return "Hold"
            if up > -30: return "Sell"
            return "Strong Sell"
 
        # ── WACC ──────────────────────────────────────────────────────────────
        cost_of_equity = risk_free_rate + beta * (market_return - risk_free_rate)
        interest_row   = find_row(income_df, "interest", "expense")
        interest_exp   = abs(safe_float(income_df, interest_row) or 0.0)
        cost_of_debt   = max(0.03, min(interest_exp / total_debt, 0.15)) if total_debt > 0 and interest_exp > 0 else 0.06
 
        if market_cap:
            equity_val = market_cap
            debt_val   = total_debt if total_debt else market_cap * 0.2
        else:
            # Price/market cap unavailable — assume a standard 80/20
            # capital structure instead of collapsing to all-debt WACC
            debt_val   = total_debt if total_debt else 1.0
            equity_val = debt_val * 4.0
        total_capital = equity_val + debt_val
 
        # Approximate avg tax rate from income statement
        pretax_row = find_row(income_df, "pretax") or find_row(income_df, "income before tax")
        tax_row    = find_row(income_df, "tax", "provision") or find_row(income_df, "income tax")
        pretax_val = safe_float(income_df, pretax_row) or 0
        tax_val    = safe_float(income_df, tax_row)    or 0
        avg_tax    = max(0.05, min(abs(tax_val / pretax_val), 0.40)) if pretax_val != 0 else 0.25
 
        wacc = (
            (equity_val / total_capital) * cost_of_equity +
            (debt_val   / total_capital) * cost_of_debt * (1 - avg_tax)
        )
 
        results = {}
 
        # ─────────────────────────────────────────────────────────────────────
        # METHOD 1 — DCF (simplified FCFF for convergence, full model in /dcf)
        # ─────────────────────────────────────────────────────────────────────
        try:
            revenue_row = find_row(income_df, "total revenue") or find_row(income_df, "revenue")
            opex_row    = (find_row(income_df, "total expenses")
                           or find_row(income_df, "total operating expenses")
                           or find_row(income_df, "operating expense"))
 
            revenue     = safe_float(income_df, revenue_row) or 0
            opex        = abs(safe_float(income_df, opex_row) or 0)
            nop         = revenue - opex
            nop_at      = nop * (1 - avg_tax)
 
            capex_row   = find_row(income_df, "depreciation") or find_row(cashflow_df, "capital expenditure")
            capex       = abs(safe_float(income_df, capex_row) or safe_float(cashflow_df, capex_row) or 0)
 
            fcff_base   = nop_at - capex
            if wacc > terminal_growth_rate and fcff_base > 0:
                # Simple Gordon Growth on FCFF
                tv           = fcff_base * (1 + terminal_growth_rate) / (wacc - terminal_growth_rate)
                pv_fcff      = fcff_base / (1 + wacc)
                ev_dcf       = pv_fcff + tv / (1 + wacc)
                equity_dcf   = ev_dcf + total_cash - total_debt
                dcf_value    = equity_dcf / shares_outstanding
            else:
                dcf_value = None
 
            results["dcf"] = {
                "method":          "DCF (FCFF)",
                "description":     "Free Cash Flow to Firm — discounted at WACC",
                "intrinsic_value": round(dcf_value, 2) if dcf_value else None,
                "upside_pct":      upside(dcf_value, current_price),
                "signal":          signal(upside(dcf_value, current_price)),
                "confidence":      "High" if dcf_value and dcf_value > 0 else "Low",
                "inputs":          {"wacc": round(wacc, 4), "terminal_growth": terminal_growth_rate},
            }
        except Exception as e:
            results["dcf"] = {"method": "DCF (FCFF)", "intrinsic_value": None, "error": str(e)}
 
        # ─────────────────────────────────────────────────────────────────────
        # METHOD 2 — EARNINGS POWER VALUE (EPV)
        # Bruce Greenwald: value assuming zero growth — conservative floor
        # EPV = Normalized EBIT * (1 - tax) / WACC
        # ─────────────────────────────────────────────────────────────────────
        try:
            ebit_row = (find_row(income_df, "ebit")
                        or find_row(income_df, "operating income")
                        or find_row(income_df, "income from operations"))
 
            # Average EBIT over available years for normalization
            ebit_values = []
            if ebit_row:
                for col in range(min(len(income_df.columns), 5)):
                    v = safe_float(income_df, ebit_row, col)
                    if v and v > 0:
                        ebit_values.append(v)
 
            if not ebit_values:
                # Derive EBIT from revenue - opex if no direct row
                rev_vals  = [safe_float(income_df, revenue_row, c) for c in range(min(len(income_df.columns), 5))]
                opex_vals = [safe_float(income_df, opex_row,    c) for c in range(min(len(income_df.columns), 5))]
                ebit_values = [r - o for r, o in zip(rev_vals, opex_vals)
                               if r is not None and o is not None and (r - o) > 0]
 
            if ebit_values and wacc > 0:
                norm_ebit  = sum(ebit_values) / len(ebit_values)
                nopat      = norm_ebit * (1 - avg_tax)
                ev_epv     = nopat / wacc
                equity_epv = ev_epv + total_cash - total_debt
                epv_value  = equity_epv / shares_outstanding
            else:
                epv_value = None
 
            results["epv"] = {
                "method":          "Earnings Power Value",
                "description":     "Normalized EBIT × (1-t) / WACC — zero-growth conservative floor",
                "intrinsic_value": round(epv_value, 2) if epv_value else None,
                "upside_pct":      upside(epv_value, current_price),
                "signal":          signal(upside(epv_value, current_price)),
                "confidence":      "Medium",
                "inputs":          {"normalized_ebit_years": len(ebit_values), "wacc": round(wacc, 4)},
            }
        except Exception as e:
            results["epv"] = {"method": "Earnings Power Value", "intrinsic_value": None, "error": str(e)}
 
        # ─────────────────────────────────────────────────────────────────────
        # METHOD 3 — GRAHAM NUMBER
        # √(22.5 × EPS × Book Value per Share)
        # Benjamin Graham's formula — value investing classic
        # ─────────────────────────────────────────────────────────────────────
        try:
            # Get EPS from info or derive from net income / shares
            g_eps = eps
            if not g_eps or g_eps <= 0:
                ni_row = find_row(income_df, "net income")
                ni     = safe_float(income_df, ni_row)
                if ni and ni > 0 and shares_outstanding:
                    g_eps = ni / shares_outstanding
 
            # Get book value per share from info or derive from balance sheet
            g_bv = book_value
            if not g_bv or g_bv <= 0:
                eq_row = (find_row(balance_df, "stockholders equity")
                          or find_row(balance_df, "total equity")
                          or find_row(balance_df, "shareholders equity"))
                equity = safe_float(balance_df, eq_row)
                if equity and equity > 0 and shares_outstanding:
                    g_bv = equity / shares_outstanding
 
            if g_eps and g_bv and g_eps > 0 and g_bv > 0:
                graham_value = (22.5 * g_eps * g_bv) ** 0.5
            else:
                graham_value = None
 
            results["graham"] = {
                "method":          "Graham Number",
                "description":     "√(22.5 × EPS × Book Value) — Benjamin Graham's formula",
                "intrinsic_value": round(graham_value, 2) if graham_value else None,
                "upside_pct":      upside(graham_value, current_price),
                "signal":          signal(upside(graham_value, current_price)),
                "confidence":      "Medium",
                "inputs":          {
                    "eps":        round(g_eps, 2) if g_eps else None,
                    "book_value": round(g_bv,  2) if g_bv  else None,
                },
            }
        except Exception as e:
            results["graham"] = {"method": "Graham Number", "intrinsic_value": None, "error": str(e)}
 
        # ─────────────────────────────────────────────────────────────────────
        # METHOD 4 — RELATIVE VALUATION (Sector P/E)
        # Fair Value = EPS × Sector Median P/E
        # ─────────────────────────────────────────────────────────────────────
        try:
            sector_pe = SECTOR_PE_MEDIANS.get(sector, SECTOR_PE_MEDIANS["Unknown"])
 
            r_eps = eps
            if not r_eps or r_eps <= 0:
                ni_row = find_row(income_df, "net income")
                ni     = safe_float(income_df, ni_row)
                if ni and ni > 0 and shares_outstanding:
                    r_eps = ni / shares_outstanding
 
            if r_eps and r_eps > 0:
                relative_value = r_eps * sector_pe
            else:
                relative_value = None
 
            # P/E premium/discount to sector
            pe_vs_sector = None
            if pe_ratio and sector_pe:
                pe_vs_sector = round((pe_ratio - sector_pe) / sector_pe * 100, 1)
 
            results["relative"] = {
                "method":           "Relative Valuation",
                "description":      f"EPS × {sector} sector median P/E ({sector_pe}x)",
                "intrinsic_value":  round(relative_value, 2) if relative_value else None,
                "upside_pct":       upside(relative_value, current_price),
                "signal":           signal(upside(relative_value, current_price)),
                "confidence":       "Medium",
                "inputs":           {
                    "sector":          sector,
                    "sector_median_pe": sector_pe,
                    "stock_pe":        round(pe_ratio, 2) if pe_ratio else None,
                    "pe_vs_sector_pct": pe_vs_sector,
                },
            }
        except Exception as e:
            results["relative"] = {"method": "Relative Valuation", "intrinsic_value": None, "error": str(e)}
 
        # ─────────────────────────────────────────────────────────────────────
        # METHOD 5 — HISTORICAL P/E MEAN REVERSION
        # Fair Value = EPS × 5-year average P/E
        # Uses cache to avoid re-hitting yfinance
        # ─────────────────────────────────────────────────────────────────────
        try:
            hist_pe_value = None
            avg_hist_pe   = None
            hist_pe_years = 0
 
            # STORE FIRST: daily closes from our own price_history table.
            # yfinance only as fallback for tickers not yet price-backfilled.
            hist_price = None
            try:
                from data_store import get_price_history
                ph = get_price_history(raw_ticker, market, days=1900)
                if ph and len(ph) > 200:
                    import pandas as pd
                    hist_price = pd.DataFrame(ph, columns=["Date", "Close"])
                    hist_price["Date"] = pd.to_datetime(hist_price["Date"])
                    hist_price = hist_price.set_index("Date")
            except Exception:
                hist_price = None
 
            if hist_price is None:
                import yfinance as yf
                hist_cache_key = f"hist_price:{raw_ticker}"
                hist_price = _cache_get(hist_cache_key)
                if hist_price is None:
                    try:
                        stock_yf   = yf.Ticker(raw_ticker)
                        hist_price = _with_retry(
                            lambda: stock_yf.history(period="5y", interval="1mo"),
                            max_retries=3, base_delay=2.0
                        )
                        if hist_price is not None and not hist_price.empty:
                            _cache_set(hist_cache_key, hist_price)
                    except Exception:
                        hist_price = None
 
            if hist_price is not None and not hist_price.empty:
                # Get trailing EPS for each year from financials
                ni_row   = find_row(income_df, "net income")
                hist_pes = []
 
                if ni_row and shares_outstanding:
                    for col in range(min(len(income_df.columns), 5)):
                        ni_hist = safe_float(income_df, ni_row, col)
                        if ni_hist and ni_hist > 0:
                            eps_hist = ni_hist / shares_outstanding
                            # Get average price for that year
                            try:
                                year_str  = str(income_df.columns[col].year)
                                yr_prices = hist_price[hist_price.index.year == int(year_str)]["Close"]
                                if not yr_prices.empty:
                                    avg_price_yr = float(yr_prices.mean())
                                    pe_yr        = avg_price_yr / eps_hist
                                    if 0 < pe_yr < 200:   # sanity check
                                        hist_pes.append(pe_yr)
                            except Exception:
                                pass
 
                if hist_pes:
                    avg_hist_pe   = sum(hist_pes) / len(hist_pes)
                    hist_pe_years = len(hist_pes)
                    h_eps         = eps
                    if not h_eps or h_eps <= 0:
                        ni_row = find_row(income_df, "net income")
                        ni     = safe_float(income_df, ni_row)
                        if ni and ni > 0 and shares_outstanding:
                            h_eps = ni / shares_outstanding
                    if h_eps and h_eps > 0:
                        hist_pe_value = h_eps * avg_hist_pe
 
            results["historical_pe"] = {
                "method":          "Historical P/E Reversion",
                "description":     f"EPS × {hist_pe_years}-year avg P/E — mean reversion signal",
                "intrinsic_value": round(hist_pe_value, 2) if hist_pe_value else None,
                "upside_pct":      upside(hist_pe_value, current_price),
                "signal":          signal(upside(hist_pe_value, current_price)),
                "confidence":      "High" if hist_pe_years >= 4 else "Low",
                "inputs":          {
                    "avg_historical_pe": round(avg_hist_pe, 2) if avg_hist_pe else None,
                    "current_pe":        round(pe_ratio, 2)    if pe_ratio    else None,
                    "years_of_data":     hist_pe_years,
                },
            }
        except Exception as e:
            results["historical_pe"] = {"method": "Historical P/E Reversion", "intrinsic_value": None, "error": str(e)}
 
        # ─────────────────────────────────────────────────────────────────────
        # CONSENSUS — Trimmed mean of valid intrinsic values
        # ─────────────────────────────────────────────────────────────────────
        valid_values = [
            r["intrinsic_value"]
            for r in results.values()
            if r.get("intrinsic_value") is not None and r["intrinsic_value"] > 0
        ]
 
        consensus_value = None
        if valid_values:
            # Trimmed mean — drop highest and lowest if we have 4+ values
            sorted_vals = sorted(valid_values)
            if len(sorted_vals) >= 4:
                trimmed = sorted_vals[1:-1]
            else:
                trimmed = sorted_vals
            consensus_value = sum(trimmed) / len(trimmed)
 
        # ─────────────────────────────────────────────────────────────────────
        # CONFIDENCE SCORE — % of models agreeing on direction (Buy/Sell)
        # ─────────────────────────────────────────────────────────────────────
        buy_signals  = sum(1 for r in results.values()
                           if r.get("upside_pct") is not None and r["upside_pct"] > 10)
        sell_signals = sum(1 for r in results.values()
                           if r.get("upside_pct") is not None and r["upside_pct"] < -10)
        total_valid  = sum(1 for r in results.values() if r.get("upside_pct") is not None)
 
        if total_valid > 0:
            dominant    = max(buy_signals, sell_signals)
            confidence  = round((dominant / total_valid) * 100)
            direction   = "Bullish" if buy_signals >= sell_signals else "Bearish"
        else:
            confidence  = 0
            direction   = "Neutral"
 
        # ─────────────────────────────────────────────────────────────────────
        # CONVERGENCE SIGNAL
        # ─────────────────────────────────────────────────────────────────────
        consensus_upside = upside(consensus_value, current_price)
 
        if confidence >= 80 and consensus_upside and consensus_upside > 20:
            convergence_signal = "Strong Buy"
        elif confidence >= 60 and consensus_upside and consensus_upside > 10:
            convergence_signal = "Buy"
        elif confidence >= 80 and consensus_upside and consensus_upside < -20:
            convergence_signal = "Strong Sell"
        elif confidence >= 60 and consensus_upside and consensus_upside < -10:
            convergence_signal = "Sell"
        else:
            convergence_signal = "Hold / Inconclusive"
 
        # ─────────────────────────────────────────────────────────────────────
        # BUY ZONE (Margin of Safety applied to consensus)
        # ─────────────────────────────────────────────────────────────────────
        buy_zone = round(consensus_value * (1 - margin_of_safety), 2) if consensus_value else None
 
        at_buy_zone = (
            buy_zone is not None
            and current_price is not None
            and current_price <= buy_zone * 1.05  # within 5% of buy zone
        )
 
        # ─────────────────────────────────────────────────────────────────────
        # VALUE RANGE (bear / base / bull)
        # ─────────────────────────────────────────────────────────────────────
        bear_value = min(valid_values) if valid_values else None
        bull_value = max(valid_values) if valid_values else None
 
        # ─────────────────────────────────────────────────────────────────────
        # RESPONSE
        # ─────────────────────────────────────────────────────────────────────
        return {
            "ticker":       raw_ticker,
            "company_name": company_name,
            "sector":       sector,
            "market":       market,
            "data_source":  data_source,
            "current_price": current_price,
 
            # Individual model results
            "models": results,
 
            # Consensus
            "consensus": {
                "intrinsic_value":   round(consensus_value, 2) if consensus_value else None,
                "upside_pct":        round(consensus_upside, 2) if consensus_upside else None,
                "convergence_signal": convergence_signal,
                "confidence_pct":    confidence,
                "direction":         direction,
                "models_used":       total_valid,
                "buy_signals":       buy_signals,
                "sell_signals":      sell_signals,
            },
 
            # Buy zone
            "buy_zone": {
                "price":          buy_zone,
                "mos_used":       margin_of_safety,
                "at_buy_zone":    at_buy_zone,
                "status":         "AT BUY ZONE ⚡" if at_buy_zone else (
                    f"{round((current_price - buy_zone) / buy_zone * 100, 1)}% above buy zone"
                    if buy_zone and current_price and current_price > buy_zone else "Below buy zone"
                ) if buy_zone else "N/A",
            },
 
            # Value range
            "value_range": {
                "bear": round(bear_value, 2) if bear_value else None,
                "base": round(consensus_value, 2) if consensus_value else None,
                "bull": round(bull_value, 2) if bull_value else None,
            },
 
            # Model assumptions
            "assumptions": {
                "wacc":                round(wacc, 4),
                "cost_of_equity":      round(cost_of_equity, 4),
                "cost_of_debt":        round(cost_of_debt, 4),
                "avg_tax_rate":        round(avg_tax, 4),
                "beta":                beta,
                "risk_free_rate":      risk_free_rate,
                "market_return":       market_return,
                "terminal_growth":     terminal_growth_rate,
            },
        }
 
    except Exception as e:
        return {"error": str(e)}
 

# ─────────────────────────────────────────────────────────────────────────────
#  /dcf  — yfinance for India default, FMP for US / Advanced
# ─────────────────────────────────────────────────────────────────────────────

# Requires: from fmp_data_layer import get_company_data   (add this import to main.py)

@app.get("/dcf")
def get_dcf(
    ticker: str = Query(...),
    market: str = Query("us"),
    source: str = Query("auto", description="Data source: auto / yfinance / fmp. Auto = yfinance for India, FMP for US."),
    projection_years: int = Query(5, description="Number of years to project FCFF"),
    risk_free_rate: float = Query(0.04, description="Risk-free rate (decimal)"),
    market_return: float = Query(0.10, description="Expected market return (decimal)"),
    terminal_growth_rate: float = Query(0.03, description="Terminal growth rate for perpetuity (decimal)"),
    margin_of_safety: float = Query(0.25, description="Margin of safety (decimal, e.g. 0.25 = 25%)"),
    # ── Modelling guardrails, exposed rather than hidden ──────────────────
    # These were added to stop specific broken outputs (XOM's compounding
    # revenue decline, TSLA's inverted margins, XOM's 0.16 beta). They are
    # deliberately conservative, which systematically lowers intrinsic value.
    # Surfacing them lets the user see and change that conservatism.
    beta_floor: float         = Query(0.50,  description="Minimum beta used in WACC"),
    beta_ceiling: float       = Query(2.50,  description="Maximum beta used in WACC"),
    # ── Operating margin assumption ───────────────────────────────────────
    # Costs are projected as a FUNCTION OF REVENUE, not as an independent
    # exponential series. Projecting the two separately let their different
    # growth rates silently expand or compress the margin every year — for
    # NALCO it drove a 40.7% margin to 52.4% by year 5 and 61.8% by year 10,
    # far outside anything the company has ever achieved, with no assumption
    # having been made anywhere. Now the margin is an explicit input.
    margin_basis: str    = Query("current",
                                 description="current | average | median | custom"),
    # Fitting a fixed/variable cost split on 4-6 annual observations can clear
    # an R2 threshold by chance, and when it fires it reintroduces margin drift
    # through operating leverage — the very effect the margin fix removed.
    # Off unless explicitly requested.
    use_cost_split: bool = Query(False,
                                 description="Fit fixed+variable costs instead of a flat margin"),
    custom_margin: float = Query(0.0, description="Used when margin_basis=custom")
):
    """
    Cash-based FCFF DCF Valuation.

    Data source (hybrid):
      - India stocks default to yfinance (reliable for NSE)
      - US stocks default to FMP (more reliable financial statements than yfinance)
      - Pass source=fmp or source=yfinance to override
      - Built-in caching (15 min) + retry-with-backoff to avoid
        yfinance's "Too Many Requests" rate-limit errors

    Income Statement (Revenue, OpEx):
      - Each line has its own avg YoY growth rate from historical data
      - Projected by compounding that rate forward

    Balance Sheet lines (CA, CL, Cash, CPLTD, Net PPE, Depreciation):
      - Each line has its own avg YoY growth rate from historical data
      - Projected Years 1-N by compounding
      - Terminal Year = rolling avg of previous 5 projected years

    Derived each year:
      WC    = CA - CL - Cash - CPLTD
      ΔNWC  = WC(year) - WC(year-1)
      CapEx = Net PPE(year) - Net PPE(year-1) + Depreciation(year)

    FCFF  = NOP*(1-t) - ΔNWC - CapEx
    TV    = FCFF_terminal / (WACC - terminal_growth_rate)
    EV    = Σ PV(FCFF) + PV(TV)
    Equity = EV + Cash + Investments - Debt - Minority Interest
    """
    try:
        # ── Ticker setup + fetch via hybrid data layer ──────────────────────────
        raw_ticker = ticker.upper()
        if market.lower() == "india" and not raw_ticker.endswith(".NS"):
            raw_ticker += ".NS"

        try:
            info, income_df, balance_df, cashflow_df, data_source = get_company_data(
                ticker=ticker, market=market, source=source
            )

            # ── DATA QUALITY GATE ───────────────────────────────────────────────
            # Refuse to publish a valuation built on inputs that fail accounting
            # sanity, rather than returning a confident wrong number.
            _ok, _why, _dq_warnings = validate_financials(
                info, income_df, balance_df, cashflow_df, market)
            if not _ok:
                return {"error": f"Data quality check failed: {_why}",
                        "ticker": ticker.upper(), "market": market,
                        "data_source": data_source}
        except Exception as fetch_err:
            return {"error": f"Could not fetch financial data: {fetch_err}"}

        # ── Info fields ───────────────────────────────────────────────────────
        current_price      = info.get("currentPrice")
        shares_outstanding = info.get("sharesOutstanding")
        beta               = info.get("beta", 1.0) or 1.0
        # ── Beta: prefer OUR OWN computed value over the external one ───────
        # yfinance reported beta 0.162 for ExxonMobil (real: ~0.9-1.1), which
        # produced a 4.77% WACC and a wildly inflated valuation. Rather than
        # clamp a bad number, use one regressed from our own 2 years of daily
        # returns against an equal-weight index of the same market — verifiable
        # and explainable. The external value remains the fallback.
        raw_beta      = float(beta)
        beta_source   = "external (yfinance)"
        beta_r2       = None
        computed_beta = None
        try:
            from data_store import _conn as _bconn
            _bt = raw_ticker
            with _bconn() as _bc:
                with _bc.cursor() as _bcur:
                    _bcur.execute("""SELECT beta_2y, beta_r2 FROM stock_signatures
                                     WHERE ticker = %s AND beta_2y IS NOT NULL
                                     ORDER BY date DESC LIMIT 1""", (_bt,))
                    _br = _bcur.fetchone()
            if _br and _br[0] is not None:
                computed_beta, beta_r2 = float(_br[0]), (float(_br[1]) if _br[1] else None)
                # A beta from a regression explaining almost nothing is not
                # more trustworthy than the external figure — require the
                # market to explain at least 10% of the stock's variance.
                if beta_r2 is not None and beta_r2 >= 0.10:
                    raw_beta    = computed_beta
                    beta_source = "computed from own price history (2y daily)"
        except Exception:
            pass

        beta = max(beta_floor, min(raw_beta, beta_ceiling))
        market_cap         = info.get("marketCap")
        total_debt         = info.get("totalDebt", 0) or 0
        total_cash         = info.get("totalCash", 0) or 0

        # Derive shares if missing: (a) marketCap/price, (b) netIncome/EPS
        if not shares_outstanding or shares_outstanding == 0:
            if market_cap and current_price:
                shares_outstanding = market_cap / current_price
            else:
                _eps = info.get("trailingEps")
                _ni  = info.get("netIncome")
                if _eps and _ni and _eps != 0:
                    derived = _ni / _eps
                    if 1e5 < abs(derived) < 1e12:
                        shares_outstanding = derived
        if not shares_outstanding or shares_outstanding == 0:
            return {"error": "Shares outstanding not available for this ticker "
                             f"(source: {data_source}). If this is an ingested "
                             "stock, re-run: python ingest.py one TICKER MARKET"}

        for label, df in [("Income statement", income_df),
                           ("Cash flow statement", cashflow_df),
                           ("Balance sheet", balance_df)]:
            if df is None or df.empty:
                return {"error": f"{label} not available for this ticker (source: {data_source})."}

        # ── Helpers ───────────────────────────────────────────────────────────
        def find_row(df, *keywords):
            for idx in df.index:
                if all(k.lower() in idx.lower() for k in keywords):
                    return idx
            return None

        def safe_float(df, row_key, col):
            if row_key is None:
                return None
            try:
                val = df.loc[row_key].iloc[col]
                if val is None or str(val) == "nan":
                    return None
                return float(val)
            except Exception:
                return None

        def avg_yoy_growth(series: list) -> float:
            """
            series = [most_recent, ..., oldest]
            Average YoY growth from valid consecutive pairs.
            Falls back to CAGR. Returns 0.0 if insufficient data.
            """
            rates = []
            for i in range(len(series) - 1):
                v_new, v_old = series[i], series[i + 1]
                if v_new is None or v_old is None or v_old == 0:
                    continue
                if v_old < 0 or v_new < 0:
                    continue
                rates.append((v_new - v_old) / v_old)
            if rates:
                # MEDIAN, not mean. A single distorted year — a merger, a
                # demerger, or a near-zero base — dominates an average and
                # was the real reason a +-60% bound seemed necessary. The
                # median is robust to one bad observation, so the outlier is
                # ignored rather than clamped, and no magic number is needed.
                rates.sort()
                n_r = len(rates)
                return (rates[n_r // 2] if n_r % 2
                        else (rates[n_r // 2 - 1] + rates[n_r // 2]) / 2)
            valid = [v for v in series if v is not None and v > 0]
            if len(valid) >= 2:
                n = len(valid) - 1
                return (valid[0] / valid[-1]) ** (1 / n) - 1
            return 0.0

        def _bs_value(df, keyword, idx):
            if df is None or df.empty or idx >= len(df.columns):
                return None
            for r in df.index:
                if keyword.lower() in r.lower():
                    try:
                        v = float(df.iloc[df.index.get_loc(r), idx])
                        return None if v != v else v
                    except Exception:
                        return None
            return None

        def project_line(base: float, growth: float, years: int) -> list:
            """Project a single line item N years forward by compounding.
            If base is 0, returns flat zero series (can't grow from zero).

            No clamp here. Growth rates are already computed with a median
            (robust to outliers) and bounded once at the point of use. Two
            layers of clamping in different places made it impossible to say
            what the model had actually assumed.
            """
            if base == 0.0:
                return [0.0] * years
            return [base * ((1 + growth) ** y) for y in range(1, years + 1)]

        def rolling_avg_terminal(projected: list) -> float:
            """
            Terminal year = rolling avg of last 5 projected values.
            If fewer than 5 projected years exist, average all of them.
            Returns 0.0 if list is empty.
            """
            if not projected:
                return 0.0
            window = projected[-5:] if len(projected) >= 5 else projected
            return sum(window) / len(window)

        # ── Identify rows ─────────────────────────────────────────────────────
        revenue_row  = find_row(income_df, "total revenue") or find_row(income_df, "revenue")
        opex_row     = (find_row(income_df, "total expenses")
                        or find_row(income_df, "total operating expenses")
                        or find_row(income_df, "operating expense")
                        or find_row(income_df, "cost of revenue"))
        pretax_row   = find_row(income_df, "pretax") or find_row(income_df, "income before tax")
        tax_row      = find_row(income_df, "tax", "provision") or find_row(income_df, "income tax")
        interest_row = find_row(income_df, "interest", "expense")

        # Balance sheet rows
        ca_row      = find_row(balance_df, "current assets")
        cl_row      = find_row(balance_df, "current liabilities")
        cash_row    = (find_row(balance_df, "cash and cash equivalents")
                       or find_row(balance_df, "cash"))
        cpltd_row   = (find_row(balance_df, "current portion", "long term")
                       or find_row(balance_df, "current", "long term debt")
                       or find_row(balance_df, "current portion"))
        net_ppe_row = (find_row(balance_df, "net ppe")
                       or find_row(balance_df, "net property plant")
                       or find_row(balance_df, "property plant equipment"))

        # Depreciation — check income statement first, then cashflow
        depr_row_inc = (find_row(income_df, "reconciled depreciation")
                        or find_row(income_df, "depreciation amortization")
                        or find_row(income_df, "depreciation"))
        depr_row_cf  = (find_row(cashflow_df, "depreciation amortization")
                        or find_row(cashflow_df, "depreciation"))

        if not revenue_row:
            return {"error": "Could not find Revenue in income statement."}

        # ── Determine usable years ───────────────────────────────────────────────
        # Collect up to 6 years of data for richer growth rate calculation
        # Balance sheet needs col+1 for CapEx derivation, so allow 7 cols
        n_inc   = min(len(income_df.columns), 6)
        n_cf    = min(len(cashflow_df.columns), 6)
        n_bal   = min(len(balance_df.columns), 7)
        n_years = min(n_inc, n_cf, n_bal)

        if n_years == 0:
            return {"error": "Not enough historical data to compute FCFF."}

        # ── Collect historical series (index 0 = most recent year) ────────────
        # Income statement series
        revenue_series = []
        opex_series    = []
        tax_rates      = []
        year_labels    = []

        # Balance sheet series (raw values per year)
        ca_series      = []
        cl_series      = []
        cash_series    = []
        cpltd_series   = []
        net_ppe_series = []
        depr_series    = []

        for col in range(n_years):
            revenue  = safe_float(income_df, revenue_row, col) or 0.0

            # Skip years with zero/missing revenue
            if revenue == 0.0:
                continue

            pretax   = safe_float(income_df, pretax_row, col)
            tax_prov = safe_float(income_df, tax_row,    col)

            # Effective tax rate
            if pretax and pretax != 0 and tax_prov is not None and tax_prov != 0:
                # Effective rates genuinely fall outside a normal band in a
                # single year (loss carry-forwards, one-off credits, deferred
                # tax reversals), but those are transient accounting events,
                # not the rate a business pays in perpetuity. Bounds are set
                # to the plausible statutory range for the market rather than
                # an arbitrary 5-40%: India's headline corporate rate is
                # ~25% post-2019 and the US federal rate is 21%, so a
                # sustained effective rate above ~35% or below ~10% reflects
                # something temporary.
                _tax_lo, _tax_hi = (0.10, 0.35)
                yr_tax = max(_tax_lo, min(abs(tax_prov / pretax), _tax_hi))
            else:
                yr_tax = 0.25

            # OpEx
            if opex_row:
                opex = abs(safe_float(income_df, opex_row, col) or 0.0)
            else:
                ebit_row_fb = find_row(income_df, "ebit") or find_row(income_df, "operating income")
                ebit_val    = safe_float(income_df, ebit_row_fb, col) or 0.0
                opex        = abs(revenue - ebit_val)

            # Balance sheet values
            ca      = safe_float(balance_df, ca_row,      col) or 0.0
            cl      = safe_float(balance_df, cl_row,      col) or 0.0
            csh     = safe_float(balance_df, cash_row,    col) or 0.0
            cpltd   = safe_float(balance_df, cpltd_row,   col) or 0.0
            net_ppe = safe_float(balance_df, net_ppe_row, col) or 0.0
            depr    = abs(safe_float(income_df,   depr_row_inc, col) or
                          safe_float(cashflow_df, depr_row_cf,  col) or 0.0)

            col_val = income_df.columns[col]
            try:
                year_labels.append(str(col_val.year))
            except Exception:
                # SEC/store columns are already year strings like "2025"
                year_labels.append(str(col_val))

            revenue_series.append(revenue)
            opex_series.append(opex)
            tax_rates.append(yr_tax)
            ca_series.append(ca)
            cl_series.append(cl)
            cash_series.append(csh)
            cpltd_series.append(cpltd)
            net_ppe_series.append(net_ppe)
            depr_series.append(depr)

        if not revenue_series:
            return {"error": "No valid historical revenue data found for this ticker."}

        n_valid = len(revenue_series)

        # ── Derive historical WC and CapEx ────────────────────────────────────
        # WC = CA - CL - Cash - CPLTD
        wc_series = [
            ca_series[i] - cl_series[i] - cash_series[i] - cpltd_series[i]
            for i in range(n_valid)
        ]

        # CapEx(i) = Net PPE(i) - Net PPE(i+1) + Depr(i)
        # i=0 is most recent, i+1 is prior year
        capex_series = []
        for i in range(n_valid):
            if i + 1 < len(net_ppe_series):
                capex_val = net_ppe_series[i] - net_ppe_series[i + 1] + depr_series[i]
            else:
                # No prior year available — use depr as proxy (maintenance CapEx floor)
                capex_val = depr_series[i]
            capex_series.append(max(capex_val, 0.0))  # CapEx can't be negative


        # ── Historical ΔNWC ───────────────────────────────────────────────────
        # ΔNWC(i) = WC(i) - WC(i+1)  [i=0 most recent, i+1 prior year]
        delta_nwc_series = []
        for i in range(n_valid):
            if i + 1 < len(wc_series):
                delta_nwc_series.append(wc_series[i] - wc_series[i + 1])
            else:
                delta_nwc_series.append(0.0)

        # ── Average growth rates ───────────────────────────────────────────────
        revenue_growth = avg_yoy_growth(revenue_series)
        opex_growth    = avg_yoy_growth(opex_series)
        # Soft cap on opex: can grow faster than revenue short term but not 2x+
        # Growth discipline:
        # - clamp revenue growth to a defensible band (extreme 4-yr windows
        #   — e.g. commodity peaks — otherwise compound into nonsense)
        # - margin freeze: opex can never compound faster than revenue,
        #   which previously inverted margins for TSLA-type profiles
        raw_revenue_growth = revenue_growth
        raw_opex_growth    = opex_growth

        # Growth FADE replaces the old hard clamp of -5%..+30%.
        #
        # The clamp was wrong in both directions: the +30% ceiling arbitrarily
        # penalised genuinely fast growers, and the -5% FLOOR made declining
        # businesses look BETTER than their own data — the opposite of
        # conservative. The real defect was never the rate itself, it was
        # holding ANY observed rate constant for five years, which turns a
        # cyclical window into a permanent trend.
        #
        # Standard practice instead: growth decays from the observed rate
        # toward terminal growth across the horizon. A 40% grower is allowed
        # to be a 40% grower in year one without being capped, and a company
        # in a downcycle is not assumed to shrink forever.
        #
        # No bound is applied. Growth is now a MEDIAN of year-on-year rates,
        # so a merger year or a near-zero base year no longer drags the
        # estimate — the outlier is simply not the middle observation. That
        # removes the reason the +-60% guard existed.
        # Cash-based DCF needs a real balance sheet — refuse garbage-in
        if (not any(v for v in ca_series if v)
                and not any(v for v in cl_series if v)
                and not any(v for v in net_ppe_series if v)):
            return {"error": "Balance-sheet data unavailable for this company — "
                             "cash-based FCFF DCF is not meaningful (common for "
                             "insurers/financial conglomerates). Use the "
                             "Convergence tab's earnings-based models instead.",
                    "data_source": data_source}

        ca_growth      = avg_yoy_growth(ca_series)
        cl_growth      = avg_yoy_growth(cl_series)
        cash_growth    = avg_yoy_growth(cash_series)
        cpltd_growth   = avg_yoy_growth(cpltd_series)
        net_ppe_growth = avg_yoy_growth(net_ppe_series)
        depr_growth    = avg_yoy_growth(depr_series)

        # ── Depreciation: derive the rate, never assume a constant ──────────
        # This previously assumed a flat 5% of net PPE whenever depreciation
        # was missing. 5% is arbitrary — real rates run roughly 4-7% for heavy
        # industry, 20-30% for IT and software, 6-10% for telecom. Worse, the
        # fabricated figure flows straight into capex (capex = dPPE + depr),
        # so a guess compounds into the valuation.
        #
        # Hierarchy, best evidence first:
        #   1. the company's own reported depreciation
        #   2. the company's own historical rate (depr / prior-year net PPE)
        #   3. the median rate among sector peers ALREADY IN OUR STORE
        #   4. no estimate at all — the DCF then refuses rather than inventing
        # Rate at which the company depreciates its opening asset base
        _dr = []
        for _i in range(len(depr_series) - 1):
            _d, _p = depr_series[_i], net_ppe_series[_i + 1]
            if _d and _p and _p > 0:
                _r = _d / _p
                if 0.005 <= _r <= 0.60:
                    _dr.append(_r)
        _dr.sort()
        _depr_rate_on_ppe = _dr[len(_dr) // 2] if _dr else None

        depr_basis = "reported"
        depr_rate_used = None
        depr_peer_n = None

        if all(d == 0.0 for d in depr_series) and any(p > 0 for p in net_ppe_series):
            own_rates = []
            for i in range(len(depr_series) - 1):
                d_i, p_prev = depr_series[i], net_ppe_series[i + 1]
                if d_i and p_prev and p_prev > 0:
                    r = d_i / p_prev
                    if 0.005 <= r <= 0.60:
                        own_rates.append(r)

            if own_rates:
                own_rates.sort()
                depr_rate_used = own_rates[len(own_rates) // 2]
                depr_basis = "company's own historical rate"
            else:
                try:
                    from data_store import get_sector_depreciation_rates
                    sector_rates = get_sector_depreciation_rates()
                    entry = sector_rates.get(info.get("sector") or "")
                    if entry:
                        depr_rate_used = entry["median"]
                        depr_peer_n = entry["n_companies"]
                        depr_basis = (f"sector median ({info.get('sector')}, "
                                      f"{depr_peer_n} peers)")
                except Exception:
                    pass

            if depr_rate_used is None:
                return {"error": (
                    "Depreciation is not reported in any year, and no peer "
                    "estimate is available for this sector. A cash-flow DCF "
                    "cannot be built without it, because capital expenditure "
                    "is derived from depreciation. The earnings-based models "
                    "in the Convergence tab do not need this figure and will "
                    "still work for this company."),
                    "ticker": ticker.upper(), "market": market,
                    "data_source": data_source}

            depr_series = [n * depr_rate_used for n in net_ppe_series]
            depr_growth = net_ppe_growth

        # Cap all BS line growth rates at revenue growth
        # No balance sheet line can sustainably outgrow the business itself
        ca_growth      = min(ca_growth,      revenue_growth)
        cl_growth      = min(cl_growth,      revenue_growth)
        cash_growth    = min(cash_growth,    revenue_growth)
        cpltd_growth   = min(cpltd_growth,   revenue_growth)
        net_ppe_growth = min(net_ppe_growth, revenue_growth)
        depr_growth    = min(depr_growth,    revenue_growth)

        avg_tax_rate = sum(tax_rates) / len(tax_rates) if tax_rates else 0.25

        # ── Build historical table — show only 5 most recent valid years ────────
        # Year 6 data used for growth rates only, not displayed
        display_years = min(n_valid, 5)
        historical_table = []
        for i in range(display_years):
            rev    = revenue_series[i]
            opex   = opex_series[i]
            capex  = capex_series[i]
            t      = tax_rates[i]
            d_nwc  = delta_nwc_series[i]

            nop    = rev - opex
            nop_at = nop * (1 - t)
            fcff   = nop_at - d_nwc - capex

            historical_table.append({
                "year":               year_labels[i],
                "revenue":            round(rev,   2),
                "operating_expenses": round(-opex,  2),
                "nop":                round(nop,   2),
                "tax_rate":           round(t,     4),
                "nop_after_tax":      round(nop_at,2),
                "delta_nwc":          round(-d_nwc, 2),
                "capex":              round(-capex, 2),
                "fcff":               round(fcff,  2),
                # Balance sheet components for transparency
                "bs_ca":              round(ca_series[i],      2),
                "bs_cl":              round(cl_series[i],      2),
                "bs_cash":            round(cash_series[i],    2),
                "bs_cpltd":           round(cpltd_series[i],   2),
                "bs_net_ppe":         round(net_ppe_series[i], 2),
                "bs_depreciation":    round(depr_series[i],    2),
                "bs_wc":              round(wc_series[i],      2),
            })

        # ── WACC ──────────────────────────────────────────────────────────────
        cost_of_equity = risk_free_rate + beta * (market_return - risk_free_rate)

        interest_expense = abs(safe_float(income_df, interest_row, 0) or 0.0)
        if total_debt > 0 and interest_expense > 0:
            # Interest expense accrues across the whole year, but total debt
            # is a point-in-time balance. Dividing by the CLOSING balance
            # misstates the rate whenever debt was raised or repaid during
            # the year — which is what made a 3-15% bound necessary. Using
            # AVERAGE debt matches the numerator's period to the denominator's
            # and the bound then rarely binds.
            _debt_series = []
            for _idx in range(len(revenue_series)):
                _d = _bs_value(balance_df, "total debt", _idx)
                if _d and _d > 0:
                    _debt_series.append(_d)
            _avg_debt = ((_debt_series[0] + _debt_series[1]) / 2
                         if len(_debt_series) >= 2 else total_debt)
            cost_of_debt = max(0.03, min(interest_expense / _avg_debt, 0.15))
        else:
            # Debt-free: the value barely matters because its weight in WACC
            # is ~0, but a plausible market rate is used rather than nothing.
            cost_of_debt = risk_free_rate + 0.02

        if market_cap:
            equity_val = market_cap
            debt_val   = total_debt if total_debt else market_cap * 0.2
        else:
            # Price/market cap unavailable — assume a standard 80/20
            # capital structure instead of collapsing to all-debt WACC
            debt_val   = total_debt if total_debt else 1.0
            equity_val = debt_val * 4.0
        total_capital = equity_val + debt_val

        wacc = (
            (equity_val / total_capital) * cost_of_equity +
            (debt_val   / total_capital) * cost_of_debt * (1 - avg_tax_rate)
        )

        if wacc <= terminal_growth_rate:
            return {"error": f"WACC ({wacc:.2%}) must be greater than terminal growth rate ({terminal_growth_rate:.2%})."}

        # ── Project Balance Sheet lines Years 1-N ─────────────────────────────
        base_ca      = ca_series[0]
        base_cl      = cl_series[0]
        base_cash    = cash_series[0]
        base_cpltd   = cpltd_series[0]
        base_net_ppe = net_ppe_series[0]
        base_depr    = depr_series[0]
        base_rev     = revenue_series[0]
        base_opex    = opex_series[0]

        proj_ca_list      = project_line(base_ca,      ca_growth,      projection_years)
        proj_cl_list      = project_line(base_cl,      cl_growth,      projection_years)
        proj_cash_list    = project_line(base_cash,    cash_growth,    projection_years)
        proj_cpltd_list   = project_line(base_cpltd,   cpltd_growth,   projection_years)
        proj_net_ppe_list = project_line(base_net_ppe, net_ppe_growth, projection_years)
        proj_depr_list    = project_line(base_depr,    depr_growth,    projection_years)
        def project_fading(base, start_g, end_g, years):
            """Growth decays linearly from the observed rate to terminal
            growth. Year 1 uses the observed rate; the final year uses
            terminal growth."""
            out, v = [], base
            for i in range(years):
                # Divide by `years`, not `years - 1`, so the final projected
                # year is still one step ABOVE terminal growth and the
                # business only reaches steady state in the terminal year.
                # Fading fully to terminal by year five assumes a durable
                # franchise decays to economy-level growth within five years,
                # which costs a stable compounder ~13% of its value for no
                # good reason.
                g = start_g if years == 1 else start_g + (end_g - start_g) * (i / years)
                v = v * (1 + g)
                out.append(v)
            return out

        proj_rev_list  = project_fading(base_rev, revenue_growth,
                                        terminal_growth_rate, projection_years)
        growth_path    = [round(revenue_growth if projection_years == 1
                          else revenue_growth + (terminal_growth_rate - revenue_growth)
                               * (i / projection_years), 4)
                          for i in range(projection_years)]

        # ── Operating margin: forecast the MARGIN, derive the cost line ──────
        # Historical margins, newest first
        hist_margins = []
        for i in range(min(len(revenue_series), len(opex_series))):
            r_i, o_i = revenue_series[i], opex_series[i]
            if r_i and r_i > 0 and o_i is not None:
                hist_margins.append((r_i - o_i) / r_i)

        base_margin = (base_rev - base_opex) / base_rev if base_rev else 0.0
        if margin_basis == "average" and hist_margins:
            margin_used = sum(hist_margins) / len(hist_margins)
        elif margin_basis == "median" and hist_margins:
            s = sorted(hist_margins)
            margin_used = s[len(s) // 2]
        elif margin_basis == "custom" and custom_margin:
            margin_used = custom_margin
        else:
            margin_basis = "current"
            margin_used = base_margin

        # ── Fixed vs variable cost decomposition ────────────────────────────
        # Textbook practice splits costs into a variable part that moves with
        # revenue and a fixed part that grows with inflation. Fitted by least
        # squares: opex = fixed + variable_rate * revenue.
        #
        # It is applied ONLY when the fit is economically sensible. For a
        # commodity producer like NALCO, revenue moves with OUTPUT prices
        # while costs move with INPUT prices (alumina, power, coal) — the two
        # are not mechanically linked, and the regression returns a negative
        # variable rate, which is meaningless. In that case the model falls
        # back to a flat margin and says so.
        cost_model = "flat margin"
        fixed_cost = variable_rate = None
        _pairs = [(revenue_series[i], opex_series[i])
                  for i in range(min(len(revenue_series), len(opex_series)))
                  if revenue_series[i] and opex_series[i] is not None
                  and revenue_series[i] > 0]
        if use_cost_split and len(_pairs) >= 4:
            _n = len(_pairs)
            _sx = sum(p[0] for p in _pairs)
            _sy = sum(p[1] for p in _pairs)
            _sxx = sum(p[0] * p[0] for p in _pairs)
            _sxy = sum(p[0] * p[1] for p in _pairs)
            _den = _n * _sxx - _sx * _sx
            if _den:
                _slope = (_n * _sxy - _sx * _sy) / _den
                _icept = (_sy - _slope * _sx) / _n
                _ybar = _sy / _n
                _ss_tot = sum((p[1] - _ybar) ** 2 for p in _pairs)
                _ss_res = sum((p[1] - (_icept + _slope * p[0])) ** 2 for p in _pairs)
                _r2 = 1 - (_ss_res / _ss_tot) if _ss_tot else 0
                # Sensible only if costs genuinely rise with revenue, the
                # fixed component is not negative, and the fit explains most
                # of the variation.
                if 0.05 < _slope < 1.0 and _icept >= 0 and _r2 >= 0.70:
                    variable_rate, fixed_cost = _slope, _icept
                    cost_model = (f"fixed + variable (variable {_slope*100:.0f}% "
                                  f"of revenue, R² {_r2:.2f})")

        if variable_rate is not None:
            # Fixed costs grow with inflation, proxied by terminal growth
            proj_opex_list = [fixed_cost * ((1 + terminal_growth_rate) ** (i + 1))
                              + variable_rate * r
                              for i, r in enumerate(proj_rev_list)]
        else:
            # Costs follow revenue at the assumed margin. Holding the margin
            # flat makes NO claim that the business improves.
            proj_opex_list = [r * (1 - margin_used) for r in proj_rev_list]

        # ── Build projection table ────────────────────────────────────────────
        projection_table = []
        pv_fcffs         = []
        # ── Working capital: driven by activity, not by four growth curves ──
        # Current assets, current liabilities, cash and CPLTD were each
        # projected with their own compounding growth rate. Nothing tied them
        # to the business, so DeltaNWC was effectively arbitrary.
        #
        # Standard practice instead: receivables scale with REVENUE (days
        # sales outstanding) while inventory and payables scale with COST OF
        # REVENUE (days inventory / days payable outstanding). Those ratios
        # are computed from the company's own history and held constant.
        def _series_from(df, *keywords):
            if df is None or df.empty:
                return []
            for idx in df.index:
                if all(k.lower() in idx.lower() for k in keywords):
                    out = []
                    for col in df.columns:
                        try:
                            v = float(df.loc[idx, col])
                            out.append(None if v != v else v)
                        except Exception:
                            out.append(None)
                    return out
            return []

        ar_series   = _series_from(balance_df, "accounts receivable")
        inv_series  = _series_from(balance_df, "inventory")
        ap_series   = _series_from(balance_df, "accounts payable")
        cogs_series = _series_from(income_df,  "cost of revenue")

        def _median_ratio(numer, denom):
            vals = []
            for i in range(min(len(numer), len(denom))):
                n_i, d_i = numer[i], denom[i]
                if n_i is not None and d_i and d_i > 0:
                    r = n_i / d_i
                    if 0 < r < 3:            # discard extraction errors
                        vals.append(r)
            if not vals:
                return None
            vals.sort()
            return vals[len(vals) // 2]

        dso_ratio = _median_ratio(ar_series,  revenue_series)      # AR / revenue
        dio_ratio = _median_ratio(inv_series, cogs_series or opex_series)
        dpo_ratio = _median_ratio(ap_series,  cogs_series or opex_series)

        wc_method = "components (DSO/DIO/DPO)" if (
            dso_ratio is not None and dio_ratio is not None
            and dpo_ratio is not None) else "aggregate current assets/liabilities"

        # Asset intensity: net PP&E per unit of revenue, from this company's
        # own history. Median, so one heavy-capex year does not set the norm.
        _ai = []
        for _i in range(min(len(net_ppe_series), len(revenue_series))):
            _p, _r = net_ppe_series[_i], revenue_series[_i]
            if _p and _r and _r > 0:
                _ratio = _p / _r
                if 0.01 <= _ratio <= 10:
                    _ai.append(_ratio)
        _ai.sort()
        asset_intensity = _ai[len(_ai) // 2] if _ai else None

        prev_wc          = wc_series[0]   # base WC = most recent historical year

        for year in range(projection_years):
            idx = year  # 0-indexed

            proj_ca      = proj_ca_list[idx]
            proj_cl      = proj_cl_list[idx]
            proj_csh     = proj_cash_list[idx]
            proj_cpltd   = proj_cpltd_list[idx]
            proj_net_ppe = proj_net_ppe_list[idx]
            # Depreciation is charged against the asset base that exists at
            # the START of the year, which is the standard schedule:
            #   Ending PP&E = Beginning PP&E + CapEx - Depreciation
            # Projecting it with its own independent growth rate let it drift
            # away from the PP&E it is supposed to be depreciating.
            _begin_ppe = proj_net_ppe_list[idx - 1] if idx > 0 else base_net_ppe
            if _depr_rate_on_ppe and _begin_ppe:
                proj_depr = _depr_rate_on_ppe * _begin_ppe
            else:
                proj_depr = proj_depr_list[idx]
            proj_rev     = proj_rev_list[idx]
            proj_opex    = proj_opex_list[idx]

            # WC derived from projected BS lines
            if wc_method.startswith("components"):
                # Receivables follow revenue; inventory and payables follow
                # cost of revenue, which is what they actually scale with.
                _cogs_proj = proj_rev * (1 - margin_used)
                _ar  = dso_ratio * proj_rev
                _inv = dio_ratio * _cogs_proj
                _ap  = dpo_ratio * _cogs_proj
                proj_wc = _ar + _inv - _ap
            else:
                proj_wc = proj_ca - proj_cl - proj_csh - proj_cpltd

            # ΔNWC = WC(this year) - WC(prior year)
            proj_delta_nwc = proj_wc - prev_wc
            prev_wc        = proj_wc

            # CapEx = Net Fixed Assets(this year) - Net Fixed Assets(prior year)
            #         + Depreciation(this year)
            #
            # The identity is right; the INPUT was wrong. Net PP&E used to be
            # projected with its own independent growth rate, so a company in
            # a build-out phase (VBL, DOMS) had that phase extrapolated for
            # ever: PP&E compounded faster than revenue, capex followed it,
            # and free cash flow was negative in every year by construction.
            #
            # PP&E is now tied to revenue through ASSET INTENSITY (PP&E per
            # rupee of revenue), held at the company's own historical level.
            # Because revenue growth fades, PP&E growth fades with it, and
            # capex falls toward maintenance level naturally rather than
            # assuming perpetual construction.
            # The standard derivation. Deliberately NOT normalised against a
            # historical capex/revenue ratio: smoothing would hide genuine
            # capital intensity, and for a company in a heavy investment cycle
            # the honest output is a warning (see the all-negative-FCFF guard),
            # not a comfortable number.
            prior_net_ppe = proj_net_ppe_list[idx - 1] if idx > 0 else base_net_ppe
            if asset_intensity:
                # PP&E follows revenue at constant asset intensity
                proj_net_ppe = asset_intensity * proj_rev
                proj_net_ppe_list[idx] = proj_net_ppe
            proj_capex = max(proj_net_ppe - prior_net_ppe + proj_depr, proj_depr)

            proj_nop    = proj_rev - proj_opex
            proj_nop_at = proj_nop * (1 - avg_tax_rate)
            proj_fcff   = proj_nop_at - proj_delta_nwc - proj_capex

            pv = proj_fcff / ((1 + wacc) ** (year + 1))

            projection_table.append({
                "year":               f"Year {year + 1}",
                "revenue":            round(proj_rev,         2),
                "operating_expenses": round(-proj_opex,        2),
                "nop":                round(proj_nop,         2),
                "tax_rate":           round(avg_tax_rate,     4),
                "nop_after_tax":      round(proj_nop_at,      2),
                "delta_nwc":          round(-proj_delta_nwc,   2),
                "capex":              round(-proj_capex,        2),
                "fcff":               round(proj_fcff,         2),
                "pv_fcff":            round(pv,                2),
                # Projected BS components
                "bs_ca":              round(proj_ca,           2),
                "bs_cl":              round(proj_cl,           2),
                "bs_cash":            round(proj_csh,          2),
                "bs_cpltd":           round(proj_cpltd,        2),
                "bs_net_ppe":         round(proj_net_ppe,      2),
                "bs_depreciation":    round(proj_depr,         2),
                "bs_wc":              round(proj_wc,           2),
            })

            pv_fcffs.append(round(pv, 2))

        total_pv_fcff = sum(pv_fcffs)

        # ── Terminal Year ─────────────────────────────────────────────────────────
        # Revenue & OpEx: grow at terminal_growth_rate from last projected year
        # BS lines: rolling avg of last 5 projected years (mean-reverting)
        # Economic constraint (Damodaran): no company outgrows the economy in
        # perpetuity, and the risk-free rate is the standard proxy for
        # long-run nominal economic growth. This replaces the arbitrary
        # "WACC - g >= 4%" floor with the actual reason such a floor existed.
        if terminal_growth_rate > risk_free_rate:
            terminal_growth_capped = True
            terminal_growth_rate = risk_free_rate
        else:
            terminal_growth_capped = False

        term_rev     = proj_rev_list[-1]  * (1 + terminal_growth_rate)
        term_opex    = term_rev * (1 - margin_used)
        term_ca      = rolling_avg_terminal(proj_ca_list)
        term_cl      = rolling_avg_terminal(proj_cl_list)
        term_cash    = rolling_avg_terminal(proj_cash_list)
        term_cpltd   = rolling_avg_terminal(proj_cpltd_list)
        term_net_ppe = rolling_avg_terminal(proj_net_ppe_list)
        term_depr    = rolling_avg_terminal(proj_depr_list)

        # WC and CapEx from terminal BS
        term_wc_raw    = term_ca - term_cl - term_cash - term_cpltd
        # Terminal WC should not be lower than last projected year WC for a going concern
        # If rolling avg pulls it down, use last projected WC grown at terminal_growth_rate
        last_proj_wc   = proj_ca_list[-1] - proj_cl_list[-1] - proj_cash_list[-1] - proj_cpltd_list[-1]
        term_wc        = max(term_wc_raw, last_proj_wc * (1 + terminal_growth_rate))
        if wc_method.startswith("components"):
            _term_cogs = term_rev * (1 - margin_used)
            term_wc = (dso_ratio * term_rev + dio_ratio * _term_cogs
                       - dpo_ratio * _term_cogs)
        term_delta_nwc = term_wc - prev_wc   # prev_wc = WC at end of last projected year
        # CapEx floor = depreciation (minimum maintenance CapEx)
        # In perpetuity a business cannot keep investing above replacement
        # level. Terminal capex = depreciation (replace what wears out) plus
        # only what is needed to support terminal growth in the asset base.
        if asset_intensity:
            term_net_ppe = asset_intensity * term_rev
        term_capex = max(term_net_ppe - proj_net_ppe_list[-1] + term_depr, term_depr)

        term_nop    = term_rev - term_opex
        term_nop_at = term_nop * (1 - avg_tax_rate)
        term_fcff   = term_nop_at - term_delta_nwc - term_capex

        terminal_year = {
            "year":               f"Year {projection_years + 1} (Terminal)",
            "revenue":            round(term_rev,         2),
            "operating_expenses": round(-term_opex,        2),
            "nop":                round(term_nop,         2),
            "tax_rate":           round(avg_tax_rate,     4),
            "nop_after_tax":      round(term_nop_at,      2),
            "delta_nwc":          round(-term_delta_nwc,   2),
            "capex":              round(-term_capex,        2),
            "fcff":               round(term_fcff,         2),
            "bs_ca":              round(term_ca,           2),
            "bs_cl":              round(term_cl,           2),
            "bs_cash":            round(term_cash,         2),
            "bs_cpltd":           round(term_cpltd,        2),
            "bs_net_ppe":         round(term_net_ppe,      2),
            "bs_depreciation":    round(term_depr,         2),
            "bs_wc":              round(term_wc,           2),
        }

        # Terminal Value = FCFF_terminal / (WACC - g)
        # Gordon growth is only valid for positive terminal cash flow —
        # a negative TV would dominate EV with economically meaningless numbers
        reliability_warning = None
        if term_fcff > 0:
            terminal_value    = term_fcff / (wacc - terminal_growth_rate)
            pv_terminal_value = terminal_value / ((1 + wacc) ** projection_years)
        else:
            terminal_value    = 0.0
            pv_terminal_value = 0.0
            reliability_warning = ("Projected terminal cash flow is negative — "
                                   "terminal value excluded; DCF output is not "
                                   "meaningful for this profile.")

        # ── Enterprise & Equity Value ─────────────────────────────────────────
        enterprise_value = total_pv_fcff + pv_terminal_value

        investments_row   = (find_row(balance_df, "long term investments")
                             or find_row(balance_df, "investments"))
        minority_row      = (find_row(balance_df, "minority interest")
                             or find_row(balance_df, "noncontrolling interest"))
        investments_raw   = safe_float(balance_df, investments_row, 0) or 0.0
        minority_interest = safe_float(balance_df, minority_row,    0) or 0.0

        # Avoid double-counting: only add investments if different from total_cash
        # yfinance sometimes returns same value for both
        investments = 0.0 if abs(investments_raw - total_cash) < 1e6 else investments_raw

        # Minority interest sanity check — cap at 20% of enterprise value to avoid bad rows
        max_minority = abs(enterprise_value) * 0.20
        minority_interest = min(abs(minority_interest), max_minority)

        equity_value_dcf = (
            enterprise_value
            + total_cash
            + investments
            - total_debt
            - minority_interest
        )

        # ── Intrinsic Value ───────────────────────────────────────────────────
        intrinsic_value_per_share = equity_value_dcf / shares_outstanding
        intrinsic_value_with_mos  = intrinsic_value_per_share * (1 - margin_of_safety)

        upside_pct = None
        if current_price and current_price > 0:
            upside_pct = ((intrinsic_value_per_share - current_price) / current_price) * 100

        verdict = (
            "Potentially Undervalued" if upside_pct is not None and upside_pct > 20
            else "Potentially Overvalued" if upside_pct is not None and upside_pct < -20
            else "Fairly Valued" if upside_pct is not None
            else None
        )
        # A valuation resting ENTIRELY on terminal value is not a defensible
        # DCF: if the business is projected to burn cash every single year,
        # the model cannot simultaneously claim a large terminal worth.
        _proj_fcffs = [r.get("fcff") for r in projection_table
                       if r.get("fcff") is not None]
        if _proj_fcffs and all(f < 0 for f in _proj_fcffs) and not reliability_warning:
            reliability_warning = (
                "Every projected year shows negative free cash flow, so the entire "
                "valuation would rest on terminal value alone. This usually means "
                "the company is in a heavy investment cycle that the model cannot "
                "reliably extrapolate — treat the earnings-based models in "
                "Convergence as primary for this stock.")

        if intrinsic_value_per_share < 0 and not reliability_warning:
            reliability_warning = ("Negative intrinsic value — projected cash "
                                   "flows don't support a meaningful DCF for "
                                   "this company profile.")
        if reliability_warning:
            verdict = "Not Meaningful"

        # ── Response ──────────────────────────────────────────────────────────
        return {
            "ticker":        raw_ticker,
            "market":        market,
            "data_source":   data_source,
            "reliability_warning": reliability_warning,
            "data_quality_warnings": _dq_warnings,
            "current_price": current_price,

            # Growth rates used
            "derived_growth_rates": {
                "revenue_growth":  round(revenue_growth,  4),
                "opex_growth":     round(opex_growth,     4),
                "ca_growth":       round(ca_growth,       4),
                "cl_growth":       round(cl_growth,       4),
                "cash_growth":     round(cash_growth,     4),
                "cpltd_growth":    round(cpltd_growth,    4),
                "net_ppe_growth":  round(net_ppe_growth,  4),
                "depr_growth":     round(depr_growth,     4),
                "terminal_growth": terminal_growth_rate,
            },

            # Model assumptions
            "historical_years_used": display_years,
            "avg_tax_rate_used":     round(avg_tax_rate,    4),
            "wacc":                  round(wacc,            4),
            "cost_of_equity":        round(cost_of_equity,  4),
            "cost_of_debt":          round(cost_of_debt,    4),
            "beta_used":             beta,
            "projection_years":      projection_years,

            # Full model tables
            "historical_table": historical_table,
            "projection_table": projection_table,
            "terminal_year":    terminal_year,

            # Summary
            "pv_of_fcffs":       pv_fcffs,
            "total_pv_fcff":     round(total_pv_fcff,     2),
            "terminal_value":    round(terminal_value,    2),
            "pv_terminal_value": round(pv_terminal_value, 2),

            # Equity bridge
            "total_cash":         total_cash,
            "investments":        round(investments,       2),
            "total_debt":         total_debt,
            "minority_interest":  round(minority_interest, 2),
            "shares_outstanding": shares_outstanding,

            # Output
            "enterprise_value":                     round(enterprise_value,          2),
            "equity_value_dcf":                     round(equity_value_dcf,          2),
            "intrinsic_value_per_share":             round(intrinsic_value_per_share, 2),
            "intrinsic_value_with_margin_of_safety": round(intrinsic_value_with_mos,  2),
            "margin_of_safety_used":                 margin_of_safety,
            "assumptions_applied": {
                "revenue_growth": {
                    "raw_from_data":  round(raw_revenue_growth, 4),
                    "year_one_used":  round(revenue_growth, 4),
                    "data_error_guard_applied":
                        abs(raw_revenue_growth - revenue_growth) > 1e-9,
                    "note": ("No growth cap is applied. The +-60% bound is a "
                             "data-error guard for near-zero base years and "
                             "merger artefacts, not a view on achievable growth."),
                },
                "opex_growth_note": (
                    "Operating expenses are no longer projected with their own "
                    "growth rate. They are derived from revenue at the margin "
                    "shown below, which is why the old 'margin freeze' setting "
                    "has been removed — it can no longer be needed."
                ),
                "beta": {
                    "source":        beta_source,
                    "raw_from_data": round(raw_beta, 3),
                    "used":          round(beta, 3),
                    "clamped":       abs(raw_beta - beta) > 1e-9,
                    "bounds":        [beta_floor, beta_ceiling],
                    "computed_beta": (round(computed_beta, 3)
                                      if computed_beta is not None else None),
                    "regression_r2": (round(beta_r2, 3)
                                      if beta_r2 is not None else None),
                    "note": ("Beta is regressed from our own daily returns "
                             "against an equal-weight index of the same market. "
                             "R² shows how much of this stock's movement the "
                             "market explains — a low R² means beta is a weak "
                             "descriptor for this company regardless of its value."),
                },
                "growth_fade": {
                    "observed_rate":  round(raw_revenue_growth, 4),
                    "year_by_year":   growth_path,
                    "fades_to":       round(terminal_growth_rate, 4),
                    "note": ("Growth decays from the observed rate toward "
                             "terminal growth rather than being held flat, so "
                             "a single cyclical window is not projected as a "
                             "permanent trend."),
                },
                "terminal_growth": {
                    "used":    round(terminal_growth_rate, 4),
                    "capped_at_risk_free": terminal_growth_capped,
                    "note": ("Terminal growth cannot exceed the risk-free rate — "
                             "no company outgrows the economy in perpetuity."),
                },
                "capital_intensity": {
                    "net_ppe_to_revenue": (round(asset_intensity, 3)
                                           if asset_intensity else None),
                    "note": ("Net PP&E is projected at this company's own asset "
                             "intensity rather than an independent growth rate, "
                             "so investment scales with the business and falls "
                             "back toward replacement level as growth fades. "
                             "Previously a build-out phase was extrapolated "
                             "indefinitely, which made free cash flow negative "
                             "in every year by construction."),
                },
                "working_capital": {
                    "method": wc_method,
                    "ar_to_revenue":   round(dso_ratio, 4) if dso_ratio else None,
                    "inv_to_cogs":     round(dio_ratio, 4) if dio_ratio else None,
                    "ap_to_cogs":      round(dpo_ratio, 4) if dpo_ratio else None,
                    "days_sales_outstanding": (round(dso_ratio * 365)
                                               if dso_ratio else None),
                    "days_inventory":         (round(dio_ratio * 365)
                                               if dio_ratio else None),
                    "days_payable":           (round(dpo_ratio * 365)
                                               if dpo_ratio else None),
                    "note": ("Receivables are driven by revenue and inventory / "
                             "payables by cost of revenue, using this company's "
                             "own historical ratios. Where those components are "
                             "not available the model falls back to aggregate "
                             "current assets and liabilities, which is weaker."),
                },
                "depreciation": {
                    "basis":      depr_basis,
                    "rate_used":  (round(depr_rate_used, 4)
                                   if depr_rate_used is not None else None),
                    "peer_count": depr_peer_n,
                    "note": ("Where depreciation is not reported, the rate is "
                             "derived from the company's own history or from "
                             "sector peers in the store — never from a fixed "
                             "constant, because the figure flows directly into "
                             "projected capital expenditure."),
                },
                "cost_model": {
                    "method":        cost_model,
                    "fixed_cost":    round(fixed_cost, 0) if fixed_cost else None,
                    "variable_rate": round(variable_rate, 4) if variable_rate else None,
                    "note": ("A fixed/variable split is used only when costs "
                             "demonstrably rise with revenue (positive variable "
                             "rate, non-negative fixed component, R² >= 0.70). "
                             "For commodity producers, revenue moves with output "
                             "prices while costs move with input prices, so the "
                             "fit fails and a flat margin is used instead."),
                },
                "operating_margin": {
                    "basis":            margin_basis,
                    "used":             round(margin_used, 4),
                    "latest_year":      round(base_margin, 4),
                    "historical_range": ([round(min(hist_margins), 4),
                                          round(max(hist_margins), 4)]
                                         if hist_margins else None),
                    "historical_avg":   (round(sum(hist_margins)/len(hist_margins), 4)
                                         if hist_margins else None),
                    "note": ("Costs are projected as a function of revenue at "
                             "this margin, so margins stay flat unless you "
                             "change the basis. Previously revenue and costs "
                             "were projected independently, which let margins "
                             "drift far outside anything the company had "
                             "achieved."),
                },
                "note": ("These guardrails are deliberately conservative and "
                         "lower intrinsic value. Adjust them to see how much "
                         "of the valuation depends on them."),
            },
            "upside_downside_pct":                   round(upside_pct, 2) if upside_pct is not None else None,
            "verdict":                               verdict,
        }

    except Exception as e:
        return {"error": str(e)}
# ─────────────────────────────────────────────────────────────────────────────
#  /reverse-dcf  — What growth rate does the current price imply?
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/reverse-dcf")
def get_reverse_dcf(
    ticker: str = Query(...),
    market: str = Query("us"),
    advanced: bool = Query(False),
    risk_free_rate: float = Query(0.04),
    market_return: float = Query(0.10),
    terminal_growth_rate: float = Query(0.03),
    projection_years: int = Query(5),
):
    """
    Reverse DCF: given the current market price, solve for the implied
    revenue growth rate that justifies it.
    Uses binary search over growth_rate in [-10%, +100%].
    """
    try:
        resolved, use_fmp = resolve_ticker(ticker, market, advanced)

        if use_fmp:
            info = get_fmp_profile(resolved)
            if not info:
                return {"error": f"FMP returned no data for {resolved}"}

            current_price      = info.get("currentPrice")
            shares_outstanding = info.get("sharesOutstanding")
            beta               = info.get("beta") or 1.0
            market_cap         = info.get("marketCap")
            total_debt         = info.get("totalDebt") or 0
            total_cash         = info.get("totalCash") or 0

            # Pull latest income + cashflow statements
            inc_list = get_fmp_income(resolved, 3)
            cf_list  = get_fmp_cashflow(resolved, 1)

            if not inc_list:
                return {"error": f"FMP income data unavailable for {resolved}"}

            inc0 = inc_list[0]
            base_revenue = float(inc0.get("revenue") or 0)
            cogs         = float(inc0.get("costOfRevenue") or 0)
            sgna         = float(inc0.get("operatingExpenses") or 0)
            base_opex    = (cogs + sgna) if (cogs + sgna) > 0 else float(inc0.get("costAndExpenses") or 0)

            pretax   = float(inc0.get("incomeBeforeTax") or 0)
            tax_prov = float(inc0.get("incomeTaxExpense") or 0)
            avg_tax_rate = max(0.05, min(abs(tax_prov / pretax), 0.40)) if pretax != 0 else 0.25

            cf0 = cf_list[0] if cf_list else {}
            base_depr        = abs(float(cf0.get("depreciationAndAmortization") or 0))
            base_capex_proxy = abs(float(cf0.get("capitalExpenditure") or 0)) or base_depr

        else:
            stock = yf.Ticker(resolved)
            info  = stock.info
            current_price      = info.get("currentPrice")
            shares_outstanding = info.get("sharesOutstanding")
            beta               = info.get("beta", 1.0) or 1.0
            market_cap         = info.get("marketCap")
            total_debt         = info.get("totalDebt", 0) or 0
            total_cash         = info.get("totalCash", 0) or 0

            income_df   = stock.financials
            cashflow_df = stock.cashflow

            if income_df is None or income_df.empty:
                return {"error": "Income statement not available for this ticker (yfinance)."}
            if cashflow_df is None or cashflow_df.empty:
                return {"error": "Cash flow statement not available for this ticker (yfinance)."}

            def find_row(df, *kw):
                for idx in df.index:
                    if all(k.lower() in idx.lower() for k in kw):
                        return idx
                return None

            def sf(df, row, col=0):
                if row is None: return None
                try:
                    v = df.loc[row].iloc[col]
                    return float(v) if v is not None and str(v) != "nan" else None
                except: return None

            rev_row  = find_row(income_df, "total revenue") or find_row(income_df, "revenue")
            opex_row = find_row(income_df, "total operating expenses") or find_row(income_df, "cost of revenue")
            tax_row  = find_row(income_df, "tax", "provision")
            pre_row  = find_row(income_df, "pretax")
            depr_row = find_row(cashflow_df, "depreciation")

            base_revenue = sf(income_df, rev_row) or 0
            base_opex    = abs(sf(income_df, opex_row) or 0)
            pretax_v     = sf(income_df, pre_row) or 0
            tax_v        = sf(income_df, tax_row) or 0
            avg_tax_rate = max(0.05, min(abs(tax_v / pretax_v), 0.40)) if pretax_v != 0 else 0.25
            base_depr    = abs(sf(cashflow_df, depr_row) or 0)
            base_capex_proxy = base_depr

        if not current_price or not shares_outstanding or not base_revenue:
            return {"error": "Insufficient data for Reverse DCF."}

        # Use market_cap as the equity target (avoids shares_outstanding precision issues)
        # Add back debt, subtract cash to get implied EV, then solve for growth
        target_equity = market_cap if market_cap else (current_price * shares_outstanding if (current_price and shares_outstanding) else None)
        if not target_equity or target_equity <= 0:
            return {"error": "Cannot determine market cap — check ticker."}

        # Guard: base_opex=0 causes div/zero in terminal formula
        if base_revenue == 0:
            return {"error": "Base revenue is zero — cannot run Reverse DCF."}
        safe_base_opex = base_opex if base_opex > 0 else base_revenue * 0.6

        cost_of_equity = risk_free_rate + beta * (market_return - risk_free_rate)
        equity_val     = market_cap if market_cap else 1
        debt_val       = total_debt if total_debt else equity_val * 0.2
        total_capital  = equity_val + debt_val
        wacc = (
            (equity_val / total_capital) * cost_of_equity +
            (debt_val   / total_capital) * 0.06 * (1 - avg_tax_rate)
        )

        if wacc <= terminal_growth_rate:
            return {"error": "WACC must be greater than terminal growth rate."}

        # target_equity already set above from market_cap — do NOT recompute here
        def compute_equity_value(growth_rate: float) -> float:
            """Returns equity value = EV + cash - debt"""
            g = max(min(growth_rate, 1.0), -0.5)
            pv_total  = 0.0
            prev_rev  = base_revenue
            prev_opex = safe_base_opex

            for yr in range(1, projection_years + 1):
                rev    = prev_rev  * (1 + g)
                opex   = prev_opex * (1 + g * 0.9)  # opex grows slightly slower than revenue
                nop_at = (rev - opex) * (1 - avg_tax_rate)
                capex  = base_capex_proxy * (1 + g * 0.5)   # capex scales with growth
                fcff   = nop_at - capex
                pv_total += fcff / ((1 + wacc) ** yr)
                prev_rev  = rev
                prev_opex = opex

            # Terminal value
            term_rev   = prev_rev  * (1 + terminal_growth_rate)
            term_opex  = prev_opex * (1 + terminal_growth_rate)
            term_fcff  = (term_rev - term_opex) * (1 - avg_tax_rate) - base_capex_proxy
            if wacc <= terminal_growth_rate:
                tv = 0
            else:
                tv = term_fcff / (wacc - terminal_growth_rate)
            pv_total += tv / ((1 + wacc) ** projection_years)

            return pv_total + total_cash - total_debt

        # Binary search: find growth_rate where compute_equity_value ≈ market_cap
        lo, hi = -0.10, 1.50
        implied_growth = None
        for _ in range(80):
            mid = (lo + hi) / 2
            eq  = compute_equity_value(mid)
            diff = eq - target_equity
            if abs(diff) < target_equity * 0.0005:
                implied_growth = mid
                break
            if eq < target_equity:
                lo = mid
            else:
                hi = mid
        if implied_growth is None:
            implied_growth = (lo + hi) / 2

        # Scenarios
        scenarios = []
        for label, g in [("Bear (-5%)", implied_growth - 0.05),
                          ("Bear (-2%)", implied_growth - 0.02),
                          ("Base (Implied)", implied_growth),
                          ("Bull (+2%)", implied_growth + 0.02),
                          ("Bull (+5%)", implied_growth + 0.05)]:
            eq   = compute_equity_value(g)
            ivps = eq / shares_outstanding if shares_outstanding else None
            scenarios.append({
                "scenario":             label,
                "growth_rate":          round(g * 100, 2),
                "implied_equity_value": round(eq, 0),
                "intrinsic_per_share":  round(ivps, 2) if ivps else None,
                "vs_current_price":     round(((ivps - current_price) / current_price) * 100, 1) if ivps and current_price else None,
            })

        interpretation = (
            "The market is pricing in HIGH growth expectations — stock may be expensive unless growth materializes."
            if implied_growth > 0.15 else
            "The market expects MODERATE growth — fairly valued if historical growth continues."
            if implied_growth > 0.05 else
            "The market expects LOW or NO growth — potential value opportunity if business improves."
        )

        return {
            "ticker":               resolved,
            "market":               market,
            "data_source":          "FMP" if use_fmp else "yfinance",
            "current_price":        current_price,
            "implied_growth_rate":  round(implied_growth * 100, 2),
            "wacc_used":            round(wacc * 100, 2),
            "terminal_growth_rate": round(terminal_growth_rate * 100, 2),
            "projection_years":     projection_years,
            "interpretation":       interpretation,
            "scenarios":            scenarios,
        }

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
#  /insider-transactions  — FMP for US, yfinance fallback for India
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/insider-transactions")
def get_insider_transactions(
    ticker: str = Query(...),
    market: str = Query("us"),
    limit: int = Query(20),
):
    try:
        resolved, use_fmp = resolve_ticker(ticker, market, False)

        # FMP insider trades
        data = fmp_get(f"/insider-trading/{resolved}", {"limit": limit})
        if not data or not isinstance(data, list):
            # yfinance fallback
            if not use_fmp:
                stock = yf.Ticker(resolved)
                holders = stock.insider_transactions
                if holders is not None and not holders.empty:
                    # Convert Timestamps to strings to avoid JSON serialization error
                    records = []
                    for _, row in holders.iterrows():
                        rec = {}
                        for k, v in row.items():
                            try:
                                rec[str(k)] = v.isoformat() if hasattr(v, 'isoformat') else (None if (isinstance(v, float) and math.isnan(v)) else v)
                            except Exception:
                                rec[str(k)] = str(v)
                        records.append(rec)
                    return {"source": "yfinance", "transactions": records}
            return {"error": "No insider transaction data available."}

        transactions = []
        for t in data:
            # FMP transactionType values: "P-Purchase", "S-Sale", "S-Sale+OE",
            # "A-Award", "M-Exempt", "G-Gift", "F-InKind", "D-Return"
            ttype = t.get("transactionType") or ""
            shares = t.get("securitiesTransacted") or 0
            price  = t.get("price") or 0
            transactions.append({
                "date":               t.get("transactionDate") or t.get("filingDate"),
                "insider_name":       t.get("reportingName"),
                "title":              t.get("typeOfOwner"),
                "transaction_type":   ttype,
                "shares":             shares,
                "price":              price,
                "value":              shares * price if (shares and price) else 0,
                "shares_owned_after": t.get("securitiesOwned"),
            })

        # Summary stats
        # FMP transactionType starts with "P-" for purchase, "S-" for sale
        buys  = [t for t in transactions if (t.get("transaction_type") or "").upper().startswith("P-")
                 or "purchase" in (t.get("transaction_type") or "").lower()]
        sells = [t for t in transactions if (t.get("transaction_type") or "").upper().startswith("S-")
                 or "sale" in (t.get("transaction_type") or "").lower()]
        buy_value  = sum(t.get("value") or 0 for t in buys)
        sell_value = sum(t.get("value") or 0 for t in sells)

        sentiment = "Bullish" if buy_value > sell_value * 1.5 else "Bearish" if sell_value > buy_value * 1.5 else "Neutral"

        return {
            "ticker":       resolved,
            "source":       "FMP",
            "total_transactions": len(transactions),
            "buy_count":    len(buys),
            "sell_count":   len(sells),
            "total_buy_value":  round(buy_value, 0),
            "total_sell_value": round(sell_value, 0),
            "insider_sentiment": sentiment,
            "transactions": transactions,
        }

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
#  /institutional-holders  — FMP for all markets
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/institutional-holders")
def get_institutional_holders(
    ticker: str = Query(...),
    market: str = Query("us"),
    limit: int = Query(15),
):
    try:
        resolved, _ = resolve_ticker(ticker, market, False)

        data = fmp_get(f"/institutional-holder/{resolved}", {"limit": limit})
        if not data or not isinstance(data, list):
            return {"error": "No institutional holder data available from FMP."}

        holders = []
        for h in data:
            holders.append({
                "holder":          h.get("holder"),
                "shares":          h.get("shares"),
                "date_reported":   h.get("dateReported"),
                "change":          h.get("change"),
                "change_pct":      round(h.get("change") / h.get("shares") * 100, 2) if (h.get("shares") and h.get("shares") != 0 and h.get("change") is not None) else None,
            })

        total_shares = sum(float(h.get("shares") or 0) for h in holders)
        net_change   = sum(float(h.get("change") or 0) for h in holders)

        return {
            "ticker":              resolved,
            "source":              "FMP",
            "top_holders_count":   len(holders),
            "total_shares_held":   total_shares,
            "net_institutional_change": net_change,
            "institutional_trend": "Accumulating" if net_change > 0 else "Distributing",
            "holders":             holders,
        }

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
#  /analyst-targets  — FMP analyst price targets & recommendations
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/analyst-targets")
def get_analyst_targets(
    ticker: str = Query(...),
    market: str = Query("us"),
):
    try:
        resolved, _ = resolve_ticker(ticker, market, False)

        # Price targets
        targets_data = fmp_get(f"/price-target/{resolved}", {"limit": 20})
        # Consensus
        consensus_data = fmp_get(f"/analyst-stock-recommendations/{resolved}", {"limit": 10})
        # Estimate summary
        estimates_data = fmp_get(f"/analyst-estimates/{resolved}", {"limit": 4})

        targets = []
        if isinstance(targets_data, list):
            for t in targets_data[:15]:
                pt = t.get("priceTarget") or t.get("adjPriceTarget")
                targets.append({
                    "published_date":   t.get("publishedDate"),
                    "analyst_company":  t.get("analystCompany"),
                    "analyst":          t.get("analyst"),
                    "price_target":     float(pt) if pt is not None else None,
                    "adj_price_target": t.get("adjPriceTarget"),
                    "news_title":       t.get("newsTitle"),
                    "news_url":         t.get("newsURL"),
                })

        # Aggregate consensus — filter None and zero values
        all_targets = [t["price_target"] for t in targets
                       if t.get("price_target") is not None and t["price_target"] > 0]
        consensus_target = round(sum(all_targets) / len(all_targets), 2) if all_targets else None

        recommendations = []
        if isinstance(consensus_data, list):
            for r in consensus_data[:8]:
                # FMP real field names (confirmed from FMP v3 docs):
                # analystRatingsStrongBuy, analystRatingsbuy (lowercase b), analystRatingsHold,
                # analystRatingsSell, analystRatingsStrongSell
                recommendations.append({
                    "date":         r.get("date"),
                    "strong_buy":   r.get("analystRatingsStrongBuy") or 0,
                    "buy":          r.get("analystRatingsbuy") or 0,
                    "hold":         r.get("analystRatingsHold") or 0,
                    "sell":         r.get("analystRatingsSell") or 0,
                    "strong_sell":  r.get("analystRatingsStrongSell") or 0,
                })

        # Latest recommendation tally
        latest_rec = recommendations[0] if recommendations else {}
        strong_buy  = latest_rec.get("strong_buy") or 0
        buy         = latest_rec.get("buy") or 0
        hold        = latest_rec.get("hold") or 0
        sell        = latest_rec.get("sell") or 0
        strong_sell = latest_rec.get("strong_sell") or 0
        total_analysts = strong_buy + buy + hold + sell + strong_sell
        bullish = strong_buy + buy
        bearish = sell + strong_sell
        consensus_rating = (
            "Strong Buy" if strong_buy > total_analysts * 0.4 else
            "Buy"        if bullish  > total_analysts * 0.5 else
            "Hold"       if hold     > total_analysts * 0.4 else
            "Sell"       if bearish  > total_analysts * 0.3 else
            "N/A"
        )

        # EPS estimates
        eps_estimates = []
        if isinstance(estimates_data, list):
            for e in estimates_data:
                # FMP real fields: estimatedEpsAvg (NOT estimatedEpsAverage),
                # estimatedRevenueAvg (NOT estimatedRevenueAverage)
                eps_estimates.append({
                    "date":              e.get("date"),
                    "estimated_eps_avg": e.get("estimatedEpsAvg") or e.get("estimatedEpsAverage"),
                    "estimated_eps_low": e.get("estimatedEpsLow"),
                    "estimated_eps_high": e.get("estimatedEpsHigh"),
                    "estimated_revenue_avg": e.get("estimatedRevenueAvg") or e.get("estimatedRevenueAverage"),
                    "number_analysts_eps":   e.get("numberAnalystEstimatedEps") or e.get("numberAnalystsEstimatedEps"),
                })

        return {
            "ticker":              resolved,
            "source":              "FMP",
            "consensus_price_target": consensus_target,
            "total_price_targets": len(targets),
            "consensus_rating":    consensus_rating,
            "analyst_breakdown": {
                "strong_buy": strong_buy, "buy": buy, "hold": hold,
                "sell": sell, "strong_sell": strong_sell,
                "total": total_analysts,
            },
            "price_targets":    targets,
            "recommendations":  recommendations,
            "eps_estimates":    eps_estimates,
        }

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
#  /earnings-calendar  — earnings dates + surprise history via FMP
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/earnings-calendar")
def get_earnings_calendar(
    ticker: str = Query(...),
    market: str = Query("us"),
    limit: int = Query(8),
):
    try:
        resolved, _ = resolve_ticker(ticker, market, False)

        # Historical earnings surprises
        hist_data = fmp_get(f"/historical/earning_calendar/{resolved}", {"limit": limit})
        # FMP /earning_calendar?from=&to= returns ALL companies — filter by symbol after.
        # Safer: use /historical/earning_calendar/{ticker} and filter by date >= today.
        today_str = _date.today().isoformat()
        # upcoming = entries from historical calendar with date >= today
        upcoming_data = [e for e in hist_data if isinstance(e, dict)
                         and (e.get("date") or "") >= today_str]
        if not upcoming_data:
            # Try dedicated upcoming endpoint as last resort
            raw_up = fmp_get(f"/earning_calendar/{resolved}")
            if isinstance(raw_up, list):
                upcoming_data = [e for e in raw_up if (e.get("date") or "") >= today_str]

        history = []
        if isinstance(hist_data, list):
            for e in hist_data:
                actual  = e.get("eps")
                est     = e.get("epsEstimated")
                surprise_pct = None
                if actual is not None and est and est != 0:
                    surprise_pct = round(((actual - est) / abs(est)) * 100, 2)
                beat = None
                if actual is not None and est is not None:
                    beat = float(actual) >= float(est)

                history.append({
                    "date":              e.get("date"),
                    "eps_actual":        actual,
                    "eps_estimated":     est,
                    "surprise_pct":      surprise_pct,
                    "beat":              beat,
                    "revenue_actual":    e.get("revenue") or e.get("actualRevenue"),
                    "revenue_estimated": e.get("revenueEstimated") or e.get("estimatedRevenue"),
                    "fiscal_quarter":    e.get("period") or e.get("fiscalQuarter"),
                    "time":              e.get("time"),
                })

        # Compute beat rate
        beats = [h for h in history if h.get("beat") is True]
        beat_rate = round(len(beats) / len(history) * 100, 1) if history else None
        avg_surprise = None
        surprises = [h["surprise_pct"] for h in history if h.get("surprise_pct") is not None]
        if surprises:
            avg_surprise = round(sum(surprises) / len(surprises), 2)

        upcoming = []
        if isinstance(upcoming_data, list):
            for e in upcoming_data[:3]:
                upcoming.append({
                    "date":          e.get("date"),
                    "eps_estimated": e.get("epsEstimated"),
                    "time":          e.get("time"),
                    "fiscal_quarter": e.get("period"),
                })

        return {
            "ticker":           resolved,
            "source":           "FMP",
            "beat_rate_pct":    beat_rate,
            "avg_eps_surprise_pct": avg_surprise,
            "upcoming_earnings": upcoming,
            "earnings_history":  history,
        }

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
#  /screener  — yfinance for India, FMP for US
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/screener")
def get_screener(
    tickers: str = Query(...),
    market: str = Query("us"),
    min_pe: float = Query(None), max_pe: float = Query(None),
    min_pb: float = Query(None), max_pb: float = Query(None),
    min_roe: float = Query(None), max_roe: float = Query(None),
    min_market_cap: float = Query(None), max_market_cap: float = Query(None),
    min_de: float = Query(None), max_de: float = Query(None),
    min_dividend_yield: float = Query(None), max_dividend_yield: float = Query(None),
    min_eps: float = Query(None), max_eps: float = Query(None),
    min_week_change: float = Query(None), max_week_change: float = Query(None),
):
    raw_tickers = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not raw_tickers:
        return {"error": "No valid tickers provided."}
    if len(raw_tickers) > 50:
        return {"error": "Maximum 50 tickers per request."}

    results = []

    def in_range(val, mn, mx):
        """Check if val falls within [mn, mx]. None bounds are ignored. None val passes."""
        if val is None: return True
        if mn is not None and val < mn: return False
        if mx is not None and val > mx: return False
        return True

    for raw in raw_tickers:
        resolved, use_fmp = resolve_ticker(raw, market, False)
        try:
            if use_fmp:
                # Use raw FMP profile (no balance sheet call) to avoid 100 API calls for 50 tickers
                raw = fmp_get(f"/profile/{resolved}")
                raw0 = raw[0] if isinstance(raw, list) and raw else {}
                current_price  = raw0.get("price")
                pe_ratio       = raw0.get("pe")
                pb_ratio       = raw0.get("priceToBookRatio")
                roe            = raw0.get("roe")
                market_cap     = raw0.get("mktCap")
                de_ratio       = raw0.get("debtToEquityRatio")
                dividend_yield = raw0.get("lastDiv")
                eps            = raw0.get("eps")
                week_change    = None  # FMP profile doesn't include 1-week change directly
            else:
                stock = yf.Ticker(resolved)
                info  = stock.info
                current_price  = info.get("currentPrice") or info.get("regularMarketPrice")
                pe_ratio       = info.get("trailingPE")
                pb_ratio       = info.get("priceToBook")
                roe            = info.get("returnOnEquity")
                market_cap     = info.get("marketCap")
                de_ratio       = info.get("debtToEquity")
                dividend_yield = info.get("dividendYield")
                eps            = info.get("trailingEps")
                week_change    = None
                try:
                    hist = stock.history(period="5d")
                    if hist is not None and len(hist) >= 2:
                        price_now  = float(hist["Close"].iloc[-1])
                        price_prev = float(hist["Close"].iloc[0])
                        if price_prev and price_prev != 0:
                            week_change = ((price_now - price_prev) / price_prev) * 100
                except Exception:
                    pass

            passed = all([
                in_range(pe_ratio,       min_pe,             max_pe),
                in_range(pb_ratio,       min_pb,             max_pb),
                in_range(roe,            min_roe,            max_roe),
                in_range(market_cap,     min_market_cap,     max_market_cap),
                in_range(de_ratio,       min_de,             max_de),
                in_range(dividend_yield, min_dividend_yield, max_dividend_yield),
                in_range(eps,            min_eps,            max_eps),
                in_range(week_change,    min_week_change,    max_week_change),
            ])

            results.append({
                "ticker":          resolved,
                "data_source":     "FMP" if use_fmp else "yfinance",
                "current_price":   current_price,
                "pe_ratio":        round(pe_ratio, 2)       if pe_ratio       is not None else None,
                "pb_ratio":        round(pb_ratio, 2)       if pb_ratio       is not None else None,
                "roe":             round(roe, 4)             if roe            is not None else None,
                "market_cap":      market_cap,
                "de_ratio":        round(de_ratio, 2)       if de_ratio       is not None else None,
                "dividend_yield":  round(dividend_yield, 4) if dividend_yield is not None else None,
                "eps":             round(eps, 2)             if eps            is not None else None,
                "week_change_pct": round(week_change, 2)    if week_change    is not None else None,
                "passed_filters":  passed,
            })

        except Exception as e:
            results.append({"ticker": resolved, "error": str(e), "passed_filters": False})

    passed_count = sum(1 for r in results if r.get("passed_filters"))
    return {
        "market":            market,
        "tickers_scanned":   len(results),
        "passed_count":      passed_count,
        "filters_applied": {
            "pe": [min_pe, max_pe], "pb": [min_pb, max_pb],
            "roe": [min_roe, max_roe], "market_cap": [min_market_cap, max_market_cap],
            "de": [min_de, max_de], "dividend_yield": [min_dividend_yield, max_dividend_yield],
            "eps": [min_eps, max_eps], "week_change_pct": [min_week_change, max_week_change],
        },
        "results": results,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  /ipos  — unchanged (Indian IPOs via yfinance)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/ipos")
def get_ipos():
    results = []
    IPOS = [
        {"name": "ZOMATO",  "ticker": "ZOMATO.NS",  "ipo_price": 76,   "ipo_date": "2021-07-23"},
        {"name": "PAYTM",   "ticker": "PAYTM.NS",   "ipo_price": 2150, "ipo_date": "2021-11-18"},
    ]
    for ipo in IPOS:
        stock = yf.Ticker(ipo["ticker"])
        info  = stock.info
        current_price = info.get("currentPrice")
        gain_pct = None
        if current_price:
            gain_pct = ((current_price - ipo["ipo_price"]) / ipo["ipo_price"]) * 100
        results.append({
            "name": ipo["name"], "ipo_date": ipo["ipo_date"],
            "ipo_price": ipo["ipo_price"], "current_price": current_price,
            "gain_pct": gain_pct,
        })
    return results


# ─────────────────────────────────────────────────────────────────────────────
#  /commodities  — unchanged
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/commodities")
def get_commodities():
    commodities = {
        "Gold": "GC=F", "Silver": "SI=F",
        "Crude Oil": "CL=F", "Natural Gas": "NG=F",
    }
    data = []
    for name, ticker in commodities.items():
        stock = yf.Ticker(ticker)
        info  = stock.info
        data.append({
            "name": name,
            "price": info.get("regularMarketPrice"),
            "change": info.get("regularMarketChangePercent"),
        })
    return data


# ─────────────────────────────────────────────────────────────────────────────
#  /ai-verdict  — with competitor comparison + all new data sources
# ─────────────────────────────────────────────────────────────────────────────

# ── AI VERDICT (hardened drop-in) ─────────────────────────────────────────────
# Replace the existing @app.post("/ai-verdict") block in main.py with this.
# Guarantees: ALWAYS returns JSON (never an empty body), survives Groq model
# deprecations via a fallback chain, bounded prompt size, 45s timeout.
# Requires (already in main.py): os, json, httpx, BaseModel

class AIVerdictRequest(BaseModel):
    ticker: str
    market: str = "us"
    dcf_result: dict = {}


_GROQ_MODELS = [
    "llama-3.3-70b-versatile",   # primary
    "llama-3.1-8b-instant",      # fast fallback
    "gemma2-9b-it",              # last resort
]


@app.post("/ai-verdict")
def ai_verdict(req: AIVerdictRequest):
    try:
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            return {"error": "AI Verdict unavailable: GROQ_API_KEY not configured."}

        d = req.dcf_result or {}
        summary = {
            "ticker":          req.ticker,
            "market":          req.market,
            "current_price":   d.get("current_price"),
            "intrinsic_value": d.get("intrinsic_value_per_share"),
            "upside_pct":      d.get("upside_pct"),
            "verdict":         d.get("verdict"),
            "wacc":            (d.get("assumptions") or {}).get("wacc") or d.get("wacc"),
            "growth_rates":    d.get("derived_growth_rates"),
            "data_source":     d.get("data_source"),
            "warning":         d.get("reliability_warning"),
        }

        prompt = f"""You are an equity analyst. Based on this DCF output, give a
balanced verdict. Respond with ONLY a JSON object, no markdown:

{{
  "verdict": "<one line: overall stance>",
  "confidence": "<High|Medium|Low> - <one-line reason>",
  "summary": "<3-4 sentence balanced analysis of the valuation>",
  "bull_case": "<2-3 sentences: the strongest case for upside>",
  "bear_case": "<2-3 sentences: the strongest case for downside>",
  "news_sentiment": "<one line on likely current sentiment for this stock>",
  "management_guidance": {{"capex": "N/A", "revenue": "N/A", "expansion": "N/A"}},
  "key_risks": ["<risk 1>", "<risk 2>", "<risk 3>"],
  "recent_headlines": []
}}

Rules: be specific to the numbers given; if a reliability warning is present,
lead with caution; never invent headlines - leave recent_headlines empty.

DCF DATA:
{json.dumps(summary)}"""

        last_err = None
        for model in _GROQ_MODELS:
            try:
                with httpx.Client(timeout=45.0) as client:
                    resp = client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {api_key}"},
                        json={"model": model,
                              "max_tokens": 700,
                              "temperature": 0.3,
                              "response_format": {"type": "json_object"},
                              "messages": [
                                  {"role": "system",
                                   "content": "Respond with valid JSON only."},
                                  {"role": "user", "content": prompt}]},
                    )
                if resp.status_code == 200:
                    text = (resp.json().get("choices") or [{}])[0] \
                        .get("message", {}).get("content", "")
                    try:
                        parsed = json.loads(text)
                    except Exception:
                        last_err = f"{model}: non-JSON reply"
                        continue
                    parsed.setdefault("verdict", "No verdict generated.")
                    parsed.setdefault("confidence", "Low")
                    parsed.setdefault("summary", "Analysis not available.")
                    parsed.setdefault("bull_case", "")
                    parsed.setdefault("bear_case", "")
                    parsed.setdefault("news_sentiment", "-")
                    if not isinstance(parsed.get("management_guidance"), dict):
                        parsed["management_guidance"] = {}
                    parsed.setdefault("key_risks", [])
                    parsed.setdefault("recent_headlines", [])
                    # Top-level fields the frontend header renders
                    company_name, sector = req.ticker.upper(), "-"
                    try:
                        from data_store import get_from_store
                        stored = get_from_store(req.ticker, req.market)
                        if stored:
                            company_name = stored[0].get("longName") or company_name
                            sector       = stored[0].get("sector") or sector
                    except Exception:
                        pass
                    return {"ai_verdict": parsed,
                            "model_used": model,
                            "ticker": req.ticker.upper(),
                            "company_name": company_name,
                            "sector": sector,
                            "current_price": (req.dcf_result or {}).get("current_price"),
                            "news_fed": []}
                last_err = f"{model}: HTTP {resp.status_code} {resp.text[:120]}"
            except Exception as e:
                last_err = f"{model}: {e}"

        return {"error": f"AI Verdict temporarily unavailable ({last_err})."}

    except Exception as e:
        return {"error": f"AI Verdict failed: {e}"}

# ── QUALITY & MOAT SCORE + RED-FLAG CHECKLIST (paste into main.py) ────────────
# v2: year-keyed joins (no positional misalignment), 3-state FCF flag,
# net-margin fallback for financials.

@app.get("/quality")
def get_quality(
    ticker: str = Query(...),
    market: str = Query("us"),
    source: str = Query("auto"),
):
    try:
        info, income_df, balance_df, cashflow_df, data_source = get_company_data(
            ticker=ticker, market=market, source=source
        )

        def series(df, *keywords, n=6):
            """{year_str: value} for the first fuzzy-matched row."""
            if df is None or df.empty:
                return {}
            for idx in df.index:
                if all(k.lower() in idx.lower() for k in keywords):
                    out = {}
                    for col in df.columns[:n]:
                        try:
                            yr = str(col.year)
                        except Exception:
                            yr = str(col)
                        try:
                            v = float(df.loc[idx, col])
                            if v == v:
                                out[yr] = v
                        except Exception:
                            pass
                    return out
            return {}

        def joined(a: dict, b: dict):
            """[(year, a_val, b_val)] for common years, newest first."""
            common = sorted(set(a) & set(b), reverse=True)
            return [(y, a[y], b[y]) for y in common]

        def newest(d: dict):
            if not d:
                return None
            return d[sorted(d, reverse=True)[0]]

        def desc_values(d: dict):
            return [d[y] for y in sorted(d, reverse=True)]

        revenue  = series(income_df, "total revenue")
        ni       = series(income_df, "net income")
        expenses = series(income_df, "total expenses")
        interest = series(income_df, "interest", "expense")
        depr     = series(income_df, "depreciation") or series(cashflow_df, "depreciation")
        equity   = series(balance_df, "stockholders equity") or series(balance_df, "total equity")
        debt     = series(balance_df, "total debt")
        ocf      = series(cashflow_df, "operating cash flow")
        capex    = series(cashflow_df, "capital expenditure")

        years_used = len(revenue)
        if years_used < 2:
            return {"error": "Insufficient multi-year data for quality scoring.",
                    "data_source": data_source}

        sector       = info.get("sector") or "Unknown"
        is_financial = sector in ("Financial Services", "Financials", "Banking")
        caveats = []

        # ── Component 1: ROE consistency (25) ────────────────────────────────
        roe_pairs = [(y, n_ / e) for y, n_, e in joined(ni, equity) if e]
        roe_score, roe_detail = 0, "insufficient data"
        if roe_pairs:
            vals   = [r for _, r in roe_pairs]
            avg    = sum(vals) / len(vals)
            steady = sum(1 for r in vals if r >= 0.12) / len(vals)
            roe_score  = max(0, min(25, round(min(avg / 0.15, 1.0) * 15 + steady * 10)))
            roe_detail = (f"avg ROE {avg*100:.1f}% over {len(vals)} yrs; "
                          f"{steady*100:.0f}% of years above 12%")

        # ── Component 2: Margin stability (20) ───────────────────────────────
        margin_label = "operating margin"
        margin_pairs = [(y, (r - e) / r) for y, r, e in joined(revenue, expenses) if r]
        if len(margin_pairs) < 2:
            # Financials (and any co. missing expenses): net-margin fallback
            margin_pairs = [(y, n_ / r) for y, r, n_ in joined(revenue, ni) if r]
            margin_label = "net margin"
            if is_financial:
                caveats.append("Margin component uses net margin (bank format "
                               "lacks operating-expense line).")
        m_score, m_detail = 0, "insufficient data"
        margins_desc = [m for _, m in margin_pairs]  # already newest-first
        if len(margins_desc) >= 2:
            avg_m  = sum(margins_desc) / len(margins_desc)
            spread = max(margins_desc) - min(margins_desc)
            stability = max(0.0, 1.0 - spread / max(abs(avg_m), 0.05) / 2)
            m_score  = max(0, min(20, round(min(max(avg_m, 0) / 0.15, 1.0) * 10 + stability * 10)))
            m_detail = (f"avg {margin_label} {avg_m*100:.1f}%; range "
                        f"{min(margins_desc)*100:.1f}%–{max(margins_desc)*100:.1f}%")

        # ── Component 3: Cash conversion (15) ────────────────────────────────
        conv = [o / n_ for _, o, n_ in joined(ocf, ni) if n_ and n_ > 0]
        c_score, c_detail = 0, "cash flow data unavailable"
        if conv:
            avg_conv = sum(conv) / len(conv)
            c_score  = round(min(max(avg_conv, 0) / 0.9, 1.0) * 15)
            c_detail = (f"operating cash flow averages {avg_conv*100:.0f}% of net "
                        f"income across {len(conv)} matched years")
        else:
            caveats.append("Cash conversion unscored — OCF not available.")

        # ── Component 4: Balance-sheet strength (20) ─────────────────────────
        b_score, b_detail = 0, "insufficient data"
        de_now = cover_now = None
        de_j = joined(debt, equity)
        if de_j and de_j[0][2]:
            de_now = de_j[0][1] / de_j[0][2]
        cov_j = joined(ni, interest)
        if cov_j and cov_j[0][2]:
            cover_now = (cov_j[0][1] + cov_j[0][2]) / cov_j[0][2]
        if is_financial:
            b_score  = 12
            b_detail = "financial-sector: leverage metrics not comparable; neutral score"
            caveats.append("Financial-sector company — D/E and coverage excluded from scoring.")
        else:
            s = 0
            if de_now is not None:
                s += 10 if de_now < 0.5 else 7 if de_now < 1.0 else 3 if de_now < 2.0 else 0
            if cover_now is not None:
                s += 10 if cover_now > 8 else 7 if cover_now > 4 else 3 if cover_now > 2 else 0
            elif de_now is not None and de_now < 0.1:
                s += 8
            b_score  = min(20, s)
            b_detail = (f"D/E {de_now:.2f}" if de_now is not None else "D/E n/a") + \
                       (f"; interest coverage ~{cover_now:.1f}x" if cover_now is not None else "")

        # ── Component 5: Growth quality (20) ─────────────────────────────────
        g_score, g_detail = 0, "insufficient data"
        rev_desc = desc_values(revenue)
        ni_desc  = desc_values(ni)
        if len(rev_desc) >= 3 and rev_desc[-1] > 0:
            yrs  = len(rev_desc) - 1
            cagr = (rev_desc[0] / rev_desc[-1]) ** (1 / yrs) - 1
            up_years   = sum(1 for i in range(yrs) if rev_desc[i] > rev_desc[i + 1])
            steadiness = up_years / yrs
            lever = 0
            if len(ni_desc) >= 3 and ni_desc[-1] > 0 and ni_desc[0] > 0:
                ni_cagr = (ni_desc[0] / ni_desc[-1]) ** (1 / (len(ni_desc) - 1)) - 1
                lever = 5 if ni_cagr >= cagr else 2
            g_score  = max(0, min(20, round(min(max(cagr, 0) / 0.12, 1.0) * 10 + steadiness * 5 + lever)))
            g_detail = f"revenue CAGR {cagr*100:.1f}%; grew in {up_years}/{yrs} years"

        quality_score = roe_score + m_score + c_score + b_score + g_score
        grade = ("A" if quality_score >= 80 else "B" if quality_score >= 65
                 else "C" if quality_score >= 50 else "D" if quality_score >= 35 else "F")

        # ── Munger Red-Flag Checklist ────────────────────────────────────────
        flags = []
        def flag(name, status, detail):
            flags.append({"check": name, "status": status, "detail": detail})

        if not is_financial:
            if cover_now is not None:
                flag("Interest coverage above 2x",
                     "pass" if cover_now > 2 else "fail", f"~{cover_now:.1f}x")
            else:
                flag("Interest coverage above 2x", "na", "interest expense not reported")
            if de_now is not None:
                flag("Debt/Equity below 2x",
                     "pass" if de_now < 2 else "fail", f"{de_now:.2f}")
        else:
            flag("Leverage checks", "na", "not comparable for financial-sector companies")

        fcf_j = [(y, o - abs(cx)) for y, o, cx in joined(ocf, capex)]
        if fcf_j:
            neg, tot = sum(1 for _, f_ in fcf_j if f_ < 0), len(fcf_j)
            status = ("pass" if neg * 2 < tot else
                      "warn" if neg * 2 == tot else "fail")
            flag("Free cash flow positive in most years", status,
                 f"negative FCF in {neg}/{tot} matched years")
        else:
            flag("Free cash flow positive in most years", "na", "OCF/capex not available")

        if conv:
            avg_conv = sum(conv) / len(conv)
            flag("Earnings backed by cash (OCF ≥ 50% of NI)",
                 "pass" if avg_conv >= 0.5 else "fail", f"avg {avg_conv*100:.0f}%")

        if len(rev_desc) >= 3:
            declining = rev_desc[0] < rev_desc[1] < rev_desc[2]
            flag("Revenue not in multi-year decline",
                 "fail" if declining else "pass",
                 "declined 2+ consecutive years" if declining else "no sustained decline")

        eq_desc = desc_values(equity)
        if len(eq_desc) >= 2 and ni_desc:
            shrinking_bad = eq_desc[0] < eq_desc[-1] and ni_desc[0] < 0
            flag("Equity base not eroding through losses",
                 "fail" if shrinking_bad else "pass",
                 "equity shrinking while loss-making" if shrinking_bad else "intact")

        if len(margins_desc) >= 3:
            collapse = margins_desc[0] < max(margins_desc) * 0.7 and max(margins_desc) > 0
            flag("No margin collapse from peak",
                 "warn" if collapse else "pass",
                 f"current {margins_desc[0]*100:.1f}% vs peak {max(margins_desc)*100:.1f}%"
                 if collapse else "margins near historical range")

        # ── Promoter pledging (India) ────────────────────────────────────
        # One of the highest-signal governance checks in Indian markets:
        # promoters borrowing against their own stake means a price fall can
        # force liquidation, which accelerates the fall.
        if market.lower() == "india":
            try:
                from data_store import _conn as _shconn
                with _shconn() as _shc:
                    with _shc.cursor() as _shcur:
                        _shcur.execute("""SELECT quarter_end, promoter_pct, pledged_pct
                                          FROM shareholding WHERE ticker = %s
                                          ORDER BY quarter_end DESC LIMIT 4""",
                                       (ticker.upper() if ticker.upper().endswith(".NS")
                                        else ticker.upper() + ".NS",))
                        _shrows = _shcur.fetchall()
            except Exception:
                _shrows = []

            if _shrows and _shrows[0][2] is not None:
                _pl = _shrows[0][2]
                _status = ("pass" if _pl < 5 else
                           "warn" if _pl < 25 else "fail")
                _trend = ""
                if len(_shrows) > 1 and _shrows[1][2] is not None:
                    _chg = _pl - _shrows[1][2]
                    if abs(_chg) >= 1:
                        _trend = (f", {'up' if _chg > 0 else 'down'} "
                                  f"{abs(_chg):.1f}pp from prior quarter")
                        if _chg > 0:
                            _status = "fail" if _pl >= 10 else "warn"
                flag("Promoter shares not pledged", _status,
                     f"{_pl:.1f}% of promoter holding pledged"
                     f" (as of {_shrows[0][0]}){_trend}")
                if _shrows[0][1] is not None:
                    caveats.append(f"Promoter holding {_shrows[0][1]:.1f}% "
                                   f"as of {_shrows[0][0]}.")
            else:
                flag("Promoter shares not pledged", "na",
                     "shareholding data not yet fetched for this company — "
                     "run: python news_engine.py shareholding")
        else:
            flag("Promoter share pledging", "na",
                 "applies to Indian listings only")

        fail_count = sum(1 for f_ in flags if f_["status"] == "fail")

        # ── Owner Earnings (Buffett 1986) ────────────────────────────────────
        owner = None
        shares = info.get("sharesOutstanding")
        ni_now = newest(ni)
        if ni_now is not None and shares:
            d0 = newest(depr) or 0
            cx_vals = sorted(abs(v) for v in capex.values())
            maint_capex = cx_vals[len(cx_vals) // 2] if cx_vals else d0
            oe = ni_now + d0 - maint_capex
            r, g_ = 0.10, 0.03
            if oe > 0:
                owner = {"owner_earnings": round(oe, 0),
                         "per_share_value": round(oe * (1 + g_) / (r - g_) / shares, 2),
                         "assumptions": {"discount": r, "growth": g_,
                                         "maint_capex": "median capex (proxy)"}}
            else:
                owner = {"owner_earnings": round(oe, 0), "per_share_value": None,
                         "note": "negative owner earnings — capex exceeds NI + depreciation"}

        caveats.append(f"Scored on {years_used} years of data; "
                       "longer histories give stronger signals.")

        return {
            "ticker":        ticker.upper(),
            "market":        market,
            "sector":        sector,
            "data_source":   data_source,
            "quality_score": quality_score,
            "grade":         grade,
            "components": {
                "roe_consistency":  {"score": roe_score, "max": 25, "detail": roe_detail},
                "margin_stability": {"score": m_score,  "max": 20, "detail": m_detail},
                "cash_conversion":  {"score": c_score,  "max": 15, "detail": c_detail},
                "balance_sheet":    {"score": b_score,  "max": 20, "detail": b_detail},
                "growth_quality":   {"score": g_score,  "max": 20, "detail": g_detail},
            },
            "red_flags":      flags,
            "red_flag_count": fail_count,
            "owner_earnings": owner,
            "years_used":     years_used,
            "caveats":        caveats,
        }
    except Exception as e:
        return {"error": str(e)}

# ── BASE RATES ENDPOINT (paste into main.py) ─────────────────────────────────
# Serves historical base rates: "what happened to stocks that looked like this
# one does today?" Reads pre-computed signatures from the store — no heavy
# computation at request time.
#
# Honest by construction: excess returns vs an equal-weight market proxy
# (so a bull market can't masquerade as signal), independent episodes only
# (60-day separation), and refusal to conclude below 15 episodes.

@app.get("/baserates")
def get_base_rates(
    ticker: str = Query(...),
    market: str = Query("us"),
    band: int = Query(7, description="± percentile band width for matching"),
):
    try:
        from data_store import _conn
        import numpy as np
        import pandas as pd

        MIN_EPISODES    = 15
        MIN_EPISODE_GAP = 60
        MAX_SELECTIVITY = 0.15
        HORIZONS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}

        raw = ticker.upper()
        if market.lower() == "india" and not raw.endswith(".NS"):
            raw += ".NS"

        # ── 1. This stock's current signature ────────────────────────────────
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT date, rank_vs_200dma, rank_from_high,
                                      rank_from_low, rank_mom_3m, rank_mom_12m,
                                      rank_volatility, price
                               FROM stock_signatures WHERE ticker = %s
                               ORDER BY date DESC LIMIT 1""", (raw,))
                row = cur.fetchone()
        if not row:
            return {"error": f"No price-signature history available for {raw}. "
                             f"Base rates require at least 200 trading days of "
                             f"price history in the store."}

        sig_date, r200, rhigh, rlow, m3, m12, rvol, price = row
        signature = {
            "as_of": str(sig_date),
            "price": price,
            "percentiles": {
                "vs_200dma":      round(r200, 0) if r200 is not None else None,
                "from_52wk_high": round(rhigh, 0) if rhigh is not None else None,
                "from_52wk_low":  round(rlow, 0) if rlow is not None else None,
                "momentum_3m":    round(m3, 0) if m3 is not None else None,
                "momentum_12m":   round(m12, 0) if m12 is not None else None,
                "volatility":     round(rvol, 0) if rvol is not None else None,
            },
        }

        if r200 is None or rlow is None or m12 is None:
            return {"error": "Signature incomplete for this stock — "
                             "insufficient price history.",
                    "signature": signature}

        # ── 2. Find matching historical days ─────────────────────────────────
        def bnd(v):
            return (max(0, v - band), min(100, v + band))

        flo, fhi = bnd(rlow)
        dlo, dhi = bnd(r200)
        mlo, mhi = bnd(m12)

        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '120s'")
                cur.execute("SELECT COUNT(*) FROM stock_signatures")
                total_days = cur.fetchone()[0]
                cur.execute("""
                    SELECT ticker, date FROM stock_signatures
                    WHERE market = %s
                      AND rank_from_low  BETWEEN %s AND %s
                      AND rank_vs_200dma BETWEEN %s AND %s
                      AND rank_mom_12m   BETWEEN %s AND %s
                """, (market, flo, fhi, dlo, dhi, mlo, mhi))
                matches = cur.fetchall()

        if not matches:
            return {"signature": signature,
                    "error": "No comparable historical setups found."}

        match_df = pd.DataFrame(matches, columns=["ticker", "date"])
        match_df["date"] = pd.to_datetime(match_df["date"])
        selectivity = len(match_df) / max(total_days, 1)
        tickers = list(set(match_df["ticker"]))

        # ── 3. Prices for matched tickers + market benchmark ─────────────────
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '120s'")
                cur.execute("""SELECT ticker, date, price FROM stock_signatures
                               WHERE ticker = ANY(%s)""", (tickers,))
                prows = cur.fetchall()
                cur.execute("""SELECT date, AVG(price) FROM stock_signatures
                               WHERE market = %s GROUP BY date""", (market,))
                brows = cur.fetchall()

        prices = pd.DataFrame(prows, columns=["ticker", "date", "price"])
        prices["date"] = pd.to_datetime(prices["date"])
        prices = prices.sort_values(["ticker", "date"])

        bench = pd.DataFrame(brows, columns=["date", "avg_price"])
        bench["date"] = pd.to_datetime(bench["date"])
        bench = bench.sort_values("date")
        bench["idx"] = (1 + bench["avg_price"].pct_change().fillna(0)).cumprod()
        bmap = dict(zip(bench["date"], bench["idx"]))

        # ── 4. Episodes → forward excess returns ─────────────────────────────
        results = {h: [] for h in HORIZONS}
        episode_count = 0

        for tkr, g in match_df.groupby("ticker"):
            series = prices[prices["ticker"] == tkr].reset_index(drop=True)
            if series.empty:
                continue
            pos = {d: i for i, d in enumerate(series["date"])}
            episodes, last = [], None
            for d in sorted(g["date"]):
                i = pos.get(d)
                if i is None:
                    continue
                if last is None or (i - last) >= MIN_EPISODE_GAP:
                    episodes.append(i)
                    last = i
            for start in episodes:
                episode_count += 1
                p0 = series["price"].iloc[start]
                b0 = bmap.get(series["date"].iloc[start])
                for hlabel, hdays in HORIZONS.items():
                    end = start + hdays
                    if end >= len(series) or not p0 or p0 <= 0:
                        continue
                    p1 = series["price"].iloc[end]
                    b1 = bmap.get(series["date"].iloc[end])
                    if b0 and b1 and b0 > 0:
                        results[hlabel].append((p1 / p0 - 1) - (b1 / b0 - 1))

        # ── 5. Honest summary ────────────────────────────────────────────────
        horizons_out = {}
        for hlabel, vals in results.items():
            n = len(vals)
            if n < MIN_EPISODES:
                horizons_out[hlabel] = {
                    "n_episodes": n, "confidence": "insufficient",
                    "message": f"Only {n} independent episodes "
                               f"(need {MIN_EPISODES}) — no conclusion drawn."}
                continue
            a = np.array(vals)
            horizons_out[hlabel] = {
                "n_episodes": n,
                "confidence": "low" if n < 30 else "moderate" if n < 75 else "adequate",
                "median_excess_pct":  round(float(np.median(a)) * 100, 1),
                "beat_market_pct":    round(float((a > 0).mean()) * 100, 1),
                "p25_pct":            round(float(np.percentile(a, 25)) * 100, 1),
                "p75_pct":            round(float(np.percentile(a, 75)) * 100, 1),
            }

        caveats = [
            "Returns shown are EXCESS vs an equal-weight market average — "
            "they strip out general market movement.",
            "Episodes are independent occurrences (minimum 60 trading days "
            "apart for the same stock), not individual matching days.",
            "Universe is today's index constituents — companies that were "
            "delisted or removed are absent (survivorship bias).",
            "History covers roughly 5 years: one market regime, not many. "
            "These are base rates for context, not predictions.",
        ]
        if selectivity > MAX_SELECTIVITY:
            caveats.insert(0, f"This setup matches {selectivity*100:.1f}% of all "
                              f"stock-days — broad enough that it describes the "
                              f"market more than a specific condition.")

        return {
            "ticker": raw,
            "market": market,
            "signature": signature,
            "match_criteria": {
                "band_pp": band,
                "from_52wk_low":  [round(flo), round(fhi)],
                "vs_200dma":      [round(dlo), round(dhi)],
                "momentum_12m":   [round(mlo), round(mhi)],
            },
            "selectivity_pct": round(selectivity * 100, 2),
            "total_episodes":  episode_count,
            "horizons":        horizons_out,
            "caveats":         caveats,
        }

    except Exception as e:
        return {"error": f"Base-rate analysis failed: {e}"}
# ── DATA QUALITY GATE (paste into main.py, ABOVE the /dcf block) ─────────────
# You cannot guarantee perfect XBRL extraction across 1,000 filers. You CAN
# guarantee that no valuation is ever published from inputs that fail
# accounting sanity. This runs before any model computes.
#
# Philosophy: refuse loudly rather than output a confident wrong number.

def validate_financials(info, income_df, balance_df, cashflow_df, market="us"):
    """Returns (ok: bool, reason: str|None, warnings: list[str])."""
    warnings = []

    def row(df, *keywords):
        if df is None or df.empty:
            return {}
        for idx in df.index:
            if all(k.lower() in idx.lower() for k in keywords):
                out = {}
                for col in df.columns:
                    try:
                        v = float(df.loc[idx, col])
                        if v == v:
                            out[str(col)] = v
                    except Exception:
                        pass
                return out
        return {}

    revenue  = row(income_df, "total revenue")
    expenses = row(income_df, "total expenses")
    ni       = row(income_df, "net income")
    depr     = row(income_df, "depreciation") or row(cashflow_df, "depreciation")
    interest = row(income_df, "interest", "expense")
    ocf      = row(cashflow_df, "operating cash flow")

    if not revenue:
        return False, "No revenue data available for this company.", warnings

    # ── Structural: a cash-flow DCF is the WRONG MODEL for banks and
    # insurers, not merely short of data. They have no working-capital cycle
    # (deposits are not debt, loans are not inventory) and no meaningful capex
    # cycle. Professionals use dividend-discount or residual-income models
    # here. Refusing outright is more useful than a number produced by an
    # inapplicable framework.
    sector_raw = (info.get("sector") or "")
    if sector_raw in ("Financial Services", "Financials", "Banking", "Insurance"):
        return False, (
            "A cash-flow DCF is not an appropriate framework for banks and "
            "insurers — they have no working-capital or capex cycle for the "
            "model to work with, which is why professional analysts use "
            "dividend-discount or residual-income methods instead. Use the "
            "Convergence tab (earnings-based models) or the Quality tab for "
            "this company."), warnings

    years = sorted(revenue.keys(), reverse=True)

    # The newest fiscal year is often partial — merged from a secondary source
    # before the primary filing exists, so it may carry revenue but no expense
    # line. Refusing the whole company for that would discard three or four
    # perfectly good years. Instead, fall back to the newest year that has
    # BOTH revenue and expenses, and note the substitution.
    latest = years[0]
    if expenses.get(latest) is None:
        complete = [y for y in years
                    if revenue.get(y) and expenses.get(y) is not None]
        if complete:
            skipped = latest
            latest = complete[0]
            warnings.append(
                f"FY{skipped} has revenue but no expense figure yet (the "
                f"filing is likely not published). Validation used FY{latest} "
                f"as the most recent complete year.")
    rev_l = revenue.get(latest)

    # ── 1. Revenue must exist and be positive in the latest year ─────────────
    if not rev_l or rev_l <= 0:
        return False, (f"Latest fiscal year ({latest}) has no usable revenue "
                       f"figure — valuation would be meaningless."), warnings

    # ── 2. Operating margin must be economically possible ────────────────────
    exp_l = expenses.get(latest)
    if exp_l is not None and exp_l > 0:
        margin = (rev_l - exp_l) / rev_l
        if margin > 0.95:
            return False, (f"Extracted costs ({exp_l:,.0f}) imply a {margin*100:.0f}% "
                           f"operating margin — the cost data is incomplete for this "
                           f"filer, so a cash-flow valuation cannot be trusted."), warnings
        if margin < -2.0:
            warnings.append(f"Operating margin of {margin*100:.0f}% is extreme — "
                            f"verify against the company's filings.")
    elif exp_l is None:
        return False, (f"No total-expense figure could be extracted for {latest}; "
                       f"operating profit cannot be computed."), warnings

    # ── 3. Revenue must exceed depreciation and interest (scale sanity) ──────
    d_l = depr.get(latest)
    if d_l and d_l > rev_l:
        return False, (f"Depreciation ({d_l:,.0f}) exceeds revenue ({rev_l:,.0f}) — "
                       f"revenue is understated for this filer; refusing to value."), warnings

    is_fin = (info.get("sector") or "") in ("Financial Services", "Financials")
    i_l = interest.get(latest)
    if not is_fin and i_l and i_l > rev_l:
        return False, (f"Interest expense ({i_l:,.0f}) exceeds revenue ({rev_l:,.0f}) — "
                       f"inputs are inconsistent; refusing to value."), warnings

    # ── 4. Shares outstanding required for a per-share value ─────────────────
    shares = info.get("sharesOutstanding")
    if not shares or shares <= 0:
        return False, "Shares outstanding unavailable — per-share value cannot be derived.", warnings

    # ── 5. Cash-flow sanity (warn only — many businesses legitimately burn) ──
    o_l, n_l = ocf.get(latest), ni.get(latest)
    if o_l is not None and n_l and n_l > 0:
        base = n_l + (d_l or 0)
        if base > 0:
            conv = o_l / base
            if conv < -3 or conv > 5:
                warnings.append(
                    f"Operating cash flow is {conv:.1f}x (net income + D&A) — "
                    f"unusual; treat cash-based outputs with caution.")

    # ── 6. Series continuity — a source flip fabricates growth rates ─────────
    if len(years) >= 2:
        for a, b in zip(years, years[1:]):
            va, vb = revenue.get(a), revenue.get(b)
            if va and vb and vb > 0:
                yoy = va / vb - 1
                if abs(yoy) > 3.0:
                    warnings.append(
                        f"Revenue changed {yoy*100:+.0f}% between FY{b} and FY{a} — "
                        f"if this is not a genuine merger/demerger, growth rates "
                        f"derived from it are unreliable.")
                    break

    # ── 7. Structural: cash-flow DCF is inappropriate for banks/insurers ─────
    if is_fin:
        warnings.append(
            "Financial-sector company — cash-flow DCF is not the appropriate "
            "framework (no meaningful working capital or capex cycle). Treat "
            "the earnings-based models in Convergence as primary.")

    return True, None, warnings
# ── NEWS & CORPORATE EVENTS ENDPOINT (paste into main.py) ────────────────────
# Serves the events collected by news_engine.py. Red flags first, because a
# pledge creation or auditor resignation matters more than a routine filing.

@app.get("/events")
def get_events(
    ticker: str = Query(...),
    market: str = Query("india"),
    days: int = Query(180, description="Lookback window"),
):
    try:
        from data_store import _conn
        from datetime import date, timedelta

        raw = ticker.upper()
        if market.lower() == "india" and not raw.endswith(".NS"):
            raw += ".NS"
        cutoff = date.today() - timedelta(days=days)

        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT event_date, category, severity, headline, url, sentiment
                    FROM news_events
                    WHERE ticker = %s AND event_date >= %s
                    ORDER BY
                      CASE severity
                        WHEN 'red_flag' THEN 0 WHEN 'watch' THEN 1
                        WHEN 'positive' THEN 2 ELSE 3 END,
                      event_date DESC
                    LIMIT 60
                """, (raw, cutoff))
                rows = cur.fetchall()

        events, counts = [], {"red_flag": 0, "watch": 0, "positive": 0, "info": 0}
        for d, cat, sev, head, url, sent in rows:
            counts[sev] = counts.get(sev, 0) + 1
            events.append({
                "date":      str(d),
                "category":  cat,
                "severity":  sev,
                "headline":  head,
                "url":       url,
                "sentiment": sent,
            })

        # Plain-English summary of what the red flags actually mean
        FLAG_MEANING = {
            "auditor_change":    "An auditor resignation is one of the highest-signal "
                                 "warnings in Indian markets — auditors rarely resign "
                                 "from clean engagements.",
            "pledge_created":    "Promoters pledging shares means borrowing against "
                                 "their stake. If the price falls, lenders can force "
                                 "sales, which accelerates declines.",
            "regulatory_action": "A regulatory notice or penalty can carry financial "
                                 "cost and signals governance issues.",
            "management_exit":   "An abrupt CFO or MD departure often precedes "
                                 "restatements or strategy reversals.",
            "rating_downgrade":  "A credit downgrade raises borrowing costs and may "
                                 "signal deteriorating cash flows.",
        }
        explanations = []
        for e in events:
            if e["severity"] == "red_flag" and e["category"] in FLAG_MEANING:
                m = FLAG_MEANING[e["category"]]
                if m not in explanations:
                    explanations.append(m)

        return {
            "ticker": raw,
            "market": market,
            "window_days": days,
            "counts": counts,
            "events": events,
            "explanations": explanations,
            "caveats": [
                "Sourced from NSE corporate announcements. Categories are assigned "
                "by keyword rules, so an unusual filing may be miscategorised.",
                "Absence of red flags is not a clean bill of health — it means "
                "nothing matching was filed in this window.",
            ] if market.lower() == "india" else [
                "Corporate event tracking currently covers Indian listings only."
            ],
        }
    except Exception as e:
        return {"error": f"Event lookup failed: {e}"}
# ── IDEAS SCREENER (paste into main.py) ──────────────────────────────────────
# ── IDEAS SCREENER v2 (paste into main.py) ───────────────────────────────────
# A shortlist of stocks matching a DEFINED, TESTED setup — not a buy list.
#
# Every strategy ships with the base rate this app measured on its OWN data,
# including when that result was unfavourable. Governance red flags are used
# as an EXCLUSION filter (Munger's inversion: avoid the obvious ways to lose).
#
# v2 adds: sector filter, valuation context (P/E and price vs 52wk range),
# multiple sort orders, a transparent 0-100 composite score with a per-stock
# breakdown of WHY it ranks where it does, and full company names.

@app.get("/ideas")
def get_ideas(
    market: str = Query("india"),
    strategy: str = Query("quality_momentum",
                          description="quality_momentum | quality_value | "
                                      "clean_compounders | turnaround_watch | all"),
    min_quality_score: int = Query(65),
    exclude_red_flags: bool = Query(True),
    sector: str = Query("", description="Filter to one sector, blank = all"),
    max_pe: float = Query(0, description="Max trailing P/E, 0 = no limit"),
    sort_by: str = Query("composite",
                         description="composite | quality | momentum | value | pe"),
    limit: int = Query(25),
):
    try:
        from data_store import _conn

        BASE_RATES = {
            "quality_momentum": {
                "thesis": "Durable business with strong 12-month momentum and "
                          "trading above its 200-day trend.",
                "measured": "+12.4% median excess return at 12 months, beat the "
                            "market 60% of the time (n=860 independent episodes)",
                "warning": None,
                "cond": "s.rank_mom_12m >= 75 AND s.rank_vs_200dma >= 60",
            },
            "quality_value": {
                "thesis": "High-quality business trading near its 52-week low — "
                          "classic contrarian value.",
                "measured": "-1.7% median excess at 12 months, beat the market "
                            "only 48% of the time (n=1,418 episodes)",
                "warning": "Our own data does NOT support this over 2020-2025 — "
                           "value underperformed in a growth-led market. Treat it "
                           "as a hypothesis you are testing, not a validated edge.",
                "cond": "s.rank_from_low <= 30 AND s.rank_vs_200dma <= 40",
            },
            "clean_compounders": {
                "thesis": "Consistently high returns on equity, strong cash "
                          "conversion, low leverage, no governance red flags.",
                "measured": "Not a timing signal — no forward-return claim is made",
                "warning": None,
                "cond": "TRUE",
            },
            "turnaround_watch": {
                "thesis": "Quality business, beaten down, but momentum has begun "
                          "to turn — price recovering back toward its trend.",
                "measured": "Not separately validated — a narrower variant of the "
                            "value setup, which underperformed in our sample",
                "warning": "Untested combination. Shown for research, not as a "
                           "measured edge.",
                "cond": "s.rank_from_low <= 40 AND s.rank_mom_12m BETWEEN 40 AND 70 "
                        "AND s.rank_vs_200dma BETWEEN 40 AND 70",
            },
            "all": {
                "thesis": "Every company passing the quality and governance "
                          "filters, with no timing condition applied.",
                "measured": "No setup applied — this is the full filtered universe",
                "warning": None,
                "cond": "TRUE",
            },
        }
        if strategy not in BASE_RATES:
            return {"error": f"Unknown strategy. Choose: {list(BASE_RATES)}"}
        meta = BASE_RATES[strategy]

        params = [market, min_quality_score]
        extra = ""
        if sector:
            extra += " AND c.sector = %s"
            params.append(sector)
        flag_filter = "AND COALESCE(f.red_flags, 0) = 0" if exclude_red_flags else ""

        sort_map = {
            "composite": "composite DESC",
            "quality":   "q.score DESC",
            "momentum":  "s.rank_mom_12m DESC",
            "value":     "s.rank_from_low ASC",
            "pe":        "pe_ratio ASC NULLS LAST",
        }
        order = sort_map.get(sort_by, "composite DESC")

        sql = f"""
            WITH latest_sig AS (
                SELECT DISTINCT ON (ticker) ticker, price,
                       rank_vs_200dma, rank_from_low, rank_from_high,
                       rank_mom_12m, rank_mom_3m, rank_volatility
                FROM stock_signatures ORDER BY ticker, date DESC
            ),
            latest_q AS (
                SELECT DISTINCT ON (ticker) ticker, signal AS grade,
                       (detail->>'score')::float AS score
                FROM signal_journal WHERE source = 'quality'
                ORDER BY ticker, signal_date DESC
            ),
            flags AS (
                SELECT ticker, COUNT(*) AS red_flags
                FROM news_events
                WHERE severity = 'red_flag'
                  AND event_date >= CURRENT_DATE - INTERVAL '180 days'
                GROUP BY ticker
            )
            SELECT c.ticker, c.name, c.sector, c.eps, c.shares_outstanding,
                   q.grade, q.score,
                   s.price, s.rank_vs_200dma, s.rank_from_low, s.rank_from_high,
                   s.rank_mom_12m, s.rank_mom_3m, s.rank_volatility,
                   COALESCE(f.red_flags, 0) AS red_flags,
                   CASE WHEN c.eps > 0 THEN s.price / c.eps END AS pe_ratio,
                   (  q.score * 0.45
                    + COALESCE(s.rank_mom_12m, 50) * 0.30
                    + COALESCE(s.rank_vs_200dma, 50) * 0.15
                    + (100 - COALESCE(s.rank_volatility, 50)) * 0.10
                   ) AS composite
            FROM companies c
            JOIN latest_sig s ON s.ticker = c.ticker
            JOIN latest_q   q ON q.ticker = c.ticker
            LEFT JOIN flags f ON f.ticker = c.ticker
            WHERE c.market = %s
              AND q.score >= %s
              AND {meta['cond']}
              {flag_filter}
              {extra}
        """
        if max_pe and max_pe > 0:
            sql += " AND c.eps > 0 AND (s.price / c.eps) <= %s"
            params.append(max_pe)
        sql += f" ORDER BY {order} LIMIT %s"
        params.append(limit)

        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '60s'")

                # Both source tables are INNER JOINed, so an empty one yields
                # zero results that look identical to "no stocks matched your
                # filters". Distinguish the two explicitly — a silent empty
                # list would send the user hunting through filter settings for
                # a problem that is actually a missing prerequisite.
                cur.execute("SELECT COUNT(*) FROM stock_signatures")
                n_sig = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM signal_journal WHERE source = 'quality'")
                n_q = cur.fetchone()[0]
                if n_sig == 0:
                    return {"error": "Price signatures have not been computed yet. "
                                     "Run: python signature_engine.py backfill"}
                if n_q == 0:
                    return {"error": "No quality scores recorded yet. "
                                     "Run: python signal_journal.py snapshot"}

                cur.execute(sql, params)
                rows = cur.fetchall()
                cur.execute("SELECT COUNT(DISTINCT ticker) FROM stock_signatures")
                n_sig_tickers = cur.fetchone()[0]
                cur.execute("""SELECT DISTINCT sector FROM companies
                               WHERE market = %s AND sector IS NOT NULL
                                 AND sector <> 'Unknown' ORDER BY sector""",
                            (market,))
                sectors = [r[0] for r in cur.fetchall()]

        ideas = []
        for (tkr, name, sec, eps, shares, grade, score, price, r200, rlow,
             rhigh, mom12, mom3, rvol, flags, pe, composite) in rows:

            # Transparent contribution breakdown — why this stock ranks here
            contrib = {
                "quality":      round((score or 0) * 0.45, 1),
                "momentum_12m": round((mom12 if mom12 is not None else 50) * 0.30, 1),
                "trend":        round((r200 if r200 is not None else 50) * 0.15, 1),
                "low_volatility": round((100 - (rvol if rvol is not None else 50)) * 0.10, 1),
            }

            reasons = []
            if score is not None:
                reasons.append(f"Quality {grade} ({int(score)}/100)")
            if mom12 is not None and mom12 >= 75:
                reasons.append(f"12m momentum: top {100-int(mom12)}% of market")
            elif mom12 is not None and mom12 <= 30:
                reasons.append(f"Weak 12m momentum ({int(mom12)}th pct)")
            if rlow is not None and rlow <= 30:
                reasons.append(f"Near 52-week low ({int(rlow)}th pct)")
            if rhigh is not None and rhigh >= 80:
                reasons.append("Near 52-week high")
            if r200 is not None and r200 >= 60:
                reasons.append("Above 200-day trend")
            if rvol is not None and rvol <= 30:
                reasons.append("Lower volatility than peers")
            if flags == 0:
                reasons.append("No red-flag filings (180d)")
            else:
                reasons.append(f"⚠ {flags} red-flag filing(s)")

            ideas.append({
                "ticker": tkr,
                "company_name": name or tkr.replace(".NS", ""),
                "sector": sec or "Unknown",
                "quality_grade": grade,
                "quality_score": int(score) if score is not None else None,
                "price": price,
                "pe_ratio": round(pe, 1) if pe else None,
                "eps": eps,
                "composite_score": round(composite, 1) if composite else None,
                "score_breakdown": contrib,
                "percentiles": {
                    "vs_200dma":     int(r200) if r200 is not None else None,
                    "from_52wk_low": int(rlow) if rlow is not None else None,
                    "from_52wk_high": int(rhigh) if rhigh is not None else None,
                    "momentum_12m":  int(mom12) if mom12 is not None else None,
                    "momentum_3m":   int(mom3) if mom3 is not None else None,
                    "volatility":    int(rvol) if rvol is not None else None,
                },
                "red_flags": flags,
                "reasons": reasons,
            })

        return {
            "market": market,
            "strategy": strategy,
            "thesis": meta["thesis"],
            "historical_base_rate": meta["measured"],
            "strategy_warning": meta["warning"],
            "composite_formula": ("45% quality score + 30% 12-month momentum "
                                  "+ 15% position vs 200-day trend "
                                  "+ 10% low-volatility preference"),
            "available_sectors": sectors,
            "filters_applied": {
                "min_quality_score": min_quality_score,
                "red_flags_excluded": exclude_red_flags,
                "sector": sector or "all",
                "max_pe": max_pe or None,
                "sort_by": sort_by,
            },
            "count": len(ideas),
            "universe_coverage": {
                "companies_with_signatures": n_sig_tickers,
                "companies_with_quality_scores": n_q,
            },
            "ideas": ideas,
            "disclaimers": [
                "This is a SCREEN, not investment advice. It lists companies "
                "matching defined conditions — nothing more.",
                "Base rates describe a ~5-year window covering one market "
                "regime, using today's index members only (survivorship bias). "
                "They are not predictions.",
                "No macro or market-outlook input is used. Corporate filings "
                "are used only to EXCLUDE companies with governance red flags, "
                "never to recommend one.",
                "The composite score is a ranking convenience, not a valuation. "
                "Run DCF, Convergence and Quality on anything here before "
                "forming a view, and consider speaking to a SEBI-registered "
                "adviser about your own circumstances.",
            ],
        }

    except Exception as e:
        return {"error": f"Idea screen failed: {e}"}
# ── TECHNICALS ENDPOINT (paste into main.py) ─────────────────────────────────
# Serves the price-based indicators the Trader horizon needs. Reads
# pre-computed values from stock_signatures — no computation at request time.
#
# Honest framing, applied throughout: these are DESCRIPTIONS of where price
# sits, not predictions. The base-rate engine is what turns a description
# into a measured historical tendency, and the two are shown side by side so
# the user can see how weak or strong the evidence actually is.

@app.get("/technicals")
def get_technicals(
    ticker: str = Query(...),
    market: str = Query("us"),
):
    try:
        from data_store import _conn

        raw = ticker.upper()
        if market.lower() == "india" and not raw.endswith(".NS"):
            raw += ".NS"

        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT date, price, rsi_14, dma_50, dma_200,
                           pct_vs_50dma, pct_vs_200dma,
                           pct_from_52wk_high, pct_from_52wk_low,
                           momentum_3m, momentum_12m, volatility_20d,
                           rank_vs_200dma, rank_mom_12m, rank_volatility
                    FROM stock_signatures WHERE ticker = %s
                    ORDER BY date DESC LIMIT 1
                """, (raw,))
                row = cur.fetchone()
                if not row:
                    return {"error": f"No price history for {raw}. Technicals need "
                                     f"at least 200 trading days of data."}
                # 30 days of RSI for trend context
                cur.execute("""
                    SELECT date, rsi_14, price FROM stock_signatures
                    WHERE ticker = %s ORDER BY date DESC LIMIT 30
                """, (raw,))
                recent = cur.fetchall()

        (d, price, rsi, dma50, dma200, p50, p200, phigh, plow,
         m3, m12, vol, r200, rm12, rvol) = row

        def pct(v):
            return round(v * 100, 1) if v is not None else None

        # RSI reading — conventional thresholds, with the honest caveat that
        # they are conventions, not laws
        if rsi is None:
            rsi_state, rsi_note = "unavailable", ""
        elif rsi >= 70:
            rsi_state = "overbought"
            rsi_note = ("Above 70 is conventionally 'overbought'. In practice a "
                        "strong uptrend can hold above 70 for weeks — this is a "
                        "description of recent buying pressure, not a sell signal.")
        elif rsi <= 30:
            rsi_state = "oversold"
            rsi_note = ("Below 30 is conventionally 'oversold'. Note that a stock "
                        "in genuine decline can stay below 30 for a long time.")
        else:
            rsi_state = "neutral"
            rsi_note = "Between 30 and 70 — no extreme in recent price action."

        # Trend structure from the two moving averages
        if dma50 and dma200:
            if price > dma50 > dma200:
                trend = "uptrend"
                trend_note = "Price above the 50-day, which is above the 200-day."
            elif price < dma50 < dma200:
                trend = "downtrend"
                trend_note = "Price below the 50-day, which is below the 200-day."
            elif price > dma200:
                trend = "mixed_above_200"
                trend_note = "Above the long-term average but the shorter average is not aligned."
            else:
                trend = "mixed_below_200"
                trend_note = "Below the long-term average; trend is not constructive."
        else:
            trend, trend_note = "insufficient_history", ""

        rsi_series = [{"date": str(x[0]), "rsi": round(x[1], 1) if x[1] else None,
                       "price": x[2]} for x in reversed(recent)]

        return {
            "ticker": raw,
            "market": market,
            "as_of": str(d),
            "price": price,
            "indicators": {
                "rsi_14":        round(rsi, 1) if rsi is not None else None,
                "rsi_state":     rsi_state,
                "rsi_note":      rsi_note,
                "dma_50":        round(dma50, 2) if dma50 else None,
                "dma_200":       round(dma200, 2) if dma200 else None,
                "pct_vs_50dma":  pct(p50),
                "pct_vs_200dma": pct(p200),
                "pct_from_52wk_high": pct(phigh),
                "pct_from_52wk_low":  pct(plow),
                "return_3m":     pct(m3),
                "return_12m":    pct(m12),
                "volatility_20d_annualised": round(vol * (252 ** 0.5) * 100, 1) if vol else None,
            },
            "trend": {"state": trend, "note": trend_note},
            "peer_percentiles": {
                "vs_200dma":   int(r200) if r200 is not None else None,
                "momentum_12m": int(rm12) if rm12 is not None else None,
                "volatility":  int(rvol) if rvol is not None else None,
            },
            "rsi_history_30d": rsi_series,
            "caveats": [
                "These indicators describe where price has been. They do not "
                "forecast where it goes next.",
                "RSI thresholds of 70/30 are conventions, not laws — strong "
                "trends routinely persist past them in both directions.",
                "For a measured historical tendency rather than a convention, "
                "use the Base Rates tab, which reports what actually followed "
                "similar setups across this universe.",
            ],
        }
    except Exception as e:
        return {"error": f"Technicals failed: {e}"}
        
