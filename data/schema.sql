-- Schema for the valuation calculator database.
--
-- One company can hold many years of historicals/estimates, three scenario
-- tracks (bear/base/bull) of forward assumptions, one terminal-value
-- assumption per scenario, and a snapshot of the market-implied trading
-- multiples pulled straight from the source Bloomberg MODL file.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY,
    ticker      TEXT NOT NULL UNIQUE,      -- e.g. 'APP US'
    name        TEXT NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'USD',
    units       TEXT NOT NULL DEFAULT 'millions',
    as_of_date  TEXT,                      -- Bloomberg "As of" date, ISO 8601
    -- 'modl_tabs': scenario/terminal/WACC assumptions extracted from a
    -- hand-built DCF/WACC tab in the source workbook (e.g. AppLovin).
    -- 'derived': no such tab existed (the normal shape of a raw Bloomberg
    -- MODL export) -- data/load_from_modl.py computed them from
    -- historicals + supplied market data instead. app.py reads this to
    -- avoid describing a company's own source file as having a bug it
    -- never had.
    data_source TEXT NOT NULL DEFAULT 'modl_tabs' CHECK (data_source IN ('modl_tabs', 'derived'))
);

-- Annual actuals (period_type='actual') and Street-consensus estimates
-- (period_type='estimate'), one row per fiscal year, straight off the
-- "Multiple Periods" sheet of the Bloomberg MODL template.
CREATE TABLE IF NOT EXISTS historicals (
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
    shares_diluted        REAL,             -- diluted weighted-avg shares for that fiscal year, millions
    UNIQUE (company_id, fiscal_year)
);

-- Raw CAPM / capital-structure inputs. Cost of debt is derived in
-- dcf_engine.py from the latest actual year in `historicals`
-- (long_term_debt, interest_expense, tax_expense/pretax_income) rather than
-- duplicated here.
CREATE TABLE IF NOT EXISTS wacc_inputs (
    id                    INTEGER PRIMARY KEY,
    company_id            INTEGER NOT NULL REFERENCES companies(id),
    risk_free_rate        REAL NOT NULL,    -- 10Y Treasury
    beta                  REAL NOT NULL,    -- 5Y monthly beta
    equity_risk_premium   REAL NOT NULL,
    stock_price           REAL NOT NULL,    -- current price used for market cap
    shares_outstanding    REAL NOT NULL,    -- diluted shares, millions
    UNIQUE (company_id)
);

-- Forward operating assumptions per scenario/year: revenue growth, EBIT
-- margin, tax rate, D&A and CapEx as % of revenue, and NWC impact as % of
-- the change in revenue. Sourced one of two ways by data/load_from_modl.py:
-- extracted from a hand-built Bear/Base/Bull DCF tab when the workbook has
-- one (e.g. AppLovin), or derived formulaically from `historicals` alone
-- (consensus-derived growth fading to a terminal rate, trailing-average
-- margins) when it doesn't -- which is the normal shape of a raw Bloomberg
-- MODL export. See README.md.
CREATE TABLE IF NOT EXISTS scenario_assumptions (
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
CREATE TABLE IF NOT EXISTS terminal_assumptions (
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
CREATE TABLE IF NOT EXISTS trading_comps (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id),
    metric        TEXT NOT NULL,    -- 'PE', 'EV_EBITDA', 'FCF_YIELD', 'PEG'
    fiscal_year   INTEGER NOT NULL, -- the forward year the metric is measured against
    value         REAL NOT NULL,
    UNIQUE (company_id, metric, fiscal_year)
);
