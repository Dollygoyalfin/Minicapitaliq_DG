from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional  # kept for forward compatibility
import yfinance as yf
import os
import httpx
import json
import re

from fastapi.responses import FileResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Keys ──────────────────────────────────────────────────────────────────
FMP_API_KEY = os.getenv("FMP_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# ── FMP Base URL ──────────────────────────────────────────────────────────────
FMP_BASE = "https://financialmodelingprep.com/api/v3"

# ─────────────────────────────────────────────────────────────────────────────
#  FMP HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def fmp_get(path: str, params: dict = None) -> dict | list:
    """Generic FMP GET. Returns parsed JSON or empty list/dict on error."""
    try:
        url = f"{FMP_BASE}{path}"
        p = dict(params) if params else {}
        p["apikey"] = FMP_API_KEY
        resp = httpx.get(url, params=p, timeout=15)
        data = resp.json()
        # FMP returns {"Error Message": "..."} on bad key / unknown ticker
        if isinstance(data, dict) and ("Error Message" in data or "error" in data):
            return []
        return data
    except Exception:
        return []


def get_fmp_profile(ticker: str) -> dict:
    """Fetch stock profile + latest balance sheet from FMP, map to yfinance-style keys.
    FMP /profile does NOT include totalDebt/totalCash — pulled from balance sheet instead.
    """
    data = fmp_get(f"/profile/{ticker}")
    if not data or not isinstance(data, list) or not data[0]:
        return {}
    d = data[0]

    # Pull totalDebt and totalCash from latest balance sheet
    bal = fmp_get(f"/balance-sheet-statement/{ticker}", {"limit": 1})
    bal0 = bal[0] if isinstance(bal, list) and bal else {}
    total_debt = float(bal0.get("totalDebt") or bal0.get("longTermDebt") or 0)
    total_cash = float(bal0.get("cashAndShortTermInvestments") or bal0.get("cashAndCashEquivalents") or 0)

    # sharesOutstanding: use profile value, fall back to mktCap/price derivation
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
    """Annual income statements from FMP."""
    data = fmp_get(f"/income-statement/{ticker}", {"limit": limit})
    return data if isinstance(data, list) else []


def get_fmp_cashflow(ticker: str, limit: int = 6) -> list:
    """Annual cash flow statements from FMP."""
    data = fmp_get(f"/cash-flow-statement/{ticker}", {"limit": limit})
    return data if isinstance(data, list) else []


def get_fmp_balance(ticker: str, limit: int = 7) -> list:
    """Annual balance sheets from FMP."""
    data = fmp_get(f"/balance-sheet-statement/{ticker}", {"limit": limit})
    return data if isinstance(data, list) else []


def resolve_ticker(ticker: str, market: str, advanced: bool = False) -> tuple[str, bool]:
    """
    Returns (resolved_ticker, use_fmp).
    - India + default  → yfinance (.NS suffix)
    - India + advanced → FMP (NO .NS — FMP uses bare ticker e.g. RELIANCE)
    - US               → FMP always
    """
    t = ticker.upper()
    if market.lower() == "india":
        if advanced:
            # FMP does not support .NS suffix — strip it
            return t.replace(".NS", ""), True
        else:
            if not t.endswith(".NS"):
                t += ".NS"
            return t, False
    else:
        return t, True  # US always uses FMP


# ─────────────────────────────────────────────────────────────────────────────
#  STATIC / ROOT
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def serve_frontend():
    return FileResponse("index.html")


# ─────────────────────────────────────────────────────────────────────────────
#  /valuation  — yfinance for India default, FMP for US / Advanced
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/valuation")
def get_valuation(
    ticker: str = Query(...),
    market: str = Query("us"),
    advanced: bool = Query(False, description="Use FMP for Indian stocks too"),
    risk_free_rate: float = Query(0.04),
    market_return: float = Query(0.10),
    growth_rate: float = Query(0.08),
):
    try:
        resolved, use_fmp = resolve_ticker(ticker, market, advanced)

        if use_fmp:
            info = get_fmp_profile(resolved)
            if not info:
                return {"error": f"FMP returned no data for {resolved}"}
        else:
            stock = yf.Ticker(resolved)
            info = stock.info

        current_price = info.get("currentPrice")
        eps           = info.get("trailingEps") or 0.0
        pe_ratio      = info.get("trailingPE")
        forward_pe    = info.get("forwardPE")
        beta          = info.get("beta") or 1.0
        pb_ratio      = info.get("priceToBook")
        market_cap    = info.get("marketCap")
        roe           = info.get("returnOnEquity")
        de_ratio      = info.get("debtToEquity")
        book_value    = info.get("bookValue")

        cost_of_equity = risk_free_rate + beta * (market_return - risk_free_rate)
        cost_of_debt   = 0.06
        equity_value   = market_cap if (market_cap and market_cap > 0) else 1
        debt_value     = equity_value * 0.2  # assumed 20% debt ratio when no real debt data
        wacc = (
            (equity_value / (equity_value + debt_value)) * cost_of_equity +
            (debt_value   / (equity_value + debt_value)) * cost_of_debt
        )

        intrinsic_value = None
        if eps and wacc > growth_rate:
            intrinsic_value = (eps * (1 + growth_rate)) / (wacc - growth_rate)

        valuation_low = valuation_high = None
        if eps:
            low_growth, high_growth = growth_rate - 0.02, growth_rate + 0.02
            low_disc,  high_disc    = wacc + 0.02,        wacc - 0.02
            if low_disc  > low_growth:
                valuation_low  = (eps * (1 + low_growth))  / (low_disc  - low_growth)
            if high_disc > high_growth:
                valuation_high = (eps * (1 + high_growth)) / (high_disc - high_growth)

        return {
            "ticker":          resolved,
            "market":          market,
            "data_source":     "FMP" if use_fmp else "yfinance",
            "current_price":   current_price,
            "eps":             eps,
            "pe_ratio":        pe_ratio,
            "forward_pe":      forward_pe,
            "beta":            beta,
            "pb_ratio":        pb_ratio,
            "book_value":      book_value,
            "market_cap":      market_cap,
            "roe":             roe,
            "de_ratio":        de_ratio,
            "intrinsic_value": intrinsic_value,
            "valuation_low":   valuation_low,
            "valuation_high":  valuation_high,
            "growth_rate_used":   growth_rate,
            "discount_rate_used": wacc,
            "wacc":               wacc,
            "promoters_holding":  None,
            "fii_holding":        None,
            "dii_holding":        None,
            "retail_holding":     None,
        }

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
#  /financials  — yfinance for India default, FMP for US / Advanced
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/financials")
def get_financials(
    ticker: str = Query(...),
    market: str = Query("us"),
    advanced: bool = Query(False),
):
    try:
        resolved, use_fmp = resolve_ticker(ticker, market, advanced)

        if use_fmp:
            inc_list = get_fmp_income(resolved, 5)
            cf_list  = get_fmp_cashflow(resolved, 5)
            bal_list = get_fmp_balance(resolved, 5)

            def list_to_dict(records: list, key_field: str = "calendarYear") -> dict:
                out = {}
                for r in records:
                    yr = r.get(key_field) or r.get("date", "N/A")
                    out[str(yr)] = r   # always string key
                return out

            income        = list_to_dict(inc_list)
            cashflow      = list_to_dict(cf_list)
            balance_sheet = list_to_dict(bal_list)

            roe_dupont = {}
            for yr, r in income.items():
                net_income = r.get("netIncome") or 1
                revenue    = r.get("revenue") or 1
                # Balance sheet key may use date string instead of calendarYear — try both
                bal = balance_sheet.get(yr) or balance_sheet.get(r.get("date", "")[:4]) or {}
                assets = bal.get("totalAssets") or 1
                equity = bal.get("totalStockholdersEquity") or bal.get("totalEquity") or 1
                try:
                    roe_dupont[yr] = (net_income / revenue) * (revenue / assets) * (assets / equity)
                except ZeroDivisionError:
                    roe_dupont[yr] = 0.0

        else:
            stock       = yf.Ticker(resolved)
            income_df   = stock.financials
            cashflow_df = stock.cashflow
            balance_df  = stock.balance_sheet

            income        = {} if income_df  is None or income_df.empty  else income_df.T.head(5).to_dict()
            cashflow      = {} if cashflow_df is None or cashflow_df.empty else cashflow_df.T.head(5).to_dict()
            balance_sheet = {} if balance_df  is None or balance_df.empty  else balance_df.T.head(5).to_dict()

            roe_dupont = {}
            for year in income:
                net_income = income[year].get("Net Income", 1)
                revenue    = income[year].get("Total Revenue", 1)
                assets     = balance_sheet.get(year, {}).get("Total Assets", 1)
                equity     = balance_sheet.get(year, {}).get("Total Stockholder Equity", 1)
                roe_dupont[year] = (
                    (net_income / revenue) * (revenue / assets) * (assets / equity)
                )

        return {
            "data_source":      "FMP" if use_fmp else "yfinance",
            "income_statement": income,
            "cash_flow":        cashflow,
            "balance_sheet":    balance_sheet,
            "dupont_roe":       roe_dupont,
        }

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
#  /dcf  — yfinance for India default, FMP for US / Advanced
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/dcf")
def get_dcf(
    ticker: str = Query(...),
    market: str = Query("us"),
    advanced: bool = Query(False),
    projection_years: int = Query(5),
    risk_free_rate: float = Query(0.04),
    market_return: float = Query(0.10),
    terminal_growth_rate: float = Query(0.03),
    margin_of_safety: float = Query(0.25),
):
    try:
        resolved, use_fmp = resolve_ticker(ticker, market, advanced)

        # ── Pull data (FMP or yfinance) ───────────────────────────────────────
        if use_fmp:
            info     = get_fmp_profile(resolved)
            inc_list = get_fmp_income(resolved, 7)
            cf_list  = get_fmp_cashflow(resolved, 7)
            bal_list = get_fmp_balance(resolved, 8)

            if not info:
                return {"error": f"FMP profile returned no data for {resolved}. Check ticker symbol."}
            if not inc_list:
                return {"error": f"FMP income statement empty for {resolved}. Ticker may not be supported on your FMP plan."}
            if not bal_list:
                return {"error": f"FMP balance sheet empty for {resolved}."}

            current_price      = info.get("currentPrice")
            shares_outstanding = info.get("sharesOutstanding")
            beta               = info.get("beta") or 1.0
            market_cap         = info.get("marketCap")
            total_debt         = info.get("totalDebt") or 0
            total_cash         = info.get("totalCash") or 0

            if not shares_outstanding or shares_outstanding == 0:
                return {"error": "Shares outstanding not available for this ticker."}

            # FMP field extractors
            def fi(record, field, fallback=0.0):
                v = record.get(field)
                try:
                    return float(v) if v is not None else fallback
                except Exception:
                    return fallback

            # Build series from FMP (index 0 = most recent)
            revenue_series = []; opex_series = []; tax_rates = []; year_labels = []
            ca_series = []; cl_series = []; cash_series = []; cpltd_series = []
            net_ppe_series = []; depr_series = []

            n = min(len(inc_list), len(cf_list), len(bal_list), 7)

            for i in range(n):
                inc = inc_list[i] if i < len(inc_list) else {}
                cf  = cf_list[i]  if i < len(cf_list)  else {}
                bal = bal_list[i] if i < len(bal_list) else {}

                revenue = fi(inc, "revenue")
                if revenue == 0.0:
                    continue

                # FMP: operatingExpenses = SG&A only (NOT total cost).
                # Total operating cost = costOfRevenue + operatingExpenses.
                # Fall back to costAndExpenses if both are zero.
                cogs   = fi(inc, "costOfRevenue")
                sgna   = fi(inc, "operatingExpenses")
                opex   = (cogs + sgna) if (cogs + sgna) > 0 else fi(inc, "costAndExpenses")
                # Last resort: derive from grossProfit
                if opex == 0:
                    gross = fi(inc, "grossProfit")
                    ebit  = fi(inc, "operatingIncome") or fi(inc, "ebitda")
                    opex  = revenue - gross + (gross - ebit) if gross and ebit else revenue * 0.7

                # Tax rate
                pretax   = fi(inc, "incomeBeforeTax")
                tax_prov = fi(inc, "incomeTaxExpense")
                if pretax != 0 and tax_prov != 0:
                    yr_tax = max(0.05, min(abs(tax_prov / pretax), 0.40))
                else:
                    yr_tax = 0.25

                ca      = fi(bal, "totalCurrentAssets")
                cl      = fi(bal, "totalCurrentLiabilities")
                csh     = fi(bal, "cashAndCashEquivalents")
                cpltd   = fi(bal, "shortTermDebt") or fi(bal, "capitalLeaseObligations") or 0.0
                net_ppe = fi(bal, "propertyPlantEquipmentNet")
                depr    = abs(fi(cf, "depreciationAndAmortization"))

                try:
                    year_labels.append(str(inc.get("calendarYear", inc.get("date", f"Y-{i}"))))
                except Exception:
                    year_labels.append(f"Y-{i}")

                revenue_series.append(revenue)
                opex_series.append(opex)
                tax_rates.append(yr_tax)
                ca_series.append(ca)
                cl_series.append(cl)
                cash_series.append(csh)
                cpltd_series.append(cpltd)
                net_ppe_series.append(net_ppe)
                depr_series.append(depr)

            # Interest expense for WACC cost of debt
            interest_expense = abs(fi(inc_list[0], "interestExpense")) if inc_list else 0.0

        else:
            # ── yfinance path (Indian stocks default) ─────────────────────────
            stock = yf.Ticker(resolved)
            info  = stock.info

            current_price      = info.get("currentPrice")
            shares_outstanding = info.get("sharesOutstanding")
            beta               = info.get("beta", 1.0) or 1.0
            market_cap         = info.get("marketCap")
            total_debt         = info.get("totalDebt", 0) or 0
            total_cash         = info.get("totalCash", 0) or 0

            if not shares_outstanding or shares_outstanding == 0:
                return {"error": "Shares outstanding not available for this ticker."}

            income_df   = stock.financials
            cashflow_df = stock.cashflow
            balance_df  = stock.balance_sheet

            for label, df in [("Income statement", income_df),
                               ("Cash flow statement", cashflow_df),
                               ("Balance sheet", balance_df)]:
                if df is None or df.empty:
                    return {"error": f"{label} not available for this ticker."}

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

            revenue_row  = find_row(income_df, "total revenue") or find_row(income_df, "revenue")
            opex_row     = (find_row(income_df, "total expenses")
                            or find_row(income_df, "total operating expenses")
                            or find_row(income_df, "operating expense")
                            or find_row(income_df, "cost of revenue"))
            pretax_row   = find_row(income_df, "pretax") or find_row(income_df, "income before tax")
            tax_row      = find_row(income_df, "tax", "provision") or find_row(income_df, "income tax")
            interest_row = find_row(income_df, "interest", "expense")
            ca_row       = find_row(balance_df, "current assets")
            cl_row       = find_row(balance_df, "current liabilities")
            cash_row     = (find_row(balance_df, "cash and cash equivalents")
                            or find_row(balance_df, "cash"))
            cpltd_row    = (find_row(balance_df, "current portion", "long term")
                            or find_row(balance_df, "current", "long term debt")
                            or find_row(balance_df, "current portion"))
            net_ppe_row  = (find_row(balance_df, "net ppe")
                            or find_row(balance_df, "net property plant")
                            or find_row(balance_df, "property plant equipment"))
            depr_row_inc = (find_row(income_df, "reconciled depreciation")
                            or find_row(income_df, "depreciation amortization")
                            or find_row(income_df, "depreciation"))
            depr_row_cf  = (find_row(cashflow_df, "depreciation amortization")
                            or find_row(cashflow_df, "depreciation"))

            if not revenue_row:
                return {"error": "Could not find Revenue in income statement."}

            n_inc   = min(len(income_df.columns), 6)
            n_cf    = min(len(cashflow_df.columns), 6)
            n_bal   = min(len(balance_df.columns), 7)
            n_years = min(n_inc, n_cf, n_bal)

            if n_years == 0:
                return {"error": "Not enough historical data to compute FCFF."}

            revenue_series = []; opex_series = []; tax_rates = []; year_labels = []
            ca_series = []; cl_series = []; cash_series = []; cpltd_series = []
            net_ppe_series = []; depr_series = []

            for col in range(n_years):
                revenue = safe_float(income_df, revenue_row, col) or 0.0
                if revenue == 0.0:
                    continue
                pretax   = safe_float(income_df, pretax_row, col)
                tax_prov = safe_float(income_df, tax_row, col)
                if pretax and pretax != 0 and tax_prov is not None and tax_prov != 0:
                    yr_tax = max(0.05, min(abs(tax_prov / pretax), 0.40))
                else:
                    yr_tax = 0.25
                if opex_row:
                    opex = abs(safe_float(income_df, opex_row, col) or 0.0)
                else:
                    ebit_row_fb = find_row(income_df, "ebit") or find_row(income_df, "operating income")
                    ebit_val    = safe_float(income_df, ebit_row_fb, col) or 0.0
                    opex        = abs(revenue - ebit_val)

                ca      = safe_float(balance_df, ca_row, col) or 0.0
                cl      = safe_float(balance_df, cl_row, col) or 0.0
                csh     = safe_float(balance_df, cash_row, col) or 0.0
                cpltd   = safe_float(balance_df, cpltd_row, col) or 0.0
                net_ppe = safe_float(balance_df, net_ppe_row, col) or 0.0
                depr    = abs(safe_float(income_df, depr_row_inc, col) or
                              safe_float(cashflow_df, depr_row_cf, col) or 0.0)

                try:
                    year_labels.append(str(income_df.columns[col].year))
                except Exception:
                    year_labels.append(f"Y-{col}")

                revenue_series.append(revenue)
                opex_series.append(opex)
                tax_rates.append(yr_tax)
                ca_series.append(ca)
                cl_series.append(cl)
                cash_series.append(csh)
                cpltd_series.append(cpltd)
                net_ppe_series.append(net_ppe)
                depr_series.append(depr)

            interest_expense = abs(safe_float(income_df, interest_row, 0) or 0.0)

        # ── Shared DCF Math (same for both data sources) ──────────────────────

        if not revenue_series:
            return {"error": "No valid historical revenue data found for this ticker."}

        # Ensure interest_expense always defined (FMP path may skip all rows)
        try:
            interest_expense
        except NameError:
            interest_expense = 0.0

        n_valid = len(revenue_series)

        def avg_yoy_growth(series):
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

        def project_line(base, growth, years):
            g = max(min(growth, 1.0), -0.5)
            if base == 0.0:
                return [0.0] * years
            return [base * ((1 + g) ** y) for y in range(1, years + 1)]

        def rolling_avg_terminal(projected):
            if not projected:
                return 0.0
            window = projected[-5:] if len(projected) >= 5 else projected
            return sum(window) / len(window)

        wc_series = [
            ca_series[i] - cl_series[i] - cash_series[i] - cpltd_series[i]
            for i in range(n_valid)
        ]
        capex_series = []
        for i in range(n_valid):
            if i + 1 < len(net_ppe_series):
                capex_val = net_ppe_series[i] - net_ppe_series[i + 1] + depr_series[i]
            else:
                capex_val = depr_series[i]
            capex_series.append(max(capex_val, 0.0))

        delta_nwc_series = []
        for i in range(n_valid):
            if i + 1 < len(wc_series):
                delta_nwc_series.append(wc_series[i] - wc_series[i + 1])
            else:
                delta_nwc_series.append(0.0)

        revenue_growth = avg_yoy_growth(revenue_series)
        opex_growth    = avg_yoy_growth(opex_series)
        # Cap opex growth: use 1.5x revenue growth when positive, else cap at 30%
        opex_growth    = min(opex_growth, max(revenue_growth * 1.5, 0.30))
        ca_growth      = avg_yoy_growth(ca_series)
        cl_growth      = avg_yoy_growth(cl_series)
        cash_growth    = avg_yoy_growth(cash_series)
        cpltd_growth   = avg_yoy_growth(cpltd_series)
        net_ppe_growth = avg_yoy_growth(net_ppe_series)
        depr_growth    = avg_yoy_growth(depr_series)

        if all(d == 0.0 for d in depr_series) and any(p > 0 for p in net_ppe_series):
            depr_series = [n * 0.05 for n in net_ppe_series]
            depr_growth = net_ppe_growth

        ca_growth      = min(ca_growth,      revenue_growth)
        cl_growth      = min(cl_growth,      revenue_growth)
        cash_growth    = min(cash_growth,    revenue_growth)
        cpltd_growth   = min(cpltd_growth,   revenue_growth)
        net_ppe_growth = min(net_ppe_growth, revenue_growth)
        depr_growth    = min(depr_growth,    revenue_growth)

        avg_tax_rate = sum(tax_rates) / len(tax_rates) if tax_rates else 0.25

        display_years = min(n_valid, 5)
        historical_table = []
        for i in range(display_years):
            rev   = revenue_series[i]
            opex  = opex_series[i]
            capex = capex_series[i]
            t     = tax_rates[i]
            d_nwc = delta_nwc_series[i]
            nop   = rev - opex
            nop_at = nop * (1 - t)
            fcff  = nop_at - d_nwc - capex
            historical_table.append({
                "year": year_labels[i],
                "revenue": round(rev, 2),
                "operating_expenses": round(-opex, 2),
                "nop": round(nop, 2),
                "tax_rate": round(t, 4),
                "nop_after_tax": round(nop_at, 2),
                "delta_nwc": round(-d_nwc, 2),
                "capex": round(-capex, 2),
                "fcff": round(fcff, 2),
                "bs_ca": round(ca_series[i], 2),
                "bs_cl": round(cl_series[i], 2),
                "bs_cash": round(cash_series[i], 2),
                "bs_cpltd": round(cpltd_series[i], 2),
                "bs_net_ppe": round(net_ppe_series[i], 2),
                "bs_depreciation": round(depr_series[i], 2),
                "bs_wc": round(wc_series[i], 2),
            })

        # WACC
        cost_of_equity = risk_free_rate + beta * (market_return - risk_free_rate)
        if total_debt > 0 and interest_expense > 0:
            cost_of_debt = max(0.03, min(interest_expense / total_debt, 0.15))
        else:
            cost_of_debt = 0.06

        equity_val    = market_cap if market_cap else 1
        debt_val      = total_debt if total_debt else equity_val * 0.2
        total_capital = equity_val + debt_val
        wacc = (
            (equity_val / total_capital) * cost_of_equity +
            (debt_val   / total_capital) * cost_of_debt * (1 - avg_tax_rate)
        )

        if wacc <= terminal_growth_rate:
            return {"error": f"WACC ({wacc:.2%}) must be greater than terminal growth rate ({terminal_growth_rate:.2%})."}

        # Projections
        base_ca = ca_series[0]; base_cl = cl_series[0]; base_cash = cash_series[0]
        base_cpltd = cpltd_series[0]; base_net_ppe = net_ppe_series[0]
        base_depr = depr_series[0]; base_rev = revenue_series[0]; base_opex = opex_series[0]

        proj_ca_list      = project_line(base_ca,      ca_growth,      projection_years)
        proj_cl_list      = project_line(base_cl,      cl_growth,      projection_years)
        proj_cash_list    = project_line(base_cash,    cash_growth,    projection_years)
        proj_cpltd_list   = project_line(base_cpltd,   cpltd_growth,   projection_years)
        proj_net_ppe_list = project_line(base_net_ppe, net_ppe_growth, projection_years)
        proj_depr_list    = project_line(base_depr,    depr_growth,    projection_years)
        proj_rev_list     = project_line(base_rev,     revenue_growth, projection_years)
        proj_opex_list    = project_line(base_opex,    opex_growth,    projection_years)

        projection_table = []
        pv_fcffs = []
        prev_wc = wc_series[0]

        for year in range(projection_years):
            idx = year
            proj_ca      = proj_ca_list[idx]
            proj_cl      = proj_cl_list[idx]
            proj_csh     = proj_cash_list[idx]
            proj_cpltd   = proj_cpltd_list[idx]
            proj_net_ppe = proj_net_ppe_list[idx]
            proj_depr    = proj_depr_list[idx]
            proj_rev     = proj_rev_list[idx]
            proj_opex    = proj_opex_list[idx]

            proj_wc        = proj_ca - proj_cl - proj_csh - proj_cpltd
            proj_delta_nwc = proj_wc - prev_wc
            prev_wc        = proj_wc

            prior_net_ppe = proj_net_ppe_list[idx - 1] if idx > 0 else base_net_ppe
            proj_capex    = max(proj_net_ppe - prior_net_ppe + proj_depr, proj_depr)

            proj_nop    = proj_rev - proj_opex
            proj_nop_at = proj_nop * (1 - avg_tax_rate)
            proj_fcff   = proj_nop_at - proj_delta_nwc - proj_capex
            pv = proj_fcff / ((1 + wacc) ** (year + 1))

            projection_table.append({
                "year": f"Year {year + 1}",
                "revenue": round(proj_rev, 2),
                "operating_expenses": round(-proj_opex, 2),
                "nop": round(proj_nop, 2),
                "tax_rate": round(avg_tax_rate, 4),
                "nop_after_tax": round(proj_nop_at, 2),
                "delta_nwc": round(-proj_delta_nwc, 2),
                "capex": round(-proj_capex, 2),
                "fcff": round(proj_fcff, 2),
                "pv_fcff": round(pv, 2),
                "bs_ca": round(proj_ca, 2), "bs_cl": round(proj_cl, 2),
                "bs_cash": round(proj_csh, 2), "bs_cpltd": round(proj_cpltd, 2),
                "bs_net_ppe": round(proj_net_ppe, 2),
                "bs_depreciation": round(proj_depr, 2),
                "bs_wc": round(proj_wc, 2),
            })
            pv_fcffs.append(round(pv, 2))

        total_pv_fcff = sum(pv_fcffs)

        term_rev     = proj_rev_list[-1]  * (1 + terminal_growth_rate)
        term_opex    = proj_opex_list[-1] * (1 + terminal_growth_rate)
        term_ca      = rolling_avg_terminal(proj_ca_list)
        term_cl      = rolling_avg_terminal(proj_cl_list)
        term_cash    = rolling_avg_terminal(proj_cash_list)
        term_cpltd   = rolling_avg_terminal(proj_cpltd_list)
        term_net_ppe = rolling_avg_terminal(proj_net_ppe_list)
        term_depr    = rolling_avg_terminal(proj_depr_list)
        term_wc      = term_ca - term_cl - term_cash - term_cpltd
        term_delta_nwc = term_wc - prev_wc
        term_capex   = max(term_net_ppe - proj_net_ppe_list[-1] + term_depr, term_depr)
        term_nop     = term_rev - term_opex
        term_nop_at  = term_nop * (1 - avg_tax_rate)
        term_fcff    = term_nop_at - term_delta_nwc - term_capex

        terminal_year = {
            "year": f"Year {projection_years + 1} (Terminal)",
            "revenue": round(term_rev, 2),
            "operating_expenses": round(-term_opex, 2),
            "nop": round(term_nop, 2),
            "tax_rate": round(avg_tax_rate, 4),
            "nop_after_tax": round(term_nop_at, 2),
            "delta_nwc": round(-term_delta_nwc, 2),
            "capex": round(-term_capex, 2),
            "fcff": round(term_fcff, 2),
            "bs_ca": round(term_ca, 2), "bs_cl": round(term_cl, 2),
            "bs_cash": round(term_cash, 2), "bs_cpltd": round(term_cpltd, 2),
            "bs_net_ppe": round(term_net_ppe, 2),
            "bs_depreciation": round(term_depr, 2),
            "bs_wc": round(term_wc, 2),
        }

        terminal_value    = term_fcff / (wacc - terminal_growth_rate)
        pv_terminal_value = terminal_value / ((1 + wacc) ** projection_years)
        enterprise_value  = total_pv_fcff + pv_terminal_value

        if use_fmp:
            investments = 0.0
            minority_interest = 0.0
        else:
            investments_row   = (find_row(balance_df, "long term investments") or find_row(balance_df, "investments"))
            minority_row      = find_row(balance_df, "minority interest")
            investments       = safe_float(balance_df, investments_row, 0) or 0.0
            minority_interest = safe_float(balance_df, minority_row, 0) or 0.0

        equity_value_dcf          = enterprise_value + total_cash + investments - total_debt - minority_interest
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

        return {
            "ticker": resolved, "market": market,
            "data_source": "FMP" if use_fmp else "yfinance",
            "current_price": current_price,
            "derived_growth_rates": {
                "revenue_growth": round(revenue_growth, 4),
                "opex_growth":    round(opex_growth, 4),
                "ca_growth":      round(ca_growth, 4),
                "cl_growth":      round(cl_growth, 4),
                "cash_growth":    round(cash_growth, 4),
                "cpltd_growth":   round(cpltd_growth, 4),
                "net_ppe_growth": round(net_ppe_growth, 4),
                "depr_growth":    round(depr_growth, 4),
                "terminal_growth": terminal_growth_rate,
            },
            "historical_years_used": display_years,
            "avg_tax_rate_used":     round(avg_tax_rate, 4),
            "wacc":                  round(wacc, 4),
            "cost_of_equity":        round(cost_of_equity, 4),
            "cost_of_debt":          round(cost_of_debt, 4),
            "beta_used":             beta,
            "projection_years":      projection_years,
            "historical_table":      historical_table,
            "projection_table":      projection_table,
            "terminal_year":         terminal_year,
            "pv_of_fcffs":           pv_fcffs,
            "total_pv_fcff":         round(total_pv_fcff, 2),
            "terminal_value":        round(terminal_value, 2),
            "pv_terminal_value":     round(pv_terminal_value, 2),
            "total_cash":            total_cash,
            "investments":           round(investments, 2),
            "total_debt":            total_debt,
            "minority_interest":     round(minority_interest, 2),
            "shares_outstanding":    shares_outstanding,
            "enterprise_value":                     round(enterprise_value, 2),
            "equity_value_dcf":                     round(equity_value_dcf, 2),
            "intrinsic_value_per_share":             round(intrinsic_value_per_share, 2),
            "intrinsic_value_with_margin_of_safety": round(intrinsic_value_with_mos, 2),
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

            income_df = stock.financials
            cashflow_df = stock.cashflow

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
                    return {"source": "yfinance", "transactions": holders.to_dict(orient="records")}
            return {"error": "No insider transaction data available."}

        transactions = []
        for t in data:
            ttype = t.get("transactionType") or t.get("acquistionOrDisposition") or ""
            transactions.append({
                "date":               t.get("transactionDate") or t.get("filingDate"),
                "insider_name":       t.get("reportingName") or t.get("reporterName"),
                "title":              t.get("typeOfOwner") or t.get("reporterTitle"),
                "transaction_type":   ttype,
                "shares":             t.get("securitiesTransacted") or t.get("sharesTransacted"),
                "price":              t.get("price"),
                "value":              t.get("value") or (
                    (t.get("securitiesTransacted") or 0) * (t.get("price") or 0)
                ),
                "shares_owned_after": t.get("securitiesOwned"),
            })

        # Summary stats
        buys  = [t for t in transactions if "purchase" in (t.get("transaction_type") or "").lower() or "acquisition" in (t.get("transaction_type") or "").lower()]
        sells = [t for t in transactions if "sale" in (t.get("transaction_type") or "").lower() or "disposition" in (t.get("transaction_type") or "").lower()]
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

        total_shares = sum(h.get("shares") or 0 for h in holders)
        net_change   = sum(h.get("change") or 0 for h in holders)

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
                targets.append({
                    "published_date": t.get("publishedDate"),
                    "analyst_company": t.get("analystCompany"),
                    "analyst":         t.get("analyst"),
                    "price_target":    t.get("priceTarget"),
                    "adj_price_target": t.get("adjPriceTarget"),
                    "news_title":      t.get("newsTitle"),
                })

        # Aggregate consensus
        all_targets = [t.get("price_target") for t in targets if t.get("price_target")]
        consensus_target = round(sum(all_targets) / len(all_targets), 2) if all_targets else None

        recommendations = []
        if isinstance(consensus_data, list):
            for r in consensus_data[:8]:
                recommendations.append({
                    "date":         r.get("date"),
                    "strong_buy":   r.get("analystRatingsStrongBuy") or r.get("analystRatingsbuy") or 0,
                    "buy":          r.get("analystRatingsBuy") or r.get("analystRatingsOverweight") or 0,
                    "hold":         r.get("analystRatingsHold") or 0,
                    "sell":         r.get("analystRatingsSell") or r.get("analystRatingsUnderweight") or 0,
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
                eps_estimates.append({
                    "date":              e.get("date"),
                    "estimated_eps_avg": e.get("estimatedEpsAverage"),
                    "estimated_eps_low": e.get("estimatedEpsLow"),
                    "estimated_eps_high": e.get("estimatedEpsHigh"),
                    "estimated_revenue_avg": e.get("estimatedRevenueAverage"),
                    "number_analysts_eps":   e.get("numberAnalystEstimatedEps"),
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
        # Upcoming earnings — use from/to so we only get future dates
        from datetime import date, timedelta
        today = date.today().isoformat()
        future = (date.today() + timedelta(days=90)).isoformat()
        upcoming_data = fmp_get(f"/earning_calendar", {"from": today, "to": future, "symbol": resolved})
        # Fallback: if parameterized call returns nothing, try the ticker-based endpoint
        if not upcoming_data:
            upcoming_data = fmp_get(f"/earning_calendar/{resolved}")

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
                    beat = actual >= est

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

            def in_range(val, mn, mx):
                if val is None: return True
                if mn is not None and val < mn: return False
                if mx is not None and val > mx: return False
                return True

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

class VerdictRequest(BaseModel):
    ticker:     str
    market:     str = "us"
    dcf_result: dict

@app.post("/ai-verdict")
async def get_ai_verdict(request: VerdictRequest):
    try:
        ticker     = request.ticker.upper()
        market     = request.market.lower()
        dcf_result = request.dcf_result

        resolved, use_fmp = resolve_ticker(ticker, market, False)

        if not dcf_result:
            return {"error": "DCF result is empty. Please run the DCF model first."}
        if dcf_result.get("error"):
            return {"error": f"DCF result has an error: {dcf_result['error']}"}

        # ── Step 1: Company info ──────────────────────────────────────────────
        if use_fmp:
            profile      = get_fmp_profile(resolved)
            company_name = profile.get("longName") or resolved
            sector       = profile.get("sector", "Unknown")
            industry     = profile.get("industry", "Unknown")
            current_price = profile.get("currentPrice")
        else:
            stock         = yf.Ticker(resolved)
            company_name  = stock.info.get("longName") or resolved
            sector        = stock.info.get("sector", "Unknown")
            industry      = stock.info.get("industry", "Unknown")
            current_price = stock.info.get("currentPrice")

        # ── Step 2: Fetch competitor data via FMP ─────────────────────────────
        peers_data = fmp_get(f"/stock_peers/{resolved}")
        # FMP /stock_peers returns a LIST: [{"symbol":"X","peersList":[...]}]
        if isinstance(peers_data, list) and peers_data:
            peer_list = peers_data[0].get("peersList", [])
        elif isinstance(peers_data, dict):
            peer_list = peers_data.get("peersList", [])
        else:
            peer_list = []
        peer_list  = peer_list[:4]  # top 4 competitors

        competitor_summaries = []
        for peer in peer_list:
            try:
                # Lightweight: only fetch /profile, skip balance sheet for speed
                p_data = fmp_get(f"/profile/{peer}")
                if isinstance(p_data, list) and p_data:
                    p = p_data[0]
                    competitor_summaries.append(
                        f"  {peer}: Price={p.get('price')}, "
                        f"PE={p.get('pe')}, "
                        f"MCap={p.get('mktCap')}, "
                        f"ROE={p.get('roe')}"
                    )
            except Exception:
                pass

        competitors_text = "\n".join(competitor_summaries) if competitor_summaries else "  No peer data available"

        # ── Step 3: Analyst targets ───────────────────────────────────────────
        targets_data = fmp_get(f"/price-target/{resolved}", {"limit": 5})
        analyst_targets = []
        if isinstance(targets_data, list):
            for t in targets_data[:5]:
                analyst_targets.append(
                    f"  {t.get('analystCompany', 'N/A')}: Target ${t.get('priceTarget', 'N/A')}"
                )
        analyst_text = "\n".join(analyst_targets) if analyst_targets else "  No analyst targets available"

        # ── Step 4: Insider sentiment ─────────────────────────────────────────
        insider_data   = fmp_get(f"/insider-trading/{resolved}", {"limit": 10})
        insider_buys   = sum(1 for t in (insider_data or []) if any(x in (t.get("transactionType") or "").lower() for x in ["purchase","acquisition","buy"]))
        insider_sells  = sum(1 for t in (insider_data or []) if any(x in (t.get("transactionType") or "").lower() for x in ["sale","disposition","sell"]))
        insider_signal = "Bullish" if insider_buys > insider_sells else "Bearish" if insider_sells > insider_buys else "Neutral"

        # ── Step 5: Earnings surprise history ────────────────────────────────
        earn_data = fmp_get(f"/historical/earning_calendar/{resolved}", {"limit": 4})
        earn_lines = []
        if isinstance(earn_data, list):
            for e in earn_data[:4]:
                actual = e.get("eps"); est = e.get("epsEstimated")
                surprise = round(((actual - est) / abs(est)) * 100, 1) if actual and est and est != 0 else None
                earn_lines.append(f"  {e.get('date')}: Actual EPS={actual}, Est={est}, Surprise={surprise}%")
        earnings_text = "\n".join(earn_lines) if earn_lines else "  No earnings history available"

        # ── Step 6: News ──────────────────────────────────────────────────────
        news_items = []
        try:
            if use_fmp:
                news_data = fmp_get(f"/stock_news", {"tickers": resolved, "limit": 8})
                if isinstance(news_data, list):
                    for item in news_data[:8]:
                        title   = item.get("title", "")
                        summary = (item.get("text", ""))[:200]
                        source  = item.get("site", "")
                        if title:
                            news_items.append(f"- [{source}] {title}: {summary}")
            else:
                stock = yf.Ticker(resolved)
                news  = stock.news or []
                for item in news[:8]:
                    if "content" in item and isinstance(item["content"], dict):
                        inner   = item["content"]
                        title   = inner.get("title", "")
                        summary = (inner.get("summary", "") or "")[:250]
                        source  = inner.get("provider", {}).get("displayName", "") if isinstance(inner.get("provider"), dict) else ""
                    else:
                        title   = item.get("title", "")
                        summary = (item.get("summary", "") or "")[:250]
                        source  = item.get("publisher", "")
                    if title:
                        news_items.append(f"- [{source}] {title}: {summary}")
        except Exception:
            pass

        if not news_items:
            try:
                company_query = (company_name or resolved).replace(" ", "+")
                rss_url = f"https://news.google.com/rss/search?q={company_query}+stock&hl=en&gl=IN&ceid=IN:en"
                async with httpx.AsyncClient(timeout=10.0) as nc:
                    rss_resp = await nc.get(rss_url)
                if rss_resp.status_code == 200:
                    titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", rss_resp.text)
                    if not titles:
                        titles = re.findall(r"<title>(.*?)</title>", rss_resp.text)
                    for t in titles[1:6]:
                        if t and len(t) > 10:
                            news_items.append(f"- {t}")
            except Exception:
                pass

        if not news_items:
            news_items = ["No recent news available."]
        news_text = "\n".join(news_items)

        # ── Step 7: DCF metrics ───────────────────────────────────────────────
        def fmt_num(n):
            if n is None: return "N/A"
            try:
                n = float(n)
                if abs(n) >= 1e12: return f"{n/1e12:.2f}T"
                if abs(n) >= 1e9:  return f"{n/1e9:.2f}B"
                if abs(n) >= 1e6:  return f"{n/1e6:.2f}M"
                return f"{n:.2f}"
            except: return str(n)

        intrinsic     = dcf_result.get("intrinsic_value_per_share", "N/A")
        intrinsic_mos = dcf_result.get("intrinsic_value_with_margin_of_safety", "N/A")
        upside        = dcf_result.get("upside_downside_pct", "N/A")
        wacc_raw      = dcf_result.get("wacc", "N/A")
        wacc          = f"{float(wacc_raw)*100:.2f}%" if wacc_raw not in (None, "N/A") else "N/A"
        verdict_dcf   = dcf_result.get("verdict", "N/A")
        ev_raw        = dcf_result.get("enterprise_value", "N/A")
        ev            = fmt_num(ev_raw) if ev_raw != "N/A" else "N/A"
        avg_tax       = f"{float(dcf_result.get('avg_tax_rate_used', 0))*100:.2f}%" if dcf_result.get('avg_tax_rate_used') is not None else "N/A"
        hist_years    = dcf_result.get("historical_years_used", "N/A")
        mos_used      = dcf_result.get("margin_of_safety_used", "N/A")

        gr         = dcf_result.get("derived_growth_rates", {})
        rev_growth = gr.get("revenue_growth", 0)
        opex_growth = gr.get("opex_growth", 0)
        term_growth = gr.get("terminal_growth", 0)

        hist = dcf_result.get("historical_table", [])
        hist_lines = [
            f"  {r.get('year')}: Revenue={fmt_num(r.get('revenue'))}, NOP After Tax={fmt_num(r.get('nop_after_tax'))}, CapEx={fmt_num(r.get('capex'))}, FCFF={fmt_num(r.get('fcff'))}"
            for r in hist[:4]
        ]
        hist_summary = "\n".join(hist_lines) if hist_lines else "  Not available"

        proj = dcf_result.get("projection_table", [])
        proj_lines = [
            f"  {r.get('year')}: Revenue={fmt_num(r.get('revenue'))}, FCFF={fmt_num(r.get('fcff'))}, PV={fmt_num(r.get('pv_fcff'))}"
            for r in proj[:3]
        ]
        proj_summary = "\n".join(proj_lines) if proj_lines else "  Not available"

        term = dcf_result.get("terminal_year", {})
        term_summary = (
            f"  {term.get('year')}: Revenue={fmt_num(term.get('revenue'))}, FCFF={fmt_num(term.get('fcff'))}"
        ) if term else "  Not available"

        # ── Step 8: Build prompt ──────────────────────────────────────────────
        prompt = f"""You are a senior equity research analyst. Provide a rigorous investment analysis for {company_name} ({resolved}) in the {sector} sector ({industry}).

═══ DCF VALUATION OUTPUT ═══
Current Market Price:     {current_price}
Intrinsic Value (DCF):    {intrinsic}
Intrinsic Value (w/ MOS): {intrinsic_mos}  [MOS used: {mos_used}]
Upside / Downside:        {upside}%
WACC:                     {wacc}
Enterprise Value:         {ev}
DCF Model Signal:         {verdict_dcf}
Historical Years Used:    {hist_years}
Avg Effective Tax Rate:   {avg_tax}
Revenue Growth:    {round(float(rev_growth or 0)*100,1)}%
OpEx Growth:       {round(float(opex_growth or 0)*100,1)}%
Terminal Growth:   {round(float(term_growth or 0)*100,1)}%

Historical FCFF:
{hist_summary}

Projected FCFF (Years 1-3):
{proj_summary}

Terminal Year:
{term_summary}

═══ COMPETITORS ═══
{competitors_text}

═══ ANALYST PRICE TARGETS ═══
{analyst_text}

═══ INSIDER ACTIVITY (last 10 trades) ═══
Buys: {insider_buys} | Sells: {insider_sells} | Signal: {insider_signal}

═══ EARNINGS SURPRISE HISTORY ═══
{earnings_text}

═══ RECENT NEWS ═══
{news_text}

═══ INSTRUCTIONS ═══
Respond ONLY with a valid JSON object. No preamble, no markdown. Pure JSON only.

{{
  "verdict": "one of: Strong Buy / Buy / Hold / Sell / Strong Sell",
  "confidence": "one of: High / Medium / Low",
  "summary": "3-4 sentence plain English investment thesis mentioning valuation, growth, and key risk",
  "bull_case": "3 specific bullish arguments referencing actual numbers",
  "bear_case": "3 specific bearish arguments referencing actual numbers",
  "competitor_analysis": "2-3 sentences: how does {resolved} compare to peers on valuation and growth? Is it cheaper or more expensive than peers? Any competitive moat?",
  "management_guidance": {{
    "capex": "CapEx guidance from news or N/A",
    "revenue": "Revenue growth guidance from news or N/A",
    "expansion": "Any expansion or strategic plans from news or N/A"
  }},
  "model_vs_reality": "2 sentences: how do DCF assumptions compare to analyst targets and what the market expects?",
  "insider_read": "1 sentence: what does insider activity signal about management confidence?",
  "earnings_track_record": "1 sentence: summarize earnings beat/miss history and what it implies",
  "news_sentiment": "one of: Positive / Neutral / Negative",
  "recent_headlines": ["headline 1", "headline 2", "headline 3"],
  "key_risks": ["specific risk 1", "specific risk 2", "specific risk 3"],
  "analyst_note": "One actionable sentence: what specific metric or event should investors watch next quarter?"
}}"""

        # ── Step 9: Call Groq ─────────────────────────────────────────────────
        if not GROQ_API_KEY:
            return {"error": "GROQ_API_KEY environment variable not set on server."}

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "max_tokens": 1800,
                    "messages": [
                        {"role": "system", "content": "You are a senior equity research analyst. Always respond with valid JSON only. No preamble, no markdown, no explanation outside the JSON object."},
                        {"role": "user",   "content": prompt},
                    ],
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                },
            )

        if response.status_code != 200:
            return {"error": f"Groq API error {response.status_code}: {response.text[:300]}"}

        raw     = response.json()
        ai_text = raw.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

        try:
            clean = ai_text
            if "```" in clean:
                for part in clean.split("```"):
                    stripped = part.strip()
                    if stripped.startswith("json"):
                        stripped = stripped[4:].strip()
                    if stripped.startswith("{"):
                        clean = stripped
                        break
            start = clean.find("{"); end = clean.rfind("}") + 1
            if start != -1 and end > start:
                clean = clean[start:end]
            ai_verdict = json.loads(clean)
        except Exception:
            ai_verdict = {
                "verdict": "Analysis Complete", "confidence": "Medium",
                "summary": ai_text[:500], "parse_error": True, "raw_response": ai_text,
            }

        return {
            "ticker":        resolved,
            "company_name":  company_name,
            "sector":        sector,
            "industry":      industry,
            "current_price": current_price,
            "data_source":   "FMP" if use_fmp else "yfinance",
            "ai_verdict":    ai_verdict,
            "news_fed":      news_items[:5],
            "competitors_analyzed": peer_list,
        }

    except Exception as e:
        return {"error": str(e)}
