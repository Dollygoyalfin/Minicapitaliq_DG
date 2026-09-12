"""
MiniTradeIQ — US Corporate Events (8-K) Engine
================================================
The SEC counterpart to the NSE announcements engine. Same principle: the
high-signal events are DETERMINISTIC, not sentiment.

8-K filings carry standardised item numbers, so classification needs no
model at all — Item 4.01 IS an auditor change, Item 4.02 IS a statement that
previously issued financials should no longer be relied upon. Those are two
of the strongest warning signals a US filer can send, and they are published
in a machine-readable form.

Item numbers used here are from the SEC's own 8-K form definition, so this
maps codes to meaning rather than guessing from headline text.

Usage:
    python us_events_engine.py init
    python us_events_engine.py fetch --days 7
    python us_events_engine.py flags AAPL
"""

import sys
import time
import json
import httpx
from datetime import date, timedelta
from data_store import _conn

US_EVENTS_BUILD = "2026-07-27a (8-K item codes)"

SEC_HEADERS = {
    "User-Agent": "MiniTradeIQ research contact@minitradeiq.example",
    "Accept-Encoding": "gzip, deflate",
}

# ── 8-K item numbers → meaning and severity ──────────────────────────────────
# Deterministic: these are the SEC's own definitions, not keyword guesses.
ITEM_MAP = {
    "1.01": ("material_agreement",     "info",     "Entry into a material definitive agreement"),
    "1.02": ("agreement_terminated",   "watch",    "Termination of a material definitive agreement"),
    "1.03": ("bankruptcy",             "red_flag", "Bankruptcy or receivership"),
    "2.01": ("acquisition_disposal",   "info",     "Completion of acquisition or disposition of assets"),
    "2.02": ("results",                "info",     "Results of operations and financial condition"),
    "2.03": ("debt_obligation",        "watch",    "Creation of a material direct financial obligation"),
    "2.04": ("acceleration",           "red_flag", "Triggering event accelerating a financial obligation"),
    "2.05": ("restructuring_costs",    "watch",    "Costs associated with exit or disposal activities"),
    "2.06": ("impairment",             "red_flag", "Material impairment"),
    "3.01": ("delisting_notice",       "red_flag", "Notice of delisting or failure to satisfy a listing rule"),
    "4.01": ("auditor_change",         "red_flag", "Changes in registrant's certifying accountant"),
    "4.02": ("financials_unreliable",  "red_flag", "Non-reliance on previously issued financial statements"),
    "5.02": ("management_change",      "watch",    "Departure or election of directors or officers"),
    "5.03": ("bylaw_change",           "info",     "Amendments to articles or bylaws"),
    "7.01": ("reg_fd",                 "info",     "Regulation FD disclosure"),
    "8.01": ("other_events",           "info",     "Other events"),
}


def init_table():
    """Reuses the news_events table so both markets land in one place."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS news_events (
                    id           SERIAL PRIMARY KEY,
                    ticker       TEXT NOT NULL,
                    market       TEXT NOT NULL,
                    event_date   DATE NOT NULL,
                    source       TEXT NOT NULL,
                    headline     TEXT NOT NULL,
                    url          TEXT,
                    category     TEXT,
                    severity     TEXT,
                    sentiment    DOUBLE PRECISION,
                    sentiment_by TEXT,
                    created_at   TIMESTAMP DEFAULT NOW(),
                    UNIQUE (ticker, event_date, headline)
                );
            """)
        conn.commit()
    finally:
        conn.close()
    print("news_events table ready (shared with the India engine).")


def _cik_map() -> dict:
    """CIK -> ticker for companies we track, from the store."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT cik, ticker FROM companies
                           WHERE market = 'us' AND cik IS NOT NULL""")
            return {str(r[0]).lstrip("0"): r[1] for r in cur.fetchall()}
    finally:
        conn.close()


def fetch_us_events(days: int = 7, sleep: float = 0.15):
    """Walk each tracked company's recent filings and keep the 8-Ks.

    The SEC's per-company submissions feed is used rather than the daily
    index: it returns the item numbers directly, which is what makes
    classification deterministic.
    """
    init_table()
    ciks = _cik_map()
    if not ciks:
        print("No US companies with a CIK in the store. Run backfill_us first.")
        return 0

    cutoff = date.today() - timedelta(days=days)
    print(f"Scanning 8-K filings since {cutoff} for {len(ciks)} companies...")

    stored, scanned, failed = 0, 0, 0
    payload = []

    for cik, ticker in ciks.items():
        try:
            url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
            with httpx.Client(timeout=30.0, headers=SEC_HEADERS) as client:
                resp = client.get(url)
            if resp.status_code != 200:
                failed += 1
                continue
            recent = resp.json().get("filings", {}).get("recent", {})
            forms  = recent.get("form", [])
            dates  = recent.get("filingDate", [])
            items  = recent.get("items", [])
            accs   = recent.get("accessionNumber", [])

            for i, form in enumerate(forms):
                if not str(form).startswith("8-K"):
                    continue
                fdate = dates[i] if i < len(dates) else None
                if not fdate or fdate < cutoff.isoformat():
                    continue
                raw_items = (items[i] if i < len(items) else "") or ""
                codes = [x.strip() for x in raw_items.split(",") if x.strip()]
                if not codes:
                    codes = ["8.01"]

                # A filing can carry several items; keep the most serious.
                best = None
                for code in codes:
                    key = code.split(" ")[0]
                    entry = ITEM_MAP.get(key)
                    if not entry:
                        continue
                    rank = {"red_flag": 0, "watch": 1, "info": 2}[entry[1]]
                    if best is None or rank < best[0]:
                        best = (rank, key, entry)
                if best is None:
                    continue
                _, code, (cat, sev, desc) = best

                acc = (accs[i] if i < len(accs) else "").replace("-", "")
                link = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}"
                        if acc else None)
                payload.append((ticker, "us", fdate, "sec_8k",
                                f"8-K Item {code}: {desc}", link, cat, sev))
            scanned += 1
        except Exception:
            failed += 1
        time.sleep(sleep)
        if scanned % 100 == 0:
            print(f"  {scanned}/{len(ciks)} companies scanned, {len(payload)} events found")

    if payload:
        from psycopg2.extras import execute_values
        BATCH = 500
        for i in range(0, len(payload), BATCH):
            chunk = payload[i:i + BATCH]
            for attempt in range(3):
                conn = None
                try:
                    conn = _conn()
                    with conn.cursor() as cur:
                        execute_values(cur, """
                            INSERT INTO news_events
                                (ticker, market, event_date, source, headline,
                                 url, category, severity)
                            VALUES %s
                            ON CONFLICT (ticker, event_date, headline) DO NOTHING
                        """, chunk, page_size=250)
                    conn.commit()
                    stored += len(chunk)
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"  batch failed: {str(e)[:80]}")
                    else:
                        time.sleep(3 * (attempt + 1))
                finally:
                    if conn:
                        try: conn.close()
                        except Exception: pass

    print(f"✅ {stored} 8-K events stored ({failed} companies unavailable)")
    return stored


def show_flags(ticker: str, days: int = 180):
    raw = ticker.upper()
    cutoff = date.today() - timedelta(days=days)
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT event_date, category, severity, headline
                           FROM news_events
                           WHERE ticker = %s AND market = 'us' AND event_date >= %s
                           ORDER BY
                             CASE severity WHEN 'red_flag' THEN 0 WHEN 'watch' THEN 1
                                           ELSE 2 END,
                             event_date DESC LIMIT 40""", (raw, cutoff))
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        print(f"No 8-K filings recorded for {raw} in the last {days} days.")
        return
    icons = {"red_flag": "🔴", "watch": "🟡", "info": "  "}
    print(f"\n{raw} — 8-K events, last {days} days\n" + "=" * 74)
    for d, cat, sev, head in rows:
        print(f"{icons.get(sev,'  ')} {d}  {cat:<22} {head[:70]}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    if cmd == "init":
        init_table()
    elif cmd == "fetch":
        d = 7
        if "--days" in sys.argv:
            d = int(sys.argv[sys.argv.index("--days") + 1])
        fetch_us_events(days=d)
    elif cmd == "flags":
        if len(sys.argv) < 3:
            print("Usage: python us_events_engine.py flags TICKER")
        else:
            show_flags(sys.argv[2])
    else:
        print("Usage: python us_events_engine.py [init|fetch|flags TICKER]")
