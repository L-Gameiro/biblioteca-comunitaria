# Biblioteca Comunitária — Protótipo (Streamlit)

Protótipo funcional do sistema de biblioteca comunitária: autenticação com
controle de acesso por papel (admin/leitor), catálogo de livros, fluxo de
empréstimo/devolução, gestão de livros (CRUD) e importação em lote via CSV.

Stack: **Python + Streamlit + SQLite**, tudo em um único arquivo (`app.py`).

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

## Rodando a aplicação

```bash
streamlit run app.py
```

O SQLite (`biblioteca.db`) é criado automaticamente na primeira execução, já
com o schema atual e uma carga inicial de livros de exemplo.

### Credenciais do administrador padrão

| Campo | Valor |
|---|---|
| E-mail | `admin@biblioteca.org` |
| Senha  | `admin123` |

### Banco de dados já existente com schema antigo

Se você já tinha um `biblioteca.db` de uma versão anterior deste protótipo
(quando o cadastro de leitor ainda coletava CPF), **apague o arquivo
`biblioteca.db`** antes de rodar novamente — ele será recriado do zero com o
schema atual (sem a coluna de CPF). Como é um protótipo, não há script de
migration; basta:

```bash
rm biblioteca.db
streamlit run app.py
```

## Rodando os testes

```bash
pytest
```

Cobre a geração de código de livro (`generate_book_code`, mesmos casos de
borda do `bookCode.test.ts`) e a lógica de importação em lote via CSV
(`parse_csv_bytes`, `process_import_rows`).

## Importação de livros via CSV

Na tela **Importar CSV** (menu do administrador), envie um arquivo com
cabeçalho na primeira linha e as colunas:

| Coluna | Obrigatória | Descrição |
|---|---|---|
| `titulo` | sim | Título do livro |
| `autor` | sim | Nome completo do autor |
| `categoria` | não | Categoria/gênero |
| `codigo` | não | Se informado, é usado exatamente como está (deve ser único). Se vazio, é gerado automaticamente pela mesma regra de `generate_book_code`, considerando os livros do mesmo autor já no banco somados aos já processados em linhas anteriores do arquivo. |
| `status` | não | Um de `Disponível`, `Emprestado`, `Em Manutenção`. Se vazio, assume `Disponível`. |

- Delimitador `,` ou `;` e encoding UTF-8 (com ou sem BOM) são detectados
  automaticamente.
- Antes de gravar no banco, a tela mostra uma **pré-visualização** com o
  resumo do lote (total de registros, códigos mantidos, códigos gerados,
  linhas com erro) e destaca linhas com erros bloqueantes: título ou autor
  vazios, status inválido, ou código duplicado (contra o banco ou entre
  linhas do próprio arquivo).
- O botão **Confirmar importação** fica desabilitado enquanto houver
  qualquer erro bloqueante no lote.

Exemplo de CSV válido:

```csv
titulo,autor,categoria,codigo,status
Dom Casmurro,Machado de Assis,Literatura Brasileira,,
O Cortiço,Aluísio Azevedo,Literatura Brasileira,COR-A-001,Disponível
```
