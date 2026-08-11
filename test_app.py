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
"""

import sqlite3

import pytest

from app import (
    add_book,
    create_schema,
    generate_book_code,
    is_valid_email,
    parse_csv_bytes,
    process_import_rows,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    create_schema(connection)
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
    existing_code = conn.execute("SELECT code FROM books").fetchone()["code"]

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
