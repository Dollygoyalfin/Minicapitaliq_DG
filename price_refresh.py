"""
MiniTradeIQ — Daily Price Refresh
==================================
Every number the app shows about "now" — P&L, the shortlist, the paper
book's fills, market breadth — is only as fresh as price_history. This job
keeps it fresh, and says so loudly when it cannot.

Why it replaced the old top-up inside ingest.refresh_all():
  - It ran only AFTER up to 200 fundamentals refreshes, so any failure or
    timeout above it meant no prices that night.
  - It returned early, skipping prices, on nights when no company was due.
  - yfinance returns an EMPTY frame rather than raising when it is blocked,
    so the job printed "complete: 0 rows" and looked successful.
  - It fetched a fixed 7 days, so a gap longer than a week never healed.
  Result: prices silently froze on 3 Sep 2026 while everything downstream
  carried on computing from them.

What this does instead:
  India — NSE's daily bhavcopy: ONE file per trading day with the close of
          every listed security, ETFs included. One request per day rather
          than a thousand per-ticker calls, straight from the exchange.
          Falls back to yfinance only if NSE is unreachable.
  US    — yfinance, batched, from the last stored date (not a fixed window),
          so any gap heals on the next successful run.
  Both  — cover the valuation universe AND your holdings, so ETFs and new
          listings you own get priced even though they have no financials.
  Health — reports the latest date and coverage per market, and exits with
           an error when prices are stale so the GitHub Action turns RED
           instead of quietly succeeding.

Usage:
    python price_refresh.py            # refresh both markets, then health check
    python price_refresh.py india
    python price_refresh.py us
    python price_refresh.py check      # health check only
    python price_refresh.py repair TICKER   # re-download full adjusted history
"""

import io
import sys
import time
import zipfile
import csv
from datetime import date, timedelta
from data_store import _conn

PRICE_BUILD = "2026-09-27b (gentler yfinance + retries; coverage gate)"

MAX_BACKFILL_DAYS = 90      # never try to heal more than this in one run
STALE_AFTER_DAYS = 5        # calendar days; covers a weekend plus a holiday
MIN_COVERAGE_PCT = 90       # share of the universe that must have the latest
                            # close. A fresh date on 73% of stocks is not
                            # "current" — it is a quarter of the market missing.
KEEP_SERIES = ("EQ", "BE", "BZ", "SM", "ST")   # equity incl. ETFs, T2T, SME


# ── Universe ─────────────────────────────────────────────────────────────────
def universe(market: str):
    """Valuation universe plus anything you hold in that market."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker FROM companies WHERE market=%s", (market,))
            tickers = {r[0] for r in cur.fetchall()}
            try:
                cur.execute("SELECT ticker FROM holdings WHERE market=%s", (market,))
                tickers |= {r[0] for r in cur.fetchall()}
            except Exception:
                conn.rollback()        # holdings table may not exist yet
    finally:
        conn.close()
    return tickers


def last_stored_date(tickers):
    if not tickers:
        return None
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT MAX(date) FROM price_history
                           WHERE ticker = ANY(%s)""", (list(tickers),))
            return cur.fetchone()[0]
    finally:
        conn.close()


def _bulk_upsert(rows):
    """rows: [(ticker, date, close, volume)] — one round trip per 1,000."""
    if not rows:
        return 0
    from psycopg2.extras import execute_values
    conn = _conn()
    try:
        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO price_history (ticker, date, close, volume)
                VALUES %s
                ON CONFLICT (ticker, date) DO UPDATE
                SET close = EXCLUDED.close, volume = EXCLUDED.volume
            """, rows, page_size=1000)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def _weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


# ── India: NSE bhavcopy ──────────────────────────────────────────────────────
def _num(x):
    try:
        x = str(x).strip().replace(",", "")
        return float(x) if x not in ("", "-", "nan") else None
    except Exception:
        return None


def parse_bhavcopy(raw: bytes):
    """Pure function. Accepts either format NSE publishes:
      - UDiFF zip (since Jul 2024): TradDt, TckrSymb, SctySrs, ClsPric, TtlTradgVol
      - sec_bhavdata_full CSV:      SYMBOL, SERIES, DATE1, CLOSE_PRICE, TTL_TRD_QNTY
    Returns {symbol: (close, volume)} and the trade date, preferring the EQ
    series when a symbol trades in more than one."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            raw = z.read(name)
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [f.strip() for f in (reader.fieldnames or [])]
    f = set(reader.fieldnames)
    if {"TckrSymb", "ClsPric"} <= f:
        k_sym, k_ser, k_cls, k_vol, k_dt = ("TckrSymb", "SctySrs", "ClsPric",
                                           "TtlTradgVol", "TradDt")
    elif {"SYMBOL", "CLOSE_PRICE"} <= f:
        k_sym, k_ser, k_cls, k_vol, k_dt = ("SYMBOL", "SERIES", "CLOSE_PRICE",
                                           "TTL_TRD_QNTY", "DATE1")
    else:
        raise ValueError(f"Unrecognised bhavcopy columns: {sorted(f)[:8]}")

    out, trade_date = {}, None
    for row in reader:
        row = {k.strip() if k else k: (v.strip() if isinstance(v, str) else v)
               for k, v in row.items()}
        ser = (row.get(k_ser) or "").upper()
        if ser not in KEEP_SERIES:
            continue
        sym, cls = row.get(k_sym), _num(row.get(k_cls))
        if not sym or not cls or cls <= 0:
            continue
        if sym in out and ser != "EQ":
            continue                       # EQ wins over BE/BZ duplicates
        vol = _num(row.get(k_vol))
        out[sym.upper()] = (cls, int(vol) if vol is not None else None)
        if trade_date is None:
            trade_date = row.get(k_dt)
    return out, trade_date


def fetch_bhavcopy(d: date):
    """None on a holiday or when NSE is unreachable; parsed dict otherwise."""
    from india_data_pipeline import _nse_get
    urls = (
        f"https://nsearchives.nseindia.com/content/cm/"
        f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip",
        f"https://nsearchives.nseindia.com/products/content/"
        f"sec_bhavdata_full_{d:%d%m%Y}.csv",
    )
    last_err = None
    for u in urls:
        try:
            resp = _nse_get(u, retries=2)
            closes, _ = parse_bhavcopy(resp.content)
            if closes:
                return closes
        except Exception as e:
            last_err = str(e)[:100]
    return {"__error__": last_err}


def refresh_india():
    tickers = universe("india")
    if not tickers:
        print("India: no tickers in universe.")
        return 0
    last = last_stored_date(tickers)
    today = date.today()
    start = (last + timedelta(days=1)) if last else today - timedelta(days=7)
    start = max(start, today - timedelta(days=MAX_BACKFILL_DAYS))
    days = list(_weekdays(start, today))
    print(f"India: last stored close {last}; checking {len(days)} weekday(s) "
          f"from {start} via NSE bhavcopy")

    wanted = {t.replace(".NS", ""): t for t in tickers}
    total, got_days, errors = 0, 0, 0
    for d in days:
        res = fetch_bhavcopy(d)
        if "__error__" in res:
            errors += 1
            # 404 on a holiday is normal; only report the reason once
            if errors == 1:
                print(f"  {d}: no file ({res['__error__']})")
            continue
        rows = [(wanted[s], d, c, v) for s, (c, v) in res.items() if s in wanted]
        total += _bulk_upsert(rows)
        got_days += 1
        print(f"  {d}: {len(rows)} closes stored "
              f"({len(rows)/len(wanted)*100:.0f}% of universe)")
        time.sleep(1.0)

    # Every weekday failing is not a run of holidays — NSE is blocking us.
    if days and got_days == 0 and len(days) >= 2:
        print("  NSE bhavcopy unreachable for every day — falling back to yfinance.")
        total += refresh_yfinance_with_retry(sorted(tickers),
                                             start - timedelta(days=3), "India")
    print(f"India: {total:,} rows written.")
    return total


# ── yfinance (US, and India fallback) ────────────────────────────────────────
def _quiet_yfinance():
    """yfinance prints 'possibly delisted' for every throttled request — for
    AMZN and MSFT as readily as for a real delisting. It is noise that hides
    the real summary, so it is silenced and replaced with one honest count."""
    import logging
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def refresh_yfinance(tickers, start: date, batch: int = 25,
                     threads: bool = False, pause: float = 2.0):
    """Returns (rows_written, set_of_tickers_that_returned_data).

    Sequential by default. threads=True fires a whole batch at Yahoo at once,
    which is exactly what trips its rate limit — the first run lost 27% of
    the US universe that way."""
    import yfinance as yf
    _quiet_yfinance()
    total, empty_batches, got = 0, 0, set()
    nb = (len(tickers) - 1) // batch + 1 if tickers else 0
    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i + batch]
        try:
            data = yf.download(" ".join(chunk), start=str(start), interval="1d",
                               group_by="ticker", auto_adjust=True,
                               threads=threads, progress=False)
        except Exception as e:
            print(f"  batch {i//batch+1}/{nb}: failed ({str(e)[:80]})")
            empty_batches += 1
            time.sleep(pause * 2)
            continue
        rows = []
        for t in chunk:
            try:
                sub = data[t] if len(chunk) > 1 else data
                sub = sub.dropna(subset=["Close"])
                n0 = len(rows)
                for idx, r in sub.iterrows():
                    v = r.get("Volume")
                    rows.append((t, idx.date(), float(r["Close"]),
                                 int(v) if v == v else None))
                if len(rows) > n0:
                    got.add(t)
            except Exception:
                pass
        if not rows:
            empty_batches += 1           # blocked requests land here silently
        total += _bulk_upsert(rows)
        time.sleep(pause)
    if nb and empty_batches == nb and len(tickers) >= 20:
        print("  ⚠ yfinance returned NOTHING for every batch — it is being "
              "blocked or rate-limited, not 'up to date'.")
    return total, got


def refresh_yfinance_with_retry(tickers, start: date, label: str):
    """First pass gently, then up to two slower passes for whatever Yahoo
    throttled. Reports what is still missing by name."""
    total, got = refresh_yfinance(tickers, start)
    missing = [t for t in tickers if t not in got]
    for attempt, (b, p) in enumerate(((10, 4.0), (5, 8.0)), 1):
        if not missing:
            break
        print(f"  {label}: {len(missing)} ticker(s) throttled — retry {attempt} "
              f"(batches of {b}, {p:.0f}s apart)")
        time.sleep(15 * attempt)
        n, g = refresh_yfinance(missing, start, batch=b, pause=p)
        total += n
        missing = [t for t in missing if t not in g]
    if missing:
        shown = ", ".join(t.replace(".NS", "") for t in missing[:25])
        print(f"  {label}: still no data for {len(missing)} after retries: "
              f"{shown}{' …' if len(missing) > 25 else ''}")
    return total


def refresh_us():
    tickers = sorted(universe("us"))
    if not tickers:
        print("US: no tickers in universe.")
        return 0
    last = last_stored_date(tickers)
    today = date.today()
    start = (last - timedelta(days=3)) if last else today - timedelta(days=10)
    start = max(start, today - timedelta(days=MAX_BACKFILL_DAYS))
    print(f"US: last stored close {last}; downloading from {start} "
          f"for {len(tickers)} tickers")
    n = refresh_yfinance_with_retry(tickers, start, "US")
    print(f"US: {n:,} rows written.")
    return n


# ── Split / bonus guard ──────────────────────────────────────────────────────
# Bhavcopy closes are the actual traded prices, not adjusted for splits and
# bonuses. Stored history was adjusted when it was downloaded. So when a
# company does a 1:1 bonus after that, the stored series shows a fake -50%
# day — which would trip the paper book's stop rule and poison every
# momentum rank. Such jumps are detected here and the ticker's history is
# re-downloaded fully adjusted.
def detect_splits(days_back: int = 10, threshold: float = 0.40):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH x AS (
                  SELECT ticker, date, close,
                         LAG(close) OVER (PARTITION BY ticker ORDER BY date) AS prev
                  FROM price_history
                  WHERE date >= CURRENT_DATE - (%s || ' days')::interval
                )
                SELECT ticker, date, prev, close FROM x
                WHERE prev > 0 AND ABS(close / prev - 1) >= %s
            """, (days_back + 10, threshold))
            return cur.fetchall()
    finally:
        conn.close()


def repair_history(ticker: str, years: int = 5):
    import yfinance as yf
    sub = yf.download(ticker, period=f"{years}y", interval="1d",
                      auto_adjust=True, progress=False)
    if sub is None or sub.empty:
        return 0
    if hasattr(sub.columns, "levels"):          # yfinance multi-index quirk
        sub.columns = sub.columns.get_level_values(0)
    sub = sub.dropna(subset=["Close"])
    rows = [(ticker, idx.date(), float(r["Close"]),
             int(r["Volume"]) if r.get("Volume") == r.get("Volume") else None)
            for idx, r in sub.iterrows()]
    return _bulk_upsert(rows)


def split_guard():
    jumps = detect_splits()
    if not jumps:
        return
    print(f"\nPossible split/bonus on {len(jumps)} ticker(s) — re-adjusting history:")
    for t, d, prev, cls in jumps:
        try:
            n = repair_history(t)
            status = f"repaired ({n:,} rows)" if n else "COULD NOT REPAIR (yfinance empty)"
        except Exception as e:
            status = f"COULD NOT REPAIR ({str(e)[:60]})"
        print(f"  {t:<16} {d}  {prev:,.2f} → {cls:,.2f}  "
              f"({(cls/prev-1)*100:+.0f}%)  {status}")


# ── Health ───────────────────────────────────────────────────────────────────
def health():
    """Returns a list of problems; empty means healthy."""
    problems = []
    today = date.today()
    print("\n" + "=" * 60 + "\nPRICE HEALTH\n" + "=" * 60)
    for market in ("india", "us"):
        tickers = universe(market)
        if not tickers:
            continue
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT MAX(date) FROM price_history
                               WHERE ticker = ANY(%s)""", (list(tickers),))
                latest = cur.fetchone()[0]
                n_latest = 0
                if latest:
                    cur.execute("""SELECT COUNT(*) FROM price_history
                                   WHERE ticker = ANY(%s) AND date = %s""",
                                (list(tickers), latest))
                    n_latest = cur.fetchone()[0]
                missing = []
                try:
                    cur.execute("""SELECT h.ticker FROM holdings h
                                   WHERE h.market=%s AND NOT EXISTS (
                                     SELECT 1 FROM price_history p
                                     WHERE p.ticker=h.ticker
                                       AND p.date >= CURRENT_DATE - INTERVAL '10 days')""",
                                (market,))
                    missing = [r[0] for r in cur.fetchall()]
                except Exception:
                    conn.rollback()
        finally:
            conn.close()
        age = (today - latest).days if latest else None
        cov = n_latest / len(tickers) * 100 if tickers else 0
        fresh = latest is not None and age <= STALE_AFTER_DAYS
        covered = cov >= MIN_COVERAGE_PCT
        status = ("✅" if fresh and covered else
                  "❌ STALE" if not fresh else f"❌ PARTIAL (<{MIN_COVERAGE_PCT}%)")
        print(f"{market.upper():<6} latest close {latest}  "
              f"({age} days old)  coverage {cov:.0f}% of {len(tickers)}  {status}")
        if not fresh:
            problems.append(f"{market}: latest close {latest} is {age} days old")
        elif not covered:
            problems.append(f"{market}: only {cov:.0f}% of {len(tickers)} tickers "
                            f"have the {latest} close")
        if missing:
            print(f"       holdings with no recent price: "
                  f"{', '.join(m.replace('.NS','') for m in missing)}")
    return problems


if __name__ == "__main__":
    print(f"price_refresh {PRICE_BUILD}")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "repair" and len(sys.argv) > 2:
        t = sys.argv[2].upper()
        print(f"{t}: {repair_history(t):,} rows rewritten")
        sys.exit(0)
    if cmd in ("all", "india"):
        try:
            refresh_india()
        except Exception as e:
            print(f"India refresh failed: {e}")
    if cmd in ("all", "us"):
        try:
            refresh_us()
        except Exception as e:
            print(f"US refresh failed: {e}")
    if cmd in ("all", "india", "us"):
        try:
            split_guard()
        except Exception as e:
            print(f"Split guard failed: {e}")
    problems = health()
    if problems:
        print("\n❌ " + "\n❌ ".join(problems))
        print("Everything downstream — P&L, shortlist, paper book — is using "
              "stale or missing prices. Failing this step on purpose so it "
              "shows red.")
        sys.exit(1)
    print("\n✅ Prices are current.")
