# Requires: from fmp_data_layer import get_company_data  (add to main.py imports)
# Requires: from fmp_data_layer import _cache_get, _cache_set, _with_retry
from fmp_data_layer import _cache_get, _cache_set, _with_retry

# Set yfinance to use a persistent session with headers to reduce rate limiting
try:
    import yfinance as yf
    import requests
    _yf_session = requests.Session()
    _yf_session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    })
    yf.utils._session = _yf_session
except Exception:
    pass

# ── Sector P/E medians (hardcoded, update quarterly) ──────────────────────────
SECTOR_PE_MEDIANS = {
    # India sectors
    "Financial Services":        18.0,
    "Banking":                   14.0,
    "Insurance":                 22.0,
    "Technology":                28.0,
    "Consumer Defensive":        35.0,
    "Consumer Cyclical":         30.0,
    "Healthcare":                32.0,
    "Energy":                    12.0,
    "Basic Materials":           14.0,
    "Industrials":               22.0,
    "Communication Services":    20.0,
    "Real Estate":               25.0,
    "Utilities":                 16.0,
    # US sectors
    "Information Technology":    30.0,
    "Health Care":               25.0,
    "Financials":                14.0,
    "Consumer Discretionary":    28.0,
    "Consumer Staples":          22.0,
    "Industrials_US":            22.0,
    "Materials":                 16.0,
    "Energy_US":                 12.0,
    "Utilities_US":              18.0,
    "Real Estate_US":            35.0,
    "Communication Services_US": 20.0,
    # Default
    "Unknown":                   20.0,
}


@app.get("/convergence")
def get_convergence(
    ticker: str = Query(...),
    market: str = Query("us"),
    source: str = Query("auto", description="auto / yfinance / fmp"),
    risk_free_rate: float = Query(0.04),
    market_return: float = Query(0.10),
    terminal_growth_rate: float = Query(0.03),
    margin_of_safety: float = Query(0.25),
):
    """
    Convergence Engine — 5 valuation methods aggregated into one consensus signal.

    Methods:
    1. DCF (FCFF-based)         — cash flow intrinsic value
    2. Earnings Power Value     — zero-growth floor value (Bruce Greenwald)
    3. Graham Number            — Benjamin Graham's formula
    4. Relative Valuation       — sector P/E median × EPS
    5. Historical P/E Mean      — reversion to historical average multiple

    Outputs:
    - Individual intrinsic values per method
    - Upside/downside per method
    - Consensus intrinsic value (trimmed mean)
    - Confidence score (% of models agreeing on direction)
    - Model convergence signal
    - Margin of safety buy zone
    """
    try:
        # ── Fetch data ────────────────────────────────────────────────────────
        raw_ticker = ticker.upper()
        if market.lower() == "india" and not raw_ticker.endswith(".NS"):
            raw_ticker += ".NS"

        try:
            info, income_df, balance_df, cashflow_df, data_source = get_company_data(
                ticker=ticker, market=market, source=source
            )
        except Exception as e:
            return {"error": f"Data fetch failed: {e}"}

        # ── Info fields ───────────────────────────────────────────────────────
        current_price      = info.get("currentPrice") or info.get("regularMarketPrice")
        shares_outstanding = info.get("sharesOutstanding")
        beta               = info.get("beta", 1.0) or 1.0
        market_cap         = info.get("marketCap")
        total_debt         = info.get("totalDebt", 0) or 0
        total_cash         = info.get("totalCash", 0) or 0
        eps                = info.get("trailingEps")
        pe_ratio           = info.get("trailingPE")
        book_value         = info.get("bookValue")
        sector             = info.get("sector", "Unknown")
        company_name       = info.get("longName", raw_ticker)
        roe                = info.get("returnOnEquity")

        if not current_price:
            return {"error": "Current price not available for this ticker."}
        if not shares_outstanding or shares_outstanding == 0:
            return {"error": "Shares outstanding not available."}

        # ── Helpers ───────────────────────────────────────────────────────────
        def find_row(df, *keywords):
            for idx in df.index:
                if all(k.lower() in idx.lower() for k in keywords):
                    return idx
            return None

        def safe_float(df, row_key, col=0):
            if row_key is None or df is None or df.empty:
                return None
            try:
                val = df.loc[row_key].iloc[col]
                if val is None or str(val) == "nan":
                    return None
                return float(val)
            except Exception:
                return None

        def upside(intrinsic, price):
            if intrinsic is None or price is None or price == 0:
                return None
            return round((intrinsic - price) / price * 100, 2)

        def signal(up):
            if up is None:
                return "N/A"
            if up > 30:  return "Strong Buy"
            if up > 10:  return "Buy"
            if up > -10: return "Hold"
            if up > -30: return "Sell"
            return "Strong Sell"

        # ── WACC ──────────────────────────────────────────────────────────────
        cost_of_equity = risk_free_rate + beta * (market_return - risk_free_rate)
        interest_row   = find_row(income_df, "interest", "expense")
        interest_exp   = abs(safe_float(income_df, interest_row) or 0.0)
        cost_of_debt   = max(0.03, min(interest_exp / total_debt, 0.15)) if total_debt > 0 and interest_exp > 0 else 0.06

        equity_val    = market_cap if market_cap else 1
        debt_val      = total_debt if total_debt else equity_val * 0.2
        total_capital = equity_val + debt_val

        # Approximate avg tax rate from income statement
        pretax_row = find_row(income_df, "pretax") or find_row(income_df, "income before tax")
        tax_row    = find_row(income_df, "tax", "provision") or find_row(income_df, "income tax")
        pretax_val = safe_float(income_df, pretax_row) or 0
        tax_val    = safe_float(income_df, tax_row)    or 0
        avg_tax    = max(0.05, min(abs(tax_val / pretax_val), 0.40)) if pretax_val != 0 else 0.25

        wacc = (
            (equity_val / total_capital) * cost_of_equity +
            (debt_val   / total_capital) * cost_of_debt * (1 - avg_tax)
        )

        results = {}

        # ─────────────────────────────────────────────────────────────────────
        # METHOD 1 — DCF (simplified FCFF for convergence, full model in /dcf)
        # ─────────────────────────────────────────────────────────────────────
        try:
            revenue_row = find_row(income_df, "total revenue") or find_row(income_df, "revenue")
            opex_row    = (find_row(income_df, "total expenses")
                           or find_row(income_df, "total operating expenses")
                           or find_row(income_df, "operating expense"))

            revenue     = safe_float(income_df, revenue_row) or 0
            opex        = abs(safe_float(income_df, opex_row) or 0)
            nop         = revenue - opex
            nop_at      = nop * (1 - avg_tax)

            capex_row   = find_row(income_df, "depreciation") or find_row(cashflow_df, "capital expenditure")
            capex       = abs(safe_float(income_df, capex_row) or safe_float(cashflow_df, capex_row) or 0)

            fcff_base   = nop_at - capex
            if wacc > terminal_growth_rate and fcff_base > 0:
                # Simple Gordon Growth on FCFF
                tv           = fcff_base * (1 + terminal_growth_rate) / (wacc - terminal_growth_rate)
                pv_fcff      = fcff_base / (1 + wacc)
                ev_dcf       = pv_fcff + tv / (1 + wacc)
                equity_dcf   = ev_dcf + total_cash - total_debt
                dcf_value    = equity_dcf / shares_outstanding
            else:
                dcf_value = None

            results["dcf"] = {
                "method":          "DCF (FCFF)",
                "description":     "Free Cash Flow to Firm — discounted at WACC",
                "intrinsic_value": round(dcf_value, 2) if dcf_value else None,
                "upside_pct":      upside(dcf_value, current_price),
                "signal":          signal(upside(dcf_value, current_price)),
                "confidence":      "High" if dcf_value and dcf_value > 0 else "Low",
                "inputs":          {"wacc": round(wacc, 4), "terminal_growth": terminal_growth_rate},
            }
        except Exception as e:
            results["dcf"] = {"method": "DCF (FCFF)", "intrinsic_value": None, "error": str(e)}

        # ─────────────────────────────────────────────────────────────────────
        # METHOD 2 — EARNINGS POWER VALUE (EPV)
        # Bruce Greenwald: value assuming zero growth — conservative floor
        # EPV = Normalized EBIT * (1 - tax) / WACC
        # ─────────────────────────────────────────────────────────────────────
        try:
            ebit_row = (find_row(income_df, "ebit")
                        or find_row(income_df, "operating income")
                        or find_row(income_df, "income from operations"))

            # Average EBIT over available years for normalization
            ebit_values = []
            if ebit_row:
                for col in range(min(len(income_df.columns), 5)):
                    v = safe_float(income_df, ebit_row, col)
                    if v and v > 0:
                        ebit_values.append(v)

            if not ebit_values:
                # Derive EBIT from revenue - opex if no direct row
                rev_vals  = [safe_float(income_df, revenue_row, c) for c in range(min(len(income_df.columns), 5))]
                opex_vals = [safe_float(income_df, opex_row,    c) for c in range(min(len(income_df.columns), 5))]
                ebit_values = [r - o for r, o in zip(rev_vals, opex_vals)
                               if r is not None and o is not None and (r - o) > 0]

            if ebit_values and wacc > 0:
                norm_ebit  = sum(ebit_values) / len(ebit_values)
                nopat      = norm_ebit * (1 - avg_tax)
                ev_epv     = nopat / wacc
                equity_epv = ev_epv + total_cash - total_debt
                epv_value  = equity_epv / shares_outstanding
            else:
                epv_value = None

            results["epv"] = {
                "method":          "Earnings Power Value",
                "description":     "Normalized EBIT × (1-t) / WACC — zero-growth conservative floor",
                "intrinsic_value": round(epv_value, 2) if epv_value else None,
                "upside_pct":      upside(epv_value, current_price),
                "signal":          signal(upside(epv_value, current_price)),
                "confidence":      "Medium",
                "inputs":          {"normalized_ebit_years": len(ebit_values), "wacc": round(wacc, 4)},
            }
        except Exception as e:
            results["epv"] = {"method": "Earnings Power Value", "intrinsic_value": None, "error": str(e)}

        # ─────────────────────────────────────────────────────────────────────
        # METHOD 3 — GRAHAM NUMBER
        # √(22.5 × EPS × Book Value per Share)
        # Benjamin Graham's formula — value investing classic
        # ─────────────────────────────────────────────────────────────────────
        try:
            # Get EPS from info or derive from net income / shares
            g_eps = eps
            if not g_eps or g_eps <= 0:
                ni_row = find_row(income_df, "net income")
                ni     = safe_float(income_df, ni_row)
                if ni and ni > 0 and shares_outstanding:
                    g_eps = ni / shares_outstanding

            # Get book value per share from info or derive from balance sheet
            g_bv = book_value
            if not g_bv or g_bv <= 0:
                eq_row = (find_row(balance_df, "stockholders equity")
                          or find_row(balance_df, "total equity")
                          or find_row(balance_df, "shareholders equity"))
                equity = safe_float(balance_df, eq_row)
                if equity and equity > 0 and shares_outstanding:
                    g_bv = equity / shares_outstanding

            if g_eps and g_bv and g_eps > 0 and g_bv > 0:
                graham_value = (22.5 * g_eps * g_bv) ** 0.5
            else:
                graham_value = None

            results["graham"] = {
                "method":          "Graham Number",
                "description":     "√(22.5 × EPS × Book Value) — Benjamin Graham's formula",
                "intrinsic_value": round(graham_value, 2) if graham_value else None,
                "upside_pct":      upside(graham_value, current_price),
                "signal":          signal(upside(graham_value, current_price)),
                "confidence":      "Medium",
                "inputs":          {
                    "eps":        round(g_eps, 2) if g_eps else None,
                    "book_value": round(g_bv,  2) if g_bv  else None,
                },
            }
        except Exception as e:
            results["graham"] = {"method": "Graham Number", "intrinsic_value": None, "error": str(e)}

        # ─────────────────────────────────────────────────────────────────────
        # METHOD 4 — RELATIVE VALUATION (Sector P/E)
        # Fair Value = EPS × Sector Median P/E
        # ─────────────────────────────────────────────────────────────────────
        try:
            sector_pe = SECTOR_PE_MEDIANS.get(sector, SECTOR_PE_MEDIANS["Unknown"])

            r_eps = eps
            if not r_eps or r_eps <= 0:
                ni_row = find_row(income_df, "net income")
                ni     = safe_float(income_df, ni_row)
                if ni and ni > 0 and shares_outstanding:
                    r_eps = ni / shares_outstanding

            if r_eps and r_eps > 0:
                relative_value = r_eps * sector_pe
            else:
                relative_value = None

            # P/E premium/discount to sector
            pe_vs_sector = None
            if pe_ratio and sector_pe:
                pe_vs_sector = round((pe_ratio - sector_pe) / sector_pe * 100, 1)

            results["relative"] = {
                "method":           "Relative Valuation",
                "description":      f"EPS × {sector} sector median P/E ({sector_pe}x)",
                "intrinsic_value":  round(relative_value, 2) if relative_value else None,
                "upside_pct":       upside(relative_value, current_price),
                "signal":           signal(upside(relative_value, current_price)),
                "confidence":       "Medium",
                "inputs":           {
                    "sector":          sector,
                    "sector_median_pe": sector_pe,
                    "stock_pe":        round(pe_ratio, 2) if pe_ratio else None,
                    "pe_vs_sector_pct": pe_vs_sector,
                },
            }
        except Exception as e:
            results["relative"] = {"method": "Relative Valuation", "intrinsic_value": None, "error": str(e)}

        # ─────────────────────────────────────────────────────────────────────
        # METHOD 5 — HISTORICAL P/E MEAN REVERSION
        # Fair Value = EPS × 5-year average P/E
        # Uses cache to avoid re-hitting yfinance
        # ─────────────────────────────────────────────────────────────────────
        try:
            hist_pe_value = None
            avg_hist_pe   = None
            hist_pe_years = 0

            # Use cached price history to avoid extra yfinance calls
            import yfinance as yf
            hist_cache_key = f"hist_price:{raw_ticker}"
            hist_price = _cache_get(hist_cache_key)
            if hist_price is None:
                try:
                    stock_yf   = yf.Ticker(raw_ticker)
                    hist_price = _with_retry(
                        lambda: stock_yf.history(period="5y", interval="1mo"),
                        max_retries=3, base_delay=2.0
                    )
                    if hist_price is not None and not hist_price.empty:
                        _cache_set(hist_cache_key, hist_price)
                except Exception:
                    hist_price = None

            if hist_price is not None and not hist_price.empty:
                # Get trailing EPS for each year from financials
                ni_row   = find_row(income_df, "net income")
                hist_pes = []

                if ni_row and shares_outstanding:
                    for col in range(min(len(income_df.columns), 5)):
                        ni_hist = safe_float(income_df, ni_row, col)
                        if ni_hist and ni_hist > 0:
                            eps_hist = ni_hist / shares_outstanding
                            # Get average price for that year
                            try:
                                year_str  = str(income_df.columns[col].year)
                                yr_prices = hist_price[hist_price.index.year == int(year_str)]["Close"]
                                if not yr_prices.empty:
                                    avg_price_yr = float(yr_prices.mean())
                                    pe_yr        = avg_price_yr / eps_hist
                                    if 0 < pe_yr < 200:   # sanity check
                                        hist_pes.append(pe_yr)
                            except Exception:
                                pass

                if hist_pes:
                    avg_hist_pe   = sum(hist_pes) / len(hist_pes)
                    hist_pe_years = len(hist_pes)
                    h_eps         = eps
                    if not h_eps or h_eps <= 0:
                        ni_row = find_row(income_df, "net income")
                        ni     = safe_float(income_df, ni_row)
                        if ni and ni > 0 and shares_outstanding:
                            h_eps = ni / shares_outstanding
                    if h_eps and h_eps > 0:
                        hist_pe_value = h_eps * avg_hist_pe

            results["historical_pe"] = {
                "method":          "Historical P/E Reversion",
                "description":     f"EPS × {hist_pe_years}-year avg P/E — mean reversion signal",
                "intrinsic_value": round(hist_pe_value, 2) if hist_pe_value else None,
                "upside_pct":      upside(hist_pe_value, current_price),
                "signal":          signal(upside(hist_pe_value, current_price)),
                "confidence":      "High" if hist_pe_years >= 4 else "Low",
                "inputs":          {
                    "avg_historical_pe": round(avg_hist_pe, 2) if avg_hist_pe else None,
                    "current_pe":        round(pe_ratio, 2)    if pe_ratio    else None,
                    "years_of_data":     hist_pe_years,
                },
            }
        except Exception as e:
            results["historical_pe"] = {"method": "Historical P/E Reversion", "intrinsic_value": None, "error": str(e)}

        # ─────────────────────────────────────────────────────────────────────
        # CONSENSUS — Trimmed mean of valid intrinsic values
        # ─────────────────────────────────────────────────────────────────────
        valid_values = [
            r["intrinsic_value"]
            for r in results.values()
            if r.get("intrinsic_value") is not None and r["intrinsic_value"] > 0
        ]

        consensus_value = None
        if valid_values:
            # Trimmed mean — drop highest and lowest if we have 4+ values
            sorted_vals = sorted(valid_values)
            if len(sorted_vals) >= 4:
                trimmed = sorted_vals[1:-1]
            else:
                trimmed = sorted_vals
            consensus_value = sum(trimmed) / len(trimmed)

        # ─────────────────────────────────────────────────────────────────────
        # CONFIDENCE SCORE — % of models agreeing on direction (Buy/Sell)
        # ─────────────────────────────────────────────────────────────────────
        buy_signals  = sum(1 for r in results.values()
                           if r.get("upside_pct") is not None and r["upside_pct"] > 10)
        sell_signals = sum(1 for r in results.values()
                           if r.get("upside_pct") is not None and r["upside_pct"] < -10)
        total_valid  = sum(1 for r in results.values() if r.get("upside_pct") is not None)

        if total_valid > 0:
            dominant    = max(buy_signals, sell_signals)
            confidence  = round((dominant / total_valid) * 100)
            direction   = "Bullish" if buy_signals >= sell_signals else "Bearish"
        else:
            confidence  = 0
            direction   = "Neutral"

        # ─────────────────────────────────────────────────────────────────────
        # CONVERGENCE SIGNAL
        # ─────────────────────────────────────────────────────────────────────
        consensus_upside = upside(consensus_value, current_price)

        if confidence >= 80 and consensus_upside and consensus_upside > 20:
            convergence_signal = "Strong Buy"
        elif confidence >= 60 and consensus_upside and consensus_upside > 10:
            convergence_signal = "Buy"
        elif confidence >= 80 and consensus_upside and consensus_upside < -20:
            convergence_signal = "Strong Sell"
        elif confidence >= 60 and consensus_upside and consensus_upside < -10:
            convergence_signal = "Sell"
        else:
            convergence_signal = "Hold / Inconclusive"

        # ─────────────────────────────────────────────────────────────────────
        # BUY ZONE (Margin of Safety applied to consensus)
        # ─────────────────────────────────────────────────────────────────────
        buy_zone = round(consensus_value * (1 - margin_of_safety), 2) if consensus_value else None

        at_buy_zone = (
            buy_zone is not None
            and current_price is not None
            and current_price <= buy_zone * 1.05  # within 5% of buy zone
        )

        # ─────────────────────────────────────────────────────────────────────
        # VALUE RANGE (bear / base / bull)
        # ─────────────────────────────────────────────────────────────────────
        bear_value = min(valid_values) if valid_values else None
        bull_value = max(valid_values) if valid_values else None

        # ─────────────────────────────────────────────────────────────────────
        # RESPONSE
        # ─────────────────────────────────────────────────────────────────────
        return {
            "ticker":       raw_ticker,
            "company_name": company_name,
            "sector":       sector,
            "market":       market,
            "data_source":  data_source,
            "current_price": current_price,

            # Individual model results
            "models": results,

            # Consensus
            "consensus": {
                "intrinsic_value":   round(consensus_value, 2) if consensus_value else None,
                "upside_pct":        round(consensus_upside, 2) if consensus_upside else None,
                "convergence_signal": convergence_signal,
                "confidence_pct":    confidence,
                "direction":         direction,
                "models_used":       total_valid,
                "buy_signals":       buy_signals,
                "sell_signals":      sell_signals,
            },

            # Buy zone
            "buy_zone": {
                "price":          buy_zone,
                "mos_used":       margin_of_safety,
                "at_buy_zone":    at_buy_zone,
                "status":         "AT BUY ZONE ⚡" if at_buy_zone else (
                    f"{round((current_price - buy_zone) / buy_zone * 100, 1)}% above buy zone"
                    if buy_zone and current_price and current_price > buy_zone else "Below buy zone"
                ) if buy_zone else "N/A",
            },

            # Value range
            "value_range": {
                "bear": round(bear_value, 2) if bear_value else None,
                "base": round(consensus_value, 2) if consensus_value else None,
                "bull": round(bull_value, 2) if bull_value else None,
            },

            # Model assumptions
            "assumptions": {
                "wacc":                round(wacc, 4),
                "cost_of_equity":      round(cost_of_equity, 4),
                "cost_of_debt":        round(cost_of_debt, 4),
                "avg_tax_rate":        round(avg_tax, 4),
                "beta":                beta,
                "risk_free_rate":      risk_free_rate,
                "market_return":       market_return,
                "terminal_growth":     terminal_growth_rate,
            },
        }

    except Exception as e:
        return {"error": str(e)}
