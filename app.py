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
import itertools
import os
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta

import streamlit as st
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    case,
    create_engine,
    func,
    inspect,
    or_,
    select,
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
    # Data prevista de devolução (ISO "YYYY-MM-DD"), anulável: empréstimos
    # anteriores a este recurso ficam sem prazo e nunca contam como atrasados.
    # Guardada como TEXT para acompanhar loan_date/return_date — datas ISO
    # comparam corretamente em ordem lexicográfica no Postgres e no SQLite.
    Column("due_date", Text),
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


@st.cache_resource(show_spinner=False)
def _build_engine(database_url: str) -> Engine:
    """Engine (e seu pool de conexões) reaproveitado entre reruns e sessões.

    Precisa ser st.cache_resource, e não lru_cache: o Streamlit executa cada
    rerun em um módulo NOVO, então qualquer cache de módulo nasce vazio e um
    engine — com pool novo e handshake TCP/TLS novo — seria criado a cada
    clique do usuário. O Engine é thread-safe e pode ser compartilhado.

    Só o Engine é cacheado. Objetos Connection JAMAIS podem entrar aqui:
    carregam estado transacional e seriam compartilhados entre sessões e
    threads, misturando commits e rollbacks de usuários diferentes.
    """
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)


def get_engine(database_url: str | None = None) -> Engine:
    # a URL é resolvida ANTES de cachear, para que a chave do cache seja o
    # banco de verdade e não o None do argumento omitido
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


def _migrate_add_loans_due_date(engine: Engine) -> None:
    """Adiciona loans.due_date em bancos criados antes deste recurso.

    metadata.create_all() só cria tabelas que faltam — nunca colunas — então
    um banco já em uso (o Supabase do CCE) precisa do ALTER TABLE explícito.
    Os empréstimos existentes ficam com due_date NULL, e NULL nunca é tratado
    como atraso. ADD COLUMN funciona igual em Postgres e SQLite.
    """
    inspector = inspect(engine)
    if "loans" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("loans")}
    if "due_date" in columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE loans ADD COLUMN due_date TEXT"))


def create_schema(engine: Engine) -> None:
    metadata.create_all(engine)
    _migrate_add_loans_due_date(engine)


@st.cache_resource(show_spinner=False)
def _ensure_initialized(database_url: str) -> bool:
    """Prepara o banco uma única vez por processo.

    Precisa ser st.cache_resource pelo mesmo motivo do engine: um guard em
    variável de módulo nunca pega, porque o Streamlit executa cada rerun em um
    módulo novo — e a criação de schema + inspeção da migração custava 6
    consultas em TODA interação, até na tela de login.

    Continua idempotente: create_all só cria o que falta, a migração só roda
    se a coluna não existir e o admin só é criado se não houver usuário. Isso
    importa porque o Streamlit Cloud hiberna o app — quando o container
    reinicia, o cache nasce vazio e esta função roda de novo.
    """
    engine = get_engine(database_url)
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
    return True


def init_db(database_url: str | None = None) -> None:
    _ensure_initialized(database_url or _get_database_url_from_secrets())


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


def loan_summary_for_books(conn, book_ids) -> dict:
    """Resumo de empréstimos de VÁRIOS livros em uma única query.

    Devolve {book_id: {"total": n, "ativos": n}} — exatamente o que a listagem
    da Gestão de Livros precisa por livro (se está emprestado agora e quantos
    registros seriam apagados junto na remoção), sem pagar uma consulta por
    livro exibido. Livros sem nenhum empréstimo vêm com zeros.
    """
    ids = list(book_ids)
    if not ids:
        return {}
    rows = conn.execute(
        select(
            loans_table.c.book_id,
            func.count().label("total"),
            func.sum(case((loans_table.c.status == "ativo", 1), else_=0)).label("ativos"),
        )
        .where(loans_table.c.book_id.in_(ids))
        .group_by(loans_table.c.book_id)
    ).all()

    summary = {book_id: {"total": 0, "ativos": 0} for book_id in ids}
    for book_id, total, ativos in rows:
        summary[book_id] = {"total": int(total), "ativos": int(ativos or 0)}
    return summary


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
# Busca, filtros e paginação de livros (tudo resolvido no banco)
# ---------------------------------------------------------------------------

BOOKS_PAGE_SIZE = 25
CATEGORY_FILTER_ALL = "Todas"
STATUS_FILTER_ALL = "Todos"

# Acentos que aparecem no acervo, em MAIÚSCULAS e minúsculas. Precisamos das
# duas caixas porque o LOWER() do SQLite é ASCII-only: ele não converte 'Ó'
# em 'ó', então remover só as minúsculas deixaria "MEMÓRIAS" sem normalizar
# (no Postgres o LOWER() é Unicode-aware e funcionaria — divergência que
# faria a busca passar nos testes e falhar em produção, ou vice-versa).
_SEARCH_ACCENT_MAP = {
    "á": "a", "à": "a", "â": "a", "ã": "a", "ä": "a",
    "é": "e", "è": "e", "ê": "e", "ë": "e",
    "í": "i", "ì": "i", "î": "i", "ï": "i",
    "ó": "o", "ò": "o", "ô": "o", "õ": "o", "ö": "o",
    "ú": "u", "ù": "u", "û": "u", "ü": "u",
    "ç": "c", "ñ": "n",
}
_SEARCH_ACCENT_MAP.update(
    {accented.upper(): plain.upper() for accented, plain in list(_SEARCH_ACCENT_MAP.items())}
)


def _sql_unaccent(column):
    """Expressão SQL que remove os acentos de uma coluna, preservando a caixa.

    Usa REPLACE aninhado em vez do unaccent() do Postgres porque unaccent()
    exige `CREATE EXTENSION unaccent` (não habilitada por padrão no Supabase)
    e não existe no SQLite usado pelos testes — assim a mesma query roda
    igual nos dois bancos, sem extensão.

    O resultado é ASCII puro, então a comparação com ILIKE resolve a caixa
    de forma idêntica em Postgres (ILIKE nativo) e SQLite (LOWER LIKE LOWER).
    """
    expr = column
    for accented, plain in _SEARCH_ACCENT_MAP.items():
        expr = func.replace(expr, accented, plain)
    return expr


def normalize_search_term(term: str | None) -> str:
    """Mesma normalização do lado Python, para o termo digitado."""
    return _strip_diacritics(term or "").lower().strip()


def _books_where_clauses(query: str = "", category: str | None = None, status: str | None = None):
    """Cláusulas WHERE para busca textual + filtros. A busca combina com os
    filtros (AND entre eles), não os substitui."""
    clauses = []

    term = normalize_search_term(query)
    if term:
        pattern = f"%{term}%"
        searchable = (
            books_table.c.title,
            books_table.c.author,
            books_table.c.code,
            func.coalesce(books_table.c.category, ""),
        )
        clauses.append(
            or_(*[_sql_unaccent(col).ilike(pattern) for col in searchable])
        )

    if category and category != CATEGORY_FILTER_ALL:
        clauses.append(books_table.c.category == category)

    if status and status != STATUS_FILTER_ALL:
        clauses.append(books_table.c.status == status)

    return clauses


def count_books(conn, query: str = "", category: str | None = None, status: str | None = None) -> int:
    """Total de livros que satisfazem busca + filtros (para a paginação)."""
    stmt = select(func.count()).select_from(books_table)
    for clause in _books_where_clauses(query, category, status):
        stmt = stmt.where(clause)
    return conn.execute(stmt).scalar_one()


def list_books(
    conn,
    query: str = "",
    category: str | None = None,
    status: str | None = None,
    limit: int = BOOKS_PAGE_SIZE,
    offset: int = 0,
    newest_first: bool = False,
):
    """Uma página de livros já filtrada e ordenada pelo banco — só as linhas
    exibidas trafegam (o acervo real tem ~2.5k livros)."""
    stmt = select(books_table)
    for clause in _books_where_clauses(query, category, status):
        stmt = stmt.where(clause)
    order = books_table.c.id.desc() if newest_first else books_table.c.title
    stmt = stmt.order_by(order).limit(limit).offset(offset)
    return conn.execute(stmt).mappings().all()


def list_book_categories(conn) -> list[str]:
    """Categorias distintas presentes no acervo, para alimentar o filtro."""
    stmt = (
        select(books_table.c.category)
        .distinct()
        .where(books_table.c.category.isnot(None))
        .where(books_table.c.category != "")
        .order_by(books_table.c.category)
    )
    return list(conn.execute(stmt).scalars().all())


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


CSV_DELIMITER_CANDIDATES = (",", ";")


def _detect_csv_delimiter(text: str) -> str:
    """Detecta o delimitador (',' ou ';') testando cada candidato contra as
    primeiras ~20 linhas do arquivo.

    Para cada candidato, parseia as linhas com csv.reader (que já respeita
    campos entre aspas, inclusive delimitadores dentro deles) e mede: (a) o
    número de colunas mais frequente entre as linhas, e (b) a consistência
    — em que fração das linhas esse número de colunas se repete. Um
    candidato cujo resultado mais comum é 1 coluna só é descartado, mesmo
    que nenhuma linha de amostra tenha campos citados (é exatamente o caso
    que quebrava antes: arquivo separado por vírgula sem aspas nas
    primeiras linhas era lido como uma coluna só, porque a heurística antiga
    dependia de encontrar aspas para funcionar).

    Levanta ValueError, com mensagem clara, se nenhum candidato produzir
    mais de uma coluna — nesse caso não seguimos adiante com uma coluna só.
    """
    if not text.strip():
        raise ValueError("O arquivo CSV está vazio.")

    best_delimiter = None
    best_score = (1, 0.0)  # (nº de colunas mais frequente, taxa de consistência)

    for delimiter in CSV_DELIMITER_CANDIDATES:
        # Lê direto do texto via csv.reader (que decide sozinho onde um
        # registro termina, inclusive campos entre aspas com quebra de
        # linha embutida) em vez de pré-quebrar em linhas com str.splitlines()
        # antes de parsear — pré-quebrar é frágil porque corta um campo
        # citado multilinha ao meio e distorce a amostra.
        try:
            sample_rows = list(
                itertools.islice(csv.reader(io.StringIO(text), delimiter=delimiter), 20)
            )
        except csv.Error:
            continue
        field_counts = [len(row) for row in sample_rows if row]
        if not field_counts:
            continue

        most_common_count, occurrences = Counter(field_counts).most_common(1)[0]
        if most_common_count <= 1:
            continue  # delimitador não aparece nas linhas -> viraria 1 coluna só

        consistency = occurrences / len(field_counts)
        score = (most_common_count, consistency)
        if score > best_score:
            best_score = score
            best_delimiter = delimiter

    if best_delimiter is None:
        raise ValueError(
            "Não foi possível detectar o delimitador do CSV: nem vírgula nem "
            "ponto-e-vírgula produziram mais de uma coluna nas primeiras linhas "
            "do arquivo. Verifique se o arquivo está no formato esperado."
        )

    return best_delimiter


def parse_csv_bytes(data: bytes) -> list[dict]:
    """Decodifica bytes de um CSV (UTF-8 com ou sem BOM, CRLF ou LF) e
    detecta o delimitador (',' ou ';') automaticamente. Retorna uma lista de
    dicts com as chaves das colunas normalizadas (minúsculas, sem espaços)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("utf-8")

    delimiter = _detect_csv_delimiter(text)

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

# Prazo padrão de devolução. Ajuste aqui para mudar em todo o sistema.
PRAZO_PADRAO_DIAS = 14


def _to_date(value) -> date | None:
    """Converte str ISO / date / datetime em date. Vazio ou None -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def default_due_date(loan_date=None) -> date:
    """Data prevista de devolução: data do empréstimo + prazo padrão."""
    base = _to_date(loan_date) or date.today()
    return base + timedelta(days=PRAZO_PADRAO_DIAS)


def days_overdue(due_date, reference_date=None) -> int:
    """Dias de atraso em relação à data prevista (0 se em dia ou sem prazo).

    Vencer exatamente hoje ainda não é atraso — só a partir do dia seguinte."""
    due = _to_date(due_date)
    if due is None:
        return 0
    reference = _to_date(reference_date) or date.today()
    return max(0, (reference - due).days)


def is_overdue(due_date, status: str = "ativo", reference_date=None) -> bool:
    """Empréstimo atrasado: ainda ativo, com prazo definido e já vencido.
    due_date nulo (empréstimos anteriores ao recurso) nunca conta como atraso."""
    if status != "ativo":
        return False
    return days_overdue(due_date, reference_date) > 0


def request_loan(conn, book_id, user_id, due_date=None):
    """Registra um empréstimo. Sem due_date explícito, aplica o prazo padrão;
    com due_date, respeita o prazo ajustado no momento do registro."""
    book = conn.execute(
        text("SELECT * FROM books WHERE id = :id"), {"id": book_id}
    ).mappings().first()
    if book is None or book["status"] != "Disponível":
        raise ValueError("Livro indisponível para empréstimo.")
    now = datetime.now().isoformat(timespec="seconds")
    due = _to_date(due_date) or default_due_date(now)
    conn.execute(
        text(
            "INSERT INTO loans (book_id, user_id, loan_date, due_date, status) "
            "VALUES (:book_id, :user_id, :loan_date, :due_date, :status)"
        ),
        {
            "book_id": book_id,
            "user_id": user_id,
            "loan_date": now,
            "due_date": due.isoformat(),
            "status": "ativo",
        },
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


BOOK_STATUSES = ["Disponível", "Emprestado", "Em Manutenção"]
STATUS_EMOJI = {"Disponível": "🟢", "Emprestado": "🔴", "Em Manutenção": "🟡"}


def _book_search_controls(conn, key_prefix: str):
    """Campo de busca + filtros de categoria e status, compartilhados pelo
    Catálogo e pela Gestão de Livros. Devolve (busca, categoria, status)."""
    query = st.text_input(
        "Buscar por título, autor, código ou categoria", key=f"{key_prefix}_query"
    )
    col_cat, col_status = st.columns(2)
    category = col_cat.selectbox(
        "Categoria",
        [CATEGORY_FILTER_ALL] + list_book_categories(conn),
        key=f"{key_prefix}_category",
    )
    status = col_status.selectbox(
        "Status", [STATUS_FILTER_ALL] + BOOK_STATUSES, key=f"{key_prefix}_status"
    )

    # Mudou a busca ou os filtros -> volta para a 1ª página, senão o usuário
    # cairia numa página do meio de um resultado que acabou de mudar.
    signature = (query, category, status)
    signature_key = f"{key_prefix}_filter_signature"
    if st.session_state.get(signature_key) != signature:
        st.session_state[signature_key] = signature
        st.session_state[f"{key_prefix}_page"] = 1

    return query, category, status


def _paginate(total: int, key_prefix: str) -> int:
    """Navegação de páginas + "X–Y de N resultados". Devolve o OFFSET da
    página atual, mantendo a página escolhida em session_state e
    reposicionando quando os filtros encolhem o resultado."""
    page_key = f"{key_prefix}_page"
    total_pages = max(1, (total + BOOKS_PAGE_SIZE - 1) // BOOKS_PAGE_SIZE)
    page = min(st.session_state.get(page_key, 1), total_pages)
    st.session_state[page_key] = page

    first = (page - 1) * BOOKS_PAGE_SIZE + 1
    last = min(page * BOOKS_PAGE_SIZE, total)

    col_prev, col_info, col_next = st.columns([1, 3, 1])
    if col_prev.button(
        "◀ Anterior", key=f"{key_prefix}_prev", disabled=page <= 1, width="stretch"
    ):
        st.session_state[page_key] = page - 1
        st.rerun()
    col_info.markdown(
        f"<div style='text-align:center'>{first}–{last} de <b>{total}</b> resultado(s)"
        f" · página {page} de {total_pages}</div>",
        unsafe_allow_html=True,
    )
    if col_next.button(
        "Próxima ▶", key=f"{key_prefix}_next", disabled=page >= total_pages, width="stretch"
    ):
        st.session_state[page_key] = page + 1
        st.rerun()

    return (page - 1) * BOOKS_PAGE_SIZE


def show_catalog(conn, user):
    st.header("Catálogo de Livros")
    query, category, status = _book_search_controls(conn, "catalog")

    total = count_books(conn, query, category, status)
    if not total:
        st.info("Nenhum livro encontrado.")
        return

    offset = _paginate(total, "catalog")
    rows = list_books(conn, query, category, status, offset=offset)

    for r in rows:
        with st.container(border=True):
            info_col, action_col = st.columns([5, 2])
            with info_col:
                st.markdown(f"**{r['title']}**")
                st.caption(f"{r['author']} · {r['code']}")
                st.write(f"{STATUS_EMOJI.get(r['status'], '')} {r['status']}")
            with action_col:
                if user["role"] == "leitor" and r["status"] == "Disponível":
                    with st.popover("Pegar emprestado", width="stretch"):
                        due = st.date_input(
                            "Devolução prevista",
                            value=default_due_date(),
                            key=f"due_{r['id']}",
                            help=f"Prazo padrão: {PRAZO_PADRAO_DIAS} dias. "
                            "Ajuste antes de confirmar, se necessário.",
                        )
                        if st.button(
                            "Confirmar empréstimo",
                            key=f"borrow_{r['id']}",
                            width="stretch",
                        ):
                            request_loan(conn, r["id"], user["id"], due_date=due)
                            conn.commit()
                            st.success(f'Empréstimo de "{r["title"]}" registrado!')
                            st.rerun()


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
    statuses = BOOK_STATUSES
    query, category, status = _book_search_controls(conn, "manage")

    total = count_books(conn, query, category, status)
    if not total:
        st.info("Nenhum livro encontrado.")
        return

    offset = _paginate(total, "manage")
    rows = list_books(conn, query, category, status, offset=offset, newest_first=True)
    # uma única agregação para a página inteira, em vez de 2 queries por livro
    loan_summary = loan_summary_for_books(conn, [r["id"] for r in rows])

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
            summary = loan_summary.get(r["id"], {"total": 0, "ativos": 0})
            has_active_loan = summary["ativos"] > 0
            loan_count = summary["total"]

            if has_active_loan:
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


# ---------------------------------------------------------------------------
# Reconciliação: livros marcados como "Emprestado" sem empréstimo registrado
# (vieram assim da carga inicial, com o nome de quem pegou só na planilha)
# ---------------------------------------------------------------------------

def _unreconciled_where(query: str = ""):
    """Livro com status 'Emprestado' e sem NENHUM empréstimo ativo na base."""
    active_loan_exists = (
        select(loans_table.c.id)
        .where(loans_table.c.book_id == books_table.c.id)
        .where(loans_table.c.status == "ativo")
        .exists()
    )
    clauses = [books_table.c.status == "Emprestado", ~active_loan_exists]

    term = normalize_search_term(query)
    if term:
        pattern = f"%{term}%"
        searchable = (
            books_table.c.title,
            books_table.c.author,
            books_table.c.code,
            func.coalesce(books_table.c.category, ""),
        )
        clauses.append(
            or_(*[_sql_unaccent(col).ilike(pattern) for col in searchable])
        )
    return clauses


def count_unreconciled_books(conn, query: str = "") -> int:
    stmt = select(func.count()).select_from(books_table)
    for clause in _unreconciled_where(query):
        stmt = stmt.where(clause)
    return conn.execute(stmt).scalar_one()


def list_unreconciled_books(
    conn, query: str = "", limit: int = BOOKS_PAGE_SIZE, offset: int = 0
):
    stmt = select(books_table)
    for clause in _unreconciled_where(query):
        stmt = stmt.where(clause)
    stmt = stmt.order_by(books_table.c.title).limit(limit).offset(offset)
    return conn.execute(stmt).mappings().all()


def _lock_unreconciled_book(conn, book_id):
    """Revalida o livro no momento da execução e o trava para escrita.

    Devolve a linha do livro; levanta ValueError se ele já tiver sido
    reconciliado por outra sessão entre o carregamento da tela e a
    confirmação. with_for_update() vira FOR UPDATE no Postgres (trava a
    linha) e é ignorado no SQLite, onde a transação já serializa a escrita.
    """
    book = conn.execute(
        select(books_table).where(books_table.c.id == book_id).with_for_update()
    ).mappings().first()

    if book is None:
        raise ValueError("Livro não encontrado — ele pode ter sido removido.")
    if book["status"] != "Emprestado":
        raise ValueError(
            f"Este livro não está mais como 'Emprestado' (agora está "
            f"'{book['status']}'). Outra pessoa já o reconciliou."
        )
    if get_active_loan_for_book(conn, book_id) is not None:
        raise ValueError(
            "Este livro já tem um empréstimo ativo registrado — "
            "outra pessoa já o reconciliou."
        )
    return book


def reconcile_register_loan(conn, book_id, user_id, loan_date=None, due_date=None):
    """Regulariza o livro criando o empréstimo ativo que faltava.

    O status do livro continua 'Emprestado' (ele segue fisicamente com o
    leitor) — o que muda é que agora existe registro de quem está com ele.
    """
    _lock_unreconciled_book(conn, book_id)

    borrower = conn.execute(
        select(users_table).where(users_table.c.id == user_id)
    ).mappings().first()
    if borrower is None:
        raise ValueError("Leitor não encontrado.")

    loan_day = _to_date(loan_date)
    loan_value = (
        loan_day.isoformat()
        if loan_day
        else datetime.now().isoformat(timespec="seconds")
    )
    due = _to_date(due_date) or default_due_date(loan_value)

    conn.execute(
        text(
            "INSERT INTO loans (book_id, user_id, loan_date, due_date, status) "
            "VALUES (:book_id, :user_id, :loan_date, :due_date, 'ativo')"
        ),
        {
            "book_id": book_id,
            "user_id": user_id,
            "loan_date": loan_value,
            "due_date": due.isoformat(),
        },
    )


def reconcile_mark_returned(conn, book_id) -> None:
    """O livro voltou fisicamente: libera para o catálogo sem inventar
    histórico de empréstimo (não sabemos quem estava com ele)."""
    _lock_unreconciled_book(conn, book_id)

    result = conn.execute(
        text(
            "UPDATE books SET status = 'Disponível' "
            "WHERE id = :id AND status = 'Emprestado'"
        ),
        {"id": book_id},
    )
    if result.rowcount == 0:
        raise ValueError(
            "Não foi possível liberar o livro: o status mudou durante a operação."
        )


# ---------------------------------------------------------------------------
# Painel de indicadores e exportação CSV
# ---------------------------------------------------------------------------

# Não existe (ainda) remoção de leitor no sistema, então este rótulo é uma
# proteção defensiva: se um empréstimo ficar órfão (usuário apagado direto no
# banco), a exportação mostra isto em vez de quebrar ou omitir a linha —
# nenhum dado pessoal do leitor removido é exposto.
ANONYMIZED_BORROWER_LABEL = "Leitor removido"


def get_dashboard_metrics(conn, reference_date=None) -> dict:
    """Indicadores do acervo calculados com COUNT/GROUP BY no banco — nenhuma
    tabela é carregada inteira em memória."""
    by_status = dict(
        conn.execute(
            select(books_table.c.status, func.count()).group_by(books_table.c.status)
        ).all()
    )

    total_livros = conn.execute(
        select(func.count()).select_from(books_table)
    ).scalar_one()

    emprestimos_ativos = conn.execute(
        select(func.count())
        .select_from(loans_table)
        .where(loans_table.c.status == "ativo")
    ).scalar_one()

    today = (_to_date(reference_date) or date.today()).isoformat()
    emprestimos_atrasados = conn.execute(
        select(func.count())
        .select_from(loans_table)
        .where(loans_table.c.status == "ativo")
        .where(loans_table.c.due_date.isnot(None))
        .where(loans_table.c.due_date < today)
    ).scalar_one()

    leitores = conn.execute(
        select(func.count()).select_from(users_table).where(users_table.c.role == "leitor")
    ).scalar_one()

    return {
        "total_livros": total_livros,
        "disponiveis": by_status.get("Disponível", 0),
        "emprestados": by_status.get("Emprestado", 0),
        "em_manutencao": by_status.get("Em Manutenção", 0),
        "emprestimos_ativos": emprestimos_ativos,
        "emprestimos_atrasados": emprestimos_atrasados,
        "leitores": leitores,
        "pendentes_reconciliacao": count_unreconciled_books(conn),
    }


def _to_excel_csv_bytes(header: list[str], rows) -> bytes:
    """CSV em UTF-8 com BOM e QUOTE_ALL.

    O BOM faz o Excel em português reconhecer o UTF-8 (sem ele os acentos
    saem corrompidos) e o QUOTE_ALL evita que um título com ';' ou ','
    quebre as colunas."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


BOOKS_EXPORT_HEADER = ["Código", "Título", "Autor", "Categoria", "Status"]


def export_books_csv(conn) -> bytes:
    """Catálogo completo em CSV."""
    rows = conn.execute(
        select(
            books_table.c.code,
            books_table.c.title,
            books_table.c.author,
            func.coalesce(books_table.c.category, ""),
            books_table.c.status,
        ).order_by(books_table.c.title)
    ).all()
    return _to_excel_csv_bytes(BOOKS_EXPORT_HEADER, rows)


LOANS_EXPORT_HEADER = [
    "Livro",
    "Código",
    "Leitor",
    "E-mail",
    "Data do empréstimo",
    "Devolução prevista",
    "Data de devolução",
    "Status",
]


def export_loans_csv(conn) -> bytes:
    """Histórico completo de empréstimos em CSV.

    Usa LEFT JOIN em users: um empréstimo cujo leitor não existe mais entra
    como "Leitor removido", sem e-mail — a linha do histórico é preservada
    sem expor dados pessoais (com INNER JOIN ela simplesmente sumiria)."""
    rows = conn.execute(
        text(
            """
            SELECT books.title, books.code,
                   users.full_name, users.email,
                   loans.loan_date, loans.due_date, loans.return_date, loans.status
            FROM loans
            JOIN books ON books.id = loans.book_id
            LEFT JOIN users ON users.id = loans.user_id
            ORDER BY loans.loan_date DESC
            """
        )
    ).mappings().all()

    data = [
        (
            r["title"],
            r["code"],
            r["full_name"] if r["full_name"] is not None else ANONYMIZED_BORROWER_LABEL,
            r["email"] or "",
            r["loan_date"] or "",
            r["due_date"] or "",
            r["return_date"] or "",
            r["status"],
        )
        for r in rows
    ]
    return _to_excel_csv_bytes(LOANS_EXPORT_HEADER, data)


def list_borrowers(conn):
    """Leitores cadastrados, para escolher quem está com o livro."""
    stmt = (
        select(users_table)
        .where(users_table.c.role == "leitor")
        .order_by(users_table.c.full_name)
    )
    return conn.execute(stmt).mappings().all()


def list_active_loans(conn, only_overdue: bool = False, reference_date=None):
    """Empréstimos ativos com dados de contato do leitor e prazo.

    Com only_overdue=True devolve apenas os vencidos — due_date nulo fica de
    fora, porque empréstimo sem prazo nunca é atraso."""
    rows = conn.execute(
        text(
            """
            SELECT loans.id AS loan_id, books.title, books.code,
                   users.full_name, users.email, users.phone,
                   loans.loan_date, loans.due_date, loans.status
            FROM loans
            JOIN books ON books.id = loans.book_id
            JOIN users ON users.id = loans.user_id
            WHERE loans.status = 'ativo'
            ORDER BY loans.due_date IS NULL, loans.due_date, loans.loan_date
            """
        )
    ).mappings().all()

    if only_overdue:
        rows = [
            r for r in rows if is_overdue(r["due_date"], r["status"], reference_date)
        ]
    return rows


def _due_date_caption(due_date, status, reference_date=None) -> str:
    """Texto do prazo, com destaque quando vencido."""
    if not due_date:
        return "Sem prazo definido"
    late = days_overdue(due_date, reference_date) if status == "ativo" else 0
    if late > 0:
        return f"🔴 **ATRASADO há {late} dia(s)** — prevista para {due_date}"
    return f"Devolução prevista: {due_date}"


def show_loan_management(conn):
    st.header("Empréstimos ativos")

    only_overdue = st.checkbox("Somente atrasados", key="loans_only_overdue")
    rows = list_active_loans(conn, only_overdue=only_overdue)

    if not rows:
        st.info(
            "Nenhum empréstimo atrasado no momento."
            if only_overdue
            else "Nenhum empréstimo ativo no momento."
        )
        return

    overdue_total = sum(1 for r in rows if is_overdue(r["due_date"], r["status"]))
    if overdue_total and not only_overdue:
        st.warning(f"⚠️ {overdue_total} empréstimo(s) em atraso.")

    for r in rows:
        late = is_overdue(r["due_date"], r["status"])
        with st.container(border=True):
            info_col, action_col = st.columns([5, 2])
            with info_col:
                st.markdown(f"{'🔴 ' if late else ''}**{r['title']}** ({r['code']})")
                st.caption(f"{r['full_name']} · {r['email']} · {r['phone'] or '-'}")
                st.write(f"Emprestado em {r['loan_date']}")
                if late:
                    st.error(_due_date_caption(r["due_date"], r["status"]))
                else:
                    st.caption(_due_date_caption(r["due_date"], r["status"]))
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
            SELECT loans.id AS loan_id, loans.loan_date, loans.due_date,
                   loans.return_date, loans.status,
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

    overdue_total = sum(1 for r in filtered if is_overdue(r["due_date"], r["status"]))
    st.write(f"{len(filtered)} empréstimo(s) encontrado(s).")
    if overdue_total:
        st.warning(f"⚠️ {overdue_total} deles em atraso.")

    status_emoji = {"ativo": "🔴", "devolvido": "🟢"}

    def _situacao(r):
        late = days_overdue(r["due_date"]) if r["status"] == "ativo" else 0
        if late > 0:
            return f"🔴 atrasado há {late} dia(s)"
        return f"{status_emoji.get(r['status'], '')} {r['status']}"

    table = [
        {
            "Livro": f"{r['book_title']} ({r['book_code']})",
            "Leitor": r["full_name"],
            "E-mail": r["email"],
            "Telefone": r["phone"] or "-",
            "Emprestado em": r["loan_date"],
            "Prevista": r["due_date"] or "-",
            "Devolvido em": r["return_date"] or "-",
            "Status": _situacao(r),
        }
        for r in filtered
    ]
    st.dataframe(table, width="stretch")

    if user_options[user_choice] is not None:
        user_loans = [r for r in rows if r["user_id"] == user_options[user_choice]]
        with st.expander(f"📋 Todos os empréstimos de {user_choice}", expanded=True):
            for r in user_loans:
                late = days_overdue(r["due_date"]) if r["status"] == "ativo" else 0
                marker = f" — 🔴 atrasado há {late} dia(s)" if late else ""
                st.write(
                    f"- **{r['book_title']}** ({r['book_code']}) — "
                    f"{r['loan_date']} → {r['return_date'] or 'em aberto'} "
                    f"[prevista: {r['due_date'] or 'sem prazo'}] [{r['status']}]{marker}"
                )


def show_my_loans(conn, user):
    st.header("Meus Empréstimos")

    st.subheader("Livros em minha posse")
    active = conn.execute(
        text(
            """
            SELECT loans.id AS loan_id, books.title, books.code,
                   loans.loan_date, loans.due_date, loans.status
            FROM loans JOIN books ON books.id = loans.book_id
            WHERE loans.user_id = :user_id AND loans.status = 'ativo'
            """
        ),
        {"user_id": user["id"]},
    ).mappings().all()

    if not active:
        st.info("Você não tem livros emprestados no momento.")
    for r in active:
        late = is_overdue(r["due_date"], r["status"])
        with st.container(border=True):
            info_col, action_col = st.columns([5, 2])
            with info_col:
                st.markdown(f"{'🔴 ' if late else ''}**{r['title']}** ({r['code']})")
                st.caption(f"Desde {r['loan_date']}")
                if late:
                    st.error(_due_date_caption(r["due_date"], r["status"]))
                else:
                    st.caption(_due_date_caption(r["due_date"], r["status"]))
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


def show_admin_dashboard(conn):
    st.header("Painel")

    m = get_dashboard_metrics(conn)

    st.subheader("Acervo")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total de livros", m["total_livros"])
    col2.metric("🟢 Disponíveis", m["disponiveis"])
    col3.metric("🔴 Emprestados", m["emprestados"])
    col4.metric("🟡 Em manutenção", m["em_manutencao"])

    st.subheader("Empréstimos e leitores")
    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Empréstimos ativos", m["emprestimos_ativos"])
    col6.metric("⚠️ Atrasados", m["emprestimos_atrasados"])
    col7.metric("Leitores cadastrados", m["leitores"])
    col8.metric("Pendentes de reconciliação", m["pendentes_reconciliacao"])

    if m["emprestimos_atrasados"]:
        st.warning(
            f"{m['emprestimos_atrasados']} empréstimo(s) em atraso — veja em "
            "**Empréstimos**, filtro *Somente atrasados*."
        )
    if m["pendentes_reconciliacao"]:
        st.info(
            f"{m['pendentes_reconciliacao']} livro(s) marcados como emprestados sem "
            "registro de quem está com eles — veja em **Reconciliação**."
        )

    st.subheader("Exportar dados")
    st.caption(
        "CSV em UTF-8 com BOM e todos os campos entre aspas — abre direto no "
        "Excel em português, sem corromper acentos nem quebrar colunas."
    )
    hoje = date.today().isoformat()
    col_books, col_loans = st.columns(2)
    col_books.download_button(
        "📚 Catálogo de livros (CSV)",
        data=export_books_csv(conn),
        file_name=f"catalogo_livros_{hoje}.csv",
        mime="text/csv",
        width="stretch",
    )
    col_loans.download_button(
        "📄 Histórico de empréstimos (CSV)",
        data=export_loans_csv(conn),
        file_name=f"historico_emprestimos_{hoje}.csv",
        mime="text/csv",
        width="stretch",
    )


def show_loan_reconciliation(conn):
    st.header("Reconciliação de empréstimos")
    st.caption(
        "Livros marcados como **Emprestado** que não têm empréstimo registrado no "
        "sistema — vieram assim da carga inicial do acervo. Regularize indicando "
        "quem está com o livro, ou marque como devolvido se ele já voltou."
    )

    query = st.text_input(
        "Buscar por título, autor, código ou categoria", key="reconcile_query"
    )

    signature_key = "reconcile_filter_signature"
    if st.session_state.get(signature_key) != query:
        st.session_state[signature_key] = query
        st.session_state["reconcile_page"] = 1

    total = count_unreconciled_books(conn, query)
    total_geral = count_unreconciled_books(conn)
    st.metric("Pendentes de reconciliação", total_geral)

    if not total:
        st.success(
            "Nenhum livro pendente de reconciliação."
            if not query
            else "Nenhum livro pendente corresponde à busca."
        )
        return

    borrowers = list_borrowers(conn)
    if not borrowers:
        st.warning(
            "Nenhum leitor cadastrado ainda — só é possível marcar como devolvido. "
            "Cadastre o leitor para poder registrar o empréstimo."
        )
    borrower_labels = {f"{b['full_name']} ({b['email']})": b["id"] for b in borrowers}

    offset = _paginate(total, "reconcile")
    rows = list_unreconciled_books(conn, query, offset=offset)

    for r in rows:
        with st.container(border=True):
            st.markdown(f"**{r['title']}** ({r['code']})")
            st.caption(f"{r['author']} · {r['category'] or 'sem categoria'}")

            col_loan, col_return = st.columns(2)

            with col_loan:
                with st.popover("📝 Registrar empréstimo", width="stretch"):
                    if not borrower_labels:
                        st.info("Cadastre um leitor primeiro.")
                    else:
                        who = st.selectbox(
                            "Quem está com o livro",
                            list(borrower_labels.keys()),
                            key=f"rec_user_{r['id']}",
                        )
                        loan_day = st.date_input(
                            "Data do empréstimo",
                            value=date.today(),
                            key=f"rec_loan_date_{r['id']}",
                        )
                        due = st.date_input(
                            "Devolução prevista",
                            value=default_due_date(),
                            key=f"rec_due_{r['id']}",
                        )
                        if st.button(
                            "Confirmar registro",
                            key=f"rec_confirm_{r['id']}",
                            width="stretch",
                        ):
                            try:
                                reconcile_register_loan(
                                    conn,
                                    r["id"],
                                    borrower_labels[who],
                                    loan_date=loan_day,
                                    due_date=due,
                                )
                                conn.commit()
                                st.success(
                                    f'Empréstimo de "{r["title"]}" registrado para {who}.'
                                )
                                st.rerun()
                            except (ValueError, IntegrityError) as exc:
                                conn.rollback()
                                st.error(str(exc))

            with col_return:
                if st.button(
                    "✅ Marcar como devolvido",
                    key=f"rec_returned_{r['id']}",
                    width="stretch",
                ):
                    try:
                        reconcile_mark_returned(conn, r["id"])
                        conn.commit()
                        st.success(f'"{r["title"]}" liberado no catálogo.')
                        st.rerun()
                    except (ValueError, IntegrityError) as exc:
                        conn.rollback()
                        st.error(str(exc))


def show_csv_import(conn):
    st.header("Importar carga de livros (CSV)")
    st.caption(
        "O arquivo **não precisa** vir no formato interno: depois do upload você "
        "escolhe qual coluna do arquivo corresponde a cada campo. Delimitador `,` ou "
        "`;` e encoding UTF-8 (com ou sem BOM) são detectados automaticamente."
    )
    uploaded = st.file_uploader("Selecione o arquivo CSV", type=["csv"])

    if uploaded is None:
        for key in ("csv_import_rows", "csv_import_signature"):
            st.session_state.pop(key, None)
        return

    # Assinatura por (nome, tamanho): reprocessa se o conteúdo mudar, mesmo
    # que o nome do arquivo permaneça o mesmo — reenviar uma versão corrigida
    # do arquivo com o mesmo nome não pode reaproveitar um resultado velho.
    upload_signature = (uploaded.name, uploaded.size)
    if st.session_state.get("csv_import_signature") != upload_signature:
        try:
            st.session_state.csv_import_rows = parse_csv_bytes(uploaded.getvalue())
        except (UnicodeDecodeError, csv.Error, ValueError) as exc:
            st.error(f"Não foi possível ler o arquivo CSV: {exc}")
            return
        st.session_state.csv_import_signature = upload_signature

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
        for key in ("csv_import_rows", "csv_import_signature"):
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
                    "Painel",
                    "Catálogo",
                    "Gestão de Livros",
                    "Empréstimos",
                    "Reconciliação",
                    "Histórico completo",
                    "Importar CSV",
                ],
            )
        else:
            page = st.radio("Menu", ["Catálogo", "Meus Empréstimos"])
        if st.button("Sair"):
            st.session_state.user = None
            st.rerun()

    if page == "Painel":
        show_admin_dashboard(conn)
    elif page == "Catálogo":
        show_catalog(conn, user)
    elif page == "Gestão de Livros":
        show_book_management(conn)
    elif page == "Empréstimos":
        show_loan_management(conn)
    elif page == "Reconciliação":
        show_loan_reconciliation(conn)
    elif page == "Histórico completo":
        show_admin_loan_history(conn)
    elif page == "Meus Empréstimos":
        show_my_loans(conn, user)
    elif page == "Importar CSV":
        show_csv_import(conn)

# CSS mira DOM interno do Streamlit (não é API pública).
# Verificado em 1.61.x via inspeção de DOM. Se a borda vinho voltar a
# ficar cinza após um upgrade, reinspecionar os data-testid dos containers.
def _inject_card_border_css() -> None:
    """Ajustes visuais para identidade do Centro Cultural Esplanada:
    1. Tinge a borda de todo st.container(border=True) em vinho suave, compatível
       com Streamlit moderno ([data-testid="stLayoutWrapper"] > [data-testid="stVerticalBlock"])
       e versões legadas ([data-testid="stVerticalBlockBorderWrapper"]).
    2. Dimensiona a logo na barra lateral para que fique proporcional e legível.
    3. Otimiza o espaçamento entre blocos em viewports mobile."""
    st.markdown(
        """
        <style>
        /* Borda de todo st.container(border=True) em vinho suave */
        [data-testid="stVerticalBlockBorderWrapper"],
        [data-testid="stLayoutWrapper"] > [data-testid="stVerticalBlock"],
        div[data-testid="stVerticalBlock"]:has(> div[data-testid="stLayoutWrapper"]) {
            border-color: rgba(122, 31, 43, 0.3) !important;
        }

        /* Logo na barra lateral proporcional, nítida e legível */
        [data-testid="stSidebarLogo"],
        [data-testid="stLogo"] img,
        [data-testid="stSidebarHeader"] img {
            height: auto !important;
            max-height: 48px !important;
            width: auto !important;
            max-width: 180px !important;
            object-fit: contain !important;
        }

        /* Ajustes de espaçamento em dispositivos móveis */
        @media (max-width: 640px) {
            [data-testid="stHorizontalBlock"] {
                gap: 0.5rem !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main():
    st.set_page_config(
        page_title="Biblioteca Comunitária",
        page_icon="assets/logo_pequeno_cce.png",
        layout="wide",
    )
    st.logo(
        "assets/logo_cce.png",
        size="large",
        icon_image="assets/logo_pequeno_cce.png",
    )
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
