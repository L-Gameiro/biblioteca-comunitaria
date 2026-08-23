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
    BookCodeAllocator,
    CATEGORY_FILTER_ALL,
    CODE_STRATEGY_AUTHOR,
    CODE_STRATEGY_NUMERIC,
    STATUS_FILTER_ALL,
    _detect_csv_delimiter,
    add_book,
    apply_column_mapping,
    count_books,
    count_loans_for_book,
    create_schema,
    create_user,
    delete_book,
    detect_column_mapping,
    generate_book_code,
    get_code_strategy,
    get_connection,
    get_csv_columns,
    get_engine,
    get_user_by_email,
    init_db,
    is_valid_email,
    list_book_categories,
    list_books,
    max_numeric_code_for_category,
    normalize_status,
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
# _detect_csv_delimiter — detecção robusta (bug real: espiritual.csv era lido
# como uma única coluna quando as primeiras linhas não tinham campos citados)
# ---------------------------------------------------------------------------

def test_deteccao_virgula_sem_nenhum_campo_citado_nas_primeiras_linhas():
    """(a) Reproduz o espiritual.csv: separado por vírgula, mas nenhuma das
    primeiras linhas tem campos entre aspas — a heurística antiga (que
    dependia de encontrar aspas) lia isso como 1 coluna só."""
    text = (
        "titulo,autor,categoria,codigo,status,obs1,obs2,obs3,obs4,obs5,obs6,obs7\r\n"
        "Livro A,Autor A,Espiritual,1,Disponível,x,x,x,x,x,x,x\r\n"
        "Livro B,Autor B,Espiritual,2,Disponível,x,x,x,x,x,x,x\r\n"
    )
    assert _detect_csv_delimiter(text) == ","

    rows = parse_csv_bytes(text.encode("utf-8-sig"))
    assert len(get_csv_columns(rows)) == 12
    assert rows[0]["titulo"] == "Livro A"
    assert rows[0]["codigo"] == "1"


def test_deteccao_virgula_com_campos_citados_na_primeira_linha_de_dados():
    """(b) Reproduz o literaria.csv: separado por vírgula, com campos entre
    aspas já na 1ª linha de dados — já funcionava antes, continua funcionando."""
    text = (
        "titulo,autor,categoria\r\n"
        '"Dom Casmurro","Machado de Assis","Literária"\r\n'
        "Vidas Secas,Graciliano Ramos,Literária\r\n"
    )
    assert _detect_csv_delimiter(text) == ","

    rows = parse_csv_bytes(text.encode("utf-8-sig"))
    assert len(get_csv_columns(rows)) == 3
    assert rows[0]["titulo"] == "Dom Casmurro"
    assert rows[1]["titulo"] == "Vidas Secas"


def test_deteccao_ponto_e_virgula_sem_nenhum_campo_citado():
    """(c) Mesma situação de (a), mas com ';' como separador."""
    text = (
        "titulo;autor;categoria;codigo\r\n"
        "Livro A;Autor A;Literária;ABC-001\r\n"
        "Livro B;Autor B;Literária;ABC-002\r\n"
    )
    assert _detect_csv_delimiter(text) == ";"

    rows = parse_csv_bytes(text.encode("utf-8-sig"))
    assert len(get_csv_columns(rows)) == 4
    assert rows[0]["codigo"] == "ABC-001"


def test_deteccao_com_virgulas_dentro_de_campos_citados():
    """(d) Conteúdo de texto com vírgulas dentro de campos entre aspas não
    pode inflar a contagem de colunas nem confundir a escolha do delimitador."""
    text = (
        "titulo,autor,categoria\r\n"
        '"Memórias, Póstumas de Brás Cubas",Machado de Assis,Literária\r\n'
        '"Outro Livro, com vírgula no meio",Outro Autor,Literária\r\n'
    )
    assert _detect_csv_delimiter(text) == ","

    rows = parse_csv_bytes(text.encode("utf-8-sig"))
    assert len(get_csv_columns(rows)) == 3
    assert rows[0]["titulo"] == "Memórias, Póstumas de Brás Cubas"
    assert rows[1]["titulo"] == "Outro Livro, com vírgula no meio"


def test_deteccao_falha_com_erro_claro_quando_nenhum_candidato_funciona():
    text = "apenasumacoluna\nsemdelimitadornenhum\noutralinha\n"

    with pytest.raises(ValueError):
        _detect_csv_delimiter(text)

    with pytest.raises(ValueError):
        parse_csv_bytes(text.encode("utf-8"))


def test_deteccao_considera_consistencia_ao_longo_de_ate_20_linhas():
    header = "titulo,autor,categoria,codigo"
    data_lines = [f"Livro {i},Autor {i},Literária,COD-{i:03d}" for i in range(25)]
    text = "\r\n".join([header] + data_lines) + "\r\n"

    assert _detect_csv_delimiter(text) == ","
    rows = parse_csv_bytes(text.encode("utf-8-sig"))
    assert len(rows) == 25
    assert len(get_csv_columns(rows)) == 4


def test_replica_bug_real_arquivo_12_colunas_bom_crlf_sem_aspas():
    """Reprodução fiel do bug relatado: 12 colunas, vírgula, UTF-8 com BOM,
    CRLF, sem aspas nas primeiras linhas de dados."""
    columns = [f"col{i}" for i in range(12)]
    header = ",".join(columns)
    row1 = ",".join(f"v{i}a" for i in range(12))
    row2 = ",".join(f"v{i}b" for i in range(12))
    text = f"{header}\r\n{row1}\r\n{row2}\r\n"

    rows = parse_csv_bytes(text.encode("utf-8-sig"))
    assert len(get_csv_columns(rows)) == 12
    assert rows[0]["col0"] == "v0a"
    assert rows[1]["col11"] == "v11b"


# ---------------------------------------------------------------------------
# Integração ponta a ponta: bytes do arquivo -> tela real de importação
# (show_csv_import via AppTest), não só parse_csv_bytes isolado.
# ---------------------------------------------------------------------------

ESPIRITUAL_CSV_COLUMNS = [
    "titulo", "autor", "categoria", "codigo", "status",
    "editora", "ano", "paginas", "idioma", "localizacao", "doador", "obs",
]


def _espiritual_csv_bytes() -> bytes:
    """Bytes com o mesmo perfil do espiritual.csv real do CCE: 12 colunas,
    vírgula, UTF-8 com BOM, CRLF, sem nenhum campo entre aspas."""
    header = ",".join(ESPIRITUAL_CSV_COLUMNS)
    row1 = ",".join(f"{c}-1" for c in ESPIRITUAL_CSV_COLUMNS)
    row2 = ",".join(f"{c}-2" for c in ESPIRITUAL_CSV_COLUMNS)
    text = f"{header}\r\n{row1}\r\n{row2}\r\n"
    return text.encode("utf-8-sig")


def test_integracao_bytes_ate_get_csv_columns_espiritual_csv():
    """Integração da camada de parsing: bytes -> parse_csv_bytes ->
    get_csv_columns, com o perfil exato do arquivo que disparou o bug."""
    data = _espiritual_csv_bytes()
    rows = parse_csv_bytes(data)
    columns = get_csv_columns(rows)
    assert len(columns) == 12
    assert columns == ESPIRITUAL_CSV_COLUMNS
    assert rows[0]["titulo"] == "titulo-1"
    assert rows[1]["obs"] == "obs-2"


def test_integracao_tela_importacao_mostra_12_colunas(tmp_path, monkeypatch):
    """Integração ponta a ponta pela tela real: sobe o app com AppTest, faz
    upload do arquivo (mesmo perfil do espiritual.csv) e confirma que a tela
    de Mapeamento de colunas mostra as 12 colunas — não a lógica isolada."""
    from streamlit.testing.v1 import AppTest

    data = _espiritual_csv_bytes()

    class FakeUpload:
        name = "espiritual.csv"
        size = len(data)

        def getvalue(self):
            return data

    database_url = f"sqlite:///{tmp_path}/integ_import.db"
    monkeypatch.setattr(app.st, "secrets", {"DATABASE_URL": database_url})
    monkeypatch.setattr(app.st, "file_uploader", lambda *a, **k: FakeUpload())

    at = AppTest.from_file("app.py")
    at.run()
    at.text_input(key="login_email").input("admin@biblioteca.org")
    at.text_input(key="login_password").input("admin123")
    at.button(key="FormSubmitter:login_form-Entrar").click().run()
    assert not at.exception, at.exception

    at.radio[0].set_value("Importar CSV").run()
    assert not at.exception, at.exception

    summary_texts = [w.value for w in at.markdown if "coluna(s) encontrada" in (w.value or "")]
    assert summary_texts, "tela não mostrou o resumo de linhas/colunas encontradas"
    assert "**12**" in summary_texts[0], summary_texts[0]

    # cada um dos 5 selectboxes de mapeamento deve oferecer as 12 colunas do
    # arquivo (+ a opção "não mapear"), confirmando que a tela recebeu as
    # 12 colunas de fato, não uma coluna só com a linha inteira dentro
    map_selectboxes = [sb for sb in at.selectbox if sb.label.rstrip(" *") in
                        {"titulo", "autor", "categoria", "codigo", "status"}]
    assert len(map_selectboxes) == 5
    for sb in map_selectboxes:
        assert len(sb.options) == 13  # 12 colunas + "(não mapear / deixar vazio)"

    titulo_sb = next(sb for sb in map_selectboxes if sb.label.startswith("titulo"))
    assert titulo_sb.value == "titulo"  # detectado automaticamente


def test_reenviar_arquivo_com_mesmo_nome_e_conteudo_diferente_reprocessa(tmp_path, monkeypatch):
    """Reproduz o cenário que explicaria 'a correção não teve efeito na
    prática': se o cache de sessão fosse chaveado só pelo nome do arquivo,
    reenviar uma versão diferente do arquivo com o MESMO nome manteria o
    resultado velho (de uma parse anterior bem-sucedida) na tela. A
    assinatura precisa incluir o tamanho também.

    As duas versões precisam parsear com SUCESSO (números de coluna
    diferentes) — se a 1ª falhasse, o cache nunca seria populado e o teste
    passaria mesmo com a chave antiga (só nome), sem provar nada."""
    from streamlit.testing.v1 import AppTest

    # 1ª versão: 2 colunas
    data_v1 = "a,b\r\nvalor1,valor2\r\n".encode("utf-8-sig")
    # 2ª versão, MESMO NOME, conteúdo diferente: 3 colunas
    data_v2 = "titulo,autor,categoria\r\nLivro A,Autor A,Cat\r\n".encode("utf-8-sig")

    state = {"data": data_v1}

    class FakeUpload:
        name = "arquivo.csv"

        @property
        def size(self):
            return len(state["data"])

        def getvalue(self):
            return state["data"]

    database_url = f"sqlite:///{tmp_path}/integ_reupload.db"
    monkeypatch.setattr(app.st, "secrets", {"DATABASE_URL": database_url})
    monkeypatch.setattr(app.st, "file_uploader", lambda *a, **k: FakeUpload())

    at = AppTest.from_file("app.py")
    at.run()
    at.text_input(key="login_email").input("admin@biblioteca.org")
    at.text_input(key="login_password").input("admin123")
    at.button(key="FormSubmitter:login_form-Entrar").click().run()
    at.radio[0].set_value("Importar CSV").run()
    assert not at.exception, at.exception

    summary_texts_v1 = [w.value for w in at.markdown if "coluna(s) encontrada" in (w.value or "")]
    assert summary_texts_v1, "tela não mostrou o resumo na 1ª versão do arquivo"
    assert "**2**" in summary_texts_v1[0], summary_texts_v1[0]

    # "reenvia" uma versão diferente do arquivo, com o MESMO nome
    state["data"] = data_v2
    at.run()
    assert not at.exception, at.exception

    summary_texts_v2 = [w.value for w in at.markdown if "coluna(s) encontrada" in (w.value or "")]
    assert summary_texts_v2, "tela não reprocessou o arquivo reenviado com o mesmo nome"
    assert "**3**" in summary_texts_v2[0], summary_texts_v2[0]


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

def test_init_db_cria_schema_e_admin_de_forma_idempotente(tmp_path):
    database_url = f"sqlite:///{tmp_path}/init.db"
    init_db(database_url)
    init_db(database_url)  # chamado 2x, como acontece a cada rerun do Streamlit

    with get_connection(database_url) as connection:
        admin = get_user_by_email(connection, "admin@biblioteca.org")
        assert admin is not None
        assert admin["role"] == "admin"
        assert verify_password("admin123", admin["password_hash"], admin["salt"])

        user_count = connection.execute(text("SELECT COUNT(*) AS n FROM users")).mappings().first()["n"]
        assert user_count == 1  # não duplicou o admin na 2ª chamada


def test_init_db_nao_insere_livros_de_exemplo(tmp_path):
    """O catálogo começa vazio: os livros vêm da carga real do acervo, e um
    seed automático colidiria com códigos reais do cliente (ASSM-001 etc.)."""
    database_url = f"sqlite:///{tmp_path}/init_sem_seed.db"
    init_db(database_url)

    with get_connection(database_url) as connection:
        book_count = connection.execute(
            text("SELECT COUNT(*) AS n FROM books")
        ).mappings().first()["n"]
        assert book_count == 0


def test_init_db_nao_reinsere_livros_apos_reinicio(tmp_path):
    """Reiniciar o app (init_db de novo, com engine novo) não pode repor
    livros — nem os de exemplo, nem repetir a carga real já existente."""
    database_url = f"sqlite:///{tmp_path}/init_reinicio.db"
    init_db(database_url)

    with get_connection(database_url) as connection:
        add_book(connection, "Livro Real do Acervo", "Machado de Assis", "Literária")
        connection.commit()

    # simula um restart: limpa o cache de engines já inicializados
    app._initialized_engine_ids.clear()
    init_db(database_url)

    with get_connection(database_url) as connection:
        rows = connection.execute(
            text("SELECT code, title FROM books ORDER BY id")
        ).mappings().all()
        assert [r["title"] for r in rows] == ["Livro Real do Acervo"]
        assert rows[0]["code"] == "ASSM-001"  # código real do cliente preservado


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


# ---------------------------------------------------------------------------
# Mapeamento flexível de colunas (importação de exports de outras ferramentas)
# ---------------------------------------------------------------------------

def test_deteccao_automatica_ignora_acentos_e_caixa():
    columns = get_csv_columns(
        parse_csv_bytes("TÍTULO;Autor;Categoria;Situação\nA;B;C;\n".encode("utf-8"))
    )
    mapping, ambiguities = detect_column_mapping(columns)
    assert mapping["titulo"] == "título"
    assert mapping["autor"] == "autor"
    assert mapping["categoria"] == "categoria"
    assert mapping["status"] == "situação"
    assert mapping["codigo"] is None  # nenhuma coluna candidata no arquivo
    assert ambiguities == {}


def test_deteccao_automatica_aceita_sinonimos_em_ingles_e_variantes():
    columns = ["Nome", "Escritor", "Acervo", "Tombo", "status"]
    mapping, ambiguities = detect_column_mapping(columns)
    assert mapping == {
        "titulo": "Nome",
        "autor": "Escritor",
        "categoria": "Acervo",
        "codigo": "Tombo",
        "status": "status",
    }
    assert ambiguities == {}


def test_duas_colunas_candidatas_ao_mesmo_campo_sinalizam_ambiguidade():
    # export real do CCE: 'Código-antigo' E 'Código' no mesmo arquivo
    columns = ["Título", "Autor", "Código-antigo", "Código"]
    mapping, ambiguities = detect_column_mapping(columns)

    # não escolhe sozinho: deixa sem pré-seleção e reporta as candidatas
    assert mapping["codigo"] is None
    assert ambiguities["codigo"] == ["Código-antigo", "Código"]
    # campos não ambíguos seguem detectados normalmente
    assert mapping["titulo"] == "Título"
    assert mapping["autor"] == "Autor"


def test_mapeamento_manual_sobrescreve_a_deteccao_automatica():
    # 'denominacao' não é sinônimo conhecido, então a detecção escolhe 'título'
    rows = [{"título": "Ignorado", "autor": "Autor X", "denominacao": "Título Real"}]
    columns = get_csv_columns(rows)
    detected, _ = detect_column_mapping(columns)
    assert detected["titulo"] == "título"

    # usuário sobrescreve para a coluna 'denominacao'
    manual = dict(detected)
    manual["titulo"] = "denominacao"
    mapped = apply_column_mapping(rows, manual)
    assert mapped[0]["titulo"] == "Título Real"
    assert mapped[0]["autor"] == "Autor X"


def test_colunas_sinonimas_do_mesmo_campo_tambem_geram_ambiguidade():
    # 'título' e 'obra' são ambos sinônimos de titulo -> usuário decide
    columns = ["título", "obra", "autor"]
    mapping, ambiguities = detect_column_mapping(columns)
    assert mapping["titulo"] is None
    assert ambiguities["titulo"] == ["título", "obra"]


def test_campos_nao_mapeados_saem_vazios():
    rows = [{"nome": "Livro A", "escritor": "Autor A", "extra": "irrelevante"}]
    mapping = {"titulo": "nome", "autor": "escritor", "categoria": None, "codigo": None, "status": None}
    mapped = apply_column_mapping(rows, mapping)
    assert mapped[0] == {
        "titulo": "Livro A",
        "autor": "Autor A",
        "categoria": "",
        "codigo": "",
        "status": "",
    }


def test_categoria_fixa_aplicada_a_todas_as_linhas():
    rows = [
        {"titulo": "Livro A", "autor": "Autor A"},
        {"titulo": "Livro B", "autor": "Autor B"},
    ]
    mapping = {"titulo": "titulo", "autor": "autor", "categoria": None, "codigo": None, "status": None}
    mapped = apply_column_mapping(rows, mapping, fixed_category="  Acervo Infantil  ")
    assert [m["categoria"] for m in mapped] == ["Acervo Infantil", "Acervo Infantil"]


def test_categoria_da_coluna_tem_precedencia_sobre_categoria_fixa():
    rows = [{"titulo": "Livro A", "autor": "Autor A", "categoria": "Poesia"}]
    mapping = {
        "titulo": "titulo",
        "autor": "autor",
        "categoria": "categoria",
        "codigo": None,
        "status": None,
    }
    mapped = apply_column_mapping(rows, mapping, fixed_category="Acervo Geral")
    assert mapped[0]["categoria"] == "Poesia"


def test_apply_column_mapping_faz_strip_dos_espacos_sobrando():
    rows = [
        {
            "titulo": "  Dom Casmurro  ",
            "autor": "  Machado de Assis ",
            "categoria": " Romance ",
            "codigo": "  ABC-001 ",
        }
    ]
    mapping = {
        "titulo": "titulo",
        "autor": "autor",
        "categoria": "categoria",
        "codigo": "codigo",
        "status": None,
    }
    mapped = apply_column_mapping(rows, mapping)
    assert mapped[0]["titulo"] == "Dom Casmurro"
    assert mapped[0]["autor"] == "Machado de Assis"
    assert mapped[0]["categoria"] == "Romance"
    assert mapped[0]["codigo"] == "ABC-001"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Disponível", "Disponível"),
        ("disponivel", "Disponível"),
        ("available", "Disponível"),
        ("Emprestado", "Emprestado"),
        ("emprestado", "Emprestado"),
        ("borrowed", "Emprestado"),
        ("on loan", "Emprestado"),
        ("On Loan", "Emprestado"),
        ("Em Manutenção", "Em Manutenção"),
        ("em manutencao", "Em Manutenção"),
        ("manutenção", "Em Manutenção"),
        ("", "Disponível"),
        ("   ", "Disponível"),
        (None, "Disponível"),
    ],
)
def test_normalize_status_aceita_variacoes_conhecidas(raw, expected):
    assert normalize_status(raw) == expected


@pytest.mark.parametrize("raw", ["Perdido", "extraviado", "qualquer coisa", "0"])
def test_normalize_status_retorna_none_para_valor_desconhecido(raw):
    assert normalize_status(raw) is None


def test_status_normalizado_no_preview_e_erro_para_desconhecido(conn):
    rows = [
        {"titulo": "Livro A", "autor": "Autor A", "status": "available"},
        {"titulo": "Livro B", "autor": "Autor B", "status": "on loan"},
        {"titulo": "Livro C", "autor": "Autor C", "status": "em manutencao"},
        {"titulo": "Livro D", "autor": "Autor D", "status": "Extraviado"},
    ]
    processed, summary = process_import_rows(conn, rows)
    assert processed[0]["status"] == "Disponível"
    assert processed[1]["status"] == "Emprestado"
    assert processed[2]["status"] == "Em Manutenção"
    assert any("Status inválido" in e for e in processed[3]["erros"])
    assert summary["com_erro"] == 1


def test_fluxo_completo_export_externo_mapeado_ate_a_gravacao(conn):
    """Export no estilo Memento: cabeçalhos diferentes, espaços sobrando,
    status em inglês e uma coluna de código legado fora de padrão."""
    csv_bytes = (
        "Nome;Escritor;Situação;Tombo\n"
        "  Dom Casmurro  ;  Machado de Assis ;available;  L-0001 \n"
        "Vidas Secas;Graciliano Ramos;on loan;L-0002\n"
    ).encode("utf-8-sig")

    raw_rows = parse_csv_bytes(csv_bytes)
    columns = get_csv_columns(raw_rows)
    mapping, ambiguities = detect_column_mapping(columns)
    assert ambiguities == {}

    mapped = apply_column_mapping(raw_rows, mapping, fixed_category="Acervo CCE")
    processed, summary = process_import_rows(conn, mapped)

    assert summary == {"total": 2, "mantidos": 2, "gerados": 0, "com_erro": 0}
    assert processed[0]["titulo"] == "Dom Casmurro"
    assert processed[0]["autor"] == "Machado de Assis"
    assert processed[0]["categoria"] == "Acervo CCE"
    assert processed[0]["codigo"] == "L-0001"  # código legado mantido como veio
    assert processed[0]["status"] == "Disponível"
    assert processed[1]["status"] == "Emprestado"

    app.commit_import(conn, processed)
    conn.commit()

    saved = conn.execute(
        text("SELECT * FROM books WHERE code = :code"), {"code": "L-0001"}
    ).mappings().first()
    assert saved["title"] == "Dom Casmurro"
    assert saved["author"] == "Machado de Assis"
    assert saved["category"] == "Acervo CCE"
    assert saved["status"] == "Disponível"


# ---------------------------------------------------------------------------
# Estratégia de código por categoria (acervos Literária x Espiritual)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "category,expected",
    [
        ("Espiritual", CODE_STRATEGY_NUMERIC),
        ("espiritual", CODE_STRATEGY_NUMERIC),
        ("  ESPIRITUAL  ", CODE_STRATEGY_NUMERIC),
        ("Literária", CODE_STRATEGY_AUTHOR),
        ("Literaria", CODE_STRATEGY_AUTHOR),
        ("Literatura Brasileira", CODE_STRATEGY_AUTHOR),  # categoria legada
        ("", CODE_STRATEGY_AUTHOR),
        (None, CODE_STRATEGY_AUTHOR),
    ],
)
def test_get_code_strategy_depende_da_categoria(category, expected):
    assert get_code_strategy(category) == expected


def test_add_book_literaria_mantem_codigo_por_autor(conn):
    code = add_book(conn, "Dom Casmurro", "Machado de Assis", "Literária")
    conn.commit()
    assert code == "ASSM-001"

    code2 = add_book(conn, "Memórias Póstumas", "Machado de Assis", "Literária")
    conn.commit()
    assert code2 == "ASSM-002"


def test_add_book_espiritual_gera_numerico_sequencial(conn):
    assert max_numeric_code_for_category(conn, "Espiritual") == 0

    first = add_book(conn, "O Livro dos Espíritos", "Allan Kardec", "Espiritual")
    conn.commit()
    assert first == "1"

    second = add_book(conn, "O Evangelho Segundo o Espiritismo", "Allan Kardec", "Espiritual")
    conn.commit()
    assert second == "2"


def test_numerico_continua_a_partir_do_maior_existente_sem_preencher_buracos(conn):
    # base legada da Espiritual: 461 e 1091, com um buraco enorme no meio
    for code, title in [("461", "Livro 461"), ("1091", "Livro 1091")]:
        conn.execute(
            text(
                "INSERT INTO books (code, title, author, category, status, created_at) "
                "VALUES (:code, :title, 'Autor Legado', 'Espiritual', 'Disponível', '2020-01-01')"
            ),
            {"code": code, "title": title},
        )
    conn.commit()

    assert max_numeric_code_for_category(conn, "Espiritual") == 1091
    # continua de 1091 -> 1092, não tenta reaproveitar 462
    assert add_book(conn, "Novo Espiritual", "Autor Novo", "Espiritual") == "1092"


def test_max_numerico_ignora_codigos_legados_fora_de_padrao_e_outras_categorias(conn):
    rows = [
        ("500", "Espiritual"),
        ("BURE", "Espiritual"),        # legado sem número, não entra na sequência
        ("9999", "Literária"),         # outra categoria, não conta
    ]
    for code, category in rows:
        conn.execute(
            text(
                "INSERT INTO books (code, title, author, category, status, created_at) "
                "VALUES (:code, 'T', 'A', :category, 'Disponível', '2020-01-01')"
            ),
            {"code": code, "category": category},
        )
    conn.commit()

    assert max_numeric_code_for_category(conn, "Espiritual") == 500
    assert add_book(conn, "Próximo", "Autor X", "Espiritual") == "501"


def test_allocator_acumula_numerico_dentro_do_mesmo_lote(conn):
    allocator = BookCodeAllocator(conn)
    assert allocator.resolve_code("Autor A", "Espiritual") == "1"
    assert allocator.resolve_code("Autor B", "Espiritual") == "2"
    assert allocator.resolve_code("Autor C", "Espiritual") == "3"


def test_allocator_nao_reemite_numero_ja_ocupado_por_codigo_explicito_do_lote(conn):
    allocator = BookCodeAllocator(conn)
    # linha traz código numérico explícito bem acima da sequência atual
    assert allocator.resolve_code("Autor A", "Espiritual", "50") == "50"
    # a próxima geração parte dele, em vez de colidir em "1"
    assert allocator.resolve_code("Autor B", "Espiritual") == "51"


def test_importacao_em_lote_mistura_as_duas_estrategias(conn):
    rows = [
        {"titulo": "Lit 1", "autor": "Machado de Assis", "categoria": "Literária"},
        {"titulo": "Esp 1", "autor": "Allan Kardec", "categoria": "Espiritual"},
        {"titulo": "Lit 2", "autor": "Machado de Assis", "categoria": "Literária"},
        {"titulo": "Esp 2", "autor": "Chico Xavier", "categoria": "Espiritual"},
        {"titulo": "Esp 3", "autor": "Allan Kardec", "categoria": "Espiritual"},
    ]
    processed, summary = process_import_rows(conn, rows)

    assert [p["codigo"] for p in processed] == ["ASSM-001", "1", "ASSM-002", "2", "3"]
    assert summary == {"total": 5, "mantidos": 0, "gerados": 5, "com_erro": 0}


def test_importacao_em_lote_considera_acervo_ja_existente_no_banco(conn):
    add_book(conn, "Existente Lit", "Machado de Assis", "Literária")
    add_book(conn, "Existente Esp", "Autor Esp", "Espiritual")
    conn.commit()

    rows = [
        {"titulo": "Nova Lit", "autor": "Machado de Assis", "categoria": "Literária"},
        {"titulo": "Nova Esp", "autor": "Outro Autor", "categoria": "Espiritual"},
    ]
    processed, _ = process_import_rows(conn, rows)
    assert processed[0]["codigo"] == "ASSM-002"  # 1 no banco + 1
    assert processed[1]["codigo"] == "2"         # maior numérico (1) + 1


def test_importacao_preserva_codigos_legados_fora_de_padrao(conn):
    """Os 13 códigos legados do acervo Literária entram como estão — sem
    validação de formato, só de unicidade."""
    legacy = ["BURE", "CUNM", "Bord-001", "GOMLI-001", "MACAL-001", "MILJ-001 (a)", "MILJ-001 (b)"]
    rows = [
        {"titulo": f"Livro {c}", "autor": f"Autor {i}", "categoria": "Literária", "codigo": c}
        for i, c in enumerate(legacy)
    ]
    processed, summary = process_import_rows(conn, rows)

    assert [p["codigo"] for p in processed] == legacy
    assert all(p["codigo_origem"] == "mantido" for p in processed)
    assert summary["com_erro"] == 0

    app.commit_import(conn, processed)
    conn.commit()
    saved = set(conn.execute(text("SELECT code FROM books")).scalars().all())
    assert set(legacy).issubset(saved)


def test_unicidade_continua_bloqueando_duplicatas_nas_duas_estrategias(conn):
    add_book(conn, "Existente", "Autor Esp", "Espiritual")  # gera código "1"
    conn.commit()

    rows = [
        {"titulo": "Dup banco", "autor": "X", "categoria": "Espiritual", "codigo": "1"},
        {"titulo": "Dup lote A", "autor": "Y", "categoria": "Literária", "codigo": "ZZZ-001"},
        {"titulo": "Dup lote B", "autor": "Z", "categoria": "Literária", "codigo": "ZZZ-001"},
    ]
    processed, summary = process_import_rows(conn, rows)

    assert any("Código duplicado" in e for e in processed[0]["erros"])  # contra o banco
    assert processed[1]["erros"] == []
    assert any("Código duplicado" in e for e in processed[2]["erros"])  # dentro do lote
    assert summary["com_erro"] == 2


# ---------------------------------------------------------------------------
# Busca, filtros e paginação no banco (acervo real tem ~2.552 livros)
# ---------------------------------------------------------------------------

def _add_books(conn, entries):
    """entries: lista de (titulo, autor, categoria[, status])."""
    for entry in entries:
        title, author, category = entry[0], entry[1], entry[2]
        add_book(conn, title, author, category)
        if len(entry) > 3:
            conn.execute(
                text("UPDATE books SET status = :s WHERE title = :t"),
                {"s": entry[3], "t": title},
            )
    conn.commit()


@pytest.fixture
def catalogo(conn):
    _add_books(
        conn,
        [
            ("Dom Casmurro", "Machado de Assis", "Literária", "Disponível"),
            ("Memórias Póstumas", "Machado de Assis", "Literária", "Emprestado"),
            ("Reflexões sobre a Vida", "José Álvares", "Espiritual", "Disponível"),
            ("O Livro dos Espíritos", "Allan Kardec", "Espiritual", "Em Manutenção"),
            ("Vidas Secas", "Graciliano Ramos", "Literária", "Disponível"),
        ],
    )
    return conn


def _titles(rows):
    return sorted(r["title"] for r in rows)


# --- busca por cada campo --------------------------------------------------

def test_busca_por_titulo(catalogo):
    assert _titles(list_books(catalogo, query="Casmurro")) == ["Dom Casmurro"]


def test_busca_por_autor(catalogo):
    assert _titles(list_books(catalogo, query="Kardec")) == ["O Livro dos Espíritos"]


def test_busca_por_codigo(catalogo):
    code = catalogo.execute(
        text("SELECT code FROM books WHERE title = 'Vidas Secas'")
    ).scalars().first()
    assert _titles(list_books(catalogo, query=code)) == ["Vidas Secas"]


def test_busca_por_categoria(catalogo):
    assert _titles(list_books(catalogo, query="Espiritual")) == [
        "O Livro dos Espíritos",
        "Reflexões sobre a Vida",
    ]


def test_busca_sem_resultado_retorna_vazio(catalogo):
    assert list_books(catalogo, query="inexistente") == []
    assert count_books(catalogo, query="inexistente") == 0


# --- busca case-insensitive e sem acento -----------------------------------

@pytest.mark.parametrize(
    "term,expected",
    [
        ("reflexoes", "Reflexões sobre a Vida"),   # sem acento -> com acento
        ("REFLEXÕES", "Reflexões sobre a Vida"),   # maiúsculas + acento
        ("Reflexoes", "Reflexões sobre a Vida"),   # caixa mista, sem acento
        ("memorias", "Memórias Póstumas"),
        ("MEMORIAS", "Memórias Póstumas"),
        ("espiritos", "O Livro dos Espíritos"),
        ("casmurro", "Dom Casmurro"),
        ("CASMURRO", "Dom Casmurro"),
    ],
)
def test_busca_case_insensitive_e_sem_acento(catalogo, term, expected):
    assert expected in _titles(list_books(catalogo, query=term))


def test_busca_sem_acento_tambem_no_autor(catalogo):
    # "José Álvares" encontrado digitando sem nenhum acento
    assert _titles(list_books(catalogo, query="jose alvares")) == [
        "Reflexões sobre a Vida"
    ]


# --- filtros ---------------------------------------------------------------

def test_list_book_categories_traz_categorias_distintas_ordenadas(catalogo):
    assert list_book_categories(catalogo) == ["Espiritual", "Literária"]


def test_filtro_por_categoria(catalogo):
    assert count_books(catalogo, category="Literária") == 3
    assert _titles(list_books(catalogo, category="Espiritual")) == [
        "O Livro dos Espíritos",
        "Reflexões sobre a Vida",
    ]


def test_filtro_por_status(catalogo):
    assert count_books(catalogo, status="Disponível") == 3
    assert _titles(list_books(catalogo, status="Emprestado")) == ["Memórias Póstumas"]
    assert _titles(list_books(catalogo, status="Em Manutenção")) == [
        "O Livro dos Espíritos"
    ]


def test_filtros_com_valor_todos_nao_restringem(catalogo):
    total = count_books(catalogo)
    assert total == 5
    assert count_books(catalogo, category=CATEGORY_FILTER_ALL) == total
    assert count_books(catalogo, status=STATUS_FILTER_ALL) == total
    assert count_books(
        catalogo, category=CATEGORY_FILTER_ALL, status=STATUS_FILTER_ALL
    ) == total


# --- combinação de busca + filtros -----------------------------------------

def test_busca_combina_com_filtro_de_categoria(catalogo):
    # "Machado" existe só na Literária; combinada com Espiritual não retorna nada
    assert _titles(list_books(catalogo, query="Machado", category="Literária")) == [
        "Dom Casmurro",
        "Memórias Póstumas",
    ]
    assert list_books(catalogo, query="Machado", category="Espiritual") == []


def test_busca_combina_com_filtro_de_status(catalogo):
    assert _titles(list_books(catalogo, query="Machado", status="Disponível")) == [
        "Dom Casmurro"
    ]
    assert _titles(list_books(catalogo, query="Machado", status="Emprestado")) == [
        "Memórias Póstumas"
    ]


def test_busca_combina_com_os_dois_filtros(catalogo):
    assert count_books(
        catalogo, query="Machado", category="Literária", status="Disponível"
    ) == 1
    assert count_books(
        catalogo, query="Machado", category="Literária", status="Em Manutenção"
    ) == 0


# --- paginação -------------------------------------------------------------

@pytest.fixture
def acervo_grande(conn):
    """60 livros para exercitar múltiplas páginas de 25.

    Insere com código explícito em vez de usar add_book porque autores como
    "Autor 001" gerariam todos o mesmo código (os dígitos não são letras)."""
    for i in range(60):
        conn.execute(
            text(
                "INSERT INTO books (code, title, author, category, status, created_at) "
                "VALUES (:code, :title, :author, 'Literária', 'Disponível', '2026-01-01')"
            ),
            {
                "code": f"PAG-{i:03d}",
                "title": f"Livro {i:03d}",
                "author": f"Autor {i:03d}",
            },
        )
    conn.commit()
    return conn


def test_paginacao_contagem_total_independe_da_pagina(acervo_grande):
    assert count_books(acervo_grande) == 60


def test_paginacao_primeira_pagina(acervo_grande):
    page = list_books(acervo_grande, limit=25, offset=0)
    assert len(page) == 25
    assert page[0]["title"] == "Livro 000"
    assert page[-1]["title"] == "Livro 024"


def test_paginacao_pagina_do_meio(acervo_grande):
    page = list_books(acervo_grande, limit=25, offset=25)
    assert len(page) == 25
    assert page[0]["title"] == "Livro 025"
    assert page[-1]["title"] == "Livro 049"


def test_paginacao_ultima_pagina_parcial(acervo_grande):
    page = list_books(acervo_grande, limit=25, offset=50)
    assert len(page) == 10  # 60 - 50
    assert page[0]["title"] == "Livro 050"
    assert page[-1]["title"] == "Livro 059"


def test_paginacao_offset_alem_do_total_retorna_vazio(acervo_grande):
    assert list_books(acervo_grande, limit=25, offset=100) == []


def test_paginacao_nao_repete_nem_perde_registros(acervo_grande):
    seen = []
    for offset in range(0, 60, 25):
        seen += [r["title"] for r in list_books(acervo_grande, limit=25, offset=offset)]
    assert len(seen) == 60
    assert len(set(seen)) == 60  # sem duplicatas entre páginas


def test_paginacao_respeita_busca_e_filtros(acervo_grande):
    # busca que casa com 10 livros (Livro 010..019)
    total = count_books(acervo_grande, query="Livro 01")
    assert total == 10
    page = list_books(acervo_grande, query="Livro 01", limit=25, offset=0)
    assert len(page) == 10
    assert count_books(acervo_grande, query="Livro 01", status="Emprestado") == 0


def test_ordenacao_newest_first_inverte_a_ordem(acervo_grande):
    por_titulo = list_books(acervo_grande, limit=3, offset=0)
    mais_novos = list_books(acervo_grande, limit=3, offset=0, newest_first=True)
    assert [r["title"] for r in por_titulo] == ["Livro 000", "Livro 001", "Livro 002"]
    assert [r["title"] for r in mais_novos] == ["Livro 059", "Livro 058", "Livro 057"]


def test_apenas_a_pagina_solicitada_e_trazida_do_banco(acervo_grande):
    """O ponto do requisito: filtrar/paginar no SQL, não em Python."""
    assert len(list_books(acervo_grande, limit=25, offset=0)) == 25
    assert len(list_books(acervo_grande, limit=5, offset=0)) == 5
