"""
MiniTradeIQ Data Store — Postgres (Supabase) Layer
====================================================
Stores slow-changing financial data (statements, shares, sector) in YOUR
database so you never hit yfinance/SEC live for fundamentals again.

Fast-changing data (current price) is fetched live via a tiny separate call.

Design:
  - financials refreshed QUARTERLY (via nightly ingestion job)
  - prices fetched LIVE (small, fast, rarely rate-limited)
  - your DCF/Convergence read fundamentals instantly from this store

Tables:
  companies          — ticker, name, sector, market, cik, shares
  income_statements  — per company per year
  balance_sheets     — per company per year
  cash_flows         — per company per year

Setup:
  1. Create a free Supabase project → get the connection string
  2. Set DATABASE_URL env var on Render
  3. Run init_db() once to create tables
  4. Run the ingestion script to populate

Requires: psycopg2-binary  (add to requirements.txt)
"""

import os
import json
import psycopg2
import psycopg2.extras
import pandas as pd
from datetime import datetime


def _conn():
    """Get a database connection from the DATABASE_URL env var."""
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise RuntimeError("DATABASE_URL environment variable not set.")
    return psycopg2.connect(db_url)


# ── Schema ──────────────────────────────────────────────────────────────────────

def init_db():
    """Create all tables. Run once at setup."""
    ddl = """
    CREATE TABLE IF NOT EXISTS companies (
        ticker          TEXT PRIMARY KEY,
        name            TEXT,
        sector          TEXT,
        industry        TEXT,
        market          TEXT,              -- 'us' or 'india'
        cik             TEXT,              -- SEC CIK for US stocks
        shares_outstanding DOUBLE PRECISION,
        beta            DOUBLE PRECISION,
        total_debt      DOUBLE PRECISION,
        total_cash      DOUBLE PRECISION,
        book_value      DOUBLE PRECISION,
        eps             DOUBLE PRECISION,
        data_source     TEXT,
        updated_at      TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS income_statements (
        ticker          TEXT REFERENCES companies(ticker) ON DELETE CASCADE,
        fiscal_year     INTEGER,
        revenue         DOUBLE PRECISION,
        total_expenses  DOUBLE PRECISION,
        operating_income DOUBLE PRECISION,
        pretax_income   DOUBLE PRECISION,
        tax_provision   DOUBLE PRECISION,
        interest_expense DOUBLE PRECISION,
        net_income      DOUBLE PRECISION,
        depreciation    DOUBLE PRECISION,
        PRIMARY KEY (ticker, fiscal_year)
    );

    CREATE TABLE IF NOT EXISTS balance_sheets (
        ticker          TEXT REFERENCES companies(ticker) ON DELETE CASCADE,
        fiscal_year     INTEGER,
        current_assets  DOUBLE PRECISION,
        current_liabilities DOUBLE PRECISION,
        cash            DOUBLE PRECISION,
        cpltd           DOUBLE PRECISION,   -- current portion of long term debt
        net_ppe         DOUBLE PRECISION,
        long_term_investments DOUBLE PRECISION,
        minority_interest DOUBLE PRECISION,
        total_debt      DOUBLE PRECISION,
        total_equity    DOUBLE PRECISION,
        PRIMARY KEY (ticker, fiscal_year)
    );

    CREATE TABLE IF NOT EXISTS cash_flows (
        ticker          TEXT REFERENCES companies(ticker) ON DELETE CASCADE,
        fiscal_year     INTEGER,
        depreciation    DOUBLE PRECISION,
        capex           DOUBLE PRECISION,
        operating_cash_flow DOUBLE PRECISION,
        PRIMARY KEY (ticker, fiscal_year)
    );

    CREATE INDEX IF NOT EXISTS idx_income_ticker  ON income_statements(ticker);
    CREATE INDEX IF NOT EXISTS idx_balance_ticker ON balance_sheets(ticker);
    CREATE INDEX IF NOT EXISTS idx_cashflow_ticker ON cash_flows(ticker);
    CREATE INDEX IF NOT EXISTS idx_companies_market ON companies(market);
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
    print("✅ Database tables created.")


# ── Ingestion — write data into the store ──────────────────────────────────────

def upsert_company(info: dict, ticker: str, market: str, data_source: str):
    """Insert or update a company's slow-changing metadata."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO companies
                    (ticker, name, sector, industry, market, cik,
                     shares_outstanding, beta, total_debt, total_cash,
                     book_value, eps, data_source, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                ON CONFLICT (ticker) DO UPDATE SET
                    name = EXCLUDED.name,
                    sector = EXCLUDED.sector,
                    industry = EXCLUDED.industry,
                    market = EXCLUDED.market,
                    cik = EXCLUDED.cik,
                    shares_outstanding = EXCLUDED.shares_outstanding,
                    beta = EXCLUDED.beta,
                    total_debt = EXCLUDED.total_debt,
                    total_cash = EXCLUDED.total_cash,
                    book_value = EXCLUDED.book_value,
                    eps = EXCLUDED.eps,
                    data_source = EXCLUDED.data_source,
                    updated_at = NOW();
            """, (
                ticker, info.get("longName"), info.get("sector"), info.get("industry"),
                market, info.get("_cik"), info.get("sharesOutstanding"), info.get("beta"),
                info.get("totalDebt"), info.get("totalCash"), info.get("bookValue"),
                info.get("trailingEps"), data_source,
            ))
        conn.commit()


def upsert_statements(ticker: str, income_df: pd.DataFrame,
                      balance_df: pd.DataFrame, cashflow_df: pd.DataFrame):
    """Write income, balance, cashflow rows for each fiscal year."""

    def gv(df, row_name, col):
        try:
            v = df.loc[row_name].iloc[col]
            return None if (v is None or str(v) == "nan") else float(v)
        except Exception:
            return None

    years = [int(c) for c in income_df.columns if str(c).isdigit()]

    with _conn() as conn:
        with conn.cursor() as cur:
            for i, col_label in enumerate(income_df.columns):
                try:
                    fy = int(col_label)
                except (ValueError, TypeError):
                    continue

                # Income statement
                cur.execute("""
                    INSERT INTO income_statements
                        (ticker, fiscal_year, revenue, total_expenses,
                         operating_income, pretax_income, tax_provision,
                         interest_expense, net_income, depreciation)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (ticker, fiscal_year) DO UPDATE SET
                        revenue = EXCLUDED.revenue,
                        total_expenses = EXCLUDED.total_expenses,
                        operating_income = EXCLUDED.operating_income,
                        pretax_income = EXCLUDED.pretax_income,
                        tax_provision = EXCLUDED.tax_provision,
                        interest_expense = EXCLUDED.interest_expense,
                        net_income = EXCLUDED.net_income,
                        depreciation = EXCLUDED.depreciation;
                """, (
                    ticker, fy,
                    gv(income_df, "Total Revenue", i),
                    gv(income_df, "Total Expenses", i),
                    gv(income_df, "Operating Income", i),
                    gv(income_df, "Pretax Income", i),
                    gv(income_df, "Tax Provision", i),
                    gv(income_df, "Interest Expense", i),
                    gv(income_df, "Net Income", i),
                    gv(income_df, "Reconciled Depreciation", i),
                ))

                # Balance sheet
                cur.execute("""
                    INSERT INTO balance_sheets
                        (ticker, fiscal_year, current_assets, current_liabilities,
                         cash, cpltd, net_ppe, long_term_investments,
                         minority_interest, total_debt, total_equity)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (ticker, fiscal_year) DO UPDATE SET
                        current_assets = EXCLUDED.current_assets,
                        current_liabilities = EXCLUDED.current_liabilities,
                        cash = EXCLUDED.cash,
                        cpltd = EXCLUDED.cpltd,
                        net_ppe = EXCLUDED.net_ppe,
                        long_term_investments = EXCLUDED.long_term_investments,
                        minority_interest = EXCLUDED.minority_interest,
                        total_debt = EXCLUDED.total_debt,
                        total_equity = EXCLUDED.total_equity;
                """, (
                    ticker, fy,
                    gv(balance_df, "Total Current Assets", i),
                    gv(balance_df, "Total Current Liabilities", i),
                    gv(balance_df, "Cash And Cash Equivalents", i),
                    gv(balance_df, "Current Portion Of Long Term Debt", i),
                    gv(balance_df, "Net PPE", i),
                    gv(balance_df, "Long Term Investments", i),
                    gv(balance_df, "Minority Interest", i),
                    gv(balance_df, "Total Debt", i),
                    gv(balance_df, "Total Stockholders Equity", i),
                ))

                # Cash flow
                cur.execute("""
                    INSERT INTO cash_flows
                        (ticker, fiscal_year, depreciation, capex, operating_cash_flow)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (ticker, fiscal_year) DO UPDATE SET
                        depreciation = EXCLUDED.depreciation,
                        capex = EXCLUDED.capex,
                        operating_cash_flow = EXCLUDED.operating_cash_flow;
                """, (
                    ticker, fy,
                    gv(cashflow_df, "Depreciation Amortization", i),
                    gv(cashflow_df, "Capital Expenditure", i),
                    gv(cashflow_df, "Operating Cash Flow", i),
                ))
        conn.commit()


# ── Reading — rebuild yfinance-shaped DataFrames FROM the store ────────────────

def get_from_store(ticker: str, market: str = "us"):
    """
    Read a company's data from YOUR store and rebuild it into the same
    (info, income_df, balance_df, cashflow_df) shape the DCF expects.

    Returns None if the company isn't in the store yet (caller can then
    fall back to live fetch + ingest).
    """
    raw_ticker = ticker.upper()
    if market.lower() == "india" and not raw_ticker.endswith(".NS"):
        raw_ticker += ".NS"

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM companies WHERE ticker = %s", (raw_ticker,))
            company = cur.fetchone()
            if not company:
                return None

            cur.execute("SELECT * FROM income_statements WHERE ticker = %s ORDER BY fiscal_year DESC", (raw_ticker,))
            income_rows = cur.fetchall()

            cur.execute("SELECT * FROM balance_sheets WHERE ticker = %s ORDER BY fiscal_year DESC", (raw_ticker,))
            balance_rows = cur.fetchall()

            cur.execute("SELECT * FROM cash_flows WHERE ticker = %s ORDER BY fiscal_year DESC", (raw_ticker,))
            cashflow_rows = cur.fetchall()

    if not income_rows:
        return None

    years = [str(r["fiscal_year"]) for r in income_rows]

    def build_df(rows, field_map):
        data = {}
        for display_name, db_field in field_map.items():
            data[display_name] = [r.get(db_field) for r in rows]
        return pd.DataFrame(data, index=[str(r["fiscal_year"]) for r in rows]).T

    income_df = build_df(income_rows, {
        "Total Revenue":           "revenue",
        "Total Expenses":          "total_expenses",
        "Operating Income":        "operating_income",
        "EBIT":                    "operating_income",
        "Pretax Income":           "pretax_income",
        "Tax Provision":           "tax_provision",
        "Interest Expense":        "interest_expense",
        "Net Income":              "net_income",
        "Reconciled Depreciation": "depreciation",
    })

    balance_df = build_df(balance_rows, {
        "Total Current Assets":              "current_assets",
        "Total Current Liabilities":         "current_liabilities",
        "Cash And Cash Equivalents":         "cash",
        "Current Portion Of Long Term Debt": "cpltd",
        "Net PPE":                           "net_ppe",
        "Long Term Investments":             "long_term_investments",
        "Minority Interest":                 "minority_interest",
        "Total Debt":                        "total_debt",
        "Total Stockholders Equity":         "total_equity",
    }) if balance_rows else pd.DataFrame()

    cashflow_df = build_df(cashflow_rows, {
        "Depreciation Amortization": "depreciation",
        "Capital Expenditure":       "capex",
        "Operating Cash Flow":       "operating_cash_flow",
    }) if cashflow_rows else pd.DataFrame()

    # Info dict — price fields filled live by caller
    info = {
        "longName":           company["name"],
        "sector":             company["sector"],
        "industry":           company["industry"],
        "sharesOutstanding":  company["shares_outstanding"],
        "beta":               company["beta"] or 1.0,
        "totalDebt":          company["total_debt"],
        "totalCash":          company["total_cash"],
        "bookValue":          company["book_value"],
        "trailingEps":        company["eps"],
        "currentPrice":       None,   # filled live
        "regularMarketPrice": None,
        "marketCap":          None,
        "trailingPE":         None,
        "priceToBook":        None,
        "returnOnEquity":     None,
        "_cik":               company["cik"],
        "_data_source":       f"store ({company['data_source']})",
        "_updated_at":        str(company["updated_at"]),
    }

    return info, income_df, balance_df, cashflow_df


# ── Live price fetch (the ONE fast-changing thing we don't store) ─────────────

def get_live_price(ticker: str, market: str = "us"):
    """
    Fetch ONLY the current price — small, fast, rarely rate-limited.
    Everything else comes from the store.
    """
    raw_ticker = ticker.upper()
    if market.lower() == "india" and not raw_ticker.endswith(".NS"):
        raw_ticker += ".NS"

    try:
        import yfinance as yf
        t    = yf.Ticker(raw_ticker)
        fast = t.fast_info
        for attr in ("last_price", "lastPrice", "regular_market_price"):
            try:
                v = getattr(fast, attr, None)
                if v:
                    return float(v)
            except Exception:
                continue
    except Exception:
        pass
    return None


def store_has_ticker(ticker: str, market: str = "us") -> bool:
    raw_ticker = ticker.upper()
    if market.lower() == "india" and not raw_ticker.endswith(".NS"):
        raw_ticker += ".NS"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM companies WHERE ticker = %s", (raw_ticker,))
            return cur.fetchone() is not None
