"""Financial health check: leverage and interest-coverage metrics.

A DCF price target says nothing about whether the company survives long
enough for that target to matter. This module answers a narrower,
earlier question than valuation: is the balance sheet itself a risk?

Both metrics are built from fields already in `historicals` (no new data
source needed): net_debt, long_term_debt, interest_expense, ebit, da.
Thresholds follow common sell-side/rating-agency rules of thumb (not a
precise credit model) and are documented inline so a reader can disagree
with them explicitly instead of guessing why a flag fired.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from cli_utils import friendly_errors
from dcf_engine import get_company_id

# Rough, commonly-cited sell-side thresholds -- not a rating-agency model.
# Investment-grade issuers typically run under ~3x net debt/EBITDA and
# above ~6x interest coverage; high-yield/distressed territory starts
# above ~5x and below ~2x respectively. The middle band is "watch, not
# alarm" -- flagged so an analyst decides, not so the tool decides.
LEVERAGE_HEALTHY_MAX = 3.0
LEVERAGE_WATCH_MAX = 5.0
COVERAGE_HEALTHY_MIN = 6.0
COVERAGE_WATCH_MIN = 2.0


def _row_to_dict(conn: sqlite3.Connection, sql: str, params: tuple) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _latest_actual(conn: sqlite3.Connection, company_id: int) -> dict:
    row = _row_to_dict(
        conn,
        """SELECT * FROM historicals WHERE company_id = ? AND period_type = 'actual'
           ORDER BY fiscal_year DESC LIMIT 1""",
        (company_id,),
    )
    if row is None:
        raise ValueError("No actual historicals found for this company")
    return row


def _flag_leverage(net_debt_ebitda: float | None) -> str:
    if net_debt_ebitda is None:
        return "indisponível"
    if net_debt_ebitda < 0:
        return "caixa líquido"
    if net_debt_ebitda <= LEVERAGE_HEALTHY_MAX:
        return "saudável"
    if net_debt_ebitda <= LEVERAGE_WATCH_MAX:
        return "atenção"
    return "alavancada"


def _flag_coverage(interest_coverage: float | None) -> str:
    if interest_coverage is None:
        return "indisponível"
    if interest_coverage < 0:
        return "atenção"  # negative EBIT: coverage is meaningless/negative
    if interest_coverage >= COVERAGE_HEALTHY_MIN:
        return "saudável"
    if interest_coverage >= COVERAGE_WATCH_MIN:
        return "atenção"
    return "fraca"


def compute_health_metrics(conn: sqlite3.Connection, ticker: str) -> dict:
    """Leverage (net debt/EBITDA) and interest coverage (EBIT/interest
    expense) off the latest reported fiscal year, each with a
    saudável/atenção/alavancada-fraca flag.

    EBITDA here is `ebit + da` (consistent with how dcf_engine.py derives
    terminal EBITDA), not `ebitda_adjusted` from historicals, so the
    leverage ratio and the DCF's own terminal-value EBITDA always agree.

    interest_expense of exactly 0.0 is a known, documented case (see
    README.md, "interest_expense virou opcional") -- some Bloomberg
    templates simply don't expose a matching field, and 0.0 was chosen
    there as an explicit conservative default for cost of debt. Interest
    coverage can't be computed from a 0 denominator, so it's reported as
    unavailable rather than an infinite/undefined number.
    """
    company_id = get_company_id(conn, ticker)
    latest = _latest_actual(conn, company_id)

    ebitda = latest["ebit"] + latest["da"]
    net_debt = latest["net_debt"]
    interest_expense = latest["interest_expense"]

    net_debt_ebitda = net_debt / ebitda if ebitda else None
    interest_coverage = (
        latest["ebit"] / interest_expense if interest_expense else None
    )

    return {
        "ticker": ticker,
        "fiscal_year": latest["fiscal_year"],
        "ebit": latest["ebit"],
        "da": latest["da"],
        "ebitda": ebitda,
        "net_debt": net_debt,
        "interest_expense": interest_expense,
        "net_debt_ebitda": net_debt_ebitda,
        "leverage_flag": _flag_leverage(net_debt_ebitda),
        "interest_coverage": interest_coverage,
        "coverage_flag": _flag_coverage(interest_coverage),
        "coverage_note": (
            "interest_expense = 0 no dado de origem (campo ausente no template "
            "Bloomberg desse arquivo, ver README.md) -- cobertura não computável, "
            "não é uma empresa sem dívida"
            if interest_expense == 0 and net_debt != 0
            else None
        ),
    }


def overall_flag(metrics: dict) -> str:
    """Worst-of leverage_flag/coverage_flag, for a single summary badge.
    'indisponível' only wins if BOTH are unavailable."""
    order = ["caixa líquido", "saudável", "atenção", "alavancada", "fraca", "indisponível"]
    flags = [metrics["leverage_flag"], metrics["coverage_flag"]]
    if set(flags) == {"indisponível"}:
        return "indisponível"
    flags = [f for f in flags if f != "indisponível"]
    # "alavancada" (leverage) and "fraca" (coverage) are both the bad end;
    # treat them as equally severe, worse than "atenção".
    severity = {"caixa líquido": 0, "saudável": 1, "atenção": 2, "alavancada": 3, "fraca": 3}
    return max(flags, key=lambda f: severity[f])


def print_summary(metrics: dict) -> None:
    print(f"{metrics['ticker']} — Saúde financeira (FY{metrics['fiscal_year']})")
    nd = metrics["net_debt_ebitda"]
    print(
        f"  Dívida líquida/EBITDA: {nd:.2f}x [{metrics['leverage_flag']}]"
        if nd is not None else f"  Dívida líquida/EBITDA: indisponível"
    )
    ic = metrics["interest_coverage"]
    print(
        f"  Cobertura de juros (EBIT/despesa financeira): {ic:.2f}x [{metrics['coverage_flag']}]"
        if ic is not None else "  Cobertura de juros: indisponível"
    )
    if metrics["coverage_note"]:
        print(f"  NOTA: {metrics['coverage_note']}")
    print(f"  Sinal geral: {overall_flag(metrics)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Health check (leverage/coverage) for a company.")
    parser.add_argument("db_path", type=Path)
    parser.add_argument("ticker")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    try:
        with friendly_errors():
            print_summary(compute_health_metrics(conn, args.ticker))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
