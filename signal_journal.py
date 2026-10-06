"""
MiniTradeIQ — Signal Journal (Phase D)
=======================================
Records every signal the app generates, then scores itself as time passes.

This is the accountability layer. Without it, "the app said buy and it went
up" is anecdote. With it, you get: "Convergence Strong Buy signals, 6-month
horizon: 71% positive, median +12% excess, n=34."

Design principles:
  - Record at the MOMENT of the signal, never reconstructed later
    (reconstruction invites hindsight bias — the signal you *would* have
    given is not the signal you *did* give).
  - Score against the market, not raw returns, for the same reason the
    base-rate engine does.
  - Never delete or revise a recorded signal. The record is the point.
  - Report honestly including small samples, marked as such.

Usage:
    python signal_journal.py init            # create table
    python signal_journal.py snapshot        # record today's quality grades
    python signal_journal.py dcf-snapshot    # record today's DCF verdicts
    python signal_journal.py score           # how have past signals done?
    python signal_journal.py score --days 90 # only signals older than 90d
    python signal_journal.py score --source dcf
"""

import os
import sys
import time
from datetime import date, timedelta
import pandas as pd
import numpy as np
from data_store import _conn

JOURNAL_BUILD = "2026-09-27 (DCF snapshot; equal-weight benchmark fixed)"


def _connect_with_retry(attempts: int = 3):
    last = None
    for i in range(attempts):
        try:
            return _conn()
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(10 * (i + 1))
    raise last


def init_table():
    conn = _connect_with_retry()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signal_journal (
                    id            SERIAL PRIMARY KEY,
                    signal_date   DATE NOT NULL,
                    ticker        TEXT NOT NULL,
                    market        TEXT NOT NULL,
                    source        TEXT NOT NULL,   -- convergence | quality | baserates | dcf
                    signal        TEXT NOT NULL,   -- Strong Buy | Buy | Hold | Sell | Strong Sell | grade
                    conviction    TEXT,            -- optional: high/medium/low
                    price_at_signal DOUBLE PRECISION,
                    detail        JSONB,           -- the numbers behind the call
                    created_at    TIMESTAMP DEFAULT NOW(),
                    UNIQUE (signal_date, ticker, source)
                );
                CREATE INDEX IF NOT EXISTS idx_journal_date ON signal_journal(signal_date);
                CREATE INDEX IF NOT EXISTS idx_journal_ticker ON signal_journal(ticker);
                CREATE INDEX IF NOT EXISTS idx_journal_source ON signal_journal(source);
            """)
        conn.commit()
    finally:
        conn.close()
    print("signal_journal table ready.")


def record_signal(ticker: str, market: str, source: str, signal: str,
                  price: float = None, conviction: str = None,
                  detail: dict = None, signal_date=None):
    """Record one signal. Safe to call repeatedly — one row per
    (date, ticker, source); re-running a day's snapshot updates in place
    rather than duplicating."""
    import json
    signal_date = signal_date or date.today()
    conn = _connect_with_retry()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signal_journal
                    (signal_date, ticker, market, source, signal, conviction,
                     price_at_signal, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (signal_date, ticker, source) DO UPDATE SET
                    signal = EXCLUDED.signal,
                    conviction = EXCLUDED.conviction,
                    price_at_signal = EXCLUDED.price_at_signal,
                    detail = EXCLUDED.detail
            """, (signal_date, ticker, market, source, signal, conviction,
                  price, json.dumps(detail or {})))
        conn.commit()
    finally:
        conn.close()


def snapshot_quality_signals(limit: int = None):
    """Record today's Quality grades for every company in the store.
    Quality is deterministic from stored statements, so this can be
    computed in bulk without hitting any external API."""
    conn = _connect_with_retry()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker, market FROM companies ORDER BY ticker")
            companies = cur.fetchall()
    finally:
        conn.close()
    if limit:
        companies = companies[:limit]

    # Last stored price per ticker — a Quality grade does not depend on
    # price, so fetching 1,000 live quotes just to stamp one on the record
    # would be ~30 minutes of API calls and rate-limit failures for nothing.
    conn = _connect_with_retry()
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '120s'")
            cur.execute("""
                SELECT DISTINCT ON (ticker) ticker, price
                FROM stock_signatures ORDER BY ticker, date DESC
            """)
            price_map = dict(cur.fetchall())
    finally:
        conn.close()

    print(f"Recording Quality signals for {len(companies)} companies "
          f"(store-only, no external calls)...")
    recorded, failed = 0, 0
    for i, (ticker, market) in enumerate(companies, 1):
        try:
            clean = ticker.replace(".NS", "")
            from data_store import get_from_store
            stored = get_from_store(clean, market)
            if not stored:
                failed += 1
                continue
            info, inc, bal, cf = stored[0], stored[1], stored[2], stored[3]
            grade, score = _quick_quality(inc, bal, cf, info)
            if grade:
                record_signal(ticker, market, "quality", grade,
                              price=price_map.get(ticker),
                              detail={"score": score})
                recorded += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        if i % 100 == 0:
            print(f"  {i}/{len(companies)} ({recorded} recorded, {failed} skipped)")
    print(f"✅ Quality snapshot: {recorded} recorded, {failed} skipped.")


# ── DCF snapshot ─────────────────────────────────────────────────────────────
# Records what the DCF said, on the day it said it. Two reasons this exists:
#
# 1. Until now the journal only tracked Quality, so the scorecard could never
#    answer the question that actually matters — does the valuation model
#    work? A DCF that is never recorded can never be wrong, which is a
#    comfortable place to be and a useless one.
#
# 2. The shortlist needs a valuation filter, and a filter needs stored values.
#
# It calls the DEPLOYED endpoint rather than recomputing. A second copy of the
# DCF in this file would drift from the real one within a month, and then the
# track record would be scoring a model the app does not actually run.

API_BASE = os.environ.get("MINITRADEIQ_API",
                          "https://minicapitaliq-dg.onrender.com").rstrip("/")


def _dcf_signal(upside):
    if upside is None:
        return None
    if upside >= 40:  return "Strong Buy"
    if upside >= 15:  return "Buy"
    if upside <= -40: return "Strong Sell"
    if upside <= -15: return "Sell"
    return "Hold"


def snapshot_dcf_signals(limit: int = None, market: str = None,
                         min_quality: int = 50, pause: float = 1.5):
    """Run the live DCF across the universe and record each verdict.

    Ordered by quality score so that a truncated run covers the companies
    most likely to matter. Lenders and insurers are refused by the validation
    gate and counted as skipped, not as failures — that refusal is correct.
    """
    import httpx

    conn = _connect_with_retry()
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '120s'")
            q = """
                WITH qual AS (
                  SELECT DISTINCT ON (ticker) ticker,
                         (detail->>'score')::float AS score
                  FROM signal_journal WHERE source='quality'
                  ORDER BY ticker, signal_date DESC
                )
                SELECT c.ticker, c.market, COALESCE(q.score, 0) AS score
                FROM companies c
                LEFT JOIN qual q ON q.ticker = c.ticker
                WHERE COALESCE(q.score, 0) >= %s
            """
            params = [min_quality]
            if market:
                q += " AND c.market = %s"
                params.append(market)
            q += " ORDER BY score DESC"
            cur.execute(q, params)
            universe = cur.fetchall()
    finally:
        conn.close()

    if limit:
        universe = universe[:limit]
    if not universe:
        print(f"No companies with a quality score >= {min_quality}. "
              "Run `python signal_journal.py snapshot` first.")
        return

    print(f"Running DCF for {len(universe)} companies against {API_BASE}")
    print("(this calls the deployed model, so it is the same numbers the app "
          "shows — expect roughly "
          f"{len(universe) * (pause + 2) / 60:.0f} minutes)\n")

    recorded = skipped = failed = 0
    reasons = {}                      # why each company was not recorded
    with httpx.Client(timeout=90.0) as client:
        for i, (tkr, mkt, score) in enumerate(universe, 1):
            short = tkr.replace(".NS", "")
            try:
                r = client.get(f"{API_BASE}/dcf",
                               params={"ticker": short, "market": mkt})
                if r.status_code != 200:
                    failed += 1
                    reasons.setdefault(f"HTTP {r.status_code}", []).append(short)
                    continue
                d = r.json()

                # A refusal is a correct answer, not an error: the validation
                # gate declines lenders and insurers because a cash-flow DCF
                # genuinely cannot value them. Those go to the RIM model.
                if d.get("error"):
                    skipped += 1
                    err = str(d["error"])
                    low = err.lower()
                    key = ("lender / insurer (use Residual Income)"
                           if "bank" in low or "insur" in low or "lender" in low
                           else err[:90])
                    reasons.setdefault(key, []).append(short)
                    continue

                upside = d.get("upside_downside_pct")
                sig = _dcf_signal(upside)
                if sig is None:
                    skipped += 1
                    reasons.setdefault("no upside figure returned", []).append(short)
                    continue

                record_signal(
                    tkr, mkt, "dcf", sig,
                    price=d.get("current_price"),
                    detail={"upside_pct":  round(float(upside), 2),
                            "intrinsic":   d.get("intrinsic_value_per_share"),
                            "wacc":        d.get("wacc"),
                            "dcf_build":   d.get("dcf_build"),
                            "quality":     score})
                recorded += 1
            except Exception as e:
                failed += 1
                reasons.setdefault(f"request error: {str(e)[:60]}", []).append(short)
            time.sleep(pause)
            if i % 25 == 0:
                print(f"  {i}/{len(universe)}  "
                      f"({recorded} recorded, {skipped} not applicable, "
                      f"{failed} failed)")

    print(f"\n✅ DCF snapshot: {recorded} recorded, {skipped} not recorded, "
          f"{failed} failed.")
    for why, names in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        print(f"   {len(names):>3} × {why}")
        print(f"         {', '.join(names[:12])}{' …' if len(names) > 12 else ''}")
    if recorded:
        print("   These are now scoreable — in 90 days "
              "`python signal_journal.py score` will tell you whether the "
              "DCF's Buy calls actually beat the market.")


def _quick_quality(inc, bal, cf, info):
    """Condensed quality score — mirrors /quality's components closely
    enough for trend tracking. Returns (grade, score) or (None, None)."""
    def series(df, *kw):
        if df is None or df.empty:
            return {}
        for idx in df.index:
            if all(k.lower() in idx.lower() for k in kw):
                out = {}
                for col in df.columns[:6]:
                    try:
                        v = float(df.loc[idx, col])
                        if v == v:
                            out[str(col)] = v
                    except Exception:
                        pass
                return out
        return {}

    rev = series(inc, "total revenue")
    ni  = series(inc, "net income")
    eq  = series(bal, "stockholders equity") or series(bal, "total equity")
    ocf = series(cf, "operating cash flow")
    if len(rev) < 2:
        return None, None

    years = sorted(set(ni) & set(eq), reverse=True)
    roes = [ni[y] / eq[y] for y in years if eq.get(y)]
    conv_years = sorted(set(ocf) & set(ni), reverse=True)
    convs = [ocf[y] / ni[y] for y in conv_years if ni.get(y, 0) > 0]

    score = 0
    if roes:
        avg_roe = sum(roes) / len(roes)
        score += min(max(avg_roe, 0) / 0.15, 1.0) * 40
        score += (sum(1 for r in roes if r >= 0.12) / len(roes)) * 20
    if convs:
        score += min(max(sum(convs) / len(convs), 0) / 0.9, 1.0) * 25
    rev_desc = [rev[y] for y in sorted(rev, reverse=True)]
    if len(rev_desc) >= 3 and rev_desc[-1] > 0:
        cagr = (rev_desc[0] / rev_desc[-1]) ** (1 / (len(rev_desc) - 1)) - 1
        score += min(max(cagr, 0) / 0.12, 1.0) * 15
    score = round(score)
    grade = ("A" if score >= 80 else "B" if score >= 65 else
             "C" if score >= 50 else "D" if score >= 35 else "F")
    return grade, score


def score_journal(min_age_days: int = 30, source: str = None):
    """How have recorded signals actually performed? Excess return vs the
    equal-weight market, same methodology as the base-rate engine."""
    cutoff = date.today() - timedelta(days=min_age_days)
    conn = _connect_with_retry()
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '120s'")
            q = """SELECT signal_date, ticker, market, source, signal,
                          price_at_signal,
                          (detail->>'upside_pct')::float AS upside
                   FROM signal_journal WHERE signal_date <= %s"""
            params = [cutoff]
            if source:
                q += " AND source = %s"
                params.append(source)
            cur.execute(q, params)
            sigs = cur.fetchall()

            if not sigs:
                print(f"No signals older than {min_age_days} days recorded yet.")
                print("The journal scores itself only once signals have had "
                      "time to play out — check back later.")
                return

            cur.execute("""SELECT ticker, market, date, price
                           FROM stock_signatures WHERE price > 0""")
            prices = cur.fetchall()
    finally:
        conn.close()

    pdf = pd.DataFrame(prices, columns=["ticker", "market", "date", "price"])
    pdf["date"] = pd.to_datetime(pdf["date"])

    # ── Equal-weight benchmark ──────────────────────────────────────────────
    # Built from the MEAN OF PER-STOCK RETURNS, not from the change in the
    # average price. The latter looks equivalent and is not: when a new ticker
    # enters the universe, the average price jumps, and that jump is counted
    # as a market return. That is what produced a +4974% index in the base
    # rate engine. A stock must be present on BOTH dates to contribute a
    # return, which pct_change() on a pivot gives us for free (missing dates
    # become NaN and are skipped by the row mean).
    bmaps = {}
    for m, g in pdf.groupby("market"):
        wide = g.pivot_table(index="date", columns="ticker",
                             values="price", aggfunc="last").sort_index()
        rets = wide.pct_change()
        eq = rets.mean(axis=1, skipna=True).fillna(0.0)
        bmaps[m] = dict(zip(wide.index, (1 + eq).cumprod()))

    sdf = pd.DataFrame(sigs, columns=["signal_date", "ticker", "market",
                                      "source", "signal", "price_at_signal",
                                      "upside"])
    # Extreme DCF upsides get their own row. Averaged in with ordinary Strong
    # Buys, the question "are +150% DCFs right or broken?" could never be
    # answered — which is the whole reason they are being kept.
    extreme = (sdf["source"] == "dcf") & (sdf["upside"] > 100)
    sdf.loc[extreme, "signal"] = "Strong Buy >100%"
    sdf["signal_date"] = pd.to_datetime(sdf["signal_date"])

    results = {}
    for _, r in sdf.iterrows():
        series = pdf[pdf["ticker"] == r["ticker"]].sort_values("date")
        if series.empty:
            continue
        after = series[series["date"] >= r["signal_date"]]
        if len(after) < 2:
            continue
        p0 = after["price"].iloc[0]
        p1 = after["price"].iloc[-1]
        d0, d1 = after["date"].iloc[0], after["date"].iloc[-1]
        bmap = bmaps.get(r["market"], {})
        b0, b1 = bmap.get(d0), bmap.get(d1)
        if not p0 or p0 <= 0 or not b0 or not b1 or b0 <= 0:
            continue
        excess = (p1 / p0 - 1) - (b1 / b0 - 1)
        key = (r["source"], r["signal"])
        results.setdefault(key, []).append(excess)

    print(f"\n{'='*70}")
    print(f"SIGNAL JOURNAL SCORECARD  (signals at least {min_age_days} days old)")
    print(f"{'='*70}")
    if not results:
        print("  No scoreable signals yet.")
        return
    print(f"{'source':<14}{'signal':<16}{'n':>5}{'median excess':>16}{'beat mkt':>11}")
    print("-" * 70)
    for (src, sig), vals in sorted(results.items()):
        a = np.array(vals)
        n = len(a)
        marker = "" if n >= 20 else "  ← small sample"
        print(f"{src:<14}{sig:<16}{n:>5}"
              f"{np.median(a)*100:>15.1f}%"
              f"{(a > 0).mean()*100:>10.0f}%{marker}")
    print("\n  Excess = return vs equal-weight market over the same period.")
    print("  Samples under 20 are indicative only, not conclusive.")
    print("  This scorecard is the honest record — it is not adjusted or")
    print("  filtered to look favourable.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "score"
    if cmd == "init":
        init_table()
    elif cmd == "snapshot":
        init_table()
        lim = None
        if "--limit" in sys.argv:
            lim = int(sys.argv[sys.argv.index("--limit") + 1])
        snapshot_quality_signals(limit=lim)
    elif cmd == "score":
        days = 30
        if "--days" in sys.argv:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        src = None
        if "--source" in sys.argv:
            src = sys.argv[sys.argv.index("--source") + 1]
        score_journal(min_age_days=days, source=src)
    elif cmd in ("dcf-snapshot", "dcf"):
        init_table()
        a = sys.argv
        snapshot_dcf_signals(
            limit=int(a[a.index("--limit") + 1]) if "--limit" in a else None,
            market=a[a.index("--market") + 1] if "--market" in a else None,
            min_quality=int(a[a.index("--min-quality") + 1])
                        if "--min-quality" in a else 50,
            pause=float(a[a.index("--pause") + 1]) if "--pause" in a else 1.5)
    else:
        print("Usage: python signal_journal.py "
              "[init|snapshot|dcf-snapshot|score]")
