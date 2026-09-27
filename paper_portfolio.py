"""
MiniTradeIQ — Paper Portfolio
==============================
The app trades its own shortlist with virtual money, so you can find out
whether it makes money before any of yours goes in.

How it stays honest — each of these closes a way paper results usually lie:

  1. Same list you see. It buys from portfolio.candidate_rows(), the exact
     function that builds the shortlist in your digest. It is testing the
     thing you would act on, not a cleaner cousin of it.

  2. No look-ahead. Orders are placed today and FILLED AT THE NEXT PRICE BAR
     dated after the order. Filling at today's stored price would use a
     price recorded before the signal existed — the most common way a
     backtest flatters itself.

  3. Costs are charged. 0.25% per side by default: STT, exchange and SEBI
     charges, stamp duty, GST on brokerage, and some slippage. Small per
     trade, large over a year of weekly rebalancing.

  4. Rules are frozen per book. The rules are stored with the book when it
     starts. Change them and you must start a NEW book under a new name —
     the old one keeps its record. Tweaking rules on a running book until it
     looks good is how every paper trader convinces themselves.

  5. Scored against the market. Every figure is shown next to an equal-weight
     index of the same universe over the same dates, built from the mean of
     per-stock returns.

What it cannot do: prices are weekly, so fills lag the order by up to a
week, and nothing here trades intraday. A result from this book is evidence
about a patient weekly strategy, which is the only kind the data supports.

Usage:
    python paper_portfolio.py init
    python paper_portfolio.py run                 # fill, mark, rebalance if due
    python paper_portfolio.py run --dry-run       # show what it would do
    python paper_portfolio.py run --force         # rebalance even if not due
    python paper_portfolio.py report
    python paper_portfolio.py trades
    python paper_portfolio.py new-book NAME       # start a book under new rules
"""

import sys
import json
import math
from datetime import date, timedelta
from data_store import _conn

PAPER_BUILD = "2026-09-27 (next-bar fills, frozen rules, costs)"

DEFAULT_BOOK = "shortlist-v1"

# The rules for a NEW book. An existing book always uses the rules it was
# created with — changing these values does not touch a running book.
DEFAULT_RULES = {
    "market":            "india",
    "start_capital":     1_000_000,   # ₹10 lakh
    "positions":         10,          # equal weight
    "rebalance_days":    7,
    "keep_if_in_top":    20,          # buffer: held names survive until they
                                      # fall out of the top 20, which stops
                                      # the book churning on small rank moves
    "stop_loss_pct":     -20,         # exit a position down 20% from entry
    "cost_per_side_pct": 0.25,
    "min_quality":       65,
    "min_dcf_upside":    0.0,
}

MAX_FILL_WAIT_DAYS = 21   # an order with no newer price in 3 weeks is void


# ── Schema ───────────────────────────────────────────────────────────────────
def init_tables():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS paper_books (
                    book            TEXT PRIMARY KEY,
                    rules           JSONB NOT NULL,
                    start_date      DATE NOT NULL,
                    cash            DOUBLE PRECISION NOT NULL,
                    last_rebalance  DATE,
                    created_at      TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    id              SERIAL PRIMARY KEY,
                    book            TEXT NOT NULL REFERENCES paper_books(book),
                    ticker          TEXT NOT NULL,
                    status          TEXT NOT NULL,
                        -- pending_buy | open | pending_sell | closed | cancelled
                    order_date      DATE NOT NULL,
                    reserved        DOUBLE PRECISION,
                    entry_date      DATE,
                    entry_price     DOUBLE PRECISION,
                    qty             INTEGER,
                    entry_reason    TEXT,
                    exit_order_date DATE,
                    exit_date       DATE,
                    exit_price      DOUBLE PRECISION,
                    exit_reason     TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_paper_book
                    ON paper_positions(book, status);
                CREATE TABLE IF NOT EXISTS paper_nav (
                    book      TEXT NOT NULL,
                    date      DATE NOT NULL,
                    nav       DOUBLE PRECISION,
                    cash      DOUBLE PRECISION,
                    invested  DOUBLE PRECISION,
                    PRIMARY KEY (book, date)
                );
            """)
        conn.commit()
    finally:
        conn.close()
    print("paper_books, paper_positions and paper_nav tables ready.")


def _get_book(cur, book, create=True):
    cur.execute("""SELECT rules, start_date, cash, last_rebalance
                   FROM paper_books WHERE book=%s""", (book,))
    row = cur.fetchone()
    if row:
        rules = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return {"book": book, "rules": rules, "start_date": row[1],
                "cash": row[2], "last_rebalance": row[3]}
    if not create:
        return None
    rules = dict(DEFAULT_RULES)
    cur.execute("""INSERT INTO paper_books (book, rules, start_date, cash)
                   VALUES (%s,%s,%s,%s)""",
                (book, json.dumps(rules), date.today(), rules["start_capital"]))
    print(f"Started paper book '{book}' with "
          f"₹{rules['start_capital']:,.0f} and these frozen rules:")
    for k, v in rules.items():
        print(f"   {k:<20} {v}")
    return {"book": book, "rules": rules, "start_date": date.today(),
            "cash": float(rules["start_capital"]), "last_rebalance": None}


# ── Prices ───────────────────────────────────────────────────────────────────
def _next_bar(cur, ticker, after):
    """First stored price strictly after `after` — the fill price for an
    order placed on `after`."""
    cur.execute("""SELECT date, price FROM stock_signatures
                   WHERE ticker=%s AND date > %s AND price > 0
                   ORDER BY date ASC LIMIT 1""", (ticker, after))
    return cur.fetchone()


def _last_price(cur, ticker):
    cur.execute("""SELECT date, price FROM stock_signatures
                   WHERE ticker=%s AND price > 0
                   ORDER BY date DESC LIMIT 1""", (ticker,))
    return cur.fetchone()


# ── Pure maths (tested without a database) ───────────────────────────────────
def fill_buy(reserved, price, cost_pct):
    """Integer shares — Indian cash equities do not trade in fractions.
    Returns (qty, cash_returned)."""
    per_share = price * (1 + cost_pct / 100)
    qty = int(math.floor(reserved / per_share)) if per_share > 0 else 0
    return qty, reserved - qty * per_share


def fill_sell(qty, price, cost_pct):
    return qty * price * (1 - cost_pct / 100)


def max_drawdown(navs):
    peak, worst = None, 0.0
    for v in navs:
        if v is None:
            continue
        peak = v if peak is None else max(peak, v)
        if peak:
            worst = min(worst, v / peak - 1)
    return worst


# ── The run ──────────────────────────────────────────────────────────────────
def run(book: str = DEFAULT_BOOK, force: bool = False, dry_run: bool = False):
    from portfolio import candidate_rows

    today = date.today()
    conn = _conn()
    log = []
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '120s'")
            b = _get_book(cur, book)
            R = b["rules"]
            cost = R["cost_per_side_pct"]
            cash = float(b["cash"])

            # 1. Fill pending orders at the next bar after the order date
            cur.execute("""SELECT id, ticker, status, order_date, reserved, qty,
                                  exit_order_date, entry_price
                           FROM paper_positions
                           WHERE book=%s AND status IN ('pending_buy','pending_sell')""",
                        (book,))
            for pid, tkr, st, odate, reserved, qty, xdate, eprice in cur.fetchall():
                short = tkr.replace(".NS", "")
                if st == "pending_buy":
                    bar = _next_bar(cur, tkr, odate)
                    if not bar:
                        if (today - odate).days > MAX_FILL_WAIT_DAYS:
                            cash += reserved
                            cur.execute("""UPDATE paper_positions SET status='cancelled',
                                           exit_reason='no price after order'
                                           WHERE id=%s""", (pid,))
                            log.append(f"✖ {short}: buy cancelled, no price after order")
                        continue
                    q, back = fill_buy(reserved, bar[1], cost)
                    if q == 0:
                        cash += reserved
                        cur.execute("""UPDATE paper_positions SET status='cancelled',
                                       exit_reason='one share costs more than the slot'
                                       WHERE id=%s""", (pid,))
                        log.append(f"✖ {short}: one share exceeds the slot size")
                        continue
                    cash += back
                    cur.execute("""UPDATE paper_positions SET status='open',
                                   entry_date=%s, entry_price=%s, qty=%s
                                   WHERE id=%s""", (bar[0], bar[1], q, pid))
                    log.append(f"✅ Bought {q} {short} @ ₹{bar[1]:,.2f} ({bar[0]})")
                else:
                    bar = _next_bar(cur, tkr, xdate)
                    if not bar:
                        if (today - xdate).days <= MAX_FILL_WAIT_DAYS:
                            continue
                        bar = _last_price(cur, tkr)     # stale: exit at last
                        if not bar:
                            continue
                    proceeds = fill_sell(qty, bar[1], cost)
                    cash += proceeds
                    cur.execute("""UPDATE paper_positions SET status='closed',
                                   exit_date=%s, exit_price=%s WHERE id=%s""",
                                (bar[0], bar[1], pid))
                    pnl = (bar[1] / eprice - 1) * 100 if eprice else 0
                    log.append(f"💱 Sold {qty} {short} @ ₹{bar[1]:,.2f} "
                               f"({pnl:+.1f}% vs entry)")

            # 2. Mark to market
            cur.execute("""SELECT id, ticker, status, reserved, qty, entry_price
                           FROM paper_positions
                           WHERE book=%s AND status IN
                             ('pending_buy','open','pending_sell')""", (book,))
            live = cur.fetchall()
            invested, mark_date = 0.0, None
            marks = {}
            for pid, tkr, st, reserved, qty, eprice in live:
                if st == "pending_buy":
                    invested += reserved or 0
                    continue
                lp = _last_price(cur, tkr)
                if lp:
                    marks[pid] = lp[1]
                    invested += qty * lp[1]
                    mark_date = max(mark_date, lp[0]) if mark_date else lp[0]
            nav = cash + invested
            # Dated by run day so the history is monotonic; the prices inside
            # it are the latest stored bars, which lag by up to a week.
            nav_date = today
            cur.execute("""INSERT INTO paper_nav (book, date, nav, cash, invested)
                           VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT (book, date) DO UPDATE SET
                             nav=EXCLUDED.nav, cash=EXCLUDED.cash,
                             invested=EXCLUDED.invested""",
                        (book, nav_date, nav, cash, invested))

            # 3. Rebalance, if due
            due = (force or b["last_rebalance"] is None or
                   (today - b["last_rebalance"]).days >= R["rebalance_days"])
            if not due:
                nxt = b["last_rebalance"] + timedelta(days=R["rebalance_days"])
                log.append(f"Next rebalance due {nxt}.")
            else:
                keep = candidate_rows(cur, R["market"], exclude=(),
                                      limit=R["keep_if_in_top"],
                                      min_quality=R["min_quality"],
                                      min_dcf_upside=R["min_dcf_upside"])
                keep_set = {c["ticker"] for c in keep}

                # Exits
                exiting = set()
                for pid, tkr, st, reserved, qty, eprice in live:
                    if st != "open":
                        continue
                    short = tkr.replace(".NS", "")
                    px = marks.get(pid)
                    reason = None
                    if px and eprice and (px / eprice - 1) * 100 <= R["stop_loss_pct"]:
                        reason = (f"down {(px/eprice-1)*100:.0f}% from entry "
                                  f"— stop rule ({R['stop_loss_pct']}%)")
                    elif tkr not in keep_set:
                        reason = (f"left the top {R['keep_if_in_top']} of the "
                                  f"shortlist (quality, red flag, DCF or rank)")
                    if reason:
                        exiting.add(pid)
                        cur.execute("""UPDATE paper_positions SET status='pending_sell',
                                       exit_order_date=%s, exit_reason=%s
                                       WHERE id=%s""", (today, reason, pid))
                        log.append(f"🔻 Sell order {short}: {reason}")

                # Entries
                held = {tkr for pid, tkr, st, *_ in live
                        if pid not in exiting and st in ('open', 'pending_buy')}
                slots = R["positions"] - len(held)
                slot_size = nav / R["positions"]
                if slots > 0:
                    for c in keep[:R["positions"] + len(held)]:
                        if slots <= 0 or c["ticker"] in held:
                            continue
                        amount = min(slot_size, cash)
                        if amount < slot_size * 0.5:
                            log.append("Cash below half a slot — waiting for "
                                       "sales to settle before buying more.")
                            break
                        cash -= amount
                        dcf = (f"DCF {c['dcf_upside']:+.0f}%"
                               if c["dcf_upside"] is not None else "no DCF")
                        reason = (f"score {c['composite']:.0f}; "
                                  f"{', '.join(c['reasons'])}; {dcf}")
                        cur.execute("""INSERT INTO paper_positions
                                       (book, ticker, status, order_date,
                                        reserved, entry_reason)
                                       VALUES (%s,%s,'pending_buy',%s,%s,%s)""",
                                    (book, c["ticker"], today, amount, reason))
                        held.add(c["ticker"])
                        slots -= 1
                        log.append(f"🔺 Buy order {c['ticker'].replace('.NS','')} "
                                   f"₹{amount:,.0f} — {reason}")
                if not keep:
                    log.append("Shortlist is empty — nothing qualifies, so the "
                               "book holds cash. That is a valid position.")
                cur.execute("UPDATE paper_books SET last_rebalance=%s WHERE book=%s",
                            (today, book))

            cur.execute("UPDATE paper_books SET cash=%s WHERE book=%s",
                        (cash, book))

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()

    print(f"\nPaper book '{book}'  ·  NAV ₹{nav:,.0f}  ·  cash ₹{cash:,.0f}")
    for line in log:
        print("  " + line)
    if dry_run:
        print("\n(dry run — nothing written)")
    return log


# ── Reporting ────────────────────────────────────────────────────────────────
def _benchmark(cur, market, start, end):
    """Equal-weight index of the universe from `start` to `end`, from the mean
    of per-stock returns — never from the change in an average price."""
    import pandas as pd
    cur.execute("""SELECT ticker, date, price FROM stock_signatures
                   WHERE market=%s AND date >= %s AND date <= %s AND price > 0""",
                (market, start - timedelta(days=14), end))
    rows = cur.fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ticker", "date", "price"])
    return equal_weight_return(df, start)


def equal_weight_return(df, start):
    """Pure function. df has ticker/date/price. The base is the last bar on
    or before `start` — the price the market stood at when the book began."""
    wide = df.pivot_table(index="date", columns="ticker", values="price",
                          aggfunc="last").sort_index()
    if wide.empty:
        return None
    before = [d for d in wide.index if d <= start]
    base = before[-1] if before else wide.index[0]
    wide = wide[wide.index >= base]
    if len(wide) < 2:
        return 0.0
    eq = wide.pct_change().mean(axis=1, skipna=True).fillna(0.0)
    idx = (1 + eq).cumprod()
    return float(idx.iloc[-1] / idx.iloc[0] - 1)


def report_data(book: str = DEFAULT_BOOK):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            b = _get_book(cur, book, create=False)
            if not b:
                return {"error": f"No paper book called '{book}'. "
                                 "Run `python paper_portfolio.py run` to start one."}
            R = b["rules"]
            cur.execute("""SELECT date, nav FROM paper_nav WHERE book=%s
                           ORDER BY date""", (book,))
            navs = cur.fetchall()
            cur.execute("""SELECT ticker, status, entry_date, entry_price, qty,
                                  exit_date, exit_price, entry_reason, exit_reason,
                                  reserved, order_date
                           FROM paper_positions WHERE book=%s
                           ORDER BY COALESCE(entry_date, order_date)""", (book,))
            pos = cur.fetchall()

            open_rows, closed = [], []
            for (tkr, st, ed, ep, q, xd, xp, er, xr, res, od) in pos:
                if st in ("open", "pending_sell"):
                    lp = _last_price(cur, tkr)
                    px = lp[1] if lp else ep
                    open_rows.append({
                        "ticker": tkr, "status": st, "qty": q,
                        "entry_date": str(ed), "entry_price": ep, "price": px,
                        "pnl_pct": round((px / ep - 1) * 100, 1) if ep else None,
                        "value": round(q * px), "why_bought": er,
                        "exit_reason": xr if st == "pending_sell" else None})
                elif st == "pending_buy":
                    open_rows.append({
                        "ticker": tkr, "status": st, "reserved": round(res),
                        "order_date": str(od), "why_bought": er})
                elif st == "closed" and ep and xp:
                    closed.append({
                        "ticker": tkr, "entry_date": str(ed), "exit_date": str(xd),
                        "return_pct": round((xp / ep - 1) * 100, 1),
                        "why_sold": xr})

            start_cap = float(R["start_capital"])
            nav_now = navs[-1][1] if navs else start_cap
            ret = nav_now / start_cap - 1
            first_nav_date = navs[0][0] if navs else b["start_date"]
            last_nav_date = navs[-1][0] if navs else date.today()
            bench = _benchmark(cur, R["market"], first_nav_date, last_nav_date)
    finally:
        conn.close()

    days = (date.today() - b["start_date"]).days
    wins = [c for c in closed if c["return_pct"] > 0]
    if days < 90:
        verdict = (f"Running {days} days. Too early to judge — a quarter is the "
                   "minimum, and a year is what it takes to mean something.")
    elif bench is None:
        verdict = "No benchmark available for the period."
    else:
        ex = (ret - bench) * 100
        verdict = (f"After {days} days the book is "
                   f"{'AHEAD of' if ex > 0 else 'BEHIND'} the market by "
                   f"{abs(ex):.1f} points, after costs.")

    return {
        "book": book, "paper_build": PAPER_BUILD, "rules": R,
        "start_date": str(b["start_date"]), "days_running": days,
        "start_capital": start_cap, "nav": round(nav_now),
        "cash": round(b["cash"]),
        "return_pct": round(ret * 100, 2),
        "benchmark_return_pct": round(bench * 100, 2) if bench is not None else None,
        "excess_pct": round((ret - bench) * 100, 2) if bench is not None else None,
        "max_drawdown_pct": round(max_drawdown([n for _, n in navs]) * 100, 2),
        "closed_trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100) if closed else None,
        "avg_win_pct": round(sum(c["return_pct"] for c in wins) / len(wins), 1) if wins else None,
        "avg_loss_pct": (round(sum(c["return_pct"] for c in closed if c["return_pct"] <= 0)
                               / max(1, len(closed) - len(wins)), 1)
                         if len(closed) > len(wins) else None),
        "positions": open_rows,
        "recent_closed": closed[-10:],
        "nav_history": [{"date": str(d), "nav": round(v)} for d, v in navs],
        "verdict": verdict,
        "how_it_stays_honest": [
            "Buys exactly the shortlist shown in your digest.",
            "Orders fill at the next weekly price after the order — never at a "
            "price recorded before the signal existed.",
            f"{R['cost_per_side_pct']}% charged on every buy and every sell.",
            "Rules are frozen when the book starts; changing them means a new book.",
            "Compared with an equal-weight index of the same universe, same dates.",
        ],
    }


def digest_line(book: str = DEFAULT_BOOK):
    """One line for the phone digest."""
    try:
        d = report_data(book)
    except Exception:
        return None
    if d.get("error"):
        return None
    bench = (f" vs market {d['benchmark_return_pct']:+.1f}%"
             if d["benchmark_return_pct"] is not None else "")
    n_open = sum(1 for p in d["positions"] if p["status"] == "open")
    return (f"🧪 *Paper book* ₹{d['nav']:,} ({d['return_pct']:+.1f}%{bench}, "
            f"after costs) · {n_open} open · day {d['days_running']}")


def print_report(book: str = DEFAULT_BOOK):
    d = report_data(book)
    if d.get("error"):
        print(d["error"])
        return
    print(f"\n{'='*66}\nPAPER BOOK: {d['book']}   (started {d['start_date']}, "
          f"day {d['days_running']})\n{'='*66}")
    print(f"NAV            ₹{d['nav']:,}   (start ₹{d['start_capital']:,.0f})")
    print(f"Return         {d['return_pct']:+.2f}%   after costs")
    if d["benchmark_return_pct"] is not None:
        print(f"Market (EW)    {d['benchmark_return_pct']:+.2f}%")
        print(f"Excess         {d['excess_pct']:+.2f} points")
    print(f"Max drawdown   {d['max_drawdown_pct']:.2f}%")
    if d["closed_trades"]:
        print(f"Closed trades  {d['closed_trades']}  ·  win rate "
              f"{d['win_rate_pct']}%  ·  avg win {d['avg_win_pct']}%  ·  "
              f"avg loss {d['avg_loss_pct']}%")
    print(f"\n{d['verdict']}\n")
    print("Positions:")
    for p in d["positions"]:
        t = p["ticker"].replace(".NS", "")
        if p["status"] == "pending_buy":
            print(f"  ⏳ {t:<12} buy order ₹{p['reserved']:,} placed "
                  f"{p['order_date']} (fills at next price)")
        else:
            flag = "  → selling" if p["status"] == "pending_sell" else ""
            print(f"  {'🟢' if (p['pnl_pct'] or 0) >= 0 else '🔴'} {t:<12}"
                  f"{p['qty']:>6} @ ₹{p['entry_price']:,.1f} → ₹{p['price']:,.1f}"
                  f"  {p['pnl_pct']:+.1f}%{flag}")


def print_trades(book: str = DEFAULT_BOOK):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT ticker, status, order_date, entry_date,
                                  entry_price, qty, exit_date, exit_price,
                                  entry_reason, exit_reason
                           FROM paper_positions WHERE book=%s ORDER BY id""",
                        (book,))
            rows = cur.fetchall()
    finally:
        conn.close()
    for (t, st, od, ed, ep, q, xd, xp, er, xr) in rows:
        t = t.replace(".NS", "")
        line = f"{od}  {st:<12} {t:<12}"
        if ep:
            line += f" in {ed} @ {ep:,.1f}"
        if xp:
            line += f"  out {xd} @ {xp:,.1f}  ({(xp/ep-1)*100:+.1f}%)"
        print(line)
        if er:
            print(f"      why in : {er}")
        if xr:
            print(f"      why out: {xr}")


if __name__ == "__main__":
    a = sys.argv
    cmd = a[1] if len(a) > 1 else "report"
    book = a[a.index("--book") + 1] if "--book" in a else DEFAULT_BOOK
    if cmd == "init":
        init_tables()
    elif cmd == "run":
        init_tables()
        run(book, force="--force" in a, dry_run="--dry-run" in a)
    elif cmd == "report":
        print_report(book)
    elif cmd == "trades":
        print_trades(book)
    elif cmd == "new-book":
        if len(a) < 3:
            print("Usage: python paper_portfolio.py new-book NAME")
        else:
            init_tables()
            conn = _conn()
            try:
                with conn.cursor() as cur:
                    if _get_book(cur, a[2], create=False):
                        print(f"Book '{a[2]}' already exists — its rules are "
                              "frozen. Pick a new name.")
                    else:
                        _get_book(cur, a[2], create=True)
                conn.commit()
            finally:
                conn.close()
    else:
        print(__doc__)
