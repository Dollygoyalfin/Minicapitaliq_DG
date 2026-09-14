"""
Patch YOUR working index.html to rebuild the IPO and Commodities tabs.

Run this in your repo folder, where the good index.html lives. It makes a
timestamped backup first, then replaces only the two tab sections and adds
the three renderer functions.

    python patch_ipo_commodities.py

If anything looks wrong afterwards, restore from the .bak file it creates.
"""

import os
import re
import shutil
from datetime import datetime

SRC = "index.html"

if not os.path.exists(SRC):
    raise SystemExit("index.html not found — run this in your repo folder.")

html = open(SRC, encoding="utf-8").read()

# Refuse to patch a file that is already damaged or already patched
if html.count("<script") == 0:
    raise SystemExit("This index.html has no <script> block — it is damaged. "
                     "Restore a good copy before patching.")
if "runIpoBaseRates" in html:
    raise SystemExit("Already patched — nothing to do.")

backup = f"index.html.bak-{datetime.now():%Y%m%d-%H%M%S}"
shutil.copy2(SRC, backup)
print(f"backup written: {backup}")

before_fns = len(re.findall(r"function \w+", html))

# ── 1. Replace the IPO tab body ─────────────────────────────────────────────
NEW_IPO = '''<div id="tab-ipos" class="tab-content">
  <div class="container">
    <div style="font-size:0.78rem;opacity:0.75;margin-bottom:12px;line-height:1.55">
      Live NSE issues, plus the one thing a listing site cannot tell you:
      <strong>what actually happened to recent IPOs after listing</strong>,
      measured from this app's own price history.
    </div>
    <div style="margin-bottom:14px">
      <button class="btn-primary" onclick="runIpos()">📋 Load IPOs</button>
      <button class="btn-ghost" onclick="runIpoBaseRates()">📊 Post-listing base rates</button>
    </div>
    <div id="ipos-results">
      <div class="state-empty"><span class="icon">📋</span>
        Current and upcoming NSE public issues.
      </div>
    </div>
  </div>
</div>'''

NEW_COMM = '''<div id="tab-commodities" class="tab-content">
  <div class="container">
    <div style="font-size:0.78rem;opacity:0.75;margin-bottom:12px;line-height:1.55">
      Not a price ticker. This measures <strong>which companies actually move
      with a commodity</strong>, regressed from real returns rather than assumed
      from a sector label — two firms in the same sector often have completely
      different exposure.
    </div>
    <div class="params-grid" style="margin-bottom:10px">
      <div class="param-group"><label>Commodity</label>
        <select id="cmd-pick" style="width:100%">
          <option value="crude">Crude Oil</option>
          <option value="gold">Gold</option>
          <option value="silver">Silver</option>
          <option value="copper">Copper</option>
          <option value="natgas">Natural Gas</option>
        </select></div>
      <div class="param-group"><label>Min R² (evidence strength)</label>
        <input type="number" id="cmd-r2" value="0.10" step="0.05" min="0" max="1"/></div>
    </div>
    <button class="btn-primary" onclick="runCommodities()">⛽ Measure Exposure</button>
    <div id="commodities-results" style="margin-top:14px">
      <div class="state-empty"><span class="icon">⛽</span>
        Pick a commodity to see which stocks move with it.
      </div>
    </div>
  </div>
</div>'''


def replace_tab(doc, tab_id, replacement):
    """Replace one tab div by counting div depth — NOT with a regex that
    guesses where the block ends. A regex is what destroyed the previous
    version of this file."""
    start = doc.find(f'<div id="tab-{tab_id}" class="tab-content">')
    if start == -1:
        print(f"  tab-{tab_id}: not found, skipped")
        return doc
    i, depth = start, 0
    while i < len(doc):
        if doc.startswith("<div", i):
            depth += 1
            i += 4
        elif doc.startswith("</div>", i):
            depth -= 1
            i += 6
            if depth == 0:
                break
        else:
            i += 1
    print(f"  tab-{tab_id}: replaced ({i - start} chars)")
    return doc[:start] + replacement + doc[i:]


html = replace_tab(html, "ipos", NEW_IPO)
html = replace_tab(html, "commodities", NEW_COMM)

# ── 2. Insert the three renderer functions before the closing </script> ─────
FUNCS = r'''
  async function runIpos() {
    setLoading("ipos-results", "Loading NSE issues...");
    try {
      const d = await safeFetch(`${BASE}/ipos?market=india`);
      if (d.error) { setError("ipos-results", d.error); return; }
      const row = (x, open) => `
        <div style="padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.07)">
          <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap">
            <div><span class="mono" style="font-weight:700">${x.symbol || "—"}</span>
              <span style="opacity:0.7;font-size:0.8rem"> ${x.company || ""}</span></div>
            <div class="mono" style="font-size:0.82rem">${x.price_band || "—"}</div>
          </div>
          <div style="font-size:0.72rem;opacity:0.6;margin-top:3px">
            ${x.issue_start || "?"} → ${x.issue_end || "?"}
            ${x.lot_size ? " · lot " + x.lot_size : ""}
            ${x.issue_size ? " · " + x.issue_size : ""}
            ${open && x.subscription ? " · subscribed " + x.subscription + "x" : ""}
          </div>
        </div>`;
      const cur = (d.current || []).map(x => row(x, true)).join("");
      const up  = (d.upcoming || []).map(x => row(x, false)).join("");
      document.getElementById("ipos-results").innerHTML = `
        <div class="result-card">
          <div style="font-weight:600;font-size:0.9rem;margin-bottom:6px">Open now</div>
          ${cur || '<div style="opacity:0.6;font-size:0.8rem">No issues currently open.</div>'}
          <div style="font-weight:600;font-size:0.9rem;margin:16px 0 6px">Upcoming</div>
          ${up || '<div style="opacity:0.6;font-size:0.8rem">Nothing announced yet.</div>'}
          <div style="margin-top:14px;padding:10px;background:rgba(255,255,255,0.04);border-radius:4px">
            <div style="font-size:0.75rem;font-weight:600;margin-bottom:4px">What this app can't tell you here</div>
            <ul style="margin:0;padding-left:16px;font-size:0.71rem;opacity:0.7">
              ${(d.what_this_app_cannot_do||[]).map(x=>`<li style="margin:3px 0">${x}</li>`).join("")}
            </ul>
          </div>
        </div>`;
    } catch (e) { setError("ipos-results", e.message); }
  }

  async function runIpoBaseRates() {
    setLoading("ipos-results", "Measuring post-listing performance...");
    try {
      const d = await safeFetch(`${BASE}/ipos/base-rates?market=india&months_back=36`);
      if (d.error) { setError("ipos-results", d.error); return; }
      let rows = "";
      for (const h of ["1m","3m","6m","12m"]) {
        const x = (d.horizons||{})[h];
        if (!x) continue;
        if (x.conclusion) {
          rows += `<tr><td class="mono">${h}</td><td colspan="3" style="opacity:0.6;font-size:0.8rem">${x.conclusion} (n=${x.n})</td></tr>`;
          continue;
        }
        const col = x.median_excess_pct > 0 ? "#22c55e" : "#ef4444";
        rows += `<tr>
          <td class="mono">${h}</td>
          <td class="mono" style="color:${col};font-weight:600">${x.median_excess_pct > 0 ? "+" : ""}${x.median_excess_pct}%</td>
          <td class="mono">${x.beat_market_pct}%</td>
          <td class="mono" style="opacity:0.7;font-size:0.8rem">${x.p25_pct}% … ${x.p75_pct}%</td>
          <td style="font-size:0.72rem;opacity:0.6">n=${x.n_listings}</td></tr>`;
      }
      document.getElementById("ipos-results").innerHTML = `
        <div class="result-card">
          <div style="font-weight:600;font-size:0.9rem">Buying at listing — what happened</div>
          <div style="font-size:0.75rem;opacity:0.6;margin:4px 0 10px">
            ${d.n_listings} listings, ${d.window}
          </div>
          <table style="width:100%;border-collapse:collapse;font-size:0.85rem">
            <thead><tr style="opacity:0.6;font-size:0.75rem;text-align:left">
              <th>Horizon</th><th>Median vs market</th><th>Beat market</th><th>Typical range</th><th></th>
            </tr></thead><tbody>${rows}</tbody></table>
          <div style="margin-top:14px;padding:10px;background:rgba(255,255,255,0.04);border-radius:4px">
            <ul style="margin:0;padding-left:16px;font-size:0.71rem;opacity:0.7">
              ${(d.caveats||[]).map(x=>`<li style="margin:3px 0">${x}</li>`).join("")}
            </ul>
          </div>
        </div>`;
    } catch (e) { setError("ipos-results", e.message); }
  }

  async function runCommodities() {
    const pick = document.getElementById("cmd-pick").value;
    const r2   = document.getElementById("cmd-r2").value || 0.10;
    const market = getMarket();
    setLoading("commodities-results", "Measuring exposure across the universe...");
    try {
      const d = await safeFetch(`${BASE}/commodities?commodity=${pick}&market=${market}&min_r2=${r2}&limit=20`);
      if (d.error) { setError("commodities-results", d.error); return; }
      const tbl = (list, title, colour) => {
        if (!list || !list.length) return "";
        return `<div style="font-weight:600;font-size:0.88rem;margin:14px 0 4px;color:${colour}">${title}</div>` +
          list.map(x => `
            <div style="display:flex;justify-content:space-between;gap:8px;padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.06)">
              <div style="flex:1;min-width:0">
                <span class="mono" style="font-weight:600">${x.ticker}</span>
                <span style="font-size:0.74rem;opacity:0.6"> ${(x.name||"").slice(0,34)}</span>
              </div>
              <div class="mono" style="font-size:0.8rem">β ${x.beta_to_commodity}</div>
              <div class="mono" style="font-size:0.74rem;opacity:0.6;width:62px;text-align:right">R² ${x.r_squared}</div>
            </div>`).join("");
      };
      document.getElementById("commodities-results").innerHTML = `
        <div class="result-card">
          <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px">
            <div style="font-weight:700;font-size:1.05rem">${d.commodity}</div>
            <div class="mono">${d.latest_price} <span style="font-size:0.78rem;color:${d.weekly_change_pct>=0?'#22c55e':'#ef4444'}">${d.weekly_change_pct>=0?"+":""}${d.weekly_change_pct}%</span></div>
          </div>
          <div style="font-size:0.72rem;opacity:0.55">${d.measured_over} · showing only R² ≥ ${d.min_r2_applied}</div>
          ${tbl(d.moves_with, "Moves WITH " + d.commodity, "#22c55e")}
          ${tbl(d.moves_against, "Moves AGAINST " + d.commodity, "#ef4444")}
          ${(!d.moves_with?.length && !d.moves_against?.length) ? '<div style="opacity:0.6;font-size:0.8rem;padding:10px 0">No stock shows a relationship this strong. Lower the R² threshold to see weaker ones.</div>' : ""}
          <div style="margin-top:14px;padding:10px;background:rgba(255,255,255,0.04);border-radius:4px">
            <div style="font-size:0.75rem;font-weight:600;margin-bottom:4px">How to read this</div>
            <ul style="margin:0;padding-left:16px;font-size:0.71rem;opacity:0.7">
              ${(d.how_to_read||[]).map(x=>`<li style="margin:3px 0">${x}</li>`).join("")}
            </ul>
          </div>
        </div>`;
    } catch (e) { setError("commodities-results", e.message); }
  }
'''

idx = html.rfind("</script>")
if idx == -1:
    raise SystemExit("No closing </script> found — aborting without writing.")
html = html[:idx] + FUNCS + "\n" + html[idx:]

# ── 3. Sanity checks BEFORE writing ─────────────────────────────────────────
after_fns = len(re.findall(r"function \w+", html))
problems = []
if html.count("<script") != html.count("</script>"):
    problems.append("script tags unbalanced")
if after_fns < before_fns:
    problems.append(f"function count fell ({before_fns} → {after_fns})")
for needed in ("runIpos", "runCommodities", "runIpoBaseRates", "switchTab", "safeFetch"):
    if needed not in html:
        problems.append(f"missing {needed}")

if problems:
    print("\nABORTED — patch would have broken the file:")
    for p in problems:
        print("   ", p)
    print(f"\nYour original is untouched. Backup also at {backup}")
    raise SystemExit(1)

open(SRC, "w", encoding="utf-8").write(html)
print(f"\n✅ patched: {before_fns} → {after_fns} functions, script tags balanced")
print(f"   backup kept at {backup}")
