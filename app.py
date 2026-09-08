"""Streamlit interface for the valuation calculator.

Pure display layer over dcf_engine.py, target_price.py, and
sensitivity.py — every number shown here is read straight out of the
dicts those modules already return; nothing is computed in this file.
The point is to expose the arithmetic, not hide it behind a headline
number: each tab shows the formula next to the inputs next to the
result, the same way you'd read the original Bloomberg MODL workbook
cell by cell.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from pathlib import Path

import openpyxl
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

from data.load_from_modl import load_workbook_data, migrate_schema, write_to_db
from dcf_engine import get_company_id, get_wacc, run_dcf
from sensitivity import (
    sensitivity_beta_risk_free,
    sensitivity_growth_margin,
    sensitivity_wacc_exit_multiple,
)
from target_price import from_ev_ebitda, from_pe, from_peg

st.set_page_config(page_title="Calculadora de Valuation", layout="wide")

DEFAULT_DB_PATH = "data/valuation.db"
SCHEMA_PATH = Path(__file__).parent / "data" / "schema.sql"
TARGET_YEAR_OPTIONS = [2026, 2027, 2028, 2029, 2030]  # bounded by Street-consensus EPS coverage

# Cached from the source Bloomberg MODL workbook (AppLovin, base case, WACC/multiple
# unchanged) while building sensitivity.py — see README.md "Segunda descoberta".
# C87 = main DCF cell; D96 = center of the file's own WACC x Exit Multiple table;
# D110 = center of the file's own Growth x Margin table.
SOURCE_FILE_BASE_CASE_CELLS = [
    ("C87 — DCF principal", "Soma PV do FCF anos 1-5; TV = EBITDA ano 5, descontado no período do ano 5", 388.54),
    ("D96 — Tabela Sensib. 1 (WACC x Múltiplo)", "Soma PV do FCF 13 anos; TV = EBITDA ano 5 (obsoleto), descontado no período do ano 13", 313.31),
    ("D110 — Tabela Sensib. 2 (Cresc. x Margem)", "Soma PV do FCF 13 anos; TV = EBITDA ano 13, descontado no período do ano 13", 412.19),
]

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');
:root {
    --ink: #201d18; --ink-2: #5b5647; --muted: #8a8371; --border: #e2ddce;
    --surface: #ffffff; --surface-2: #f5f2e9; --bg: #faf8f2;
    --accent: #a8461f; --accent-soft: #f3e2d6; --positive: #2e7d4f; --positive-soft: #e1efe5;
}

.stApp { background: var(--bg); }
[data-testid="stAppViewContainer"], [data-testid="stMain"] { font-family: "IBM Plex Sans", system-ui, sans-serif; color: var(--ink); }
h1, h2, h3 { font-family: "Source Serif 4", Georgia, serif; letter-spacing: -0.01em; color: var(--ink); font-weight: 700; }
h1 { font-size: 2.1rem !important; }
h2 { font-size: 1.35rem !important; margin-top: 1.6rem !important; }
h3 { font-size: 1.1rem !important; }
p, li, span, div { font-variant-numeric: tabular-nums; }

/* -- sidebar -- */
section[data-testid="stSidebar"] {
    background: var(--surface); border-right: 1px solid var(--border);
}
section[data-testid="stSidebar"] h2, section[data-testid="stSidebar"] h3 {
    font-family: "IBM Plex Sans", sans-serif !important; font-size: 0.78rem !important;
    text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted) !important;
    font-weight: 600 !important; margin: 1.1rem 0 0.3rem 0 !important;
}
section[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {
    background: var(--surface-2); border: 1.5px dashed #cfc6ac; border-radius: 10px;
}

/* -- metrics -- */
[data-testid="stMetric"] {
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    padding: 0.85rem 1rem; box-shadow: 0 1px 2px rgba(40,30,10,0.05);
}
[data-testid="stMetricLabel"] { color: var(--muted); font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.05em; }
[data-testid="stMetricValue"] {
    font-family: "Source Serif 4", serif; font-variant-numeric: tabular-nums; color: var(--ink);
}

/* -- tabs -- */
[data-baseweb="tab-list"] { gap: 0.25rem; border-bottom: 1px solid var(--border); }
[data-baseweb="tab"] {
    font-family: "IBM Plex Sans", sans-serif; font-weight: 500; color: var(--muted);
    padding: 0.6rem 0.9rem !important;
}
[data-baseweb="tab"][aria-selected="true"] { color: var(--accent) !important; font-weight: 600; }
[data-baseweb="tab-highlight"] { background-color: var(--accent) !important; height: 2.5px !important; }

/* -- buttons -- */
.stButton button, .stFormSubmitButton button {
    background: var(--accent); color: #fff9f2; border: none; border-radius: 8px;
    font-weight: 600; font-family: "IBM Plex Sans", sans-serif;
}
.stButton button:hover, .stFormSubmitButton button:hover { background: #8f3a19; color: #fff9f2; }
section[data-testid="stSidebar"] .stButton button[kind="secondary"] {
    background: var(--surface); color: var(--ink-2); border: 1px solid var(--border);
}

/* -- content blocks -- */
.formula-box {
    background: var(--surface-2); border-left: 3px solid #c9bb96; border-radius: 0 8px 8px 0;
    padding: 0.7rem 1.05rem; font-family: "IBM Plex Mono", monospace; font-size: 0.86rem;
    margin: 0.5rem 0 1rem 0; white-space: pre-wrap; color: var(--ink-2); line-height: 1.6;
}
.note {
    border-left: 3px solid var(--accent); background: var(--accent-soft); border-radius: 0 8px 8px 0;
    padding: 0.7rem 1.05rem; margin: 0.7rem 0; font-size: 0.9rem; color: var(--ink-2);
}
.source-tag {
    font-family: "IBM Plex Mono", monospace; font-size: 0.76rem; color: var(--muted); margin-top: -0.4rem;
}

/* -- tables -- */
table td, table th { font-variant-numeric: tabular-nums; }
[data-testid="stDataFrame"], [data-testid="stTable"] {
    border: 1px solid var(--border); border-radius: 10px; overflow: hidden;
}

/* -- containers used as cards -- */
[data-testid="stVerticalBlockBorderWrapper"] {
    border-color: var(--border) !important; border-radius: 12px !important;
    background: var(--surface);
}
hr { border-color: var(--border); }
</style>
"""


@st.cache_resource
def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # A db created before a schema column existed (e.g. the committed
    # data/valuation.db, from before data_source was added) would otherwise
    # crash the first time a query reads that column, even without a new
    # upload -- see migrate_schema()'s docstring.
    migrate_schema(conn, SCHEMA_PATH)
    return conn


def list_tickers(conn: sqlite3.Connection) -> list[str]:
    return [row["ticker"] for row in conn.execute("SELECT ticker FROM companies ORDER BY ticker")]


def get_company(conn: sqlite3.Connection, ticker: str) -> dict:
    return dict(conn.execute("SELECT * FROM companies WHERE ticker = ?", (ticker,)).fetchone())


def get_latest_actual(conn: sqlite3.Connection, company_id: int) -> dict:
    return dict(conn.execute(
        """SELECT * FROM historicals WHERE company_id = ? AND period_type = 'actual'
           ORDER BY fiscal_year DESC LIMIT 1""",
        (company_id,),
    ).fetchone())


# Damodaran's published US implied equity risk premium is not something
# any free live API exposes -- it's a periodic research estimate, not a
# market quote -- so this is a documented static default the user can
# override, never presented as live-fetched.
DEFAULT_ERP = 0.0445
TICKERS_PATH = Path(__file__).parent / "data" / "tickers.csv"


@st.cache_data
def load_ticker_reference() -> pd.DataFrame:
    """A small curated set of large/liquid tickers (S&P 500 blue chips,
    other well-known NASDAQ/NYSE names, Ibovespa) for the sidebar's
    autocomplete -- not an exhaustive exchange listing (see README).
    Typing any other real ticker still works via fetch_market_data()."""
    if not TICKERS_PATH.exists():
        return pd.DataFrame(columns=["ticker", "name", "exchange"])
    return pd.read_csv(TICKERS_PATH)


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_risk_free_rate() -> float | None:
    """10Y Treasury yield from ^TNX (quoted at 10x the yield in percentage
    points, e.g. 44.5 -> 4.45%)."""
    try:
        hist = yf.Ticker("^TNX").history(period="5d")
        closes = hist["Close"].dropna()
        if closes.empty:
            return None
        return float(closes.iloc[-1]) / 1000
    except Exception:  # noqa: BLE001 — network/parse failures degrade to manual entry
        return None


@st.cache_data(ttl=900, show_spinner=False)
def fetch_market_data(ticker: str) -> tuple[dict | None, str | None]:
    """Live stock price + beta for `ticker` via Yahoo Finance, plus the 10Y
    Treasury yield and a documented static ERP default. Never fabricates:
    a field Yahoo doesn't have (e.g. beta for some tickers) is reported
    back as missing rather than guessed, so the caller can say so."""
    try:
        info = yf.Ticker(ticker).info
    except Exception as exc:  # noqa: BLE001
        return None, f"não consegui consultar {ticker} no Yahoo Finance ({exc})"
    price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
    if price is None:
        return None, f"ticker {ticker!r} não encontrado ou sem preço disponível"
    beta = info.get("beta")
    risk_free = _fetch_risk_free_rate()
    return {
        "stock_price": round(float(price), 2),
        "beta": round(float(beta), 2) if beta is not None else 1.0,
        "beta_missing": beta is None,
        "risk_free_rate": risk_free if risk_free is not None else 0.045,
        "risk_free_missing": risk_free is None,
        "equity_risk_premium": DEFAULT_ERP,
    }, None


def format_axis(value: float, fmt: str) -> str:
    if fmt == "pct":
        return f"{value:.2%}"
    if fmt == "num":
        return f"{value:.2f}"
    return f"{value:.4g}x"


def formula(text: str) -> None:
    st.markdown(f'<div class="formula-box">{text}</div>', unsafe_allow_html=True)


def note(text: str) -> None:
    st.markdown(f'<div class="note">{text}</div>', unsafe_allow_html=True)


def source_tag(text: str) -> None:
    st.markdown(f'<div class="source-tag">{text}</div>', unsafe_allow_html=True)


def line_items_table(rows: list[tuple[str, str, str]]) -> None:
    df = pd.DataFrame(rows, columns=["Componente", "Fórmula", "Valor"])
    st.table(df.set_index("Componente"))


def heatmap(table: dict, title: str) -> go.Figure:
    row_labels = [format_axis(v, table["row_format"]) for v in table["row_axis"]]
    col_labels = [format_axis(v, table["col_format"]) for v in table["col_axis"]]
    grid = table["grid"]
    fig = go.Figure(data=go.Heatmap(
        z=grid, x=col_labels, y=row_labels,
        text=[[f"${v:,.0f}" for v in row] for row in grid],
        texttemplate="%{text}",
        colorscale="RdYlGn",
        colorbar=dict(title="$/ação"),
    ))
    fig.update_layout(
        title=title,
        xaxis_title=table["col_label"],
        yaxis_title=table["row_label"],
        # Force categorical axes: Plotly otherwise parses the "%"-suffixed
        # labels as numbers and substitutes its own auto-generated linear
        # ticks instead of the actual axis values.
        xaxis=dict(type="category"),
        yaxis=dict(type="category", autorange="reversed"),
        height=420,
        margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig


def safe_result(fn, *args, **kwargs) -> tuple[dict | None, str | None]:
    """Call a target_price.from_* function, catching the ValueError those
    raise when a year isn't covered by the underlying data (e.g. trading_comps
    only has FY2027E/FY2030E market multiples) instead of crashing the page."""
    try:
        return fn(*args, **kwargs), None
    except ValueError as exc:
        return None, str(exc)


def grid_dataframe(table: dict) -> pd.DataFrame:
    row_labels = [format_axis(v, table["row_format"]) for v in table["row_axis"]]
    col_labels = [format_axis(v, table["col_format"]) for v in table["col_axis"]]
    return pd.DataFrame(table["grid"], index=row_labels, columns=col_labels)


st.markdown(CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------- sidebar --

st.sidebar.title("Calculadora de Valuation")

with st.sidebar.expander("Banco de dados", expanded=False):
    db_path_input = st.text_input("Caminho do .db", value=DEFAULT_DB_PATH)
    st.caption("Onde o arquivo enviado abaixo é gravado/lido. O schema é multi-empresa: cada envio soma ao banco, não substitui.")
    isolate_session = st.checkbox(
        "Isolar esta sessão (não gravar no banco compartilhado)",
        value=False,
        help=(
            "Se a interface estiver hospedada para mais de uma pessoa, marque isto "
            "antes de enviar uma planilha: em vez de todas as sessões escreverem no "
            "mesmo arquivo configurado acima, esta sessão passa a usar uma cópia "
            "temporária só sua, que some quando a sessão do navegador terminar."
        ),
    )

if "session_id" not in st.session_state:
    st.session_state.session_id = uuid.uuid4().hex[:8]

if isolate_session:
    p = Path(db_path_input)
    db_path = str(p.with_name(f"{p.stem}.session_{st.session_state.session_id}{p.suffix}"))
    st.sidebar.caption(f"Sessão isolada: `{db_path}`")
else:
    db_path = db_path_input

st.sidebar.subheader("Enviar planilha Bloomberg MODL")
uploaded_file = st.sidebar.file_uploader(
    "Arquivo .xlsx (abas: Multiple Periods; DCF e WACC só se você tiver)", type=["xlsx"],
)

if uploaded_file is not None:
    file_signature = hashlib.md5(uploaded_file.getvalue()).hexdigest()
    if st.session_state.get("last_upload_signature") != file_signature:
        uploaded_file.seek(0)
        try:
            sheet_names = openpyxl.load_workbook(uploaded_file, read_only=True).sheetnames
        except Exception as exc:  # noqa: BLE001
            st.sidebar.error(f"Falha ao ler o arquivo: {exc}")
            sheet_names = None
        uploaded_file.seek(0)

        market_data = None
        ready_to_load = sheet_names is not None
        needs_market_data = sheet_names is not None and not {"DCF", "WACC"} <= set(sheet_names)

        if needs_market_data:
            st.sidebar.info(
                "Esta planilha só tem 'Multiple Periods' (sem abas DCF/WACC prontas) — "
                "o formato normal de um export Bloomberg MODL. As premissas de cenário e "
                "o WACC vão ser calculados a partir dos dados históricos; só preciso de 4 "
                "números de mercado que não existem em nenhum export de demonstrações "
                "financeiras."
            )

            ticker_ref = load_ticker_reference()
            picker_options = ["Digitar manualmente…"] + [
                f"{row.ticker} — {row.name}" for row in ticker_ref.itertuples()
            ]
            picked = st.sidebar.selectbox(
                "Preencher via ticker (opcional)", picker_options, key="md_ticker_pick",
                help="Lista curada dos nomes mais conhecidos — qualquer outro ticker real também funciona, digite abaixo.",
            )
            manual_ticker = st.sidebar.text_input(
                "Ou digite o ticker (ex: AAPL, PETR4.SA)", key="md_ticker_manual",
            )
            if st.sidebar.button("Buscar preço, beta e 10Y no Yahoo Finance"):
                resolved = manual_ticker.strip() or (
                    picked.split(" — ")[0] if picked != "Digitar manualmente…" else ""
                )
                if not resolved:
                    st.sidebar.warning("Escolha da lista ou digite um ticker primeiro.")
                else:
                    fetched, fetch_err = fetch_market_data(resolved)
                    if fetched is None:
                        st.sidebar.error(f"Falha ao buscar {resolved}: {fetch_err}")
                    else:
                        st.session_state["md_stock_price"] = fetched["stock_price"]
                        st.session_state["md_beta"] = fetched["beta"]
                        st.session_state["md_risk_free_pct"] = round(fetched["risk_free_rate"] * 100, 3)
                        st.session_state["md_erp_pct"] = round(fetched["equity_risk_premium"] * 100, 3)
                        st.session_state["md_fetched_ticker"] = resolved
                        caveats = []
                        if fetched["beta_missing"]:
                            caveats.append("beta indisponível no Yahoo, mantive 1.00 — confira antes de carregar")
                        if fetched["risk_free_missing"]:
                            caveats.append("10Y Treasury indisponível, mantive 4.50% — confira antes de carregar")
                        msg = f"Preenchido com dados de {resolved}."
                        if caveats:
                            msg += " Atenção: " + "; ".join(caveats) + "."
                        (st.sidebar.warning if caveats else st.sidebar.success)(msg)

            with st.sidebar.form("market_data_form"):
                stock_price = st.number_input("Preço atual da ação ($)", min_value=0.0, value=0.0, step=0.01, key="md_stock_price")
                beta = st.number_input("Beta (5Y mensal)", min_value=0.0, value=1.0, step=0.05, key="md_beta")
                risk_free_pct = st.number_input("Risk-free rate — 10Y Treasury (%)", min_value=0.0, value=4.0, step=0.05, key="md_risk_free_pct")
                erp_pct = st.number_input("Equity Risk Premium (%)", min_value=0.0, value=4.5, step=0.05, key="md_erp_pct")
                submitted = st.form_submit_button("Carregar com esses dados de mercado")
            ready_to_load = submitted and stock_price > 0
            if submitted and stock_price <= 0:
                st.sidebar.error("Preço da ação precisa ser maior que zero.")
            fetched_ticker = st.session_state.get("md_fetched_ticker")
            source = (
                f"Yahoo Finance (yfinance), ticker {fetched_ticker} — preço/beta ao vivo, "
                "10Y Treasury via ^TNX, ERP fixo (Damodaran)"
                if fetched_ticker else "Informado manualmente no upload"
            )
            market_data = {
                "risk_free_rate": risk_free_pct / 100,
                "beta": beta,
                "equity_risk_premium": erp_pct / 100,
                "stock_price": stock_price,
                "source": source,
            }

        if ready_to_load:
            try:
                data = load_workbook_data(uploaded_file, market_data=market_data)
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
                write_to_db(Path(db_path), SCHEMA_PATH, data)
            except Exception as exc:  # noqa: BLE001 — surface any parse/load failure to the user
                st.sidebar.error(f"Falha ao carregar o arquivo: {exc}")
            else:
                st.session_state.last_upload_signature = file_signature
                st.session_state.last_uploaded_ticker = data["ticker"]
                get_connection.clear()
                st.sidebar.success(f"{data['ticker']} ({data['name']}) carregado em `{db_path}`.")
                st.rerun()

st.sidebar.divider()

if not Path(db_path).exists():
    st.info(
        f"Nenhum banco encontrado ainda em `{db_path}`. Envie um arquivo Bloomberg "
        f"MODL (.xlsx) na barra lateral para gerá-lo e calcular tudo automaticamente."
    )
    st.stop()

conn = get_connection(db_path)
tickers = list_tickers(conn)
if not tickers:
    st.info(
        "O banco em `{}` existe mas ainda não tem nenhuma empresa carregada. "
        "Envie um arquivo Bloomberg MODL (.xlsx) na barra lateral.".format(db_path)
    )
    st.stop()

default_ticker_index = 0
if st.session_state.get("last_uploaded_ticker") in tickers:
    default_ticker_index = tickers.index(st.session_state["last_uploaded_ticker"])

ticker = st.sidebar.selectbox("Empresa", tickers, index=default_ticker_index)
scenario = st.sidebar.radio("Cenário", ["bear", "base", "bull"], index=1, horizontal=True)
explicit_years = st.sidebar.radio(
    "Anos de projeção explícita (DCF)",
    [5, 13],
    index=0,
    help=(
        "O arquivo Bloomberg original soma apenas 5 anos na avaliação principal "
        "(o preço-alvo de $388.54 validado no caso base) apesar de projetar 13 "
        "anos completos. Ver aba Metodologia."
    ),
)
target_year = st.sidebar.selectbox(
    "Ano-alvo (métodos de múltiplo)",
    TARGET_YEAR_OPTIONS,
    index=1,
    help="P/E e PEG usam EPS de consenso Bloomberg, disponível só até FY2030E.",
)

company_id = get_company_id(conn, ticker)
company = get_company(conn, ticker)
wacc_data = get_wacc(conn, company_id)
latest_actual = get_latest_actual(conn, company_id)
current_price = wacc_data["stock_price"]
dcf_result = run_dcf(conn, ticker, scenario, explicit_years=explicit_years)

as_of = company["as_of_date"] or "sem data-base (planilha não traz esse carimbo fora das abas DCF/WACC)"
st.title(f"{company['name']} ({company['ticker']})")
st.caption(
    f"As of {as_of} · valores em {company['currency']} {company['units']} · "
    f"cenário: {scenario} · fonte: {db_path}"
)

tab_summary, tab_wacc, tab_dcf, tab_terminal, tab_multiples, tab_sens, tab_method, tab_glossary = st.tabs(
    ["Sumário", "WACC", "Projeção & FCF", "Valor Terminal", "Múltiplos", "Sensibilidade", "Metodologia", "Glossário"]
)

# =============================================================== Sumário ===
with tab_summary:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Preço atual", f"${current_price:,.2f}")
    m2.metric(
        "Preço-alvo (DCF)",
        f"${dcf_result['price_per_share']:,.2f}",
        f"{dcf_result['price_per_share'] / current_price - 1:+.1%}",
    )
    m3.metric("WACC", f"{dcf_result['wacc_used']:.2%}")
    m4.metric("g implícito (perpetuidade)", f"{dcf_result['implied_perpetuity_growth_rate']:.2%}")

    st.markdown(
        f"Equity Value (\\${dcf_result['equity_value']:,.0f}M) ÷ "
        f"Ações Diluídas ({dcf_result['shares_outstanding']:,.2f}M) = "
        f"**\\${dcf_result['price_per_share']:,.2f}**, usando "
        f"**{explicit_years} de {dcf_result['total_projected_years']}** anos de "
        f"projeção explícita para o caso **{scenario}**. Detalhe completo do cálculo "
        f"nas abas *WACC*, *Projeção & FCF* e *Valor Terminal*."
    )

    st.subheader("Football field — comparação de métodos")
    st.caption(
        "As barras comparam **valor presente**: o preço-alvo de cada método de "
        "múltiplo (uma estimativa para FY{}E, daqui a {} anos) é trazido a valor "
        "presente pelo WACC — a mesma base do preço do DCF, que já é um valor "
        "presente por construção. O preço-alvo nominal (o número que um relatório "
        "de research imprimiria, como o alvo de US\\$350 da Morgan Stanley para a "
        "Vertiv em 12-18 meses) e o ano a que ele se refere aparecem na tabela abaixo."
        .format(target_year, target_year - latest_actual["fiscal_year"])
    )

    results = [{
        "method": "DCF",
        "target_price": dcf_result["price_per_share"],
        "present_value_target_price": dcf_result["price_per_share"],
        "target_year": latest_actual["fiscal_year"],
        "years_out": 0,
    }]
    unavailable = []
    for name, fn, kwargs in [
        ("EV/EBITDA", from_ev_ebitda, dict(scenario=scenario, target_year=target_year)),
        ("P/E", from_pe, dict(target_year=target_year)),
        ("PEG", from_peg, dict(target_year=target_year)),
    ]:
        result, error = safe_result(fn, conn, ticker, **kwargs)
        if result is not None:
            results.append(result)
        else:
            unavailable.append((name, error))

    if unavailable:
        st.caption(
            "Indisponível para FY{}E: {} — sem dado de mercado para esse ano "
            "(ver aba Múltiplos para o motivo exato de cada método).".format(
                target_year, ", ".join(name for name, _ in unavailable)
            )
        )

    ff_df = pd.DataFrame(
        [{"Método": r["method"], "Valor Presente": r["present_value_target_price"]} for r in results]
    ).sort_values("Valor Presente")

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=ff_df["Valor Presente"], y=ff_df["Método"], orientation="h",
        text=[f"${v:,.2f}" for v in ff_df["Valor Presente"]], textposition="outside",
        marker_color="#4C78A8",
    ))
    fig.add_vline(
        x=current_price, line_dash="dash", line_color="gray",
        annotation_text=f"Preço atual (${current_price:,.2f})", annotation_position="top",
    )
    fig.update_layout(
        xaxis_title="Preço por ação, valor presente ($)", yaxis_title=None, height=320,
        margin=dict(l=10, r=10, t=30, b=10),
    )
    st.plotly_chart(fig, use_container_width=True)

    upside_df = pd.DataFrame([
        {
            "Método": r["method"],
            "Horizonte": "hoje (DCF)" if r["years_out"] == 0 else f"FY{r['target_year']}E (+{r['years_out']}a)",
            "Preço-alvo nominal": r["target_price"],
            "Valor Presente": r["present_value_target_price"],
            "Upside/(Downside) (VP)": r["present_value_target_price"] / current_price - 1,
        }
        for r in results
    ]).set_index("Método")
    st.table(
        upside_df.style.format({
            "Preço-alvo nominal": "${:,.2f}",
            "Valor Presente": "${:,.2f}",
            "Upside/(Downside) (VP)": "{:+.1%}",
        })
    )
    st.caption(
        "\"Preço-alvo nominal\" é o valor projetado para o ano-alvo, sem desconto — "
        "compare-o com preços-alvo de research (ex.: o caso Vertiv/Morgan Stanley acima). "
        "\"Valor Presente\" e o upside implícito usam esse mesmo preço descontado a WACC, "
        "e são a base comparável usada no gráfico. Fórmulas e inputs completos de cada "
        "método de múltiplo: aba *Múltiplos*."
    )

    if company["data_source"] == "modl_tabs":
        st.markdown("#### Achados sobre o arquivo original")
        note(
            "<b>5 vs. 13 anos de projeção explícita.</b> O template Bloomberg projeta FCF "
            "para 13 anos (FY2026E–FY2038E) mas soma apenas os 5 primeiros na célula de "
            "avaliação (<code>C78 = SUM(C75:G75)</code>), com valor terminal ancorado no "
            "EBITDA do ano 5. 8 anos de fluxo de caixa calculado ficam de fora da conta. "
            "Compare no seletor \u201cAnos de projeção explícita\u201d na barra lateral: "
            "$388.54 (5 anos, validado) vs. $412.19 (13 anos)."
        )
        note(
            "<b>As três tabelas de sensibilidade da planilha não concordam entre si nem com "
            "a célula principal.</b> Ver aba Sensibilidade e Metodologia para a tabela "
            "completa de comparação."
        )
    else:
        st.markdown("#### Como esses números foram calculados")
        note(
            "Essa planilha não tinha abas DCF/WACC prontas — o caso normal de um export "
            "Bloomberg MODL (só a AppLovin, o exemplo padrão, teve essas abas montadas à "
            "mão). Premissas de cenário, WACC e o múltiplo terminal foram <b>calculados</b> "
            "a partir dos dados históricos + os 4 números de mercado informados no upload "
            "— não extraídos de fórmulas prontas. Ver aba Metodologia para o detalhe."
        )

# =================================================================== WACC ===
with tab_wacc:
    st.markdown("### Custo de capital próprio (CAPM)")
    formula("Ke = Rf + β × ERP")
    line_items_table([
        ("Risk-free rate (10Y Treasury)", wacc_data["source"], f"{wacc_data['risk_free_rate']:.2%}"),
        ("Beta (5Y mensal)", wacc_data["source"], f"{wacc_data['beta']:.2f}"),
        ("Equity Risk Premium", wacc_data["source"], f"{wacc_data['equity_risk_premium']:.2%}"),
        (
            "Custo de Equity (Ke)",
            f"{wacc_data['risk_free_rate']:.2%} + {wacc_data['beta']:.2f} × {wacc_data['equity_risk_premium']:.2%}",
            f"{wacc_data['cost_of_equity']:.2%}",
        ),
    ])

    st.markdown("### Custo de dívida (última ano reportado)")
    formula(
        "Kd (pré-imposto) = Despesa de Juros / Dívida Total\n"
        "Alíquota efetiva = Impostos / Lucro antes de Impostos\n"
        "Kd (pós-imposto) = Kd (pré-imposto) × (1 − Alíquota efetiva)"
    )
    source_tag(f"Fonte: historicals, FY{latest_actual['fiscal_year']}A (último ano reportado)")
    line_items_table([
        ("Dívida de longo prazo", "input (FY{}A)".format(latest_actual["fiscal_year"]), f"${wacc_data['total_debt']:,.1f}M"),
        ("Despesa de juros líquida", "input (FY{}A)".format(latest_actual["fiscal_year"]), f"${wacc_data['interest_expense']:,.1f}M"),
        (
            "Kd pré-imposto",
            f"{wacc_data['interest_expense']:,.1f} / {wacc_data['total_debt']:,.1f}",
            f"{wacc_data['pretax_cost_of_debt']:.2%}",
        ),
        ("Alíquota efetiva", f"{latest_actual['tax_expense']:,.1f} / {latest_actual['pretax_income']:,.1f}", f"{wacc_data['effective_tax_rate']:.2%}"),
        (
            "Kd pós-imposto",
            f"{wacc_data['pretax_cost_of_debt']:.2%} × (1 − {wacc_data['effective_tax_rate']:.2%})",
            f"{wacc_data['after_tax_cost_of_debt']:.2%}",
        ),
    ])

    st.markdown("### Estrutura de capital")
    formula(
        "Market Cap = Preço da ação × Ações em circulação\n"
        "Enterprise Value = Market Cap + Dívida Líquida\n"
        "Peso Equity = Market Cap / EV;  Peso Dívida = Dívida Líquida / EV"
    )
    line_items_table([
        ("Preço da ação", wacc_data["source"], f"${wacc_data['stock_price']:,.2f}"),
        ("Ações em circulação (diluídas)", "historicals, FY{}A".format(latest_actual["fiscal_year"]), f"{wacc_data['shares_outstanding']:,.2f}M"),
        ("Market Cap", f"{wacc_data['stock_price']:,.2f} × {wacc_data['shares_outstanding']:,.2f}", f"${wacc_data['market_cap']:,.1f}M"),
        ("Dívida Líquida", f"FY{latest_actual['fiscal_year']}A", f"${wacc_data['net_debt']:,.1f}M"),
        ("Enterprise Value", f"{wacc_data['market_cap']:,.1f} + {wacc_data['net_debt']:,.1f}", f"${wacc_data['enterprise_value']:,.1f}M"),
        ("Peso Equity", f"{wacc_data['market_cap']:,.1f} / {wacc_data['enterprise_value']:,.1f}", f"{wacc_data['equity_weight']:.2%}"),
        ("Peso Dívida", f"{wacc_data['net_debt']:,.1f} / {wacc_data['enterprise_value']:,.1f}", f"{wacc_data['debt_weight']:.2%}"),
    ])

    st.markdown("### WACC")
    formula("WACC = Peso Equity × Ke + Peso Dívida × Kd (pós-imposto)")
    wacc_build = pd.DataFrame([
        {"Componente": "Equity", "Peso": wacc_data["equity_weight"], "Custo": wacc_data["cost_of_equity"],
         "Contribuição": wacc_data["equity_weight"] * wacc_data["cost_of_equity"]},
        {"Componente": "Dívida", "Peso": wacc_data["debt_weight"], "Custo": wacc_data["after_tax_cost_of_debt"],
         "Contribuição": wacc_data["debt_weight"] * wacc_data["after_tax_cost_of_debt"]},
    ]).set_index("Componente")
    st.table(wacc_build.style.format({"Peso": "{:.2%}", "Custo": "{:.2%}", "Contribuição": "{:.2%}"}))
    st.metric("WACC (soma das contribuições)", f"{wacc_data['wacc']:.4%}")
    source_tag("Calculado por dcf_engine.get_wacc()")

# ======================================================== Projeção & FCF ===
with tab_dcf:
    st.markdown("### Construção da receita e do FCF não alavancado, ano a ano")
    formula(
        "Receita_t = Receita_{t-1} × (1 + Crescimento_t)\n"
        "EBIT_t = Receita_t × Margem EBIT_t\n"
        "Impostos_t = -EBIT_t × Alíquota_t   |   NOPAT_t = EBIT_t + Impostos_t\n"
        "D&A_t = Receita_t × D&A%_t   |   CapEx_t = Receita_t × CapEx%_t\n"
        "ΔNWC_t = (Receita_t − Receita_{t-1}) × NWC%_t\n"
        "UFCF_t = NOPAT_t + D&A_t − CapEx_t − ΔNWC_t"
    )
    source_tag(
        f"Premissas de scenario_assumptions (cenário '{scenario}'); receita semeada em "
        f"FY{latest_actual['fiscal_year']}A = ${latest_actual['revenue']:,.1f}M. "
        f"Calculado por dcf_engine.project_financials()."
    )

    proj_df = pd.DataFrame(dcf_result["projection"]).set_index("fiscal_year")
    used_years = set(proj_df.index[:explicit_years])

    def highlight_used(row: pd.Series) -> list[str]:
        if row.name in used_years:
            return ["background-color: #eaf3ea"] * len(row)
        return ["background-color: #fbeeee; color:#8a7f7f"] * len(row)

    pct_cols = ["revenue_growth", "ebit_margin", "tax_rate"]
    fmt = {col: "{:.2%}" for col in pct_cols}
    fmt.update({col: "{:,.1f}" for col in proj_df.columns if col not in pct_cols})
    st.dataframe(
        proj_df.style.apply(highlight_used, axis=1).format(fmt),
        use_container_width=True,
    )
    st.markdown(
        f"🟩 verde = anos incluídos na avaliação (1–{explicit_years}) · "
        f"🟥 vermelho = anos projetados mas **não** somados no preço-alvo com a "
        f"configuração atual — é o núcleo do achado \"5 vs. 13 anos\"."
    )

    st.markdown("### Desconto a valor presente (convenção mid-year)")
    formula(
        "Período_t = t − 0.5 (ano 1 → 0.5; ano 2 → 1.5; ...)\n"
        "Fator de Desconto_t = 1 / (1 + WACC) ^ Período_t\n"
        "VP(FCF)_t = UFCF_t × Fator de Desconto_t"
    )
    disc_df = pd.DataFrame(dcf_result["discounted_cash_flows"]).set_index("fiscal_year")
    disc_cols = ["revenue", "ufcf", "period", "discount_factor", "pv_ufcf"]
    st.dataframe(
        disc_df[disc_cols].style.format({
            "revenue": "{:,.1f}", "ufcf": "{:,.1f}", "period": "{:.1f}",
            "discount_factor": "{:.4f}", "pv_ufcf": "{:,.1f}",
        }),
        use_container_width=True,
    )
    st.metric(f"Soma de VP(FCF), anos 1–{explicit_years}", f"${dcf_result['sum_pv_ufcf']:,.1f}M")

# =============================================================== Terminal ===
with tab_terminal:
    terminal_year_row = dcf_result["discounted_cash_flows"][-1]
    st.markdown(f"### EBITDA terminal (ano {explicit_years}: FY{terminal_year_row['fiscal_year']})")
    formula("EBITDA terminal = EBIT do último ano explícito + D&A do último ano explícito")
    line_items_table([
        (f"EBIT (FY{terminal_year_row['fiscal_year']})", "projeção", f"${terminal_year_row['ebit']:,.1f}M"),
        (f"D&A (FY{terminal_year_row['fiscal_year']})", "projeção", f"${terminal_year_row['da']:,.1f}M"),
        ("EBITDA terminal", f"{terminal_year_row['ebit']:,.1f} + {terminal_year_row['da']:,.1f}", f"${dcf_result['terminal_ebitda']:,.1f}M"),
    ])

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("#### Método 1 — EV/EBITDA Exit Multiple")
        formula(
            "TV = EBITDA terminal × Múltiplo de saída\n"
            "VP(TV) = TV / (1 + WACC) ^ Período do último ano explícito"
        )
        line_items_table([
            ("Múltiplo de saída (EV/EBITDA)", f"terminal_assumptions ('{scenario}')", f"{dcf_result['exit_multiple_ebitda']:.1f}x"),
            ("Valor Terminal (TV)", f"{dcf_result['terminal_ebitda']:,.1f} × {dcf_result['exit_multiple_ebitda']:.1f}", f"${dcf_result['terminal_value_exit_multiple']:,.1f}M"),
            ("VP(TV)", f"TV / (1+{dcf_result['wacc_used']:.2%})^{terminal_year_row['period']:.1f}", f"${dcf_result['pv_terminal_value']:,.1f}M"),
        ])
    with col_b:
        st.markdown("#### Cross-check — g implícito")
        formula(
            "Resolvendo TV = FCF_terminal × (1+g) / (WACC−g) para g:\n"
            "g = (TV × WACC − FCF_terminal) / (FCF_terminal + TV)"
        )
        line_items_table([
            ("FCF do ano terminal", "UFCF do último ano explícito", f"${terminal_year_row['ufcf']:,.1f}M"),
            ("g implícito", "ver fórmula acima", f"{dcf_result['implied_perpetuity_growth_rate']:.2%}"),
        ])
        st.markdown("#### Método 2 — Gordon Growth")
        formula("TV (Gordon Growth) = FCF_terminal × (1+g) / (WACC − g)")
        line_items_table([
            ("g usado", "= g implícito por padrão (reconcilia os dois métodos)", f"{dcf_result['gordon_growth_rate_used']:.2%}"),
            ("TV (Gordon Growth)", "ver fórmula acima", f"${dcf_result['terminal_value_gordon_growth']:,.1f}M"),
        ])
    note(
        "Com <code>g</code> = g implícito (padrão), os dois métodos de valor terminal "
        "batem exatamente — essa <b>é</b> a reconciliação. Passe "
        "<code>--gordon-growth-rate</code> na CLI do dcf_engine.py para ver os dois "
        "métodos divergirem e testar a sensibilidade da premissa de múltiplo de saída "
        "contra uma taxa de crescimento perpétuo explícita."
    )

    st.markdown("### Enterprise Value → Preço por ação")
    formula(
        "EV = Soma de VP(FCF) + VP(TV)\n"
        "Equity Value = EV − Dívida Líquida\n"
        "Preço por ação = Equity Value / Ações em circulação"
    )
    line_items_table([
        ("Soma de VP(FCF)", "aba Projeção & FCF", f"${dcf_result['sum_pv_ufcf']:,.1f}M"),
        ("VP(TV)", "acima", f"${dcf_result['pv_terminal_value']:,.1f}M"),
        ("Enterprise Value", f"{dcf_result['sum_pv_ufcf']:,.1f} + {dcf_result['pv_terminal_value']:,.1f}", f"${dcf_result['enterprise_value']:,.1f}M"),
        ("(−) Dívida Líquida", "aba WACC", f"−${dcf_result['net_debt']:,.1f}M"),
        ("Equity Value", f"{dcf_result['enterprise_value']:,.1f} − {dcf_result['net_debt']:,.1f}", f"${dcf_result['equity_value']:,.1f}M"),
        ("÷ Ações em circulação", "aba WACC", f"{dcf_result['shares_outstanding']:,.2f}M"),
        ("Preço por ação (DCF)", "Equity Value / Ações", f"${dcf_result['price_per_share']:,.2f}"),
    ])

# ============================================================== Múltiplos ===
with tab_multiples:
    years_out_selected = target_year - latest_actual["fiscal_year"]
    st.markdown(
        f"Ano-alvo selecionado: **FY{target_year}E** (daqui a **{years_out_selected} anos**). "
        f"Métodos P/E e PEG usam EPS de consenso Bloomberg e por isso não variam por cenário "
        f"bear/base/bull (o arquivo original não tem premissas de EPS por cenário)."
    )
    st.caption(
        "Cada método abaixo mostra o preço-alvo nominal para o ano-alvo (o número que um "
        "relatório de research imprimiria) e, na última linha, esse mesmo preço trazido a "
        "valor presente pelo WACC — comparável ao preço do DCF."
    )

    st.markdown("### EV/EBITDA")
    ev_ebitda_result, ev_ebitda_error = safe_result(
        from_ev_ebitda, conn, ticker, scenario=scenario, target_year=target_year
    )
    formula(
        "EV = EBITDA projetado(ano-alvo) × Múltiplo EV/EBITDA\n"
        "Equity Value = EV − Dívida Líquida\n"
        "Preço-alvo = Equity Value / Ações em circulação\n"
        "Valor Presente = Preço-alvo / (1 + WACC) ^ anos até o ano-alvo"
    )
    if ev_ebitda_result:
        line_items_table([
            (f"EBITDA projetado (FY{target_year}E, cenário {scenario})", "dcf_engine.project_financials()", f"${ev_ebitda_result['ebitda']:,.1f}M"),
            ("Múltiplo EV/EBITDA usado", ev_ebitda_result["multiple_source"], f"{ev_ebitda_result['ev_ebitda_multiple']:.1f}x"),
            ("Enterprise Value", f"{ev_ebitda_result['ebitda']:,.1f} × {ev_ebitda_result['ev_ebitda_multiple']:.1f}", f"${ev_ebitda_result['enterprise_value']:,.1f}M"),
            ("(−) Dívida Líquida", "", f"−${ev_ebitda_result['net_debt']:,.1f}M"),
            ("Equity Value", "", f"${ev_ebitda_result['equity_value']:,.1f}M"),
            (f"Preço-alvo (FY{target_year}E, nominal)", "Equity Value / Ações", f"${ev_ebitda_result['target_price']:,.2f}"),
            (
                f"Valor Presente ({ev_ebitda_result['years_out']} anos, WACC {dcf_result['wacc_used']:.2%})",
                f"{ev_ebitda_result['target_price']:,.2f} / (1+{dcf_result['wacc_used']:.2%})^{ev_ebitda_result['years_out']}",
                f"${ev_ebitda_result['present_value_target_price']:,.2f}",
            ),
        ])
        source_tag("target_price.from_ev_ebitda()")
    else:
        st.warning(f"EV/EBITDA indisponível para FY{target_year}E: {ev_ebitda_error}")

    st.markdown("### P/E")
    pe_result, pe_error = safe_result(from_pe, conn, ticker, target_year=target_year)
    formula(
        "Preço-alvo = Múltiplo P/E × EPS diluído consenso (ano-alvo)\n"
        "Valor Presente = Preço-alvo / (1 + WACC) ^ anos até o ano-alvo"
    )
    if pe_result:
        line_items_table([
            (f"EPS diluído ajustado (FY{target_year}E)", "historicals (consenso Bloomberg)", f"${pe_result['eps']:,.2f}"),
            ("Múltiplo P/E usado", pe_result["multiple_source"], f"{pe_result['pe_multiple']:.2f}x"),
            (f"Preço-alvo (FY{target_year}E, nominal)", f"{pe_result['pe_multiple']:.2f} × {pe_result['eps']:,.2f}", f"${pe_result['target_price']:,.2f}"),
            (
                f"Valor Presente ({pe_result['years_out']} anos, WACC {dcf_result['wacc_used']:.2%})",
                f"{pe_result['target_price']:,.2f} / (1+{dcf_result['wacc_used']:.2%})^{pe_result['years_out']}",
                f"${pe_result['present_value_target_price']:,.2f}",
            ),
        ])
        source_tag("target_price.from_pe() — múltiplo default = trading_comps (P/E de mercado atual para o ano)")
    else:
        st.warning(
            f"P/E indisponível para FY{target_year}E: {pe_error}. A planilha original só "
            f"traz múltiplo de mercado (trading_comps) para FY2027E e FY2030E — escolha um "
            f"desses anos, ou chame target_price.from_pe(..., pe_multiple=X) com um múltiplo próprio."
        )

    st.markdown("### PEG")
    peg_result, peg_error = safe_result(from_peg, conn, ticker, target_year=target_year)
    formula(
        "CAGR do EPS = (EPS_alvo / EPS_base) ^ (1/anos) − 1\n"
        "P/E implícito = PEG-alvo × (CAGR do EPS × 100)\n"
        "Preço-alvo = P/E implícito × EPS_alvo\n"
        "Valor Presente = Preço-alvo / (1 + WACC) ^ anos até o ano-alvo"
    )
    if peg_result:
        line_items_table([
            (f"EPS base (FY{peg_result['base_year']}A)", "historicals", f"${peg_result['eps_base']:,.2f}"),
            (f"EPS alvo (FY{peg_result['target_year']}E)", "historicals", f"${peg_result['eps_target']:,.2f}"),
            ("CAGR do EPS", f"({peg_result['eps_target']:,.2f}/{peg_result['eps_base']:,.2f})^(1/{peg_result['target_year']-peg_result['base_year']}) − 1", f"{peg_result['eps_cagr']:.2%}"),
            ("PEG-alvo", "heurística 'valor justo'", f"{peg_result['target_peg']:.2f}x"),
            ("P/E implícito", f"{peg_result['target_peg']:.2f} × {peg_result['eps_cagr']*100:.2f}", f"{peg_result['implied_pe']:.2f}x"),
            (f"Preço-alvo (FY{peg_result['target_year']}E, nominal)", f"{peg_result['implied_pe']:.2f} × {peg_result['eps_target']:,.2f}", f"${peg_result['target_price']:,.2f}"),
            (
                f"Valor Presente ({peg_result['years_out']} anos, WACC {dcf_result['wacc_used']:.2%})",
                f"{peg_result['target_price']:,.2f} / (1+{dcf_result['wacc_used']:.2%})^{peg_result['years_out']}",
                f"${peg_result['present_value_target_price']:,.2f}",
            ),
        ])
        note(
            "Em nomes de altíssimo crescimento como a AppLovin (CAGR de EPS de consenso "
            "~45% entre FY2025A e FY2027E), a heurística PEG-alvo = 1,0x produz um P/E "
            "implícito extremo e, por consequência, um preço-alvo bem acima dos demais "
            "métodos. Não é um bug — é uma limitação conhecida da regra PEG=1 aplicada "
            "fora do regime de crescimento moderado onde ela foi pensada."
        )
        source_tag("target_price.from_peg()")
    else:
        st.warning(f"PEG indisponível para FY{target_year}E: {peg_error}")

# ============================================================= Sensibilidade ===
with tab_sens:
    st.markdown(
        "Cada célula abaixo é uma chamada independente a `dcf_engine.run_dcf()` "
        "com WACC/múltiplo (ou crescimento/margem) deslocados — por isso a célula "
        "central de cada grid sempre bate com o preço-alvo do DCF na aba Sumário."
    )

    wacc_multiple = sensitivity_wacc_exit_multiple(conn, ticker, scenario, explicit_years)
    growth_margin = sensitivity_growth_margin(conn, ticker, scenario, explicit_years)
    beta_risk_free = sensitivity_beta_risk_free(conn, ticker, scenario, explicit_years)

    col1, col2 = st.columns(2)
    with col1:
        st.plotly_chart(heatmap(wacc_multiple, "WACC × Múltiplo de Saída"), use_container_width=True)
        with st.expander("Ver grid em números"):
            st.dataframe(grid_dataframe(wacc_multiple).style.format("${:,.2f}"), use_container_width=True)
    with col2:
        st.plotly_chart(heatmap(growth_margin, "Crescimento × Margem EBIT (Δ)"), use_container_width=True)
        with st.expander("Ver grid em números"):
            st.dataframe(grid_dataframe(growth_margin).style.format("${:,.2f}"), use_container_width=True)

    col3, _unused = st.columns(2)
    with col3:
        st.plotly_chart(heatmap(beta_risk_free, "Risk-Free Rate × Beta"), use_container_width=True)
        with st.expander("Ver grid em números"):
            st.dataframe(grid_dataframe(beta_risk_free).style.format("${:,.2f}"), use_container_width=True)
        st.caption(
            "Recalcula o custo de equity (Rf + beta x ERP) para cada par, mantendo "
            "peso de capital e custo de divida fixos no valor do caso base, e "
            "alimenta o WACC resultante em dcf_engine.run_dcf() -- mesmo principio "
            "das outras duas tabelas."
        )

    if company["data_source"] == "modl_tabs":
        st.markdown("### O arquivo original não reconcilia essas tabelas com a própria célula principal")
        st.markdown(
            "Ao validar `sensitivity.py` célula a célula contra o arquivo Bloomberg, as "
            "referências de \"preço-alvo no caso base\" da própria planilha divergem "
            "entre si:"
        )
        mismatch_df = pd.DataFrame(
            SOURCE_FILE_BASE_CASE_CELLS, columns=["Célula / fonte", "Fórmula usada pela planilha", "Preço-alvo ($)"]
        ).set_index("Célula / fonte")
        st.table(mismatch_df.style.format({"Preço-alvo ($)": "${:,.2f}"}))
        note(
            "A Tabela 2 (Cresc. × Margem) é a única internamente consistente — soma os 13 "
            "anos completos com o valor terminal alinhado ao último ano. É por isso que, "
            "com \u201cAnos de projeção explícita\u201d = 13 na barra lateral, o grid de "
            "Crescimento × Margem acima reproduz D110 = $412.19 célula a célula. As "
            "Tabelas 1 (WACC × Múltiplo) e 3 (Risk-Free × Beta) somam 13 anos de FCF mas "
            "ainda âncoram o EBITDA terminal no ano 5, descontando esse valor obsoleto 8 "
            "anos além do que deveria — por isso as duas dão o mesmo $313.31 no caso "
            "base, apesar de sensibilizarem inputs completamente diferentes. Nem "
            "conservador nem agressivo, apenas inconsistente."
        )
    else:
        st.markdown("### Sensibilidade em cima de premissas calculadas, não extraídas")
        note(
            "Essa empresa não tem um arquivo Bloomberg original com abas DCF/WACC para "
            "comparar contra — as premissas de cenário, o WACC e o múltiplo terminal "
            "acima já são o resultado de <code>data/load_from_modl.py</code> calculando "
            "tudo a partir dos dados históricos (ver aba Metodologia). Os três grids "
            "ainda reconciliam entre si e com o preço-alvo do DCF na aba Sumário, pelo "
            "mesmo motivo: todos chamam <code>dcf_engine.run_dcf()</code>."
        )

# =============================================================== Metodologia ===
with tab_method:
    if company["data_source"] == "modl_tabs":
        st.markdown(
            """
### Linhagem dos dados

`data/load_from_modl.py` lê três abas do arquivo Bloomberg MODL (`Multiple
Periods`, `DCF`, `WACC`) e grava em `data/valuation.db` seguindo o schema em
`data/schema.sql`: `companies`, `historicals`, `wacc_inputs`,
`scenario_assumptions`, `terminal_assumptions`, `trading_comps`.

Todos os números desta página vêm dessas tabelas via `dcf_engine.py`,
`target_price.py` e `sensitivity.py` — nada é recalculado aqui na interface.

### Achado 1 — 5 vs. 13 anos de projeção explícita

O template Bloomberg projeta FCF para 13 anos (FY2026E–FY2038E: 5 anos de
consenso de mercado + 8 anos de premissas assumidas com fade de crescimento),
mas a célula de avaliação principal (`C78 = SUM(C75:G75)` na aba `DCF`) soma
apenas os 5 primeiros, com o valor terminal ancorado no EBITDA do ano 5. Os
outros 8 anos de FCF são calculados na planilha mas nunca entram na conta.

`dcf_engine.run_dcf()` reproduz esse comportamento por padrão
(`explicit_years=5`) porque é isso que o preço-alvo de $388.54 — validado
célula a célula contra o arquivo original — representa. O parâmetro
`explicit_years` (exposto aqui como "Anos de projeção explícita") permite
rodar a versão de 13 anos lado a lado.

### Achado 2 — as três tabelas de sensibilidade da planilha usam fórmulas diferentes entre si

Ver aba Sensibilidade para a tabela de comparação completa. Em resumo: a
célula principal (`C87`), a Tabela 1 de sensibilidade (`D96`), a Tabela 2 de
sensibilidade (`D110`) e a Tabela 3 de sensibilidade (`D124`) dão números
diferentes no mesmo cenário base — $388.54, $313.31, $412.19 e $313.31 de
novo, respectivamente — porque cada uma usa uma combinação distinta de
"quantos anos somar" e "em que ano ancorar o valor terminal" (a Tabela 3
repete o número da Tabela 1 porque usa exatamente a mesma fórmula,
só que sensibilizando Risk-Free/Beta em vez do WACC direto).
`sensitivity.py` não reproduz nenhuma dessas inconsistências: ele sempre
chama `dcf_engine.run_dcf()`, então qualquer célula de qualquer grid aqui
é diretamente comparável ao preço-alvo do DCF mostrado na aba Sumário.
            """
        )
    else:
        st.markdown(
            """
### Linhagem dos dados

Essa empresa não tem abas `DCF`/`WACC` prontas no arquivo Bloomberg MODL —
esse é o caso normal de um export bruto. `data/load_from_modl.py` lê só a
aba `Multiple Periods` (históricos e consenso) e recebe um pequeno conjunto
de inputs de mercado no momento do upload (preço da ação, beta, risk-free
rate, equity risk premium). A partir disso ele mesmo calcula premissas de
cenário, WACC e múltiplo terminal — nada é extraído de uma aba pronta, é
calculado por este projeto (ver `derive_scenario_assumptions()` em
`data/load_from_modl.py`). Tudo é gravado em `data/valuation.db` seguindo o
mesmo schema de sempre: `companies`, `historicals`, `wacc_inputs`,
`scenario_assumptions`, `terminal_assumptions`, `trading_comps` (vazia —
sem abas DCF/WACC não há bloco "TRADING MULTIPLES" para ler, então P/E e
PEG ficam indisponíveis, ver aba Sumário).

Todos os números desta página vêm dessas tabelas via `dcf_engine.py`,
`target_price.py` e `sensitivity.py` — nada é recalculado aqui na interface.

### Como as premissas de cenário são derivadas

- **Crescimento de receita**: usa o consenso de mercado (`historicals`) nos
  anos em que ele existe e depois faz um fade linear até a taxa terminal
  (3% ao ano) nos anos seguintes — ver `fade_to_terminal()`.
- **Margem EBIT, alíquota efetiva, D&A % receita, capex % receita, NWC %
  Δreceita**: média dos últimos anos reais (`period_type = 'actual'`),
  mantida constante daí para frente.
- **Bear/Bull**: multiplicam o crescimento por 0.7x/1.3x e deslocam a
  margem EBIT em -3pp/+2pp em relação ao caso base — um envelope mecânico
  em cima do caso derivado, não premissas de um analista.
- **Múltiplo terminal (EV/EBITDA)**: o EV/EBITDA implícito de mercado hoje
  (`(market cap + dívida líquida) / EBITDA ajustado` do último ano real) é
  usado como base, com bear/bull em 0.85x/1.15x desse valor.
- **WACC**: CAPM padrão (Rf + beta × ERP) para custo de equity; custo de
  dívida pré-imposto = despesa de juros / dívida bruta do último ano real,
  ajustado pela alíquota efetiva; pesos de capital a valor de mercado
  (market cap e dívida líquida).

Como não existe um DCF/WACC de referência da própria empresa para comparar
contra, não há um "achado" de inconsistência aqui — o objetivo dessas
premissas é ser uma primeira aproximação formulaica e razoável a partir dos
históricos, não reproduzir uma planilha específica.
            """
        )

    st.markdown(
        """
### Onde está cada cálculo no código

| Nesta página | Função |
|---|---|
| WACC | `dcf_engine.get_wacc()` |
| Projeção de receita/FCF | `dcf_engine.project_financials()` |
| Desconto e valor terminal | `dcf_engine.run_dcf()` |
| EV/EBITDA (múltiplos) | `target_price.from_ev_ebitda()` |
| P/E (múltiplos) | `target_price.from_pe()` |
| PEG (múltiplos) | `target_price.from_peg()` |
| Sensibilidade WACC × Múltiplo | `sensitivity.sensitivity_wacc_exit_multiple()` |
| Sensibilidade Crescimento × Margem | `sensitivity.sensitivity_growth_margin()` |
| Sensibilidade Risk-Free × Beta | `sensitivity.sensitivity_beta_risk_free()` |
        """
    )
    if company["data_source"] == "modl_tabs":
        st.markdown(
            "Leitura completa dos dois achados, com as fórmulas originais do Excel "
            "citadas célula a célula, está em `README.md` no repositório."
        )
    else:
        st.markdown(
            "Detalhes de implementação completos de como as premissas, o WACC e o "
            "múltiplo terminal são calculados estão em `data/load_from_modl.py` "
            "(`derive_scenario_assumptions()`, `load_workbook_data()`) e no "
            "`README.md`."
        )

# ============================================================== Glossário ===
with tab_glossary:
    st.markdown(
        "Todo termo técnico usado nas outras abas, explicado em uma frase. "
        "Os agrupamentos seguem a ordem em que os termos aparecem no app: "
        "WACC/CAPM primeiro, depois projeção e FCF, valor terminal, DCF, "
        "múltiplos e, por fim, sensibilidade."
    )

    def glossary_section(title: str, terms: list[tuple[str, str]]) -> None:
        st.markdown(f"#### {title}")
        st.table(
            pd.DataFrame(terms, columns=["Termo", "Definição"]).set_index("Termo")
        )

    glossary_section("WACC & CAPM", [
        ("WACC", "Weighted Average Cost of Capital — a taxa usada para trazer fluxos de caixa futuros a valor presente, ponderando o custo de equity e o custo de dívida pelos seus pesos na estrutura de capital a valor de mercado."),
        ("CAPM", "Capital Asset Pricing Model — modelo que estima o custo de equity (Ke) como taxa livre de risco mais beta vezes o prêmio de risco de mercado."),
        ("Ke (custo de equity)", "Retorno mínimo exigido pelos acionistas, estimado via CAPM: Rf + β × ERP."),
        ("Kd (custo de dívida)", "Taxa de juros média que a empresa paga sobre sua dívida, aqui líquida do benefício fiscal (Kd × (1 − alíquota efetiva))."),
        ("Risk-free rate (Rf)", "Retorno de um ativo sem risco de crédito, aproximado pelo yield do Treasury de 10 anos dos EUA (^TNX)."),
        ("Beta (β)", "Sensibilidade histórica do retorno da ação em relação ao mercado (5 anos, base mensal) — β > 1 significa mais volátil que o mercado."),
        ("ERP (Equity Risk Premium)", "Prêmio de retorno exigido para investir em ações em vez do ativo livre de risco; aqui um valor fixo (referência Damodaran) quando não vem do arquivo original."),
        ("Market cap", "Preço da ação × ações em circulação diluídas — o valor de mercado do equity."),
        ("Dívida líquida", "Dívida bruta menos caixa e equivalentes — o que resta a pagar aos credores depois de usar o caixa disponível."),
    ])

    glossary_section("Projeção & Fluxo de Caixa", [
        ("EBIT", "Earnings Before Interest and Taxes — lucro operacional, antes de juros e impostos."),
        ("EBITDA", "EBIT mais depreciação e amortização (D&A) — lucro operacional antes também dos efeitos não-caixa de D&A."),
        ("NOPAT", "Net Operating Profit After Tax — EBIT × (1 − alíquota efetiva), o lucro operacional já líquido de impostos, usado como ponto de partida do FCF."),
        ("D&A", "Depreciação e Amortização — desgaste contábil (não-caixa) de ativos fixos e intangíveis, somado de volta ao NOPAT no cálculo do FCF."),
        ("CapEx", "Capital Expenditures — investimento em ativos fixos (imobilizado), saída de caixa subtraída no FCF."),
        ("NWC / Δ NWC", "Necessidade de Capital de Giro (Net Working Capital) — capital preso em contas a receber, estoque e contas a pagar; sua variação (Δ NWC) é subtraída do FCF quando aumenta."),
        ("UFCF", "Unlevered Free Cash Flow — fluxo de caixa livre antes dos efeitos de dívida: NOPAT + D&A − CapEx − Δ NWC. É o fluxo descontado no DCF."),
        ("Mid-year convention", "Convenção de desconto que assume o fluxo de caixa de cada ano concentrado no meio do ano (período t − 0,5) em vez do fim do ano — reduz levemente o desconto porque o caixa chega, em média, mais cedo."),
        ("Cenário (bear/base/bull)", "Três conjuntos de premissas de crescimento e margem — pessimista, central e otimista — aplicados sobre a mesma metodologia de projeção."),
    ])

    glossary_section("Valor Terminal", [
        ("Valor Terminal (TV)", "Valor de todos os fluxos de caixa além do período de projeção explícita, condensado em um único número no último ano projetado."),
        ("Múltiplo de saída (exit multiple)", "Método de TV que aplica um múltiplo EV/EBITDA de mercado ao EBITDA do último ano projetado, como se a empresa fosse vendida naquele momento."),
        ("Gordon Growth (perpetuidade)", "Método de TV que assume o FCF do último ano crescendo para sempre a uma taxa constante g, descontado por (WACC − g)."),
        ("g (taxa de crescimento na perpetuidade)", "Taxa de crescimento perpétuo assumida (ou implícita) no valor terminal — precisa ser menor que o WACC para a fórmula de Gordon Growth fazer sentido."),
        ("g implícito", "A taxa g que, usada no método Gordon Growth, reproduziria o mesmo valor terminal do método de múltiplo de saída — um cross-check de consistência entre os dois métodos."),
    ])

    glossary_section("Preço-Alvo & Múltiplos", [
        ("EV (Enterprise Value)", "Valor da operação como um todo, antes de separar entre credores e acionistas: soma do valor presente dos FCFs mais o valor terminal (no DCF), ou EBITDA × múltiplo (nos métodos de múltiplo)."),
        ("Equity Value", "EV menos dívida líquida — o valor que sobra para os acionistas."),
        ("Preço-alvo (target price)", "Equity Value dividido pelas ações em circulação diluídas — o preço por ação implícito por um método específico."),
        ("P/E (Price/Earnings)", "Múltiplo de preço sobre lucro por ação (EPS) — aqui usado com EPS de consenso de mercado."),
        ("PEG", "P/E dividido pela taxa de crescimento do EPS (em pontos percentuais) — usado para checar se um P/E é 'caro' ou 'barato' relativo ao crescimento esperado; PEG = 1,0x é a heurística de 'valor justo'."),
        ("EPS diluído ajustado", "Lucro por ação diluído (considerando conversão de opções/ações restritas), ajustado por itens não-recorrentes — a métrica de consenso Bloomberg usada nos métodos P/E e PEG."),
        ("Trading comps", "Múltiplos de mercado implícitos hoje (preço atual da ação sobre métricas futuras), extraídos do bloco 'TRADING MULTIPLES' do arquivo original quando disponível."),
        ("Ano-alvo (target year)", "O ano futuro para o qual um método de múltiplo projeta um preço — o preço-alvo nominal é o valor esperado *nesse* ano, sem desconto."),
        ("Valor Presente (do preço-alvo)", "O preço-alvo nominal trazido para hoje via desconto ao WACC pelo número de anos até o ano-alvo — a base comparável ao preço do DCF, que já é um valor presente."),
        ("Upside / (Downside)", "Variação percentual entre um preço-alvo (ou seu valor presente) e o preço atual da ação."),
        ("Football field", "Gráfico que compara os preços-alvo de vários métodos lado a lado como barras horizontais, para visualizar a dispersão de estimativas."),
    ])

    glossary_section("Sensibilidade", [
        ("Grid de sensibilidade", "Tabela que recalcula o preço-alvo do DCF variando duas premissas ao mesmo tempo (ex.: WACC × múltiplo de saída), mostrando como o resultado reage a cada combinação."),
        ("Anos de projeção explícita", "Quantos anos de fluxo de caixa são somados diretamente (em vez de capturados no valor terminal) — o arquivo original usa 5 apesar de projetar 13."),
        ("Data source (modl_tabs vs. derivado)", "Indica se as premissas de WACC/cenário/múltiplo terminal vieram prontas das abas DCF/WACC do arquivo original (`modl_tabs`) ou foram calculadas por este projeto a partir de históricos e inputs de mercado, quando essas abas não existem."),
    ])
