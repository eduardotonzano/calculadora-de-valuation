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
| `sensitivity.py` | Pronto — tabelas WACC × múltiplo, crescimento × margem, e risk-free × beta |
| `app.py` (Streamlit) | Pronto — abre todos os cálculos, não só o resultado final |
| `tests/test_valuation.py` + CI | Pronto — 23 testes `pytest`, rodando no GitHub Actions a cada push/PR |

Rode para confirmar que tudo está funcionando:

```bash
pip install -r requirements.txt
python dcf_engine.py data/valuation.db "APP US" base
# ... Price per share: $388.54
python target_price.py data/valuation.db "APP US" base

# suite de regressão (23 testes) — cobre o caso base, os limites de
# explicit_years, a lacuna de cobertura do P/E e o isolamento multi-empresa
pip install -r requirements-dev.txt
pytest
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

## Segunda descoberta: as três tabelas de sensibilidade do arquivo original nem concordam entre si

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
python data/load_from_modl.py <caminho-para-MODL.xlsx> data/valuation.db

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

- **Sumário** — preço-alvo, WACC, g implícito, football field, e os dois
  achados documentados acima em destaque.
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
  margem, risk-free × beta) mais a grade em números, e a tabela comparando
  os preços-alvo divergentes que a própria planilha original produz em
  "caso base" (C87/D96/D110/D124 — ver "Segunda descoberta" acima).
- **Metodologia** — a linhagem dos dados e os dois achados por extenso,
  com a tabela de "onde está cada cálculo no código".

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
  push/PR: compila todos os módulos, roda os 23 testes de
  `tests/test_valuation.py`, e faz um smoke test das três CLIs contra o
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

## Testado com uma segunda empresa real

O item que tinha ficado de fora por depender de dado externo foi
fechado: testei o upload com o export Bloomberg MODL de uma segunda
empresa real (Blackstone, BX US). O resultado já valeu a pena — esse
arquivo só tinha a aba `Multiple Periods` (sem `DCF` nem `WACC`), e
`load_workbook_data()` acessava `wb["DCF"]`/`wb["WACC"]` direto, sem
checar se existiam. O usuário via um `KeyError` cru do openpyxl
("Worksheet DCF does not exist.") na barra lateral — não quebrava o app
(o `try/except` do upload já cobria isso), mas não explicava nada.

Agora `load_workbook_data()` confere as três abas logo no início e
levanta um `ValueError` claro dizendo quais faltam e por quê ("historicals
sozinho não é suficiente para rodar dcf_engine.py/target_price.py/
sensitivity.py"). Coberto por um teste com uma planilha sintética mínima
(`tests/test_valuation.py`), sem precisar versionar o arquivo real de
terceiros no repositório.

De quebra, essa segunda empresa confirmou que o parser já era robusto a
uma variação real que a AppLovin não tinha: a BX só tem 4 anos de
consenso (FY2026E–FY2029E) contra 5 da AppLovin, e a fronteira
"Fwd"/"Rep" cai numa coluna diferente (`I` em vez de `J`) — `load_from_modl.py`
já lê isso dinamicamente pelo rótulo de cada coluna (`period_type_for_column()`),
não por posição fixa, então essa parte funcionou sem nenhuma mudança.
