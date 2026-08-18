"""
app.py — Protótipo funcional: Sistema de Biblioteca Comunitária
=================================================================
Stack: Python + Streamlit + Postgres (Supabase) via SQLAlchemy (lógica de
negócio e UI num único arquivo). Ver README.md para instruções completas.

Como rodar:
    pip install -r requirements.txt
    cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # preencha DATABASE_URL
    streamlit run app.py

Login de administrador criado automaticamente na 1ª execução:
    e-mail:  admin@biblioteca.org
    senha:   admin123

⚠️ Este é um PROTÓTIPO para validar as regras de negócio (RBAC simples,
geração de código do livro, fluxo de empréstimo/devolução). O hashing de
senha aqui é básico (sha256 + salt) e não há proteção contra força bruta,
CSRF, etc. — não usar como está em produção real; a versão de produção
(Next.js) deve usar um provedor de auth de verdade (NextAuth/Supabase Auth).
"""

import csv
import hashlib
import io
import os
import re
import unicodedata
from datetime import datetime
from functools import lru_cache

import streamlit as st
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError


# ---------------------------------------------------------------------------
# Algoritmo de geração de código do livro (mesma regra do bookCode.ts)
# ---------------------------------------------------------------------------

GENERATIONAL_SUFFIXES = {"NETO", "FILHO", "JUNIOR", "JR", "SOBRINHO"}


def _strip_diacritics(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _only_upper_letters(value: str) -> str:
    return re.sub(r"[^A-Z]", "", _strip_diacritics(value).upper())


def _normalize_key(value: str) -> str:
    """Chave de comparação tolerante: sem acento, minúscula e sem
    separadores — 'Código-antigo', 'codigo antigo' e 'CODIGO_ANTIGO'
    viram todos 'codigoantigo'; 'Espiritual' e ' espiritual ' viram
    'espiritual'."""
    return re.sub(r"[^a-z0-9]", "", _strip_diacritics(value or "").lower())


def generate_book_code(
    author_full_name: str,
    existing_count_for_author: int,
    treat_suffix_as_surname: bool = True,
) -> str:
    """
    [3 primeiras letras do último token do nome, maiúsculas]
    + [1ª letra do primeiro nome, maiúscula] - [sequencial de 3 dígitos]

    Ex.: "João Mellão Neto", 1º livro -> "NETJ-001"
    """
    if not author_full_name or not author_full_name.strip():
        raise ValueError("Nome do autor não pode ser vazio.")
    if existing_count_for_author < 0:
        raise ValueError("existing_count_for_author deve ser >= 0.")

    parts = author_full_name.strip().split()
    first_name = parts[0]
    surname_token = parts[-1]

    if not treat_suffix_as_surname and len(parts) > 1:
        if _only_upper_letters(surname_token) in GENERATIONAL_SUFFIXES:
            surname_token = parts[-2]

    surname_code = _only_upper_letters(surname_token)
    if len(surname_code) < 3:
        surname_code = surname_code.ljust(3, "X")
    surname_code = surname_code[:3]

    first_initial = _only_upper_letters(first_name)[:1] or "X"
    sequence = str(existing_count_for_author + 1).zfill(3)

    return f"{surname_code}{first_initial}-{sequence}"


# ---------------------------------------------------------------------------
# Banco de dados (Postgres/Supabase via SQLAlchemy; SQLite local para testes)
# ---------------------------------------------------------------------------

metadata = MetaData()

users_table = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("full_name", Text, nullable=False),
    Column("email", Text, nullable=False, unique=True),
    Column("phone", Text),
    Column("password_hash", Text, nullable=False),
    Column("salt", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    CheckConstraint("role IN ('admin','leitor')", name="ck_users_role"),
)

books_table = Table(
    "books",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("code", Text, nullable=False, unique=True),
    Column("title", Text, nullable=False),
    Column("author", Text, nullable=False),
    Column("category", Text),
    Column("status", Text, nullable=False, server_default="Disponível"),
    Column("created_at", Text, nullable=False),
    CheckConstraint(
        "status IN ('Disponível','Emprestado','Em Manutenção')", name="ck_books_status"
    ),
)

loans_table = Table(
    "loans",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("book_id", Integer, ForeignKey("books.id"), nullable=False),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("loan_date", Text, nullable=False),
    Column("return_date", Text),
    Column("status", Text, nullable=False, server_default="ativo"),
    CheckConstraint("status IN ('ativo','devolvido')", name="ck_loans_status"),
)


def _get_database_url_from_secrets() -> str:
    try:
        return st.secrets["DATABASE_URL"]
    except Exception as exc:
        raise RuntimeError(
            "DATABASE_URL não encontrada em st.secrets. Copie "
            ".streamlit/secrets.toml.example para .streamlit/secrets.toml e "
            "preencha com a connection string do Supabase (ou configure os "
            "Secrets do Streamlit Community Cloud em produção)."
        ) from exc


@lru_cache(maxsize=8)
def _build_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)


def get_engine(database_url: str | None = None) -> Engine:
    return _build_engine(database_url or _get_database_url_from_secrets())


def get_connection(database_url: str | None = None) -> Connection:
    engine = get_engine(database_url)
    conn = engine.connect()
    if engine.dialect.name == "sqlite":
        conn.execute(text("PRAGMA foreign_keys = ON"))
    return conn


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = os.urandom(16).hex()
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return digest, salt


def verify_password(password: str, digest: str, salt: str) -> bool:
    check, _ = hash_password(password, salt)
    return check == digest


def create_schema(engine: Engine) -> None:
    metadata.create_all(engine)


_initialized_engine_ids: set[int] = set()


def init_db(database_url: str | None = None) -> None:
    engine = get_engine(database_url)
    if id(engine) in _initialized_engine_ids:
        return

    create_schema(engine)
    with get_connection(database_url) as conn:
        # Só o admin padrão é criado automaticamente — necessário para o
        # primeiro acesso. O catálogo começa vazio: os livros vêm da carga
        # real do acervo (cadastro manual ou importação de CSV).
        if conn.execute(text("SELECT COUNT(*) AS n FROM users")).mappings().first()["n"] == 0:
            create_user(
                conn, "Administrador", "admin@biblioteca.org", "", "admin123", "admin"
            )
            conn.commit()

    _initialized_engine_ids.add(id(engine))


# ---------------------------------------------------------------------------
# Usuários
# ---------------------------------------------------------------------------

def create_user(conn, full_name, email, phone, password, role):
    digest, salt = hash_password(password)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        text(
            """INSERT INTO users
               (full_name, email, phone, password_hash, salt, role, created_at)
               VALUES (:full_name, :email, :phone, :password_hash, :salt, :role, :created_at)"""
        ),
        {
            "full_name": full_name,
            "email": email.lower().strip(),
            "phone": phone,
            "password_hash": digest,
            "salt": salt,
            "role": role,
            "created_at": now,
        },
    )


def get_user_by_email(conn, email):
    return conn.execute(
        text("SELECT * FROM users WHERE email = :email"),
        {"email": email.lower().strip()},
    ).mappings().first()


def authenticate(conn, email, password):
    user = get_user_by_email(conn, email)
    if user and verify_password(password, user["password_hash"], user["salt"]):
        return user
    return None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match((email or "").strip()))


# ---------------------------------------------------------------------------
# Livros
# ---------------------------------------------------------------------------

# Acervos físicos do CCE. Cada um tem uma convenção de código própria, que
# precisa ser preservada exatamente como está nas prateleiras.
CATEGORY_LITERARIA = "Literária"
CATEGORY_ESPIRITUAL = "Espiritual"
BOOK_CATEGORIES = [CATEGORY_LITERARIA, CATEGORY_ESPIRITUAL]

# Estratégias de geração de código.
CODE_STRATEGY_AUTHOR = "autor"      # NETJ-001 (3 letras do sobrenome + inicial + seq)
CODE_STRATEGY_NUMERIC = "numerico"  # 461, 1091 (sequencial puramente numérico)


def get_code_strategy(category: str | None) -> str:
    """Qual estratégia de código vale para uma categoria.

    Acervo Espiritual usa numeração sequencial pura; qualquer outra categoria
    (incluindo Literária e as categorias legadas) mantém a regra por autor.
    A comparação ignora acento, caixa e espaços."""
    if _normalize_key(category) == _normalize_key(CATEGORY_ESPIRITUAL):
        return CODE_STRATEGY_NUMERIC
    return CODE_STRATEGY_AUTHOR


def count_books_by_author(conn, author: str) -> int:
    return conn.execute(
        text("SELECT COUNT(*) AS n FROM books WHERE author = :author"),
        {"author": author},
    ).mappings().first()["n"]


def max_numeric_code_for_category(conn, category: str) -> int:
    """Maior código puramente numérico já usado na categoria (0 se não houver).

    Códigos não numéricos da categoria são ignorados — a base legada tem
    códigos fora de padrão que não participam da sequência numérica."""
    target = _normalize_key(category)
    rows = conn.execute(text("SELECT code, category FROM books")).mappings().all()
    numbers = [
        int(r["code"].strip())
        for r in rows
        if _normalize_key(r["category"]) == target and (r["code"] or "").strip().isdigit()
    ]
    return max(numbers, default=0)


class BookCodeAllocator:
    """Resolve o código de cada livro conforme a estratégia da sua categoria,
    acumulando o que já foi alocado nesta instância.

    Uma instância por lote: no cadastro manual é um livro só, na importação
    CSV é o arquivo inteiro — assim o sequencial (por autor ou numérico)
    considera tanto o que já está no banco quanto as linhas anteriores do
    próprio lote.
    """

    def __init__(self, conn):
        self._conn = conn
        self._author_counts: dict[str, int] = {}
        self._numeric_max: dict[str, int] = {}

    def _numeric_base(self, category: str) -> int:
        key = _normalize_key(category)
        if key not in self._numeric_max:
            self._numeric_max[key] = max_numeric_code_for_category(self._conn, category)
        return self._numeric_max[key]

    def resolve_code(self, author: str, category: str, code_in: str = "") -> str:
        """Código final de uma linha: mantém o que veio preenchido (inclusive
        os legados fora de padrão) ou gera conforme a estratégia da categoria.
        Contabiliza a linha para as próximas do mesmo lote."""
        code_in = (code_in or "").strip()
        author = (author or "").strip()

        if code_in:
            final_code = code_in
            # um código numérico já ocupado não pode ser reemitido adiante
            if code_in.isdigit():
                key = _normalize_key(category)
                self._numeric_max[key] = max(self._numeric_base(category), int(code_in))
        elif get_code_strategy(category) == CODE_STRATEGY_NUMERIC:
            key = _normalize_key(category)
            final_code = str(self._numeric_base(category) + 1)
            self._numeric_max[key] = int(final_code)
        elif author:
            db_count = count_books_by_author(self._conn, author)
            final_code = generate_book_code(author, db_count + self._author_counts.get(author, 0))
        else:
            final_code = ""

        if author:
            self._author_counts[author] = self._author_counts.get(author, 0) + 1
        return final_code


def add_book(conn, title, author, category):
    code = BookCodeAllocator(conn).resolve_code(author, category)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        text(
            """INSERT INTO books (code, title, author, category, status, created_at)
               VALUES (:code, :title, :author, :category, :status, :created_at)"""
        ),
        {
            "code": code,
            "title": title,
            "author": author,
            "category": category,
            "status": "Disponível",
            "created_at": now,
        },
    )
    return code


def get_active_loan_for_book(conn, book_id):
    return conn.execute(
        text("SELECT * FROM loans WHERE book_id = :book_id AND status = 'ativo'"),
        {"book_id": book_id},
    ).mappings().first()


def count_loans_for_book(conn, book_id) -> int:
    return conn.execute(
        text("SELECT COUNT(*) AS n FROM loans WHERE book_id = :book_id"),
        {"book_id": book_id},
    ).mappings().first()["n"]


def delete_book(conn, book_id) -> None:
    """Remove um livro e todo o seu histórico de empréstimos (já devolvidos),
    de forma atômica: as duas exclusões acontecem na mesma transação e só
    persistem quando o chamador der commit.

    Levanta ValueError se houver empréstimo ATIVO para o livro — controle do
    exemplar físico em posse de alguém, que precisa ser devolvido antes.
    """
    if get_active_loan_for_book(conn, book_id) is not None:
        raise ValueError(
            "Este livro está emprestado no momento. Registre a devolução antes de removê-lo."
        )
    conn.execute(text("DELETE FROM loans WHERE book_id = :book_id"), {"book_id": book_id})
    conn.execute(text("DELETE FROM books WHERE id = :id"), {"id": book_id})


# ---------------------------------------------------------------------------
# Importação em lote (CSV)
# ---------------------------------------------------------------------------

VALID_BOOK_STATUSES = {"Disponível", "Emprestado", "Em Manutenção"}

# Campos internos que a importação sabe preencher, na ordem em que aparecem
# na tela de mapeamento. titulo/autor são obrigatórios; o resto é opcional.
IMPORT_FIELDS = ["titulo", "autor", "categoria", "codigo", "status"]
REQUIRED_IMPORT_FIELDS = ["titulo", "autor"]

# Sinônimos aceitos para detecção automática de colunas. A comparação é
# tolerante: sem acento, sem caixa e sem separadores (espaço, hífen, _).
IMPORT_FIELD_SYNONYMS = {
    "titulo": ["titulo", "título", "title", "nome", "obra"],
    "autor": ["autor", "author", "escritor"],
    "categoria": ["categoria", "category", "acervo", "colecao", "coleção"],
    "codigo": ["codigo", "código", "código-antigo", "code", "cod", "tombo", "registro"],
    "status": ["status", "situacao", "situação"],
}

# Variações aceitas para cada status interno (normalizadas por _normalize_key).
STATUS_SYNONYMS = {
    "Disponível": ["disponível", "disponivel", "available"],
    "Emprestado": ["emprestado", "borrowed", "on loan"],
    "Em Manutenção": ["em manutenção", "em manutencao", "manutenção", "manutencao"],
}


_NORMALIZED_FIELD_SYNONYMS = {
    field: {_normalize_key(s) for s in synonyms}
    for field, synonyms in IMPORT_FIELD_SYNONYMS.items()
}

_NORMALIZED_STATUS_SYNONYMS = {
    _normalize_key(variant): canonical
    for canonical, variants in STATUS_SYNONYMS.items()
    for variant in variants
}


def normalize_status(value: str | None) -> str | None:
    """Normaliza um valor de status vindo do CSV para um dos status internos.

    Vazio -> 'Disponível' (padrão). Valor desconhecido -> None, para que o
    chamador registre erro bloqueante na linha (nunca adivinhamos)."""
    raw = (value or "").strip()
    if not raw:
        return "Disponível"
    return _NORMALIZED_STATUS_SYNONYMS.get(_normalize_key(raw))


def detect_column_mapping(columns: list[str]) -> tuple[dict, dict]:
    """Pré-seleciona, para cada campo interno, qual coluna do arquivo parece
    corresponder a ele.

    Retorna (mapeamento, ambiguidades):
      - mapeamento: campo -> coluna do arquivo (ou None se não detectado)
      - ambiguidades: campo -> lista de colunas candidatas, quando houver mais
        de uma. Nesse caso o campo fica SEM pré-seleção de propósito: a escolha
        é do usuário (o export real do CCE traz 'Código-antigo' e 'Código'
        no mesmo arquivo, com significados diferentes).
    """
    mapping: dict[str, str | None] = {field: None for field in IMPORT_FIELDS}
    ambiguities: dict[str, list[str]] = {}

    for field in IMPORT_FIELDS:
        candidates = [
            column
            for column in columns
            if _normalize_key(column) in _NORMALIZED_FIELD_SYNONYMS[field]
        ]
        if len(candidates) == 1:
            mapping[field] = candidates[0]
        elif len(candidates) > 1:
            ambiguities[field] = candidates

    return mapping, ambiguities


def apply_column_mapping(
    rows: list[dict], mapping: dict, fixed_category: str = ""
) -> list[dict]:
    """Converte as linhas cruas do CSV para o formato interno
    (titulo/autor/categoria/codigo/status), conforme o mapeamento escolhido.

    Campos não mapeados saem vazios — o processamento posterior aplica o
    comportamento padrão (código gerado, status 'Disponível'). Uma categoria
    fixa, quando informada, vale para todas as linhas e dispensa mapear uma
    coluna de categoria. Todos os valores saem com .strip() aplicado, porque o
    export real traz espaços sobrando que gerariam autores/categorias
    duplicados por diferença invisível.
    """
    fixed_category = (fixed_category or "").strip()
    mapped_rows = []
    for row in rows:
        mapped = {}
        for field in IMPORT_FIELDS:
            column = mapping.get(field)
            value = row.get(column) if column else None
            mapped[field] = (value or "").strip()
        if fixed_category and not mapped["categoria"]:
            mapped["categoria"] = fixed_category
        mapped_rows.append(mapped)
    return mapped_rows


def parse_csv_bytes(data: bytes) -> list[dict]:
    """Decodifica bytes de um CSV (UTF-8 com ou sem BOM) e detecta o
    delimitador (',' ou ';') automaticamente. Retorna uma lista de dicts
    com as chaves das colunas normalizadas (minúsculas, sem espaços)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("utf-8")

    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") > sample.count(",") else ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return [
        {(key or "").strip().lower(): value for key, value in row.items()}
        for row in reader
    ]


def get_csv_columns(rows: list[dict]) -> list[str]:
    """Nomes das colunas do arquivo, na ordem em que aparecem no cabeçalho."""
    return list(rows[0].keys()) if rows else []


def process_import_rows(conn, rows: list[dict]) -> tuple[list[dict], dict]:
    """Processa as linhas de um CSV de importação de livros, calculando
    código (mantido ou gerado) e status finais, e sinalizando erros
    bloqueantes (título/autor vazios, status inválido, código duplicado
    — seja contra o banco, seja entre linhas do próprio arquivo).

    Retorna (linhas_processadas, resumo_estatistico).
    """
    existing_codes = set(conn.execute(text("SELECT code FROM books")).scalars().all())
    used_codes_in_batch: set[str] = set()
    allocator = BookCodeAllocator(conn)

    processed = []
    kept_count = 0
    generated_count = 0
    error_count = 0

    for idx, row in enumerate(rows, start=1):
        title = (row.get("titulo") or "").strip()
        author = (row.get("autor") or "").strip()
        category = (row.get("categoria") or "").strip()
        code_in = (row.get("codigo") or "").strip()
        status_in = (row.get("status") or "").strip()

        errors = []
        if not title:
            errors.append("Título é obrigatório.")
        if not author:
            errors.append("Autor é obrigatório.")

        normalized_status = normalize_status(status_in)
        if normalized_status is None:
            errors.append(f"Status inválido: '{status_in}'.")
        final_status = normalized_status or "Disponível"

        code_source = "mantido" if code_in else "gerado"
        final_code = allocator.resolve_code(author, category, code_in)

        if final_code:
            if code_source == "mantido":
                if final_code in existing_codes or final_code in used_codes_in_batch:
                    errors.append(f"Código duplicado: '{final_code}'.")
            elif final_code in existing_codes or final_code in used_codes_in_batch:
                errors.append(f"Colisão inesperada de código gerado: '{final_code}'.")

        if final_code:
            used_codes_in_batch.add(final_code)

        processed.append(
            {
                "linha": idx,
                "titulo": title,
                "autor": author,
                "categoria": category,
                "codigo": final_code,
                "codigo_origem": code_source,
                "status": final_status,
                "erros": errors,
            }
        )

        if code_source == "mantido":
            kept_count += 1
        else:
            generated_count += 1
        if errors:
            error_count += 1

    summary = {
        "total": len(processed),
        "mantidos": kept_count,
        "gerados": generated_count,
        "com_erro": error_count,
    }
    return processed, summary


def commit_import(conn, processed_rows: list[dict]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    count = 0
    for row in processed_rows:
        conn.execute(
            text(
                """INSERT INTO books (code, title, author, category, status, created_at)
                   VALUES (:code, :title, :author, :category, :status, :created_at)"""
            ),
            {
                "code": row["codigo"],
                "title": row["titulo"],
                "author": row["autor"],
                "category": row["categoria"],
                "status": row["status"],
                "created_at": now,
            },
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# Empréstimos
# ---------------------------------------------------------------------------

def request_loan(conn, book_id, user_id):
    book = conn.execute(
        text("SELECT * FROM books WHERE id = :id"), {"id": book_id}
    ).mappings().first()
    if book is None or book["status"] != "Disponível":
        raise ValueError("Livro indisponível para empréstimo.")
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        text(
            "INSERT INTO loans (book_id, user_id, loan_date, status) "
            "VALUES (:book_id, :user_id, :loan_date, :status)"
        ),
        {"book_id": book_id, "user_id": user_id, "loan_date": now, "status": "ativo"},
    )
    conn.execute(
        text("UPDATE books SET status = 'Emprestado' WHERE id = :id"), {"id": book_id}
    )


def return_loan(conn, loan_id):
    loan = conn.execute(
        text("SELECT * FROM loans WHERE id = :id"), {"id": loan_id}
    ).mappings().first()
    if loan is None or loan["status"] != "ativo":
        raise ValueError("Empréstimo não está ativo.")
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        text("UPDATE loans SET status = 'devolvido', return_date = :return_date WHERE id = :id"),
        {"return_date": now, "id": loan_id},
    )
    conn.execute(
        text("UPDATE books SET status = 'Disponível' WHERE id = :id"), {"id": loan["book_id"]}
    )


# ---------------------------------------------------------------------------
# Telas (UI)
# ---------------------------------------------------------------------------

def show_auth_screen(conn):
    st.title("Biblioteca Comunitária")
    tab_login, tab_cadastro = st.tabs(["Entrar", "Cadastrar-se"])

    with tab_login:
        with st.form("login_form"):
            email = st.text_input("E-mail", key="login_email")
            password = st.text_input("Senha", type="password", key="login_password")
            if st.form_submit_button("Entrar"):
                user = authenticate(conn, email, password)
                if user:
                    st.session_state.user = dict(user)
                    st.rerun()
                else:
                    st.error("E-mail ou senha inválidos.")
        st.caption("Admin padrão: admin@biblioteca.org / admin123")

    with tab_cadastro:
        with st.form("cadastro_form"):
            full_name = st.text_input("Nome completo", key="cad_nome")
            email_c = st.text_input("E-mail", key="cad_email")
            phone = st.text_input("Telefone/WhatsApp", key="cad_phone")
            password_c = st.text_input("Senha", type="password", key="cad_senha")
            if st.form_submit_button("Cadastrar"):
                has_error = False
                if not full_name.strip():
                    st.error("Nome completo é obrigatório.")
                    has_error = True
                if not email_c.strip():
                    st.error("E-mail é obrigatório.")
                    has_error = True
                elif not is_valid_email(email_c):
                    st.error("E-mail inválido. Informe um endereço no formato nome@dominio.com.")
                    has_error = True
                if not password_c:
                    st.error("Senha é obrigatória.")
                    has_error = True
                if not has_error and get_user_by_email(conn, email_c):
                    st.error("Já existe um cadastro com esse e-mail.")
                    has_error = True
                if not has_error:
                    create_user(conn, full_name, email_c, phone, password_c, "leitor")
                    conn.commit()
                    st.success("Cadastro realizado! Faça login na aba ao lado.")


def show_catalog(conn, user):
    st.header("Catálogo de Livros")
    query = st.text_input("Buscar por título, autor, código ou categoria")
    rows = conn.execute(text("SELECT * FROM books ORDER BY title")).mappings().all()

    if query:
        q = query.lower()
        rows = [
            r
            for r in rows
            if q in r["title"].lower()
            or q in r["author"].lower()
            or q in r["code"].lower()
            or q in (r["category"] or "").lower()
        ]

    status_emoji = {"Disponível": "🟢", "Emprestado": "🔴", "Em Manutenção": "🟡"}

    for r in rows:
        with st.container(border=True):
            info_col, action_col = st.columns([5, 2])
            with info_col:
                st.markdown(f"**{r['title']}**")
                st.caption(f"{r['author']} · {r['code']}")
                st.write(f"{status_emoji.get(r['status'], '')} {r['status']}")
            with action_col:
                if user["role"] == "leitor" and r["status"] == "Disponível":
                    if st.button(
                        "Pegar emprestado", key=f"borrow_{r['id']}", width="stretch"
                    ):
                        request_loan(conn, r["id"], user["id"])
                        conn.commit()
                        st.success(f'Empréstimo de "{r["title"]}" registrado!')
                        st.rerun()

    if not rows:
        st.info("Nenhum livro encontrado.")


def show_book_management(conn):
    st.header("Gestão de Livros (CRUD)")

    with st.expander("➕ Adicionar novo livro"):
        with st.form("add_book_form"):
            title = st.text_input("Título")
            author = st.text_input("Autor (nome completo)")
            category = st.selectbox(
                "Categoria (acervo)",
                BOOK_CATEGORIES,
                help="A categoria determina a regra do código: Literária usa "
                "sobrenome + inicial + sequencial (ex.: NETJ-001); Espiritual usa "
                "numeração sequencial (ex.: 461).",
            )
            if st.form_submit_button("Cadastrar livro"):
                if not title or not author:
                    st.error("Título e autor são obrigatórios.")
                else:
                    code = add_book(conn, title, author, category)
                    conn.commit()
                    st.success(
                        f'Livro cadastrado no acervo **{category}** com código **{code}**'
                    )

    st.subheader("Livros cadastrados")
    statuses = ["Disponível", "Emprestado", "Em Manutenção"]
    rows = conn.execute(text("SELECT * FROM books ORDER BY id DESC")).mappings().all()
    for r in rows:
        with st.expander(f"{r['code']} — {r['title']}"):
            with st.form(f"edit_form_{r['id']}"):
                title = st.text_input("Título", value=r["title"], key=f"t_{r['id']}")
                author = st.text_input("Autor", value=r["author"], key=f"a_{r['id']}")
                category = st.text_input(
                    "Categoria", value=r["category"] or "", key=f"c_{r['id']}"
                )
                status = st.selectbox(
                    "Status",
                    statuses,
                    index=statuses.index(r["status"]),
                    key=f"s_{r['id']}",
                )
                if st.form_submit_button("Salvar alterações"):
                    conn.execute(
                        text(
                            "UPDATE books SET title = :title, author = :author, "
                            "category = :category, status = :status WHERE id = :id"
                        ),
                        {
                            "title": title,
                            "author": author,
                            "category": category,
                            "status": status,
                            "id": r["id"],
                        },
                    )
                    conn.commit()
                    st.success("Livro atualizado.")
                    st.rerun()

            st.markdown("---")
            active_loan = get_active_loan_for_book(conn, r["id"])
            loan_count = count_loans_for_book(conn, r["id"])

            if active_loan is not None:
                st.error(
                    "Este livro está emprestado no momento. Registre a devolução "
                    "antes de removê-lo."
                )
            elif loan_count > 0:
                confirm = st.checkbox(
                    f"Confirmo a remoção do livro **{r['title']}** e de "
                    f"**{loan_count}** registro(s) de empréstimo/devolução associado(s).",
                    key=f"confirm_delete_{r['id']}",
                )
                if st.button(
                    "🗑️ Remover livro e histórico",
                    key=f"delete_{r['id']}",
                    disabled=not confirm,
                ):
                    try:
                        delete_book(conn, r["id"])
                        conn.commit()
                        st.warning("Livro e histórico de empréstimos removidos.")
                        st.rerun()
                    except (ValueError, IntegrityError) as exc:
                        conn.rollback()
                        st.error(f"Não foi possível remover o livro: {exc}")
            else:
                if st.button("🗑️ Remover livro", key=f"delete_{r['id']}"):
                    try:
                        delete_book(conn, r["id"])
                        conn.commit()
                        st.warning("Livro removido.")
                        st.rerun()
                    except (ValueError, IntegrityError) as exc:
                        conn.rollback()
                        st.error(f"Não foi possível remover o livro: {exc}")


def show_loan_management(conn):
    st.header("Empréstimos ativos")
    rows = conn.execute(
        text(
            """
            SELECT loans.id AS loan_id, books.title, books.code,
                   users.full_name, users.email, users.phone, loans.loan_date
            FROM loans
            JOIN books ON books.id = loans.book_id
            JOIN users ON users.id = loans.user_id
            WHERE loans.status = 'ativo'
            ORDER BY loans.loan_date
            """
        )
    ).mappings().all()

    if not rows:
        st.info("Nenhum empréstimo ativo no momento.")

    for r in rows:
        with st.container(border=True):
            info_col, action_col = st.columns([5, 2])
            with info_col:
                st.markdown(f"**{r['title']}** ({r['code']})")
                st.caption(f"{r['full_name']} · {r['email']} · {r['phone'] or '-'}")
                st.write(f"Emprestado em {r['loan_date']}")
            with action_col:
                if st.button(
                    "✅ Registrar devolução", key=f"return_{r['loan_id']}", width="stretch"
                ):
                    return_loan(conn, r["loan_id"])
                    conn.commit()
                    st.success("Devolução registrada.")
                    st.rerun()


def show_admin_loan_history(conn):
    st.header("Histórico completo de empréstimos")

    rows = conn.execute(
        text(
            """
            SELECT loans.id AS loan_id, loans.loan_date, loans.return_date, loans.status,
                   books.id AS book_id, books.title AS book_title, books.code AS book_code,
                   users.id AS user_id, users.full_name, users.email, users.phone
            FROM loans
            JOIN books ON books.id = loans.book_id
            JOIN users ON users.id = loans.user_id
            ORDER BY loans.loan_date DESC
            """
        )
    ).mappings().all()

    if not rows:
        st.info("Nenhum empréstimo registrado ainda.")
        return

    book_options = {"Todos": None}
    for title, code, book_id in sorted(
        {(r["book_title"], r["book_code"], r["book_id"]) for r in rows}
    ):
        book_options[f"{title} ({code})"] = book_id

    user_options = {"Todos": None}
    for name, email, user_id in sorted(
        {(r["full_name"], r["email"], r["user_id"]) for r in rows}
    ):
        user_options[f"{name} ({email})"] = user_id

    loan_dates = [datetime.fromisoformat(r["loan_date"]).date() for r in rows]

    col1, col2, col3 = st.columns(3)
    book_choice = col1.selectbox("Filtrar por livro", list(book_options.keys()))
    user_choice = col2.selectbox("Filtrar por usuário", list(user_options.keys()))
    date_range = col3.date_input(
        "Período (data de empréstimo)",
        value=(min(loan_dates), max(loan_dates)),
    )

    filtered = rows
    if book_options[book_choice] is not None:
        filtered = [r for r in filtered if r["book_id"] == book_options[book_choice]]
    if user_options[user_choice] is not None:
        filtered = [r for r in filtered if r["user_id"] == user_options[user_choice]]
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start, end = date_range
        filtered = [
            r
            for r in filtered
            if start <= datetime.fromisoformat(r["loan_date"]).date() <= end
        ]

    st.write(f"{len(filtered)} empréstimo(s) encontrado(s).")
    status_emoji = {"ativo": "🔴", "devolvido": "🟢"}
    table = [
        {
            "Livro": f"{r['book_title']} ({r['book_code']})",
            "Leitor": r["full_name"],
            "E-mail": r["email"],
            "Telefone": r["phone"] or "-",
            "Emprestado em": r["loan_date"],
            "Devolvido em": r["return_date"] or "-",
            "Status": f"{status_emoji.get(r['status'], '')} {r['status']}",
        }
        for r in filtered
    ]
    st.dataframe(table, width="stretch")

    if user_options[user_choice] is not None:
        user_loans = [r for r in rows if r["user_id"] == user_options[user_choice]]
        with st.expander(f"📋 Todos os empréstimos de {user_choice}", expanded=True):
            for r in user_loans:
                st.write(
                    f"- **{r['book_title']}** ({r['book_code']}) — "
                    f"{r['loan_date']} → {r['return_date'] or 'em aberto'} [{r['status']}]"
                )


def show_my_loans(conn, user):
    st.header("Meus Empréstimos")

    st.subheader("Livros em minha posse")
    active = conn.execute(
        text(
            """
            SELECT loans.id AS loan_id, books.title, books.code, loans.loan_date
            FROM loans JOIN books ON books.id = loans.book_id
            WHERE loans.user_id = :user_id AND loans.status = 'ativo'
            """
        ),
        {"user_id": user["id"]},
    ).mappings().all()

    if not active:
        st.info("Você não tem livros emprestados no momento.")
    for r in active:
        with st.container(border=True):
            info_col, action_col = st.columns([5, 2])
            with info_col:
                st.markdown(f"**{r['title']}** ({r['code']})")
                st.caption(f"Desde {r['loan_date']}")
            with action_col:
                if st.button(
                    "Solicitar devolução", key=f"selfreturn_{r['loan_id']}", width="stretch"
                ):
                    return_loan(conn, r["loan_id"])
                    conn.commit()
                    st.success("Devolução registrada. Obrigado!")
                    st.rerun()

    st.subheader("Histórico completo")
    history = conn.execute(
        text(
            """
            SELECT books.title, books.code, loans.loan_date, loans.return_date, loans.status
            FROM loans JOIN books ON books.id = loans.book_id
            WHERE loans.user_id = :user_id
            ORDER BY loans.loan_date DESC
            """
        ),
        {"user_id": user["id"]},
    ).mappings().all()

    if not history:
        st.info("Nenhum empréstimo no histórico ainda.")
    for r in history:
        with st.container(border=True):
            st.markdown(f"**{r['title']}** ({r['code']})")
            st.caption(
                f"{r['loan_date']} → {r['return_date'] or 'em aberto'} · {r['status']}"
            )


def show_csv_import(conn):
    st.header("Importar carga de livros (CSV)")
    st.caption(
        "O arquivo **não precisa** vir no formato interno: depois do upload você "
        "escolhe qual coluna do arquivo corresponde a cada campo. Delimitador `,` ou "
        "`;` e encoding UTF-8 (com ou sem BOM) são detectados automaticamente."
    )
    uploaded = st.file_uploader("Selecione o arquivo CSV", type=["csv"])

    if uploaded is None:
        for key in ("csv_import_rows", "csv_import_filename"):
            st.session_state.pop(key, None)
        return

    if st.session_state.get("csv_import_filename") != uploaded.name:
        try:
            st.session_state.csv_import_rows = parse_csv_bytes(uploaded.getvalue())
        except (UnicodeDecodeError, csv.Error) as exc:
            st.error(f"Não foi possível ler o arquivo CSV: {exc}")
            return
        st.session_state.csv_import_filename = uploaded.name

    raw_rows = st.session_state.csv_import_rows
    columns = get_csv_columns(raw_rows)

    if not raw_rows:
        st.warning("O arquivo não contém nenhuma linha de dados.")
        return

    st.subheader("Mapeamento de colunas")
    st.write(
        f"**{len(raw_rows)}** linha(s) e **{len(columns)}** coluna(s) encontrada(s): "
        + ", ".join(f"`{c}`" for c in columns)
    )

    detected, ambiguities = detect_column_mapping(columns)
    for field, candidates in ambiguities.items():
        st.warning(
            f"⚠️ Mais de uma coluna parece corresponder a **{field}**: "
            + ", ".join(f"`{c}`" for c in candidates)
            + ". Nenhuma foi escolhida automaticamente — selecione abaixo qual usar."
        )

    none_label = "(não mapear / deixar vazio)"
    mapping = {}
    map_cols = st.columns(len(IMPORT_FIELDS))
    for col, field in zip(map_cols, IMPORT_FIELDS):
        options = [none_label] + columns
        default = detected.get(field)
        label = field + (" *" if field in REQUIRED_IMPORT_FIELDS else "")
        choice = col.selectbox(
            label,
            options,
            index=options.index(default) if default in options else 0,
            key=f"map_{field}_{uploaded.name}",
        )
        mapping[field] = None if choice == none_label else choice

    fixed_category = ""
    if mapping["categoria"] is None:
        fixed_category = st.text_input(
            "Categoria fixa para todas as linhas (opcional)",
            help="Use quando o arquivo inteiro pertence a um único acervo, "
            "em vez de mapear uma coluna de categoria.",
            key=f"fixed_category_{uploaded.name}",
        )

    missing_required = [f for f in REQUIRED_IMPORT_FIELDS if mapping[f] is None]
    if missing_required:
        st.error(
            "Mapeie os campos obrigatórios antes de continuar: "
            + ", ".join(f"**{f}**" for f in missing_required)
        )
        return

    mapped_rows = apply_column_mapping(raw_rows, mapping, fixed_category)
    processed, summary = process_import_rows(conn, mapped_rows)

    st.subheader("Pré-visualização")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total de registros", summary["total"])
    col2.metric("Códigos mantidos", summary["mantidos"])
    col3.metric("Códigos gerados", summary["gerados"])
    col4.metric("Linhas com erro", summary["com_erro"])

    preview_table = [
        {
            "Linha": r["linha"],
            "Título": r["titulo"],
            "Autor": r["autor"],
            "Categoria": r["categoria"],
            "Código": r["codigo"],
            "Origem código": r["codigo_origem"],
            "Status": r["status"],
            "Erros": "; ".join(r["erros"]) if r["erros"] else "",
        }
        for r in processed
    ]
    st.dataframe(preview_table, width="stretch")

    error_rows = [r for r in preview_table if r["Erros"]]
    if error_rows:
        st.error(
            f"{len(error_rows)} linha(s) com erro bloqueante. "
            "Ajuste o mapeamento acima ou corrija o arquivo e reenvie "
            "antes de confirmar a importação."
        )
        st.dataframe(error_rows, width="stretch")

    if st.button("Confirmar importação", disabled=bool(error_rows)):
        count = commit_import(conn, processed)
        conn.commit()
        st.success(f"{count} livro(s) importado(s) com sucesso.")
        for key in ("csv_import_rows", "csv_import_filename"):
            st.session_state.pop(key, None)
        st.rerun()


def show_app(conn):
    user = st.session_state.user
    with st.sidebar:
        st.write(f"👤 **{user['full_name']}**")
        st.caption("Perfil: " + ("Administrador" if user["role"] == "admin" else "Leitor"))
        if user["role"] == "admin":
            page = st.radio(
                "Menu",
                [
                    "Catálogo",
                    "Gestão de Livros",
                    "Empréstimos",
                    "Histórico completo",
                    "Importar CSV",
                ],
            )
        else:
            page = st.radio("Menu", ["Catálogo", "Meus Empréstimos"])
        if st.button("Sair"):
            st.session_state.user = None
            st.rerun()

    if page == "Catálogo":
        show_catalog(conn, user)
    elif page == "Gestão de Livros":
        show_book_management(conn)
    elif page == "Empréstimos":
        show_loan_management(conn)
    elif page == "Histórico completo":
        show_admin_loan_history(conn)
    elif page == "Meus Empréstimos":
        show_my_loans(conn, user)
    elif page == "Importar CSV":
        show_csv_import(conn)


def _inject_card_border_css() -> None:
    """Tinge a borda de todo st.container(border=True) em vinho suave, no
    lugar do cinza padrão do tema, sem competir com o vermelho sólido dos
    botões primários."""
    st.markdown(
        """
        <style>
        [data-testid="stVerticalBlockBorderWrapper"] {
            border-color: rgba(122, 31, 43, 0.3) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main():
    st.set_page_config(
        page_title="Biblioteca Comunitária", page_icon="assets/logo_cce.png", layout="wide"
    )
    st.logo("assets/logo_cce.png")
    _inject_card_border_css()
    init_db()

    if "user" not in st.session_state:
        st.session_state.user = None

    with get_connection() as conn:
        if st.session_state.user is None:
            show_auth_screen(conn)
        else:
            show_app(conn)


if __name__ == "__main__":
    main()
