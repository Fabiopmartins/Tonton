"""
tonton · Lookbook
═══════════════════════════════════════════════════════════════════════════
Combinações curatoriais (looks) com produtos relacionados.

Modelo:
  looks            - 1 look = 1 combinação (nome, descrição, capa, slug)
  look_products    - N produtos por look, com posição

Fluxo curatorial:
  1. Lorena cria look "Outono frio" + descrição editorial + foto da combinação
  2. Adiciona 2-4 produtos do catálogo que compõem o look
  3. Publica → vira página pública /catalogo/look/<slug>
  4. Compartilha link no Instagram → cliente clica em peças individuais

Integração com app.py — apenas 3 linhas após `app = create_app()`:

    from lookbook import lookbook_bp, init_lookbook_db
    init_lookbook_db(app)
    app.register_blueprint(lookbook_bp)

Tudo o mais (rotas, schema, helpers) fica isolado neste arquivo.
═══════════════════════════════════════════════════════════════════════════
"""

import re
import unicodedata
from datetime import datetime, timezone

from flask import (
    Blueprint, current_app, render_template, request, redirect, url_for,
    flash, abort, Response, session
)

# Helpers DB vêm do módulo db.py (mesmos que o app.py importa).
from db import get_db, transaction

# Helpers de auth/CSRF/normalização ficam no nível do módulo app.py.
from app import (
    login_required,
    validate_csrf_or_abort,
    utc_now_iso,
    _normalize_uploaded_image,  # pra rotacionar EXIF de fotos
)


lookbook_bp = Blueprint(
    "lookbook",
    __name__,
    template_folder="templates",
)


# ─────────────────────────────────────────────────────────────────────────
# 1 · SCHEMA — auto-criação no startup
# ─────────────────────────────────────────────────────────────────────────

def _table_exists(db, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = %s",
        (name,)
    ).fetchone() is not None


def init_lookbook_db(app) -> None:
    """Cria tabelas de lookbook se não existirem. Idempotente."""
    with app.app_context():
        db = get_db()

        if not _table_exists(db, "looks"):
            current_app.logger.info("Lookbook: criando tabela looks…")
            db.execute("""
                CREATE TABLE looks (
                    id              SERIAL PRIMARY KEY,
                    slug            TEXT UNIQUE NOT NULL,
                    name            TEXT NOT NULL,
                    description     TEXT,
                    eyebrow         TEXT,
                    image_blob      BYTEA,
                    image_mime      TEXT,
                    image_version   INTEGER NOT NULL DEFAULT 1,
                    is_published    SMALLINT NOT NULL DEFAULT 0,
                    sort_order      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT
                )
            """)
            db.execute(
                "CREATE INDEX idx_looks_published_order "
                "ON looks (is_published, sort_order, id DESC)"
            )

        if not _table_exists(db, "look_products"):
            current_app.logger.info("Lookbook: criando tabela look_products…")
            db.execute("""
                CREATE TABLE look_products (
                    look_id     INTEGER NOT NULL REFERENCES looks(id) ON DELETE CASCADE,
                    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    position    INTEGER NOT NULL DEFAULT 0,
                    note        TEXT,
                    PRIMARY KEY (look_id, product_id)
                )
            """)
            db.execute(
                "CREATE INDEX idx_look_products_pos "
                "ON look_products (look_id, position, product_id)"
            )

        db.commit()


# ─────────────────────────────────────────────────────────────────────────
# 2 · HELPERS
# ─────────────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Versão simples de slugify pt-BR."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    text = re.sub(r"[-\s]+", "-", text)
    return text[:80] or "look"


def _ensure_unique_slug(db, base: str, exclude_id: int | None = None) -> str:
    candidate = base
    n = 2
    while True:
        if exclude_id:
            row = db.execute(
                "SELECT 1 FROM looks WHERE slug=%s AND id<>%s",
                (candidate, exclude_id)
            ).fetchone()
        else:
            row = db.execute(
                "SELECT 1 FROM looks WHERE slug=%s",
                (candidate,)
            ).fetchone()
        if not row:
            return candidate
        candidate = f"{base}-{n}"
        n += 1


def _fetch_look_products(db, look_id: int):
    return db.execute("""
        SELECT p.id, p.name, p.slug, p.sale_price, p.cost_price,
               lp.position, lp.note,
               (SELECT id FROM product_images
                WHERE product_id=p.id ORDER BY id LIMIT 1) AS img_id
        FROM look_products lp
        JOIN products p ON p.id = lp.product_id
        WHERE lp.look_id=%s
        ORDER BY lp.position, p.id
    """, (look_id,)).fetchall()


# ─────────────────────────────────────────────────────────────────────────
# 3 · ROTAS ADMIN
# ─────────────────────────────────────────────────────────────────────────

@lookbook_bp.route("/looks")
@login_required
def list_looks():
    db = get_db()
    looks = db.execute("""
        SELECT l.id, l.slug, l.name, l.eyebrow, l.is_published,
               l.image_version, l.created_at, l.sort_order,
               (SELECT COUNT(*) FROM look_products WHERE look_id=l.id) AS product_count
        FROM looks l
        ORDER BY l.is_published DESC, l.sort_order, l.created_at DESC
    """).fetchall()
    return render_template("looks_list.html", looks=looks)


@lookbook_bp.route("/looks/new", methods=["GET", "POST"])
@login_required
def create_look():
    if request.method == "POST":
        validate_csrf_or_abort()
        return _save_look(None)

    db = get_db()
    products = db.execute("""
        SELECT id, name, sale_price, sku, slug,
               (SELECT id FROM product_images WHERE product_id=p.id ORDER BY id LIMIT 1) AS img_id
        FROM products p
        WHERE is_active=1
        ORDER BY name
    """).fetchall()
    return render_template("look_form.html",
                           look=None, look_products=[], products=products)


@lookbook_bp.route("/looks/<int:look_id>/edit", methods=["GET", "POST"])
@login_required
def edit_look(look_id: int):
    db = get_db()
    look = db.execute("SELECT * FROM looks WHERE id=%s", (look_id,)).fetchone()
    if not look:
        abort(404)

    if request.method == "POST":
        validate_csrf_or_abort()
        return _save_look(look_id)

    look_products = _fetch_look_products(db, look_id)
    products = db.execute("""
        SELECT id, name, sale_price, sku, slug,
               (SELECT id FROM product_images WHERE product_id=p.id ORDER BY id LIMIT 1) AS img_id
        FROM products p
        WHERE is_active=1
        ORDER BY name
    """).fetchall()
    return render_template("look_form.html",
                           look=look, look_products=look_products, products=products)


def _save_look(look_id):
    """Lógica compartilhada entre create e edit."""
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Dê um nome ao look.", "error")
        return redirect(request.url)

    description = (request.form.get("description") or "").strip()
    eyebrow = (request.form.get("eyebrow") or "").strip()
    is_published = 1 if request.form.get("is_published") else 0

    # Produtos selecionados (lista de ids; ordem na lista = posição)
    product_ids = request.form.getlist("product_ids[]")
    product_ids = [int(pid) for pid in product_ids if pid.isdigit()]

    # Imagem da capa (opcional)
    image_blob = None
    image_mime = None
    file = request.files.get("cover_image")
    if file and file.filename:
        raw = file.read()
        try:
            image_blob, image_mime = _normalize_uploaded_image(raw, file.mimetype)
        except Exception:
            image_blob, image_mime = raw, file.mimetype or "image/jpeg"

    with transaction() as db:
        if look_id:
            slug_base = _slugify(request.form.get("slug") or name)
            slug = _ensure_unique_slug(db, slug_base, exclude_id=look_id)

            if image_blob is not None:
                db.execute("""
                    UPDATE looks SET name=%s, slug=%s, description=%s, eyebrow=%s,
                                     is_published=%s, image_blob=%s, image_mime=%s,
                                     image_version=image_version+1, updated_at=%s
                    WHERE id=%s
                """, (name, slug, description, eyebrow, is_published,
                      image_blob, image_mime, utc_now_iso(), look_id))
            else:
                db.execute("""
                    UPDATE looks SET name=%s, slug=%s, description=%s, eyebrow=%s,
                                     is_published=%s, updated_at=%s
                    WHERE id=%s
                """, (name, slug, description, eyebrow, is_published,
                      utc_now_iso(), look_id))

            db.execute("DELETE FROM look_products WHERE look_id=%s", (look_id,))
            new_id = look_id
        else:
            slug_base = _slugify(request.form.get("slug") or name)
            slug = _ensure_unique_slug(db, slug_base)
            row = db.execute("""
                INSERT INTO looks (slug, name, description, eyebrow,
                                   is_published, image_blob, image_mime,
                                   created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (slug, name, description, eyebrow, is_published,
                  image_blob, image_mime, utc_now_iso())).fetchone()
            new_id = row["id"]

        for pos, pid in enumerate(product_ids):
            db.execute("""
                INSERT INTO look_products (look_id, product_id, position)
                VALUES (%s, %s, %s)
                ON CONFLICT (look_id, product_id) DO UPDATE SET position=EXCLUDED.position
            """, (new_id, pid, pos))

        flash(f"Look “{name}” {'publicado' if is_published else 'salvo como rascunho'}.", "success")
        return redirect(url_for("lookbook.edit_look", look_id=new_id))


@lookbook_bp.route("/looks/<int:look_id>/delete", methods=["POST"])
@login_required
def delete_look(look_id: int):
    validate_csrf_or_abort()
    with transaction() as db:
        row = db.execute("SELECT name FROM looks WHERE id=%s", (look_id,)).fetchone()
        if not row:
            abort(404)
        db.execute("DELETE FROM looks WHERE id=%s", (look_id,))
        flash(f"Look “{row['name']}” removido.", "success")
    return redirect(url_for("lookbook.list_looks"))


@lookbook_bp.route("/looks/<int:look_id>/toggle-publish", methods=["POST"])
@login_required
def toggle_publish_look(look_id: int):
    validate_csrf_or_abort()
    with transaction() as db:
        row = db.execute("SELECT is_published, name FROM looks WHERE id=%s", (look_id,)).fetchone()
        if not row:
            abort(404)
        new_state = 0 if row["is_published"] else 1
        db.execute("UPDATE looks SET is_published=%s, updated_at=%s WHERE id=%s",
                   (new_state, utc_now_iso(), look_id))
        flash(f"Look “{row['name']}” {'publicado' if new_state else 'despublicado'}.", "success")
    return redirect(url_for("lookbook.edit_look", look_id=look_id))


# ─────────────────────────────────────────────────────────────────────────
# 4 · ROTAS PÚBLICAS
# ─────────────────────────────────────────────────────────────────────────

@lookbook_bp.route("/looks/<int:look_id>/image")
def look_image(look_id: int):
    """Serve a foto da capa do look (cache-busted via ?v=)."""
    db = get_db()
    row = db.execute(
        "SELECT image_blob, image_mime FROM looks WHERE id=%s",
        (look_id,)
    ).fetchone()
    if not row or not row["image_blob"]:
        abort(404)
    return Response(
        bytes(row["image_blob"]),
        mimetype=row["image_mime"] or "image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"}
    )


@lookbook_bp.route("/catalogo/look/<slug>")
def public_look(slug: str):
    """Página pública de um look — combinação curatorial."""
    db = get_db()
    look = db.execute(
        "SELECT * FROM looks WHERE slug=%s AND is_published=1",
        (slug,)
    ).fetchone()
    if not look:
        abort(404)

    products = _fetch_look_products(db, look["id"])

    # Outros looks publicados (até 3) pra "veja também"
    other_looks = db.execute("""
        SELECT id, slug, name, eyebrow, image_version
        FROM looks
        WHERE is_published=1 AND id<>%s
        ORDER BY sort_order, id DESC LIMIT 3
    """, (look["id"],)).fetchall()

    return render_template("public_look.html",
                           look=look,
                           products=products,
                           other_looks=other_looks)
