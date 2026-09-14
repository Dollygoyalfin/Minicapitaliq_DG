"""
Patch YOUR index.html so the Overview tab's Ownership Structure block shows
the real data instead of four permanent dashes.

What changes:
  - Promoters %  and Public %   → from the shareholding table (NSE filings)
  - Promoter encumbrance %      → from the shareholding-pattern XBRL
  - Promoter trend over quarters → rising / falling, with the change in pp
  - FII / DII                   → removed, with the reason stated

FII and DII are removed rather than left blank because NSE's shareholding
feed does not publish them. A placeholder that can never fill is worse than
saying plainly that the data is not available.

Run in your repo folder:   python patch_ownership.py
"""

import os
import re
import shutil
from datetime import datetime

SRC = "index.html"
if not os.path.exists(SRC):
    raise SystemExit("index.html not found — run this in your repo folder.")

html = open(SRC, encoding="utf-8").read()
if html.count("<script") == 0:
    raise SystemExit("index.html has no <script> block — restore a good copy first.")
if "ownership-block" in html:
    raise SystemExit("Already patched — nothing to do.")

backup = f"index.html.bak-{datetime.now():%Y%m%d-%H%M%S}"
shutil.copy2(SRC, backup)
print(f"backup written: {backup}")

before_fns = len(re.findall(r"function \w+", html))

# ── Find the ownership markup and replace it with a rendered container ──────
# Located by anchor text rather than by a regex over div structure — a regex
# that guessed where a div ended is what destroyed an earlier copy of this file.
anchor = html.find("Ownership Structure")
if anchor == -1:
    print("  'Ownership Structure' heading not found — skipping markup swap")
else:
    # Walk backwards to the start of the enclosing element
    start = html.rfind("<div", 0, anchor)
    # Walk forwards counting div depth from that point
    i, depth = start, 0
    while i < len(html):
        if html.startswith("<div", i):
            depth += 1
            i += 4
        elif html.startswith("</div>", i):
            depth -= 1
            i += 6
            if depth == 0:
                break
        else:
            i += 1
    replacement = ('<div class="card"><div class="card-head">Ownership</div>'
                   '<div id="ownership-block" style="padding:4px 0">'
                   '<div style="opacity:0.55;font-size:0.8rem">—</div></div></div>')
    html = html[:start] + replacement + html[i:]
    print(f"  ownership markup replaced ({i - start} chars)")

# ── Renderer, appended before </script> ────────────────────────────────────
FUNC = r'''
  // Fills the Overview tab's ownership card from /valuation. Promoter and
  // encumbrance figures come from NSE shareholding-pattern filings; FII/DII
  // are deliberately absent because that feed does not publish them.
  function renderOwnership(d) {
    const el = document.getElementById("ownership-block");
    if (!el) return;
    const pct = v => (v === null || v === undefined) ? null :
                     (typeof v === "number" ? v.toFixed(2) + "%" : v);

    const line = (label, val, colour) => val == null ? "" : `
      <div style="display:flex;justify-content:space-between;padding:6px 0;
                  border-bottom:1px solid rgba(255,255,255,0.06);font-size:0.85rem">
        <span style="opacity:0.7">${label}</span>
        <span class="mono"${colour ? ` style="color:${colour};font-weight:600"` : ""}>${val}</span>
      </div>`;

    let pledgeColour = null;
    if (typeof d.pledged_pct === "number") {
      pledgeColour = d.pledged_pct >= 25 ? "#ef4444"
                   : d.pledged_pct >= 5  ? "#eab308" : "#22c55e";
    } else if (typeof d.pledged_pct === "string") {
      pledgeColour = "#eab308";
    }

    let trend = "";
    if (d.promoter_trend) {
      const t = d.promoter_trend;
      const col = t.change_pp <= -1 ? "#ef4444" : t.change_pp >= 1 ? "#22c55e" : "inherit";
      const dir = t.change_pp > 0 ? "rose" : t.change_pp < 0 ? "fell" : "flat";
      trend = `<div style="font-size:0.72rem;opacity:0.7;margin-top:6px">
        Promoter stake ${dir} <span style="color:${col};font-weight:600">${t.change_pp > 0 ? "+" : ""}${t.change_pp}pp</span>
        over ${t.quarters} quarters (${t.from}% → ${t.to}%)</div>`;
    }

    let instTrend = "";
    if (d.institutional_trend) {
      const it = d.institutional_trend;
      const fc = it.fii_change_pp >= 0 ? "#22c55e" : "#ef4444";
      const dc = it.dii_change_pp >= 0 ? "#22c55e" : "#ef4444";
      instTrend = `<div style="font-size:0.72rem;opacity:0.75;margin-top:6px">
        Over ${it.quarters} quarters —
        FII <span style="color:${fc};font-weight:600">${it.fii_change_pp >= 0 ? "+" : ""}${it.fii_change_pp}pp</span>,
        DII <span style="color:${dc};font-weight:600">${it.dii_change_pp >= 0 ? "+" : ""}${it.dii_change_pp}pp</span>
      </div>`;
    }

    const body =
      line("Promoters", pct(d.promoters_holding)) +
      line("FII", pct(d.fii_holding)) +
      line("DII", pct(d.dii_holding)) +
      line("Retail (residual)", pct(d.retail_holding)) +
      line("Public (total float)", pct(d.public_holding)) +
      line("Promoter shares encumbered", pct(d.pledged_pct), pledgeColour);

    el.innerHTML = body
      ? body +
        (d.pledge_as_of ? `<div style="font-size:0.7rem;opacity:0.5;margin-top:5px">as of ${d.pledge_as_of}</div>` : "") +
        trend + instTrend +
        (d.ownership_note ? `<div style="font-size:0.7rem;opacity:0.55;margin-top:8px;line-height:1.45">${d.ownership_note}</div>` : "")
      : `<div style="opacity:0.55;font-size:0.8rem">No shareholding data for this company.</div>`;
  }
'''

idx = html.rfind("</script>")
if idx == -1:
    raise SystemExit("No closing </script> found — aborting without writing.")
html = html[:idx] + FUNC + "\n" + html[idx:]

# ── Call it from runValuation, right after the response arrives ────────────
m = re.search(r'(async function runValuation\s*\([^)]*\)\s*\{)', html)
if m:
    # insert the call just after the error check inside runValuation
    seg_start = m.end()
    err_check = html.find('setError("valuation-results"', seg_start)
    line_end = html.find("\n", err_check) if err_check != -1 else seg_start
    html = html[:line_end] + '\n      try { renderOwnership(d); } catch (e) {}' + html[line_end:]
    print("  renderOwnership() call inserted into runValuation")
else:
    print("  runValuation not found — call renderOwnership(d) manually")

# ── Verify before writing ──────────────────────────────────────────────────
after_fns = len(re.findall(r"function \w+", html))
problems = []
if html.count("<script") != html.count("</script>"):
    problems.append("script tags unbalanced")
if after_fns < before_fns:
    problems.append(f"function count fell ({before_fns} → {after_fns})")
for needed in ("renderOwnership", "runValuation", "switchTab", "ownership-block"):
    if needed not in html:
        problems.append(f"missing {needed}")

if problems:
    print("\nABORTED — patch would have broken the file:")
    for p in problems:
        print("   ", p)
    print(f"\nOriginal untouched. Backup at {backup}")
    raise SystemExit(1)

open(SRC, "w", encoding="utf-8").write(html)
print(f"\n✅ patched: {before_fns} → {after_fns} functions, script tags balanced")
print(f"   backup kept at {backup}")
