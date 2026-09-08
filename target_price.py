"""Target price by trading multiples: EV/EBITDA, P/E, and PEG.

Each `from_*` function follows the same pattern as dcf_engine.run_dcf():
it pulls its inputs from the database, applies one multiples method, and
returns a dict with both the inputs used and the resulting per-share
target price, so the methods can be laid out side by side against the DCF
result as a "football field" (see `football_field()` below).

Unlike the DCF, these are current (undiscounted) fair-value estimates: a
target multiple applied to a forward metric, mirroring how the source
file's own "TRADING MULTIPLES" block reads today's market-implied
multiples off FY2027E/FY2030E consensus estimates. FY2027E is used as the
default target year for the same reason the source file anchors there.

P/E and PEG are built on Street-consensus EPS (`historicals.eps_diluted_adjusted`),
which is only available for FY2026E-FY2030E — the source workbook has no
Bear/Bull EPS assumptions, so those two methods do not vary by scenario.
EV/EBITDA reuses dcf_engine's scenario-aware FCF projection and therefore
does support bear/base/bull.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from cli_utils import friendly_errors
from dcf_engine import get_company_id, get_wacc, project_financials, run_dcf

DEFAULT_TARGET_YEAR = 2027


def _row_to_dict(conn: sqlite3.Connection, sql: str, params: tuple) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _get_eps(conn: sqlite3.Connection, company_id: int, fiscal_year: int) -> float:
    row = _row_to_dict(
        conn,
        "SELECT eps_diluted_adjusted FROM historicals WHERE company_id = ? AND fiscal_year = ?",
        (company_id, fiscal_year),
    )
    if row is None or row["eps_diluted_adjusted"] is None:
        raise ValueError(f"No EPS data for fiscal year {fiscal_year}")
    return row["eps_diluted_adjusted"]


def _get_trading_comp(conn: sqlite3.Connection, company_id: int, metric: str, fiscal_year: int) -> float | None:
    row = _row_to_dict(
        conn,
        "SELECT value FROM trading_comps WHERE company_id = ? AND metric = ? AND fiscal_year = ?",
        (company_id, metric, fiscal_year),
    )
    return row["value"] if row else None


def from_ev_ebitda(
    conn: sqlite3.Connection,
    ticker: str,
    scenario: str = "base",
    target_year: int = DEFAULT_TARGET_YEAR,
    ev_ebitda_multiple: float | None = None,
) -> dict:
    """Target price = (projected EBITDA(target_year) x multiple - net debt) / shares.

    Defaults the multiple to the scenario's own terminal EV/EBITDA exit
    multiple (the only real multiple input in the source file); pass a
    peer/comp multiple explicitly to use something else.
    """
    company_id = get_company_id(conn, ticker)
    wacc_data = get_wacc(conn, company_id)

    projection = project_financials(conn, company_id, scenario)
    years = {row["fiscal_year"]: row for row in projection}
    if target_year not in years:
        raise ValueError(f"target_year must be one of {sorted(years)}")
    year_data = years[target_year]
    ebitda = year_data["ebit"] + year_data["da"]

    multiple_source = "user"
    if ev_ebitda_multiple is None:
        ev_ebitda_multiple = conn.execute(
            "SELECT exit_multiple_ebitda FROM terminal_assumptions WHERE company_id = ? AND scenario = ?",
            (company_id, scenario),
        ).fetchone()[0]
        multiple_source = "terminal_assumptions"

    enterprise_value = ebitda * ev_ebitda_multiple
    equity_value = enterprise_value - wacc_data["net_debt"]
    target_price = equity_value / wacc_data["shares_outstanding"]

    return {
        "method": "EV/EBITDA",
        "ticker": ticker,
        "scenario": scenario,
        "target_year": target_year,
        "ebitda": ebitda,
        "ev_ebitda_multiple": ev_ebitda_multiple,
        "multiple_source": multiple_source,
        "enterprise_value": enterprise_value,
        "net_debt": wacc_data["net_debt"],
        "equity_value": equity_value,
        "shares_outstanding": wacc_data["shares_outstanding"],
        "current_price": wacc_data["stock_price"],
        "target_price": target_price,
        "implied_upside": target_price / wacc_data["stock_price"] - 1,
    }


def from_pe(
    conn: sqlite3.Connection,
    ticker: str,
    target_year: int = DEFAULT_TARGET_YEAR,
    pe_multiple: float | None = None,
) -> dict:
    """Target price = P/E multiple x consensus EPS(target_year).

    Defaults the multiple to today's market-implied P/E for that year
    (from `trading_comps`) when available.
    """
    company_id = get_company_id(conn, ticker)
    wacc_data = get_wacc(conn, company_id)
    eps = _get_eps(conn, company_id, target_year)

    multiple_source = "user"
    if pe_multiple is None:
        pe_multiple = _get_trading_comp(conn, company_id, "PE", target_year)
        multiple_source = "trading_comps"
        if pe_multiple is None:
            raise ValueError(
                f"No trading_comps P/E for FY{target_year}; pass pe_multiple explicitly"
            )

    target_price = pe_multiple * eps

    return {
        "method": "P/E",
        "ticker": ticker,
        "target_year": target_year,
        "eps": eps,
        "pe_multiple": pe_multiple,
        "multiple_source": multiple_source,
        "current_price": wacc_data["stock_price"],
        "target_price": target_price,
        "implied_upside": target_price / wacc_data["stock_price"] - 1,
    }


def from_peg(
    conn: sqlite3.Connection,
    ticker: str,
    target_year: int = DEFAULT_TARGET_YEAR,
    base_year: int | None = None,
    target_peg: float = 1.0,
) -> dict:
    """Target price = implied P/E x consensus EPS(target_year), where the
    implied P/E is backed out from a target PEG ratio and the EPS CAGR
    between `base_year` and `target_year` (both from consensus EPS data).

    PEG = P/E / (EPS CAGR, as a whole number percent), inverted:
    implied P/E = target_peg x EPS CAGR%. target_peg defaults to 1.0, the
    textbook "fairly valued" heuristic (Bloomberg's own PEG for this stock
    is available in `trading_comps` for comparison, not as the default).
    """
    company_id = get_company_id(conn, ticker)
    wacc_data = get_wacc(conn, company_id)

    if base_year is None:
        base_year = _row_to_dict(
            conn,
            """SELECT MAX(fiscal_year) AS fy FROM historicals
               WHERE company_id = ? AND period_type = 'actual'""",
            (company_id,),
        )["fy"]

    eps_base = _get_eps(conn, company_id, base_year)
    eps_target = _get_eps(conn, company_id, target_year)
    years = target_year - base_year
    if years <= 0:
        raise ValueError("target_year must be after base_year")
    if eps_base <= 0 or eps_target <= 0:
        raise ValueError(
            f"Cannot compute EPS CAGR: EPS must be positive in both the base year "
            f"(FY{base_year}: {eps_base}) and the target year (FY{target_year}: "
            f"{eps_target}) — a negative base raised to a fractional power is undefined "
            "for this formula."
        )

    eps_cagr = (eps_target / eps_base) ** (1 / years) - 1
    implied_pe = target_peg * (eps_cagr * 100)
    target_price = implied_pe * eps_target

    return {
        "method": "PEG",
        "ticker": ticker,
        "base_year": base_year,
        "target_year": target_year,
        "eps_base": eps_base,
        "eps_target": eps_target,
        "eps_cagr": eps_cagr,
        "target_peg": target_peg,
        "implied_pe": implied_pe,
        "current_price": wacc_data["stock_price"],
        "target_price": target_price,
        "implied_upside": target_price / wacc_data["stock_price"] - 1,
    }


def football_field(conn: sqlite3.Connection, ticker: str, scenario: str = "base") -> list[dict]:
    """Line up DCF and all three multiples methods for side-by-side comparison."""
    dcf_result = run_dcf(conn, ticker, scenario)
    return [
        {"method": "DCF", "target_price": dcf_result["price_per_share"]},
        from_ev_ebitda(conn, ticker, scenario=scenario),
        from_pe(conn, ticker),
        from_peg(conn, ticker),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run target price methods for a company.")
    parser.add_argument("db_path", type=Path)
    parser.add_argument("ticker")
    parser.add_argument("scenario", nargs="?", default="base", choices=["bear", "base", "bull"])
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    try:
        with friendly_errors():
            print(f"{args.ticker} — Target Price Football Field ({args.scenario} case)")
            scenario_independent = {"P/E", "PEG"}
            for result in football_field(conn, args.ticker, args.scenario):
                tag = " (scenario-independent, consensus EPS)" if result["method"] in scenario_independent else ""
                print(f"  {result['method']:<10} ${result['target_price']:,.2f}{tag}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
