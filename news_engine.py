"""
MiniTradeIQ — News & Corporate Events Engine (Phase C)
=======================================================
Two distinct capabilities, deliberately separated because their reliability
is completely different:

1. EVENT DETECTION (high signal, deterministic)
   NSE publishes every corporate announcement — auditor resignations,
   promoter pledge creation/release, regulatory actions, order wins, rating
   changes. These are exactly the Munger-style red flags we listed as "not
   yet tracked", they are detectable by rule (no model guesswork), and they
   diffuse slowly enough to matter. This is the part worth trusting.

2. HEADLINE SENTIMENT (low signal, use with caution)
   Generic news sentiment on a large cap is priced in within seconds. It is
   included for texture and for TESTING (once enough history accumulates,
   the base-rate engine can measure whether it predicts anything at all),
   NOT as a trading signal. Scored via Groq rather than FinBERT because
   Render's 512MB tier cannot hold a 440MB model.

Bulk-fetches ALL announcements for a date range in ONE call rather than
per-company, so a nightly run costs a handful of requests, not a thousand.

Usage:
    python news_engine.py init
    python news_engine.py fetch --days 7      # India announcements
    python news_engine.py sentiment --limit 50 # optional LLM scoring
    python news_engine.py flags RELIANCE       # red flags for one stock
"""

import os
import re
import sys
import json
import time
from datetime import date, timedelta
from data_store import _conn

NEWS_BUILD = "2026-07-27a (promoter shareholding + pledging)"


# ── Rule-based classification ────────────────────────────────────────────────
# Ordered: first match wins, so put the high-signal patterns first.
EVENT_RULES = [
    # (category, severity, [regex patterns])
    ("auditor_change", "red_flag", [
        r"resignation.*auditor", r"auditor.*resign", r"change.*statutory auditor",
        r"casual vacancy.*auditor",
    ]),
    ("pledge_created", "red_flag", [
        r"creation of (encumbrance|pledge)", r"pledge.*creat", r"invocation of pledge",
    ]),
    ("pledge_released", "positive", [
        r"release of (encumbrance|pledge)", r"revocation of (encumbrance|pledge)",
    ]),
    ("regulatory_action", "red_flag", [
        r"show cause notice", r"\bsebi\b.*(order|penalty|notice)", r"adjudication",
        r"penalty.*imposed", r"prosecution", r"\bnclt\b", r"insolvency",
    ]),
    ("management_exit", "watch", [
        r"resignation.*(managing director|chief executive|chief financial|\bceo\b|\bcfo\b)",
        r"(managing director|chief financial officer|\bcfo\b).*resign",
    ]),
    ("rating_downgrade", "red_flag", [
        r"(credit )?rating.*(downgrade|revised downward)", r"downgrade.*rating",
    ]),
    ("rating_upgrade", "positive", [
        r"(credit )?rating.*(upgrade|revised upward)", r"upgrade.*rating",
    ]),
    ("order_win", "positive", [
        r"order (win|received|bagged)", r"letter of (award|intent)", r"\bloa\b",
        r"work order", r"contract (award|win|secured)", r"bags order",
    ]),
    ("capital_raise", "info", [
        r"\bqip\b", r"preferential (issue|allotment)", r"fund rais", r"rights issue",
        r"\bfccb\b", r"debenture",
    ]),
    ("corporate_action", "info", [
        r"amalgamation", r"demerger", r"scheme of arrangement", r"acquisition",
        r"buyback", r"\bmerger\b",
    ]),
    ("dividend", "info", [r"dividend"]),
    ("results", "info", [
        r"financial results", r"quarterly results", r"audited results",
    ]),
    ("board_meeting", "info", [r"board meeting"]),
]

COMPILED = [(cat, sev, [re.compile(p, re.I) for p in pats])
            for cat, sev, pats in EVENT_RULES]


def _parse_nse_date(raw):
    """NSE returns '23-Aug-2026 18:30:00'. Truncating to 10 chars yields
    '23-Aug-202' — which Postgres reads as year 202 AD, silently placing every
    event ~1800 years in the past and hiding it from any recent-window query.
    Parse the real formats instead."""
    from datetime import datetime
    s = (raw or "").strip()
    if not s:
        return str(date.today())
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:len(fmt) + 4], fmt).date().isoformat()
        except ValueError:
            continue
    try:                                  # last resort: leading ISO date
        return datetime.strptime(s[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return str(date.today())


def classify(text: str):
    """Returns (category, severity). Deterministic — no model involved."""
    t = (text or "").lower()
    for cat, sev, pats in COMPILED:
        for p in pats:
            if p.search(t):
                return cat, sev
    return "other", "neutral"


def init_table():
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
                CREATE INDEX IF NOT EXISTS idx_news_ticker ON news_events(ticker);
                CREATE INDEX IF NOT EXISTS idx_news_date ON news_events(event_date);
                CREATE INDEX IF NOT EXISTS idx_news_sev ON news_events(severity);
            """)
        conn.commit()
    finally:
        conn.close()
    print("news_events table ready.")


def fetch_india_announcements(days: int = 7):
    """ONE bulk call covers every listed company for the window — not one
    call per company."""
    from india_data_pipeline import _nse_get_json

    to_d   = date.today()
    from_d = to_d - timedelta(days=days)
    url = ("https://www.nseindia.com/api/corporate-announcements"
           f"?index=equities&from_date={from_d.strftime('%d-%m-%Y')}"
           f"&to_date={to_d.strftime('%d-%m-%Y')}")
    print(f"Fetching NSE announcements {from_d} → {to_d} (one bulk call)...")
    try:
        data = _nse_get_json(url)
    except Exception as e:
        print(f"  NSE fetch failed: {e}")
        return 0
    rows = data if isinstance(data, list) else data.get("data", [])
    print(f"  {len(rows)} announcements returned")

    # only keep companies we actually track
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker FROM companies WHERE market = 'india'")
            known = {r[0].replace(".NS", "") for r in cur.fetchall()}
    finally:
        conn.close()

    # Build the full row list first, then write in BATCHES with a fresh
    # connection per batch. Holding one connection open across 22,000
    # single-row inserts gets it killed by the pooler partway through.
    from psycopg2.extras import execute_values

    payload, skipped = [], 0
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if sym not in known:
            skipped += 1
            continue
        subject = (r.get("desc") or r.get("subject") or "").strip()
        detail  = (r.get("attchmntText") or "").strip()
        headline = (subject + " — " + detail).strip(" —")[:500]
        if not headline:
            continue
        ev_date = _parse_nse_date(r.get("an_dt") or r.get("sort_date"))
        cat, sev = classify(headline)
        payload.append((sym + ".NS", "india", ev_date, "nse_announcement",
                        headline, r.get("attchmntFile"), cat, sev))

    # de-duplicate within the batch itself (NSE repeats the same filing across
    # revisions, which would otherwise trip the ON CONFLICT clause repeatedly)
    seen, deduped = set(), []
    for p in payload:
        key = (p[0], p[2], p[4])
        if key not in seen:
            seen.add(key)
            deduped.append(p)
    payload = deduped

    print(f"  {len(payload)} relevant announcements for tracked companies "
          f"({skipped} for untracked)")

    BATCH = 500
    stored = 0
    for i in range(0, len(payload), BATCH):
        chunk = payload[i:i + BATCH]
        for attempt in range(3):
            conn = None
            try:
                conn = _conn()                      # fresh connection per batch
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
                    print(f"  batch {i//BATCH + 1} failed after 3 tries: {e}")
                else:
                    time.sleep(3 * (attempt + 1))
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
        if (i // BATCH + 1) % 5 == 0:
            print(f"  {min(i+BATCH, len(payload))}/{len(payload)} written...")

    print(f"✅ {stored} announcements stored ({skipped} for untracked companies)")
    return stored


def init_shareholding_table():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shareholding (
                    ticker        TEXT,
                    quarter_end   DATE,
                    promoter_pct  DOUBLE PRECISION,
                    pledged_pct   DOUBLE PRECISION,
                    fii_pct       DOUBLE PRECISION,
                    dii_pct       DOUBLE PRECISION,
                    public_pct    DOUBLE PRECISION,
                    fetched_at    TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (ticker, quarter_end)
                );
                CREATE INDEX IF NOT EXISTS idx_sh_ticker ON shareholding(ticker);
            """)
        conn.commit()
    finally:
        conn.close()
    print("shareholding table ready.")


def fetch_shareholding(limit: int = None, sleep: float = 1.2):
    """Quarterly shareholding pattern per company from NSE.

    Promoter pledging is one of the highest-signal governance red flags in
    Indian markets — promoters borrowing against their own stake means a
    price fall can force liquidation, which accelerates the fall. It is the
    check Jhunjhunwala was known for, and until now the app could only say
    'not yet tracked'.

    Unlike announcements there is no bulk endpoint, so this walks companies
    one at a time and is meant to run weekly, not nightly.
    """
    from india_data_pipeline import _nse_get_json, _q
    init_shareholding_table()

    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT ticker FROM companies WHERE market='india'
                           ORDER BY ticker""")
            tickers = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    if limit:
        tickers = tickers[:limit]

    stored, failed = 0, 0
    for i, tkr in enumerate(tickers, 1):
        sym = tkr.replace(".NS", "")
        try:
            data = _nse_get_json(
                f"https://www.nseindia.com/api/quote-equity?symbol={_q(sym)}"
                "&section=corp_info")
            sh = (data or {}).get("shareholdingPatterns", {}) or {}
            rows = sh.get("data") or sh.get("Shareholding Pattern") or {}
            if not rows:
                failed += 1
                continue

            for period, entries in list(rows.items())[:4]:
                vals = {}
                if isinstance(entries, list):
                    for e in entries:
                        for k, v in (e or {}).items():
                            kl = str(k).lower()
                            try:
                                fv = float(str(v).replace("%", "").strip())
                            except Exception:
                                continue
                            if "promoter" in kl and "pledge" not in kl:
                                vals["promoter"] = fv
                            elif "pledge" in kl or "encumber" in kl:
                                vals["pledged"] = fv
                            elif "foreign" in kl or kl.startswith("fii"):
                                vals["fii"] = fv
                            elif "domestic" in kl or kl.startswith("dii"):
                                vals["dii"] = fv
                            elif "public" in kl:
                                vals["public"] = fv
                if not vals:
                    continue

                q_end = _parse_nse_date(period)
                conn = _conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO shareholding
                                (ticker, quarter_end, promoter_pct, pledged_pct,
                                 fii_pct, dii_pct, public_pct)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (ticker, quarter_end) DO UPDATE SET
                                promoter_pct = EXCLUDED.promoter_pct,
                                pledged_pct  = EXCLUDED.pledged_pct,
                                fii_pct      = EXCLUDED.fii_pct,
                                dii_pct      = EXCLUDED.dii_pct,
                                public_pct   = EXCLUDED.public_pct
                        """, (tkr, q_end, vals.get("promoter"), vals.get("pledged"),
                              vals.get("fii"), vals.get("dii"), vals.get("public")))
                    conn.commit()
                    stored += 1
                finally:
                    conn.close()
        except Exception:
            failed += 1
        time.sleep(sleep)
        if i % 50 == 0:
            print(f"  {i}/{len(tickers)} ({stored} rows, {failed} unavailable)")
    print(f"✅ shareholding: {stored} rows stored, {failed} companies unavailable")


def score_sentiment(limit: int = 50):
    """OPTIONAL: LLM sentiment on unscored headlines. Deliberately secondary —
    the event category above is the reliable signal; this is texture, and
    material for later testing of whether sentiment predicts anything."""
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        print("GROQ_API_KEY not set — skipping sentiment scoring.")
        return
    import httpx

    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, ticker, headline FROM news_events
                           WHERE sentiment IS NULL AND category != 'board_meeting'
                           ORDER BY event_date DESC LIMIT %s""", (limit,))
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        print("Nothing to score.")
        return

    print(f"Scoring {len(rows)} headlines via Groq...")
    scored = 0
    for rid, ticker, headline in rows:
        prompt = (
            "Score the likely impact of this Indian corporate announcement on "
            "the company's share price. Respond ONLY with JSON: "
            '{"sentiment": <number between -1 and 1>, "reason": "<8 words max>"}\n'
            "-1 = clearly negative, 0 = neutral/procedural, 1 = clearly positive.\n"
            "Most routine filings are 0. Be conservative.\n\n"
            f"Company: {ticker}\nAnnouncement: {headline}")
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": "llama-3.1-8b-instant",
                          "max_tokens": 80, "temperature": 0.0,
                          "response_format": {"type": "json_object"},
                          "messages": [
                              {"role": "system", "content": "Respond with JSON only."},
                              {"role": "user", "content": prompt}]})
            if resp.status_code != 200:
                continue
            parsed = json.loads(resp.json()["choices"][0]["message"]["content"])
            s = float(parsed.get("sentiment", 0))
            s = max(-1.0, min(1.0, s))
            conn = _conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("""UPDATE news_events SET sentiment=%s,
                                   sentiment_by='groq-llama-3.1-8b' WHERE id=%s""",
                                (s, rid))
                conn.commit()
            finally:
                conn.close()
            scored += 1
        except Exception:
            pass
        time.sleep(0.3)
    print(f"✅ {scored} headlines scored.")


def show_flags(ticker: str, days: int = 180):
    """Red flags and notable events for one stock."""
    raw = ticker.upper()
    if not raw.endswith(".NS"):
        raw += ".NS"
    cutoff = date.today() - timedelta(days=days)
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT event_date, category, severity, headline, sentiment
                           FROM news_events
                           WHERE ticker=%s AND event_date >= %s
                           ORDER BY
                             CASE severity WHEN 'red_flag' THEN 0 WHEN 'watch' THEN 1
                                           WHEN 'positive' THEN 2 ELSE 3 END,
                             event_date DESC
                           LIMIT 40""", (raw, cutoff))
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        print(f"No announcements recorded for {raw} in the last {days} days.")
        return
    print(f"\n{raw} — corporate events, last {days} days")
    print("=" * 78)
    icons = {"red_flag": "🔴", "watch": "🟡", "positive": "🟢",
             "info": "  ", "neutral": "  "}
    for d, cat, sev, head, sent in rows:
        s = f" [{sent:+.1f}]" if sent is not None else ""
        print(f"{icons.get(sev,'  ')} {d}  {cat:<18}{s}")
        print(f"     {head[:110]}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    if cmd == "init":
        init_table()
    elif cmd == "fetch":
        init_table()
        d = 7
        if "--days" in sys.argv:
            d = int(sys.argv[sys.argv.index("--days") + 1])
        fetch_india_announcements(days=d)
    elif cmd == "shareholding":
        lim = None
        if "--limit" in sys.argv:
            lim = int(sys.argv[sys.argv.index("--limit") + 1])
        fetch_shareholding(limit=lim)
    elif cmd == "sentiment":
        lim = 50
        if "--limit" in sys.argv:
            lim = int(sys.argv[sys.argv.index("--limit") + 1])
        score_sentiment(limit=lim)
    elif cmd == "flags":
        if len(sys.argv) < 3:
            print("Usage: python news_engine.py flags TICKER")
        else:
            show_flags(sys.argv[2])
    else:
        print("Usage: python news_engine.py [init|fetch|sentiment|flags TICKER]")
