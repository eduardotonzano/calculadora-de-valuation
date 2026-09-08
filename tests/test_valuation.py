"""Regression tests for the valuation calculator.

Run with: pytest

Pins down the numbers this project was validated against (the source
Bloomberg MODL file's $388.54 base-case price, and the three-way
sensitivity-table mismatch it also contains), plus the edge cases found
during a full-project review: explicit_years bounds and target_year
data-coverage gaps that could otherwise crash app.py.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from dcf_engine import get_company_id, run_dcf
from sensitivity import sensitivity_growth_margin, sensitivity_wacc_exit_multiple
from target_price import from_ev_ebitda, from_pe, from_peg

DB_PATH = Path(__file__).parent.parent / "data" / "valuation.db"
TICKER = "APP US"

CLONE_COLUMNS = {
    "historicals": "fiscal_year,period_type,revenue,ebit,ebitda_adjusted,da,"
                   "interest_expense,pretax_income,tax_expense,net_income,"
                   "eps_diluted_adjusted,long_term_debt,net_debt,capex,nwc_change",
    "wacc_inputs": "risk_free_rate,beta,equity_risk_premium,stock_price,shares_outstanding",
    "scenario_assumptions": "scenario,fiscal_year,revenue_growth,ebit_margin,tax_rate,"
                             "da_pct_revenue,capex_pct_revenue,nwc_pct_delta_revenue",
    "terminal_assumptions": "scenario,exit_multiple_ebitda",
    "trading_comps": "metric,fiscal_year,value",
}


@pytest.fixture
def conn():
    connection = sqlite3.connect(DB_PATH)
    yield connection
    connection.close()


@pytest.fixture
def multi_company_conn(tmp_path):
    """A temp copy of valuation.db with a second, cloned company under a
    different ticker, to prove company_id scoping doesn't bleed across
    companies (the schema is designed to be multi-company but this was
    never actually tested with more than one before this review)."""
    db_copy = tmp_path / "multi.db"
    shutil.copy(DB_PATH, db_copy)
    connection = sqlite3.connect(db_copy)
    cur = connection.cursor()
    cur.execute(
        "INSERT INTO companies (ticker, name, currency, units, as_of_date) "
        "SELECT 'CLONE US', 'Clone Corp', currency, units, as_of_date "
        "FROM companies WHERE ticker = ?", (TICKER,),
    )
    new_id = cur.lastrowid
    old_id = get_company_id(connection, TICKER)
    for table, cols in CLONE_COLUMNS.items():
        cur.execute(
            f"INSERT INTO {table} (company_id,{cols}) SELECT ?,{cols} FROM {table} WHERE company_id = ?",
            (new_id, old_id),
        )
    connection.commit()
    yield connection
    connection.close()


# --------------------------------------------------------- DCF baseline --

def test_dcf_base_case_matches_source_file(conn):
    """C87 in the source Bloomberg MODL workbook, cell-by-cell validated."""
    result = run_dcf(conn, TICKER, "base")
    assert result["price_per_share"] == pytest.approx(388.54471895377645)


def test_dcf_thirteen_year_alternative(conn):
    """The one internally-consistent variant of the source file's own
    sensitivity tables (D110) — see README 'Segunda descoberta'."""
    result = run_dcf(conn, TICKER, "base", explicit_years=13)
    assert result["price_per_share"] == pytest.approx(412.1852731339656)


@pytest.mark.parametrize("bad_years", [0, -1, 14, 20])
def test_dcf_rejects_out_of_range_explicit_years(conn, bad_years):
    """explicit_years=0 used to IndexError; negative values silently sliced
    the projection list backwards and returned a wrong-but-plausible price."""
    with pytest.raises(ValueError):
        run_dcf(conn, TICKER, "base", explicit_years=bad_years)


def test_dcf_rejects_unknown_scenario(conn):
    with pytest.raises(ValueError):
        run_dcf(conn, TICKER, "mid")


def test_dcf_rejects_unknown_ticker(conn):
    with pytest.raises(ValueError):
        run_dcf(conn, "FAKE US", "base")


# --------------------------------------------------------- sensitivity --

def test_sensitivity_wacc_multiple_center_cell_matches_dcf(conn):
    dcf_price = run_dcf(conn, TICKER, "base")["price_per_share"]
    table = sensitivity_wacc_exit_multiple(conn, TICKER, "base", explicit_years=5)
    center = table["grid"][2][2]  # zero WACC delta, zero multiple delta
    assert center == pytest.approx(dcf_price)


def test_sensitivity_growth_margin_center_cell_matches_dcf(conn):
    dcf_price = run_dcf(conn, TICKER, "base")["price_per_share"]
    table = sensitivity_growth_margin(conn, TICKER, "base", explicit_years=5)
    center = table["grid"][2][2]
    assert center == pytest.approx(dcf_price)


def test_sensitivity_reproduces_source_files_own_thirteen_year_table(conn):
    """B108/F112 in the source file's Growth x Margin sensitivity table —
    the one internally-consistent one (see README)."""
    table = sensitivity_growth_margin(conn, TICKER, "base", explicit_years=13)
    assert table["grid"][0][0] == pytest.approx(204.29703188199804)
    assert table["grid"][-1][-1] == pytest.approx(865.4601924527196)


# ------------------------------------------------------------- multiples --

def test_from_ev_ebitda_matches_known_value(conn):
    result = from_ev_ebitda(conn, TICKER, scenario="base", target_year=2027)
    assert result["target_price"] == pytest.approx(344.146541586244)


def test_from_pe_raises_for_year_without_trading_comps(conn):
    """FY2026E/FY2028E/FY2029E have no market-implied P/E in the source
    file's TRADING MULTIPLES block (only FY2027E/FY2030E do). Found while
    reviewing why app.py's target_year dropdown could crash the page."""
    with pytest.raises(ValueError):
        from_pe(conn, TICKER, target_year=2026)


@pytest.mark.parametrize("year", [2027, 2030])
def test_from_pe_works_for_covered_years(conn, year):
    result = from_pe(conn, TICKER, target_year=year)
    assert result["target_price"] > 0


def test_from_peg_does_not_depend_on_trading_comps(conn):
    """Unlike P/E, PEG derives its own multiple from EPS CAGR, so it works
    for every consensus year (2026-2030) even though trading_comps only
    covers 2027/2030."""
    for year in range(2026, 2031):
        result = from_peg(conn, TICKER, target_year=year)
        assert result["target_price"] > 0


# -------------------------------------------------- multi-company scoping --

def test_multi_company_results_do_not_bleed(multi_company_conn):
    original = run_dcf(multi_company_conn, TICKER, "base")["price_per_share"]
    clone = run_dcf(multi_company_conn, "CLONE US", "base")["price_per_share"]
    assert original == pytest.approx(clone)  # CLONE US is a byte-for-byte copy
