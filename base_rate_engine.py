"""
MiniTradeIQ — Base-Rate Engine v2
==================================
Answers "what happened historically when a stock looked like this?" — but
only when the question is specific enough for the answer to mean something.

Four corrections over v1, each addressing a way v1's numbers looked precise
while meaning little:

1. BENCHMARK-RELATIVE RETURNS. v1 reported raw forward returns, so in a
   5-year bull market almost any setup showed a ~69% win rate — that was
   the market, not the setup. v2 reports EXCESS return vs the equal-weight
   market over the same window.

2. STRICT EPISODE SEPARATION. v1 counted a stock drifting in and out of a
   band as many "episodes" of one continuous decline. v2 requires a minimum
   gap (60 trading days) between episodes for the same stock.

3. SETUP SPECIFICITY GATE. A filter matching >15% of all stock-days is not
   a setup, it is a description of the market. v2 measures selectivity and
   warns when a filter is too broad.

4. SERVER-SIDE FILTERING. v1 pulled all 1.19M rows per query. v2 filters in
   SQL and pulls only what it needs.
"""

import sys
import time
import pandas as pd
import numpy as np
from data_store import _conn

BASE_RATE_BUILD = "2026-07-27a (equal-weight index from returns, weekly)"

MIN_EPISODES    = 15
MIN_EPISODE_GAP = 12        # weeks (~60 trading days)
MAX_SELECTIVITY = 0.15
# Signatures are sampled WEEKLY, so these offsets are in WEEKS, not trading
# days. Using trading-day counts against a weekly series would silently make
# every horizon five times longer than its label — a "1 month" return would
# actually measure five months, with no error raised anywhere.
HORIZONS = {"1m": 4, "3m": 13, "6m": 26, "12m": 52}


def _connect_with_retry(attempts: int = 3):
    last = None
    for i in range(attempts):
        try:
            return _conn()
        except Exception as e:
            last = e
            if i < attempts - 1:
                wait = 10 * (i + 1)
                print(f"  connection busy, retrying in {wait}s...")
                time.sleep(wait)
    raise last


def load_benchmark() -> pd.DataFrame:
    """Equal-weight market proxy: the mean of per-stock RETURNS.

    This previously chained the percentage change of the AVERAGE PRICE across
    stocks, which is not an index — it is an artefact. Companies have
    different history start dates, so when a high-priced stock enters the
    sample the average price jumps and the "index" records a large fake
    return. In testing, a universe whose true average move was +1.5% produced
    a +4974% index that way. Since excess return = stock return minus market
    return, that corrupted every base rate the engine reported.
    """
    conn = _connect_with_retry()
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '300s'")
            cur.execute("""
                SELECT ticker, market, date, price
                FROM stock_signatures WHERE price IS NOT NULL
            """)
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["ticker", "market", "date", "price"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"])
    df["ret"] = df.groupby("ticker")["price"].pct_change()

    out = []
    for mkt, g in df.groupby("market"):
        # Mean of returns on each date = equal-weight index return. A stock
        # entering the sample contributes NaN on its first date and is simply
        # excluded from that day's mean, so entry cannot fabricate a move.
        daily = g.groupby("date")["ret"].mean().sort_index()
        idx = (1 + daily.fillna(0)).cumprod()
        out.append(pd.DataFrame({"market": mkt, "date": idx.index,
                                 "mkt_index": idx.values}))
    return pd.concat(out, ignore_index=True)


def load_filtered_signatures(filters: dict, market: str = None) -> pd.DataFrame:
    where, params = [], []
    for col, (lo, hi) in filters.items():
        where.append(f"{col} BETWEEN %s AND %s")
        params += [lo, hi]
    if market:
        where.append("market = %s")
        params.append(market)
    sql = f"SELECT ticker, date, market FROM stock_signatures WHERE {' AND '.join(where)}"
    conn = _connect_with_retry()
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '300s'")
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["ticker", "date", "market"])
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def load_prices_for(tickers) -> pd.DataFrame:
    tickers = list(tickers)
    if not tickers:
        return pd.DataFrame(columns=["ticker", "date", "price"])
    conn = _connect_with_retry()
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '300s'")
            cur.execute("""SELECT ticker, date, price, market FROM stock_signatures
                           WHERE ticker = ANY(%s)""", (tickers,))
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["ticker", "date", "price", "market"])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["ticker", "date"])


def _total_days() -> int:
    conn = _connect_with_retry()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM stock_signatures")
            return cur.fetchone()[0]
    finally:
        conn.close()


def analyze_setup(filters: dict, market: str = None, label: str = "setup") -> dict:
    matches = load_filtered_signatures(filters, market)
    if matches.empty:
        return {"label": label, "error": "No historical days matched this setup."}

    selectivity = len(matches) / max(_total_days(), 1)
    prices = load_prices_for(set(matches["ticker"]))
    bench  = load_benchmark()
    bmaps  = {m: dict(zip(g["date"], g["mkt_index"]))
              for m, g in bench.groupby("market")}

    results = {h: [] for h in HORIZONS}
    episode_count = 0

    for ticker, g in matches.groupby("ticker"):
        series = prices[prices["ticker"] == ticker].reset_index(drop=True)
        if series.empty:
            continue
        mkt  = series["market"].iloc[0]
        bmap = bmaps.get(mkt, {})
        pos  = {d: i for i, d in enumerate(series["date"])}

        episodes, last = [], None
        for d in sorted(g["date"]):
            i = pos.get(d)
            if i is None:
                continue
            if last is None or (i - last) >= MIN_EPISODE_GAP:
                episodes.append(i)
                last = i

        for start in episodes:
            episode_count += 1
            p0 = series["price"].iloc[start]
            b0 = bmap.get(series["date"].iloc[start])
            for hlabel, hdays in HORIZONS.items():
                end = start + hdays
                if end >= len(series) or not p0 or p0 <= 0:
                    continue
                p1 = series["price"].iloc[end]
                b1 = bmap.get(series["date"].iloc[end])
                if b0 and b1 and b0 > 0:
                    results[hlabel].append((p1 / p0 - 1) - (b1 / b0 - 1))

    summary = {"label": label,
               "selectivity_pct": round(selectivity * 100, 2),
               "n_episodes": episode_count,
               "horizons": {}}
    if selectivity > MAX_SELECTIVITY:
        summary["warning"] = (
            f"Matches {selectivity*100:.1f}% of all stock-days — too broad to be "
            f"meaningful. This describes the market, not a specific setup.")

    for hlabel, vals in results.items():
        n = len(vals)
        if n < MIN_EPISODES:
            summary["horizons"][hlabel] = {
                "n": n, "verdict": "insufficient",
                "message": f"Only {n} independent episodes (need {MIN_EPISODES})."}
            continue
        a = np.array(vals)
        summary["horizons"][hlabel] = {
            "n": n,
            "verdict": "low" if n < 30 else "moderate" if n < 75 else "adequate",
            "median_excess":   round(float(np.median(a)) * 100, 1),
            "mean_excess":     round(float(np.mean(a)) * 100, 1),
            "beat_market_pct": round(float((a > 0).mean()) * 100, 1),
            "p25": round(float(np.percentile(a, 25)) * 100, 1),
            "p75": round(float(np.percentile(a, 75)) * 100, 1),
        }
    return summary


def print_summary(s: dict):
    print(f"\n{'='*70}")
    print(s["label"])
    print("=" * 70)
    if s.get("error"):
        print(f"  {s['error']}")
        return
    print(f"  selectivity : {s['selectivity_pct']}% of all stock-days")
    print(f"  episodes    : {s['n_episodes']} (min {MIN_EPISODE_GAP} weeks apart)")
    if s.get("warning"):
        print(f"\n  WARNING: {s['warning']}")
    print("\n  Forward EXCESS return vs equal-weight market:")
    for h in HORIZONS:
        d = s["horizons"].get(h, {})
        if d.get("verdict") == "insufficient":
            print(f"    {h:>4}: {d['message']}")
        elif d:
            print(f"    {h:>4}: n={d['n']:<4} median={d['median_excess']:+.1f}%  "
                  f"beat-mkt={d['beat_market_pct']}%  "
                  f"[p25 {d['p25']:+.1f}%, p75 {d['p75']:+.1f}%]  ({d['verdict']})")


def validate():
    print("VALIDATION — using EXCESS returns, a real effect shows positive")
    print("median excess; a non-effect hovers near zero.")

    print_summary(analyze_setup(
        {"rank_from_low": (0, 5), "rank_vs_200dma": (0, 10)},
        label="DEEP VALUE: bottom 5% from 52wk low AND bottom 10% vs 200DMA"))

    print_summary(analyze_setup(
        {"rank_mom_12m": (95, 100), "rank_vs_200dma": (90, 100)},
        label="STRONG MOMENTUM: top 5% 12m momentum AND top 10% vs 200DMA"))

    print(f"\n{'='*70}\nMANDATORY DISCLOSURE\n{'='*70}")
    print("  - Survivorship bias: today's index members only.")
    print("  - ~5 years = ONE regime. Not a claim about all markets.")
    print("  - Excess vs an equal-weight proxy, not a tradable index.")
    print("  - Base rates are context for judgment, NOT a prediction.")


def query_stock(ticker: str, market: str):
    raw = ticker.upper()
    if market.lower() == "india" and not raw.endswith(".NS"):
        raw += ".NS"
    conn = _connect_with_retry()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT date, rank_vs_200dma, rank_from_high,
                                  rank_from_low, rank_mom_3m, rank_mom_12m,
                                  rank_volatility
                           FROM stock_signatures WHERE ticker = %s
                           ORDER BY date DESC LIMIT 1""", (raw,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        print(f"No signature data for {raw}")
        return
    d, r200, rhigh, rlow, m3, m12, vol = row
    print(f"\n{raw} — signature as of {d}")
    print(f"  vs 200DMA     : {r200:.0f}th percentile")
    print(f"  from 52wk high: {rhigh:.0f}th")
    print(f"  from 52wk low : {rlow:.0f}th")
    print(f"  momentum 3m   : {m3:.0f}th")
    print(f"  momentum 12m  : {m12:.0f}th")
    print(f"  volatility    : {vol:.0f}th")

    def band(v, w=7):
        return (max(0, v - w), min(100, v + w))
    filters = {"rank_from_low":  band(rlow),
               "rank_vs_200dma": band(r200),
               "rank_mom_12m":   band(m12)}
    print_summary(analyze_setup(
        filters, market=market,
        label=f"Stocks that looked like {raw} today (±7pp on 3 dimensions)"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "validate"
    if cmd == "validate":
        validate()
    elif cmd == "query":
        if len(sys.argv) < 4:
            print("Usage: python base_rate_engine.py query TICKER MARKET")
        else:
            query_stock(sys.argv[2], sys.argv[3])
    else:
        print("Usage: python base_rate_engine.py [validate|query TICKER MARKET]")
