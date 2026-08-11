"""
app.py — Protótipo funcional: Sistema de Biblioteca Comunitária
=================================================================
Stack: Python + Streamlit + SQLite (lógica de negócio e UI num único
arquivo). Ver README.md para instruções completas.

Como rodar:
    pip install -r requirements.txt
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
from contextlib import closing
from datetime import datetime

import streamlit as st

DB_PATH = "biblioteca.db"


# ---------------------------------------------------------------------------
# Algoritmo de geração de código do livro (mesma regra do bookCode.ts)
# ---------------------------------------------------------------------------

GENERATIONAL_SUFFIXES = {"NETO", "FILHO", "JUNIOR", "JR", "SOBRINHO"}


def _strip_diacritics(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _only_upper_letters(value: str) -> str:
    return re.sub(r"[^A-Z]", "", _strip_diacritics(value).upper())


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
# Banco de dados
# ---------------------------------------------------------------------------

def get_connection():
    import sqlite3

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = os.urandom(16).hex()
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return digest, salt


def verify_password(password: str, digest: str, salt: str) -> bool:
    check, _ = hash_password(password, salt)
    return check == digest


def create_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            phone TEXT,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','leitor')),
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            category TEXT,
            status TEXT NOT NULL DEFAULT 'Disponível'
                CHECK(status IN ('Disponível','Emprestado','Em Manutenção')),
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            loan_date TEXT NOT NULL,
            return_date TEXT,
            status TEXT NOT NULL DEFAULT 'ativo' CHECK(status IN ('ativo','devolvido')),
            FOREIGN KEY(book_id) REFERENCES books(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )


def init_db():
    with closing(get_connection()) as conn, conn:
        create_schema(conn)

        if conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0:
            create_user(
                conn, "Administrador", "admin@biblioteca.org", "", "admin123", "admin"
            )

        if conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"] == 0:
            seed = [
                ("Dom Casmurro", "Machado de Assis", "Literatura Brasileira"),
                ("Grande Sertão: Veredas", "João Guimarães Rosa", "Literatura Brasileira"),
                ("Memórias Póstumas de Brás Cubas", "Machado de Assis", "Literatura Brasileira"),
                ("Vidas Secas", "Graciliano Ramos", "Literatura Brasileira"),
            ]
            for title, author, category in seed:
                add_book(conn, title, author, category)


# ---------------------------------------------------------------------------
# Usuários
# ---------------------------------------------------------------------------

def create_user(conn, full_name, email, phone, password, role):
    digest, salt = hash_password(password)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO users
           (full_name, email, phone, password_hash, salt, role, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (full_name, email.lower().strip(), phone, digest, salt, role, now),
    )


def get_user_by_email(conn, email):
    return conn.execute(
        "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
    ).fetchone()


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

def count_books_by_author(conn, author: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM books WHERE author = ?", (author,)
    ).fetchone()["n"]


def add_book(conn, title, author, category):
    existing = count_books_by_author(conn, author)
    code = generate_book_code(author, existing)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO books (code, title, author, category, status, created_at)
           VALUES (?,?,?,?,?,?)""",
        (code, title, author, category, "Disponível", now),
    )
    return code


# ---------------------------------------------------------------------------
# Importação em lote (CSV)
# ---------------------------------------------------------------------------

VALID_BOOK_STATUSES = {"Disponível", "Emprestado", "Em Manutenção"}


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


def process_import_rows(conn, rows: list[dict]) -> tuple[list[dict], dict]:
    """Processa as linhas de um CSV de importação de livros, calculando
    código (mantido ou gerado) e status finais, e sinalizando erros
    bloqueantes (título/autor vazios, status inválido, código duplicado
    — seja contra o banco, seja entre linhas do próprio arquivo).

    Retorna (linhas_processadas, resumo_estatistico).
    """
    existing_codes = {
        row["code"] for row in conn.execute("SELECT code FROM books").fetchall()
    }
    used_codes_in_batch: set[str] = set()
    author_batch_counts: dict[str, int] = {}

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

        if status_in and status_in not in VALID_BOOK_STATUSES:
            errors.append(f"Status inválido: '{status_in}'.")
        final_status = status_in if status_in in VALID_BOOK_STATUSES else "Disponível"

        final_code = ""
        if code_in:
            code_source = "mantido"
            final_code = code_in
            if final_code in existing_codes or final_code in used_codes_in_batch:
                errors.append(f"Código duplicado: '{final_code}'.")
        elif author:
            code_source = "gerado"
            db_count = count_books_by_author(conn, author)
            batch_count = author_batch_counts.get(author, 0)
            final_code = generate_book_code(author, db_count + batch_count)
            if final_code in existing_codes or final_code in used_codes_in_batch:
                errors.append(f"Colisão inesperada de código gerado: '{final_code}'.")
        else:
            code_source = "gerado"

        if author:
            author_batch_counts[author] = author_batch_counts.get(author, 0) + 1
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
            """INSERT INTO books (code, title, author, category, status, created_at)
               VALUES (?,?,?,?,?,?)""",
            (row["codigo"], row["titulo"], row["autor"], row["categoria"], row["status"], now),
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# Empréstimos
# ---------------------------------------------------------------------------

def request_loan(conn, book_id, user_id):
    book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if book is None or book["status"] != "Disponível":
        raise ValueError("Livro indisponível para empréstimo.")
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO loans (book_id, user_id, loan_date, status) VALUES (?,?,?,?)",
        (book_id, user_id, now, "ativo"),
    )
    conn.execute("UPDATE books SET status='Emprestado' WHERE id=?", (book_id,))


def return_loan(conn, loan_id):
    loan = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if loan is None or loan["status"] != "ativo":
        raise ValueError("Empréstimo não está ativo.")
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE loans SET status='devolvido', return_date=? WHERE id=?", (now, loan_id)
    )
    conn.execute("UPDATE books SET status='Disponível' WHERE id=?", (loan["book_id"],))


# ---------------------------------------------------------------------------
# Telas (UI)
# ---------------------------------------------------------------------------

def show_auth_screen(conn):
    st.title("📚 Biblioteca Comunitária")
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
    rows = conn.execute("SELECT * FROM books ORDER BY title").fetchall()

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
        cols = st.columns([4, 3, 2, 2, 2])
        cols[0].write(f"**{r['title']}**")
        cols[1].write(r["author"])
        cols[2].write(r["code"])
        cols[3].write(f"{status_emoji.get(r['status'], '')} {r['status']}")
        if user["role"] == "leitor" and r["status"] == "Disponível":
            if cols[4].button("Pegar emprestado", key=f"borrow_{r['id']}"):
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
            category = st.text_input("Categoria")
            if st.form_submit_button("Cadastrar livro"):
                if not title or not author:
                    st.error("Título e autor são obrigatórios.")
                else:
                    code = add_book(conn, title, author, category)
                    conn.commit()
                    st.success(f"Livro cadastrado com código **{code}**")
                    st.rerun()

    st.subheader("Livros cadastrados")
    statuses = ["Disponível", "Emprestado", "Em Manutenção"]
    rows = conn.execute("SELECT * FROM books ORDER BY id DESC").fetchall()
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
                col1, col2 = st.columns(2)
                save = col1.form_submit_button("Salvar alterações")
                delete = col2.form_submit_button("🗑️ Remover livro")
                if save:
                    conn.execute(
                        "UPDATE books SET title=?, author=?, category=?, status=? WHERE id=?",
                        (title, author, category, status, r["id"]),
                    )
                    conn.commit()
                    st.success("Livro atualizado.")
                    st.rerun()
                if delete:
                    conn.execute("DELETE FROM books WHERE id=?", (r["id"],))
                    conn.commit()
                    st.warning("Livro removido.")
                    st.rerun()


def show_loan_management(conn):
    st.header("Empréstimos ativos")
    rows = conn.execute(
        """
        SELECT loans.id AS loan_id, books.title, books.code,
               users.full_name, loans.loan_date
        FROM loans
        JOIN books ON books.id = loans.book_id
        JOIN users ON users.id = loans.user_id
        WHERE loans.status = 'ativo'
        ORDER BY loans.loan_date
        """
    ).fetchall()

    if not rows:
        st.info("Nenhum empréstimo ativo no momento.")

    for r in rows:
        cols = st.columns([3, 2, 3, 2])
        cols[0].write(f"**{r['title']}** ({r['code']})")
        cols[1].write(r["full_name"])
        cols[2].write(f"Emprestado em {r['loan_date']}")
        if cols[3].button("✅ Registrar devolução", key=f"return_{r['loan_id']}"):
            return_loan(conn, r["loan_id"])
            conn.commit()
            st.success("Devolução registrada.")
            st.rerun()


def show_my_loans(conn, user):
    st.header("Meus Empréstimos")

    st.subheader("Livros em minha posse")
    active = conn.execute(
        """
        SELECT loans.id AS loan_id, books.title, books.code, loans.loan_date
        FROM loans JOIN books ON books.id = loans.book_id
        WHERE loans.user_id = ? AND loans.status = 'ativo'
        """,
        (user["id"],),
    ).fetchall()

    if not active:
        st.info("Você não tem livros emprestados no momento.")
    for r in active:
        cols = st.columns([4, 3, 3])
        cols[0].write(f"**{r['title']}** ({r['code']})")
        cols[1].write(f"Desde {r['loan_date']}")
        if cols[2].button("Solicitar devolução", key=f"selfreturn_{r['loan_id']}"):
            return_loan(conn, r["loan_id"])
            conn.commit()
            st.success("Devolução registrada. Obrigado!")
            st.rerun()

    st.subheader("Histórico completo")
    history = conn.execute(
        """
        SELECT books.title, books.code, loans.loan_date, loans.return_date, loans.status
        FROM loans JOIN books ON books.id = loans.book_id
        WHERE loans.user_id = ?
        ORDER BY loans.loan_date DESC
        """,
        (user["id"],),
    ).fetchall()
    for r in history:
        st.write(
            f"- **{r['title']}** ({r['code']}) — {r['loan_date']} → "
            f"{r['return_date'] or 'em aberto'} [{r['status']}]"
        )


def show_csv_import(conn):
    st.header("Importar carga de livros (CSV)")
    st.caption(
        "Colunas aceitas (cabeçalho na 1ª linha): **titulo**, **autor**, **categoria**, "
        "**codigo** (opcional), **status** (opcional — Disponível/Emprestado/Em Manutenção). "
        "Delimitador `,` ou `;` e encoding UTF-8 (com ou sem BOM) são detectados automaticamente."
    )
    uploaded = st.file_uploader("Selecione o arquivo CSV", type=["csv"])

    if uploaded is None:
        for key in ("csv_import_processed", "csv_import_summary", "csv_import_filename"):
            st.session_state.pop(key, None)
        return

    if st.session_state.get("csv_import_filename") != uploaded.name:
        try:
            rows = parse_csv_bytes(uploaded.getvalue())
        except (UnicodeDecodeError, csv.Error) as exc:
            st.error(f"Não foi possível ler o arquivo CSV: {exc}")
            return
        processed, summary = process_import_rows(conn, rows)
        st.session_state.csv_import_processed = processed
        st.session_state.csv_import_summary = summary
        st.session_state.csv_import_filename = uploaded.name

    processed = st.session_state.csv_import_processed
    summary = st.session_state.csv_import_summary

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
    st.dataframe(preview_table, use_container_width=True)

    error_rows = [r for r in preview_table if r["Erros"]]
    if error_rows:
        st.error(
            f"{len(error_rows)} linha(s) com erro bloqueante. "
            "Corrija o arquivo e reenvie antes de confirmar a importação."
        )
        st.dataframe(error_rows, use_container_width=True)

    if st.button("Confirmar importação", disabled=bool(error_rows)):
        count = commit_import(conn, processed)
        conn.commit()
        st.success(f"{count} livro(s) importado(s) com sucesso.")
        for key in ("csv_import_processed", "csv_import_summary", "csv_import_filename"):
            st.session_state.pop(key, None)
        st.rerun()


def show_app(conn):
    user = st.session_state.user
    with st.sidebar:
        st.write(f"👤 **{user['full_name']}**")
        st.caption("Perfil: " + ("Administrador" if user["role"] == "admin" else "Leitor"))
        if user["role"] == "admin":
            page = st.radio(
                "Menu", ["Catálogo", "Gestão de Livros", "Empréstimos", "Importar CSV"]
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
    elif page == "Meus Empréstimos":
        show_my_loans(conn, user)
    elif page == "Importar CSV":
        show_csv_import(conn)


def main():
    st.set_page_config(page_title="Biblioteca Comunitária", page_icon="📚", layout="wide")
    init_db()

    if "user" not in st.session_state:
        st.session_state.user = None

    with closing(get_connection()) as conn:
        if st.session_state.user is None:
            show_auth_screen(conn)
        else:
            show_app(conn)


if __name__ == "__main__":
    main()
