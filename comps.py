"""Sector peer comparables via Yahoo Finance.

`trading_comps` in the database only gets populated when the source
workbook has a hand-built "TRADING MULTIPLES" block (data_source ==
'modl_tabs', e.g. the AppLovin file) -- see README.md, "Duas fontes de
dados". Every 'derived' company (the normal case) has no peer reference
at all: there's no way to say a target price implies the stock is cheap
or expensive versus its sector without one.

This module doesn't try to auto-discover peers (no free, reliable
"who are this company's comps" API exists) -- the analyst supplies the
peer ticker list, same as a sell-side comp sheet is built by hand. What
this module automates is pulling each peer's current trading multiples.

Follows the same degrade-instead-of-crash pattern as app.py's
fetch_market_data(): a peer with missing data is reported with the
fields it has and `None` for what's missing, never dropped silently and
never fabricated.
"""

from __future__ import annotations

import argparse
import statistics

import yfinance as yf


def _safe_float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_peer_multiples(peer_tickers: list[str]) -> list[dict]:
    """Current EV/EBITDA, trailing P/E, forward P/E and PEG for each
    ticker in `peer_tickers`, straight from Yahoo Finance's `.info`
    endpoint. One dict per ticker; a ticker Yahoo can't resolve is
    included with `error` set instead of being dropped, so the caller
    can see exactly which peers didn't come back."""
    results = []
    for ticker in peer_tickers:
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception as exc:  # noqa: BLE001 — network/parse failures reported, not raised
            results.append({"ticker": ticker, "error": str(exc)})
            continue
        if not info or info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
            results.append({"ticker": ticker, "error": "sem dados retornados pelo Yahoo Finance"})
            continue
        results.append({
            "ticker": ticker,
            "name": info.get("shortName") or info.get("longName") or ticker,
            "ev_ebitda": _safe_float(info.get("enterpriseToEbitda")),
            "pe_trailing": _safe_float(info.get("trailingPE")),
            "pe_forward": _safe_float(info.get("forwardPE")),
            "peg": _safe_float(info.get("trailingPegRatio") or info.get("pegRatio")),
            "error": None,
        })
    return results


def peer_medians(peer_data: list[dict]) -> dict:
    """Median of each multiple across peers that have it. A peer missing
    a given multiple is excluded from that multiple's median only (not
    from the others) -- one gap doesn't throw out an otherwise-usable row."""
    medians = {}
    for key in ("ev_ebitda", "pe_trailing", "pe_forward", "peg"):
        values = [p[key] for p in peer_data if p.get("error") is None and p.get(key) is not None]
        medians[key] = statistics.median(values) if values else None
    medians["n_peers_with_data"] = sum(1 for p in peer_data if p.get("error") is None)
    medians["n_peers_requested"] = len(peer_data)
    return medians


def compare_to_peers(
    company_ev_ebitda: float | None,
    company_pe: float | None,
    peer_data: list[dict],
) -> dict:
    """Where the company's own multiples sit versus the peer median --
    the number that actually answers 'is this cheap or expensive versus
    the sector', which a target price alone doesn't."""
    medians = peer_medians(peer_data)

    def _premium(company_value, median_value):
        if company_value is None or median_value is None or median_value == 0:
            return None
        return company_value / median_value - 1

    return {
        "peer_medians": medians,
        "company_ev_ebitda": company_ev_ebitda,
        "peer_median_ev_ebitda": medians["ev_ebitda"],
        "ev_ebitda_premium_to_peers": _premium(company_ev_ebitda, medians["ev_ebitda"]),
        "company_pe": company_pe,
        "peer_median_pe": medians["pe_trailing"],
        "pe_premium_to_peers": _premium(company_pe, medians["pe_trailing"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch peer trading multiples from Yahoo Finance.")
    parser.add_argument("tickers", nargs="+", help="Peer tickers, e.g. GOOGL MSFT META")
    args = parser.parse_args()

    peer_data = fetch_peer_multiples(args.tickers)
    for p in peer_data:
        if p.get("error"):
            print(f"  {p['ticker']}: erro — {p['error']}")
        else:
            print(
                f"  {p['ticker']} ({p['name']}): EV/EBITDA={p['ev_ebitda']}, "
                f"P/E (LTM)={p['pe_trailing']}, P/E (fwd)={p['pe_forward']}, PEG={p['peg']}"
            )
    medians = peer_medians(peer_data)
    print(
        f"Mediana ({medians['n_peers_with_data']}/{medians['n_peers_requested']} peers com dado): "
        f"EV/EBITDA={medians['ev_ebitda']}, P/E (LTM)={medians['pe_trailing']}"
    )


if __name__ == "__main__":
    main()
