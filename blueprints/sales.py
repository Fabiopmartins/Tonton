"""
blueprints/sales.py — Blueprint de vendas e PIX.

Escopo (20 rotas):

  Vendas — CRUD + visualização (5)
    GET  /sales                                       → list_sales
    GET/POST /sales/new                               → create_sale
    GET  /sales/<sid>                                 → sale_detail
    POST /sales/<sid>/cancel                          → cancel_sale
    POST /sales/<sid>/return                          → return_item

  Pagamento — confirmação e pendentes (3)
    POST /sales/<sid>/payment/confirm                 → confirm_payment
    POST /sales/<sid>/payment/cancel                  → cancel_payment
    GET  /sales/pending                               → pending_payments

  PIX — geração e tracking (8)
    GET  /sales/<sid>/pix.txt                         → sale_pix_brcode
    GET  /sales/<sid>/pix.png                         → sale_pix_qr
    GET  /sales/<sid>/pix-info.json                   → sale_pix_info
    POST /sales/<sid>/charge-pix                      → create_pix_charge_for_sale
    GET  /pix-charges/<id>/qr.png                     → pix_charge_qr
    GET  /pix-charges/<id>/status                     → pix_charge_status
    POST /pix-charges/<id>/mark-paid                  → pix_charge_mark_paid
    POST /webhooks/pix/<provider_slug>                → pix_webhook  (PÚBLICO — webhook)

  Documentos — etiquetas e pré-nota (4)
    GET  /sales/<sid>/dispatch-label.pdf              → sale_dispatch_label
    GET  /sales/<sid>/pre-nota.pdf                    → sale_prefatura_pdf
    GET  /sales/<sid>/pre-nota.json                   → sale_prefatura_json
    GET  /sales/<sid>/pre-nota.csv                    → sale_prefatura_csv

Notas (Onda 2 — Etapa H):

  1. Endpoints PRESERVAM nomes globais via aliases registrados em
     init_sales_blueprint().

  2. /webhooks/pix/<provider_slug> é PÚBLICO (sem @login_required) —
     callback de PSPs (Inter etc.). Mantida em sales por coesão de
     domínio (pagamento de venda), não por auth-classification.

  3. Helpers _check_and_create_alerts e _create_notification eram
     nested em register_v3_routes (app.py:4759 e 4763). Replicados
     INLINE neste módulo, verbatim.

  4. Substituições aplicadas: bare `app` → `current_app`.

  5. Código das views é EXTRAÇÃO VERBATIM. Apenas decorators alterados.

  6. Esta etapa NÃO migra: dashboard, redeem, atender, accounting,
     pricing, reports, goals, notifications, calendar, catalog_qr.
     Esses ficam para a Etapa I (operations) ou em app.py (root).
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from flask import (
    Blueprint, current_app, request, render_template, redirect, url_for,
    abort, Response, jsonify, session, flash, send_file,
)
from PIL import Image, ImageDraw, ImageFont

from app import (
    BASE_DIR,
    LOCAL_TZ,
    UTC,
    _make_sale_number,
    _mm_setting,
    _prefatura_payload,
    _sale_shipping_snapshot,
    _shipping_label_has_data,
    _shipping_lines,
    _store_invoice_lines,
    _store_invoice_profile,
    _wrap_for_pdf,
    audit_log,
    effective_price,
    ensure_csrf_token,
    format_datetime_local,
    format_money,
    get_current_user,
    get_setting,
    login_required,
    parse_money,
    psp_decrypt,
    require_role,
    resync_product_stock,
    send_pdf_download,
    utc_now_iso,
    validate_coupon_row,
    validate_csrf_or_abort,
)
from db import get_db, insert_returning_id, transaction


sales_bp = Blueprint("sales", __name__, template_folder="../templates")


# ─────────────────────────────────────────────────────────────────────────
# Helpers privados replicados verbatim de app.py:4759 e 4763 (eram
# nested em register_v3_routes; nested functions não são exportáveis
# pelo nome).
# ─────────────────────────────────────────────────────────────────────────
def _create_notification(db, ntype, title, body="", link=""):
    db.execute(
        "INSERT INTO notifications(type,title,body,link,is_read,created_at) "
        "VALUES(%s,%s,%s,%s,0,%s)",
        (ntype, title, body, link, utc_now_iso()),
    )


def _check_and_create_alerts(db):
    """Run after any stock/sale change to generate relevant notifications."""
    # Low stock alerts
    low = db.execute(
        "SELECT name, stock_qty, stock_min FROM products "
        "WHERE stock_qty<=stock_min AND is_active=1 LIMIT 20"
    ).fetchall()
    for p in low:
        exists = db.execute(
            "SELECT id FROM notifications "
            "WHERE type='low_stock' AND title LIKE %s AND is_read=0",
            (f"%{p['name']}%",),
        ).fetchone()
        if not exists:
            _create_notification(
                db, "low_stock",
                f"Estoque baixo: {p['name']}",
                f"{p['stock_qty']} {p.get('unit','un')} restantes (mín: {p['stock_min']})",
                "/products?low_stock=1",
            )
    # Birthday alerts (today)
    today_md = datetime.now(LOCAL_TZ).strftime("-%m-%d")
    bdays = db.execute(
        "SELECT name, birthday FROM customers "
        "WHERE birthday LIKE %s AND birthday IS NOT NULL",
        (f"%{today_md}",),
    ).fetchall()
    for c in bdays:
        exists = db.execute(
            "SELECT id FROM notifications "
            "WHERE type='birthday' AND title LIKE %s AND is_read=0",
            (f"%{c['name']}%",),
        ).fetchone()
        if not exists:
            _create_notification(
                db, "birthday",
                f"🎂 Aniversário: {c['name']}",
                "Aproveite para enviar uma mensagem especial!",
                "/customers",
            )
    db.commit()


@sales_bp.route("/sales", endpoint="list_sales")
@login_required
def list_sales():
    db = get_db()
    month  = request.args.get("month", datetime.now(LOCAL_TZ).strftime("%Y-%m"))
    status_f = request.args.get("status","")
    try:
        ms = datetime.strptime(month, "%Y-%m").replace(tzinfo=LOCAL_TZ).astimezone(UTC).isoformat()
        me_d = (datetime.strptime(month, "%Y-%m").replace(day=28)+timedelta(days=4)).replace(day=1)
        me   = me_d.replace(tzinfo=LOCAL_TZ).astimezone(UTC).isoformat()
    except Exception:
        ms = "2000-01-01"; me = "2099-01-01"
    status_clause = "AND s.status=%s" if status_f else ""
    params = [ms, me] + ([status_f] if status_f else [])
    sales = db.execute(f"""SELECT s.id, s.sale_number, s.total, s.payment_method, s.created_at,
               s.status, s.cancel_reason, s.buyer_name_free,
               c.name as customer_name, STRING_AGG(p.name, ', ') as items
        FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
        LEFT JOIN sale_items si ON si.sale_id=s.id LEFT JOIN products p ON p.id=si.product_id
        WHERE s.created_at>=%s AND s.created_at<%s {status_clause}
        GROUP BY s.id, s.sale_number, s.total, s.payment_method, s.created_at, s.status, s.cancel_reason, s.buyer_name_free, c.name
        ORDER BY s.created_at DESC""", params).fetchall()
    totals = db.execute("SELECT COALESCE(SUM(CASE WHEN status!='cancelled' THEN CAST(total AS DOUBLE PRECISION) ELSE 0 END),0) as rev, SUM(CASE WHEN status!='cancelled' THEN 1 ELSE 0 END) as cnt FROM sales WHERE created_at>=%s AND created_at<%s", (ms,me)).fetchone()
    cancelled_rev = db.execute("SELECT COALESCE(SUM(CAST(total AS DOUBLE PRECISION)),0) FROM sales WHERE status='cancelled' AND created_at>=%s AND created_at<%s", (ms,me)).fetchone()[0]
    # Run background alert check
    try: _check_and_create_alerts(db)
    except Exception: pass
    return render_template("sales.html", sales=sales, totals=totals, month=month,
                           status_filter=status_f, cancelled_rev=cancelled_rev)



@sales_bp.route("/sales/new", methods=["GET","POST"], endpoint="create_sale")
@login_required
def create_sale():
    db = get_db()
    if request.method == "POST":
        validate_csrf_or_abort()
        pids   = request.form.getlist("product_id[]")
        qtys   = request.form.getlist("qty[]")
        vids   = request.form.getlist("variant_id[]")  # paralelo a pids; '' se sem variante
        if not pids:
            flash("Adicione ao menos um produto.","danger"); return redirect(url_for("create_sale"))
        cid = request.form.get("customer_id") or None
        if cid: cid = int(cid)
        payment = request.form.get("payment_method","").strip() or None
        notes   = request.form.get("notes","").strip() or None
        coupon_code = request.form.get("coupon_code","").strip().upper() or None
        now     = utc_now_iso(); user = get_current_user()

        buyer_name_free = request.form.get("buyer_name_free","").strip() or None
        shipping_name = request.form.get("shipping_name","").strip() or None
        shipping_document = request.form.get("shipping_document","").strip() or None
        shipping_cep = request.form.get("shipping_cep","").strip() or None
        shipping_street = request.form.get("shipping_street","").strip() or None
        shipping_number = request.form.get("shipping_number","").strip() or None
        shipping_complement = request.form.get("shipping_complement","").strip() or None
        shipping_neighborhood = request.form.get("shipping_neighborhood","").strip() or None
        shipping_city = request.form.get("shipping_city","").strip() or None
        shipping_state = request.form.get("shipping_state","").strip().upper()[:2] or None
        subtotal = Decimal("0")
        items_data = []

        # Compatibilidade: se vids vier menor que pids (form antigo), preenche com strings vazias
        while len(vids) < len(pids):
            vids.append("")

        for pid_s, qty_s, vid_s in zip(pids, qtys, vids):
            if not pid_s or not pid_s.strip():
                continue
            try:
                pid = int(pid_s)
                qty = int(qty_s or 1)
            except (ValueError, TypeError):
                continue
            if qty <= 0:
                continue
            prod = db.execute(
                "SELECT id,name,sku,sale_price,promo_price,promo_until,cost_price,"
                "stock_qty,is_active FROM products WHERE id=%s",
                (pid,),
            ).fetchone()
            if not prod or not prod["is_active"]:
                flash("Um dos produtos esta inativo ou nao existe mais.", "danger")
                return redirect(url_for("create_sale"))

            # Detecta se o produto TEM variantes ativas
            has_variants_row = db.execute(
                "SELECT 1 FROM product_variants WHERE product_id = %s AND is_active = 1 LIMIT 1",
                (pid,),
            ).fetchone()
            has_variants = bool(has_variants_row)

            variant_id = None
            variant_row = None
            if has_variants:
                # Produto com variantes EXIGE escolha
                if not vid_s or not vid_s.strip():
                    flash(
                        f"O produto «{prod['name']}» tem variantes. "
                        f"Escolha tamanho/cor antes de finalizar.",
                        "danger",
                    )
                    return redirect(url_for("create_sale"))
                try:
                    variant_id = int(vid_s)
                except (ValueError, TypeError):
                    flash("Variante invalida.", "danger")
                    return redirect(url_for("create_sale"))
                variant_row = db.execute(
                    "SELECT id, size, color, stock_qty, sale_price, promo_price, promo_until, "
                    "cost_price FROM product_variants "
                    "WHERE id = %s AND product_id = %s AND is_active = 1",
                    (variant_id, pid),
                ).fetchone()
                if not variant_row:
                    flash("Variante nao encontrada ou inativa.", "danger")
                    return redirect(url_for("create_sale"))
                avail = int(variant_row["stock_qty"] or 0)
                if avail < qty:
                    v_label = f"{variant_row['size'] or '-'} / {variant_row['color'] or '-'}"
                    flash(
                        f"Estoque insuficiente para {prod['name']} ({v_label}). "
                        f"Disponivel: {avail}.",
                        "danger",
                    )
                    return redirect(url_for("create_sale"))
                # Preco e custo VEM da variante (cada variante pode ter o seu)
                price = Decimal(str(effective_price(variant_row))).quantize(Decimal("0.01"))
                cost = Decimal(str(variant_row["cost_price"] or 0)).quantize(Decimal("0.01"))
            else:
                # Produto sem variantes: comportamento antigo
                if int(prod["stock_qty"] or 0) < qty:
                    flash(
                        f"Estoque insuficiente para {prod['name']}. "
                        f"Disponivel: {prod['stock_qty']}.",
                        "danger",
                    )
                    return redirect(url_for("create_sale"))
                price = Decimal(str(effective_price(prod))).quantize(Decimal("0.01"))
                cost = Decimal(str(prod["cost_price"] or 0)).quantize(Decimal("0.01"))

            subtotal += price * qty
            items_data.append({
                "pid": pid,
                "vid": variant_id,
                "qty": qty,
                "price": price,
                "cost": cost,
                "name": prod["name"],
                "variant_label": (
                    f"{variant_row['size'] or '-'} / {variant_row['color'] or '-'}"
                    if variant_row else None
                ),
            })
        if not items_data:
            flash("Adicione ao menos um produto valido.","danger")
            return redirect(url_for("create_sale"))

        disc_amt = Decimal("0")
        coupon_id = None
        if coupon_code:
            coup = db.execute("SELECT * FROM discount_coupons WHERE code=%s", (coupon_code,)).fetchone()
            valid_coupon, coupon_message, coupon_discount = validate_coupon_row(coup, subtotal, datetime.now(LOCAL_TZ))
            if not valid_coupon:
                flash(coupon_message, "warning")
                return redirect(url_for("create_sale"))
            disc_amt = coupon_discount
            coupon_id = coup["id"]

        manual_disc = Decimal("0")
        try:
            manual_disc = parse_money(request.form.get("discount","0") or "0") or Decimal("0")
        except Exception:
            manual_disc = Decimal("0")
        manual_disc = max(Decimal("0"), manual_disc)
        disc_amt = min(subtotal, disc_amt + manual_disc)
        total = max(Decimal("0"), subtotal - disc_amt)

        with transaction() as db2:
            sale_num = _make_sale_number()
            # v9: payment_status — PIX nasce pendente; cartão/dinheiro nascem pagos.
            # Operador presencial vê pagamento imediato, exceto PIX que precisa confirmação.
            if payment == "pix":
                payment_status = "pending"
                pay_confirmed_at = None
                pay_confirmed_by = None
            else:
                payment_status = "paid"
                pay_confirmed_at = now
                pay_confirmed_by = user["display_name"]

            sid = insert_returning_id("""INSERT INTO sales(
                customer_id,operator_name,subtotal,discount_amount,discount_coupon_id,total,
                payment_method,notes,created_at,sale_number,buyer_name_free,
                shipping_name,shipping_document,shipping_cep,shipping_street,shipping_number,
                shipping_complement,shipping_neighborhood,shipping_city,shipping_state,
                payment_status,payment_confirmed_at,payment_confirmed_by
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    cid, user["display_name"], str(subtotal), str(disc_amt), coupon_id, str(total),
                    payment, notes, now, sale_num, buyer_name_free,
                    shipping_name, shipping_document, shipping_cep, shipping_street, shipping_number,
                    shipping_complement, shipping_neighborhood, shipping_city, shipping_state,
                    payment_status, pay_confirmed_at, pay_confirmed_by
                ))
            affected_pids = set()
            for item in items_data:
                if item["vid"]:
                    # Re-checa estoque sob lock implicito da transacao
                    v_now = db2.execute(
                        "SELECT stock_qty FROM product_variants WHERE id = %s",
                        (item["vid"],),
                    ).fetchone()
                    if not v_now or int(v_now["stock_qty"] or 0) < item["qty"]:
                        raise ValueError(
                            f"Estoque insuficiente para {item['name']} "
                            f"({item['variant_label']})"
                        )
                    # Baixa estoque da VARIANTE
                    db2.execute(
                        "UPDATE product_variants SET stock_qty = stock_qty - %s WHERE id = %s",
                        (item["qty"], item["vid"]),
                    )
                    # sale_item com variant_id
                    db2.execute(
                        "INSERT INTO sale_items(sale_id, product_id, variant_id, qty, unit_price, cost_price) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (sid, item["pid"], item["vid"], item["qty"],
                         str(item["price"]), str(item["cost"])),
                    )
                    # stock_movement com variant_id
                    db2.execute(
                        "INSERT INTO stock_movements"
                        "(product_id, variant_id, type, qty, reason, sale_id, operator_name, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (item["pid"], item["vid"], "sale", item["qty"],
                         f"Venda #{sid} ({item['variant_label']})",
                         sid, user["display_name"], now),
                    )
                    affected_pids.add(item["pid"])
                else:
                    # Produto sem variantes - comportamento antigo
                    stock_now = db2.execute(
                        "SELECT stock_qty FROM products WHERE id=%s",
                        (item["pid"],),
                    ).fetchone()
                    if not stock_now or int(stock_now["stock_qty"] or 0) < item["qty"]:
                        raise ValueError(f"Estoque insuficiente para {item['name']}")
                    db2.execute(
                        "INSERT INTO sale_items(sale_id, product_id, qty, unit_price, cost_price) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (sid, item["pid"], item["qty"], str(item["price"]), str(item["cost"])),
                    )
                    db2.execute(
                        "INSERT INTO stock_movements"
                        "(product_id, type, qty, reason, sale_id, operator_name, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (item["pid"], "sale", item["qty"], f"Venda #{sid}",
                         sid, user["display_name"], now),
                    )
                    db2.execute(
                        "UPDATE products SET stock_qty = stock_qty - %s, updated_at = %s "
                        "WHERE id = %s",
                        (item["qty"], now, item["pid"]),
                    )
            # Re-sincroniza products.stock_qty para todos os pids que tiveram
            # baixa por variante. resync_product_stock e idempotente.
            for pid_to_sync in affected_pids:
                resync_product_stock(db2, pid_to_sync)
            if coupon_id:
                db2.execute("UPDATE discount_coupons SET used_count=used_count+1 WHERE id=%s", (coupon_id,))
            if cid:
                pts = int(float(total) * float(get_setting("loyalty_points_per_real","1")))
                db2.execute("UPDATE customers SET loyalty_points=loyalty_points+%s WHERE id=%s", (pts, cid))
        flash(f"Venda #{sid} registrada! Total: R$ {format_money(total)}","success")
        # Sugestão de cadastro: nome avulso preenchido e sem customer_id vinculado
        if buyer_name_free and not cid:
            from urllib.parse import urlencode
            qs = urlencode({"name": buyer_name_free})
            flash(
                f'Cliente "{buyer_name_free}" não está cadastrado. '
                f'<a href="/customers/new?{qs}" style="font-weight:700;text-decoration:underline">Cadastrar agora</a>',
                "info-html"
            )
        # Se método=PIX e existe conta PIX ativa, vai direto pro detalhe
        # com autogen=1 — auto-dispara geração do QR e rola até o card.
        if payment == "pix":
            has_pix_account = db.execute(
                "SELECT 1 FROM pix_provider_accounts WHERE is_active = 1 LIMIT 1"
            ).fetchone()
            if has_pix_account:
                return redirect(url_for("sale_detail", sid=sid) + "?autogen=1#pix-card")
        return redirect(url_for("list_sales"))

    # GET: lista de produtos com flag has_variants para o front
    products = db.execute("""
        SELECT p.*,
               EXISTS(
                   SELECT 1 FROM product_variants v
                   WHERE v.product_id = p.id AND v.is_active = 1 AND v.stock_qty > 0
               ) AS has_variants
          FROM products p
         WHERE p.is_active = 1
           AND (
               p.stock_qty > 0
               OR EXISTS(
                   SELECT 1 FROM product_variants v
                   WHERE v.product_id = p.id AND v.is_active = 1 AND v.stock_qty > 0
               )
           )
         ORDER BY p.name
    """).fetchall()
    customers = db.execute("SELECT id,name,phone FROM customers ORDER BY name").fetchall()
    return render_template("create_sale.html", products=products, customers=customers)

# ─── CUPONS DE DESCONTO ─────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# Cupons extraídos para blueprints/marketing.py (Onda 2 — Etapa E).
# ─────────────────────────────────────────────────────────────

# ─── CONTABILIDADE ──────────────────────────────────────────


@sales_bp.route("/sales/<int:sid>", endpoint="sale_detail")
@login_required
def sale_detail(sid):
    db = get_db()
    s = db.execute("""
        SELECT s.*, c.name as customer_name, c.phone as customer_phone
        FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
        WHERE s.id=%s""", (sid,)).fetchone()
    if not s: abort(404)
    items = db.execute("""
        SELECT si.*, p.name as product_name, p.sku, p.unit,
               v.size AS variant_size, v.color AS variant_color
        FROM sale_items si
        JOIN products p ON p.id = si.product_id
        LEFT JOIN product_variants v ON v.id = si.variant_id
        WHERE si.sale_id = %s
        ORDER BY si.id
    """, (sid,)).fetchall()
    returns = db.execute("""
        SELECT sr.*, p.name as product_name
        FROM sale_returns sr JOIN products p ON p.id=sr.product_id
        WHERE sr.sale_id=%s""", (sid,)).fetchall()
    credit = db.execute("SELECT * FROM store_credits WHERE source_sale_id=%s", (sid,)).fetchone()
    shipping = _sale_shipping_snapshot(s, {"name": s["customer_name"]} if s["customer_name"] else None)
    # Multi-PSP: contas ativas para o seletor + cobranca atual (se existir)
    pix_accounts = db.execute(
        "SELECT id, provider, label, is_default FROM pix_provider_accounts "
        "WHERE is_active = 1 ORDER BY is_default DESC, id ASC"
    ).fetchall()
    active_charge = db.execute(
        "SELECT * FROM pix_charges WHERE sale_id = %s ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    return render_template(
        "sale_detail.html",
        sale=s, items=items, returns=returns, credit=credit,
        shipping=shipping, shipping_ready=_shipping_label_has_data(shipping),
        pix_accounts=pix_accounts, active_charge=active_charge,
    )

# ─── PIX dinamico copia-e-cola (Fase 1: sem PSP) ────────────
def _pix_settings_for_sale(sale_row) -> dict | None:
    """Resolve config PIX a partir de store_settings. Fallback razoavel
    para nome/cidade se admin nao preencher os campos dedicados."""
    db = get_db()
    rows = db.execute(
        "SELECT key, value FROM store_settings WHERE key IN "
        "('pix_key','pix_key_type','pix_merchant_name','pix_merchant_city',"
        " 'store_legal_name','store_name','store_address_city')"
    ).fetchall()
    cfg = {r["key"]: (r["value"] or "").strip() for r in rows}
    pix_key = cfg.get("pix_key", "")
    pix_type = cfg.get("pix_key_type", "")
    if not pix_key or not pix_type:
        return None
    merchant_name = (
        cfg.get("pix_merchant_name")
        or cfg.get("store_legal_name")
        or cfg.get("store_name")
        or "Recebedor"
    )
    merchant_city = (
        cfg.get("pix_merchant_city")
        or cfg.get("store_address_city")
        or "Brasil"
    )
    return {
        "key": pix_key,
        "key_type": pix_type,
        "merchant_name": merchant_name,
        "merchant_city": merchant_city,
    }

def _pix_brcode_for_sale(sid: int) -> tuple[str, dict] | None:
    """Retorna (brcode, contexto) ou None se PIX nao configurado /
    venda invalida / valor zero. Levanta ValueError com mensagem de
    validacao para o caller."""
    from pix import build_brcode
    db = get_db()
    sale = db.execute(
        "SELECT id, total, sale_number, status FROM sales WHERE id = %s",
        (sid,),
    ).fetchone()
    if not sale:
        return None
    cfg = _pix_settings_for_sale(sale)
    if not cfg:
        return None
    # TxID = sale_number quando existe, senao "VENDA<id>"
    raw_txid = sale["sale_number"] or f"VENDA{sale['id']}"
    brcode = build_brcode(
        pix_key=cfg["key"],
        key_type=cfg["key_type"],
        merchant_name=cfg["merchant_name"],
        merchant_city=cfg["merchant_city"],
        amount=sale["total"],
        txid=raw_txid,
    )
    return brcode, {
        "sale_id": sale["id"],
        "amount": sale["total"],
        "txid": raw_txid,
        "merchant_name": cfg["merchant_name"],
    }



@sales_bp.route("/sales/<int:sid>/pix.txt", endpoint="sale_pix_brcode")
@login_required
def sale_pix_brcode(sid: int):
    try:
        result = _pix_brcode_for_sale(sid)
    except ValueError as exc:
        return Response(f"PIX nao gerado: {exc}", status=400, mimetype="text/plain")
    if not result:
        return Response("PIX nao configurado nas Configuracoes da loja.", status=404, mimetype="text/plain")
    brcode, _ = result
    return Response(brcode, mimetype="text/plain; charset=utf-8")



@sales_bp.route("/sales/<int:sid>/pix.png", endpoint="sale_pix_qr")
@login_required
def sale_pix_qr(sid: int):
    from pix import render_qr_png
    try:
        result = _pix_brcode_for_sale(sid)
    except ValueError as exc:
        abort(400, description=f"PIX nao gerado: {exc}")
    if not result:
        abort(404, description="PIX nao configurado.")
    brcode, _ = result
    png = render_qr_png(brcode, box_size=8, border=2)
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp



@sales_bp.route("/sales/<int:sid>/pix-info.json", endpoint="sale_pix_info")
@login_required
def sale_pix_info(sid: int):
    """JSON com brcode + metadados, para AJAX no template."""
    try:
        result = _pix_brcode_for_sale(sid)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not result:
        return jsonify({"ok": False, "error": "PIX nao configurado nas Configuracoes."}), 404
    brcode, ctx = result
    return jsonify({
        "ok": True,
        "brcode": brcode,
        "amount": str(ctx["amount"]),
        "txid": ctx["txid"],
        "merchant_name": ctx["merchant_name"],
        "qr_url": url_for("sale_pix_qr", sid=sid),
    })

# ─── Multi-PSP: contas, cobrancas, webhooks ─────────────────
def _row_to_dict_local(row):
    """Local: register_v3_routes não tem acesso ao _row_to_dict do escopo
    do primeiro create_app. Versão local para evitar NameError."""
    if row is None:
        return None
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        return row

def _list_active_pix_accounts() -> list[dict]:
    rows = get_db().execute(
        "SELECT id, provider, label, is_active, is_default "
        "FROM pix_provider_accounts WHERE is_active = 1 ORDER BY is_default DESC, id ASC"
    ).fetchall()
    return [_row_to_dict_local(r) for r in rows]

def _load_account(account_id: int) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM pix_provider_accounts WHERE id = %s", (account_id,)
    ).fetchone()
    return _row_to_dict_local(row) if row else None

def _build_provider_for_account(account_id: int):
    from pix_providers import build_provider, ProviderError, ProviderNotConfigured
    row = _load_account(account_id)
    if not row:
        raise ProviderError("Conta PIX nao encontrada.")
    if not int(row.get("is_active") or 0):
        raise ProviderError("Conta PIX desativada.")
    try:
        cred_json = psp_decrypt(row.get("credentials_encrypted") or "")
        credentials = json.loads(cred_json) if cred_json else {}
    except json.JSONDecodeError:
        raise ProviderError("Credenciais corrompidas.")
    return build_provider(row, credentials), row



@sales_bp.route("/sales/<int:sid>/charge-pix", methods=["POST"], endpoint="create_pix_charge_for_sale")
@login_required
def create_pix_charge_for_sale(sid: int):
    """Cria uma cobranca PIX em uma conta PSP escolhida no momento da venda.

    Body: account_id (FK pix_provider_accounts).
    Idempotencia: se ja existe pix_charge pending para a venda+conta,
    retorna a existente em vez de duplicar."""
    from pix_providers import ChargeRequest, ProviderError, ProviderNotConfigured
    try:
        validate_csrf_or_abort()
        account_id = request.form.get("account_id", type=int)
        if not account_id:
            return jsonify({"ok": False, "error": "Conta PIX nao informada."}), 400

        db = get_db()
        sale = db.execute(
            "SELECT id, total, sale_number FROM sales WHERE id = %s AND status != 'cancelled'",
            (sid,),
        ).fetchone()
        if not sale:
            return jsonify({"ok": False, "error": "Venda nao encontrada ou cancelada."}), 404
        try:
            amount = Decimal(str(sale["total"] or "0"))
        except Exception:
            return jsonify({"ok": False, "error": "Valor da venda invalido."}), 400
        if amount <= 0:
            return jsonify({"ok": False, "error": "Valor da venda deve ser positivo."}), 400

        # Idempotencia: reusa cobranca pending existente
        existing = db.execute(
            "SELECT * FROM pix_charges WHERE sale_id = %s AND account_id = %s AND status = 'pending'",
            (sid, account_id),
        ).fetchone()
        if existing:
            return jsonify({
                "ok": True,
                "charge_id": existing["id"],
                "brcode": existing["brcode"],
                "status": existing["status"],
                "provider": existing["provider"],
                "reused": True,
            })

        try:
            provider, account_row = _build_provider_for_account(account_id)
        except ProviderNotConfigured as exc:
            return jsonify({"ok": False, "error": f"Conta nao configurada: {exc}"}), 400
        except ProviderError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        txid_raw = sale["sale_number"] or f"VENDA{sale['id']}"
        try:
            result = provider.create_charge(ChargeRequest(
                amount=amount,
                txid=txid_raw,
                description=f"Venda {sale['id']} Tonton",
            ))
        except ProviderError as exc:
            current_app.logger.error("create_charge falhou: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

        now = utc_now_iso()
        charge_id = db.execute(
            "INSERT INTO pix_charges "
            "(sale_id, account_id, provider, provider_charge_id, txid, amount, brcode, status, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (sid, account_id, result.provider, result.provider_charge_id,
             txid_raw, str(amount), result.brcode, result.status, now, now),
        ).fetchone()["id"]
        db.commit()
        audit_log(
            "pix_charge_created",
            target_id=sid,
            extra=f"provider={result.provider} account={account_id} charge_id={charge_id}",
        )
        return jsonify({
            "ok": True,
            "charge_id": charge_id,
            "brcode": result.brcode,
            "status": result.status,
            "provider": result.provider,
            "qr_url": url_for("pix_charge_qr", charge_id=charge_id),
            "expires_at": result.expires_at,
        })
    except Exception as exc:
        # Defesa em profundidade: qualquer falha não-prevista vira JSON com mensagem útil
        # ao invés de HTML 500 (que o JS interpreta como "Falha de rede").
        current_app.logger.exception("charge-pix: erro nao tratado para sale=%s", sid)
        return jsonify({
            "ok": False,
            "error": f"Erro interno ao gerar cobranca: {type(exc).__name__}. "
                     f"Verifique os logs do servidor."
        }), 500



@sales_bp.route("/pix-charges/<int:charge_id>/qr.png", endpoint="pix_charge_qr")
@login_required
def pix_charge_qr(charge_id: int):
    from pix import render_qr_png
    row = get_db().execute(
        "SELECT brcode FROM pix_charges WHERE id = %s", (charge_id,)
    ).fetchone()
    if not row or not row["brcode"]:
        abort(404)
    png = render_qr_png(row["brcode"], box_size=8, border=2)
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp



@sales_bp.route("/pix-charges/<int:charge_id>/status", endpoint="pix_charge_status")
@login_required
def pix_charge_status(charge_id: int):
    """Polling endpoint: sincroniza com PSP e retorna status atual."""
    from pix_providers import ProviderError
    db = get_db()
    row = db.execute(
        "SELECT * FROM pix_charges WHERE id = %s", (charge_id,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Cobranca nao encontrada."}), 404
    # Status terminal: nao consulta de novo
    if row["status"] in ("paid", "expired", "cancelled", "refunded"):
        return jsonify({
            "ok": True,
            "status": row["status"],
            "paid_at": row["paid_at"],
            "payer_name": row["payer_name"],
        })
    # Manual nao tem PSP - status so muda via /mark-paid
    if row["provider"] == "manual":
        return jsonify({"ok": True, "status": row["status"]})
    # Consulta PSP
    try:
        provider, _ = _build_provider_for_account(row["account_id"])
        result = provider.get_charge(row["provider_charge_id"])
    except ProviderError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    if result.status != row["status"]:
        _apply_status_update(charge_id, result)
    return jsonify({
        "ok": True,
        "status": result.status,
        "paid_at": result.paid_at,
        "payer_name": result.payer_name,
    })



@sales_bp.route("/pix-charges/<int:charge_id>/mark-paid", methods=["POST"], endpoint="pix_charge_mark_paid")
@require_role("admin", "operator")
def pix_charge_mark_paid(charge_id: int):
    """Marca como pago manualmente (provider=manual ou override admin)."""
    validate_csrf_or_abort()
    db = get_db()
    row = db.execute(
        "SELECT * FROM pix_charges WHERE id = %s", (charge_id,)
    ).fetchone()
    if not row:
        abort(404)
    if row["status"] == "paid":
        flash("Cobranca ja estava marcada como paga.", "info")
        return redirect(url_for("sale_detail", sid=row["sale_id"]))
    now = utc_now_iso()
    db.execute(
        "UPDATE pix_charges SET status = 'paid', paid_at = %s, paid_amount = amount, "
        "updated_at = %s WHERE id = %s",
        (now, now, charge_id),
    )
    db.commit()
    audit_log(
        "pix_charge_marked_paid",
        target_id=row["sale_id"],
        extra=f"charge_id={charge_id} provider={row['provider']}",
    )
    flash("Cobranca marcada como paga.", "success")
    return redirect(url_for("sale_detail", sid=row["sale_id"]))

def _apply_status_update(charge_id: int, result) -> None:
    """Aplica StatusResult a uma pix_charge. Idempotente."""
    db = get_db()
    now = utc_now_iso()
    db.execute(
        "UPDATE pix_charges SET status = %s, paid_at = %s, paid_amount = %s, "
        "payer_name = %s, raw_webhook = %s, updated_at = %s WHERE id = %s",
        (
            result.status,
            result.paid_at,
            str(result.paid_amount) if result.paid_amount is not None else None,
            result.payer_name,
            json.dumps(result.raw, ensure_ascii=False, default=str)[:50000] if result.raw else None,
            now,
            charge_id,
        ),
    )
    db.commit()



@sales_bp.route("/webhooks/pix/<provider_slug>", methods=["POST"], endpoint="pix_webhook")
def pix_webhook(provider_slug: str):
    """Webhook generico. Encontra a conta correspondente, valida assinatura
    e atualiza status. Sem auth - protegido por mTLS (Inter) ou
    validacao no provider.parse_webhook."""
    from pix_providers import ProviderError, PROVIDER_REGISTRY
    if provider_slug not in PROVIDER_REGISTRY:
        current_app.logger.warning("Webhook PIX: provider desconhecido %s", provider_slug)
        return jsonify({"ok": False}), 404
    body = request.get_data() or b""
    # Tenta cada conta ativa do provider ate alguma validar.
    # Em deploys com 1 conta por provider isso e direto.
    db = get_db()
    accounts = db.execute(
        "SELECT id FROM pix_provider_accounts WHERE provider = %s AND is_active = 1",
        (provider_slug,),
    ).fetchall()
    if not accounts:
        return jsonify({"ok": False, "error": "Sem conta ativa."}), 404
    headers = dict(request.headers)
    for acc in accounts:
        try:
            provider, _ = _build_provider_for_account(acc["id"])
        except ProviderError:
            continue
        if not provider.verify_webhook(headers, body):
            continue
        result = provider.parse_webhook(headers, body)
        if not result:
            continue
        charge = db.execute(
            "SELECT id, sale_id, status FROM pix_charges "
            "WHERE provider = %s AND provider_charge_id = %s",
            (result.provider, result.provider_charge_id),
        ).fetchone()
        if not charge:
            current_app.logger.warning(
                "Webhook PIX %s: charge_id %s nao encontrado",
                provider_slug, result.provider_charge_id,
            )
            return jsonify({"ok": True, "ignored": True}), 200
        if charge["status"] != "paid":
            _apply_status_update(charge["id"], result)
            audit_log(
                "pix_webhook_paid",
                target_id=charge["sale_id"],
                extra=f"provider={provider_slug} charge_id={charge['id']}",
            )
        return jsonify({"ok": True}), 200
    current_app.logger.warning("Webhook PIX %s: nenhuma conta validou", provider_slug)
    return jsonify({"ok": False, "error": "Validacao falhou."}), 400



@sales_bp.route("/sales/<int:sid>/dispatch-label.pdf", endpoint="sale_dispatch_label")
@login_required
def sale_dispatch_label(sid):
    db = get_db()
    s = db.execute(
        """
        SELECT s.*, c.name as customer_name
        FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
        WHERE s.id=%s
        """,
        (sid,),
    ).fetchone()
    if not s:
        abort(404)

    shipping = _sale_shipping_snapshot(s, {"name": s["customer_name"]} if s["customer_name"] else None)
    if not _shipping_label_has_data(shipping):
        return ("Endereço de despacho incompleto para esta venda.", 400)

    width_mm = _mm_setting("dispatch_label_width_mm", "100")
    height_mm = _mm_setting("dispatch_label_height_mm", "150")
    px_mm = 11.8
    w, h = max(int(width_mm * px_mm), 700), max(int(height_mm * px_mm), 900)

    page = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle([0, 0, w - 1, h - 1], outline="#191714", width=3)

    try:
        font_title = ImageFont.truetype(str(BASE_DIR/"static"/"fonts"/"Poppins-Medium.ttf"), 34)
        font_body = ImageFont.truetype(str(BASE_DIR/"static"/"fonts"/"DejaVuSans.ttf"), 30)
        font_meta = ImageFont.truetype(str(BASE_DIR/"static"/"fonts"/"DejaVuSans.ttf"), 22)
    except Exception:
        font_title = font_body = font_meta = None

    y = 36
    draw.text((36, y), "ETIQUETA DE DESPACHO", fill="#191714", font=font_title)
    y += 66
    draw.text((36, y), f"Pedido #{s['sale_number'] or s['id']}", fill="#7b6a61", font=font_meta)
    y += 54

    for line in _shipping_lines(shipping):
        draw.text((36, y), line, fill="#191714", font=font_body)
        y += 54

    y += 10
    draw.line((36, y, w - 36, y), fill="#e8ddd3", width=2)
    y += 22
    draw.text((36, y), "tonton", fill="#b5924c", font=font_title)
    if s["created_at"]:
        draw.text((w - 36, y + 8), f"Emitido em {format_datetime_local(s['created_at'])}", fill="#7b6a61", font=font_meta, anchor="ra")

    buf = io.BytesIO()
    page.save(buf, format="PDF", resolution=300.0)
    return send_pdf_download(buf.getvalue(), f"despacho-pedido-{s['sale_number'] or s['id']}.pdf")



@sales_bp.route("/sales/<int:sid>/pre-nota.pdf", endpoint="sale_prefatura_pdf")
@login_required
def sale_prefatura_pdf(sid):
    db = get_db()
    sale = db.execute(
        """
        SELECT s.*, c.name as customer_name, c.phone as customer_phone, c.email as customer_email
        FROM sales s
        LEFT JOIN customers c ON c.id = s.customer_id
        WHERE s.id = %s
        """,
        (sid,),
    ).fetchone()
    if not sale:
        abort(404)

    items = db.execute(
        """
        SELECT si.*, p.name as product_name, p.sku, p.unit, p.ncm, p.cfop, p.origin_code
        FROM sale_items si
        JOIN products p ON p.id = si.product_id
        WHERE si.sale_id = %s
        ORDER BY si.id
        """,
        (sid,),
    ).fetchall()

    shipping = _sale_shipping_snapshot(sale, {"name": sale["customer_name"]} if sale["customer_name"] else None)
    store = _store_invoice_profile()
    prefatura = _prefatura_payload(sale, items, shipping, store)

    w, h = 1240, 1754
    page = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle([0, 0, w - 1, h - 1], outline="#1a0f0a", width=2)

    try:
        font_title = ImageFont.truetype(str(BASE_DIR/"static"/"fonts"/"Poppins-Medium.ttf"), 34)
        font_sub = ImageFont.truetype(str(BASE_DIR/"static"/"fonts"/"Poppins-Medium.ttf"), 22)
        font_body = ImageFont.truetype(str(BASE_DIR/"static"/"fonts"/"DejaVuSans.ttf"), 22)
        font_small = ImageFont.truetype(str(BASE_DIR/"static"/"fonts"/"DejaVuSans.ttf"), 18)
    except Exception:
        font_title = font_sub = font_body = font_small = None

    y = 42
    draw.text((46, y), "PRÉ-NOTA / PRÉ-FATURAMENTO", fill="#1a0f0a", font=font_title)
    draw.text((w - 46, y + 8), f"Pedido #{sale['sale_number'] or sale['id']}", fill="#7b6a61", font=font_sub, anchor="ra")
    y += 66
    draw.line((46, y, w - 46, y), fill="#e8ddd3", width=2)
    y += 24

    draw.text((46, y), "DADOS DA LOJA", fill="#b5924c", font=font_sub)
    y += 34
    for line in _store_invoice_lines(store) or [store.get("trade_name") or "Tonton"]:
        draw.text((46, y), line, fill="#1a0f0a", font=font_body)
        y += 30
    draw.text((46, y), f"Regime tributário: {store.get('tax_regime') or 'MEI'}", fill="#1a0f0a", font=font_body)
    y += 30
    draw.text((46, y), f"Natureza da operação: {store.get('operation_nature') or 'Venda de mercadoria'}", fill="#1a0f0a", font=font_body)
    y += 30

    y += 10
    draw.text((46, y), "DADOS DO COMPRADOR", fill="#b5924c", font=font_sub)
    y += 34
    customer_lines = []
    if shipping.get("name"):
        customer_lines.append(shipping["name"])
    elif sale["customer_name"]:
        customer_lines.append(str(sale["customer_name"]))
    elif sale["buyer_name_free"]:
        customer_lines.append(str(sale["buyer_name_free"]))
    if shipping.get("document"):
        customer_lines.append(f"CPF/CNPJ {shipping['document']}")
    if sale["customer_phone"]:
        customer_lines.append(f"Telefone {sale['customer_phone']}")
    if sale["customer_email"]:
        customer_lines.append(f"E-mail {sale['customer_email']}")
    customer_lines.extend(_shipping_lines(shipping))
    if not customer_lines:
        customer_lines = ["Cliente não identificado"]
    deduped = []
    for line in customer_lines:
        if line and line not in deduped:
            deduped.append(line)
    for line in deduped:
        draw.text((46, y), line, fill="#1a0f0a", font=font_body)
        y += 30

    y += 10
    draw.text((46, y), "ITENS", fill="#b5924c", font=font_sub)
    y += 40
    draw.rectangle([46, y, w - 46, y + 38], fill="#f5ede3")
    draw.text((58, y + 9), "Produto", fill="#1a0f0a", font=font_small)
    draw.text((800, y + 9), "Qtd", fill="#1a0f0a", font=font_small)
    draw.text((900, y + 9), "Unit.", fill="#1a0f0a", font=font_small)
    draw.text((w - 58, y + 9), "Total", fill="#1a0f0a", font=font_small, anchor="ra")
    y += 50

    for item in items:
        product_label = f"{item['product_name']}"
        if item["sku"]:
            product_label += f" · SKU {item['sku']}"
        wrapped = _wrap_for_pdf(draw, product_label, font_small, 700)
        line_height = 24
        row_height = max(32, len(wrapped) * line_height)
        for idx2, line in enumerate(wrapped):
            draw.text((58, y + (idx2 * line_height)), line, fill="#1a0f0a", font=font_small)
        draw.text((810, y), str(item["qty"]), fill="#1a0f0a", font=font_small)
        draw.text((900, y), f"R$ {format_money(item['unit_price'])}", fill="#1a0f0a", font=font_small)
        total_item = Decimal(str(item["unit_price"] or 0)) * Decimal(str(item["qty"] or 0))
        draw.text((w - 58, y), f"R$ {format_money(total_item)}", fill="#1a0f0a", font=font_small, anchor="ra")
        y += row_height + 10
        draw.line((46, y, w - 46, y), fill="#efe7df", width=1)
        y += 10

    y += 8
    summary_x = 760
    draw.text((summary_x, y), f"Subtotal: R$ {format_money(sale['subtotal'])}", fill="#1a0f0a", font=font_body)
    y += 30
    draw.text((summary_x, y), f"Desconto: R$ {format_money(sale['discount_amount'])}", fill="#1a0f0a", font=font_body)
    y += 30
    draw.text((summary_x, y), f"Total: R$ {format_money(sale['total'])}", fill="#1a0f0a", font=font_title)
    y += 44
    draw.text((46, y), f"Forma de pagamento: {(sale['payment_method'] or 'não informado').upper()}", fill="#1a0f0a", font=font_body)
    y += 30
    draw.text((46, y), f"Emitido em: {format_datetime_local(sale['created_at'])}", fill="#7b6a61", font=font_small)
    y += 24
    if sale["notes"]:
        draw.text((46, y), "Observações:", fill="#1a0f0a", font=font_small)
        y += 24
        for line in _wrap_for_pdf(draw, sale["notes"], font_small, w - 92):
            draw.text((46, y), line, fill="#7b6a61", font=font_small)
            y += 22

    footer = "Documento interno para pré-faturamento. Validar tributação, CFOP, NCM e dados fiscais antes da emissão da nota."
    for line in _wrap_for_pdf(draw, footer, font_small, w - 92):
        draw.text((46, h - 120 + (_wrap_for_pdf(draw, footer, font_small, w - 92).index(line) * 20)), line, fill="#7b6a61", font=font_small)

    buf = io.BytesIO()
    page.save(buf, format="PDF", resolution=300.0)
    return send_pdf_download(buf.getvalue(), f"pre-nota-pedido-{sale['sale_number'] or sale['id']}.pdf")



@sales_bp.route("/sales/<int:sid>/pre-nota.json", endpoint="sale_prefatura_json")
@login_required
def sale_prefatura_json(sid):
    db = get_db()
    sale = db.execute(
        """
        SELECT s.*, c.name as customer_name, c.phone as customer_phone, c.email as customer_email
        FROM sales s
        LEFT JOIN customers c ON c.id = s.customer_id
        WHERE s.id = %s
        """,
        (sid,),
    ).fetchone()
    if not sale:
        abort(404)

    items = db.execute(
        """
        SELECT si.*, p.name as product_name, p.sku, p.unit, p.ncm, p.cfop, p.origin_code
        FROM sale_items si
        JOIN products p ON p.id = si.product_id
        WHERE si.sale_id = %s
        ORDER BY si.id
        """,
        (sid,),
    ).fetchall()

    shipping = _sale_shipping_snapshot(sale, {"name": sale["customer_name"]} if sale["customer_name"] else None)
    store = _store_invoice_profile()
    payload = _prefatura_payload(sale, items, shipping, store)
    response = current_app.response_class(
        json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype="application/json",
    )
    response.headers["Content-Disposition"] = f"attachment; filename=pre-nota-pedido-{sale['sale_number'] or sale['id']}.json"
    return response



@sales_bp.route("/sales/<int:sid>/pre-nota.csv", endpoint="sale_prefatura_csv")
@login_required
def sale_prefatura_csv(sid):
    db = get_db()
    sale = db.execute(
        """
        SELECT s.*, c.name as customer_name, c.phone as customer_phone, c.email as customer_email
        FROM sales s
        LEFT JOIN customers c ON c.id = s.customer_id
        WHERE s.id = %s
        """,
        (sid,),
    ).fetchone()
    if not sale:
        abort(404)

    items = db.execute(
        """
        SELECT si.*, p.name as product_name, p.sku, p.unit, p.ncm, p.cfop, p.origin_code
        FROM sale_items si
        JOIN products p ON p.id = si.product_id
        WHERE si.sale_id = %s
        ORDER BY si.id
        """,
        (sid,),
    ).fetchall()

    shipping = _sale_shipping_snapshot(sale, {"name": sale["customer_name"]} if sale["customer_name"] else None)
    store = _store_invoice_profile()
    payload = _prefatura_payload(sale, items, shipping, store)

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([
        "numero_pedido", "emitido_em", "natureza_operacao", "regime_tributario",
        "emitente_razao_social", "emitente_cnpj", "emitente_ie",
        "destinatario_nome", "destinatario_cpf_cnpj", "destinatario_cep",
        "destinatario_cidade", "destinatario_uf", "forma_pagamento",
        "subtotal", "desconto", "total",
        "item", "sku", "descricao", "ncm", "cfop", "origem",
        "unidade", "quantidade", "valor_unitario", "valor_total"
    ])
    for item in payload["itens"]:
        writer.writerow([
            payload["documento"]["numero_pedido"],
            payload["documento"]["emitido_em"],
            payload["documento"]["natureza_operacao"],
            payload["documento"]["regime_tributario_loja"],
            payload["emitente"]["razao_social"] or payload["emitente"]["nome_fantasia"],
            payload["emitente"]["cnpj"],
            payload["emitente"]["inscricao_estadual"],
            payload["destinatario"]["nome"],
            payload["destinatario"]["cpf_cnpj"],
            payload["destinatario"]["cep"],
            payload["destinatario"]["cidade"],
            payload["destinatario"]["uf"],
            payload["documento"]["forma_pagamento"],
            payload["totais"]["subtotal"],
            payload["totais"]["desconto"],
            payload["totais"]["total"],
            item["item"],
            item["sku"],
            item["descricao"],
            item["ncm"],
            item["cfop"],
            item["origem"],
            item["unidade"],
            item["quantidade"],
            item["valor_unitario"],
            item["valor_total"],
        ])

    resp = current_app.response_class(buf.getvalue(), mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = f"attachment; filename=pre-nota-pedido-{sale['sale_number'] or sale['id']}.csv"
    return resp

# ─── CANCELAR VENDA ─────────────────────────────────────────


@sales_bp.route("/sales/<int:sid>/cancel", methods=["POST"], endpoint="cancel_sale")
@login_required
def cancel_sale(sid):
    validate_csrf_or_abort()
    db = get_db()
    s = db.execute("SELECT * FROM sales WHERE id=%s", (sid,)).fetchone()
    if not s: abort(404)
    if s["status"] == "cancelled":
        flash("Venda já está cancelada.","warning")
        return redirect(url_for("sale_detail", sid=sid))
    reason     = request.form.get("reason","").strip() or "Cancelamento"
    restock    = request.form.get("restock","1") == "1"
    gen_credit = request.form.get("gen_credit","0") == "1"
    user = get_current_user(); now = utc_now_iso()
    with transaction() as db2:
        # Cancel the sale
        db2.execute("""UPDATE sales SET status='cancelled', cancelled_at=%s, cancelled_by=%s,
            cancel_reason=%s WHERE id=%s""", (now, user["display_name"], reason, sid))
        # Restock items
        if restock:
            items = db2.execute("SELECT * FROM sale_items WHERE sale_id=%s", (sid,)).fetchall()
            for item in items:
                db2.execute("UPDATE products SET stock_qty=stock_qty+%s, updated_at=%s WHERE id=%s",
                            (item["qty"], now, item["product_id"]))
                db2.execute("""INSERT INTO stock_movements(product_id,type,qty,reason,sale_id,operator_name,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                    (item["product_id"],"return",item["qty"],f"Cancelamento venda #{sid}",sid,user["display_name"],now))
        # Generate store credit
        if gen_credit and s["total"] and float(s["total"]) > 0:
            cname = db2.execute("SELECT name FROM customers WHERE id=%s", (s["customer_id"],)).fetchone()
            cname = cname["name"] if cname else (s["buyer_name_free"] or "Cliente")
            new_credit_id = insert_returning_id("""INSERT INTO store_credits(customer_id,customer_name,amount,reason,
                source_sale_id,status,created_at,created_by)
                VALUES(%s,%s,%s,%s,%s,'active',%s,%s)""",
                (s["customer_id"], cname, s["total"],
                 f"Crédito por cancelamento venda #{sid}", sid, now, user["display_name"]))
            db2.execute("UPDATE sales SET credit_id=%s WHERE id=%s", (new_credit_id, sid))
        # Return loyalty points
        if s["customer_id"]:
            pts = int(float(s["total"] or 0) * float(get_setting("loyalty_points_per_real","1")))
            if pts > 0:
                db2.execute("UPDATE customers SET loyalty_points=MAX(0,loyalty_points-%s) WHERE id=%s",
                            (pts, s["customer_id"]))
        # Revert coupon usage
        if s["discount_coupon_id"]:
            db2.execute("UPDATE discount_coupons SET used_count=MAX(0,used_count-1) WHERE id=%s",
                        (s["discount_coupon_id"],))
    restock_msg = " Estoque reposto." if restock else ""
    credit_msg  = " Crédito gerado." if gen_credit else ""
    flash(f"Venda #{sid} cancelada.{restock_msg}{credit_msg}", "success")
    return redirect(url_for("sale_detail", sid=sid))

# ─── v9: CONFIRMAR PAGAMENTO ────────────────────────────────


@sales_bp.route("/sales/<int:sid>/payment/confirm", methods=["POST"], endpoint="confirm_payment")
@login_required
def confirm_payment(sid):
    validate_csrf_or_abort()
    db = get_db()
    s = db.execute("SELECT id, status, payment_status, total FROM sales WHERE id=%s",
                   (sid,)).fetchone()
    if not s: abort(404)
    if s["status"] == "cancelled":
        flash("Venda cancelada — não é possível confirmar pagamento.", "warning")
        return redirect(url_for("sale_detail", sid=sid))
    if s["payment_status"] == "paid":
        flash("Pagamento já estava confirmado.", "info")
        return redirect(url_for("sale_detail", sid=sid))
    user = get_current_user(); now = utc_now_iso()
    with transaction() as db2:
        db2.execute(
            "UPDATE sales SET payment_status='paid', payment_confirmed_at=%s, "
            "payment_confirmed_by=%s WHERE id=%s",
            (now, user["display_name"], sid)
        )
        # Se houver cobrança PIX ativa, marca como paid também
        db2.execute(
            "UPDATE pix_charges SET status='paid', paid_at=%s "
            "WHERE sale_id=%s AND status='pending'",
            (now, sid)
        )
    flash(f"Pagamento da venda #{sid} confirmado.", "success")
    return redirect(request.referrer or url_for("sale_detail", sid=sid))

# ─── v9: CANCELAR POR NÃO-PAGAMENTO ──────────────────────────


@sales_bp.route("/sales/<int:sid>/payment/cancel", methods=["POST"], endpoint="cancel_payment")
@login_required
def cancel_payment(sid):
    """Cancela a venda por falta de pagamento. Devolve estoque por padrão.
    Reaproveita lógica de cancel_sale, mas com cancel_reason fixo."""
    validate_csrf_or_abort()
    db = get_db()
    s = db.execute("SELECT * FROM sales WHERE id=%s", (sid,)).fetchone()
    if not s: abort(404)
    if s["status"] == "cancelled":
        flash("Venda já está cancelada.", "warning")
        return redirect(url_for("sale_detail", sid=sid))
    user = get_current_user(); now = utc_now_iso()
    with transaction() as db2:
        db2.execute(
            "UPDATE sales SET status='cancelled', cancelled_at=%s, cancelled_by=%s, "
            "cancel_reason=%s, payment_status='failed' WHERE id=%s",
            (now, user["display_name"], "Cancelado por não-pagamento", sid)
        )
        # Devolve estoque
        items = db2.execute("SELECT * FROM sale_items WHERE sale_id=%s", (sid,)).fetchall()
        for item in items:
            db2.execute(
                "UPDATE products SET stock_qty=stock_qty+%s, updated_at=%s WHERE id=%s",
                (item["qty"], now, item["product_id"])
            )
            db2.execute(
                "INSERT INTO stock_movements(product_id,type,qty,reason,sale_id,operator_name,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (item["product_id"], "return", item["qty"],
                 f"Cancelamento por não-pagamento venda #{sid}", sid, user["display_name"], now)
            )
        # Cancela cobranças PIX ativas
        db2.execute(
            "UPDATE pix_charges SET status='cancelled' "
            "WHERE sale_id=%s AND status='pending'", (sid,)
        )
        # Reverte cupom se houver
        if s["discount_coupon_id"]:
            db2.execute(
                "UPDATE discount_coupons SET used_count=MAX(0,used_count-1) WHERE id=%s",
                (s["discount_coupon_id"],)
            )
    flash(f"Venda #{sid} cancelada por não-pagamento. Estoque reposto.", "success")
    return redirect(request.referrer or url_for("list_sales"))

# ─── v9: LISTA DE VENDAS COM PAGAMENTO PENDENTE ──────────────


@sales_bp.route("/sales/pending", endpoint="pending_payments")
@login_required
def pending_payments():
    db = get_db()
    rows = db.execute("""
        SELECT s.id, s.sale_number, s.total, s.payment_method, s.created_at,
               s.buyer_name_free, c.name AS customer_name, c.phone AS customer_phone,
               pc.brcode AS pix_brcode, pc.id AS charge_id
        FROM sales s
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN pix_charges pc ON pc.sale_id = s.id AND pc.status = 'pending'
        WHERE s.payment_status = 'pending' AND s.status = 'active'
        ORDER BY s.created_at ASC
    """).fetchall()

    # Calcula idade e flag de alerta (>= 8h)
    from datetime import datetime as _dt, timezone as _tz
    ALERT_HOURS = 8
    items = []
    total_pending = Decimal("0")
    alert_count = 0
    for r in rows:
        d = dict(r)
        try:
            created = _dt.fromisoformat(str(d["created_at"]).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=_tz.utc)
            age_hours = (_dt.now(_tz.utc) - created).total_seconds() / 3600
        except Exception:
            age_hours = 0
        d["age_hours"] = age_hours
        d["is_overdue"] = age_hours >= ALERT_HOURS
        if d["is_overdue"]: alert_count += 1
        try:
            total_pending += Decimal(str(d["total"] or 0))
        except Exception:
            pass
        items.append(d)
    ensure_csrf_token()
    return render_template("pending_payments.html",
                           items=items, total_pending=total_pending,
                           alert_count=alert_count, alert_hours=ALERT_HOURS)


# ─── DEVOLVER ITEM ──────────────────────────────────────────


@sales_bp.route("/sales/<int:sid>/return", methods=["POST"], endpoint="return_item")
@login_required
def return_item(sid):
    validate_csrf_or_abort()
    db = get_db()
    s = db.execute("SELECT * FROM sales WHERE id=%s", (sid,)).fetchone()
    if not s or s["status"] == "cancelled": abort(404)
    pid      = int(request.form.get("product_id","0"))
    qty      = int(request.form.get("qty","1") or 1)
    reason   = request.form.get("reason","").strip() or "Devolução"
    restock  = request.form.get("restock","1") == "1"
    gen_credit = request.form.get("gen_credit","0") == "1"
    user = get_current_user(); now = utc_now_iso()
    # Get original price for this item
    item = db.execute("SELECT * FROM sale_items WHERE sale_id=%s AND product_id=%s", (sid,pid)).fetchone()
    if not item:
        flash("Item não encontrado nesta venda.","danger")
        return redirect(url_for("sale_detail", sid=sid))
    credit_id = None
    with transaction() as db2:
        if restock:
            db2.execute("UPDATE products SET stock_qty=stock_qty+%s, updated_at=%s WHERE id=%s", (qty,now,pid))
            db2.execute("""INSERT INTO stock_movements(product_id,type,qty,reason,sale_id,operator_name,created_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (pid,"return",qty,f"Devolução venda #{sid}",sid,user["display_name"],now))
        if gen_credit:
            credit_amount = str(Decimal(str(item["unit_price"])) * qty)
            cname = db2.execute("SELECT name FROM customers WHERE id=%s", (s["customer_id"],)).fetchone()
            cname = cname["name"] if cname else (s["buyer_name_free"] or "Cliente")
            credit_id = insert_returning_id("""INSERT INTO store_credits(customer_id,customer_name,amount,reason,
                source_sale_id,status,created_at,created_by)
                VALUES(%s,%s,%s,%s,%s,'active',%s,%s)""",
                (s["customer_id"], cname, credit_amount,
                 f"Crédito devolução venda #{sid}", sid, now, user["display_name"]))
        db2.execute("""INSERT INTO sale_returns(sale_id,product_id,qty,reason,restock,credit_id,operator_name,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (sid,pid,qty,reason,1 if restock else 0,credit_id,user["display_name"],now))
    flash(f"Devolução registrada.{' Estoque reposto.' if restock else ''}{' Crédito gerado.' if gen_credit else ''}","success")
    return redirect(url_for("sale_detail", sid=sid))

# ─── CRÉDITOS DE LOJA ───────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────
# init_sales_blueprint — registra aliases sem prefixo "sales."
# ─────────────────────────────────────────────────────────────────────────
def init_sales_blueprint(app):
    """Hook chamado por app.py após app.register_blueprint(sales_bp). Idempotente."""
    if getattr(app, "_sales_bp_initialized", False):
        return

    ALIAS_MAP = {
        "list_sales":                 "sales.list_sales",
        "create_sale":                "sales.create_sale",
        "sale_detail":                "sales.sale_detail",
        "cancel_sale":                "sales.cancel_sale",
        "return_item":                "sales.return_item",
        "confirm_payment":            "sales.confirm_payment",
        "cancel_payment":             "sales.cancel_payment",
        "pending_payments":           "sales.pending_payments",
        "sale_pix_brcode":            "sales.sale_pix_brcode",
        "sale_pix_qr":                "sales.sale_pix_qr",
        "sale_pix_info":              "sales.sale_pix_info",
        "create_pix_charge_for_sale": "sales.create_pix_charge_for_sale",
        "pix_charge_qr":              "sales.pix_charge_qr",
        "pix_charge_status":          "sales.pix_charge_status",
        "pix_charge_mark_paid":       "sales.pix_charge_mark_paid",
        "pix_webhook":                "sales.pix_webhook",
        "sale_dispatch_label":        "sales.sale_dispatch_label",
        "sale_prefatura_pdf":         "sales.sale_prefatura_pdf",
        "sale_prefatura_json":        "sales.sale_prefatura_json",
        "sale_prefatura_csv":         "sales.sale_prefatura_csv",
    }

    _ROUTE_DEFS = [
        ("list_sales",                 "/sales",                              ("GET",)),
        ("create_sale",                "/sales/new",                          ("GET", "POST")),
        ("sale_detail",                "/sales/<int:sid>",                    ("GET",)),
        ("cancel_sale",                "/sales/<int:sid>/cancel",             ("POST",)),
        ("return_item",                "/sales/<int:sid>/return",             ("POST",)),
        ("confirm_payment",            "/sales/<int:sid>/payment/confirm",    ("POST",)),
        ("cancel_payment",             "/sales/<int:sid>/payment/cancel",     ("POST",)),
        ("pending_payments",           "/sales/pending",                      ("GET",)),
        ("sale_pix_brcode",            "/sales/<int:sid>/pix.txt",            ("GET",)),
        ("sale_pix_qr",                "/sales/<int:sid>/pix.png",            ("GET",)),
        ("sale_pix_info",              "/sales/<int:sid>/pix-info.json",      ("GET",)),
        ("create_pix_charge_for_sale", "/sales/<int:sid>/charge-pix",         ("POST",)),
        ("pix_charge_qr",              "/pix-charges/<int:charge_id>/qr.png", ("GET",)),
        ("pix_charge_status",          "/pix-charges/<int:charge_id>/status", ("GET",)),
        ("pix_charge_mark_paid",       "/pix-charges/<int:charge_id>/mark-paid", ("POST",)),
        ("pix_webhook",                "/webhooks/pix/<provider_slug>",       ("POST",)),
        ("sale_dispatch_label",        "/sales/<int:sid>/dispatch-label.pdf", ("GET",)),
        ("sale_prefatura_pdf",         "/sales/<int:sid>/pre-nota.pdf",       ("GET",)),
        ("sale_prefatura_json",        "/sales/<int:sid>/pre-nota.json",      ("GET",)),
        ("sale_prefatura_csv",         "/sales/<int:sid>/pre-nota.csv",       ("GET",)),
    ]

    for short, full in ALIAS_MAP.items():
        view = app.view_functions.get(full)
        if view is None:
            app.logger.warning(
                "Sales blueprint: endpoint %r ausente — alias %r não registrado.",
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

    app._sales_bp_initialized = True
