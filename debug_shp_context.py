"""
Diagnostic: how does the SHP XBRL identify WHICH shareholder category a
number belongs to?

Taking max() across all contexts gave promoter_pct = 100 (that is the Total
row) and pledged_pct = 300 (three category-level percentages summed). The
figures are in the file; the problem is selecting the right context.

This prints, for each fact we care about, the contextRef and whatever
dimension/member that context declares — so the parser can select on the
promoter category explicitly instead of guessing by magnitude.

Usage:  python debug_shp_context.py
"""

import xml.etree.ElementTree as ET
from collections import defaultdict
from india_data_pipeline import _nse_get

FILES = {
    "RELIANCE": "https://nsearchives.nseindia.com/corporate/xbrl/SHP_1694620_16072026072434_WEB.xml",
    "VEDL":     "https://nsearchives.nseindia.com/corporate/xbrl/SHP_1696479_20072026120029_WEB.xml",
}

WANTED = (
    "ShareholdingAsAPercentageOfTotalNumberOfShares",
    "EncumberedSharesHeldAsPercentageOfTotalNumberOfShares",
    "EncumberedShareUnderNonDisposalUndertakingAsPercentageOfTotalNumberOfShares",
    "EncumberedShareUnderOtherEncumbrancesAsPercentageOfTotalNumberOfShares",
    "NumberOfSharesEncumbered",
    "NumberOfFullyPaidUpEquityShares",
    "CategoryOfShareholder",
    "CategoryOfShareholderMember",
)

def local(t):
    return t.split("}")[-1]

for name, url in FILES.items():
    print("=" * 78)
    print(name)
    print("=" * 78)
    root = ET.fromstring(_nse_get(url).content)

    # 1. Map each context id to the dimension members it declares
    ctx_desc = {}
    for el in root.iter():
        if local(el.tag) != "context":
            continue
        cid = el.get("id")
        members = []
        for ch in el.iter():
            lt = local(ch.tag)
            if lt in ("explicitMember", "typedMember"):
                dim = ch.get("dimension", "")
                members.append(f"{local(dim)}={(ch.text or '').strip()}")
        ctx_desc[cid] = " | ".join(members) if members else "(no dimensions)"

    print(f"  total contexts: {len(ctx_desc)}\n")

    # 2. For the facts we care about, show value + context description
    by_tag = defaultdict(list)
    for el in root.iter():
        tag = local(el.tag)
        if tag not in WANTED:
            continue
        txt = (el.text or "").strip()
        if not txt:
            continue
        by_tag[tag].append((el.get("contextRef"), txt))

    for tag in WANTED:
        vals = by_tag.get(tag, [])
        if not vals:
            continue
        print(f"  {tag}  ({len(vals)} facts)")
        for cref, val in vals[:10]:
            print(f"     {val:<14} ctx={cref}")
            print(f"        {ctx_desc.get(cref, '?')[:110]}")
        if len(vals) > 10:
            print(f"     ... {len(vals)-10} more")
        print()
