"""
Diagnostic: what does NSE actually return for shareholding data?

The shareholding fetcher returned 0 rows for all 50 companies, which means
the assumed response shape is wrong. Rather than guess at it a second time,
this dumps the real structure so the parser can be written from evidence.

Usage:  python debug_shareholding.py RELIANCE
"""

import sys
import json
from india_data_pipeline import _nse_get_json, _q

sym = (sys.argv[1] if len(sys.argv) > 1 else "RELIANCE").upper().replace(".NS", "")

ENDPOINTS = [
    ("corp_info section",
     f"https://www.nseindia.com/api/quote-equity?symbol={_q(sym)}&section=corp_info"),
    ("trade_info section",
     f"https://www.nseindia.com/api/quote-equity?symbol={_q(sym)}&section=trade_info"),
    ("plain quote-equity",
     f"https://www.nseindia.com/api/quote-equity?symbol={_q(sym)}"),
    ("corp-info endpoint",
     f"https://www.nseindia.com/api/top-corp-info?symbol={_q(sym)}&market=equities"),
]

for label, url in ENDPOINTS:
    print("=" * 74)
    print(f"{label}\n{url}")
    print("=" * 74)
    try:
        data = _nse_get_json(url)
    except Exception as e:
        print(f"  FAILED: {e}\n")
        continue

    if not isinstance(data, dict):
        print(f"  returned {type(data).__name__}, length {len(data) if hasattr(data,'__len__') else '?'}\n")
        continue

    print(f"  top-level keys: {list(data.keys())}\n")

    # Look for anything shareholding-related at any depth
    def walk(obj, path="", depth=0):
        if depth > 3:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                p = f"{path}.{k}" if path else k
                if any(t in kl for t in ("sharehold", "promoter", "pledge",
                                         "encumb", "fii", "dii", "public")):
                    preview = json.dumps(v)[:260] if not isinstance(v, (int, float)) else v
                    print(f"  MATCH  {p}")
                    print(f"         type={type(v).__name__}  {preview}\n")
                walk(v, p, depth + 1)
        elif isinstance(obj, list) and obj:
            walk(obj[0], f"{path}[0]", depth + 1)

    walk(data)
    print()
