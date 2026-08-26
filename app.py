"""
app.py — Protótipo funcional: Sistema de Biblioteca Comunitária
=================================================================
Stack: Python + Streamlit + Postgres (Supabase) via SQLAlchemy (lógica de
negócio e UI num único arquivo). Ver README.md para instruções completas.

Como rodar:
    pip install -r requirements.txt
    cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # preencha DATABASE_URL
    streamlit run app.py

Um administrador é criado automaticamente na 1ª execução (e sempre que o
banco fica sem nenhum admin), já obrigado a trocar a senha no primeiro login.
As credenciais iniciais vêm dos Secrets (BOOTSTRAP_ADMIN_EMAIL e
BOOTSTRAP_ADMIN_PASSWORD) — nunca de um literal no código, porque este
repositório é público. Sem elas configuradas o app exibe a instrução e para,
em vez de criar um admin com senha conhecida.

Recuperação de senha é presencial: um admin redefine a senha pela tela
"Gestão de Usuários" e entrega a senha temporária ao usuário. Não há fluxo
por e-mail — ver README, seção "Por que não há recuperação por e-mail".

⚠️ Este é um PROTÓTIPO para validar as regras de negócio (RBAC simples,
geração de código do livro, fluxo de empréstimo/devolução). Não há CSRF
dedicado — a versão de produção (Next.js) deve usar um provedor de auth de
verdade (NextAuth/Supabase Auth).
"""

import csv
import hashlib
import hmac
import io
import itertools
import re
import secrets
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta

import bcrypt
import streamlit as st
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
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
from sqlalchemy.exc import IntegrityError, SQLAlchemyError


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


def book_code_prefix(author_full_name: str, treat_suffix_as_surname: bool = True) -> str:
    """As 4 letras iniciais do código, derivadas só do nome do autor:
    [3 primeiras letras do último token] + [1ª letra do primeiro nome].

    Ex.: "João Mellão Neto" -> "NETJ"

    É o prefixo, e não a string do autor, que identifica a sequência de
    numeração. Grafias diferentes da mesma pessoa convergem aqui: tanto
    "G. K. Chesterton" quanto "Gilbert Keith Chesterton" produzem "CHEG",
    e por isso compartilham a mesma sequência de sequenciais.
    """
    if not author_full_name or not author_full_name.strip():
        raise ValueError("Nome do autor não pode ser vazio.")

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

    return f"{surname_code}{first_initial}"


def generate_book_code(
    author_full_name: str,
    previous_sequence: int,
    treat_suffix_as_surname: bool = True,
) -> str:
    """
    [prefixo de 4 letras do autor] - [sequencial de 3 dígitos]

    Ex.: "João Mellão Neto", nada emitido ainda -> "NETJ-001"

    `previous_sequence` é o MAIOR sequencial já emitido para o prefixo, não a
    contagem de livros do autor — ver BookCodeAllocator para o porquê.
    """
    prefix = book_code_prefix(author_full_name, treat_suffix_as_surname)
    if previous_sequence < 0:
        raise ValueError("previous_sequence deve ser >= 0.")

    return f"{prefix}-{str(previous_sequence + 1).zfill(3)}"


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
    # password_hash guarda bcrypt (salt embutido no próprio hash) para contas
    # novas/já migradas; salt só é usado para o formato legado sha256+salt e
    # fica '' assim que a senha é trocada ou re-hasheada — ver hash_password.
    Column("password_hash", Text, nullable=False),
    Column("salt", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("must_change_password", Integer, nullable=False, server_default="0"),
    # Incrementado sempre que um admin redefine a senha da conta. A view da
    # sessão carrega o valor visto no login e ele é reconferido a cada rerun
    # (ver _session_is_current): a sessão que ficou aberta em outra aba com a
    # senha antiga cai no próximo clique, em vez de continuar valendo.
    Column("session_version", Integer, nullable=False, server_default="0"),
    # Cadastro simplificado: leitor criado pelo balcão, para registrar o
    # empréstimo de quem não tem (ou não quer) conta. Não tem senha utilizável
    # e nunca autentica — ver authenticate() e UNUSABLE_PASSWORD_HASH. Vira
    # conta completa por convert_simplified_to_full, preservando o histórico.
    Column("is_simplified", Integer, nullable=False, server_default="0"),
    Column("created_at", Text, nullable=False),
    CheckConstraint("role IN ('admin','leitor')", name="ck_users_role"),
    CheckConstraint("must_change_password IN (0,1)", name="ck_users_must_change_password"),
    CheckConstraint("is_simplified IN (0,1)", name="ck_users_is_simplified"),
    # Um cadastro simplificado é sempre leitor: admin sem senha utilizável
    # seria uma conta administrativa impossível de usar e impossível de
    # recuperar (o reset de senha é bloqueado para conta simplificada).
    CheckConstraint(
        "is_simplified = 0 OR role = 'leitor'", name="ck_users_simplified_is_reader"
    ),
)

# Trilha de auditoria das ações administrativas sobre contas (hoje só o
# reset de senha; mudança de papel entra aqui quando existir). Guarda os
# e-mails além dos ids porque o log precisa continuar legível depois que a
# conta envolvida for removida — por isso também não há FK nem CASCADE.
admin_audit_table = Table(
    "admin_audit_log",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("action", Text, nullable=False),
    Column("actor_user_id", Integer),
    Column("actor_email", Text, nullable=False),
    Column("target_user_id", Integer),
    Column("target_email", Text, nullable=False),
    Column("created_at", Text, nullable=False),
)

# Contador de tentativas de login malsucedidas por e-mail, para bloqueio
# temporário por força bruta. Tabela própria (em vez de colunas em users)
# porque precisa registrar tentativas mesmo contra e-mails que não existem,
# sem vazar essa distinção para quem está tentando (mensagem de erro genérica
# tanto para e-mail inexistente quanto para senha errada).
login_attempts_table = Table(
    "login_attempts",
    metadata,
    Column("email", Text, primary_key=True),
    Column("failed_count", Integer, nullable=False, server_default="0"),
    Column("locked_until", Text),
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
    # Colunas usadas em junção e filtro nas telas de empréstimo, no painel e na
    # subconsulta EXISTS da reconciliação. Sem índice, cada uma delas é uma
    # varredura da tabela inteira.
    Index("ix_loans_book_id", "book_id"),
    Index("ix_loans_user_id", "user_id"),
    Index("ix_loans_status", "status"),
    Index("ix_loans_due_date", "due_date"),
)

# Índices do acervo. A busca textual NÃO é coberta por eles: _sql_unaccent
# envolve a coluna em REPLACE aninhados, e uma expressão assim não usa índice
# de coluna. Cobrir a busca exigiria coluna gerada + pg_trgm, que só existe no
# Postgres e quebraria a paridade com o SQLite dos testes — não compensa no
# volume atual (~2,5 mil livros). Estes aqui servem aos filtros e à ordenação.
Index("ix_books_status", books_table.c.status)
Index("ix_books_category", books_table.c.category)
Index("ix_books_title", books_table.c.title)
Index("ix_books_author", books_table.c.author)


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


BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def _is_bcrypt_hash(digest: str) -> bool:
    return isinstance(digest, str) and digest.startswith(BCRYPT_PREFIXES)


def hash_password(password: str) -> str:
    """Hash bcrypt (custo default da lib) — o salt vem embutido no hash."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _hash_password_legacy(password: str, salt: str) -> str:
    """Formato antigo (sha256 + salt), mantido só para verificar contas que
    ainda não logaram desde a migração para bcrypt — ver authenticate()."""
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def verify_password(password: str, digest: str, salt: str) -> bool:
    if _is_bcrypt_hash(digest):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), digest.encode("utf-8"))
        except ValueError:
            return False
    return hmac.compare_digest(_hash_password_legacy(password, salt or ""), digest)


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


def _migrate_add_users_must_change_password(engine: Engine) -> None:
    """Adiciona users.must_change_password em bancos criados antes deste
    recurso (metadata.create_all só cria tabelas que faltam, nunca colunas).

    Contas admin existentes só puderam nascer com a senha padrão do
    bootstrap, que antes ficava exibida na própria tela de login — força a
    troca no próximo login delas também, fechando essa exposição em bancos
    já em produção. Contas leitor não são afetadas.
    """
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("users")}
    if "must_change_password" in columns:
        return
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        )
        conn.execute(text("UPDATE users SET must_change_password = 1 WHERE role = 'admin'"))


def _migrate_add_users_session_version(engine: Engine) -> None:
    """Adiciona users.session_version em bancos criados antes deste recurso.

    Todas as contas começam na versão 0 — igual ao que uma sessão aberta
    antes da migração carregaria — então ninguém é deslogado pela migração
    em si. A invalidação só acontece a partir do primeiro reset de senha.
    """
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("users")}
    if "session_version" in columns:
        return
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
        )


def _migrate_add_users_is_simplified(engine: Engine) -> None:
    """Adiciona users.is_simplified em bancos criados antes deste recurso.

    Toda conta existente nasceu com senha própria e continua podendo logar,
    então o default 0 é o valor correto para todas elas — a migração não muda
    o comportamento de ninguém. As CheckConstraints declaradas na Table valem
    para bancos novos (create_all); aqui o ADD COLUMN vai sem elas, porque
    Postgres não aceita adicionar constraint de tabela junto do ADD COLUMN e
    a regra também é aplicada no código (create_simplified_reader é o único
    caminho que grava 1).
    """
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("users")}
    if "is_simplified" in columns:
        return
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE users ADD COLUMN is_simplified INTEGER NOT NULL DEFAULT 0")
        )


def _index_statements(concurrently: bool) -> list[str]:
    """CREATE INDEX de todos os índices declarados no metadata.

    Gerado a partir do próprio metadata para não haver duas listas de índices
    para divergirem: declarar um Index na Table basta para bancos novos (via
    create_all) e para os já existentes (via esta migração).
    """
    modifier = "CONCURRENTLY " if concurrently else ""
    statements = []
    for table in metadata.tables.values():
        for index in sorted(table.indexes, key=lambda i: i.name):
            columns = ", ".join(column.name for column in index.columns)
            statements.append(
                f"CREATE INDEX {modifier}IF NOT EXISTS {index.name} "
                f"ON {table.name} ({columns})"
            )
    return statements


def _migrate_add_indexes(engine: Engine) -> None:
    """Cria os índices que faltam em um banco já em uso.

    metadata.create_all() cria índices apenas junto com a tabela — numa tabela
    que já existe (o Supabase do CCE) ele não faz nada, então a criação precisa
    ser explícita.

    No Postgres usa CONCURRENTLY, para não travar escrita no acervo enquanto o
    índice é construído. CONCURRENTLY não pode rodar dentro de transação, daí o
    isolation_level AUTOCOMMIT; no SQLite dos testes o CREATE INDEX comum roda
    na transação normal.

    Falha em criar índice não impede o app de subir: índice é desempenho, não
    correção. Um CONCURRENTLY interrompido deixa índice inválido no Postgres,
    que a próxima execução não recria (IF NOT EXISTS o considera existente) —
    se a lentidão persistir, conferir com \\di+ e recriar à mão.
    """
    is_postgres = engine.dialect.name == "postgresql"
    statements = _index_statements(concurrently=is_postgres)

    if is_postgres:
        context = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    else:
        context = engine.begin()

    with context as conn:
        for statement in statements:
            try:
                conn.execute(text(statement))
            except SQLAlchemyError:
                continue


def create_schema(engine: Engine) -> None:
    metadata.create_all(engine)
    _migrate_add_loans_due_date(engine)
    _migrate_add_users_must_change_password(engine)
    _migrate_add_users_session_version(engine)
    _migrate_add_users_is_simplified(engine)
    _migrate_add_indexes(engine)


BOOTSTRAP_ADMIN_EMAIL_KEY = "BOOTSTRAP_ADMIN_EMAIL"
BOOTSTRAP_ADMIN_PASSWORD_KEY = "BOOTSTRAP_ADMIN_PASSWORD"

def bootstrap_not_configured_message() -> str:
    """Instrução exibida quando não há admin e nem credenciais iniciais.

    É função, e não constante, porque interpola MIN_PASSWORD_LENGTH — definido
    mais adiante, na seção de senhas. Em tempo de import ele ainda não existe.
    """
    return (
        "**O banco não tem nenhum administrador e as credenciais iniciais não estão "
        "configuradas.**\n\n"
        f"Configure `{BOOTSTRAP_ADMIN_EMAIL_KEY}` e `{BOOTSTRAP_ADMIN_PASSWORD_KEY}` nos "
        "Secrets do app (no Streamlit Community Cloud: *Settings → Secrets*; localmente: "
        "`.streamlit/secrets.toml`) e recarregue a página. A senha precisa ter pelo menos "
        f"{MIN_PASSWORD_LENGTH} caracteres.\n\n"
        "O administrador será criado com essas credenciais e obrigado a trocar a senha no "
        "primeiro login."
    )


class BootstrapAdminNotConfigured(RuntimeError):
    """Não há admin no banco e não há credenciais iniciais configuradas.

    Deliberadamente NÃO existe uma senha padrão de fallback: este repositório é
    público, então qualquer literal no código seria uma credencial publicada.
    Um app que sobe sem admin é recuperável — basta configurar o segredo e
    recarregar. Um admin com senha conhecida por qualquer pessoa, não.
    """


def _get_bootstrap_admin_credentials() -> tuple[str, str] | None:
    """Credenciais iniciais do admin vindas dos Secrets, ou None se não
    estiverem configuradas (ausentes, vazias ou com senha fraca demais)."""
    try:
        email = st.secrets[BOOTSTRAP_ADMIN_EMAIL_KEY]
        password = st.secrets[BOOTSTRAP_ADMIN_PASSWORD_KEY]
    except Exception:
        return None

    email = (email or "").strip().lower()
    password = password or ""
    if not email or password_strength_error(password):
        return None
    return email, password


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
        #
        # A checagem é pela ausência de ADMIN, não de usuários: um banco com
        # leitores cadastrados mas sem nenhum admin (anonimização, migração,
        # SQL manual) precisa recriar o acesso, senão fica irrecuperável pela
        # aplicação.
        #
        # As credenciais iniciais vêm dos Secrets, NUNCA de um literal: este
        # repositório é público, e uma senha no código é uma senha publicada.
        # Sem elas configuradas, não criamos admin nenhum — ver
        # BootstrapAdminNotConfigured.
        admin_count = conn.execute(
            text("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'")
        ).mappings().first()["n"]
        if admin_count == 0:
            credentials = _get_bootstrap_admin_credentials()
            if credentials is None:
                raise BootstrapAdminNotConfigured(bootstrap_not_configured_message())
            email, password = credentials
            create_user(
                conn, "Administrador", email, "", password, "admin",
                must_change_password=True,
            )
            conn.commit()
    return True


def init_db(database_url: str | None = None) -> None:
    _ensure_initialized(database_url or _get_database_url_from_secrets())


# Tamanho de página de todas as listagens paginadas da UI (livros, usuários,
# reconciliação) — o mesmo número que _paginate usa para calcular o offset.
PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# Usuários
# ---------------------------------------------------------------------------

def create_user(conn, full_name, email, phone, password, role, must_change_password=False):
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        text(
            """INSERT INTO users
               (full_name, email, phone, password_hash, salt, role, must_change_password, created_at)
               VALUES (:full_name, :email, :phone, :password_hash, :salt, :role,
                       :must_change_password, :created_at)"""
        ),
        {
            "full_name": full_name,
            "email": email.lower().strip(),
            "phone": phone,
            "password_hash": hash_password(password),
            "salt": "",
            "role": role,
            "must_change_password": 1 if must_change_password else 0,
            "created_at": now,
        },
    )


def try_create_account(
    conn, full_name, email, phone, password, role, must_change_password=False
) -> tuple[bool, str | None]:
    """Cria uma conta. Retorna (sucesso, mensagem_de_erro).

    A checagem de e-mail duplicado feita antes de chamar isto é só uma
    otimização de UX (evita o INSERT na maioria dos casos): a garantia real
    vem da constraint UNIQUE do banco, tratada aqui via IntegrityError para
    não vazar stack trace ao visitante se dois cadastros com o mesmo e-mail
    chegarem em paralelo (corrida entre a checagem e o INSERT).
    """
    try:
        create_user(
            conn, full_name, email, phone, password, role,
            must_change_password=must_change_password,
        )
        conn.commit()
        return True, None
    except IntegrityError:
        conn.rollback()
        return False, "Já existe um cadastro com esse e-mail."


def try_create_reader(conn, full_name, email, phone, password) -> tuple[bool, str | None]:
    """Auto-cadastro pela tela de login — sempre nasce como leitor."""
    return try_create_account(conn, full_name, email, phone, password, "leitor")


# ---------------------------------------------------------------------------
# Cadastro simplificado (leitor de balcão)
# ---------------------------------------------------------------------------
# Numa biblioteca comunitária boa parte do público não cria conta sozinho: a
# pessoa chega no balcão, leva o livro e vai embora. Sem um cadastro para ela,
# o empréstimo só existiria no papel — ou viraria "livro Emprestado sem dono",
# que é exatamente o passivo que a tela de Reconciliação existe para limpar.
#
# O cadastro simplificado é uma conta de leitor SEM acesso ao sistema: guarda
# nome (e, se a pessoa quiser, e-mail/telefone) só para dizer quem está com o
# livro. Não é um perfil novo no RBAC — continua role='leitor', então todas as
# regras que olham o papel seguem valendo sem precisar conhecer este conceito.

# Hash impossível de casar: não tem prefixo bcrypt (cai no ramo legado de
# verify_password) e não é um hex de sha256, então compare_digest é sempre
# falso, qualquer que seja a senha tentada. authenticate ainda recusa a conta
# explicitamente antes disso — este valor é a segunda barreira, para o caso de
# alguém gravar uma senha aqui por outro caminho no futuro.
UNUSABLE_PASSWORD_HASH = "!cadastro-simplificado-sem-senha"

# Domínio reservado dos e-mails internos. `.invalid` é reservado pela RFC 2606
# justamente para isto: nunca vai existir de verdade, então um placeholder
# jamais colide com o e-mail real de alguém nem recebe mensagem por engano.
PLACEHOLDER_EMAIL_DOMAIN = "cadastro-simplificado.invalid"
PLACEHOLDER_EMAIL_ATTEMPTS = 5

SIMPLIFIED_NO_LOGIN_MESSAGE = (
    "Cadastro simplificado não tem acesso ao sistema. Para dar acesso, "
    "converta a conta em completa na Gestão de Usuários."
)


def generate_placeholder_email() -> str:
    """E-mail interno para o cadastro sem e-mail informado.

    A coluna users.email é NOT NULL UNIQUE (é ela que identifica a conta no
    login), então "sem e-mail" precisa de um valor único mesmo assim. O token
    aleatório evita a colisão na origem; quem grava ainda trata a colisão
    improvável — ver create_simplified_reader.
    """
    return f"balcao-{secrets.token_hex(8)}@{PLACEHOLDER_EMAIL_DOMAIN}"


def is_placeholder_email(email: str | None) -> bool:
    """True para e-mail interno gerado por nós — nunca deve ser exibido,
    exportado nem usado para falar com a pessoa."""
    return bool(email) and email.lower().endswith(f"@{PLACEHOLDER_EMAIL_DOMAIN}")


def display_email(email: str | None) -> str:
    """E-mail como a tela deve mostrar: vazio quando é placeholder."""
    return "" if (not email or is_placeholder_email(email)) else email


def borrower_label(row) -> str:
    """Rótulo de leitor nos seletores: nome e e-mail, ou 'sem e-mail' quando
    a conta é simplificada e não informou um."""
    email = display_email(row["email"])
    return f"{row['full_name']} ({email})" if email else f"{row['full_name']} (sem e-mail)"


def _insert_simplified_reader(conn, full_name, email, phone) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        text(
            """INSERT INTO users
               (full_name, email, phone, password_hash, salt, role,
                must_change_password, session_version, is_simplified, created_at)
               VALUES (:full_name, :email, :phone, :password_hash, '', 'leitor',
                       0, 0, 1, :created_at)"""
        ),
        {
            "full_name": full_name,
            "email": email,
            "phone": (phone or "").strip(),
            "password_hash": UNUSABLE_PASSWORD_HASH,
            "created_at": now,
        },
    )
    # O id vem de um SELECT pelo e-mail (UNIQUE) em vez de RETURNING/lastrowid:
    # é o mesmo SQL no Postgres e no SQLite, e a linha acabou de ser gravada
    # nesta transação, então a leitura enxerga o próprio INSERT.
    return conn.execute(
        text("SELECT id FROM users WHERE email = :email"), {"email": email}
    ).scalar_one()


def create_simplified_reader(conn, full_name, email=None, phone=None) -> int:
    """Cria o leitor de balcão e devolve o id. NÃO faz commit: quem chama
    grava o empréstimo na mesma transação, para não sobrar cadastro órfão se
    o empréstimo falhar.

    E-mail é opcional. Informado, é validado e precisa ser inédito (a pessoa
    pode converter a conta depois e logar com ele). Em branco, recebe um
    placeholder interno único.

    Levanta ValueError com a mensagem que a tela mostra.
    """
    name = (full_name or "").strip()
    if not name:
        raise ValueError("Nome completo é obrigatório.")

    address = (email or "").strip().lower()
    if address:
        if not is_valid_email(address):
            raise ValueError(
                "E-mail inválido. Informe um endereço no formato nome@dominio.com "
                "ou deixe em branco."
            )
        if is_placeholder_email(address):
            raise ValueError("Este domínio de e-mail é reservado pelo sistema.")
    else:
        address = _free_placeholder_email(conn)

    if get_user_by_email(conn, address) is not None:
        raise ValueError("Já existe um cadastro com esse e-mail.")

    try:
        return _insert_simplified_reader(conn, name, address, phone)
    except IntegrityError as exc:
        # A checagem acima é UX (mensagem clara sem custar um erro de banco);
        # a garantia é o UNIQUE, que fecha a corrida entre checar e inserir —
        # mesma divisão de papéis de try_create_account. Vira ValueError para
        # a tela tratar como as demais recusas (e dar rollback, obrigatório no
        # Postgres depois de um IntegrityError).
        raise ValueError(
            "Já existe um cadastro com esse e-mail. Atualize a página e tente de novo."
        ) from exc


def _free_placeholder_email(conn) -> str:
    """Sorteia um e-mail interno que ainda não está em uso.

    O token aleatório já torna a colisão improvável; a conferência existe
    porque o custo dela é um SELECT e o custo de errar seria um IntegrityError
    no meio da transação que grava o empréstimo junto."""
    for _ in range(PLACEHOLDER_EMAIL_ATTEMPTS):
        candidate = generate_placeholder_email()
        if get_user_by_email(conn, candidate) is None:
            return candidate
    raise ValueError(
        "Não foi possível gerar um identificador interno para este cadastro. "
        "Tente novamente ou informe um e-mail."
    )


def convert_simplified_to_full(conn, user_id, email, password, phone=None) -> None:
    """Dá acesso ao sistema a um cadastro simplificado: grava e-mail e senha
    e desmarca is_simplified. NÃO faz commit.

    O id da conta não muda, então todo o histórico de empréstimos continua
    ligado a ela — é conversão, não recadastro. A senha é provisória: a pessoa
    é obrigada a trocá-la no primeiro login, como no cadastro de admin.

    A revalidação acontece com a linha travada (FOR UPDATE), para que duas
    conversões simultâneas do mesmo cadastro não gravem senhas diferentes.
    """
    target = conn.execute(
        select(users_table).where(users_table.c.id == user_id).with_for_update()
    ).mappings().first()
    if target is None:
        raise ValueError("Usuário não encontrado.")
    if not target["is_simplified"]:
        raise ValueError("Esta conta já é completa — não há o que converter.")

    address = (email or "").strip().lower()
    if not is_valid_email(address):
        raise ValueError("E-mail inválido. Informe um endereço no formato nome@dominio.com.")
    if is_placeholder_email(address):
        raise ValueError("Este domínio de e-mail é reservado pelo sistema.")
    strength_error = password_strength_error(password)
    if strength_error:
        raise ValueError(strength_error)

    existing = get_user_by_email(conn, address)
    if existing is not None and existing["id"] != user_id:
        raise ValueError("Já existe um cadastro com esse e-mail.")

    conn.execute(
        text(
            """UPDATE users
               SET email = :email, phone = :phone, password_hash = :password_hash,
                   salt = '', must_change_password = 1, is_simplified = 0
               WHERE id = :id AND is_simplified = 1"""
        ),
        {
            "email": address,
            "phone": (phone if phone is not None else target["phone"]) or "",
            "password_hash": hash_password(password),
            "id": user_id,
        },
    )


def get_user_by_email(conn, email):
    return conn.execute(
        text("SELECT * FROM users WHERE email = :email"),
        {"email": email.lower().strip()},
    ).mappings().first()


# Hash descartável, contra o qual a senha é conferida quando o e-mail não
# existe. Não é credencial de ninguém: serve só para o login gastar o mesmo
# tempo nos dois casos. Gerado sob demanda para não pagar um bcrypt no import.
_dummy_password_hash: str | None = None


def _timing_equalizer_hash() -> str:
    global _dummy_password_hash
    if _dummy_password_hash is None:
        _dummy_password_hash = hash_password(secrets.token_urlsafe(16))
    return _dummy_password_hash


def authenticate(conn, email, password):
    user = get_user_by_email(conn, email)
    if not user:
        # Confere contra um hash descartável antes de desistir: sem isso o
        # e-mail inexistente responde na hora e o existente demora o bcrypt,
        # e a diferença de tempo diz qual é qual. A tabela login_attempts já
        # foi feita para não distinguir os dois casos na mensagem — o relógio
        # não pode entregar o que a mensagem esconde.
        verify_password(password, _timing_equalizer_hash(), "")
        return None
    if user["is_simplified"]:
        # Cadastro de balcão não autentica, com senha nenhuma. A checagem vem
        # antes da senha (e mesmo assim gasta o tempo de um bcrypt) para que a
        # recusa não se distinga, nem no relógio nem na mensagem, de uma senha
        # errada — quem tenta não descobre por aqui que a conta existe.
        verify_password(password, _timing_equalizer_hash(), "")
        return None
    if not verify_password(password, user["password_hash"], user["salt"]):
        return None
    if not _is_bcrypt_hash(user["password_hash"]):
        # Login bem-sucedido com hash no formato legado: re-hasheia para
        # bcrypt agora que temos a senha em texto puro em mãos. Migração
        # transparente — o usuário não percebe, e o hash antigo nunca mais
        # fica gravado no banco depois deste ponto.
        new_hash = hash_password(password)
        conn.execute(
            text("UPDATE users SET password_hash = :h, salt = '' WHERE id = :id"),
            {"h": new_hash, "id": user["id"]},
        )
        conn.commit()
        user = dict(user)
        user["password_hash"] = new_hash
        user["salt"] = ""
    return user


def _session_user_view(user) -> dict:
    """Só os campos necessários em st.session_state — nunca password_hash
    nem salt, que não precisam viver na memória do processo por toda a
    sessão."""
    return {
        "id": user["id"],
        "full_name": user["full_name"],
        "email": user["email"],
        "role": user["role"],
        "must_change_password": bool(user["must_change_password"]),
        # Versão da sessão vista no login; reconferida contra o banco a cada
        # rerun para derrubar a sessão se um admin redefinir esta senha.
        "session_version": user["session_version"],
    }


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match((email or "").strip()))


MIN_PASSWORD_LENGTH = 8


def password_strength_error(password: str) -> str | None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"A senha precisa ter pelo menos {MIN_PASSWORD_LENGTH} caracteres."
    return None


def change_password(conn, user_id, current_password, new_password) -> bool:
    """Troca a senha se a atual conferir; commita e retorna True nesse caso.
    Se a senha atual não confere, não altera nada e retorna False."""
    row = conn.execute(
        text("SELECT password_hash, salt FROM users WHERE id = :id"),
        {"id": user_id},
    ).mappings().first()
    if not row or not verify_password(current_password, row["password_hash"], row["salt"]):
        return False
    conn.execute(
        text(
            "UPDATE users SET password_hash = :h, salt = '', must_change_password = 0 "
            "WHERE id = :id"
        ),
        {"h": hash_password(new_password), "id": user_id},
    )
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Reset de senha pelo administrador (presencial, sem envio de e-mail)
# ---------------------------------------------------------------------------
# Não existe fluxo de "esqueci minha senha" por e-mail — ver README. A
# recuperação é presencial: um admin gera uma senha temporária, entrega ao
# usuário e a conta fica obrigada a trocá-la no próximo login.

# Alfabeto sem caracteres ambíguos (0/O, 1/l/I) — a senha é lida na tela e
# ditada/anotada à mão, então confundir um caractere custa um novo reset.
TEMP_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
TEMP_PASSWORD_LENGTH = 12

AUDIT_ACTION_PASSWORD_RESET = "password_reset"


def generate_temporary_password(length: int = TEMP_PASSWORD_LENGTH) -> str:
    """Senha temporária aleatória, sempre acima do mínimo de força exigido.

    Usa secrets.choice (CSPRNG), não random: é credencial de acesso, ainda
    que de vida curta.
    """
    length = max(length, MIN_PASSWORD_LENGTH)
    return "".join(secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(length))


def log_admin_action(conn, action, actor, target) -> None:
    """Registra uma ação administrativa sobre uma conta. Não commita — quem
    chama decide a transação, para a ação e o log caírem juntos."""
    conn.execute(
        text(
            """INSERT INTO admin_audit_log
               (action, actor_user_id, actor_email, target_user_id, target_email, created_at)
               VALUES (:action, :actor_user_id, :actor_email, :target_user_id,
                       :target_email, :created_at)"""
        ),
        {
            "action": action,
            "actor_user_id": actor["id"],
            "actor_email": actor["email"],
            "target_user_id": target["id"],
            "target_email": target["email"],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


def admin_reset_password(conn, actor, target_user_id, new_password=None) -> str:
    """Redefine a senha de uma conta e devolve a senha temporária em texto
    puro — a única vez em que ela existe fora do hash, para ser exibida ao
    admin e repassada presencialmente.

    Em uma única transação:
      * grava o novo hash bcrypt (salt legado zerado, como em change_password);
      * marca must_change_password, forçando a troca no próximo login;
      * incrementa session_version, derrubando qualquer sessão que o usuário
        tenha aberta com a senha antiga (ver _session_is_current);
      * zera o rate limit do e-mail, senão um usuário que travou a conta
        errando a senha continuaria bloqueado mesmo com a senha nova em mãos;
      * registra na auditoria quem redefiniu a senha de quem e quando.

    Levanta ValueError se o usuário não existir, se a conta for um cadastro
    simplificado (que não tem login para recuperar), ou se a senha informada
    pelo admin não passar na regra de força.
    """
    target = conn.execute(
        text("SELECT id, email, is_simplified FROM users WHERE id = :id"),
        {"id": target_user_id},
    ).mappings().first()
    if not target:
        raise ValueError("Usuário não encontrado.")
    if target["is_simplified"]:
        # Gravar senha aqui criaria uma conta que o login recusa de qualquer
        # jeito (authenticate barra is_simplified): o admin sairia com uma
        # senha temporária na mão que não abre nada. O caminho é converter.
        raise ValueError(SIMPLIFIED_NO_LOGIN_MESSAGE)

    if new_password:
        strength_error = password_strength_error(new_password)
        if strength_error:
            raise ValueError(strength_error)
        password = new_password
    else:
        password = generate_temporary_password()

    conn.execute(
        text(
            """UPDATE users
               SET password_hash = :h, salt = '', must_change_password = 1,
                   session_version = session_version + 1
               WHERE id = :id"""
        ),
        {"h": hash_password(password), "id": target_user_id},
    )
    _clear_login_attempts(conn, target["email"])
    log_admin_action(conn, AUDIT_ACTION_PASSWORD_RESET, actor, target)
    conn.commit()
    return password


def _session_is_current(conn, session_user) -> bool:
    """A sessão ainda vale? Falso se a conta sumiu ou se a senha foi
    redefinida por um admin depois deste login (session_version avançou).

    Uma consulta por chave primária a cada rerun — o mesmo custo de qualquer
    clique na tela, e o preço de a senha antiga parar de valer de verdade em
    todas as abas, não só na que fez o reset.
    """
    row = conn.execute(
        text("SELECT role, session_version FROM users WHERE id = :id"),
        {"id": session_user["id"]},
    ).mappings().first()
    if not row:
        return False
    if row["session_version"] != session_user.get("session_version", 0):
        return False
    # papel rebaixado em outra aba não pode continuar valendo como admin
    return row["role"] == session_user["role"]


def count_admins(conn) -> int:
    return conn.execute(
        select(func.count()).select_from(users_table).where(users_table.c.role == "admin")
    ).scalar_one()


def _users_where_clauses(query: str = "", role: str | None = None):
    """Busca por nome, e-mail ou telefone — a mesma busca sem acento do
    acervo (ver _sql_unaccent), para "Jose" achar "José"."""
    clauses = []

    term = normalize_search_term(query)
    if term:
        pattern = f"%{term}%"
        searchable = (
            users_table.c.full_name,
            users_table.c.email,
            func.coalesce(users_table.c.phone, ""),
        )
        clauses.append(or_(*[_sql_unaccent(col).ilike(pattern) for col in searchable]))

    if role:
        clauses.append(users_table.c.role == role)

    return clauses


def count_users(conn, query: str = "", role: str | None = None) -> int:
    stmt = select(func.count()).select_from(users_table)
    for clause in _users_where_clauses(query, role):
        stmt = stmt.where(clause)
    return conn.execute(stmt).scalar_one()


def list_users(
    conn, query: str = "", role: str | None = None,
    limit: int = PAGE_SIZE, offset: int = 0,
):
    """Uma página de usuários — admins primeiro, depois por nome. Nunca
    seleciona password_hash/salt: a tela não tem o que fazer com eles."""
    stmt = select(
        users_table.c.id,
        users_table.c.full_name,
        users_table.c.email,
        users_table.c.phone,
        users_table.c.role,
        users_table.c.must_change_password,
        users_table.c.is_simplified,
        users_table.c.created_at,
    )
    for clause in _users_where_clauses(query, role):
        stmt = stmt.where(clause)
    stmt = (
        stmt.order_by(
            case((users_table.c.role == "admin", 0), else_=1),
            func.lower(users_table.c.full_name),
        )
        .limit(limit)
        .offset(offset)
    )
    return conn.execute(stmt).mappings().all()


def list_admin_audit(conn, limit: int = 20):
    return conn.execute(
        text(
            """SELECT action, actor_email, target_email, created_at
               FROM admin_audit_log ORDER BY id DESC LIMIT :limit"""
        ),
        {"limit": limit},
    ).mappings().all()


# ---------------------------------------------------------------------------
# Rate limiting de login (força bruta)
# ---------------------------------------------------------------------------
# Persistido no banco, não em memória do processo: o Streamlit Cloud
# hiberna e reinicia o container, o que zeraria um contador em memória e
# derrubaria a proteção justamente quando o app volta ao ar.

MAX_LOGIN_ATTEMPTS = 5
# A partir da MAX_LOGIN_ATTEMPTS-ésima falha, cada falha seguinte aumenta o
# tempo de bloqueio (1, 5, 15, 30, 60 minutos; permanece em 60 depois disso).
LOGIN_LOCKOUT_MINUTES_SCHEDULE = [1, 5, 15, 30, 60]


def _lockout_minutes_for(failed_count: int) -> int:
    step = max(failed_count - MAX_LOGIN_ATTEMPTS, 0)
    index = min(step, len(LOGIN_LOCKOUT_MINUTES_SCHEDULE) - 1)
    return LOGIN_LOCKOUT_MINUTES_SCHEDULE[index]


def _login_locked_until(conn, email, now=None):
    """Retorna o datetime até quando o e-mail está bloqueado, ou None se
    não está bloqueado (nunca tentou, nunca excedeu o limite, ou o bloqueio
    anterior já expirou)."""
    now = now or datetime.now()
    row = conn.execute(
        text("SELECT locked_until FROM login_attempts WHERE email = :email"),
        {"email": (email or "").lower().strip()},
    ).mappings().first()
    if not row or not row["locked_until"]:
        return None
    locked_until = datetime.fromisoformat(row["locked_until"])
    return locked_until if now < locked_until else None


def _register_failed_login(conn, email, now=None) -> None:
    now = now or datetime.now()
    email_norm = (email or "").lower().strip()
    row = conn.execute(
        text("SELECT failed_count FROM login_attempts WHERE email = :email"),
        {"email": email_norm},
    ).mappings().first()
    failed_count = (row["failed_count"] if row else 0) + 1
    locked_until = None
    if failed_count >= MAX_LOGIN_ATTEMPTS:
        minutes = _lockout_minutes_for(failed_count)
        locked_until = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    if row:
        conn.execute(
            text(
                "UPDATE login_attempts SET failed_count = :fc, locked_until = :lu "
                "WHERE email = :email"
            ),
            {"fc": failed_count, "lu": locked_until, "email": email_norm},
        )
    else:
        conn.execute(
            text(
                "INSERT INTO login_attempts (email, failed_count, locked_until) "
                "VALUES (:email, :fc, :lu)"
            ),
            {"email": email_norm, "fc": failed_count, "lu": locked_until},
        )
    conn.commit()


def _clear_login_attempts(conn, email) -> None:
    """Zera o contador de tentativas do e-mail. Não commita — quem chama
    decide a transação, para o reset de senha e a liberação do bloqueio
    caírem juntos (ver admin_reset_password)."""
    conn.execute(
        text("DELETE FROM login_attempts WHERE email = :email"),
        {"email": (email or "").lower().strip()},
    )


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


# Código no padrão "PREFIXO-NNN" (ASSM-001, NETJ-1000). Exatamente 4 letras
# maiúsculas, porque é o que book_code_prefix consegue emitir — um código com
# outro formato não pertence a nenhuma sequência que este módulo gera.
# Ancorado nas duas pontas de propósito: os códigos legados do CCE que NÃO
# seguem o padrão ficam de fora do cálculo do máximo. Ficam de fora, todos
# corretamente:
#   BURE, CUNM      -> sem sequencial
#   Bord-001        -> prefixo fora da caixa alta; não é o BORD de um autor
#   GOMLI-001       -> 5 letras; não é o GOML de "Lima Gomes"
#   MILJ-001 (a)    -> sufixo depois do número
BOOK_CODE_RE = re.compile(r"([A-Z]{4})-(\d+)")


def max_sequence_by_prefix(conn) -> dict[str, int]:
    """Maior sequencial já emitido para CADA prefixo de código, em uma query.

    Uma leitura da coluna `code` inteira devolvendo o mapa completo, em vez de
    uma consulta por prefixo: o lote da importação tem centenas de prefixos
    distintos, e uma consulta por linha do arquivo era justamente o N+1 que
    esta função elimina de passagem.
    """
    maxima: dict[str, int] = {}
    for code in conn.execute(select(books_table.c.code)).scalars():
        match = BOOK_CODE_RE.fullmatch((code or "").strip())
        if not match:
            continue
        prefix, sequence = match.group(1), int(match.group(2))
        if sequence > maxima.get(prefix, 0):
            maxima[prefix] = sequence
    return maxima


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
    CSV é o arquivo inteiro — assim o sequencial (por prefixo ou numérico)
    considera tanto o que já está no banco quanto as linhas anteriores do
    próprio lote.

    As duas estratégias seguem a MESMA regra: o próximo código parte do MAIOR
    sequencial já emitido naquela sequência, nunca de uma contagem de livros.
    Contar quebra de duas formas que o acervo real do CCE exibe hoje:

      * o mesmo autor aparece com grafias diferentes ("G. K. Chesterton" e
        "Gilbert Keith Chesterton"; Dostoiévski aparece com 5 grafias). São
        strings distintas com o MESMO prefixo, então contar por autor divide
        a contagem e as duas grafias geram o mesmo sequencial;
      * a numeração legada tem buracos (Agatha Christie tem 43 livros e
        sequencial até 44), então a contagem fica abaixo do máximo.

    Nos dois casos o código gerado colidiria com um já existente. Medido
    contra o acervo real (1.285 livros, 744 autores): 60 autores gerariam
    hoje um código duplicado pela regra de contagem, e nenhum pela de máximo.
    """

    # Espaços de numeração independentes. 'prefixo' é a regra por autor (as 4
    # letras); 'numerico' é o sequencial puro do acervo Espiritual.
    SEQ_PREFIX = "prefixo"
    SEQ_NUMERIC = "numerico"

    def __init__(self, conn):
        self._conn = conn
        # Máximos já gravados no banco, lidos sob demanda e uma vez só.
        self._prefix_db_max: dict[str, int] | None = None
        self._numeric_db_max: dict[str, int] = {}
        # Máximos alocados — ou ocupados por código explícito — neste lote.
        self._batch_max: dict[tuple[str, str], int] = {}

    def _prefix_base(self, prefix: str) -> int:
        if self._prefix_db_max is None:
            self._prefix_db_max = max_sequence_by_prefix(self._conn)
        return self._prefix_db_max.get(prefix, 0)

    def _numeric_base(self, category: str) -> int:
        key = _normalize_key(category)
        if key not in self._numeric_db_max:
            self._numeric_db_max[key] = max_numeric_code_for_category(self._conn, category)
        return self._numeric_db_max[key]

    def _next(self, space: str, key: str, db_max: int) -> int:
        """Próximo número da sequência: um acima do maior entre o que já está
        no banco e o que este lote já alocou."""
        sequence = max(db_max, self._batch_max.get((space, key), 0)) + 1
        self._batch_max[(space, key)] = sequence
        return sequence

    def _occupy(self, space: str, key: str, sequence: int) -> None:
        """Marca um número como já usado, para que a próxima geração da mesma
        sequência não o reemita."""
        if sequence > self._batch_max.get((space, key), 0):
            self._batch_max[(space, key)] = sequence

    def _occupy_code_in(self, code_in: str, category: str) -> None:
        """Um código que veio preenchido na linha não pode ser reemitido
        adiante no mesmo lote — nem o numérico puro, nem o PREFIXO-NNN.

        O prefixo é lido do PRÓPRIO código, que pode não corresponder ao autor
        da linha. E código fora de padrão não ocupa nada: um 'BURE' num livro
        de Machado de Assis não consome um número da sequência ASSM.
        """
        if code_in.isdigit():
            self._occupy(self.SEQ_NUMERIC, _normalize_key(category), int(code_in))
            return
        match = BOOK_CODE_RE.fullmatch(code_in)
        if match:
            self._occupy(self.SEQ_PREFIX, match.group(1), int(match.group(2)))

    def resolve_code(self, author: str, category: str, code_in: str = "") -> str:
        """Código final de uma linha: mantém o que veio preenchido (inclusive
        os legados fora de padrão) ou gera conforme a estratégia da categoria.
        Contabiliza a linha para as próximas do mesmo lote."""
        code_in = (code_in or "").strip()
        author = (author or "").strip()

        if code_in:
            self._occupy_code_in(code_in, category)
            return code_in

        if get_code_strategy(category) == CODE_STRATEGY_NUMERIC:
            key = _normalize_key(category)
            return str(self._next(self.SEQ_NUMERIC, key, self._numeric_base(category)))

        if author:
            prefix = book_code_prefix(author)
            sequence = self._next(self.SEQ_PREFIX, prefix, self._prefix_base(prefix))
            return f"{prefix}-{str(sequence).zfill(3)}"

        return ""


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


def update_book(conn, book_id, title, author, category, status) -> None:
    """Atualiza um livro, preservando o invariante livro↔empréstimo.

    Um livro com empréstimo ATIVO não pode passar a 'Disponível' nem a
    'Em Manutenção': ele está fisicamente com alguém, e liberá-lo no catálogo
    permitiria um segundo empréstimo do mesmo exemplar — dois leitores
    registrados com o mesmo livro, sem nenhuma tela sinalizando.

    A checagem é feita DENTRO da transação, com a linha do livro travada, e não
    com o que a tela carregou: entre abrir o formulário e clicar em salvar, o
    livro pode ter sido emprestado por outra sessão.
    """
    book = conn.execute(
        select(books_table).where(books_table.c.id == book_id).with_for_update()
    ).mappings().first()
    if book is None:
        raise ValueError("Livro não encontrado — ele pode ter sido removido.")

    if status != "Emprestado" and get_active_loan_for_book(conn, book_id) is not None:
        raise ValueError(
            f"Este livro tem um empréstimo ativo e não pode ficar como '{status}'. "
            "Registre a devolução em Empréstimos antes de mudar o status."
        )

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
            "id": book_id,
        },
    )


def delete_book(conn, book_id) -> None:
    """Remove um livro e todo o seu histórico de empréstimos (já devolvidos),
    de forma atômica: as duas exclusões acontecem na mesma transação e só
    persistem quando o chamador der commit.

    Levanta ValueError se houver empréstimo ATIVO para o livro — controle do
    exemplar físico em posse de alguém, que precisa ser devolvido antes.

    Trava a linha do livro antes de checar, para que um empréstimo criado entre
    a checagem e o DELETE não seja apagado junto. E apaga apenas os empréstimos
    já DEVOLVIDOS: se um ativo aparecer mesmo assim, a FK de loans.book_id
    impede a remoção do livro em vez de o histórico sumir em silêncio.
    """
    book = conn.execute(
        select(books_table).where(books_table.c.id == book_id).with_for_update()
    ).mappings().first()
    if book is None:
        raise ValueError("Livro não encontrado — ele pode ter sido removido.")
    if get_active_loan_for_book(conn, book_id) is not None:
        raise ValueError(
            "Este livro está emprestado no momento. Registre a devolução antes de removê-lo."
        )
    conn.execute(
        text("DELETE FROM loans WHERE book_id = :book_id AND status = 'devolvido'"),
        {"book_id": book_id},
    )
    conn.execute(text("DELETE FROM books WHERE id = :id"), {"id": book_id})


# ---------------------------------------------------------------------------
# Busca, filtros e paginação de livros (tudo resolvido no banco)
# ---------------------------------------------------------------------------

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
    limit: int = PAGE_SIZE,
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


# Codificações tentadas, na ordem. UTF-8 primeiro porque é o formato correto e
# o único que pode ser detectado com segurança (byte inválido = não é UTF-8).
# cp1252 depois: é o que o Excel em português no Windows gera ao salvar "CSV
# (separado por vírgulas)", e o acervo do CCE é cheio de acento. latin-1 por
# último, como rede: cobre os poucos bytes que cp1252 não define.
CSV_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")

CSV_ENCODING_ERROR = (
    "Não foi possível ler o arquivo: ele não parece ser um CSV de texto. Se o "
    "arquivo veio do Excel, abra-o e use **Arquivo → Salvar como → CSV UTF-8 "
    "(delimitado por vírgulas)** — o formato \"Unicode\" e as planilhas .xlsx "
    "não são lidos aqui."
)


def _decode_csv_bytes(data: bytes) -> str:
    """Texto do CSV, tentando as codificações que o cliente realmente usa.

    latin-1 decodifica QUALQUER byte sem erro, então a cascata nunca chega ao
    fim por UnicodeDecodeError — o que é bom para arquivos acentuados e ruim
    para arquivos que não são texto: um .xlsx ou um CSV em UTF-16 viraria
    caractere ilegível em vez de erro. Por isso a validação real não é o
    decode, e sim o NUL: texto de verdade não tem \\x00, e UTF-16 e binário
    têm aos montes.
    """
    for encoding in CSV_ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            break
        return text
    raise ValueError(CSV_ENCODING_ERROR)


def parse_csv_bytes(data: bytes) -> list[dict]:
    """Decodifica bytes de um CSV (UTF-8 com ou sem BOM, cp1252 ou latin-1;
    CRLF ou LF) e detecta o delimitador (',' ou ';') automaticamente. Retorna
    uma lista de dicts com as chaves das colunas normalizadas (minúsculas,
    sem espaços)."""
    text = _decode_csv_bytes(data)

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
    """Grava as linhas processadas de uma importação, em uma transação só.

    Recusa o lote inteiro se alguma linha estiver marcada com erro, em vez de
    confiar que o botão da tela continua desabilitado: a validação de
    process_import_rows é um retrato do banco no momento da pré-visualização, e
    quem chama esta função pode não ser aquela tela.

    Recusar, e não pular: se uma linha com erro chegou até aqui, o pressuposto
    da tela foi violado — importar as outras deixaria uma carga pela metade,
    silenciosamente incompleta, num arquivo que o operador acredita ter
    importado inteiro.
    """
    blocked = [row["linha"] for row in processed_rows if row.get("erros")]
    if blocked:
        linhas = ", ".join(str(n) for n in blocked[:10])
        reticencias = "…" if len(blocked) > 10 else ""
        raise ValueError(
            f"{len(blocked)} linha(s) ainda têm erro bloqueante e nada foi importado "
            f"(linha(s) {linhas}{reticencias}). Corrija o arquivo ou o mapeamento e "
            "reenvie."
        )

    now = datetime.now().isoformat(timespec="seconds")
    count = 0
    for row in processed_rows:
        try:
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
        except IntegrityError as exc:
            # Alguém cadastrou este código entre a pré-visualização e o clique.
            # Vira ValueError com a linha e o código identificados: sem isso o
            # operador recebe um erro de constraint sem saber onde olhar.
            raise ValueError(
                f"A linha {row['linha']} usa o código '{row['codigo']}', que já existe "
                "no acervo — provavelmente cadastrado por outra pessoa depois que esta "
                "pré-visualização foi gerada. Nada foi importado; reenvie o arquivo "
                "para recalcular os códigos."
            ) from exc
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
    com due_date, respeita o prazo ajustado no momento do registro.

    A disponibilidade é decidida pelo PRÓPRIO UPDATE condicional, não por um
    SELECT anterior: ler o status e depois gravar deixa uma janela em que duas
    sessões leem 'Disponível' e ambas criam empréstimo do mesmo exemplar. O
    UPDATE trava a linha; em READ COMMITTED (padrão do Supabase) a segunda
    sessão espera, reavalia o WHERE e não afeta nenhuma linha.
    """
    now = datetime.now().isoformat(timespec="seconds")
    due = _to_date(due_date) or default_due_date(now)

    claimed = conn.execute(
        text(
            "UPDATE books SET status = 'Emprestado' "
            "WHERE id = :id AND status = 'Disponível'"
        ),
        {"id": book_id},
    )
    if claimed.rowcount != 1:
        # livro inexistente, em manutenção ou já emprestado por outra sessão
        raise ValueError("Livro indisponível para empréstimo.")

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


def return_loan(conn, loan_id):
    """Registra a devolução. Trava a linha do empréstimo antes de decidir, para
    que duas sessões clicando "Registrar devolução" ao mesmo tempo não gravem
    a devolução duas vezes (a segunda espera a trava e vê 'devolvido')."""
    loan = conn.execute(
        select(loans_table).where(loans_table.c.id == loan_id).with_for_update()
    ).mappings().first()
    if loan is None or loan["status"] != "ativo":
        raise ValueError("Empréstimo não está ativo.")
    now = datetime.now().isoformat(timespec="seconds")
    settled = conn.execute(
        text(
            "UPDATE loans SET status = 'devolvido', return_date = :return_date "
            "WHERE id = :id AND status = 'ativo'"
        ),
        {"return_date": now, "id": loan_id},
    )
    if settled.rowcount != 1:
        raise ValueError("Empréstimo não está ativo.")
    conn.execute(
        text("UPDATE books SET status = 'Disponível' WHERE id = :id"), {"id": loan["book_id"]}
    )


# ---------------------------------------------------------------------------
# Telas (UI)
# ---------------------------------------------------------------------------

# A tela pública DIZ que o e-mail já tem cadastro, em vez de responder algo
# neutro. É uma decisão consciente, não um descuido: a mensagem neutra ("se
# este e-mail ainda não estava em uso, a conta foi criada") fecha a enumeração
# de contas, mas deixa sem saída quem só esqueceu que já tinha cadastro — e
# esse é o caso comum numa biblioteca comunitária, enquanto o atacante que
# enumera e-mails é o caso raro. Aqui a orientação vale mais que o sigilo.
#
# O vazamento por TEMPO no login continua fechado (ver _timing_equalizer_hash):
# aquilo não custava usabilidade nenhuma, então não há motivo para abrir mão.
SIGNUP_DUPLICATE_MESSAGE = (
    "Já existe um cadastro com esse e-mail. Use a aba **Entrar** para acessar "
    "sua conta. Se não lembra a senha, peça a um administrador da biblioteca "
    "para redefini-la — a redefinição é presencial."
)


def show_auth_screen(conn):
    st.title("Biblioteca Comunitária")
    tab_login, tab_cadastro = st.tabs(["Entrar", "Cadastrar-se"])

    with tab_login:
        with st.form("login_form"):
            email = st.text_input("E-mail", key="login_email")
            password = st.text_input("Senha", type="password", key="login_password")
            if st.form_submit_button("Entrar"):
                locked_until = _login_locked_until(conn, email)
                if locked_until:
                    st.error(
                        "Muitas tentativas inválidas para este e-mail. Tente "
                        f"novamente após {locked_until.strftime('%H:%M:%S')}."
                    )
                else:
                    user = authenticate(conn, email, password)
                    if user:
                        _clear_login_attempts(conn, email)
                        conn.commit()
                        st.session_state.user = _session_user_view(user)
                        st.rerun()
                    else:
                        _register_failed_login(conn, email)
                        st.error("E-mail ou senha inválidos.")

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
                else:
                    strength_error = password_strength_error(password_c)
                    if strength_error:
                        st.error(strength_error)
                        has_error = True
                if not has_error and get_user_by_email(conn, email_c):
                    st.error(SIGNUP_DUPLICATE_MESSAGE)
                    has_error = True
                if not has_error:
                    success, _ = try_create_reader(
                        conn, full_name, email_c, phone, password_c
                    )
                    if success:
                        st.success("Cadastro realizado! Faça login na aba ao lado.")
                    else:
                        # Corrida entre a checagem acima e o INSERT: a única
                        # falha que try_create_reader reporta é o e-mail
                        # duplicado, então a orientação é a mesma.
                        st.error(SIGNUP_DUPLICATE_MESSAGE)


def show_change_password_screen(conn, user, forced: bool = False):
    st.title("Alterar minha senha")
    if forced:
        st.warning(
            "Por segurança, defina uma nova senha antes de continuar usando o sistema."
        )
    with st.form("change_password_form"):
        current = st.text_input("Senha atual", type="password", key="cp_current")
        new = st.text_input("Nova senha", type="password", key="cp_new")
        confirm = st.text_input("Confirmar nova senha", type="password", key="cp_confirm")
        if st.form_submit_button("Salvar nova senha"):
            has_error = False
            if not current:
                st.error("Informe a senha atual.")
                has_error = True
            strength_error = password_strength_error(new)
            if strength_error:
                st.error(strength_error)
                has_error = True
            if not has_error and new != confirm:
                st.error("A confirmação não corresponde à nova senha.")
                has_error = True
            if not has_error and current == new:
                st.error("A nova senha deve ser diferente da atual.")
                has_error = True
            if not has_error:
                if change_password(conn, user["id"], current, new):
                    st.session_state.user["must_change_password"] = False
                    st.success("Senha alterada com sucesso.")
                    st.rerun()
                else:
                    st.error("Senha atual incorreta.")
    if forced and st.button("Sair"):
        st.session_state.user = None
        st.rerun()


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
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(st.session_state.get(page_key, 1), total_pages)
    st.session_state[page_key] = page

    first = (page - 1) * PAGE_SIZE + 1
    last = min(page * PAGE_SIZE, total)

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

    return (page - 1) * PAGE_SIZE


def _loan_error(conn, exc) -> None:
    """Mensagem única dos dois caminhos de empréstimo do catálogo."""
    conn.rollback()
    st.error(
        f"Não foi possível registrar o empréstimo: {exc} "
        "Atualize a página para ver a situação atual do livro."
    )


def _render_self_loan(conn, user, book) -> None:
    """Leitor logado pegando o livro para si."""
    with st.popover("Pegar emprestado", width="stretch"):
        due = st.date_input(
            "Devolução prevista",
            value=default_due_date(),
            key=f"due_{book['id']}",
            help=f"Prazo padrão: {PRAZO_PADRAO_DIAS} dias. "
            "Ajuste antes de confirmar, se necessário.",
        )
        if st.button("Confirmar empréstimo", key=f"borrow_{book['id']}", width="stretch"):
            try:
                request_loan(conn, book["id"], user["id"], due_date=due)
                conn.commit()
            except (ValueError, IntegrityError) as exc:
                _loan_error(conn, exc)
            else:
                st.success(f'Empréstimo de "{book["title"]}" registrado!')
                st.rerun()


def _borrowers_for_term(conn, term: str, cache: dict):
    """Busca de leitores memoizada dentro de UM render.

    O corpo de um popover é executado a cada rerun, aberto ou não: sem esta
    memória, uma página de 25 livros faria 50 consultas por rerun só para
    montar seletores que ninguém abriu. Como quase todos os popovers estão com
    a busca vazia, na prática sobra uma consulta por termo distinto. O cache
    vive só durante o render, então nunca serve dado velho."""
    if term not in cache:
        cache[term] = (search_borrowers(conn, term), count_borrowers(conn, term))
    return cache[term]


def _render_counter_loan(conn, book, cache: dict) -> None:
    """Empréstimo de balcão: o admin registra o livro em nome de um leitor.

    Mesmo request_loan do leitor — inclusive a trava por UPDATE condicional —
    então dois atendimentos simultâneos do mesmo exemplar continuam sem poder
    gerar dois empréstimos ativos."""
    book_id = book["id"]
    with st.popover("Registrar empréstimo para…", width="stretch"):
        term = st.text_input(
            "Buscar leitor por nome ou e-mail",
            key=f"loan_for_query_{book_id}",
            help="Deixe em branco para ver os primeiros leitores em ordem alfabética.",
        )
        matches, total = _borrowers_for_term(conn, term, cache)
        if not matches:
            st.info(
                "Nenhum leitor encontrado. Cadastre-o em **Reconciliação** "
                "(cadastro de balcão) ou peça que ele se cadastre."
            )
            return

        if total > len(matches):
            st.caption(
                f"Mostrando {len(matches)} de {total} leitores — refine a busca."
            )

        options = {borrower_label(b): b["id"] for b in matches}
        who = st.selectbox(
            "Leitor", list(options.keys()), key=f"loan_for_user_{book_id}"
        )
        due = st.date_input(
            "Devolução prevista",
            value=default_due_date(),
            key=f"loan_for_due_{book_id}",
            help=f"Prazo padrão: {PRAZO_PADRAO_DIAS} dias. "
            "Ajuste antes de confirmar, se necessário.",
        )
        if st.button(
            "Confirmar empréstimo", key=f"admin_borrow_{book_id}", width="stretch"
        ):
            try:
                request_loan(conn, book_id, options[who], due_date=due)
                conn.commit()
            except (ValueError, IntegrityError) as exc:
                _loan_error(conn, exc)
            else:
                st.success(f'Empréstimo de "{book["title"]}" registrado para {who}.')
                st.rerun()


def show_catalog(conn, user):
    st.header("Catálogo de Livros")
    query, category, status = _book_search_controls(conn, "catalog")

    total = count_books(conn, query, category, status)
    if not total:
        st.info("Nenhum livro encontrado.")
        return

    offset = _paginate(total, "catalog")
    rows = list_books(conn, query, category, status, offset=offset)

    borrower_cache: dict = {}
    for r in rows:
        with st.container(border=True):
            info_col, action_col = st.columns([5, 2])
            with info_col:
                st.markdown(f"**{r['title']}**")
                st.caption(f"{r['author']} · {r['code']}")
                st.write(f"{STATUS_EMOJI.get(r['status'], '')} {r['status']}")
            with action_col:
                if r["status"] == "Disponível":
                    if user["role"] == "leitor":
                        _render_self_loan(conn, user, r)
                    elif user["role"] == "admin":
                        _render_counter_loan(conn, r, borrower_cache)


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
                    try:
                        code = add_book(conn, title, author, category)
                        conn.commit()
                    except (ValueError, IntegrityError) as exc:
                        conn.rollback()
                        st.error(
                            "Não foi possível cadastrar o livro. Se o código gerado já "
                            "existir no acervo, cadastre informando um código manualmente "
                            f"pela importação de CSV. Detalhe: {exc}"
                        )
                    else:
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
                    try:
                        update_book(conn, r["id"], title, author, category, status)
                        conn.commit()
                    except (ValueError, IntegrityError) as exc:
                        conn.rollback()
                        st.error(f"Não foi possível salvar as alterações: {exc}")
                    else:
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

def _active_loan_exists():
    """Subconsulta: este livro tem algum empréstimo ativo?"""
    return (
        select(loans_table.c.id)
        .where(loans_table.c.book_id == books_table.c.id)
        .where(loans_table.c.status == "ativo")
        .exists()
    )


def _unreconciled_where(query: str = ""):
    """Livro com status 'Emprestado' e sem NENHUM empréstimo ativo na base."""
    clauses = [books_table.c.status == "Emprestado", ~_active_loan_exists()]

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
    conn, query: str = "", limit: int = PAGE_SIZE, offset: int = 0
):
    stmt = select(books_table)
    for clause in _unreconciled_where(query):
        stmt = stmt.where(clause)
    stmt = stmt.order_by(books_table.c.title).limit(limit).offset(offset)
    return conn.execute(stmt).mappings().all()


def count_books_loaned_but_available(conn) -> int:
    """Sentido INVERSO da reconciliação: livro que NÃO está 'Emprestado' mas
    tem empréstimo ativo.

    Depois da validação em update_book e das travas em request_loan/return_loan,
    a aplicação não consegue mais produzir esse estado — mas SQL manual, uma
    restauração de backup ou uma carga direta no Supabase conseguem. É uma
    rede de segurança: sem detecção, o exemplar volta ao catálogo e pode ser
    emprestado a um segundo leitor sem que nenhuma tela avise.
    """
    stmt = (
        select(func.count())
        .select_from(books_table)
        .where(books_table.c.status != "Emprestado")
        .where(_active_loan_exists())
    )
    return conn.execute(stmt).scalar_one()


def list_books_loaned_but_available(conn, limit: int = PAGE_SIZE):
    """Os livros do sentido inverso, com quem consta como estando com eles."""
    return conn.execute(
        text(
            """
            SELECT books.code, books.title, books.status,
                   users.full_name, loans.loan_date
            FROM books
            JOIN loans ON loans.book_id = books.id AND loans.status = 'ativo'
            JOIN users ON users.id = loans.user_id
            WHERE books.status <> 'Emprestado'
            ORDER BY books.title
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings().all()


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
            # display_email zera o placeholder do cadastro de balcão: e-mail
            # interno nosso não é dado de contato e não pode sair na planilha
            # como se fosse o endereço da pessoa.
            display_email(r["email"]),
            r["loan_date"] or "",
            r["due_date"] or "",
            r["return_date"] or "",
            r["status"],
        )
        for r in rows
    ]
    return _to_excel_csv_bytes(LOANS_EXPORT_HEADER, data)


# Quantos leitores o seletor de "quem está com o livro" mostra por vez. O
# seletor é alimentado por busca, não pela lista inteira: com cadastro de
# balcão o número de leitores cresce com o movimento da biblioteca, e uma
# combo com centenas de nomes é tão inútil quanto cara.
BORROWER_PICKER_LIMIT = 20


def search_borrowers(conn, query: str = "", limit: int = BORROWER_PICKER_LIMIT):
    """Leitores que casam com a busca (nome, e-mail ou telefone), no máximo
    `limit` — a mesma busca sem acento do resto do app.

    Sem busca, devolve os primeiros em ordem alfabética: serve para o admin
    que abre o seletor e já vê alguém, em vez de uma lista vazia."""
    stmt = select(
        users_table.c.id,
        users_table.c.full_name,
        users_table.c.email,
        users_table.c.is_simplified,
    )
    for clause in _users_where_clauses(query, role="leitor"):
        stmt = stmt.where(clause)
    stmt = stmt.order_by(func.lower(users_table.c.full_name)).limit(limit)
    return conn.execute(stmt).mappings().all()


def count_borrowers(conn, query: str = "") -> int:
    return count_users(conn, query, role="leitor")


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


# ---------------------------------------------------------------------------
# Histórico de empréstimos: filtro e paginação no banco
# ---------------------------------------------------------------------------
# Tudo aqui é resolvido em SQL — inclusive a contagem de atrasados e as listas
# dos seletores de filtro. A tela carrega uma página por vez, e é a que traz
# mais dado pessoal de uma só vez: quanto menos linha vier, melhor.
#
# Todas as consultas usam LEFT JOIN em users, como export_loans_csv: se um dia
# um empréstimo ficar órfão (leitor removido/anonimizado no banco), a linha do
# histórico continua aparecendo como "Leitor removido", sem nome, e-mail nem
# telefone — com INNER JOIN ela sumiria da tela e o total mentiria.

_loan_history_from = (
    loans_table
    .join(books_table, books_table.c.id == loans_table.c.book_id)
    .outerjoin(users_table, users_table.c.id == loans_table.c.user_id)
)


def _loan_history_where_clauses(
    book_id: int | None = None,
    user_id: int | None = None,
    start=None,
    end=None,
):
    """Filtros de livro, leitor e período da data de empréstimo.

    loan_date é TEXT ISO ("YYYY-MM-DD" ou "YYYY-MM-DDTHH:MM:SS"), que compara
    corretamente em ordem lexicográfica. O fim do período é comparado contra o
    dia SEGUINTE com "<", senão um empréstimo das 14h do último dia ficaria de
    fora por causa da hora."""
    clauses = []
    if book_id is not None:
        clauses.append(loans_table.c.book_id == book_id)
    if user_id is not None:
        clauses.append(loans_table.c.user_id == user_id)
    start_day = _to_date(start)
    if start_day is not None:
        clauses.append(loans_table.c.loan_date >= start_day.isoformat())
    end_day = _to_date(end)
    if end_day is not None:
        clauses.append(loans_table.c.loan_date < (end_day + timedelta(days=1)).isoformat())
    return clauses


def count_loan_history(conn, **filters) -> int:
    stmt = select(func.count()).select_from(_loan_history_from)
    for clause in _loan_history_where_clauses(**filters):
        stmt = stmt.where(clause)
    return conn.execute(stmt).scalar_one()


def count_overdue_loan_history(conn, reference_date=None, **filters) -> int:
    """Atrasados dentro do mesmo filtro — contado no banco, porque a tela só
    tem em mãos a página atual. Mesma regra de is_overdue: ativo, com prazo, e
    vencido antes de hoje (vencer hoje ainda não é atraso)."""
    reference = _to_date(reference_date) or date.today()
    stmt = (
        select(func.count())
        .select_from(_loan_history_from)
        .where(loans_table.c.status == "ativo")
        .where(loans_table.c.due_date.is_not(None))
        .where(loans_table.c.due_date < reference.isoformat())
    )
    for clause in _loan_history_where_clauses(**filters):
        stmt = stmt.where(clause)
    return conn.execute(stmt).scalar_one()


def list_loan_history(conn, limit: int = PAGE_SIZE, offset: int = 0, **filters):
    """Uma página do histórico, do empréstimo mais recente para o mais antigo.

    Desempate por loans.id: sem ele, dois empréstimos com a mesma loan_date
    poderiam trocar de posição entre uma página e outra e um deles nunca
    aparecer."""
    stmt = select(
        loans_table.c.id.label("loan_id"),
        loans_table.c.loan_date,
        loans_table.c.due_date,
        loans_table.c.return_date,
        loans_table.c.status,
        books_table.c.id.label("book_id"),
        books_table.c.title.label("book_title"),
        books_table.c.code.label("book_code"),
        loans_table.c.user_id,
        users_table.c.full_name,
        users_table.c.email,
        users_table.c.phone,
    ).select_from(_loan_history_from)
    for clause in _loan_history_where_clauses(**filters):
        stmt = stmt.where(clause)
    stmt = (
        stmt.order_by(loans_table.c.loan_date.desc(), loans_table.c.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return conn.execute(stmt).mappings().all()


def list_loan_history_books(conn):
    """Livros que aparecem no histórico — consulta própria, três colunas, uma
    linha por livro. Alimenta o seletor de filtro sem carregar empréstimo
    nenhum."""
    stmt = (
        select(books_table.c.id, books_table.c.title, books_table.c.code)
        .where(select(loans_table.c.id)
               .where(loans_table.c.book_id == books_table.c.id)
               .exists())
        .order_by(func.lower(books_table.c.title))
    )
    return conn.execute(stmt).mappings().all()


def list_loan_history_borrowers(conn):
    """Leitores que aparecem no histórico, para o seletor de filtro.

    Um empréstimo órfão (leitor removido) não entra: não há por quem filtrar,
    e ele continua visível na listagem como "Leitor removido"."""
    stmt = (
        select(users_table.c.id, users_table.c.full_name, users_table.c.email)
        .where(select(loans_table.c.id)
               .where(loans_table.c.user_id == users_table.c.id)
               .exists())
        .order_by(func.lower(users_table.c.full_name))
    )
    return conn.execute(stmt).mappings().all()


def loan_history_date_bounds(conn) -> tuple[date | None, date | None]:
    """Primeira e última data de empréstimo registradas, para o valor inicial
    do filtro de período. Dois agregados numa consulta só, sem trazer linha."""
    row = conn.execute(
        select(func.min(loans_table.c.loan_date), func.max(loans_table.c.loan_date))
    ).first()
    if row is None or row[0] is None:
        return None, None
    return _to_date(row[0]), _to_date(row[1])


def _history_borrower_name(row) -> str:
    return row["full_name"] if row["full_name"] is not None else ANONYMIZED_BORROWER_LABEL


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
                st.caption(
                    f"{r['full_name']} · {display_email(r['email']) or 'sem e-mail'} · "
                    f"{r['phone'] or '-'}"
                )
                st.write(f"Emprestado em {r['loan_date']}")
                if late:
                    st.error(_due_date_caption(r["due_date"], r["status"]))
                else:
                    st.caption(_due_date_caption(r["due_date"], r["status"]))
            with action_col:
                if st.button(
                    "✅ Registrar devolução", key=f"return_{r['loan_id']}", width="stretch"
                ):
                    try:
                        return_loan(conn, r["loan_id"])
                        conn.commit()
                    except (ValueError, IntegrityError) as exc:
                        conn.rollback()
                        st.error(
                            f"Não foi possível registrar a devolução: {exc} "
                            "Atualize a página para ver a lista atualizada."
                        )
                    else:
                        st.success("Devolução registrada.")
                        st.rerun()


def show_admin_loan_history(conn):
    st.header("Histórico completo de empréstimos")

    first_day, last_day = loan_history_date_bounds(conn)
    if first_day is None:
        st.info("Nenhum empréstimo registrado ainda.")
        return

    book_options = {"Todos": None}
    for b in list_loan_history_books(conn):
        book_options[f"{b['title']} ({b['code']})"] = b["id"]

    user_options = {"Todos": None}
    for b in list_loan_history_borrowers(conn):
        user_options[borrower_label(b)] = b["id"]

    col1, col2, col3 = st.columns(3)
    book_choice = col1.selectbox(
        "Filtrar por livro", list(book_options.keys()), key="history_book"
    )
    user_choice = col2.selectbox(
        "Filtrar por usuário", list(user_options.keys()), key="history_user"
    )
    date_range = col3.date_input(
        "Período (data de empréstimo)",
        value=(first_day, last_day),
        key="history_period",
    )

    start_day, end_day = None, None
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_day, end_day = date_range

    filters = {
        "book_id": book_options[book_choice],
        "user_id": user_options[user_choice],
        "start": start_day,
        "end": end_day,
    }

    # Mudou algum filtro -> volta para a 1ª página, como no Catálogo: senão o
    # admin continuaria na página 7 de um resultado que agora tem duas.
    signature = (book_choice, user_choice, str(start_day), str(end_day))
    if st.session_state.get("history_filter_signature") != signature:
        st.session_state["history_filter_signature"] = signature
        st.session_state["history_page"] = 1

    total = count_loan_history(conn, **filters)
    st.write(f"{total} empréstimo(s) encontrado(s).")
    if not total:
        return

    overdue_total = count_overdue_loan_history(conn, **filters)
    if overdue_total:
        st.warning(f"⚠️ {overdue_total} deles em atraso.")

    offset = _paginate(total, "history")
    rows = list_loan_history(conn, offset=offset, **filters)

    status_emoji = {"ativo": "🔴", "devolvido": "🟢"}

    def _situacao(r):
        late = days_overdue(r["due_date"]) if r["status"] == "ativo" else 0
        if late > 0:
            return f"🔴 atrasado há {late} dia(s)"
        return f"{status_emoji.get(r['status'], '')} {r['status']}"

    table = [
        {
            "Livro": f"{r['book_title']} ({r['book_code']})",
            "Leitor": _history_borrower_name(r),
            "E-mail": display_email(r["email"]) or "-",
            "Telefone": r["phone"] or "-",
            "Emprestado em": r["loan_date"],
            "Prevista": r["due_date"] or "-",
            "Devolvido em": r["return_date"] or "-",
            "Status": _situacao(r),
        }
        for r in rows
    ]
    st.dataframe(table, width="stretch")

    if filters["user_id"] is not None:
        _render_borrower_history(conn, filters["user_id"], user_choice)


def _render_borrower_history(conn, user_id, label) -> None:
    """Últimos empréstimos do leitor filtrado, ignorando livro e período — é a
    pergunta "o que mais está com essa pessoa?", que os outros filtros
    esconderiam. Limitado a uma página: quem precisa de mais usa a listagem
    acima, que é paginada."""
    loans = list_loan_history(conn, user_id=user_id)
    total = count_loan_history(conn, user_id=user_id)
    with st.expander(f"📋 Todos os empréstimos de {label}", expanded=True):
        if total > len(loans):
            st.caption(f"Mostrando os {len(loans)} mais recentes de {total}.")
        for r in loans:
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
                    try:
                        return_loan(conn, r["loan_id"])
                        conn.commit()
                    except (ValueError, IntegrityError) as exc:
                        conn.rollback()
                        st.error(
                            f"Não foi possível registrar a devolução: {exc} "
                            "Atualize a página — ela pode já ter sido registrada no balcão."
                        )
                    else:
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


# ---------------------------------------------------------------------------
# Gestão de usuários (admin): reset de senha presencial e cadastro de admins
# ---------------------------------------------------------------------------

# Onde a senha temporária recém-gerada espera para ser exibida. Vive só no
# session_state do admin que fez o reset, e some no primeiro clique depois
# disso — o banco guarda apenas o hash, então esta é literalmente a única
# chance de ler a senha em texto puro.
RESET_RESULT_KEY = "password_reset_result"

SINGLE_ADMIN_WARNING = (
    "⚠️ **Este é o único administrador do sistema.** Se esta senha for perdida, "
    "não há como recuperar o acesso administrativo pela aplicação — a recuperação "
    "de senha depende de outro admin fazer a redefinição. Cadastre um segundo "
    "administrador agora, no formulário abaixo."
)


def _render_reset_result() -> None:
    """Painel da senha temporária gerada no último reset (se houver)."""
    result = st.session_state.get(RESET_RESULT_KEY)
    if not result:
        return
    st.success(
        f"Senha redefinida para **{result['full_name']}** ({result['email']})."
    )
    st.warning(
        "Anote ou entregue esta senha agora — ela **não será exibida de novo** "
        "e não pode ser consultada depois (o sistema guarda só o hash). O "
        "usuário será obrigado a trocá-la no próximo login."
    )
    st.code(result["password"], language=None)
    if st.button("Já anotei — ocultar senha", key="dismiss_reset_result"):
        del st.session_state[RESET_RESULT_KEY]
        st.rerun()


def _render_new_admin_form(conn) -> None:
    with st.expander("➕ Cadastrar novo administrador"):
        st.caption(
            "Contas de administrador não podem ser criadas pela tela de cadastro "
            "(que só cria leitores). A senha definida aqui é provisória: o novo "
            "admin é obrigado a trocá-la no primeiro login."
        )
        with st.form("new_admin_form"):
            full_name = st.text_input("Nome completo", key="new_admin_name")
            email = st.text_input("E-mail", key="new_admin_email")
            phone = st.text_input("Telefone/WhatsApp", key="new_admin_phone")
            password = st.text_input(
                "Senha provisória", type="password", key="new_admin_password"
            )
            if st.form_submit_button("Cadastrar administrador"):
                has_error = False
                if not full_name.strip():
                    st.error("Nome completo é obrigatório.")
                    has_error = True
                if not is_valid_email(email):
                    st.error("E-mail inválido. Informe um endereço no formato nome@dominio.com.")
                    has_error = True
                strength_error = password_strength_error(password)
                if strength_error:
                    st.error(strength_error)
                    has_error = True
                if not has_error and get_user_by_email(conn, email):
                    st.error("Já existe um cadastro com esse e-mail.")
                    has_error = True
                if not has_error:
                    success, error_message = try_create_account(
                        conn, full_name, email, phone, password, "admin",
                        must_change_password=True,
                    )
                    if success:
                        st.success(
                            f"Administrador **{full_name}** cadastrado. Ele precisará "
                            "trocar a senha no primeiro login."
                        )
                        st.rerun()
                    else:
                        st.error(error_message)


def _render_convert_simplified(conn, row) -> None:
    """Bloco 'Dar acesso ao sistema' de um cadastro de balcão.

    Converter mantém o mesmo id, então o histórico de empréstimos da pessoa
    continua inteiro — é a mesma conta ganhando login, não um cadastro novo."""
    st.markdown("---")
    st.markdown("**Dar acesso ao sistema**")
    st.caption(
        "Transforma este cadastro de balcão em conta completa, preservando todo "
        "o histórico de empréstimos. A senha definida aqui é provisória: a pessoa "
        "é obrigada a trocá-la no primeiro login."
    )
    email = st.text_input("E-mail de acesso", key=f"convert_email_{row['id']}")
    phone = st.text_input(
        "Telefone/WhatsApp",
        value=row["phone"] or "",
        key=f"convert_phone_{row['id']}",
    )
    password = st.text_input(
        "Senha provisória", type="password", key=f"convert_pw_{row['id']}"
    )
    if st.button("🔓 Converter em conta completa", key=f"convert_{row['id']}"):
        try:
            convert_simplified_to_full(conn, row["id"], email, password, phone)
            conn.commit()
        except (ValueError, IntegrityError) as exc:
            conn.rollback()
            st.error(f"Não foi possível converter o cadastro: {exc}")
        else:
            st.success(
                f"**{row['full_name']}** agora tem acesso com o e-mail {email.strip().lower()}."
            )
            st.rerun()


def _render_password_reset(conn, current_user, row) -> None:
    """Bloco 'Redefinir senha' de um usuário na listagem."""
    st.markdown("---")
    st.markdown("**Redefinir senha**")

    if row["id"] == current_user["id"]:
        st.caption(
            "Para trocar a sua própria senha, use **Alterar minha senha** no menu "
            "— redefinir a si mesmo encerraria esta sessão no mesmo instante."
        )
        return

    st.caption(
        "Gera uma senha temporária para entregar ao usuário presencialmente. A "
        "senha antiga deixa de valer imediatamente, a sessão que ele tiver aberta "
        "é encerrada e a troca é obrigatória no próximo login."
    )
    new_password = st.text_input(
        "Nova senha temporária (opcional)",
        type="password",
        key=f"reset_pw_{row['id']}",
        help="Deixe em branco para o sistema gerar uma senha aleatória.",
    )
    confirm = st.checkbox(
        f"Confirmo a redefinição da senha de **{row['full_name']}** ({row['email']}).",
        key=f"confirm_reset_{row['id']}",
    )
    if st.button(
        "🔑 Redefinir senha",
        key=f"reset_{row['id']}",
        disabled=not confirm,
    ):
        try:
            temp_password = admin_reset_password(
                conn, current_user, row["id"], new_password or None
            )
        except ValueError as exc:
            conn.rollback()
            st.error(f"Não foi possível redefinir a senha: {exc}")
        else:
            st.session_state[RESET_RESULT_KEY] = {
                "full_name": row["full_name"],
                "email": row["email"],
                "password": temp_password,
            }
            st.rerun()


def show_user_management(conn, current_user):
    st.header("Gestão de Usuários")

    if count_admins(conn) <= 1:
        st.warning(SINGLE_ADMIN_WARNING)

    _render_reset_result()
    _render_new_admin_form(conn)

    st.subheader("Usuários cadastrados")
    query = st.text_input("Buscar por nome, e-mail ou telefone", key="users_query")

    total = count_users(conn, query)
    if total:
        offset = _paginate(total, "users")
        for row in list_users(conn, query, offset=offset):
            is_admin = row["role"] == "admin"
            icon = "🛡️" if is_admin else ("🪪" if row["is_simplified"] else "👤")
            contact = display_email(row["email"]) or "sem e-mail"
            with st.expander(f"{icon} {row['full_name']} — {contact}"):
                if row["is_simplified"]:
                    perfil = "Leitor (cadastro de balcão)"
                else:
                    perfil = "Administrador" if is_admin else "Leitor"
                st.caption(
                    f"Perfil: **{perfil}** · "
                    f"Telefone: {row['phone'] or '—'} · "
                    f"Cadastro: {row['created_at'][:10]}"
                )
                if row["is_simplified"]:
                    st.info(
                        "Cadastro criado no balcão: serve para registrar empréstimos "
                        "em nome desta pessoa e **não dá acesso ao sistema**."
                    )
                elif row["must_change_password"]:
                    st.info("Troca de senha pendente no próximo login.")
                # Conta de balcão não tem senha para redefinir: o que ela pode
                # ganhar é acesso, e é isso que o bloco oferece no lugar.
                if row["is_simplified"]:
                    _render_convert_simplified(conn, row)
                else:
                    _render_password_reset(conn, current_user, row)
    else:
        st.info("Nenhum usuário encontrado.")

    with st.expander("🗒️ Auditoria de redefinições de senha"):
        resets = [
            {
                "Quando": e["created_at"].replace("T", " "),
                "Quem redefiniu": e["actor_email"],
                "Senha de": e["target_email"],
            }
            for e in list_admin_audit(conn)
            if e["action"] == AUDIT_ACTION_PASSWORD_RESET
        ]
        if resets:
            st.dataframe(resets, hide_index=True, width="stretch")
        else:
            st.caption("Nenhuma redefinição de senha registrada até agora.")


def _show_loaned_but_available_warning(conn) -> None:
    """Alerta do sentido inverso: livro liberado no catálogo com empréstimo
    ativo. Só detecta e orienta — a correção é registrar a devolução em
    Empréstimos, que fecha o empréstimo e o status de uma vez só."""
    total = count_books_loaned_but_available(conn)
    if not total:
        return

    st.error(
        f"⚠️ **{total} livro(s) com empréstimo ativo, mas fora do status "
        "'Emprestado'.** Nesse estado o mesmo exemplar pode ser emprestado a um "
        "segundo leitor. Registre a devolução em **Empréstimos** (isso fecha o "
        "empréstimo e libera o livro juntos) ou volte o status para *Emprestado* "
        "em **Gestão de Livros**."
    )
    st.dataframe(
        [
            {
                "Livro": f"{r['title']} ({r['code']})",
                "Status atual": r["status"],
                "Consta com": r["full_name"],
                "Desde": r["loan_date"],
            }
            for r in list_books_loaned_but_available(conn)
        ],
        hide_index=True,
        width="stretch",
    )


def _render_reconcile_loan(conn, book, cache: dict) -> None:
    """Corpo do "Registrar empréstimo" da reconciliação: escolher um leitor já
    cadastrado ou criar o cadastro de balcão na hora.

    O caminho do cadastro novo grava a conta e o empréstimo na MESMA
    transação: se a reconciliação falhar (outra sessão regularizou o livro no
    meio do caminho), o rollback leva junto o cadastro que acabou de nascer, e
    não fica um leitor fantasma sem empréstimo nenhum.
    """
    book_id = book["id"]
    loan_day = st.date_input(
        "Data do empréstimo", value=date.today(), key=f"rec_loan_date_{book_id}"
    )
    due = st.date_input(
        "Devolução prevista", value=default_due_date(), key=f"rec_due_{book_id}"
    )

    def _registrar(user_id, quem):
        try:
            reconcile_register_loan(
                conn, book_id, user_id, loan_date=loan_day, due_date=due
            )
            conn.commit()
        except (ValueError, IntegrityError) as exc:
            conn.rollback()
            st.error(str(exc))
        else:
            st.success(f'Empréstimo de "{book["title"]}" registrado para {quem}.')
            st.rerun()

    st.markdown("**Quem está com o livro**")
    term = st.text_input(
        "Buscar leitor por nome ou e-mail", key=f"rec_query_{book_id}"
    )
    matches, _total = _borrowers_for_term(conn, term, cache)
    if matches:
        options = {borrower_label(b): b["id"] for b in matches}
        who = st.selectbox("Leitor", list(options.keys()), key=f"rec_user_{book_id}")
        if st.button("Confirmar registro", key=f"rec_confirm_{book_id}", width="stretch"):
            _registrar(options[who], who)
    else:
        st.caption(
            "Nenhum leitor cadastrado corresponde à busca."
            if term
            else "Nenhum leitor cadastrado ainda."
        )

    st.markdown("---")
    st.caption(
        "Não está na lista? Cadastre aqui mesmo. O cadastro de balcão guarda só "
        "quem está com o livro — não dá acesso ao sistema e o e-mail é opcional."
    )
    new_name = st.text_input("Nome completo do leitor", key=f"rec_new_name_{book_id}")
    new_email = st.text_input("E-mail (opcional)", key=f"rec_new_email_{book_id}")
    new_phone = st.text_input("Telefone (opcional)", key=f"rec_new_phone_{book_id}")
    if st.button(
        "Cadastrar e registrar empréstimo",
        key=f"rec_new_confirm_{book_id}",
        width="stretch",
    ):
        try:
            new_id = create_simplified_reader(conn, new_name, new_email, new_phone)
        except (ValueError, IntegrityError) as exc:
            conn.rollback()
            st.error(str(exc))
        else:
            _registrar(new_id, new_name.strip())


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

    _show_loaned_but_available_warning(conn)

    if not total:
        st.success(
            "Nenhum livro pendente de reconciliação."
            if not query
            else "Nenhum livro pendente corresponde à busca."
        )
        return

    offset = _paginate(total, "reconcile")
    rows = list_unreconciled_books(conn, query, offset=offset)

    borrower_cache: dict = {}
    for r in rows:
        with st.container(border=True):
            st.markdown(f"**{r['title']}** ({r['code']})")
            st.caption(f"{r['author']} · {r['category'] or 'sem categoria'}")

            col_loan, col_return = st.columns(2)

            with col_loan:
                with st.popover("📝 Registrar empréstimo", width="stretch"):
                    _render_reconcile_loan(conn, r, borrower_cache)

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
        "`;` e codificação (UTF-8, com ou sem BOM, ou a do Excel no Windows) são "
        "detectados automaticamente."
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
        try:
            count = commit_import(conn, processed)
            conn.commit()
        except (ValueError, IntegrityError) as exc:
            conn.rollback()
            st.error(
                "Não foi possível concluir a importação e **nenhum livro foi gravado** "
                "(a importação inteira é uma transação só). Isso costuma acontecer "
                "quando um código já foi cadastrado por outra pessoa depois que esta "
                f"pré-visualização foi gerada. Reenvie o arquivo para recalcular. "
                f"Detalhe: {exc}"
            )
        else:
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
                    "Gestão de Usuários",
                    "Alterar minha senha",
                ],
            )
        else:
            page = st.radio("Menu", ["Catálogo", "Meus Empréstimos", "Alterar minha senha"])
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
    elif page == "Gestão de Usuários":
        show_user_management(conn, user)
    elif page == "Alterar minha senha":
        show_change_password_screen(conn, user)

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
    try:
        init_db()
    except BootstrapAdminNotConfigured as exc:
        # Estado de configuração, não erro de execução: mostra o que fazer e
        # para aqui, em vez de seguir para uma tela de login sem nenhuma conta.
        st.error(str(exc))
        st.stop()

    if "user" not in st.session_state:
        st.session_state.user = None

    with get_connection() as conn:
        # A sessão é reconferida contra o banco a cada rerun: se um admin
        # redefiniu esta senha (ou a conta sumiu), a aba que ficou aberta com
        # a senha antiga cai aqui, em vez de continuar navegando.
        if st.session_state.user is not None and not _session_is_current(
            conn, st.session_state.user
        ):
            st.session_state.user = None
            st.session_state.session_revoked = True

        if st.session_state.user is None:
            if st.session_state.pop("session_revoked", False):
                st.warning(
                    "Sua sessão foi encerrada porque a senha desta conta foi "
                    "redefinida por um administrador. Entre novamente com a "
                    "senha temporária que você recebeu."
                )
            show_auth_screen(conn)
        elif st.session_state.user.get("must_change_password"):
            show_change_password_screen(conn, st.session_state.user, forced=True)
        else:
            show_app(conn)


if __name__ == "__main__":
    main()
