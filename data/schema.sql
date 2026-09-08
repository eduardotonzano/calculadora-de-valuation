-- Schema for the valuation calculator database.
--
-- One company can hold many years of historicals/estimates, three scenario
-- tracks (bear/base/bull) of forward assumptions, one terminal-value
-- assumption per scenario, and a snapshot of the market-implied trading
-- multiples pulled straight from the source Bloomberg MODL file.

PRAGMA foreign_keys = ON;

CREATE TABLE companies (
    id          INTEGER PRIMARY KEY,
    ticker      TEXT NOT NULL UNIQUE,      -- e.g. 'APP US'
    name        TEXT NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'USD',
    units       TEXT NOT NULL DEFAULT 'millions',
    as_of_date  TEXT                       -- Bloomberg "As of" date, ISO 8601
);

-- Annual actuals (period_type='actual') and Street-consensus estimates
-- (period_type='estimate'), one row per fiscal year, straight off the
-- "Multiple Periods" sheet of the Bloomberg MODL template.
CREATE TABLE historicals (
    id                    INTEGER PRIMARY KEY,
    company_id            INTEGER NOT NULL REFERENCES companies(id),
    fiscal_year           INTEGER NOT NULL,
    period_type           TEXT NOT NULL CHECK (period_type IN ('actual', 'estimate')),
    revenue               REAL,
    ebit                  REAL,
    ebitda_adjusted       REAL,             -- Bloomberg "Adjusted EBITDA" (IS_COMPARABLE_EBITDA)
    da                    REAL,             -- depreciation & amortization
    interest_expense      REAL,             -- net interest expense
    pretax_income         REAL,
    tax_expense           REAL,
    net_income            REAL,
    eps_diluted_adjusted  REAL,
    long_term_debt        REAL,
    net_debt              REAL,             -- positive = net debt, negative = net cash
    capex                 REAL,             -- raw Bloomberg cash-flow sign (outflow is negative)
    nwc_change            REAL,             -- raw Bloomberg cash-flow sign, (use)/source of cash
    UNIQUE (company_id, fiscal_year)
);

-- Raw CAPM / capital-structure inputs. Cost of debt is derived in
-- dcf_engine.py from the latest actual year in `historicals`
-- (long_term_debt, interest_expense, tax_expense/pretax_income) rather than
-- duplicated here.
CREATE TABLE wacc_inputs (
    id                    INTEGER PRIMARY KEY,
    company_id            INTEGER NOT NULL REFERENCES companies(id),
    risk_free_rate        REAL NOT NULL,    -- 10Y Treasury
    beta                  REAL NOT NULL,    -- 5Y monthly beta
    equity_risk_premium   REAL NOT NULL,
    stock_price           REAL NOT NULL,    -- current price used for market cap
    shares_outstanding    REAL NOT NULL,    -- diluted shares, millions
    UNIQUE (company_id)
);

-- Forward operating assumptions per scenario/year, matching the
-- Bear/Base/Bull blocks on the DCF sheet: revenue growth, EBIT margin,
-- tax rate, D&A and CapEx as % of revenue, and NWC impact as % of the
-- change in revenue.
CREATE TABLE scenario_assumptions (
    id                     INTEGER PRIMARY KEY,
    company_id             INTEGER NOT NULL REFERENCES companies(id),
    scenario               TEXT NOT NULL CHECK (scenario IN ('bear', 'base', 'bull')),
    fiscal_year            INTEGER NOT NULL,
    revenue_growth         REAL NOT NULL,
    ebit_margin            REAL NOT NULL,
    tax_rate               REAL NOT NULL,
    da_pct_revenue         REAL NOT NULL,
    capex_pct_revenue      REAL NOT NULL,
    nwc_pct_delta_revenue  REAL NOT NULL,
    UNIQUE (company_id, scenario, fiscal_year)
);

-- Terminal-value assumption per scenario. Only the EV/EBITDA exit multiple
-- is an explicit input in the source file; the Gordon Growth rate is a
-- cross-check dcf_engine.py backs out from the exit-multiple terminal
-- value (see README.md).
CREATE TABLE terminal_assumptions (
    id                    INTEGER PRIMARY KEY,
    company_id            INTEGER NOT NULL REFERENCES companies(id),
    scenario              TEXT NOT NULL CHECK (scenario IN ('bear', 'base', 'bull')),
    exit_multiple_ebitda  REAL NOT NULL,
    UNIQUE (company_id, scenario)
);

-- Market-implied trading multiples read directly off the DCF sheet's
-- "TRADING MULTIPLES" block (current market cap/EV over forward consensus
-- metrics). Used as reference points for target_price.py, not fabricated
-- peer-comp data.
CREATE TABLE trading_comps (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id),
    metric        TEXT NOT NULL,    -- 'PE', 'EV_EBITDA', 'FCF_YIELD', 'PEG'
    fiscal_year   INTEGER NOT NULL, -- the forward year the metric is measured against
    value         REAL NOT NULL,
    UNIQUE (company_id, metric, fiscal_year)
);
