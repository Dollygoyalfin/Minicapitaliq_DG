"""
Verify that what is DEPLOYED matches the latest builds.

Run this in your repo folder. It prints the build stamp found in each local
file next to the expected one, so a stale file is obvious rather than being
discovered later through a puzzling bug — which has happened repeatedly in
this project.

Usage:  python verify_deploy.py
"""

import os
import re

EXPECTED = {
    # standalone modules
    "sec_edgar_layer.py":                "2026-07-27a (working capital components)",
    "india_data_pipeline.py":            "2026-07-27a (working capital components)",
    "signature_engine.py":               "2026-07-27d (weekly sampling, ~80% smaller)",
    "base_rate_engine.py":               "2026-07-27a (equal-weight index from returns, weekly)",
    "news_engine.py":                    "2026-07-27j (fraction scaling + NumberOfShares denominator)",
    "us_events_engine.py":               "2026-07-27a (8-K item codes)",
    "signal_journal.py":                 "2026-07-26b (store-only, no live price calls)",
    "portfolio.py":                      "2026-07-27c (full digest + screen)",
    "audit_store.py":                    "2026-07-25c (build census)",
}

# These are pasted INTO main.py, so their stamps must be found there
MAIN_PY_BLOCKS = {
    "GATE_BUILD":        "2026-07-27 (bank refusal, partial-year tolerance)",
    "DCF_BUILD":         "2026-07-27 (margin-based costs, growth fade, asset intensity, DSO/DIO/DPO, computed beta)",
    "VALUATION_BUILD":   "2026-07-27 (normalised EPS, terminal growth <= risk free)",
    "QUALITY_BUILD":     "2026-07-27 (promoter encumbrance + corporate events)",
    "TECHNICALS_BUILD":  "2026-07-27 (RSI/DMA from signatures)",
    "EVENTS_BUILD":      "2026-07-27 (india + US 8-K)",
    "IDEAS_BUILD":       "2026-07-27 (5 strategies, sector/PE filters, composite score)",
    "BASERATES_BUILD":   "2026-07-27 (weekly horizons, equal-weight index from returns)",
}

# Marker strings that must exist — catches a file that has the right stamp
# but is missing content (e.g. pasted partially)
CONTENT_CHECKS = {
    "data_store.py":      ["accounts_receivable", "get_sector_depreciation_rates"],
    "fmp_data_layer.py":  ["_ingest_budget_available"],
    "index.html":         ["tab-ideas", "tab-reversedcf", "tab-technicals",
                           "Corporate Events", "safeFetch"],
    "service_worker.js":  ["minitradeiq-shell-v3"],
}


def stamp_in(path, pattern=r'_BUILD = "([^"]*)"'):
    if not os.path.exists(path):
        return None
    m = re.search(pattern, open(path, encoding="utf-8", errors="ignore").read())
    return m.group(1) if m else None


ok = warn = 0
print("=" * 78)
print("STANDALONE MODULES")
print("=" * 78)
for fn, want in EXPECTED.items():
    got = stamp_in(fn)
    if got is None:
        print(f"  MISSING   {fn}")
        warn += 1
    elif got == want:
        print(f"  OK        {fn}")
        ok += 1
    else:
        print(f"  STALE     {fn}")
        print(f"              have: {got}")
        print(f"              want: {want}")
        warn += 1

print()
print("=" * 78)
print("BLOCKS INSIDE main.py")
print("=" * 78)
if not os.path.exists("main.py"):
    print("  main.py not found in this folder")
else:
    main = open("main.py", encoding="utf-8", errors="ignore").read()
    for var, want in MAIN_PY_BLOCKS.items():
        m = re.search(rf'{var} = "([^"]*)"', main)
        if not m:
            print(f"  MISSING   {var}  — block not pasted into main.py")
            warn += 1
        elif m.group(1) == want:
            print(f"  OK        {var}")
            ok += 1
        else:
            print(f"  STALE     {var}")
            print(f"              have: {m.group(1)}")
            print(f"              want: {want}")
            warn += 1

print()
print("=" * 78)
print("CONTENT CHECKS (files without build stamps)")
print("=" * 78)
for fn, markers in CONTENT_CHECKS.items():
    if not os.path.exists(fn):
        print(f"  MISSING   {fn}")
        warn += 1
        continue
    body = open(fn, encoding="utf-8", errors="ignore").read()
    missing = [m for m in markers if m not in body]
    if missing:
        print(f"  STALE     {fn}  — missing: {', '.join(missing)}")
        warn += 1
    else:
        print(f"  OK        {fn}")
        ok += 1

print()
print("=" * 78)
print(f"  {ok} up to date, {warn} need attention")
print("=" * 78)
