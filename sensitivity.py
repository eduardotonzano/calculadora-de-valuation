"""DCF sensitivity tables: WACC x Exit Multiple, and Revenue Growth Δ x EBIT
Margin Δ.

Both tables are built by calling dcf_engine.run_dcf() once per grid cell,
perturbing one pair of inputs at a time. Because they reuse run_dcf()
directly, the zero-delta cell of every grid always reconciles exactly to
run_dcf()'s own price per share for that scenario/explicit_years.

That reconciliation is the whole reason to build it this way: the source
Bloomberg MODL file does NOT have it. Its main DCF cell (`C87`) and its
own two sensitivity tables each use a different DCF formula:

  - Main DCF (C87): sums PV of FCF for years 1-5 only, terminal value off
    year 5 EBITDA discounted at year 5's mid-year period. -> $388.54
  - "SENSITIVITY 1: WACC vs. TERMINAL EXIT MULTIPLE" (rows 92-98): sums PV
    of FCF for all 13 years, but the terminal EBITDA is still year 5's
    (stale) and gets discounted at year 13's mid-year period instead of
    year 5's. At base WACC/multiple -> $313.31.
  - "SENSITIVITY 2: REVENUE GROWTH Δ vs. EBIT MARGIN Δ" (rows 106-112):
    sums PV of FCF for all 13 years AND bases the terminal EBITDA on year
    13, discounted at year 13. At zero/zero -> $412.19 (this one matches
    dcf_engine.run_dcf(explicit_years=13) exactly).

So the file's own three "base case" numbers disagree with each other by
as much as ~24%. See README.md for the full writeup. sensitivity.py does
not reproduce any of that: it always uses dcf_engine's single formula
(explicit_years=5 by default, matching the validated $388.54), so every
cell in both grids here is directly comparable to run_dcf()'s own output.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from dcf_engine import get_company_id, get_wacc, run_dcf

WACC_DELTAS = (-0.04, -0.02, 0.0, 0.02, 0.04)
EXIT_MULTIPLE_DELTAS = (-4.0, -2.0, 0.0, 2.0, 4.0)
GROWTH_DELTAS = (-0.08, -0.04, 0.0, 0.04, 0.08)
MARGIN_DELTAS = (-0.04, -0.02, 0.0, 0.02, 0.04)


def sensitivity_wacc_exit_multiple(
    conn: sqlite3.Connection,
    ticker: str,
    scenario: str = "base",
    explicit_years: int = 5,
    wacc_deltas: tuple[float, ...] = WACC_DELTAS,
    multiple_deltas: tuple[float, ...] = EXIT_MULTIPLE_DELTAS,
) -> dict:
    company_id = get_company_id(conn, ticker)
    base_wacc = get_wacc(conn, company_id)["wacc"]
    base_multiple = conn.execute(
        """SELECT exit_multiple_ebitda FROM terminal_assumptions
           WHERE company_id = ? AND scenario = ?""",
        (company_id, scenario),
    ).fetchone()[0]

    wacc_axis = [base_wacc + d for d in wacc_deltas]
    multiple_axis = [base_multiple + d for d in multiple_deltas]

    grid = [
        [
            run_dcf(
                conn, ticker, scenario, explicit_years=explicit_years,
                wacc_override=wacc, exit_multiple_override=multiple,
            )["price_per_share"]
            for multiple in multiple_axis
        ]
        for wacc in wacc_axis
    ]

    return {
        "row_label": "WACC",
        "col_label": "Exit Multiple (EV/EBITDA, x)",
        "row_axis": wacc_axis,
        "col_axis": multiple_axis,
        "row_format": "pct",
        "col_format": "x",
        "grid": grid,
    }


def sensitivity_growth_margin(
    conn: sqlite3.Connection,
    ticker: str,
    scenario: str = "base",
    explicit_years: int = 5,
    growth_deltas: tuple[float, ...] = GROWTH_DELTAS,
    margin_deltas: tuple[float, ...] = MARGIN_DELTAS,
) -> dict:
    grid = [
        [
            run_dcf(
                conn, ticker, scenario, explicit_years=explicit_years,
                growth_delta=gd, margin_delta=md,
            )["price_per_share"]
            for md in margin_deltas
        ]
        for gd in growth_deltas
    ]

    return {
        "row_label": "Revenue Growth Δ",
        "col_label": "EBIT Margin Δ",
        "row_axis": list(growth_deltas),
        "col_axis": list(margin_deltas),
        "row_format": "pct",
        "col_format": "pct",
        "grid": grid,
    }


def _fmt_axis(value: float, fmt: str) -> str:
    if fmt == "pct":
        return f"{value:.2%}"
    return f"{value:.4g}x"


def print_grid(table: dict) -> None:
    print(f"{table['row_label']} \\ {table['col_label']}")
    print(" " * 10 + "".join(f"{_fmt_axis(c, table['col_format']):>12}" for c in table["col_axis"]))
    for row_val, row in zip(table["row_axis"], table["grid"]):
        row_str = f"{_fmt_axis(row_val, table['row_format']):>10}"
        print(row_str + "".join(f"{v:>12,.2f}" for v in row))


def main() -> None:
    parser = argparse.ArgumentParser(description="Print DCF sensitivity tables.")
    parser.add_argument("db_path", type=Path)
    parser.add_argument("ticker")
    parser.add_argument("scenario", nargs="?", default="base", choices=["bear", "base", "bull"])
    parser.add_argument(
        "--explicit-years", type=int, default=5,
        help="Passed through to dcf_engine.run_dcf() for every cell (default: 5).",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    try:
        print(f"{args.ticker} — Sensitivity ({args.scenario} case, {args.explicit_years}y explicit)\n")
        print_grid(sensitivity_wacc_exit_multiple(conn, args.ticker, args.scenario, args.explicit_years))
        print()
        print_grid(sensitivity_growth_margin(conn, args.ticker, args.scenario, args.explicit_years))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
