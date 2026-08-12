"""
test_app.py — Suíte pytest para app.py
=======================================
Cobre:
  1) generate_book_code (mesmos casos de borda de bookCode.test.ts)
  2) parse_csv_bytes (delimitador , / ; e encoding utf-8 / utf-8-sig)
  3) process_import_rows (lógica de importação em lote: código mantido vs.
     gerado, sequencial incremental por autor no lote, status inválido,
     campos obrigatórios vazios, duplicidade de código no CSV e contra o
     banco)

Os testes usam SQLite local descartável através da mesma camada SQLAlchemy
usada em produção (Postgres/Supabase) — não dependem de acesso de rede.
"""

import pytest
from sqlalchemy import text

import app
from app import (
    add_book,
    count_loans_for_book,
    create_schema,
    create_user,
    delete_book,
    generate_book_code,
    get_connection,
    get_engine,
    get_user_by_email,
    init_db,
    is_valid_email,
    parse_csv_bytes,
    process_import_rows,
    request_loan,
    return_loan,
    verify_password,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    database_url = f"sqlite:///{tmp_path}/test.db"
    engine = get_engine(database_url)
    create_schema(engine)
    connection = get_connection(database_url)
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# generate_book_code — mesmos casos de borda de bookCode.test.ts
# ---------------------------------------------------------------------------

def test_exemplo_da_especificacao_joao_mellao_neto_primeiro_livro():
    assert generate_book_code("João Mellão Neto", 0) == "NETJ-001"


def test_segundo_e_decimo_livro_do_mesmo_autor_incrementa_sequencia():
    assert generate_book_code("João Mellão Neto", 1) == "NETJ-002"
    assert generate_book_code("João Mellão Neto", 9) == "NETJ-010"


def test_sobrenome_comum_sem_sufixo_geracional():
    assert generate_book_code("Clarice Lispector", 2) == "LISC-003"


def test_nome_com_particula_usa_ultimo_token():
    assert generate_book_code("Machado de Assis", 0) == "ASSM-001"


def test_autor_com_um_unico_nome_usa_o_mesmo_token_duas_vezes():
    assert generate_book_code("Homero", 0) == "HOMH-001"


def test_remove_acentos_do_sobrenome_e_do_primeiro_nome():
    assert generate_book_code("Eça de Queirós", 0) == "QUEE-001"


def test_sobrenome_curto_e_completado_com_x():
    assert generate_book_code("Ana Li", 0) == "LIXA-001"


def test_sobrenome_composto_com_hifen_e_tratado_como_um_token_so():
    assert generate_book_code("Ana Paula Souza-Lima", 0) == "SOUA-001"


def test_sequencia_ultrapassa_999_sem_truncar():
    assert generate_book_code("João Mellão Neto", 999) == "NETJ-1000"


def test_treat_suffix_as_surname_false_usa_sobrenome_anterior_ao_sufixo():
    assert (
        generate_book_code("João Mellão Neto", 0, treat_suffix_as_surname=False)
        == "MELJ-001"
    )
    assert (
        generate_book_code("Carlos Andrade Filho", 0, treat_suffix_as_surname=False)
        == "ANDC-001"
    )


def test_treat_suffix_as_surname_false_nao_afeta_autores_sem_sufixo():
    assert (
        generate_book_code("Clarice Lispector", 0, treat_suffix_as_surname=False)
        == "LISC-001"
    )


def test_lanca_erro_para_nome_vazio():
    with pytest.raises(ValueError):
        generate_book_code("", 0)
    with pytest.raises(ValueError):
        generate_book_code("   ", 0)


def test_lanca_erro_para_contagem_negativa():
    with pytest.raises(ValueError):
        generate_book_code("Autor Teste", -1)


# ---------------------------------------------------------------------------
# is_valid_email
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "email,expected",
    [
        ("leitor@biblioteca.org", True),
        ("nome.sobrenome@dominio.com.br", True),
        ("sememail", False),
        ("sem@dominio", False),
        ("@dominio.com", False),
        ("usuario@", False),
        ("", False),
        ("com espaco@dominio.com", False),
    ],
)
def test_is_valid_email(email, expected):
    assert is_valid_email(email) is expected


# ---------------------------------------------------------------------------
# parse_csv_bytes — delimitador e encoding
# ---------------------------------------------------------------------------

def test_parse_csv_bytes_delimitador_virgula():
    data = "titulo,autor,categoria\nDom Casmurro,Machado de Assis,Romance\n".encode("utf-8")
    rows = parse_csv_bytes(data)
    assert rows == [
        {"titulo": "Dom Casmurro", "autor": "Machado de Assis", "categoria": "Romance"}
    ]


def test_parse_csv_bytes_delimitador_ponto_e_virgula():
    data = "titulo;autor;categoria\nDom Casmurro;Machado de Assis;Romance\n".encode("utf-8")
    rows = parse_csv_bytes(data)
    assert rows == [
        {"titulo": "Dom Casmurro", "autor": "Machado de Assis", "categoria": "Romance"}
    ]


def test_parse_csv_bytes_com_bom_utf8_sig():
    data = "titulo,autor,categoria\nDom Casmurro,Machado de Assis,Romance\n".encode(
        "utf-8-sig"
    )
    rows = parse_csv_bytes(data)
    assert rows[0]["titulo"] == "Dom Casmurro"
    # BOM não deve vazar para dentro do nome da primeira coluna
    assert list(rows[0].keys())[0] == "titulo"


# ---------------------------------------------------------------------------
# process_import_rows — regras de importação em lote
# ---------------------------------------------------------------------------

def test_codigo_preenchido_e_mantido_como_veio(conn):
    rows = [{"titulo": "Livro A", "autor": "Fulano de Tal", "categoria": "X", "codigo": "ABC-999"}]
    processed, summary = process_import_rows(conn, rows)
    assert processed[0]["codigo"] == "ABC-999"
    assert processed[0]["codigo_origem"] == "mantido"
    assert processed[0]["erros"] == []
    assert summary == {"total": 1, "mantidos": 1, "gerados": 0, "com_erro": 0}


def test_codigo_ausente_e_gerado_automaticamente(conn):
    rows = [{"titulo": "Livro A", "autor": "Clarice Lispector", "categoria": ""}]
    processed, summary = process_import_rows(conn, rows)
    assert processed[0]["codigo"] == "LISC-001"
    assert processed[0]["codigo_origem"] == "gerado"
    assert processed[0]["erros"] == []


def test_sequencial_incremental_por_autor_considera_banco_e_linhas_anteriores(conn):
    # já existem 2 livros de Clarice Lispector no banco
    add_book(conn, "Livro Existente 1", "Clarice Lispector", "Romance")
    add_book(conn, "Livro Existente 2", "Clarice Lispector", "Romance")
    conn.commit()

    rows = [
        {"titulo": "Novo 1", "autor": "Clarice Lispector", "categoria": ""},
        {"titulo": "Novo 2", "autor": "Clarice Lispector", "categoria": ""},
        {"titulo": "Novo 3", "autor": "Machado de Assis", "categoria": ""},
    ]
    processed, summary = process_import_rows(conn, rows)

    assert processed[0]["codigo"] == "LISC-003"  # 2 no banco + 0 no lote
    assert processed[1]["codigo"] == "LISC-004"  # 2 no banco + 1 no lote
    assert processed[2]["codigo"] == "ASSM-001"  # autor diferente, contagem própria
    assert summary["gerados"] == 3
    assert summary["com_erro"] == 0


def test_codigo_duplicado_dentro_do_proprio_csv_e_sinalizado(conn):
    rows = [
        {"titulo": "Livro A", "autor": "Fulano", "codigo": "DUP-001"},
        {"titulo": "Livro B", "autor": "Beltrano", "codigo": "DUP-001"},
    ]
    processed, summary = process_import_rows(conn, rows)
    assert processed[0]["erros"] == []
    assert "Código duplicado" in processed[1]["erros"][0]
    assert summary["com_erro"] == 1


def test_codigo_duplicado_contra_livro_ja_existente_no_banco_e_sinalizado(conn):
    add_book(conn, "Livro Existente", "Fulano de Tal", "Romance")
    conn.commit()
    existing_code = conn.execute(text("SELECT code FROM books")).mappings().first()["code"]

    rows = [{"titulo": "Livro Novo", "autor": "Outro Autor", "codigo": existing_code}]
    processed, summary = process_import_rows(conn, rows)
    assert "Código duplicado" in processed[0]["erros"][0]
    assert summary["com_erro"] == 1


def test_status_valido_e_respeitado(conn):
    rows = [{"titulo": "Livro A", "autor": "Fulano", "status": "Em Manutenção"}]
    processed, _ = process_import_rows(conn, rows)
    assert processed[0]["status"] == "Em Manutenção"
    assert processed[0]["erros"] == []


def test_status_ausente_usa_padrao_disponivel(conn):
    rows = [{"titulo": "Livro A", "autor": "Fulano", "status": ""}]
    processed, _ = process_import_rows(conn, rows)
    assert processed[0]["status"] == "Disponível"
    assert processed[0]["erros"] == []


def test_status_invalido_e_sinalizado_como_erro_bloqueante(conn):
    rows = [{"titulo": "Livro A", "autor": "Fulano", "status": "Perdido"}]
    processed, summary = process_import_rows(conn, rows)
    assert any("Status inválido" in e for e in processed[0]["erros"])
    assert summary["com_erro"] == 1


def test_titulo_vazio_e_erro_bloqueante(conn):
    rows = [{"titulo": "", "autor": "Fulano"}]
    processed, summary = process_import_rows(conn, rows)
    assert "Título é obrigatório." in processed[0]["erros"]
    assert summary["com_erro"] == 1


def test_autor_vazio_e_erro_bloqueante(conn):
    rows = [{"titulo": "Livro A", "autor": ""}]
    processed, summary = process_import_rows(conn, rows)
    assert "Autor é obrigatório." in processed[0]["erros"]
    assert summary["com_erro"] == 1


def test_resumo_estatistico_mistura_mantidos_gerados_e_erros(conn):
    rows = [
        {"titulo": "Livro A", "autor": "Fulano", "codigo": "COD-001"},
        {"titulo": "Livro B", "autor": "Beltrano"},
        {"titulo": "", "autor": "Ciclano"},
    ]
    processed, summary = process_import_rows(conn, rows)
    assert summary["total"] == 3
    assert summary["mantidos"] == 1
    assert summary["gerados"] == 2
    assert summary["com_erro"] == 1


# ---------------------------------------------------------------------------
# Camada de banco (SQLAlchemy) — init_db, engine/conexão, empréstimos
# ---------------------------------------------------------------------------

def test_init_db_cria_schema_admin_e_seed_de_forma_idempotente(tmp_path):
    database_url = f"sqlite:///{tmp_path}/init.db"
    init_db(database_url)
    init_db(database_url)  # chamado 2x, como acontece a cada rerun do Streamlit

    with get_connection(database_url) as connection:
        admin = get_user_by_email(connection, "admin@biblioteca.org")
        assert admin is not None
        assert admin["role"] == "admin"
        assert verify_password("admin123", admin["password_hash"], admin["salt"])

        user_count = connection.execute(text("SELECT COUNT(*) AS n FROM users")).mappings().first()["n"]
        book_count = connection.execute(text("SELECT COUNT(*) AS n FROM books")).mappings().first()["n"]
        assert user_count == 1  # não duplicou o admin na 2ª chamada
        assert book_count == 4  # não duplicou a carga inicial de livros


def test_get_connection_sem_database_url_levanta_erro_claro(monkeypatch):
    monkeypatch.setattr(app.st, "secrets", {})
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        app.get_engine()


def test_fluxo_completo_de_emprestimo_e_devolucao(conn):
    create_user(conn, "Leitor Teste", "leitor@teste.org", "11999999999", "senha123", "leitor")
    conn.commit()
    leitor = get_user_by_email(conn, "leitor@teste.org")

    code = add_book(conn, "1984", "George Orwell", "Distopia")
    conn.commit()
    book = conn.execute(text("SELECT * FROM books WHERE code = :code"), {"code": code}).mappings().first()
    assert book["status"] == "Disponível"

    request_loan(conn, book["id"], leitor["id"])
    conn.commit()
    book_after_loan = conn.execute(
        text("SELECT * FROM books WHERE id = :id"), {"id": book["id"]}
    ).mappings().first()
    assert book_after_loan["status"] == "Emprestado"

    with pytest.raises(ValueError):
        request_loan(conn, book["id"], leitor["id"])

    active_loan = conn.execute(
        text("SELECT * FROM loans WHERE book_id = :book_id AND status = 'ativo'"),
        {"book_id": book["id"]},
    ).mappings().first()
    return_loan(conn, active_loan["id"])
    conn.commit()

    book_after_return = conn.execute(
        text("SELECT * FROM books WHERE id = :id"), {"id": book["id"]}
    ).mappings().first()
    assert book_after_return["status"] == "Disponível"

    with pytest.raises(ValueError):
        return_loan(conn, active_loan["id"])


# ---------------------------------------------------------------------------
# delete_book — remoção de livro em Gestão de Livros
# ---------------------------------------------------------------------------

def _books_count(conn, book_id) -> int:
    return conn.execute(
        text("SELECT COUNT(*) AS n FROM books WHERE id = :id"), {"id": book_id}
    ).mappings().first()["n"]


def _loans_count_for(conn, book_id) -> int:
    return conn.execute(
        text("SELECT COUNT(*) AS n FROM loans WHERE book_id = :id"), {"id": book_id}
    ).mappings().first()["n"]


def test_delete_book_bloqueado_quando_ha_emprestimo_ativo(conn):
    create_user(conn, "Leitor Um", "leitor.um@teste.org", "", "senha123", "leitor")
    conn.commit()
    leitor = get_user_by_email(conn, "leitor.um@teste.org")

    code = add_book(conn, "Livro Emprestado", "Autor Um", "Categoria")
    conn.commit()
    book = conn.execute(
        text("SELECT * FROM books WHERE code = :code"), {"code": code}
    ).mappings().first()

    request_loan(conn, book["id"], leitor["id"])
    conn.commit()

    with pytest.raises(ValueError, match="devolução"):
        delete_book(conn, book["id"])

    # nada foi apagado: livro e empréstimo ativo continuam intactos
    assert _books_count(conn, book["id"]) == 1
    assert _loans_count_for(conn, book["id"]) == 1


def test_delete_book_com_historico_apenas_devolvido_remove_livro_e_loans(conn):
    create_user(conn, "Leitor Dois", "leitor.dois@teste.org", "", "senha123", "leitor")
    conn.commit()
    leitor = get_user_by_email(conn, "leitor.dois@teste.org")

    code = add_book(conn, "Livro Com Historico", "Autor Dois", "Categoria")
    conn.commit()
    book = conn.execute(
        text("SELECT * FROM books WHERE code = :code"), {"code": code}
    ).mappings().first()

    # dois ciclos de empréstimo/devolução -> 2 registros em loans, ambos devolvidos
    for _ in range(2):
        request_loan(conn, book["id"], leitor["id"])
        conn.commit()
        active = conn.execute(
            text("SELECT * FROM loans WHERE book_id = :id AND status = 'ativo'"),
            {"id": book["id"]},
        ).mappings().first()
        return_loan(conn, active["id"])
        conn.commit()

    assert count_loans_for_book(conn, book["id"]) == 2

    delete_book(conn, book["id"])
    conn.commit()

    assert _books_count(conn, book["id"]) == 0
    assert _loans_count_for(conn, book["id"]) == 0


def test_delete_book_sem_historico_remove_direto(conn):
    code = add_book(conn, "Livro Sem Historico", "Autor Tres", "Categoria")
    conn.commit()
    book = conn.execute(
        text("SELECT * FROM books WHERE code = :code"), {"code": code}
    ).mappings().first()

    assert count_loans_for_book(conn, book["id"]) == 0

    delete_book(conn, book["id"])
    conn.commit()

    assert _books_count(conn, book["id"]) == 0


def test_delete_book_e_atomico_rollback_desfaz_as_duas_exclusoes(conn):
    create_user(conn, "Leitor Quatro", "leitor.quatro@teste.org", "", "senha123", "leitor")
    conn.commit()
    leitor = get_user_by_email(conn, "leitor.quatro@teste.org")

    code = add_book(conn, "Livro Atomico", "Autor Quatro", "Categoria")
    conn.commit()
    book = conn.execute(
        text("SELECT * FROM books WHERE code = :code"), {"code": code}
    ).mappings().first()

    request_loan(conn, book["id"], leitor["id"])
    conn.commit()
    active = conn.execute(
        text("SELECT * FROM loans WHERE book_id = :id AND status = 'ativo'"),
        {"id": book["id"]},
    ).mappings().first()
    return_loan(conn, active["id"])
    conn.commit()

    delete_book(conn, book["id"])  # ainda não commitado
    conn.rollback()

    # sem commit, o rollback desfaz as duas exclusões (livro + loans) juntas
    assert _books_count(conn, book["id"]) == 1
    assert _loans_count_for(conn, book["id"]) == 1
