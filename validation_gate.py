# ── DATA QUALITY GATE (paste into main.py, ABOVE the /dcf block) ─────────────
# You cannot guarantee perfect XBRL extraction across 1,000 filers. You CAN
# guarantee that no valuation is ever published from inputs that fail
# accounting sanity. This runs before any model computes.
#
# Philosophy: refuse loudly rather than output a confident wrong number.

def validate_financials(info, income_df, balance_df, cashflow_df, market="us"):
    """Returns (ok: bool, reason: str|None, warnings: list[str])."""
    warnings = []

    def row(df, *keywords):
        if df is None or df.empty:
            return {}
        for idx in df.index:
            if all(k.lower() in idx.lower() for k in keywords):
                out = {}
                for col in df.columns:
                    try:
                        v = float(df.loc[idx, col])
                        if v == v:
                            out[str(col)] = v
                    except Exception:
                        pass
                return out
        return {}

    revenue  = row(income_df, "total revenue")
    expenses = row(income_df, "total expenses")
    ni       = row(income_df, "net income")
    depr     = row(income_df, "depreciation") or row(cashflow_df, "depreciation")
    interest = row(income_df, "interest", "expense")
    ocf      = row(cashflow_df, "operating cash flow")

    if not revenue:
        return False, "No revenue data available for this company.", warnings

    years = sorted(revenue.keys(), reverse=True)
    latest = years[0]
    rev_l = revenue.get(latest)

    # ── 1. Revenue must exist and be positive in the latest year ─────────────
    if not rev_l or rev_l <= 0:
        return False, (f"Latest fiscal year ({latest}) has no usable revenue "
                       f"figure — valuation would be meaningless."), warnings

    # ── 2. Operating margin must be economically possible ────────────────────
    exp_l = expenses.get(latest)
    if exp_l is not None and exp_l > 0:
        margin = (rev_l - exp_l) / rev_l
        if margin > 0.95:
            return False, (f"Extracted costs ({exp_l:,.0f}) imply a {margin*100:.0f}% "
                           f"operating margin — the cost data is incomplete for this "
                           f"filer, so a cash-flow valuation cannot be trusted."), warnings
        if margin < -2.0:
            warnings.append(f"Operating margin of {margin*100:.0f}% is extreme — "
                            f"verify against the company's filings.")
    elif exp_l is None:
        return False, (f"No total-expense figure could be extracted for {latest}; "
                       f"operating profit cannot be computed."), warnings

    # ── 3. Revenue must exceed depreciation and interest (scale sanity) ──────
    d_l = depr.get(latest)
    if d_l and d_l > rev_l:
        return False, (f"Depreciation ({d_l:,.0f}) exceeds revenue ({rev_l:,.0f}) — "
                       f"revenue is understated for this filer; refusing to value."), warnings

    is_fin = (info.get("sector") or "") in ("Financial Services", "Financials")
    i_l = interest.get(latest)
    if not is_fin and i_l and i_l > rev_l:
        return False, (f"Interest expense ({i_l:,.0f}) exceeds revenue ({rev_l:,.0f}) — "
                       f"inputs are inconsistent; refusing to value."), warnings

    # ── 4. Shares outstanding required for a per-share value ─────────────────
    shares = info.get("sharesOutstanding")
    if not shares or shares <= 0:
        return False, "Shares outstanding unavailable — per-share value cannot be derived.", warnings

    # ── 5. Cash-flow sanity (warn only — many businesses legitimately burn) ──
    o_l, n_l = ocf.get(latest), ni.get(latest)
    if o_l is not None and n_l and n_l > 0:
        base = n_l + (d_l or 0)
        if base > 0:
            conv = o_l / base
            if conv < -3 or conv > 5:
                warnings.append(
                    f"Operating cash flow is {conv:.1f}x (net income + D&A) — "
                    f"unusual; treat cash-based outputs with caution.")

    # ── 6. Series continuity — a source flip fabricates growth rates ─────────
    if len(years) >= 2:
        for a, b in zip(years, years[1:]):
            va, vb = revenue.get(a), revenue.get(b)
            if va and vb and vb > 0:
                yoy = va / vb - 1
                if abs(yoy) > 3.0:
                    warnings.append(
                        f"Revenue changed {yoy*100:+.0f}% between FY{b} and FY{a} — "
                        f"if this is not a genuine merger/demerger, growth rates "
                        f"derived from it are unreliable.")
                    break

    # ── 7. Structural: cash-flow DCF is inappropriate for banks/insurers ─────
    if is_fin:
        warnings.append(
            "Financial-sector company — cash-flow DCF is not the appropriate "
            "framework (no meaningful working capital or capex cycle). Treat "
            "the earnings-based models in Convergence as primary.")

    return True, None, warnings
