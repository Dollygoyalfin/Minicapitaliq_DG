# main.py (FastAPI backend)
from fastapi import FastAPI
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
def read_root():
    return {"message": "Mini Capital IQ backend is running 🚀"}

@app.get("/valuation/{ticker}")
def get_valuation(ticker: str, growth: float = 0.08, discount: float = 0.10):
    try:
        stock = yf.Ticker(ticker)
        info = stock.info

        current_price = info.get("currentPrice")
        eps = info.get("trailingEps")
        pe_ratio = info.get("trailingPE")
        forward_pe = info.get("forwardPE")
        beta = info.get("beta")
        pb_ratio = info.get("priceToBook")
        market_cap = info.get("marketCap")
        roe = info.get("returnOnEquity")
        de_ratio = info.get("debtToEquity")

        # Simple DCF model assuming constant growth
        if eps is not None and discount > growth:
            intrinsic_value = eps * (1 + growth) / (discount - growth)
        else:
            intrinsic_value = None

        return {
            "ticker": ticker.upper(),
            "current_price": current_price,
            "eps": eps,
            "pe_ratio": pe_ratio,
            "forward_pe": forward_pe,
            "beta": beta,
            "pb_ratio": pb_ratio,
            "market_cap": market_cap,
            "roe": roe * 100 if roe is not None else None,
            "de_ratio": de_ratio,
            "intrinsic_value": intrinsic_value,
            "growth_rate_used": growth,
            "discount_rate_used": discount
        }

    except Exception as e:
        return {"error": str(e)}
