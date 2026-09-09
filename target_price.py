"""Target price by trading multiples: EV/EBITDA, P/E, and PEG.

Each `from_*` function follows the same pattern as dcf_engine.run_dcf():
it pulls its inputs from the database, applies one multiples method, and
returns a dict with both the inputs used and the resulting per-share
target price, so the methods can be laid out side by side against the DCF
result as a "football field" (see `football_field()` below).

Unlike the DCF, `target_price` here is a **future, undiscounted** estimate
for `target_year` specifically — a target multiple applied to a forward
metric, mirroring how the source file's own "TRADING MULTIPLES" block
reads today's market-implied multiples off FY2027E/FY2030E consensus
estimates, and how sell-side research actually quotes a price target
(e.g. "$350, 12-18 months out" is the projected future price, not a
present-value number). FY2027E is used as the default target year for the
same reason the source file anchors there.

Each result also carries `present_value_target_price`: `target_price`
discounted back to today at the company's own WACC over `years_out`
(`target_year` minus the latest actual fiscal year). This is the number
that's actually comparable to the DCF's price (which already is a
present-value estimate) — `football_field()` uses it for exactly that
side-by-side comparison — while `target_price` stays the number a
research report would actually print.

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


def _latest_actual_fiscal_year(conn: sqlite3.Connection, company_id: int) -> int:
    return _row_to_dict(
        conn,
        """SELECT MAX(fiscal_year) AS fy FROM historicals
           WHERE company_id = ? AND period_type = 'actual'""",
        (company_id,),
    )["fy"]


def _discount_to_present(price: float, wacc: float, years_out: int) -> float:
    """`price`, expected at `years_out` years from now, brought back to a
    present-value estimate at the company's own WACC -- the same discount
    rate the DCF itself uses, so the result is genuinely comparable to
    run_dcf()'s price_per_share."""
    return price / (1 + wacc) ** years_out


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

    years_out = target_year - _latest_actual_fiscal_year(conn, company_id)
    pv_target_price = _discount_to_present(target_price, wacc_data["wacc"], years_out)

    return {
        "method": "EV/EBITDA",
        "ticker": ticker,
        "scenario": scenario,
        "target_year": target_year,
        "years_out": years_out,
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
        "present_value_target_price": pv_target_price,
        "present_value_implied_upside": pv_target_price / wacc_data["stock_price"] - 1,
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

    years_out = target_year - _latest_actual_fiscal_year(conn, company_id)
    pv_target_price = _discount_to_present(target_price, wacc_data["wacc"], years_out)

    return {
        "method": "P/E",
        "ticker": ticker,
        "target_year": target_year,
        "years_out": years_out,
        "eps": eps,
        "pe_multiple": pe_multiple,
        "multiple_source": multiple_source,
        "current_price": wacc_data["stock_price"],
        "target_price": target_price,
        "implied_upside": target_price / wacc_data["stock_price"] - 1,
        "present_value_target_price": pv_target_price,
        "present_value_implied_upside": pv_target_price / wacc_data["stock_price"] - 1,
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

    latest_actual_year = _latest_actual_fiscal_year(conn, company_id)
    if base_year is None:
        base_year = latest_actual_year

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

    years_out = target_year - latest_actual_year
    pv_target_price = _discount_to_present(target_price, wacc_data["wacc"], years_out)

    return {
        "method": "PEG",
        "ticker": ticker,
        "base_year": base_year,
        "target_year": target_year,
        "years_out": years_out,
        "eps_base": eps_base,
        "eps_target": eps_target,
        "eps_cagr": eps_cagr,
        "target_peg": target_peg,
        "implied_pe": implied_pe,
        "current_price": wacc_data["stock_price"],
        "target_price": target_price,
        "implied_upside": target_price / wacc_data["stock_price"] - 1,
        "present_value_target_price": pv_target_price,
        "present_value_implied_upside": pv_target_price / wacc_data["stock_price"] - 1,
    }


def expected_value_price(
    conn: sqlite3.Connection,
    ticker: str,
    weights: dict[str, float] | None = None,
) -> dict:
    """DCF price target weighted across bear/base/bull, using
    analyst-supplied probabilities -- not something the model infers.

    `weights` defaults to {'bear': 0.25, 'base': 0.5, 'bull': 0.25} (a
    common textbook default), must cover exactly the three scenarios,
    and must sum to 1.0 (within floating-point tolerance). The result
    reports both the per-scenario prices and the weights used, so the
    probability judgment behind the number is always visible next to it
    -- an expected value with no visible weights is unreviewable.
    """
    if weights is None:
        weights = {"bear": 0.25, "base": 0.5, "bull": 0.25}
    if set(weights) != {"bear", "base", "bull"}:
        raise ValueError(f"weights must cover exactly bear/base/bull, got {sorted(weights)}")
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"weights must sum to 1.0, got {total}")

    scenario_prices = {}
    for scenario in ("bear", "base", "bull"):
        scenario_prices[scenario] = run_dcf(conn, ticker, scenario)["price_per_share"]

    weighted_price = sum(scenario_prices[s] * weights[s] for s in ("bear", "base", "bull"))

    return {
        "ticker": ticker,
        "weights": dict(weights),
        "scenario_prices": scenario_prices,
        "expected_value_price": weighted_price,
    }


def football_field(conn: sqlite3.Connection, ticker: str, scenario: str = "base") -> list[dict]:
    """Line up DCF and all three multiples methods for side-by-side comparison.

    A method that can't run for this company/year (e.g. no trading_comps —
    always true for a company whose data was derived rather than extracted
    from a hand-built DCF tab, see data/load_from_modl.py) is skipped
    rather than raising, so one unavailable method doesn't take down the
    whole comparison.

    Every entry carries `present_value_target_price`: the DCF's own price
    already is one (`years_out=0`, present value by construction); each
    multiples method's is its future `target_price` discounted back to
    today at WACC (see module docstring). Use `present_value_target_price`
    for a chart that compares all four methods on the same basis; use
    `target_price` (and `target_year`) to show the number the way a
    research report would actually print it.
    """
    dcf_result = run_dcf(conn, ticker, scenario)
    results = [{
        "method": "DCF",
        "target_price": dcf_result["price_per_share"],
        "present_value_target_price": dcf_result["price_per_share"],
        "years_out": 0,
    }]
    for fn, kwargs in [
        (from_ev_ebitda, dict(scenario=scenario)),
        (from_pe, {}),
        (from_peg, {}),
    ]:
        try:
            results.append(fn(conn, ticker, **kwargs))
        except ValueError:
            continue
    return results


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
            results = football_field(conn, args.ticker, args.scenario)
            for result in results:
                tag = " (scenario-independent, consensus EPS)" if result["method"] in scenario_independent else ""
                horizon = (
                    "today"
                    if result["years_out"] == 0
                    else f"FY{result['target_year']}, {result['years_out']}y out"
                )
                print(
                    f"  {result['method']:<10} ${result['target_price']:,.2f} "
                    f"({horizon}) -> PV ${result['present_value_target_price']:,.2f}{tag}"
                )
            missing = {"EV/EBITDA", "P/E", "PEG"} - {r["method"] for r in results}
            if missing:
                print(f"  (indisponível: {', '.join(sorted(missing))} — sem dados suficientes para esse método)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
