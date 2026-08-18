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
automaticamente no Postgres, junto com o usuário administrador padrão e uma
carga inicial de livros de exemplo.

### Credenciais do administrador padrão

| Campo | Valor |
|---|---|
| E-mail | `admin@biblioteca.org` |
| Senha  | `admin123` |

### Banco local antigo (SQLite)

Versões anteriores deste protótipo usavam um arquivo `biblioteca.db`
(SQLite) local. Esse arquivo não é mais usado — a aplicação agora sempre lê
de `DATABASE_URL` (Postgres/Supabase). Se você tiver um `biblioteca.db`
antigo por aí, pode apagá-lo com segurança:

```bash
rm -f biblioteca.db
```

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
borda do `bookCode.test.ts`), a lógica de importação em lote via CSV
(`parse_csv_bytes`, `process_import_rows`), o mapeamento flexível de colunas
(`detect_column_mapping`, `apply_column_mapping`, `normalize_status`),
inicialização do schema/seed (`init_db`), o fluxo de empréstimo/devolução e a
remoção de livros (`delete_book`).

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
| `codigo` | não | Código gerado automaticamente por `generate_book_code`, considerando os livros do mesmo autor já no banco somados aos já processados em linhas anteriores do arquivo |
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

## Painéis administrativos de empréstimos

- **Empréstimos ativos**: cada linha mostra também o e-mail e telefone do
  leitor responsável, além do nome, para facilitar o contato.
- **Histórico completo** (admin): lista todos os empréstimos — ativos e
  devolvidos, de todos os usuários e livros — com filtros por livro, por
  usuário e por período (data de empréstimo). Ao selecionar um usuário
  específico no filtro, um painel abaixo mostra todo o histórico daquele
  usuário (livro, datas de empréstimo/devolução e status).
