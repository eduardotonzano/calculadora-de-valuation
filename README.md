# Calculadora de Valuation

Ferramenta para gerar teses de investimento buy-side: DCF com valor
terminal por perpetuidade, WACC por CAPM, e target price por múltiplos de
mercado (EV/EBITDA, P/E, PEG). Os dados de origem são exportações do
template Bloomberg MODL (`.xlsx`), carregadas em um banco SQLite.

O core do projeto é **calcular** DCF/WACC/perpetuidade, não só ler
premissas prontas: a maioria dos exports Bloomberg reais só traz
históricos e consenso de mercado (aba `Multiple Periods`), sem nenhuma
aba de DCF ou WACC pronta — isso é um artefato manual, não algo que o
Bloomberg exporta. Quando o arquivo não tem essas abas, `data/load_from_modl.py`
deriva ele mesmo as premissas de cenário, o WACC e o múltiplo terminal a
partir dos históricos + um punhado de inputs de mercado (preço, beta,
risk-free rate, ERP) — ver "Duas fontes de dados: `modl_tabs` vs.
`derived`" abaixo.

## Status atual

| Componente | Status |
|---|---|
| `data/schema.sql` | Pronto |
| `data/load_from_modl.py` | Pronto — extrai (Modo A) ou calcula (Modo B) DCF/WACC/perpetuidade; lê planilhas com ou sem código de campo Bloomberg, ver seção abaixo |
| `data/valuation.db` | Populado com AppLovin (APP US, Modo A) |
| `dcf_engine.py` | Pronto, output validado célula a célula ($388.54 no caso base da AppLovin) |
| `target_price.py` | Pronto — `from_ev_ebitda()`, `from_pe()`, `from_peg()`, degrada graciosamente quando um método não tem dado suficiente |
| `sensitivity.py` | Pronto — tabelas WACC × múltiplo, crescimento × margem, e risk-free × beta |
| `app.py` (Streamlit) | Pronto — abre todos os cálculos, não só o resultado final; upload calcula tudo, inclusive DCF/WACC quando o arquivo não os traz |
| `tests/test_valuation.py` + CI | Pronto — 31 testes `pytest`, rodando no GitHub Actions a cada push/PR |

Rode para confirmar que tudo está funcionando:

```bash
pip install -r requirements.txt
python dcf_engine.py data/valuation.db "APP US" base
# ... Price per share: $388.54
python target_price.py data/valuation.db "APP US" base

# suite de regressão (31 testes) — cobre o caso base, os limites de
# explicit_years, a lacuna de cobertura do P/E, o isolamento multi-empresa,
# a derivação de premissas do Modo B e a leitura de planilhas sem código
# de campo Bloomberg
pip install -r requirements-dev.txt
pytest
```

## Duas fontes de dados: `modl_tabs` vs. `derived`

Este projeto começou a partir de um arquivo Bloomberg MODL da AppLovin
que tinha abas `DCF` e `WACC` prontas, com premissas de cenário
(Bear/Base/Bull), múltiplo de saída e inputs de CAPM já preenchidos à
mão. Era natural supor que isso fosse o formato normal de um export
Bloomberg — mas essas abas eram trabalho manual feito especificamente
para a AppLovin, não algo que o Bloomberg gera. Testando o upload com um
segundo arquivo real (Blackstone, BX US), ficou claro que o formato
normal de um export bruto do MODL só tem **uma** aba, `Multiple Periods`
(históricos + consenso de mercado) — sem `DCF` nem `WACC`.

Como o core deste projeto é calcular DCF/WACC/perpetuidade de forma
rápida e correta — não só ler premissas que alguém já montou —
`data/load_from_modl.py` detecta qual dos dois formatos o arquivo tem e
segue um de dois caminhos, gravados em `companies.data_source`:

- **`modl_tabs`** (abas `DCF`/`WACC` presentes): extrai as premissas
  manuais verbatim, exatamente como antes. É o caminho pelo qual o
  preço-alvo de $388.54 da AppLovin foi validado célula a célula, e
  continua **sem nenhuma mudança de comportamento**.
- **`derived`** (só `Multiple Periods`, o caso normal): `data/load_from_modl.py`
  calcula tudo sozinho a partir dos históricos:
  - **Crescimento de receita**: usa o consenso de mercado enquanto ele
    existe e depois faz um fade linear até uma taxa terminal de 3% ao
    ano (`fade_to_terminal()`) — uma proxy padrão de crescimento nominal
    de longo prazo.
  - **Margem EBIT, alíquota efetiva, D&A%, CapEx%, NWC%**: média dos
    últimos anos reportados, mantida constante daí para frente.
  - **Bear/Bull**: um envelope mecânico e documentado em cima do caso
    base — crescimento ×0.7/×1.3, margem EBIT −3pp/+2pp — não são
    premissas de um analista, é uma forma transparente de gerar uma
    faixa de sensibilidade quando não existe uma tabela de cenários
    pronta para ler.
  - **Múltiplo terminal (EV/EBITDA)**: o múltiplo EV/EBITDA implícito de
    mercado hoje — `(market cap + dívida líquida) / EBITDA ajustado` do
    último ano reportado — usado como base, com bear/bull em
    0.85x/1.15x desse valor.
  - **WACC**: CAPM padrão (Rf + beta × ERP) para custo de equity; o
    custo de dívida e a estrutura de capital continuam vindo de
    `dcf_engine.get_wacc()`, como no Modo A.

  Os inputs de CAPM (risk-free rate, beta, equity risk premium, preço da
  ação) **nunca** aparecem num export de dados financeiros do Bloomberg
  — são dado de mercado, não dado da empresa — então `load_workbook_data()`
  exige que sejam passados explicitamente via `market_data` e levanta um
  `ValueError` claro se faltar algum, em vez de inventar um valor. Na
  interface Streamlit, o upload de um arquivo sem abas `DCF`/`WACC`
  mostra um formulário pedindo esses quatro números antes de calcular
  qualquer coisa.

  Como não há abas `DCF`/`WACC` de onde extrair o bloco "TRADING
  MULTIPLES", `trading_comps` fica vazio para uma empresa `derived` —
  os métodos `from_pe()`/`from_peg()` de `target_price.py` que dependem
  de múltiplo de P/E de mercado degradam graciosamente (ver
  `football_field()`).

`app.py` lê `companies.data_source` para não descrever o arquivo de uma
empresa `derived` como se tivesse os mesmos problemas do arquivo
manualmente construído da AppLovin (ver "Descoberta importante" e
"Segunda descoberta" abaixo, que são específicas do Modo A).

## Descoberta importante: inconsistência de 5 vs. 13 anos de projeção explícita

*(Específico do Modo A — a aba `DCF` manual da AppLovin. Uma empresa
`derived`, como a Blackstone, não tem essa aba nem esse problema — ver
"Duas fontes de dados" acima.)*

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

## Segunda descoberta: as três tabelas de sensibilidade do arquivo original nem concordam entre si

*(Também específico do Modo A/AppLovin — sem uma aba `DCF` manual não há
tabelas de sensibilidade da própria planilha para comparar.)*

Ao construir `sensitivity.py` reproduzindo as tabelas "SENSITIVITY 1:
WACC vs. TERMINAL EXIT MULTIPLE" (linhas 92–98), "SENSITIVITY 2: REVENUE
GROWTH Δ vs. EBIT MARGIN Δ" (linhas 106–112) e "SENSITIVITY 3: BETA vs.
RISK-FREE RATE" (linhas 120–126) da aba `DCF`, descobri que nenhuma das
três usa a mesma fórmula da célula principal (`C87` = $388.54). E elas
também não usam a mesma fórmula *entre si*. Na célula de "delta zero"
(inputs no valor do caso base) cada uma dá um número diferente:

| Fonte | Fórmula | Preço-alvo (base) |
|---|---|---|
| `C87` (DCF principal) | soma PV do FCF dos anos 1–5; valor terminal = EBITDA do ano 5, descontado no período do ano 5 | **$388.54** |
| Tabela 1 (WACC × Múltiplo), célula `D96` | soma PV do FCF dos **13 anos**; valor terminal ainda usa o EBITDA (desatualizado) **do ano 5**, mas descontado no período do **ano 13** | **$313.31** |
| Tabela 2 (Crescimento × Margem), célula `D110` | soma PV do FCF dos **13 anos**; valor terminal usa o EBITDA **do ano 13**, descontado no período do **ano 13** | **$412.19** |
| Tabela 3 (Risk-Free × Beta), célula `D124` | mesma fórmula da Tabela 1 (13 anos, EBITDA do ano 5, desconto no ano 13), só que sensibiliza Rf/Beta em vez do WACC direto | **$313.31** |

A Tabela 2 é internamente consistente (é a mesma matemática de somar os
13 anos completos com o terminal alinhado ao último ano — o mesmo
resultado que `dcf_engine.run_dcf(..., explicit_years=13)` produz). As
Tabelas 1 e 3 são as mais problemáticas: somam 13 anos de FCF mas ainda
ancoram o EBITDA terminal no ano 5, e descontam esse valor terminal
desatualizado 8 anos além do que deveria — um erro que não é conservador
nem agressivo, é simplesmente inconsistente com qualquer definição
única de "quantos anos de projeção explícita" o modelo usa. Não é
coincidência as duas darem exatamente o mesmo $313.31 no caso base: é a
mesma fórmula por trás, só que uma sensibiliza o WACC diretamente e a
outra sensibiliza os dois inputs (Rf, Beta) que compõem o custo de
equity que entra nesse WACC.

Diante disso, `sensitivity.py` **não** replica nenhuma dessas três
variantes da planilha. Em vez disso, os três grids são construídos
chamando `dcf_engine.run_dcf()` diretamente, perturbando um par de
inputs por vez — então a célula de delta zero de qualquer um dos três
grids sempre bate exatamente com a saída do próprio `run_dcf()` para o
mesmo cenário/`explicit_years` (por padrão, $388.54). Isso garante que
uma tabela de sensibilidade sempre reconcilia com o caso-base que ela
está "sensibilizando" — o que a planilha original, surpreendentemente,
não garante.

Como checagem cruzada: rodando `sensitivity.py` com `--explicit-years 13`,
o grid de Crescimento × Margem bate célula a célula com a Tabela 2 do
arquivo original (ex.: delta -8%/-4% = $204.30, delta +8%/+4% = $865.46),
confirmando que a implementação é equivalente à única das três tabelas
originais que é internamente consistente.

## Estrutura de dados (`data/schema.sql`)

- **companies** — ticker, nome, moeda, data-base ("as of" do Bloomberg), e
  `data_source` (`modl_tabs` ou `derived` — ver "Duas fontes de dados"
  acima; `app.py` usa essa coluna para não descrever o arquivo de uma
  empresa `derived` como se tivesse os problemas do Modo A).
- **historicals** — receita, EBIT, EBITDA ajustado, D&A, despesa de juros,
  lucro antes de impostos, impostos, lucro líquido, EPS diluído ajustado,
  dívida de longo prazo, dívida líquida, capex, variação de capital de
  giro, ações diluídas — um registro por ano fiscal, `period_type` =
  `actual` (reportado) ou `estimate` (consenso Bloomberg). Único dado que
  toda empresa tem, `modl_tabs` ou `derived` — é a partir daqui que o
  Modo B deriva tudo o mais.
- **wacc_inputs** — inputs de CAPM e mercado (risk-free, beta, ERP, preço
  da ação, ações em circulação). No Modo A vêm da aba `WACC` manual; no
  Modo B vêm do `market_data` fornecido no upload/CLI. Custo de dívida e
  estrutura de capital são derivados em `dcf_engine.get_wacc()` a partir
  do último ano reportado em `historicals` nos dois modos, para não
  duplicar dado.
- **scenario_assumptions** — premissas ano a ano (13 anos) para os casos
  `bear`/`base`/`bull`: crescimento de receita, margem EBIT, alíquota de
  imposto, D&A% e CapEx% da receita, impacto de NWC% sobre a variação de
  receita. Extraídas da aba `DCF` manual (Modo A) ou calculadas por
  `derive_scenario_assumptions()` a partir de `historicals` (Modo B).
- **terminal_assumptions** — múltiplo de saída EV/EBITDA por cenário. No
  Modo A é o único input explícito de valor terminal do arquivo original
  (8x/14x/18x para a AppLovin); no Modo B é o múltiplo EV/EBITDA
  implícito de mercado hoje (ver "Duas fontes de dados" acima). Nos dois
  casos, a taxa de crescimento na perpetuidade (Gordon Growth) não é um
  input — é uma taxa implícita que `dcf_engine.py` calcula a partir do
  valor terminal por múltiplo de saída.
- **trading_comps** — múltiplos de mercado atuais (P/E, EV/EBITDA, FCF
  yield, PEG para FY2027E e FY2030E). No Modo A, extraídos diretamente do
  bloco "TRADING MULTIPLES" da aba `DCF` — não são comparáveis de pares
  fabricados, é o que o próprio arquivo já calcula. No Modo B fica vazio
  (sem aba `DCF` não há esse bloco para ler), e `target_price.py` degrada
  os métodos que dependem dele em vez de fabricar um múltiplo.

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

`sensitivity.py` segue o mesmo princípio de reaproveitamento: em vez de
reimplementar a matemática do DCF, ele chama `dcf_engine.run_dcf()` uma
vez por célula da grade, passando `wacc_override`/`exit_multiple_override`
(tabela WACC × múltiplo) ou `growth_delta`/`margin_delta` (tabela
crescimento × margem) — parâmetros adicionados a `run_dcf()` e
`project_financials()` exatamente para esse reuso.

## Como rodar

```bash
pip install -r requirements.txt

# (re)carregar o banco a partir de um .xlsx Bloomberg MODL
# Modo A: arquivo já tem abas DCF/WACC (ex.: AppLovin) — nada mais é necessário.
python data/load_from_modl.py <caminho-para-MODL.xlsx> data/valuation.db

# Modo B: arquivo só tem 'Multiple Periods' (o caso normal, ex.: Blackstone) —
# passe os inputs de CAPM/mercado explicitamente.
python data/load_from_modl.py <caminho-para-MODL.xlsx> data/valuation.db \
    --risk-free-rate 0.0410 --beta 1.45 --erp 0.0450 --stock-price 168.50

# DCF
python dcf_engine.py data/valuation.db "APP US" base
python dcf_engine.py data/valuation.db "APP US" bear --explicit-years 13

# Target price por múltiplos (football field)
python target_price.py data/valuation.db "APP US" base

# Sensibilidade (WACC x múltiplo, crescimento x margem)
python sensitivity.py data/valuation.db "APP US" base
python sensitivity.py data/valuation.db "APP US" base --explicit-years 13

# Interface Streamlit
streamlit run app.py
```

## Interface (`app.py`)

Camada de exibição pura sobre `dcf_engine.py`, `target_price.py` e
`sensitivity.py` — nenhuma conta é refeita na interface, tudo vem dos dicts
que essas três funções já devolvem. Organizada em abas para abrir os
cálculos em vez de só mostrar o resultado final:

- **Sumário** — preço-alvo, WACC, g implícito, football field, e (só para
  empresas `modl_tabs`) os dois achados documentados acima em destaque;
  para uma empresa `derived`, mostra em vez disso como as premissas/WACC/
  múltiplo terminal foram calculados.
- **WACC** — CAPM, custo de dívida e estrutura de capital linha a linha,
  com a fórmula de cada componente ao lado do valor.
- **Projeção & FCF** — a tabela de 13 anos completa (receita, EBIT, NOPAT,
  D&A, CapEx, ΔNWC, UFCF), com os anos realmente somados na avaliação
  destacados em verde e os anos órfãos em vermelho — o achado "5 vs. 13"
  visível na própria tabela, não só em texto — seguida da tabela de
  desconto (período, fator, VP do FCF).
- **Valor Terminal** — EBITDA terminal, os dois métodos (Exit Multiple e
  Gordon Growth) lado a lado, o cross-check de g implícito, e a ponte de
  Enterprise Value até preço por ação.
- **Múltiplos** — `from_ev_ebitda()`, `from_pe()`, `from_peg()`, cada um
  com fórmula + inputs + resultado.
- **Sensibilidade** — os três heatmaps (WACC × múltiplo, crescimento ×
  margem, risk-free × beta) mais a grade em números; para uma empresa
  `modl_tabs`, também a tabela comparando os preços-alvo divergentes que
  a própria planilha original produz em "caso base" (C87/D96/D110/D124 —
  ver "Segunda descoberta" acima); para uma empresa `derived`, uma nota
  explicando que não há arquivo original para comparar contra.
- **Metodologia** — a linhagem dos dados; para `modl_tabs`, os dois
  achados por extenso; para `derived`, como cada premissa/WACC/múltiplo
  terminal é calculado; nos dois casos, a tabela de "onde está cada
  cálculo no código".

Um detalhe de implementação que rendeu um bug real ao construir os
heatmaps: o Plotly, ao receber rótulos de eixo com "%" (ex.: `"9.81%"`),
tenta convertê-los para número e desenha seus próprios ticks arredondados
(`10, 12, 14...`) em vez de usar os rótulos reais — é preciso forçar
`xaxis=dict(type="category")` / `yaxis=dict(type="category")` para o eixo
mostrar os valores verdadeiros da grade.

## Revisão completa do projeto

Depois que DCF, múltiplos, sensibilidade e a interface estavam prontos, o
projeto passou por uma revisão de ponta a ponta — banco de dados, os
quatro módulos Python e a interface — com testes ativos (não só leitura de
código). A revisão achou e corrigiu três bugs reais:

1. **`schema.sql` não era idempotente.** As tabelas eram criadas sem
   `IF NOT EXISTS`; carregar um segundo arquivo (ou reenviar o mesmo) no
   mesmo banco quebrava com "table already exists". Corrigido antes de
   liberar o upload pela interface, que depende exatamente desse caminho.
2. **`dcf_engine.run_dcf()` não validava `explicit_years`.** `0` derrubava
   a função com `IndexError`; valores negativos (ex. `-1`) não davam erro
   nenhum — fatiavam a lista de projeção ao contrário e devolviam um
   preço-alvo plausível, porém errado, sem nenhum aviso. Agora qualquer
   valor fora de 1–13 levanta `ValueError` explicando o intervalo válido.
3. **`app.py` derrubava a página inteira ao selecionar FY2026E, FY2028E ou
   FY2029E como "Ano-alvo".** `trading_comps` só tem múltiplo de P/E de
   mercado para FY2027E e FY2030E (é tudo que a planilha original
   calcula) — `target_price.from_pe()` levanta `ValueError` para os
   outros anos, e a interface não capturava essa exceção. Corrigido com
   degradação graciosa: cada método de múltiplo agora é chamado via um
   helper `safe_result()` que mostra um aviso explicando a lacuna de
   dados em vez de quebrar a página; o football field some só o método
   indisponível e segue mostrando os outros.

Os três casos (mais o isolamento entre empresas num banco multi-empresa,
testado com uma segunda empresa clonada) viraram testes automatizados em
`tests/test_valuation.py` (`pytest`, 23 casos, todos passando) para não
regredir.

## Sugestões implementadas

Todas as lacunas encontradas na revisão que dava para fechar sem dados
externos foram implementadas:

- **Guardas de divisão por zero / base negativa em `get_wacc()` e
  `from_peg()`.** `get_wacc()` agora levanta `ValueError` claro para
  dívida zero (custo de dívida indefinido), lucro antes de impostos zero
  (alíquota efetiva indefinida) e enterprise value zero (pesos de
  capital indefinidos). `from_peg()` valida que o EPS base e o EPS alvo
  sejam positivos antes de elevar a razão entre eles a uma potência
  fracionária — antes disso, um EPS-base negativo (a AppLovin teve EPS
  negativo em 2022) fazia o Python devolver silenciosamente um número
  complexo, que só quebrava mais adiante, longe da causa real, na hora
  de formatar o número. Cobertos por 4 novos testes.
- **CLIs com mensagens de erro limpas.** `dcf_engine.py`,
  `target_price.py` e `sensitivity.py` agora usam um helper compartilhado
  (`cli_utils.friendly_errors()`) que imprime `Error: <mensagem>` e sai
  com código 1 em vez de um traceback cru para ticker/banco inexistente
  ou parâmetros inválidos.
- **CI no GitHub Actions** (`.github/workflows/tests.yml`) rodando a cada
  push/PR: compila todos os módulos, roda os testes de
  `tests/test_valuation.py` (31 no momento, incluindo os do Modo B e os do
  parser sem código de campo Bloomberg), e
  faz um smoke test das três CLIs contra o
  `data/valuation.db` versionado. Os bugs desta revisão só tinham sido
  achados porque testei manualmente depois do fato — agora regressões
  seriam pegas automaticamente.
- **Terceiro heatmap de sensibilidade (Risk-Free Rate × Beta)**
  implementado em `sensitivity.sensitivity_beta_risk_free()` e na aba
  Sensibilidade da interface, seguindo o mesmo princípio das outras duas
  tabelas (recalcula o WACC e chama `dcf_engine.run_dcf()`, então a
  célula central sempre bate com o preço-alvo do DCF). Validado célula a
  célula contra a Tabela 3 original (linhas 120–126 da aba `DCF`) — que,
  como a Tabela 1, também soma 13 anos de FCF mas ancora o EBITDA
  terminal no ano 5, dando o mesmo $313.31 no caso base apesar de
  sensibilizar inputs diferentes.
- **Isolamento por sessão do banco.** A barra lateral agora tem um
  checkbox "Isolar esta sessão" — quando marcado, o upload grava numa
  cópia `<nome>.session_<id>.db` exclusiva daquela sessão de navegador em
  vez do arquivo compartilhado, sem mudar o comportamento padrão (uso
  local de uma pessoa só continua carregando `data/valuation.db`
  diretamente).

## Testado com uma segunda empresa real — e o que isso revelou

Testar o upload com o export Bloomberg MODL de uma segunda empresa real
(Blackstone, BX US) mudou o entendimento do projeto. Minha primeira
reação, quando esse arquivo só trouxe a aba `Multiple Periods` (sem
`DCF` nem `WACC`), foi tratar isso como um arquivo incompleto: o parser
acessava `wb["DCF"]`/`wb["WACC"]` direto sem checar se existiam, o
usuário via um `KeyError` cru do openpyxl na barra lateral, e a correção
inicial foi só levantar um `ValueError` mais claro dizendo que essas
abas faltavam — tratando a ausência delas como erro.

Isso estava invertido. As abas `DCF`/`WACC` do arquivo da AppLovin eram
um artefato manual construído especificamente para aquela tese de
investimento — não algo que o Bloomberg exporta. O arquivo da Blackstone,
só com `Multiple Periods`, é o formato *normal* de um export bruto do
MODL. Ou seja: o core deste projeto nunca foi "ler o DCF/WACC que
alguém já montou" — é **calcular** DCF, WACC e perpetuidade a partir dos
dados brutos, rápido e corretamente, para qualquer empresa que só tenha
históricos e consenso de mercado.

Isso motivou a reescrita descrita em "Duas fontes de dados" acima:
`data/load_from_modl.py` agora trata "só `Multiple Periods`" como o
caminho normal (Modo `derived`), calculando premissas de cenário, WACC e
múltiplo terminal ele mesmo (`derive_scenario_assumptions()`), e mantém
o caminho antigo (Modo `modl_tabs`) intacto para arquivos que já trazem
essas abas prontas — sem regressão no preço-alvo de $388.54 da AppLovin,
confirmado por teste de regressão exato. `data/valuation.db` continua
populado só com a AppLovin; o arquivo da Blackstone não foi versionado
no repositório (dado de terceiros), mas o caminho `derived` que ele
expôs está coberto por testes com uma planilha sintética mínima
(`tests/test_valuation.py`).

De quebra, essa segunda empresa confirmou duas coisas sobre a robustez
do parser:

- **Códigos de campo Bloomberg não são estáveis entre templates.**
  `IS_COMP_PTP_EX_STK_BASED_COMP` significa "lucro antes de impostos"
  para a AppLovin, mas para a Blackstone (uma gestora de ativos
  alternativos) é um conceito ajustado diferente ("Segment Distributable
  Earnings"). `historicals` agora é montado a partir de uma cadeia de
  aliases por conceito, priorizando o código Bloomberg mais universal
  (ex.: `PRETAX_INC`, presente na BX e ausente na AppLovin) — os dois
  arquivos resolvem para o valor certo sem mudança de comportamento para
  a AppLovin.
- **O parser já era robusto a variações estruturais reais** que a
  AppLovin não tinha: a BX só tem 4 anos de consenso (FY2026E–FY2029E)
  contra 5 da AppLovin, e a fronteira "Fwd"/"Rep" cai numa coluna
  diferente (`I` em vez de `J`) — `load_from_modl.py` já lê isso
  dinamicamente pelo rótulo de cada coluna (`period_type_for_column()`),
  não por posição fixa, então essa parte funcionou sem nenhuma mudança.

## Testado com uma terceira empresa real — um export sem nenhuma edição

Um terceiro arquivo real (Alphabet, GOOGL US) — explicitamente descrito
como "puro", sem nenhuma alteração manual — expôs que "sem abas
DCF/WACC" não é a única variação que um export bruto do Bloomberg pode
ter. Esse arquivo:

- Tinha a aba renomeada para `Planilha1` (o padrão do LibreOffice/Excel
  em PT-BR) em vez de `Multiple Periods` — o texto "... (Multiple
  Periods)" só sobrevivia no título da própria planilha (célula `A1`),
  não no nome da aba.
- Não tinha **nenhum** código de campo Bloomberg na coluna B (`IS_COMP_SALES`
  etc.) — a coluna B já era o primeiro ano de dado. Isso é o resultado de
  salvar a tela "Company Financial (Multiple Periods)" do Bloomberg
  diretamente (`Ctrl+Alt+S` ou similar), em vez de exportar via o
  template MODL orientado a código de campo que os outros dois arquivos
  usam — aparentemente dois jeitos diferentes de tirar a mesma tela do
  Bloomberg, e um analista sem saber a diferença não teria como prever
  qual vai sair.
- Os anos de dado começavam na coluna `B`, não na `E`, e cobriam 15 anos
  (FY2021–FY2035) em vez de 10.

Nenhuma dessas três coisas era algo que `load_from_modl.py` conseguia
lidar antes — o loader simplesmente não encontrava a aba `Multiple
Periods` e falhava de cara. As correções, todas em `data/load_from_modl.py`:

- **`find_multiple_periods_sheet()`** substitui a checagem por nome de
  aba: percorre todas as abas do workbook e usa a primeira cujo título
  (`A1`) contenha "(Multiple Periods)", com fallback para uma aba
  literalmente chamada `Multiple Periods`.
- **`find_year_columns()`** substitui a lista fixa de colunas E–N:
  detecta as colunas de ano lendo o rótulo de cada uma na linha de
  período (`"2026 A (Fwd)"` etc.), então funciona igual para 10 anos
  começando em `E` ou 15 anos começando em `B`.
- **`load_historicals_by_label()`** é o caminho novo para quando não há
  nenhum código de campo Bloomberg na planilha: cada conceito (receita,
  EBIT, EBITDA ajustado, ...) é resolvido pelo rótulo em texto da coluna
  A — mas rótulos se repetem na mesma planilha (ex.: "Net Income"
  aparece na demonstração de resultado, nos resultados ajustados e na
  demonstração de fluxo de caixa; "Revenue" aparece de novo em cada
  segmento de negócio). A resolução usa três coisas ao mesmo tempo:
  **em que seção** ("Income Statement", "Condensed Balance Sheet",
  "Condensed Cash Flow Statement" — cabeçalhos padrão do Bloomberg para
  essa tela), **qual rótulo exato**, e **em que nível de indentação**
  (a planilha indenta 2 espaços por nível de aninhamento) — as três
  coisas juntas desambiguam qualquer rótulo repetido.
- Achado no meio do caminho: o EPS diluído ajustado do consenso da
  Alphabet vinha como string vazia (`''`) em alguns anos distantes
  (2032–2034) em vez de célula em branco — gravar isso direto numa
  coluna `REAL` do SQLite funcionaria sem erro (SQLite não impõe tipo de
  coluna) e só quebraria bem mais tarde, numa conta em `target_price.py`
  longe da causa real. `_numeric_or_none()` normaliza qualquer string
  vazia/só-espaço para `None` no ponto em que o valor é lido da célula,
  para os dois formatos (com e sem código de campo).
- Achado mais sério: a Alphabet é uma empresa de caixa líquido positivo
  (mais caixa do que dívida) — a despesa de juros *líquida* (receita de
  juros do caixa menos despesa de juros da dívida) é **negativa**. O
  formato antigo usa essa métrica líquida (`IS_NET_INTEREST_EXPENSE`)
  para o custo de dívida em `dcf_engine.get_wacc()`
  (`Kd = despesa de juros / dívida total`); com o valor líquido negativo,
  isso vira um custo de dívida negativo, algo que nenhum WACC deveria
  ter. `load_historicals_by_label()` resolve `interest_expense` para a
  despesa de juros **bruta** (a linha "Interest Expense" dentro de
  "Interest Expense/(Income), Net", não o líquido) — o custo de uma
  empresa financiar sua própria dívida não deveria depender de quanta
  receita de juros o caixa dela rende.

Confirmado por regressão: o preço-alvo de $388.54 da AppLovin (Modo
`modl_tabs`, caminho por código de campo) e o resultado da Blackstone
(Modo `derived`, caminho por código de campo) continuam idênticos depois
dessa mudança — o caminho por rótulo só entra em ação quando nenhum
código de campo é encontrado na planilha. O arquivo da Alphabet não foi
versionado no repositório (dado de terceiros); o caminho por rótulo está
coberto por testes com uma planilha sintética mínima
(`tests/test_valuation.py`), incluindo um teste específico para a
desambiguação por profundidade (dois rótulos "Net Income" idênticos em
níveis de indentação diferentes, só um deles correto) e um para o
fallback de "Changes in Working Capital" quando essa linha vem em branco
mas as linhas-filhas por baixo dela têm valor.

## `interest_expense` virou opcional (mais um código de campo variável)

Um quarto arquivo real quebrou o upload com `Could not find any known
Bloomberg field code for ['interest_expense']` — o código de campo dessa
planilha para despesa de juros não batia com nenhum dos três alias
conhecidos. Igual ao achado da Blackstone/Alphabet, o código de campo para
esse conceito varia mais entre templates do que qualquer outro. Em vez de
seguir caçando alias por alias a cada novo arquivo real, `interest_expense`
virou **opcional**: a lista de aliases foi ampliada, e quando nenhum bate,
o valor grava como `0.0` em vez de derrubar o upload inteiro — o custo de
dívida de `dcf_engine.get_wacc()` fica 0% para essa empresa nesse caso
(melhor um número explicitamente conservador e sinalizado do que travar a
ferramenta inteira por um campo que a maioria dos templates chama de um
jeito diferente).

## Preenchimento automático de dados de mercado (yfinance)

Para uma empresa sem abas DCF/WACC (Modo `derived`), a barra lateral agora
tem um campo de ticker — escolha um nome da lista curada (S&P 500 e outros
grandes nomes de NYSE/NASDAQ, mais Ibovespa, em `data/tickers.csv`) ou
digite qualquer ticker real — e um botão que busca preço atual e beta no
Yahoo Finance (`yfinance`) e a taxa do Treasury de 10 anos (`^TNX`),
preenchendo os 4 campos de mercado sozinho. O Equity Risk Premium continua
um valor estático documentado (não existe uma API gratuita de ERP ao
vivo — é uma estimativa periódica da pesquisa do Damodaran, não uma
cotação de mercado). Quando beta ou o Treasury não vêm no retorno do
Yahoo, o app avisa exatamente qual campo ficou com um valor padrão em vez
de preencher silenciosamente. Preenchimento manual continua funcionando
igual, para quem não quer depender de uma API externa.

Importante: `data/tickers.csv` é uma lista curada de ~90 tickers
conhecidos, não um cadastro completo de todas as empresas listadas em
NYSE/NASDAQ/Ibovespa — um diretório completo (milhares de tickers) exigiria
acesso a fontes como o `nasdaqtrader.com` ou a B3, que não são alcançáveis
do ambiente onde este projeto é desenvolvido. Isso não limita a busca por
preço/beta em si: qualquer ticker real digitado funciona via `yfinance`,
esteja ele na lista curada ou não — a lista é só um atalho de autocomplete
para os nomes mais comuns.

## Rastreabilidade de origem dos inputs de mercado

A aba WACC mostrava "input" na coluna Fórmula para Risk-free rate, Beta,
ERP e Preço da ação — verdade, mas inútil: não dizia se o número veio da
aba WACC do arquivo original, de uma busca automática via `yfinance`, ou
de digitação manual no upload. Esses quatro campos agora carregam uma
`source` própria (coluna nova em `wacc_inputs`, populada em
`load_wacc_inputs()` para o Modo A e a partir do dicionário `market_data`
para o Modo B), com um texto específico para cada caminho: a célula exata
do Excel no Modo A ("Aba WACC do arquivo original (células B5, B6, B7,
B18)"), o ticker e a API no caso de busca automática ("Yahoo Finance
(yfinance), ticker X — preço/beta ao vivo, 10Y Treasury via ^TNX, ERP fixo
(Damodaran)"), ou "Informado manualmente no upload" quando o usuário
digitou os valores à mão. A aba WACC exibe esse texto no lugar do "input"
genérico.

## Preço-alvo: horizonte explícito e valor presente comparável ao DCF

Os métodos de múltiplo (EV/EBITDA, P/E, PEG) projetam um preço para um ano
futuro específico (`target_year`, por padrão FY2027E) sem desconto — é
assim que um relatório de research realmente publica um preço-alvo (ex.:
o alvo de US$350 da Morgan Stanley para a Vertiv é para um horizonte de
12-18 meses, não um valor de hoje). O problema: colocar esse número lado a
lado com o preço do DCF — que já é um valor presente — sem dizer o
horizonte de cada um é enganoso, e fazia os preços-alvo de múltiplo
parecerem sistematicamente mais otimistas do que realmente são.

Cada resultado de `target_price.py` agora carrega três campos novos:
`years_out` (anos entre o último ano real reportado e `target_year`),
`present_value_target_price` (o preço-alvo trazido a valor presente pelo
WACC da própria empresa: `target_price / (1 + wacc) ** years_out`) e
`present_value_implied_upside` (o upside implícito calculado sobre esse
valor presente, não sobre o número nominal futuro). `target_price` e
`target_year` continuam existindo sem alteração — são o número que um
research realmente imprimiria.

O gráfico football field na aba Sumário agora compara `present_value_target_price`
entre os quatro métodos (base genuinamente comparável, já que o preço do
DCF é `years_out = 0` por construção), e a tabela de apoio abaixo mostra
lado a lado o preço-alvo nominal, o horizonte (`FY{ano} (+N anos)`) e o
valor presente. A aba Múltiplos detalha o cálculo completo de cada
método, incluindo a linha final de desconto a valor presente. No exemplo
da AppLovin (caso base, FY2027E, 2 anos de horizonte): o PEG nominal de
$913.87 (inflado pela heurística PEG=1,0x aplicada a um CAGR de EPS de
consenso de ~45%, já documentado como limitação conhecida) cai para
$705.52 em valor presente — ainda o mais otimista dos quatro, mas na
mesma base do DCF ($388.54) em vez de comparado com um número de 2 anos
no futuro sem desconto.

## Glossário

Nova aba "Glossário" no app reúne, em uma frase cada, todo termo técnico
usado nas outras abas — WACC, CAPM, Ke/Kd, beta, ERP, EBIT/EBITDA/NOPAT,
D&A, CapEx, NWC, UFCF, mid-year convention, valor terminal (múltiplo de
saída vs. Gordon Growth), g implícito, EV/Equity Value, P/E, PEG, trading
comps, ano-alvo, valor presente do preço-alvo, football field, grid de
sensibilidade e a distinção `modl_tabs` vs. `derived`. Agrupado na mesma
ordem em que os conceitos aparecem no app (WACC → projeção/FCF → valor
terminal → preço-alvo/múltiplos → sensibilidade), para servir de
referência rápida sem precisar sair do app.

## Anos de projeção explícita: 1/2/5/10 em vez de 5/13, e preço-alvo de 12–18 meses

O controle "Anos de projeção explícita (DCF)" na barra lateral tinha só
duas opções, 5 e 13, escolhidas para reproduzir as duas leituras que o
próprio arquivo Bloomberg faz da sua avaliação principal (ver "Descoberta
importante" acima). Isso deixava sem opção qualquer horizonte
intermediário. O controle agora oferece **1, 2, 5 e 10** anos (5 continua
sendo o padrão, o que reproduz a avaliação original de $388.54 sem
alteração) — `dcf_engine.run_dcf()` já aceitava qualquer valor entre 1 e
13, então a mudança foi só na UI.

A aba Sumário também ganhou dois cartões novos: **Preço-alvo (12 meses)**
e **Preço-alvo (18 meses)**. O preço-alvo do DCF é um valor presente "de
hoje" — para expressar um alvo no formato de horizonte que relatórios de
research realmente usam (ex.: o alvo de 12-18 meses da Morgan Stanley
para a Vertiv), ele é projetado para frente pela própria taxa de desconto
(WACC): `preço-alvo(n meses) = preço-alvo (DCF) × (1 + WACC) ^ (n/12)`.
Não é uma nova projeção de fluxo de caixa — é o mesmo valor justo do DCF,
só que na data futura em vez de hoje (a lógica-padrão usada por analistas
para converter um valor justo de longo prazo em um alvo de curto prazo).

## Busca de preço/beta no Yahoo Finance mais resiliente, e confirmação do que foi carregado

Dois problemas relatados no formulário de dados de mercado (Modo B):

- **"Buscar preço, beta e 10Y no Yahoo Finance" falhando** com "ticker não
  encontrado ou sem preço disponível" mesmo para tickers reais e líquidos
  (ex.: AAPL). Isso é um problema conhecido do endpoint `.info` do
  `yfinance`/Yahoo Finance — ele intermitentemente volta vazio ou sem os
  campos de preço quando o Yahoo aplica rate-limit ou exige um crumb/cookie
  que a sessão do `yfinance` não conseguiu obter (não é um problema do
  ticker em si). `fetch_market_data()` agora tenta, em cascata, três
  fontes para o preço antes de desistir: `.info` (também a única fonte de
  beta), depois `.fast_info`, depois o preço de fechamento de `.history()`
  — a mesma técnica que `_fetch_risk_free_rate()` já usava para o Treasury.
- **Suspeita de que o Beta digitado manualmente não estava sendo usado no
  cálculo do WACC.** Testado extensivamente (upload novo, re-upload
  sobrescrevendo uma empresa existente, com e sem uma tentativa de busca
  via Yahoo falhando antes) — em todos os casos o valor de Beta digitado
  chegou corretamente até a tabela de WACC. Não foi possível reproduzir
  uma falha real no fluxo de dados. Para tornar qualquer discrepância
  futura imediatamente visível (e cobrir a hipótese mais provável — um
  envio anterior ter falhado silenciosamente, deixando dados antigos na
  tela), o app agora mostra, na página principal e não só na barra
  lateral, uma confirmação com os 4 valores exatos (preço, beta,
  risk-free, ERP) que foram de fato gravados a cada envio do formulário —
  ou o erro exato, se o envio falhar.
