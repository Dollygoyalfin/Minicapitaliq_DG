from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import yfinance as yf

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"message": "Mini Capital IQ backend is running 🚀"}

@app.get("/valuation")
def get_valuation(
    ticker: str = Query(...),
    market: str = Query("us"),
    risk_free_rate: float = Query(0.04, description="Risk free rate in decimal"),
    market_return: float = Query(0.10, description="Expected market return in decimal"),
    growth_rate: float = Query(0.08, description="Growth rate for intrinsic value calculation")
):
    try:
        # Adjust ticker for Indian stocks
        if market.lower() == "india" and not ticker.endswith(".NS"):
            ticker = ticker.upper() + ".NS"

        stock = yf.Ticker(ticker)
        info = stock.info

        # Basic fields
        current_price = info.get("currentPrice")
        eps = info.get("trailingEps", 0.0)
        pe_ratio = info.get("trailingPE", None)
        forward_pe = info.get("forwardPE", None)
        beta = info.get("beta", 1.0)
        pb_ratio = info.get("priceToBook", None)
        market_cap = info.get("marketCap", None)
        roe = info.get("returnOnEquity", None)
        de_ratio = info.get("debtToEquity", None)
        book_value = info.get("bookValue", None)

        # Ownership placeholders
        promoters_holding = None
        fii_holding = None
        dii_holding = None
        retail_holding = None

        # Cost of equity (CAPM)
        cost_of_equity = risk_free_rate + beta * (market_return - risk_free_rate)

        # Approximate WACC
        cost_of_debt = 0.06
        equity_value = market_cap if market_cap else 1
        debt_value = equity_value * 0.2
        wacc = (
            (equity_value / (equity_value + debt_value)) * cost_of_equity +
               (debt_value / (equity_value + debt_value)) * cost_of_debt
        )

        # Intrinsic value (Gordon growth)
        if eps and wacc > growth_rate:
            intrinsic_value = (eps * (1 + growth_rate)) / (wacc - growth_rate)
        else:
            intrinsic_value = None

        # Sensitivity range
        valuation_low, valuation_high = None, None
        if eps:
            low_growth, high_growth = growth_rate - 0.02, growth_rate + 0.02
            low_disc, high_disc = wacc + 0.02, wacc - 0.02

            if low_disc > low_growth:
                valuation_low = (eps * (1 + low_growth)) / (low_disc - low_growth)
            if high_disc > high_growth:
                valuation_high = (eps * (1 + high_growth)) / (high_disc - high_growth)

        return {
            "ticker": ticker.upper(),
            "market": market,
            "current_price": current_price,
            "eps": eps,
            "pe_ratio": pe_ratio,
            "forward_pe": forward_pe,
            "beta": beta,
            "pb_ratio": pb_ratio,
            "book_value": book_value,
            "market_cap": market_cap,
            "roe": roe,
            "de_ratio": de_ratio,
            "intrinsic_value": intrinsic_value,
            "valuation_low": valuation_low,
            "valuation_high": valuation_high,
            "growth_rate_used": growth_rate,
            "discount_rate_used": wacc,
            "wacc": wacc,
            "promoters_holding": promoters_holding,
            "fii_holding": fii_holding,
            "dii_holding": dii_holding,
            "retail_holding": retail_holding
        }

    except Exception as e:
        return {"error": str(e)}

@app.get("/financials")
def get_financials(
    ticker: str = Query(...),
    market: str = Query("us")
):
    try:
        ticker = ticker.upper()
        if market.lower() == "india" and not ticker.endswith(".NS"):
            ticker += ".NS"
        stock = yf.Ticker(ticker)
        
        #fin data
        income_df = stock.financials
        cashflow_df = stock.cashflow
        balance_df = stock.balance_sheet
        if income_df is None or income_df.empty:
            income = {}
        else:
            income = income_df.T.head(5).to_dict()
        if cashflow_df is None or cashflow_df.empty:
            cashflow = {}
        else:
            cashflow = cashflow_df.T.head(5).to_dict()
        if balance_df is None or balance_df.empty:
            balance_sheet = {}
        else:
            balance_sheet = balance_df.T.head(5).to_dict()
        

    # Example: DuPont ROE calculation
        roe_dupont = {}

        for year in income:
            net_income = income[year].get("Net Income", 1)
            revenue = income[year].get("Total Revenue", 1)
            assets = balance_sheet.get(year, {}).get("Total Assets", 1)
            equity = balance_sheet.get(year, {}).get("Total Stockholder Equity", 1)

            roe_dupont[year] = (
                (net_income / revenue) *
                (revenue / assets) *
                (assets / equity)
            )

        return {
            "income_statement": income,
            "cash_flow": cashflow,
            "balance_sheet": balance_sheet,
            "dupont_roe": roe_dupont
        }

    except Exception as e:
        return {"error": str(e)}
@app.get("/dcf")
def get_dcf(
    ticker: str = Query(...),
    market: str = Query("us"),
    projection_years: int = Query(5, description="Number of years to project FCFF"),
    risk_free_rate: float = Query(0.04, description="Risk-free rate (decimal)"),
    market_return: float = Query(0.10, description="Expected market return (decimal)"),
    terminal_growth_rate: float = Query(0.03, description="Terminal growth rate for Year 6+ (decimal)"),
    margin_of_safety: float = Query(0.25, description="Margin of safety (decimal, e.g. 0.25 = 25%)")
):
    """
    Cash-based FCFF Valuation. D&A is never involved.

    NOP   = Revenue - Operating Expenses        (cash operating profit, pre-tax)
    FCFF  = NOP * (1 - Tax Rate) - ΔNWC - CapEx

    - 5 historical years of actuals
    - 5 projected years (each line item grown at its own 5-yr avg growth rate)
    - Year 6 terminal year (each line item grown at terminal_growth_rate from Year 5)
    - Terminal Value = FCFF_Year6 / (WACC - terminal_growth_rate)

    Equity Value = EV + Cash + Investments - Debt - Minority Interest
    Intrinsic Value = Equity Value / Shares Outstanding
    """
    try:
        # ── Ticker setup ──────────────────────────────────────────────────────
        raw_ticker = ticker.upper()
        if market.lower() == "india" and not raw_ticker.endswith(".NS"):
            raw_ticker += ".NS"

        stock = yf.Ticker(raw_ticker)
        info  = stock.info

        # ── Info fields ───────────────────────────────────────────────────────
        current_price      = info.get("currentPrice")
        shares_outstanding = info.get("sharesOutstanding")
        beta               = info.get("beta", 1.0) or 1.0
        market_cap         = info.get("marketCap")
        total_debt         = info.get("totalDebt", 0) or 0
        total_cash         = info.get("totalCash", 0) or 0

        if not shares_outstanding or shares_outstanding == 0:
            return {"error": "Shares outstanding not available for this ticker."}

        # ── Pull financial statements ─────────────────────────────────────────
        income_df   = stock.financials
        cashflow_df = stock.cashflow
        balance_df  = stock.balance_sheet

        for label, df in [("Income statement", income_df),
                           ("Cash flow statement", cashflow_df),
                           ("Balance sheet", balance_df)]:
            if df is None or df.empty:
                return {"error": f"{label} not available for this ticker."}

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
            Average YoY growth from valid consecutive positive pairs.
            Falls back to CAGR if no valid YoY pairs available.
            """
            rates = []
            for i in range(len(series) - 1):
                v_new = series[i]
                v_old = series[i + 1]
                if v_new is None or v_old is None or v_old == 0:
                    continue
                if v_old < 0 or v_new < 0:
                    continue
                rates.append((v_new - v_old) / v_old)
            if rates:
                return sum(rates) / len(rates)
            # CAGR fallback
            valid = [v for v in series if v is not None and v > 0]
            if len(valid) >= 2:
                n = len(valid) - 1
                return (valid[0] / valid[-1]) ** (1 / n) - 1
            return 0.0

        # ── Identify rows ─────────────────────────────────────────────────────
        revenue_row  = find_row(income_df, "total revenue") or find_row(income_df, "revenue")
        opex_row     = (find_row(income_df, "total expenses")
                        or find_row(income_df, "total operating expenses")
                        or find_row(income_df, "operating expense")
                        or find_row(income_df, "cost of revenue"))
        pretax_row   = find_row(income_df, "pretax") or find_row(income_df, "income before tax")
        tax_row      = find_row(income_df, "tax", "provision") or find_row(income_df, "income tax")
        interest_row = find_row(income_df, "interest", "expense")
        # CapEx derived as: Net PPE(t) - Net PPE(t-1) + Depreciation(t)
        # More reliable than cashflow capex row for Indian stocks
        net_ppe_row  = (find_row(balance_df, "net ppe")
                        or find_row(balance_df, "net property plant")
                        or find_row(balance_df, "property plant equipment"))
        depr_row_inc = (find_row(income_df, "reconciled depreciation")
                        or find_row(income_df, "depreciation amortization")
                        or find_row(income_df, "depreciation"))
        depr_row_cf  = (find_row(cashflow_df, "depreciation amortization")
                        or find_row(cashflow_df, "depreciation"))
        ca_row       = find_row(balance_df, "current assets")
        cl_row       = find_row(balance_df, "current liabilities")
        # For correct NWC: exclude cash from current assets, exclude short-term debt from current liabilities
        cash_row  = (find_row(balance_df, "cash and cash equivalents")
                      or find_row(balance_df, "cash"))
        cpltd_row = (find_row(balance_df, "current portion", "long term")
                      or find_row(balance_df, "current", "long term debt")
                      or find_row(balance_df, "current portion"))

        if not revenue_row:
            return {"error": "Could not find Revenue in income statement."}

        # ── Determine usable years (max 5) ────────────────────────────────────
        # Decouple NWC from year count — use all 5 years of income/cashflow data
        # For years where next balance sheet col is missing, ΔNWC defaults to 0
        n_inc   = min(len(income_df.columns), 5)
        n_cf    = min(len(cashflow_df.columns), 5)
        n_years = min(n_inc, n_cf)   # no longer limited by balance sheet cols

        if n_years == 0:
            return {"error": "Not enough historical data to compute FCFF."}

        # ── Collect historical series (index 0 = most recent year) ────────────
        revenue_series = []
        opex_series    = []
        capex_series   = []   # stored as positive magnitudes
        nwc_series     = []
        tax_rates      = []
        year_labels    = []

        for col in range(n_years):
            revenue   = safe_float(income_df, revenue_row, col) or 0.0
            pretax    = safe_float(income_df, pretax_row,  col)
            tax_prov  = safe_float(income_df, tax_row,     col)
            # CapEx = Net PPE(t) - Net PPE(t-1) + Depreciation(t)
            net_ppe_t  = safe_float(balance_df, net_ppe_row, col)     or 0.0
            net_ppe_t1 = safe_float(balance_df, net_ppe_row, col + 1) or 0.0
            depr_t     = abs(safe_float(income_df,   depr_row_inc, col) or
                             safe_float(cashflow_df, depr_row_cf,  col) or 0.0)
            capex_raw  = net_ppe_t - net_ppe_t1 + depr_t   # always positive for growing firms

            # Working Capital = Current Assets
            #                - Current Liabilities       (STD stays inside — operating liability)
            #                - Cash & Cash Equivalents   (financing — handled in equity bridge)
            #                - Current Portion of LTD    (financing — not an operating item)
            ca_t0    = safe_float(balance_df, ca_row,    col) or 0.0
            cl_t0    = safe_float(balance_df, cl_row,    col) or 0.0
            csh_t0   = safe_float(balance_df, cash_row,  col) or 0.0
            cpltd_t0 = safe_float(balance_df, cpltd_row, col) or 0.0
            nwc_t0   = ca_t0 - cl_t0 - csh_t0 - cpltd_t0

            # Operating Expenses: use directly if found, else derive from EBIT
            if opex_row:
                opex = abs(safe_float(income_df, opex_row, col) or 0.0)
            else:
                ebit_row_fb = find_row(income_df, "ebit") or find_row(income_df, "operating income")
                ebit_val    = safe_float(income_df, ebit_row_fb, col) or 0.0
                opex        = abs(revenue - ebit_val)

            # Effective tax rate for this year
            if pretax and pretax != 0 and tax_prov is not None and tax_prov != 0:
                yr_tax = max(0.05, min(abs(tax_prov / pretax), 0.40))
            else:
                yr_tax = 0.25

            # Skip years with zero/missing revenue — bad yfinance data corrupts growth rates
            if revenue == 0.0:
                continue

            try:
                year_labels.append(str(income_df.columns[col].year))
            except Exception:
                year_labels.append(f"Y-{col}")

            revenue_series.append(revenue)
            opex_series.append(opex)
            capex_series.append(abs(capex_raw))
            nwc_series.append(nwc_t0)
            tax_rates.append(yr_tax)

        # ── Average growth rates (fully data-derived, no user input) ──────────
        revenue_growth = avg_yoy_growth(revenue_series)
        opex_growth    = avg_yoy_growth(opex_series)
        capex_growth   = avg_yoy_growth(capex_series)

        nwc_pos    = [v for v in nwc_series if v is not None and v > 0]
        nwc_growth = avg_yoy_growth(nwc_series) if len(nwc_pos) >= 2 else 0.03
        # Cap NWC growth at revenue growth — NWC cannot grow faster than the business
        nwc_growth = min(nwc_growth, revenue_growth)

        avg_tax_rate = sum(tax_rates) / len(tax_rates)

        # ── Build historical table ────────────────────────────────────────────
        historical_table = []
        for i in range(n_years):
            rev   = revenue_series[i]
            opex  = opex_series[i]
            capex = capex_series[i]
            nwc   = nwc_series[i]
            t     = tax_rates[i]

            nop    = rev - opex       # Net Operating Profit (pre-tax, no D&A)
            nop_at = nop * (1 - t)   # NOP after tax

            # ΔNWC = NWC(this year) - NWC(prior year)
            # index 0 = most recent, so prior year = index i+1
            # If prior year balance sheet not available, ΔNWC = 0
            if i < len(nwc_series) - 1:
                delta_nwc = nwc_series[i] - nwc_series[i + 1]
            else:
                delta_nwc = 0.0

            # FCFF = NOP*(1-t) - ΔNWC - CapEx
            fcff = nop_at - delta_nwc - capex

            historical_table.append({
                "year":               year_labels[i],
                "revenue":            round(rev, 2),
                "operating_expenses": round(-opex, 2),      # negative = cash outflow
                "nop":                round(nop, 2),         # Net Operating Profit (pre-tax)
                "tax_rate":           round(t, 4),
                "nop_after_tax":      round(nop_at, 2),      # NOP * (1 - t)
                "delta_nwc":          round(-delta_nwc, 2),  # negative = outflow
                "capex":              round(-capex, 2),       # negative = outflow
                "fcff":               round(fcff, 2),
            })

        # ── WACC ──────────────────────────────────────────────────────────────
        cost_of_equity = risk_free_rate + beta * (market_return - risk_free_rate)

        interest_expense = abs(safe_float(income_df, interest_row, 0) or 0.0)
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

        # ── Project Years 1–5 (each component at its own growth rate) ─────────
        base_rev   = revenue_series[0]
        base_opex  = opex_series[0]
        base_capex = capex_series[0]
        base_nwc   = nwc_series[0]

        projection_table = []
        projected_fcffs  = []
        pv_fcffs         = []
        prev_nwc         = base_nwc

        for year in range(1, projection_years + 1):
            proj_rev   = base_rev   * ((1 + revenue_growth) ** year)
            proj_opex  = base_opex  * ((1 + opex_growth)    ** year)
            proj_capex = base_capex * ((1 + capex_growth)   ** year)
            proj_nwc   = base_nwc   * ((1 + nwc_growth)     ** year) if base_nwc > 0 else base_nwc

            proj_delta_nwc = proj_nwc - prev_nwc   # increase in NWC = cash outflow
            prev_nwc       = proj_nwc

            proj_nop    = proj_rev - proj_opex
            proj_nop_at = proj_nop * (1 - avg_tax_rate)

            # FCFF = NOP*(1-t) - ΔNWC - CapEx
            proj_fcff = proj_nop_at - proj_delta_nwc - proj_capex

            pv = proj_fcff / ((1 + wacc) ** year)

            projection_table.append({
                "year":               f"Year {year}",
                "revenue":            round(proj_rev, 2),
                "operating_expenses": round(-proj_opex, 2),
                "nop":                round(proj_nop, 2),
                "tax_rate":           round(avg_tax_rate, 4),
                "nop_after_tax":      round(proj_nop_at, 2),
                "delta_nwc":          round(-proj_delta_nwc, 2),
                "capex":              round(-proj_capex, 2),
                "fcff":               round(proj_fcff, 2),
                "pv_fcff":            round(pv, 2),
            })

            projected_fcffs.append(round(proj_fcff, 2))
            pv_fcffs.append(round(pv, 2))

        total_pv_fcff = sum(pv_fcffs)

        # ── Terminal Year — grows at terminal_growth_rate from last projected year ──
        # Use the last row of projection_table as the base (Year N values)
        last = projection_table[-1]
        yr_last_rev   = last["revenue"]
        yr_last_opex  = abs(last["operating_expenses"])
        yr_last_capex = abs(last["capex"])
        yr_last_nwc   = prev_nwc   # prev_nwc holds NWC at end of last projection year

        yr_term_rev   = yr_last_rev   * (1 + terminal_growth_rate)
        yr_term_opex  = yr_last_opex  * (1 + terminal_growth_rate)
        yr_term_capex = yr_last_capex * (1 + terminal_growth_rate)
        yr_term_nwc   = yr_last_nwc   * (1 + terminal_growth_rate) if yr_last_nwc > 0 else yr_last_nwc

        # ΔNWC = NWC(terminal) - NWC(last projected year)
        yr_term_delta_nwc = yr_term_nwc - yr_last_nwc
        yr_term_nop       = yr_term_rev - yr_term_opex
        yr_term_nop_at    = yr_term_nop * (1 - avg_tax_rate)

        # FCFF = NOP*(1-t) - ΔNWC - CapEx
        yr_term_fcff = yr_term_nop_at - yr_term_delta_nwc - yr_term_capex

        terminal_year = {
            "year":               f"Year {projection_years + 1} (Terminal)",
            "revenue":            round(yr_term_rev, 2),
            "operating_expenses": round(-yr_term_opex, 2),
            "nop":                round(yr_term_nop, 2),
            "tax_rate":           round(avg_tax_rate, 4),
            "nop_after_tax":      round(yr_term_nop_at, 2),
            "delta_nwc":          round(-yr_term_delta_nwc, 2),
            "capex":              round(-yr_term_capex, 2),
            "fcff":               round(yr_term_fcff, 2),
        }

        # Terminal Value = FCFF_Terminal / (WACC - g)
        terminal_value    = yr_term_fcff / (wacc - terminal_growth_rate)
        pv_terminal_value = terminal_value / ((1 + wacc) ** projection_years)

        # ── Enterprise Value ──────────────────────────────────────────────────
        enterprise_value = total_pv_fcff + pv_terminal_value

        # ── Equity Value bridge ───────────────────────────────────────────────
        investments_row   = (find_row(balance_df, "long term investments")
                             or find_row(balance_df, "investments"))
        minority_row      = find_row(balance_df, "minority interest")
        investments       = safe_float(balance_df, investments_row, 0) or 0.0
        minority_interest = safe_float(balance_df, minority_row,    0) or 0.0

        equity_value_dcf = (
            enterprise_value
            + total_cash
            + investments
            - total_debt
            - minority_interest
        )

        # ── Intrinsic Value per Share ─────────────────────────────────────────
        intrinsic_value_per_share = equity_value_dcf / shares_outstanding
        intrinsic_value_with_mos  = intrinsic_value_per_share * (1 - margin_of_safety)

        # ── Upside / Verdict ──────────────────────────────────────────────────
        upside_pct = None
        if current_price and current_price > 0:
            upside_pct = ((intrinsic_value_per_share - current_price) / current_price) * 100

        verdict = (
            "Potentially Undervalued" if upside_pct and upside_pct > 20
            else "Potentially Overvalued" if upside_pct and upside_pct < -20
            else "Fairly Valued" if upside_pct is not None
            else None
        )

        # ── Response ──────────────────────────────────────────────────────────
        return {
            "ticker":        raw_ticker,
            "market":        market,
            "current_price": current_price,

            # Fully data-derived growth rates
            "derived_growth_rates": {
                "revenue_growth":  round(revenue_growth,  4),
                "opex_growth":     round(opex_growth,     4),
                "capex_growth":    round(capex_growth,    4),
                "nwc_growth":      round(nwc_growth,      4),
                "terminal_growth": terminal_growth_rate,
            },

            # Model assumptions
            "historical_years_used": n_years,
            "avg_tax_rate_used":     round(avg_tax_rate, 4),
            "wacc":                  round(wacc, 4),
            "cost_of_equity":        round(cost_of_equity, 4),
            "cost_of_debt":          round(cost_of_debt, 4),
            "beta_used":             beta,
            "projection_years":      projection_years,

            # Full model tables
            "historical_table": historical_table,   # 5 actual years
            "projection_table": projection_table,   # Years 1–5 projected + PV
            "terminal_year":    terminal_year,       # Year 6 terminal

            # Summary
            "pv_of_fcffs":       pv_fcffs,
            "total_pv_fcff":     round(total_pv_fcff, 2),
            "terminal_value":    round(terminal_value, 2),
            "pv_terminal_value": round(pv_terminal_value, 2),

            # Balance sheet bridge
            "total_cash":         total_cash,
            "investments":        round(investments, 2),
            "total_debt":         total_debt,
            "minority_interest":  round(minority_interest, 2),
            "shares_outstanding": shares_outstanding,

            # Final output
            "enterprise_value":                      round(enterprise_value, 2),
            "equity_value_dcf":                      round(equity_value_dcf, 2),
            "intrinsic_value_per_share":              round(intrinsic_value_per_share, 2),
            "intrinsic_value_with_margin_of_safety":  round(intrinsic_value_with_mos, 2),
            "margin_of_safety_used":                  margin_of_safety,
            "upside_downside_pct":                    round(upside_pct, 2) if upside_pct is not None else None,
            "verdict":                                verdict,
        }

    except Exception as e:
        return {"error": str(e)}
@app.get("/screener")
def get_screener(
    tickers: str = Query(..., description="Comma-separated list of tickers e.g. AAPL,MSFT,TSLA"),
    market: str = Query("us", description="'us' or 'india'"),

    # Filter params — all optional, None means no filter applied
    min_pe: float = Query(None, description="Minimum P/E ratio"),
    max_pe: float = Query(None, description="Maximum P/E ratio"),

    min_pb: float = Query(None, description="Minimum P/B ratio"),
    max_pb: float = Query(None, description="Maximum P/B ratio"),

    min_roe: float = Query(None, description="Minimum ROE (decimal, e.g. 0.15 = 15%)"),
    max_roe: float = Query(None, description="Maximum ROE (decimal)"),

    min_market_cap: float = Query(None, description="Minimum market cap in USD/INR"),
    max_market_cap: float = Query(None, description="Maximum market cap"),

    min_de: float = Query(None, description="Minimum D/E ratio"),
    max_de: float = Query(None, description="Maximum D/E ratio"),

    min_dividend_yield: float = Query(None, description="Minimum dividend yield (decimal, e.g. 0.02 = 2%)"),
    max_dividend_yield: float = Query(None, description="Maximum dividend yield"),

    min_eps: float = Query(None, description="Minimum EPS"),
    max_eps: float = Query(None, description="Maximum EPS"),

    min_week_change: float = Query(None, description="Minimum 1-week price change % (e.g. -5 = -5%)"),
    max_week_change: float = Query(None, description="Maximum 1-week price change %"),
):
    """
    Screen a user-supplied list of tickers against optional filters.

    Metrics fetched per ticker:
      - Current Price
      - P/E Ratio (trailing)
      - P/B Ratio
      - ROE
      - Market Cap
      - D/E Ratio
      - Dividend Yield
      - EPS (trailing)
      - 1-Week Price Change %

    Returns all tickers with their metrics plus a 'passed' flag indicating
    whether they passed all active filters.
    """
    import datetime

    # ── Parse and clean tickers ───────────────────────────────────────────────
    raw_tickers = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not raw_tickers:
        return {"error": "No valid tickers provided."}
    if len(raw_tickers) > 50:
        return {"error": "Maximum 50 tickers per request."}

    results = []

    for raw in raw_tickers:
        ticker_sym = raw
        if market.lower() == "india" and not ticker_sym.endswith(".NS"):
            ticker_sym += ".NS"

        try:
            stock = yf.Ticker(ticker_sym)
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

            # 1-week price change
            try:
                hist = stock.history(period="5d")
                if hist is not None and len(hist) >= 2:
                    price_now  = float(hist["Close"].iloc[-1])
                    price_prev = float(hist["Close"].iloc[0])
                    if price_prev and price_prev != 0:
                        week_change = ((price_now - price_prev) / price_prev) * 100
            except Exception:
                week_change = None

            # ── Apply filters ─────────────────────────────────────────────────
            def in_range(val, mn, mx):
                """Return False only if val exists AND violates the range."""
                if val is None:
                    return True   # can't filter what we don't have
                if mn is not None and val < mn:
                    return False
                if mx is not None and val > mx:
                    return False
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
                "ticker":         ticker_sym,
                "current_price":  current_price,
                "pe_ratio":       round(pe_ratio, 2)       if pe_ratio       is not None else None,
                "pb_ratio":       round(pb_ratio, 2)       if pb_ratio       is not None else None,
                "roe":            round(roe, 4)             if roe            is not None else None,
                "market_cap":     market_cap,
                "de_ratio":       round(de_ratio, 2)       if de_ratio       is not None else None,
                "dividend_yield": round(dividend_yield, 4) if dividend_yield is not None else None,
                "eps":            round(eps, 2)             if eps            is not None else None,
                "week_change_pct":round(week_change, 2)    if week_change    is not None else None,
                "passed_filters": passed,
            })

        except Exception as e:
            results.append({
                "ticker":          ticker_sym,
                "error":           str(e),
                "passed_filters":  False,
            })

    passed_count = sum(1 for r in results if r.get("passed_filters"))

    return {
        "market":        market,
        "tickers_scanned": len(results),
        "passed_count":  passed_count,
        "filters_applied": {
            "pe":             [min_pe, max_pe],
            "pb":             [min_pb, max_pb],
            "roe":            [min_roe, max_roe],
            "market_cap":     [min_market_cap, max_market_cap],
            "de":             [min_de, max_de],
            "dividend_yield": [min_dividend_yield, max_dividend_yield],
            "eps":            [min_eps, max_eps],
            "week_change_pct":[min_week_change, max_week_change],
        },
        "results": results,
    }
@app.get("/ipos")
def get_ipos():
    results = []

    IPOS = [
        {"name": "ZOMATO", "ticker": "ZOMATO.NS", "ipo_price": 76, "ipo_date": "2021-07-23"},
        {"name": "PAYTM", "ticker": "PAYTM.NS", "ipo_price": 2150, "ipo_date": "2021-11-18"}
    ]

    for ipo in IPOS:
        stock = yf.Ticker(ipo["ticker"])
        info = stock.info
        current_price = info.get("currentPrice")

        gain_pct = None
        if current_price:
            gain_pct = ((current_price - ipo["ipo_price"]) / ipo["ipo_price"]) * 100

        results.append({
            "name": ipo["name"],
            "ipo_date": ipo["ipo_date"],
            "ipo_price": ipo["ipo_price"],
            "current_price": current_price,
            "gain_pct": gain_pct
        })

    return results

@app.get("/commodities")
def get_commodities():
    commodities = {
        "Gold": "GC=F",
        "Silver": "SI=F",
        "Crude Oil": "CL=F",
        "Natural Gas": "NG=F"
    }

    data = []

    for name, ticker in commodities.items():
        stock = yf.Ticker(ticker)
        info = stock.info

        data.append({
            "name": name,
            "price": info.get("regularMarketPrice"),
            "change": info.get("regularMarketChangePercent")
        })

    return data
