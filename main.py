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
    projection_years: int = Query(5, description="Number of years to project FCF"),
    risk_free_rate: float = Query(0.04, description="Risk-free rate (decimal)"),
    market_return: float = Query(0.10, description="Expected market return (decimal)"),
    revenue_growth_rate: float = Query(0.08, description="Revenue growth rate (decimal)"),
    fcf_margin: float = Query(None, description="Override FCF margin (decimal). Auto-calculated if not provided."),
    terminal_growth_rate: float = Query(0.03, description="Terminal growth rate (decimal)"),
    margin_of_safety: float = Query(0.25, description="Margin of safety (decimal, e.g. 0.25 = 25%)")
):
    """
    Full multi-year DCF valuation using Free Cash Flow projections.

    Steps:
    1. Pull trailing FCF and revenue from yfinance
    2. Calculate historical FCF margin
    3. Project revenue and FCF over N years
    4. Discount each year's FCF using WACC
    5. Calculate terminal value using Gordon Growth
    6. Sum PV of FCFs + PV of terminal value = Enterprise Value
    7. Subtract net debt → Equity Value → Intrinsic value per share
    """
    try:
        # --- Ticker setup ---
        raw_ticker = ticker.upper()
        if market.lower() == "india" and not raw_ticker.endswith(".NS"):
            raw_ticker += ".NS"

        stock = yf.Ticker(raw_ticker)
        info = stock.info

        # --- Key info fields ---
        current_price = info.get("currentPrice")
        shares_outstanding = info.get("sharesOutstanding")
        beta = info.get("beta", 1.0) or 1.0
        market_cap = info.get("marketCap")
        total_debt = info.get("totalDebt", 0) or 0
        total_cash = info.get("totalCash", 0) or 0

        if not shares_outstanding or shares_outstanding == 0:
            return {"error": "Shares outstanding not available for this ticker."}

        # --- Pull financials ---
        cashflow_df = stock.cashflow
        income_df = stock.financials

        if cashflow_df is None or cashflow_df.empty:
            return {"error": "Cash flow data not available for this ticker."}
        if income_df is None or income_df.empty:
            return {"error": "Income statement data not available for this ticker."}

        # --- Extract trailing FCF (Operating CF - CapEx) ---
        try:
            operating_cf_row = next(
                (r for r in cashflow_df.index if "Operating" in r and "Cash" in r), None
            )
            capex_row = next(
                (r for r in cashflow_df.index if "Capital" in r and "Expenditure" in r), None
            )

            if operating_cf_row and capex_row:
                operating_cf = float(cashflow_df.loc[operating_cf_row].iloc[0])
                capex = float(cashflow_df.loc[capex_row].iloc[0])
                trailing_fcf = operating_cf + capex  # CapEx is usually negative
            else:
                # Fallback: use Free Cash Flow row if available
                fcf_row = next(
                    (r for r in cashflow_df.index if "Free" in r and "Cash" in r), None
                )
                if fcf_row:
                    trailing_fcf = float(cashflow_df.loc[fcf_row].iloc[0])
                else:
                    return {"error": "Could not extract Free Cash Flow from cash flow statement."}
        except Exception:
            return {"error": "Error parsing cash flow statement."}

        # --- Extract trailing revenue ---
        try:
            revenue_row = next(
                (r for r in income_df.index if "Revenue" in r or "Total Revenue" in r), None
            )
            if revenue_row:
                trailing_revenue = float(income_df.loc[revenue_row].iloc[0])
            else:
                return {"error": "Could not extract revenue from income statement."}
        except Exception:
            return {"error": "Error parsing income statement."}

        # --- FCF Margin ---
        if fcf_margin is not None:
            fcf_margin_used = fcf_margin
        else:
            if trailing_revenue and trailing_revenue != 0:
                fcf_margin_used = trailing_fcf / trailing_revenue
            else:
                return {"error": "Cannot calculate FCF margin — revenue is zero or missing."}

        # --- WACC ---
        cost_of_equity = risk_free_rate + beta * (market_return - risk_free_rate)
        cost_of_debt = 0.06
        equity_value = market_cap if market_cap else 1
        debt_value = total_debt if total_debt else equity_value * 0.2
        total_capital = equity_value + debt_value
        tax_rate = 0.25  # assumed corporate tax rate

        wacc = (
            (equity_value / total_capital) * cost_of_equity +
            (debt_value / total_capital) * cost_of_debt * (1 - tax_rate)
        )

        if wacc <= terminal_growth_rate:
            return {"error": "WACC must be greater than terminal growth rate."}

        # --- Project FCFs ---
        projected_fcfs = []
        projected_revenues = []
        base_revenue = trailing_revenue

        for year in range(1, projection_years + 1):
            projected_revenue = base_revenue * ((1 + revenue_growth_rate) ** year)
            projected_fcf = projected_revenue * fcf_margin_used
            projected_revenues.append(round(projected_revenue, 2))
            projected_fcfs.append(round(projected_fcf, 2))

        # --- Discount projected FCFs ---
        pv_fcfs = []
        for i, fcf in enumerate(projected_fcfs):
            year = i + 1
            pv = fcf / ((1 + wacc) ** year)
            pv_fcfs.append(round(pv, 2))

        total_pv_fcf = sum(pv_fcfs)

        # --- Terminal Value (Gordon Growth on final year FCF) ---
        terminal_fcf = projected_fcfs[-1] * (1 + terminal_growth_rate)
        terminal_value = terminal_fcf / (wacc - terminal_growth_rate)
        pv_terminal_value = terminal_value / ((1 + wacc) ** projection_years)

        # --- Enterprise Value → Equity Value ---
        enterprise_value = total_pv_fcf + pv_terminal_value
        net_debt = total_debt - total_cash
        equity_value_dcf = enterprise_value - net_debt

        # --- Intrinsic value per share ---
        intrinsic_value_per_share = equity_value_dcf / shares_outstanding
        intrinsic_value_with_mos = intrinsic_value_per_share * (1 - margin_of_safety)

        # --- Upside/downside ---
        upside_pct = None
        if current_price and current_price > 0:
            upside_pct = ((intrinsic_value_per_share - current_price) / current_price) * 100

        verdict = None
        if upside_pct is not None:
            if upside_pct > 20:
                verdict = "Potentially Undervalued"
            elif upside_pct < -20:
                verdict = "Potentially Overvalued"
            else:
                verdict = "Fairly Valued"

        return {
            "ticker": raw_ticker,
            "market": market,
            "current_price": current_price,

            # Inputs used
            "projection_years": projection_years,
            "revenue_growth_rate_used": revenue_growth_rate,
            "fcf_margin_used": round(fcf_margin_used, 4),
            "terminal_growth_rate": terminal_growth_rate,
            "wacc": round(wacc, 4),
            "cost_of_equity": round(cost_of_equity, 4),
            "beta_used": beta,
            "tax_rate_assumed": tax_rate,

            # Raw inputs
            "trailing_fcf": round(trailing_fcf, 2),
            "trailing_revenue": round(trailing_revenue, 2),
            "total_debt": total_debt,
            "total_cash": total_cash,
            "net_debt": round(net_debt, 2),
            "shares_outstanding": shares_outstanding,

            # Projections
            "projected_revenues": projected_revenues,
            "projected_fcfs": projected_fcfs,
            "pv_of_fcfs": pv_fcfs,
            "total_pv_fcf": round(total_pv_fcf, 2),

            # Terminal value
            "terminal_value": round(terminal_value, 2),
            "pv_terminal_value": round(pv_terminal_value, 2),

            # Valuation output
            "enterprise_value": round(enterprise_value, 2),
            "equity_value_dcf": round(equity_value_dcf, 2),
            "intrinsic_value_per_share": round(intrinsic_value_per_share, 2),
            "intrinsic_value_with_margin_of_safety": round(intrinsic_value_with_mos, 2),
            "margin_of_safety_used": margin_of_safety,
            "upside_downside_pct": round(upside_pct, 2) if upside_pct is not None else None,
            "verdict": verdict
        }

    except Exception as e:
        return {"error": str(e)}

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
