"""
blueprints/products.py — Blueprint de produtos.

Escopo (34 rotas, todas com @login_required exceto onde anotado):

  CRUD de produtos (5)
    GET  /products                                     → list_products
    GET/POST /products/new                             → create_product
    GET  /products/<pid>                               → product_detail
    POST /products/<pid>/delete                        → delete_product
    GET/POST /products/<pid>/edit                      → edit_product

  Operações em produtos (5)
    POST /products/<pid>/toggle-featured               → toggle_product_featured
    POST /products/<pid>/stock                         → adjust_stock
    GET  /products/<pid>/qr                            → product_qr
    POST /products/qr-batch                            → product_qr_batch
    GET  /products/scan-lookup                         → scan_lookup
    GET  /api/products/lookup                          → api_product_lookup

  Imagens (8)
    GET  /products/<pid>/image                         → product_image
    GET  /products/<pid>/gallery/<img_id>              → product_gallery_image
    GET  /products/<pid>/gallery                       → product_gallery
    POST /products/<pid>/gallery/upload                → product_gallery_upload
    POST /products/<pid>/gallery/<img_id>/delete       → product_gallery_delete
    POST /products/<pid>/gallery/<img_id>/rotate       → product_gallery_rotate
    POST /products/<pid>/gallery/<img_id>/set-primary  → product_gallery_set_primary
    GET  /public/product-gallery/<pid>/<img_id>        → public_product_gallery_image (PÚBLICO)

  Variantes & estoque (8)
    GET  /products/<pid>/variants.json                 → product_variants_json
    GET/POST /products/<pid>/variants                  → product_variants_view
    POST /products/<pid>/variants/<vid>/stock          → variant_stock
    POST /products/<pid>/variants/<vid>/stock-quick    → variant_stock_quick
    GET  /products/<pid>/variants/<vid>/history.json   → variant_stock_history
    POST /products/<pid>/variants/<vid>/promo          → variant_promo
    POST /products/<pid>/variants/promo-bulk           → variant_promo_bulk
    GET  /products/<pid>/price-history                 → product_price_history

  Etiquetas / labels (2)
    GET  /products/labels                              → product_labels_batch_ui
    GET/POST /products/labels-niimbot.pdf              → product_labels_niimbot

  Reposição (4)
    GET  /restock                                      → restock_list
    POST /restock/new                                  → create_restock
    GET  /restock/<oid>                                → restock_detail
    POST /restock/<oid>/status                         → update_restock_status

  Análise ABC (1)
    GET  /produtos/abc                                 → produtos_abc

Notas (Onda 2 — Etapa G):

  1. Endpoints PRESERVAM nomes globais via aliases registrados em
     init_products_blueprint(). Templates seguem usando url_for(\'list_products\'),
     url_for(\'product_detail\'), etc.

  2. _product_or_404 era nested em register_v3_routes (app.py:3090). Foi
     replicada INLINE neste módulo, igual ao _customer_or_404 em
     blueprints/customers.py.

  3. /public/product-gallery/<pid>/<img_id> é pública (sem @login_required).
     Mantida em products por coesão de domínio (galeria de produto), não
     pela classificação pública/privada.

  4. Substituições aplicadas: bare `app` → `current_app` (app.config,
     smtp_is_configured) — necessário porque o blueprint não tem `app` no
     escopo léxico da fábrica original.

  5. Código das views é EXTRAÇÃO VERBATIM. Apenas decorators alterados.

  6. _patch_edit_product_for_price_history (em app.py) ainda funciona porque
     intercepta `app.view_functions["edit_product"]` — esse alias é registrado
     por init_products_blueprint(). Ordem: primeiro o blueprint, depois o
     patch de price-history (que continua em create_app).
"""
from __future__ import annotations

import io
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import (
    Blueprint, current_app, request, render_template, redirect, url_for,
    abort, Response, jsonify, session, flash, send_file,
)
from PIL import Image, ImageDraw, ImageFont

from app import (
    BASE_DIR,
    _clean_optional,
    _compute_avg_fixed_expense,
    _ensure_product_qr_tokens,
    _make_thumbnail,
    _margin_color,
    _mm_setting,
    _normalize_uploaded_image,
    _pricing_metrics,
    _product_image_uri,
    _product_qr_payload,
    _read_clothing_fields,
    _rotate_image_blob,
    _sync_legacy_to_gallery,
    _table_exists,
    effective_price,
    ensure_csrf_token,
    format_money,
    get_card_value_font,
    get_current_user,
    get_setting,
    get_ui_font,
    has_active_promo,
    login_required,
    parse_money,
    resync_product_stock,
    sanitize_filename_part,
    send_pdf_download,
    size_sort_key,
    slugify,
    utc_now,
    utc_now_iso,
    validate_csrf_or_abort,
)
from db import IntegrityError, get_db, insert_returning_id, transaction


products_bp = Blueprint("products", __name__, template_folder="../templates")


# ─────────────────────────────────────────────────────────────────────────
# Helper privado replicado verbatim de app.py:3090 (era nested em
# register_v3_routes; nested functions não são exportáveis).
# ─────────────────────────────────────────────────────────────────────────
def _product_or_404(product_id):
    p = get_db().execute("SELECT * FROM products WHERE id=%s", (product_id,)).fetchone()
    if not p:
        abort(404)
    return p


@products_bp.route("/products", endpoint="list_products")
@login_required
def list_products():
    db = get_db()
    q        = request.args.get("q","").strip()
    cat      = request.args.get("category","").strip()
    low      = request.args.get("low_stock","")
    sql      = "SELECT * FROM products WHERE is_active=1"
    params   = []
    if q:
        sql += " AND (name ILIKE %s OR sku ILIKE %s OR barcode ILIKE %s OR description ILIKE %s)"
        params += [f"%{q}%"]*4
    if cat:
        sql += " AND category=%s"; params.append(cat)
    if low == "1":
        sql += " AND stock_qty<=stock_min"
    sql += " ORDER BY name"
    products   = db.execute(sql, params).fetchall()
    categories = db.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL ORDER BY category").fetchall()
    stats      = db.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN stock_qty<=stock_min THEN 1 ELSE 0 END) as low_count,
               COALESCE(SUM(stock_qty*CAST(cost_price AS DOUBLE PRECISION)),0) as cost_val,
               COALESCE(SUM(stock_qty*CAST(sale_price AS DOUBLE PRECISION)),0) as sale_val
        FROM products WHERE is_active=1
    """).fetchone()
    target_m = get_setting("target_margin_pct","60")
    min_alert = get_setting("min_margin_alert_pct", "30")
    return render_template("products.html", products=products, categories=categories,
                           stats=stats, q=q, selected_category=cat, low_stock=low,
                           target_margin=target_m, settings_alert=min_alert,
                           _margin_color=_margin_color)



@products_bp.route("/products/new", methods=["GET","POST"], endpoint="create_product")
@login_required
def create_product():
    if request.method == "POST":
        validate_csrf_or_abort()
        name = request.form.get("name","").strip()
        if not name:
            flash("Nome é obrigatório.","danger"); return redirect(url_for("create_product"))
        sku   = _clean_optional(request.form.get("sku"))
        desc  = _clean_optional(request.form.get("description"))
        cat   = _clean_optional(request.form.get("category"))
        barcd = _clean_optional(request.form.get("barcode"))
        unit  = (request.form.get("unit","un") or "un").strip() or "un"
        stock = int(request.form.get("stock_qty","0") or 0)
        smin  = int(request.form.get("stock_min","2") or 2)
        try:
            cost  = str(parse_money(request.form.get("cost_price","0")))
            sale  = str(parse_money(request.form.get("sale_price","0")))
        except Exception:
            flash("Valor inválido.","danger"); return redirect(url_for("create_product"))
        # Photo upload
        image_blob = None; image_mime = None
        uploaded = request.files.get("image")
        if uploaded and uploaded.filename:
            raw = uploaded.read()
            if raw:
                try:
                    image_blob = _make_thumbnail(raw)
                    image_mime = "image/jpeg"
                except Exception as e:
                    flash(f"Não foi possível processar a imagem: {e}. "
                          f"Tente converter para JPG antes de enviar.", "warning")

        tgt_margin = request.form.get("target_margin_pct","").strip() or get_setting("target_margin_pct","60")
        qt = secrets.token_urlsafe(14)
        now = utc_now_iso(); user = get_current_user()
        brand = _clean_optional(request.form.get("brand"))
        size  = _clean_optional(request.form.get("size"))
        color = _clean_optional(request.form.get("color"))
        ncm = _clean_optional(request.form.get("ncm"))
        cfop = _clean_optional(request.form.get("cfop"))
        origin_code = _clean_optional(request.form.get("origin_code"))

        # Onda 1 · Ficha técnica de roupa
        try:
            (composition, care_wash, fabric_type, fabric_weight_gsm,
             fit, length_class, country_of_origin) = _read_clothing_fields(request.form)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("create_product"))

        db = get_db()
        if sku and db.execute("SELECT id FROM products WHERE sku=%s", (sku,)).fetchone():
            flash("Já existe um produto com este SKU.","danger")
            return redirect(url_for("create_product"))
        if barcd and db.execute("SELECT id FROM products WHERE barcode=%s", (barcd,)).fetchone():
            flash("Já existe um produto com este código de barras.","danger")
            return redirect(url_for("create_product"))

        try:
            with transaction() as db:
                db.execute("""
                    INSERT INTO products(sku,name,description,category,cost_price,sale_price,
                        stock_qty,stock_min,unit,barcode,qr_token,created_at,updated_at,created_by,
                        brand,size,color,target_margin_pct,image_blob,image_mime,ncm,cfop,origin_code,
                        composition,care_wash,fabric_type,fabric_weight_gsm,fit,length_class,country_of_origin)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s)
                """, (sku,name,desc,cat,cost,sale,stock,smin,unit,barcd,qt,now,now,user["display_name"],
                      brand,size,color,tgt_margin,image_blob,image_mime,ncm,cfop,origin_code,
                      composition,care_wash,fabric_type,fabric_weight_gsm,fit,length_class,country_of_origin))

                # Recupera o ID do produto recém-criado (precisa para sync da galeria e estoque)
                new_pid = db.execute("SELECT id FROM products WHERE qr_token=%s",(qt,)).fetchone()["id"]

                # Gera slug único para a URL pública do produto.
                # Se já existe outro com mesmo slug, sufixa com -2, -3, etc.
                base_slug = slugify(name)
                candidate = base_slug
                n = 2
                while db.execute(
                    "SELECT 1 FROM products WHERE slug=%s AND id<>%s LIMIT 1",
                    (candidate, new_pid)
                ).fetchone():
                    candidate = f"{base_slug}-{n}"
                    n += 1
                db.execute(
                    "UPDATE products SET slug=%s WHERE id=%s",
                    (candidate, new_pid)
                )

                # Sincroniza foto legacy → galeria (catálogo público lê da galeria)
                if image_blob and image_mime:
                    _sync_legacy_to_gallery(db, new_pid, image_blob, image_mime)

                if stock > 0:
                    db.execute("INSERT INTO stock_movements(product_id,type,qty,reason,operator_name,created_at) VALUES(%s,%s,%s,%s,%s,%s)",
                               (new_pid,"in",stock,"Estoque inicial",user["display_name"],now))

                # ── Variantes em lote (matriz tamanho × cor) ──
                var_sizes  = request.form.getlist("var_size[]")
                var_colors = request.form.getlist("var_color[]")
                var_qtys   = request.form.getlist("var_qty[]")
                if var_sizes:
                    pid_row = db.execute("SELECT id FROM products WHERE qr_token=%s",(qt,)).fetchone()
                    pid = pid_row["id"]
                    sku_base = sku or f"P{pid:06d}"
                    any_variant_added = False
                    for vsz, vcl, vqt in zip(var_sizes, var_colors, var_qtys):
                        try:
                            vqty = int(vqt or 0)
                        except (TypeError, ValueError):
                            vqty = 0
                        if vqty <= 0:
                            continue
                        vsz_clean = (vsz or "").strip() or None
                        vcl_clean = (vcl or "").strip() or None
                        if not vsz_clean and not vcl_clean:
                            continue
                        sz_part = (vsz_clean or "X")[:3].upper().replace(" ","")
                        cl_part = (vcl_clean or "X")[:3].upper().replace(" ","")
                        v_sku = f"{sku_base}-{sz_part}-{cl_part}-{secrets.token_hex(2).upper()}"
                        v_qr  = secrets.token_urlsafe(10)
                        db.execute("""INSERT INTO product_variants(product_id,size,color,sku,qr_token,
                            stock_qty,stock_min,cost_price,sale_price,is_active,created_at)
                            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s)""",
                            (pid, vsz_clean, vcl_clean, v_sku, v_qr,
                             vqty, smin, cost, sale, now))
                        db.execute("""INSERT INTO stock_movements(product_id,type,qty,reason,operator_name,created_at)
                            VALUES(%s,'in',%s,%s,%s,%s)""",
                            (pid, vqty, f"Estoque inicial · variante {sz_part}/{cl_part}",
                             user["display_name"], now))
                        any_variant_added = True
                    # Sincroniza products.stock_qty com a soma das variantes
                    if any_variant_added:
                        resync_product_stock(db, pid)
        except IntegrityError as exc:
            msg = str(exc)
            if "products.barcode" in msg:
                flash("Já existe um produto com este código de barras.","danger")
            elif "products.sku" in msg:
                flash("Já existe um produto com este SKU.","danger")
            else:
                flash("Não foi possível salvar o produto. Verifique se SKU e código de barras não estão duplicados.","danger")
            return redirect(url_for("create_product"))
        flash(f"Produto «{name}» criado!","success")
        return redirect(url_for("list_products"))
    categories = get_db().execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL ORDER BY category").fetchall()
    return render_template("create_product.html", categories=categories,
                           target_margin=get_setting("target_margin_pct","60"))



@products_bp.route("/products/<int:pid>", endpoint="product_detail")
@login_required
def product_detail(pid):
    p = _product_or_404(pid)
    db = get_db()
    moves = db.execute("SELECT * FROM stock_movements WHERE product_id=%s ORDER BY created_at DESC LIMIT 60", (pid,)).fetchall()
    cost = float(p["cost_price"]); sale = float(p["sale_price"])
    margin = ((sale-cost)/sale*100) if sale>0 else 0
    overhead = _compute_avg_fixed_expense(get_db())
    tgt = p["target_margin_pct"] if "target_margin_pct" in p.keys() and p["target_margin_pct"] else get_setting("target_margin_pct","60")
    metrics = _pricing_metrics(cost, tgt, sale, overhead)
    image_uri = _product_image_uri(p)
    return render_template("product_detail.html", product=p, movements=moves,
                           margin=margin, margin_color=_margin_color(margin),
                           target_margin=tgt, metrics=metrics, image_uri=image_uri)



@products_bp.route("/products/<int:pid>/delete", methods=["POST"], endpoint="delete_product")
@login_required
def delete_product(pid):
    validate_csrf_or_abort()
    product = _product_or_404(pid)
    now = utc_now_iso()

    try:
        with transaction() as db:
            dependency_counts = {}
            for table_name in ("sale_items", "sale_returns", "restock_order_items"):
                if _table_exists(db, table_name):
                    dependency_counts[table_name] = db.execute(
                        f"SELECT COUNT(*) FROM {table_name} WHERE product_id=%s",
                        (pid,),
                    ).fetchone()[0]
                else:
                    dependency_counts[table_name] = 0

            has_locked_history = any(int(v or 0) > 0 for v in dependency_counts.values())

            if has_locked_history:
                db.execute(
                    "UPDATE products SET is_active=0, updated_at=%s WHERE id=%s",
                    (now, pid),
                )
                flash(
                    "Produto com histórico vinculado. Ele não pode ser apagado do banco, mas foi removido da lista ao ser desativado.",
                    "warning",
                )
                return redirect(url_for("list_products"))

            db.execute("DELETE FROM products WHERE id=%s", (pid,))
            flash(f"Produto «{product['name']}» apagado.", "success")
            return redirect(url_for("list_products"))
    except IntegrityError:
        # Race condition: a dependency appeared between the count and the delete,
        # or a FK we don't explicitly check (e.g. stock_movements without CASCADE
        # on some older DBs) blocked the delete. Fall back to soft-delete.
        try:
            with transaction() as db:
                db.execute(
                    "UPDATE products SET is_active=0, updated_at=%s WHERE id=%s",
                    (now, pid),
                )
            flash(
                "Produto com vínculos no banco. Ele foi desativado e deixou de aparecer na lista de produtos ativos.",
                "warning",
            )
        except Exception:
            current_app.logger.exception("delete_product: fallback deactivation failed for pid=%s", pid)
            flash("Não foi possível apagar nem desativar o produto. Tente novamente em alguns segundos.", "danger")
        return redirect(url_for("list_products"))
    except Exception:
        # Unexpected error: DB locked, disk full, permissions, etc.
        # Log full traceback and show a friendly message — never a stack trace.
        current_app.logger.exception("delete_product: unexpected error for pid=%s", pid)
        flash("Erro ao apagar o produto. O administrador foi notificado nos logs.", "danger")
        return redirect(url_for("list_products"))



@products_bp.route("/products/<int:pid>/edit", methods=["GET","POST"], endpoint="edit_product")
@login_required
def edit_product(pid):
    p = _product_or_404(pid)
    if request.method == "POST":
        validate_csrf_or_abort()
        name = request.form.get("name","").strip()
        if not name:
            flash("Nome é obrigatório.","danger")
            return redirect(url_for("edit_product", pid=pid))
        try:
            cost = str(parse_money(request.form.get("cost_price","0")))
            sale = str(parse_money(request.form.get("sale_price","0")))
        except Exception:
            flash("Valor inválido.","danger"); return redirect(url_for("edit_product",pid=pid))
        brand = _clean_optional(request.form.get("brand"))
        size  = _clean_optional(request.form.get("size"))
        color = _clean_optional(request.form.get("color"))
        ncm = _clean_optional(request.form.get("ncm"))
        cfop = _clean_optional(request.form.get("cfop"))
        origin_code = _clean_optional(request.form.get("origin_code"))
        sku = _clean_optional(request.form.get("sku"))
        description = _clean_optional(request.form.get("description"))
        category = _clean_optional(request.form.get("category"))
        barcode = _clean_optional(request.form.get("barcode"))
        unit = (request.form.get("unit","un") or "un").strip() or "un"
        tgt_m = request.form.get("target_margin_pct","").strip() or get_setting("target_margin_pct","60")
        is_featured = 1 if request.form.get("is_featured") == "1" else 0

        # Onda 1 · Ficha técnica de roupa
        try:
            (composition, care_wash, fabric_type, fabric_weight_gsm,
             fit, length_class, country_of_origin) = _read_clothing_fields(request.form)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("edit_product", pid=pid))

        # Photo update
        image_updates = ""
        image_params  = []
        new_image_blob = None
        new_image_mime = None
        uploaded = request.files.get("image")
        if uploaded and uploaded.filename:
            raw = uploaded.read()
            if raw:
                try:
                    new_image_blob = _make_thumbnail(raw)
                    new_image_mime = "image/jpeg"
                    image_updates = ", image_blob=%s, image_mime=%s"
                    image_params  = [new_image_blob, new_image_mime]
                except Exception as e:
                    flash(f"Não foi possível processar a imagem: {e}. "
                          f"Tente converter para JPG antes de enviar.", "warning")
        remove_image = request.form.get("remove_image") == "1"
        if remove_image:
            image_updates = ", image_blob=NULL, image_mime=NULL"
            image_params  = []

        db = get_db()
        if sku and db.execute("SELECT id FROM products WHERE sku=%s AND id<>%s", (sku, pid)).fetchone():
            flash("Já existe outro produto com este SKU.","danger")
            return redirect(url_for("edit_product", pid=pid))
        if barcode and db.execute("SELECT id FROM products WHERE barcode=%s AND id<>%s", (barcode, pid)).fetchone():
            flash("Já existe outro produto com este código de barras.","danger")
            return redirect(url_for("edit_product", pid=pid))

        try:
            with transaction() as db:
                db.execute(f"""UPDATE products SET sku=%s,name=%s,description=%s,category=%s,
                    cost_price=%s,sale_price=%s,stock_min=%s,unit=%s,barcode=%s,
                    brand=%s,size=%s,color=%s,ncm=%s,cfop=%s,origin_code=%s,
                    target_margin_pct=%s,is_featured=%s,
                    composition=%s,care_wash=%s,fabric_type=%s,fabric_weight_gsm=%s,
                    fit=%s,length_class=%s,country_of_origin=%s,
                    updated_at=%s{image_updates} WHERE id=%s""",
                    [sku, name, description, category, cost, sale,
                     int(request.form.get("stock_min","2") or 2),
                     unit, barcode,
                     brand, size, color, ncm, cfop, origin_code, tgt_m, is_featured,
                     composition, care_wash, fabric_type, fabric_weight_gsm,
                     fit, length_class, country_of_origin,
                     utc_now_iso()] + image_params + [pid])

                # Sincroniza foto legacy → galeria (catálogo público lê da galeria)
                if new_image_blob and new_image_mime:
                    _sync_legacy_to_gallery(db, pid, new_image_blob, new_image_mime)
                elif remove_image:
                    # Remove também a foto primária da galeria que veio do legacy
                    # (preserva fotos de variantes que tenham `color` definido)
                    db.execute(
                        "DELETE FROM product_images "
                        "WHERE product_id=%s AND color IS NULL AND is_primary=1",
                        (pid,)
                    )
        except IntegrityError as exc:
            msg = str(exc)
            if "products.barcode" in msg:
                flash("Já existe outro produto com este código de barras.","danger")
            elif "products.sku" in msg:
                flash("Já existe outro produto com este SKU.","danger")
            else:
                flash("Não foi possível atualizar o produto. Verifique se SKU e código de barras não estão duplicados.","danger")
            return redirect(url_for("edit_product", pid=pid))

        flash("Produto atualizado.","success")
        return redirect(url_for("product_detail",pid=pid))
    image_uri = _product_image_uri(p)
    return render_template("edit_product.html", product=p,
                           image_uri=image_uri,
                           categories=get_db().execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL").fetchall())



@products_bp.route("/products/<int:pid>/toggle-featured", methods=["POST"], endpoint="toggle_product_featured")
@login_required
def toggle_product_featured(pid):
    """Liga/desliga o destaque (`is_featured`) de um produto.

    Aceita formulário simples ou retorna JSON quando solicitado, para que
    a estrelinha na lista de produtos faça toggle sem precisar recarregar
    a página. Múltiplos produtos podem estar em destaque ao mesmo tempo
    (catálogo prioriza os mais recentes)."""
    validate_csrf_or_abort()
    p = _product_or_404(pid)
    new_value = 0 if int(p.get("is_featured") or 0) == 1 else 1
    with transaction() as db:
        db.execute(
            "UPDATE products SET is_featured=%s, updated_at=%s WHERE id=%s",
            (new_value, utc_now_iso(), pid)
        )
    # Resposta JSON para chamadas AJAX (estrelinha clicável)
    if request.headers.get("Accept", "").startswith("application/json") \
       or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return {"ok": True, "is_featured": new_value, "pid": pid}
    # Fallback: redireciona para a origem
    flash(
        "Peça marcada como destaque." if new_value
        else "Peça removida dos destaques.",
        "success"
    )
    return redirect(request.referrer or url_for("list_products"))



@products_bp.route("/products/<int:pid>/stock", methods=["POST"], endpoint="adjust_stock")
@login_required
def adjust_stock(pid):
    validate_csrf_or_abort()
    p = _product_or_404(pid)
    mtype = request.form.get("type","in")
    qty   = int(request.form.get("qty","0") or 0)
    if qty <= 0:
        flash("Quantidade inválida.","danger"); return redirect(url_for("product_detail",pid=pid))
    reason = request.form.get("reason","").strip() or None
    user   = get_current_user()
    delta  = qty if mtype in ("in","return") else -qty
    with transaction() as db:
        db.execute("INSERT INTO stock_movements(product_id,type,qty,reason,operator_name,created_at) VALUES(%s,%s,%s,%s,%s,%s)",
                   (pid,mtype,qty,reason,user["display_name"],utc_now_iso()))
        db.execute("UPDATE products SET stock_qty=stock_qty+%s,updated_at=%s WHERE id=%s", (delta,utc_now_iso(),pid))
    flash(f"Estoque ajustado ({'+' if delta>0 else ''}{delta}).","success")
    return redirect(url_for("product_detail",pid=pid))



@products_bp.route("/products/<int:pid>/qr", endpoint="product_qr")
@login_required
def product_qr(pid):
    p = _product_or_404(pid)
    if not p["qr_token"]:
        with transaction() as db:
            db.execute("UPDATE products SET qr_token=%s, updated_at=%s WHERE id=%s", (secrets.token_urlsafe(18), utc_now_iso(), pid))
        p = _product_or_404(pid)
    import qrcode as qrlib
    qr = qrlib.QRCode(box_size=10, border=3)
    qr.add_data(_product_qr_payload(p))
    qr.make(fit=True)
    img = qr.make_image(fill_color="#191714", back_color="white")
    buf = io.BytesIO(); img.save(buf,format="PNG"); buf.seek(0)
    return send_file(buf, mimetype="image/png",
                     download_name=f"qr-{sanitize_filename_part(p['name'])}.png")



@products_bp.route("/products/qr-batch", methods=["POST"], endpoint="product_qr_batch")
@login_required
def product_qr_batch():
    validate_csrf_or_abort()
    ids = request.form.getlist("ids")
    if not ids:
        flash("Selecione ao menos um produto.","warning"); return redirect(url_for("list_products"))
    with transaction() as db:
        _ensure_product_qr_tokens(db)
    db = get_db()
    import qrcode as qrlib
    cols=3; cw=280; ch=340
    prods = [db.execute("SELECT * FROM products WHERE id=%s",(int(i),)).fetchone() for i in ids if i]
    prods = [p for p in prods if p]
    rows  = (len(prods)+cols-1)//cols
    # Modo grayscale "L" — convertemos para 1-bit no final com dither.
    # Garante legibilidade em impressora térmica (sem cinzas, sem dourado).
    sheet = Image.new("L",(cols*cw, rows*ch), 255)
    draw  = ImageDraw.Draw(sheet)
    for idx, p in enumerate(prods):
        ox = (idx%cols)*cw; oy = (idx//cols)*ch
        # QR pixel-perfect: cores em STRING (qrcode>=8 trata int como RGB
        # e back_color=255 vira (255,0,0)/vermelho → some no threshold) e
        # escala inteira (box_size=1 → reescala por múltiplo inteiro, sem
        # fracionamento que vira borrão).
        qr = qrlib.QRCode(box_size=1, border=2)
        qr.add_data(_product_qr_payload(p))
        qr.make(fit=True)
        qri_min = qr.make_image(fill_color="black", back_color="white").convert("L")
        target = 200
        mod_total = qri_min.size[0]
        mod_scale = max(1, target // mod_total)
        qr_side  = mod_total * mod_scale
        qri = qri_min.resize((qr_side, qr_side), Image.Resampling.NEAREST)
        sheet.paste(qri,(ox+40 + (200 - qr_side)//2, oy+20 + (200 - qr_side)//2))
        fn = get_ui_font(15, bold=True)
        fp = get_card_value_font(17)
        # TUDO PRETO PURO (fill=0). Em térmica, qualquer cor vira dithering.
        draw.text((ox+140,oy+238),p["name"][:28],                              fill=0, font=fn, anchor="mm")
        draw.text((ox+140,oy+265),f"R$ {format_money(p['sale_price'])}",        fill=0, font=fp, anchor="mm")
        if p["sku"]:
            draw.text((ox+140,oy+288),p["sku"],                                  fill=0,         anchor="mm")
        # Borda mais escura para visibilidade após threshold
        draw.rectangle([ox+4,oy+4,ox+cw-5,oy+ch-5],outline=180,width=1)
    # Conversão final para 1-bit puro (threshold 200)
    sheet_1bit = sheet.point(lambda v: 0 if v < 200 else 255, mode="1")
    buf = io.BytesIO()
    sheet_1bit.save(buf,format="PDF",resolution=203.0)
    return send_pdf_download(buf.getvalue(), "etiquetas-male.pdf")



@products_bp.route("/products/scan-lookup", endpoint="scan_lookup")
@login_required
def scan_lookup():
    token = request.args.get("token","").strip()
    if token.startswith("TONTON-PROD-"):
        token = token.replace("TONTON-PROD-", "", 1)
    if not token:
        return {"error":"token missing","ok":False}, 400
    db = get_db()
    # Try qr_token first, then SKU, barcode, or raw code
    p = (db.execute("SELECT * FROM products WHERE qr_token=%s", (token,)).fetchone()
         or db.execute("SELECT * FROM products WHERE sku=%s OR barcode=%s", (token,token)).fetchone())
    if not p:
        return {"error":"Produto não encontrado","ok":False}, 404
    return {"ok":True, "product":{
        "id": p["id"], "name": p["name"], "sku": p["sku"] or "",
        "price": p["sale_price"], "sale_price": p["sale_price"],
        "cost_price": p["cost_price"], "stock": p["stock_qty"], "unit": p["unit"],
    }}

# Alias for GPT-version compatibility


@products_bp.route("/api/products/lookup", endpoint="api_product_lookup")
@login_required
def api_product_lookup():
    code = request.args.get("code","").strip()
    return redirect(url_for("scan_lookup", token=code))



@products_bp.route("/products/<int:pid>/image", endpoint="product_image")
@login_required
def product_image(pid):
    db = get_db()
    # Prefere foto primária da galeria; fallback para image_blob legado
    img = db.execute(
        "SELECT image_blob, image_mime FROM product_images "
        "WHERE product_id=%s AND is_primary=1 LIMIT 1", (pid,)
    ).fetchone()
    if not img:
        img = db.execute(
            "SELECT image_blob, image_mime FROM product_images "
            "WHERE product_id=%s ORDER BY sort_order ASC, id ASC LIMIT 1", (pid,)
        ).fetchone()
    if not img:
        img = db.execute(
            "SELECT image_blob, image_mime FROM products WHERE id=%s", (pid,)
        ).fetchone()
    if not img or not img["image_blob"]:
        abort(404)
    return send_file(io.BytesIO(img["image_blob"]), mimetype=img["image_mime"] or "image/jpeg")

# ─── v10: GALERIA — servir foto específica da galeria ───────


@products_bp.route("/products/<int:pid>/gallery/<int:img_id>", endpoint="product_gallery_image")
@login_required
def product_gallery_image(pid, img_id):
    db = get_db()
    img = db.execute(
        "SELECT image_blob, image_mime FROM product_images "
        "WHERE id=%s AND product_id=%s",
        (img_id, pid)
    ).fetchone()
    if not img:
        abort(404)
    return send_file(io.BytesIO(img["image_blob"]), mimetype=img["image_mime"] or "image/jpeg")

# ─── v10: GALERIA — listar fotos do produto (admin) ─────────


@products_bp.route("/products/<int:pid>/gallery", endpoint="product_gallery")
@login_required
def product_gallery(pid):
    db = get_db()
    prod = db.execute("SELECT * FROM products WHERE id=%s", (pid,)).fetchone()
    if not prod: abort(404)
    images = db.execute(
        "SELECT id, color, image_mime, sort_order, is_primary, image_version, created_at "
        "FROM product_images WHERE product_id=%s "
        "ORDER BY is_primary DESC, sort_order ASC, id ASC", (pid,)
    ).fetchall()
    # Cores cadastradas nas variantes (para o select de upload)
    colors = db.execute(
        "SELECT DISTINCT color FROM product_variants "
        "WHERE product_id=%s AND color IS NOT NULL AND color != '' "
        "ORDER BY color", (pid,)
    ).fetchall()
    ensure_csrf_token()
    return render_template("product_gallery.html",
                           product=prod, images=images,
                           variant_colors=[c["color"] for c in colors])

# ─── v10: GALERIA — upload de foto ───────────────────────────


@products_bp.route("/products/<int:pid>/gallery/upload", methods=["POST"], endpoint="product_gallery_upload")
@login_required
def product_gallery_upload(pid):
    validate_csrf_or_abort()
    db = get_db()
    prod = db.execute("SELECT id FROM products WHERE id=%s", (pid,)).fetchone()
    if not prod: abort(404)

    # v10.4: redirect adaptável — tela de variantes pode chamar essa rota
    back = (request.form.get("back") or "").strip()
    def _back_url():
        if back == "variants":
            return url_for("product_variants_view", pid=pid)
        return url_for("product_gallery", pid=pid)

    # Limite de 5 fotos
    count = db.execute(
        "SELECT COUNT(*) AS n FROM product_images WHERE product_id=%s", (pid,)
    ).fetchone()["n"]
    if count >= 5:
        flash("Limite de 5 fotos por produto atingido. Remova uma para subir outra.", "warning")
        return redirect(_back_url())

    f = request.files.get("image")
    if not f or not f.filename:
        flash("Selecione uma imagem.", "danger")
        return redirect(_back_url())

    # Valida MIME (aceita HEIC/HEIF de iPhone — convertido para JPEG no _normalize)
    mime = (f.mimetype or "").lower()
    accepted_mimes = ("image/jpeg", "image/jpg", "image/png", "image/webp",
                      "image/heic", "image/heif")
    if mime not in accepted_mimes:
        flash("Formato não suportado. Use JPG, PNG, WebP ou HEIC.", "danger")
        return redirect(_back_url())

    blob = f.read()
    # Limite de 20 MB no upload bruto. O _normalize_uploaded_image redimensiona
    # para no máx 1600px e recomprime — na prática a imagem final fica em <500KB.
    max_upload_mb = 20
    if len(blob) > max_upload_mb * 1024 * 1024:
        flash(f"Imagem maior que {max_upload_mb} MB. "
              f"Tente enviar em qualidade média no celular.", "danger")
        return redirect(_back_url())
    if len(blob) == 0:
        flash("Arquivo vazio.", "danger")
        return redirect(_back_url())

    # Auto-corrige orientação EXIF, redimensiona e recomprime.
    # Se a imagem for inválida/corrompida, mostra mensagem útil.
    try:
        blob, mime = _normalize_uploaded_image(blob, mime)
    except ValueError as e:
        flash(f"Não foi possível processar a imagem: {e}", "danger")
        return redirect(_back_url())

    color = (request.form.get("color") or "").strip() or None
    now = utc_now_iso()

    with transaction() as db2:
        # Próximo sort_order
        next_order = db2.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n "
            "FROM product_images WHERE product_id=%s", (pid,)
        ).fetchone()["n"]
        # Se ainda não há foto primária, esta vira primária
        has_primary = db2.execute(
            "SELECT 1 FROM product_images WHERE product_id=%s AND is_primary=1",
            (pid,)
        ).fetchone() is not None
        db2.execute(
            "INSERT INTO product_images "
            "(product_id, color, image_blob, image_mime, sort_order, is_primary, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (pid, color, blob, mime, next_order, 0 if has_primary else 1, now)
        )
    flash("Foto adicionada.", "success")
    return redirect(_back_url())

# ─── v10: GALERIA — deletar foto ─────────────────────────────


@products_bp.route("/products/<int:pid>/gallery/<int:img_id>/delete", methods=["POST"], endpoint="product_gallery_delete")
@login_required
def product_gallery_delete(pid, img_id):
    validate_csrf_or_abort()
    db = get_db()
    img = db.execute(
        "SELECT id, is_primary FROM product_images WHERE id=%s AND product_id=%s",
        (img_id, pid)
    ).fetchone()
    if not img:
        abort(404)
    with transaction() as db2:
        db2.execute("DELETE FROM product_images WHERE id=%s", (img_id,))
        # Se a deletada era primária, promove a próxima
        if img["is_primary"]:
            next_img = db2.execute(
                "SELECT id FROM product_images WHERE product_id=%s "
                "ORDER BY sort_order ASC, id ASC LIMIT 1", (pid,)
            ).fetchone()
            if next_img:
                db2.execute(
                    "UPDATE product_images SET is_primary=1 WHERE id=%s",
                    (next_img["id"],)
                )
    flash("Foto removida.", "success")
    return redirect(url_for("product_gallery", pid=pid))

# ─── v10.2: GALERIA — rotacionar foto 90° horário ────────────


@products_bp.route("/products/<int:pid>/gallery/<int:img_id>/rotate", methods=["POST"], endpoint="product_gallery_rotate")
@login_required
def product_gallery_rotate(pid, img_id):
    validate_csrf_or_abort()
    db = get_db()
    img = db.execute(
        "SELECT id, image_blob, image_mime FROM product_images "
        "WHERE id=%s AND product_id=%s",
        (img_id, pid)
    ).fetchone()
    if not img:
        abort(404)
    new_blob, new_mime = _rotate_image_blob(
        bytes(img["image_blob"]), img["image_mime"], degrees=90
    )
    with transaction() as db2:
        db2.execute(
            "UPDATE product_images SET image_blob=%s, image_mime=%s, "
            "image_version = image_version + 1 WHERE id=%s",
            (new_blob, new_mime, img_id)
        )
    flash("Foto rotacionada.", "success")
    return redirect(url_for("product_gallery", pid=pid))

# ─── v10: GALERIA — marcar como primária ─────────────────────


@products_bp.route("/products/<int:pid>/gallery/<int:img_id>/set-primary", methods=["POST"], endpoint="product_gallery_set_primary")
@login_required
def product_gallery_set_primary(pid, img_id):
    validate_csrf_or_abort()
    db = get_db()
    img = db.execute(
        "SELECT id FROM product_images WHERE id=%s AND product_id=%s",
        (img_id, pid)
    ).fetchone()
    if not img:
        abort(404)
    with transaction() as db2:
        db2.execute(
            "UPDATE product_images SET is_primary=0 WHERE product_id=%s",
            (pid,)
        )
        db2.execute(
            "UPDATE product_images SET is_primary=1 WHERE id=%s",
            (img_id,)
        )
    flash("Foto definida como capa.", "success")
    return redirect(url_for("product_gallery", pid=pid))

# ─── v10: PÚBLICO — servir foto da galeria sem autenticação ──


@products_bp.route("/public/product-gallery/<int:pid>/<int:img_id>", endpoint="public_product_gallery_image")
def public_product_gallery_image(pid, img_id):
    db = get_db()
    img = db.execute(
        "SELECT pi.image_blob, pi.image_mime FROM product_images pi "
        "JOIN products p ON p.id = pi.product_id "
        "WHERE pi.id=%s AND pi.product_id=%s AND p.is_active=1",
        (img_id, pid)
    ).fetchone()
    if not img:
        abort(404)
    resp = send_file(io.BytesIO(img["image_blob"]), mimetype=img["image_mime"] or "image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp



@products_bp.route("/products/<int:pid>/variants.json", endpoint="product_variants_json")
@login_required
def product_variants_json(pid):
    """Endpoint usado pelo PDV ao clicar num produto que tem variantes.
    Retorna lista de variantes ATIVAS com estoque > 0 e preco efetivo
    (considera promocoes da variante)."""
    db = get_db()
    prod = db.execute(
        "SELECT id, name, sale_price, promo_price, promo_until "
        "FROM products WHERE id = %s AND is_active = 1",
        (pid,),
    ).fetchone()
    if not prod:
        return jsonify({"ok": False, "error": "Produto nao encontrado."}), 404
    rows = db.execute(
        "SELECT id, size, color, stock_qty, sale_price, promo_price, promo_until "
        "FROM product_variants "
        "WHERE product_id = %s AND is_active = 1",
        (pid,),
    ).fetchall()
    rows = sorted(rows, key=lambda v: (size_sort_key(v["size"]), (v["color"] or "")))
    variants = []
    for v in rows:
        v_dict = {
            "id": v["id"],
            "size": v["size"] or "",
            "color": v["color"] or "",
            "stock": int(v["stock_qty"] or 0),
            "price": str(effective_price(v)),
            "in_promo": bool(has_active_promo(v)),
        }
        variants.append(v_dict)
    return jsonify({
        "ok": True,
        "product": {"id": prod["id"], "name": prod["name"]},
        "variants": variants,
    })



@products_bp.route("/products/labels", endpoint="product_labels_batch_ui")
@login_required
def product_labels_batch_ui():
    """UI to pick products for bulk label printing (Niimbot D110 via PDF)."""
    db = get_db()
    q = request.args.get("q", "").strip()
    cat = request.args.get("cat", "").strip()
    sql = "SELECT id, name, sku, category, sale_price, stock_qty, image_mime FROM products WHERE is_active=1"
    params = []
    if q:
        sql += " AND (name ILIKE %s OR sku ILIKE %s)"; params += [f"%{q}%", f"%{q}%"]
    if cat:
        sql += " AND category=%s"; params.append(cat)
    sql += " ORDER BY name ASC"
    products = db.execute(sql, params).fetchall()
    categories = db.execute(
        "SELECT DISTINCT category FROM products WHERE is_active=1 AND category IS NOT NULL ORDER BY category"
    ).fetchall()
    width_mm  = _mm_setting("label_width_mm", "40")
    height_mm = _mm_setting("label_height_mm", "12")
    ensure_csrf_token()
    return render_template("product_labels_batch.html",
        products=products, categories=categories, q=q, selected_cat=cat,
        width_mm=int(width_mm), height_mm=int(height_mm))



@products_bp.route("/products/labels-niimbot.pdf", methods=["GET", "POST"], endpoint="product_labels_niimbot")
@login_required
def product_labels_niimbot():
    """Etiqueta PDF para impressora térmica / Niimbot.
    GET  ?ids=1,2,3           — IDs na querystring (comportamento antigo)
    POST product_id[] + qty_N — form do seletor em massa, com quantidade por item
    """
    with transaction() as db:
        _ensure_product_qr_tokens(db)

    db = get_db()

    # Gather (product_id, qty) pairs from either POST form or GET ids
    pairs = []
    if request.method == "POST":
        validate_csrf_or_abort()
        selected = request.form.getlist("product_id")
        for sid in selected:
            try:
                pid = int(sid)
                qty = max(1, min(50, int(request.form.get(f"qty_{pid}", "1") or "1")))
                pairs.append((pid, qty))
            except (ValueError, TypeError):
                continue
    else:
        ids_s = request.args.get("ids", "")
        if ids_s:
            for x in ids_s.split(","):
                if x.strip().isdigit():
                    pairs.append((int(x), 1))

    if pairs:
        unique_ids = list({pid for pid, _ in pairs})
        rows = db.execute(
            f"SELECT * FROM products WHERE id IN ({','.join(['%s']*len(unique_ids))})",
            unique_ids
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        # Expand according to qty, preserving order
        products = []
        for pid, qty in pairs:
            if pid in by_id:
                products.extend([by_id[pid]] * qty)
    else:
        products = db.execute("SELECT * FROM products WHERE is_active=1 ORDER BY name LIMIT 24").fetchall()

    width_mm = _mm_setting("label_width_mm", "40")
    height_mm = _mm_setting("label_height_mm", "12")
    # Niimbot D110: 203 DPI (8 dots/mm). Para qualidade térmica máxima,
    # rasterizamos exatamente nessa densidade — sem interpolação no driver.
    DPMM = 8  # dots per mm para 203 DPI
    lw = max(int(round(width_mm * DPMM)), 200)
    lh = max(int(round(height_mm * DPMM)),  96)
    scale = max(0.85, min(height_mm / 12.0, 2.3))

    import qrcode as qrlib
    try:
        # Fontes maiores e bold — térmica perde detalhes finos no dithering
        font_name  = ImageFont.truetype(str(BASE_DIR/"static"/"fonts"/"Poppins-Medium.ttf"), int(20 * scale))
        font_price = ImageFont.truetype(str(BASE_DIR/"static"/"fonts"/"Poppins-Medium.ttf"), int(22 * scale))
        font_sku   = ImageFont.truetype(str(BASE_DIR/"static"/"fonts"/"DejaVuSans.ttf"),     max(int(13 * scale), 11))
    except Exception:
        font_name = font_price = font_sku = None

    pages = []
    try:
        for p in products:
            # Renderiza em modo "L" (8-bit grayscale) — ImageDraw funciona melhor
            # aqui que em modo "1". Convertemos para 1-bit no final, com dither.
            page = Image.new("L", (lw, lh), 255)  # branco
            draw = ImageDraw.Draw(page)
            # Borda de 2px preto puro
            draw.rectangle([0, 0, lw - 1, lh - 1], outline=0, width=2)

            # QR code: PIXEL-PERFECT para impressão térmica.
            # Dois cuidados críticos para a Niimbot não borrar o QR:
            #   1) Cores como STRING ("black"/"white"). Passar inteiros
            #      em qrcode>=8 cai num factory RGB onde back_color=255
            #      vira (255,0,0) → vermelho. Após .convert("L") fica
            #      luminância ~76 e o threshold transforma tudo em preto.
            #   2) Escala INTEIRA. Geramos com box_size=1 para descobrir
            #      o nº exato de módulos e escalamos por um múltiplo
            #      inteiro que cabe no espaço. Sem fracionamento, cada
            #      módulo vira N×N pixels exatos (sem anti-alias).
            qr_max = max(int(lh - 8), 80)
            qr = qrlib.QRCode(box_size=1, border=2)
            qr.add_data(_product_qr_payload(p))
            qr.make(fit=True)
            qri_min = qr.make_image(fill_color="black", back_color="white").convert("L")
            mod_total = qri_min.size[0]  # já inclui o quiet zone
            mod_scale = max(1, qr_max // mod_total)
            qr_side  = mod_total * mod_scale
            qri = qri_min.resize((qr_side, qr_side), Image.Resampling.NEAREST)
            page.paste(qri, (4, max((lh - qr_side) // 2, 4)))

            # Textos: TUDO PRETO PURO (fill=0). Cores cinzentas/douradas
            # viram dithering ilegível em térmica.
            tx = qr_side + 12
            if font_name:
                draw.text((tx, 6),               (p["name"] or "")[:26],                              fill=0, font=font_name)
            if font_price:
                draw.text((tx, int(34 * scale)), f"R$ {format_money(p['sale_price'])}",                fill=0, font=font_price)
            if font_sku:
                draw.text((tx, int(60 * scale)), (p["sku"] or "SEM SKU")[:20],                         fill=0, font=font_sku)

            # Conversão final para 1-bit com dither Floyd-Steinberg.
            # Threshold em 200 (em vez do padrão 128) — mantém pretos pretos
            # e brancos brancos, evita "manchas" cinzentas que viram dithering.
            page_1bit = page.point(lambda v: 0 if v < 200 else 255, mode="1")
            pages.append(page_1bit)
    except Exception:
        current_app.logger.exception("label PDF rendering failed")
        flash("Erro ao gerar etiquetas. Veja os logs do servidor.", "danger")
        return redirect(url_for("product_labels_batch_ui"))

    if not pages:
        pages = [Image.new("1", (lw, lh), 1)]

    buf = io.BytesIO()
    try:
        # Resolution = 203 (DPI Niimbot D110). PDF sai com tamanho físico
        # exato, sem reescalonamento no driver de impressão.
        pages[0].save(buf, format="PDF", save_all=True,
                      append_images=pages[1:], resolution=203.0)
    except Exception:
        current_app.logger.exception("label PDF save failed (pages=%d, lw=%d, lh=%d)", len(pages), lw, lh)
        flash("Erro ao montar o PDF. Tente reduzir a quantidade de etiquetas.", "danger")
        return redirect(url_for("product_labels_batch_ui"))
    buf.seek(0)
    pdf_bytes = buf.getvalue()
    current_app.logger.info("label PDF generated: %d pages, %d bytes (1-bit / 203 DPI)", len(pages), len(pdf_bytes))

    filename = f"etiquetas-niimbot-{int(width_mm)}x{int(height_mm)}mm.pdf"
    return send_pdf_download(pdf_bytes, filename)

# ─── CLIENTES ───────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# Clientes extraídos para blueprints/customers.py
# (Onda 2 — Etapa F). Endpoints preservam nomes globais.
# ─────────────────────────────────────────────────────────────






# ─── VENDAS ─────────────────────────────────────────────────


@products_bp.route("/products/<int:pid>/variants", methods=["GET","POST"], endpoint="product_variants_view")
@login_required
def product_variants_view(pid):
    db  = get_db()
    p   = db.execute("SELECT * FROM products WHERE id=%s", (pid,)).fetchone()
    if not p: abort(404)
    if request.method == "POST":
        validate_csrf_or_abort()
        user   = get_current_user(); now = utc_now_iso()
        added  = 0
        skipped_dup = 0

        # Formato preferencial: matriz cor × tamanho (var_size[]/var_color[]/var_qty[])
        var_sizes  = request.form.getlist("var_size[]")
        var_colors = request.form.getlist("var_color[]")
        var_qtys   = request.form.getlist("var_qty[]")

        sku_base = p["sku"] or f"P{pid:06d}"

        # Mapa de combinações já existentes para evitar duplicatas (size, color)
        existing_combos = set()
        for r in db.execute(
            "SELECT size, color FROM product_variants WHERE product_id=%s", (pid,)
        ).fetchall():
            existing_combos.add(((r["size"] or "").strip().lower(),
                                 (r["color"] or "").strip().lower()))

        if var_sizes:
            with transaction() as db2:
                for vsz, vcl, vqt in zip(var_sizes, var_colors, var_qtys):
                    try:
                        vqty = int(vqt or 0)
                    except (TypeError, ValueError):
                        vqty = 0
                    if vqty <= 0:
                        continue
                    sz_clean = (vsz or "").strip() or None
                    cl_clean = (vcl or "").strip() or None
                    if not sz_clean and not cl_clean:
                        continue
                    combo_key = ((sz_clean or "").lower(), (cl_clean or "").lower())
                    if combo_key in existing_combos:
                        skipped_dup += 1
                        continue
                    existing_combos.add(combo_key)
                    sz_part = (sz_clean or "X")[:3].upper().replace(" ","")
                    cl_part = (cl_clean or "X")[:3].upper().replace(" ","")
                    v_sku = f"{sku_base}-{sz_part}-{cl_part}-{secrets.token_hex(2).upper()}"
                    v_qr  = secrets.token_urlsafe(10)
                    try:
                        db2.execute("""INSERT INTO product_variants(product_id,size,color,sku,qr_token,
                            stock_qty,stock_min,cost_price,sale_price,is_active,created_at)
                            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s)""",
                            (pid, sz_clean, cl_clean, v_sku, v_qr,
                             vqty, p["stock_min"],
                             str(p["cost_price"]), str(p["sale_price"]), now))
                        db2.execute("""INSERT INTO stock_movements(product_id,type,qty,reason,operator_name,created_at)
                            VALUES(%s,'in',%s,%s,%s,%s)""",
                            (pid, vqty, f"Entrada · variante {sz_part}/{cl_part}",
                             user["display_name"], now))
                        added += 1
                    except IntegrityError:
                        skipped_dup += 1
                    except Exception:
                        pass
        else:
            # Fallback: formato antigo (size[]/color[]/stock[]/cost[]/sale[])
            sizes  = request.form.getlist("size[]")
            colors = request.form.getlist("color[]")
            stocks = request.form.getlist("stock[]")
            costs  = request.form.getlist("cost[]")
            sales  = request.form.getlist("sale[]")
            with transaction() as db2:
                for sz, cl, stk, cst, sl in zip(sizes, colors, stocks, costs, sales):
                    if not sz.strip() and not cl.strip(): continue
                    combo_key = (sz.strip().lower(), cl.strip().lower())
                    if combo_key in existing_combos:
                        skipped_dup += 1
                        continue
                    existing_combos.add(combo_key)
                    qt_tok = secrets.token_urlsafe(10)
                    v_sku = f"{sku_base}-{sz[:3].upper()}-{cl[:3].upper()}-{secrets.token_hex(2).upper()}"
                    try:
                        db2.execute("""INSERT INTO product_variants(product_id,size,color,sku,qr_token,
                            stock_qty,stock_min,cost_price,sale_price,is_active,created_at)
                            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s)""",
                            (pid, sz.strip() or None, cl.strip() or None, v_sku, qt_tok,
                             int(stk or 0), p["stock_min"],
                             str(parse_money(cst) or p["cost_price"]),
                             str(parse_money(sl) or p["sale_price"]), now))
                        added += 1
                    except IntegrityError:
                        skipped_dup += 1
                    except Exception:
                        pass

        msg_parts = []
        if added:
            msg_parts.append(f"{added} variante(s) adicionada(s)")
        if skipped_dup:
            msg_parts.append(f"{skipped_dup} ignorada(s) (já existiam)")
        # Mantem products.stock_qty em sincronia com a soma das variantes
        if added:
            with transaction() as db_sync:
                new_total = resync_product_stock(db_sync, pid)
            if new_total >= 0:
                msg_parts.append(f"estoque do produto: {new_total} peça(s)")
        flash(". ".join(msg_parts) + "." if msg_parts else "Nenhuma variante adicionada.",
              "success" if added else "warning")
        return redirect(url_for("product_variants_view", pid=pid))
    variants_raw = db.execute("SELECT * FROM product_variants WHERE product_id=%s", (pid,)).fetchall()
    variants = sorted(variants_raw, key=lambda v: (size_sort_key(v["size"]), (v["color"] or "")))

    # v10.4: para cada variante, anexa a foto cadastrada para sua cor (se houver).
    # Foto é por cor (não por variante), então PMG da mesma cor compartilham.
    color_photos = {}
    photo_rows = db.execute(
        "SELECT id, color, image_version FROM product_images "
        "WHERE product_id=%s AND color IS NOT NULL "
        "ORDER BY is_primary DESC, sort_order ASC, id ASC",
        (pid,)
    ).fetchall()
    for ph in photo_rows:
        key = (ph["color"] or "").strip().lower()
        if key and key not in color_photos:
            color_photos[key] = {"id": ph["id"], "version": ph["image_version"]}

    variants_enriched = []
    for v in variants:
        vd = dict(v)
        ck = (v["color"] or "").strip().lower()
        ph = color_photos.get(ck)
        vd["color_photo_id"] = ph["id"] if ph else None
        vd["color_photo_version"] = ph["version"] if ph else None
        variants_enriched.append(vd)

    return render_template("product_variants.html",
                           product=p, variants=variants_enriched)



@products_bp.route("/products/<int:pid>/variants/<int:vid>/stock", methods=["POST"], endpoint="variant_stock")
@login_required
def variant_stock(pid, vid):
    """Movimentacao completa de estoque com motivo. 3 modos:
    - in:      entrada (delta = +qty)
    - out:     saida   (delta = -qty)
    - adjust:  acerto absoluto (qty vira o novo total, delta = qty - atual)
    Sempre grava em stock_movements com variant_id e reason.
    Em caso de inconsistencia (saida > estoque), recusa.
    """
    validate_csrf_or_abort()
    db = get_db()
    v = db.execute(
        "SELECT * FROM product_variants WHERE id=%s AND product_id=%s",
        (vid, pid),
    ).fetchone()
    if not v:
        abort(404)

    try:
        qty = int(request.form.get("qty", "0") or 0)
    except (TypeError, ValueError):
        qty = 0
    mtype = (request.form.get("type") or "in").strip().lower()
    reason = (request.form.get("reason") or "").strip()[:200]

    if mtype not in {"in", "out", "adjust"}:
        flash("Tipo de movimentacao invalido.", "danger")
        return redirect(url_for("product_variants_view", pid=pid))
    if qty < 0 or (mtype != "adjust" and qty == 0):
        flash("Quantidade deve ser positiva.", "danger")
        return redirect(url_for("product_variants_view", pid=pid))

    current = int(v["stock_qty"] or 0)
    if mtype == "in":
        delta = qty
        log_type = "in"
    elif mtype == "out":
        if qty > current:
            flash(f"Saida de {qty} excede o estoque atual ({current}).", "danger")
            return redirect(url_for("product_variants_view", pid=pid))
        delta = -qty
        log_type = "out"
    else:  # adjust
        delta = qty - current
        log_type = "adjust"

    operator = (get_current_user() or {}).get("display_name") or "sistema"
    with transaction() as db2:
        db2.execute(
            "UPDATE product_variants SET stock_qty = stock_qty + %s WHERE id = %s",
            (delta, vid),
        )
        db2.execute(
            "INSERT INTO stock_movements "
            "(product_id, variant_id, type, qty, reason, operator_name, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (pid, vid, log_type, abs(delta) if log_type != "adjust" else qty,
             reason, operator, utc_now_iso()),
        )
        # Mantem products.stock_qty sincronizado
        resync_product_stock(db2, pid)
    new_total = current + delta
    flash(
        f"Estoque {v['size'] or '-'} / {v['color'] or '-'}: "
        f"{current} -> {new_total} ({'+' if delta >= 0 else ''}{delta}).",
        "success",
    )
    return redirect(url_for("product_variants_view", pid=pid))



@products_bp.route("/products/<int:pid>/variants/<int:vid>/stock-quick", methods=["POST"], endpoint="variant_stock_quick")
@login_required
def variant_stock_quick(pid, vid):
    """Incremento +/-1 via fetch (AJAX). Sem reload, sem flash.
    Motivo padrao: 'ajuste rapido'. Bloqueia saida abaixo de zero."""
    validate_csrf_or_abort()
    db = get_db()
    v = db.execute(
        "SELECT id, stock_qty, size, color FROM product_variants WHERE id=%s AND product_id=%s",
        (vid, pid),
    ).fetchone()
    if not v:
        return jsonify({"ok": False, "error": "Variante nao encontrada."}), 404
    try:
        delta = int(request.form.get("delta", "0") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Delta invalido."}), 400
    if delta not in (-1, 1):
        return jsonify({"ok": False, "error": "Delta deve ser +1 ou -1."}), 400
    current = int(v["stock_qty"] or 0)
    if current + delta < 0:
        return jsonify({"ok": False, "error": "Estoque nao pode ficar negativo."}), 400

    operator = (get_current_user() or {}).get("display_name") or "sistema"
    log_type = "in" if delta > 0 else "out"
    with transaction() as db2:
        db2.execute(
            "UPDATE product_variants SET stock_qty = stock_qty + %s WHERE id = %s",
            (delta, vid),
        )
        db2.execute(
            "INSERT INTO stock_movements "
            "(product_id, variant_id, type, qty, reason, operator_name, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (pid, vid, log_type, abs(delta), "ajuste rapido", operator, utc_now_iso()),
        )
        resync_product_stock(db2, pid)
    return jsonify({"ok": True, "new_qty": current + delta, "delta": delta})



@products_bp.route("/products/<int:pid>/variants/<int:vid>/history.json", endpoint="variant_stock_history")
@login_required
def variant_stock_history(pid, vid):
    """Ultimas 30 movimentacoes desta variante. JSON para drawer."""
    db = get_db()
    v = db.execute(
        "SELECT id FROM product_variants WHERE id=%s AND product_id=%s",
        (vid, pid),
    ).fetchone()
    if not v:
        return jsonify({"ok": False, "error": "Variante nao encontrada."}), 404
    rows = db.execute(
        "SELECT type, qty, reason, operator_name, created_at, sale_id "
        "FROM stock_movements WHERE variant_id = %s "
        "ORDER BY created_at DESC LIMIT 30",
        (vid,),
    ).fetchall()
    events = [{
        "type": r["type"],
        "qty": int(r["qty"]),
        "reason": r["reason"] or "",
        "operator": r["operator_name"] or "",
        "at": r["created_at"],
        "sale_id": r["sale_id"],
    } for r in rows]
    return jsonify({"ok": True, "events": events})

# ─── PROMOÇÕES POR VARIANTE ─────────────────────────────────


@products_bp.route("/products/<int:pid>/variants/<int:vid>/promo", methods=["POST"], endpoint="variant_promo")
@login_required
def variant_promo(pid, vid):
    """Aplica ou remove promoção em uma variante específica."""
    validate_csrf_or_abort()
    db = get_db()
    v = db.execute(
        "SELECT * FROM product_variants WHERE id=%s AND product_id=%s",
        (vid, pid),
    ).fetchone()
    if not v:
        abort(404)

    action = request.form.get("action", "set")
    if action == "clear":
        with transaction() as db2:
            db2.execute(
                "UPDATE product_variants SET promo_price=NULL, promo_until=NULL WHERE id=%s",
                (vid,),
            )
        flash("Promoção removida.", "success")
        return redirect(url_for("product_variants_view", pid=pid))

    # set: pode ser preço fixo OU percentual
    promo_until = (request.form.get("promo_until") or "").strip() or None
    sale_price = Decimal(str(v["sale_price"] or "0"))

    promo_pct_raw = (request.form.get("promo_pct") or "").strip()
    promo_price_raw = (request.form.get("promo_price") or "").strip()

    try:
        if promo_pct_raw:
            pct = Decimal(promo_pct_raw.replace(",", "."))
            if pct <= 0 or pct >= 100:
                flash("Desconto deve ser entre 0 e 100%.", "danger")
                return redirect(url_for("product_variants_view", pid=pid))
            new_price = (sale_price * (Decimal("100") - pct) / Decimal("100")).quantize(Decimal("0.01"))
        elif promo_price_raw:
            new_price = parse_money(promo_price_raw)
            if new_price <= 0 or new_price >= sale_price:
                flash("Preço promocional deve ser maior que zero e menor que o preço cheio.", "danger")
                return redirect(url_for("product_variants_view", pid=pid))
        else:
            flash("Informe percentual ou preço promocional.", "danger")
            return redirect(url_for("product_variants_view", pid=pid))
    except (InvalidOperation, ValueError):
        flash("Valor inválido.", "danger")
        return redirect(url_for("product_variants_view", pid=pid))

    with transaction() as db2:
        db2.execute(
            "UPDATE product_variants SET promo_price=%s, promo_until=%s WHERE id=%s",
            (str(new_price), promo_until, vid),
        )
    flash(f"Promoção aplicada: R$ {format_money(new_price)}.", "success")
    return redirect(url_for("product_variants_view", pid=pid))



@products_bp.route("/products/<int:pid>/variants/promo-bulk", methods=["POST"], endpoint="variant_promo_bulk")
@login_required
def variant_promo_bulk(pid):
    """Aplica/remove promoção em múltiplas variantes selecionadas."""
    validate_csrf_or_abort()
    db = get_db()
    p = db.execute("SELECT id FROM products WHERE id=%s", (pid,)).fetchone()
    if not p:
        abort(404)

    ids = request.form.getlist("variant_ids[]")
    try:
        ids = [int(i) for i in ids if i]
    except (TypeError, ValueError):
        ids = []
    if not ids:
        flash("Selecione ao menos uma variante.", "warning")
        return redirect(url_for("product_variants_view", pid=pid))

    action = request.form.get("action", "set")

    if action == "clear":
        with transaction() as db2:
            db2.execute(
                "UPDATE product_variants SET promo_price=NULL, promo_until=NULL "
                "WHERE product_id=%s AND id = ANY(%s)",
                (pid, ids),
            )
        flash(f"Promoção removida de {len(ids)} variante(s).", "success")
        return redirect(url_for("product_variants_view", pid=pid))

    promo_pct_raw = (request.form.get("promo_pct") or "").strip()
    promo_until = (request.form.get("promo_until") or "").strip() or None
    if not promo_pct_raw:
        flash("Informe o percentual de desconto para aplicação em lote.", "danger")
        return redirect(url_for("product_variants_view", pid=pid))

    try:
        pct = Decimal(promo_pct_raw.replace(",", "."))
        if pct <= 0 or pct >= 100:
            flash("Desconto deve ser entre 0 e 100%.", "danger")
            return redirect(url_for("product_variants_view", pid=pid))
    except InvalidOperation:
        flash("Percentual inválido.", "danger")
        return redirect(url_for("product_variants_view", pid=pid))

    # Cada variante recebe seu próprio promo_price calculado sobre seu sale_price
    rows = db.execute(
        "SELECT id, sale_price FROM product_variants WHERE product_id=%s AND id = ANY(%s)",
        (pid, ids),
    ).fetchall()

    applied = 0
    with transaction() as db2:
        for r in rows:
            try:
                base = Decimal(str(r["sale_price"] or "0"))
                if base <= 0:
                    continue
                new_price = (base * (Decimal("100") - pct) / Decimal("100")).quantize(Decimal("0.01"))
                db2.execute(
                    "UPDATE product_variants SET promo_price=%s, promo_until=%s WHERE id=%s",
                    (str(new_price), promo_until, r["id"]),
                )
                applied += 1
            except Exception:
                continue

    flash(f"Promoção de {pct}% aplicada a {applied} variante(s).", "success")
    return redirect(url_for("product_variants_view", pid=pid))

# ─── HISTÓRICO DE PREÇOS ────────────────────────────────────


@products_bp.route("/products/<int:pid>/price-history", endpoint="product_price_history")
@login_required
def product_price_history(pid):
    db  = get_db()
    p   = db.execute("SELECT * FROM products WHERE id=%s", (pid,)).fetchone()
    if not p: abort(404)
    hist = db.execute("""SELECT * FROM price_history WHERE product_id=%s
        ORDER BY created_at DESC LIMIT 100""", (pid,)).fetchall()
    return render_template("price_history.html", product=p, history=hist)

# ─── ANIVERSARIANTES ────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# Aniversários extraídos para blueprints/customers.py
# (Onda 2 — Etapa F). Endpoints preservam nomes globais.
# ─────────────────────────────────────────────────────────────


# ─── PEDIDO DE REPOSIÇÃO ────────────────────────────────────


@products_bp.route("/restock", endpoint="restock_list")
@login_required
def restock_list():
    db = get_db()
    low = db.execute("""
        SELECT p.*, (p.stock_min - p.stock_qty) as qty_needed
        FROM products p WHERE p.stock_qty <= p.stock_min AND p.is_active=1
        ORDER BY (p.stock_qty*1.0/NULLIF(p.stock_min,0)) ASC, p.name
    """).fetchall()
    orders = db.execute("SELECT * FROM restock_orders ORDER BY created_at DESC LIMIT 20").fetchall()
    return render_template("restock.html", low=low, orders=orders)



@products_bp.route("/restock/new", methods=["POST"], endpoint="create_restock")
@login_required
def create_restock():
    validate_csrf_or_abort()
    db   = get_db()
    pids = request.form.getlist("product_id[]")
    qtys = request.form.getlist("qty[]")
    sup  = request.form.get("supplier","").strip() or None
    notes= request.form.get("notes","").strip() or None
    user = get_current_user(); now = utc_now_iso()
    if not pids:
        flash("Selecione ao menos um produto.","danger"); return redirect(url_for("restock_list"))
    with transaction() as db2:
        oid = insert_returning_id("INSERT INTO restock_orders(status,supplier,notes,created_by,created_at) VALUES('pending',%s,%s,%s,%s)",
                          (sup, notes, user["display_name"], now))
        for pid, qty in zip(pids, qtys):
            if pid and qty:
                db2.execute("INSERT INTO restock_order_items(order_id,product_id,qty_ordered) VALUES(%s,%s,%s)",
                            (oid, int(pid), int(qty or 1)))
    flash(f"Pedido #{oid} criado.","success")
    return redirect(url_for("restock_detail", oid=oid))



@products_bp.route("/restock/<int:oid>", endpoint="restock_detail")
@login_required
def restock_detail(oid):
    db  = get_db()
    order = db.execute("SELECT * FROM restock_orders WHERE id=%s", (oid,)).fetchone()
    if not order: abort(404)
    items = db.execute("""SELECT roi.*, p.name as product_name, p.sku, p.unit, p.stock_qty
        FROM restock_order_items roi JOIN products p ON p.id=roi.product_id
        WHERE roi.order_id=%s""", (oid,)).fetchall()
    return render_template("restock_detail.html", order=order, items=items)



@products_bp.route("/restock/<int:oid>/status", methods=["POST"], endpoint="update_restock_status")
@login_required
def update_restock_status(oid):
    validate_csrf_or_abort()
    db     = get_db()
    order  = db.execute("SELECT * FROM restock_orders WHERE id=%s", (oid,)).fetchone()
    if not order: abort(404)
    status = request.form.get("status","")
    user   = get_current_user(); now = utc_now_iso()
    with transaction() as db2:
        if status == "sent":
            db2.execute("UPDATE restock_orders SET status='sent', sent_at=%s WHERE id=%s", (now,oid))
        elif status == "received":
            # Restock items
            items = db2.execute("SELECT * FROM restock_order_items WHERE order_id=%s", (oid,)).fetchall()
            for item in items:
                qty = item["qty_received"] or item["qty_ordered"]
                db2.execute("UPDATE products SET stock_qty=stock_qty+%s, updated_at=%s WHERE id=%s",
                            (qty, now, item["product_id"]))
                db2.execute("""INSERT INTO stock_movements(product_id,type,qty,reason,operator_name,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s)""",
                    (item["product_id"],"in",qty,f"Recebimento pedido #{oid}",user["display_name"],now))
            db2.execute("UPDATE restock_orders SET status='received', received_at=%s WHERE id=%s", (now,oid))
        elif status == "cancelled":
            db2.execute("UPDATE restock_orders SET status='cancelled' WHERE id=%s", (oid,))
    flash(f"Pedido #{oid} atualizado para {status}.","success")
    return redirect(url_for("restock_detail", oid=oid))

# ─── CATÁLOGO PÚBLICO ───────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# Rotas públicas (catálogo, sitemap, robots, interest API,
# imagem pública) extraídas para blueprints/public.py
# (Onda 2 — Etapa C). Endpoints preservam nomes globais:
# public_catalog, public_product, public_catalog_category,
# public_sitemap, public_robots, log_interest,
# public_product_image. Registro acontece no fim de app.py.
# ─────────────────────────────────────────────────────────────


# ─── PÁGINA INDIVIDUAL DA PEÇA ─────────────────────────
# ─────────────────────────────────────────────────────────────
# Interesse extraídos para blueprints/customers.py
# (Onda 2 — Etapa F). Endpoints preservam nomes globais.
# ─────────────────────────────────────────────────────────────




@products_bp.route("/produtos/abc", endpoint="produtos_abc")
@login_required
def produtos_abc():
    db = get_db()
    # Janela configurável: 30, 60, 90, 180, 365
    try:
        window = int(request.args.get("window", get_setting("abc_default_window", "90")))
    except (ValueError, TypeError):
        window = 90
    if window not in (30, 60, 90, 180, 365):
        window = 90

    cutoff = (utc_now() - timedelta(days=window)).isoformat()

    rows = db.execute("""
        SELECT
            p.id,
            p.name,
            p.sku,
            p.category,
            p.stock_qty,
            p.cost_price,
            p.sale_price,
            COALESCE(SUM(CASE WHEN s.status <> 'cancelled'
                        THEN CAST(si.qty AS INTEGER) ELSE 0 END), 0) AS units_sold,
            COALESCE(SUM(CASE WHEN s.status <> 'cancelled'
                        THEN CAST(si.qty AS DOUBLE PRECISION)
                             * CAST(si.unit_price AS DOUBLE PRECISION)
                        ELSE 0 END), 0) AS revenue
        FROM products p
        LEFT JOIN sale_items si ON si.product_id = p.id
        LEFT JOIN sales s ON s.id = si.sale_id AND s.created_at >= %s
        WHERE p.is_active = 1
        GROUP BY p.id, p.name, p.sku, p.category, p.stock_qty, p.cost_price, p.sale_price
        ORDER BY revenue DESC
    """, (cutoff,)).fetchall()

    # Classificação ABC (Pareto): A = 80% receita acumulada, B = +15%, C = resto
    total_rev = sum(float(r["revenue"] or 0) for r in rows)
    items = []
    cum = 0.0
    for r in rows:
        rev = float(r["revenue"] or 0)
        cum += rev
        share = (cum / total_rev * 100.0) if total_rev > 0 else 0.0
        if total_rev <= 0:
            cls = "C"
        elif share <= 80.0:
            cls = "A"
        elif share <= 95.0:
            cls = "B"
        else:
            cls = "C"
        units = int(r["units_sold"] or 0)
        stock = int(r["stock_qty"] or 0)
        # Dias de cobertura: estoque atual ÷ velocidade diária
        daily_velocity = (units / window) if window > 0 else 0
        days_coverage = int(stock / daily_velocity) if daily_velocity > 0 else None
        items.append({
            "id": r["id"], "name": r["name"], "sku": r["sku"],
            "category": r["category"],
            "stock": stock,
            "units_sold": units,
            "revenue": rev,
            "share_cum": share,
            "class": cls,
            "days_coverage": days_coverage,
            "velocity_per_day": daily_velocity,
        })

    # Resumo agregado
    summary = {
        "A": {"count": 0, "revenue": 0.0},
        "B": {"count": 0, "revenue": 0.0},
        "C": {"count": 0, "revenue": 0.0},
    }
    for it in items:
        summary[it["class"]]["count"] += 1
        summary[it["class"]]["revenue"] += it["revenue"]

    return render_template(
        "produtos_abc.html",
        items=items,
        summary=summary,
        total_revenue=total_rev,
        window=window,
    )



# ─────────────────────────────────────────────────────────────────────────
# init_products_blueprint — registra aliases sem prefixo "products."
# IMPORTANTE: o monkey-patch _patch_edit_product_for_price_history em
# app.py captura `app.view_functions["edit_product"]` — esse alias é
# registrado abaixo, garantindo que o patch continua funcionando.
# ─────────────────────────────────────────────────────────────────────────
def init_products_blueprint(app):
    """Hook chamado por app.py após `app.register_blueprint(products_bp)`. Idempotente."""
    if getattr(app, "_products_bp_initialized", False):
        return

    ALIAS_MAP = {
        "list_products":                 "products.list_products",
        "create_product":                "products.create_product",
        "product_detail":                "products.product_detail",
        "delete_product":                "products.delete_product",
        "edit_product":                  "products.edit_product",
        "toggle_product_featured":       "products.toggle_product_featured",
        "adjust_stock":                  "products.adjust_stock",
        "product_qr":                    "products.product_qr",
        "product_qr_batch":              "products.product_qr_batch",
        "scan_lookup":                   "products.scan_lookup",
        "api_product_lookup":            "products.api_product_lookup",
        "product_image":                 "products.product_image",
        "product_gallery_image":         "products.product_gallery_image",
        "product_gallery":               "products.product_gallery",
        "product_gallery_upload":        "products.product_gallery_upload",
        "product_gallery_delete":        "products.product_gallery_delete",
        "product_gallery_rotate":        "products.product_gallery_rotate",
        "product_gallery_set_primary":   "products.product_gallery_set_primary",
        "public_product_gallery_image":  "products.public_product_gallery_image",
        "product_variants_json":         "products.product_variants_json",
        "product_labels_batch_ui":       "products.product_labels_batch_ui",
        "product_labels_niimbot":        "products.product_labels_niimbot",
        "product_variants_view":         "products.product_variants_view",
        "variant_stock":                 "products.variant_stock",
        "variant_stock_quick":           "products.variant_stock_quick",
        "variant_stock_history":         "products.variant_stock_history",
        "variant_promo":                 "products.variant_promo",
        "variant_promo_bulk":            "products.variant_promo_bulk",
        "product_price_history":         "products.product_price_history",
        "restock_list":                  "products.restock_list",
        "create_restock":                "products.create_restock",
        "restock_detail":                "products.restock_detail",
        "update_restock_status":         "products.update_restock_status",
        "produtos_abc":                  "products.produtos_abc",
    }

    _ROUTE_DEFS = [
        ("list_products",                "/products",                                            ("GET",)),
        ("create_product",               "/products/new",                                        ("GET", "POST")),
        ("product_detail",               "/products/<int:pid>",                                  ("GET",)),
        ("delete_product",               "/products/<int:pid>/delete",                           ("POST",)),
        ("edit_product",                 "/products/<int:pid>/edit",                             ("GET", "POST")),
        ("toggle_product_featured",      "/products/<int:pid>/toggle-featured",                  ("POST",)),
        ("adjust_stock",                 "/products/<int:pid>/stock",                            ("POST",)),
        ("product_qr",                   "/products/<int:pid>/qr",                               ("GET",)),
        ("product_qr_batch",             "/products/qr-batch",                                   ("POST",)),
        ("scan_lookup",                  "/products/scan-lookup",                                ("GET",)),
        ("api_product_lookup",           "/api/products/lookup",                                 ("GET",)),
        ("product_image",                "/products/<int:pid>/image",                            ("GET",)),
        ("product_gallery_image",        "/products/<int:pid>/gallery/<int:img_id>",             ("GET",)),
        ("product_gallery",              "/products/<int:pid>/gallery",                          ("GET",)),
        ("product_gallery_upload",       "/products/<int:pid>/gallery/upload",                   ("POST",)),
        ("product_gallery_delete",       "/products/<int:pid>/gallery/<int:img_id>/delete",      ("POST",)),
        ("product_gallery_rotate",       "/products/<int:pid>/gallery/<int:img_id>/rotate",      ("POST",)),
        ("product_gallery_set_primary",  "/products/<int:pid>/gallery/<int:img_id>/set-primary", ("POST",)),
        ("public_product_gallery_image", "/public/product-gallery/<int:pid>/<int:img_id>",       ("GET",)),
        ("product_variants_json",        "/products/<int:pid>/variants.json",                    ("GET",)),
        ("product_labels_batch_ui",      "/products/labels",                                     ("GET",)),
        ("product_labels_niimbot",       "/products/labels-niimbot.pdf",                         ("GET", "POST")),
        ("product_variants_view",        "/products/<int:pid>/variants",                         ("GET", "POST")),
        ("variant_stock",                "/products/<int:pid>/variants/<int:vid>/stock",         ("POST",)),
        ("variant_stock_quick",          "/products/<int:pid>/variants/<int:vid>/stock-quick",   ("POST",)),
        ("variant_stock_history",        "/products/<int:pid>/variants/<int:vid>/history.json",  ("GET",)),
        ("variant_promo",                "/products/<int:pid>/variants/<int:vid>/promo",         ("POST",)),
        ("variant_promo_bulk",           "/products/<int:pid>/variants/promo-bulk",              ("POST",)),
        ("product_price_history",        "/products/<int:pid>/price-history",                    ("GET",)),
        ("restock_list",                 "/restock",                                             ("GET",)),
        ("create_restock",               "/restock/new",                                         ("POST",)),
        ("restock_detail",               "/restock/<int:oid>",                                   ("GET",)),
        ("update_restock_status",        "/restock/<int:oid>/status",                            ("POST",)),
        ("produtos_abc",                 "/produtos/abc",                                        ("GET",)),
    ]

    for short, full in ALIAS_MAP.items():
        view = app.view_functions.get(full)
        if view is None:
            app.logger.warning(
                "Products blueprint: endpoint %r ausente — alias %r não registrado.",
                full, short,
            )
            continue
        app.view_functions[short] = view

    existing_endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    for endpoint, rule, methods in _ROUTE_DEFS:
        if endpoint in existing_endpoints:
            continue
        view = app.view_functions.get(endpoint)
        if view is None:
            continue
        app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=list(methods))

    app._products_bp_initialized = True
