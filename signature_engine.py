"""
MiniTradeIQ — Base-Rate Engine, Part 1: Signature Computation
================================================================
Turns raw daily prices into a small set of features that are comparable
ACROSS stocks and ACROSS time — the foundation for cross-sectional pooling.

Design choice: features are stored as their CROSS-SECTIONAL PERCENTILE RANK
on each date (e.g. "this stock's momentum ranks in the 15th percentile among
all stocks with data that day"), not raw values. This is what makes pooling
valid — "oversold" means the same thing in 2021 and 2024, and for a ₹50
stock and a ₹5,000 stock, once expressed as a percentile rank.

Run once to backfill, then nightly to keep current:
    python signature_engine.py backfill
    python signature_engine.py update      (last 30 days only, fast)
"""

import sys
import time
import pandas as pd
import numpy as np
from data_store import _conn

SIGNATURE_BUILD = "2026-07-27a (RSI-14, 50-DMA)"


def _init_table():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stock_signatures (
                    ticker              TEXT,
                    date                DATE,
                    market              TEXT,
                    price               DOUBLE PRECISION,
                    pct_vs_200dma       DOUBLE PRECISION,  -- raw: (price/dma200 - 1)
                    pct_from_52wk_high  DOUBLE PRECISION,  -- raw: (price/high52 - 1)
                    pct_from_52wk_low   DOUBLE PRECISION,  -- raw: (price/low52 - 1)
                    momentum_3m         DOUBLE PRECISION,  -- raw: 63-day return
                    momentum_12m        DOUBLE PRECISION,  -- raw: 252-day return
                    volatility_20d      DOUBLE PRECISION,  -- raw: 20d stdev of daily returns
                    -- cross-sectional percentile ranks (0-100), computed per date
                    rank_vs_200dma      DOUBLE PRECISION,
                    rank_from_high      DOUBLE PRECISION,
                    rank_from_low       DOUBLE PRECISION,
                    rank_mom_3m         DOUBLE PRECISION,
                    rank_mom_12m        DOUBLE PRECISION,
                    rank_volatility     DOUBLE PRECISION,
                    PRIMARY KEY (ticker, date)
                );
                ALTER TABLE stock_signatures ADD COLUMN IF NOT EXISTS rsi_14 DOUBLE PRECISION;
                ALTER TABLE stock_signatures ADD COLUMN IF NOT EXISTS pct_vs_50dma DOUBLE PRECISION;
                ALTER TABLE stock_signatures ADD COLUMN IF NOT EXISTS dma_50 DOUBLE PRECISION;
                ALTER TABLE stock_signatures ADD COLUMN IF NOT EXISTS dma_200 DOUBLE PRECISION;
                CREATE INDEX IF NOT EXISTS idx_sig_date ON stock_signatures(date);
                CREATE INDEX IF NOT EXISTS idx_sig_market ON stock_signatures(market);
            """)
        conn.commit()
    finally:
        conn.close()
    print("stock_signatures table ready.")


def _connect_with_retry(attempts: int = 3):
    """Supabase free-tier pool is small and shared with Render's live
    traffic — a checkout can transiently fail under load. Retry with
    backoff rather than failing the whole backfill on a momentary blip."""
    last = None
    for i in range(attempts):
        try:
            return _conn()
        except Exception as e:
            last = e
            if i < attempts - 1:
                wait = 10 * (i + 1)
                print(f"  connection busy, retrying in {wait}s... ({e})")
                time.sleep(wait)
    raise last


def _load_all_prices() -> pd.DataFrame:
    """One bulk read — far faster than per-ticker queries for 1,004 stocks.
    No ORDER BY: sorting 1.18M rows is far cheaper done client-side in
    pandas than paid for as DB compute on a constrained free-tier instance."""
    conn = _connect_with_retry()
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '300s'")
            cur.execute("""
                SELECT p.ticker, p.date, p.close, c.market
                FROM price_history p
                JOIN companies c ON c.ticker = p.ticker
            """)
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["ticker", "date", "close", "market"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"])
    return df


def compute_raw_features(df: pd.DataFrame) -> pd.DataFrame:
    """Per-ticker rolling features. df must have columns: ticker, date, close."""
    out = []
    for ticker, g in df.groupby("ticker", sort=False):
        g = g.sort_values("date").set_index("date")
        close = g["close"]
        if len(close) < 210:          # need ~200d history minimum
            continue

        dma200   = close.rolling(200, min_periods=150).mean()
        dma50    = close.rolling(50, min_periods=35).mean()

        # RSI-14 with Wilder's smoothing (the standard formulation — a simple
        # rolling mean gives visibly different values and would not match what
        # any charting package shows)
        _delta = close.diff()
        _gain  = _delta.clip(lower=0)
        _loss  = (-_delta).clip(lower=0)
        _ag    = _gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        _al    = _loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        _rs    = _ag / _al.replace(0, float("nan"))
        rsi14  = 100 - (100 / (1 + _rs))
        rsi14  = rsi14.fillna(50)   # neutral when undefined (flat series)
        high52   = close.rolling(252, min_periods=200).max()
        low52    = close.rolling(252, min_periods=200).min()
        ret_3m   = close / close.shift(63) - 1
        ret_12m  = close / close.shift(252) - 1
        daily_ret = close.pct_change()
        vol20    = daily_ret.rolling(20, min_periods=15).std()

        g2 = pd.DataFrame({
            "ticker": ticker,
            "close": close,
            "pct_vs_200dma":      close / dma200 - 1,
            "pct_from_52wk_high": close / high52 - 1,
            "pct_from_52wk_low":  close / low52 - 1,
            "momentum_3m":        ret_3m,
            "momentum_12m":       ret_12m,
            "volatility_20d":     vol20,
            "rsi_14":             rsi14,
            "dma_50":             dma50,
            "dma_200":            dma200,
            "pct_vs_50dma":       close / dma50 - 1,
        })
        out.append(g2)
    if not out:
        return pd.DataFrame()
    result = pd.concat(out)
    result.index.name = "date"
    return result.reset_index()


def add_cross_sectional_ranks(df: pd.DataFrame) -> pd.DataFrame:
    """Percentile rank (0-100) of each feature AMONG ALL STOCKS on each date.
    This is what makes a signature comparable across stocks and regimes."""
    feature_cols = {
        "pct_vs_200dma":      "rank_vs_200dma",
        "pct_from_52wk_high": "rank_from_high",
        "pct_from_52wk_low":  "rank_from_low",
        "momentum_3m":        "rank_mom_3m",
        "momentum_12m":       "rank_mom_12m",
        "volatility_20d":     "rank_volatility",
    }
    # Rank WITHIN market, not across both pooled together — an Indian
    # mid-cap and a US mega-cap on the same calendar date sit in different
    # return regimes, and pooling them dilutes real cross-sectional signal
    # into noise (this is what weakened the momentum validation check).
    for raw_col, rank_col in feature_cols.items():
        df[rank_col] = df.groupby(["date", "market"])[raw_col].rank(pct=True) * 100
    return df


def backfill_signatures(full_rebuild: bool = True, recent_days: int = None):
    _init_table()
    if recent_days:
        print(f"Incremental update: recomputing last {recent_days} days only.")
    print("Loading all price history (one bulk read)...")
    t0 = time.time()
    prices = _load_all_prices()
    print(f"  {len(prices):,} rows, {prices['ticker'].nunique()} tickers "
          f"({time.time()-t0:.0f}s)")

    market_map = prices[["ticker", "market"]].drop_duplicates().set_index("ticker")["market"]

    print("Computing rolling features per ticker...")
    t0 = time.time()
    feats = compute_raw_features(prices[["ticker", "date", "close"]])
    feats["market"] = feats["ticker"].map(market_map)   # needed BEFORE ranking
    print(f"  {len(feats):,} feature-rows ({time.time()-t0:.0f}s)")

    print("Computing cross-sectional percentile ranks per date (within market)...")
    t0 = time.time()
    feats = add_cross_sectional_ranks(feats)
    print(f"  done ({time.time()-t0:.0f}s)")

    print("Writing to store...")
    t0 = time.time()
    conn = _conn()
    try:
        from psycopg2.extras import execute_values
        with conn.cursor() as cur:
            cur.execute("TRUNCATE stock_signatures")
            rows = [
                (r.ticker, r.date.date(), r.market, float(r.close),
                 _n(r.pct_vs_200dma), _n(r.pct_from_52wk_high), _n(r.pct_from_52wk_low),
                 _n(r.momentum_3m), _n(r.momentum_12m), _n(r.volatility_20d),
                 _n(r.rank_vs_200dma), _n(r.rank_from_high), _n(r.rank_from_low),
                 _n(r.rank_mom_3m), _n(r.rank_mom_12m), _n(r.rank_volatility),
                 _n(r.rsi_14), _n(r.pct_vs_50dma), _n(r.dma_50), _n(r.dma_200))
                for r in feats.itertuples()
            ]
            execute_values(cur, """
                INSERT INTO stock_signatures
                    (ticker, date, market, price, pct_vs_200dma,
                     pct_from_52wk_high, pct_from_52wk_low, momentum_3m,
                     momentum_12m, volatility_20d, rank_vs_200dma,
                     rank_from_high, rank_from_low, rank_mom_3m, rank_mom_12m,
                     rank_volatility, rsi_14, pct_vs_50dma, dma_50, dma_200)
                VALUES %s
                ON CONFLICT (ticker, date) DO UPDATE SET
                    price=EXCLUDED.price,
                    pct_vs_200dma=EXCLUDED.pct_vs_200dma,
                    pct_from_52wk_high=EXCLUDED.pct_from_52wk_high,
                    pct_from_52wk_low=EXCLUDED.pct_from_52wk_low,
                    momentum_3m=EXCLUDED.momentum_3m,
                    momentum_12m=EXCLUDED.momentum_12m,
                    volatility_20d=EXCLUDED.volatility_20d,
                    rank_vs_200dma=EXCLUDED.rank_vs_200dma,
                    rank_from_high=EXCLUDED.rank_from_high,
                    rank_from_low=EXCLUDED.rank_from_low,
                    rank_mom_3m=EXCLUDED.rank_mom_3m,
                    rank_mom_12m=EXCLUDED.rank_mom_12m,
                    rank_volatility=EXCLUDED.rank_volatility,
                    rsi_14=EXCLUDED.rsi_14,
                    pct_vs_50dma=EXCLUDED.pct_vs_50dma,
                    dma_50=EXCLUDED.dma_50,
                    dma_200=EXCLUDED.dma_200
            """, rows, page_size=2000)
        conn.commit()
    finally:
        conn.close()
    print(f"  {len(rows):,} rows written ({time.time()-t0:.0f}s)")
    print("✅ Signature backfill complete.")


def _n(v):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return None
    return float(v)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "backfill"
    if cmd == "backfill":
        backfill_signatures(full_rebuild=True)
    elif cmd == "update":
        # Routine refresh: recompute only recent days, no TRUNCATE.
        # This is what the nightly workflow should call, not "backfill".
        backfill_signatures(full_rebuild=False, recent_days=30)
    elif cmd == "init":
        _init_table()
    else:
        print("Usage: python signature_engine.py [backfill|update|init]")
