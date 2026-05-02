"""
tests/test_smoke.py — smoke tests Postgres-only.

Objetivo: validar que o db.py e a fachada DBConnection funcionam contra um
Postgres real (Railway, local Docker, ou CI). Cobre:
    1. Abrir conexão, ler information_schema
    2. Contrato db.execute(sql).fetchone() / .fetchall()
    3. insert_returning_id
    4. Upsert via ON CONFLICT
    5. Funções de tempo Postgres nativas (NOW(), INTERVAL, EXTRACT)
    6. BLOB (bytes ↔ BYTEA) round-trip
    7. FK cascade
    8. Rollback em exceção

Uso:
    export DATABASE_URL='postgresql://postgres:SENHA@host:porta/dbname'
    # Banco DEDICADO de teste (os testes criam e removem dados com prefixo TEST-)
    psql $DATABASE_URL -f schema_pg.sql   # antes da primeira execução
    pytest tests/test_smoke.py -v
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))


# Fail-fast se DATABASE_URL ausente — estes testes são Postgres-only
if not os.environ.get("DATABASE_URL"):
    pytest.skip(
        "DATABASE_URL não configurada — estes testes requerem Postgres.",
        allow_module_level=True,
    )

from db import close_db, get_db, insert_returning_id, transaction  # noqa: E402


@pytest.fixture(scope="session")
def app():
    a = Flask(__name__)
    a.config["TESTING"] = True
    return a


@pytest.fixture(autouse=True)
def request_context(app):
    with app.app_context():
        yield
        close_db()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- tests

class TestConnection:
    def test_connects(self):
        db = get_db()
        row = db.execute("SELECT 1 AS n").fetchone()
        assert row["n"] == 1

    def test_schema_applied(self):
        """Valida que schema_pg.sql foi aplicado antes dos testes."""
        db = get_db()
        row = db.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name='gift_cards'"
        ).fetchone()
        assert row is not None, "Schema não aplicado — rode psql -f schema_pg.sql"


class TestCrudContract:
    def test_insert_returning_id(self):
        now = _iso_now()
        pid = insert_returning_id(
            "INSERT INTO products (sku,name,cost_price,sale_price,stock_qty,"
            "created_at,updated_at,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            ("TEST-SMOKE-1", "Smoke A", "10.00", "25.00", 5, now, now, "test"),
        )
        assert isinstance(pid, int) and pid > 0

        db = get_db()
        row = db.execute(
            "SELECT id, sku, name FROM products WHERE id=%s", (pid,)
        ).fetchone()
        assert row is not None
        assert row["sku"] == "TEST-SMOKE-1"

        # cleanup
        db.execute("DELETE FROM products WHERE id=%s", (pid,))
        db.commit()

    def test_fetchall_and_fetchone(self):
        """Contrato legacy: db.execute(sql).fetchone() / .fetchall()."""
        db = get_db()
        rows = db.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY table_name LIMIT 3"
        ).fetchall()
        assert isinstance(rows, list)
        assert len(rows) > 0
        assert "table_name" in rows[0]

        row = db.execute(
            "SELECT 'hello' AS greet"
        ).fetchone()
        assert row["greet"] == "hello"

    def test_fetchone_index_access(self):
        """COUNT(*) retorna dict — acesso via [0] deve falhar em psycopg2."""
        db = get_db()
        row = db.execute("SELECT COUNT(*) AS c FROM products").fetchone()
        # Com RealDictCursor, acesso é por nome
        assert "c" in row
        assert isinstance(row["c"], int)


class TestUpsert:
    def test_on_conflict(self):
        db = get_db()
        now = _iso_now()
        db.execute(
            "INSERT INTO store_settings(key,value,updated_at) VALUES(%s,%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("test_smoke_upsert", "v1", now),
        )
        db.commit()
        row = db.execute(
            "SELECT value FROM store_settings WHERE key=%s", ("test_smoke_upsert",)
        ).fetchone()
        assert row["value"] == "v1"

        db.execute(
            "INSERT INTO store_settings(key,value,updated_at) VALUES(%s,%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("test_smoke_upsert", "v2", now),
        )
        db.commit()
        row = db.execute(
            "SELECT value FROM store_settings WHERE key=%s", ("test_smoke_upsert",)
        ).fetchone()
        assert row["value"] == "v2"

        # cleanup
        db.execute("DELETE FROM store_settings WHERE key=%s", ("test_smoke_upsert",))
        db.commit()


class TestPgNativeTime:
    def test_interval(self):
        """Traduz equivalente de datetime('now','-30 days') em PG."""
        db = get_db()
        row = db.execute(
            "SELECT (NOW() - INTERVAL '30 days') AS cutoff"
        ).fetchone()
        assert row["cutoff"] is not None

    def test_extract_dow_and_hour(self):
        db = get_db()
        row = db.execute(
            "SELECT EXTRACT(DOW FROM NOW()::timestamp)::int AS dow, "
            "EXTRACT(HOUR FROM NOW()::timestamptz AT TIME ZONE 'America/Sao_Paulo')::int AS hr"
        ).fetchone()
        assert 0 <= row["dow"] <= 6
        assert 0 <= row["hr"] <= 23


class TestBlob:
    def test_bytea_roundtrip(self):
        now = _iso_now()
        payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100 + b"\xffEND"
        pid = insert_returning_id(
            "INSERT INTO products (sku,name,cost_price,sale_price,stock_qty,"
            "image_blob,image_mime,created_at,updated_at,created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                "TEST-BLOB-1", "Blob", "0", "0", 0,
                psycopg2_binary(payload), "image/png",
                now, now, "test",
            ),
        )
        db = get_db()
        row = db.execute(
            "SELECT image_blob, image_mime FROM products WHERE id=%s", (pid,)
        ).fetchone()
        # psycopg2 devolve BYTEA como memoryview
        blob = row["image_blob"]
        blob_bytes = bytes(blob) if not isinstance(blob, bytes) else blob
        assert blob_bytes == payload
        assert row["image_mime"] == "image/png"

        db.execute("DELETE FROM products WHERE id=%s", (pid,))
        db.commit()


def psycopg2_binary(data: bytes):
    """Wrap bytes em psycopg2.Binary para evitar tentativa de decode como str."""
    import psycopg2
    return psycopg2.Binary(data)


class TestForeignKeyCascade:
    def test_delete_gift_card_cascades_redemptions(self):
        now = _iso_now()
        gc_id = insert_returning_id(
            "INSERT INTO gift_cards (code_hash,code_last4,initial_value,current_balance,"
            "status,created_at,updated_at,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            ("hashtest_smoke_1", "9999", "100.00", "100.00", "active", now, now, "test"),
        )
        db = get_db()
        db.execute(
            "INSERT INTO gift_card_redemptions(gift_card_id,amount,operator_name,created_at) "
            "VALUES(%s,%s,%s,%s)",
            (gc_id, "10.00", "op", now),
        )
        db.commit()

        n = db.execute(
            "SELECT COUNT(*) AS n FROM gift_card_redemptions WHERE gift_card_id=%s",
            (gc_id,),
        ).fetchone()["n"]
        assert n == 1

        db.execute("DELETE FROM gift_cards WHERE id=%s", (gc_id,))
        db.commit()

        n = db.execute(
            "SELECT COUNT(*) AS n FROM gift_card_redemptions WHERE gift_card_id=%s",
            (gc_id,),
        ).fetchone()["n"]
        assert n == 0


class TestTransactionRollback:
    def test_rollback_on_exception(self):
        sku = "TEST-SMOKE-TX-ROLLBACK"
        now = _iso_now()
        with pytest.raises(RuntimeError):
            with transaction() as db:
                insert_returning_id(
                    "INSERT INTO products (sku,name,cost_price,sale_price,stock_qty,"
                    "created_at,updated_at,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (sku, "TX", "0", "0", 0, now, now, "test"),
                )
                raise RuntimeError("forçar rollback")

        db = get_db()
        row = db.execute(
            "SELECT id FROM products WHERE sku=%s", (sku,)
        ).fetchone()
        assert row is None, "produto não deveria ter sido persistido"
