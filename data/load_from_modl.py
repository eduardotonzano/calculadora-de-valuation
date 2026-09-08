"""Load a Bloomberg MODL .xlsx export into the valuation.db SQLite schema.

The MODL template (see data/schema.sql for the target schema) ships three
sheets:

  - "Multiple Periods": ~10 years of annual actuals + Street-consensus
    estimates, one column per fiscal year, rows identified by a Bloomberg
    field code in column B (e.g. IS_COMP_SALES, NET_DEBT).
  - "DCF": Bear/Base/Bull forward assumption blocks (revenue growth, EBIT
    margin, tax rate, D&A %, CapEx %, NWC %, terminal EV/EBITDA exit
    multiple) plus a "TRADING MULTIPLES" block of current market-implied
    multiples.
  - "WACC": CAPM and capital-structure inputs.

Usage:
    python data/load_from_modl.py <path-to-modl.xlsx> <path-to-valuation.db>
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import openpyxl

# Columns C..O on the DCF sheet hold FY2026E..FY2038E (13 years) for every
# assumption block (Base, Bear, Bull) and for the FCF build.
YEAR_COLS = ["C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O"]
FIRST_FORECAST_YEAR = 2026

# Columns E..N on the "Multiple Periods" sheet hold 2030 (fwd) down to 2021 (rep).
MULTIPLE_PERIODS_YEAR_COLS = ["E", "F", "G", "H", "I", "J", "K", "L", "M", "N"]

# Bloomberg field code (column B on "Multiple Periods") -> historicals column.
FIELD_CODE_MAP = {
    "IS_COMP_SALES": "revenue",
    "IS_COMPARABLE_EBIT": "ebit",
    "IS_COMPARABLE_EBITDA": "ebitda_adjusted",
    "CF_DEPR_AMORT": "da",
    "IS_NET_INTEREST_EXPENSE": "interest_expense",
    "IS_COMP_PTP_EX_STK_BASED_COMP": "pretax_income",
    "IS_INC_TAX_EXP": "tax_expense",
    "IS_COMP_NET_INCOME_GAAP": "net_income",
    "IS_COMP_EPS_ADJUSTED_OLD": "eps_diluted_adjusted",
    "CB_BS_LT_BORROWING": "long_term_debt",
    "NET_DEBT": "net_debt",
    "CF_CHNG_NON_CASH_WORK_CAP": "nwc_change",
    "CB_CF_PURCHASES_OF_PPE": "capex",
}

# (scenario, header row of the assumption block on the DCF sheet).
# Each block is: header (years), +1 revenue growth, +2 EBIT margin, +3 tax
# rate, +4 D&A %, +5 CapEx %, +6 NWC %, +7 terminal exit multiple (col C only).
SCENARIO_BLOCKS = {
    "base": 16,
    "bear": 26,
    "bull": 36,
}

# DCF sheet "TRADING MULTIPLES" block: row -> (metric name, {column: fiscal_year}).
TRADING_COMPS_ROWS = {
    130: "PE",
    131: "EV_EBITDA",
    132: "FCF_YIELD",
}
TRADING_COMPS_COLS = {"B": 2027, "C": 2030}


def find_field_rows(ws, field_codes: set[str]) -> dict[str, int]:
    """Map each Bloomberg field code to the first row it appears in (column B)."""
    rows: dict[str, int] = {}
    for (cell,) in ws.iter_rows(min_col=2, max_col=2):
        if cell.value in field_codes and cell.value not in rows:
            rows[cell.value] = cell.row
    missing = field_codes - rows.keys()
    if missing:
        raise ValueError(f"Missing Bloomberg field codes in 'Multiple Periods': {sorted(missing)}")
    return rows


def period_type_for_column(ws, col: str) -> str:
    """Row 3 tags each year column '... (Fwd)' (estimate) or '... (Rep)' (actual)."""
    label = ws[f"{col}3"].value or ""
    if "Fwd" in label:
        return "estimate"
    if "Rep" in label:
        return "actual"
    raise ValueError(f"Cannot determine period type for column {col} from label {label!r}")


def load_historicals(ws) -> list[dict]:
    field_rows = find_field_rows(ws, set(FIELD_CODE_MAP))
    records = []
    for col in MULTIPLE_PERIODS_YEAR_COLS:
        year = ws[f"{col}4"].value.year
        record = {"fiscal_year": year, "period_type": period_type_for_column(ws, col)}
        for code, field in FIELD_CODE_MAP.items():
            record[field] = ws[f"{col}{field_rows[code]}"].value
        records.append(record)
    return records


def load_wacc_inputs(wacc_ws) -> dict:
    return {
        "risk_free_rate": wacc_ws["B5"].value,
        "beta": wacc_ws["B6"].value,
        "equity_risk_premium": wacc_ws["B7"].value,
        "stock_price": wacc_ws["B18"].value,
        "shares_outstanding": wacc_ws["B19"].value,
    }


def load_scenario_block(dcf_ws, header_row: int) -> tuple[list[dict], float]:
    growth_row, margin_row, tax_row = header_row + 1, header_row + 2, header_row + 3
    da_row, capex_row, nwc_row = header_row + 4, header_row + 5, header_row + 6
    exit_multiple_row = header_row + 7

    assumptions = []
    for col, year in zip(YEAR_COLS, range(FIRST_FORECAST_YEAR, FIRST_FORECAST_YEAR + len(YEAR_COLS))):
        assumptions.append({
            "fiscal_year": year,
            "revenue_growth": dcf_ws[f"{col}{growth_row}"].value,
            "ebit_margin": dcf_ws[f"{col}{margin_row}"].value,
            "tax_rate": dcf_ws[f"{col}{tax_row}"].value,
            "da_pct_revenue": dcf_ws[f"{col}{da_row}"].value,
            "capex_pct_revenue": dcf_ws[f"{col}{capex_row}"].value,
            "nwc_pct_delta_revenue": dcf_ws[f"{col}{nwc_row}"].value,
        })
    exit_multiple = dcf_ws[f"C{exit_multiple_row}"].value
    return assumptions, exit_multiple


def load_trading_comps(dcf_ws) -> list[dict]:
    comps = []
    for row, metric in TRADING_COMPS_ROWS.items():
        for col, year in TRADING_COMPS_COLS.items():
            comps.append({"metric": metric, "fiscal_year": year, "value": dcf_ws[f"{col}{row}"].value})
    # PEG (row 133) is a single value anchored to the block's first column (FY2027E).
    comps.append({"metric": "PEG", "fiscal_year": TRADING_COMPS_COLS["B"], "value": dcf_ws["B133"].value})
    return comps


def parse_ticker(multiple_periods_ws) -> str:
    header = multiple_periods_ws["A2"].value or ""
    match = re.match(r"^(\S+\s+\S+)\s+Equity", header)
    if not match:
        raise ValueError(f"Could not parse ticker from 'Multiple Periods'!A2: {header!r}")
    return match.group(1)


def parse_company_name(dcf_ws) -> str:
    title = dcf_ws["A1"].value or ""
    match = re.match(r"^(.+?)\s*\(", title)
    return match.group(1).strip() if match else title.strip()


def parse_as_of_date(dcf_ws) -> str | None:
    header = dcf_ws["A2"].value or ""
    match = re.search(r"As of:\s*([\d/]+)", header)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%m/%d/%Y").date().isoformat()


REQUIRED_SHEETS = ("Multiple Periods", "DCF", "WACC")


def load_workbook_data(xlsx_path: Path) -> dict:
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    missing = [name for name in REQUIRED_SHEETS if name not in wb.sheetnames]
    if missing:
        raise ValueError(
            f"This workbook only has {wb.sheetnames}, missing {missing}. "
            "A full valuation needs all three: 'Multiple Periods' for historicals, "
            "'DCF' for the Bear/Base/Bull scenario assumptions and terminal exit "
            "multiple, and 'WACC' for the CAPM inputs — historicals alone aren't "
            "enough to run dcf_engine.py, target_price.py, or sensitivity.py."
        )
    dcf_ws = wb["DCF"]
    mp_ws = wb["Multiple Periods"]
    wacc_ws = wb["WACC"]

    scenarios = {}
    terminal_multiples = {}
    for scenario, header_row in SCENARIO_BLOCKS.items():
        assumptions, exit_multiple = load_scenario_block(dcf_ws, header_row)
        scenarios[scenario] = assumptions
        terminal_multiples[scenario] = exit_multiple

    return {
        "ticker": parse_ticker(mp_ws),
        "name": parse_company_name(dcf_ws),
        "as_of_date": parse_as_of_date(dcf_ws),
        "historicals": load_historicals(mp_ws),
        "wacc_inputs": load_wacc_inputs(wacc_ws),
        "scenarios": scenarios,
        "terminal_multiples": terminal_multiples,
        "trading_comps": load_trading_comps(dcf_ws),
    }


def write_to_db(db_path: Path, schema_path: Path, data: dict) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_path.read_text())
        conn.execute("PRAGMA foreign_keys = ON")

        existing = conn.execute(
            "SELECT id FROM companies WHERE ticker = ?", (data["ticker"],)
        ).fetchone()
        if existing:
            company_id = existing[0]
            for table in (
                "historicals", "wacc_inputs", "scenario_assumptions",
                "terminal_assumptions", "trading_comps",
            ):
                conn.execute(f"DELETE FROM {table} WHERE company_id = ?", (company_id,))
            conn.execute(
                "UPDATE companies SET name = ?, as_of_date = ? WHERE id = ?",
                (data["name"], data["as_of_date"], company_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO companies (ticker, name, as_of_date) VALUES (?, ?, ?)",
                (data["ticker"], data["name"], data["as_of_date"]),
            )
            company_id = cur.lastrowid

        for row in data["historicals"]:
            conn.execute(
                """INSERT INTO historicals
                   (company_id, fiscal_year, period_type, revenue, ebit, ebitda_adjusted,
                    da, interest_expense, pretax_income, tax_expense, net_income,
                    eps_diluted_adjusted, long_term_debt, net_debt, capex, nwc_change)
                   VALUES (:company_id, :fiscal_year, :period_type, :revenue, :ebit,
                    :ebitda_adjusted, :da, :interest_expense, :pretax_income, :tax_expense,
                    :net_income, :eps_diluted_adjusted, :long_term_debt, :net_debt, :capex,
                    :nwc_change)""",
                {**row, "company_id": company_id},
            )

        conn.execute(
            """INSERT INTO wacc_inputs
               (company_id, risk_free_rate, beta, equity_risk_premium, stock_price, shares_outstanding)
               VALUES (:company_id, :risk_free_rate, :beta, :equity_risk_premium, :stock_price, :shares_outstanding)""",
            {**data["wacc_inputs"], "company_id": company_id},
        )

        for scenario, assumptions in data["scenarios"].items():
            for row in assumptions:
                conn.execute(
                    """INSERT INTO scenario_assumptions
                       (company_id, scenario, fiscal_year, revenue_growth, ebit_margin, tax_rate,
                        da_pct_revenue, capex_pct_revenue, nwc_pct_delta_revenue)
                       VALUES (:company_id, :scenario, :fiscal_year, :revenue_growth, :ebit_margin,
                        :tax_rate, :da_pct_revenue, :capex_pct_revenue, :nwc_pct_delta_revenue)""",
                    {**row, "company_id": company_id, "scenario": scenario},
                )
            conn.execute(
                """INSERT INTO terminal_assumptions (company_id, scenario, exit_multiple_ebitda)
                   VALUES (?, ?, ?)""",
                (company_id, scenario, data["terminal_multiples"][scenario]),
            )

        for row in data["trading_comps"]:
            conn.execute(
                """INSERT INTO trading_comps (company_id, metric, fiscal_year, value)
                   VALUES (:company_id, :metric, :fiscal_year, :value)""",
                {**row, "company_id": company_id},
            )

        conn.commit()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx_path", type=Path, help="Path to the Bloomberg MODL .xlsx export")
    parser.add_argument("db_path", type=Path, help="Path to the SQLite database to create/update")
    args = parser.parse_args()

    schema_path = Path(__file__).parent / "schema.sql"
    data = load_workbook_data(args.xlsx_path)
    args.db_path.parent.mkdir(parents=True, exist_ok=True)
    write_to_db(args.db_path, schema_path, data)
    print(f"Loaded {data['ticker']} ({data['name']}) into {args.db_path}")


if __name__ == "__main__":
    main()
