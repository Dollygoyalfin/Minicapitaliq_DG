# ── IPO POST-LISTING PERFORMANCE (paste into main.py, below /ipos/base-rates) ─
# Reads what ipo_tracker.py stored — fast, and makes no Yahoo or NSE calls.
#   /ipos/performance                 every tracked mainboard IPO + cohort stats
#   /ipos/performance?min_days=252    only IPOs listed at least a year

@app.get("/ipos/performance")
def get_ipo_performance(min_days: int = Query(0, ge=0),
                        include_sme: bool = Query(False)):
    try:
        from ipo_tracker import report_data
        return report_data(min_days=min_days, include_sme=include_sme)
    except Exception as e:
        return {"error": f"IPO performance unavailable: {e}. "
                         f"Run `python ipo_tracker.py run` first."}
