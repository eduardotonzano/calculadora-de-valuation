"""Qualitative layer: narrative thesis and catalyst calendar.

Everything else in this project (dcf_engine.py, target_price.py,
sensitivity.py, comps.py, health.py) computes a number from data. This
module stores what an analyst writes instead -- the bull/bear narrative
and known risks the DCF can't capture (customer concentration,
litigation, regulatory change), and a calendar of upcoming events that
could move the stock or invalidate the thesis. Pure CRUD, no math.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from dcf_engine import get_company_id


def _row_to_dict(conn: sqlite3.Connection, sql: str, params: tuple) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def get_thesis(conn: sqlite3.Connection, ticker: str) -> dict:
    """Returns the stored thesis, or an empty one (all fields None) if
    nothing has been saved yet -- never raises for a missing thesis,
    since not having written one yet is the normal starting state."""
    company_id = get_company_id(conn, ticker)
    row = _row_to_dict(
        conn, "SELECT * FROM investment_thesis WHERE company_id = ?", (company_id,)
    )
    if row is None:
        return {
            "ticker": ticker, "bull_case": None, "bear_case": None,
            "key_risks": None, "last_updated": None,
        }
    return {
        "ticker": ticker,
        "bull_case": row["bull_case"],
        "bear_case": row["bear_case"],
        "key_risks": row["key_risks"],
        "last_updated": row["last_updated"],
    }


def save_thesis(
    conn: sqlite3.Connection,
    ticker: str,
    bull_case: str = "",
    bear_case: str = "",
    key_risks: str = "",
) -> dict:
    """Upserts the thesis for `ticker` (one row per company -- a new save
    replaces the previous text, it doesn't version it)."""
    company_id = get_company_id(conn, ticker)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO investment_thesis (company_id, bull_case, bear_case, key_risks, last_updated)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (company_id) DO UPDATE SET
               bull_case = excluded.bull_case,
               bear_case = excluded.bear_case,
               key_risks = excluded.key_risks,
               last_updated = excluded.last_updated""",
        (company_id, bull_case, bear_case, key_risks, now),
    )
    conn.commit()
    return get_thesis(conn, ticker)


def list_catalysts(conn: sqlite3.Connection, ticker: str) -> list[dict]:
    """Catalysts for `ticker`, soonest first."""
    company_id = get_company_id(conn, ticker)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT event_date, description FROM catalysts
           WHERE company_id = ? ORDER BY event_date""",
        (company_id,),
    ).fetchall()
    return [{"event_date": r["event_date"], "description": r["description"]} for r in rows]


def add_catalyst(conn: sqlite3.Connection, ticker: str, event_date: str, description: str) -> None:
    """`event_date` must be ISO 8601 ('YYYY-MM-DD'). Duplicate (date,
    description) pairs are silently ignored (UNIQUE constraint) rather
    than raising, so re-submitting the same form twice is harmless."""
    company_id = get_company_id(conn, ticker)
    conn.execute(
        """INSERT OR IGNORE INTO catalysts (company_id, event_date, description)
           VALUES (?, ?, ?)""",
        (company_id, event_date, description),
    )
    conn.commit()


def remove_catalyst(conn: sqlite3.Connection, ticker: str, event_date: str, description: str) -> None:
    company_id = get_company_id(conn, ticker)
    conn.execute(
        """DELETE FROM catalysts WHERE company_id = ? AND event_date = ? AND description = ?""",
        (company_id, event_date, description),
    )
    conn.commit()
