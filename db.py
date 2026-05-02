"""
db.py — camada de dados Postgres-only via psycopg2.

Princípios:
  * Zero abstração sobre driver. Código é psycopg2 puro.
  * Cursor default: RealDictCursor → rows já vêm como dict com acesso por chave.
  * Transação por request: commit em sucesso no teardown, rollback em erro.
  * `transaction()` context manager para blocos que precisam de atomicidade
    explícita dentro de uma rota.
  * Sem fallback para SQLite. DATABASE_URL é obrigatória.

Contrato de compatibilidade:
  A fachada `DBConnection` expõe `.execute(sql, params)` que retorna um cursor
  pronto com `.fetchone() / .fetchall() / [0]`, idêntico ao contrato legado do
  sqlite3.Connection. Isso permite manter o código de rotas sem refactor em
  massa. Placeholders já foram convertidos de `?` para `%s` na reescrita.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg2
import psycopg2.extensions
import psycopg2.extras
from flask import g
from psycopg2 import IntegrityError  # noqa: F401  (reexport)


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL não configurada. Esta aplicação requer Postgres."
    )
# Railway/Heroku usam postgres:// em alguns lugares; psycopg2 aceita ambos,
# mas normalizamos por segurança.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]


class RowDict(dict):
    """
    Dict que também suporta acesso por índice numérico, emulando sqlite3.Row.

    Motivação: código legado faz `row[0]` para COUNT(*) e outras agregações
    sem alias nomeado. psycopg2 com RealDictCursor retorna dict puro, que
    não suporta `row[0]`. Este RowDict mantém os dois contratos.

    row["col"]  → acesso por chave (preserva comportamento dict)
    row[0]       → acesso por posição (primeiro valor, segundo, etc)
    """

    __slots__ = ("_keys_ordered",)

    def __init__(self, mapping=None, **kwargs):
        if mapping is None:
            super().__init__(**kwargs)
        else:
            super().__init__(mapping, **kwargs)
        # Preserva ordem de inserção para acesso por índice
        self._keys_ordered = list(super().keys())

    def __getitem__(self, key):
        # Se for inteiro, retorna o N-ésimo valor (compat sqlite3.Row)
        if isinstance(key, int):
            if key < 0 or key >= len(self._keys_ordered):
                raise IndexError(f"row index out of range: {key}")
            return super().__getitem__(self._keys_ordered[key])
        # Caso contrário, acesso normal por chave
        return super().__getitem__(key)

    def keys(self):
        return self._keys_ordered


def _to_rowdict(row) -> "RowDict | None":
    """Converte RealDictRow (ou None) em RowDict."""
    if row is None:
        return None
    return RowDict(row)


class _CursorResult:
    """
    Wrapper que sustenta o cursor vivo até o caller consumir, e fecha
    automaticamente no garbage-collect. Expõe o contrato sqlite3 clássico:
        db.execute(sql).fetchone()
        db.execute(sql).fetchall()
        for row in db.execute(sql):
    """

    __slots__ = ("_cur",)

    def __init__(self, cur: psycopg2.extensions.cursor) -> None:
        self._cur = cur

    def fetchone(self) -> "RowDict | None":
        return _to_rowdict(self._cur.fetchone())

    def fetchall(self) -> list[RowDict]:
        return [RowDict(r) for r in self._cur.fetchall()]

    def fetchmany(self, size: int | None = None) -> list[RowDict]:
        rows = self._cur.fetchmany(size) if size else self._cur.fetchmany()
        return [RowDict(r) for r in rows]

    def __iter__(self):
        for r in self._cur:
            yield RowDict(r)

    def __getitem__(self, idx):
        # Suporta padrão db.execute(sql).fetchone()[0] e db.execute(sql)[0]
        rows = self.fetchall()
        return rows[idx]

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self) -> None:
        raise AttributeError(
            "Postgres não suporta lastrowid. Use insert_returning_id() ou "
            "adicione RETURNING id ao INSERT."
        )

    def close(self) -> None:
        try:
            self._cur.close()
        except Exception:
            pass

    def __del__(self):
        self.close()


class DBConnection:
    """
    Fachada sobre psycopg2.connection que expõe `.execute()` retornando cursor
    idiomático sqlite3. Mantém o código das rotas legível.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: psycopg2.extensions.connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple | list | dict | None = None) -> _CursorResult:
        """
        Executa SQL e retorna resultado.

        Se a query falhar, faz rollback automático da transação. Isso evita
        que um erro de uma query corrompa a transação inteira e cause o
        erro secundário 'current transaction is aborted' em queries
        seguintes (ex: get_current_user no inject_helpers ao renderizar
        a página de erro 500).

        O rollback NÃO suprime a exceção original — ela é re-lançada para
        que o handler de erro do Flask tome conhecimento normalmente.
        """
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params)
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            try:
                cur.close()
            except Exception:
                pass
            raise
        return _CursorResult(cur)

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        try:
            with self._conn.cursor() as cur:
                cur.executemany(sql, seq)
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    @property
    def raw(self) -> psycopg2.extensions.connection:
        """Acesso ao driver nativo quando necessário."""
        return self._conn


def _connect() -> DBConnection:
    """Abre conexão nova. RealDictCursor devolve rows como dict."""
    conn = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=10,
    )
    conn.autocommit = False
    return DBConnection(conn)


def get_db() -> DBConnection:
    """Retorna conexão do request atual. Cria sob demanda."""
    if "db" not in g:
        g.db = _connect()
    return g.db


@contextmanager
def transaction() -> Iterator[DBConnection]:
    """
    Bloco transacional explícito. Commit em sucesso, rollback em erro.
    """
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def close_db(error: Any | None = None) -> None:
    """
    Teardown do Flask. Rollback em erro, commit em sucesso para pegar qualquer
    transação implícita pendente.
    """
    db = g.pop("db", None)
    if db is None:
        return
    try:
        if error is None:
            db.commit()
        else:
            db.rollback()
    except Exception:
        pass
    finally:
        try:
            db.close()
        except Exception:
            pass


def insert_returning_id(sql: str, params: tuple | list | dict | None = None) -> int:
    """
    Executa INSERT e devolve o id gerado. Adiciona RETURNING id se ausente.
    """
    if "returning" not in sql.lower():
        sql = sql.rstrip().rstrip(";") + " RETURNING id"
    db = get_db()
    with db.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return int(row["id"]) if row else 0

