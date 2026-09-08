# Calculadora de Valuation

Ferramenta para gerar teses de investimento buy-side: DCF com valor
terminal por perpetuidade, e target price por múltiplos de mercado
(EV/EBITDA, P/E, PEG). Os dados de origem são exportações do template
Bloomberg MODL (`.xlsx`), carregadas em um banco SQLite.

## Status atual

| Componente | Status |
|---|---|
| `data/schema.sql` | Pronto |
| `data/load_from_modl.py` | Pronto, validado contra o `.xlsx` original da AppLovin |
| `data/valuation.db` | Populado com AppLovin (APP US) |
| `dcf_engine.py` | Pronto, output validado célula a célula ($388.54 no caso base) |
| `target_price.py` | Pronto — `from_ev_ebitda()`, `from_pe()`, `from_peg()` |
| `sensitivity.py` | Não iniciado |
| Interface Streamlit | Não iniciada |

Rode para confirmar que tudo está funcionando:

```bash
pip install -r requirements.txt
python dcf_engine.py data/valuation.db "APP US" base
# ... Price per share: $388.54
python target_price.py data/valuation.db "APP US" base
```

## Descoberta importante: inconsistência de 5 vs. 13 anos de projeção explícita

O template Bloomberg MODL original (aba `DCF`) monta uma projeção
explícita de **13 anos** (FY2026E–FY2038E): os primeiros 5 anos
(FY2026–FY2030) vêm de estimativas de consenso Bloomberg (aba
`Multiple Periods`), e os 8 anos seguintes usam premissas manualmente
assumidas (crescimento de receita decrescendo de 12% para 3%, margem
EBIT/D&A%/CapEx%/NWC% como médias móveis das premissas anteriores). O
FCF não alavancado é de fato calculado para os 13 anos (linha "Unlevered
Free Cash Flow", colunas C:O da aba `DCF`).

Só que a soma que entra na avaliação (`Sum of PV of FCFs`, célula `C78`)
usa a fórmula `=SUM(C75:G75)` — **soma apenas os primeiros 5 anos**
(FY2026–FY2030, colunas C:G) — e o valor terminal é calculado em cima do
EBITDA do ano 5 (`=G59+G64`, FY2030), descontado no fator do ano 5
(período 4.5 na convenção mid-year). O FCF descontado dos anos 6 a 13
(colunas H:O, linha 75) é calculado na planilha mas **nunca entra na
conta** — 8 anos de fluxo de caixa projetado ficam órfãos.

Isso não é um bug de arredondamento: o impacto no preço-alvo é material.
No caso base, usando os 5 anos (comportamento original do arquivo):

```
python dcf_engine.py data/valuation.db "APP US" base --explicit-years 5
# Price per share: $388.54
```

Usando os 13 anos completos que a própria planilha projeta:

```
python dcf_engine.py data/valuation.db "APP US" base --explicit-years 13
# Price per share: $412.19  (~6% acima)
```

`dcf_engine.py` reproduz o comportamento original (`explicit_years=5`
por padrão) porque é isso que o preço-alvo de $388.54 — validado célula a
célula contra o arquivo — representa. O parâmetro `--explicit-years`
existe para permitir rodar a versão "corrigida" (13 anos) lado a lado,
mas a decisão de qual usar na tese de investimento é do analista: uma
soma de 5 anos é mais conservadora/defensável quando a confiança nas
premissas cai depois do horizonte de consenso, enquanto 13 anos captura
o fade completo que a própria planilha já modela. O importante é não
alegar "13 anos de projeção explícita" numa tese enquanto o número
usado na prática só reflete 5.

## Estrutura de dados (`data/schema.sql`)

- **companies** — ticker, nome, moeda, data-base ("as of" do Bloomberg).
- **historicals** — receita, EBIT, EBITDA ajustado, D&A, despesa de juros,
  lucro antes de impostos, impostos, lucro líquido, EPS diluído ajustado,
  dívida de longo prazo, dívida líquida, capex, variação de capital de
  giro — um registro por ano fiscal, `period_type` = `actual` (reportado)
  ou `estimate` (consenso Bloomberg, disponível só para FY2026E–FY2030E).
- **wacc_inputs** — inputs de CAPM e mercado (risk-free, beta, ERP, preço
  da ação, ações em circulação). Custo de dívida e estrutura de capital
  são derivados em `dcf_engine.get_wacc()` a partir do último ano
  reportado em `historicals`, para não duplicar dado.
- **scenario_assumptions** — premissas ano a ano (13 anos) para os casos
  `bear`/`base`/`bull`: crescimento de receita, margem EBIT, alíquota de
  imposto, D&A% e CapEx% da receita, impacto de NWC% sobre a variação de
  receita.
- **terminal_assumptions** — múltiplo de saída EV/EBITDA por cenário
  (8x/14x/18x). É o único input explícito de valor terminal no arquivo
  original; a taxa de crescimento na perpetuidade (Gordon Growth) não é
  um input — é uma taxa implícita que `dcf_engine.py` calcula a partir do
  valor terminal por múltiplo de saída (cross-check "TERMINAL VALUE
  CROSS-CHECK" do arquivo original).
- **trading_comps** — múltiplos de mercado atuais (P/E, EV/EBITDA, FCF
  yield, PEG para FY2027E e FY2030E), extraídos diretamente do bloco
  "TRADING MULTIPLES" da aba `DCF`. Não são comparáveis de pares
  fabricados — é o que o arquivo original já calcula sobre a própria
  AppLovin, usado como referência de múltiplo de mercado.

## Padrão de código

Cada motor de valuation segue o mesmo formato:

- Uma função por método (`run_dcf()`; `from_ev_ebitda()`, `from_pe()`,
  `from_peg()`).
- Cada função recebe uma conexão sqlite3 + ticker (+ parâmetros do
  método) e devolve um `dict` com **todos os inputs usados** e o
  **preço-alvo resultante**, para poder comparar métodos lado a lado
  (`target_price.football_field()` já faz isso, juntando DCF + os três
  múltiplos num único resultado).
- Sem estado global, sem classes — funções puras sobre dados lidos do
  banco. CLI fina em cada arquivo (`if __name__ == "__main__"`) só para
  rodar/depurar rápido.

`target_price.py` reaproveita `dcf_engine.project_financials()` para o
método EV/EBITDA (por isso ele varia por cenário bear/base/bull). P/E e
PEG usam EPS de consenso Bloomberg diretamente de `historicals`
(`eps_diluted_adjusted`), porque o arquivo original não tem premissas de
EPS por cenário — só existem para FY2026E–FY2030E, então esses dois
métodos não variam por cenário (o CLI sinaliza isso explicitamente).

O método PEG usa a heurística padrão PEG-alvo = 1,0x ("valor justo"). Em
nomes de altíssimo crescimento como AppLovin (CAGR de EPS de consenso
~45% entre FY2025A e FY2027E), essa heurística produz um P/E implícito
extremo (~45x) e, por consequência, um preço-alvo bem acima dos demais
métodos — isso é uma limitação conhecida da regra PEG=1 em hypergrowth,
não um bug. `target_peg` é parametrizável para testar outros patamares.

## Como rodar

```bash
pip install -r requirements.txt

# (re)carregar o banco a partir de um .xlsx Bloomberg MODL
python data/load_from_modl.py <caminho-para-MODL.xlsx> data/valuation.db

# DCF
python dcf_engine.py data/valuation.db "APP US" base
python dcf_engine.py data/valuation.db "APP US" bear --explicit-years 13

# Target price por múltiplos (football field)
python target_price.py data/valuation.db "APP US" base
```

## Próximos passos

1. `sensitivity.py` — tabelas de sensibilidade WACC × múltiplo de saída e
   crescimento de receita × margem EBIT (o arquivo original já tem essas
   duas tabelas prontas na aba `DCF`, linhas 92–112, que servem de
   referência para validar a implementação).
2. Interface Streamlit por cima de `dcf_engine.py` + `target_price.py` +
   `sensitivity.py`, com seletor de cenário e o football field visual.
