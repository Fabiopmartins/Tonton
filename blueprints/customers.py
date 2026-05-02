"""
blueprints/customers.py — Blueprint de clientes.

Escopo (10 rotas, todas com @login_required):

  Atendimento e gestão (5 rotas)
    GET  /customers/lookup                    → customer_lookup
    GET  /customers                           → list_customers
    GET/POST /customers/new                   → create_customer
    GET  /customers/<cid>                     → customer_detail
    GET/POST /customers/<cid>/edit            → edit_customer

  Créditos da loja / vale-troca (3 rotas)
    GET  /credits                             → list_credits
    POST /credits/<cid>/use                   → use_credit
    GET/POST /credits/new                     → create_credit

  Aniversários e analytics (2 rotas)
    GET  /birthdays                           → birthdays
    GET  /interest                            → interest_dashboard

Notas (Onda 2 — Etapa F):

  1. Endpoints PRESERVAM nomes globais via aliases registrados em
     init_customers_blueprint(). Templates seguem usando url_for(\'list_customers\'),
     url_for(\'birthdays\'), etc.

  2. Helpers (get_db, format_money, parse_money, etc.) importados de
     app.py em top-level.

  3. _customer_or_404 era função ANINHADA dentro de register_v3_routes em
     app.py. Como não é exportável, foi DEFINIDA INLINE neste módulo,
     verbatim. Comportamento idêntico.

  4. /interest (interest_dashboard) é mantido em customers porque é
     análise de comportamento de cliente (quem demonstrou interesse em
     quais peças), não um endpoint público. A rota POST anônima
     /api/catalog/interest fica em blueprints/public.py.

  5. Substituições aplicadas durante extração: nenhuma — as views não
     usam `app` diretamente.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import (
    Blueprint, current_app, request, render_template, redirect, url_for,
    abort, session, flash,
)

from app import (
    LOCAL_TZ,
    format_money,
    get_current_user,
    get_setting,
    login_required,
    normalize_phone,
    parse_money,
    utc_now_iso,
    validate_csrf_or_abort,
)
from db import get_db, transaction


customers_bp = Blueprint("customers", __name__, template_folder="../templates")


# ─────────────────────────────────────────────────────────────────────────
# Helper privado — replicado verbatim de app.py:3095 (era nested em
# register_v3_routes). Necessário porque funções aninhadas não são
# importáveis pelo nome.
# ─────────────────────────────────────────────────────────────────────────
def _customer_or_404(cid):
    c = get_db().execute("SELECT * FROM customers WHERE id=%s", (cid,)).fetchone()
    if not c:
        abort(404)
    return c


@customers_bp.route("/customers/lookup", endpoint="customer_lookup")
@login_required
def customer_lookup():
    """Atendimento rápido: vendedora digita telefone, vê histórico em 1s.
    Mostra: última compra, tamanhos preferidos, gift cards/créditos ativos,
    aniversário do mês, e gera link WhatsApp pré-preenchido."""
    db = get_db()
    q = (request.args.get("q", "") or "").strip()
    customer = None
    last_sales = []
    size_preferences = []
    active_credits = []
    active_cards = []
    suggested_categories = []

    if q:
        # Normaliza busca: telefone (só dígitos) OU nome
        q_digits = "".join(c for c in q if c.isdigit())
        if q_digits and len(q_digits) >= 4:
            customer = db.execute(
                "SELECT * FROM customers WHERE REGEXP_REPLACE(COALESCE(phone,''), '[^0-9]', '', 'g') LIKE %s ORDER BY id DESC LIMIT 1",
                (f"%{q_digits}%",)
            ).fetchone()
        if not customer:
            customer = db.execute(
                "SELECT * FROM customers WHERE name ILIKE %s ORDER BY id DESC LIMIT 1",
                (f"%{q}%",)
            ).fetchone()

    if customer:
        cid = customer["id"]
        last_sales = db.execute("""
            SELECT s.id, s.sale_number, s.total, s.created_at, s.status,
                   STRING_AGG(p.name, ', ') as items
            FROM sales s
            LEFT JOIN sale_items si ON si.sale_id = s.id
            LEFT JOIN products p ON p.id = si.product_id
            WHERE s.customer_id = %s AND s.status != 'cancelled'
            GROUP BY s.id, s.sale_number, s.total, s.created_at, s.status
            ORDER BY s.created_at DESC LIMIT 5
        """, (cid,)).fetchall()
        size_preferences = db.execute("""
            SELECT p.size, COUNT(*) as freq
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            JOIN products p ON p.id = si.product_id
            WHERE s.customer_id = %s AND s.status != 'cancelled'
              AND p.size IS NOT NULL AND p.size <> ''
            GROUP BY p.size ORDER BY freq DESC LIMIT 3
        """, (cid,)).fetchall()
        active_cards = db.execute("""
            SELECT id, code, current_balance FROM gift_cards
            WHERE recipient_phone = %s
              AND status = 'active'
              AND CAST(current_balance AS DOUBLE PRECISION) > 0
            ORDER BY created_at DESC LIMIT 5
        """, (customer["phone"] or "",)).fetchall() if customer.get("phone") else []
        try:
            active_credits = db.execute("""
                SELECT id, amount, reason FROM store_credits
                WHERE customer_id = %s AND status = 'active'
                ORDER BY created_at DESC
            """, (cid,)).fetchall()
        except Exception:
            active_credits = []
        suggested_categories = db.execute("""
            SELECT p.category, COUNT(*) as freq
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            JOIN products p ON p.id = si.product_id
            WHERE s.customer_id = %s AND s.status != 'cancelled'
              AND p.category IS NOT NULL AND p.category <> ''
            GROUP BY p.category ORDER BY freq DESC LIMIT 3
        """, (cid,)).fetchall()

    store_phone = (get_setting("store_contact_phone", "") or "").strip()
    store_name = (get_setting("store_name", "Tonton") or "Tonton").strip()
    return render_template("customer_lookup.html",
        q=q, customer=customer, last_sales=last_sales,
        size_preferences=size_preferences, active_credits=active_credits,
        active_cards=active_cards, suggested_categories=suggested_categories,
        store_phone=store_phone, store_name=store_name,
        format_money=format_money)


@customers_bp.route("/customers", endpoint="list_customers")
@login_required
def list_customers():
    db = get_db(); q = request.args.get("q","").strip()
    sql = """SELECT c.*, COUNT(s.id) as sale_count,
                    COALESCE(SUM(CAST(s.total AS DOUBLE PRECISION)),0) as total_spent
             FROM customers c LEFT JOIN sales s ON s.customer_id=c.id WHERE 1=1"""
    params=[]
    if q:
        sql += " AND (c.name ILIKE %s OR c.phone ILIKE %s OR c.email ILIKE %s)"; params+=[f"%{q}%"]*3
    sql += " GROUP BY c.id ORDER BY c.name"
    customers = db.execute(sql,params).fetchall()
    return render_template("customers.html", customers=customers, q=q)


@customers_bp.route("/customers/new", methods=["GET","POST"], endpoint="create_customer")
@login_required
def create_customer():
    if request.method == "POST":
        validate_csrf_or_abort()
        name = request.form.get("name","").strip()
        if not name:
            flash("Nome é obrigatório.","danger"); return redirect(url_for("create_customer"))
        phone = normalize_phone(request.form.get("phone","")) or None
        now   = utc_now_iso()
        with transaction() as db:
            db.execute("""INSERT INTO customers(name,phone,email,instagram,birthday,notes,whatsapp_opt_in,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (name, phone,
                 request.form.get("email","").strip().lower() or None,
                 request.form.get("instagram","").strip() or None,
                 request.form.get("birthday","").strip() or None,
                 request.form.get("notes","").strip() or None,
                 1 if request.form.get("whatsapp_opt_in") else 0,
                 now, now))
        flash(f"Cliente «{name}» cadastrada!","success")
        return redirect(url_for("list_customers"))
    prefill = {
        "name": request.args.get("name", "").strip(),
        "phone": request.args.get("phone", "").strip(),
    }
    return render_template("create_customer.html", prefill=prefill)


@customers_bp.route("/customers/<int:cid>", endpoint="customer_detail")
@login_required
def customer_detail(cid):
    db  = get_db(); c = _customer_or_404(cid)
    sales = db.execute("""SELECT s.*, STRING_AGG(p.name, ', ') as items
        FROM sales s LEFT JOIN sale_items si ON si.sale_id=s.id LEFT JOIN products p ON p.id=si.product_id
        WHERE s.customer_id=%s GROUP BY s.id ORDER BY s.created_at DESC""", (cid,)).fetchall()
    prefs = db.execute("""SELECT p.category, COUNT(*) as qty FROM sale_items si
        JOIN sales s ON s.id=si.sale_id JOIN products p ON p.id=si.product_id
        WHERE s.customer_id=%s AND p.category IS NOT NULL GROUP BY p.category ORDER BY qty DESC LIMIT 5""", (cid,)).fetchall()
    return render_template("customer_detail.html", customer=c, sales=sales, prefs=prefs)


@customers_bp.route("/customers/<int:cid>/edit", methods=["GET","POST"], endpoint="edit_customer")
@login_required
def edit_customer(cid):
    c = _customer_or_404(cid)
    if request.method == "POST":
        validate_csrf_or_abort()
        name = request.form.get("name","").strip()
        phone = normalize_phone(request.form.get("phone","")) or None
        with transaction() as db:
            db.execute("""UPDATE customers SET name=%s,phone=%s,email=%s,instagram=%s,birthday=%s,notes=%s,
                whatsapp_opt_in=%s,updated_at=%s WHERE id=%s""",
                (name, phone,
                 request.form.get("email","").strip().lower() or None,
                 request.form.get("instagram","").strip() or None,
                 request.form.get("birthday","").strip() or None,
                 request.form.get("notes","").strip() or None,
                 1 if request.form.get("whatsapp_opt_in") else 0,
                 utc_now_iso(), cid))
        flash("Cliente atualizada.","success"); return redirect(url_for("customer_detail",cid=cid))
    return render_template("edit_customer.html", customer=c)


@customers_bp.route("/credits", endpoint="list_credits")
@login_required
def list_credits():
    db = get_db()
    credits = db.execute("""
        SELECT sc.*, c.name as cust_name
        FROM store_credits sc LEFT JOIN customers c ON c.id=sc.customer_id
        ORDER BY sc.created_at DESC LIMIT 100
    """).fetchall()
    total_active = db.execute(
        "SELECT COALESCE(SUM(CAST(amount AS DOUBLE PRECISION)),0) FROM store_credits WHERE status='active'"
    ).fetchone()[0]
    return render_template("credits.html", credits=credits, total_active=total_active)


@customers_bp.route("/credits/<int:cid>/use", methods=["POST"], endpoint="use_credit")
@login_required
def use_credit(cid):
    validate_csrf_or_abort()
    db = get_db()
    credit = db.execute("SELECT * FROM store_credits WHERE id=%s", (cid,)).fetchone()
    if not credit or credit["status"] != "active":
        flash("Crédito inválido ou já utilizado.","danger")
        return redirect(url_for("list_credits"))
    note = request.form.get("note","").strip()
    user = get_current_user()
    with transaction() as db2:
        db2.execute("UPDATE store_credits SET status='used', used_at=%s WHERE id=%s",
                    (utc_now_iso(), cid))
    flash(f"Crédito de R$ {format_money(credit['amount'])} marcado como utilizado.","success")
    return redirect(url_for("list_credits"))


@customers_bp.route("/credits/new", methods=["GET","POST"], endpoint="create_credit")
@login_required
def create_credit():
    db = get_db()
    if request.method == "POST":
        validate_csrf_or_abort()
        amount = parse_money(request.form.get("amount","0") or "0")
        if not amount or amount <= 0:
            flash("Valor inválido.","danger"); return redirect(url_for("create_credit"))
        reason = request.form.get("reason","").strip() or "Vale-troca manual"
        cid    = request.form.get("customer_id") or None
        if cid: cid = int(cid)
        cname  = request.form.get("customer_name","").strip() or None
        user   = get_current_user()
        with transaction() as db2:
            db2.execute("""INSERT INTO store_credits(customer_id,customer_name,amount,reason,
                status,created_at,created_by) VALUES(%s,%s,%s,%s,'active',%s,%s)""",
                (cid, cname, str(amount), reason, utc_now_iso(), user["display_name"]))
        flash(f"Crédito de R$ {format_money(amount)} criado.","success")
        return redirect(url_for("list_credits"))
    customers = db.execute("SELECT id,name FROM customers ORDER BY name").fetchall()
    return render_template("create_credit.html", customers=customers)


@customers_bp.route("/birthdays", endpoint="birthdays")
@login_required
def birthdays():
    db     = get_db()
    now    = datetime.now(LOCAL_TZ)
    view   = request.args.get("view","week")
    today_md   = now.strftime("-%m-%d")
    this_month = now.strftime("-%m-")
    next_month = (now.replace(day=28)+timedelta(days=4)).replace(day=1).strftime("-%m-")

    if view == "week":
        # Next 7 days
        candidates = []
        for i in range(7):
            d = now + timedelta(days=i)
            md = d.strftime("-%m-%d")
            rows = db.execute("SELECT *, %s as days_ahead FROM customers WHERE birthday LIKE %s AND birthday IS NOT NULL",
                              (i, f"%{md}")).fetchall()
            candidates.extend(rows)
        label = "Esta semana"
    elif view == "month":
        candidates = db.execute("SELECT *, 0 as days_ahead FROM customers WHERE birthday LIKE %s AND birthday IS NOT NULL ORDER BY birthday",
                                (f"%{this_month}%",)).fetchall()
        label = now.strftime("Mês de %B")
    else:
        candidates = db.execute("SELECT *, 0 as days_ahead FROM customers WHERE birthday LIKE %s AND birthday IS NOT NULL ORDER BY birthday",
                                (f"%{next_month}%",)).fetchall()
        label = "Próximo mês"

    return render_template("birthdays.html", customers=candidates, view=view, label=label, today=today_md)


@customers_bp.route("/interest", endpoint="interest_dashboard")
@login_required
def interest_dashboard():
    db  = get_db()
    # Most desired products (last 30 days)
    top = db.execute("""
        SELECT
            p.id,
            p.name AS product_name,
            p.category,
            p.sku,
            p.sale_price,
            p.stock_qty,
            COUNT(ci.id) AS count,
            MAX(ci.created_at) AS last_interest
        FROM catalog_interest ci
        JOIN products p ON p.id = ci.product_id
        WHERE ci.created_at::timestamptz >= (NOW() - INTERVAL '30 days')
        GROUP BY p.id, p.name, p.category, p.sku, p.sale_price, p.stock_qty
        ORDER BY count DESC
        LIMIT 20
    """).fetchall()
    # Recent interests (feed)
    recent = db.execute("""
        SELECT ci.*, p.name as product_name, p.category
        FROM catalog_interest ci
        LEFT JOIN products p ON p.id=ci.product_id
        ORDER BY ci.created_at DESC LIMIT 40
    """).fetchall()
    # Total
    total_30d = db.execute("SELECT COUNT(*) FROM catalog_interest WHERE created_at::timestamptz >= (NOW() - INTERVAL '30 days')").fetchone()[0]
    unique_products = db.execute("SELECT COUNT(DISTINCT product_id) FROM catalog_interest WHERE created_at::timestamptz >= (NOW() - INTERVAL '30 days')").fetchone()[0]
    return render_template("interest.html", top=top, recent=recent,
        total_30d=total_30d, unique_products=unique_products)



# ─────────────────────────────────────────────────────────────────────────
# init_customers_blueprint — registra aliases sem prefixo "customers."
# para que url_for("list_customers"), url_for("birthdays") etc. continuem
# funcionando em todos os templates legados.
# ─────────────────────────────────────────────────────────────────────────
def init_customers_blueprint(app):
    """
    Hook chamado por app.py após `app.register_blueprint(customers_bp)`.
    Idempotente.
    """
    if getattr(app, "_customers_bp_initialized", False):
        return

    ALIAS_MAP = {
        "customer_lookup":     "customers.customer_lookup",
        "list_customers":      "customers.list_customers",
        "create_customer":     "customers.create_customer",
        "customer_detail":     "customers.customer_detail",
        "edit_customer":       "customers.edit_customer",
        "list_credits":        "customers.list_credits",
        "use_credit":          "customers.use_credit",
        "create_credit":       "customers.create_credit",
        "birthdays":           "customers.birthdays",
        "interest_dashboard":  "customers.interest_dashboard",
    }

    _ROUTE_DEFS = [
        ("customer_lookup",    "/customers/lookup",          ("GET",)),
        ("list_customers",     "/customers",                 ("GET",)),
        ("create_customer",    "/customers/new",             ("GET", "POST")),
        ("customer_detail",    "/customers/<int:cid>",       ("GET",)),
        ("edit_customer",      "/customers/<int:cid>/edit",  ("GET", "POST")),
        ("list_credits",       "/credits",                   ("GET",)),
        ("use_credit",         "/credits/<int:cid>/use",     ("POST",)),
        ("create_credit",      "/credits/new",               ("GET", "POST")),
        ("birthdays",          "/birthdays",                 ("GET",)),
        ("interest_dashboard", "/interest",                  ("GET",)),
    ]

    for short, full in ALIAS_MAP.items():
        view = app.view_functions.get(full)
        if view is None:
            app.logger.warning(
                "Customers blueprint: endpoint %r ausente — alias %r não registrado.",
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

    app._customers_bp_initialized = True
