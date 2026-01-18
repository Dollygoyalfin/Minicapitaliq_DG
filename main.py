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
        income = stock.financials.T.head(5).to_dict()
        cashflow = stock.cashflow.T.head(5).to_dict()
        balance_sheet = stock.balance_sheet.T.head(5).to_dict()

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
