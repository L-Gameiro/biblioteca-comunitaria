"""Heartbeat do Supabase: mantém o projeto gratuito ativo e confere o RLS.

O Supabase gratuito pausa o projeto depois de 7 dias sem atividade. Este
script, rodado pelo GitHub Actions (.github/workflows/heartbeat.yml), grava
um instante em public.heartbeat e confere que nenhuma tabela do schema public
está sem Row Level Security — sem RLS, a API REST automática do Supabase
expõe a tabela a quem tiver a chave pública do projeto.

A URL do banco vem da variável de ambiente SUPABASE_POOLER_URL. Nenhuma
mensagem deste script contém a URL ou parte dela: o log do Actions de um
repositório público é público, e a mensagem de erro do psycopg2 traz o host e
o usuário do banco. Por isso os erros são relatados só pela classe e pelo
código do Postgres.

Saída: 0 se o heartbeat foi gravado e todas as tabelas têm RLS; 1 caso
contrário.
"""

import os
import re
import sys
import time

import psycopg2

URL_ENV_VAR = "SUPABASE_POOLER_URL"

# Espera antes de cada tentativa de conexão: três tentativas, com espera
# crescente — o pooler do Supabase às vezes recusa a primeira conexão de um
# projeto que estava ocioso.
CONNECT_WAITS_SECONDS = (0, 10, 30)
CONNECT_TIMEOUT_SECONDS = 15

HEARTBEAT_SQL = "UPDATE public.heartbeat SET last_ping = now() WHERE id = 1"
TABLES_WITHOUT_RLS_SQL = """
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind IN ('r', 'p')
      AND NOT c.relrowsecurity
    ORDER BY c.relname
"""


def libpq_url(url: str) -> str:
    """O psycopg2 só aceita postgresql:// ou postgres://: tira o "+psycopg2"
    que a SQLAlchemy usa para declarar o driver."""
    return re.sub(r"^(postgres(?:ql)?)\+psycopg2://", r"\1://", url)


def describe(exc: Exception) -> str:
    """A falha sem a mensagem do driver, que pode conter host e usuário."""
    code = getattr(exc, "pgcode", None)
    return f"{type(exc).__name__}" + (f" (código {code})" if code else "")


def connect(url: str):
    last_error = None
    for attempt, wait in enumerate(CONNECT_WAITS_SECONDS, start=1):
        if wait:
            time.sleep(wait)
        try:
            return psycopg2.connect(url, connect_timeout=CONNECT_TIMEOUT_SECONDS)
        except psycopg2.Error as exc:
            last_error = exc
            print(
                f"Tentativa {attempt} de {len(CONNECT_WAITS_SECONDS)} de conexão "
                f"falhou: {describe(exc)}.",
                file=sys.stderr,
            )
    raise SystemExit(
        f"Não foi possível conectar ao banco: {describe(last_error)}."
    )


def main() -> int:
    url = os.environ.get(URL_ENV_VAR, "").strip()
    if not url:
        print(f"A variável de ambiente {URL_ENV_VAR} não está definida.", file=sys.stderr)
        return 1

    conn = connect(libpq_url(url))
    try:
        with conn.cursor() as cur:
            cur.execute(HEARTBEAT_SQL)
            updated = cur.rowcount
        conn.commit()
        if updated != 1:
            print(
                f"O heartbeat alterou {updated} linha(s), e não 1: confira se "
                "public.heartbeat tem a linha id = 1.",
                file=sys.stderr,
            )
            return 1
        print("Heartbeat gravado.")

        with conn.cursor() as cur:
            cur.execute(TABLES_WITHOUT_RLS_SQL)
            unprotected = [row[0] for row in cur.fetchall()]
        if unprotected:
            print(
                "Tabelas do schema public sem RLS: " + ", ".join(unprotected)
                + ". Ative o RLS nelas no SQL Editor do Supabase "
                "(ALTER TABLE public.<tabela> ENABLE ROW LEVEL SECURITY).",
                file=sys.stderr,
            )
            return 1
        print("Todas as tabelas do schema public têm RLS.")
        return 0
    except psycopg2.Error as exc:
        conn.rollback()
        print(f"Falha no banco: {describe(exc)}.", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
