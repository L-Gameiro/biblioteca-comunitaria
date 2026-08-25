# Relatório de revisão — Biblioteca Comunitária CCE

**Escopo:** `app.py` (2.872 linhas, 89 funções), `test_app.py` (3.100 linhas, 235 testes),
`bookCode.ts`, `.streamlit/`, `.gitignore`, artefatos versionados.
**Método:** leitura integral do código + execução da suíte (235 passed, 41s) + provas
empíricas em SQLite descartável para os achados 2, 5, 6 e 8 (os resultados medidos estão
citados em cada item).
**Nenhum código foi alterado.** Foi criado apenas este arquivo.

> Nota sobre o número de linhas: o enunciado fala em ~2.400. O arquivo hoje tem **2.872**.
> O `AUDITORIA-PERFORMANCE.md` do repositório ainda descreve o app com 2.070 linhas e
> marca como pendentes itens que continuam pendentes — ele está desatualizado no tamanho,
> mas continua correto no diagnóstico.

---

## Sumário por severidade

| # | Achado | Sev. | Esforço | Risco de corrigir |
|---|---|---|---|---|
| 1 | Senha padrão `admin123` em repositório público + caminho de recriação automática | **Crítico** | Baixo | Baixo |
| 2 | Edição livre de `status` na Gestão de Livros quebra o invariante livro↔empréstimo | **Alto** | Baixo | Baixo |
| 3 | Exceções não tratadas nos caminhos de escrita da UI → traceback completo no navegador | **Alto** | Baixo | Baixo |
| 4 | `add_book` reemite código já usado após exclusão → `IntegrityError` e livro perdido | **Alto** | Médio | Médio |
| 5 | `request_loan` / `return_loan` / `delete_book` sem trava de linha (corrida real no Postgres) | **Alto** | Baixo | Baixo |
| 6 | N+1 no `process_import_rows`, reexecutado a cada rerun da tela de importação | **Médio** | Médio | Baixo |
| 7 | Admin não tem como registrar um empréstimo de balcão | **Médio** | Médio | Baixo |
| 8 | CSV em Latin-1/Windows-1252 falha com mensagem técnica; fallback é código morto | **Médio** | Baixo | Baixo |
| 9 | `commit_import` grava sem revalidar erros nem duplicidade | **Médio** | Baixo | Baixo |
| 10 | Enumeração de contas na tela pública de cadastro (+ oráculo de timing no login) | **Médio** | Baixo | Baixo |
| 11 | Nenhum índice declarado; busca com 48 `REPLACE` aninhados × 4 colunas | **Médio** | Baixo | Baixo |
| 12 | `show_admin_loan_history` carrega todo o histórico com PII, sem paginação | **Médio** | Médio | Baixo |
| 13 | Não existe exclusão nem anonimização de leitor (LGPD) — e a FK bloqueia fazer à mão | **Médio** | Alto | Médio |
| 14 | Lockout por e-mail permite negar acesso a uma conta conhecida | **Médio** | Médio | Médio |
| 15 | Colunas fixas e tabelas largas em tela estreita | **Médio** | Médio | Baixo |
| 16 | `full_name` de auto-cadastro renderizado como markdown nas telas de admin | Baixo | Baixo | Baixo |
| 17 | Senha temporária persiste em `session_state` até ser dispensada | Baixo | Baixo | Baixo |
| 18 | `loan_date` gravado ora como data, ora como datetime | Baixo | Baixo | Médio |
| 19 | Regras duplicadas (status, busca, formatação de atraso) | Baixo | Baixo | Baixo |
| 20 | Código e arquivos mortos versionados | Cosmético | Baixo | Baixo |
| 21 | Categoria: `selectbox` no cadastro, texto livre na edição | Cosmético | Baixo | Baixo |

---

## (a) Corrigir agora

### 1. `admin123` em repositório público — **CRÍTICO**

**Onde:** `app.py:384`

```python
create_user(conn, "Administrador", "admin@biblioteca.org", "", "admin123", "admin",
            must_change_password=True)
```

**O que está errado.** O commit `5c32ffe` removeu as credenciais do README, das docstrings
e dos comentários — mas a senha continua literal no código, que é público. E ela também
está em todo o histórico do Git (`git log -S"admin123"` acha desde `f32a408`), então
apagar a linha agora **não** resolve sozinho.

Pior: o bootstrap não roda só na primeira execução. `_ensure_initialized` (`app.py:349`)
recria essa conta **sempre que o banco fica sem nenhum admin**, e o Streamlit Cloud
hiberna o container — a cada retorno do app o cache nasce vazio e a checagem roda de novo.
`must_change_password=True` não protege: quem chega primeiro com a senha conhecida é quem
define a nova. O rate limit também não protege, porque a senha está *correta*.

**Por que importa em produção.** O app é público, a URL do Streamlit Cloud é enumerável e
o repositório expõe o e-mail e a senha do administrador. Um admin tem acesso a nome,
e-mail e telefone de todos os leitores, ao histórico completo de quem leu o quê, e à
exportação disso tudo em CSV.

**Correção sugerida:** ler a credencial inicial de `st.secrets` (ex.: `BOOTSTRAP_ADMIN_EMAIL`
/ `BOOTSTRAP_ADMIN_PASSWORD`) e falhar de forma clara se não estiver configurada, em vez de
cair num valor literal. Independente disso, **trocar hoje a senha do admin em produção** e
confirmar que `admin@biblioteca.org` não está mais com a senha do código — essa parte não
depende de deploy.

**Esforço:** baixo (uma função). **Risco:** baixo — mas exige coordenar com o deploy: se o
segredo não for configurado antes, o app sobe sem conseguir criar o admin.

---

### 2. A edição de status quebra o invariante livro↔empréstimo — **ALTO**

**Onde:** `app.py:1713-1735` (`show_book_management`, `st.selectbox("Status", ...)` + o
`UPDATE books SET ... status = :status`)

**O que está errado.** O formulário de edição deixa o admin gravar qualquer status
diretamente, sem olhar para os empréstimos. Marcar como `Disponível` um livro que tem
empréstimo ativo produz exatamente o estado inconsistente que você citou — e a
Reconciliação **não** detecta esse caso: `_unreconciled_where` (`app.py:1783`) só procura o
sentido oposto (`Emprestado` sem loan ativo).

**Prova (rodada agora, SQLite descartável):**

```
=== admin edita status p/ Disponível com loan ativo ===
 empréstimos ATIVOS no mesmo exemplar: 2
 aparece em Reconciliação? 0
```

Depois do `UPDATE` manual, `request_loan` revalida o status, vê `Disponível` e cria um
segundo empréstimo ativo. Dois leitores ficam registrados com o mesmo exemplar físico, e
nenhuma tela sinaliza. Ao devolver o primeiro, o livro volta a `Disponível` com o segundo
empréstimo ainda aberto.

**Por que importa.** Empréstimo é o dado que o app existe para controlar. Um exemplar com
dois responsáveis registrados só é descoberto quando alguém for cobrar a devolução — e aí
o histórico já está errado, e não há como saber qual dos dois registros é o real.

**Correção sugerida (três camadas, do mais barato ao mais completo):**
1. Bloquear no `UPDATE`: recusar `Disponível`/`Em Manutenção` se `get_active_loan_for_book`
   retornar linha, com mensagem explicando que a devolução precisa ser registrada antes.
2. Estender `_unreconciled_where` (ou criar um segundo painel) para listar também
   `status != 'Emprestado'` **com** loan ativo — a inconsistência inversa.
3. Rodar uma consulta de verificação no Supabase hoje, para saber se o estado já existe na
   base real:
   ```sql
   SELECT b.id, b.code, b.title, b.status, COUNT(l.id) AS loans_ativos
   FROM books b JOIN loans l ON l.book_id = b.id AND l.status = 'ativo'
   GROUP BY b.id HAVING b.status <> 'Emprestado' OR COUNT(l.id) > 1;
   ```

**Esforço:** baixo. **Risco:** baixo — só restringe.

---

### 3. Exceções não tratadas nos caminhos de escrita — **ALTO**

**Onde:** `app.py:1661` (`request_loan` no catálogo), `1685` (`add_book`), `1722` (o
`UPDATE` da edição), `2120` e `2257` (`return_loan`), `2732` (`commit_import`).

**O que está errado.** Todas essas chamadas podem levantar `ValueError` ou `IntegrityError`
e nenhuma está dentro de `try`. `.streamlit/config.toml` não define `client.showErrorDetails`,
então vale o padrão `"full"`: o Streamlit renderiza **o traceback completo no navegador**,
incluindo o SQL, os caminhos do servidor e os valores dos parâmetros.

Compare com a Reconciliação (`app.py:2588-2617`) e com a remoção de livro
(`app.py:1751-1777`), que fazem certo: `try / except (ValueError, IntegrityError)` +
`conn.rollback()` + `st.error`. O padrão correto existe no arquivo; simplesmente não foi
aplicado nos outros lugares.

Falta também o `conn.rollback()`: a conexão fica em transação abortada até o fim do rerun.

**Por que importa.** O caso mais provável não é hipotético — é o achado 4 abaixo, que já
reproduzi. O admin clica em "Cadastrar livro", vê uma tela de erro em inglês com SQL, e o
livro não é salvo. E o catálogo é acessível a qualquer leitor auto-cadastrado.

**Correção sugerida:** aplicar o mesmo `try/except/rollback/st.error` dos dois pontos que já
o fazem, e adicionar ao `config.toml`:
```toml
[client]
showErrorDetails = "stacktrace"   # ou "none" — avaliar quanto de detalhe você quer em prod
```

**Esforço:** baixo. **Risco:** baixo.

---

### 4. `add_book` reemite código já usado após exclusão — **ALTO**

**Onde:** `app.py:880` (`BookCodeAllocator.resolve_code` → `count_books_by_author`) e
`app.py:1685`.

**O que está errado.** A estratégia por autor deriva o sequencial de uma **contagem**, não
do maior código já emitido. Se um livro do meio for excluído, a contagem cai e o próximo
livro do mesmo autor recebe um código que já existe.

**Prova (rodada agora):**

```
add: ASSM-001 / ASSM-002 / ASSM-003
apagado ASSM-002 → próximo add: IntegrityError (UNIQUE constraint failed: books.code)
```

A estratégia numérica (`max_numeric_code_for_category`, `app.py:826`) já usa `MAX` e está
correta; só a estratégia por autor usa `COUNT`. Duas regras para o mesmo problema, e só uma
delas envelheceu bem.

**Por que importa.** A Gestão de Livros oferece exclusão, com confirmação, na tela. Excluir
uma duplicata do acervo — operação de limpeza absolutamente normal — arma a falha para o
próximo cadastro daquele autor. Combinado com o achado 3, o resultado é um traceback e um
livro que o admin acha que cadastrou.

**Correção sugerida:** derivar do maior sufixo já emitido para o autor
(`MAX(CAST(substring(code from '\d+$') AS int))` com o mesmo prefixo), como já é feito no
caminho numérico. Cuidado: isso muda o código gerado para autores que já tiveram exclusões,
então vale rodar antes uma consulta de quantos autores estariam nessa situação.

**Esforço:** médio. **Risco:** médio — mexe na regra de código, que é o dado impresso na
etiqueta física. Precisa de teste com o acervo real antes de subir.

---

### 5. Empréstimo, devolução e exclusão sem trava de linha — **ALTO**

**Onde:** `app.py:1415` (`request_loan`), `app.py:1443` (`return_loan`), `app.py:951`
(`delete_book`).

**O que está errado.** As três seguem o padrão *ler → decidir em Python → escrever*, sem
`with_for_update()` e sem `UPDATE ... WHERE` condicional com checagem de `rowcount`. No
Postgres em `READ COMMITTED` (o padrão do Supabase), duas sessões podem ler
`status = 'Disponível'` na mesma janela e ambas inserirem o empréstimo.

O contraste é gritante dentro do próprio arquivo: `_lock_unreconciled_book` (`app.py:1825`)
faz exatamente a coisa certa — `with_for_update()`, revalidação e mensagem de conflito —
e tem teste de duas conexões simultâneas (`test_app.py:2189`). A Reconciliação é a única
operação de empréstimo protegida; o empréstimo normal, que é o caminho mais usado, não é.

Vale registrar o que **está** correto: `request_loan` revalida o status com um `SELECT`
fresco, não confia no que a tela carregou. A falta é só a trava.

**Por que importa.** Numa biblioteca comunitária a concorrência real é baixa, mas não é
zero: dois leitores no celular na mesma noite, ou um duplo clique no botão antes do rerun
completar. O custo do erro é o mesmo do achado 2 — dois responsáveis pelo mesmo exemplar.

`delete_book` tem a variante mais desagradável: entre a checagem de empréstimo ativo e o
`DELETE FROM loans`, um empréstimo pode nascer — e ele seria apagado junto, sem aviso.

**Correção sugerida:** reutilizar o padrão de `_lock_unreconciled_book`. Para `request_loan`,
o caminho mais barato é `UPDATE books SET status='Emprestado' WHERE id=:id AND
status='Disponível'` e só inserir o loan se `rowcount == 1`.

**Esforço:** baixo — o padrão já existe e está testado. **Risco:** baixo.

---

## (b) Pode esperar

### 6. N+1 no `process_import_rows`, a cada rerun da tela — **Médio**

**Onde:** `app.py:880` (dentro do allocator) e `app.py:2707` (a chamada, fora de qualquer
botão).

**Medição (acervo sintético de 2.552 livros, contador de queries na engine):**

| Cenário | Tempo (SQLite local) | Queries |
|---|---|---|
| 500 linhas, categoria Literária | 0,04 s | **501** |
| 500 linhas, categoria Espiritual | 0,01 s | 2 |

Uma consulta `count_books_by_author` **por linha** do CSV. Local não dói; contra o Supabase
em São Paulo, com o app no Streamlit Cloud (o `AUDITORIA-PERFORMANCE.md` já documenta o
descasamento de região), 500 round trips a ~30 ms são ~15 s — e `process_import_rows` roda
**em todo rerun** da tela, inclusive ao mudar um `selectbox` de mapeamento. Para o acervo
inteiro de 2.552 linhas, são ~2.500 queries por interação.

O `AUDITORIA-PERFORMANCE.md` já lista isso como itens 11 e 12 (pendentes). A medição acima
mostra que o item 12 é pior do que o documento descreve: não é só reprocessamento, é N+1.

**Correção sugerida:** carregar `SELECT author, COUNT(*) FROM books GROUP BY author` uma vez
por lote no `BookCodeAllocator` (uma query em vez de N), e mover o `process_import_rows` para
trás do botão de confirmação, exibindo a pré-visualização a partir de `session_state`.

**Esforço:** médio. **Risco:** baixo, com a suíte de importação existente (que é boa) como rede.

---

### 7. Admin não tem como registrar um empréstimo de balcão — **Médio**

**Onde:** `app.py:1655` — `if user["role"] == "leitor" and r["status"] == "Disponível"`.

O botão "Pegar emprestado" só aparece para leitor. Um admin logado vê o catálogo sem
nenhuma ação. A Reconciliação (`app.py:2513`) só lista livros que já estão `Emprestado`.

Ou seja: o único jeito de o bibliotecário registrar um empréstimo no balcão é **editar o
status do livro para `Emprestado` na Gestão de Livros e depois ir na Reconciliação** — dois
passos, em duas telas, usando uma tela cujo nome diz que serve para consertar carga antiga.
E é exatamente o mesmo `UPDATE` que causa o achado 2.

**Por que importa.** Numa biblioteca comunitária, boa parte do público não vai se cadastrar
e operar o app sozinho. O fluxo de balcão é o fluxo principal, e hoje ele é um contorno.

**Correção sugerida:** um botão "Registrar empréstimo para…" no catálogo quando
`role == 'admin'`, com o mesmo seletor de leitor e as mesmas datas da Reconciliação,
chamando `request_loan` com a trava do achado 5. Fecha o achado 2 por tabela, porque tira a
razão de o admin editar o status na mão.

**Esforço:** médio (uma tela). **Risco:** baixo, é adição.

---

### 8. CSV em Latin-1/Windows-1252 — **Médio**

**Onde:** `app.py:1253-1256`

```python
try:
    text = data.decode("utf-8-sig")
except UnicodeDecodeError:
    text = data.decode("utf-8")   # nunca funciona: se utf-8-sig falhou, utf-8 falha igual
```

O `except` é **código morto**: `utf-8-sig` só difere do `utf-8` no BOM, então se o primeiro
falhou o segundo levanta a mesma exceção.

**Prova:** `"Memórias Póstumas".encode("latin-1")` → `UnicodeDecodeError: 'utf-8' codec
can't decode byte 0xf3 in position 16`.

`show_csv_import` captura (`app.py:2643`), então não vira traceback — mas a mensagem que o
usuário vê é `Não foi possível ler o arquivo CSV: 'utf-8' codec can't decode byte 0xf3 in
position 16: invalid continuation byte`. Não diz o que fazer.

**Por que importa.** Excel em português salvando "CSV (separado por vírgulas)" no Windows
gera Windows-1252, não UTF-8. É o caminho mais provável de um cliente exportar uma planilha.
E o acervo é cheio de acento, então a falha é garantida, não eventual.

**Correção sugerida:** cascata `utf-8-sig → cp1252 → latin-1`, e, se ainda assim falhar,
mensagem acionável ("salve o arquivo como CSV UTF-8 no Excel: Salvar como → CSV UTF-8").
Atenção: `cp1252` decodifica quase qualquer byte sem erro, então a ordem importa e o
`utf-8-sig` precisa vir primeiro.

**Esforço:** baixo. **Risco:** baixo.

---

### 9. `commit_import` grava sem revalidar — **Médio**

**Onde:** `app.py:1349`

O botão fica `disabled=bool(error_rows)`, mas a função em si percorre `processed_rows` e
insere tudo, sem olhar `row["erros"]` e sem revalidar o código contra o banco. A validação
mora inteiramente na tela; a função de escrita confia nela.

Também não há revalidação entre a pré-visualização e o clique. Se outro admin cadastrar um
livro nesse intervalo, o `UNIQUE` do banco pega — mas cai no achado 3 (traceback, sem
rollback), e a importação inteira é perdida sem indicar qual linha causou.

Vale registrar o que está certo: é uma transação única com um `commit` no fim, então não
existe importação pela metade.

**Correção sugerida:** `commit_import` pula (ou recusa) linhas com `erros`, e o `except
IntegrityError` reporta qual código colidiu.

**Esforço:** baixo. **Risco:** baixo.

---

### 10. Enumeração de contas — **Médio**

**Onde:** `app.py:1515` (cadastro público), `app.py:446`, `app.py:2402`; e `authenticate`
(`app.py:461`).

A aba pública "Cadastrar-se" responde **"Já existe um cadastro com esse e-mail."** — um
oráculo direto: qualquer pessoa na internet testa um e-mail e descobre se ele é leitor da
biblioteca. O `authenticate` também vaza por tempo de resposta: quando o e-mail não existe,
ele retorna sem executar o `bcrypt.checkpw` (dezenas de ms de diferença, medível).

Isso contrasta com o cuidado real que existe no login: a tabela `login_attempts` foi
projetada justamente para *não* distinguir e-mail inexistente de senha errada
(`app.py:161-167`). A proteção está no lugar certo e é contornada pela tela ao lado.

**Por que importa.** A base é de dados pessoais de uma comunidade local. Confirmar que
"fulano@gmail.com está cadastrado na biblioteca do CCE" é vazamento de PII, mesmo sem senha.

**Correção sugerida:** no cadastro público, responder sempre com a mesma mensagem neutra
("Se este e-mail ainda não estiver cadastrado, a conta foi criada. Tente entrar."). Manter a
mensagem explícita apenas no formulário de novo admin (`app.py:2402`), que é autenticado.
Para o timing, executar um `bcrypt.checkpw` contra um hash fixo quando o usuário não existe.

**Esforço:** baixo. **Risco:** baixo, mas piora um pouco a UX do cadastro — vale decidir se
o trade-off compensa no seu contexto.

---

### 11. Nenhum índice; busca cara por construção — **Médio**

Confirmado programaticamente: `metadata` não declara **nenhum** `Index`. Existem só os
implícitos de PK e de `UNIQUE` (`books.code`, `users.email`).

Sem índice: `loans.book_id`, `loans.user_id`, `loans.status`, `books.status`, `books.title`,
`books.author`. Toda tela de empréstimo e a subquery `EXISTS` da Reconciliação fazem varredura.

Além disso, `_sql_unaccent` (`app.py:992`) envolve cada coluna em **48 `REPLACE` aninhados**
(medido), aplicados a 4 colunas em cada busca — ~490 mil chamadas de `REPLACE` por busca no
acervo de 2.552 livros, e o resultado é intrinsecamente não indexável.

A decisão em si é bem fundamentada e está documentada no código (evita `CREATE EXTENSION
unaccent`, que o Supabase não habilita por padrão, e mantém paridade com o SQLite dos
testes) — não é um erro. Mas o custo cresce linearmente e não tem saída por índice.

**Correção sugerida:** os índices são triviais e seguros (`CREATE INDEX CONCURRENTLY` no
Supabase). O `unaccent` só vale revisitar se a busca começar a incomodar — e aí a saída
seria uma coluna gerada `title_norm`/`author_norm` com índice `pg_trgm`, o que só funciona
no Postgres e quebraria a paridade com os testes.

**Esforço:** baixo (índices) / alto (busca). **Risco:** baixo / médio.

---

### 12. Histórico completo sem paginação — **Médio**

**Onde:** `app.py:2126-2224`

Carrega **todos** os empréstimos com nome, e-mail e telefone, monta os `selectbox` de filtro
com a lista inteira de livros e usuários, e filtra em Python (`app.py:2196-2207`). O
`AUDITORIA-PERFORMANCE.md` já registra isso como item 10.

O resto do app foi paginado com cuidado (`_paginate`, `list_books`, `list_users`,
`list_unreconciled_books`) — esta tela é a exceção, provavelmente por ser mais antiga.

Hoje, com poucos empréstimos, não dói. Vai doer proporcionalmente ao uso, e é a tela que
carrega mais PII de uma vez.

**Esforço:** médio. **Risco:** baixo.

---

### 13. Não existe exclusão nem anonimização de leitor — **Médio**

O enunciado assume que esse fluxo existe. Ele não existe. Não há caminho na UI para remover
ou anonimizar um leitor, e `ANONYMIZED_BORROWER_LABEL` (`app.py:1914`) é uma defesa para um
caminho que, na prática, **não é alcançável**: `loans.user_id` tem FK para `users.id` sem
`ON DELETE`, então o Postgres recusa a exclusão de qualquer leitor com histórico.

**Prova:** `DELETE FROM users WHERE id = ...` → `IntegrityError: FOREIGN KEY constraint failed`.

Como consequência, o `LEFT JOIN` de `export_loans_csv` (`app.py:2003`) e o teste
`test_export_loans_csv_nao_vaza_dados_de_leitor_removido` (`test_app.py:2460`) validam um
cenário que a FK impede de acontecer — o teste passa, mas está exercitando um caminho morto.
Vale notar também a inconsistência: a exportação usa `LEFT JOIN`, enquanto
`list_active_loans` (`app.py:2049`) e `show_admin_loan_history` (`app.py:2129`) usam `JOIN`;
se um dia houver anonimização, os empréstimos sumiriam dessas telas.

**Por que importa.** LGPD, art. 18: o titular pode pedir eliminação. Hoje a resposta é "não
dá pela aplicação, e no banco a FK bloqueia". Não é urgente para um app comunitário, mas é
uma pendência real que o app trata como se já estivesse resolvida.

**Correção sugerida (quando for a hora):** anonimizar em vez de excluir — substituir
`full_name`/`email`/`phone` por marcadores, preservando o `user_id` e o histórico. Isso
mantém a FK intacta e é a única forma compatível com "não podemos apagar o registro de que o
livro esteve emprestado".

**Esforço:** alto (regra + tela + auditoria + testes). **Risco:** médio.

---

### 14. Lockout como vetor de negação de acesso — **Médio**

`_register_failed_login` (`app.py:752`) conta por e-mail e escala até 60 min. Quem sabe o
e-mail do administrador (que, hoje, está no código público — achado 1) pode mantê-lo
travado indefinidamente com 5 tentativas erradas a cada hora.

É um trade-off consciente e bem documentado no código, e a alternativa (contar por IP) tem
problemas próprios no Streamlit Cloud, onde o IP real nem sempre está acessível. Mas o
combo com o achado 1 merece atenção: trocar o e-mail padrão do admin já reduz muito.

**Esforço:** médio. **Risco:** médio. **Mitigação barata:** ao trocar a credencial do
bootstrap (achado 1), usar também um e-mail que não seja adivinhável.

---

### 15. Telas estreitas — **Médio**

O sistema será usado no celular, e há pontos que não sobrevivem bem a isso:

- `app.py:2667` — `st.columns(len(IMPORT_FIELDS))` = **5 colunas** de `selectbox` lado a
  lado no mapeamento de importação. É a tela mais apertada do app.
- `app.py:2295` e `2301` — duas fileiras de 4 `st.metric` no Painel.
- `app.py:2215` (histórico, 8 colunas) e `app.py:2500` (auditoria) — `st.dataframe` largo,
  que vira rolagem horizontal.
- `st.columns([5, 2])` nos cards de catálogo, empréstimos e meus empréstimos: a coluna de
  ação fica com ~28% da largura e o botão quebra em várias linhas.
- `layout="wide"` (`app.py:2836`) mais o menu na sidebar: no celular a sidebar começa
  recolhida, então navegar exige abrir o menu a cada troca de tela.

O CSS injetado (`app.py:2820`) só ajusta `gap` abaixo de 640px — ajuda no espaçamento, não
no número de colunas.

**Correção sugerida:** o teste real é abrir o app no celular e percorrer os 3 fluxos que a
comunidade vai usar (buscar no catálogo, pegar emprestado, ver meus empréstimos). Onde
doer, trocar `st.columns` por empilhamento vertical — a importação de CSV, que é operação de
admin em desktop, pode ficar como está.

**Esforço:** médio. **Risco:** baixo (só apresentação).

---

### 16-19. Baixos

- **16 — `full_name` como markdown (`app.py:2489`, `2440`).** Qualquer pessoa se
  auto-cadastra escolhendo o próprio nome, e ele é renderizado com markdown nos rótulos da
  Gestão de Usuários. Não há XSS (`unsafe_allow_html` não é usado aí), mas dá para injetar
  um link clicável na tela do admin. Correção: escapar `*_[]()` ao exibir.
- **17 — senha temporária em `session_state` (`app.py:2356`).** Fica visível no topo da
  Gestão de Usuários até o admin clicar em "Já anotei". Se ele navegar para outra tela e
  voltar, a senha reaparece. Correção: expirar por tempo, ou limpar ao trocar de página.
- **18 — formato de `loan_date` inconsistente.** `request_loan` (`app.py:1425`) grava
  datetime ISO completo; `reconcile_register_loan` (`app.py:1867`) grava só a data quando
  `loan_date` é informado. `ORDER BY` lexicográfico e `datetime.fromisoformat` toleram os
  dois hoje, mas é uma armadilha para qualquer comparação futura.
- **19 — regras duplicadas.** `BOOK_STATUSES` (`app.py:1564`) vs `VALID_BOOK_STATUSES`
  (`app.py:1084`) vs o `CheckConstraint` (`app.py:189`) — três listas dos mesmos status.
  `_books_where_clauses` (`app.py:1014`) e `_unreconciled_where` (`app.py:1783`) repetem a
  mesma tupla de colunas pesquisáveis. `_due_date_caption` (`app.py:2076`) e `_situacao`
  (`app.py:2204`) formatam atraso de dois jeitos. Nada quebrado hoje; é onde a divergência
  vai nascer.

---

## (c) Intencional ou não vale mexer

**Dividir o `app.py`.** Minha recomendação honesta: **não agora.** O arquivo tem 2.872
linhas, mas é navegável — as seções têm banners consistentes, as funções puras estão
separadas do acesso a dados, e há docstrings explicando o *porquê* das decisões (raro e
valioso). O custo real da divisão não é mover código: é que `test_app.py` importa ~60
símbolos de `app`, os testes de UI usam `AppTest.from_file("app.py")`, e `st.cache_resource`
em `_build_engine` / `_ensure_initialized` tem semântica sensível a como o módulo é
carregado. É um refactor que toca tudo e não corrige nenhum dos achados acima.

Se e quando fizer sentido (digamos, além de 4.000 linhas), o corte natural seria — e as
fronteiras já existem no arquivo, marcadas pelos banners:

| Módulo | Conteúdo | Linhas atuais |
|---|---|---|
| `domain/codes.py` | `generate_book_code`, `_normalize_key`, `BookCodeAllocator` | 61-118, 793-890 |
| `db.py` | tabelas, engine, migrações, `_ensure_initialized` | 120-398 |
| `auth.py` | usuários, senhas, rate limit, auditoria, sessão | 400-792 |
| `books.py` | CRUD, busca, filtros, paginação | 793-1079 |
| `importer.py` | CSV: parsing, mapeamento, processamento | 1080-1370 |
| `loans.py` | empréstimos, prazos, reconciliação | 1371-1458, 1778-1905 |
| `ui/*.py` | as 15 funções `show_*` | 1459-1777, 1906-2789 |
| `app.py` | só `main()` + roteamento | ~80 linhas |

Faça-o em uma sessão dedicada, com a suíte verde antes e depois, e não misturado com
correção de bug. **Mas só depois dos itens (a).**

**O que é decisão consciente e está bem resolvido:**

- **`st.cache_resource` no engine e no `_ensure_initialized`** — a docstring explica
  corretamente por que `lru_cache` não serve (o Streamlit executa cada rerun em um módulo
  novo). O aviso de que `Connection` jamais pode ser cacheada está certo e é importante.
- **`_sql_unaccent` com `REPLACE` em vez de `unaccent()`** — a justificativa (paridade
  Postgres/SQLite sem extensão) é sólida. Ver achado 11 para o custo.
- **Ausência de recuperação de senha por e-mail** — decisão documentada; para uma biblioteca
  comunitária presencial, o reset feito pelo admin com senha temporária é adequado, e a
  implementação (`admin_reset_password`, `app.py:580`) é uma das melhores partes do código.
- **`due_date` anulável** — empréstimos legados sem prazo nunca contam como atraso.
  Consistente entre SQL e Python.
- **Datas como `TEXT` ISO** — não é o que eu escolheria do zero, mas está justificado
  (ordenação lexicográfica correta nos dois bancos) e é consistente. Migrar para `DATE` teria
  custo alto e ganho pequeno. Ver achado 18 para a inconsistência que sobrou.
- **Bootstrap que recria o admin quando não há nenhum** — a lógica está certa (`ae2c915`
  corrigiu bem o caso de "leitores sem admin"). O problema é só a credencial literal.
- **`.gitignore` cobrindo `.streamlit/secrets.toml`** — verificado: o arquivo real nunca foi
  versionado (`git log --all -- .streamlit/secrets.toml` volta vazio). O
  `secrets.toml.example` só tem placeholders.

**20 — código e arquivos mortos (cosmético, mas vale a limpeza):**
- `bookCode.ts` + `bookCode.test.ts` — TypeScript da fase de protótipo, duplicando
  `generate_book_code` em outra linguagem, sem tocar desde o primeiro commit (`f32a408`).
  A regra do código do livro agora vive em dois idiomas, e só um está em produção. Apagar.
- `files.zip:Zone.Identifier` e `assets/logo CCE.png:Zone.Identifier` — lixo do Windows/WSL
  versionado.
- `.claude/settings.local.json` — configuração local de máquina, versionada.
- `statuses = BOOK_STATUSES` (`app.py:1692`) — alias sem propósito.
- `ANONYMIZED_BORROWER_LABEL` e o `LEFT JOIN` que o usa — ver achado 13.
- `AUDITORIA-PERFORMANCE.md` — continua útil, mas o cabeçalho ("2.070 linhas") está
  desatualizado. Vale um carimbo de data.

**21 — categoria: `selectbox` no cadastro (`app.py:1673`), `text_input` livre na edição
(`app.py:1710`).** Como a categoria decide a estratégia do código (`get_code_strategy`,
`app.py:808`), um "Espiritual " com espaço ou "espiritual" minúsculo passa pelo
`_normalize_key` na hora de decidir a estratégia — mas entra literal no `list_book_categories`
e polui o filtro do catálogo com variantes da mesma categoria. Trocar por `selectbox` é
trivial; a ressalva é que o acervo real pode ter categorias legadas fora de `BOOK_CATEGORIES`
que precisariam de uma opção "outra".

---

## O que revisei e considerei correto

**Segurança**
- **SQL injection: nenhuma ocorrência.** Todas as 55 chamadas a `text()` usam parâmetros
  vinculados; nada é montado por concatenação. As partes dinâmicas (`_books_where_clauses`,
  `_users_where_clauses`, `_unreconciled_where`) usam SQLAlchemy Core, não strings.
- **Segredos:** `DATABASE_URL` só via `st.secrets`; nunca aparece em log ou mensagem de erro.
  `.streamlit/secrets.toml` está no `.gitignore` e nunca foi versionado (verificado no
  histórico completo). A única credencial literal é o achado 1.
- **Hashing:** bcrypt com salt embutido, migração transparente do sha256+salt legado no
  primeiro login bem-sucedido (`app.py:461-480`), `hmac.compare_digest` na verificação
  legada. Bem feito.
- **Autorização:** verificada com atenção, e **está correta**. `show_app` (`app.py:2740`) usa
  `session_state`, mas `_session_is_current` (`app.py:626`) consulta o banco *a cada rerun*
  em `main()` e derruba a sessão se o papel ou o `session_version` divergirem. Como toda ação
  passa por um rerun, não há janela para um admin rebaixado continuar agindo como admin. E
  `session_state` no Streamlit é server-side, ligado ao websocket — não é manipulável pelo
  cliente. É melhor do que a maioria dos apps Streamlit faz.
- **`unsafe_allow_html`:** dois usos (`app.py:1613`, `2822`), ambos com conteúdo estático ou
  só inteiros interpolados. Sem vetor de XSS.
- **`_session_user_view`** (`app.py:482`) exclui `password_hash` e `salt` do `session_state`
  — cuidado deliberado e correto.
- **`list_users`** (`app.py:680`) seleciona colunas explicitamente, nunca `SELECT *`.

**Correção**
- **`delete_book` é atômico** e tem teste de rollback (`test_app.py:1052`) que prova que as
  duas exclusões desfazem juntas.
- **`admin_reset_password`** (`app.py:580`) faz as cinco coisas numa transação só (hash,
  `must_change_password`, `session_version`, limpeza do lockout, auditoria), com teste de
  durabilidade conjunta (`test_app.py:3051`).
- **Reconciliação** é o melhor código do arquivo: `with_for_update()`, revalidação no momento
  da escrita, mensagens de conflito específicas, e teste com duas conexões reais
  simultâneas (`test_app.py:2189`).
- **`request_loan` revalida** o status com `SELECT` fresco, sem confiar no que a tela
  carregou. Falta só a trava (achado 5).
- **`BookCodeAllocator`** acumula corretamente dentro do lote e contra o banco, e preserva
  códigos legados fora de padrão. Bem testado.
- **`_detect_csv_delimiter`** (`app.py:1190`) é sólido: usa `csv.reader` (respeita campos
  citados e multilinha), pontua por nº de colunas + consistência, e falha com mensagem clara.
  A docstring documenta o bug real que motivou a reescrita.
- **`get_dashboard_metrics`** (`app.py:1917`) calcula tudo com `COUNT`/`GROUP BY` no banco.
- **`loan_summary_for_books`** (`app.py:924`) resolve o N+1 da Gestão de Livros em uma query,
  com teste que conta queries de verdade (`test_app.py:2566`).
- **`try_create_account`** trata `IntegrityError` corretamente para a corrida entre a
  checagem e o `INSERT`, com teste (`test_app.py:909`).
- **Migrações** (`app.py:282-340`) são idempotentes, inspecionam antes de alterar e não
  deslogam ninguém. Testadas individualmente.

**Testes** — 235 testes, todos passando em 41 s. A suíte é melhor do que o normal:
- Usa a **camada SQLAlchemy real** contra SQLite descartável, não mocks. A lógica de negócio
  é exercitada de verdade.
- **Não é curto-circuitada por cache:** `app._ensure_initialized.clear()` aparece em 5
  testes, com comentário explicando exatamente esse risco (`test_app.py:596`). Você já pensou
  nisso.
- Usa **`AppTest` de verdade** (8 testes) para fluxos ponta a ponta: login bloqueado até
  trocar a senha, sessão caindo em outra aba, reset pela tela real, upload de CSV.
- Cobre casos de borda dos dados reais: 12 colunas com BOM e CRLF sem aspas (o bug real do
  `espiritual.csv`), códigos legados fora de padrão, acentuação na busca, sequencial acima de
  999, paginação com acervo de tamanho realista.
- As asserções são específicas (valores esperados, contagens de query), não `assert result`.

**Lacunas reais da suíte** (por ordem de importância):
1. **Nenhum teste de concorrência fora da Reconciliação.** O padrão de duas conexões
   (`test_app.py:2189`) existe e funciona — replicá-lo para `request_loan` provaria o
   achado 5 e serviria de rede para a correção.
2. **Nenhuma das funções `show_*` de escrita é testada via `AppTest`** — catálogo/pegar
   emprestado, gestão de livros/salvar e remover, empréstimos/devolver. É exatamente onde
   ficam os achados 2, 3 e 4. Um teste que afirmasse `assert not at.exception` depois de
   cadastrar um livro pegaria o achado 4.
3. **Encoding não-UTF-8 não é testado** — achado 8.
4. **O invariante livro↔empréstimo não é testado como invariante.** Há testes de cada
   operação isolada, mas nenhum verifica "todo livro `Emprestado` tem exatamente 1 loan ativo,
   e nenhum livro não-`Emprestado` tem loan ativo" após uma sequência de operações.
5. **`test_export_loans_csv_nao_vaza_dados_de_leitor_removido`** (`test_app.py:2460`) passa
   testando um caminho que a FK impede (achado 13) — o único teste que encontrei exercitando
   código morto.
6. **Volume:** o maior acervo testado é de dezenas de livros. Nada exercita 2.552 livros nem
   um CSV de milhares de linhas — que é onde o achado 6 aparece.

---

## Ordem sugerida (impacto ÷ esforço)

1. **Hoje, sem deploy:** trocar a senha do admin em produção e confirmar que
   `admin@biblioteca.org` não usa mais a senha do código (achado 1).
2. **Hoje, sem deploy:** rodar a consulta do achado 2 no Supabase para saber se já existe
   livro com estado inconsistente.
3. **Primeiro PR (tudo baixo esforço / baixo risco):** achados 1 (código), 3, 5, 2, 8.
   Fecham o crítico e três dos quatro altos, sem tocar em regra de negócio nova.
4. **Segundo PR:** achado 4 (código do livro — precisa de validação contra o acervo real) e
   achado 9.
5. **Terceiro PR:** achado 7 (empréstimo de balcão) — o de maior valor para o cliente, e que
   fecha o achado 2 pela origem.
6. **Depois:** 6, 11 (índices), 12, 15.
7. **Quando houver folga:** 13, 14, 10, e a limpeza do 20.
8. **Divisão do `app.py`:** só depois de tudo acima, em sessão dedicada.
