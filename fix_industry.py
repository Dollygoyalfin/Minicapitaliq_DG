"""
One-time: give Indian financial companies a detailed industry label.

The pipeline stored NSE's top-level sector ("Financial Services") as the
industry for every bank, insurer, AMC, exchange and depository alike. The DCF
gate needs to know which is which - a depository can be valued by DCF, an
insurer cannot - so this replaces that coarse label with a detailed one:
NSE's basicIndustry where NSE still answers, otherwise Yahoo's industry.

New companies get the detailed label automatically (india_data_pipeline now
asks for it); this fixes the ones already stored. Safe to re-run.

    python fix_industry.py            # fix, then print the result
    python fix_industry.py --dry-run  # show what would change
    python fix_industry.py --probe    # try ONE symbol and show raw errors
"""
import sys
import time
from data_store import _conn
import india_data_pipeline as idp

dry = "--dry-run" in sys.argv

# Checked corrections, using NSE's own industry names. Applied after the
# automatic lookup, and only for these symbols. Each is here for a reason:
# Yahoo returned nothing, or a label that misdescribes what the business is.
OVERRIDES = {
    # Holding / investment companies: value is mostly stakes in other
    # companies, which a cash-flow DCF cannot capture. Yahoo calls them
    # "Asset Management", which would wrongly let them through.
    "BAJAJHLDNG.NS": ("Holding Company", "holding company, Yahoo said Asset Management"),
    "BAJAJFINSV.NS": ("Holding Company", "holds Bajaj Finance and insurers; Yahoo gave nothing"),
    "TATAINVEST.NS": ("Investment Company", "investment company, Yahoo said Asset Management"),
    "JIOFIN.NS":     ("Non Banking Financial Company (NBFC)", "an NBFC, Yahoo said Asset Management"),
    # Yahoo returned no industry for these
    "360ONE.NS":     ("Asset Management Company", "wealth and asset manager"),
    "NAM-INDIA.NS":  ("Asset Management Company", "Nippon Life India AMC"),
    "CENTRALBK.NS":  ("Public Sector Bank", "Central Bank of India"),
    "IDBI.NS":       ("Private Sector Bank", "IDBI Bank"),
    "NIACL.NS":      ("General Insurance", "New India Assurance"),
    "AZAD.NS":       ("Aerospace & Defense", "Azad Engineering, not a financial"),
    "JBCHEPHARM.NS": ("Pharmaceuticals", "JB Chemicals, not a financial"),
}

if "--probe" in sys.argv:
    sym = next((a for a in sys.argv[1:] if not a.startswith("--")), "CDSL")
    idp._LAST_INDUSTRY_ERROR.clear()
    label, src = idp.fetch_detailed_industry(sym)
    print(f"{sym}: {label!r} from {src}")
    for k, v in idp._LAST_INDUSTRY_ERROR.items():
        print(f"  {k} failed: {v}")
    sys.exit(0)

conn = _conn()
try:
    with conn.cursor() as cur:
        cur.execute("""SELECT ticker, industry FROM companies
                       WHERE market='india'
                         AND (LOWER(COALESCE(industry,'')) IN
                              ('financial services','unknown','')
                              OR sector IN ('Financial Services','Financials'))
                       ORDER BY ticker""")
        todo = cur.fetchall()
finally:
    conn.close()

print(f"{len(todo)} Indian financial companies to check\n")
changed, failed, sources = [], [], {"nse": 0, "yahoo": 0, "checked": 0}
first_errors_shown = False
for i, (tkr, old) in enumerate(todo, 1):
    idp._LAST_INDUSTRY_ERROR.clear()
    if tkr in OVERRIDES:
        detail, src = OVERRIDES[tkr][0], "checked"
    else:
        detail, src = idp.fetch_detailed_industry(tkr.replace(".NS", ""))
    if not detail:
        failed.append(tkr)
        if not first_errors_shown:
            # Never fail silently: say WHY on the first miss
            print(f"  first failure ({tkr}):")
            for k, v in idp._LAST_INDUSTRY_ERROR.items():
                print(f"    {k}: {v}")
            first_errors_shown = True
    else:
        sources[src] += 1
        if detail != old:
            changed.append((tkr, old, detail, src))
    time.sleep(1.5)
    if i % 20 == 0:
        print(f"  {i}/{len(todo)}  (nse {sources['nse']}, yahoo {sources['yahoo']}, "
              f"none {len(failed)})")

if changed and not dry:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            for tkr, _, detail, _src in changed:
                cur.execute("UPDATE companies SET industry=%s WHERE ticker=%s",
                            (detail, tkr))
        conn.commit()
    finally:
        conn.close()

print(f"\nLabels found: {sources['nse']} from NSE, {sources['yahoo']} from Yahoo, "
      f"{sources['checked']} from the checked list, {len(failed)} not found")
print(f"{'Would change' if dry else 'Changed'} {len(changed)}:")
for tkr, old, new, src in changed:
    print(f"  {tkr.replace('.NS',''):<14} {old or '-':<20} → {new}  [{src}]")
if failed:
    print(f"\nNo classification for {len(failed)}: "
          f"{', '.join(t.replace('.NS','') for t in failed[:30])}"
          f"{' …' if len(failed) > 30 else ''}")
    print("These keep their old label, and the DCF gate refuses them as "
          "'cannot confirm it is not a lender or insurer'. Re-run later.")
