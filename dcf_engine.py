"""Discounted cash flow engine for the valuation calculator.

Projects unlevered free cash flow from `scenario_assumptions`, discounts it
to present value using the mid-year convention, and computes a terminal
value two ways (EV/EBITDA exit multiple and Gordon Growth), reconciling
them through an implied perpetuity growth rate.

KNOWN FINDING — 5 vs. 13 years of explicit projection
-------------------------------------------------------
The source Bloomberg MODL template builds a 13-year explicit forecast
(FY2026E-FY2038E: 5 years of Street-consensus estimates, then 8 more years
of assumption-driven fade), and it computes unlevered FCF for all 13 years.
But the DCF sheet's valuation summary only sums the present value of the
FIRST 5 years (FY2026-FY2030) and drops the terminal value straight off
year 5's EBITDA — the FCF computed for years 6-13 is built out on the sheet
but never enters the valuation. `run_dcf()` reproduces this behavior
exactly by default (`explicit_years=5`), because that is what the original
file's validated $388.54 base-case price target is built on. Pass
`explicit_years=13` to use the full projection instead — see README.md.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from cli_utils import friendly_errors

FIRST_FORECAST_YEAR = 2026
LAST_FORECAST_YEAR = 2038
TOTAL_FORECAST_YEARS = LAST_FORECAST_YEAR - FIRST_FORECAST_YEAR + 1  # 13


def get_company_id(conn: sqlite3.Connection, ticker: str) -> int:
    row = conn.execute("SELECT id FROM companies WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        raise ValueError(f"No company found for ticker {ticker!r}")
    return row[0]


def _row_to_dict(conn: sqlite3.Connection, sql: str, params: tuple) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def get_wacc(conn: sqlite3.Connection, company_id: int) -> dict:
    """Compute WACC from wacc_inputs (CAPM/market data) plus the latest
    actual year in historicals (cost of debt, capital structure)."""
    inputs = _row_to_dict(
        conn, "SELECT * FROM wacc_inputs WHERE company_id = ?", (company_id,)
    )
    latest_actual = _row_to_dict(
        conn,
        """SELECT * FROM historicals WHERE company_id = ? AND period_type = 'actual'
           ORDER BY fiscal_year DESC LIMIT 1""",
        (company_id,),
    )
    if inputs is None or latest_actual is None:
        raise ValueError("Missing wacc_inputs or actual historicals")

    cost_of_equity = inputs["risk_free_rate"] + inputs["beta"] * inputs["equity_risk_premium"]

    total_debt = latest_actual["long_term_debt"]
    if total_debt == 0:
        raise ValueError(
            f"Cannot compute cost of debt: FY{latest_actual['fiscal_year']} long_term_debt "
            "is 0 (interest expense / total debt is undefined for a debt-free company). "
            "The source Bloomberg template's own Kd formula has the same limitation."
        )
    if latest_actual["pretax_income"] == 0:
        raise ValueError(
            f"Cannot compute effective tax rate: FY{latest_actual['fiscal_year']} "
            "pretax_income is 0."
        )
    interest_expense = latest_actual["interest_expense"]
    pretax_cost_of_debt = interest_expense / total_debt
    effective_tax_rate = latest_actual["tax_expense"] / latest_actual["pretax_income"]
    after_tax_cost_of_debt = pretax_cost_of_debt * (1 - effective_tax_rate)

    market_cap = inputs["stock_price"] * inputs["shares_outstanding"]
    net_debt = latest_actual["net_debt"]
    enterprise_value = market_cap + net_debt
    if enterprise_value == 0:
        raise ValueError("Cannot compute capital structure weights: enterprise value (market cap + net debt) is 0.")
    equity_weight = market_cap / enterprise_value
    debt_weight = net_debt / enterprise_value

    wacc = equity_weight * cost_of_equity + debt_weight * after_tax_cost_of_debt

    return {
        "risk_free_rate": inputs["risk_free_rate"],
        "beta": inputs["beta"],
        "equity_risk_premium": inputs["equity_risk_premium"],
        "source": inputs.get("source") or "Fonte não registrada",
        "cost_of_equity": cost_of_equity,
        "total_debt": total_debt,
        "interest_expense": interest_expense,
        "pretax_cost_of_debt": pretax_cost_of_debt,
        "effective_tax_rate": effective_tax_rate,
        "after_tax_cost_of_debt": after_tax_cost_of_debt,
        "stock_price": inputs["stock_price"],
        "shares_outstanding": inputs["shares_outstanding"],
        "market_cap": market_cap,
        "net_debt": net_debt,
        "enterprise_value": enterprise_value,
        "equity_weight": equity_weight,
        "debt_weight": debt_weight,
        "wacc": wacc,
    }


def project_financials(
    conn: sqlite3.Connection,
    company_id: int,
    scenario: str,
    growth_delta: float = 0.0,
    margin_delta: float = 0.0,
) -> list[dict]:
    """Project revenue and unlevered FCF for all 13 forecast years
    (FY2026-FY2038) from scenario_assumptions, seeded off the latest actual
    year's revenue.

    `growth_delta`/`margin_delta` shift every year's revenue growth / EBIT
    margin by a flat amount (used by sensitivity.py), matching how the
    source file's own Growth x Margin sensitivity table perturbs both
    assumptions uniformly across the projection.
    """
    conn.row_factory = sqlite3.Row
    latest_actual = dict(conn.execute(
        """SELECT * FROM historicals WHERE company_id = ? AND period_type = 'actual'
           ORDER BY fiscal_year DESC LIMIT 1""",
        (company_id,),
    ).fetchone())

    assumptions = conn.execute(
        """SELECT * FROM scenario_assumptions WHERE company_id = ? AND scenario = ?
           ORDER BY fiscal_year""",
        (company_id, scenario),
    ).fetchall()
    if len(assumptions) != TOTAL_FORECAST_YEARS:
        raise ValueError(
            f"Expected {TOTAL_FORECAST_YEARS} years of '{scenario}' assumptions, got {len(assumptions)}"
        )

    projection = []
    prior_revenue = latest_actual["revenue"]
    for row in assumptions:
        a = dict(row)
        revenue = prior_revenue * (1 + a["revenue_growth"] + growth_delta)
        ebit = revenue * (a["ebit_margin"] + margin_delta)
        taxes = -ebit * a["tax_rate"]
        nopat = ebit + taxes
        da = revenue * a["da_pct_revenue"]
        capex = revenue * a["capex_pct_revenue"]
        nwc_use = (revenue - prior_revenue) * a["nwc_pct_delta_revenue"]
        ufcf = nopat + da - capex - nwc_use

        projection.append({
            "fiscal_year": a["fiscal_year"],
            "revenue": revenue,
            "revenue_growth": a["revenue_growth"],
            "ebit": ebit,
            "ebit_margin": a["ebit_margin"],
            "taxes": taxes,
            "tax_rate": a["tax_rate"],
            "nopat": nopat,
            "da": da,
            "capex": capex,
            "nwc_use": nwc_use,
            "ufcf": ufcf,
        })
        prior_revenue = revenue

    return projection


def discount_cash_flows(projection: list[dict], wacc: float, explicit_years: int) -> list[dict]:
    """Mid-year convention: year N's cash flow lands at period N - 0.5."""
    discounted = []
    for i, year in enumerate(projection[:explicit_years], start=1):
        period = i - 0.5
        discount_factor = 1 / (1 + wacc) ** period
        discounted.append({
            **year,
            "period": period,
            "discount_factor": discount_factor,
            "pv_ufcf": year["ufcf"] * discount_factor,
        })
    return discounted


def implied_perpetuity_growth(terminal_year_ufcf: float, terminal_value: float, wacc: float) -> float:
    """Solve TV = FCF*(1+g)/(WACC-g) for g, given TV and the terminal year's FCF."""
    return (terminal_value * wacc - terminal_year_ufcf) / (terminal_year_ufcf + terminal_value)


def terminal_value_gordon_growth(terminal_year_ufcf: float, wacc: float, g: float) -> float:
    return terminal_year_ufcf * (1 + g) / (wacc - g)


def run_dcf(
    conn: sqlite3.Connection,
    ticker: str,
    scenario: str,
    explicit_years: int = 5,
    gordon_growth_rate: float | None = None,
    wacc_override: float | None = None,
    exit_multiple_override: float | None = None,
    growth_delta: float = 0.0,
    margin_delta: float = 0.0,
) -> dict:
    """Run the DCF. `wacc_override`/`exit_multiple_override`/`growth_delta`/
    `margin_delta` let sensitivity.py perturb one input at a time while
    reusing this exact formula, so a sensitivity table's zero-delta cell
    always reconciles to this function's own base-case output."""
    if scenario not in ("bear", "base", "bull"):
        raise ValueError(f"scenario must be 'bear', 'base', or 'bull', got {scenario!r}")
    if not (1 <= explicit_years <= TOTAL_FORECAST_YEARS):
        raise ValueError(
            f"explicit_years must be between 1 and {TOTAL_FORECAST_YEARS}, got {explicit_years}"
        )

    company_id = get_company_id(conn, ticker)
    wacc_data = get_wacc(conn, company_id)
    wacc = wacc_override if wacc_override is not None else wacc_data["wacc"]

    if exit_multiple_override is not None:
        exit_multiple = exit_multiple_override
    else:
        exit_multiple = conn.execute(
            """SELECT exit_multiple_ebitda FROM terminal_assumptions
               WHERE company_id = ? AND scenario = ?""",
            (company_id, scenario),
        ).fetchone()[0]

    projection = project_financials(conn, company_id, scenario, growth_delta, margin_delta)
    discounted = discount_cash_flows(projection, wacc, explicit_years)

    sum_pv_ufcf = sum(y["pv_ufcf"] for y in discounted)

    terminal_year = discounted[-1]
    terminal_ebitda = terminal_year["ebit"] + terminal_year["da"]
    terminal_value_exit_multiple = terminal_ebitda * exit_multiple
    pv_terminal_value = terminal_value_exit_multiple * terminal_year["discount_factor"]

    enterprise_value = sum_pv_ufcf + pv_terminal_value
    equity_value = enterprise_value - wacc_data["net_debt"]
    price_per_share = equity_value / wacc_data["shares_outstanding"]

    implied_g = implied_perpetuity_growth(
        terminal_year["ufcf"], terminal_value_exit_multiple, wacc
    )
    g_for_gordon = gordon_growth_rate if gordon_growth_rate is not None else implied_g
    terminal_value_gordon = terminal_value_gordon_growth(terminal_year["ufcf"], wacc, g_for_gordon)

    return {
        "ticker": ticker,
        "scenario": scenario,
        "explicit_years": explicit_years,
        "total_projected_years": len(projection),
        "wacc": wacc_data,
        "wacc_used": wacc,
        "projection": projection,
        "discounted_cash_flows": discounted,
        "sum_pv_ufcf": sum_pv_ufcf,
        "terminal_ebitda": terminal_ebitda,
        "exit_multiple_ebitda": exit_multiple,
        "terminal_value_exit_multiple": terminal_value_exit_multiple,
        "pv_terminal_value": pv_terminal_value,
        "enterprise_value": enterprise_value,
        "net_debt": wacc_data["net_debt"],
        "equity_value": equity_value,
        "shares_outstanding": wacc_data["shares_outstanding"],
        "price_per_share": price_per_share,
        "implied_perpetuity_growth_rate": implied_g,
        "gordon_growth_rate_used": g_for_gordon,
        "terminal_value_gordon_growth": terminal_value_gordon,
    }


def print_summary(result: dict) -> None:
    print(f"{result['ticker']} — DCF ({result['scenario']} case)")
    print(f"  WACC: {result['wacc_used']:.4%}")
    print(
        f"  Explicit projection used: {result['explicit_years']} of "
        f"{result['total_projected_years']} projected years"
    )
    if result["explicit_years"] < result["total_projected_years"]:
        print(
            f"  NOTE: {result['total_projected_years']} years are projected but only the "
            f"first {result['explicit_years']} are summed for valuation — see README.md"
        )
    print(f"  Sum of PV of UFCF: ${result['sum_pv_ufcf']:,.1f}M")
    print(
        f"  Terminal value (Exit Multiple {result['exit_multiple_ebitda']}x EBITDA): "
        f"${result['terminal_value_exit_multiple']:,.1f}M "
        f"(PV: ${result['pv_terminal_value']:,.1f}M)"
    )
    print(f"  Implied perpetuity growth rate (g): {result['implied_perpetuity_growth_rate']:.4%}")
    print(
        f"  Terminal value (Gordon Growth, g={result['gordon_growth_rate_used']:.4%}): "
        f"${result['terminal_value_gordon_growth']:,.1f}M"
    )
    print(f"  Enterprise value: ${result['enterprise_value']:,.1f}M")
    print(f"  Equity value: ${result['equity_value']:,.1f}M")
    print(f"  Price per share: ${result['price_per_share']:,.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DCF engine for a company/scenario.")
    parser.add_argument("db_path", type=Path)
    parser.add_argument("ticker")
    parser.add_argument("scenario", choices=["bear", "base", "bull"])
    parser.add_argument(
        "--explicit-years", type=int, default=5,
        help="Years of explicit FCF to sum before applying terminal value (default: 5, "
             "matching the original template's behavior; pass 13 for the full projection).",
    )
    parser.add_argument("--gordon-growth-rate", type=float, default=None)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    try:
        with friendly_errors():
            result = run_dcf(
                conn, args.ticker, args.scenario,
                explicit_years=args.explicit_years,
                gordon_growth_rate=args.gordon_growth_rate,
            )
            print_summary(result)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
