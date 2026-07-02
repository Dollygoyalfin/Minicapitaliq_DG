"""
MiniTradeIQ Ingestion Script
=============================
Populates your data store with financial statements. Run this:
  1. ONCE for initial backfill (populate your chosen universe)
  2. NIGHTLY via a cron job (refresh — only re-fetches when data changed)

Usage:
    python ingest.py init                    # create tables
    python ingest.py backfill_us             # populate S&P 500 US stocks
    python ingest.py backfill_india          # populate Nifty stocks
    python ingest.py refresh                  # nightly refresh all

The whole point: this runs in the BACKGROUND (nightly), not when a user
makes a request. Users read from the store instantly. Rate limits during
ingestion don't matter because no user is waiting.
"""

import sys
import time
from fmp_data_layer import get_company_data
from data_store import init_db, upsert_company, upsert_statements


# ── Define your stock universe ────────────────────────────────────────────────
# Start small, expand later. These are examples — replace with your list.

US_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NVDA", "JPM",
    "V", "WMT", "JNJ", "PG", "MA", "HD", "DIS", "KO", "PEP", "MCD",
    "CSCO", "INTC", "AMD", "NFLX", "ADBE", "CRM", "ORCL",
    # ... add more up to S&P 500
]

INDIA_UNIVERSE = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "HINDUNILVR",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK",
    "ASIANPAINT", "MARUTI", "TITAN", "SUNPHARMA", "WIPRO", "VBL",
    # ... add more up to Nifty 500
]


def ingest_one(ticker: str, market: str):
    """Fetch one company's data and write it to the store."""
    try:
        source = "sec" if market == "us" else "yfinance"
        info, income_df, balance_df, cashflow_df, data_source = get_company_data(
            ticker=ticker, market=market, source=source
        )
        if income_df is None or income_df.empty:
            print(f"  ⚠ {ticker}: no financial data, skipping")
            return False

        raw_ticker = ticker.upper()
        if market == "india" and not raw_ticker.endswith(".NS"):
            raw_ticker += ".NS"

        upsert_company(info, raw_ticker, market, data_source)
        upsert_statements(raw_ticker, income_df, balance_df, cashflow_df)
        print(f"  ✅ {ticker}: stored ({data_source})")
        return True
    except Exception as e:
        print(f"  ❌ {ticker}: {e}")
        return False


def backfill(universe: list, market: str):
    """Populate the store for a whole universe. Slow but runs in background."""
    total = len(universe)
    success = 0
    for i, ticker in enumerate(universe, 1):
        print(f"[{i}/{total}] {ticker}...")
        if ingest_one(ticker, market):
            success += 1
        # Be polite to data sources — space out requests
        time.sleep(1.5 if market == "us" else 3.0)
    print(f"\n✅ Backfill complete: {success}/{total} stored.")


def backfill_india_own(universe: list):
    """Populate the store for India using YOUR OWN pipeline (NSE + PDF/Groq),
    not yfinance. Slower per stock (PDF + LLM step) but fully independent."""
    from india_data_pipeline import ingest_india_own
    total, success = len(universe), 0
    for i, ticker in enumerate(universe, 1):
        print(f"[{i}/{total}] {ticker}...")
        try:
            if ingest_india_own(ticker):
                success += 1
        except Exception as e:
            print(f"  ❌ {ticker}: {e}")
        time.sleep(4.0)  # be polite to NSE — background job, no user waiting
    print(f"\n✅ Own-pipeline backfill complete: {success}/{total} stored.")


def refresh_all():
    """
    Nightly refresh. Re-fetches all companies in the store.
    Since financials change quarterly, most nights this just confirms
    existing data. Run via cron at 2 AM.
    """
    from data_store import _conn
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker, market FROM companies")
            companies = cur.fetchall()

    print(f"Refreshing {len(companies)} companies...")
    for ticker, market in companies:
        clean_ticker = ticker.replace(".NS", "")
        ingest_one(clean_ticker, market)
        time.sleep(1.5 if market == "us" else 3.0)
    print("✅ Refresh complete.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "init":
        init_db()
    elif cmd == "backfill_us":
        backfill(US_UNIVERSE, "us")
    elif cmd == "backfill_india":
        backfill(INDIA_UNIVERSE, "india")
    elif cmd == "backfill_india_own":
        backfill_india_own(INDIA_UNIVERSE)
    elif cmd == "refresh":
        refresh_all()
    else:
        print("Usage: python ingest.py [init|backfill_us|backfill_india|backfill_india_own|refresh]")
