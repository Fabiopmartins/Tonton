"""
tests/test_parity.py — paridade funcional SQLite ↔ Postgres.

Objetivo: validar que o mesmo conjunto de operações produz o mesmo resultado
semântico nos dois dialetos. NÃO é um teste exaustivo do app — foca em:
  1. CRUD com preservação de IDs
  2. INSERT ... RETURNING id (via DBFacade.insert_returning_id)
  3. ON CONFLICT DO UPDATE (upsert de store_settings)
  4. Funções de tempo portáveis (Dialect.last_n_days_expr, dow_expr, hour_local_expr)
  5. BLOB (bytes ↔ BYTEA)
  6. FK cascade
  7. table_columns / table_exists portáveis

Uso:
    # SQLite (in-memory)
    pytest tests/test_parity.py -v

    # Postgres (requer DATABASE_URL apontando pra banco vazio ou dedicado de teste)
    DATABASE_URL=postgresql://postgres:...@host:5432/test pytest tests/test_parity.py -v

IMPORTANTE: rodar em banco DEDICADO. Os testes criam e removem dados.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask

# Garantir que o app raiz está no path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from db import DB, ENGINE, close_conn, get_conn, init_db_from_schema, is_postgres, transaction  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def app():
    """Flask app mínimo só para contexto de request."""
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture(autouse=True)
def request_context(app):
    """Envelopa cada teste em app_context + limpa conexão ao fim."""
    with app.app_context():
        yield
        close_conn()


@pytest.fixture(scope="session", autouse=True)
def bootstrap_schema(app):
    """Cria schema no início da sessão de testes."""
    with app.app_context():
        init_db_from_schema()
    yield
    # Teardown: limpa tabelas de teste (opcional)
    with app.app_context():
        try:
            _purge_test_rows()
            close_conn(None)
        except Exception:
            pass


def _purge_test_rows():
    """Remove dados criados pelos testes (filtra por marcadores)."""
    conn = get_conn()
    with conn.begin():
        for table, col, value in [
            ("products", "sku", "TEST-%"),
            ("customers", "name", "Test %"),
            ("store_settings", "key", "test_%"),
            ("catalog_interest", "notes", "parity-test%"),
        ]:
            conn.execute(
                __import__("sqlalchemy").text(f"DELETE FROM {table} WHERE {col} LIKE :v"),
                {"v": value},
            )


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Testes
# --------------------------------------------------------------------------- #
class TestDialectInfo:
    def test_engine_selected(self):
        name = ENGINE.dialect.name
        assert name in ("sqlite", "postgresql")

    def test_table_exists(self):
        assert DB.table_exists("products")
        assert DB.table_exists("gift_cards")
        assert not DB.table_exists("definitely_not_a_table")

    def test_table_columns(self):
        cols = DB.table_columns("products")
        expected = {"id", "name", "sku", "sale_price", "image_blob", "image_mime"}
        assert expected.issubset(cols)


class TestCrudPreservesData:
    def test_insert_returning_id(self):
        now = _iso_now()
        new_id = DB.insert_returning_id(
            "INSERT INTO products (sku,name,cost_price,sale_price,stock_qty,created_at,updated_at,created_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("TEST-A001", "Test Produto A", "10.00", "25.00", 5, now, now, "test"),
        )
        assert isinstance(new_id, int) and new_id > 0

        row = DB.fetchone("SELECT id, sku, name FROM products WHERE id=?", (new_id,))
        assert row is not None
        assert row["sku"] == "TEST-A001"
        assert row["name"] == "Test Produto A"

    def test_update_and_delete(self):
        now = _iso_now()
        pid = DB.insert_returning_id(
            "INSERT INTO products (sku,name,cost_price,sale_price,stock_qty,created_at,updated_at,created_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("TEST-A002", "Test Produto B", "5.00", "15.00", 1, now, now, "test"),
        )
        DB.execute(
            "UPDATE products SET stock_qty=?, updated_at=? WHERE id=?",
            (99, now, pid),
        )
        row = DB.fetchone("SELECT stock_qty FROM products WHERE id=?", (pid,))
        assert int(row["stock_qty"]) == 99

        DB.execute("DELETE FROM products WHERE id=?", (pid,))
        assert DB.fetchone("SELECT id FROM products WHERE id=?", (pid,)) is None

    def test_scalar(self):
        now = _iso_now()
        DB.insert_returning_id(
            "INSERT INTO products (sku,name,cost_price,sale_price,stock_qty,created_at,updated_at,created_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("TEST-A003", "Test Produto C", "1.00", "2.00", 0, now, now, "test"),
        )
        count = DB.scalar("SELECT COUNT(*) FROM products WHERE sku LIKE ?", ("TEST-A%",))
        assert count >= 1


class TestUpsert:
    def test_on_conflict_do_update(self):
        """store_settings usa ON CONFLICT(key) DO UPDATE — sintaxe ANSI."""
        now = _iso_now()
        DB.execute(
            "INSERT INTO store_settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("test_key_1", "v1", now),
        )
        row = DB.fetchone("SELECT value FROM store_settings WHERE key=?", ("test_key_1",))
        assert row["value"] == "v1"

        DB.execute(
            "INSERT INTO store_settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("test_key_1", "v2", now),
        )
        row = DB.fetchone("SELECT value FROM store_settings WHERE key=?", ("test_key_1",))
        assert row["value"] == "v2"


class TestDialectHelpers:
    def test_last_n_days_expr(self):
        # Insere um registro com created_at=agora; deve entrar no filtro de 30d
        now = _iso_now()
        DB.insert_returning_id(
            "INSERT INTO catalog_interest(product_id,customer_phone,notes,created_at) "
            "VALUES(?,?,?,?)",
            (None, "+5511999999999", "parity-test-now", now),
        )
        sql = (
            "SELECT COUNT(*) FROM catalog_interest "
            "WHERE notes LIKE 'parity-test%' AND "
            + DB.dialect.last_n_days_expr("created_at", 30)
        )
        count = DB.scalar(sql)
        assert count >= 1

    def test_dow_and_hour_expr(self):
        # Só valida que as expressões compilam em ambos dialetos
        now = _iso_now()
        sql = (
            "SELECT "
            + DB.dialect.dow_expr("created_at")
            + " AS dow, "
            + DB.dialect.hour_local_expr("created_at")
            + " AS hr FROM catalog_interest WHERE notes LIKE 'parity-test%' LIMIT 1"
        )
        row = DB.fetchone(sql)
        if row:
            assert 0 <= int(row["dow"]) <= 6
            assert 0 <= int(row["hr"]) <= 23


class TestBlob:
    def test_blob_roundtrip(self):
        now = _iso_now()
        payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100 + b"\xffEND"
        pid = DB.insert_returning_id(
            "INSERT INTO products (sku,name,cost_price,sale_price,stock_qty,"
            "image_blob,image_mime,created_at,updated_at,created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("TEST-BLOB-1", "Test Blob", "0", "0", 0, payload, "image/png", now, now, "test"),
        )
        row = DB.fetchone("SELECT image_blob, image_mime FROM products WHERE id=?", (pid,))
        assert row is not None
        blob = row["image_blob"]
        # Em Postgres vem como memoryview/bytes; em SQLite como bytes
        blob_bytes = bytes(blob) if not isinstance(blob, bytes) else blob
        assert blob_bytes == payload
        assert row["image_mime"] == "image/png"


class TestForeignKeyCascade:
    def test_delete_gift_card_cascades_redemptions(self):
        now = _iso_now()
        gc_id = DB.insert_returning_id(
            "INSERT INTO gift_cards (code_hash,code_last4,initial_value,current_balance,"
            "status,created_at,updated_at,created_by) VALUES (?,?,?,?,?,?,?,?)",
            ("hashtest_parity_1", "9999", "100.00", "100.00", "active", now, now, "test"),
        )
        DB.execute(
            "INSERT INTO gift_card_redemptions(gift_card_id,amount,operator_name,created_at) "
            "VALUES(?,?,?,?)",
            (gc_id, "10.00", "op", now),
        )
        assert DB.scalar(
            "SELECT COUNT(*) FROM gift_card_redemptions WHERE gift_card_id=?", (gc_id,)
        ) == 1
        DB.execute("DELETE FROM gift_cards WHERE id=?", (gc_id,))
        assert DB.scalar(
            "SELECT COUNT(*) FROM gift_card_redemptions WHERE gift_card_id=?", (gc_id,)
        ) == 0


class TestTransactionRollback:
    def test_rollback_on_exception(self):
        now = _iso_now()
        sku = "TEST-TX-ROLLBACK-1"
        with pytest.raises(RuntimeError):
            with transaction() as db:
                db.insert_returning_id(
                    "INSERT INTO products (sku,name,cost_price,sale_price,stock_qty,"
                    "created_at,updated_at,created_by) VALUES (?,?,?,?,?,?,?,?)",
                    (sku, "TX", "0", "0", 0, now, now, "test"),
                )
                raise RuntimeError("force rollback")
        # Produto NÃO deve estar no banco
        assert DB.fetchone("SELECT id FROM products WHERE sku=?", (sku,)) is None
