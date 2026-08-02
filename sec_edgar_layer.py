"""
SEC EDGAR Data Layer for MiniTradeIQ
=====================================
Pulls US company financial statements directly from the SEC's official
EDGAR XBRL API — completely free, no API key, no rate-limit blocking like
yfinance. This is the SAME source FMP and other data vendors use.

Returns data in the SAME yfinance-compatible shape (income_df, balance_df,
cashflow_df + info dict) so dcf_endpoint.py / convergence_endpoint.py don't
need to change their find_row() logic.

Rate limit: SEC allows 10 req/sec. We need ~1 call per company (companyfacts
returns everything), so this is extremely efficient.

Requirements:
- A descriptive User-Agent header is MANDATORY (SEC blocks requests without it)
- CIK must be 10-digit zero-padded

Usage:
    info, income_df, balance_df, cashflow_df = get_sec_company_data("AAPL")
"""

SEC_LAYER_BUILD = "2026-07-25j (net income fallbacks)"


import time
import httpx
import pandas as pd

# SEC REQUIRES a descriptive User-Agent with contact info, or it blocks you.
# Replace with your real email before production.
SEC_HEADERS = {
    "User-Agent": "MiniTradeIQ youremail@example.com",
    "Accept-Encoding": "gzip, deflate",
}

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL   = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# SIC code ranges → sector (maps SEC's industry classification to our sectors)
def _sic_to_sector(sic: str) -> str:
    try:
        code = int(sic)
    except (ValueError, TypeError):
        return "Unknown"
    if 100   <= code <= 999:   return "Basic Materials"      # Agriculture
    if 1000  <= code <= 1499:  return "Energy"               # Mining
    if 1500  <= code <= 1799:  return "Industrials"          # Construction
    if 2000  <= code <= 2199:  return "Consumer Defensive"   # Food
    if 2200  <= code <= 2799:  return "Consumer Cyclical"    # Textiles/Apparel
    if 2800  <= code <= 2899:  return "Basic Materials"      # Chemicals
    if 2900  <= code <= 2999:  return "Energy"               # Petroleum
    if 3000  <= code <= 3999:  return "Industrials"          # Manufacturing
    if 4000  <= code <= 4899:  return "Industrials"          # Transport/Comms
    if 4900  <= code <= 4999:  return "Utilities"            # Utilities
    if 5000  <= code <= 5199:  return "Consumer Cyclical"    # Wholesale
    if 5200  <= code <= 5999:  return "Consumer Cyclical"    # Retail
    if 6000  <= code <= 6799:  return "Financial Services"   # Finance
    if 7000  <= code <= 8999:  return "Technology"           # Services/Tech
    return "Unknown"

# ── Ticker → CIK cache (loaded once, reused) ──────────────────────────────────
_TICKER_CIK_MAP: dict = {}
_CIK_CACHE: dict = {}
_CIK_CACHE_TTL = 86400  # 24 hours — financials change quarterly at most


def _load_ticker_cik_map():
    """Load and cache the SEC's ticker→CIK mapping (one-time, persisted to disk)."""
    global _TICKER_CIK_MAP
    if _TICKER_CIK_MAP:
        return _TICKER_CIK_MAP

    # Try loading from disk cache first (avoids re-fetching the 10MB file)
    import os, json
    cache_file = "/tmp/sec_ticker_cik_map.json"
    try:
        if os.path.exists(cache_file):
            age = time.time() - os.path.getmtime(cache_file)
            if age < 7 * 86400:  # 7 days
                with open(cache_file, "r") as f:
                    _TICKER_CIK_MAP = json.load(f)
                if _TICKER_CIK_MAP:
                    return _TICKER_CIK_MAP
    except Exception:
        pass

    headers = {"User-Agent": SEC_HEADERS["User-Agent"]}
    last_err = None
    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(2 ** attempt)  # 2s, 4s backoff
            with httpx.Client(timeout=20.0) as client:
                resp = client.get(SEC_TICKERS_URL, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                for entry in data.values():
                    ticker = entry.get("ticker", "").upper()
                    cik    = entry.get("cik_str")
                    title  = entry.get("title", "")
                    if ticker and cik:
                        _TICKER_CIK_MAP[ticker] = {"cik": cik, "title": title}
                # Persist to disk
                try:
                    with open(cache_file, "w") as f:
                        json.dump(_TICKER_CIK_MAP, f)
                except Exception:
                    pass
                return _TICKER_CIK_MAP
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = str(e)

    raise RuntimeError(f"SEC ticker map fetch failed: {last_err}")


def ticker_to_cik(ticker: str) -> str | None:
    """Resolve a ticker symbol to its 10-digit zero-padded CIK."""
    mapping = _load_ticker_cik_map()
    entry   = mapping.get(ticker.upper())
    if not entry:
        return None
    return str(entry["cik"]).zfill(10)


def company_title(ticker: str) -> str:
    mapping = _load_ticker_cik_map()
    entry   = mapping.get(ticker.upper())
    return entry["title"] if entry else ticker.upper()


# ── Fetch company facts (all XBRL data in one call) ────────────────────────────

_FACTS_CACHE_MAX = 3   # companyfacts JSONs are multi-MB — keep RAM bounded

# ── Disk cache ────────────────────────────────────────────────────────────────
# companyfacts JSONs are 5-50MB each; downloading 503 of them takes hours.
# The SEC data changes at most quarterly, but the PARSER changes constantly
# during development — so cache raw JSON on disk and re-parse locally.
# Set SEC_CACHE_DIR to change location, SEC_CACHE_DAYS=0 to force refresh.
import os as _os
import json as _json
import gzip as _gzip

_DISK_CACHE_DIR  = _os.environ.get("SEC_CACHE_DIR", "sec_cache")
_DISK_CACHE_DAYS = float(_os.environ.get("SEC_CACHE_DAYS", "7"))


def _disk_cache_path(cik: str) -> str:
    return _os.path.join(_DISK_CACHE_DIR, f"{cik}.json.gz")


def _disk_cache_read(cik: str):
    if _DISK_CACHE_DAYS <= 0:
        return None
    path = _disk_cache_path(cik)
    try:
        age_days = (time.time() - _os.path.getmtime(path)) / 86400.0
        if age_days > _DISK_CACHE_DAYS:
            return None
        with _gzip.open(path, "rt", encoding="utf-8") as fh:
            return _json.load(fh)
    except Exception:
        return None


def _disk_cache_write(cik: str, data: dict):
    try:
        _os.makedirs(_DISK_CACHE_DIR, exist_ok=True)
        with _gzip.open(_disk_cache_path(cik), "wt", encoding="utf-8") as fh:
            _json.dump(data, fh)
    except Exception:
        pass


def _fetch_company_facts(cik: str) -> dict:
    cache_key = f"sec_facts:{cik}"
    cached = _CIK_CACHE.get(cache_key)
    if cached and (time.time() - cached["time"]) < _CIK_CACHE_TTL:
        return cached["data"]

    disk = _disk_cache_read(cik)
    if disk is not None:
        while len(_CIK_CACHE) >= _FACTS_CACHE_MAX:
            oldest = min(_CIK_CACHE, key=lambda k: _CIK_CACHE[k]["time"])
            del _CIK_CACHE[oldest]
        _CIK_CACHE[cache_key] = {"data": disk, "time": time.time()}
        return disk

    url = SEC_FACTS_URL.format(cik=cik)
    time.sleep(0.12)  # respect 10 req/sec limit
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=SEC_HEADERS)
    if resp.status_code != 200:
        raise RuntimeError(f"SEC companyfacts failed for CIK {cik}: {resp.status_code}")

    data = resp.json()
    _disk_cache_write(cik, data)
    # Evict oldest entries beyond the cap (prevents unbounded RAM growth
    # on small servers — each cached facts dict can be tens of MB)
    while len(_CIK_CACHE) >= _FACTS_CACHE_MAX:
        oldest = min(_CIK_CACHE, key=lambda k: _CIK_CACHE[k]["time"])
        del _CIK_CACHE[oldest]
    _CIK_CACHE[cache_key] = {"data": data, "time": time.time()}
    return data


# ── Extract a concept's annual values ──────────────────────────────────────────

def _get_concept_annual(facts: dict, *tags, unit: str = "USD") -> dict:
    """
    Extract annual (FY) values for the first matching XBRL tag.
    Returns {year: value} dict keyed by the PERIOD END YEAR (reliable),
    not the 'fy' filing-year field (unreliable — a 10-K tags multiple
    years of comparatives with the same fy).
    Tries multiple tag names since companies use different ones.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    from datetime import datetime

    for tag in tags:
        if tag not in us_gaap:
            continue
        units = us_gaap[tag].get("units", {})
        unit_data = units.get(unit) or (list(units.values())[0] if units else [])

        annual = {}
        for item in unit_data:
            form  = item.get("form", "")
            val   = item.get("val")
            start = item.get("start")
            end   = item.get("end")

            # Accept 10-K, 10-K/A (amended), 20-F (foreign filers)
            if not (str(form).startswith("10-K") or str(form).startswith("20-F")) \
                    or val is None or not end:
                continue

            # Period must be a full year (~365 days) — skip quarterly chunks
            if start and end:
                try:
                    d0 = datetime.fromisoformat(start)
                    d1 = datetime.fromisoformat(end)
                    days = (d1 - d0).days
                    if days < 300 or days > 400:
                        continue
                except Exception:
                    continue
            else:
                continue

            # Key by the period END YEAR — this is the actual data year
            try:
                period_year = datetime.fromisoformat(end).year
            except Exception:
                continue

            # Keep the value; later filings overwrite earlier (latest restatement wins)
            annual[period_year] = val

        if annual:
            return dict(sorted(annual.items(), key=lambda x: x[0], reverse=True))

    return {}


def _get_concept_instant(facts: dict, *tags, unit: str = "USD") -> dict:
    """
    Extract instantaneous (balance sheet) values — point-in-time.
    Keyed by the period END YEAR (reliable), not the fy field.
    Returns {year: value} most recent first.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    from datetime import datetime

    for tag in tags:
        if tag not in us_gaap:
            continue
        units = us_gaap[tag].get("units", {})
        unit_data = units.get(unit) or (list(units.values())[0] if units else [])

        annual = {}
        for item in unit_data:
            form = item.get("form", "")
            val  = item.get("val")
            end  = item.get("end")
            # Accept 10-K, 10-K/A (amended), 20-F (foreign filers)
            if not (str(form).startswith("10-K") or str(form).startswith("20-F")) \
                    or val is None or not end:
                continue
            try:
                period_year = datetime.fromisoformat(end).year
            except Exception:
                continue
            # Latest filing wins for each year-end
            annual[period_year] = val

        if annual:
            return dict(sorted(annual.items(), key=lambda x: x[0], reverse=True))

    return {}


# ── Build yfinance-shaped DataFrames ───────────────────────────────────────────

def _series_to_columns(*concept_dicts, limit=6):
    """Find common fiscal years across concepts, return sorted year list."""
    all_years = set()
    for d in concept_dicts:
        all_years.update(d.keys())
    years = sorted(all_years, reverse=True)[:limit]
    return years



# ── Industry statement-structure profiles ────────────────────────────────────
# Different industries publish fundamentally different income statements.
# Rather than guessing with a blanket rule, detect which structure THIS filer
# uses from the tags present in its own XBRL, then apply that industry's
# composition rules: an explicit total line when the filer reports one,
# otherwise the sum of that industry's standard revenue components.

def _has_tag(facts: dict, tag: str) -> bool:
    return tag in facts.get("facts", {}).get("us-gaap", {})


def _latest_val(facts: dict, tag: str):
    d = _get_concept_annual(facts, tag) or {}
    if not d:
        return None
    return d[max(d.keys())]


def _material(facts: dict, tag: str, ref: float, threshold: float = 0.2) -> bool:
    """True only if this tag carries a MATERIAL share of the company's income
    statement. Many ordinary companies incidentally report a small
    OperatingLeaseLeaseIncome (subleasing office space) — presence alone must
    never reclassify them as a REIT."""
    v = _latest_val(facts, tag)
    return v is not None and ref and v >= threshold * ref


def _detect_filer_profile(facts: dict) -> str:
    # Reference scale: the largest ordinary revenue line reported
    ref = 0.0
    for t in ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
              "SalesRevenueNet"):
        v = _latest_val(facts, t)
        if v and v > ref:
            ref = v

    # If the filer publishes NO conventional revenue line, it is definitively
    # not a standard filer — materiality cannot be judged against a zero
    # reference, so fall back to presence-based classification.
    if not ref:
        if _has_tag(facts, "PremiumsEarnedNet"):
            return "insurance"
        if (_has_tag(facts, "InterestAndDividendIncomeOperating")
                or _has_tag(facts, "InterestIncomeExpenseNet")):
            return "bank"
        if (_has_tag(facts, "OperatingLeaseLeaseIncome")
                or _has_tag(facts, "RealEstateRevenueNet")):
            return "reit"
        if _has_tag(facts, "RegulatedAndUnregulatedOperatingRevenue"):
            return "utility"

    if _material(facts, "PremiumsEarnedNet", ref):
        return "insurance"
    if (_material(facts, "InterestAndDividendIncomeOperating", ref)
            or _material(facts, "InterestIncomeExpenseNet", ref)):
        return "bank"
    if (_material(facts, "OperatingLeaseLeaseIncome", ref, 0.5)
            or _material(facts, "RealEstateRevenueNet", ref, 0.5)):
        return "reit"
    if _has_tag(facts, "RegulatedAndUnregulatedOperatingRevenue"):
        return "utility"
    if (_has_tag(facts, "PaymentsToExploreAndDevelopOilAndGasProperties")
            or _has_tag(facts, "DepletionOfOilAndGasProperties")
            or _has_tag(facts, "PaymentsToAcquireOilAndGasProperty")):
        return "energy"
    return "standard"


REVENUE_RULES = {
    # totals: authoritative single-line revenue, in preference order
    # parts:  components to SUM when the filer reports no total line
    "reit": {
        "totals": ["Revenues", "RealEstateRevenueNet"],
        "parts":  ["OperatingLeaseLeaseIncome", "TenantReimbursementRevenue",
                   "RevenueFromContractWithCustomerExcludingAssessedTax"],
    },
    "bank": {
        "totals": ["RevenuesNetOfInterestExpense", "Revenues"],
        "parts":  ["InterestAndDividendIncomeOperating", "NoninterestIncome"],
    },
    "insurance": {
        "totals": ["Revenues"],
        "parts":  ["PremiumsEarnedNet", "NetInvestmentIncome",
                   "RevenueFromContractWithCustomerExcludingAssessedTax"],
    },
    "utility": {
        "totals": ["Revenues", "RegulatedAndUnregulatedOperatingRevenue"],
        "parts":  [],
    },
    "standard": {
        # EXACT original priority order — standard filers must extract
        # identically to before these industry profiles were introduced
        "totals": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                   "Revenues",
                   "SalesRevenueNet",
                   "RevenueFromContractWithCustomerIncludingAssessedTax"],
        "parts":  [],
    },
}

# Expense structure differs by industry too: insurers report
# BenefitsLossesAndExpenses; banks report NoninterestExpense; REITs and
# utilities report OperatingExpenses AS their full operating cost (no COGS),
# whereas for a manufacturer OperatingExpenses excludes COGS and must not be
# used alone.
EXPENSE_RULES = {
    "insurance": ["BenefitsLossesAndExpenses", "CostsAndExpenses"],
    "bank":      ["NoninterestExpense", "CostsAndExpenses"],
    "reit":      ["CostsAndExpenses", "OperatingExpenses"],
    "utility":   ["CostsAndExpenses", "OperatingExpenses"],
    "standard":  ["CostsAndExpenses"],
}


# Capex and depreciation vary by industry as much as revenue does.
# REIT capex = acquisitions + development + capital improvements (three
# separate lines). Energy capex = exploration & development spending.
# Depreciation may be one combined line, or components to be summed
# (energy adds depletion, which dwarfs ordinary depreciation).
CAPEX_RULES = {
    "reit": {"totals": [],
             "parts": ["PaymentsToAcquireRealEstate",
                       "PaymentsToDevelopRealEstateAssets",
                       "PaymentsForCapitalImprovements",
                       "PaymentsToAcquirePropertyPlantAndEquipment"]},
    "energy": {"totals": [],
               "parts": ["PaymentsToExploreAndDevelopOilAndGasProperties",
                         "PaymentsToAcquireOilAndGasProperty",
                         "PaymentsToAcquirePropertyPlantAndEquipment"]},
    "bank": {"totals": ["PaymentsToAcquirePropertyPlantAndEquipment"], "parts": []},
    "insurance": {"totals": ["PaymentsToAcquirePropertyPlantAndEquipment"], "parts": []},
    "utility": {"totals": ["PaymentsToAcquirePropertyPlantAndEquipment",
                           "PaymentsToAcquireProductiveAssets"], "parts": []},
    "standard": {"totals": ["PaymentsToAcquirePropertyPlantAndEquipment",
                            "PaymentsToAcquireProductiveAssets"], "parts": []},
}

DEPRECIATION_RULES = {
    "energy": {"totals": ["DepreciationDepletionAndAmortization"],
               "parts": ["Depreciation", "DepletionOfOilAndGasProperties",
                         "AmortizationOfIntangibleAssets"]},
    "standard": {"totals": ["DepreciationDepletionAndAmortization",
                            "DepreciationAmortizationAndAccretionNet",
                            "DepreciationAndAmortization"],
                 "parts": ["Depreciation", "AmortizationOfIntangibleAssets"]},
}


def _compose_revenue(facts: dict, profile: str):
    """Standard extraction is authoritative. The industry series replaces it
    only if the standard series is structurally impossible MORE OFTEN — and
    the decision is made ONCE for the whole company, so the series never
    switches source between years."""
    std, std_basis = _revenue_by_rules(facts, REVENUE_RULES["standard"])
    if profile == "standard":
        if std:
            return std, std_basis
        # Standard extraction found nothing at all — try every industry's
        # component set rather than returning a company with no revenue.
        for _prof, _rules in REVENUE_RULES.items():
            if _prof == "standard" or not _rules.get("parts"):
                continue
            alt, alt_basis = _revenue_by_rules(facts, _rules)
            if alt:
                return alt, alt_basis
        return std, std_basis

    ind, ind_basis = _revenue_by_rules(
        facts, REVENUE_RULES.get(profile, REVENUE_RULES["standard"]))

    dep = _get_concept_annual(facts, "DepreciationDepletionAndAmortization",
                              "DepreciationAndAmortization") or {}
    iex = _get_concept_annual(facts, "InterestExpense") or {}

    def violation_rate(series):
        if not series:
            return 1.0
        bad = 0
        for y, v in series.items():
            if v is None or v <= 0:
                bad += 1
                continue
            d, x = dep.get(y), iex.get(y)
            if (d and v < d) or (x and v < x):
                bad += 1
        return bad / len(series)

    vs, vi = violation_rate(std), violation_rate(ind)
    if vi < vs:                    # industry series is structurally sounder
        return ind, ind_basis
    if std:
        return std, std_basis
    return ind, ind_basis


def _compose_by_rules(facts: dict, rules: dict):
    """Totals first (authoritative single line); else SUM the components."""
    result, basis = {}, {}
    for tag in rules.get("totals", []):
        for y, v in (_get_concept_annual(facts, tag) or {}).items():
            if v is not None and y not in result:
                result[y] = v
                basis[y]  = f"total:{tag}"
    parts = rules.get("parts", [])
    if parts:
        part_data = [(t, _get_concept_annual(facts, t) or {}) for t in parts]
        years = set()
        for _, d in part_data:
            years |= set(d.keys())
        for y in years:
            if y in result:
                continue
            present = [(t, d[y]) for t, d in part_data if d.get(y) is not None]
            if present:
                result[y] = sum(v for _, v in present)
                basis[y]  = "sum:" + "+".join(t for t, _ in present)
    return result, basis


BALANCE_RULES = {
    "reit": {
        "net_ppe": ["RealEstateInvestmentPropertyNet",
                    "RealEstateInvestmentPropertyAtCost",
                    "PropertyPlantAndEquipmentNet"],
        "cash":    ["CashAndCashEquivalentsAtCarryingValue"],
        "investments": ["EquityMethodInvestments", "LongTermInvestments"],
    },
    "bank": {
        "net_ppe": ["PropertyPlantAndEquipmentNet", "PremisesAndEquipmentNet"],
        "cash":    ["CashAndDueFromBanks", "CashAndCashEquivalentsAtCarryingValue",
                    "InterestBearingDepositsInBanks"],
        "investments": ["AvailableForSaleSecuritiesDebtSecurities",
                        "HeldToMaturitySecurities", "LongTermInvestments"],
    },
    "insurance": {
        "net_ppe": ["PropertyPlantAndEquipmentNet"],
        "cash":    ["CashAndCashEquivalentsAtCarryingValue"],
        "investments": ["DebtSecuritiesAvailableForSaleExcludingAccruedInterest",
                        "AvailableForSaleSecuritiesDebtSecurities",
                        "HeldToMaturitySecurities", "LongTermInvestments"],
    },
    "standard": {
        "net_ppe": ["PropertyPlantAndEquipmentNet"],
        "cash":    ["CashAndCashEquivalentsAtCarryingValue",
                    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
        "investments": ["LongTermInvestments", "MarketableSecuritiesNoncurrent",
                        "AvailableForSaleSecuritiesNoncurrent"],
    },
}


def _compose_instant(facts: dict, tags: list):
    """First authoritative instant tag wins (balance-sheet lines are totals,
    not components — summing them would double-count)."""
    out = {}
    for tag in tags:
        for y, v in (_get_concept_instant(facts, tag) or {}).items():
            if v is not None and y not in out:
                out[y] = v
    return out


def _revenue_by_rules(facts: dict, rules: dict):
    """Pick ONE concept for the whole series, never a different tag per year.

    A source that flips mid-series (Revenues in FY2024, a small fee-income
    tag in FY2023) manufactures fake growth rates like -84% / +579% — and
    DCF projections are driven by exactly those year-over-year changes, so
    an inconsistent series is more damaging than a uniformly imperfect one.
    Selection = best year coverage, ties broken by the rules' priority order.
    """
    # Select by year coverage first. Among concepts with (near-)equal
    # coverage, prefer the one with the larger median value: an aggregate
    # revenue line cannot be SMALLER than a concept nested inside it
    # (ASC-606 contract revenue is a subset of total Revenues). Extra Space
    # Storage reports both — Revenues ~$3.3B and contract revenue ~$0.16B of
    # tenant fees — and list-order tiebreaking picked the $0.16B line.
    import statistics as _stats
    _cands = []
    for tag in rules["totals"]:
        d = _get_concept_annual(facts, tag) or {}
        if d:
            _cands.append((tag, d))

    best_tag, best_data, best_cov = None, {}, 0
    if _cands:
        _top_cov = max(len(d) for _, d in _cands)
        _finalists = [(t, d) for t, d in _cands if len(d) >= _top_cov - 1]
        best_tag, best_data = max(
            _finalists,
            key=lambda td: _stats.median([v for v in td[1].values() if v is not None] or [0]))
        best_cov = len(best_data)

    if best_cov:
        result = dict(best_data)
        basis  = {y: f"total:{best_tag}" for y in best_data}
        # Gap-fill missing years from the other candidate tags, but ONLY when
        # the value is scale-consistent with its nearest known year. This keeps
        # full coverage (a missing latest year breaks the DCF entirely) while
        # still blocking the order-of-magnitude source flips that fabricated
        # -84% / +579% growth rates.
        for tag in rules["totals"]:
            if tag == best_tag:
                continue
            for y, v in (_get_concept_annual(facts, tag) or {}).items():
                if y in result or v is None or v <= 0:
                    continue
                nearest = min(result.keys(), key=lambda yy: abs(yy - y), default=None)
                if nearest is None:
                    continue
                ref = result[nearest]
                if ref and 0.33 <= v / ref <= 3.0:
                    result[y] = v
                    basis[y]  = f"fill:{tag}"

        # Still-missing years: compose from this industry's COMPONENTS.
        # Banks in particular often stop publishing a single Revenues line
        # for the newest year while the components (interest income +
        # non-interest income) are present — previously unreachable because
        # the totals branch never fell through to parts.
        parts = rules.get("parts", [])
        if parts:
            pdata = [(t, _get_concept_annual(facts, t) or {}) for t in parts]
            pdata = [(t, d) for t, d in pdata if d]
            if pdata:
                pyears = set()
                for _, d in pdata:
                    pyears |= set(d.keys())
                for y in sorted(pyears):
                    if y in result:
                        continue
                    present = [(t, d[y]) for t, d in pdata if d.get(y) is not None]
                    if not present:
                        continue
                    v = sum(val for _, val in present)
                    nearest = min(result.keys(), key=lambda yy: abs(yy - y),
                                  default=None)
                    if nearest is None:
                        continue
                    ref = result[nearest]
                    if ref and v > 0 and 0.33 <= v / ref <= 3.0:
                        result[y] = v
                        basis[y]  = "fill_sum:" + "+".join(t for t, _ in present)
        return result, basis

    # No single total line — compose from this industry's components, using
    # only years where the SAME component set is available
    parts = rules.get("parts", [])
    if not parts:
        return {}, {}
    part_data = [(t, _get_concept_annual(facts, t) or {}) for t in parts]
    part_data = [(t, d) for t, d in part_data if d]
    if not part_data:
        return {}, {}
    common_years = set.intersection(*[set(d.keys()) for _, d in part_data])
    if not common_years:                      # fall back to the widest part
        t, d = max(part_data, key=lambda td: len(td[1]))
        return dict(d), {y: f"part:{t}" for y in d}
    label = "sum:" + "+".join(t for t, _ in part_data)
    result = {y: sum(d[y] for _, d in part_data) for y in common_years}
    return result, {y: label for y in result}


def _build_dataframes(facts: dict):
    # ── Income statement concepts ─────────────────────────────────────────────
    # ── Filer profile: read the STRUCTURE of this company's own statements ──
    profile = _detect_filer_profile(facts)
    revenue, revenue_basis = _compose_revenue(facts, profile)

    cost_revenue = _get_concept_annual(facts, "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold")
    # CostsAndExpenses = TOTAL costs incl COGS + OpEx (what we want for NOP).
    # OperatingExpenses alone EXCLUDES COGS for most companies.
    costs_and_expenses = _get_concept_annual(facts, "CostsAndExpenses")
    operating_expenses = _get_concept_annual(facts, "OperatingExpenses")
    operating_income = _get_concept_annual(facts, "OperatingIncomeLoss")
    pretax       = _get_concept_annual(facts, "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments")
    tax_expense  = _get_concept_annual(facts, "IncomeTaxExpenseBenefit")
    interest_exp = _get_concept_annual(facts, "InterestExpense", "InterestExpenseDebt",
                                       "InterestExpenseBorrowings")
    # Interest income is REVENUE for a bank but other income for a corporate —
    # captured separately so downstream models never conflate the two
    interest_inc = _get_concept_annual(facts, "InvestmentIncomeInterest",
                                       "InterestAndDividendIncomeOperating")
    # Net income: NetIncomeLoss is the standard tag, but REITs and companies
    # with non-controlling interests frequently report only ProfitLoss (total
    # incl. NCI) or the available-to-common variant. AvalonBay had NI for just
    # 2 of 6 years and Essex none at all — which silently broke ROE, cash
    # conversion and tax-rate derivation downstream.
    net_income = _get_concept_annual(facts, "NetIncomeLoss")
    for _ni_tag in ("ProfitLoss",
                    "NetIncomeLossAvailableToCommonStockholdersBasic",
                    "IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest",
                    "IncomeLossFromContinuingOperations"):
        _extra = _get_concept_annual(facts, _ni_tag) or {}
        for _y, _v in _extra.items():
            if _y not in net_income and _v is not None:
                net_income[_y] = _v
    depreciation, depr_basis = _compose_by_rules(
        facts, DEPRECIATION_RULES.get(profile, DEPRECIATION_RULES["standard"]))

    # ── Balance sheet concepts (instantaneous) ────────────────────────────────
    current_assets      = _get_concept_instant(facts, "AssetsCurrent")
    current_liabilities = _get_concept_instant(facts, "LiabilitiesCurrent")
    _bs_rules = BALANCE_RULES.get(profile, BALANCE_RULES["standard"])
    cash                = _compose_instant(facts, _bs_rules["cash"])
    short_term_debt     = _get_concept_instant(facts, "LongTermDebtCurrent", "DebtCurrent")
    net_ppe             = _compose_instant(facts, _bs_rules["net_ppe"])
    long_term_debt      = _get_concept_instant(facts, "LongTermDebtNoncurrent", "LongTermDebt")
    total_equity        = _get_concept_instant(facts, "StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
    long_term_inv       = _compose_instant(facts, _bs_rules["investments"])
    minority_interest   = _get_concept_instant(facts, "MinorityInterest")

    # ── Cash flow concepts ────────────────────────────────────────────────────
    capex, capex_basis  = _compose_by_rules(
        facts, CAPEX_RULES.get(profile, CAPEX_RULES["standard"]))
    operating_cf        = _get_concept_annual(
        facts,
        "NetCashProvidedByUsedInOperatingActivities",
        # some filers report only the continuing-operations variant
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")

    # ── Determine common years ────────────────────────────────────────────────
    years = _series_to_columns(revenue, current_assets, net_income, limit=6)
    if not years:
        raise RuntimeError("No annual financial data found in SEC filings.")

    col_labels = [str(y) for y in years]

    def row(concept_dict):
        return [concept_dict.get(y) for y in years]

    # ── Total Expenses: pick ONE consistent series for all years ─────────────
    # A source that changes between years (CostsAndExpenses in FY24, a partial
    # line in FY23) fabricates growth rates that flow straight into the DCF
    # projection. Priority order preserved; COGS+OpEx requires BOTH present
    # (treating a missing COGS as zero produced McKesson's 98% margin).
    _exp_series = []
    if costs_and_expenses:
        _exp_series.append(("CostsAndExpenses", costs_and_expenses))
    _both = {y: cost_revenue[y] + operating_expenses[y]
             for y in set(cost_revenue) & set(operating_expenses)}
    if _both:
        _exp_series.append(("COGS+OpEx", _both))
    _derived = {y: revenue[y] - operating_income[y]
                for y in set(revenue) & set(operating_income)}
    if _derived:
        _exp_series.append(("Revenue-EBIT", _derived))
    for _tag in EXPENSE_RULES.get(profile, EXPENSE_RULES["standard"]):
        _d = _get_concept_annual(facts, _tag) or {}
        if _d:
            _exp_series.append((_tag, _d))

    # ── Choose the expense series, validated against accounting identities ──
    # Two failures found in live SEC data:
    #  (a) HP redefined CostsAndExpenses in FY2022 — it now excludes COGS, so
    #      the tag reports $8.2B against $43.9B of cost of revenue. Total
    #      expenses can NEVER be less than cost of revenue, so that series is
    #      rejected outright rather than trusted for having long history.
    #  (b) AvalonBay's CostsAndExpenses covers 8 years vs 16 for
    #      OperatingIncomeLoss — measuring coverage over ALL history rejected
    #      the correct series even though it covers every year we use.
    #      Coverage is therefore measured over the TARGET years only.
    def _covers(series):
        return sum(1 for y in years if series.get(y) is not None)

    def _series_valid(series):
        for y in years:
            v = series.get(y)
            if v is None:
                continue
            if v <= 0:
                return False
            cogs_y = cost_revenue.get(y)
            if cogs_y and v < cogs_y * 0.999:      # expenses < COGS: impossible
                return False
            d_y = depreciation.get(y)
            if d_y and v < d_y * 0.999:            # expenses < D&A: impossible
                return False
        # internal discontinuity not mirrored by revenue implies the filer
        # changed the tag's meaning mid-series
        ys = [y for y in years if series.get(y) is not None]
        for a, b in zip(ys, ys[1:]):
            va, vb = series[a], series[b]
            ra, rb = revenue.get(a), revenue.get(b)
            if vb and va / vb > 3.0 or (vb and va / vb < 0.34):
                if ra and rb and rb > 0 and 0.5 <= (ra / rb) <= 2.0:
                    return False                   # expenses jumped, revenue didn't
        return True

    _target_cov = max((_covers(d) for _, d in _exp_series), default=0)
    _chosen_name, _chosen = None, {}
    for _name, _d in _exp_series:                  # priority order preserved
        if _covers(_d) >= _target_cov - 1 and _covers(_d) > 0 and _series_valid(_d):
            _chosen_name, _chosen = _name, _d
            break
    if not _chosen:                                # relax validity, keep coverage
        for _name, _d in _exp_series:
            if _covers(_d) >= _target_cov - 1 and _covers(_d) > 0:
                _chosen_name, _chosen = _name, _d
                break
    if not _chosen and _exp_series:
        _chosen_name, _chosen = max(_exp_series, key=lambda nd: _covers(nd[1]))

    # Same treatment for expenses: fill gaps from other series when the value
    # is scale-consistent, so a single missing year doesn't void the row.
    _exp_final = dict(_chosen)
    for _name, _d in _exp_series:
        if _name == _chosen_name:
            continue
        for _y, _v in _d.items():
            if _y in _exp_final or _v is None or _v <= 0:
                continue
            _near = min(_exp_final.keys(), key=lambda yy: abs(yy - _y), default=None)
            if _near is None:
                continue
            _ref = _exp_final[_near]
            if _ref and 0.33 <= _v / _ref <= 3.0:
                _exp_final[_y] = _v

    total_expenses_row = [_exp_final.get(y) for y in years]

    # Income statement DataFrame (index names match find_row searches)
    income_data = {
        "Total Revenue":           row(revenue),
        "Cost Of Revenue":         row(cost_revenue),
        "Total Expenses":          total_expenses_row,
        "Operating Income":        row(operating_income),
        "EBIT":                    row(operating_income),
        "Pretax Income":           row(pretax),
        "Tax Provision":           row(tax_expense),
        "Interest Expense":        row(interest_exp),
        "Net Income":              row(net_income),
        "Reconciled Depreciation": row(depreciation),
    }
    income_df = pd.DataFrame(income_data, index=col_labels).T

    # ── Total Debt = Long Term Debt + Current/Short Term Debt ──────────────────
    total_debt_row = []
    for y in years:
        ltd = long_term_debt.get(y, 0) or 0
        std = short_term_debt.get(y, 0) or 0
        total_debt_row.append(ltd + std if (ltd or std) else None)

    # Balance sheet DataFrame
    balance_data = {
        "Total Current Assets":              row(current_assets),
        "Total Current Liabilities":         row(current_liabilities),
        "Cash And Cash Equivalents":         row(cash),
        "Current Portion Of Long Term Debt": row(short_term_debt),
        "Net PPE":                           row(net_ppe),
        "Long Term Investments":             row(long_term_inv),
        "Minority Interest":                 row(minority_interest),
        "Total Debt":                        total_debt_row,
        "Total Stockholders Equity":         row(total_equity),
    }
    balance_df = pd.DataFrame(balance_data, index=col_labels).T

    # Cash flow DataFrame
    cashflow_data = {
        "Depreciation Amortization": row(depreciation),
        "Capital Expenditure":       row(capex),
        "Operating Cash Flow":       row(operating_cf),
    }
    cashflow_df = pd.DataFrame(cashflow_data, index=col_labels).T

    # Structural metadata for downstream models: banks/insurers file
    # UNCLASSIFIED balance sheets — working capital is not merely missing,
    # it is undefined for them, and a cash-flow DCF is inappropriate.
    try:
        income_df.attrs["filer_profile"]  = profile
        income_df.attrs["nwc_applicable"] = profile not in ("bank", "insurance")
        income_df.attrs["revenue_basis"]  = revenue_basis
    except Exception:
        pass

    # Provenance for diagnosis: which concept produced each series
    try:
        income_df.attrs["extraction_report"] = {
            "profile":        profile,
            "revenue_basis":  revenue_basis,
            "expense_source": _chosen_name,
            "capex_basis":    capex_basis,
            "depr_basis":     depr_basis,
            "revenue":        dict(revenue),
            "total_expenses": {y: v for y, v in zip(years, total_expenses_row)},
            "operating_cf":   dict(operating_cf),
            "net_income":     dict(net_income),
            "depreciation":   dict(depreciation),
            "capex":          dict(capex),
        }
    except Exception:
        pass

    return income_df, balance_df, cashflow_df, net_income, total_equity


# ── Public entry point ──────────────────────────────────────────────────────────

def get_sec_company_data(ticker: str):
    """
    Returns (info, income_df, balance_df, cashflow_df) for a US ticker,
    using SEC EDGAR as the data source. Raises if ticker is not a US filer.

    Note: SEC provides financial statements but NOT live price/market cap.
    Those still need to come from a price source (yfinance quote, or a
    cheap price-only API). The `info` dict here fills financial fields and
    leaves price fields as None for the caller to populate.
    """
    cik = ticker_to_cik(ticker)
    if not cik:
        raise RuntimeError(f"{ticker} not found in SEC EDGAR (not a US filer?).")

    facts = _fetch_company_facts(cik)
    income_df, balance_df, cashflow_df, net_income, total_equity = _build_dataframes(facts)

    # Most recent year's values for the info dict
    def latest(df, row_name):
        try:
            return float(df.loc[row_name].iloc[0])
        except Exception:
            return None

    total_debt = latest(balance_df, "Total Debt") or 0.0
    total_cash = latest(balance_df, "Cash And Cash Equivalents") or 0.0
    equity_latest = latest(balance_df, "Total Stockholders Equity")

    # Shares outstanding from SEC (dei taxonomy)
    shares = _get_shares_outstanding(facts)

    # Sector from SEC submissions (SIC code) — needed for Relative Valuation
    sector = "Unknown"
    try:
        sub_url = SEC_SUBMISSIONS_URL.format(cik=cik)
        time.sleep(0.12)
        with httpx.Client(timeout=20.0) as client:
            sub_resp = client.get(sub_url, headers=SEC_HEADERS)
        if sub_resp.status_code == 200:
            sub_data = sub_resp.json()
            sic      = sub_data.get("sic", "")
            sector   = _sic_to_sector(sic)
    except Exception:
        pass

    info = {
        # Financial fields from SEC
        "longName":           company_title(ticker),
        "totalDebt":          total_debt,
        "totalCash":          total_cash,
        "netIncome":          latest(income_df, "Net Income"),
        "sharesOutstanding":  shares,
        # Price fields — filled by _enrich_with_price() below
        "currentPrice":       None,
        "regularMarketPrice": None,
        "beta":               None,
        "marketCap":          None,
        "trailingEps":        (latest(income_df, "Net Income") / shares) if (shares and latest(income_df, "Net Income")) else None,
        "trailingPE":         None,
        "priceToBook":        None,
        "bookValue":          (equity_latest / shares) if (shares and equity_latest) else None,
        "returnOnEquity":     (latest(income_df, "Net Income") / equity_latest) if (equity_latest and latest(income_df, "Net Income")) else None,
        "sector":             sector,
        "industry":           sector,
        "_data_source":       "sec_edgar",
        "_cik":               cik,
    }

    # Enrich with live price + beta + sector from a price source
    info = _enrich_with_price(ticker, info)

    return info, income_df, balance_df, cashflow_df


def _get_shares_outstanding(facts: dict):
    """Get shares outstanding from SEC dei taxonomy or us-gaap."""
    dei = facts.get("facts", {}).get("dei", {})
    for tag in ["EntityCommonStockSharesOutstanding"]:
        if tag in dei:
            units = dei[tag].get("units", {})
            shares_data = units.get("shares", [])
            if shares_data:
                # Most recent value
                latest = sorted(shares_data, key=lambda x: x.get("end", ""), reverse=True)
                for item in latest:
                    if item.get("val"):
                        return float(item["val"])

    # Fallback 1: us-gaap instant share counts
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in ["CommonStockSharesOutstanding", "CommonStockSharesIssued"]:
        if tag in us_gaap:
            units = us_gaap[tag].get("units", {})
            shares_data = units.get("shares", [])
            if shares_data:
                latest = sorted(shares_data, key=lambda x: x.get("end", ""), reverse=True)
                for item in latest:
                    if item.get("val"):
                        return float(item["val"])

    # Fallback 2: weighted-average shares (every filer reports this; handles
    # dual-class companies like META where dei share counts don't resolve)
    for tag in ["WeightedAverageNumberOfSharesOutstandingBasic",
                "WeightedAverageNumberOfDilutedSharesOutstanding"]:
        if tag in us_gaap:
            units = us_gaap[tag].get("units", {})
            shares_data = units.get("shares", [])
            annual = [x for x in shares_data
                      if str(x.get("form", "")).startswith(("10-K", "20-F")) and x.get("val")]
            if annual:
                latest = sorted(annual, key=lambda x: x.get("end", ""), reverse=True)
                return float(latest[0]["val"])
    return None


def _enrich_with_price(ticker: str, info: dict) -> dict:
    """
    SEC has no live price. Pull current price + market cap from yfinance
    fast_info — a lightweight, cached call that rarely rate-limits (it does
    NOT hit the heavy /quoteSummary endpoint that .info uses).

    We deliberately AVOID t.info here since that's the heavy call that gets
    rate-limited. Beta defaults to 1.0 if unavailable; sector stays Unknown.
    Price/PE are derived from fast_info + SEC-derived EPS.
    """
    import os as _os
    if _os.environ.get("MINITRADEIQ_INGEST"):
        # Background ingestion: financials only, no live price needed
        if info.get("beta") is None:
            info["beta"] = 1.0
        return info

    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        # fast_info is a lightweight object (NOT a dict) — use getattr only
        fast = t.fast_info

        price = None
        for attr in ("last_price", "lastPrice", "regular_market_price"):
            try:
                v = getattr(fast, attr, None)
                if v:
                    price = float(v)
                    break
            except Exception:
                continue

        if price:
            info["currentPrice"]       = price
            info["regularMarketPrice"] = price

            # Market cap from fast_info
            try:
                mc = getattr(fast, "market_cap", None)
                if mc:
                    info["marketCap"] = float(mc)
            except Exception:
                pass

            # Derive market cap from price × shares if not available
            if not info.get("marketCap") and info.get("sharesOutstanding"):
                info["marketCap"] = price * info["sharesOutstanding"]

            # Derive trailing P/E from price and SEC-derived EPS
            if info.get("trailingEps") and info["trailingEps"] > 0:
                info["trailingPE"] = price / info["trailingEps"]

            # Derive Price/Book from price and SEC-derived book value
            if info.get("bookValue") and info["bookValue"] > 0:
                info["priceToBook"] = price / info["bookValue"]

        # Beta: default to 1.0 (avoids the heavy .info call).
        # A more accurate beta can be computed from price history later.
        if info.get("beta") is None:
            info["beta"] = 1.0

    except Exception:
        # yfinance failed entirely — financials still work; default beta
        if info.get("beta") is None:
            info["beta"] = 1.0

    # ── Stooq fallback: if yfinance gave no price, try Stooq (free CSV) ──────
    if not info.get("currentPrice"):
        try:
            from fmp_data_layer import get_stooq_price
            sp = get_stooq_price(ticker, "us")
            if sp:
                info["currentPrice"]       = sp
                info["regularMarketPrice"] = sp
                if info.get("sharesOutstanding"):
                    info["marketCap"] = sp * info["sharesOutstanding"]
                if info.get("trailingEps") and info["trailingEps"] > 0:
                    info["trailingPE"] = sp / info["trailingEps"]
                if info.get("bookValue") and info["bookValue"] > 0:
                    info["priceToBook"] = sp / info["bookValue"]
        except Exception:
            pass

    return info


def diagnose(ticker: str) -> dict:
    """Return exactly what was extracted and WHICH us-gaap concept produced it.
    Removes guesswork when a company's numbers look wrong."""
    cik = ticker_to_cik(ticker)
    facts = _fetch_company_facts(cik)
    income_df, balance_df, cashflow_df, ni, eq = _build_dataframes(facts)
    rep = income_df.attrs.get("extraction_report", {})

    print(f"\n=== {ticker}  (CIK {cik}) ===")
    print(f"filer profile : {rep.get('profile')}")
    print(f"expense source: {rep.get('expense_source')}")
    print(f"\n{'year':<8}{'revenue':>16}{'expenses':>16}{'margin':>9}"
          f"{'OCF':>16}{'NI':>14}{'D&A':>14}{'capex':>14}")
    rev  = rep.get("revenue", {})
    exp  = rep.get("total_expenses", {})
    ocf  = rep.get("operating_cf", {})
    nirep= rep.get("net_income", {})
    dep  = rep.get("depreciation", {})
    cx   = rep.get("capex", {})
    for y in sorted(rev.keys(), reverse=True)[:6]:
        r, e = rev.get(y), exp.get(y)
        m = f"{(r-e)/r*100:7.1f}%" if (r and e is not None and r != 0) else "      -"
        def f(v):
            return f"{v:>16.4g}" if v is not None else f"{'-':>16}"
        print(f"{y:<8}{f(r)}{f(e)}{m:>9}{f(ocf.get(y))}"
              f"{str(round(nirep.get(y))) if nirep.get(y) is not None else '-':>14}"
              f"{str(round(dep.get(y))) if dep.get(y) is not None else '-':>14}"
              f"{str(round(cx.get(y))) if cx.get(y) is not None else '-':>14}")

    print("\nrevenue concept by year:")
    for y, b in sorted(rep.get("revenue_basis", {}).items(), reverse=True)[:6]:
        print(f"  {y}: {b}")

    print("\nAVAILABLE us-gaap revenue/expense concepts in this filing:")
    ug = facts.get("facts", {}).get("us-gaap", {})
    for tag in sorted(ug):
        low = tag.lower()
        if any(k in low for k in ("revenue", "costsandexpenses", "operatingexpenses",
                                  "costofrevenue", "costofgoods", "benefitslosses",
                                  "noninterestexpense", "operatingincomeloss")):
            annual = _get_concept_annual(facts, tag)
            if annual:
                latest = max(annual)
                print(f"  {tag:<62} {latest}: {annual[latest]:.4g}  ({len(annual)} yrs)")
    return rep
