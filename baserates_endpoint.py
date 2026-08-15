# ── BASE RATES ENDPOINT (paste into main.py) ─────────────────────────────────
# Serves historical base rates: "what happened to stocks that looked like this
# one does today?" Reads pre-computed signatures from the store — no heavy
# computation at request time.
#
# Honest by construction: excess returns vs an equal-weight market proxy
# (so a bull market can't masquerade as signal), independent episodes only
# (60-day separation), and refusal to conclude below 15 episodes.

@app.get("/baserates")
def get_base_rates(
    ticker: str = Query(...),
    market: str = Query("us"),
    band: int = Query(7, description="± percentile band width for matching"),
):
    try:
        from data_store import _conn
        import numpy as np
        import pandas as pd

        MIN_EPISODES    = 15
        MIN_EPISODE_GAP = 60
        MAX_SELECTIVITY = 0.15
        HORIZONS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}

        raw = ticker.upper()
        if market.lower() == "india" and not raw.endswith(".NS"):
            raw += ".NS"

        # ── 1. This stock's current signature ────────────────────────────────
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT date, rank_vs_200dma, rank_from_high,
                                      rank_from_low, rank_mom_3m, rank_mom_12m,
                                      rank_volatility, price
                               FROM stock_signatures WHERE ticker = %s
                               ORDER BY date DESC LIMIT 1""", (raw,))
                row = cur.fetchone()
        if not row:
            return {"error": f"No price-signature history available for {raw}. "
                             f"Base rates require at least 200 trading days of "
                             f"price history in the store."}

        sig_date, r200, rhigh, rlow, m3, m12, rvol, price = row
        signature = {
            "as_of": str(sig_date),
            "price": price,
            "percentiles": {
                "vs_200dma":      round(r200, 0) if r200 is not None else None,
                "from_52wk_high": round(rhigh, 0) if rhigh is not None else None,
                "from_52wk_low":  round(rlow, 0) if rlow is not None else None,
                "momentum_3m":    round(m3, 0) if m3 is not None else None,
                "momentum_12m":   round(m12, 0) if m12 is not None else None,
                "volatility":     round(rvol, 0) if rvol is not None else None,
            },
        }

        if r200 is None or rlow is None or m12 is None:
            return {"error": "Signature incomplete for this stock — "
                             "insufficient price history.",
                    "signature": signature}

        # ── 2. Find matching historical days ─────────────────────────────────
        def bnd(v):
            return (max(0, v - band), min(100, v + band))

        flo, fhi = bnd(rlow)
        dlo, dhi = bnd(r200)
        mlo, mhi = bnd(m12)

        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '120s'")
                cur.execute("SELECT COUNT(*) FROM stock_signatures")
                total_days = cur.fetchone()[0]
                cur.execute("""
                    SELECT ticker, date FROM stock_signatures
                    WHERE market = %s
                      AND rank_from_low  BETWEEN %s AND %s
                      AND rank_vs_200dma BETWEEN %s AND %s
                      AND rank_mom_12m   BETWEEN %s AND %s
                """, (market, flo, fhi, dlo, dhi, mlo, mhi))
                matches = cur.fetchall()

        if not matches:
            return {"signature": signature,
                    "error": "No comparable historical setups found."}

        match_df = pd.DataFrame(matches, columns=["ticker", "date"])
        match_df["date"] = pd.to_datetime(match_df["date"])
        selectivity = len(match_df) / max(total_days, 1)
        tickers = list(set(match_df["ticker"]))

        # ── 3. Prices for matched tickers + market benchmark ─────────────────
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '120s'")
                cur.execute("""SELECT ticker, date, price FROM stock_signatures
                               WHERE ticker = ANY(%s)""", (tickers,))
                prows = cur.fetchall()
                cur.execute("""SELECT date, AVG(price) FROM stock_signatures
                               WHERE market = %s GROUP BY date""", (market,))
                brows = cur.fetchall()

        prices = pd.DataFrame(prows, columns=["ticker", "date", "price"])
        prices["date"] = pd.to_datetime(prices["date"])
        prices = prices.sort_values(["ticker", "date"])

        bench = pd.DataFrame(brows, columns=["date", "avg_price"])
        bench["date"] = pd.to_datetime(bench["date"])
        bench = bench.sort_values("date")
        bench["idx"] = (1 + bench["avg_price"].pct_change().fillna(0)).cumprod()
        bmap = dict(zip(bench["date"], bench["idx"]))

        # ── 4. Episodes → forward excess returns ─────────────────────────────
        results = {h: [] for h in HORIZONS}
        episode_count = 0

        for tkr, g in match_df.groupby("ticker"):
            series = prices[prices["ticker"] == tkr].reset_index(drop=True)
            if series.empty:
                continue
            pos = {d: i for i, d in enumerate(series["date"])}
            episodes, last = [], None
            for d in sorted(g["date"]):
                i = pos.get(d)
                if i is None:
                    continue
                if last is None or (i - last) >= MIN_EPISODE_GAP:
                    episodes.append(i)
                    last = i
            for start in episodes:
                episode_count += 1
                p0 = series["price"].iloc[start]
                b0 = bmap.get(series["date"].iloc[start])
                for hlabel, hdays in HORIZONS.items():
                    end = start + hdays
                    if end >= len(series) or not p0 or p0 <= 0:
                        continue
                    p1 = series["price"].iloc[end]
                    b1 = bmap.get(series["date"].iloc[end])
                    if b0 and b1 and b0 > 0:
                        results[hlabel].append((p1 / p0 - 1) - (b1 / b0 - 1))

        # ── 5. Honest summary ────────────────────────────────────────────────
        horizons_out = {}
        for hlabel, vals in results.items():
            n = len(vals)
            if n < MIN_EPISODES:
                horizons_out[hlabel] = {
                    "n_episodes": n, "confidence": "insufficient",
                    "message": f"Only {n} independent episodes "
                               f"(need {MIN_EPISODES}) — no conclusion drawn."}
                continue
            a = np.array(vals)
            horizons_out[hlabel] = {
                "n_episodes": n,
                "confidence": "low" if n < 30 else "moderate" if n < 75 else "adequate",
                "median_excess_pct":  round(float(np.median(a)) * 100, 1),
                "beat_market_pct":    round(float((a > 0).mean()) * 100, 1),
                "p25_pct":            round(float(np.percentile(a, 25)) * 100, 1),
                "p75_pct":            round(float(np.percentile(a, 75)) * 100, 1),
            }

        caveats = [
            "Returns shown are EXCESS vs an equal-weight market average — "
            "they strip out general market movement.",
            "Episodes are independent occurrences (minimum 60 trading days "
            "apart for the same stock), not individual matching days.",
            "Universe is today's index constituents — companies that were "
            "delisted or removed are absent (survivorship bias).",
            "History covers roughly 5 years: one market regime, not many. "
            "These are base rates for context, not predictions.",
        ]
        if selectivity > MAX_SELECTIVITY:
            caveats.insert(0, f"This setup matches {selectivity*100:.1f}% of all "
                              f"stock-days — broad enough that it describes the "
                              f"market more than a specific condition.")

        return {
            "ticker": raw,
            "market": market,
            "signature": signature,
            "match_criteria": {
                "band_pp": band,
                "from_52wk_low":  [round(flo), round(fhi)],
                "vs_200dma":      [round(dlo), round(dhi)],
                "momentum_12m":   [round(mlo), round(mhi)],
            },
            "selectivity_pct": round(selectivity * 100, 2),
            "total_episodes":  episode_count,
            "horizons":        horizons_out,
            "caveats":         caveats,
        }

    except Exception as e:
        return {"error": f"Base-rate analysis failed: {e}"}
