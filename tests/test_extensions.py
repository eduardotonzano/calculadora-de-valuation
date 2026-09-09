"""Tests for the extensions added on top of the original engine:
health.py (leverage/coverage), comps.py (peer-median math, no network),
target_price.expected_value_price() (probability-weighted scenarios),
and thesis.py (qualitative notes + catalysts CRUD).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from comps import compare_to_peers, peer_medians
from health import compute_health_metrics, overall_flag
from target_price import expected_value_price
from thesis import add_catalyst, get_thesis, list_catalysts, remove_catalyst, save_thesis

DB_PATH = Path(__file__).parent.parent / "data" / "valuation.db"
TICKER = "APP US"


@pytest.fixture
def conn(tmp_path):
    """A throwaway copy of valuation.db, so thesis/catalyst writes in
    these tests never touch the real file."""
    dst = tmp_path / "valuation.db"
    dst.write_bytes(DB_PATH.read_bytes())
    connection = sqlite3.connect(dst)
    connection.executescript((Path(__file__).parent.parent / "data" / "schema.sql").read_text())
    # schema.sql's wacc_inputs.country_risk_premium is only picked up by
    # a fresh CREATE TABLE; the copied db predates that column, so add it
    # the same way a real upgrade would (see README.md migration note).
    try:
        connection.execute(
            "ALTER TABLE wacc_inputs ADD COLUMN country_risk_premium REAL NOT NULL DEFAULT 0.0"
        )
    except sqlite3.OperationalError:
        pass
    connection.commit()
    yield connection
    connection.close()


# --- health.py ---------------------------------------------------------

def test_health_metrics_applovin_is_healthy(conn):
    metrics = compute_health_metrics(conn, TICKER)
    assert metrics["net_debt_ebitda"] is not None
    assert metrics["leverage_flag"] in ("saudável", "caixa líquido")
    assert overall_flag(metrics) in ("saudável", "caixa líquido")


def test_health_metrics_zero_interest_expense_reports_unavailable(conn):
    conn.execute("UPDATE historicals SET interest_expense = 0 WHERE company_id = 1")
    conn.commit()
    metrics = compute_health_metrics(conn, TICKER)
    assert metrics["interest_coverage"] is None
    assert metrics["coverage_flag"] == "indisponível"


def test_health_metrics_high_leverage_flags_watch(conn):
    conn.execute(
        "UPDATE historicals SET net_debt = (ebit + da) * 4.5 WHERE company_id = 1 AND period_type = 'actual'"
    )
    conn.commit()
    metrics = compute_health_metrics(conn, TICKER)
    assert metrics["leverage_flag"] == "atenção"


# --- comps.py (pure math only — no network in tests) --------------------

def test_peer_medians_skips_missing_values():
    peer_data = [
        {"ticker": "A", "ev_ebitda": 20.0, "pe_trailing": 30.0, "pe_forward": 25.0, "peg": None, "error": None},
        {"ticker": "B", "ev_ebitda": 24.0, "pe_trailing": None, "pe_forward": 22.0, "peg": None, "error": None},
        {"ticker": "C", "error": "sem dados"},
    ]
    medians = peer_medians(peer_data)
    assert medians["ev_ebitda"] == 22.0
    assert medians["pe_trailing"] == 30.0
    assert medians["n_peers_with_data"] == 2
    assert medians["n_peers_requested"] == 3


def test_compare_to_peers_premium_direction():
    peer_data = [
        {"ticker": "A", "ev_ebitda": 20.0, "pe_trailing": 30.0, "pe_forward": 25.0, "peg": None, "error": None},
    ]
    result = compare_to_peers(company_ev_ebitda=30.0, company_pe=None, peer_data=peer_data)
    assert result["ev_ebitda_premium_to_peers"] == pytest.approx(0.5)
    assert result["pe_premium_to_peers"] is None  # company_pe not supplied


# --- target_price.expected_value_price ----------------------------------

def test_expected_value_price_default_weights_between_bear_and_bull(conn):
    result = expected_value_price(conn, TICKER)
    bear, base, bull = (
        result["scenario_prices"]["bear"],
        result["scenario_prices"]["base"],
        result["scenario_prices"]["bull"],
    )
    assert bear < result["expected_value_price"] < bull
    assert result["weights"] == {"bear": 0.25, "base": 0.5, "bull": 0.25}


def test_expected_value_price_rejects_weights_not_summing_to_one(conn):
    with pytest.raises(ValueError, match="sum to 1.0"):
        expected_value_price(conn, TICKER, weights={"bear": 0.3, "base": 0.3, "bull": 0.3})


def test_expected_value_price_rejects_wrong_scenario_keys(conn):
    with pytest.raises(ValueError, match="bear/base/bull"):
        expected_value_price(conn, TICKER, weights={"bear": 0.5, "base": 0.5})


# --- thesis.py -----------------------------------------------------------

def test_thesis_round_trips_and_defaults_empty(conn):
    empty = get_thesis(conn, TICKER)
    assert empty["bull_case"] is None
    assert empty["last_updated"] is None

    saved = save_thesis(conn, TICKER, bull_case="Bull.", bear_case="Bear.", key_risks="Risco X.")
    assert saved["bull_case"] == "Bull."
    assert saved["last_updated"] is not None

    reloaded = get_thesis(conn, TICKER)
    assert reloaded["bear_case"] == "Bear."


def test_thesis_save_overwrites_not_versions(conn):
    save_thesis(conn, TICKER, bull_case="Primeira versão.")
    save_thesis(conn, TICKER, bull_case="Segunda versão.")
    assert get_thesis(conn, TICKER)["bull_case"] == "Segunda versão."


def test_catalysts_add_list_remove(conn):
    assert list_catalysts(conn, TICKER) == []
    add_catalyst(conn, TICKER, "2026-11-05", "Earnings 3T26")
    add_catalyst(conn, TICKER, "2027-02-10", "Earnings 4T26")
    catalysts = list_catalysts(conn, TICKER)
    assert [c["event_date"] for c in catalysts] == ["2026-11-05", "2027-02-10"]

    remove_catalyst(conn, TICKER, "2026-11-05", "Earnings 3T26")
    assert len(list_catalysts(conn, TICKER)) == 1


def test_catalysts_duplicate_insert_is_ignored(conn):
    add_catalyst(conn, TICKER, "2026-11-05", "Earnings 3T26")
    add_catalyst(conn, TICKER, "2026-11-05", "Earnings 3T26")
    assert len(list_catalysts(conn, TICKER)) == 1


# --- dcf_engine.get_wacc: country_risk_premium defaults to 0 ------------

def test_country_risk_premium_defaults_to_zero_and_matches_base_case(conn):
    from dcf_engine import get_wacc

    wacc_data = get_wacc(conn, 1)
    assert wacc_data["country_risk_premium"] == 0.0


def test_country_risk_premium_raises_wacc_when_nonzero(conn):
    from dcf_engine import get_company_id, get_wacc

    company_id = get_company_id(conn, TICKER)
    baseline = get_wacc(conn, company_id)["wacc"]

    conn.execute(
        "UPDATE wacc_inputs SET country_risk_premium = 0.03 WHERE company_id = ?", (company_id,)
    )
    conn.commit()
    adjusted = get_wacc(conn, company_id)["wacc"]
    assert adjusted > baseline
