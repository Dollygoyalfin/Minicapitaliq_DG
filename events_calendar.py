"""
MiniTradeIQ — Known-Events Calendar
====================================
The part of the future that is already written down.

Most of the market is a forecast. A few things are not: they are scheduled,
or set by published rules, and you can know about them weeks ahead. This
module collects those, for Indian listed companies:

  results        Board meetings called to approve quarterly results
  ex_dividend    Ex-dates and record dates for dividends
  bonus / split  Ex-dates for bonus issues and stock splits
  buyback        Record dates for buybacks
  lockin_*       IPO lock-in expiries — the dates when anchor and pre-IPO
                 shareholders are first ALLOWED to sell
  index_*        Stocks that look set to enter or leave the Nifty 50 at the
                 next semi-annual review, computed from NSE's published rules

What each kind is worth, stated plainly:

  - Results, ex-dates, record dates: facts. The date is set by the company.
  - Lock-in expiries: facts about WHEN supply can arrive. Whether holders
    actually sell is not known in advance. Measured studies of Indian IPOs
    find prices tend to weaken around anchor unlocks on average, but the
    spread is wide.
  - Index watch: an ESTIMATE. It follows NSE's methodology with three
    approximations stated in the output — so treat it as a watchlist, not a
    prediction. It is still the closest thing here to seeing the future,
    because index funds must buy what is added, on a known date.

Usage:
    python events_calendar.py init
    python events_calendar.py fetch            # results + corporate actions
    python events_calendar.py ipos             # refresh IPO list + lock-ins
    python events_calendar.py index            # Nifty 50 inclusion watch
    python events_calendar.py all              # everything (nightly)
    python events_calendar.py show --days 30   # print what is coming up
"""

import io
import csv
import sys
import json
import time
from datetime import date, datetime, timedelta
from data_store import _conn

CAL_BUILD = "2026-09-27b (index list from NSE archive CSV; lock-ins on trading days)"

DEFAULT_DAYS_AHEAD = 60


# ── Schema ───────────────────────────────────────────────────────────────────
def init_table():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS event_calendar (
                    id          SERIAL PRIMARY KEY,
                    ticker      TEXT NOT NULL,
                    market      TEXT NOT NULL DEFAULT 'india',
                    event_date  DATE NOT NULL,
                    kind        TEXT NOT NULL,
                    title       TEXT,
                    detail      JSONB,
                    certainty   TEXT,          -- scheduled | rule_based | estimate
                    source      TEXT,
                    fetched_at  TIMESTAMP DEFAULT NOW(),
                    UNIQUE (ticker, event_date, kind)
                );
                CREATE INDEX IF NOT EXISTS idx_cal_date ON event_calendar(event_date);
                CREATE INDEX IF NOT EXISTS idx_cal_ticker ON event_calendar(ticker);

                -- Our own record of IPOs, accumulated nightly. NSE's
                -- past-issues feed is not always reliable, and a lock-in
                -- expiry needs the allotment date from months ago, so the
                -- app keeps its own copy rather than depending on one call.
                CREATE TABLE IF NOT EXISTS ipo_listings (
                    symbol          TEXT PRIMARY KEY,
                    company         TEXT,
                    series          TEXT,
                    issue_end       DATE,
                    allotment_date  DATE,
                    listing_date    DATE,
                    issue_price     DOUBLE PRECISION,
                    dates_estimated BOOLEAN DEFAULT FALSE,
                    first_seen      TIMESTAMP DEFAULT NOW()
                );
            """)
        conn.commit()
    finally:
        conn.close()
    print("event_calendar and ipo_listings tables ready.")


# ── Helpers ──────────────────────────────────────────────────────────────────
_DATE_FORMATS = ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d-%B-%Y",
                 "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%b %d, %Y",
                 "%d/%m/%Y")


def _parse_date(s):
    """NSE uses at least five date formats across its APIs."""
    if not s:
        return None
    if isinstance(s, date):
        return s
    s = str(s).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Some fields carry a date plus trailing text
    head = s.split(" ")[0]
    if head != s:
        return _parse_date(head)
    return None


def _add_working_days(d: date, n: int) -> date:
    """Weekends only. Exchange holidays are ignored, so a date computed this
    way can be one or two days early around a holiday — which is why these
    are marked as estimates wherever they are used."""
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if d.weekday() < 5:
            remaining -= 1
    return d


def _nse(url):
    from india_data_pipeline import _nse_get_json
    try:
        d = _nse_get_json(url)
    except Exception as e:
        print(f"  NSE request failed: {str(e)[:120]}")
        return []
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return d.get("data") or []
    return []


def _pick(row, *keys):
    """First non-empty value among several possible field names — NSE renames
    fields between endpoints and occasionally between releases."""
    for k in keys:
        v = row.get(k)
        if v not in (None, "", "-"):
            return v
    return None


def _upsert(rows):
    """rows: (ticker, market, event_date, kind, title, detail, certainty, source)"""
    if not rows:
        return 0
    conn = _conn()
    n = 0
    try:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute("""
                    INSERT INTO event_calendar
                        (ticker, market, event_date, kind, title, detail,
                         certainty, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (ticker, event_date, kind) DO UPDATE SET
                        title = EXCLUDED.title, detail = EXCLUDED.detail,
                        certainty = EXCLUDED.certainty,
                        source = EXCLUDED.source, fetched_at = NOW()
                """, (r[0], r[1], r[2], r[3], r[4][:300] if r[4] else None,
                      json.dumps(r[5]) if r[5] is not None else None,
                      r[6], r[7]))
                n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def _ns(symbol):
    s = (symbol or "").strip().upper()
    return s if s.endswith(".NS") else s + ".NS"


# ── 1. Board meetings (results dates) ────────────────────────────────────────
_BM_KINDS = (
    ("results",    ("financial result", "quarterly result", "audited result",
                    "unaudited result", "results")),
    ("buyback",    ("buyback", "buy back", "buy-back")),
    ("fund_raise", ("fund raising", "fund raise", "qip", "preferential",
                    "rights issue", "raising of funds")),
    ("bonus",      ("bonus",)),
    ("split",      ("split", "sub-division", "subdivision")),
    ("dividend",   ("dividend",)),
)


def _bm_kind(purpose: str) -> str:
    p = (purpose or "").lower()
    for kind, words in _BM_KINDS:
        if any(w in p for w in words):
            return "meeting_" + kind if kind != "results" else "results"
    return "board_meeting"


def fetch_board_meetings(days_ahead: int = DEFAULT_DAYS_AHEAD):
    today = date.today()
    horizon = today + timedelta(days=days_ahead)
    rows = _nse("https://www.nseindia.com/api/corporate-board-meetings"
                "?index=equities")
    out = []
    for r in rows:
        sym = _pick(r, "bm_symbol", "symbol")
        d = _parse_date(_pick(r, "bm_date", "meetingDate", "date"))
        if not sym or not d or d < today or d > horizon:
            continue
        purpose = _pick(r, "bm_purpose", "purpose") or ""
        desc = _pick(r, "bm_desc", "description") or ""
        kind = _bm_kind(purpose + " " + desc)
        title = purpose.strip() or "Board meeting"
        out.append((_ns(sym), "india", d, kind, title,
                    {"description": desc[:400],
                     "company": _pick(r, "sm_name", "companyName")},
                    "scheduled", "nse_board_meetings"))
    n = _upsert(out)
    print(f"  Board meetings: {n} upcoming within {days_ahead} days")
    return n


# ── 2. Corporate actions (ex-dates, record dates) ────────────────────────────
def _ca_kind(subject: str):
    s = (subject or "").lower()
    if "buy back" in s or "buyback" in s or "buy-back" in s:
        return "buyback"
    if "bonus" in s:
        return "bonus"
    if "split" in s or "sub-division" in s or "subdivision" in s:
        return "split"
    if "rights" in s:
        return "rights"
    if "dividend" in s:
        return "ex_dividend"
    if "demerger" in s or "scheme" in s:
        return "scheme"
    return None       # AGMs, interest payments etc. are not worth a slot


def fetch_corporate_actions(days_ahead: int = DEFAULT_DAYS_AHEAD):
    today = date.today()
    horizon = today + timedelta(days=days_ahead)
    url = ("https://www.nseindia.com/api/corporates-corporateActions"
           f"?index=equities&from_date={today:%d-%m-%Y}"
           f"&to_date={horizon:%d-%m-%Y}")
    rows = _nse(url)
    if not rows:
        # Without the date window NSE returns the recent list, which still
        # contains the forward-dated actions
        rows = _nse("https://www.nseindia.com/api/corporates-corporateActions"
                    "?index=equities")
    out = []
    for r in rows:
        sym = _pick(r, "symbol")
        subject = _pick(r, "subject", "purpose") or ""
        kind = _ca_kind(subject)
        if not sym or not kind:
            continue
        ex = _parse_date(_pick(r, "exDate", "ex_date"))
        rec = _parse_date(_pick(r, "recDate", "recordDate", "record_date"))
        d = ex or rec
        if not d or d < today or d > horizon:
            continue
        out.append((_ns(sym), "india", d, kind, subject.strip(),
                    {"ex_date": str(ex) if ex else None,
                     "record_date": str(rec) if rec else None,
                     "company": _pick(r, "comp", "companyName")},
                    "scheduled", "nse_corporate_actions"))
    n = _upsert(out)
    print(f"  Corporate actions: {n} upcoming within {days_ahead} days")
    return n


# ── 3. IPO lock-in expiries ──────────────────────────────────────────────────
# SEBI ICDR, as it stands for main-board IPOs:
#   - Anchor investors: 50% of their shares locked for 30 days from allotment,
#     the remaining 50% for 90 days.
#   - Pre-IPO shareholders other than promoters: 6 months from allotment.
#   - Promoters' minimum contribution: 18 months (3 years if the issue funds
#     capex). Promoter lock-ins are long and rarely a supply event, so they
#     are left out.
# Since the T+3 regime (Dec 2023), allotment is about 2 working days after
# the issue closes and listing about 3. Where NSE gives us the real dates we
# use them; where it does not, the dates are estimated and marked as such.
LOCKINS = (
    ("lockin_anchor_50",  30,  "50% of anchor investor shares free to sell"),
    ("lockin_anchor_all", 90,  "Remaining anchor investor shares free to sell"),
    ("lockin_pre_ipo",    182, "Pre-IPO shareholders' 6-month lock-in ends"),
)


def refresh_ipos():
    """Accumulate IPOs into ipo_listings from NSE's current and past feeds."""
    found = []
    for url in ("https://www.nseindia.com/api/ipo-current-issue",
                "https://www.nseindia.com/api/public-past-issues"):
        for r in _nse(url):
            sym = _pick(r, "symbol")
            if not sym:
                continue
            series = (_pick(r, "series", "securityType") or "").upper()
            end = _parse_date(_pick(r, "issueEndDate", "ipoEndDate",
                                    "bidEndDate", "endDate"))
            listing = _parse_date(_pick(r, "listingDate", "listing_date"))
            allot = _parse_date(_pick(r, "allotmentDate", "dateOfAllotment"))
            price = _pick(r, "issuePrice", "priceBand", "maxPrice")
            try:
                price = float(str(price).split("-")[-1].replace(",", "")
                              .replace("Rs.", "").strip()) if price else None
            except Exception:
                price = None
            found.append((sym.upper(), _pick(r, "companyName", "company",
                                             "issueName"),
                          series, end, allot, listing, price))

    if not found:
        print("  IPOs: NSE returned nothing (feed unavailable or blocked).")
        return 0

    cutoff = date.today() - timedelta(days=200)
    conn = _conn()
    n = 0
    try:
        with conn.cursor() as cur:
            for sym, comp, series, end, allot, listing, price in found:
                if not end or end < cutoff:
                    continue           # lock-ins for these have all expired
                estimated = allot is None or listing is None
                allot = allot or _add_working_days(end, 2)
                listing = listing or _add_working_days(end, 3)
                cur.execute("""
                    INSERT INTO ipo_listings
                        (symbol, company, series, issue_end, allotment_date,
                         listing_date, issue_price, dates_estimated)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol) DO UPDATE SET
                        company = COALESCE(EXCLUDED.company, ipo_listings.company),
                        series  = COALESCE(NULLIF(EXCLUDED.series,''), ipo_listings.series),
                        issue_end = COALESCE(EXCLUDED.issue_end, ipo_listings.issue_end),
                        -- a real date always beats an estimate
                        allotment_date = CASE WHEN ipo_listings.dates_estimated
                            THEN EXCLUDED.allotment_date
                            ELSE ipo_listings.allotment_date END,
                        listing_date = CASE WHEN ipo_listings.dates_estimated
                            THEN EXCLUDED.listing_date
                            ELSE ipo_listings.listing_date END,
                        dates_estimated = ipo_listings.dates_estimated
                                          AND EXCLUDED.dates_estimated,
                        issue_price = COALESCE(EXCLUDED.issue_price,
                                               ipo_listings.issue_price)
                """, (sym, comp, series, end, allot, listing, price, estimated))
                n += 1
        conn.commit()
    finally:
        conn.close()
    print(f"  IPOs: {n} recent issues recorded")
    return n


def lockin_events(allotment: date, include_sme: bool = False, series: str = ""):
    """Pure function — the lock-in schedule for one IPO. Kept separate so the
    date arithmetic can be tested without a database."""
    if not allotment:
        return []
    if not include_sme and "SME" in (series or "").upper():
        return []
    out = []
    for kind, days, label in LOCKINS:
        d = allotment + timedelta(days=days)
        # The lock-in ends on a calendar date, but shares can only be SOLD
        # on a trading day — so the date that matters is the next weekday.
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append((kind, d, label))
    return out


def build_lockins(days_ahead: int = DEFAULT_DAYS_AHEAD):
    today = date.today()
    horizon = today + timedelta(days=days_ahead)
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT symbol, company, series, allotment_date,
                                  listing_date, issue_price, dates_estimated
                           FROM ipo_listings""")
            ipos = cur.fetchall()
    finally:
        conn.close()

    # Lock-ins are fully recomputed from ipo_listings each run, so clear the
    # future ones first — otherwise a date that moved (weekend roll, or an
    # estimated allotment replaced by the real one) would leave a stale twin.
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""DELETE FROM event_calendar WHERE kind LIKE 'lockin_%%'
                           AND event_date >= CURRENT_DATE""")
        conn.commit()
    finally:
        conn.close()

    out = []
    for sym, comp, series, allot, listing, price, est in ipos:
        for kind, d, label in lockin_events(allot, series=series):
            if today <= d <= horizon:
                out.append((_ns(sym), "india", d, kind, label,
                            {"company": comp, "allotment_date": str(allot),
                             "listing_date": str(listing) if listing else None,
                             "issue_price": price,
                             "dates_estimated": bool(est)},
                            "rule_based", "sebi_icdr_lockin"))
    n = _upsert(out)
    print(f"  Lock-in expiries: {n} within {days_ahead} days")
    return n


# ── 4. Nifty 50 inclusion watch ──────────────────────────────────────────────
# NSE's method, in the parts that matter here:
#   - Reviewed twice a year. Changes take effect at the end of March and the
#     end of September, announced about four weeks before.
#   - Ranked on AVERAGE free-float market cap over the six months to the
#     cut-off (end of January for the March review, end of July for
#     September).
#   - A non-member is added if its free-float cap is at least 1.5x that of
#     the smallest current member. The index stays at 50, so each addition
#     removes the weakest member.
#   - Only stocks in the F&O segment are eligible.
#
# Our approximations, all stated in the output:
#   1. Free float = 100% minus promoter holding. NSE also excludes some
#      strategic and government holdings, so our float is slightly high for
#      those companies.
#   2. Six-month average price from our weekly bars, times current shares.
#   3. F&O eligibility is not checked — a flagged stock outside F&O cannot
#      be added regardless of size.

def next_index_review(today: date = None):
    today = today or date.today()
    y = today.year
    candidates = [date(y, 3, 31), date(y, 9, 30), date(y + 1, 3, 31)]
    for d in candidates:
        # effective date must still be ahead AND its announcement not yet
        # made (announcement ~4 weeks earlier), otherwise the next one
        if d - timedelta(days=28) > today:
            cutoff = date(d.year, 1, 31) if d.month == 3 else date(d.year, 7, 31)
            return d, cutoff
    d = date(y + 1, 9, 30)
    return d, date(y + 1, 7, 31)


def rank_for_index(rows, members, add_ratio: float = 1.5, top_n: int = 50):
    """Pure function. rows: [(ticker, ffmcap)]; members: set of tickers.
    Returns (adds, drops, smallest_member_ffmcap)."""
    ranked = sorted([r for r in rows if r[1] and r[1] > 0],
                    key=lambda r: r[1], reverse=True)
    mem = [r for r in ranked if r[0] in members]
    if not mem:
        return [], [], None
    smallest = mem[-1][1]
    adds = [(t, v, v / smallest) for t, v in ranked
            if t not in members and v >= add_ratio * smallest]
    # Each addition displaces the weakest member
    drops = [(t, v) for t, v in reversed(mem)][:len(adds)]
    return adds, drops, smallest


def nifty50_members():
    """NSE publishes the constituent list as a plain CSV in its archive
    (Company Name, Industry, Symbol, Series, ISIN Code). That file is far
    steadier than the live API, which started returning 404."""
    from india_data_pipeline import _nse_get
    try:
        raw = _nse_get("https://nsearchives.nseindia.com/content/indices/"
                       "ind_nifty50list.csv", retries=2).content
        rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig",
                                                     errors="replace")))
        rows.fieldnames = [f.strip() for f in (rows.fieldnames or [])]
        syms = {(r.get("Symbol") or "").strip().upper() for r in rows}
        syms.discard("")
        if len(syms) >= 40:
            return {_ns(x) for x in syms}
    except Exception as e:
        print(f"  Nifty 50 list (archive CSV) unavailable: {str(e)[:90]}")
    raw = _nse("https://www.nseindia.com/api/equity-stockIndices"
               "?index=NIFTY%2050")
    return {_ns(r.get("symbol")) for r in raw
            if r.get("symbol") and r.get("symbol") != "NIFTY 50"
            and r.get("priority", 0) != 1}


def index_watch():
    members = nifty50_members()
    if len(members) < 40:
        print(f"  Index watch: could not read Nifty 50 membership "
              f"({len(members)} names) — skipped.")
        return 0

    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '60s'")
            cur.execute("""
                WITH px AS (
                  SELECT ticker, AVG(price) AS avg_px
                  FROM stock_signatures
                  WHERE market='india'
                    AND date >= CURRENT_DATE - INTERVAL '182 days'
                    AND price > 0
                  GROUP BY ticker
                ), prom AS (
                  SELECT DISTINCT ON (ticker) ticker, promoter_pct
                  FROM shareholding ORDER BY ticker, quarter_end DESC
                )
                SELECT c.ticker, c.name,
                       px.avg_px * c.shares_outstanding
                         * (1 - COALESCE(prom.promoter_pct, 0) / 100.0)
                FROM companies c
                JOIN px ON px.ticker = c.ticker
                LEFT JOIN prom ON prom.ticker = c.ticker
                WHERE c.market='india' AND c.shares_outstanding > 0
                  -- A shareholding filing must exist, so we are not guessing
                  -- 100% float for a company we know nothing about. But a
                  -- filing with no promoter line is a real answer — HDFC Bank,
                  -- ICICI, ITC and L&T have no promoter — and means 0%.
                  AND prom.ticker IS NOT NULL
            """)
            rows = cur.fetchall()
            cur.execute("DELETE FROM event_calendar WHERE kind IN "
                        "('index_add_candidate','index_drop_risk')")
        conn.commit()
    finally:
        conn.close()

    names = {t: n for t, n, _ in rows}
    adds, drops, smallest = rank_for_index([(t, v) for t, _, v in rows],
                                           members)
    eff, cutoff = next_index_review()
    caveat = ("Estimate from NSE's published rules using our own data: free "
              "float approximated as non-promoter holding, 6-month average "
              "from weekly prices, F&O eligibility not checked.")
    out = []
    for t, v, ratio in adds:
        out.append((t, "india", eff, "index_add_candidate",
                    f"Nifty 50 inclusion candidate "
                    f"({ratio:.1f}x the smallest member)",
                    {"company": names.get(t), "ffmcap_cr": round(v / 1e7),
                     "ratio_to_smallest": round(ratio, 2),
                     "data_cutoff": str(cutoff), "caveat": caveat},
                    "estimate", "nifty50_methodology"))
    for t, v in drops:
        out.append((t, "india", eff, "index_drop_risk",
                    "Nifty 50 exclusion risk (weakest member by free-float cap)",
                    {"company": names.get(t), "ffmcap_cr": round(v / 1e7),
                     "data_cutoff": str(cutoff), "caveat": caveat},
                    "estimate", "nifty50_methodology"))
    _upsert(out)
    print(f"  Index watch: {len(adds)} inclusion candidate(s), "
          f"{len(drops)} exclusion risk(s) for the review effective {eff}")
    if smallest:
        print(f"  (smallest member free-float cap ≈ ₹{smallest/1e7:,.0f} Cr; "
              f"threshold to enter ≈ ₹{1.5*smallest/1e7:,.0f} Cr)")
    return len(out)


# ── Reading ──────────────────────────────────────────────────────────────────
KIND_LABELS = {
    "results": "📊 Results",
    "ex_dividend": "💰 Ex-dividend",
    "bonus": "🎁 Bonus",
    "split": "✂️ Split",
    "buyback": "🔁 Buyback record date",
    "rights": "📝 Rights",
    "scheme": "🧩 Scheme/demerger",
    "meeting_buyback": "🔁 Board to consider buyback",
    "meeting_fund_raise": "💵 Board to consider fund raise",
    "meeting_bonus": "🎁 Board to consider bonus",
    "meeting_split": "✂️ Board to consider split",
    "meeting_dividend": "💰 Board to consider dividend",
    "board_meeting": "🏛 Board meeting",
    "lockin_anchor_50": "🔓 Anchor lock-in (50%)",
    "lockin_anchor_all": "🔓 Anchor lock-in (100%)",
    "lockin_pre_ipo": "🔓 Pre-IPO lock-in",
    "index_add_candidate": "⬆️ Nifty 50 candidate",
    "index_drop_risk": "⬇️ Nifty 50 exit risk",
}


def upcoming(days: int = 30, tickers=None, kinds=None, market: str = "india"):
    """Rows of (ticker, event_date, kind, title, detail, certainty)."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            q = """SELECT ticker, event_date, kind, title, detail, certainty
                   FROM event_calendar
                   WHERE market=%s AND event_date >= CURRENT_DATE
                     AND event_date <= CURRENT_DATE + (%s || ' days')::interval"""
            p = [market, days]
            if tickers:
                q += " AND ticker = ANY(%s)"
                p.append(list(tickers))
            if kinds:
                q += " AND kind = ANY(%s)"
                p.append(list(kinds))
            q += " ORDER BY event_date, kind, ticker"
            cur.execute(q, p)
            return cur.fetchall()
    finally:
        conn.close()


def show(days: int = 30):
    rows = upcoming(days)
    if not rows:
        print(f"Nothing in the calendar for the next {days} days. "
              "Run `python events_calendar.py all` first.")
        return
    last = None
    for t, d, kind, title, detail, cert in rows:
        if d != last:
            print(f"\n{d:%a %d %b %Y}")
            last = d
        tag = {"estimate": " (estimate)", "rule_based": ""}.get(cert, "")
        print(f"  {KIND_LABELS.get(kind, kind):<30} {t.replace('.NS',''):<14}"
              f"{(title or '')[:60]}{tag}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    a = sys.argv
    days = int(a[a.index("--days") + 1]) if "--days" in a else DEFAULT_DAYS_AHEAD

    if cmd == "init":
        init_table()
    elif cmd == "fetch":
        init_table()
        fetch_board_meetings(days)
        fetch_corporate_actions(days)
    elif cmd == "ipos":
        init_table()
        refresh_ipos()
        build_lockins(days)
    elif cmd == "index":
        init_table()
        index_watch()
    elif cmd == "all":
        init_table()
        print("Refreshing known-events calendar...")
        for step in (lambda: fetch_board_meetings(days),
                     lambda: fetch_corporate_actions(days),
                     refresh_ipos,
                     lambda: build_lockins(days),
                     index_watch):
            try:
                step()
            except Exception as e:
                print(f"  step failed: {str(e)[:150]}")
            time.sleep(1)
        print("Done.")
    elif cmd == "show":
        show(int(a[a.index("--days") + 1]) if "--days" in a else 30)
    else:
        print(__doc__)
