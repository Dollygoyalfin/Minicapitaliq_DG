"""
ASHOKLEY showing 51-52% pledged is not credible. Rather than guess a third
fix, this dumps EVERY numeric tag inside the promoter context of a real
filing, unfiltered — so the actual encumbrance tags (and whatever tag is
being mismatched) are visible directly.

Usage:  python debug_pledge2.py ASHOKLEY
"""

import sys
import xml.etree.ElementTree as ET
from india_data_pipeline import _nse_get, _nse_get_json, _q

sym = (sys.argv[1] if len(sys.argv) > 1 else "ASHOKLEY").upper().replace(".NS", "")

filings = _nse_get_json(
    "https://www.nseindia.com/api/corporate-share-holdings-master"
    f"?index=equities&symbol={_q(sym)}")
xbrl_url = filings[0]["xbrl"]
print(f"Filing: {filings[0].get('date')}  →  {xbrl_url}\n")

root = ET.fromstring(_nse_get(xbrl_url).content)


def local(t):
    return t.split("}")[-1]


# Find every context whose dimension member mentions "promoter"
promoter_ctx = {}
for el in root.iter():
    if local(el.tag) != "context":
        continue
    cid = el.get("id")
    for ch in el.iter():
        if local(ch.tag) in ("explicitMember", "typedMember"):
            member = (ch.text or "").strip()
            if "promoter" in member.lower():
                promoter_ctx[cid] = member

print(f"Contexts matching 'promoter': {len(promoter_ctx)}")
for cid, member in list(promoter_ctx.items())[:5]:
    print(f"  {cid}  →  {member}")
print()

# Dump EVERY numeric fact in those contexts, unfiltered — no keyword guessing
print("ALL NUMERIC FACTS IN PROMOTER CONTEXTS (unfiltered)")
print("=" * 78)
seen = set()
for el in root.iter():
    cref = el.get("contextRef")
    if cref not in promoter_ctx:
        continue
    tag = local(el.tag)
    txt = (el.text or "").strip()
    if not txt:
        continue
    try:
        val = float(txt)
    except ValueError:
        continue
    key = (tag, cref)
    if key in seen:
        continue
    seen.add(key)
    print(f"  {val:<16} {tag}")
    print(f"       ctx={cref} ({promoter_ctx[cref][:60]})")

print()
print("BOOLEAN / TEXT FACTS mentioning encumber/pledge (any context)")
print("=" * 78)
for el in root.iter():
    tag = local(el.tag)
    if "encumber" not in tag.lower() and "pledg" not in tag.lower():
        continue
    txt = (el.text or "").strip()
    if txt:
        print(f"  {tag:<70} = {txt}   ctx={el.get('contextRef')}")
