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

NEWS_BUILD = "2026-07-27j (fraction scaling + NumberOfShares denominator)"


# ── Rule-based classification ────────────────────────────────────────────────
# Ordered: first match wins, so put the high-signal patterns first.
EVENT_RULES = [
    # (category, severity, [regex patterns])
    ("auditor_change", "red_flag", [
        r"resignation.*auditor", r"auditor.*resign", r"change.*statutory auditor",
        r"casual vacancy.*auditor",
    ]),
    ("pledge_created", "red_flag", [
        # Real NSE wording varies; "encumbrance" is the term used in SAST
        # filings, and invocation means a lender has actually sold the stock.
        r"creation of (encumbrance|pledge)", r"pledge.*creat",
        r"invocation of pledge", r"encumbrance.*creat", r"shares? pledged",
        r"pledge of (equity )?shares",
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

    # ── NSE subject-line prefixes ────────────────────────────────────────────
    # NSE stamps every announcement with its own subject category before the
    # em-dash. Matching that prefix is deterministic — the same reason the US
    # engine keys off 8-K item numbers instead of headline text. Without these
    # rules 82% of filings fell into "other".
    ("takeover_disclosure", "watch", [
        # SEBI SAST filings cover substantial acquisitions AND encumbrances.
        # Deliberately "watch", not "red_flag": the heading alone does not
        # tell us whether shares were pledged, bought or sold, and marking
        # every one red would make the red-flag count meaningless.
        r"disclosure under sebi takeover regulations",
        r"disclosure under regulation 29", r"regulation 31",
    ]),
    ("analyst_meet",       "info", [
        r"analysts?/institutional investor meet", r"con\.? call",
        r"earnings call", r"investor presentation",
    ]),
    ("order_win", "positive", [
        r"bagging/receiving of orders", r"receiving of orders/contracts",
    ]),
    ("monitoring_report",  "info", [r"monitoring agency report"]),
    ("trading_window",     "info", [r"trading window", r"closure of trading"]),
    ("general_update",     "info", [
        r"^general updates", r"^updates —", r"newspaper publication",
        r"press release", r"investor complaint",
    ]),
    ("compliance_filing",  "info", [
        r"reconciliation of share capital", r"certificate under regulation",
        r"compliance certificate", r"related party transaction",
        r"corporate governance report", r"shareholding pattern",
    ]),
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
                    -- A filing can DECLARE encumbrance while reporting no
                    -- usable percentage. Storing 0 for that case reads as
                    -- "nothing encumbered", which is the opposite of what the
                    -- company disclosed — so the flag is kept separately.
                    encumbrance_declared BOOLEAN,
                    fetched_at    TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (ticker, quarter_end)
                );
                ALTER TABLE shareholding
                    ADD COLUMN IF NOT EXISTS encumbrance_declared BOOLEAN;
                CREATE INDEX IF NOT EXISTS idx_sh_ticker ON shareholding(ticker);
            """)
        conn.commit()
    finally:
        conn.close()
    print("shareholding table ready.")


def _parse_shp_xbrl(xml_bytes, promoter_pct_hint=None):
    """Promoter encumbrance from a shareholding-pattern XBRL.

    Ground truth from Ashok Leyland's actual filing, checked against a known
    real-world figure rather than a synthetic test:

        EncumberedSharesHeldAsPercentageOfTotalNumberOfShares = 0.401
        (inside context ShareholdingOfPromoterAndPromoterGroup_ContextI)

    0.401 means 0.401%, NOT 40.1%. Two earlier versions of this parser both
    produced wrong answers on real filings:
      - treating any value <= 1.0 as "already a fraction, times 100" turned
        0.401% into 40.1%
      - deriving from NumberOfSharesEncumbered / NumberOfFullyPaidUpEquityShares
        used the wrong denominator (the promoter context ALSO contains
        NumberOfShares, which includes depository receipts, and the two
        counts differ enough to swing the answer from 0.4% to 51%)

    The correct approach: read the percentage tag directly from the EXACT
    promoter-group context, take the value at face value (the tag's own
    definition is "a percentage figure," i.e. already scaled 0-100, not a
    0-1 fraction) and do not derive it from counts at all — the counts in
    this context are not guaranteed to be the matching pair.

    The ONE context that matters is ShareholdingOfPromoterAndPromoterGroup_*
    specifically — not "Foreign", not "OtherForeignShareholders", not any
    sub-grouping context, which report different (irrelevant) percentages
    for the same tag name.
    """
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_bytes)

    def local(t):
        return t.split("}")[-1]

    PROMOTER_MEMBER = "shareholdingofpromoterandpromotergroupmember"

    # Find contexts whose dimension member is EXACTLY the promoter-group
    # member — not merely containing the word "promoter" (which also matches
    # "ShareholdingByCompaniesOrBodiesCorporateWhereGovernmentIsPromoter").
    promoter_ctx = set()
    for el in root.iter():
        if local(el.tag) != "context":
            continue
        cid = el.get("id")
        for ch in el.iter():
            if local(ch.tag) in ("explicitMember", "typedMember"):
                member = (ch.text or "").strip().lower()
                if member.endswith(PROMOTER_MEMBER) or member == PROMOTER_MEMBER:
                    promoter_ctx.add(cid)
    if not promoter_ctx:
        for el in root.iter():
            if local(el.tag) == "context" and "shareholdingofpromoterandpromotergroup" in (el.get("id") or "").lower():
                promoter_ctx.add(el.get("id"))

    flags = {}
    pct_vals = []
    counts = {}
    for el in root.iter():
        tag = local(el.tag)
        txt = (el.text or "").strip()
        if not txt:
            continue
        low = txt.lower()
        if low in ("true", "false"):
            flags[tag] = flags.get(tag, False) or (low == "true")
            continue
        if el.get("contextRef") not in promoter_ctx:
            continue
        if tag == "EncumberedSharesHeldAsPercentageOfTotalNumberOfShares":
            try:
                pct_vals.append(float(txt))
            except ValueError:
                pass
        elif tag in ("NumberOfSharesEncumbered", "NumberOfShares"):
            try:
                counts.setdefault(tag, []).append(float(txt))
            except ValueError:
                pass

    any_encumbered = any(flags.get(k) for k in (
        "WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged",
        "WhetherAnySharesHeldByPromotersAreEncumberedUnderPledgedForPromoterAndPromoterGroup",
        "WhetherAnySharesHeldByPromotersAreEncumberedUnderNonDisposalUndertaking",
        "WhetherAnySharesHeldByPromotersAreEncumberedUnderNonDisposalUndertakingForPromoterAndPromoterGroup",
        "WhetherAnySharesHeldByPromotersAreEncumberedOtherThanByWayOfPledgeOrNDU",
        "WhetherAnySharesHeldByPromotersAreEncumberedOtherThanByWayOfPledgeOrNDUForPromoterAndPromoterGroup",
    ))

    pct, basis = None, None
    if pct_vals:
        # The tag is a FRACTION of the promoter's total holding: Ashok
        # Leyland reports 0.401, and the publicly quoted figure is 40.1%.
        #
        # Verified against the filing's own counts:
        #     encumbered 1,203,500,000 / NumberOfShares 3,001,320,522 = 40.1%
        #
        # Note the denominator: NumberOfShares (3,001,320,522), NOT
        # NumberOfFullyPaidUpEquityShares (2,342,920,242). The difference is
        # 658,400,280 depository receipts the promoters also hold, and using
        # the smaller figure inflated the answer to 51.4%.
        raw = max(pct_vals)
        pct = raw * 100.0 if raw <= 1.0 else raw
        basis = "reported fraction from the exact promoter-group context"
        if not (0 <= pct <= 100):
            pct, basis = None, "reported value out of range — not used"
    if pct is None and any_encumbered:
        basis = "encumbrance declared, but no percentage found in the exact promoter context"
    if pct is None and not any_encumbered:
        pct, basis = 0.0, "no encumbrance declared"

    # Cross-check against the filing's own counts, using NumberOfShares as
    # the denominator (the promoter's TOTAL holding, including depository
    # receipts). Reported and derived should agree closely; if they do not,
    # say so rather than presenting one silently.
    derived = None
    enc = max(counts.get("NumberOfSharesEncumbered", []), default=None)
    tot = max(counts.get("NumberOfShares", []), default=None)
    if enc is not None and tot:
        derived = enc / tot * 100.0
        if pct is not None and abs(derived - pct) > 2:
            basis = (f"{basis}; counts imply {derived:.2f}% — figures disagree")
        elif pct is not None:
            basis = "reported fraction, confirmed by the filing's share counts"
        else:
            pct, basis = derived, "derived from share counts"

    return {
        "promoter_pct": None,      # taken from the API header, which is reliable
        "pledged_pct":  round(pct, 3) if pct is not None else None,
        "encumbrance_declared": any_encumbered,
        "basis": basis,
        "derived_from_counts": round(derived, 3) if derived is not None else None,
    }


def fetch_shareholding(limit: int = None, sleep: float = 1.0, quarters: int = 6):
    """Quarterly promoter holding AND encumbrance, parsed from NSE's
    shareholding-pattern XBRL.

    One call to share-holdings-master returns the full filing history with
    XBRL links; each XBRL is then parsed for the exact figures. XBRL files
    are immutable, so the disk cache makes re-runs almost free.
    """
    from india_data_pipeline import _nse_get, _nse_get_json, _q
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

    stored, failed, encumbered_cos = 0, 0, 0
    for i, tkr in enumerate(tickers, 1):
        sym = tkr.replace(".NS", "")
        try:
            filings = _nse_get_json(
                "https://www.nseindia.com/api/corporate-share-holdings-master"
                f"?index=equities&symbol={_q(sym)}")
            if not isinstance(filings, list) or not filings:
                failed += 1
                continue

            rows, saw_enc = [], False
            for f_ in filings[:quarters]:
                xbrl = f_.get("xbrl")
                qdate = _parse_nse_date(f_.get("date"))
                if not xbrl:
                    # header values still give promoter/public
                    try:
                        rows.append((tkr, qdate, float(f_.get("pr_and_prgrp")),
                                     None, None, None,
                                     float(f_.get("public_val")), None))
                    except (TypeError, ValueError):
                        pass
                    continue
                try:
                    hdr_prom = float(f_.get("pr_and_prgrp"))
                except (TypeError, ValueError):
                    hdr_prom = None
                try:
                    parsed = _parse_shp_xbrl(_nse_get(xbrl).content,
                                             promoter_pct_hint=hdr_prom)
                except Exception:
                    parsed = {}
                try:
                    hdr_pub = float(f_.get("public_val"))
                except (TypeError, ValueError):
                    hdr_pub = None
                if parsed.get("encumbrance_declared"):
                    saw_enc = True
                rows.append((tkr, qdate, hdr_prom,
                             parsed.get("pledged_pct"), None, None, hdr_pub,
                             bool(parsed.get("encumbrance_declared"))))

            if saw_enc:
                encumbered_cos += 1
            if rows:
                conn = _conn()
                try:
                    with conn.cursor() as cur:
                        for r in rows:
                            cur.execute("""
                                INSERT INTO shareholding
                                    (ticker, quarter_end, promoter_pct, pledged_pct,
                                     fii_pct, dii_pct, public_pct,
                                     encumbrance_declared)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                                ON CONFLICT (ticker, quarter_end) DO UPDATE SET
                                    promoter_pct = EXCLUDED.promoter_pct,
                                    pledged_pct  = EXCLUDED.pledged_pct,
                                    public_pct   = EXCLUDED.public_pct,
                                    encumbrance_declared = EXCLUDED.encumbrance_declared
                            """, r)
                    conn.commit()
                    stored += len(rows)
                finally:
                    conn.close()
        except Exception:
            failed += 1
        time.sleep(sleep)
        if i % 25 == 0:
            print(f"  {i}/{len(tickers)} ({stored} rows, {encumbered_cos} with "
                  f"encumbrance, {failed} unavailable)")

    print(f"✅ shareholding: {stored} rows, {encumbered_cos} companies with "
          f"declared encumbrance, {failed} unavailable")


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
