#!/usr/bin/env python3
"""
migrate_sqlite_to_pg.py — migração offline SQLite → Postgres.

Uso:
    export DATABASE_URL='postgresql://postgres:SENHA@host:5432/railway'
    python migrate_sqlite_to_pg.py /caminho/para/giftcards.db

Garantias:
  * IDs preservados em todas as tabelas com BIGSERIAL.
  * image_blob (BYTEA) recebe os bytes vindos do BLOB SQLite sem alteração.
  * SEQUENCES resetadas para MAX(id)+1 ao final.
  * Ordem de inserção respeita FKs.
  * Idempotente com --truncate: limpa destino antes de importar.
  * Verificação de contagem por tabela ao final; sai != 0 se divergir.

Segurança:
  * Nada é deletado no SQLite de origem.
  * TRUNCATE só acontece se --truncate for passado explicitamente.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras


# Ordem importa: tabelas-pai antes de filhas (FKs)
TABLE_ORDER = [
    "users",
    "expense_categories",
    "store_settings",
    "customers",
    "gift_cards",
    "discount_coupons",
    "products",
    "product_variants",
    "sales",
    "sale_items",
    "gift_card_redemptions",
    "audit_logs",
    "stock_movements",
    "expenses",
    "whatsapp_campaigns",
    "campaign_logs",
    "store_credits",
    "sale_returns",
    "notifications",
    "store_goals",
    "price_history",
    "restock_orders",
    "restock_order_items",
    "catalog_interest",
    "fashion_calendar",
]

# Colunas BLOB → precisam de bytes() wrap para virar BYTEA
BLOB_COLUMNS = {
    "products": ["image_blob"],
}


def log(msg: str) -> None:
    print(f"[migrate] {msg}", flush=True)


def fail(msg: str, code: int = 1) -> None:
    print(f"[migrate][ERRO] {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def sqlite_tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def pg_tables(cur) -> set[str]:
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema()"
    )
    return {r[0] for r in cur.fetchall()}


def sqlite_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def pg_columns(cur, table: str) -> list[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s "
        "ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def common_columns(
    con: sqlite3.Connection, cur, table: str
) -> list[str]:
    src = set(sqlite_columns(con, table))
    dst = set(pg_columns(cur, table))
    common = sorted(src & dst)
    missing_in_pg = src - dst
    missing_in_sqlite = dst - src
    if missing_in_pg:
        log(f"  (aviso) colunas ausentes no Postgres para {table}: {sorted(missing_in_pg)}")
    if missing_in_sqlite:
        log(f"  (aviso) colunas só no Postgres para {table}: {sorted(missing_in_sqlite)}")
    return common


def truncate_all(cur) -> None:
    log("TRUNCATE CASCADE em todas as tabelas alvo...")
    cur.execute(
        "TRUNCATE " + ", ".join(TABLE_ORDER) + " RESTART IDENTITY CASCADE"
    )


def copy_table(
    con: sqlite3.Connection,
    cur,
    table: str,
    batch_size: int = 500,
) -> tuple[int, int]:
    """Devolve (qtd_origem, qtd_inserida)."""
    src_count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if src_count == 0:
        return 0, 0

    cols = common_columns(con, cur, table)
    if not cols:
        log(f"  {table}: sem colunas comuns — pulando")
        return src_count, 0

    blob_cols = set(BLOB_COLUMNS.get(table, []))
    sel_cols = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))
    insert_sql = (
        f"INSERT INTO {table} ({sel_cols}) VALUES ({placeholders})"
    )

    total = 0
    src_cur = con.execute(f"SELECT {sel_cols} FROM {table} ORDER BY ROWID")
    batch: list[tuple] = []
    while True:
        rows = src_cur.fetchmany(batch_size)
        if not rows:
            break
        for row in rows:
            values = []
            for col, val in zip(cols, row):
                if col in blob_cols and val is not None:
                    # SQLite BLOB chega como bytes; psycopg2 aceita como BYTEA com Binary
                    values.append(psycopg2.Binary(val))
                else:
                    values.append(val)
            batch.append(tuple(values))
        psycopg2.extras.execute_batch(cur, insert_sql, batch, page_size=batch_size)
        total += len(batch)
        batch.clear()

    return src_count, total


def reset_sequences(cur, table: str) -> None:
    """Reseta a sequence da PK para MAX(id)+1."""
    # Heurística: PK geralmente é 'id' com sequence named '<table>_id_seq'
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s "
        "  AND column_default LIKE 'nextval%%'",
        (table,),
    )
    row = cur.fetchone()
    if not row:
        return
    pk_col = row[0]
    cur.execute(
        f"SELECT pg_get_serial_sequence(%s, %s)",
        (table, pk_col),
    )
    seq = cur.fetchone()[0]
    if not seq:
        return
    cur.execute(
        f"SELECT setval(%s, COALESCE((SELECT MAX({pk_col}) FROM {table}), 0) + 1, false)",
        (seq,),
    )


def verify_counts(con: sqlite3.Connection, cur, tables: list[str]) -> list[tuple[str, int, int]]:
    mismatches = []
    for t in tables:
        src = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        dst = cur.fetchone()[0]
        if src != dst:
            mismatches.append((t, src, dst))
    return mismatches


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sqlite_path", type=Path)
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="TRUNCATE CASCADE antes de inserir (destrutivo no destino).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Conecta e lista tabelas mas não insere nada.",
    )
    args = parser.parse_args()

    if not args.sqlite_path.exists():
        fail(f"SQLite não encontrado: {args.sqlite_path}")

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        fail("DATABASE_URL não definida no ambiente.")
    # psycopg2 aceita postgres:// e postgresql://
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]

    log(f"Lendo SQLite: {args.sqlite_path}")
    con = sqlite3.connect(f"file:{args.sqlite_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    log("Conectando ao Postgres...")
    pg = psycopg2.connect(dsn)
    pg.autocommit = False

    try:
        with pg.cursor() as cur:
            src_tables = sqlite_tables(con)
            dst_tables = pg_tables(cur)

            # Filtra ordem pelos que existem em ambos os lados
            to_copy = [t for t in TABLE_ORDER if t in src_tables and t in dst_tables]
            extras_src = src_tables - set(to_copy) - {"sqlite_sequence"}
            if extras_src:
                log(f"(aviso) tabelas no SQLite sem destino: {sorted(extras_src)}")

            log(f"Tabelas a migrar ({len(to_copy)}): {to_copy}")

            if args.dry_run:
                log("DRY-RUN: nada será modificado.")
                return 0

            if args.truncate:
                truncate_all(cur)

            for t in to_copy:
                src, dst = copy_table(con, cur, t)
                log(f"  {t:28s} origem={src:6d}  inserido={dst:6d}")

            log("Resetando sequences...")
            for t in to_copy:
                reset_sequences(cur, t)

            log("Verificando contagens...")
            mismatches = verify_counts(con, cur, to_copy)
            if mismatches:
                pg.rollback()
                for t, s, d in mismatches:
                    log(f"  DIVERGÊNCIA em {t}: origem={s}, destino={d}")
                fail("Migração abortada por divergência de contagem.", code=2)

        pg.commit()
        log("Migração concluída com sucesso.")
        return 0

    except Exception as exc:
        pg.rollback()
        fail(f"Falha: {exc!r}")
        return 3
    finally:
        con.close()
        pg.close()


if __name__ == "__main__":
    sys.exit(main())
