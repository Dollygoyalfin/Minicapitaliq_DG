"""
MiniTradeIQ — IPO Post-Listing Tracker
=======================================
What happened to each IPO after it listed, measured three ways at 3 months,
6 months and 1 year:

  vs issue price    — what an ALLOTTEE earned (includes the listing pop)
  vs listing close  — what someone who BOUGHT ON LISTING DAY earned
  vs the market     — the second figure minus an equal-weight market index
                      over the same dates; the only one that says whether
                      the IPO was worth choosing over simply owning the market

Then the cohort view: across all tracked IPOs, how often each horizon beat
the market, and whether a big listing-day pop predicts anything afterwards.

How it avoids the usual errors:
  - Listing date is the first day the stock actually traded (from price
    history), not an estimate from the issue calendar.
  - Splits and bonuses after listing are handled: the path from listing day
    uses ADJUSTED prices (Yahoo), the listing-day pop uses the RAW exchange
    close (NSE bhavcopy), and the two are chained. Using raw prices at both
    ends would show a 1:1 bonus as a 50% loss.
  - Horizons are counted in trading days (63 / 126 / 252), so a holiday-
    heavy quarter is not shorter than another.
  - Nothing is recommended. A strong first year is how an IPO becomes
    expensive, not evidence it will keep going.

Usage:
    python ipo_tracker.py init
    python ipo_tracker.py run               # mainboard IPOs of the last 3 years
    python ipo_tracker.py run --years 2 --sme
    python ipo_tracker.py report            # table + cohort statistics
    python ipo_tracker.py report --min-days 365
"""

import sys
import time
import json
from datetime import date, timedelta
from data_store import _conn

IPO_TRACK_BUILD = "2026-09-30 (issue / listing / market, split-safe)"

HORIZONS = {"3m": 63, "6m": 126, "1y": 252}      # trading days after listing


# ── Schema ───────────────────────────────────────────────────────────────────
def init_table():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ipo_performance (
                    symbol          TEXT PRIMARY KEY,
                    company         TEXT,
                    series          TEXT,
                    issue_price     DOUBLE PRECISION,
                    listing_date    DATE,
                    listing_close   DOUBLE PRECISION,   -- raw exchange close
                    listing_gain    DOUBLE PRECISION,   -- % vs issue, day one
                    days_listed     INTEGER,            -- trading days so far
                    metrics         JSONB,              -- per-horizon figures
                    now_vs_issue    DOUBLE PRECISION,
                    notes           TEXT,
                    updated_at      TIMESTAMP DEFAULT NOW()
                );
            """)
        conn.commit()
    finally:
        conn.close()
    print("ipo_performance table ready.")


# ── Which IPOs ───────────────────────────────────────────────────────────────
def gather_ipos(years: int = 3, include_sme: bool = False):
    """From NSE's issue feeds plus the app's own ipo_listings copy (which
    survives if NSE retires the feed)."""
    from events_calendar import _nse, _pick, _parse_date
    cutoff = date.today() - timedelta(days=365 * years + 30)
    ipos = {}

    def add(sym, comp, series, end, price):
        if not sym or not price or price <= 0:
            return
        if end and end < cutoff:
            return
        if not include_sme and "SME" in (series or "").upper():
            return
        cur = ipos.get(sym)
        if not cur or (not cur["issue_price"] and price):
            ipos[sym] = {"symbol": sym, "company": comp, "series": series,
                         "issue_end": end, "issue_price": price}

    def num(v):
        try:
            return float(str(v).split("-")[-1].replace(",", "")
                         .replace("Rs.", "").replace("₹", "").strip())
        except Exception:
            return None

    n_nse = 0
    for url in ("https://www.nseindia.com/api/public-past-issues",
                "https://www.nseindia.com/api/ipo-current-issue"):
        for r in _nse(url):
            n_nse += 1
            add((_pick(r, "symbol") or "").upper(),
                _pick(r, "companyName", "company", "issueName"),
                (_pick(r, "series", "securityType") or "").upper(),
                _parse_date(_pick(r, "issueEndDate", "ipoEndDate",
                                  "bidEndDate", "endDate")),
                num(_pick(r, "issuePrice", "priceBand", "maxPrice")))
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute("""SELECT symbol, company, series, issue_end, issue_price
                           FROM ipo_listings""")
            for sym, comp, ser, end, px in cur.fetchall():
                add(sym, comp, ser, end, px)
        conn.close()
    except Exception:
        pass
    print(f"IPOs: {len(ipos)} to track ({n_nse} rows from NSE feeds, plus the "
          f"app's own IPO list)")
    return list(ipos.values())


# ── Prices ───────────────────────────────────────────────────────────────────
def adjusted_history(symbol: str, start: date):
    """Split/bonus-adjusted daily closes from listing onward, or None."""
    import logging
    import yfinance as yf
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    for attempt in range(2):
        try:
            df = yf.download(f"{symbol}.NS", start=str(start), interval="1d",
                             auto_adjust=True, progress=False, threads=False)
            if df is not None and not df.empty:
                if hasattr(df.columns, "levels"):
                    df.columns = df.columns.get_level_values(0)
                s = df["Close"].dropna()
                s.index = [d.date() for d in s.index]
                return s if len(s) else None
        except Exception:
            pass
        time.sleep(4 * (attempt + 1))
    return None


_BHAV_CACHE = {}


def raw_close_on(symbol: str, d: date):
    """The actual traded close on d, from NSE's bhavcopy for that day."""
    if d not in _BHAV_CACHE:
        try:
            from price_refresh import fetch_bhavcopy
            res = fetch_bhavcopy(d)
            _BHAV_CACHE[d] = {} if "__error__" in res else res
        except Exception:
            _BHAV_CACHE[d] = {}
    hit = _BHAV_CACHE[d].get(symbol.upper())
    return hit[0] if hit else None


class Market:
    """Equal-weight index of the valuation universe from price_history,
    built from the mean of per-stock daily returns."""
    def __init__(self, start: date):
        import pandas as pd
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '180s'")
                cur.execute("""SELECT p.ticker, p.date, p.close
                               FROM price_history p JOIN companies c USING (ticker)
                               WHERE c.market='india' AND p.date >= %s
                                 AND p.close > 0""", (start,))
                rows = cur.fetchall()
        finally:
            conn.close()
        df = pd.DataFrame(rows, columns=["ticker", "date", "close"])
        wide = df.pivot_table(index="date", columns="ticker", values="close",
                              aggfunc="last").sort_index()
        eq = wide.pct_change().mean(axis=1, skipna=True).fillna(0.0)
        self.idx = (1 + eq).cumprod()
        self.dates = list(self.idx.index)

    def ret(self, d0, d1):
        import bisect
        if not self.dates:
            return None
        def at(d):                    # last index value on or before d
            i = bisect.bisect_right(self.dates, d) - 1
            return self.idx.iloc[i] if i >= 0 else None
        a, b = at(d0), at(d1)
        return (b / a - 1) if a and b else None


# ── Measurement (pure; tested without a network) ─────────────────────────────
def measure(ipo, hist, raw_listing_close, market_ret):
    """ipo: dict with issue_price; hist: adjusted closes (date-indexed,
    ascending, first row = listing day); market_ret(d0, d1) -> float|None.
    Returns (row dict, notes list)."""
    notes = []
    d0 = hist.index[0]
    adj0 = float(hist.iloc[0])
    issue = float(ipo["issue_price"])
    if raw_listing_close is None:
        raw_listing_close = adj0
        notes.append("listing close from adjusted prices (bhavcopy unavailable)")
    listing_gain = raw_listing_close / issue - 1

    metrics = {}
    for h, k in HORIZONS.items():
        if len(hist) <= k:
            continue
        dh = hist.index[k]
        from_listing = float(hist.iloc[k]) / adj0 - 1
        vs_issue = (1 + listing_gain) * (1 + from_listing) - 1
        mkt = market_ret(d0, dh)
        metrics[h] = {
            "date": str(dh),
            "vs_issue_pct": round(vs_issue * 100, 1),
            "vs_listing_pct": round(from_listing * 100, 1),
            "market_pct": round(mkt * 100, 1) if mkt is not None else None,
            "excess_vs_market_pct": (round((from_listing - mkt) * 100, 1)
                                     if mkt is not None else None),
        }
    now_from_listing = float(hist.iloc[-1]) / adj0 - 1
    now_vs_issue = (1 + listing_gain) * (1 + now_from_listing) - 1
    return {
        "symbol": ipo["symbol"], "company": ipo.get("company"),
        "series": ipo.get("series"), "issue_price": issue,
        "listing_date": d0, "listing_close": raw_listing_close,
        "listing_gain": round(listing_gain * 100, 1),
        "days_listed": len(hist) - 1,
        "metrics": metrics,
        "now_vs_issue": round(now_vs_issue * 100, 1),
    }, notes


# ── Run ──────────────────────────────────────────────────────────────────────
def run(years: int = 3, include_sme: bool = False, limit: int = None,
        pause: float = 1.2):
    init_table()
    ipos = gather_ipos(years, include_sme)
    if limit:
        ipos = ipos[:limit]
    if not ipos:
        print("Nothing to track.")
        return
    earliest = min((i["issue_end"] or date.today()) for i in ipos) - timedelta(days=10)
    print("Building market benchmark from price_history...")
    mkt = Market(earliest)

    done = skipped = 0
    reasons = {}
    for n, ipo in enumerate(ipos, 1):
        sym = ipo["symbol"]
        start = (ipo["issue_end"] or date.today() - timedelta(days=365 * years)) \
            - timedelta(days=3)
        hist = adjusted_history(sym, start)
        if hist is None or len(hist) < 2:
            skipped += 1
            reasons.setdefault("no price history yet (or not listed)", []).append(sym)
            time.sleep(pause)
            continue
        # A symbol reused by an older security would show history from before
        # this issue closed — that is not this IPO.
        if ipo["issue_end"] and hist.index[0] < ipo["issue_end"]:
            skipped += 1
            reasons.setdefault("history predates the issue (symbol reuse?)", []).append(sym)
            time.sleep(pause)
            continue
        raw = raw_close_on(sym, hist.index[0])
        row, notes = measure(ipo, hist, raw, mkt.ret)
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ipo_performance
                      (symbol, company, series, issue_price, listing_date,
                       listing_close, listing_gain, days_listed, metrics,
                       now_vs_issue, notes, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (symbol) DO UPDATE SET
                      company=EXCLUDED.company, series=EXCLUDED.series,
                      issue_price=EXCLUDED.issue_price,
                      listing_date=EXCLUDED.listing_date,
                      listing_close=EXCLUDED.listing_close,
                      listing_gain=EXCLUDED.listing_gain,
                      days_listed=EXCLUDED.days_listed,
                      metrics=EXCLUDED.metrics,
                      now_vs_issue=EXCLUDED.now_vs_issue,
                      notes=EXCLUDED.notes, updated_at=NOW()
                """, (row["symbol"], row["company"], row["series"],
                      row["issue_price"], row["listing_date"],
                      row["listing_close"], row["listing_gain"],
                      row["days_listed"], json.dumps(row["metrics"]),
                      row["now_vs_issue"], "; ".join(notes) or None))
            conn.commit()
        finally:
            conn.close()
        done += 1
        if n % 20 == 0:
            print(f"  {n}/{len(ipos)}  ({done} measured, {skipped} skipped)")
        time.sleep(pause)

    print(f"\n✅ IPO tracker: {done} measured, {skipped} skipped.")
    for why, syms in reasons.items():
        print(f"   {len(syms)} × {why}: {', '.join(syms[:15])}"
              f"{' …' if len(syms) > 15 else ''}")


# ── Report ───────────────────────────────────────────────────────────────────
def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def cohort_stats(rows):
    """Pure. rows: list of dicts with listing_gain and metrics."""
    out = {}
    for h in HORIZONS:
        have = [r for r in rows if h in r["metrics"]]
        ex = [r["metrics"][h]["excess_vs_market_pct"] for r in have]
        ex = [x for x in ex if x is not None]
        out[h] = {
            "n": len(have),
            "median_vs_issue_pct": _median([r["metrics"][h]["vs_issue_pct"] for r in have]),
            "median_vs_listing_pct": _median([r["metrics"][h]["vs_listing_pct"] for r in have]),
            "median_excess_vs_market_pct": _median(ex),
            "beat_market_pct": round(sum(1 for x in ex if x > 0) / len(ex) * 100) if ex else None,
            "above_issue_pct": (round(sum(1 for r in have if r["metrics"][h]["vs_issue_pct"] > 0)
                                      / len(have) * 100) if have else None),
        }
    # Does a big day-one pop predict what comes next?
    buckets = (("listed below issue", -1e9, 0), ("0 to 20% pop", 0, 20),
               ("20 to 50% pop", 20, 50), ("over 50% pop", 50, 1e9))
    pop = {}
    for name, lo, hi in buckets:
        grp = [r for r in rows if r["listing_gain"] is not None
               and lo <= r["listing_gain"] < hi and "1y" in r["metrics"]]
        ex = [r["metrics"]["1y"]["excess_vs_market_pct"] for r in grp]
        ex = [x for x in ex if x is not None]
        pop[name] = {"n": len(grp),
                     "median_1y_after_listing_pct": _median(
                         [r["metrics"]["1y"]["vs_listing_pct"] for r in grp]),
                     "median_1y_excess_pct": _median(ex)}
    return out, pop


def report_data(min_days: int = 0, include_sme: bool = False):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT symbol, company, series, issue_price,
                                  listing_date, listing_gain, days_listed,
                                  metrics, now_vs_issue, notes
                           FROM ipo_performance
                           WHERE days_listed >= %s
                           ORDER BY listing_date DESC""", (min_days,))
            raw = cur.fetchall()
    finally:
        conn.close()
    rows = []
    for (sym, comp, ser, ip, ld, lg, dl, met, now, notes) in raw:
        if not include_sme and "SME" in (ser or "").upper():
            continue
        met = met if isinstance(met, dict) else json.loads(met or "{}")
        rows.append({"symbol": sym, "company": comp, "issue_price": ip,
                     "listing_date": str(ld), "listing_gain": lg,
                     "days_listed": dl, "metrics": met,
                     "now_vs_issue": now, "notes": notes})
    stats, pop = cohort_stats(rows)
    return {
        "ipo_track_build": IPO_TRACK_BUILD,
        "n_ipos": len(rows),
        "cohort": stats,
        "listing_pop_vs_next_year": pop,
        "ipos": rows,
        "how_to_read": [
            "vs issue = what an allottee earned, including the listing-day pop.",
            "vs listing = what you earned buying at the listing-day close — "
            "the realistic figure if you did not get an allotment.",
            "excess vs market = the vs-listing return minus an equal-weight "
            "index of ~500 Indian stocks over the same dates. Positive means "
            "the IPO beat simply owning the market.",
        ],
        "caveats": [
            "Past post-listing returns describe what happened; a strong first "
            "year is usually how a stock becomes expensive, not evidence it "
            "will continue.",
            "Samples are small and from one market regime.",
            "Companies that delisted or merged may be missing, which flatters "
            "the averages slightly (survivorship).",
        ],
    }


def print_report(min_days: int = 0):
    d = report_data(min_days)
    if not d["ipos"]:
        print("No IPOs measured yet. Run: python ipo_tracker.py run")
        return
    f = lambda v: "   —  " if v is None else f"{v:+6.1f}"
    print(f"\n{'='*92}\nIPO POST-LISTING PERFORMANCE  ({d['n_ipos']} mainboard IPOs)\n{'='*92}")
    print(f"{'symbol':<13}{'listed':<12}{'issue':>8}{'day 1':>8}"
          f"{'3m*':>8}{'6m*':>8}{'1y*':>8}{'1y vs mkt':>11}{'now*':>8}")
    for r in d["ipos"]:
        m = r["metrics"]
        g = lambda h, k: m.get(h, {}).get(k)
        print(f"{r['symbol'][:12]:<13}{r['listing_date']:<12}{r['issue_price']:>8,.0f}"
              f"{f(r['listing_gain']):>8}{f(g('3m','vs_issue_pct')):>8}"
              f"{f(g('6m','vs_issue_pct')):>8}{f(g('1y','vs_issue_pct')):>8}"
              f"{f(g('1y','excess_vs_market_pct')):>11}{f(r['now_vs_issue']):>8}")
    print("  * % vs issue price.  '1y vs mkt' = return from listing close minus the market.\n")
    print("COHORT")
    for h, s in d["cohort"].items():
        if not s["n"]:
            print(f"  {h:<3} not enough history yet")
            continue
        print(f"  {h:<3} n={s['n']:<4} median vs issue {f(s['median_vs_issue_pct'])}%  "
              f"vs listing {f(s['median_vs_listing_pct'])}%  "
              f"vs market {f(s['median_excess_vs_market_pct'])}%  "
              f"beat market {s['beat_market_pct']}%  above issue {s['above_issue_pct']}%")
    print("\nDOES A BIG LISTING POP PREDICT THE NEXT YEAR?")
    for name, s in d["listing_pop_vs_next_year"].items():
        print(f"  {name:<20} n={s['n']:<4} median 1y after listing "
              f"{f(s['median_1y_after_listing_pct'])}%   vs market "
              f"{f(s['median_1y_excess_pct'])}%")
    print("\n" + "\n".join("  - " + c for c in d["caveats"]))


if __name__ == "__main__":
    a = sys.argv
    cmd = a[1] if len(a) > 1 else "report"
    if cmd == "init":
        init_table()
    elif cmd == "run":
        run(years=int(a[a.index("--years") + 1]) if "--years" in a else 3,
            include_sme="--sme" in a,
            limit=int(a[a.index("--limit") + 1]) if "--limit" in a else None)
    elif cmd == "report":
        print_report(int(a[a.index("--min-days") + 1]) if "--min-days" in a else 0)
    else:
        print(__doc__)
