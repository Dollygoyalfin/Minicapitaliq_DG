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
    python portfolio.py digest             # standing position report, always sends
    python portfolio.py digest --dry-run --market all
"""

import os
import sys
import json
from datetime import date, timedelta
from data_store import _conn

PORTFOLIO_BUILD = "2026-09-27 (digest + shortlist + calendar + paper book)"

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

# Minimum DCF upside for a name to appear on the shortlist. Valuation is a
# gate rather than a scoring input: set this to 15 to demand a real margin,
# or to -100 to disable the gate entirely and screen on quality alone.
MIN_DCF_UPSIDE = float(os.environ.get("MIN_DCF_UPSIDE", "0"))

# Upside above this is flagged, not filtered. Chosen on 2026-09-27: keep such
# names in the shortlist and the paper book, so the record shows whether
# extreme DCF upsides turn out right, and score them as their own bucket.
EXTREME_DCF_UPSIDE = float(os.environ.get("EXTREME_DCF_UPSIDE", "100"))


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


def market_summary(force: bool = False):
    """Factual market state — breadth and index move. Not a forecast, and
    deliberately not called 'sentiment': this is what the market DID, which
    is measurable, rather than what it 'feels', which is not.

    `force` bypasses the ALERT_LEVEL gate. The gate keeps event alerts quiet,
    but the digest is something you asked for on purpose, so it always
    carries the market block.
    """
    if not force and ALERT_LEVEL != "all":
        return None
    out = []
    conn = _conn()
    try:
        with conn.cursor() as cur:
            for mkt, label, sym in (("india", "India", "₹"), ("us", "US", "$")):
                # DISTINCT matters: there is one row per ticker per date, so
                # a bare LIMIT 2 returns two rows of the SAME date and the
                # join below then compares every stock against itself — which
                # is exactly 0.0% every time.
                cur.execute("""SELECT DISTINCT date FROM stock_signatures
                               WHERE market=%s ORDER BY date DESC LIMIT 2""",
                            (mkt,))
                dates = [r[0] for r in cur.fetchall()]
                if len(dates) < 2 or dates[0] == dates[1]:
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
                # Breadth must use pct_vs_200dma (the raw price/dma200 - 1),
                # NOT rank_vs_200dma. The rank is a cross-sectional percentile,
                # so "rank >= 50" is true for half the universe by definition
                # and reports 50% on every day in every market.
                cur.execute("""SELECT
                       AVG(CASE WHEN pct_vs_200dma > 0 THEN 1.0 ELSE 0.0 END) * 100,
                       COUNT(pct_vs_200dma)
                     FROM stock_signatures WHERE market=%s AND date=%s""",
                     (mkt, dates[0]))
                breadth, n_breadth = cur.fetchone()
                if not n_breadth:
                    breadth = None
                if move is not None and breadth is not None:
                    # Signatures are sampled weekly, so name the window rather
                    # than letting it read as a day's move.
                    span = (dates[0] - dates[1]).days
                    out.append(
                        f"{label}: {move:+.1f}% equal-weight over {span}d "
                        f"(to {dates[0]}), {breadth:.0f}% of {n_breadth} stocks "
                        f"above their 200-day average")
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
# ── Notification delivery ────────────────────────────────────────────────────
# Channels are tried in order of preference and the first configured one wins.
#
# WhatsApp is deliberately LAST despite being the obvious choice, because it is
# the least reliable for this job. Twilio's WhatsApp sandbox session expires
# after 24 hours of inactivity — a nightly digest would work for a day and then
# silently stop — and production WhatsApp needs a verified business account plus
# pre-approved message templates, which a scheduled digest is not. Telegram has
# none of those constraints: free, no expiry, no verification, and it accepts
# files, which matters later if these alerts ever carry a chart.

def _send_telegram(body: str) -> bool:
    """TELEGRAM_TOKEN + TELEGRAM_CHAT_ID.

    Setup, about two minutes: message @BotFather on Telegram, send /newbot,
    copy the token. Then message your new bot once and open
    https://api.telegram.org/bot<TOKEN>/getUpdates to read your chat id.
    """
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (token and chat):
        return False
    try:
        import httpx
        with httpx.Client(timeout=30.0) as client:
            r = client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat,
                                  "text": body[:4000],
                                  "parse_mode": "Markdown",
                                  "disable_web_page_preview": True})
        if r.status_code == 200:
            print("  Telegram sent")
            return True
        # Markdown is strict; a stray underscore in a company name breaks it.
        with httpx.Client(timeout=30.0) as client:
            r = client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat, "text": body[:4000]})
        if r.status_code == 200:
            print("  Telegram sent (plain text)")
            return True
        print(f"  Telegram failed: HTTP {r.status_code} {r.text[:140]}")
    except Exception as e:
        print(f"  Telegram error: {str(e)[:140]}")
    return False


def _send_ntfy(body: str) -> bool:
    """NTFY_TOPIC — the simplest option available: no account at all.

    Pick an unguessable topic name, install the ntfy app, subscribe to it.
    Anyone who knows the topic name can post to it, so treat it as a secret.
    """
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        return False
    try:
        import httpx
        with httpx.Client(timeout=30.0) as client:
            r = client.post(f"https://ntfy.sh/{topic}",
                            content=body[:3500].encode("utf-8"),
                            headers={"Title": "MiniTradeIQ",
                                     "Tags": "chart_with_upwards_trend"})
        if r.status_code in (200, 201):
            print("  ntfy sent")
            return True
        print(f"  ntfy failed: HTTP {r.status_code}")
    except Exception as e:
        print(f"  ntfy error: {str(e)[:140]}")
    return False


def _send_email(body: str) -> bool:
    """SMTP_USER + SMTP_PASS + EMAIL_TO. For Gmail, SMTP_PASS must be an App
    Password, not the account password."""
    user = os.environ.get("SMTP_USER", "")
    pw   = os.environ.get("SMTP_PASS", "")
    to   = os.environ.get("EMAIL_TO", "") or user
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    if not (user and pw and to):
        return False
    try:
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = "MiniTradeIQ update"
        msg["From"], msg["To"] = user, to
        msg.set_content(body)
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        print("  email sent")
        return True
    except Exception as e:
        print(f"  email error: {str(e)[:140]}")
    return False


def _send_whatsapp(body: str) -> bool:
    """TWILIO_SID + TWILIO_TOKEN + TWILIO_WHATSAPP_FROM + MY_WHATSAPP_TO.

    Kept for completeness. Note the sandbox session expires after 24 hours of
    inactivity, so a nightly job will stop delivering without warning unless
    you re-join or move to an approved WhatsApp Business template.
    """
    sid   = os.environ.get("TWILIO_SID", "")
    token = os.environ.get("TWILIO_TOKEN", "")
    src   = os.environ.get("TWILIO_WHATSAPP_FROM", "")
    dst   = os.environ.get("MY_WHATSAPP_TO", "")
    if not all([sid, token, src, dst]):
        return False
    try:
        import httpx
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                auth=(sid, token),
                data={"From": src, "To": dst, "Body": body[:1500]})
        if r.status_code in (200, 201):
            print("  WhatsApp sent")
            return True
        print(f"  WhatsApp failed: HTTP {r.status_code} {r.text[:160]}")
    except Exception as e:
        print(f"  WhatsApp error: {str(e)[:140]}")
    return False


_CHANNELS = [("Telegram", _send_telegram),
             ("ntfy",     _send_ntfy),
             ("email",    _send_email),
             ("WhatsApp", _send_whatsapp)]


def send_alert(body: str) -> bool:
    """Deliver through every CONFIGURED channel, not just the first.

    Sending to all of them is deliberate: a channel that silently stops
    working is the failure mode that matters here, and a second channel is
    how you notice. Returns True if at least one delivery succeeded.
    """
    sent_any = False
    for name, fn in _CHANNELS:
        try:
            if fn(body):
                sent_any = True
        except Exception as e:
            print(f"  {name} raised: {str(e)[:100]}")
    if not sent_any:
        print("  No notification channel configured. Set TELEGRAM_TOKEN +"
              " TELEGRAM_CHAT_ID (recommended), NTFY_TOPIC, SMTP_USER +"
              " SMTP_PASS, or the four TWILIO_* variables.")
    return sent_any


# Kept so existing calls keep working
def send_whatsapp(body: str) -> bool:
    return send_alert(body)


def check(dry_run: bool = False):
    alerts = evaluate_alerts()
    summary = market_summary()

    if not alerts and summary:
        body = f"*MiniTradeIQ*\n\n{summary}\n\nNo changes in your holdings."
        print("\n" + body + "\n")
        if not dry_run:
            send_alert(body)
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
    if send_alert(body):
        log_alerts(alerts)


# ── Recommendations ──────────────────────────────────────────────────────────
# Two rules govern everything below.
#
# 1. Every suggestion shows its reason. A bare "BUY RELIANCE" is unfalsifiable;
#    "quality A (82), 12m momentum in the top quartile, DCF says +31%" can be
#    argued with, and arguing with it is how you avoid acting on a bad one.
#
# 2. Every batch carries the track record. The scorecard line is computed from
#    signal_journal, not written by hand, so it gets better as the journal
#    matures and it cannot flatter itself. While the sample is thin it says so.

def _track_record(cur, min_n: int = 20):
    """One honest line about whether this app's signals have worked yet."""
    cur.execute("SELECT COUNT(*) FROM signal_journal")
    total = cur.fetchone()[0] or 0
    cur.execute("""SELECT COUNT(*) FROM signal_journal
                   WHERE signal_date <= CURRENT_DATE - INTERVAL '90 days'""")
    mature = cur.fetchone()[0] or 0
    if total == 0:
        return ("_No signals recorded yet, so these suggestions have no track "
                "record behind them._")
    if mature < min_n:
        return (f"_{total:,} signals recorded, {mature} of them old enough to "
                f"score. That is below the {min_n} needed to say anything about "
                f"whether they work. Treat the list as a shortlist to research, "
                f"not as a verdict._")
    return (f"_{total:,} signals recorded, {mature:,} scoreable. Run "
            f"`python signal_journal.py score` for the hit rate — it is the "
            f"honest record and is not filtered to look good._")


def review_holdings(cur, holdings):
    """Things you own that warrant a look, and why."""
    out = []
    for tkr, hmkt, qty, cost in holdings:
        short, reasons = tkr.replace(".NS", ""), []

        cur.execute("""SELECT signal, detail FROM signal_journal
                       WHERE ticker=%s AND source IN ('dcf','convergence')
                       ORDER BY signal_date DESC LIMIT 1""", (tkr,))
        row = cur.fetchone()
        if row and row[0]:
            sig = row[0]
            if "Sell" in sig:
                reasons.append(f"valuation models say {sig}")
            elif "Buy" in sig:
                reasons.append(f"valuation models say {sig}")

        cur.execute("""SELECT signal FROM signal_journal WHERE ticker=%s
                       AND source='quality' ORDER BY signal_date DESC LIMIT 2""",
                    (tkr,))
        qg = [r[0] for r in cur.fetchall()]
        order = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}
        if len(qg) == 2 and order.get(qg[0], 9) > order.get(qg[1], 9):
            reasons.append(f"quality fell {qg[1]}→{qg[0]}")

        cur.execute("""SELECT COUNT(*) FROM news_events WHERE ticker=%s
                       AND severity='red_flag'
                       AND event_date >= CURRENT_DATE - INTERVAL '90 days'""",
                    (tkr,))
        nflag = cur.fetchone()[0] or 0
        if nflag:
            reasons.append(f"{nflag} red flag{'s' if nflag > 1 else ''} in 90d")

        if cost:
            cur.execute("""SELECT price FROM stock_signatures WHERE ticker=%s
                           AND price IS NOT NULL ORDER BY date DESC LIMIT 1""",
                        (tkr,))
            p = cur.fetchone()
            if p and p[0] and (p[0] / cost - 1) * 100 <= -20:
                reasons.append(f"{(p[0]/cost-1)*100:.0f}% below your cost")

        if reasons:
            out.append(f"• *{short}* — " + "; ".join(reasons))
    return out


def candidate_rows(cur, market: str, exclude=(), limit: int = 5,
                   min_quality: int = 65, min_dcf_upside: float = 0.0,
                   dcf_max_age_days: int = 45, require_dcf: bool = False):
    """The shortlist as data. Both the digest and the paper portfolio call
    this, so the paper book trades exactly the list you are shown — testing
    anything else would prove nothing about the list you act on.

    Highest-scoring names not in `exclude`.

    The composite mirrors /ideas: quality 45%, 12-month momentum 30%, trend
    15%, low volatility 10%. Weights are a judgement, not a discovery — they
    are shown so you can disagree with them.

    Valuation is a FILTER, not a score component, and that distinction is the
    whole point. Folded into a weighted score, a DCF saying "40% overvalued"
    could be cancelled out by strong momentum, and the list would happily
    recommend expensive things that are going up — which is how momentum
    screens hurt people. As a filter it cannot be outvoted.

    A stale DCF is treated as no DCF. A verdict from before the last set of
    model fixes is not evidence about today's price.
    """
    mkts = ("india", "us") if market == "all" else (market,)
    cur.execute("""
        WITH latest AS (
          SELECT DISTINCT ON (ticker) ticker, market, price,
                 rank_vs_200dma, rank_mom_12m, rank_volatility, rank_from_low
          FROM stock_signatures ORDER BY ticker, date DESC
        ), qual AS (
          SELECT DISTINCT ON (ticker) ticker, signal AS grade,
                 (detail->>'score')::float AS score
          FROM signal_journal WHERE source='quality'
          ORDER BY ticker, signal_date DESC
        ), val AS (
          SELECT DISTINCT ON (ticker) ticker,
                 (detail->>'upside_pct')::float AS upside,
                 signal_date
          FROM signal_journal
          WHERE source='dcf'
            AND signal_date >= CURRENT_DATE - (%s || ' days')::interval
          ORDER BY ticker, signal_date DESC
        ), flags AS (
          SELECT ticker, COUNT(*) AS n FROM news_events
          WHERE severity='red_flag'
            AND event_date >= CURRENT_DATE - INTERVAL '180 days'
          GROUP BY ticker
        )
        SELECT c.ticker, c.name, c.sector, q.grade, q.score, l.price,
               l.rank_mom_12m, l.rank_vs_200dma, l.rank_volatility,
               v.upside,
               (  q.score * 0.45
                + COALESCE(l.rank_mom_12m, 50) * 0.30
                + COALESCE(l.rank_vs_200dma, 50) * 0.15
                + (100 - COALESCE(l.rank_volatility, 50)) * 0.10) AS composite
        FROM companies c
        JOIN latest l ON l.ticker = c.ticker
        JOIN qual   q ON q.ticker = c.ticker
        LEFT JOIN val   v ON v.ticker = c.ticker
        LEFT JOIN flags f ON f.ticker = c.ticker
        WHERE l.market = ANY(%s)
          AND q.score >= %s
          AND COALESCE(f.n, 0) = 0
          AND (v.upside IS NULL OR v.upside >= %s)
          AND (%s = FALSE OR v.upside IS NOT NULL)
        ORDER BY (v.upside IS NULL), composite DESC
        LIMIT %s
    """, (dcf_max_age_days, list(mkts), min_quality, min_dcf_upside,
          require_dcf, limit + len(exclude or ()) + 5))

    exclude = set(exclude or ())
    out = []
    for (tkr, name, sec, grade, score, price, mom, trend, vol, upside,
         comp) in cur.fetchall():
        if tkr in exclude or not price:
            continue
        why = [f"quality {grade} ({int(score)})"]
        if mom is not None and mom >= 70:
            why.append(f"12m momentum top {100-int(mom)}%")
        if trend is not None and trend >= 60:
            why.append("above trend")
        if vol is not None and vol <= 35:
            why.append("low volatility")
        out.append({"ticker": tkr, "name": name, "sector": sec,
                    "grade": grade, "quality": score, "price": float(price),
                    "dcf_upside": upside, "composite": float(comp),
                    "reasons": why})
        if len(out) >= limit:
            break
    return out


def find_candidates(cur, market: str, exclude, limit: int = 5,
                    min_quality: int = 65, min_dcf_upside: float = 0.0,
                    dcf_max_age_days: int = 45):
    """The shortlist, formatted for a message."""
    out = []
    for c in candidate_rows(cur, market, exclude, limit, min_quality,
                            min_dcf_upside, dcf_max_age_days):
        tkr = c["ticker"]
        short = tkr.replace(".NS", "")
        sym = "₹" if tkr.endswith(".NS") else "$"
        up = c["dcf_upside"]
        if up is None:
            val = "⚠ no recent DCF"
        elif up > EXTREME_DCF_UPSIDE:
            # Kept on the list by choice, but a quality company worth 2-3x
            # its price is more often a model error (recent IPO growth
            # extrapolated, cyclical peak margins) than a real bargain.
            val = f"⚠ DCF {up:+.0f}% — implausibly high, check the model"
        else:
            val = f"DCF {up:+.0f}%"
        name = (c["name"] or "").strip()
        label = f" — {name[:28]}" if name and name.upper() != short.upper() else ""
        out.append(f"• *{short}*{label}  {sym}{c['price']:,.0f}\n"
                   f"   {', '.join(c['reasons'])}\n"
                   f"   {val}  ·  score {c['composite']:.0f}/100")
    return out


def upcoming_for_digest(cur, held_tickers, days: int = 14):
    """Scheduled events for what you own, plus market-wide items rare enough
    to always matter (index changes). Everything else in the calendar lives
    in the app, not on your phone."""
    from events_calendar import KIND_LABELS
    lines = []
    if held_tickers:
        cur.execute("""SELECT ticker, event_date, kind, title, certainty
                       FROM event_calendar
                       WHERE ticker = ANY(%s) AND event_date >= CURRENT_DATE
                         AND event_date <= CURRENT_DATE + (%s || ' days')::interval
                       ORDER BY event_date""", (list(held_tickers), days))
        for t, d, kind, title, cert in cur.fetchall():
            est = " _(est.)_" if cert == "estimate" else ""
            lines.append(f"• {d:%d %b} — *{t.replace('.NS','')}* "
                         f"{KIND_LABELS.get(kind, kind)}{est}")

    # Strongest candidates first, weakest members first — and capped per side,
    # so a long list of additions cannot crowd the exclusions out of view.
    cur.execute("""SELECT ticker, event_date, kind FROM event_calendar
                   WHERE kind IN ('index_add_candidate','index_drop_risk')
                     AND event_date >= CURRENT_DATE
                   ORDER BY kind,
                     CASE WHEN kind='index_add_candidate'
                          THEN -(detail->>'ratio_to_smallest')::float
                          ELSE (detail->>'ffmcap_cr')::float END""")
    idx = cur.fetchall()
    if idx:
        adds = [t.replace('.NS', '') for t, _, k in idx
                if k == "index_add_candidate"][:4]
        drops = [t.replace('.NS', '') for t, _, k in idx
                 if k == "index_drop_risk"][:4]
        eff = idx[0][1]
        s = f"• Nifty 50 review (effective ~{eff:%b %Y}, _estimate_): "
        if adds:
            s += "in? " + ", ".join(adds)
        if drops:
            s += ("  ·  " if adds else "") + "out? " + ", ".join(drops)
        lines.append(s)

    return ("*Coming up*\n" + "\n".join(lines)) if lines else None


def digest(dry_run: bool = False, market: str = "india"):
    """A scheduled snapshot of where your holdings stand.

    The difference from `check` is deliberate. `check` is event-driven: it
    stays silent unless something actually happened, which is what makes it
    worth reading when it does fire. `digest` always sends, because you asked
    for a standing report — so it must earn the interruption by being a
    position summary rather than a list of prices you could see anywhere.

    Nothing here is a recommendation. It reports cost versus market, what
    moved, and any red flag already recorded against a company you hold.

    market: "india", "us", or "all".
    """
    mkt = (market or "india").lower()
    sections = []
    stale_banner = None

    conn = _conn()
    try:
        with conn.cursor() as cur:
            if mkt == "all":
                cur.execute("""SELECT ticker, market, quantity, avg_cost
                               FROM holdings ORDER BY market, ticker""")
            else:
                cur.execute("""SELECT ticker, market, quantity, avg_cost
                               FROM holdings WHERE market=%s
                               ORDER BY ticker""", (mkt,))
            holdings = cur.fetchall()

            if not holdings:
                scope = "any market" if mkt == "all" else mkt
                print(f"No holdings recorded for {scope}.")
                print("Add one:  python portfolio.py add RELIANCE india 100 1250")
                return

            lines, movers, flags = [], [], []
            tot_cost = tot_value = 0.0
            priced = 0
            unpriced_with_cost = []
            price_dates = []

            for tkr, hmkt, qty, cost in holdings:
                short = tkr.replace(".NS", "")
                sym = "₹" if hmkt == "india" else "$"

                # Daily closes first: they are the freshest thing we hold and
                # they cover ETFs and new listings that have no signatures.
                # Weekly signatures only as a fallback.
                cur.execute("""SELECT date, close FROM price_history
                               WHERE ticker=%s AND close > 0
                               ORDER BY date DESC LIMIT 2""", (tkr,))
                px_rows = cur.fetchall()
                if not px_rows:
                    cur.execute("""SELECT date, price FROM stock_signatures
                                   WHERE ticker=%s AND price IS NOT NULL
                                   ORDER BY date DESC LIMIT 2""", (tkr,))
                    px_rows = cur.fetchall()
                if not px_rows:
                    lines.append(f"• *{short}* — no price on file")
                    if qty and cost:
                        unpriced_with_cost.append((short, qty * cost))
                    continue

                px = px_rows[0][1]
                price_dates.append(px_rows[0][0])
                priced += 1

                # Change since the previous stored close (a day, normally)
                chg = None
                if len(px_rows) > 1 and px_rows[1][1]:
                    chg = (px / px_rows[1][1] - 1) * 100

                if qty and cost:
                    value = px * qty
                    spent = cost * qty
                    tot_value += value
                    tot_cost += spent
                    pl_pct = (px / cost - 1) * 100
                    mark = "🟢" if pl_pct >= 0 else "🔴"
                    lines.append(
                        f"{mark} *{short}*  {sym}{px:,.0f}  "
                        f"({pl_pct:+.1f}% vs {sym}{cost:,.0f} cost)")
                else:
                    move = f"  {chg:+.1f}%" if chg is not None else ""
                    lines.append(f"• *{short}*  {sym}{px:,.0f}{move}")

                if chg is not None and abs(chg) >= BIG_MOVE_1D:
                    movers.append((abs(chg), f"{short} {chg:+.1f}%"))

                cur.execute("""SELECT event_date, category FROM news_events
                               WHERE ticker=%s AND severity='red_flag'
                                 AND event_date >= CURRENT_DATE - INTERVAL '30 days'
                               ORDER BY event_date DESC LIMIT 2""", (tkr,))
                for ev_date, cat in cur.fetchall():
                    flags.append(f"🔴 {short} — {cat.replace('_',' ')} ({ev_date})")

            # Say how old the prices are. A P&L without a date is a claim
            # about now, and stale prices must never be passed off as current.
            as_of = max(price_dates) if price_dates else None
            if as_of:
                age = (date.today() - as_of).days
                head = f"*Holdings* _(closes as of {as_of:%d %b})_"
                if age > 4:
                    stale_banner = (f"⚠️ *Prices are {age} days old* — last close "
                                    f"{as_of:%d %b}. The nightly price update is "
                                    f"failing, so every figure below is out of "
                                    f"date. Check the 'Refresh daily prices' step.")
            else:
                head = "*Holdings*"
            sections.append(head + "\n" + "\n".join(lines))

            if tot_cost > 0:
                pl = (tot_value / tot_cost - 1) * 100
                csym = "₹" if mkt == "india" else "$"
                pos = (f"*Position*\nCost {csym}{tot_cost:,.0f} → "
                       f"now {csym}{tot_value:,.0f}  (*{pl:+.1f}%*)")
                if unpriced_with_cost:
                    left = sum(v for _, v in unpriced_with_cost)
                    pos += (f"\n_Excludes {len(unpriced_with_cost)} holding(s) "
                            f"with no price ({', '.join(n for n, _ in unpriced_with_cost)}"
                            f"; cost {csym}{left:,.0f})._")
                sections.append(pos)
            elif priced:
                sections.append(
                    "_Quantity and cost are not recorded, so there is no "
                    "profit figure. Add them with:_\n"
                    "`portfolio.py add TICKER MARKET QTY COST`")

            if movers:
                movers.sort(reverse=True)
                sections.append("*Moved most*\n" +
                                "\n".join(m for _, m in movers[:5]))

            if flags:
                sections.append("*Red flags on file (30 days)*\n" +
                                "\n".join(flags[:6]))

            # ── Recommendations ──────────────────────────────────────────
            try:
                held = {h[0] for h in holdings}
                review = review_holdings(cur, holdings)
                if review:
                    sections.append("*Worth a look — what you own*\n" +
                                    "\n".join(review))

                cands = find_candidates(cur, mkt, held, limit=5,
                                        min_dcf_upside=MIN_DCF_UPSIDE)
                if cands:
                    sections.append(
                        "*Shortlist — what you don't own*\n" +
                        "\n".join(cands) +
                        f"\n_Filters: quality 65+, no red flag in 180d, "
                        f"DCF upside ≥ {MIN_DCF_UPSIDE:.0f}%._")
                else:
                    sections.append(
                        "*Shortlist*\n_Nothing cleared the filters: quality "
                        f"65+, no red flag in 180d, DCF upside ≥ "
                        f"{MIN_DCF_UPSIDE:.0f}%._")

                record = _track_record(cur)
                if record:
                    sections.append(record)
            except Exception as e:
                # A failed query aborts the whole Postgres transaction; without
                # this rollback every section after it would fail silently too.
                conn.rollback()
                sections.append(f"_Shortlist unavailable: {str(e)[:120]}_")

            # ── Coming up: the part of the future that is scheduled ─────
            try:
                cal = upcoming_for_digest(cur, [h[0] for h in holdings])
                if cal:
                    sections.append(cal)
            except Exception:
                conn.rollback()   # calendar tables may not exist yet
    finally:
        conn.close()

    try:
        from paper_portfolio import digest_line
        pl = digest_line()
        if pl:
            sections.append(pl)
    except Exception:
        pass

    mkt_block = market_summary(force=True)
    if mkt_block:
        sections.append(mkt_block)

    # The nightly job runs at 22:30 UTC, which is 4 AM IST the NEXT day, so
    # a UTC date would greet you with yesterday. Stamp it in IST.
    from datetime import datetime, timezone
    ist_today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
    if stale_banner:
        sections.insert(0, stale_banner)
    body = (f"*MiniTradeIQ — {ist_today:%d %b %Y}*\n\n" +
            "\n\n".join(sections) +
            "\n\n_Shortlist = screening output, not advice. Every name still "
            "needs the DCF and the filings read before you act._")

    print("\n" + body + "\n")
    if dry_run:
        print("(dry run — nothing sent)")
        return
    send_alert(body)


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
