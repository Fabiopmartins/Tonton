"""
blueprints/marketing.py — Blueprint de marketing (gift cards, cupons, campanhas).

Escopo (21 rotas):

  Gift cards / vale-presente (13 rotas)
    GET/POST /cards/new                              → create_card
    GET  /cards                                      → list_cards
    GET  /cards/<id>                                 → card_detail
    GET  /cards/<id>/image                           → card_image
    GET  /shared/card/<token>/image                  → shared_card_image (público)
    POST /cards/<id>/send-email                      → send_card_email
    POST /cards/<id>/open-whatsapp                   → open_card_whatsapp
    POST /cards/<id>/toggle-release                  → toggle_release
    POST /cards/<id>/cancel                          → cancel_card
    POST /cards/<id>/redeem                          → redeem_amount
    GET  /cards/export-csv                           → export_csv
    POST /cards/<id>/quick-note                      → quick_note
    POST /cards/<id>/resend-email                    → resend_card_email

  Cupons de desconto (4 rotas)
    GET  /coupons                                    → list_coupons
    GET/POST /coupons/new                            → create_coupon
    POST /coupons/<id>/toggle                        → toggle_coupon
    GET  /coupons/validate                           → validate_coupon_api

  Campanhas (4 rotas)
    GET  /campaigns                                  → list_campaigns
    GET/POST /campaigns/new                          → create_campaign
    GET  /campaigns/<id>                             → campaign_detail
    POST /campaigns/<id>/send                        → send_campaign

Notas (Onda 2 — Etapa E):

  1. Endpoints PRESERVAM nomes globais via aliases registrados em
     init_marketing_blueprint(). Templates seguem usando url_for('list_cards'),
     url_for('list_coupons'), url_for('list_campaigns'), etc.

  2. /shared/card/<token>/image é a única rota anônima (sem login_required) —
     compartilhamento público de imagem de cupom via token. Mantida no
     marketing por coesão de domínio (gift cards), não pela classificação
     pública/privada.

  3. Helpers (get_db, send_email, generate_card_image, validate_coupon_row,
     etc.) importados de app.py em top-level. Mesma técnica de auth/public/admin.

  4. Substituições aplicadas durante extração:
     - bare `app` → `current_app` (14 ocorrências em send_email,
       smtp_is_configured, encrypt/decrypt_visible_code, app.config).

  5. Código das views é EXTRAÇÃO VERBATIM. Apenas decorators alterados para
     @marketing_bp.route(..., endpoint="...") e referências a `app` ajustadas.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import quote_plus

from flask import (
    Blueprint, current_app, request, render_template, redirect, url_for,
    abort, Response, jsonify, session, flash, send_file,
)

from app import (
    LOCAL_TZ,
    available_template_paths,
    build_card_email,
    build_whatsapp_url,
    card_verification_matches,
    decrypt_visible_code,
    encrypt_visible_code,
    ensure_csrf_token,
    format_date_local,
    format_datetime_local,
    format_money,
    format_phone_display,
    generate_card_image,
    generate_human_code,
    get_current_user,
    hash_code,
    is_card_expired,
    login_required,
    normalize_phone,
    parse_expiry,
    parse_money,
    sanitize_filename_part,
    select_template_path,
    send_email,
    smtp_is_configured,
    utc_now,
    utc_now_iso,
    validate_coupon_row,
    validate_csrf_or_abort,
    write_audit,
)
from db import get_db, insert_returning_id, transaction


marketing_bp = Blueprint("marketing", __name__, template_folder="../templates")


@marketing_bp.route("/cards/new", methods=["GET", "POST"], endpoint="create_card")
@login_required
def create_card():
    visible_code = None
    selected_template_name = None
    if request.method == "POST":
        validate_csrf_or_abort()
        recipient_name = request.form.get("recipient_name", "").strip()
        recipient_email = request.form.get("recipient_email", "").strip().lower()
        recipient_phone = request.form.get("recipient_phone", "").strip()
        buyer_name = request.form.get("buyer_name", "").strip()
        buyer_phone = request.form.get("buyer_phone", "").strip()
        order_reference = request.form.get("order_reference", "").strip()
        notes = request.form.get("notes", "").strip()
        release_now = request.form.get("release_now") == "on"
        send_email_now = request.form.get("send_email_now") == "on"
        open_whatsapp_now = request.form.get("open_whatsapp_now") == "on"
        expires_at_raw = request.form.get("expires_at", "").strip()
        initial_value = parse_money(request.form.get("initial_value", ""))

        if initial_value is None or initial_value <= Decimal("0"):
            flash("Coloque um valor válido maior que zero.", "danger")
        elif not recipient_name:
            flash("Preencha o nome da presenteada.", "danger")
        elif recipient_email and "@" not in recipient_email:
            flash("E-mail da presenteada inválido.", "danger")
        elif not (buyer_name or buyer_phone or order_reference):
            flash("Para manter controle, informe comprador, telefone ou referência do pedido.", "danger")
        else:
            expires_at = parse_expiry(expires_at_raw)
            if expires_at_raw and expires_at is None:
                flash("A data de validade não está em um formato válido.", "danger")
            else:
                visible_code = generate_human_code()
                code_hash = hash_code(visible_code, current_app.config["CODE_PEPPER"])
                qr_token = secrets.token_urlsafe(18)
                share_token = secrets.token_urlsafe(18)
                encrypted_code = encrypt_visible_code(current_app, visible_code)
                selected_template = select_template_path()
                selected_template_name = selected_template.name
                now = utc_now_iso()
                card = None
                with transaction() as db:
                    card_id = insert_returning_id(
                        """
                        INSERT INTO gift_cards (
                            code_hash, code_last4, initial_value, current_balance, currency,
                            status, expires_at, created_at, updated_at, created_by,
                            recipient_name, recipient_email, recipient_phone, buyer_name, buyer_phone, order_reference, notes,
                            qr_token, is_released, template_name, encrypted_code, share_token
                        )
                        VALUES (%s, %s, %s, %s, 'BRL', 'active', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            code_hash,
                            visible_code[-4:],
                            str(initial_value),
                            str(initial_value),
                            expires_at,
                            now,
                            now,
                            session.get("user_email", "system"),
                            recipient_name,
                            recipient_email or None,
                            recipient_phone or None,
                            buyer_name or None,
                            buyer_phone or None,
                            order_reference or None,
                            notes or None,
                            qr_token,
                            1 if release_now else 0,
                            selected_template_name,
                            encrypted_code,
                            share_token,
                        ),
                    )
                    card = db.execute("SELECT * FROM gift_cards WHERE id = %s", (card_id,)).fetchone()
                    write_audit(db, card_id, "create", f"Cupom criado com saldo inicial {format_money(initial_value)}. Template: {selected_template_name}. Liberado: {'sim' if release_now else 'não'}.")

                    if send_email_now and recipient_email and smtp_is_configured(current_app):
                        image_bytes = generate_card_image(card).getvalue()
                        subject, html, text_body = build_card_email(card, current_app.config.get("PUBLIC_BASE_URL"), visible_code)
                        send_email(
            current_app,
                            recipient_email,
                            card["recipient_name"] or "",
                            subject,
                            html,
                            text_body,
                            attachments=[(
                                f"{sanitize_filename_part(card['recipient_name'], 'presenteada')}-{sanitize_filename_part(visible_code, 'cupom')}.png",
                                image_bytes,
                                "image/png",
                            )],
                        )
                        timestamp = utc_now_iso()
                        db.execute("UPDATE gift_cards SET last_sent_at = %s, updated_at = %s WHERE id = %s", (timestamp, timestamp, card_id))
                        write_audit(db, card_id, "send_email", f"Vale enviado por e-mail para {recipient_email} logo após a criação.")

                    if open_whatsapp_now and recipient_phone:
                        timestamp = utc_now_iso()
                        db.execute("UPDATE gift_cards SET last_whatsapp_at = %s, updated_at = %s WHERE id = %s", (timestamp, timestamp, card_id))
                        write_audit(db, card_id, "open_whatsapp", f"Contato preparado no WhatsApp para {format_phone_display(recipient_phone)} logo após a criação.")
                flash(f"Vale criado. Código: {visible_code}", "success")
                if open_whatsapp_now and recipient_phone and card is not None:
                    whatsapp_url = build_whatsapp_url(recipient_phone, card, current_app.config.get("PUBLIC_BASE_URL"), visible_code)
                    if whatsapp_url:
                        return redirect(whatsapp_url)
                return redirect(url_for("redeem", prefill=visible_code))
    ensure_csrf_token()
    return render_template("create_card.html", visible_code=visible_code, template_count=len(available_template_paths()), selected_template_name=selected_template_name)



@marketing_bp.route("/cards", endpoint="list_cards")
@login_required
def list_cards():
    search = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "").strip()
    db = get_db()

    base_select = """
        SELECT id, code_last4, recipient_name, recipient_email, recipient_phone, buyer_name, buyer_phone, order_reference,
               current_balance, initial_value, status, expires_at, created_at, is_released, template_name, last_sent_at, last_whatsapp_at
        FROM gift_cards
    """

    conditions = []
    params = []

    if search:
        like = f"%{search}%"
        conditions.append(
            "(code_last4 LIKE %s OR COALESCE(recipient_name,'') LIKE %s OR COALESCE(recipient_email,'') LIKE %s"
            " OR COALESCE(recipient_phone,'') LIKE %s OR COALESCE(buyer_name,'') LIKE %s"
            " OR COALESCE(buyer_phone,'') LIKE %s OR COALESCE(order_reference,'') LIKE %s)"
        )
        params.extend([like, like, like, like, like, like, like])

    if status_filter == "active":
        conditions.append("status = 'active'")
    elif status_filter == "redeemed":
        conditions.append("status = 'redeemed'")
    elif status_filter == "cancelled":
        conditions.append("status = 'cancelled'")
    elif status_filter == "pending":
        conditions.append("is_released = 0 AND status = 'active'")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    query = f"{base_select} {where} ORDER BY id DESC LIMIT 200"
    cards = db.execute(query, params).fetchall()

    def _to_float(value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    active_cards = [c for c in cards if c["status"] == "active"]
    redeemed_cards = [c for c in cards if c["status"] == "redeemed"]
    pending_cards = [c for c in cards if c["status"] == "active" and not int(c["is_released"] or 0)]
    total_balance = sum(_to_float(c["current_balance"]) for c in active_cards)
    total_issued = sum(_to_float(c["initial_value"]) for c in cards)

    ensure_csrf_token()
    return render_template(
        "list_cards.html",
        cards=cards,
        search=search,
        status_filter=status_filter,
        active_count=len(active_cards),
        redeemed_count=len(redeemed_cards),
        pending_count=len(pending_cards),
        total_balance=total_balance,
        total_issued=total_issued,
    )



@marketing_bp.route("/cards/<int:card_id>", endpoint="card_detail")
@login_required
def card_detail(card_id: int):
    db = get_db()
    card = db.execute("SELECT * FROM gift_cards WHERE id = %s", (card_id,)).fetchone()
    if not card:
        abort(404)
    redemptions = db.execute(
        "SELECT amount, operator_name, created_at, note FROM gift_card_redemptions WHERE gift_card_id = %s ORDER BY id DESC",
        (card_id,),
    ).fetchall()
    audits = db.execute(
        "SELECT action, details, actor_name, ip_address, created_at FROM audit_logs WHERE gift_card_id = %s ORDER BY id DESC LIMIT 30",
        (card_id,),
    ).fetchall()
    ensure_csrf_token()
    return render_template("card_detail.html", card=card, redemptions=redemptions, audits=audits)



@marketing_bp.route("/cards/<int:card_id>/image", endpoint="card_image")
@login_required
def card_image(card_id: int):
    db = get_db()
    card = db.execute("SELECT * FROM gift_cards WHERE id = %s", (card_id,)).fetchone()
    if not card:
        abort(404)
    image_data = generate_card_image(card)
    visible_code = decrypt_visible_code(current_app, card["encrypted_code"]) or f"{card_id}"
    filename = f"cupom-male-{sanitize_filename_part(card['recipient_name'], 'presente')}-{sanitize_filename_part(visible_code, 'codigo')}.png"
    return send_file(image_data, mimetype="image/png", as_attachment=True, download_name=filename)



@marketing_bp.route("/shared/card/<share_token>/image", endpoint="shared_card_image")
def shared_card_image(share_token: str):
    db = get_db()
    card = db.execute("SELECT * FROM gift_cards WHERE share_token = %s", (share_token,)).fetchone()
    if not card:
        abort(404)
    image_data = generate_card_image(card)
    return send_file(image_data, mimetype="image/png", as_attachment=False, download_name=f"vale-presente-male-{card['id']}.png")



@marketing_bp.route("/cards/<int:card_id>/send-email", methods=["POST"], endpoint="send_card_email")
@login_required
def send_card_email(card_id: int):
    validate_csrf_or_abort()
    if not smtp_is_configured(current_app):
        flash("Configure o SMTP antes de enviar e-mails.", "danger")
        return redirect(url_for("card_detail", card_id=card_id))
    db = get_db()
    card = db.execute("SELECT * FROM gift_cards WHERE id = %s", (card_id,)).fetchone()
    if not card:
        abort(404)
    recipient_email = request.form.get("recipient_email", "").strip().lower() or (card["recipient_email"] or "")
    if not recipient_email or "@" not in recipient_email:
        flash("Informe um e-mail válido para envio.", "danger")
        return redirect(url_for("card_detail", card_id=card_id))

    image_bytes = generate_card_image(card).getvalue()
    visible_code = decrypt_visible_code(current_app, card["encrypted_code"])
    subject, html, text = build_card_email(card, current_app.config["PUBLIC_BASE_URL"], visible_code)
    try:
        send_email(
            current_app,
            recipient_email,
            card["recipient_name"] or "",
            subject,
            html,
            text,
            attachments=[(f"{sanitize_filename_part(card['recipient_name'], 'presenteada')}-{sanitize_filename_part(visible_code, 'cupom')}.png", image_bytes, "image/png")],
        )
    except Exception as exc:
        flash(f"Falha ao enviar e-mail: {exc}", "danger")
        return redirect(url_for("card_detail", card_id=card_id))

    with transaction() as db2:
        db2.execute("UPDATE gift_cards SET recipient_email = %s, last_sent_at = %s, updated_at = %s WHERE id = %s", (recipient_email, utc_now_iso(), utc_now_iso(), card_id))
        write_audit(db2, card_id, "send_email", f"Vale enviado por e-mail para {recipient_email}.")
    flash("Vale enviado por e-mail.", "success")
    return redirect(url_for("card_detail", card_id=card_id))



@marketing_bp.route("/cards/<int:card_id>/open-whatsapp", methods=["POST"], endpoint="open_card_whatsapp")
@login_required
def open_card_whatsapp(card_id: int):
    validate_csrf_or_abort()
    db = get_db()
    card = db.execute("SELECT * FROM gift_cards WHERE id = %s", (card_id,)).fetchone()
    if not card:
        abort(404)
    destination_phone = request.form.get("destination_phone", "").strip() or (card["recipient_phone"] or card["buyer_phone"] or "")
    visible_code = decrypt_visible_code(current_app, card["encrypted_code"])
    whatsapp_url = build_whatsapp_url(destination_phone, card, current_app.config["PUBLIC_BASE_URL"], visible_code)
    if not whatsapp_url:
        flash("Informe um telefone válido com DDD para abrir o WhatsApp.", "danger")
        return redirect(url_for("card_detail", card_id=card_id))
    with transaction() as db2:
        db2.execute("UPDATE gift_cards SET recipient_phone = COALESCE(NULLIF(%s, ''), recipient_phone), last_whatsapp_at = %s, updated_at = %s WHERE id = %s", (destination_phone, utc_now_iso(), utc_now_iso(), card_id))
        write_audit(db2, card_id, "open_whatsapp", f"Contato do vale aberto no WhatsApp para {format_phone_display(destination_phone)}.")
    return redirect(whatsapp_url)



@marketing_bp.route("/cards/<int:card_id>/toggle-release", methods=["POST"], endpoint="toggle_release")
@login_required
def toggle_release(card_id: int):
    validate_csrf_or_abort()
    with transaction() as db:
        card = db.execute("SELECT * FROM gift_cards WHERE id = %s", (card_id,)).fetchone()
        if not card:
            abort(404)
        new_value = 0 if int(card["is_released"]) else 1
        db.execute("UPDATE gift_cards SET is_released = %s, updated_at = %s WHERE id = %s", (new_value, utc_now_iso(), card_id))
        write_audit(db, card_id, "release_toggle", f"Liberação alterada para {'liberado' if new_value else 'pendente'}.")
    flash("Status de liberação atualizado.", "success")
    return redirect(url_for("card_detail", card_id=card_id))



@marketing_bp.route("/cards/<int:card_id>/cancel", methods=["POST"], endpoint="cancel_card")
@login_required
def cancel_card(card_id: int):
    validate_csrf_or_abort()
    with transaction() as db:
        card = db.execute("SELECT * FROM gift_cards WHERE id = %s", (card_id,)).fetchone()
        if not card:
            abort(404)
        if card["status"] == "cancelled":
            flash("Esse cupom já está cancelado.", "warning")
            return redirect(url_for("card_detail", card_id=card_id))
        db.execute("UPDATE gift_cards SET status = 'cancelled', updated_at = %s WHERE id = %s", (utc_now_iso(), card_id))
        write_audit(db, card_id, "cancel", "Cupom cancelado manualmente.")
    flash("Cupom cancelado.", "success")
    return redirect(url_for("card_detail", card_id=card_id))



@marketing_bp.route("/cards/<int:card_id>/redeem", methods=["POST"], endpoint="redeem_amount")
@login_required
def redeem_amount(card_id: int):
    validate_csrf_or_abort()
    amount = parse_money(request.form.get("amount", ""))
    note = request.form.get("note", "").strip()
    confirmation = request.form.get("confirmation", "")
    if amount is None or amount <= Decimal("0"):
        flash("Coloque um valor válido para abatimento.", "danger")
        return redirect(url_for("card_detail", card_id=card_id))

    with transaction() as db:
        card = db.execute("SELECT * FROM gift_cards WHERE id = %s", (card_id,)).fetchone()
        if not card:
            abort(404)
        if card["status"] != "active":
            flash("Somente cupons ativos podem receber baixa.", "danger")
            return redirect(url_for("card_detail", card_id=card_id))
        if not int(card["is_released"]):
            flash("Esse cupom ainda não foi liberado.", "danger")
            return redirect(url_for("card_detail", card_id=card_id))
        if not card_verification_matches(card, confirmation):
            flash("A segunda conferência não bate com o cadastro.", "danger")
            return redirect(url_for("card_detail", card_id=card_id))
        if is_card_expired(card):
            db.execute("UPDATE gift_cards SET status = 'expired', updated_at = %s WHERE id = %s", (utc_now_iso(), card_id))
            write_audit(db, card_id, "expire", "Cupom marcado como expirado durante tentativa de uso.")
            flash("Esse cupom está expirado.", "danger")
            return redirect(url_for("card_detail", card_id=card_id))
        current_balance = Decimal(card["current_balance"])
        if amount > current_balance:
            flash("O abatimento não pode ser maior que o saldo atual.", "danger")
            return redirect(url_for("card_detail", card_id=card_id))
        new_balance = current_balance - amount
        new_status = "redeemed" if new_balance == Decimal("0") else "active"
        now = utc_now_iso()
        db.execute("UPDATE gift_cards SET current_balance = %s, status = %s, updated_at = %s WHERE id = %s", (str(new_balance), new_status, now, card_id))
        db.execute(
            "INSERT INTO gift_card_redemptions (gift_card_id, amount, operator_name, note, created_at) VALUES (%s, %s, %s, %s, %s)",
            (card_id, str(amount), session.get("display_name", session.get("user_email", "")), note or None, now),
        )
        write_audit(db, card_id, "redeem", f"Abatido {format_money(amount)}. Saldo anterior: {format_money(current_balance)}. Novo saldo: {format_money(new_balance)}.")
    flash("Baixa registrada com sucesso.", "success")
    return redirect(url_for("redeem"))



@marketing_bp.route("/cards/export-csv", endpoint="export_csv")
@login_required
def export_csv():
    import csv, io as _io
    db = get_db()
    cards = db.execute(
        """
        SELECT id, code_last4, recipient_name, recipient_email, recipient_phone,
               buyer_name, buyer_phone, order_reference,
               initial_value, current_balance, status, is_released,
               expires_at, created_at, last_sent_at, last_whatsapp_at, notes
        FROM gift_cards ORDER BY id DESC
        """
    ).fetchall()
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID","Final código","Presenteada","E-mail","WhatsApp presenteada",
                     "Comprador","Tel comprador","Referência",
                     "Valor inicial","Saldo atual","Status","Liberado",
                     "Validade","Criado em","Último e-mail","Último WhatsApp","Observações"])
    for c in cards:
        writer.writerow([
            c["id"], f"****{c['code_last4']}", c["recipient_name"] or "",
            c["recipient_email"] or "", format_phone_display(c["recipient_phone"]),
            c["buyer_name"] or "", format_phone_display(c["buyer_phone"]),
            c["order_reference"] or "",
            format_money(c["initial_value"]), format_money(c["current_balance"]),
            c["status"], "sim" if c["is_released"] else "não",
            format_date_local(c["expires_at"]),
            format_datetime_local(c["created_at"]),
            format_datetime_local(c["last_sent_at"]),
            format_datetime_local(c["last_whatsapp_at"]),
            c["notes"] or "",
        ])
    output.seek(0)
    from flask import Response
    return Response(
        "﻿" + output.getvalue(),  # BOM for Excel UTF-8
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=cupons-male-{utc_now().strftime('%Y%m%d')}.csv"}
    )



@marketing_bp.route("/cards/<int:card_id>/quick-note", methods=["POST"], endpoint="quick_note")
@login_required
def quick_note(card_id: int):
    validate_csrf_or_abort()
    note = request.form.get("note", "").strip()
    with transaction() as db:
        card = db.execute("SELECT id FROM gift_cards WHERE id = %s", (card_id,)).fetchone()
        if not card:
            abort(404)
        db.execute("UPDATE gift_cards SET notes = %s, updated_at = %s WHERE id = %s",
                   (note or None, utc_now_iso(), card_id))
        write_audit(db, card_id, "edit_note", f"Observação atualizada.")
    flash("Observação salva.", "success")
    return redirect(url_for("card_detail", card_id=card_id))



@marketing_bp.route("/cards/<int:card_id>/resend-email", methods=["POST"], endpoint="resend_card_email")
@login_required
def resend_card_email(card_id: int):
    validate_csrf_or_abort()
    if not smtp_is_configured(current_app):
        flash("Configure o SMTP/Brevo antes de enviar e-mails.", "danger")
        return redirect(url_for("list_cards"))
    db = get_db()
    card = db.execute("SELECT * FROM gift_cards WHERE id = %s", (card_id,)).fetchone()
    if not card:
        abort(404)
    recipient_email = card["recipient_email"]
    if not recipient_email:
        flash("Esse cupom não tem e-mail cadastrado.", "danger")
        return redirect(url_for("list_cards"))
    visible_code = decrypt_visible_code(current_app, card["encrypted_code"])
    subject, html, text = build_card_email(card, current_app.config["PUBLIC_BASE_URL"], visible_code)
    image_bytes = generate_card_image(card).getvalue()
    try:
        send_email(
            current_app, recipient_email, card["recipient_name"] or "",
            subject, html, text,
            attachments=[(f"cupom-male-{sanitize_filename_part(card['recipient_name'], 'presente')}.png", image_bytes, "image/png")],
        )
    except Exception as exc:
        flash(f"Falha ao reenviar: {exc}", "danger")
        return redirect(url_for("list_cards"))
    with transaction() as db2:
        db2.execute("UPDATE gift_cards SET recipient_email = %s, last_sent_at = %s, updated_at = %s WHERE id = %s",
                    (recipient_email, utc_now_iso(), utc_now_iso(), card_id))
        write_audit(db2, card_id, "send_email", f"Vale reenviado para {recipient_email} via lista.")
    flash(f"Vale reenviado para {recipient_email}.", "success")
    return redirect(url_for("list_cards"))



@marketing_bp.route("/coupons", endpoint="list_coupons")
@login_required
def list_coupons():
    coupons = get_db().execute("SELECT * FROM discount_coupons ORDER BY created_at DESC").fetchall()
    return render_template("coupons.html", coupons=coupons)



@marketing_bp.route("/coupons/new", methods=["GET","POST"], endpoint="create_coupon")
@login_required
def create_coupon():
    if request.method == "POST":
        validate_csrf_or_abort()
        code  = request.form.get("code","").strip().upper()
        ctype = request.form.get("type","percent")
        try:
            value = str(parse_money(request.form.get("value","0")))
        except Exception:
            flash("Valor inválido.","danger"); return redirect(url_for("create_coupon"))
        min_p = str(parse_money(request.form.get("min_purchase","0") or "0"))
        max_u = request.form.get("max_uses","").strip()
        max_u = int(max_u) if max_u else None
        exp   = request.form.get("expires_at","").strip() or None
        desc  = request.form.get("description","").strip() or None
        user  = get_current_user()
        try:
            with transaction() as db:
                db.execute("""INSERT INTO discount_coupons(code,description,type,value,min_purchase,max_uses,expires_at,created_at,created_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (code,desc,ctype,value,min_p,max_u,exp,utc_now_iso(),user["display_name"]))
            flash(f"Cupom {code} criado!","success")
        except Exception:
            flash("Código já existe.","danger")
        return redirect(url_for("list_coupons"))
    return render_template("create_coupon.html")



@marketing_bp.route("/coupons/<int:cid>/toggle", methods=["POST"], endpoint="toggle_coupon")
@login_required
def toggle_coupon(cid):
    validate_csrf_or_abort()
    c = get_db().execute("SELECT * FROM discount_coupons WHERE id=%s",(cid,)).fetchone()
    if not c: abort(404)
    with transaction() as db:
        db.execute("UPDATE discount_coupons SET is_active=%s WHERE id=%s", (0 if c["is_active"] else 1, cid))
    flash("Cupom atualizado.","success"); return redirect(url_for("list_coupons"))



@marketing_bp.route("/coupons/validate", endpoint="validate_coupon_api")
@login_required
def validate_coupon_api():
    code = request.args.get("code","").upper()
    subtotal_s = request.args.get("subtotal","0")
    try:
        subtotal = Decimal(subtotal_s)
    except Exception:
        subtotal = Decimal("0")
    c = get_db().execute("SELECT * FROM discount_coupons WHERE code=%s",(code,)).fetchone()
    valid, msg, disc = validate_coupon_row(c, subtotal, datetime.now(LOCAL_TZ))
    if not valid:
        return {"valid":False,"message":msg}
    return {"valid":True,"message":msg,"discount":str(disc),"description":c["description"] or ""}



@marketing_bp.route("/campaigns", endpoint="list_campaigns")
@login_required
def list_campaigns():
    campaigns = get_db().execute("SELECT * FROM whatsapp_campaigns ORDER BY created_at DESC").fetchall()
    opt_in    = get_db().execute("SELECT COUNT(*) FROM customers WHERE whatsapp_opt_in=1 AND phone IS NOT NULL").fetchone()[0]
    return render_template("campaigns.html", campaigns=campaigns, opt_in_count=opt_in)



@marketing_bp.route("/campaigns/new", methods=["GET","POST"], endpoint="create_campaign")
@login_required
def create_campaign():
    db = get_db()
    if request.method == "POST":
        validate_csrf_or_abort()
        name = request.form.get("name","").strip()
        msg  = request.form.get("message_template","").strip()
        if not name or not msg:
            flash("Nome e mensagem são obrigatórios.","danger"); return redirect(url_for("create_campaign"))
        user = get_current_user()
        with transaction() as db2:
            new_campaign_id = insert_returning_id("""INSERT INTO whatsapp_campaigns(name,message_template,status,target_filter,created_at,created_by)
                VALUES(%s,%s,'draft',%s,%s,%s)""", (name,msg,request.form.get("target_filter","all"),utc_now_iso(),user["display_name"]))
        flash(f"Campanha «{name}» criada.","success")
        return redirect(url_for("campaign_detail",campaign_id=new_campaign_id))
    return render_template("create_campaign.html",
                           opt_in_count=db.execute("SELECT COUNT(*) FROM customers WHERE whatsapp_opt_in=1").fetchone()[0])



@marketing_bp.route("/campaigns/<int:campaign_id>", endpoint="campaign_detail")
@login_required
def campaign_detail(campaign_id):
    db = get_db()
    c  = db.execute("SELECT * FROM whatsapp_campaigns WHERE id=%s",(campaign_id,)).fetchone()
    if not c: abort(404)
    logs = db.execute("""SELECT cl.*,cu.name as customer_name,cu.phone FROM campaign_logs cl
        JOIN customers cu ON cu.id=cl.customer_id WHERE cl.campaign_id=%s ORDER BY cl.id DESC""", (campaign_id,)).fetchall()
    targets = db.execute("SELECT * FROM customers WHERE whatsapp_opt_in=1 AND phone IS NOT NULL ORDER BY name").fetchall()
    return render_template("campaign_detail.html", campaign=c, logs=logs, targets=targets)



@marketing_bp.route("/campaigns/<int:campaign_id>/send", methods=["POST"], endpoint="send_campaign")
@login_required
def send_campaign(campaign_id):
    validate_csrf_or_abort()
    db = get_db()
    c  = db.execute("SELECT * FROM whatsapp_campaigns WHERE id=%s",(campaign_id,)).fetchone()
    if not c: abort(404)
    targets = db.execute("SELECT * FROM customers WHERE whatsapp_opt_in=1 AND phone IS NOT NULL").fetchall()
    now = utc_now_iso(); sent = 0
    with transaction() as db2:
        for t in targets:
            phone = normalize_phone(t["phone"])
            if not phone: continue
            msg   = c["message_template"].replace("{nome}", t["name"] or "cliente")
            wa    = f"https://wa.me/{phone}?text={quote_plus(msg)}"
            db2.execute("INSERT OR IGNORE INTO campaign_logs(campaign_id,customer_id,status,whatsapp_url,sent_at) VALUES(%s,%s,%s,%s,%s)",
                        (campaign_id,t["id"],"sent",wa,now))
            sent += 1
        db2.execute("UPDATE whatsapp_campaigns SET status='sent',sent_count=%s,sent_at=%s WHERE id=%s", (sent,now,campaign_id))
    flash(f"Links gerados para {sent} clientes.","success")
    return redirect(url_for("campaign_detail",campaign_id=campaign_id))



# ─────────────────────────────────────────────────────────────────────────
# init_marketing_blueprint — registra aliases sem prefixo "marketing." para
# que url_for("list_cards"), url_for("list_coupons"), url_for("list_campaigns"),
# etc. continuem funcionando em todos os templates legados.
# ─────────────────────────────────────────────────────────────────────────
def init_marketing_blueprint(app):
    """
    Hook chamado por app.py após `app.register_blueprint(marketing_bp)`.

    Idempotente.
    """
    if getattr(app, "_marketing_bp_initialized", False):
        return

    ALIAS_MAP = {
        # gift cards
        "create_card":          "marketing.create_card",
        "list_cards":           "marketing.list_cards",
        "card_detail":          "marketing.card_detail",
        "card_image":           "marketing.card_image",
        "shared_card_image":    "marketing.shared_card_image",
        "send_card_email":      "marketing.send_card_email",
        "open_card_whatsapp":   "marketing.open_card_whatsapp",
        "toggle_release":       "marketing.toggle_release",
        "cancel_card":          "marketing.cancel_card",
        "redeem_amount":        "marketing.redeem_amount",
        "export_csv":           "marketing.export_csv",
        "quick_note":           "marketing.quick_note",
        "resend_card_email":    "marketing.resend_card_email",
        # coupons
        "list_coupons":         "marketing.list_coupons",
        "create_coupon":        "marketing.create_coupon",
        "toggle_coupon":        "marketing.toggle_coupon",
        "validate_coupon_api":  "marketing.validate_coupon_api",
        # campaigns
        "list_campaigns":       "marketing.list_campaigns",
        "create_campaign":      "marketing.create_campaign",
        "campaign_detail":      "marketing.campaign_detail",
        "send_campaign":        "marketing.send_campaign",
    }

    _ROUTE_DEFS = [
        ("create_card",         "/cards/new",                            ("GET", "POST")),
        ("list_cards",          "/cards",                                ("GET",)),
        ("card_detail",         "/cards/<int:card_id>",                  ("GET",)),
        ("card_image",          "/cards/<int:card_id>/image",            ("GET",)),
        ("shared_card_image",   "/shared/card/<share_token>/image",      ("GET",)),
        ("send_card_email",     "/cards/<int:card_id>/send-email",       ("POST",)),
        ("open_card_whatsapp",  "/cards/<int:card_id>/open-whatsapp",    ("POST",)),
        ("toggle_release",      "/cards/<int:card_id>/toggle-release",   ("POST",)),
        ("cancel_card",         "/cards/<int:card_id>/cancel",           ("POST",)),
        ("redeem_amount",       "/cards/<int:card_id>/redeem",           ("POST",)),
        ("export_csv",          "/cards/export-csv",                     ("GET",)),
        ("quick_note",          "/cards/<int:card_id>/quick-note",       ("POST",)),
        ("resend_card_email",   "/cards/<int:card_id>/resend-email",     ("POST",)),
        ("list_coupons",        "/coupons",                              ("GET",)),
        ("create_coupon",       "/coupons/new",                          ("GET", "POST")),
        ("toggle_coupon",       "/coupons/<int:cid>/toggle",             ("POST",)),
        ("validate_coupon_api", "/coupons/validate",                     ("GET",)),
        ("list_campaigns",      "/campaigns",                            ("GET",)),
        ("create_campaign",     "/campaigns/new",                        ("GET", "POST")),
        ("campaign_detail",     "/campaigns/<int:campaign_id>",          ("GET",)),
        ("send_campaign",       "/campaigns/<int:campaign_id>/send",     ("POST",)),
    ]

    for short, full in ALIAS_MAP.items():
        view = app.view_functions.get(full)
        if view is None:
            app.logger.warning(
                "Marketing blueprint: endpoint %r ausente — alias %r não registrado.",
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

    app._marketing_bp_initialized = True
