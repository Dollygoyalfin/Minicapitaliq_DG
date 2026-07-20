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
        g_eff = min(growth_rate, wacc - 0.04)
        intrinsic_value = None
        if eps and eps > 0 and wacc > g_eff:
            intrinsic_value = (eps * (1 + g_eff)) / (wacc - g_eff)

        valuation_low = valuation_high = None
        if eps and eps > 0:
            g_low,  d_low  = g_eff - 0.02, wacc + 0.02
            g_high, d_high = g_eff + 0.01, max(wacc - 0.01, g_eff + 0.04)
            if d_low > g_low:
                valuation_low = (eps * (1 + g_low)) / (d_low - g_low)
            if d_high > g_high:
                valuation_high = (eps * (1 + g_high)) / (d_high - g_high)

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

            # Use cached price history to avoid extra yfinance calls
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
    margin_of_safety: float = Query(0.25, description="Margin of safety (decimal, e.g. 0.25 = 25%)")
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
        except Exception as fetch_err:
            return {"error": f"Could not fetch financial data: {fetch_err}"}

        # ── Info fields ───────────────────────────────────────────────────────
        current_price      = info.get("currentPrice")
        shares_outstanding = info.get("sharesOutstanding")
        beta               = info.get("beta", 1.0) or 1.0
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
                return sum(rates) / len(rates)
            valid = [v for v in series if v is not None and v > 0]
            if len(valid) >= 2:
                n = len(valid) - 1
                return (valid[0] / valid[-1]) ** (1 / n) - 1
            return 0.0

        def project_line(base: float, growth: float, years: int) -> list:
            """Project a single line item N years forward by compounding.
            If base is 0, returns flat zero series (can't grow from zero).
            Caps growth at 100% per year to prevent runaway projections.
            """
            g = max(min(growth, 1.0), -0.5)  # cap: -50% to +100% per year
            if base == 0.0:
                return [0.0] * years
            return [base * ((1 + g) ** y) for y in range(1, years + 1)]

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
                yr_tax = max(0.05, min(abs(tax_prov / pretax), 0.40))
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
        revenue_growth = max(-0.05, min(revenue_growth, 0.30))
        opex_growth    = min(opex_growth, revenue_growth)
        opex_growth    = max(-0.10, min(opex_growth, 0.30))
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

        # If depreciation data is missing (all zeros), estimate as % of Net PPE
        # Must run BEFORE caps so depr_growth gets capped correctly
        if all(d == 0.0 for d in depr_series) and any(p > 0 for p in net_ppe_series):
            avg_depr_rate = 0.05  # assume 5% depreciation rate on Net PPE
            depr_series   = [n * avg_depr_rate for n in net_ppe_series]
            depr_growth   = net_ppe_growth  # depr grows with PPE

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
            cost_of_debt = max(0.03, min(interest_expense / total_debt, 0.15))
        else:
            cost_of_debt = 0.06

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
        proj_rev_list     = project_line(base_rev,     revenue_growth, projection_years)
        proj_opex_list    = project_line(base_opex,    opex_growth,    projection_years)

        # ── Build projection table ────────────────────────────────────────────
        projection_table = []
        pv_fcffs         = []
        prev_wc          = wc_series[0]   # base WC = most recent historical year

        for year in range(projection_years):
            idx = year  # 0-indexed

            proj_ca      = proj_ca_list[idx]
            proj_cl      = proj_cl_list[idx]
            proj_csh     = proj_cash_list[idx]
            proj_cpltd   = proj_cpltd_list[idx]
            proj_net_ppe = proj_net_ppe_list[idx]
            proj_depr    = proj_depr_list[idx]
            proj_rev     = proj_rev_list[idx]
            proj_opex    = proj_opex_list[idx]

            # WC derived from projected BS lines
            proj_wc = proj_ca - proj_cl - proj_csh - proj_cpltd

            # ΔNWC = WC(this year) - WC(prior year)
            proj_delta_nwc = proj_wc - prev_wc
            prev_wc        = proj_wc

            # CapEx = Net PPE(this year) - Net PPE(prior year) + Depreciation(this year)
            prior_net_ppe  = proj_net_ppe_list[idx - 1] if idx > 0 else base_net_ppe
            proj_capex     = max(proj_net_ppe - prior_net_ppe + proj_depr, proj_depr)

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
        term_rev     = proj_rev_list[-1]  * (1 + terminal_growth_rate)
        term_opex    = proj_opex_list[-1] * (1 + terminal_growth_rate)
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
        term_delta_nwc = term_wc - prev_wc   # prev_wc = WC at end of last projected year
        # CapEx floor = depreciation (minimum maintenance CapEx)
        term_capex     = max(term_net_ppe - proj_net_ppe_list[-1] + term_depr, term_depr)

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
