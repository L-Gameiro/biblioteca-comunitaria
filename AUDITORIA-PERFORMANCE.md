# Auditoria de performance — Biblioteca CCE

Relatório de análise do `app.py` (2.070 linhas) rodando em Streamlit Community
Cloud + Supabase Postgres (transaction pooler, região `sa-east-1`).
> ⚠️ **Status:** os itens **1, 2 e 3** da ordem de ataque (N+1 da Gestão de
> Livros, engine em `st.cache_resource`, schema/init uma vez por processo) **já
> foram aplicados**. Os números "ANTES" abaixo são o estado original; veja a
> seção *Resultado das otimizações aplicadas* no fim do documento para o
> depois. Os demais itens seguem pendentes.

---

## Resumo executivo

A lentidão **não é causada pelo volume de dados**. A causa dominante é o número
de *round trips* ao banco por interação, multiplicado pela latência de rede.

Três achados explicam a maior parte do problema:

1. **O Streamlit descarta todo o estado de módulo a cada rerun.** Confirmado no
   código-fonte do Streamlit: cada rerun executa o script em um módulo novo
   (`types.ModuleType`). Isso zera o `@lru_cache` do engine e o
   `_initialized_engine_ids`. Resultado medido: **1 engine novo + 1 conexão TCP
   nova + 6 queries de schema a cada clique do usuário** — inclusive na tela de
   login, sem nenhum dado carregado.
2. **N+1 na Gestão de Livros:** 50 das 61 queries por rerun são duas consultas
   por livro exibido.
3. **Descasamento de região:** o app roda no Streamlit Cloud (provavelmente EUA)
   e o banco está em São Paulo. Isso multiplica cada round trip por ~10x em
   relação ao ambiente local.

Corrigir os itens 1 e 2 reduz a tela mais pesada de **66 para ~11 round trips**
(−83%), sem mudança de schema e sem risco de dado desatualizado.

---

## Como os números foram obtidos

**Medido de verdade** (não é estimativa):

- Instrumentei `sqlalchemy.event` (`before_cursor_execute`, `connect`) e um
  wrapper em `create_engine` para contar queries, conexões novas e engines
  criados **por rerun**, sem tocar no `app.py`.
- Renderizei cada tela com `streamlit.testing.v1.AppTest` (o mesmo motor de
  rerun do app real) contra um banco com volume realista: **2.552 livros,
  582 empréstimos (182 ativos), 41 usuários, 183 livros pendentes de
  reconciliação**.
- Latência de rede: handshake TCP puro até o pooler do Supabase (sem
  autenticar, sem ler dado): **mediana 13,1 ms** a partir desta máquina.

**Estimado** (marcado como tal ao longo do texto):

- Custo de abrir uma conexão Postgres nova ≈ **4–6 RTT** (TCP + TLS + startup/auth).
- RTT do Streamlit Community Cloud até `sa-east-1` ≈ **110–160 ms** — inferido da
  geografia, **não medido**. Veja "Como confirmar" na seção de limitações.

**Tempos em ms de SQLite local não são representativos** do Supabase e por isso
não são usados como argumento — a métrica que importa é a **contagem de round
trips**, que se traduz diretamente em latência.

### Custo medido por rerun

| Tela | Queries | Conexões novas | RTT total¹ | @13 ms (local) | @130 ms (cloud, est.) |
|---|---:|---:|---:|---:|---:|
| **Gestão de Livros** | **61** | 1 | ~66 | 0,86 s | **8,6 s** |
| Painel | 16 | 1 | ~21 | 0,27 s | 2,7 s |
| Reconciliação | 12 | 1 | ~17 | 0,22 s | 2,2 s |
| Catálogo | 11 | 1 | ~16 | 0,21 s | 2,1 s |
| Empréstimos | 9 | 1 | ~14 | 0,18 s | 1,8 s |
| Histórico completo | 9 | 1 | ~14 | 0,18 s | 1,8 s |
| Importar CSV (sem arquivo) | 8 | 1 | ~13 | 0,17 s | 1,7 s |
| **Piso fixo (tela de login)** | **8** | **1** | ~13 | 0,17 s | 1,7 s |

¹ RTT total = queries + ~5 RTT estimados para abrir a conexão nova.

O **piso fixo de 8 queries + 1 conexão** aparece em *toda* interação, mesmo na
tela de login com o banco vazio. É isso que faz o app parecer lento
"independente da quantidade de livros" — exatamente o sintoma relatado.

---

## (a) Otimizações seguras

Ordenadas por relação impacto/esforço. Nenhuma delas serve dado desatualizado.

### 1. Cachear o engine com `st.cache_resource`

| | |
|---|---|
| **Onde** | `app.py:178-186` (`_build_engine`, `get_engine`), usado por `main()` em `app.py:2045` |
| **Hoje** | `@lru_cache` é estado de módulo. O Streamlit cria um módulo novo por rerun, então o cache nasce vazio: **medido 1 `create_engine` + 1 conexão TCP nova por rerun**. O pool de conexões nunca é reaproveitado — ele é criado e descartado a cada clique. |
| **Impacto** | Elimina o handshake completo (TCP+TLS+auth) por interação: **−4 a −6 RTT** (estimado, ~0,07 s local / **~0,7 s no cloud**). É o único item que ataca o custo de *conexão*, não de query. |
| **Esforço** | **Baixo** — trocar `@lru_cache` por `@st.cache_resource`. |
| **Risco** | **Baixo.** `st.cache_resource` existe exatamente para isso (recursos globais não serializáveis). O Engine é thread-safe e compartilhável entre sessões. |
| **Schema/migração** | Nenhuma. |
| **Dado desatualizado** | Nenhum risco — cacheia a *fábrica de conexões*, não dados. |

### 2. Rodar schema/migração uma vez por processo, não por rerun

| | |
|---|---|
| **Onde** | `app.py:226-255` (`create_schema`, `init_db`, `_migrate_add_loans_due_date`), chamado em `main()` |
| **Hoje** | `_initialized_engine_ids` também é estado de módulo, então o guard nunca pega. **Medido, toda vez:** 3× `PRAGMA table_info`, 1× listagem de tabelas, 1× `table_xinfo` (a inspeção da migração de `due_date`) e 1× `SELECT COUNT(*) FROM users`. São **6 das 8 queries do piso fixo**. No Postgres essas viram consultas a `information_schema`, mais caras que os PRAGMAs do SQLite. |
| **Impacto** | **−6 RTT por rerun** (~0,08 s local / **~0,78 s no cloud**). Medido. |
| **Esforço** | **Baixo** — mover a inicialização para dentro de uma função `@st.cache_resource`. |
| **Risco** | **Baixo.** A semântica correta já é "uma vez por processo"; hoje ela só não funciona por causa do reset de módulo. |
| **Schema/migração** | A migração passa a rodar no boot do container em vez de a cada clique — que é o comportamento pretendido. |

### 3. Eliminar o N+1 da Gestão de Livros

| | |
|---|---|
| **Onde** | `app.py:1161-1162` — `get_active_loan_for_book()` e `count_loans_for_book()` dentro do loop de livros |
| **Hoje** | **Medido: 61 queries por rerun**, sendo **25 + 25 = 50** consultas individuais (uma página = 25 livros). Cada livro custa 2 round trips só para saber se tem empréstimo ativo e quantos registros históricos possui. |
| **Impacto** | **61 → ~11 queries** (−50 RTT ≈ 0,65 s local / **~6,5 s no cloud**). É o maior ganho isolado do relatório. Substituir por **uma** agregação com `GROUP BY book_id` sobre os 25 ids da página. |
| **Esforço** | **Baixo/médio** — uma query nova + adaptar o loop para ler de um dict. |
| **Risco** | **Baixo.** Leitura pura, sem alterar regra de negócio. Os testes de remoção de livro cobrem o comportamento dependente desses contadores. |
| **Schema/migração** | Nenhuma. |

### 4. Não gerar os dois CSVs de exportação a cada rerun do Painel

| | |
|---|---|
| **Onde** | `app.py:1746` e `app.py:1753` — `data=export_books_csv(conn)` / `data=export_loans_csv(conn)` |
| **Hoje** | `st.download_button` exige os bytes **antes** de renderizar. **Medido: 3.134 linhas lidas e 262 KB de CSV gerados a cada rerun do Painel**, mesmo que ninguém clique em baixar. Esses 262 KB ainda são serializados para o browser em todo rerun. |
| **Impacto** | −2 queries, −3.134 linhas lidas e −262 KB de CPU/tráfego por rerun. Cresce linearmente com o acervo. |
| **Esforço** | **Baixo** — gerar sob demanda (botão que só então produz o arquivo, ou `st.fragment`). |
| **Risco** | **Baixo.** Gerar no clique deixa o CSV **mais** atual, não menos. |

### 5. Criar os índices ausentes

| | |
|---|---|
| **Onde** | Definição das tabelas, `app.py:120-163` |
| **Hoje** | **Medido: zero índices de usuário.** Só existem os automáticos de PK e UNIQUE (`books.code`, `users.email`). Todas as colunas de filtro/junção estão sem índice: `loans.book_id`, `loans.user_id`, `loans.status`, `loans.due_date`, `books.status`, `books.category`, `books.title`, `books.author`. `EXPLAIN QUERY PLAN` confirma `SCAN books`, `SCAN loans` e `USE TEMP B-TREE FOR ORDER BY`. **No Postgres, chaves estrangeiras não ganham índice automático** — `loans.book_id` e `loans.user_id` estão realmente sem cobertura. |
| **Impacto** | **Honestamente modesto agora:** com 2.552 livros e 582 empréstimos, uma varredura sequencial no Postgres custa poucos milissegundos — perto de nada frente aos 13–130 ms de rede por round trip. **O ganho real é evitar degradação:** o `EXISTS` da Reconciliação e os lookups por `book_id` degradam com o crescimento do histórico. |
| **Esforço** | **Baixo** — `CREATE INDEX`, aditivo. |
| **Risco** | **Baixo.** Custo: escrita marginalmente mais lenta e um pouco de disco. |
| **Schema/migração** | **Sim, mas aditiva** — `CREATE INDEX IF NOT EXISTS`, sem downtime e sem mexer em dado. Use `CONCURRENTLY` no Postgres se quiser evitar lock. |

### 6. Paginar "Empréstimos ativos"

| | |
|---|---|
| **Onde** | `app.py:1508` (`show_loan_management`) — usa `list_active_loans()` sem `LIMIT` |
| **Hoje** | Renderiza **todos** os empréstimos ativos. **Medido com 182 ativos: 183 botões e 854 elementos por rerun.** As outras listas já são paginadas em 25; esta escapou. |
| **Impacto** | Custo de *renderização*, não de banco (a query é 1 só). Reduz drasticamente o payload que o Streamlit serializa para o browser a cada interação. |
| **Esforço** | **Baixo** — reaproveitar `_paginate()`, que já existe. |
| **Risco** | **Baixo.** Muda a UX (passa a ter páginas). |

### 7. Empacotar o `commit_import` em lote

| | |
|---|---|
| **Onde** | `app.py:829-840` — loop com um `conn.execute(INSERT)` por linha |
| **Hoje** | **Medido: 300 linhas → 300 queries (1,00 por linha).** Extrapolando para a carga real de 2.552 livros: **2.552 round trips** ≈ 33 s local / **~5,5 min no cloud** (estimativa). |
| **Impacto** | `executemany` (passar a lista de dicts em um `execute`) reduz a dezenas de round trips. |
| **Esforço** | **Baixo.** |
| **Risco** | **Baixo.** Atenção: com lote, o erro de uma linha aborta o lote inteiro — o que é aceitável porque a validação já bloqueia o import antes de chegar aqui. |
| **Observação** | Só dói durante a importação, que é rara. Alta prioridade se você ainda vai recarregar o acervo; baixa se a carga já foi feita. |

---

## (b) Exigem cuidado com dado desatualizado

Aqui `st.cache_data` **funcionaria** e daria ganho — mas o preço é servir dado
velho. Regra que eu seguiria: **nada que represente disponibilidade física de
livro ou empréstimo ativo pode ser cacheado sem invalidação explícita na
escrita.**

### 8. `list_book_categories()` — o candidato mais seguro

- **Onde:** `app.py:548`, chamado por `_book_search_controls()` em **toda** tela de listagem.
- **Hoje:** 1 `SELECT DISTINCT` por rerun, por tela.
- **Por que é seguro:** categorias mudam raramente (só em cadastro, edição ou importação).
- **Recomendação:** `st.cache_data(ttl=300)` **com invalidação explícita** (`.clear()`) em `add_book`, na edição e no `commit_import`. Sem a invalidação, uma categoria nova não aparece no filtro por até 5 min.
- **Impacto:** −1 RTT por rerun nas telas de listagem. **Risco:** baixo *se* invalidar.

### 9. Métricas do Painel

- **Onde:** `app.py:1339` (`get_dashboard_metrics`) — 5 queries agregadas por rerun.
- **Tensão real:** são contadores de painel (toleram alguns segundos de atraso), **mas** "Pendentes de reconciliação" e "Atrasados" disparam ação do admin. Um número velho pode levar alguém a abrir a Reconciliação e não achar o livro que outro admin acabou de resolver.
- **Recomendação:** `ttl` curto (15–30 s) **ou** deixar sem cache. Se cachear, invalidar ao registrar empréstimo, devolução e reconciliação.
- **Impacto:** −5 RTT por rerun do Painel. **Risco:** médio.

### 10. Filtros e paginação do "Histórico completo" em SQL

- **Onde:** `app.py:1551` em diante — carrega **todos** os empréstimos (JOIN `books` + `users`) e filtra por livro, usuário e período **em Python** (`filtered = [...]`). Sem `LIMIT`.
- **Hoje:** 582 linhas por rerun; cresce sem limite com o histórico.
- **Recomendação:** empurrar filtros + `LIMIT/OFFSET` para o SQL, como já é feito no Catálogo. Os dropdowns de filtro precisam de listas distintas — duas queries pequenas e separadas.
- **Esforço:** **médio** (a tela monta as opções de filtro a partir do mesmo resultado, então exige reestruturação). **Risco:** baixo. **Dado desatualizado:** nenhum.

### 11. `max_numeric_code_for_category()` lê a tabela inteira

- **Onde:** `app.py:332-345` — `SELECT code, category FROM books` sem `WHERE`, filtrando e convertendo para inteiro em Python.
- **Hoje:** **medido — lê as 2.552 linhas** para calcular um `MAX`. Chamado a cada `add_book` no acervo Espiritual e uma vez por lote de importação.
- **Por que não é trivial:** o filtro "código é puramente numérico" precisa ser portátil entre SQLite e Postgres (`GLOB` vs `~ '^[0-9]+$'`), e a base legada tem códigos fora de padrão (`BURE`, `MILJ-001 (a)`) que **não podem** entrar na sequência.
- **Esforço:** **médio.** **Risco:** **médio** — errar o predicado muda qual código é emitido. Há testes cobrindo isso (`test_max_numerico_ignora_codigos_legados_fora_de_padrao_e_outras_categorias`).

### 12. Reprocessamento do CSV a cada rerun da tela de importação

- **Onde:** `app.py:1945` chama `process_import_rows()` incondicionalmente; o N+1 está em `app.py:386` (`count_books_by_author` por linha).
- **Hoje:** **medido — 1,00 query por linha do CSV.** Um arquivo de 500 linhas gera **501 queries**; a carga real de 2.552 linhas geraria **2.553 queries a cada interação com qualquer widget da tela** (trocar um selectbox de mapeamento, digitar na categoria fixa). No cloud isso é da ordem de **minutos por clique**.
- **Recomendação:** (a) pré-carregar as contagens por autor em **um** `GROUP BY`; (b) só reprocessar quando o mapeamento realmente mudar (assinatura em `session_state`, como já é feito no parsing).
- **Esforço:** **médio.** **Risco:** baixo. **Prioridade:** alta se ainda haverá importações; irrelevante se a carga acabou.

---

## (c) O que eu evitaria, apesar do ganho

### 13. `st.cache_data` em disponibilidade de livro, catálogo ou empréstimos ativos
Seria o maior ganho bruto do relatório — e é o mais perigoso. Um `list_books()`
cacheado faz um livro aparecer como "Disponível" depois de emprestado, e dois
leitores conseguem pegar o mesmo exemplar. `count_books()`, `list_books()`,
`list_active_loans()` e `list_unreconciled_books()` **devem continuar indo ao
banco**. O ganho real está em reduzir *round trips por tela* (itens 1–3), não em
cachear resultado.

### 14. `st.cache_resource` em objetos `Connection`
Tentador (evitaria o checkout por rerun), mas **quebra**: uma `Connection`
carrega estado transacional e o `st.cache_resource` compartilha o objeto entre
**todas as sessões e threads**. Duas pessoas usando o app dividiriam a mesma
transação — commits e rollbacks de uma afetariam a outra. Cachear o **Engine**
(item 1) é correto; cachear a **Connection** não é.

### 15. Coluna gerada + índice trigram para a busca sem acento
A cadeia de `REPLACE` aninhados em `_sql_unaccent` (`app.py:472`) impede uso de
índice na busca textual — tecnicamente um problema real. Mas **com 2.552 livros
a varredura custa poucos milissegundos**, ordens de grandeza abaixo dos 13–130 ms
de um único round trip. Adotar coluna gerada + `pg_trgm` + `unaccent` traria
migração de schema, backfill e complexidade em toda escrita, para resolver algo
que não é o gargalo. **Revisitar se o acervo passar de ~50 mil itens.**

### 16. Espalhar `st.fragment` pelo app
`st.fragment` evita reexecutar o script inteiro e é atraente aqui. Mas usado de
forma ampla ele fragmenta o estado e cria bugs sutis de sincronia (um fragmento
com dado velho ao lado de outro atualizado). Vale **cirurgicamente** — por
exemplo, isolando os botões de exportação (item 4) —, não como estratégia geral.

---

## Limitações estruturais — otimizar código não resolve

### A. O Streamlit reexecuta o script inteiro a cada interação
Verificado no fonte (`streamlit/runtime/scriptrunner/script_runner.py:689`):
cada rerun cria um `types.ModuleType` novo e executa o script dentro dele.
**Não existe forma de preservar estado de módulo entre reruns** — `lru_cache`,
variáveis globais e sets de controle são todos descartados. Os únicos
sobreviventes são `st.session_state`, `st.cache_data` e `st.cache_resource`.
Os itens 1 e 2 não são "otimizações": são a forma correta de conviver com esse
modelo.

### B. Descasamento de região (provavelmente o maior multiplicador)
O banco está em `sa-east-1` (São Paulo). O Streamlit Community Cloud roda em
infraestrutura dos EUA. Cada round trip cruza o continente — **estimados
110–160 ms contra os 13,1 ms medidos** desta máquina. Todos os números da coluna
"@130 ms" saem daí.

**Nenhuma otimização de código elimina isso** — só é possível (i) reduzir a
*quantidade* de round trips (itens 1–3 cortam ~56 dos 66 da tela mais pesada) ou
(ii) aproximar app e banco.

**Como confirmar** (vale muito antes de priorizar): adicione temporariamente ao
app, rodando **no Community Cloud**, uma medição de `SELECT 1` em laço e mostre a
mediana na tela. Se der ~15 ms, minha estimativa está errada e o item B some. Se
der ~130 ms, ele é o multiplicador de tudo.

### C. Cold start do Community Cloud
O container hiberna após inatividade. A primeira interação depois disso paga
boot do processo + import do Streamlit/SQLAlchemy + conexão — vários segundos,
independente do código. Nada a fazer no `app.py`.

### D. `pool_pre_ping` frente ao transaction pooler
`app.py:181` usa `pool_pre_ping=True`. Hoje ele quase não pesa (com engine novo
por rerun, a conexão nasce fresca e não é "pingada"). **Depois do item 1** ele
passa a custar **+1 RTT por checkout** de conexão reaproveitada. Como o pooler
do Supabase (porta 6543) já cuida de conexões mortas, vale avaliar
`pool_pre_ping=False` + `pool_recycle`. **Risco médio:** com o container
hibernando (item C), o pre_ping é justamente o que evita erro de conexão morta
na primeira interação. **Medir antes de mexer.**

---

## O que investiguei e descartei

| Verificado | Conclusão |
|---|---|
| N+1 no Catálogo (`show_catalog`) | **Limpo.** 3 queries por rerun (contagem, página, categorias), independente do tamanho do acervo. |
| N+1 em "Meus Empréstimos" | **Limpo.** 2 queries fixas. |
| N+1 na Reconciliação | **Limpo.** 12 queries por rerun; `list_borrowers()` é chamado uma vez, fora do loop. |
| `add_book()` | 2 queries por livro — aceitável para operação manual. (Uma delas é o item 11 no acervo Espiritual.) |
| Cache do Streamlit já em uso | **Nenhum.** Zero ocorrências de `st.cache_data`/`st.cache_resource`. Não há risco de dado velho hoje — nem reaproveitamento. |
| Cache de parsing do CSV | **Correto.** Chaveado por `(nome, tamanho)` em `session_state`; não reparseia à toa. O problema é o `process_import_rows` (item 12), não o parsing. |
| Detecção de delimitador (`_detect_csv_delimiter`) | **Barato.** Lê no máximo 20 linhas. |
| `generate_book_code`, `_strip_diacritics`, `normalize_status` | Python puro, microssegundos. Irrelevantes. |
| `_inject_card_border_css` | Uma string de markdown por rerun. Desprezível. |
| Nº de checkouts de conexão por rerun | 2 (`init_db` + `main`). Some para 1 com o item 2. |
| `list_active_loans(only_overdue=True)` | Filtra atraso em Python sobre 182 linhas já carregadas. 1 query de qualquer forma — ganho quase nulo, não vale mexer isolado; cai junto com o item 6. |
| Índices de PK/UNIQUE | Presentes e usados (`books.code`, `users.email`). O que falta é o das colunas de filtro (item 5). |
| Vazamento de conexão | **Nenhum.** Todos os `get_connection()` estão em `with` ou com `.close()` explícito. |
| Volume como causa raiz | **Descartado.** O piso de 8 queries + 1 conexão por rerun ocorre com o banco vazio — condizente com a lentidão relatada *antes* da carga. |

---

## Ordem sugerida de ataque

| # | Item | Ganho medido | Esforço | Risco |
|---|---|---|---|---|
| 1 | N+1 da Gestão de Livros (item 3) | −50 RTT/rerun | Baixo/médio | Baixo |
| 2 | Engine em `st.cache_resource` (item 1) | −1 conexão/rerun (~5 RTT) | Baixo | Baixo |
| 3 | Schema/init uma vez por processo (item 2) | −6 RTT/rerun | Baixo | Baixo |
| 4 | Exportações sob demanda (item 4) | −262 KB e −2 queries/rerun | Baixo | Baixo |
| 5 | Paginar Empréstimos ativos (item 6) | −830 elementos/rerun | Baixo | Baixo |
| 6 | Índices (item 5) | Pequeno agora, protege o futuro | Baixo | Baixo |
| 7 | `commit_import` em lote (item 7) | −2.5k RTT por carga | Baixo | Baixo |
| 8 | CSV import sem reprocessar (item 12) | −2.5k RTT por clique | Médio | Baixo |
| 9 | Histórico paginado em SQL (item 10) | Evita crescimento sem limite | Médio | Baixo |
| 10 | Confirmar RTT real no cloud (limitação B) | Define se vale mudar de região | Baixo | Nenhum |

Os itens 1–3 juntos levam a Gestão de Livros de **66 para ~11 round trips
por interação** — de ~8,6 s para ~1,4 s no cenário de cloud estimado — sem
mudança de schema e sem qualquer risco de servir dado desatualizado.


---

# Resultado das otimizações aplicadas

Aplicados os itens 1, 2 e 3, um de cada vez, com medição antes/depois pelo mesmo
harness (contagem de round trips por rerun, acervo de 2.552 livros).

## Round trips por rerun

| Tela | Queries antes | Queries depois | Δ | Conexões novas antes → depois |
|---|---:|---:|---:|---|
| **Gestão de Livros** | 61 | **5** | **−56 (−92%)** | 1 → **0** |
| Painel | 16 | **9** | −7 (−44%) | 1 → **0** |
| Reconciliação | 12 | **5** | −7 (−58%) | 1 → **0** |
| Catálogo | 11 | **4** | −7 (−64%) | 1 → **0** |
| Empréstimos | 9 | **2** | −7 (−78%) | 1 → **0** |
| Histórico completo | 9 | **2** | −7 (−78%) | 1 → **0** |
| Importar CSV (sem arquivo) | 8 | **1** | −7 (−88%) | 1 → **0** |
| **Piso fixo (login)** | 8 | **1** | **−7 (−88%)** | 1 → **0** |

Engines criados por rerun: **1 → 0** em todas as telas (o pool passou a ser
reaproveitado entre reruns e sessões).

> No Postgres o ganho é ainda um pouco maior: a única query restante no piso é
> `PRAGMA foreign_keys = ON`, emitida apenas no SQLite. Em produção o piso vai a
> **0 queries** e a Gestão de Livros a **4**.

## Tradução para latência

| Tela | Antes | Depois | @13 ms (local) | @130 ms (cloud, est.) |
|---|---:|---:|---|---|
| Gestão de Livros | ~66 RTT | **~5 RTT** | 0,86 s → **0,07 s** | 8,6 s → **0,65 s** |
| Piso fixo | ~13 RTT | **~1 RTT** | 0,17 s → **0,01 s** | 1,7 s → **0,13 s** |

## O que restou na tela mais pesada

As 5 consultas da Gestão de Livros são todas necessárias e independentes do
número de livros exibidos:

1. `PRAGMA foreign_keys` (só SQLite — não existe no Postgres)
2. `SELECT DISTINCT category` — opções do filtro
3. `COUNT(*)` — total para a paginação
4. `SELECT ... LIMIT 25 OFFSET n` — a página
5. `SELECT book_id, COUNT(*), SUM(CASE WHEN status='ativo')... GROUP BY book_id` — **a agregação única que substituiu as 50 consultas por livro**

## Verificação

- **193/193 testes passando** (188 anteriores + 5 novos para `loan_summary_for_books`).
- Os testes de `init_db` foram adaptados ao novo mecanismo e **reforçados**: a
  verificação de idempotência agora limpa o cache entre as duas chamadas, de
  modo que a inicialização roda de fato duas vezes (antes, o cache
  curto-circuitaria e o teste não provaria nada). O teste de que o seed de
  livros não volta após restart continua valendo.
- Semântica da remoção de livro validada na UI real: bloqueio por empréstimo
  ativo, confirmação com contagem de histórico e remoção atômica — os três
  estados inalterados.
- Isolamento transacional verificado: o Engine é compartilhado, mas cada
  chamada a `get_connection()` devolve uma `Connection` distinta e escritas não
  commitadas de uma sessão não vazam para outra.
