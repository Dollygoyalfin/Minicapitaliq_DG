"""
MiniTradeIQ — Portfolio Monitor
=================================
Watches YOUR holdings and messages you when something FACTUAL changes.

Design principle, and the reason this is not a "buy/sell signal" bot:
every alert here reports something that HAS HAPPENED — an auditor resigned,
promoter encumbrance rose, a quality grade fell, a price crossed a level you
set yourself. None of them predict anything.

That matters for two reasons. First, the app has no track record yet: the
signal journal has snapshots but nothing old enough to score, so a daily
"SELL X" would carry authority the model has not earned. Second, alerts that
demand action every day are how people trade themselves into losses. An alert
that fires three times a year, for something that genuinely matters, is worth
more than a daily digest you learn to ignore.

The app tells you what happened. You decide what to do.

Usage:
    python portfolio.py init
    python portfolio.py add RELIANCE india 100 1250
    python portfolio.py add AAPL us 20 180
    python portfolio.py list
    python portfolio.py remove RELIANCE
    python portfolio.py target RELIANCE --sell-above 1600 --buy-below 1100
    python portfolio.py check              # evaluate, print, and send
    python portfolio.py check --dry-run    # evaluate and print only
"""

import os
import sys
import json
from datetime import date, timedelta
from data_store import _conn

PORTFOLIO_BUILD = "2026-07-27c (full digest + screen)"

# Don't repeat the same alert for the same reason within this many days
ALERT_COOLDOWN_DAYS = 7

# How much to report. Every level reports FACTS — the difference is the
# threshold for what counts as worth your attention, not whether the app
# starts predicting.
#
#   critical : governance red flags and your own price targets only
#   important: the above, plus big price moves, 52-week breaks, quality changes
#   all      : the above, plus every corporate filing and a daily market summary
ALERT_LEVEL = os.environ.get("ALERT_LEVEL", "important").lower()

BIG_MOVE_1D = float(os.environ.get("BIG_MOVE_1D", "5"))    # percent in a day
BIG_MOVE_5D = float(os.environ.get("BIG_MOVE_5D", "10"))   # percent in a week


# ── Schema ───────────────────────────────────────────────────────────────────
def init_tables():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS holdings (
                    ticker      TEXT PRIMARY KEY,
                    market      TEXT NOT NULL,
                    quantity    DOUBLE PRECISION,
                    avg_cost    DOUBLE PRECISION,
                    sell_above  DOUBLE PRECISION,
                    buy_below   DOUBLE PRECISION,
                    added_at    TIMESTAMP DEFAULT NOW(),
                    notes       TEXT
                );

                CREATE TABLE IF NOT EXISTS alert_log (
                    id         SERIAL PRIMARY KEY,
                    ticker     TEXT NOT NULL,
                    rule       TEXT NOT NULL,
                    detail     TEXT,
                    sent_at    TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_alert_ticker
                    ON alert_log(ticker, rule, sent_at);
            """)
        conn.commit()
    finally:
        conn.close()
    print("holdings and alert_log tables ready.")


# ── Holdings management ──────────────────────────────────────────────────────
def add_holding(ticker, market, qty=None, cost=None):
    t = ticker.upper()
    if market == "india" and not t.endswith(".NS"):
        t += ".NS"
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO holdings (ticker, market, quantity, avg_cost)
                           VALUES (%s,%s,%s,%s)
                           ON CONFLICT (ticker) DO UPDATE SET
                               quantity = EXCLUDED.quantity,
                               avg_cost = EXCLUDED.avg_cost""",
                        (t, market, qty, cost))
        conn.commit()
    finally:
        conn.close()
    print(f"✅ {t} added ({qty or '?'} @ {cost or '?'})")


def remove_holding(ticker):
    t = ticker.upper()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM holdings WHERE ticker IN (%s, %s)",
                        (t, t + ".NS"))
            n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    print(f"{'✅ removed' if n else 'not found:'} {t}")


def set_targets(ticker, sell_above=None, buy_below=None):
    t = ticker.upper()
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE holdings SET
                             sell_above = COALESCE(%s, sell_above),
                             buy_below  = COALESCE(%s, buy_below)
                           WHERE ticker IN (%s, %s)""",
                        (sell_above, buy_below, t, t + ".NS"))
            n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    print(f"{'✅ targets set for' if n else 'not found:'} {t}")


def list_holdings():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT h.ticker, h.market, h.quantity, h.avg_cost,
                                  h.sell_above, h.buy_below,
                                  (SELECT price FROM stock_signatures s
                                   WHERE s.ticker = h.ticker
                                   ORDER BY date DESC LIMIT 1)
                           FROM holdings h ORDER BY h.ticker""")
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        print("No holdings yet. Add one:  python portfolio.py add RELIANCE india 100 1250")
        return
    print(f"\n{'ticker':<16}{'qty':>8}{'cost':>10}{'price':>10}{'P/L %':>9}"
          f"{'sell>':>9}{'buy<':>9}")
    print("-" * 72)
    for t, mkt, q, c, sa, bb, px in rows:
        pl = ((px / c - 1) * 100) if (px and c) else None
        print(f"{t:<16}{(q or 0):>8.0f}{(c or 0):>10.2f}{(px or 0):>10.2f}"
              f"{(pl if pl is not None else 0):>8.1f}%"
              f"{(sa or 0):>9.0f}{(bb or 0):>9.0f}")
    print()


# ── Alert rules — all report facts, none predict ─────────────────────────────
def _recent_alert(cur, ticker, rule):
    cur.execute("""SELECT 1 FROM alert_log
                   WHERE ticker=%s AND rule=%s
                     AND sent_at > NOW() - INTERVAL '%s days'
                   LIMIT 1""", (ticker, rule, ALERT_COOLDOWN_DAYS))
    return cur.fetchone() is not None


def evaluate_alerts():
    """Returns a list of (ticker, rule, message) for things that changed."""
    alerts = []
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT ticker, market, quantity, avg_cost,
                                  sell_above, buy_below FROM holdings""")
            holdings = cur.fetchall()
            if not holdings:
                return []

            for tkr, mkt, qty, cost, sell_above, buy_below in holdings:
                short = tkr.replace(".NS", "")

                # 1. Red-flag corporate filings in the last week
                cur.execute("""SELECT event_date, category, headline
                               FROM news_events
                               WHERE ticker=%s AND severity='red_flag'
                                 AND event_date >= CURRENT_DATE - INTERVAL '7 days'
                               ORDER BY event_date DESC LIMIT 3""", (tkr,))
                for ev_date, cat, headline in cur.fetchall():
                    rule = f"red_flag:{cat}"
                    if _recent_alert(cur, tkr, rule):
                        continue
                    alerts.append((tkr, rule,
                        f"🔴 *{short}* — {cat.replace('_',' ')} "
                        f"({ev_date})\n{headline[:160]}"))

                # 2. Promoter encumbrance increased
                cur.execute("""SELECT quarter_end, pledged_pct FROM shareholding
                               WHERE ticker=%s AND pledged_pct IS NOT NULL
                               ORDER BY quarter_end DESC LIMIT 2""", (tkr,))
                sh = cur.fetchall()
                if len(sh) == 2 and sh[0][1] is not None and sh[1][1] is not None:
                    rise = sh[0][1] - sh[1][1]
                    if rise >= 1.0 and not _recent_alert(cur, tkr, "encumbrance_up"):
                        alerts.append((tkr, "encumbrance_up",
                            f"⚠️ *{short}* — promoter encumbrance rose to "
                            f"{sh[0][1]:.1f}% (was {sh[1][1]:.1f}%) as of {sh[0][0]}"))

                # 3. Quality grade fell
                cur.execute("""SELECT signal_date, signal FROM signal_journal
                               WHERE ticker=%s AND source='quality'
                               ORDER BY signal_date DESC LIMIT 2""", (tkr,))
                qg = cur.fetchall()
                if len(qg) == 2 and qg[0][1] != qg[1][1]:
                    order = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}
                    if order.get(qg[0][1], 9) > order.get(qg[1][1], 9):
                        if not _recent_alert(cur, tkr, "quality_drop"):
                            alerts.append((tkr, "quality_drop",
                                f"📉 *{short}* — quality grade fell from "
                                f"{qg[1][1]} to {qg[0][1]}"))

                # 4. Any corporate filing at all (level: all)
                if ALERT_LEVEL == "all":
                    cur.execute("""SELECT event_date, category, severity, headline
                                   FROM news_events
                                   WHERE ticker=%s AND severity <> 'red_flag'
                                     AND event_date >= CURRENT_DATE - INTERVAL '2 days'
                                   ORDER BY event_date DESC LIMIT 4""", (tkr,))
                    for ev_date, cat, sev, headline in cur.fetchall():
                        rule = f"filing:{cat}:{ev_date}"
                        if _recent_alert(cur, tkr, rule):
                            continue
                        icon = {"positive": "🟢", "watch": "🟡"}.get(sev, "📄")
                        alerts.append((tkr, rule,
                            f"{icon} *{short}* — {cat.replace('_',' ')}\n"
                            f"{headline[:150]}"))

                # 5. Large price moves (level: important and above)
                if ALERT_LEVEL in ("important", "all"):
                    cur.execute("""SELECT date, price FROM stock_signatures
                                   WHERE ticker=%s ORDER BY date DESC LIMIT 6""",
                                (tkr,))
                    px_rows = cur.fetchall()
                    if len(px_rows) >= 2 and px_rows[0][1] and px_rows[1][1]:
                        chg1 = (px_rows[0][1] / px_rows[1][1] - 1) * 100
                        if abs(chg1) >= BIG_MOVE_1D:
                            rule = f"move_1d:{px_rows[0][0]}"
                            if not _recent_alert(cur, tkr, rule):
                                arrow = "📈" if chg1 > 0 else "📉"
                                alerts.append((tkr, rule,
                                    f"{arrow} *{short}* moved {chg1:+.1f}% "
                                    f"to {'₹' if mkt=='india' else '$'}"
                                    f"{px_rows[0][1]:,.0f}"))
                    if len(px_rows) >= 6 and px_rows[0][1] and px_rows[5][1]:
                        chg5 = (px_rows[0][1] / px_rows[5][1] - 1) * 100
                        if abs(chg5) >= BIG_MOVE_5D:
                            rule = "move_5d"
                            if not _recent_alert(cur, tkr, rule):
                                arrow = "📈" if chg5 > 0 else "📉"
                                alerts.append((tkr, rule,
                                    f"{arrow} *{short}* is {chg5:+.1f}% over "
                                    f"the last few weeks"))

                    # 52-week breaks — factual, and often what prompts a look
                    cur.execute("""SELECT rank_from_high, rank_from_low
                                   FROM stock_signatures WHERE ticker=%s
                                   ORDER BY date DESC LIMIT 1""", (tkr,))
                    rk = cur.fetchone()
                    if rk:
                        if rk[0] is not None and rk[0] >= 98 and not _recent_alert(cur, tkr, "new_high"):
                            alerts.append((tkr, "new_high",
                                f"🔼 *{short}* is at/near a 52-week high"))
                        if rk[1] is not None and rk[1] <= 2 and not _recent_alert(cur, tkr, "new_low"):
                            alerts.append((tkr, "new_low",
                                f"🔽 *{short}* is at/near a 52-week low"))

                # 6. Price crossed a level YOU set
                cur.execute("""SELECT price FROM stock_signatures WHERE ticker=%s
                               ORDER BY date DESC LIMIT 1""", (tkr,))
                row = cur.fetchone()
                px = row[0] if row else None
                cur_sym = "₹" if mkt == "india" else "$"
                if px and sell_above and px >= sell_above:
                    if not _recent_alert(cur, tkr, "above_target"):
                        pl = f" (cost {cur_sym}{cost:.0f})" if cost else ""
                        alerts.append((tkr, "above_target",
                            f"🎯 *{short}* — {cur_sym}{px:,.0f} is above your "
                            f"{cur_sym}{sell_above:,.0f} level{pl}"))
                if px and buy_below and px <= buy_below:
                    if not _recent_alert(cur, tkr, "below_target"):
                        alerts.append((tkr, "below_target",
                            f"🎯 *{short}* — {cur_sym}{px:,.0f} is below your "
                            f"{cur_sym}{buy_below:,.0f} level"))
    finally:
        conn.close()
    return alerts


def market_summary():
    """Factual market state — breadth and index move. Not a forecast, and
    deliberately not called 'sentiment': this is what the market DID, which
    is measurable, rather than what it 'feels', which is not."""
    if ALERT_LEVEL != "all":
        return None
    out = []
    conn = _conn()
    try:
        with conn.cursor() as cur:
            for mkt, label, sym in (("india", "India", "₹"), ("us", "US", "$")):
                cur.execute("""SELECT date FROM stock_signatures WHERE market=%s
                               ORDER BY date DESC LIMIT 2""", (mkt,))
                dates = [r[0] for r in cur.fetchall()]
                if len(dates) < 2:
                    continue
                # Equal-weight move: mean of per-stock returns, not the change
                # in an average price (that would count entries as returns)
                cur.execute("""
                    SELECT AVG(chg) FROM (
                      SELECT (a.price / b.price - 1) * 100 AS chg
                      FROM stock_signatures a
                      JOIN stock_signatures b
                        ON a.ticker = b.ticker AND b.date = %s
                      WHERE a.date = %s AND a.market = %s
                        AND b.price > 0
                    ) t""", (dates[1], dates[0], mkt))
                move = cur.fetchone()[0]
                cur.execute("""SELECT
                       AVG(CASE WHEN rank_vs_200dma >= 50 THEN 1.0 ELSE 0.0 END) * 100
                     FROM stock_signatures WHERE market=%s AND date=%s""",
                     (mkt, dates[0]))
                breadth = cur.fetchone()[0]
                if move is not None and breadth is not None:
                    out.append(f"{label}: {move:+.1f}% (equal-weight), "
                               f"{breadth:.0f}% of stocks above their 200-day average")
    finally:
        conn.close()
    return "📊 *Market*\n" + "\n".join(out) if out else None


def log_alerts(alerts):
    if not alerts:
        return
    conn = _conn()
    try:
        with conn.cursor() as cur:
            for tkr, rule, msg in alerts:
                cur.execute("""INSERT INTO alert_log (ticker, rule, detail)
                               VALUES (%s,%s,%s)""", (tkr, rule, msg[:400]))
        conn.commit()
    finally:
        conn.close()


# ── WhatsApp delivery (Twilio) ───────────────────────────────────────────────
def send_whatsapp(body: str) -> bool:
    """Requires TWILIO_SID, TWILIO_TOKEN, TWILIO_WHATSAPP_FROM and
    MY_WHATSAPP_TO in the environment. Fails quietly but reports."""
    sid   = os.environ.get("TWILIO_SID", "")
    token = os.environ.get("TWILIO_TOKEN", "")
    src   = os.environ.get("TWILIO_WHATSAPP_FROM", "")   # e.g. whatsapp:+14155238886
    dst   = os.environ.get("MY_WHATSAPP_TO", "")         # e.g. whatsapp:+9198xxxxxxxx
    if not all([sid, token, src, dst]):
        print("  WhatsApp not configured — set TWILIO_SID, TWILIO_TOKEN, "
              "TWILIO_WHATSAPP_FROM, MY_WHATSAPP_TO")
        return False
    try:
        import httpx
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                auth=(sid, token),
                data={"From": src, "To": dst, "Body": body[:1500]})
        if r.status_code in (200, 201):
            print("  ✅ WhatsApp sent")
            return True
        print(f"  WhatsApp failed: HTTP {r.status_code} {r.text[:160]}")
    except Exception as e:
        print(f"  WhatsApp error: {str(e)[:140]}")
    return False


def portfolio_digest():
    """Full status of every holding, plus screener ideas.

    Unlike the event alerts, this is a scheduled summary — it goes out
    whether or not anything changed, because you asked to see the whole book.
    Each line reports measured facts: price, your P/L, the quality grade from
    the store, and promoter encumbrance. It does not say hold or sell; the
    numbers are there so you can.
    """
    lines = []
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT ticker, market, quantity, avg_cost,
                                  sell_above, buy_below FROM holdings
                           ORDER BY ticker""")
            holdings = cur.fetchall()
            if not holdings:
                return None

            total_cost = total_value = 0.0
            rows = []
            for tkr, mkt, qty, cost, sa, bb in holdings:
                short = tkr.replace(".NS", "")
                sym = "₹" if mkt == "india" else "$"

                cur.execute("""SELECT price, rank_from_high, rank_mom_12m, rsi_14
                               FROM stock_signatures WHERE ticker=%s
                               ORDER BY date DESC LIMIT 1""", (tkr,))
                r = cur.fetchone()
                px = r[0] if r else None
                rk_high, mom12, rsi = (r[1], r[2], r[3]) if r else (None, None, None)

                cur.execute("""SELECT signal, (detail->>'score')::float
                               FROM signal_journal
                               WHERE ticker=%s AND source='quality'
                               ORDER BY signal_date DESC LIMIT 1""", (tkr,))
                q = cur.fetchone()
                grade = q[0] if q else None

                cur.execute("""SELECT pledged_pct FROM shareholding
                               WHERE ticker=%s AND pledged_pct IS NOT NULL
                               ORDER BY quarter_end DESC LIMIT 1""", (tkr,))
                pl = cur.fetchone()
                pledged = pl[0] if pl else None

                cur.execute("""SELECT COUNT(*) FROM news_events
                               WHERE ticker=%s AND severity='red_flag'
                                 AND event_date >= CURRENT_DATE - INTERVAL '180 days'""",
                            (tkr,))
                flags = cur.fetchone()[0]

                bits = [f"*{short}*  {sym}{px:,.0f}" if px else f"*{short}*  —"]
                if qty and cost and px:
                    pnl = (px / cost - 1) * 100
                    total_cost += qty * cost
                    total_value += qty * px
                    bits.append(f"{pnl:+.1f}% (cost {sym}{cost:,.0f})")
                if grade:
                    bits.append(f"Q:{grade}")
                if pledged is not None and pledged > 1:
                    bits.append(f"⚠️{pledged:.0f}% pledged")
                if flags:
                    bits.append(f"🔴{flags} flag(s)/180d")
                if rsi is not None:
                    bits.append(f"RSI {rsi:.0f}")
                rows.append("  ".join(bits))

            lines.append("*📁 Your holdings*")
            lines.extend(rows)
            if total_cost > 0:
                tot = (total_value / total_cost - 1) * 100
                lines.append(f"\n_Book: {tot:+.1f}% overall_")
    finally:
        conn.close()
    return "\n".join(lines)


def screen_ideas(market="india", limit=5):
    """Stocks matching the one setup that validated on this app's own data:
    quality + momentum (+12.4% median excess at 12m, beat market 60%, n=860).

    This is a SCREEN, not a recommendation — it lists what matches defined
    conditions, with the measured base rate attached so the evidence travels
    with the list.
    """
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '45s'")
            cur.execute("""
                WITH sig AS (
                    SELECT DISTINCT ON (ticker) ticker, price, rank_mom_12m,
                           rank_vs_200dma
                    FROM stock_signatures WHERE market=%s
                    ORDER BY ticker, date DESC
                ),
                q AS (
                    SELECT DISTINCT ON (ticker) ticker, signal,
                           (detail->>'score')::float AS score
                    FROM signal_journal WHERE source='quality'
                    ORDER BY ticker, signal_date DESC
                ),
                f AS (
                    SELECT ticker, COUNT(*) n FROM news_events
                    WHERE severity='red_flag'
                      AND event_date >= CURRENT_DATE - INTERVAL '180 days'
                    GROUP BY ticker
                ),
                p AS (
                    SELECT DISTINCT ON (ticker) ticker, pledged_pct
                    FROM shareholding WHERE pledged_pct IS NOT NULL
                    ORDER BY ticker, quarter_end DESC
                )
                SELECT c.ticker, c.name, s.price, q.signal, q.score, s.rank_mom_12m
                FROM companies c
                JOIN sig s ON s.ticker=c.ticker
                JOIN q     ON q.ticker=c.ticker
                LEFT JOIN f ON f.ticker=c.ticker
                LEFT JOIN p ON p.ticker=c.ticker
                WHERE c.market=%s
                  AND q.score >= 70
                  AND s.rank_mom_12m >= 75
                  AND s.rank_vs_200dma >= 60
                  AND COALESCE(f.n,0) = 0
                  AND COALESCE(p.pledged_pct,0) < 25
                  AND NOT EXISTS (SELECT 1 FROM holdings h WHERE h.ticker=c.ticker)
                ORDER BY q.score * 0.5 + s.rank_mom_12m * 0.5 DESC
                LIMIT %s""", (market, market, limit))
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    sym = "₹" if market == "india" else "$"
    out = ["*💡 Screen: quality + momentum*"]
    for tkr, name, px, grade, score, mom in rows:
        out.append(f"{tkr.replace('.NS','')} {sym}{px:,.0f}  Q:{grade}({score:.0f})  "
                   f"mom top {100-mom:.0f}%")
        if name:
            out.append(f"   _{name[:42]}_")
    out.append("_Historically +12.4% median excess at 12m, beat market 60% "
               "(n=860). Past base rates, not forecasts — run DCF and Quality "
               "before acting._")
    return "\n".join(out)


def digest(dry_run: bool = False, market="india"):
    parts = []
    p = portfolio_digest()
    if p:
        parts.append(p)
    s = market_summary_always()
    if s:
        parts.append(s)
    i = screen_ideas(market=market)
    if i:
        parts.append(i)
    if not parts:
        print("Nothing to report — add holdings first.")
        return
    body = "*MiniTradeIQ daily*\n\n" + "\n\n".join(parts)
    print("\n" + body + "\n")
    if not dry_run:
        send_whatsapp(body)


def market_summary_always():
    """Same as market_summary() but not gated on ALERT_LEVEL — the digest
    always includes it."""
    global ALERT_LEVEL
    prev, ALERT_LEVEL = ALERT_LEVEL, "all"
    try:
        return market_summary()
    finally:
        ALERT_LEVEL = prev


def check(dry_run: bool = False):
    alerts = evaluate_alerts()
    summary = market_summary()

    if not alerts and summary:
        body = f"*MiniTradeIQ*\n\n{summary}\n\nNo changes in your holdings."
        print("\n" + body + "\n")
        if not dry_run:
            send_whatsapp(body)
        return

    if not alerts:
        print("No changes in your holdings today.")
        print("(Silence is the normal state — these alerts fire on events, "
              "not on a schedule.)")
        return

    header = f"*MiniTradeIQ — {len(alerts)} update(s)*\n"
    body = header + "\n\n".join(m for _, _, m in alerts)
    if summary:
        body += "\n\n" + summary
    body += ("\n\n_These are factual changes in companies you hold, not "
             "buy/sell advice. Check the app before acting._")

    print("\n" + body + "\n")
    if dry_run:
        print("(dry run — nothing sent, nothing logged)")
        return
    if send_whatsapp(body):
        log_alerts(alerts)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    args = sys.argv[2:]

    if cmd == "init":
        init_tables()
    elif cmd == "add":
        if len(args) < 2:
            print("Usage: python portfolio.py add TICKER MARKET [QTY] [AVG_COST]")
        else:
            add_holding(args[0], args[1],
                        float(args[2]) if len(args) > 2 else None,
                        float(args[3]) if len(args) > 3 else None)
    elif cmd == "remove":
        remove_holding(args[0]) if args else print("Usage: remove TICKER")
    elif cmd == "list":
        list_holdings()
    elif cmd == "target":
        sa = float(args[args.index("--sell-above") + 1]) if "--sell-above" in args else None
        bb = float(args[args.index("--buy-below") + 1]) if "--buy-below" in args else None
        set_targets(args[0], sa, bb)
    elif cmd == "check":
        check(dry_run="--dry-run" in args)
    elif cmd == "digest":
        mkt = args[args.index("--market") + 1] if "--market" in args else "india"
        digest(dry_run="--dry-run" in args, market=mkt)
    else:
        print(__doc__)
