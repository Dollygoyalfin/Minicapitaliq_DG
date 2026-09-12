"""
Check promoter encumbrance for one or more companies BY SYMBOL.

The earlier one-liner had two XBRL URLs hardcoded, so changing the printed
label did not change which filing was parsed — every company appeared to
return the same two numbers. This resolves the symbol to its own latest
filing before parsing.

Usage:
    python check_pledge.py RELIANCE
    python check_pledge.py ASHOKLEY VEDL RELIANCE BAJAJHFL VBL
"""

import sys
from india_data_pipeline import _nse_get, _nse_get_json, _q
from news_engine import _parse_shp_xbrl

symbols = [s.upper().replace(".NS", "") for s in sys.argv[1:]] or ["RELIANCE"]

print(f"{'symbol':<12}{'quarter':<13}{'promoter%':>10}{'encumbered%':>13}   basis")
print("-" * 92)

for sym in symbols:
    try:
        filings = _nse_get_json(
            "https://www.nseindia.com/api/corporate-share-holdings-master"
            f"?index=equities&symbol={_q(sym)}")
        if not isinstance(filings, list) or not filings:
            print(f"{sym:<12}no filings returned")
            continue

        latest = filings[0]
        xbrl = latest.get("xbrl")
        qdate = latest.get("date", "?")
        try:
            promoter = float(latest.get("pr_and_prgrp"))
        except (TypeError, ValueError):
            promoter = None

        if not xbrl:
            print(f"{sym:<12}{qdate:<13}{(promoter or 0):>9.2f}%{'no XBRL':>13}")
            continue

        r = _parse_shp_xbrl(_nse_get(xbrl).content)
        pl = r.get("pledged_pct")
        pl_s = f"{pl:>12.2f}%" if pl is not None else f"{'declared':>13}"
        pr_s = f"{promoter:>9.2f}%" if promoter is not None else f"{'—':>10}"
        print(f"{sym:<12}{qdate:<13}{pr_s}{pl_s}   {(r.get('basis') or '')[:44]}")

    except Exception as e:
        print(f"{sym:<12}ERROR: {str(e)[:64]}")

print()
print("Cross-check a few of these against Screener or Trendlyne before")
print("relying on the column — matching one company is not the same as")
print("verifying the parser across different filing styles.")
