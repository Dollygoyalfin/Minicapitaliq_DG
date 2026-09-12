"""
Diagnostic: where does NSE publish actual PLEDGE percentages?

The top-corp-info feed gives promoter/public percentages but no pledging.
Pledge data lives in the quarterly shareholding-pattern filing. Those are
filed under SEBI LODR Regulation 31 and — like financial results — may carry
an XBRL attachment, which would be far better than parsing a PDF.

This checks, in order:
  1. the shareholding-pattern filings list (is there an XBRL link?)
  2. if XBRL exists, dumps the pledge-related tags it contains
  3. if only a PDF exists, reports that so we fall back to PDF + Groq

Usage:  python debug_pledge.py RELIANCE
        python debug_pledge.py VEDL        (a company known to pledge)
"""

import sys
import json
import xml.etree.ElementTree as ET
from india_data_pipeline import _nse_get, _nse_get_json, _q

sym = (sys.argv[1] if len(sys.argv) > 1 else "RELIANCE").upper().replace(".NS", "")

CANDIDATES = [
    ("share-holdings-master",
     f"https://www.nseindia.com/api/corporate-share-holdings-master"
     f"?index=equities&symbol={_q(sym)}"),
    ("corporate-shareholding",
     f"https://www.nseindia.com/api/corporate-shareholding"
     f"?index=equities&symbol={_q(sym)}"),
    ("corp-info shareholding",
     f"https://www.nseindia.com/api/top-corp-info?symbol={_q(sym)}&market=equities"),
]

xbrl_urls = []

for label, url in CANDIDATES:
    print("=" * 76)
    print(f"{label}\n{url}")
    print("=" * 76)
    try:
        data = _nse_get_json(url)
    except Exception as e:
        print(f"  FAILED: {str(e)[:120]}\n")
        continue

    rows = data if isinstance(data, list) else (
        data.get("data") if isinstance(data.get("data"), list) else [data])

    print(f"  type={type(data).__name__}  entries={len(rows) if rows else 0}")
    if rows and isinstance(rows[0], dict):
        print(f"  keys on first entry: {list(rows[0].keys())[:18]}\n")
        print(f"  first entry:\n{json.dumps(rows[0], indent=2)[:900]}\n")

    # collect any XBRL / attachment links
    def find_links(obj, depth=0):
        if depth > 4:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and (v.endswith(".xml") or v.endswith(".XML")):
                    xbrl_urls.append(v)
                    print(f"  XBRL LINK  {k}: {v}")
                elif isinstance(v, str) and v.lower().endswith(".pdf"):
                    print(f"  PDF LINK   {k}: {v[:110]}")
                else:
                    find_links(v, depth + 1)
        elif isinstance(obj, list):
            for x in obj[:6]:
                find_links(x, depth + 1)

    find_links(data)
    print()

# ── If we found XBRL, look for pledge tags inside it ────────────────────────
if xbrl_urls:
    print("=" * 76)
    print(f"INSPECTING XBRL: {xbrl_urls[0]}")
    print("=" * 76)
    try:
        root = ET.fromstring(_nse_get(xbrl_urls[0]).content)
        hits = 0
        for el in root.iter():
            tag = el.tag.split("}")[-1]
            tl = tag.lower()
            if any(t in tl for t in ("pledge", "encumber", "promoter",
                                     "shareholding", "percentage")):
                txt = (el.text or "").strip()[:40]
                if txt:
                    print(f"  {tag:<62} = {txt}")
                    hits += 1
            if hits > 45:
                print("  ... (truncated)")
                break
        if hits == 0:
            print("  No pledge/promoter tags found in this XBRL.")
    except Exception as e:
        print(f"  XBRL parse failed: {str(e)[:140]}")
else:
    print("=" * 76)
    print("NO XBRL LINKS FOUND")
    print("=" * 76)
    print("  If only PDF links appeared above, pledge data must come from the")
    print("  shareholding-pattern PDF via the existing PDF + Groq extraction.")
