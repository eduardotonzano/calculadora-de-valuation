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
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from data.load_from_modl import load_workbook_data, write_to_db
from dcf_engine import get_company_id, get_wacc, run_dcf
from sensitivity import sensitivity_growth_margin, sensitivity_wacc_exit_multiple
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
h1, h2, h3 { font-family: Georgia, "Times New Roman", serif; letter-spacing: -0.01em; }
[data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; }
.formula-box {
    background: #f7f6f3; border-left: 3px solid #8a8578; padding: 0.6rem 1rem;
    font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 0.92rem;
    margin: 0.4rem 0 0.9rem 0; white-space: pre-wrap;
}
.note {
    border-left: 3px solid #b3543a; background: #fbf3ef; padding: 0.6rem 1rem;
    margin: 0.6rem 0; font-size: 0.92rem;
}
.source-tag {
    font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 0.78rem;
    color: #6b6558; margin-top: -0.4rem;
}
table td, table th { font-variant-numeric: tabular-nums; }
</style>
"""


@st.cache_resource
def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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


def format_axis(value: float, fmt: str) -> str:
    return f"{value:.2%}" if fmt == "pct" else f"{value:.4g}x"


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


def grid_dataframe(table: dict) -> pd.DataFrame:
    row_labels = [format_axis(v, table["row_format"]) for v in table["row_axis"]]
    col_labels = [format_axis(v, table["col_format"]) for v in table["col_axis"]]
    return pd.DataFrame(table["grid"], index=row_labels, columns=col_labels)


st.markdown(CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------- sidebar --

st.sidebar.title("Calculadora de Valuation")

with st.sidebar.expander("Banco de dados", expanded=False):
    db_path = st.text_input("Caminho do .db", value=DEFAULT_DB_PATH)
    st.caption("Onde o arquivo enviado abaixo é gravado/lido. O schema é multi-empresa: cada envio soma ao banco, não substitui.")

st.sidebar.subheader("Enviar planilha Bloomberg MODL")
uploaded_file = st.sidebar.file_uploader(
    "Arquivo .xlsx (abas Multiple Periods, DCF, WACC)", type=["xlsx"],
)

if uploaded_file is not None:
    file_signature = hashlib.md5(uploaded_file.getvalue()).hexdigest()
    if st.session_state.get("last_upload_signature") != file_signature:
        try:
            data = load_workbook_data(uploaded_file)
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

st.title(f"{company['name']} ({company['ticker']})")
st.caption(
    f"As of {company['as_of_date']} · valores em {company['currency']} {company['units']} · "
    f"cenário: {scenario} · fonte: {db_path}"
)

tab_summary, tab_wacc, tab_dcf, tab_terminal, tab_multiples, tab_sens, tab_method = st.tabs(
    ["Sumário", "WACC", "Projeção & FCF", "Valor Terminal", "Múltiplos", "Sensibilidade", "Metodologia"]
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

    results = [
        {"method": "DCF", "target_price": dcf_result["price_per_share"]},
        from_ev_ebitda(conn, ticker, scenario=scenario, target_year=target_year),
        from_pe(conn, ticker, target_year=target_year),
        from_peg(conn, ticker, target_year=target_year),
    ]
    ff_df = pd.DataFrame(
        [{"Método": r["method"], "Preço-alvo": r["target_price"]} for r in results]
    ).sort_values("Preço-alvo")

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=ff_df["Preço-alvo"], y=ff_df["Método"], orientation="h",
        text=[f"${v:,.2f}" for v in ff_df["Preço-alvo"]], textposition="outside",
        marker_color="#4C78A8",
    ))
    fig.add_vline(
        x=current_price, line_dash="dash", line_color="gray",
        annotation_text=f"Preço atual (${current_price:,.2f})", annotation_position="top",
    )
    fig.update_layout(
        xaxis_title="Preço por ação ($)", yaxis_title=None, height=320,
        margin=dict(l=10, r=10, t=30, b=10),
    )
    st.plotly_chart(fig, use_container_width=True)

    upside_df = ff_df.copy()
    upside_df["Upside/(Downside)"] = upside_df["Preço-alvo"] / current_price - 1
    st.table(
        upside_df.set_index("Método").style.format({"Preço-alvo": "${:,.2f}", "Upside/(Downside)": "{:+.1%}"})
    )
    st.caption("Fórmulas e inputs completos de cada método de múltiplo: aba *Múltiplos*.")

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
        "<b>As duas tabelas de sensibilidade da planilha não concordam entre si nem com "
        "a célula principal.</b> Ver aba Sensibilidade e Metodologia para a tabela "
        "completa de comparação."
    )

# =================================================================== WACC ===
with tab_wacc:
    st.markdown("### Custo de capital próprio (CAPM)")
    formula("Ke = Rf + β × ERP")
    line_items_table([
        ("Risk-free rate (10Y Treasury)", "input", f"{wacc_data['risk_free_rate']:.2%}"),
        ("Beta (5Y mensal)", "input", f"{wacc_data['beta']:.2f}"),
        ("Equity Risk Premium", "input", f"{wacc_data['equity_risk_premium']:.2%}"),
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
        ("Preço da ação", "input", f"${wacc_data['stock_price']:,.2f}"),
        ("Ações em circulação (diluídas)", "input", f"{wacc_data['shares_outstanding']:,.2f}M"),
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
    st.markdown(f"Ano-alvo selecionado: **FY{target_year}E**. Métodos P/E e PEG usam EPS de "
                f"consenso Bloomberg e por isso não variam por cenário bear/base/bull "
                f"(o arquivo original não tem premissas de EPS por cenário).")

    ev_ebitda_result = from_ev_ebitda(conn, ticker, scenario=scenario, target_year=target_year)
    st.markdown("### EV/EBITDA")
    formula(
        "EV = EBITDA projetado(ano-alvo) × Múltiplo EV/EBITDA\n"
        "Equity Value = EV − Dívida Líquida\n"
        "Preço-alvo = Equity Value / Ações em circulação"
    )
    line_items_table([
        (f"EBITDA projetado (FY{target_year}E, cenário {scenario})", "dcf_engine.project_financials()", f"${ev_ebitda_result['ebitda']:,.1f}M"),
        ("Múltiplo EV/EBITDA usado", ev_ebitda_result["multiple_source"], f"{ev_ebitda_result['ev_ebitda_multiple']:.1f}x"),
        ("Enterprise Value", f"{ev_ebitda_result['ebitda']:,.1f} × {ev_ebitda_result['ev_ebitda_multiple']:.1f}", f"${ev_ebitda_result['enterprise_value']:,.1f}M"),
        ("(−) Dívida Líquida", "", f"−${ev_ebitda_result['net_debt']:,.1f}M"),
        ("Equity Value", "", f"${ev_ebitda_result['equity_value']:,.1f}M"),
        ("Preço-alvo", "Equity Value / Ações", f"${ev_ebitda_result['target_price']:,.2f}"),
    ])
    source_tag("target_price.from_ev_ebitda()")

    pe_result = from_pe(conn, ticker, target_year=target_year)
    st.markdown("### P/E")
    formula("Preço-alvo = Múltiplo P/E × EPS diluído consenso (ano-alvo)")
    line_items_table([
        (f"EPS diluído ajustado (FY{target_year}E)", "historicals (consenso Bloomberg)", f"${pe_result['eps']:,.2f}"),
        ("Múltiplo P/E usado", pe_result["multiple_source"], f"{pe_result['pe_multiple']:.2f}x"),
        ("Preço-alvo", f"{pe_result['pe_multiple']:.2f} × {pe_result['eps']:,.2f}", f"${pe_result['target_price']:,.2f}"),
    ])
    source_tag("target_price.from_pe() — múltiplo default = trading_comps (P/E de mercado atual para o ano)")

    peg_result = from_peg(conn, ticker, target_year=target_year)
    st.markdown("### PEG")
    formula(
        "CAGR do EPS = (EPS_alvo / EPS_base) ^ (1/anos) − 1\n"
        "P/E implícito = PEG-alvo × (CAGR do EPS × 100)\n"
        "Preço-alvo = P/E implícito × EPS_alvo"
    )
    line_items_table([
        (f"EPS base (FY{peg_result['base_year']}A)", "historicals", f"${peg_result['eps_base']:,.2f}"),
        (f"EPS alvo (FY{peg_result['target_year']}E)", "historicals", f"${peg_result['eps_target']:,.2f}"),
        ("CAGR do EPS", f"({peg_result['eps_target']:,.2f}/{peg_result['eps_base']:,.2f})^(1/{peg_result['target_year']-peg_result['base_year']}) − 1", f"{peg_result['eps_cagr']:.2%}"),
        ("PEG-alvo", "heurística 'valor justo'", f"{peg_result['target_peg']:.2f}x"),
        ("P/E implícito", f"{peg_result['target_peg']:.2f} × {peg_result['eps_cagr']*100:.2f}", f"{peg_result['implied_pe']:.2f}x"),
        ("Preço-alvo", f"{peg_result['implied_pe']:.2f} × {peg_result['eps_target']:,.2f}", f"${peg_result['target_price']:,.2f}"),
    ])
    note(
        "Em nomes de altíssimo crescimento como a AppLovin (CAGR de EPS de consenso "
        "~45% entre FY2025A e FY2027E), a heurística PEG-alvo = 1,0x produz um P/E "
        "implícito extremo e, por consequência, um preço-alvo bem acima dos demais "
        "métodos. Não é um bug — é uma limitação conhecida da regra PEG=1 aplicada "
        "fora do regime de crescimento moderado onde ela foi pensada."
    )
    source_tag("target_price.from_peg()")

# ============================================================= Sensibilidade ===
with tab_sens:
    st.markdown(
        "Cada célula abaixo é uma chamada independente a `dcf_engine.run_dcf()` "
        "com WACC/múltiplo (ou crescimento/margem) deslocados — por isso a célula "
        "central de cada grid sempre bate com o preço-alvo do DCF na aba Sumário."
    )

    wacc_multiple = sensitivity_wacc_exit_multiple(conn, ticker, scenario, explicit_years)
    growth_margin = sensitivity_growth_margin(conn, ticker, scenario, explicit_years)

    col1, col2 = st.columns(2)
    with col1:
        st.plotly_chart(heatmap(wacc_multiple, "WACC × Múltiplo de Saída"), use_container_width=True)
        with st.expander("Ver grid em números"):
            st.dataframe(grid_dataframe(wacc_multiple).style.format("${:,.2f}"), use_container_width=True)
    with col2:
        st.plotly_chart(heatmap(growth_margin, "Crescimento × Margem EBIT (Δ)"), use_container_width=True)
        with st.expander("Ver grid em números"):
            st.dataframe(grid_dataframe(growth_margin).style.format("${:,.2f}"), use_container_width=True)

    st.markdown("### O arquivo original não reconcilia essas duas tabelas com a própria célula principal")
    st.markdown(
        "Ao validar `sensitivity.py` célula a célula contra o arquivo Bloomberg, as "
        "três referências de \"preço-alvo no caso base\" da própria planilha "
        "divergem entre si:"
    )
    mismatch_df = pd.DataFrame(
        SOURCE_FILE_BASE_CASE_CELLS, columns=["Célula / fonte", "Fórmula usada pela planilha", "Preço-alvo ($)"]
    ).set_index("Célula / fonte")
    st.table(mismatch_df.style.format({"Preço-alvo ($)": "${:,.2f}"}))
    note(
        "A Tabela 2 (Cresc. × Margem) é a única internamente consistente — soma os 13 "
        "anos completos com o valor terminal alinhado ao último ano. É por isso que, "
        "com \u201cAnos de projeção explícita\u201d = 13 na barra lateral, o grid de "
        "Crescimento × Margem acima reproduz D110 = $412.19 célula a célula. A Tabela "
        "1 (WACC × Múltiplo) soma 13 anos de FCF mas ainda âncora o EBITDA terminal no "
        "ano 5, descontando esse valor obsoleto 8 anos além do que deveria — nem "
        "conservadora nem agressiva, apenas inconsistente."
    )

# =============================================================== Metodologia ===
with tab_method:
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

### Achado 2 — as duas tabelas de sensibilidade da planilha usam fórmulas diferentes entre si

Ver aba Sensibilidade para a tabela de comparação completa. Em resumo: a
célula principal (`C87`), a Tabela 1 de sensibilidade (`D96`) e a Tabela 2 de
sensibilidade (`D110`) dão três números diferentes no mesmo cenário base —
$388.54, $313.31 e $412.19, respectivamente — porque cada uma usa uma
combinação distinta de "quantos anos somar" e "em que ano ancorar o valor
terminal". `sensitivity.py` não reproduz nenhuma dessas inconsistências: ele
sempre chama `dcf_engine.run_dcf()`, então qualquer célula de qualquer grid
aqui é diretamente comparável ao preço-alvo do DCF mostrado na aba Sumário.

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

Leitura completa dos dois achados, com as fórmulas originais do Excel
citadas célula a célula, está em `README.md` no repositório.
        """
    )
