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
| `sensitivity.py` | Pronto — tabelas WACC × múltiplo de saída e crescimento × margem |
| `app.py` (Streamlit) | Pronto — abre todos os cálculos, não só o resultado final |

Rode para confirmar que tudo está funcionando:

```bash
pip install -r requirements.txt
python dcf_engine.py data/valuation.db "APP US" base
# ... Price per share: $388.54
python target_price.py data/valuation.db "APP US" base

# suite de regressão (17 testes) — cobre o caso base, os limites de
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

## Segunda descoberta: as duas tabelas de sensibilidade do arquivo original nem concordam entre si

Ao construir `sensitivity.py` reproduzindo as tabelas "SENSITIVITY 1:
WACC vs. TERMINAL EXIT MULTIPLE" (linhas 92–98) e "SENSITIVITY 2:
REVENUE GROWTH Δ vs. EBIT MARGIN Δ" (linhas 106–112) da aba `DCF`,
descobri que nenhuma das duas usa a mesma fórmula da célula principal
(`C87` = $388.54). E elas também não usam a mesma fórmula *entre si*.
Na célula de "delta zero" (WACC/múltiplo base, ou crescimento/margem
base) cada uma dá um número diferente:

| Fonte | Fórmula | Preço-alvo (base) |
|---|---|---|
| `C87` (DCF principal) | soma PV do FCF dos anos 1–5; valor terminal = EBITDA do ano 5, descontado no período do ano 5 | **$388.54** |
| Tabela 1 (WACC × Múltiplo), célula `D96` | soma PV do FCF dos **13 anos**; valor terminal ainda usa o EBITDA (desatualizado) **do ano 5**, mas descontado no período do **ano 13** | **$313.31** |
| Tabela 2 (Crescimento × Margem), célula `D110` | soma PV do FCF dos **13 anos**; valor terminal usa o EBITDA **do ano 13**, descontado no período do **ano 13** | **$412.19** |

A Tabela 2 é internamente consistente (é a mesma matemática de somar os
13 anos completos com o terminal alinhado ao último ano — o mesmo
resultado que `dcf_engine.run_dcf(..., explicit_years=13)` produz). A
Tabela 1 é a mais problemática das três: soma 13 anos de FCF mas ainda
ancora o EBITDA terminal no ano 5, e desconta esse valor terminal
desatualizado 8 anos além do que deveria — um erro que não é conservador
nem agressivo, é simplesmente inconsistente com qualquer definição
única de "quantos anos de projeção explícita" o modelo usa.

Diante disso, `sensitivity.py` **não** replica nenhuma dessas duas
variantes da planilha. Em vez disso, os dois grids são construídos
chamando `dcf_engine.run_dcf()` diretamente, perturbando um par de
inputs por vez — então a célula de delta zero de qualquer um dos dois
grids sempre bate exatamente com a saída do próprio `run_dcf()` para o
mesmo cenário/`explicit_years` (por padrão, $388.54). Isso garante que
uma tabela de sensibilidade sempre reconcilia com o caso-base que ela
está "sensibilizando" — o que a planilha original, surpreendentemente,
não garante.

Como checagem cruzada: rodando `sensitivity.py` com `--explicit-years 13`,
o grid de Crescimento × Margem bate célula a célula com a Tabela 2 do
arquivo original (ex.: delta -8%/-4% = $204.30, delta +8%/+4% = $865.46),
confirmando que a implementação é equivalente à única das duas tabelas
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
- **Sensibilidade** — os dois heatmaps (WACC × múltiplo, crescimento ×
  margem) mais a grade em números, e a tabela comparando os três preços-alvo
  divergentes que a própria planilha original produz em "caso base"
  (C87/D96/D110 — ver "Segunda descoberta" acima).
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
`tests/test_valuation.py` (`pytest`, 17 casos, todos passando) para não
regredir.

## Sugestões — o que falta para amadurecer o projeto

Nenhum item do escopo original (DCF, target price por múltiplos,
sensibilidade, interface) ficou pendente. O que listo abaixo são lacunas
reais encontradas na revisão que não bloqueiam o uso atual (só há uma
empresa carregada, AppLovin), mas que valem a pena antes de tratar isso
como uma ferramenta de produção com múltiplas empresas/usuários:

- **Só foi testado com uma empresa real.** O schema e o motor são
  multi-empresa por design e o isolamento entre empresas foi validado
  com um clone sintético (`tests/test_valuation.py`), mas nunca com um
  segundo arquivo `.xlsx` de verdade. Vale carregar um peer (ex. outro
  ad-tech) para achar premissas do parser que só aparecem com dados
  diferentes dos da AppLovin.
- **`get_wacc()` e `from_peg()` têm riscos de divisão por zero / base
  negativa não tratados.** `pretax_cost_of_debt = juros / dívida total`
  quebra para uma empresa sem dívida; `from_peg()` eleva
  `EPS_alvo/EPS_base` a uma potência fracionária, o que quebra
  silenciosamente (gera um número complexo, depois um erro de formatação
  mais adiante) se o ano-base tiver EPS negativo — a AppLovin teve
  EPS negativo em 2022, mas o ano-base usado hoje (2025A) é positivo,
  então isso não aparece com os dados atuais. Adicionar validação
  explícita (`ValueError` claro) antes de carregar uma segunda empresa.
- **CLIs devolvem traceback do Python cru** para ticker/banco inexistente
  (`dcf_engine.py`, `target_price.py`, `sensitivity.py`) — funciona, mas
  não é uma mensagem amigável. Baixa prioridade, mas fácil de arrumar
  (`try/except` no `main()` de cada CLI).
- **Sem CI.** Os 17 testes em `tests/test_valuation.py` só rodam quando
  alguém lembra de rodar `pytest` — os três bugs desta revisão só foram
  achados porque testei manualmente depois do fato. Um workflow simples
  do GitHub Actions (`pytest` a cada push/PR) fecharia esse buraco.
- **Terceiro heatmap de sensibilidade (Beta × Risk-Free Rate)** existe na
  planilha original (linhas 120–126) mas não foi replicado em
  `sensitivity.py`/`app.py`.
- **`data/valuation.db` acumula estado no servidor** quando `app.py` está
  hospedado para mais de um usuário — o upload grava direto no arquivo
  configurado em "Banco de dados", compartilhado entre todas as sessões
  apontando pro mesmo caminho. Bom para uso pessoal/local; para expor a
  um time, vale isolar por sessão ou trocar por um banco por usuário.
