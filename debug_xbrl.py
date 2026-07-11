"""Debug: dump contexts + locate P&L facts in an NSE XBRL file.
Usage: python debug_xbrl.py RELIANCE
"""
import sys
import xml.etree.ElementTree as ET
from india_data_pipeline import fetch_annual_filings, _nse_get, _parse_date, _local

sym = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
filings = fetch_annual_filings(sym)
fy = sorted(filings.keys(), reverse=True)[0]
fi = filings[fy]
period_end = _parse_date(fi["to_date"])
print(f"FY{fy}  period_end={period_end}  xbrl={fi['xbrl'][:80]}...")

root = ET.fromstring(_nse_get(fi["xbrl"]).content)

# ── 1) All contexts ────────────────────────────────────────────────────────────
print("\n=== CONTEXTS (first 40) ===")
contexts = {}
count = 0
for el in root.iter():
    if _local(el.tag) != "context":
        continue
    cid = el.get("id")
    start = end = instant = None
    for child in el.iter():
        lc = _local(child.tag)
        if lc == "startdate":  start   = (child.text or "").strip()
        elif lc == "enddate":  end     = (child.text or "").strip()
        elif lc == "instant":  instant = (child.text or "").strip()
    contexts[cid] = (start, end, instant)
    if count < 40:
        print(f"  {cid:<25} start={start} end={end} instant={instant}")
        count += 1
print(f"  (total contexts: {len(contexts)})")

# ── 2) Where do revenue/income/profit facts live? ─────────────────────────────
print("\n=== FACTS whose name contains revenue/income/profit/expense (first 30) ===")
shown = 0
for el in root.iter():
    ctx = el.get("contextRef")
    if ctx is None or el.text is None:
        continue
    name = _local(el.tag)
    if any(k in name for k in ("revenue", "income", "profit", "expense", "eps", "earnings")):
        txt = el.text.strip()[:20]
        cdates = contexts.get(ctx, ("?", "?", "?"))
        if shown < 30:
            print(f"  {name:<55} ctx={ctx:<20} val={txt:<20} dates={cdates}")
            shown += 1
print(f"  (matching facts shown: {shown})")

# ── 3) Fact counts per context ─────────────────────────────────────────────────
print("\n=== NUMERIC FACT COUNT PER CONTEXT (top 10) ===")
from collections import Counter
cnt = Counter()
for el in root.iter():
    ctx = el.get("contextRef")
    if ctx and el.text:
        try:
            float(el.text.strip())
            cnt[ctx] += 1
        except ValueError:
            pass
for ctx, n in cnt.most_common(10):
    print(f"  {ctx:<25} {n:>4} numeric facts   dates={contexts.get(ctx)}")
