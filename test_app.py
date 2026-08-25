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

import contextlib
import csv
import io
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text

import app
from app import (
    AUDIT_ACTION_PASSWORD_RESET,
    BookCodeAllocator,
    CATEGORY_FILTER_ALL,
    CODE_STRATEGY_AUTHOR,
    CODE_STRATEGY_NUMERIC,
    MAX_LOGIN_ATTEMPTS,
    MIN_PASSWORD_LENGTH,
    STATUS_FILTER_ALL,
    _clear_login_attempts,
    _detect_csv_delimiter,
    _hash_password_legacy,
    _login_locked_until,
    _register_failed_login,
    _session_is_current,
    add_book,
    admin_reset_password,
    authenticate,
    count_admins,
    count_users,
    generate_temporary_password,
    list_admin_audit,
    list_users,
    try_create_account,
    apply_column_mapping,
    change_password,
    count_books,
    count_loans_for_book,
    count_unreconciled_books,
    create_schema,
    create_user,
    days_overdue,
    default_due_date,
    delete_book,
    detect_column_mapping,
    export_books_csv,
    export_loans_csv,
    generate_book_code,
    get_code_strategy,
    get_dashboard_metrics,
    get_connection,
    get_csv_columns,
    get_engine,
    get_user_by_email,
    get_active_loan_for_book,
    hash_password,
    init_db,
    is_overdue,
    is_valid_email,
    list_active_loans,
    list_book_categories,
    list_books,
    list_borrowers,
    loan_summary_for_books,
    list_unreconciled_books,
    max_numeric_code_for_category,
    normalize_status,
    parse_csv_bytes,
    password_strength_error,
    process_import_rows,
    reconcile_mark_returned,
    reconcile_register_loan,
    request_loan,
    return_loan,
    try_create_reader,
    verify_password,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Credenciais de bootstrap usadas pelos testes. Injetadas via st.secrets em
# cada teste que inicializa um banco — o app não tem mais literal nenhum, e
# a suíte não pode reintroduzir um por dependência implícita.
BOOTSTRAP_EMAIL = "admin.teste@biblioteca.org"
BOOTSTRAP_PASSWORD = "SenhaBootstrap#2026"


def _secrets(**extra) -> dict:
    """Secrets com as credenciais de bootstrap, mais o que o teste precisar."""
    return {
        "BOOTSTRAP_ADMIN_EMAIL": BOOTSTRAP_EMAIL,
        "BOOTSTRAP_ADMIN_PASSWORD": BOOTSTRAP_PASSWORD,
        **extra,
    }


@pytest.fixture
def bootstrap_secrets(monkeypatch):
    """Deixa as credenciais iniciais configuradas para este teste."""
    monkeypatch.setattr(app.st, "secrets", _secrets())
    return BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD


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


# --- codificação: o Excel em português não salva UTF-8 (achado 8) ----------

# Linha com a acentuação que o acervo do CCE tem de verdade — é o que quebrava
# o import inteiro com UnicodeDecodeError.
_LINHA_ACENTUADA = (
    "titulo,autor,categoria\n"
    "Memórias Póstumas de Brás Cubas,Machado de Assis,Literária\n"
    "A Divina Comédia,Dante Alighieri,Espiritual\n"
)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1252", "latin-1"])
def test_parse_csv_bytes_le_acentuacao_em_todas_as_codificacoes(encoding):
    rows = parse_csv_bytes(_LINHA_ACENTUADA.encode(encoding))

    assert rows[0]["titulo"] == "Memórias Póstumas de Brás Cubas"
    assert rows[0]["categoria"] == "Literária"
    assert rows[1]["titulo"] == "A Divina Comédia"


def test_parse_csv_bytes_arquivo_real_do_excel_em_cp1252():
    """Perfil do que o Excel PT-BR no Windows gera: cp1252, ';' e CRLF."""
    conteudo = (
        "Título;Autor;Código-antigo\r\n"
        "Memórias Póstumas;Machado de Assis;461\r\n"
        "Ação e Reação;Chico Xavier;1091\r\n"
    )
    rows = parse_csv_bytes(conteudo.encode("cp1252"))

    assert list(rows[0].keys()) == ["título", "autor", "código-antigo"]
    assert rows[0]["título"] == "Memórias Póstumas"
    assert rows[1]["título"] == "Ação e Reação"
    assert rows[1]["código-antigo"] == "1091"


def test_cp1252_com_aspas_tipograficas_do_word():
    """0x93/0x94 são aspas no cp1252 e indefinidos no latin-1 — se a ordem da
    cascata invertesse, viriam como caractere de controle."""
    dados = b"titulo,autor\n\x93O Corti\xe7o\x94,Alu\xedsio Azevedo\n"
    rows = parse_csv_bytes(dados)
    assert rows[0]["titulo"] == "“O Cortiço”"
    assert rows[0]["autor"] == "Aluísio Azevedo"


def test_arquivo_binario_ou_utf16_falha_com_instrucao_de_salvar_em_utf8():
    """latin-1 decodifica qualquer byte, então sem a checagem de NUL um .xlsx
    ou um CSV em UTF-16 viraria lixo silencioso em vez de erro."""
    utf16 = _LINHA_ACENTUADA.encode("utf-16")
    with pytest.raises(ValueError, match="CSV UTF-8"):
        parse_csv_bytes(utf16)

    xlsx_falso = b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + b"\x00" * 40
    with pytest.raises(ValueError, match="CSV UTF-8"):
        parse_csv_bytes(xlsx_falso)


def test_tela_de_importacao_explica_o_que_fazer_com_arquivo_ilegivel(conn, monkeypatch):
    """A mensagem chega ao usuário pela tela, sem traceback."""

    class FakeUpload:
        name = "acervo.xlsx"
        size = 100

        def getvalue(self):
            return b"PK\x03\x04" + b"\x00" * 40

    tela = _Tela(monkeypatch, session=_SessionStateFake())
    monkeypatch.setattr(app.st, "file_uploader", lambda *a, **k: FakeUpload())

    app.show_csv_import(conn)

    assert "Salvar como" in tela.texto and "CSV UTF-8" in tela.texto


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


def _login_as_admin_and_complete_forced_password_change(at, new_password="NovaSenh@123"):
    """Login com a senha padrão do bootstrap (must_change_password=True) e
    conclui a troca obrigatória, deixando o AppTest pronto para navegar nas
    telas normais do admin."""
    at.text_input(key="login_email").input(BOOTSTRAP_EMAIL)
    at.text_input(key="login_password").input(BOOTSTRAP_PASSWORD)
    at.button(key="FormSubmitter:login_form-Entrar").click().run()
    assert not at.exception, at.exception

    at.text_input(key="cp_current").input(BOOTSTRAP_PASSWORD)
    at.text_input(key="cp_new").input(new_password)
    at.text_input(key="cp_confirm").input(new_password)
    at.button(key="FormSubmitter:change_password_form-Salvar nova senha").click().run()
    assert not at.exception, at.exception


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
    monkeypatch.setattr(app.st, "secrets", _secrets(DATABASE_URL=database_url))
    monkeypatch.setattr(app.st, "file_uploader", lambda *a, **k: FakeUpload())

    at = AppTest.from_file("app.py")
    at.run()
    _login_as_admin_and_complete_forced_password_change(at)

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
    monkeypatch.setattr(app.st, "secrets", _secrets(DATABASE_URL=database_url))
    monkeypatch.setattr(app.st, "file_uploader", lambda *a, **k: FakeUpload())

    at = AppTest.from_file("app.py")
    at.run()
    _login_as_admin_and_complete_forced_password_change(at)
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

def test_init_db_cria_schema_e_admin_de_forma_idempotente(tmp_path, bootstrap_secrets):
    database_url = f"sqlite:///{tmp_path}/init.db"
    init_db(database_url)
    # limpar o cache entre as chamadas garante que a inicializacao roda MESMO
    # de novo (senao o st.cache_resource curto-circuitaria e o teste nao
    # provaria idempotencia nenhuma) — equivale ao restart do container
    app._ensure_initialized.clear()
    init_db(database_url)  # 2a execucao real, como num restart do Streamlit Cloud

    with get_connection(database_url) as connection:
        admin = get_user_by_email(connection, BOOTSTRAP_EMAIL)
        assert admin is not None
        assert admin["role"] == "admin"
        assert verify_password(BOOTSTRAP_PASSWORD, admin["password_hash"], admin["salt"])
        assert admin["password_hash"].startswith("$2")  # bcrypt, não sha256
        assert bool(admin["must_change_password"]) is True  # força troca no 1º login

        user_count = connection.execute(text("SELECT COUNT(*) AS n FROM users")).mappings().first()["n"]
        assert user_count == 1  # não duplicou o admin na 2ª chamada


def test_init_db_recria_admin_quando_ha_leitores_mas_nenhum_admin(tmp_path, bootstrap_secrets):
    """Estado inconsistente (anonimização, migração, SQL manual) pode deixar
    o banco com leitores cadastrados e nenhum admin — sem recriação, o acesso
    fica irrecuperável pela aplicação."""
    database_url = f"sqlite:///{tmp_path}/sem_admin.db"
    init_db(database_url)

    with get_connection(database_url) as connection:
        connection.execute(text("DELETE FROM users WHERE role = 'admin'"))
        create_user(connection, "Leitora", "leitora@teste.org", "", "senha123", "leitor")
        connection.commit()

    app._ensure_initialized.clear()
    init_db(database_url)  # simula o restart que deve recuperar o acesso

    with get_connection(database_url) as connection:
        admin = get_user_by_email(connection, BOOTSTRAP_EMAIL)
        assert admin is not None
        assert admin["role"] == "admin"

        role_counts = connection.execute(
            text("SELECT role, COUNT(*) AS n FROM users GROUP BY role")
        ).mappings().all()
        counts = {row["role"]: row["n"] for row in role_counts}
        assert counts == {"admin": 1, "leitor": 1}


def test_init_db_com_admin_existente_nao_recria_nem_duplica(tmp_path, bootstrap_secrets):
    database_url = f"sqlite:///{tmp_path}/com_admin.db"
    init_db(database_url)

    with get_connection(database_url) as connection:
        connection.execute(
            text("UPDATE users SET must_change_password = 0 WHERE role = 'admin'")
        )
        create_user(connection, "Outro Admin", "outro.admin@teste.org", "", "senha123", "admin")
        connection.commit()

    app._ensure_initialized.clear()
    init_db(database_url)

    with get_connection(database_url) as connection:
        admin_count = connection.execute(
            text("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'")
        ).mappings().first()["n"]
        assert admin_count == 2  # nenhum admin novo foi criado

        default_admin = get_user_by_email(connection, BOOTSTRAP_EMAIL)
        assert bool(default_admin["must_change_password"]) is False  # não foi recriado/sobrescrito


def test_admin_recriado_apos_perda_nasce_exigindo_troca_de_senha(tmp_path, bootstrap_secrets):
    database_url = f"sqlite:///{tmp_path}/recriado_forca_troca.db"
    init_db(database_url)

    with get_connection(database_url) as connection:
        connection.execute(text("DELETE FROM users WHERE role = 'admin'"))
        connection.commit()

    app._ensure_initialized.clear()
    init_db(database_url)

    with get_connection(database_url) as connection:
        admin = get_user_by_email(connection, BOOTSTRAP_EMAIL)
        assert bool(admin["must_change_password"]) is True


def test_init_db_nao_insere_livros_de_exemplo(tmp_path, bootstrap_secrets):
    """O catálogo começa vazio: os livros vêm da carga real do acervo, e um
    seed automático colidiria com códigos reais do cliente (ASSM-001 etc.)."""
    database_url = f"sqlite:///{tmp_path}/init_sem_seed.db"
    init_db(database_url)

    with get_connection(database_url) as connection:
        book_count = connection.execute(
            text("SELECT COUNT(*) AS n FROM books")
        ).mappings().first()["n"]
        assert book_count == 0


def test_init_db_nao_reinsere_livros_apos_reinicio(tmp_path, bootstrap_secrets):
    """Reiniciar o app (init_db de novo, com engine novo) não pode repor
    livros — nem os de exemplo, nem repetir a carga real já existente."""
    database_url = f"sqlite:///{tmp_path}/init_reinicio.db"
    init_db(database_url)

    with get_connection(database_url) as connection:
        add_book(connection, "Livro Real do Acervo", "Machado de Assis", "Literária")
        connection.commit()

    # simula um restart do container: o cache de inicialização nasce vazio
    app._ensure_initialized.clear()
    init_db(database_url)

    with get_connection(database_url) as connection:
        rows = connection.execute(
            text("SELECT code, title FROM books ORDER BY id")
        ).mappings().all()
        assert [r["title"] for r in rows] == ["Livro Real do Acervo"]
        assert rows[0]["code"] == "ASSM-001"  # código real do cliente preservado


# --- índices (achado 11) ---------------------------------------------------

# Colunas usadas em junção, filtro e ordenação. A busca textual fica de fora de
# propósito: _sql_unaccent envolve a coluna em REPLACE aninhados e nenhum índice
# de coluna cobre isso (a parte cara do achado 11, que não compensa neste volume).
INDICES_ESPERADOS = {
    "loans": {"book_id", "user_id", "status", "due_date"},
    "books": {"status", "category", "title", "author"},
}


def _indexed_columns(engine, table: str) -> set[str]:
    from sqlalchemy import inspect as sa_inspect

    colunas = set()
    for index in sa_inspect(engine).get_indexes(table):
        colunas.update(c for c in index["column_names"] if c)
    return colunas


@pytest.mark.parametrize("tabela,colunas", sorted(INDICES_ESPERADOS.items()))
def test_banco_novo_nasce_com_os_indices(tmp_path, tabela, colunas):
    engine = get_engine(f"sqlite:///{tmp_path}/indices_novo.db")
    create_schema(engine)

    assert colunas <= _indexed_columns(engine, tabela)


@pytest.mark.parametrize("tabela,colunas", sorted(INDICES_ESPERADOS.items()))
def test_banco_ja_existente_ganha_os_indices_na_migracao(tmp_path, tabela, colunas):
    """create_all só cria índice junto com a tabela: num banco em uso (o
    Supabase do CCE) a criação precisa ser explícita."""
    database_url = f"sqlite:///{tmp_path}/indices_legado.db"
    engine = get_engine(database_url)

    # banco "antigo": tabelas criadas sem nenhum índice declarado
    metadata_sem_indices = app.MetaData()
    for nome, tabela_orig in app.metadata.tables.items():
        colunas_copia = [c._copy() for c in tabela_orig.columns]
        app.Table(nome, metadata_sem_indices, *colunas_copia)
    metadata_sem_indices.create_all(engine)
    assert not _indexed_columns(engine, tabela) >= colunas

    create_schema(engine)  # a migração roda sobre a tabela que já existe

    assert colunas <= _indexed_columns(engine, tabela)


def test_migracao_de_indices_e_idempotente(tmp_path):
    """Roda a cada restart do container: não pode falhar na segunda vez."""
    engine = get_engine(f"sqlite:///{tmp_path}/indices_idem.db")
    create_schema(engine)
    create_schema(engine)
    create_schema(engine)

    assert INDICES_ESPERADOS["books"] <= _indexed_columns(engine, "books")


def test_indices_do_postgres_usam_concurrently_para_nao_travar_escrita():
    """No acervo real o índice é construído com o app no ar."""
    postgres = app._index_statements(concurrently=True)
    sqlite = app._index_statements(concurrently=False)

    assert postgres and len(postgres) == len(sqlite)
    assert all(s.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS") for s in postgres)
    assert all(s.startswith("CREATE INDEX IF NOT EXISTS") for s in sqlite)


def test_lista_de_indices_vem_do_metadata_sem_segunda_lista_para_divergir():
    declarados = {
        (tabela.name, coluna.name)
        for tabela in app.metadata.tables.values()
        for indice in tabela.indexes
        for coluna in indice.columns
    }
    esperados = {
        (tabela, coluna)
        for tabela, colunas in INDICES_ESPERADOS.items()
        for coluna in colunas
    }
    assert esperados <= declarados


def test_indice_que_falha_nao_impede_o_app_de_subir(tmp_path, monkeypatch):
    """Índice é desempenho, não correção: um erro ao criá-lo não pode derrubar
    a inicialização do banco inteiro."""
    engine = get_engine(f"sqlite:///{tmp_path}/indices_falha.db")
    monkeypatch.setattr(
        app, "_index_statements", lambda concurrently: ["CREATE INDEX isso nao e sql valido"]
    )

    create_schema(engine)  # não levanta

    with get_connection(f"sqlite:///{tmp_path}/indices_falha.db") as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM books")).scalar_one() == 0


# --- credenciais iniciais vindas dos Secrets (achado 1) --------------------

def test_bootstrap_sem_segredos_nao_cria_admin_e_explica_o_que_configurar(
    tmp_path, monkeypatch
):
    """Sem credenciais configuradas o app NÃO inventa uma senha padrão: um app
    sem admin é recuperável, um admin com senha pública não é."""
    monkeypatch.setattr(app.st, "secrets", {})
    database_url = f"sqlite:///{tmp_path}/sem_segredos.db"
    app._ensure_initialized.clear()

    with pytest.raises(app.BootstrapAdminNotConfigured) as exc:
        init_db(database_url)

    mensagem = str(exc.value)
    assert "BOOTSTRAP_ADMIN_EMAIL" in mensagem
    assert "BOOTSTRAP_ADMIN_PASSWORD" in mensagem

    # o schema foi criado, mas nenhuma conta nasceu
    with get_connection(database_url) as connection:
        total = connection.execute(text("SELECT COUNT(*) AS n FROM users")).mappings().first()["n"]
        assert total == 0


@pytest.mark.parametrize(
    "secrets_incompletos",
    [
        {},
        {"BOOTSTRAP_ADMIN_EMAIL": BOOTSTRAP_EMAIL},                # falta a senha
        {"BOOTSTRAP_ADMIN_PASSWORD": BOOTSTRAP_PASSWORD},          # falta o e-mail
        {"BOOTSTRAP_ADMIN_EMAIL": "", "BOOTSTRAP_ADMIN_PASSWORD": BOOTSTRAP_PASSWORD},
        {"BOOTSTRAP_ADMIN_EMAIL": BOOTSTRAP_EMAIL, "BOOTSTRAP_ADMIN_PASSWORD": ""},
        {"BOOTSTRAP_ADMIN_EMAIL": BOOTSTRAP_EMAIL, "BOOTSTRAP_ADMIN_PASSWORD": "curta"},
    ],
)
def test_bootstrap_recusa_configuracao_incompleta_ou_senha_fraca(
    tmp_path, monkeypatch, secrets_incompletos
):
    monkeypatch.setattr(app.st, "secrets", secrets_incompletos)
    app._ensure_initialized.clear()

    with pytest.raises(app.BootstrapAdminNotConfigured):
        init_db(f"sqlite:///{tmp_path}/incompleto.db")


def test_bootstrap_com_segredos_cria_o_admin_com_essas_credenciais(
    tmp_path, bootstrap_secrets
):
    email, senha = bootstrap_secrets
    database_url = f"sqlite:///{tmp_path}/com_segredos.db"
    app._ensure_initialized.clear()
    init_db(database_url)

    with get_connection(database_url) as connection:
        admin = get_user_by_email(connection, email)
        assert admin is not None
        assert admin["role"] == "admin"
        assert verify_password(senha, admin["password_hash"], admin["salt"])
        assert bool(admin["must_change_password"]) is True


def test_bootstrap_usa_o_email_dos_segredos_e_nao_um_padrao(tmp_path, monkeypatch):
    """Nenhum e-mail literal sobrou: quem manda é o segredo."""
    monkeypatch.setattr(
        app.st,
        "secrets",
        {"BOOTSTRAP_ADMIN_EMAIL": "OutroAdmin@CCE.org", "BOOTSTRAP_ADMIN_PASSWORD": "OutraSenha#123"},
    )
    database_url = f"sqlite:///{tmp_path}/email_proprio.db"
    app._ensure_initialized.clear()
    init_db(database_url)

    with get_connection(database_url) as connection:
        assert get_user_by_email(connection, "outroadmin@cce.org") is not None  # normalizado
        assert get_user_by_email(connection, "admin@biblioteca.org") is None


def test_tela_sem_segredos_mostra_instrucao_e_nao_abre_o_login(tmp_path, monkeypatch):
    """Ponta a ponta: o app para na instrução, sem tela de login utilizável."""
    from streamlit.testing.v1 import AppTest

    database_url = f"sqlite:///{tmp_path}/tela_sem_segredos.db"
    monkeypatch.setattr(app.st, "secrets", {"DATABASE_URL": database_url})
    app._ensure_initialized.clear()

    at = AppTest.from_file("app.py")
    at.run()

    assert not at.exception, at.exception
    assert any("BOOTSTRAP_ADMIN_EMAIL" in e.value for e in at.error)
    assert not at.text_input  # nem o formulário de login foi renderizado


def test_get_connection_sem_database_url_levanta_erro_claro(monkeypatch):
    monkeypatch.setattr(app.st, "secrets", {})
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        app.get_engine()


# ---------------------------------------------------------------------------
# Autenticação: troca obrigatória de senha, hashing, rate limiting, cadastro
# ---------------------------------------------------------------------------

def test_login_admin_bootstrap_fica_bloqueado_ate_trocar_a_senha(tmp_path, monkeypatch):
    """Ponta a ponta pela tela real: logar com a senha padrão do bootstrap
    não dá acesso a nenhuma tela do sistema — só à tela de troca de senha —
    até que a troca seja concluída."""
    from streamlit.testing.v1 import AppTest

    database_url = f"sqlite:///{tmp_path}/forca_troca.db"
    monkeypatch.setattr(app.st, "secrets", _secrets(DATABASE_URL=database_url))

    at = AppTest.from_file("app.py")
    at.run()
    at.text_input(key="login_email").input(BOOTSTRAP_EMAIL)
    at.text_input(key="login_password").input(BOOTSTRAP_PASSWORD)
    at.button(key="FormSubmitter:login_form-Entrar").click().run()
    assert not at.exception, at.exception

    # Nada de menu/sidebar com as telas normais: só a tela de troca de senha.
    assert at.title[0].value == "Alterar minha senha"
    assert not at.radio  # sidebar com "Painel"/"Catálogo"/etc. não aparece

    at.text_input(key="cp_current").input(BOOTSTRAP_PASSWORD)
    at.text_input(key="cp_new").input("NovaSenh@123")
    at.text_input(key="cp_confirm").input("NovaSenh@123")
    at.button(key="FormSubmitter:change_password_form-Salvar nova senha").click().run()
    assert not at.exception, at.exception

    assert at.header[0].value == "Painel"  # já dentro do app, tela padrão do admin
    assert at.radio  # menu normal liberado

    with get_connection(database_url) as connection:
        admin = get_user_by_email(connection, BOOTSTRAP_EMAIL)
        assert bool(admin["must_change_password"]) is False
        assert verify_password("NovaSenh@123", admin["password_hash"], admin["salt"])
        assert not verify_password(BOOTSTRAP_PASSWORD, admin["password_hash"], admin["salt"])


def test_migracao_forca_troca_de_senha_para_admin_ja_existente_em_producao(tmp_path, bootstrap_secrets):
    """Banco criado antes deste recurso: a migração precisa marcar
    must_change_password=1 para contas admin já existentes, porque a única
    senha que elas puderam ter é a padrão do bootstrap, que ficava exposta na
    tela de login antiga. Contas leitor não são afetadas."""
    import sqlite3

    db_path = tmp_path / "legado_admin.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT NOT NULL,
          email TEXT NOT NULL UNIQUE, phone TEXT, password_hash TEXT NOT NULL,
          salt TEXT NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE books (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL, author TEXT NOT NULL, category TEXT,
          status TEXT NOT NULL DEFAULT 'Disponível', created_at TEXT NOT NULL);
        CREATE TABLE loans (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL, loan_date TEXT NOT NULL, due_date TEXT,
          return_date TEXT, status TEXT NOT NULL DEFAULT 'ativo');
        INSERT INTO users VALUES
          (1,'Admin','admin@x.org','','h','s','admin','2026-01-01'),
          (2,'Leitora','leitora@x.org','','h','s','leitor','2026-01-01');
        """
    )
    raw.commit()
    raw.close()

    database_url = f"sqlite:///{db_path}"
    engine = get_engine(database_url)
    create_schema(engine)  # dispara a migração

    with get_connection(database_url) as connection:
        admin = get_user_by_email(connection, "admin@x.org")
        leitora = get_user_by_email(connection, "leitora@x.org")
        assert bool(admin["must_change_password"]) is True
        assert bool(leitora["must_change_password"]) is False


def test_change_password_exige_senha_atual_correta(conn):
    create_user(conn, "Leitor", "leitor@teste.org", "", "senhaAntiga1", "leitor")
    conn.commit()
    user = get_user_by_email(conn, "leitor@teste.org")

    assert change_password(conn, user["id"], "senhaErrada", "senhaNova12") is False
    ainda = get_user_by_email(conn, "leitor@teste.org")
    assert verify_password("senhaAntiga1", ainda["password_hash"], ainda["salt"])

    assert change_password(conn, user["id"], "senhaAntiga1", "senhaNova12") is True
    trocado = get_user_by_email(conn, "leitor@teste.org")
    assert verify_password("senhaNova12", trocado["password_hash"], trocado["salt"])
    assert not verify_password("senhaAntiga1", trocado["password_hash"], trocado["salt"])
    assert bool(trocado["must_change_password"]) is False


def test_login_com_hash_legado_sha256_migra_para_bcrypt_automaticamente(conn):
    """Simula uma conta criada antes da migração para bcrypt (hash sha256 +
    salt gravado direto no banco). O primeiro login bem-sucedido deve
    re-hashear a senha para bcrypt de forma transparente."""
    salt = "abc123"
    legacy_hash = _hash_password_legacy("senhaLegada1", salt)
    conn.execute(
        text(
            """INSERT INTO users
               (full_name, email, phone, password_hash, salt, role,
                must_change_password, created_at)
               VALUES ('Legado', 'legado@teste.org', '', :hash, :salt, 'leitor',
                       0, '2026-01-01')"""
        ),
        {"hash": legacy_hash, "salt": salt},
    )
    conn.commit()

    antes = get_user_by_email(conn, "legado@teste.org")
    assert not antes["password_hash"].startswith("$2")

    user = app.authenticate(conn, "legado@teste.org", "senhaLegada1")
    assert user is not None
    assert user["password_hash"].startswith("$2")  # já veio re-hasheado

    depois = get_user_by_email(conn, "legado@teste.org")
    assert depois["password_hash"].startswith("$2")
    assert verify_password("senhaLegada1", depois["password_hash"], depois["salt"])

    # login seguinte continua funcionando, já 100% no formato novo
    assert app.authenticate(conn, "legado@teste.org", "senhaLegada1") is not None


def test_rate_limit_bloqueia_apos_maximo_de_tentativas_e_libera_depois(conn):
    email = "alvo@teste.org"
    now = datetime(2026, 1, 1, 12, 0, 0)

    for i in range(MAX_LOGIN_ATTEMPTS - 1):
        _register_failed_login(conn, email, now=now)
        assert _login_locked_until(conn, email, now=now) is None  # ainda não bateu o teto

    _register_failed_login(conn, email, now=now)  # MAX_LOGIN_ATTEMPTS-ésima falha
    locked_until = _login_locked_until(conn, email, now=now)
    assert locked_until is not None
    assert locked_until > now

    # ainda bloqueado um instante antes do prazo
    assert _login_locked_until(conn, email, now=locked_until - timedelta(seconds=1)) is not None
    # liberado assim que o prazo passa
    assert _login_locked_until(conn, email, now=locked_until + timedelta(seconds=1)) is None


def test_rate_limit_cada_bloqueio_subsequente_e_maior_que_o_anterior(conn):
    email = "reincidente@teste.org"
    now = datetime(2026, 1, 1, 12, 0, 0)

    for _ in range(MAX_LOGIN_ATTEMPTS):
        _register_failed_login(conn, email, now=now)
    primeiro_bloqueio = _login_locked_until(conn, email, now=now)

    _register_failed_login(conn, email, now=primeiro_bloqueio)
    segundo_bloqueio = _login_locked_until(conn, email, now=primeiro_bloqueio)

    assert segundo_bloqueio > primeiro_bloqueio
    assert (segundo_bloqueio - primeiro_bloqueio) > (primeiro_bloqueio - now)


def test_login_bem_sucedido_limpa_o_contador_de_tentativas(conn):
    create_user(conn, "Leitor", "sortudo@teste.org", "", "senhaCerta1", "leitor")
    conn.commit()
    now = datetime(2026, 1, 1, 12, 0, 0)

    _register_failed_login(conn, "sortudo@teste.org", now=now)
    _register_failed_login(conn, "sortudo@teste.org", now=now)
    _clear_login_attempts(conn, "sortudo@teste.org")

    row = conn.execute(
        text("SELECT * FROM login_attempts WHERE email = :e"), {"e": "sortudo@teste.org"}
    ).mappings().first()
    assert row is None


@pytest.mark.parametrize("senha", ["", "a", "1234567", "curtass"])
def test_password_strength_error_rejeita_senha_curta(senha):
    assert password_strength_error(senha) is not None


@pytest.mark.parametrize("senha", ["12345678", "senhaRazoavel123"])
def test_password_strength_error_aceita_senha_com_tamanho_minimo(senha):
    assert password_strength_error(senha) is None


def test_try_create_reader_cadastro_concorrente_mesmo_email_nao_vaza_excecao(conn):
    """Reproduz a corrida entre a checagem de e-mail duplicado e o INSERT:
    dois cadastros para o mesmo e-mail chegando em paralelo. O segundo deve
    falhar com uma mensagem amigável, nunca com uma IntegrityError crua
    subindo até a tela."""
    sucesso1, erro1 = try_create_reader(
        conn, "Primeiro", "duplicado@teste.org", "", "senhaValida1"
    )
    assert sucesso1 is True
    assert erro1 is None

    sucesso2, erro2 = try_create_reader(
        conn, "Segundo", "duplicado@teste.org", "", "outraSenha1"
    )
    assert sucesso2 is False
    assert erro2 == "Já existe um cadastro com esse e-mail."

    count = conn.execute(
        text("SELECT COUNT(*) AS n FROM users WHERE email = 'duplicado@teste.org'")
    ).mappings().first()["n"]
    assert count == 1  # só o primeiro cadastro foi persistido


# --- enumeração de contas: só o canal de tempo é fechado (achado 10) -------
# A tela pública informa explicitamente o e-mail já cadastrado, por decisão de
# produto: quem esqueceu que já tinha conta é o caso comum numa biblioteca
# comunitária, e a mensagem neutra o deixava sem saída. O vazamento por tempo
# no login, que não custa usabilidade, continua fechado.

def _cadastrar_pela_tela_publica(conn, monkeypatch, email, nome="Fulano de Tal"):
    tela = _Tela(monkeypatch, submits={"Cadastrar"}, session=_SessionStateFake())
    monkeypatch.setattr(
        app.st, "tabs", lambda labels: [contextlib.nullcontext()] * len(labels)
    )
    campos = {"Nome completo": nome, "E-mail": email, "Senha": "senha1234"}
    monkeypatch.setattr(app.st, "text_input", lambda label, *a, **k: campos.get(label, ""))
    app.show_auth_screen(conn)
    return tela


def test_cadastro_publico_avisa_que_o_email_ja_tem_cadastro_e_orienta(
    conn, monkeypatch
):
    create_user(conn, "Já Cadastrada", "existente@teste.org", "", "senha1234", "leitor")
    conn.commit()

    tela = _cadastrar_pela_tela_publica(conn, monkeypatch, "existente@teste.org")

    assert "Já existe um cadastro com esse e-mail" in tela.texto
    # a mensagem precisa dizer o que fazer, não só o que deu errado
    assert "Entrar" in tela.texto
    assert "administrador" in tela.texto
    assert tela.sucessos == []

    # e não criou conta duplicada
    total = conn.execute(
        text("SELECT COUNT(*) FROM users WHERE email = 'existente@teste.org'")
    ).scalar_one()
    assert total == 1


def test_cadastro_publico_com_email_novo_cria_a_conta(conn, monkeypatch):
    tela = _cadastrar_pela_tela_publica(conn, monkeypatch, "nova@teste.org")

    assert tela.erros == []
    assert "Cadastro realizado" in " ".join(tela.sucessos)

    nova = get_user_by_email(conn, "nova@teste.org")
    assert nova is not None
    assert nova["role"] == "leitor"


def test_cadastro_duplicado_continua_barrado_pelo_banco(conn):
    """A mensagem neutra é só de tela: o UNIQUE segue valendo."""
    create_user(conn, "Primeira", "unica@teste.org", "", "senha1234", "leitor")
    conn.commit()

    ok, erro = try_create_reader(conn, "Segunda", "unica@teste.org", "", "senha1234")
    assert ok is False
    assert erro is not None

    total = conn.execute(
        text("SELECT COUNT(*) FROM users WHERE email = 'unica@teste.org'")
    ).scalar_one()
    assert total == 1


def test_cadastro_de_admin_autenticado_ainda_avisa_sobre_email_duplicado(conn):
    """Só a tela pública fica neutra: para um admin logado, saber que a conta já
    existe é informação útil, e ele já enxerga a lista de usuários."""
    create_user(conn, "Existente", "dup@teste.org", "", "senha1234", "leitor")
    conn.commit()

    ok, erro = try_create_account(
        conn, "Novo Admin", "dup@teste.org", "", "senha1234", "admin"
    )
    assert ok is False
    assert "Já existe um cadastro" in erro


def test_login_gasta_tempo_comparavel_com_email_inexistente_e_senha_errada(conn):
    """Sem o hash descartável, o e-mail inexistente responde na hora e o
    existente paga o bcrypt — a diferença de tempo diz qual é qual."""
    import time

    create_user(conn, "Alguém", "existe@teste.org", "", "senha1234", "leitor")
    conn.commit()

    def _medir(email):
        amostras = []
        for _ in range(3):
            inicio = time.perf_counter()
            assert authenticate(conn, email, "senha-errada") is None
            amostras.append(time.perf_counter() - inicio)
        return min(amostras)

    inexistente = _medir("naoexiste@teste.org")
    existente = _medir("existe@teste.org")

    # bcrypt domina os dois caminhos; sem a equalização a razão passa de 100x
    assert inexistente > existente / 3, (
        f"e-mail inexistente respondeu rápido demais: {inexistente:.4f}s "
        f"vs {existente:.4f}s para e-mail existente"
    )


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


def test_commit_import_recusa_lote_com_linha_marcada_com_erro(conn):
    """A validação da tela é um retrato do banco na pré-visualização; a
    gravação não pode confiar que ela ainda vale (achado 9)."""
    rows = [
        {"titulo": "Bom", "autor": "Autor A", "categoria": "Literária"},
        {"titulo": "", "autor": "Autor B", "categoria": "Literária"},  # título vazio
    ]
    processed, summary = process_import_rows(conn, rows)
    assert summary["com_erro"] == 1

    with pytest.raises(ValueError, match="erro bloqueante"):
        app.commit_import(conn, processed)
    conn.rollback()

    # nada foi gravado, nem a linha boa: carga pela metade é pior que nenhuma
    assert conn.execute(text("SELECT COUNT(*) FROM books")).scalar_one() == 0


def test_commit_import_aponta_a_linha_e_o_codigo_em_colisao_na_gravacao(conn):
    """Outra pessoa cadastrou o código entre a pré-visualização e o clique."""
    rows = [
        {"titulo": "Livro A", "autor": "Autor A", "categoria": "Literária", "codigo": "XYZ-001"},
        {"titulo": "Livro B", "autor": "Autor B", "categoria": "Literária", "codigo": "XYZ-002"},
    ]
    processed, summary = process_import_rows(conn, rows)
    assert summary["com_erro"] == 0  # na pré-visualização estava tudo certo

    # a corrida: o código da 2ª linha é cadastrado depois da pré-visualização
    conn.execute(
        text(
            "INSERT INTO books (code, title, author, category, status, created_at) "
            "VALUES ('XYZ-002', 'Chegou antes', 'Outro', 'Literária', 'Disponível', '2026-01-01')"
        )
    )
    conn.commit()

    with pytest.raises(ValueError) as exc:
        app.commit_import(conn, processed)
    conn.rollback()

    assert "linha 2" in str(exc.value)
    assert "XYZ-002" in str(exc.value)

    # transação única: nem a linha 1, que era válida, entrou
    assert conn.execute(text("SELECT COUNT(*) FROM books")).scalar_one() == 1


def test_commit_import_grava_o_lote_inteiro_quando_esta_tudo_certo(conn):
    rows = [
        {"titulo": f"Livro {i}", "autor": f"Autor {i}", "categoria": "Literária"}
        for i in range(5)
    ]
    processed, _ = process_import_rows(conn, rows)

    assert app.commit_import(conn, processed) == 5
    conn.commit()
    assert conn.execute(text("SELECT COUNT(*) FROM books")).scalar_one() == 5


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
# Caminhos de escrita da UI falham com st.error, nunca com traceback (achado 3)
# ---------------------------------------------------------------------------
# Sem tratamento, o Streamlit renderiza o traceback completo (SQL, parâmetros,
# caminhos do servidor) no navegador de qualquer visitante.
#
# As funções show_* são chamadas DIRETAMENTE, fora do runtime do Streamlit:
# a função de tela, a conexão e o try/except exercitados são os reais — só a
# camada de widget (qual botão foi clicado, o que foi exibido) é substituída.
# Não dá para fazer isso com AppTest: ele executa app.py num módulo novo a cada
# run, então um monkeypatch em app.<função> não alcança a tela, e não há como
# forçar a falha de dentro.


class _SessionStateFake(dict):
    """session_state não funciona fora do `streamlit run`; este substituto
    cobre o que as telas usam (atributo, get, pop, item)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class _Tela:
    """Captura o que a tela mostraria e decide quais botões foram clicados."""

    def __init__(self, monkeypatch, botoes=(), submits=False, session=None):
        self.erros: list[str] = []
        self.sucessos: list[str] = []
        alvos = set(botoes)
        # st.form/st.expander guardam estado de formulário no ScriptRunContext,
        # que não existe fora do `streamlit run`: em bare mode eles não o
        # restauram e o vazamento derruba os testes de AppTest seguintes com
        # "Forms cannot be nested in other forms". São contêineres de layout,
        # irrelevantes para o try/except que está sob teste.
        monkeypatch.setattr(app.st, "form", lambda *a, **k: contextlib.nullcontext())
        monkeypatch.setattr(app.st, "expander", lambda *a, **k: contextlib.nullcontext())
        monkeypatch.setattr(app.st, "button", lambda *a, **k: k.get("key") in alvos)
        # submits=True submete todo formulário da tela; um conjunto de rótulos
        # submete só aqueles (uma tela com dois formulários, como a de login com
        # a aba de cadastro, precisa distinguir qual foi enviado).
        if isinstance(submits, bool):
            _submeteu = lambda label=None, *a, **k: submits  # noqa: E731
        else:
            rotulos = set(submits)
            _submeteu = lambda label=None, *a, **k: label in rotulos  # noqa: E731
        monkeypatch.setattr(app.st, "form_submit_button", _submeteu)
        monkeypatch.setattr(app.st, "error", lambda m, *a, **k: self.erros.append(str(m)))
        monkeypatch.setattr(app.st, "success", lambda m, *a, **k: self.sucessos.append(str(m)))
        monkeypatch.setattr(app.st, "warning", lambda *a, **k: None)
        monkeypatch.setattr(app.st, "rerun", lambda *a, **k: None)
        monkeypatch.setattr(app.st, "session_state", session or _SessionStateFake())

    @property
    def texto(self) -> str:
        return " ".join(self.erros)


def _falha_com(monkeypatch, nome, excecao):
    """Faz a operação de domínio `nome` falhar, para exercitar o wrapper."""

    def _boom(*args, **kwargs):
        raise excecao

    monkeypatch.setattr(app, nome, _boom)


def _integrity_error():
    """IntegrityError equivalente ao que o UNIQUE(code) produz no Postgres."""
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    return SAIntegrityError(
        "INSERT INTO books (code, ...) VALUES (...)",
        {"code": "ASSM-001"},
        Exception('duplicate key value violates unique constraint "books_code_key"'),
    )


@pytest.fixture
def cenario_emprestimo(conn):
    """Um livro emprestado a um leitor, com as views que as telas esperam."""
    create_user(conn, "Leitor UI", "leitor.ui@teste.org", "", "senha1234", "leitor")
    code = add_book(conn, "Dom Casmurro", "Machado de Assis", "Literária")
    conn.commit()
    book_id = conn.execute(text("SELECT id FROM books WHERE code = :c"), {"c": code}).scalar_one()
    leitor = get_user_by_email(conn, "leitor.ui@teste.org")
    user_view = {
        "id": leitor["id"], "full_name": leitor["full_name"], "email": leitor["email"],
        "role": "leitor", "must_change_password": False, "session_version": 0,
    }
    return conn, book_id, user_view


def test_ponto1_catalogo_emprestimo_que_falha_mostra_erro_e_nao_propaga(
    cenario_emprestimo, monkeypatch
):
    conn, book_id, user_view = cenario_emprestimo
    tela = _Tela(monkeypatch, botoes={f"borrow_{book_id}"})
    _falha_com(monkeypatch, "request_loan", ValueError("Livro indisponível para empréstimo."))

    app.show_catalog(conn, user_view)  # não levanta

    assert "Não foi possível registrar o empréstimo" in tela.texto
    assert _loans_count_for(conn, book_id) == 0
    assert conn.execute(text("SELECT COUNT(*) FROM books")).scalar_one() == 1  # conexão viva


def test_ponto2_cadastro_de_livro_com_codigo_duplicado_mostra_erro(conn, monkeypatch):
    create_user(conn, "Admin", "admin.ui@teste.org", "", "senha1234", "admin")
    conn.commit()
    tela = _Tela(monkeypatch, submits=True)
    monkeypatch.setattr(app.st, "text_input", lambda *a, **k: "Preenchido")
    _falha_com(monkeypatch, "add_book", _integrity_error())

    app.show_book_management(conn)  # acervo vazio: só o formulário de cadastro roda

    assert "Não foi possível cadastrar o livro" in tela.texto
    assert conn.execute(text("SELECT COUNT(*) FROM books")).scalar_one() == 0


def test_ponto3_edicao_de_status_com_emprestimo_ativo_mostra_erro(
    cenario_emprestimo, monkeypatch
):
    """Único dos seis que falha naturalmente, sem injeção: a validação de
    update_book recusa liberar um livro que está com alguém."""
    conn, book_id, user_view = cenario_emprestimo
    request_loan(conn, book_id, user_view["id"])
    conn.commit()

    tela = _Tela(monkeypatch, submits=True)
    monkeypatch.setattr(app.st, "text_input", lambda *a, **k: "Dom Casmurro")
    monkeypatch.setattr(app.st, "selectbox", lambda *a, **k: "Disponível")

    app.show_book_management(conn)

    assert "empréstimo ativo" in tela.texto
    assert _book_status(conn, book_id) == "Emprestado"


def test_ponto4_devolucao_que_falha_em_emprestimos_mostra_erro(
    cenario_emprestimo, monkeypatch
):
    conn, book_id, user_view = cenario_emprestimo
    request_loan(conn, book_id, user_view["id"])
    conn.commit()
    loan_id = get_active_loan_for_book(conn, book_id)["id"]

    tela = _Tela(monkeypatch, botoes={f"return_{loan_id}"})
    monkeypatch.setattr(app.st, "checkbox", lambda *a, **k: False)
    _falha_com(monkeypatch, "return_loan", ValueError("Empréstimo não está ativo."))

    app.show_loan_management(conn)

    assert "Não foi possível registrar a devolução" in tela.texto
    assert get_active_loan_for_book(conn, book_id) is not None  # nada mudou


def test_ponto5_devolucao_que_falha_em_meus_emprestimos_mostra_erro(
    cenario_emprestimo, monkeypatch
):
    conn, book_id, user_view = cenario_emprestimo
    request_loan(conn, book_id, user_view["id"])
    conn.commit()
    loan_id = get_active_loan_for_book(conn, book_id)["id"]

    tela = _Tela(monkeypatch, botoes={f"selfreturn_{loan_id}"})
    _falha_com(monkeypatch, "return_loan", ValueError("Empréstimo não está ativo."))

    app.show_my_loans(conn, user_view)

    assert "Não foi possível registrar a devolução" in tela.texto
    assert get_active_loan_for_book(conn, book_id) is not None


def test_ponto6_importacao_que_falha_na_gravacao_mostra_erro_e_nao_grava_nada(
    conn, monkeypatch
):
    csv_bytes = "titulo,autor,categoria\nDom Casmurro,Machado de Assis,Literária\n".encode("utf-8")

    class FakeUpload:
        name = "acervo.csv"
        size = len(csv_bytes)

        def getvalue(self):
            return csv_bytes

    tela = _Tela(monkeypatch, botoes={None}, session=_SessionStateFake())
    monkeypatch.setattr(app.st, "file_uploader", lambda *a, **k: FakeUpload())
    monkeypatch.setattr(app.st, "text_input", lambda *a, **k: "")
    _falha_com(monkeypatch, "commit_import", _integrity_error())

    app.show_csv_import(conn)

    assert "nenhum livro foi gravado" in tela.texto
    assert conn.execute(text("SELECT COUNT(*) FROM books")).scalar_one() == 0


def test_config_nao_expoe_stacktrace_no_navegador():
    """O padrão do Streamlit é "full" — traceback com SQL para qualquer
    visitante, inclusive anônimo na tela de login."""
    import tomllib

    with open(".streamlit/config.toml", "rb") as arquivo:
        config = tomllib.load(arquivo)

    assert config["client"]["showErrorDetails"] == "none"


def test_botao_de_emprestimo_some_quando_o_livro_deixa_de_estar_disponivel(
    cenario_emprestimo, monkeypatch
):
    """Primeira linha de defesa, antes do try/except: a tela relê o estado a
    cada rerun, então a ação nem chega a ser oferecida."""
    conn, book_id, user_view = cenario_emprestimo
    conn.execute(text("UPDATE books SET status = 'Em Manutenção' WHERE id = :i"), {"i": book_id})
    conn.commit()

    oferecidos = []
    monkeypatch.setattr(app.st, "button", lambda *a, **k: oferecidos.append(k.get("key")) or False)
    monkeypatch.setattr(app.st, "error", lambda *a, **k: None)
    monkeypatch.setattr(app.st, "session_state", _SessionStateFake())

    app.show_catalog(conn, user_view)

    assert f"borrow_{book_id}" not in oferecidos


# ---------------------------------------------------------------------------
# Invariante livro↔empréstimo na edição de status (achado 2)
# ---------------------------------------------------------------------------

@pytest.fixture
def livro_emprestado(conn):
    """Um livro com empréstimo ativo, pronto para tentativas de edição."""
    create_user(conn, "Leitor A", "a@teste.org", "", "senha1234", "leitor")
    code = add_book(conn, "Dom Casmurro", "Machado de Assis", "Literária")
    conn.commit()
    book_id = conn.execute(text("SELECT id FROM books WHERE code = :c"), {"c": code}).scalar_one()
    leitor = get_user_by_email(conn, "a@teste.org")["id"]
    request_loan(conn, book_id, leitor)
    conn.commit()
    return conn, book_id, leitor


@pytest.mark.parametrize("status_proibido", ["Disponível", "Em Manutenção"])
def test_update_book_recusa_liberar_livro_com_emprestimo_ativo(
    livro_emprestado, status_proibido
):
    conn, book_id, _ = livro_emprestado

    with pytest.raises(ValueError, match="empréstimo ativo"):
        app.update_book(conn, book_id, "Dom Casmurro", "Machado de Assis",
                        "Literária", status_proibido)
    conn.rollback()

    assert _book_status(conn, book_id) == "Emprestado"


def test_update_book_permite_editar_os_outros_campos_com_emprestimo_ativo(
    livro_emprestado,
):
    """A trava é só sobre o status: corrigir título/autor continua liberado."""
    conn, book_id, _ = livro_emprestado

    app.update_book(conn, book_id, "Dom Casmurro (rev.)", "Machado de Assis Filho",
                    "Romance", "Emprestado")
    conn.commit()

    livro = conn.execute(
        text("SELECT title, author, category, status FROM books WHERE id = :i"),
        {"i": book_id},
    ).mappings().first()
    assert livro["title"] == "Dom Casmurro (rev.)"
    assert livro["author"] == "Machado de Assis Filho"
    assert livro["status"] == "Emprestado"


def test_update_book_libera_status_depois_da_devolucao(livro_emprestado):
    conn, book_id, _ = livro_emprestado
    return_loan(conn, get_active_loan_for_book(conn, book_id)["id"])
    conn.commit()

    app.update_book(conn, book_id, "Dom Casmurro", "Machado de Assis",
                    "Literária", "Em Manutenção")
    conn.commit()
    assert _book_status(conn, book_id) == "Em Manutenção"


def test_update_book_valida_no_momento_da_escrita_e_nao_com_o_dado_da_tela(conn):
    """A tela carregou o livro como Disponível; outra sessão emprestou antes do
    clique em salvar. A validação precisa ver o estado NOVO."""
    create_user(conn, "Leitor", "leitor@teste.org", "", "senha1234", "leitor")
    code = add_book(conn, "Livro", "Autor X", "Literária")
    conn.commit()
    book_id = conn.execute(text("SELECT id FROM books WHERE code = :c"), {"c": code}).scalar_one()

    carregado_na_tela = conn.execute(
        text("SELECT status FROM books WHERE id = :i"), {"i": book_id}
    ).scalar_one()
    assert carregado_na_tela == "Disponível"  # o que o formulário exibiu

    request_loan(conn, book_id, get_user_by_email(conn, "leitor@teste.org")["id"])
    conn.commit()

    with pytest.raises(ValueError, match="empréstimo ativo"):
        app.update_book(conn, book_id, "Livro", "Autor X", "Literária", carregado_na_tela)
    conn.rollback()


def test_update_book_de_livro_removido_avisa(conn):
    with pytest.raises(ValueError, match="não encontrado"):
        app.update_book(conn, 99999, "T", "A", "C", "Disponível")
    conn.rollback()


def test_um_segundo_emprestimo_do_mesmo_exemplar_fica_impossivel(livro_emprestado):
    """O cenário completo do achado 2: sem o UPDATE manual liberando o livro,
    o segundo leitor não consegue pegar o mesmo exemplar."""
    conn, book_id, _ = livro_emprestado
    create_user(conn, "Leitor B", "b@teste.org", "", "senha1234", "leitor")
    conn.commit()
    leitor_b = get_user_by_email(conn, "b@teste.org")["id"]

    with pytest.raises(ValueError, match="empréstimo ativo"):
        app.update_book(conn, book_id, "Dom Casmurro", "Machado de Assis",
                        "Literária", "Disponível")
    conn.rollback()

    with pytest.raises(ValueError, match="indisponível"):
        request_loan(conn, book_id, leitor_b)
    conn.rollback()

    ativos = conn.execute(
        text("SELECT COUNT(*) FROM loans WHERE book_id = :b AND status = 'ativo'"),
        {"b": book_id},
    ).scalar_one()
    assert ativos == 1


# --- detecção do sentido inverso -------------------------------------------

def test_deteccao_do_sentido_inverso_livro_liberado_com_emprestimo_ativo(
    livro_emprestado,
):
    """Estado que só SQL manual produz depois das travas — precisa ser visto."""
    conn, book_id, _ = livro_emprestado
    assert app.count_books_loaned_but_available(conn) == 0

    # simula a intervenção direta no banco que a aplicação não permite mais
    conn.execute(text("UPDATE books SET status = 'Disponível' WHERE id = :i"), {"i": book_id})
    conn.commit()

    assert app.count_books_loaned_but_available(conn) == 1
    detectados = app.list_books_loaned_but_available(conn)
    assert len(detectados) == 1
    assert detectados[0]["title"] == "Dom Casmurro"
    assert detectados[0]["status"] == "Disponível"
    assert detectados[0]["full_name"] == "Leitor A"  # quem consta com o livro


def test_sentido_inverso_tambem_pega_em_manutencao(livro_emprestado):
    conn, book_id, _ = livro_emprestado
    conn.execute(text("UPDATE books SET status = 'Em Manutenção' WHERE id = :i"), {"i": book_id})
    conn.commit()
    assert app.count_books_loaned_but_available(conn) == 1


def test_os_dois_sentidos_da_inconsistencia_sao_contados_separadamente(conn):
    """Reconciliação normal e sentido inverso não se confundem: cada um tem a
    sua contagem, porque a correção de cada um é diferente."""
    create_user(conn, "Leitor", "leitor@teste.org", "", "senha1234", "leitor")
    pendente = add_book(conn, "Sem registro", "Autor A", "Literária")
    invertido = add_book(conn, "Com registro", "Autor B", "Literária")
    conn.commit()

    # sentido A: Emprestado sem loan ativo
    conn.execute(text("UPDATE books SET status = 'Emprestado' WHERE code = :c"), {"c": pendente})
    # sentido B: loan ativo com status liberado
    invertido_id = conn.execute(
        text("SELECT id FROM books WHERE code = :c"), {"c": invertido}
    ).scalar_one()
    request_loan(conn, invertido_id, get_user_by_email(conn, "leitor@teste.org")["id"])
    conn.execute(text("UPDATE books SET status = 'Disponível' WHERE id = :i"), {"i": invertido_id})
    conn.commit()

    assert count_unreconciled_books(conn) == 1
    assert app.count_books_loaned_but_available(conn) == 1

    # a lista acionável da Reconciliação não mistura os dois
    pendentes = list_unreconciled_books(conn)
    assert [p["title"] for p in pendentes] == ["Sem registro"]


def test_base_consistente_nao_reporta_nenhum_dos_dois_sentidos(livro_emprestado):
    conn, _, _ = livro_emprestado
    assert count_unreconciled_books(conn) == 0
    assert app.count_books_loaned_but_available(conn) == 0


# ---------------------------------------------------------------------------
# Concorrência em empréstimo, devolução e exclusão (achado 5)
# ---------------------------------------------------------------------------
# Mesmo padrão de duas conexões simultâneas já usado na Reconciliação: o
# segundo a confirmar age sobre um estado obsoleto e precisa falhar, em vez
# de duplicar o empréstimo do mesmo exemplar físico.

def _book_status(conn, book_id) -> str:
    return conn.execute(
        text("SELECT status FROM books WHERE id = :i"), {"i": book_id}
    ).scalar_one()


@pytest.fixture
def livro_disputado(tmp_path):
    """Um livro Disponível e dois leitores, com conexões independentes."""
    database_url = f"sqlite:///{tmp_path}/disputa.db"
    engine = get_engine(database_url)
    create_schema(engine)

    setup = get_connection(database_url)
    create_user(setup, "Leitor A", "a@teste.org", "", "senha1234", "leitor")
    create_user(setup, "Leitor B", "b@teste.org", "", "senha1234", "leitor")
    code = add_book(setup, "Livro Disputado", "Ana Silva", "Literária")
    setup.commit()
    book_id = setup.execute(text("SELECT id FROM books WHERE code = :c"), {"c": code}).scalar_one()
    leitor_a = get_user_by_email(setup, "a@teste.org")["id"]
    leitor_b = get_user_by_email(setup, "b@teste.org")["id"]
    setup.close()

    sessao_a = get_connection(database_url)
    sessao_b = get_connection(database_url)
    yield sessao_a, sessao_b, book_id, leitor_a, leitor_b
    sessao_a.close()
    sessao_b.close()


def test_dois_leitores_emprestando_o_mesmo_livro_so_um_vence(livro_disputado):
    sessao_a, sessao_b, book_id, leitor_a, leitor_b = livro_disputado

    # os dois carregaram o catálogo e veem o livro como Disponível
    for sessao in (sessao_a, sessao_b):
        assert count_books(sessao, status="Disponível") == 1

    request_loan(sessao_a, book_id, leitor_a)
    sessao_a.commit()

    with pytest.raises(ValueError, match="indisponível"):
        request_loan(sessao_b, book_id, leitor_b)
    sessao_b.rollback()

    ativos = sessao_a.execute(
        text("SELECT COUNT(*) FROM loans WHERE book_id = :b AND status = 'ativo'"),
        {"b": book_id},
    ).scalar_one()
    assert ativos == 1
    assert _book_status(sessao_a, book_id) == "Emprestado"


def test_duas_devolucoes_simultaneas_do_mesmo_emprestimo(livro_disputado):
    sessao_a, sessao_b, book_id, leitor_a, _ = livro_disputado
    request_loan(sessao_a, book_id, leitor_a)
    sessao_a.commit()
    loan_id = get_active_loan_for_book(sessao_a, book_id)["id"]

    return_loan(sessao_a, loan_id)
    sessao_a.commit()

    with pytest.raises(ValueError, match="não está ativo"):
        return_loan(sessao_b, loan_id)
    sessao_b.rollback()

    devolvidos = sessao_a.execute(
        text("SELECT COUNT(*) FROM loans WHERE id = :i AND status = 'devolvido'"),
        {"i": loan_id},
    ).scalar_one()
    assert devolvidos == 1


def test_emprestimo_criado_apos_a_checagem_impede_a_exclusao_do_livro(livro_disputado):
    """A exclusão não pode levar junto um empréstimo ativo que apareceu no
    meio do caminho: a FK barra a remoção em vez de o histórico sumir."""
    sessao_a, sessao_b, book_id, leitor_a, _ = livro_disputado

    # histórico já devolvido, que a exclusão normalmente levaria junto
    request_loan(sessao_a, book_id, leitor_a)
    sessao_a.commit()
    return_loan(sessao_a, get_active_loan_for_book(sessao_a, book_id)["id"])
    sessao_a.commit()

    # outra sessão empresta de novo antes da exclusão ser confirmada
    request_loan(sessao_b, book_id, leitor_a)
    sessao_b.commit()

    with pytest.raises(ValueError, match="emprestado no momento"):
        delete_book(sessao_a, book_id)
    sessao_a.rollback()

    assert _books_count(sessao_a, book_id) == 1
    assert _loans_count_for(sessao_a, book_id) == 2  # nenhum registro foi perdido


def test_delete_book_de_livro_ja_removido_avisa_em_vez_de_seguir(livro_disputado):
    sessao_a, sessao_b, book_id, _, _ = livro_disputado
    delete_book(sessao_a, book_id)
    sessao_a.commit()

    with pytest.raises(ValueError, match="não encontrado"):
        delete_book(sessao_b, book_id)
    sessao_b.rollback()


def test_request_loan_recusa_livro_em_manutencao(conn):
    code = add_book(conn, "Em conserto", "Autor X", "Literária")
    create_user(conn, "Leitor", "leitor@teste.org", "", "senha1234", "leitor")
    conn.commit()
    book_id = conn.execute(text("SELECT id FROM books WHERE code = :c"), {"c": code}).scalar_one()
    conn.execute(text("UPDATE books SET status = 'Em Manutenção' WHERE id = :i"), {"i": book_id})
    conn.commit()
    leitor = get_user_by_email(conn, "leitor@teste.org")["id"]

    with pytest.raises(ValueError, match="indisponível"):
        request_loan(conn, book_id, leitor)
    conn.rollback()

    assert _loans_count_for(conn, book_id) == 0
    assert _book_status(conn, book_id) == "Em Manutenção"


def test_request_loan_em_livro_inexistente_nao_cria_loan_orfao(conn):
    create_user(conn, "Leitor", "leitor@teste.org", "", "senha1234", "leitor")
    conn.commit()
    leitor = get_user_by_email(conn, "leitor@teste.org")["id"]

    with pytest.raises(ValueError, match="indisponível"):
        request_loan(conn, 99999, leitor)
    conn.rollback()

    assert conn.execute(text("SELECT COUNT(*) FROM loans")).scalar_one() == 0


# ---------------------------------------------------------------------------
# Sequencial por PREFIXO (MAX), não por contagem de livros do autor
# ---------------------------------------------------------------------------
# Regressão do achado 4 da revisão: derivar o sequencial de COUNT(livros do
# autor) colide com códigos já existentes em dois cenários que o acervo real
# do CCE exibe — grafias diferentes do mesmo autor e buracos na numeração
# legada. Medido contra o acervo real: 60 dos 744 autores gerariam hoje um
# código duplicado pela regra antiga.

def _insert_legacy_book(conn, code, author, category="Literária"):
    """Grava um livro com código já definido, como veio da carga do acervo."""
    conn.execute(
        text(
            "INSERT INTO books (code, title, author, category, status, created_at) "
            "VALUES (:code, :title, :author, :category, 'Disponível', '2020-01-01')"
        ),
        {"code": code, "title": f"Livro {code}", "author": author, "category": category},
    )
    conn.commit()


def test_prefixo_converge_grafias_diferentes_do_mesmo_autor():
    """A causa raiz nº 1: strings de autor distintas, mesmo prefixo."""
    assert app.book_code_prefix("G. K. Chesterton") == "CHEG"
    assert app.book_code_prefix("Gilbert Keith Chesterton") == "CHEG"

    for grafia in [
        "Fiódor Dostoiévski",
        "Fiodor Dostoievski",
        "Fyodor Dostoievsky",
    ]:
        assert app.book_code_prefix(grafia).startswith("DOS")


def test_grafias_diferentes_do_mesmo_autor_geram_sequenciais_consecutivos(conn):
    """Antes: as duas grafias contavam separado e as duas geravam CHEG-001."""
    primeiro = add_book(conn, "Ortodoxia", "G. K. Chesterton", "Literária")
    conn.commit()
    segundo = add_book(conn, "O Homem Eterno", "Gilbert Keith Chesterton", "Literária")
    conn.commit()

    assert primeiro == "CHEG-001"
    assert segundo == "CHEG-002"  # não CHEG-001 de novo
    assert primeiro != segundo


def test_grafias_diferentes_no_mesmo_lote_de_importacao_nao_colidem(conn):
    rows = [
        {"titulo": "Ortodoxia", "autor": "G. K. Chesterton", "categoria": "Literária"},
        {"titulo": "O Homem Eterno", "autor": "Gilbert Keith Chesterton", "categoria": "Literária"},
        {"titulo": "Hereges", "autor": "Chesterton", "categoria": "Literária"},
    ]
    processed, summary = process_import_rows(conn, rows)

    # "Chesterton" sozinho vira CHEC (o token é primeiro nome e sobrenome),
    # então é uma sequência própria — não colide com as duas grafias completas
    codigos = [p["codigo"] for p in processed]
    assert codigos == ["CHEG-001", "CHEG-002", "CHEC-001"]
    assert len(set(codigos)) == len(codigos)
    assert summary["com_erro"] == 0


def test_sequencial_parte_do_maior_existente_mesmo_com_buraco_na_numeracao(conn):
    """A causa raiz nº 2: Agatha Christie tem 43 livros e sequencial até 44.
    Pela contagem o próximo seria CHRA-044, que já existe."""
    for numero in [1, 2, 44]:  # buraco entre 2 e 44
        _insert_legacy_book(conn, f"CHRA-{numero:03d}", "Agatha Christie")

    assert count_books(conn) == 3  # contagem (3) muito abaixo do máximo (44)
    assert add_book(conn, "Assassinato no Expresso", "Agatha Christie", "Literária") == "CHRA-045"


def test_codigo_gerado_apos_exclusao_de_livro_do_meio_nao_reemite(conn):
    """Repro do achado 4: excluir uma duplicata do acervo derrubava a
    contagem e o próximo cadastro colidia com um código existente."""
    for _ in range(3):
        add_book(conn, "Obra", "Machado de Assis", "Literária")
    conn.commit()

    do_meio = conn.execute(
        text("SELECT id FROM books WHERE code = 'ASSM-002'")
    ).scalar_one()
    delete_book(conn, do_meio)
    conn.commit()

    # a contagem caiu para 2; a regra antiga geraria ASSM-003, que já existe
    assert add_book(conn, "Nova Obra", "Machado de Assis", "Literária") == "ASSM-004"
    conn.commit()

    codigos = conn.execute(text("SELECT code FROM books ORDER BY code")).scalars().all()
    assert codigos == ["ASSM-001", "ASSM-003", "ASSM-004"]
    assert len(set(codigos)) == len(codigos)


def test_cadastro_apos_exclusao_nao_levanta_integrity_error(conn):
    """A colisão se manifestava como IntegrityError cru na tela de cadastro."""
    for _ in range(3):
        add_book(conn, "Obra", "Clarice Lispector", "Literária")
    conn.commit()
    do_meio = conn.execute(text("SELECT id FROM books WHERE code = 'LISC-002'")).scalar_one()
    delete_book(conn, do_meio)
    conn.commit()

    add_book(conn, "Nova", "Clarice Lispector", "Literária")
    conn.commit()  # não levanta IntegrityError


@pytest.mark.parametrize(
    "codigo_legado",
    ["BURE", "CUNM", "Bord-001", "GOMLI-001", "MILJ-001 (a)", "MILJ-001 (b)"],
)
def test_codigos_legados_fora_de_padrao_nao_entram_no_maximo(conn, codigo_legado):
    """Só PREFIXO-NNN participa da sequência; o resto é preservado mas ignorado."""
    _insert_legacy_book(conn, codigo_legado, "Autor Legado")
    assert app.max_sequence_by_prefix(conn) == {}


def test_max_sequence_by_prefix_agrupa_por_prefixo_e_ignora_fora_de_padrao(conn):
    for code in ["ASSM-001", "ASSM-007", "LISC-003", "BURE", "Bord-001",
                 "GOMLI-001", "MILJ-001 (a)", "461"]:
        _insert_legacy_book(conn, code, f"Autor {code}")

    assert app.max_sequence_by_prefix(conn) == {"ASSM": 7, "LISC": 3}


def test_legado_fora_de_padrao_nao_bloqueia_o_primeiro_codigo_do_prefixo(conn):
    """'Bord-001' e 'GOMLI-001' se PARECEM com códigos de "Daniel Borba"
    (BORD) e "Lima Gomes" (GOML), mas não são — caixa e tamanho diferentes.
    Não podem empurrar essas sequências para 002."""
    _insert_legacy_book(conn, "Bord-001", "Autor Legado Um")
    _insert_legacy_book(conn, "GOMLI-001", "Autor Legado Dois")

    assert add_book(conn, "Ficções", "Daniel Borba", "Literária") == "BORD-001"
    assert add_book(conn, "Sertão", "Lima Gomes", "Literária") == "GOML-001"


def test_sequencial_de_4_digitos_e_lido_de_volta_como_maximo(conn):
    """generate_book_code não trunca acima de 999; o máximo precisa acompanhar."""
    _insert_legacy_book(conn, "NETJ-1000", "João Mellão Neto")
    assert app.max_sequence_by_prefix(conn) == {"NETJ": 1000}
    assert add_book(conn, "Novo", "João Mellão Neto", "Literária") == "NETJ-1001"


def test_lote_acumula_sobre_o_maximo_do_banco_com_buraco(conn):
    """Acúmulo dentro do lote partindo do MÁXIMO, não da contagem."""
    for numero in [1, 9]:
        _insert_legacy_book(conn, f"ASSM-{numero:03d}", "Machado de Assis")

    rows = [
        {"titulo": f"Novo {i}", "autor": "Machado de Assis", "categoria": "Literária"}
        for i in range(3)
    ]
    processed, summary = process_import_rows(conn, rows)

    assert [p["codigo"] for p in processed] == ["ASSM-010", "ASSM-011", "ASSM-012"]
    assert summary["com_erro"] == 0


def test_codigo_explicito_do_lote_ocupa_o_sequencial_do_prefixo(conn):
    """Simétrico ao que a estratégia numérica já fazia: um PREFIXO-NNN
    explícito não pode ser reemitido pela geração seguinte."""
    allocator = BookCodeAllocator(conn)
    assert allocator.resolve_code("Machado de Assis", "Literária", "ASSM-050") == "ASSM-050"
    assert allocator.resolve_code("Machado de Assis", "Literária") == "ASSM-051"


def test_codigo_explicito_fora_de_padrao_nao_consome_sequencial(conn):
    """'BURE' num livro de Machado não gasta um número da sequência ASSM."""
    allocator = BookCodeAllocator(conn)
    assert allocator.resolve_code("Machado de Assis", "Literária", "BURE") == "BURE"
    assert allocator.resolve_code("Machado de Assis", "Literária") == "ASSM-001"


def test_acervo_real_sem_colisao_entre_grafias_e_buracos(conn):
    """Cenário combinado: grafias múltiplas + numeração com buraco + legados,
    no mesmo lote. Nenhum código repetido, e a gravação passa no UNIQUE."""
    for code in ["DOSF-001", "DOSF-002", "DOSF-017", "BURE", "Bord-001"]:
        _insert_legacy_book(conn, code, "Fiódor Dostoiévski")

    rows = [
        {"titulo": "Crime e Castigo", "autor": "Fiódor Dostoiévski", "categoria": "Literária"},
        {"titulo": "O Idiota", "autor": "Fiodor Dostoievski", "categoria": "Literária"},
        {"titulo": "Os Irmãos", "autor": "Fyodor Dostoievsky", "categoria": "Literária"},
    ]
    processed, summary = process_import_rows(conn, rows)

    codigos = [p["codigo"] for p in processed]
    assert codigos == ["DOSF-018", "DOSF-019", "DOSF-020"]
    assert summary["com_erro"] == 0

    app.commit_import(conn, processed)
    conn.commit()  # UNIQUE(code) não é violado

    todos = conn.execute(text("SELECT code FROM books")).scalars().all()
    assert len(todos) == len(set(todos))


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


# ---------------------------------------------------------------------------
# Prazo de devolução e controle de atraso
# ---------------------------------------------------------------------------

HOJE = date(2026, 6, 15)


def test_prazo_padrao_e_de_14_dias():
    assert app.PRAZO_PADRAO_DIAS == 14


def test_default_due_date_soma_o_prazo_padrao_a_data_do_emprestimo():
    assert default_due_date(date(2026, 6, 1)) == date(2026, 6, 15)
    # aceita string ISO e datetime, como vem do banco
    assert default_due_date("2026-06-01") == date(2026, 6, 15)
    assert default_due_date("2026-06-01T10:30:00") == date(2026, 6, 15)


def test_default_due_date_sem_argumento_parte_de_hoje():
    assert default_due_date() == date.today() + timedelta(days=app.PRAZO_PADRAO_DIAS)


def test_request_loan_aplica_prazo_padrao(conn):
    create_user(conn, "Leitor Prazo", "prazo@teste.org", "", "senha123", "leitor")
    conn.commit()
    leitor = get_user_by_email(conn, "prazo@teste.org")
    code = add_book(conn, "Livro Prazo", "Autor Prazo", "Literária")
    conn.commit()
    book = conn.execute(
        text("SELECT * FROM books WHERE code = :c"), {"c": code}
    ).mappings().first()

    request_loan(conn, book["id"], leitor["id"])
    conn.commit()

    loan = conn.execute(
        text("SELECT loan_date, due_date FROM loans WHERE book_id = :b"),
        {"b": book["id"]},
    ).mappings().first()
    esperado = date.fromisoformat(loan["loan_date"][:10]) + timedelta(
        days=app.PRAZO_PADRAO_DIAS
    )
    assert loan["due_date"] == esperado.isoformat()


def test_request_loan_aceita_prazo_ajustado_manualmente(conn):
    create_user(conn, "Leitor Ajuste", "ajuste@teste.org", "", "senha123", "leitor")
    conn.commit()
    leitor = get_user_by_email(conn, "ajuste@teste.org")
    code = add_book(conn, "Livro Ajuste", "Autor Ajuste", "Literária")
    conn.commit()
    book = conn.execute(
        text("SELECT * FROM books WHERE code = :c"), {"c": code}
    ).mappings().first()

    request_loan(conn, book["id"], leitor["id"], due_date=date(2026, 12, 31))
    conn.commit()

    due = conn.execute(
        text("SELECT due_date FROM loans WHERE book_id = :b"), {"b": book["id"]}
    ).scalars().first()
    assert due == "2026-12-31"  # prazo ajustado vence o padrão


# --- days_overdue / is_overdue --------------------------------------------

@pytest.mark.parametrize(
    "due,esperado",
    [
        ("2026-06-10", 5),   # venceu há 5 dias
        ("2026-06-14", 1),   # venceu ontem
        ("2026-06-15", 0),   # vence exatamente hoje -> ainda não é atraso
        ("2026-06-16", 0),   # vence amanhã
        ("2026-07-01", 0),   # bem no futuro
        (None, 0),           # sem prazo
        ("", 0),
    ],
)
def test_days_overdue(due, esperado):
    assert days_overdue(due, reference_date=HOJE) == esperado


def test_is_overdue_vencido_e_ativo(conn):
    assert is_overdue("2026-06-10", "ativo", HOJE) is True


def test_is_overdue_vencendo_exatamente_hoje_nao_e_atraso():
    assert is_overdue("2026-06-15", "ativo", HOJE) is False
    assert days_overdue("2026-06-15", HOJE) == 0


def test_is_overdue_prazo_futuro_nao_e_atraso():
    assert is_overdue("2026-06-20", "ativo", HOJE) is False


def test_is_overdue_due_date_nulo_nunca_e_atraso():
    assert is_overdue(None, "ativo", HOJE) is False
    assert is_overdue("", "ativo", HOJE) is False


def test_is_overdue_devolvido_nao_e_atraso_mesmo_vencido():
    assert is_overdue("2026-01-01", "devolvido", HOJE) is False


# --- filtro de atrasados ---------------------------------------------------

@pytest.fixture
def emprestimos_variados(conn):
    """4 empréstimos ativos: 2 atrasados, 1 no prazo, 1 sem prazo."""
    create_user(conn, "Leitor A", "a@teste.org", "119", "senha123", "leitor")
    conn.commit()
    leitor = get_user_by_email(conn, "a@teste.org")

    # autores alfabéticos e distintos: nomes terminados em dígito gerariam
    # todos o mesmo código (os dígitos não entram na regra) e colidiriam
    cenarios = [
        ("Atrasado 1", "Ana Silva", "2026-06-01"),    # 14 dias de atraso
        ("Atrasado 2", "Bruno Costa", "2026-06-14"),  # 1 dia de atraso
        ("No prazo", "Carla Dias", "2026-06-20"),
        ("Sem prazo", "Diego Souza", None),
    ]
    for titulo, autor, due in cenarios:
        code = add_book(conn, titulo, autor, "Literária")
        conn.commit()
        book = conn.execute(
            text("SELECT * FROM books WHERE code = :c"), {"c": code}
        ).mappings().first()
        request_loan(conn, book["id"], leitor["id"])
        conn.execute(
            text("UPDATE loans SET due_date = :d WHERE book_id = :b"),
            {"d": due, "b": book["id"]},
        )
        conn.commit()
    return conn


def test_list_active_loans_traz_todos_por_padrao(emprestimos_variados):
    rows = list_active_loans(emprestimos_variados)
    assert len(rows) == 4


def test_filtro_somente_atrasados(emprestimos_variados):
    rows = list_active_loans(
        emprestimos_variados, only_overdue=True, reference_date=HOJE
    )
    titulos = sorted(r["title"] for r in rows)
    assert titulos == ["Atrasado 1", "Atrasado 2"]


def test_filtro_de_atrasados_exclui_sem_prazo_e_no_prazo(emprestimos_variados):
    rows = list_active_loans(
        emprestimos_variados, only_overdue=True, reference_date=HOJE
    )
    titulos = {r["title"] for r in rows}
    assert "Sem prazo" not in titulos
    assert "No prazo" not in titulos


def test_filtro_de_atrasados_ignora_devolvidos(emprestimos_variados):
    conn = emprestimos_variados
    loan = conn.execute(
        text(
            "SELECT loans.id FROM loans JOIN books ON books.id = loans.book_id "
            "WHERE books.title = 'Atrasado 1'"
        )
    ).scalars().first()
    return_loan(conn, loan)
    conn.commit()

    rows = list_active_loans(conn, only_overdue=True, reference_date=HOJE)
    assert [r["title"] for r in rows] == ["Atrasado 2"]


def test_dias_de_atraso_reportados_corretamente(emprestimos_variados):
    rows = list_active_loans(
        emprestimos_variados, only_overdue=True, reference_date=HOJE
    )
    atrasos = {r["title"]: days_overdue(r["due_date"], HOJE) for r in rows}
    assert atrasos == {"Atrasado 1": 14, "Atrasado 2": 1}


# --- migração --------------------------------------------------------------

def test_migracao_adiciona_due_date_preservando_emprestimos_existentes(tmp_path, bootstrap_secrets):
    """Banco criado antes do recurso: ALTER TABLE adiciona a coluna sem
    perder dados, e os empréstimos antigos ficam com due_date nulo."""
    import sqlite3

    db_path = tmp_path / "legado.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT NOT NULL,
          email TEXT NOT NULL UNIQUE, phone TEXT, password_hash TEXT NOT NULL,
          salt TEXT NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE books (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL, author TEXT NOT NULL, category TEXT,
          status TEXT NOT NULL DEFAULT 'Disponível', created_at TEXT NOT NULL);
        CREATE TABLE loans (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL, loan_date TEXT NOT NULL, return_date TEXT,
          status TEXT NOT NULL DEFAULT 'ativo');
        INSERT INTO users VALUES (1,'Admin','admin@x.org','','h','s','admin','2026-01-01');
        INSERT INTO books VALUES (1,'ANT-001','Antigo','Autor','Literária','Emprestado','2026-01-01');
        INSERT INTO loans VALUES (1,1,1,'2026-01-05T10:00:00',NULL,'ativo');
        """
    )
    raw.commit()
    raw.close()

    database_url = f"sqlite:///{db_path}"
    engine = get_engine(database_url)
    create_schema(engine)  # dispara a migração

    with get_connection(database_url) as connection:
        loan = connection.execute(
            text("SELECT loan_date, due_date, status FROM loans")
        ).mappings().all()
        assert len(loan) == 1                      # dado preservado
        assert loan[0]["due_date"] is None         # antigo fica sem prazo
        assert is_overdue(loan[0]["due_date"], loan[0]["status"]) is False

    create_schema(engine)  # idempotente


# ---------------------------------------------------------------------------
# Reconciliação de empréstimos (livros "Emprestado" sem loan ativo)
# ---------------------------------------------------------------------------

def _set_book_status(conn, code, status):
    conn.execute(
        text("UPDATE books SET status = :s WHERE code = :c"), {"s": status, "c": code}
    )
    conn.commit()


def _book_by_code(conn, code):
    return conn.execute(
        text("SELECT * FROM books WHERE code = :c"), {"c": code}
    ).mappings().first()


@pytest.fixture
def acervo_para_reconciliar(conn):
    """Cenário da carga inicial:
      - 2 livros "Emprestado" SEM registro de empréstimo (pendentes)
      - 1 livro "Emprestado" COM empréstimo ativo (já regular)
      - 1 livro "Disponível" (nada a fazer)
    """
    create_user(conn, "Leitora Rec", "rec@teste.org", "119", "senha123", "leitor")
    conn.commit()

    codes = {}
    for titulo, autor in [
        ("Pendente Um", "Ana Silva"),
        ("Pendente Dois", "Bruno Costa"),
        ("Regular", "Carla Dias"),
        ("Livre", "Diego Souza"),
    ]:
        codes[titulo] = add_book(conn, titulo, autor, "Literária")
    conn.commit()

    # os dois pendentes vieram da planilha já como Emprestado, sem loan
    _set_book_status(conn, codes["Pendente Um"], "Emprestado")
    _set_book_status(conn, codes["Pendente Dois"], "Emprestado")

    # o "Regular" tem empréstimo ativo de verdade
    leitor = get_user_by_email(conn, "rec@teste.org")
    request_loan(conn, _book_by_code(conn, codes["Regular"])["id"], leitor["id"])
    conn.commit()

    return conn, codes, leitor


# --- listagem --------------------------------------------------------------

def test_lista_apenas_emprestados_sem_loan_ativo(acervo_para_reconciliar):
    conn, _, _ = acervo_para_reconciliar
    titulos = sorted(r["title"] for r in list_unreconciled_books(conn))
    assert titulos == ["Pendente Dois", "Pendente Um"]


def test_contagem_total_pendente(acervo_para_reconciliar):
    conn, _, _ = acervo_para_reconciliar
    assert count_unreconciled_books(conn) == 2


def test_livro_com_emprestimo_ativo_nao_aparece(acervo_para_reconciliar):
    conn, _, _ = acervo_para_reconciliar
    assert "Regular" not in {r["title"] for r in list_unreconciled_books(conn)}


def test_livro_disponivel_nao_aparece(acervo_para_reconciliar):
    conn, _, _ = acervo_para_reconciliar
    assert "Livre" not in {r["title"] for r in list_unreconciled_books(conn)}


def test_emprestimo_devolvido_volta_a_contar_como_pendente(acervo_para_reconciliar):
    """Devolução registrada zera o loan ativo; se o livro seguir 'Emprestado'
    por inconsistência, ele reaparece como pendente."""
    conn, codes, _ = acervo_para_reconciliar
    book = _book_by_code(conn, codes["Regular"])
    loan = conn.execute(
        text("SELECT id FROM loans WHERE book_id = :b AND status = 'ativo'"),
        {"b": book["id"]},
    ).scalars().first()
    return_loan(conn, loan)
    conn.commit()
    _set_book_status(conn, codes["Regular"], "Emprestado")

    assert "Regular" in {r["title"] for r in list_unreconciled_books(conn)}


def test_busca_e_paginacao_da_reconciliacao(acervo_para_reconciliar):
    conn, _, _ = acervo_para_reconciliar
    assert count_unreconciled_books(conn, query="Pendente Um") == 1
    assert [r["title"] for r in list_unreconciled_books(conn, query="Pendente Um")] == [
        "Pendente Um"
    ]
    # busca sem acento/caixa igual ao resto do sistema
    assert count_unreconciled_books(conn, query="pendente") == 2
    # paginação
    page = list_unreconciled_books(conn, limit=1, offset=0)
    assert len(page) == 1
    assert len(list_unreconciled_books(conn, limit=1, offset=1)) == 1
    assert list_unreconciled_books(conn, limit=1, offset=5) == []


def test_list_borrowers_traz_somente_leitores(acervo_para_reconciliar):
    conn, _, _ = acervo_para_reconciliar
    borrowers = list_borrowers(conn)
    assert [b["email"] for b in borrowers] == ["rec@teste.org"]  # admin fora


# --- ação 1: registrar empréstimo -----------------------------------------

def test_registrar_emprestimo_cria_loan_e_mantem_status(acervo_para_reconciliar):
    conn, codes, leitor = acervo_para_reconciliar
    book = _book_by_code(conn, codes["Pendente Um"])

    reconcile_register_loan(conn, book["id"], leitor["id"])
    conn.commit()

    loan = conn.execute(
        text("SELECT * FROM loans WHERE book_id = :b"), {"b": book["id"]}
    ).mappings().first()
    assert loan is not None
    assert loan["status"] == "ativo"
    assert loan["user_id"] == leitor["id"]
    assert loan["due_date"]  # prazo preenchido

    # o livro continua com o leitor -> segue Emprestado
    assert _book_by_code(conn, codes["Pendente Um"])["status"] == "Emprestado"
    # e sai da lista de pendentes
    assert "Pendente Um" not in {r["title"] for r in list_unreconciled_books(conn)}
    assert count_unreconciled_books(conn) == 1


def test_registrar_emprestimo_aceita_data_e_prazo_informados(acervo_para_reconciliar):
    conn, codes, leitor = acervo_para_reconciliar
    book = _book_by_code(conn, codes["Pendente Um"])

    reconcile_register_loan(
        conn,
        book["id"],
        leitor["id"],
        loan_date=date(2026, 3, 1),
        due_date=date(2026, 3, 20),
    )
    conn.commit()

    loan = conn.execute(
        text("SELECT loan_date, due_date FROM loans WHERE book_id = :b"),
        {"b": book["id"]},
    ).mappings().first()
    assert loan["loan_date"] == "2026-03-01"
    assert loan["due_date"] == "2026-03-20"


def test_registrar_emprestimo_sem_prazo_aplica_prazo_padrao(acervo_para_reconciliar):
    conn, codes, leitor = acervo_para_reconciliar
    book = _book_by_code(conn, codes["Pendente Um"])

    reconcile_register_loan(conn, book["id"], leitor["id"], loan_date=date(2026, 3, 1))
    conn.commit()

    due = conn.execute(
        text("SELECT due_date FROM loans WHERE book_id = :b"), {"b": book["id"]}
    ).scalars().first()
    assert due == (date(2026, 3, 1) + timedelta(days=app.PRAZO_PADRAO_DIAS)).isoformat()


def test_registrar_emprestimo_o_torna_visivel_em_emprestimos_ativos(
    acervo_para_reconciliar,
):
    conn, codes, leitor = acervo_para_reconciliar
    book = _book_by_code(conn, codes["Pendente Um"])
    assert "Pendente Um" not in {r["title"] for r in list_active_loans(conn)}

    reconcile_register_loan(conn, book["id"], leitor["id"])
    conn.commit()

    assert "Pendente Um" in {r["title"] for r in list_active_loans(conn)}


def test_registrar_emprestimo_com_leitor_inexistente_falha(acervo_para_reconciliar):
    conn, codes, _ = acervo_para_reconciliar
    book = _book_by_code(conn, codes["Pendente Um"])

    with pytest.raises(ValueError, match="Leitor"):
        reconcile_register_loan(conn, book["id"], 9999)
    conn.rollback()

    # nada foi criado
    assert count_loans_for_book(conn, book["id"]) == 0


# --- ação 2: marcar como devolvido ----------------------------------------

def test_marcar_como_devolvido_libera_o_livro_sem_criar_loan(acervo_para_reconciliar):
    conn, codes, _ = acervo_para_reconciliar
    book = _book_by_code(conn, codes["Pendente Um"])

    reconcile_mark_returned(conn, book["id"])
    conn.commit()

    assert _book_by_code(conn, codes["Pendente Um"])["status"] == "Disponível"
    assert count_loans_for_book(conn, book["id"]) == 0  # sem histórico inventado
    assert count_unreconciled_books(conn) == 1


# --- concorrência: já reconciliado por outra sessão ------------------------

def test_registrar_falha_se_outra_sessao_ja_marcou_como_devolvido(
    acervo_para_reconciliar,
):
    conn, codes, leitor = acervo_para_reconciliar
    book = _book_by_code(conn, codes["Pendente Um"])

    # outra sessão liberou o livro entre o carregamento da tela e a confirmação
    reconcile_mark_returned(conn, book["id"])
    conn.commit()

    with pytest.raises(ValueError, match="não está mais como 'Emprestado'"):
        reconcile_register_loan(conn, book["id"], leitor["id"])
    conn.rollback()

    assert count_loans_for_book(conn, book["id"]) == 0


def test_registrar_falha_se_outra_sessao_ja_registrou_o_emprestimo(
    acervo_para_reconciliar,
):
    conn, codes, leitor = acervo_para_reconciliar
    book = _book_by_code(conn, codes["Pendente Um"])

    reconcile_register_loan(conn, book["id"], leitor["id"])
    conn.commit()

    with pytest.raises(ValueError, match="já tem um empréstimo ativo"):
        reconcile_register_loan(conn, book["id"], leitor["id"])
    conn.rollback()

    # continua com um único empréstimo, sem duplicata
    assert count_loans_for_book(conn, book["id"]) == 1


def test_marcar_devolvido_falha_se_outra_sessao_ja_registrou_emprestimo(
    acervo_para_reconciliar,
):
    conn, codes, leitor = acervo_para_reconciliar
    book = _book_by_code(conn, codes["Pendente Um"])

    reconcile_register_loan(conn, book["id"], leitor["id"])
    conn.commit()

    with pytest.raises(ValueError, match="já tem um empréstimo ativo"):
        reconcile_mark_returned(conn, book["id"])
    conn.rollback()

    # o livro segue emprestado e o registro permanece
    assert _book_by_code(conn, codes["Pendente Um"])["status"] == "Emprestado"
    assert count_loans_for_book(conn, book["id"]) == 1


def test_marcar_devolvido_falha_se_outra_sessao_ja_liberou(acervo_para_reconciliar):
    conn, codes, _ = acervo_para_reconciliar
    book = _book_by_code(conn, codes["Pendente Um"])

    reconcile_mark_returned(conn, book["id"])
    conn.commit()

    with pytest.raises(ValueError, match="não está mais como 'Emprestado'"):
        reconcile_mark_returned(conn, book["id"])
    conn.rollback()


def test_acao_em_livro_removido_falha_com_mensagem_clara(acervo_para_reconciliar):
    conn, codes, leitor = acervo_para_reconciliar
    book = _book_by_code(conn, codes["Pendente Um"])
    conn.execute(text("DELETE FROM books WHERE id = :id"), {"id": book["id"]})
    conn.commit()

    with pytest.raises(ValueError, match="não encontrado"):
        reconcile_mark_returned(conn, book["id"])
    conn.rollback()


def test_conflito_real_entre_duas_conexoes_simultaneas(tmp_path):
    """Duas sessões de admin abertas ao mesmo tempo (conexões distintas):
    a segunda precisa falhar ao confirmar sobre um livro já reconciliado
    pela primeira, em vez de duplicar o empréstimo."""
    database_url = f"sqlite:///{tmp_path}/concorrencia.db"
    engine = get_engine(database_url)
    create_schema(engine)

    setup = get_connection(database_url)
    create_user(setup, "Leitora", "dupla@teste.org", "", "senha123", "leitor")
    code = add_book(setup, "Livro Disputado", "Ana Silva", "Literária")
    setup.commit()
    _set_book_status(setup, code, "Emprestado")
    book_id = _book_by_code(setup, code)["id"]
    leitor_id = get_user_by_email(setup, "dupla@teste.org")["id"]
    setup.close()

    admin_a = get_connection(database_url)
    admin_b = get_connection(database_url)
    try:
        # os dois carregaram a tela e veem o mesmo livro pendente
        assert count_unreconciled_books(admin_a) == 1
        assert count_unreconciled_books(admin_b) == 1

        # admin A confirma primeiro
        reconcile_register_loan(admin_a, book_id, leitor_id)
        admin_a.commit()

        # admin B confirma depois, sobre um estado já obsoleto
        with pytest.raises(ValueError, match="já tem um empréstimo ativo"):
            reconcile_register_loan(admin_b, book_id, leitor_id)
        admin_b.rollback()

        # exatamente um empréstimo, sem duplicata
        assert count_loans_for_book(admin_a, book_id) == 1
        assert count_unreconciled_books(admin_a) == 0
    finally:
        admin_a.close()
        admin_b.close()


# ---------------------------------------------------------------------------
# Painel de indicadores e exportação CSV
# ---------------------------------------------------------------------------

def _decode_csv(data: bytes):
    """Decodifica o CSV exportado e devolve (texto, linhas parseadas)."""
    assert data.startswith(b"\xef\xbb\xbf"), "CSV precisa começar com BOM UTF-8"
    text_content = data.decode("utf-8-sig")
    return text_content, list(csv.reader(io.StringIO(text_content)))


@pytest.fixture
def acervo_com_indicadores(conn):
    """Cenário com números conhecidos para conferir cada indicador."""
    create_user(conn, "Leitora A", "a@teste.org", "111", "senha123", "leitor")
    create_user(conn, "Leitor B", "b@teste.org", "222", "senha123", "leitor")
    create_user(conn, "Admin Extra", "admin2@teste.org", "", "senha123", "admin")
    conn.commit()

    codes = {}
    for titulo, autor, categoria in [
        ("Livro Disponível", "Ana Silva", "Literária"),
        ("Livro Emprestado No Prazo", "Bruno Costa", "Literária"),
        ("Livro Atrasado", "Carla Dias", "Literária"),
        ("Livro Em Manutenção", "Diego Souza", "Espiritual"),
        ("Livro Sem Registro", "Elena Rocha", "Espiritual"),
    ]:
        codes[titulo] = add_book(conn, titulo, autor, categoria)
    conn.commit()

    leitora = get_user_by_email(conn, "a@teste.org")

    # empréstimo no prazo
    request_loan(
        conn,
        _book_by_code(conn, codes["Livro Emprestado No Prazo"])["id"],
        leitora["id"],
        due_date=date(2026, 12, 31),
    )
    # empréstimo atrasado
    request_loan(
        conn,
        _book_by_code(conn, codes["Livro Atrasado"])["id"],
        leitora["id"],
        due_date=date(2020, 1, 10),
    )
    conn.commit()

    _set_book_status(conn, codes["Livro Em Manutenção"], "Em Manutenção")
    # emprestado na planilha, sem loan -> pendente de reconciliação
    _set_book_status(conn, codes["Livro Sem Registro"], "Emprestado")

    return conn, codes, leitora


def test_indicadores_com_dados_conhecidos(acervo_com_indicadores):
    conn, _, _ = acervo_com_indicadores
    m = get_dashboard_metrics(conn, reference_date=date(2026, 6, 1))

    assert m["total_livros"] == 5
    assert m["disponiveis"] == 1
    assert m["emprestados"] == 3   # 2 com loan + 1 pendente de reconciliação
    assert m["em_manutencao"] == 1
    assert m["emprestimos_ativos"] == 2
    assert m["emprestimos_atrasados"] == 1
    assert m["leitores"] == 2      # o admin não conta
    assert m["pendentes_reconciliacao"] == 1


def test_indicadores_somam_o_total_de_livros(acervo_com_indicadores):
    conn, _, _ = acervo_com_indicadores
    m = get_dashboard_metrics(conn)
    assert m["disponiveis"] + m["emprestados"] + m["em_manutencao"] == m["total_livros"]


def test_indicadores_com_banco_vazio(conn):
    m = get_dashboard_metrics(conn)
    assert m == {
        "total_livros": 0,
        "disponiveis": 0,
        "emprestados": 0,
        "em_manutencao": 0,
        "emprestimos_ativos": 0,
        "emprestimos_atrasados": 0,
        "leitores": 0,
        "pendentes_reconciliacao": 0,
    }


def test_indicador_de_atraso_respeita_a_data_de_referencia(acervo_com_indicadores):
    conn, _, _ = acervo_com_indicadores
    # antes de qualquer vencimento: nenhum atraso
    assert get_dashboard_metrics(conn, reference_date=date(2019, 1, 1))[
        "emprestimos_atrasados"
    ] == 0
    # depois dos dois vencimentos: os dois atrasados
    assert get_dashboard_metrics(conn, reference_date=date(2027, 1, 1))[
        "emprestimos_atrasados"
    ] == 2


def test_emprestimo_sem_prazo_nao_conta_como_atrasado(conn):
    create_user(conn, "Leitora", "sem@teste.org", "", "senha123", "leitor")
    code = add_book(conn, "Livro Antigo", "Ana Silva", "Literária")
    conn.commit()
    leitor = get_user_by_email(conn, "sem@teste.org")
    request_loan(conn, _book_by_code(conn, code)["id"], leitor["id"])
    # empréstimo legado, anterior ao controle de prazo
    conn.execute(text("UPDATE loans SET due_date = NULL"))
    conn.commit()

    m = get_dashboard_metrics(conn, reference_date=date(2030, 1, 1))
    assert m["emprestimos_ativos"] == 1
    assert m["emprestimos_atrasados"] == 0


def test_devolucao_reduz_emprestimos_ativos(acervo_com_indicadores):
    conn, codes, _ = acervo_com_indicadores
    book = _book_by_code(conn, codes["Livro Atrasado"])
    loan = conn.execute(
        text("SELECT id FROM loans WHERE book_id = :b AND status = 'ativo'"),
        {"b": book["id"]},
    ).scalars().first()
    return_loan(conn, loan)
    conn.commit()

    m = get_dashboard_metrics(conn, reference_date=date(2026, 6, 1))
    assert m["emprestimos_ativos"] == 1
    assert m["emprestimos_atrasados"] == 0
    assert m["disponiveis"] == 2


# --- exportação: catálogo --------------------------------------------------

def test_export_books_csv_cabecalho_e_conteudo(acervo_com_indicadores):
    conn, _, _ = acervo_com_indicadores
    raw, rows = _decode_csv(export_books_csv(conn))

    assert rows[0] == ["Código", "Título", "Autor", "Categoria", "Status"]
    assert len(rows) == 6  # cabeçalho + 5 livros

    por_titulo = {r[1]: r for r in rows[1:]}
    assert por_titulo["Livro Disponível"][2] == "Ana Silva"
    assert por_titulo["Livro Disponível"][3] == "Literária"
    assert por_titulo["Livro Disponível"][4] == "Disponível"
    assert por_titulo["Livro Em Manutenção"][4] == "Em Manutenção"


def test_export_books_csv_usa_bom_e_aspas_em_todos_os_campos(acervo_com_indicadores):
    conn, _, _ = acervo_com_indicadores
    data = export_books_csv(conn)
    assert data.startswith(b"\xef\xbb\xbf")

    raw, _ = _decode_csv(data)
    primeira_linha = raw.splitlines()[0]
    assert primeira_linha == '"Código","Título","Autor","Categoria","Status"'
    # nenhum campo sem aspas em nenhuma linha
    for linha in raw.splitlines():
        for campo in linha.split(","):
            assert campo.startswith('"') or campo.endswith('"'), linha


def test_export_books_csv_preserva_acentos(acervo_com_indicadores):
    conn, _, _ = acervo_com_indicadores
    raw, _ = _decode_csv(export_books_csv(conn))
    assert "Livro Em Manutenção" in raw
    assert "Livro Disponível" in raw


def test_export_books_csv_nao_quebra_com_virgula_no_titulo(conn):
    add_book(conn, "Memórias, Póstumas; e outras", "Ana Silva", "Literária")
    conn.commit()

    _, rows = _decode_csv(export_books_csv(conn))
    assert rows[1][1] == "Memórias, Póstumas; e outras"
    assert len(rows[1]) == 5  # o título com vírgula não criou colunas extras


def test_export_books_csv_com_acervo_vazio_traz_so_o_cabecalho(conn):
    _, rows = _decode_csv(export_books_csv(conn))
    assert rows == [["Código", "Título", "Autor", "Categoria", "Status"]]


# --- exportação: histórico de empréstimos ---------------------------------

def test_export_loans_csv_cabecalho_e_conteudo(acervo_com_indicadores):
    conn, _, _ = acervo_com_indicadores
    _, rows = _decode_csv(export_loans_csv(conn))

    assert rows[0] == [
        "Livro",
        "Código",
        "Leitor",
        "E-mail",
        "Data do empréstimo",
        "Devolução prevista",
        "Data de devolução",
        "Status",
    ]
    assert len(rows) == 3  # cabeçalho + 2 empréstimos

    por_livro = {r[0]: r for r in rows[1:]}
    atrasado = por_livro["Livro Atrasado"]
    assert atrasado[2] == "Leitora A"
    assert atrasado[3] == "a@teste.org"
    assert atrasado[5] == "2020-01-10"   # devolução prevista
    assert atrasado[6] == ""             # ainda não devolvido
    assert atrasado[7] == "ativo"


def test_export_loans_csv_inclui_devolucao_registrada(acervo_com_indicadores):
    conn, codes, _ = acervo_com_indicadores
    book = _book_by_code(conn, codes["Livro Atrasado"])
    loan = conn.execute(
        text("SELECT id FROM loans WHERE book_id = :b AND status = 'ativo'"),
        {"b": book["id"]},
    ).scalars().first()
    return_loan(conn, loan)
    conn.commit()

    _, rows = _decode_csv(export_loans_csv(conn))
    devolvido = next(r for r in rows[1:] if r[0] == "Livro Atrasado")
    assert devolvido[6] != ""          # data de devolução preenchida
    assert devolvido[7] == "devolvido"


def test_export_loans_csv_com_historico_vazio_traz_so_o_cabecalho(conn):
    _, rows = _decode_csv(export_loans_csv(conn))
    assert len(rows) == 1
    assert rows[0][0] == "Livro"


def test_export_loans_csv_nao_vaza_dados_de_leitor_removido(acervo_com_indicadores):
    """Empréstimo órfão (leitor apagado direto no banco) mantém a linha do
    histórico, mas sem nome nem e-mail da pessoa."""
    conn, _, leitora = acervo_com_indicadores
    email = leitora["email"]

    # apaga o leitor contornando a FK, simulando remoção fora do app
    conn.commit()
    conn.execute(text("PRAGMA foreign_keys = OFF"))
    conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": leitora["id"]})
    conn.commit()
    conn.execute(text("PRAGMA foreign_keys = ON"))

    raw, rows = _decode_csv(export_loans_csv(conn))

    # as linhas do histórico continuam lá (LEFT JOIN, não INNER)
    assert len(rows) == 3
    for linha in rows[1:]:
        assert linha[2] == app.ANONYMIZED_BORROWER_LABEL
        assert linha[3] == ""

    # nenhum dado pessoal do leitor removido aparece no arquivo
    assert email not in raw
    assert "Leitora A" not in raw


# ---------------------------------------------------------------------------
# loan_summary_for_books — agregação que substituiu o N+1 da Gestão de Livros
# ---------------------------------------------------------------------------

def test_loan_summary_livro_sem_emprestimo_vem_zerado(conn):
    code = add_book(conn, "Sem Historico", "Ana Silva", "Literária")
    conn.commit()
    book = _book_by_code(conn, code)

    assert loan_summary_for_books(conn, [book["id"]]) == {
        book["id"]: {"total": 0, "ativos": 0}
    }


def test_loan_summary_conta_ativos_e_total(conn):
    create_user(conn, "Leitora", "sum@teste.org", "", "senha123", "leitor")
    code = add_book(conn, "Com Historico", "Ana Silva", "Literária")
    conn.commit()
    leitor = get_user_by_email(conn, "sum@teste.org")
    book = _book_by_code(conn, code)

    # dois ciclos devolvidos + um ativo = total 3, ativos 1
    for _ in range(2):
        request_loan(conn, book["id"], leitor["id"])
        conn.commit()
        loan = conn.execute(
            text("SELECT id FROM loans WHERE book_id=:b AND status='ativo'"),
            {"b": book["id"]},
        ).scalars().first()
        return_loan(conn, loan)
        conn.commit()
    request_loan(conn, book["id"], leitor["id"])
    conn.commit()

    assert loan_summary_for_books(conn, [book["id"]]) == {
        book["id"]: {"total": 3, "ativos": 1}
    }


def test_loan_summary_lista_vazia_nao_consulta_o_banco(conn):
    assert loan_summary_for_books(conn, []) == {}


def test_loan_summary_equivale_as_funcoes_por_livro_que_substituiu(conn):
    """Garante que a agregação em lote devolve exatamente a mesma informação
    que as duas consultas por livro usadas antes."""
    create_user(conn, "Leitora", "eq@teste.org", "", "senha123", "leitor")
    conn.commit()
    leitor = get_user_by_email(conn, "eq@teste.org")

    # nomes alfabeticamente distintos: "Autor 1"/"Autor 2" gerariam o MESMO
    # codigo (digitos nao entram na regra) e colidiriam no UNIQUE
    autores = ["Ana Silva", "Bruno Costa", "Carla Dias", "Diego Souza", "Elena Rocha"]
    codes = [add_book(conn, f"Livro {i}", autores[i], "Literária") for i in range(5)]
    conn.commit()
    books = [_book_by_code(conn, c) for c in codes]

    # livro 0: nada | livro 1: ativo | livro 2: devolvido | livro 3: devolvido+ativo
    request_loan(conn, books[1]["id"], leitor["id"])
    conn.commit()
    for idx in (2, 3):
        request_loan(conn, books[idx]["id"], leitor["id"])
        conn.commit()
        loan = conn.execute(
            text("SELECT id FROM loans WHERE book_id=:b AND status='ativo'"),
            {"b": books[idx]["id"]},
        ).scalars().first()
        return_loan(conn, loan)
        conn.commit()
    request_loan(conn, books[3]["id"], leitor["id"])
    conn.commit()

    summary = loan_summary_for_books(conn, [b["id"] for b in books])
    for b in books:
        esperado_ativo = get_active_loan_for_book(conn, b["id"]) is not None
        esperado_total = count_loans_for_book(conn, b["id"])
        assert (summary[b["id"]]["ativos"] > 0) is esperado_ativo, b["code"]
        assert summary[b["id"]]["total"] == esperado_total, b["code"]


def test_loan_summary_uma_unica_query_para_a_pagina_inteira(conn):
    """O ponto da otimização: 25 livros custam 1 query, não 50."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    for i in range(25):
        conn.execute(
            text(
                "INSERT INTO books (code,title,author,category,status,created_at) "
                "VALUES (:c,:t,'Autor X','Literária','Disponível','2026-01-01')"
            ),
            {"c": f"SUM-{i:03d}", "t": f"Pag {i}"},
        )
    conn.commit()
    ids = [_book_by_code(conn, f"SUM-{i:03d}")["id"] for i in range(25)]

    contador = {"n": 0}

    def _conta(*a, **k):
        contador["n"] += 1

    event.listen(Engine, "before_cursor_execute", _conta)
    try:
        loan_summary_for_books(conn, ids)
    finally:
        event.remove(Engine, "before_cursor_execute", _conta)

    assert contador["n"] == 1


# ---------------------------------------------------------------------------
# Recuperação de senha: reset presencial feito pelo administrador
# (não há fluxo por e-mail — ver README, seção "Recuperação de senha")
# ---------------------------------------------------------------------------

def _admin_e_leitor(conn):
    """Um admin (o ator) e um leitor (o alvo), já commitados."""
    create_user(conn, "Admin Um", "admin1@teste.org", "", "SenhaAdmin1", "admin")
    create_user(conn, "Maria Souza", "maria@teste.org", "", "senhaAntiga1", "leitor")
    conn.commit()
    return (
        get_user_by_email(conn, "admin1@teste.org"),
        get_user_by_email(conn, "maria@teste.org"),
    )


def test_reset_gera_senha_temporaria_e_marca_troca_obrigatoria(conn):
    admin, maria = _admin_e_leitor(conn)

    temporaria = admin_reset_password(conn, admin, maria["id"])

    assert password_strength_error(temporaria) is None
    depois = get_user_by_email(conn, "maria@teste.org")
    assert verify_password(temporaria, depois["password_hash"], depois["salt"])
    assert bool(depois["must_change_password"]) is True


def test_reset_invalida_a_senha_antiga(conn):
    """O ponto central: depois do reset a senha anterior não abre mais a conta."""
    admin, maria = _admin_e_leitor(conn)
    assert authenticate(conn, "maria@teste.org", "senhaAntiga1") is not None

    admin_reset_password(conn, admin, maria["id"])

    assert authenticate(conn, "maria@teste.org", "senhaAntiga1") is None
    depois = get_user_by_email(conn, "maria@teste.org")
    assert not verify_password("senhaAntiga1", depois["password_hash"], depois["salt"])


def test_reset_aceita_senha_definida_pelo_admin_e_recusa_senha_fraca(conn):
    admin, maria = _admin_e_leitor(conn)

    escolhida = admin_reset_password(conn, admin, maria["id"], "SenhaEscolhida9")
    assert escolhida == "SenhaEscolhida9"
    depois = get_user_by_email(conn, "maria@teste.org")
    assert verify_password("SenhaEscolhida9", depois["password_hash"], depois["salt"])

    with pytest.raises(ValueError):
        admin_reset_password(conn, admin, maria["id"], "curta")
    # a senha fraca não pode ter sido gravada
    ainda = get_user_by_email(conn, "maria@teste.org")
    assert verify_password("SenhaEscolhida9", ainda["password_hash"], ainda["salt"])


def test_reset_de_usuario_inexistente_levanta_erro(conn):
    admin, _ = _admin_e_leitor(conn)
    with pytest.raises(ValueError):
        admin_reset_password(conn, admin, 999999)


def test_admin_redefine_senha_de_outro_admin(conn):
    """Proteção contra perda de acesso administrativo: o segundo admin é
    justamente quem consegue devolver o acesso a quem esqueceu a senha."""
    create_user(conn, "Admin Um", "admin1@teste.org", "", "SenhaAdmin1", "admin")
    create_user(conn, "Admin Dois", "admin2@teste.org", "", "SenhaAdmin2", "admin")
    conn.commit()
    ator = get_user_by_email(conn, "admin1@teste.org")
    esquecido = get_user_by_email(conn, "admin2@teste.org")

    temporaria = admin_reset_password(conn, ator, esquecido["id"])

    assert authenticate(conn, "admin2@teste.org", "SenhaAdmin2") is None
    recuperado = authenticate(conn, "admin2@teste.org", temporaria)
    assert recuperado is not None
    assert recuperado["role"] == "admin"
    assert bool(recuperado["must_change_password"]) is True


def test_reset_registra_na_auditoria_quem_redefiniu_de_quem_e_quando(conn):
    admin, maria = _admin_e_leitor(conn)

    antes = datetime.now().replace(microsecond=0)
    admin_reset_password(conn, admin, maria["id"])

    entradas = list_admin_audit(conn)
    assert len(entradas) == 1
    registro = entradas[0]
    assert registro["action"] == AUDIT_ACTION_PASSWORD_RESET
    assert registro["actor_email"] == "admin1@teste.org"
    assert registro["target_email"] == "maria@teste.org"
    assert datetime.fromisoformat(registro["created_at"]) >= antes


def test_auditoria_mantem_ordem_mais_recente_primeiro(conn):
    admin, maria = _admin_e_leitor(conn)
    create_user(conn, "Joao", "joao@teste.org", "", "senhaJoao12", "leitor")
    conn.commit()
    joao = get_user_by_email(conn, "joao@teste.org")

    admin_reset_password(conn, admin, maria["id"])
    admin_reset_password(conn, admin, joao["id"])

    alvos = [e["target_email"] for e in list_admin_audit(conn)]
    assert alvos == ["joao@teste.org", "maria@teste.org"]


def test_reset_libera_conta_travada_pelo_rate_limit(conn):
    """Quem esqueceu a senha normalmente errou várias vezes antes de pedir
    ajuda — a senha temporária tem que funcionar na hora, sem esperar o
    bloqueio de força bruta expirar."""
    admin, maria = _admin_e_leitor(conn)
    for _ in range(MAX_LOGIN_ATTEMPTS):
        _register_failed_login(conn, "maria@teste.org")
    assert _login_locked_until(conn, "maria@teste.org") is not None

    temporaria = admin_reset_password(conn, admin, maria["id"])

    assert _login_locked_until(conn, "maria@teste.org") is None
    assert authenticate(conn, "maria@teste.org", temporaria) is not None


def test_senha_temporaria_gerada_e_forte_aleatoria_e_sem_caracteres_ambiguos(conn):
    senhas = {generate_temporary_password() for _ in range(50)}
    assert len(senhas) == 50  # aleatória de verdade, não um valor fixo
    for senha in senhas:
        assert password_strength_error(senha) is None
        assert len(senha) >= MIN_PASSWORD_LENGTH
        assert not (set(senha) & set("0O1lI"))


# ---------------------------------------------------------------------------
# Invalidação da sessão ativa do usuário cuja senha foi redefinida
# ---------------------------------------------------------------------------

def test_session_is_current_derruba_sessao_apos_reset(conn):
    admin, maria = _admin_e_leitor(conn)
    sessao = app._session_user_view(maria)
    assert _session_is_current(conn, sessao) is True

    admin_reset_password(conn, admin, maria["id"])

    assert _session_is_current(conn, sessao) is False


def test_session_is_current_falso_para_conta_removida(conn):
    admin, maria = _admin_e_leitor(conn)
    sessao = app._session_user_view(maria)
    conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": maria["id"]})
    conn.commit()

    assert _session_is_current(conn, sessao) is False


def test_session_is_current_sobrevive_a_troca_de_senha_do_proprio_usuario(conn):
    """Trocar a própria senha não pode derrubar a sessão de quem está
    trocando — só o reset feito por um admin invalida."""
    _, maria = _admin_e_leitor(conn)
    sessao = app._session_user_view(maria)

    assert change_password(conn, maria["id"], "senhaAntiga1", "senhaNova123") is True

    assert _session_is_current(conn, sessao) is True


def test_sessao_aberta_em_outra_aba_cai_no_proximo_clique(tmp_path, monkeypatch):
    """Ponta a ponta: a leitora está logada; um admin redefine a senha dela
    pelo banco (como faria em outra aba) e o próximo rerun da sessão dela cai
    na tela de login com o aviso, em vez de continuar navegando."""
    from streamlit.testing.v1 import AppTest

    database_url = f"sqlite:///{tmp_path}/sessao_revogada.db"
    monkeypatch.setattr(app.st, "secrets", _secrets(DATABASE_URL=database_url))

    with get_connection(database_url) as connection:
        create_schema(get_engine(database_url))
        create_user(connection, "Maria", "maria@teste.org", "", "senhaAntiga1", "leitor")
        create_user(connection, "Admin Um", "admin1@teste.org", "", "SenhaAdmin1", "admin")
        connection.commit()

    at = AppTest.from_file("app.py")
    at.run()
    at.text_input(key="login_email").input("maria@teste.org")
    at.text_input(key="login_password").input("senhaAntiga1")
    at.button(key="FormSubmitter:login_form-Entrar").click().run()
    assert not at.exception, at.exception
    assert at.radio  # dentro do app, menu do leitor à mostra

    with get_connection(database_url) as connection:
        admin = get_user_by_email(connection, "admin1@teste.org")
        maria = get_user_by_email(connection, "maria@teste.org")
        temporaria = admin_reset_password(connection, admin, maria["id"])

    at.run()  # próximo clique na aba que ficou aberta
    assert not at.exception, at.exception
    assert not at.radio  # menu sumiu: voltou para a tela de login
    assert any("sessão foi encerrada" in (w.value or "") for w in at.warning)

    # e a senha antiga não reabre a sessão; a temporária cai na troca obrigatória
    at.text_input(key="login_email").input("maria@teste.org")
    at.text_input(key="login_password").input("senhaAntiga1")
    at.button(key="FormSubmitter:login_form-Entrar").click().run()
    assert any("E-mail ou senha inválidos" in (e.value or "") for e in at.error)

    at.text_input(key="login_email").input("maria@teste.org")
    at.text_input(key="login_password").input(temporaria)
    at.button(key="FormSubmitter:login_form-Entrar").click().run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Alterar minha senha"


# ---------------------------------------------------------------------------
# Tela "Gestão de Usuários" (admin)
# ---------------------------------------------------------------------------

def _abre_gestao_de_usuarios(tmp_path, monkeypatch, nome_db):
    """Sobe o app, loga como admin do bootstrap (concluindo a troca forçada)
    e navega até a tela de Gestão de Usuários."""
    from streamlit.testing.v1 import AppTest

    database_url = f"sqlite:///{tmp_path}/{nome_db}.db"
    monkeypatch.setattr(app.st, "secrets", _secrets(DATABASE_URL=database_url))

    at = AppTest.from_file("app.py")
    at.run()
    _login_as_admin_and_complete_forced_password_change(at)
    at.radio[0].set_value("Gestão de Usuários").run()
    assert not at.exception, at.exception
    return at, database_url


def test_tela_avisa_quando_ha_apenas_um_administrador(tmp_path, monkeypatch):
    at, database_url = _abre_gestao_de_usuarios(tmp_path, monkeypatch, "um_admin")

    assert at.header[0].value == "Gestão de Usuários"
    assert any("único administrador" in (w.value or "") for w in at.warning)

    with get_connection(database_url) as connection:
        assert count_admins(connection) == 1


def test_aviso_de_admin_unico_some_apos_cadastrar_o_segundo(tmp_path, monkeypatch):
    at, database_url = _abre_gestao_de_usuarios(tmp_path, monkeypatch, "dois_admins")
    assert any("único administrador" in (w.value or "") for w in at.warning)

    at.text_input(key="new_admin_name").input("Segunda Admin")
    at.text_input(key="new_admin_email").input("segunda@teste.org")
    at.text_input(key="new_admin_password").input("SenhaSegunda1")
    at.button(key="FormSubmitter:new_admin_form-Cadastrar administrador").click().run()
    assert not at.exception, at.exception

    assert not any("único administrador" in (w.value or "") for w in at.warning)
    with get_connection(database_url) as connection:
        assert count_admins(connection) == 2
        nova = get_user_by_email(connection, "segunda@teste.org")
        assert nova["role"] == "admin"
        # nasce obrigada a trocar a senha provisória definida pelo outro admin
        assert bool(nova["must_change_password"]) is True


def test_tela_redefine_senha_exigindo_confirmacao_e_mostra_a_senha_uma_vez(
    tmp_path, monkeypatch
):
    at, database_url = _abre_gestao_de_usuarios(tmp_path, monkeypatch, "reset_tela")

    with get_connection(database_url) as connection:
        create_user(connection, "Maria Souza", "maria@teste.org", "", "senhaAntiga1", "leitor")
        connection.commit()
        maria = get_user_by_email(connection, "maria@teste.org")

    at.run()
    # sem marcar a confirmação, o botão de redefinir fica desabilitado
    botao = at.button(key=f"reset_{maria['id']}")
    assert botao.disabled is True

    at.checkbox(key=f"confirm_reset_{maria['id']}").check().run()
    at.button(key=f"reset_{maria['id']}").click().run()
    assert not at.exception, at.exception

    assert any("Senha redefinida" in (s.value or "") for s in at.success)
    temporaria = at.code[0].value
    assert password_strength_error(temporaria) is None

    with get_connection(database_url) as connection:
        depois = get_user_by_email(connection, "maria@teste.org")
        assert verify_password(temporaria, depois["password_hash"], depois["salt"])
        assert not verify_password("senhaAntiga1", depois["password_hash"], depois["salt"])
        assert bool(depois["must_change_password"]) is True

    # exibida uma única vez: ao ocultar, a senha some da tela e não volta
    at.button(key="dismiss_reset_result").click().run()
    assert not at.code


def test_tela_nao_oferece_reset_para_a_propria_conta_do_admin(tmp_path, monkeypatch):
    """Redefinir a si mesmo encerraria a própria sessão no mesmo instante —
    a tela manda usar 'Alterar minha senha'."""
    at, database_url = _abre_gestao_de_usuarios(tmp_path, monkeypatch, "auto_reset")

    with get_connection(database_url) as connection:
        admin = get_user_by_email(connection, BOOTSTRAP_EMAIL)

    assert not [b for b in at.button if b.key == f"reset_{admin['id']}"]
    assert any(
        "Alterar minha senha" in (c.value or "") for c in at.caption
    )


def test_tela_permite_admin_redefinir_senha_de_outro_admin(tmp_path, monkeypatch):
    at, database_url = _abre_gestao_de_usuarios(tmp_path, monkeypatch, "reset_admin")

    with get_connection(database_url) as connection:
        create_user(connection, "Admin Dois", "admin2@teste.org", "", "SenhaAdmin2", "admin")
        connection.commit()
        outro = get_user_by_email(connection, "admin2@teste.org")

    at.run()
    at.checkbox(key=f"confirm_reset_{outro['id']}").check().run()
    at.button(key=f"reset_{outro['id']}").click().run()
    assert not at.exception, at.exception

    temporaria = at.code[0].value
    with get_connection(database_url) as connection:
        assert authenticate(connection, "admin2@teste.org", "SenhaAdmin2") is None
        recuperado = authenticate(connection, "admin2@teste.org", temporaria)
        assert recuperado is not None and recuperado["role"] == "admin"
        registros = list_admin_audit(connection)
        assert registros[0]["actor_email"] == BOOTSTRAP_EMAIL
        assert registros[0]["target_email"] == "admin2@teste.org"


def test_login_com_senha_temporaria_cai_na_troca_obrigatoria(tmp_path, monkeypatch):
    """Fluxo completo do usuário que esqueceu a senha: recebe a temporária do
    admin, entra, é obrigado a trocar e só então usa o sistema."""
    from streamlit.testing.v1 import AppTest

    at, database_url = _abre_gestao_de_usuarios(tmp_path, monkeypatch, "fluxo_completo")

    with get_connection(database_url) as connection:
        create_user(connection, "Maria Souza", "maria@teste.org", "", "senhaAntiga1", "leitor")
        connection.commit()
        maria = get_user_by_email(connection, "maria@teste.org")

    at.run()
    at.checkbox(key=f"confirm_reset_{maria['id']}").check().run()
    at.button(key=f"reset_{maria['id']}").click().run()
    temporaria = at.code[0].value

    leitora = AppTest.from_file("app.py")
    leitora.run()
    leitora.text_input(key="login_email").input("maria@teste.org")
    leitora.text_input(key="login_password").input(temporaria)
    leitora.button(key="FormSubmitter:login_form-Entrar").click().run()
    assert not leitora.exception, leitora.exception

    # troca obrigatória: só a tela de senha, sem menu do sistema
    assert leitora.title[0].value == "Alterar minha senha"
    assert not leitora.radio

    leitora.text_input(key="cp_current").input(temporaria)
    leitora.text_input(key="cp_new").input("MinhaSenhaNova9")
    leitora.text_input(key="cp_confirm").input("MinhaSenhaNova9")
    leitora.button(
        key="FormSubmitter:change_password_form-Salvar nova senha"
    ).click().run()
    assert not leitora.exception, leitora.exception

    assert leitora.radio  # liberada no app
    with get_connection(database_url) as connection:
        depois = get_user_by_email(connection, "maria@teste.org")
        assert bool(depois["must_change_password"]) is False
        assert verify_password("MinhaSenhaNova9", depois["password_hash"], depois["salt"])
        assert not verify_password(temporaria, depois["password_hash"], depois["salt"])


# ---------------------------------------------------------------------------
# Listagem de usuários da tela de gestão
# ---------------------------------------------------------------------------

def test_list_users_traz_admins_primeiro_e_busca_sem_acento(conn):
    create_user(conn, "Zulmira Alves", "zulmira@teste.org", "", "senhaZulmira", "leitor")
    create_user(conn, "José Antônio", "jose@teste.org", "11999", "senhaJose123", "leitor")
    create_user(conn, "Admin Um", "admin1@teste.org", "", "SenhaAdmin1", "admin")
    conn.commit()

    todos = list_users(conn)
    assert [u["email"] for u in todos] == [
        "admin1@teste.org",  # admins primeiro
        "jose@teste.org",    # depois por nome
        "zulmira@teste.org",
    ]
    # nunca devolve material de senha para a tela
    assert "password_hash" not in todos[0].keys()
    assert "salt" not in todos[0].keys()

    assert [u["email"] for u in list_users(conn, "jose")] == ["jose@teste.org"]
    assert [u["email"] for u in list_users(conn, "ANTONIO")] == ["jose@teste.org"]
    assert [u["email"] for u in list_users(conn, "11999")] == ["jose@teste.org"]
    assert count_users(conn, "jose") == 1
    assert count_users(conn) == 3


def test_count_admins_conta_somente_administradores(conn):
    assert count_admins(conn) == 0
    create_user(conn, "Leitor", "leitor@teste.org", "", "senhaLeitor1", "leitor")
    conn.commit()
    assert count_admins(conn) == 0
    create_user(conn, "Admin Um", "admin1@teste.org", "", "SenhaAdmin1", "admin")
    conn.commit()
    assert count_admins(conn) == 1


def test_try_create_account_recusa_email_duplicado(conn):
    ok, erro = try_create_account(
        conn, "Admin Um", "admin1@teste.org", "", "SenhaAdmin1", "admin"
    )
    assert ok is True and erro is None

    ok, erro = try_create_account(
        conn, "Outro", "admin1@teste.org", "", "OutraSenha1", "admin"
    )
    assert ok is False
    assert "mesmo e-mail" in erro or "esse e-mail" in erro
    assert count_admins(conn) == 1


def test_migracao_adiciona_session_version_sem_deslogar_ninguem(tmp_path, bootstrap_secrets):
    """Banco criado antes deste recurso: a coluna entra com 0 em todas as
    contas, que é o mesmo valor que uma sessão aberta antes da migração
    carregaria — a invalidação só começa no primeiro reset."""
    import sqlite3

    db_path = tmp_path / "legado_session.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT NOT NULL,
          email TEXT NOT NULL UNIQUE, phone TEXT, password_hash TEXT NOT NULL,
          salt TEXT NOT NULL, role TEXT NOT NULL,
          must_change_password INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        INSERT INTO users VALUES
          (1,'Admin','admin@x.org','','h','s','admin',0,'2026-01-01');
        """
    )
    raw.commit()
    raw.close()

    database_url = f"sqlite:///{db_path}"
    create_schema(get_engine(database_url))  # dispara a migração

    with get_connection(database_url) as connection:
        admin = get_user_by_email(connection, "admin@x.org")
        assert admin["session_version"] == 0
        assert _session_is_current(connection, app._session_user_view(admin)) is True


def test_reset_e_liberacao_do_bloqueio_sao_commitados_juntos(tmp_path, bootstrap_secrets):
    """Reset, liberação do rate limit e auditoria fecham na mesma transação —
    e ficam visíveis em uma conexão nova, não só na que executou."""
    database_url = f"sqlite:///{tmp_path}/reset_commit.db"
    create_schema(get_engine(database_url))

    with get_connection(database_url) as connection:
        create_user(connection, "Admin Um", "admin1@teste.org", "", "SenhaAdmin1", "admin")
        create_user(connection, "Maria", "maria@teste.org", "", "senhaAntiga1", "leitor")
        connection.commit()
        admin = get_user_by_email(connection, "admin1@teste.org")
        maria = get_user_by_email(connection, "maria@teste.org")
        for _ in range(MAX_LOGIN_ATTEMPTS):
            _register_failed_login(connection, "maria@teste.org")
        temporaria = admin_reset_password(connection, admin, maria["id"])

    with get_connection(database_url) as outra:
        assert _login_locked_until(outra, "maria@teste.org") is None
        assert authenticate(outra, "maria@teste.org", temporaria) is not None
        assert authenticate(outra, "maria@teste.org", "senhaAntiga1") is None
        assert len(list_admin_audit(outra)) == 1


def test_login_bem_sucedido_limpa_o_bloqueio_de_forma_duravel(tmp_path, monkeypatch):
    """A limpeza do contador acontece na tela de login e precisa ser commitada
    antes do rerun — senão o bloqueio voltaria na conexão seguinte."""
    from streamlit.testing.v1 import AppTest

    database_url = f"sqlite:///{tmp_path}/login_commit.db"
    monkeypatch.setattr(app.st, "secrets", _secrets(DATABASE_URL=database_url))
    create_schema(get_engine(database_url))
    with get_connection(database_url) as connection:
        create_user(connection, "Maria", "maria@teste.org", "", "senhaCerta12", "leitor")
        connection.commit()
        for _ in range(MAX_LOGIN_ATTEMPTS - 1):  # ainda sem bloquear
            _register_failed_login(connection, "maria@teste.org")

    at = AppTest.from_file("app.py")
    at.run()
    at.text_input(key="login_email").input("maria@teste.org")
    at.text_input(key="login_password").input("senhaCerta12")
    at.button(key="FormSubmitter:login_form-Entrar").click().run()
    assert not at.exception, at.exception
    assert at.radio  # entrou

    with get_connection(database_url) as outra:
        restantes = outra.execute(
            text("SELECT COUNT(*) AS n FROM login_attempts WHERE email = 'maria@teste.org'")
        ).mappings().first()["n"]
        assert restantes == 0
