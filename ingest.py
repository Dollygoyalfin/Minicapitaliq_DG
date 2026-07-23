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

import os
import sys
import time

# Ingestion mode: skip live-price enrichment (prices are fetched at
# request-time, not stored) — avoids pointless yfinance calls during backfill
os.environ["MINITRADEIQ_INGEST"] = "1"
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


def fetch_us_universe() -> list:
    """
    S&P 500 constituents from the maintained datasets CSV on GitHub
    (tested live: 503 symbols, includes GICS Sector). Dots in class-share
    tickers (BRK.B) are converted to dashes for SEC's ticker map.
    Falls back to the hardcoded starter list on failure.
    """
    global US_SECTOR_MAP
    try:
        import httpx, csv, io
        resp = httpx.get(
            "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
            timeout=30.0, follow_redirects=True,
        )
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        syms, US_SECTOR_MAP = [], {}
        for r in rows:
            s = (r.get("Symbol") or "").strip().upper().replace(".", "-")
            if s:
                syms.append(s)
                US_SECTOR_MAP[s] = (r.get("GICS Sector") or "").strip()
        if len(syms) >= 400:
            print(f"  Universe: {len(syms)} S&P 500 tickers (constituents CSV)")
            return syms
    except Exception as e:
        print(f"  Universe fetch failed ({e}) — using starter list")
    return US_UNIVERSE


US_SECTOR_MAP: dict = {}   # ticker -> GICS Sector, filled by fetch_us_universe


def fetch_india_universe() -> list:
    """
    Nifty 500 constituents from NSE's official index CSV (via the same
    cookie-handshake session the pipeline already uses).
    Falls back to the starter list.
    """
    try:
        import csv, io
        from india_data_pipeline import _nse_get
        resp = _nse_get(
            "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
        )
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        syms = [r["Symbol"].strip().upper() for r in rows if r.get("Symbol")]
        if len(syms) >= 300:
            print(f"  Universe: {len(syms)} Nifty 500 tickers from NSE")
            return syms
    except Exception as e:
        print(f"  Universe fetch failed ({e}) — using starter list")
    return INDIA_UNIVERSE


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

        # Enrich US sector from constituents CSV when SEC SIC mapping failed
        if market == "us" and info.get("sector") in (None, "Unknown"):
            gics = US_SECTOR_MAP.get(raw_ticker)
            if gics:
                _g2s = {
                    "information technology": "Technology",
                    "health care": "Healthcare",
                    "financials": "Financial Services",
                    "consumer discretionary": "Consumer Cyclical",
                    "consumer staples": "Consumer Defensive",
                    "communication services": "Communication Services",
                    "industrials": "Industrials",
                    "energy": "Energy",
                    "materials": "Basic Materials",
                    "utilities": "Utilities",
                    "real estate": "Real Estate",
                }
                info["sector"] = _g2s.get(gics.lower(), gics)
                info["industry"] = info.get("industry") or gics
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


def backfill_prices(target: str = "both", period: str = "5y"):
    """
    Batched daily-price backfill via yf.download (40 tickers per call —
    far gentler than per-ticker). ~15 min for the full 1,000-name universe.
    Also used nightly with period='7d' as a cheap top-up.
    """
    import yfinance as yf
    from data_store import _conn, upsert_prices

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker, market FROM companies")
            all_companies = cur.fetchall()

    tickers = [t for t, m in all_companies
               if target == "both" or m == target]
    print(f"Price backfill: {len(tickers)} tickers, period={period}")

    total_rows = 0
    for i in range(0, len(tickers), 40):
        batch = tickers[i:i + 40]
        try:
            data = yf.download(
                " ".join(batch), period=period, interval="1d",
                group_by="ticker", auto_adjust=True,
                threads=True, progress=False,
            )
        except Exception as e:
            print(f"  batch {i//40 + 1}: download failed ({e})")
            time.sleep(5)
            continue

        for t in batch:
            try:
                sub = data[t] if len(batch) > 1 else data
                sub = sub.dropna(subset=["Close"])
                rows = []
                for idx, r in sub.iterrows():
                    vol = r.get("Volume")
                    rows.append((idx.date(), float(r["Close"]),
                                 int(vol) if vol == vol else None))
                n = upsert_prices(t, rows)
                total_rows += n
            except Exception:
                pass
        print(f"  batch {i//40 + 1}/{(len(tickers)-1)//40 + 1} done "
              f"({total_rows} rows so far)")
        time.sleep(2)
    print(f"✅ Price backfill complete: {total_rows} rows.")


def refresh_prices():
    """Nightly top-up: last 7 days for every ticker (fast, batched)."""
    backfill_prices(target="both", period="7d")


def refresh_all():
    """
    Nightly refresh. Re-fetches all companies in the store.
    Since financials change quarterly, most nights this just confirms
    existing data. Run via cron at 2 AM.
    """
    from data_store import _conn
    with _conn() as conn:
        with conn.cursor() as cur:
            # Financials change quarterly — refresh each company ~weekly.
            # Oldest-updated first, hard nightly cap keeps runs ~1-1.5h.
            cur.execute("""
                SELECT ticker, market FROM companies
                WHERE updated_at < NOW() - INTERVAL '6 days'
                ORDER BY updated_at ASC
                LIMIT 200
            """)
            companies = cur.fetchall()
    if not companies:
        print("Nothing stale — all companies refreshed within 6 days.")
        return

    print(f"Refreshing {len(companies)} companies...")
    ok = 0
    for ticker, market in companies:
        clean_ticker = ticker.replace(".NS", "")
        try:
            if market == "india":
                from india_data_pipeline import ingest_india_own
                if ingest_india_own(clean_ticker):
                    ok += 1
            else:
                if ingest_one(clean_ticker, market):
                    ok += 1
        except Exception as e:
            print(f"  ❌ {clean_ticker}: {e}")
        time.sleep(1.5 if market == "us" else 3.0)
    print(f"✅ Refresh complete: {ok}/{len(companies)}.")
    # Daily price top-up for the whole universe (cheap, batched)
    try:
        refresh_prices()
    except Exception as e:
        print(f"⚠ price refresh failed: {e}")


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
    elif cmd == "one":
        # Re-ingest a single ticker: python ingest.py one MARUTI [india|us]
        tkr = sys.argv[2] if len(sys.argv) > 2 else None
        mkt = sys.argv[3] if len(sys.argv) > 3 else "india"
        if not tkr:
            print("Usage: python ingest.py one TICKER [india|us]")
        elif mkt == "india":
            from india_data_pipeline import ingest_india_own
            ingest_india_own(tkr)
        else:
            ingest_one(tkr, mkt)
    elif cmd == "backfill_us_full":
        backfill(fetch_us_universe(), "us")
    elif cmd == "backfill_india_full":
        backfill_india_own(fetch_india_universe())
    elif cmd == "backfill_prices":
        tgt = sys.argv[2] if len(sys.argv) > 2 else "both"
        backfill_prices(target=tgt)
    elif cmd == "refresh":
        refresh_all()
    else:
        print("Usage: python ingest.py [init|one TICKER MARKET|backfill_us|backfill_india|backfill_india_own|backfill_us_full|backfill_india_full|refresh]")
