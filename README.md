# Biblioteca Comunitária — Protótipo (Streamlit)

Protótipo funcional do sistema de biblioteca comunitária: autenticação com
controle de acesso por papel (admin/leitor), catálogo de livros, fluxo de
empréstimo/devolução, gestão de livros (CRUD), importação em lote via CSV e
painéis administrativos de empréstimos.

Stack: **Python + Streamlit + Postgres (Supabase)**, acessado via
**SQLAlchemy**, tudo em um único arquivo (`app.py`).

> ⚠️ Este é um protótipo para validar as regras de negócio. O hash de senha é
> básico (SHA-256 + salt) e não há proteção contra força bruta, CSRF etc. —
> não use como está em produção.

---

## Instalação

1. Crie e ative um ambiente virtual:

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
```

2. Instale as dependências:

```bash
pip install -r requirements.txt
```

## Configurando o banco (Supabase/Postgres)

A aplicação lê a connection string do banco em `st.secrets["DATABASE_URL"]`
— **nunca** fica hardcoded no código.

### 1. Crie um projeto no Supabase

Em [supabase.com](https://supabase.com), crie um projeto (grátis) e aguarde
o provisionamento do banco Postgres.

### 2. Pegue a connection string

No painel do projeto: **Project Settings → Database → Connection string**.
Duas opções são exibidas lá:

- **Connection pooling** (porta `6543`, modo *transaction*) — recomendada
  para este projeto, pois o Streamlit (local ou no Community Cloud) abre
  várias conexões curtas e o pooler evita esgotar o limite de conexões do
  Postgres.
- **Direct connection** (porta `5432`) — mais simples, mas menos adequada se
  o app tiver múltiplos usuários simultâneos.

Copie a string escolhida (ela já vem com usuário/host/porta preenchidos;
você só precisa colocar sua senha do banco no lugar de `[YOUR-PASSWORD]`).

### 3. Configure o secrets.toml local

```bash
mkdir -p .streamlit
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Edite `.streamlit/secrets.toml` e cole sua connection string real em
`DATABASE_URL`. Esse arquivo **nunca deve ser versionado** — já está listado
no `.gitignore`.

## Rodando a aplicação

```bash
streamlit run app.py
```

Na primeira execução, o schema (`users`, `books`, `loans`) é criado
automaticamente no Postgres, junto com o usuário administrador padrão. O
catálogo começa **vazio**: os livros entram pela carga real do acervo, seja
pelo cadastro manual (**Gestão de Livros**) ou pela **Importação via CSV**.

### Administrador padrão do primeiro boot

No primeiro boot — e sempre que o banco não tiver **nenhuma** conta de
administrador — o sistema cria automaticamente um administrador padrão, já
marcado com `must_change_password`: ele cai direto na tela **Alterar minha
senha** e não acessa nenhuma outra tela até definir uma senha nova.

As credenciais iniciais dessa conta estão **no código-fonte**, em
`_ensure_initialized` (`app.py`) — este README não as repete de propósito.
Quem faz o deploy consulta o código; quem só lê a documentação não recebe uma
senha pronta para usar.

> **Troque a senha logo no primeiro acesso.** A troca obrigatória fecha a
> maior parte do risco, mas ela só acontece quando alguém loga: entre um boot
> que recriou o admin e o primeiro login, a conta ainda está com a senha
> inicial. Faça esse login como primeira etapa do deploy, e depois cadastre um
> segundo administrador (ver
> [Proteção contra perda de acesso administrativo](#proteção-contra-perda-de-acesso-administrativo)).

Se a senha se perder depois disso, a recuperação está descrita em
[Recuperação de senha](#recuperação-de-senha).

### Banco local antigo (SQLite)

Versões anteriores deste protótipo usavam um arquivo `biblioteca.db`
(SQLite) local. Esse arquivo não é mais usado — a aplicação agora sempre lê
de `DATABASE_URL` (Postgres/Supabase). Se você tiver um `biblioteca.db`
antigo por aí, pode apagá-lo com segurança:

```bash
rm -f biblioteca.db
```

## Recuperação de senha

Não existe fluxo de "esqueci minha senha" por e-mail — ver
[Por que não há recuperação por e-mail](#por-que-não-há-recuperação-por-e-mail).
A recuperação é **presencial**: um administrador redefine a senha e entrega
uma senha temporária ao usuário.

### Redefinindo a senha de um usuário (tela **Gestão de Usuários**)

1. No menu do admin, abra **Gestão de Usuários** e localize a pessoa (busca
   por nome, e-mail ou telefone — sem acento também encontra).
2. Abra o registro dela e vá em **Redefinir senha**. Deixe o campo de senha em
   branco para o sistema gerar uma senha aleatória, ou digite uma senha
   provisória combinada na hora.
3. Marque a confirmação (o botão só habilita depois disso) e clique em
   **🔑 Redefinir senha**.
4. A senha temporária aparece **uma única vez** na tela. Anote ou entregue
   naquele momento: o banco guarda apenas o hash, então ela não pode ser
   consultada depois. Se você perder a senha antes de repassá-la, é só
   redefinir de novo.

O que acontece na conta redefinida:

| Efeito | Detalhe |
|---|---|
| Senha antiga deixa de valer | O hash é substituído na hora. |
| Troca obrigatória | A conta fica com `must_change_password = 1` e cai na tela **Alterar minha senha** no próximo login, sem acesso a nenhuma outra tela até concluir. |
| Sessão aberta é encerrada | `users.session_version` é incrementado; a sessão que a pessoa tiver aberta em outra aba cai na tela de login no clique seguinte, com aviso — a senha antiga não continua valendo em lugar nenhum. |
| Bloqueio por força bruta é liberado | Quem esqueceu a senha normalmente errou várias vezes antes de pedir ajuda; sem isso a senha temporária não funcionaria até o bloqueio expirar. |
| Auditoria | Fica registrado em `admin_audit_log` quem redefiniu a senha de quem e quando (visível no fim da própria tela, em **🗒️ Auditoria de redefinições de senha**). |

Um admin **não** redefine a própria senha por essa tela — isso encerraria a
sessão dele no mesmo instante. Para a própria senha, use **Alterar minha
senha** no menu.

## Proteção contra perda de acesso administrativo

Um admin pode redefinir a senha de outro admin — é assim que o acesso
administrativo se recupera dentro da aplicação. Mas isso só funciona se
**existir mais de um administrador**: o bootstrap automático só recria a conta
`admin@biblioteca.org` quando **não há nenhum admin** no banco, então um único
admin com a senha perdida trava o sistema inteiro.

Por isso, enquanto houver só um administrador, a tela **Gestão de Usuários**
exibe um aviso recomendando cadastrar o segundo. Use o formulário
**➕ Cadastrar novo administrador** na mesma tela (a tela pública de cadastro
só cria leitores). A senha definida ali é provisória: o novo admin é obrigado
a trocá-la no primeiro login.

### Procedimento de emergência: nenhum admin acessível

Se ninguém mais consegue entrar como administrador, a recuperação é feita do
**ambiente local**, com a `DATABASE_URL` de produção em mãos.

> **Não adianta editar o `password_hash` direto no banco.** A coluna guarda um
> hash **bcrypt** — escrever a senha em texto puro ali não autentica ninguém, e
> um hash colado de outro lugar precisaria vir com o `salt` correspondente e
> ainda deixaria `must_change_password` e `session_version` inconsistentes. Use
> as funções do próprio app, que fazem tudo junto.

**Caso 1 — a conta admin existe, mas a senha se perdeu.** Rode a partir da raiz
do repositório, com o venv ativado:

```python
# recuperar_admin.py
from app import admin_reset_password, get_connection, get_user_by_email

DATABASE_URL = "postgresql://..."      # a mesma do .streamlit/secrets.toml
OPERADOR = {"id": None, "email": "recuperacao-local@cli"}   # aparece na auditoria

with get_connection(DATABASE_URL) as conn:
    admin = get_user_by_email(conn, "admin@biblioteca.org")
    print("senha temporária:", admin_reset_password(conn, OPERADOR, admin["id"]))
```

```bash
python recuperar_admin.py
```

Entre com a senha impressa; o app exige a troca no primeiro login. A operação
fica registrada na auditoria com o e-mail fictício do operador local, deixando
claro que foi uma recuperação por fora da aplicação.

**Caso 2 — não existe nenhuma conta admin no banco** (removida, migração,
anonimização). Basta subir o app: o bootstrap detecta a ausência de admin e
recria a conta padrão (credenciais em `_ensure_initialized`, no código-fonte),
já marcada para troca obrigatória. Para forçar isso sem subir a interface:

```bash
python -c "from app import init_db; init_db('postgresql://...')"
```

Se preferir não passar pelo padrão, crie o admin já com a senha desejada:

```python
# criar_admin.py
from app import get_connection, try_create_account

DATABASE_URL = "postgresql://..."

with get_connection(DATABASE_URL) as conn:
    print(try_create_account(
        conn, "Administrador", "admin@biblioteca.org", "",
        "TROQUE-POR-UMA-SENHA-FORTE", "admin", must_change_password=True,
    ))
```

### Por que não há recuperação por e-mail

Um fluxo de "esqueci minha senha" por link enviado ao e-mail foi
deliberadamente deixado de fora deste protótipo. Ele exigiria, para ser seguro
e não virar um vetor de ataque:

- um servidor SMTP (ou serviço transacional) configurado e com domínio
  verificado, mais o segredo correspondente no deploy;
- uma tabela de tokens de reset — de uso único, com expiração curta e
  guardados como hash, não em texto puro;
- proteção contra enumeração de contas: a resposta da tela precisa ser
  idêntica para e-mail cadastrado e não cadastrado, e o envio precisa de rate
  limit próprio, senão o formulário vira um oráculo de quem é usuário;
- tratamento de entrega (bounce, spam, caixa cheia) — sem isso o usuário fica
  sem senha e sem aviso.

Para a escala do Centro Cultural Esplanada, onde o atendimento já é presencial,
o reset feito pelo admin resolve o mesmo problema sem nenhuma dessas peças.
**Evolução futura:** se o app passar a atender pessoas remotas, o fluxo por
e-mail entra como complemento — não como substituto — do reset presencial, que
continua sendo o caminho de recuperação do acesso administrativo.

## Deploy no Streamlit Community Cloud

1. Suba o repositório (sem o `.streamlit/secrets.toml` — ele está no
   `.gitignore` e não deve ir para o Git) para o GitHub.
2. Em [share.streamlit.io](https://share.streamlit.io), crie um novo app
   apontando para `app.py` neste repositório.
3. No painel do app, vá em **Settings → Secrets** e cole o conteúdo no
   mesmo formato TOML do `secrets.toml.example`, substituindo pelos valores
   reais:

   ```toml
   DATABASE_URL = "postgresql://postgres.SEU_PROJETO:SUA_SENHA@aws-0-SUA_REGIAO.pooler.supabase.com:6543/postgres"
   ```

4. Salve — o app reinicia automaticamente e passa a usar o Postgres do
   Supabase em produção.

## Rodando os testes

```bash
pytest
```

Os testes usam **SQLite local descartável através da mesma camada
SQLAlchemy** usada em produção (um arquivo temporário por teste, limpo
automaticamente pelo pytest) — não dependem de acesso de rede ao Supabase.
Cobrem: a geração de código de livro (`generate_book_code`, mesmos casos de
borda do `bookCode.test.ts`), a estratégia de código por acervo
(`get_code_strategy`, `BookCodeAllocator`), a lógica de importação em lote via CSV
(`parse_csv_bytes`, `process_import_rows`), o mapeamento flexível de colunas
(`detect_column_mapping`, `apply_column_mapping`, `normalize_status`), a busca
com filtros e paginação no banco (`count_books`, `list_books`,
`list_book_categories`), o prazo de devolução e o controle de atraso
(`default_due_date`, `days_overdue`, `is_overdue`, `list_active_loans`), a
reconciliação de empréstimos, incluindo o conflito entre duas sessões
simultâneas (`list_unreconciled_books`, `reconcile_register_loan`,
`reconcile_mark_returned`), inicialização do schema e do admin padrão
(`init_db`), o fluxo de empréstimo/devolução e a
remoção de livros (`delete_book`).

## Código do livro por acervo

O CCE tem dois acervos físicos com convenções de código diferentes, e ambas
precisam ser preservadas exatamente como estão nas prateleiras. A categoria do
livro determina qual regra vale:

| Categoria | Estratégia | Formato | Exemplo |
|---|---|---|---|
| **Literária** (e qualquer outra categoria) | por autor | 3 primeiras letras do último token do nome + inicial do primeiro nome + sequencial de 3 dígitos daquele autor | `NETJ-001` |
| **Espiritual** | numérica | próximo inteiro livre da sequência da categoria (maior número existente + 1) | `461`, `1091` |

Detalhes:

- A escolha da estratégia fica em `get_code_strategy(categoria)`; a alocação
  (incluindo o acúmulo dentro de um lote de importação) fica em
  `BookCodeAllocator`. A comparação da categoria ignora acento, caixa e
  espaços, então `Espiritual`, `espiritual` e ` ESPIRITUAL ` são equivalentes.
- A sequência numérica **não preenche buracos**: se a categoria já tem `461` e
  `1091`, o próximo é `1092`, não `462`.
- Códigos não numéricos dentro da Espiritual (legados) são ignorados no
  cálculo do maior número, mas continuam válidos no acervo.
- No cadastro manual (**Gestão de Livros**), Categoria é um selectbox com os
  dois acervos, e o código gerado é exibido na confirmação.

### Códigos legados fora de padrão

A base real do CCE tem 13 códigos legados no acervo Literária que fogem do
formato (`BURE` e `CUNM` sem número, `Bord-001` em minúsculas, `GOMLI-001` e
`MACAL-001` com 5 letras, `MILJ-001 (a)` e `MILJ-001 (b)` com sufixo manual).
Eles são válidos e entram sem alteração: **a importação não valida formato de
código, apenas unicidade** — contra o banco e dentro do próprio arquivo.

## Importação de livros via CSV

Na tela **Importar CSV** (menu do administrador). O arquivo **não precisa vir
no formato interno** — exports de outras ferramentas (Memento Database, por
exemplo) são aceitos diretamente, porque você escolhe na tela qual coluna do
arquivo corresponde a cada campo.

O fluxo tem três passos: **upload → mapeamento de colunas → pré-visualização
e confirmação**.

### 1. Upload

Basta que o arquivo tenha cabeçalho na primeira linha. Delimitador `,` ou `;`
e encoding UTF-8 (com ou sem BOM) são detectados automaticamente.

### 2. Mapeamento de colunas

A tela lista as colunas encontradas no arquivo e, para cada campo interno,
oferece um selectbox com as colunas disponíveis mais a opção
`(não mapear / deixar vazio)`:

| Campo interno | Obrigatório | Quando não mapeado |
|---|---|---|
| `titulo` | **sim** | — (bloqueia o avanço) |
| `autor` | **sim** | — (bloqueia o avanço) |
| `categoria` | não | Fica vazia, ou recebe a *categoria fixa* (veja abaixo) |
| `codigo` | não | Código gerado automaticamente conforme a **estratégia da categoria** daquela linha (veja *Código do livro por acervo*), acumulando dentro do próprio lote |
| `status` | não | Assume `Disponível` |

**Detecção automática:** os selectboxes já vêm pré-selecionados comparando os
nomes das colunas de forma tolerante (sem acento, sem diferenciar maiúsculas
e ignorando espaços, hífens e `_`). Sinônimos reconhecidos:

| Campo | Sinônimos |
|---|---|
| `titulo` | titulo, título, title, nome, obra |
| `autor` | autor, author, escritor |
| `categoria` | categoria, category, acervo, colecao, coleção |
| `codigo` | codigo, código, código-antigo, code, cod, tombo, registro |
| `status` | status, situacao, situação |

Qualquer pré-seleção pode ser sobrescrita manualmente.

**Ambiguidade:** quando mais de uma coluna do arquivo é candidata ao mesmo
campo (o export real do CCE traz `Código-antigo` **e** `Código`, com
significados diferentes), o sistema **não escolhe sozinho** — exibe um aviso
listando as candidatas e deixa o campo sem pré-seleção, para você decidir.

**Categoria fixa:** quando nenhuma coluna de categoria é mapeada, aparece um
campo para informar uma categoria única aplicada a todas as linhas — útil
quando o arquivo inteiro pertence a um só acervo.

### 3. Pré-visualização e confirmação

Antes de gravar no banco, a tela mostra:

- **Resumo do lote**: total de registros, códigos mantidos, códigos gerados e
  linhas com erro.
- **Tabela de inconsistências** destacando erros bloqueantes: título ou autor
  vazios, status desconhecido, ou código duplicado (contra o banco ou entre
  linhas do próprio arquivo).
- O botão **Confirmar importação** fica desabilitado enquanto houver qualquer
  erro bloqueante no lote.

### Tratamento dos valores

- **Espaços sobrando** são removidos (`.strip()`) de `titulo`, `autor`,
  `categoria` e `codigo` — evita autores e categorias duplicados por uma
  diferença invisível.
- **Status** aceita variações comuns e normaliza para os valores internos:

  | Valor no arquivo | Vira |
  |---|---|
  | `Disponível`, `disponivel`, `available` | `Disponível` |
  | `Emprestado`, `emprestado`, `borrowed`, `on loan` | `Emprestado` |
  | `Em Manutenção`, `em manutencao`, `manutenção` | `Em Manutenção` |
  | vazio | `Disponível` |
  | qualquer outro | **erro bloqueante** na linha (nunca adivinhamos) |

- **Códigos legados são preservados como estão.** Não há validação de formato
  de código: a base legada do CCE tem códigos fora do padrão e eles devem ser
  importados exatamente como vieram.

Exemplo de export externo aceito (cabeçalhos diferentes, `;`, status em
inglês, espaços sobrando):

```csv
Nome;Escritor;Acervo;Código;Situação
  Dom Casmurro  ;  Machado de Assis ;Literatura;L-0001;available
Vidas Secas;Graciliano Ramos;Literatura;L-0002;on loan
```

Exemplo já no formato interno (detectado automaticamente, sem ajuste manual):

```csv
titulo,autor,categoria,codigo,status
Dom Casmurro,Machado de Assis,Literatura Brasileira,,
O Cortiço,Aluísio Azevedo,Literatura Brasileira,COR-A-001,Disponível
```

## Busca, filtros e paginação do acervo

As telas **Catálogo** e **Gestão de Livros** compartilham os mesmos controles:

- **Busca** por título, autor, código ou categoria.
- **Filtro por categoria**, com as opções carregadas dinamicamente do banco
  (`SELECT DISTINCT categoria`) mais a opção `Todas`.
- **Filtro por status**: `Disponível` / `Emprestado` / `Em Manutenção` / `Todos`.
- Busca e filtros **combinam** (AND entre eles) — o filtro não substitui a busca.
- **Paginação de 25 itens**, com navegação e indicação de
  `X–Y de N resultado(s) · página P de T`. Trocar a busca ou os filtros
  volta para a primeira página.

Tudo é resolvido no banco (`WHERE` + `LIKE`/`ILIKE` + `LIMIT`/`OFFSET`): só a
página exibida trafega, o que importa porque o acervo real tem ~2.552 livros e
a Gestão de Livros renderiza um `st.expander` por livro.

**Busca sem acento e sem diferenciar maiúsculas:** buscar `reflexoes` encontra
`Reflexões`, e `MEMORIAS` encontra `Memórias Póstumas`. Isso é feito com uma
cadeia de `REPLACE` aninhados sobre a coluna, e não com o `unaccent()` do
Postgres, por dois motivos: `unaccent()` exige `CREATE EXTENSION unaccent`
(não habilitada por padrão no Supabase) e não existe no SQLite usado pelos
testes. Os acentos são removidos em maiúsculas **e** minúsculas antes da
comparação, porque o `LOWER()` do SQLite é ASCII-only e não converteria `Ó`
em `ó` — sem isso a busca funcionaria em produção e falharia nos testes.

> Nota de escala: a cadeia de `REPLACE` impede o uso de índice na busca
> textual (varredura sequencial). Para ~2.5k livros isso é irrelevante, mas se
> o acervo crescer muito, o próximo passo é uma coluna gerada e indexada com o
> texto já normalizado, ou `pg_trgm` + `unaccent` no Postgres.

## Prazo de devolução e atrasos

- O prazo padrão fica na constante **`PRAZO_PADRAO_DIAS`** (hoje `14`) no
  `app.py` — mudou ali, mudou em todo o sistema.
- Ao registrar um empréstimo, a data prevista é calculada como
  *data do empréstimo + prazo padrão*, e pode ser **ajustada antes de
  confirmar** no popover "Pegar emprestado".
- **Empréstimos ativos** e **Histórico completo** mostram a data prevista,
  destacam os vencidos em vermelho e informam **há quantos dias** estão
  atrasados. Empréstimos ativos tem ainda o filtro **"Somente atrasados"**.
- Vencer **exatamente hoje ainda não é atraso** — a contagem começa no dia
  seguinte.
- Empréstimos com **prazo nulo nunca contam como atrasados** (é o caso dos
  registros anteriores a este recurso).

### Migração da coluna `due_date`

`loans.due_date` é adicionada automaticamente por `create_schema()` via
`ALTER TABLE ... ADD COLUMN` quando o banco já existe — `metadata.create_all()`
só cria tabelas que faltam, nunca colunas, então um banco já em uso (o Supabase
do CCE) precisa desse passo explícito. **Nenhum dado é perdido**: os
empréstimos existentes ficam com `due_date` nulo. A migração é idempotente e
funciona igual em Postgres e SQLite.

## Painel do administrador

Tela inicial do admin, com indicadores calculados por `COUNT`/`GROUP BY` no
banco (nenhuma tabela é carregada inteira em memória):

| Acervo | Empréstimos e leitores |
|---|---|
| Total de livros | Empréstimos ativos |
| Disponíveis | Atrasados |
| Emprestados | Leitores cadastrados |
| Em manutenção | Pendentes de reconciliação |

Alertas aparecem quando há empréstimos em atraso ou livros pendentes de
reconciliação, apontando para a tela correspondente.

### Exportação em CSV

O painel oferece dois downloads:

- **Catálogo de livros** — código, título, autor, categoria, status.
- **Histórico de empréstimos** — livro, código, leitor, e-mail, data do
  empréstimo, devolução prevista, data de devolução e status.

Os arquivos saem em **UTF-8 com BOM** e com **todos os campos entre aspas**
(`QUOTE_ALL`), para abrir direto no Excel em português: o BOM evita acentos
corrompidos e as aspas impedem que um título contendo `,` ou `;` quebre as
colunas.

> **Leitores removidos:** o sistema não tem (ainda) remoção nem anonimização
> de leitores. A exportação do histórico usa `LEFT JOIN` como proteção: se um
> empréstimo ficar órfão (usuário apagado direto no banco), a linha é
> preservada com o rótulo `Leitor removido` e e-mail vazio, em vez de sumir do
> relatório ou expor dados pessoais.

## Painéis administrativos de empréstimos

- **Empréstimos ativos**: cada linha mostra também o e-mail e telefone do
  leitor responsável, além do nome, para facilitar o contato.
- **Histórico completo** (admin): lista todos os empréstimos — ativos e
  devolvidos, de todos os usuários e livros — com filtros por livro, por
  usuário e por período (data de empréstimo). Ao selecionar um usuário
  específico no filtro, um painel abaixo mostra todo o histórico daquele
  usuário (livro, datas de empréstimo/devolução e status).

## Reconciliação de empréstimos

A carga inicial do acervo trouxe centenas de livros com status **Emprestado**
mas sem nenhum registro na tabela `loans` — a planilha de origem tinha o nome
de quem pegou o livro, mas essas pessoas não são usuárias do sistema. Esses
livros ficavam num limbo: indisponíveis no catálogo, porém ausentes de
"Empréstimos ativos", sem ninguém saber quem está com eles.

A tela **Reconciliação** (menu do admin) lista exatamente esses casos — livros
com status `Emprestado` **e** sem nenhum empréstimo ativo registrado — com a
contagem total pendente, busca (mesma busca sem acento do resto do sistema) e
paginação de 25 itens. Para cada livro, duas ações:

| Ação | O que faz |
|---|---|
| **📝 Registrar empréstimo** | Escolhe um leitor cadastrado, a data do empréstimo e a devolução prevista (prazo padrão pré-preenchido), e cria o empréstimo ativo que faltava. O livro **continua `Emprestado`** — ele segue fisicamente com o leitor; o que muda é que agora existe registro de quem está com ele, e ele passa a aparecer em "Empréstimos ativos". |
| **✅ Marcar como devolvido** | O livro já voltou fisicamente: muda o status para `Disponível` e **não cria registro de empréstimo** (não sabemos quem estava com ele, e inventar histórico seria pior que não ter). |

As duas ações rodam em transação e **revalidam o estado do livro no momento da
execução**, não no momento em que a tela foi carregada. Se outro admin agir
sobre o mesmo livro em paralelo, a segunda confirmação falha com mensagem
explicativa em vez de duplicar o empréstimo ou sobrescrever a ação anterior. No
Postgres a revalidação usa `SELECT ... FOR UPDATE` (trava a linha); no SQLite o
`FOR UPDATE` é ignorado pelo dialeto e a própria transação serializa a escrita.
