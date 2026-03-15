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
    terminal_growth_rate: float = Query(0.03, description="Terminal growth rate for perpetuity (decimal)"),
    margin_of_safety: float = Query(0.25, description="Margin of safety (decimal, e.g. 0.25 = 25%)")
):
    """
    Cash-based FCFF DCF Valuation.

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
        opex_growth    = min(opex_growth, revenue_growth * 1.5)
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

        equity_val    = market_cap if market_cap else 1
        debt_val      = total_debt if total_debt else equity_val * 0.2
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
        term_wc        = term_ca - term_cl - term_cash - term_cpltd
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
        terminal_value    = term_fcff / (wacc - terminal_growth_rate)
        pv_terminal_value = terminal_value / ((1 + wacc) ** projection_years)

        # ── Enterprise & Equity Value ─────────────────────────────────────────
        enterprise_value = total_pv_fcff + pv_terminal_value

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

        # ── Response ──────────────────────────────────────────────────────────
        return {
            "ticker":        raw_ticker,
            "market":        market,
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
