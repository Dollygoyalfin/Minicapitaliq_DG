"""
MiniTradeIQ — Store Data-Quality Audit (v2)
============================================
Scans EVERY company's stored statements against accounting sanity rules and
prints a flag report. Wrong data gets caught by arithmetic, not by luck.

Usage:
    python audit_store.py               # full report
    python audit_store.py > audit.txt   # save to file

Checks:
  R1  revenue missing/non-positive in latest year (other checks still run)
  R2  |revenue YoY| > 80% between ADJACENT fiscal years only
  C1  OCF/NI outside [0.2, 3.0] when both positive  [non-financials only]
  C2  capex > 60% of revenue
  C3  capex > 4x depreciation                        [non-financials only]
  B1  equity <= 0 while profitable
  B2  CA/CL ratio outside [0.05, 20]
  M1  operating margin outside [-100%, +80%]
  E1  stored EPS vs NI/shares — best-matching year drift > 25%
  D1  interest expense > 30% of revenue              [non-financials only]
  D2  depreciation > 50% of revenue
Notes: financial-sector companies are excluded from C1/C3/D1 because those
ratios are legitimately extreme for banks/NBFCs — flags there would be noise.
"""

from collections import defaultdict, Counter
from data_store import _conn


def fetch_all():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT ticker, market, sector, data_source,
                                  shares_outstanding, eps FROM companies""")
            companies = {r[0]: {"market": r[1], "sector": r[2], "source": r[3],
                                "shares": r[4], "eps": r[5]} for r in cur.fetchall()}
            cur.execute("""SELECT ticker, fiscal_year, revenue, total_expenses,
                                  net_income, interest_expense, depreciation
                           FROM income_statements ORDER BY ticker, fiscal_year DESC""")
            income = defaultdict(list)
            for r in cur.fetchall():
                income[r[0]].append({"fy": r[1], "rev": r[2], "exp": r[3],
                                     "ni": r[4], "int": r[5], "depr": r[6]})
            cur.execute("""SELECT ticker, fiscal_year, current_assets,
                                  current_liabilities, total_equity
                           FROM balance_sheets ORDER BY ticker, fiscal_year DESC""")
            balance = defaultdict(list)
            for r in cur.fetchall():
                balance[r[0]].append({"fy": r[1], "ca": r[2], "cl": r[3], "eq": r[4]})
            cur.execute("""SELECT ticker, fiscal_year, operating_cash_flow, capex
                           FROM cash_flows ORDER BY ticker, fiscal_year DESC""")
            cash = defaultdict(list)
            for r in cur.fetchall():
                cash[r[0]].append({"fy": r[1], "ocf": r[2], "capex": r[3]})
    return companies, income, balance, cash


def audit():
    companies, income, balance, cash = fetch_all()
    flags = defaultdict(list)
    check_counts = Counter()

    def add(tkr, code, msg):
        flags[tkr].append(f"{code}: {msg}")
        check_counts[code] += 1

    for tkr, meta in companies.items():
        inc = income.get(tkr, [])
        bal = {b["fy"]: b for b in balance.get(tkr, [])}
        cfs = {c["fy"]: c for c in cash.get(tkr, [])}
        is_fin = (meta.get("sector") or "") in ("Financial Services", "Financials")

        # R1 — latest revenue (flag, but keep auditing other years)
        if not inc:
            add(tkr, "R1", "no income rows at all")
            continue
        if inc[0]["rev"] is None or (inc[0]["rev"] or 0) <= 0:
            add(tkr, "R1", f"FY{inc[0]['fy']} (latest) revenue missing/non-positive")

        # R2 — YoY jumps, ADJACENT fiscal years only
        for a, b in zip(inc, inc[1:]):
            if a["fy"] != b["fy"] + 1:
                continue          # gap year — a 2-yr move is not a YoY
            if a["rev"] and b["rev"] and b["rev"] > 0:
                yoy = a["rev"] / b["rev"] - 1
                if abs(yoy) > 0.8:
                    add(tkr, "R2", f"FY{a['fy']} revenue YoY {yoy*100:+.0f}%")

        for row in inc:
            fy, rev, ni = row["fy"], row["rev"], row["ni"]
            cf = cfs.get(fy, {})
            ocf, capex = cf.get("ocf"), cf.get("capex")
            b = bal.get(fy, {})

            # C1 — cash conversion band (skip financials: deposit-flow noise)
            if not is_fin and ocf and ni and ni > 0:
                conv = ocf / ni
                if conv < 0.2 or conv > 3.0:
                    add(tkr, "C1", f"FY{fy} OCF/NI = {conv:.2f} (suspicious)")

            # C2 — capex vs revenue
            if capex and rev and rev > 0 and abs(capex) > 0.6 * rev:
                add(tkr, "C2", f"FY{fy} capex {abs(capex)/rev*100:.0f}% of revenue")

            # C3 — capex vs depreciation (skip financials)
            if (not is_fin and capex and row["depr"] and row["depr"] > 0
                    and abs(capex) > 4 * row["depr"]):
                add(tkr, "C3", f"FY{fy} capex {abs(capex)/row['depr']:.1f}x depreciation")

            # B1 — negative equity while profitable
            if b.get("eq") is not None and b["eq"] <= 0 and ni and ni > 0:
                add(tkr, "B1", f"FY{fy} equity <= 0 while profitable")

            # B2 — CA/CL ratio
            if b.get("ca") and b.get("cl") and b["cl"] > 0:
                r = b["ca"] / b["cl"]
                if r < 0.05 or r > 20:
                    add(tkr, "B2", f"FY{fy} CA/CL = {r:.2f}")

            # M1 — margin band
            if rev and rev > 0 and row["exp"] is not None:
                m = (rev - row["exp"]) / rev
                if m < -1.0 or m > 0.8:
                    add(tkr, "M1", f"FY{fy} operating margin {m*100:.0f}%")

            # D1 — interest vs revenue (skip financials: interest IS the business)
            if not is_fin and row["int"] and rev and rev > 0 and row["int"] > 0.3 * rev:
                add(tkr, "D1", f"FY{fy} interest {row['int']/rev*100:.0f}% of revenue")

            # D2 — depreciation vs revenue
            if row["depr"] and rev and rev > 0 and row["depr"] > 0.5 * rev:
                add(tkr, "D2", f"FY{fy} depreciation {row['depr']/rev*100:.0f}% of revenue")

        # E1 — EPS consistency: best-matching year (stored EPS belongs to SOME
        # year; comparing only the latest falsely flags every growing company)
        if meta["eps"] and meta["shares"]:
            drifts = []
            for row in inc:
                if row["ni"]:
                    implied = row["ni"] / meta["shares"]
                    drifts.append(abs(implied - meta["eps"]) / abs(meta["eps"]))
            if drifts and min(drifts) > 0.25:
                add(tkr, "E1", f"stored EPS {meta['eps']:.2f} matches no year "
                               f"(best drift {min(drifts)*100:.0f}%)")

    # ── Completeness census ──────────────────────────────────────────────────
    null_census = Counter()
    for tkr, meta in companies.items():
        mkt = meta["market"]
        if meta["shares"] is None:
            null_census[f"{mkt}: shares_outstanding NULL"] += 1
        if meta["eps"] is None:
            null_census[f"{mkt}: eps NULL"] += 1
        if (meta.get("sector") or "Unknown") == "Unknown":
            null_census[f"{mkt}: sector Unknown"] += 1
        if not cash.get(tkr) or all(c["ocf"] is None for c in cash.get(tkr, [])):
            null_census[f"{mkt}: no OCF any year"] += 1

    # ── Report ───────────────────────────────────────────────────────────────
    print("=" * 70)
    print(f"STORE AUDIT v2 — {len(companies)} companies")
    print("=" * 70)
    print(f"\nCompanies with at least one flag: {len(flags)} "
          f"({len(flags)/max(len(companies),1)*100:.0f}%)")
    print("\nFlags by check:")
    for code, n in check_counts.most_common():
        print(f"  {code}: {n}")
    print("\nCompleteness gaps:")
    for k, n in sorted(null_census.items()):
        print(f"  {k}: {n}")
    print("\n" + "=" * 70)
    print("TOP 40 MOST-FLAGGED COMPANIES")
    print("=" * 70)
    ranked = sorted(flags.items(), key=lambda kv: len(kv[1]), reverse=True)[:40]
    for tkr, fl in ranked:
        meta = companies[tkr]
        print(f"\n{tkr} [{meta['market']}, {meta['source']}] — {len(fl)} flags")
        for f_ in fl[:6]:
            print(f"   {f_}")
        if len(fl) > 6:
            print(f"   ... and {len(fl)-6} more")


if __name__ == "__main__":
    audit()
