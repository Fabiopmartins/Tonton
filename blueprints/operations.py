"""
blueprints/operations.py — Blueprint de operações de loja.

Escopo (14 rotas):

  PDV / atendimento (2)
    GET/POST /redeem                          → redeem (resgate de gift card)
    GET  /atender                             → atender_cliente (balcão rápido)

  Financeiro (5)
    GET  /accounting                          → accounting
    POST /accounting/expense/new              → create_expense
    POST /accounting/expense/<id>/cancel      → cancel_expense
    POST /accounting/expense/<id>/delete      → delete_expense
    GET  /pricing                             → pricing_helper

  Notificações (2)
    GET  /notifications                       → notifications_view
    POST /notifications/mark-read             → mark_notifications_read

  Relatórios e metas (2)
    GET  /reports                             → reports
    GET/POST /goals                           → manage_goals

  Catálogo / calendário comercial (3)
    GET  /catalog/qr                          → catalog_qr
    GET  /calendar                            → commercial_calendar
    POST /calendar/new                        → create_calendar_event

Notas (Onda 2 — Etapa I, fechamento da Onda 2):

  1. Endpoints PRESERVAM nomes globais via aliases registrados em
     init_operations_blueprint().

  2. Helpers (_create_notification) era nested em register_v3_routes
     em app.py. Replicado INLINE neste módulo para isolar dependência.

  3. Substituições aplicadas: bare `app` → `current_app` (config, logger,
     send_email, smtp_is_configured, encrypt/decrypt_visible_code,
     record_failed_lookup).

  4. LIMPEZA: o bloco duplicado de cálculo de wa_link em
     atender_cliente (app.py:3868-3874, idêntico ao bloco 3861-3867)
     foi REMOVIDO durante a extração. Comportamento idêntico — o segundo
     bloco apenas sobrescrevia os valores com cálculos iguais. Bug
     pré-existente de copy-paste.

  5. /public/hero-image/<hid> NÃO está aqui — foi movida para
     blueprints/public.py (mesma natureza de public_product_image).

  6. Mantidas em app.py: `/` (root index) e `/dashboard` (home), que são
     responsabilidades do shell da aplicação, não de um domínio específico.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import quote_plus

from flask import (
    Blueprint, current_app, request, render_template, redirect, url_for,
    abort, Response, jsonify, session, flash,
)

from app import (
    LOCAL_TZ,
    UTC,
    _compute_avg_fixed_expense,
    _pricing_metrics,
    card_verification_matches,
    decrypt_visible_code,
    ensure_csrf_token,
    format_money,
    get_current_user,
    get_setting,
    hash_code,
    is_card_expired,
    is_lookup_locked,
    login_required,
    normalize_code,
    parse_money,
    record_failed_lookup,
    require_role,
    reset_failed_lookups,
    utc_now,
    utc_now_iso,
    validate_csrf_or_abort,
)
from db import get_db, transaction


operations_bp = Blueprint("operations", __name__, template_folder="../templates")


# ─────────────────────────────────────────────────────────────────────────
# Helper privado replicado verbatim de app.py:4759 (era nested em
# register_v3_routes; nested functions não são exportáveis).
# Mesma definição usada em blueprints/sales.py.
# ─────────────────────────────────────────────────────────────────────────
def _create_notification(db, ntype, title, body="", link=""):
    db.execute(
        "INSERT INTO notifications(type,title,body,link,is_read,created_at) "
        "VALUES(%s,%s,%s,%s,0,%s)",
        (ntype, title, body, link, utc_now_iso()),
    )


@operations_bp.route("/redeem", methods=["GET", "POST"], endpoint="redeem")
@login_required
def redeem():
    card = None
    prefill_code = request.args.get("prefill", "")
    if request.method == "POST":
        validate_csrf_or_abort()
        code = normalize_code(request.form.get("code", ""))
        confirmation = request.form.get("confirmation", "")
        if is_lookup_locked():
            flash("Muitas tentativas inválidas. Aguarde alguns minutos.", "danger")
        elif not code:
            flash("Digite o código do cupom.", "danger")
        else:
            code_hash = hash_code(code, current_app.config["CODE_PEPPER"])
            db = get_db()
            candidate = db.execute("SELECT * FROM gift_cards WHERE code_hash = %s", (code_hash,)).fetchone()
            if candidate and card_verification_matches(candidate, confirmation):
                card = candidate
                reset_failed_lookups()
                if is_card_expired(card):
                    flash("Esse cupom está expirado.", "warning")
                elif card["status"] == "cancelled":
                    flash("Esse cupom foi cancelado.", "warning")
                elif not int(card["is_released"]):
                    flash("O cupom existe, mas ainda não está liberado para uso.", "warning")
                else:
                    flash("Cupom confirmado. Agora é só conferir o saldo e lançar a baixa.", "success")
            else:
                record_failed_lookup(current_app)
                flash("Código ou conferência complementar inválidos.", "danger")
    ensure_csrf_token()
    # Provide recent active cards for quick-select UI
    _db = get_db()
    _active = _db.execute(
        """SELECT id, code_last4, recipient_name, current_balance, encrypted_code
           FROM gift_cards WHERE status = 'active' AND is_released = 1
           ORDER BY id DESC LIMIT 30"""
    ).fetchall()
    active_cards_display = []
    for c in _active:
        vc = decrypt_visible_code(current_app, c["encrypted_code"])
        active_cards_display.append({
            "id": c["id"],
            "code": vc or f"****{c['code_last4']}",
            "recipient_name": c["recipient_name"] or "—",
            "current_balance": format_money(c["current_balance"]),
        })
    return render_template("redeem.html", card=card, prefill_code=prefill_code, active_cards=active_cards_display)


@operations_bp.route("/accounting", endpoint="accounting")
@login_required
def accounting():
    db    = get_db()
    month = request.args.get("month", datetime.now(LOCAL_TZ).strftime("%Y-%m"))
    try:
        md  = datetime.strptime(month,"%Y-%m")
        ms  = md.strftime("%Y-%m-01")
        me  = (md.replace(day=28)+timedelta(days=4)).replace(day=1).strftime("%Y-%m-01")
    except Exception:
        ms = "2000-01-01"; me = "2099-01-01"

    expenses = db.execute("""SELECT e.*, ec.name as cat_name, ec.color as cat_color
        FROM expenses e LEFT JOIN expense_categories ec ON ec.id=e.category_id
        WHERE e.expense_date>=%s AND e.expense_date<%s ORDER BY e.status ASC, e.expense_date DESC""",
        (ms, me)).fetchall()

    exp_total  = sum((Decimal(str(e["amount"])) for e in expenses if e["status"]=="active"), Decimal("0"))
    categories = db.execute("SELECT * FROM expense_categories ORDER BY name").fetchall()

    # Revenue — apenas vendas com pagamento confirmado (CPC 47 / IFRS 15)
    ms_utc = datetime.strptime(ms,"%Y-%m-%d").replace(tzinfo=LOCAL_TZ).astimezone(UTC).isoformat()
    me_utc = datetime.strptime(me,"%Y-%m-%d").replace(tzinfo=LOCAL_TZ).astimezone(UTC).isoformat()
    sales_rev = Decimal(str(db.execute("SELECT COALESCE(SUM(CASE WHEN status!='cancelled' AND payment_status='paid' THEN CAST(total AS DOUBLE PRECISION) ELSE 0 END),0) FROM sales WHERE created_at>=%s AND created_at<%s", (ms_utc,me_utc)).fetchone()[0]))
    cogs_v    = Decimal(str(db.execute("""SELECT COALESCE(SUM(si.qty*CAST(si.cost_price AS DOUBLE PRECISION)),0) FROM sale_items si
        JOIN sales s ON s.id=si.sale_id WHERE s.created_at>=%s AND s.created_at<%s AND s.status!='cancelled' AND s.payment_status='paid'""", (ms_utc,me_utc)).fetchone()[0]))
    gc_rev    = Decimal(str(db.execute("SELECT COALESCE(SUM(CAST(initial_value AS DOUBLE PRECISION)),0) FROM gift_cards WHERE created_at>=%s AND created_at<%s AND status!='cancelled'", (ms_utc,me_utc)).fetchone()[0]))
    gross_rev = sales_rev + gc_rev
    gross     = gross_rev - cogs_v
    op        = gross - exp_total
    gross_pct = float(gross/gross_rev*100) if gross_rev>0 else 0
    op_pct    = float(op/gross_rev*100) if gross_rev>0 else 0

    cat_breakdown = db.execute("""SELECT ec.name as cat_name,ec.color as cat_color,SUM(CAST(e.amount AS DOUBLE PRECISION)) as total FROM expenses e
        JOIN expense_categories ec ON ec.id=e.category_id
        WHERE e.expense_date>=%s AND e.expense_date<%s AND e.status='active'
        GROUP BY ec.id ORDER BY total DESC""", (ms,me)).fetchall()

    # Monthly trend (12 months)
    trend = []
    for i in range(11,-1,-1):
        d  = (datetime.now(LOCAL_TZ).replace(day=1) - timedelta(days=i*28)).replace(day=1)
        ms2 = d.strftime("%Y-%m-01")
        me2 = (d.replace(day=28)+timedelta(days=4)).replace(day=1).strftime("%Y-%m-01")
        ms2u= datetime.strptime(ms2,"%Y-%m-%d").replace(tzinfo=LOCAL_TZ).astimezone(UTC).isoformat()
        me2u= datetime.strptime(me2,"%Y-%m-%d").replace(tzinfo=LOCAL_TZ).astimezone(UTC).isoformat()
        rv_sales = float(db.execute("SELECT COALESCE(SUM(CASE WHEN status!='cancelled' AND payment_status='paid' THEN CAST(total AS DOUBLE PRECISION) ELSE 0 END),0) FROM sales WHERE created_at>=%s AND created_at<%s", (ms2u,me2u)).fetchone()[0])
        rv_gc    = float(db.execute("SELECT COALESCE(SUM(CAST(initial_value AS DOUBLE PRECISION)),0) FROM gift_cards WHERE created_at>=%s AND created_at<%s AND status!='cancelled'", (ms2u,me2u)).fetchone()[0])
        rv  = rv_sales + rv_gc
        ev  = float(db.execute("SELECT COALESCE(SUM(CAST(amount AS DOUBLE PRECISION)),0) FROM expenses WHERE expense_date>=%s AND expense_date<%s AND status='active'", (ms2,me2)).fetchone()[0])
        trend.append({"label":d.strftime("%b/%y"),"rev":rv,"exp":ev,"result":round(rv-ev,2)})

    accounting_max_t = max((t["rev"] for t in trend), default=1) or 1

    return render_template("accounting.html",
        accounting_max_t=accounting_max_t,
        expenses=expenses, categories=categories, cat_breakdown=cat_breakdown,
        exp_total=exp_total, sales_rev=sales_rev, gc_rev=gc_rev, gross_rev=gross_rev,
        cogs_v=cogs_v, gross=gross, op=op, gross_pct=gross_pct, op_pct=op_pct,
        month=month, trend=trend)



@operations_bp.route("/accounting/expense/new", methods=["POST"], endpoint="create_expense")
@login_required
def create_expense():
    validate_csrf_or_abort()
    desc = request.form.get("description","").strip()
    if not desc:
        flash("Descrição é obrigatória.","danger"); return redirect(url_for("accounting"))
    try:
        amount = parse_money(request.form.get("amount","0"))
    except Exception:
        flash("Valor inválido.","danger"); return redirect(url_for("accounting"))
    cat_id = request.form.get("category_id") or None
    if cat_id: cat_id = int(cat_id)
    exp_date   = request.form.get("expense_date","") or datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    recurrence = request.form.get("recurrence","once")
    notes      = request.form.get("notes","").strip() or None
    user       = get_current_user()
    is_fixed = 1 if request.form.get("is_fixed","1") != "0" else 0
    supplier = request.form.get("supplier","").strip() or None
    with transaction() as db:
        db.execute("""INSERT INTO expenses(category_id,description,amount,expense_date,recurrence,notes,
            status,is_fixed,supplier,created_at,created_by)
            VALUES(%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s)""",
            (cat_id,desc,str(amount),exp_date,recurrence,notes,
             is_fixed,supplier,utc_now_iso(),user["display_name"]))
    flash("Despesa registrada.","success")
    return redirect(url_for("accounting") + f"?month={exp_date[:7]}")



@operations_bp.route("/accounting/expense/<int:eid>/cancel", methods=["POST"], endpoint="cancel_expense")
@login_required
def cancel_expense(eid):
    validate_csrf_or_abort()
    user = get_current_user()
    with transaction() as db:
        db.execute("UPDATE expenses SET status='cancelled',cancelled_at=%s,cancelled_by=%s WHERE id=%s",
                   (utc_now_iso(), user["display_name"], eid))
    flash("Despesa cancelada.","success")
    return redirect(request.referrer or url_for("accounting"))



@operations_bp.route("/accounting/expense/<int:eid>/delete", methods=["POST"], endpoint="delete_expense")
@login_required
@require_role("admin")
def delete_expense(eid):
    validate_csrf_or_abort()
    with transaction() as db:
        db.execute("DELETE FROM expenses WHERE id=%s",(eid,))
    flash("Despesa removida.","success")
    return redirect(request.referrer or url_for("accounting"))



@operations_bp.route("/pricing", endpoint="pricing_helper")
@login_required
def pricing_helper():
    db = get_db()
    products  = db.execute("SELECT * FROM products WHERE is_active=1 ORDER BY name").fetchall()
    settings  = {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM store_settings").fetchall()}
    overhead  = _compute_avg_fixed_expense(db)
    tgt_global = float(get_setting("target_margin_pct","60"))
    product_metrics = []
    for p in products:
        tgt = float(p["target_margin_pct"]) if p["target_margin_pct"] else tgt_global
        m   = _pricing_metrics(p["cost_price"], tgt, p["sale_price"], overhead)
        product_metrics.append((p, m))
    return render_template("pricing_helper.html", products=products,
                           product_metrics=product_metrics, settings=settings,
                           overhead=overhead)

# ─── HERO DA VITRINE: galeria de imagens da loja ────────────
# ─────────────────────────────────────────────────────────────
# Rotas extraídas para blueprints/admin.py (Onda 2 — Etapa D).
# Endpoints preservam nomes globais; aliases via init_admin_blueprint.
# ─────────────────────────────────────────────────────────────



@operations_bp.route("/notifications", endpoint="notifications_view")
@login_required
def notifications_view():
    db = get_db()
    notifications = db.execute("""
        SELECT id, type, title, body, link, is_read, created_at
        FROM notifications
        ORDER BY is_read ASC, created_at DESC, id DESC
        LIMIT 100
    """).fetchall()
    return render_template("notifications.html", notifications=notifications)


@operations_bp.route("/notifications/mark-read", methods=["POST"], endpoint="mark_notifications_read")
@login_required
def mark_notifications_read():
    validate_csrf_or_abort()
    with transaction() as db:
        db.execute("UPDATE notifications SET is_read=1")
    return {"ok": True}


@operations_bp.route("/reports", endpoint="reports")
@login_required
def reports():
    db   = get_db()
    now  = datetime.now(LOCAL_TZ)
    month = request.args.get("month", now.strftime("%Y-%m"))
    try:
        md  = datetime.strptime(month, "%Y-%m").replace(tzinfo=LOCAL_TZ)
        ms  = md.astimezone(UTC).isoformat()
        me  = (md.replace(day=28)+timedelta(days=4)).replace(day=1).astimezone(UTC).isoformat()
        # prev month
        pm  = (md.replace(day=1) - timedelta(days=1)).replace(day=1).replace(tzinfo=LOCAL_TZ)
        pms = pm.astimezone(UTC).isoformat()
        pme = md.astimezone(UTC).isoformat()
    except Exception:
        ms = pms = "2000-01-01"; me = pme = "2099-01-01"

    # Revenue & count (non-cancelled only)
    rev_row  = db.execute("SELECT COALESCE(SUM(CASE WHEN status!='cancelled' THEN CAST(total AS DOUBLE PRECISION) ELSE 0 END),0) as rev, SUM(CASE WHEN status!='cancelled' THEN 1 ELSE 0 END) as cnt FROM sales WHERE created_at>=%s AND created_at<%s", (ms,me)).fetchone()
    prev_row = db.execute("SELECT COALESCE(SUM(CASE WHEN status!='cancelled' THEN CAST(total AS DOUBLE PRECISION) ELSE 0 END),0) as rev, SUM(CASE WHEN status!='cancelled' THEN 1 ELSE 0 END) as cnt FROM sales WHERE created_at>=%s AND created_at<%s", (pms,pme)).fetchone()
    rev      = Decimal(str(rev_row["rev"]))
    prev_rev = Decimal(str(prev_row["rev"]))
    growth   = float((rev - prev_rev) / prev_rev * 100) if prev_rev > 0 else None

    # Sales by day of week
    by_dow = db.execute("""
        SELECT EXTRACT(DOW FROM created_at::timestamp)::int as dow,
               SUM(CASE WHEN status!='cancelled' THEN CAST(total AS DOUBLE PRECISION) ELSE 0 END) as rev,
               SUM(CASE WHEN status!='cancelled' THEN 1 ELSE 0 END) as cnt
        FROM sales WHERE created_at>=%s AND created_at<%s
        GROUP BY dow ORDER BY dow""", (ms,me)).fetchall()

    # Sales by hour
    by_hour = db.execute("""
        SELECT EXTRACT(HOUR FROM created_at::timestamptz AT TIME ZONE 'America/Sao_Paulo')::int as hr,
               SUM(CASE WHEN status!='cancelled' THEN 1 ELSE 0 END) as cnt
        FROM sales WHERE created_at>=%s AND created_at<%s
        GROUP BY hr ORDER BY hr""", (ms,me)).fetchall()

    # Top categories
    top_cats = db.execute("""
        SELECT p.category, SUM(si.qty) as units,
               SUM(si.qty * CAST(si.unit_price AS DOUBLE PRECISION)) as revenue
        FROM sale_items si JOIN products p ON p.id=si.product_id
        JOIN sales s ON s.id=si.sale_id
        WHERE s.created_at>=%s AND s.created_at<%s AND s.status!='cancelled' AND p.category IS NOT NULL
        GROUP BY p.category ORDER BY revenue DESC LIMIT 6""", (ms,me)).fetchall()

    # Top products
    top_prods = db.execute("""
        SELECT p.name, p.sku, SUM(si.qty) as units,
               SUM(si.qty * CAST(si.unit_price AS DOUBLE PRECISION)) as revenue,
               SUM(si.qty * (CAST(si.unit_price AS DOUBLE PRECISION)-CAST(si.cost_price AS DOUBLE PRECISION))) as profit,
               CASE WHEN SUM(si.qty*CAST(si.unit_price AS DOUBLE PRECISION))>0 THEN
                    SUM(si.qty*(CAST(si.unit_price AS DOUBLE PRECISION)-CAST(si.cost_price AS DOUBLE PRECISION)))/SUM(si.qty*CAST(si.unit_price AS DOUBLE PRECISION))*100
               ELSE 0 END as margin_pct
        FROM sale_items si JOIN products p ON p.id=si.product_id
        JOIN sales s ON s.id=si.sale_id
        WHERE s.created_at>=%s AND s.created_at<%s AND s.status!='cancelled'
        GROUP BY p.id ORDER BY revenue DESC LIMIT 8""", (ms,me)).fetchall()

    # Top operators
    top_ops = db.execute("""
        SELECT operator_name,
               SUM(CASE WHEN status!='cancelled' THEN 1 ELSE 0 END) as cnt,
               SUM(CASE WHEN status!='cancelled' THEN CAST(total AS DOUBLE PRECISION) ELSE 0 END) as rev
        FROM sales WHERE created_at>=%s AND created_at<%s
        GROUP BY operator_name ORDER BY rev DESC""", (ms,me)).fetchall()

    # Goal for month
    goal_row = db.execute("SELECT revenue_goal FROM store_goals WHERE month=%s", (month,)).fetchone()
    goal     = Decimal(str(goal_row["revenue_goal"])) if goal_row else None
    goal_pct = float(rev / goal * 100) if goal and goal > 0 else None

    # 12-month trend for chart — include sales + gift cards + expenses
    trend = []
    for i in range(11, -1, -1):
        d   = (now.replace(day=1) - timedelta(days=i*28)).replace(day=1)
        ms2 = d.replace(tzinfo=LOCAL_TZ).astimezone(UTC).isoformat()
        me2 = (d.replace(day=28)+timedelta(days=4)).replace(day=1).replace(tzinfo=LOCAL_TZ).astimezone(UTC).isoformat()
        ms2_local = d.strftime("%Y-%m-01")
        me2_local = (d.replace(day=28)+timedelta(days=4)).replace(day=1).strftime("%Y-%m-01")
        rv_sales = float(db.execute("SELECT COALESCE(SUM(CASE WHEN status!='cancelled' THEN CAST(total AS DOUBLE PRECISION) ELSE 0 END),0) FROM sales WHERE created_at>=%s AND created_at<%s", (ms2,me2)).fetchone()[0])
        rv_gc    = float(db.execute("SELECT COALESCE(SUM(CAST(initial_value AS DOUBLE PRECISION)),0) FROM gift_cards WHERE created_at>=%s AND created_at<%s AND status!='cancelled'", (ms2,me2)).fetchone()[0])
        ev       = float(db.execute("SELECT COALESCE(SUM(CAST(amount AS DOUBLE PRECISION)),0) FROM expenses WHERE expense_date>=%s AND expense_date<%s AND status='active'", (ms2_local, me2_local)).fetchone()[0])
        rv       = rv_sales + rv_gc
        trend.append({"label": d.strftime("%b/%y"), "rev": rv, "exp": ev, "result": round(rv-ev,2)})

    # cancel rate
    total_cnt    = db.execute("SELECT COUNT(*) FROM sales WHERE created_at>=%s AND created_at<%s", (ms,me)).fetchone()[0]
    cancel_cnt   = db.execute("SELECT COUNT(*) FROM sales WHERE created_at>=%s AND created_at<%s AND status='cancelled'", (ms,me)).fetchone()[0]
    cancel_rate  = round(cancel_cnt / total_cnt * 100, 1) if total_cnt > 0 else 0

    DOW = ["Dom","Seg","Ter","Qua","Qui","Sex","Sáb"]
    dow_data = {str(i): {"label": DOW[i], "rev": 0, "cnt": 0} for i in range(7)}
    for r in by_dow:
        dow_data[r["dow"]] = {"label": DOW[int(r["dow"])], "rev": float(r["rev"] or 0), "cnt": int(r["cnt"] or 0)}

    return render_template("reports.html",
        month=month, rev=rev, prev_rev=prev_rev, growth=growth,
        sales_cnt=int(rev_row["cnt"] or 0), prev_cnt=int(prev_row["cnt"] or 0),
        ticket_avg=rev / rev_row["cnt"] if rev_row["cnt"] else Decimal("0"),
        cancel_rate=cancel_rate, cancel_cnt=cancel_cnt,
        top_cats=top_cats, top_prods=top_prods, top_ops=top_ops,
        dow_data=list(dow_data.values()), by_hour=by_hour,
        trend=trend, trend_max=max([t["rev"] for t in trend] + [t["exp"] for t in trend] + [1]),
        goal=goal, goal_pct=goal_pct)

# ─── METAS DA LOJA ──────────────────────────────────────────


@operations_bp.route("/goals", methods=["GET","POST"], endpoint="manage_goals")
@login_required
@require_role("admin")
def manage_goals():
    db = get_db()
    if request.method == "POST":
        validate_csrf_or_abort()
        month = request.form.get("month","").strip()
        try:
            goal = str(parse_money(request.form.get("goal","0") or "0"))
        except Exception:
            flash("Valor inválido.","danger"); return redirect(url_for("manage_goals"))
        now = utc_now_iso()
        with transaction() as db2:
            db2.execute("""INSERT INTO store_goals(month,revenue_goal,created_at,updated_at)
                VALUES(%s,%s,%s,%s)
                ON CONFLICT(month) DO UPDATE SET revenue_goal=excluded.revenue_goal, updated_at=excluded.updated_at""",
                (month, goal, now, now))
        flash(f"Meta de {month} salva: R$ {format_money(goal)}","success")
        return redirect(url_for("manage_goals"))
    goals = db.execute("SELECT * FROM store_goals ORDER BY month DESC LIMIT 24").fetchall()
    return render_template("goals.html", goals=goals)

# ─── VARIANTES DE PRODUTO ───────────────────────────────────
# ─────────────────────────────────────────────────────────────
# Variantes e histórico extraídos para blueprints/products.py
# (Onda 2 — Etapa G). Endpoints preservam nomes globais.
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# Reposição extraídos para blueprints/products.py
# (Onda 2 — Etapa G). Endpoints preservam nomes globais.
# ─────────────────────────────────────────────────────────────

# ─── QR CODE DO CATÁLOGO PÚBLICO ────────────────────────


@operations_bp.route("/catalog/qr", endpoint="catalog_qr")
@login_required
def catalog_qr():
    import qrcode, io
    from flask import Response
    base = current_app.config.get("PUBLIC_BASE_URL","") or request.url_root.rstrip("/")
    url  = base.rstrip("/") + url_for("public_catalog")
    qr   = qrcode.QRCode(version=3, box_size=10, border=4, error_correction=qrcode.constants.ERROR_CORRECT_H)
    qr.add_data(url); qr.make(fit=True)
    img  = qr.make_image(fill_color="#1a1714", back_color="#f6f2ec").convert("RGB")
    buf  = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png",
                    headers={"Content-Disposition":"inline; filename=catalogo-qr.png"})

# ─── CALENDÁRIO COMERCIAL ───────────────────────────────


@operations_bp.route("/calendar", endpoint="commercial_calendar")
@login_required
def commercial_calendar():
    db = get_db()
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    upcoming = db.execute("""SELECT * FROM fashion_calendar
        WHERE event_date >= %s AND is_active=1
        ORDER BY event_date ASC LIMIT 12""", (today,)).fetchall()
    past = db.execute("""SELECT * FROM fashion_calendar
        WHERE event_date < %s AND is_active=1
        ORDER BY event_date DESC LIMIT 6""", (today,)).fetchall()
    # Mark to_sell = up to 30 days ahead
    return render_template("calendar.html", upcoming=upcoming, past=past, today=today)



@operations_bp.route("/calendar/new", methods=["POST"], endpoint="create_calendar_event")
@login_required
@require_role("admin")
def create_calendar_event():
    validate_csrf_or_abort()
    title = request.form.get("title","").strip()
    date  = request.form.get("event_date","").strip()
    kind  = request.form.get("kind","data").strip()
    notes = request.form.get("notes","").strip() or None
    if not title or not date:
        flash("Título e data são obrigatórios.","danger")
        return redirect(url_for("commercial_calendar"))
    with transaction() as db:
        db.execute("INSERT INTO fashion_calendar(title,event_date,kind,notes,is_active,created_at) VALUES(%s,%s,%s,%s,1,%s)",
            (title,date,kind,notes,utc_now_iso()))
    flash("Evento adicionado ao calendário.","success")
    return redirect(url_for("commercial_calendar"))

# ─── IMAGEM PÚBLICA DO PRODUTO ───────────────────────────

# ─── SETTINGS extras ────────────────────────────────────────
# Patch the existing settings view to also handle catalog & goal settings
# (new keys go through get_setting/set_setting which already work)

# ─── BACKUP DO BANCO (admin only) ───────────────────────────
# Gera dump SQL via psycopg2 puro — não depende de pg_dump no host.
# Saída: arquivo .sql descarregado no navegador, com INSERTs por tabela.
# Restaurar: psql $DATABASE_URL -f backup-AAAA-MM-DD.sql
# ─────────────────────────────────────────────────────────────
# Rotas extraídas para blueprints/admin.py (Onda 2 — Etapa D).
# Endpoints preservam nomes globais; aliases via init_admin_blueprint.
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# Curva abc extraídos para blueprints/products.py
# (Onda 2 — Etapa G). Endpoints preservam nomes globais.
# ─────────────────────────────────────────────────────────────


# ─── ATENDIMENTO (Vendedora-balconista) ─────────────────────
# Tela única para a vendedora consultar cliente em segundos:
# histórico, tamanho usual, aniversário, créditos, gift cards.
# Botão direto para abrir WhatsApp com mensagem padrão.


@operations_bp.route("/atender", methods=["GET"], endpoint="atender_cliente")
@login_required
def atender_cliente():
    db = get_db()
    q = (request.args.get("q", "") or "").strip()
    customer = None
    history = []
    usual_sizes = []
    active_credits = 0.0
    active_gift_cards = []
    days_since_last = None
    birthday_this_month = False

    if q:
        # Busca por telefone (normalizado), nome ou e-mail
        digits = "".join(c for c in q if c.isdigit())
        sql = """
            SELECT * FROM customers
            WHERE (%s <> '' AND REGEXP_REPLACE(COALESCE(phone,''), '[^0-9]', '', 'g') ILIKE %s)
               OR name ILIKE %s
               OR email ILIKE %s
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 1
        """
        customer = db.execute(
            sql,
            (digits, f"%{digits}%" if digits else "%__nope__%", f"%{q}%", f"%{q}%")
        ).fetchone()

    if customer:
        cid = customer["id"]
        history = db.execute("""
            SELECT s.id, s.sale_number, s.total, s.payment_method, s.created_at, s.status,
                   STRING_AGG(p.name, ', ') AS items
            FROM sales s
            LEFT JOIN sale_items si ON si.sale_id = s.id
            LEFT JOIN products p ON p.id = si.product_id
            WHERE s.customer_id = %s
            GROUP BY s.id, s.sale_number, s.total, s.payment_method, s.created_at, s.status
            ORDER BY s.created_at DESC
            LIMIT 20
        """, (cid,)).fetchall()

        # Tamanho usual: moda dos sizes vendidos
        sz = db.execute("""
            SELECT p.size, COUNT(*) AS qty
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            JOIN products p ON p.id = si.product_id
            WHERE s.customer_id = %s
              AND p.size IS NOT NULL AND p.size <> ''
              AND s.status <> 'cancelled'
            GROUP BY p.size
            ORDER BY qty DESC
            LIMIT 3
        """, (cid,)).fetchall()
        usual_sizes = sz

        # Crédito ativo
        # Bug pré-existente: query original usava COALESCE(used,0)=0, mas a
        # coluna `used` não existe em store_credits. Schema canônico tem
        # `status` ('active'|'used'|'expired'|'cancelled'). Corrigido para
        # filtrar pelo status correto + checagem de expiração.
        cr = db.execute(
            "SELECT COALESCE(SUM(CAST(amount AS DOUBLE PRECISION)),0) AS total "
            "FROM store_credits "
            "WHERE customer_id = %s "
            "  AND status = 'active' "
            "  AND (expires_at IS NULL OR expires_at > %s)",
            (cid, utc_now_iso())
        ).fetchone()
        active_credits = float(cr["total"] or 0)

        # Gift cards ativos do cliente
        active_gift_cards = db.execute("""
            SELECT id, current_balance, expires_at, status
            FROM gift_cards
            WHERE customer_id = %s
              AND COALESCE(status,'active') = 'active'
              AND CAST(current_balance AS DOUBLE PRECISION) > 0
            ORDER BY created_at DESC
            LIMIT 5
        """, (cid,)).fetchall()

        # Dias desde a última compra
        if history:
            last = history[0]["created_at"]
            try:
                last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                days_since_last = (utc_now() - last_dt).days
            except Exception:
                days_since_last = None

        # Aniversário no mês corrente (formato esperado: 'YYYY-MM-DD' ou 'MM-DD')
        bday = (customer.get("birthday") or "").strip() if hasattr(customer, "get") else (customer["birthday"] or "").strip()
        if bday:
            try:
                parts = bday.split("-")
                bmonth = int(parts[1]) if len(parts) >= 2 else 0
                if bmonth == datetime.now(LOCAL_TZ).month:
                    birthday_this_month = True
            except Exception:
                pass

    store_phone = (get_setting("store_whatsapp", "") or "").strip()
    wa_default_msg = get_setting(
        "atender_wa_template",
        "Olá {nome}! Aqui é da Tonton. Tenho novidades que combinam com você 💛"
    )

    # Monta o link do WhatsApp já pronto (telefone normalizado, mensagem url-encoded)
    wa_link = None
    if customer and store_phone:
        cphone_digits = "".join(c for c in (customer["phone"] or "") if c.isdigit())
        if cphone_digits:
            first_name = (customer["name"] or "").split(" ")[0]
            msg = wa_default_msg.replace("{nome}", first_name)
            wa_link = f"https://wa.me/{cphone_digits}?text={quote_plus(msg)}"

    return render_template(
        "atender.html",
        q=q,
        customer=customer,
        history=history,
        usual_sizes=usual_sizes,
        active_credits=active_credits,
        active_gift_cards=active_gift_cards,
        days_since_last=days_since_last,
        birthday_this_month=birthday_this_month,
        store_phone=store_phone,
        wa_default_msg=wa_default_msg,
        wa_link=wa_link,
    )



# ─────────────────────────────────────────────────────────────────────────
# init_operations_blueprint — registra aliases sem prefixo "operations."
# ─────────────────────────────────────────────────────────────────────────
def init_operations_blueprint(app):
    """Hook chamado por app.py após `app.register_blueprint(operations_bp)`. Idempotente."""
    if getattr(app, "_operations_bp_initialized", False):
        return

    ALIAS_MAP = {
        "redeem":                  "operations.redeem",
        "accounting":              "operations.accounting",
        "create_expense":          "operations.create_expense",
        "cancel_expense":          "operations.cancel_expense",
        "delete_expense":          "operations.delete_expense",
        "pricing_helper":          "operations.pricing_helper",
        "notifications_view":      "operations.notifications_view",
        "mark_notifications_read": "operations.mark_notifications_read",
        "reports":                 "operations.reports",
        "manage_goals":            "operations.manage_goals",
        "catalog_qr":              "operations.catalog_qr",
        "commercial_calendar":     "operations.commercial_calendar",
        "create_calendar_event":   "operations.create_calendar_event",
        "atender_cliente":         "operations.atender_cliente",
    }

    _ROUTE_DEFS = [
        ("redeem",                  "/redeem",                              ("GET", "POST")),
        ("accounting",              "/accounting",                          ("GET",)),
        ("create_expense",          "/accounting/expense/new",              ("POST",)),
        ("cancel_expense",          "/accounting/expense/<int:eid>/cancel", ("POST",)),
        ("delete_expense",          "/accounting/expense/<int:eid>/delete", ("POST",)),
        ("pricing_helper",          "/pricing",                             ("GET",)),
        ("notifications_view",      "/notifications",                       ("GET",)),
        ("mark_notifications_read", "/notifications/mark-read",             ("POST",)),
        ("reports",                 "/reports",                             ("GET",)),
        ("manage_goals",            "/goals",                               ("GET", "POST")),
        ("catalog_qr",              "/catalog/qr",                          ("GET",)),
        ("commercial_calendar",     "/calendar",                            ("GET",)),
        ("create_calendar_event",   "/calendar/new",                        ("POST",)),
        ("atender_cliente",         "/atender",                             ("GET",)),
    ]

    for short, full in ALIAS_MAP.items():
        view = app.view_functions.get(full)
        if view is None:
            app.logger.warning(
                "Operations blueprint: endpoint %r ausente — alias %r não registrado.",
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

    app._operations_bp_initialized = True
