"""
blueprints/admin.py — Blueprint administrativo.

Escopo (rotas restritas a admin via @require_role("admin")):

  Gestão de usuários (7 rotas)
    GET  /users                              → list_users
    GET/POST /users/new                      → create_user
    POST /users/<id>/toggle                  → toggle_user
    POST /users/<id>/delete                  → delete_user
    POST /users/<id>/promote                 → promote_user
    POST /users/<id>/send-reset              → send_user_reset
    GET  /security/audit                     → security_audit_view

  Hero do catálogo público (5 rotas)
    GET  /admin/hero-images                  → hero_images_admin
    POST /admin/hero-images/upload           → hero_images_upload
    POST /admin/hero-images/<hid>/delete     → hero_images_delete
    POST /admin/hero-images/<hid>/toggle     → hero_images_toggle
    POST /admin/hero-images/<hid>/reorder    → hero_images_reorder

  Configurações da loja (1 rota)
    GET/POST /settings                       → store_settings_view

  Contas PIX (4 rotas)
    GET  /settings/pix-accounts              → list_pix_accounts
    GET/POST /settings/pix-accounts/new      → create_pix_account
    POST /settings/pix-accounts/<aid>/toggle → toggle_pix_account
    POST /settings/pix-accounts/<aid>/delete → delete_pix_account

  Backup (1 rota)
    GET  /admin/backup                       → admin_backup

Notas (Onda 2 — Etapa D):

  1. Endpoints PRESERVAM nomes globais via aliases registrados em
     init_admin_blueprint(). Templates seguem usando url_for(\'list_users\'),
     url_for(\'store_settings_view\'), etc. sem prefixo \'admin.\'.

  2. Helpers (get_db, audit_log, require_role, require_sudo, etc.) são
     importados de app.py em top-level. Mesma técnica de auth/public.

  3. Substituições aplicadas durante extração:
     - bare `app` → `current_app` (5 ocorrências em send_email,
       smtp_is_configured, app.config) — necessário porque o blueprint
       não tem o `app` no escopo léxico da fábrica original.

  4. REMOVIDO o monkey-patch redundante de store_settings_view que existia
     em app.py (Onda 2/A linhas 3340-3354). As 5 chaves que ele persistia
     (catalog_show_prices, store_whatsapp, responsible_phones,
     catalog_tagline, catalog_instagram) já estão na lista `keys` da view
     original — o patch era arqueologia. Auditado e confirmado redundante
     antes da remoção.

  5. Código das views é EXTRAÇÃO VERBATIM de app.py. Comportamento
     idêntico ao original. Apenas decorators @app.route trocados por
     @admin_bp.route(..., endpoint="...") e referências a `app` ajustadas.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta

from flask import (
    Blueprint, current_app, request, render_template, redirect, url_for,
    abort, Response, jsonify, session, flash, send_file,
)
from werkzeug.security import generate_password_hash

# Helpers do app principal — import top-level. Funciona porque admin.py
# é importado em app.py APÓS `app = create_app()`. Mesma técnica usada
# em lookbook.py / blueprints/auth.py / blueprints/public.py.
from app import (
    LOCAL_TZ,
    audit_log,
    bump_session_version,
    create_password_reset_token,
    ensure_csrf_token,
    login_required,
    password_is_strong,
    psp_encrypt,
    refresh_user_role_hmac,
    require_role,
    require_sudo,
    send_email,
    set_setting,
    smtp_is_configured,
    utc_now,
    utc_now_iso,
    validate_csrf_or_abort,
    _normalize_uploaded_image,
)
from db import get_db, transaction


admin_bp = Blueprint("admin", __name__, template_folder="../templates")


@admin_bp.route("/users", endpoint="list_users")
@require_role("admin")
def list_users():
    users = get_db().execute(
        "SELECT id, email, display_name, role, is_active, created_at, last_login_at, "
        "COALESCE(mfa_enabled,0) AS mfa_enabled "
        "FROM users ORDER BY id DESC"
    ).fetchall()
    ensure_csrf_token()
    return render_template("users.html", users=users, current_user_id=session.get("user_id"))



@admin_bp.route("/users/new", methods=["GET", "POST"], endpoint="create_user")
@require_role("admin")
@require_sudo
def create_user():
    if request.method == "POST":
        validate_csrf_or_abort()
        email = request.form.get("email", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()
        role = request.form.get("role", "operator")
        password = request.form.get("password", "")
        send_invite = request.form.get("send_invite") == "on"
        if not email or "@" not in email:
            flash("Informe um e-mail válido.", "danger")
        elif not display_name:
            flash("Preencha o nome da pessoa.", "danger")
        elif role not in {"admin", "operator"}:
            flash("Perfil inválido.", "danger")
        elif not password_is_strong(password):
            flash("A senha inicial precisa ter pelo menos 10 caracteres, com letras e números.", "danger")
        else:
            db = get_db()
            existing = db.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
            if existing:
                flash("Esse e-mail já está cadastrado.", "danger")
            else:
                now = utc_now_iso()
                db.execute(
                    "INSERT INTO users (email, display_name, password_hash, role, is_active, created_at, updated_at) VALUES (%s, %s, %s, %s, 1, %s, %s)",
                    (email, display_name, generate_password_hash(password), role, now, now),
                )
                new_user = db.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
                if new_user:
                    refresh_user_role_hmac(db, new_user["id"])
                db.commit()
                audit_log(
                    "user_created",
                    target_id=new_user["id"] if new_user else None,
                    after=f"email={email} role={role}",
                )
                if send_invite and smtp_is_configured(current_app):
                    subject = "Seu acesso ao painel Tonton"
                    html = f"<p>Oi, {display_name}.</p><p>Seu acesso ao painel Tonton foi criado.</p><p><strong>Login:</strong> {email}</p><p>Acesse: <a href='{current_app.config['PUBLIC_BASE_URL'].rstrip('/')}/login'>{current_app.config['PUBLIC_BASE_URL'].rstrip('/')}/login</a></p>"
                    text = f"Oi, {display_name}.\nSeu acesso ao painel Tonton foi criado.\nLogin: {email}\nAcesse: {current_app.config['PUBLIC_BASE_URL'].rstrip('/')}/login"
                    try:
                        send_email(current_app, email, display_name, subject, html, text)
                    except Exception:
                        flash("Usuário criado, mas o e-mail não foi enviado. Confira o SMTP/Brevo.", "warning")
                flash("Usuário criado.", "success")
                return redirect(url_for("list_users"))
    ensure_csrf_token()
    return render_template("create_user.html")



@admin_bp.route("/users/<int:user_id>/toggle", methods=["POST"], endpoint="toggle_user")
@require_role("admin")
@require_sudo
def toggle_user(user_id: int):
    validate_csrf_or_abort()
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    if not user:
        abort(404)
    if user["id"] == session.get("user_id") and int(user["is_active"]):
        flash("Você não pode desativar sua própria conta logada.", "danger")
        return redirect(url_for("list_users"))
    # Trava: nao deixar desativar o ultimo admin ativo
    if user["role"] == "admin" and int(user["is_active"]):
        other_admins = db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND is_active = 1 AND id <> %s",
            (user_id,),
        ).fetchone()
        if int(other_admins["n"]) == 0:
            flash("Não é possível desativar o último admin ativo.", "danger")
            return redirect(url_for("list_users"))
    new_active = 0 if int(user["is_active"]) else 1
    db.execute(
        "UPDATE users SET is_active = %s, updated_at = %s WHERE id = %s",
        (new_active, utc_now_iso(), user_id),
    )
    # Desativar = derrubar sessoes vivas. Reativar tambem incrementa
    # para invalidar qualquer cookie residual.
    bump_session_version(db, user_id)
    db.commit()
    audit_log(
        "user_deactivated" if new_active == 0 else "user_reactivated",
        target_id=user_id,
        before=f"is_active={user['is_active']}",
        after=f"is_active={new_active}",
    )
    flash("Status do usuário atualizado.", "success")
    return redirect(url_for("list_users"))



@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"], endpoint="delete_user")
@require_role("admin")
@require_sudo
def delete_user(user_id: int):
    """Apaga apenas usuarios INATIVOS. Defesa em camadas:
    1. require_sudo: re-auth recente
    2. CSRF
    3. Confirmacao via parametro `confirm` no form (anti-clique acidental)
    4. Bloqueio de auto-exclusao
    5. Bloqueio do ultimo admin
    """
    validate_csrf_or_abort()
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    if not user:
        abort(404)
    if user["id"] == session.get("user_id"):
        flash("Você não pode apagar sua própria conta.", "danger")
        return redirect(url_for("list_users"))
    if int(user["is_active"]):
        flash("Apenas usuários desativados podem ser apagados. Desative antes.", "warning")
        return redirect(url_for("list_users"))
    if request.form.get("confirm") != "DELETE":
        flash("Confirmação inválida.", "danger")
        return redirect(url_for("list_users"))
    # Audit ANTES do delete para preservar o registro
    audit_log(
        "user_deleted",
        target_id=user_id,
        before=f"email={user['email']} role={user['role']}",
    )
    db.execute("DELETE FROM users WHERE id = %s", (user_id,))
    db.commit()
    flash(f"Usuário {user['display_name']} apagado.", "success")
    return redirect(url_for("list_users"))



@admin_bp.route("/users/<int:user_id>/promote", methods=["POST"], endpoint="promote_user")
@require_role("admin")
@require_sudo
def promote_user(user_id: int):
    """Toggle de role admin <-> operator. Bumpa session_version e
    recalcula HMAC, garantindo consistencia."""
    validate_csrf_or_abort()
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    if not user:
        abort(404)
    if user["id"] == session.get("user_id"):
        flash("Você não pode mudar seu próprio perfil. Peça a outro admin.", "danger")
        return redirect(url_for("list_users"))
    new_role = "operator" if user["role"] == "admin" else "admin"
    # Trava: nao rebaixar o ultimo admin
    if user["role"] == "admin" and new_role != "admin":
        other_admins = db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND is_active = 1 AND id <> %s",
            (user_id,),
        ).fetchone()
        if int(other_admins["n"]) == 0:
            flash("Não é possível rebaixar o último admin ativo.", "danger")
            return redirect(url_for("list_users"))
    db.execute(
        "UPDATE users SET role = %s, updated_at = %s WHERE id = %s",
        (new_role, utc_now_iso(), user_id),
    )
    # Mudanca de role: bumpa sessao + recalcula HMAC
    bump_session_version(db, user_id)
    db.commit()
    audit_log(
        "role_changed",
        target_id=user_id,
        before=f"role={user['role']}",
        after=f"role={new_role}",
    )
    flash(f"Perfil atualizado para {new_role}.", "success")
    return redirect(url_for("list_users"))



@admin_bp.route("/users/<int:user_id>/send-reset", methods=["POST"], endpoint="send_user_reset")
@require_role("admin")
@require_sudo
def send_user_reset(user_id: int):
    validate_csrf_or_abort()
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    if not user:
        abort(404)
    token = create_password_reset_token()
    expires = (utc_now() + timedelta(hours=1)).isoformat()
    db.execute("UPDATE users SET reset_token = %s, reset_expires_at = %s, updated_at = %s WHERE id = %s", (token, expires, utc_now_iso(), user_id))
    db.commit()
    audit_log("password_reset_requested", target_id=user_id)
    if smtp_is_configured(current_app):
        reset_url = f"{current_app.config['PUBLIC_BASE_URL'].rstrip('/')}{url_for('reset_password', token=token)}"
        subject = "Redefinição de senha · Tonton"
        html = f"<p>Oi, {user['display_name']}.</p><p>Para definir uma nova senha, clique aqui:</p><p><a href='{reset_url}'>{reset_url}</a></p>"
        text = f"Oi, {user['display_name']}.\nDefina uma nova senha aqui: {reset_url}"
        try:
            send_email(current_app, user["email"], user["display_name"], subject, html, text)
            flash("Link de redefinição enviado por e-mail.", "success")
        except Exception:
            flash("Não foi possível enviar o e-mail. Confira o SMTP.", "danger")
    else:
        flash("SMTP não configurado. O link não pôde ser enviado.", "danger")
    return redirect(url_for("list_users"))



@admin_bp.route("/security/audit", endpoint="security_audit_view")
@require_role("admin")
def security_audit_view():
    rows = get_db().execute(
        "SELECT * FROM security_audit ORDER BY at DESC LIMIT 200"
    ).fetchall()
    ensure_csrf_token()
    return render_template("security_audit.html", events=rows)



@admin_bp.route("/admin/hero-images", methods=["GET"], endpoint="hero_images_admin")
@login_required
@require_role("admin")
def hero_images_admin():
    """Lista imagens do hero (carrossel da vitrine pública)."""
    db = get_db()
    images = db.execute("""
        SELECT id, image_mime, caption, sort_order, is_active, image_version, created_at
        FROM catalog_hero_images
        ORDER BY sort_order ASC, id ASC
    """).fetchall()
    return render_template("hero_images_admin.html", images=images)



@admin_bp.route("/admin/hero-images/upload", methods=["POST"], endpoint="hero_images_upload")
@login_required
@require_role("admin")
def hero_images_upload():
    """Sobe nova imagem para o carrossel do hero."""
    validate_csrf_or_abort()
    f = request.files.get("image")
    if not f or not f.filename:
        flash("Selecione uma imagem.", "danger")
        return redirect(url_for("hero_images_admin"))

    mime = (f.mimetype or "").lower()
    accepted = ("image/jpeg", "image/jpg", "image/png", "image/webp",
                "image/heic", "image/heif")
    if mime not in accepted:
        flash("Formato não suportado. Use JPG, PNG, WebP ou HEIC.", "danger")
        return redirect(url_for("hero_images_admin"))

    blob = f.read()
    if len(blob) > 25 * 1024 * 1024:
        flash("Imagem maior que 25 MB.", "danger")
        return redirect(url_for("hero_images_admin"))
    if not blob:
        flash("Arquivo vazio.", "danger")
        return redirect(url_for("hero_images_admin"))

    try:
        blob, mime = _normalize_uploaded_image(blob, mime)
    except ValueError as e:
        flash(f"Não foi possível processar a imagem: {e}", "danger")
        return redirect(url_for("hero_images_admin"))

    caption = (request.form.get("caption", "") or "").strip()[:120]
    with transaction() as db:
        next_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM catalog_hero_images"
        ).fetchone()["n"]
        db.execute(
            "INSERT INTO catalog_hero_images "
            "(image_blob, image_mime, caption, sort_order, is_active, "
            " image_version, created_at) "
            "VALUES (%s, %s, %s, %s, 1, 1, %s)",
            (blob, mime, caption or None, next_order, utc_now_iso())
        )
    flash("Imagem adicionada ao hero.", "success")
    return redirect(url_for("hero_images_admin"))


# ─────────────────────────────────────────────────────────────────────────
# Open Graph image — imagem fixa para preview de link em redes sociais.
# Armazenada como base64 em store_settings (chaves catalog_og_image_b64
# + catalog_og_image_mime). Aceita upload via /settings.
# ─────────────────────────────────────────────────────────────────────────
@admin_bp.route("/admin/og-image/upload", methods=["POST"], endpoint="og_image_upload")
@login_required
@require_role("admin")
def og_image_upload():
    """Sobe imagem OG (preview do link em Insta/WA/FB).
    Idealmente 1200x630px, JPG ou PNG."""
    import base64
    validate_csrf_or_abort()
    f = request.files.get("og_image")
    if not f or not f.filename:
        flash("Selecione uma imagem.", "danger")
        return redirect(url_for("store_settings_view") + "#og")

    mime = (f.mimetype or "").lower()
    if mime not in ("image/jpeg", "image/jpg", "image/png", "image/webp"):
        flash("Use JPG, PNG ou WebP para a imagem de compartilhamento.", "danger")
        return redirect(url_for("store_settings_view") + "#og")

    blob = f.read()
    if len(blob) > 5 * 1024 * 1024:
        flash("Imagem OG maior que 5 MB. Reduza ou comprima.", "danger")
        return redirect(url_for("store_settings_view") + "#og")
    if not blob:
        flash("Arquivo vazio.", "danger")
        return redirect(url_for("store_settings_view") + "#og")

    # Normaliza orientação EXIF e converte se necessário.
    try:
        blob, mime = _normalize_uploaded_image(blob, mime)
    except ValueError as e:
        flash(f"Não foi possível processar: {e}", "danger")
        return redirect(url_for("store_settings_view") + "#og")

    # Armazena como base64 em store_settings (1 chave para blob, outra para mime).
    encoded = base64.b64encode(blob).decode("ascii")
    set_setting("catalog_og_image_b64", encoded)
    set_setting("catalog_og_image_mime", mime)
    flash("Imagem de compartilhamento atualizada.", "success")
    return redirect(url_for("store_settings_view") + "#og")


@admin_bp.route("/admin/og-image/delete", methods=["POST"], endpoint="og_image_delete")
@login_required
@require_role("admin")
def og_image_delete():
    """Remove a imagem OG (volta a usar fallback)."""
    validate_csrf_or_abort()
    set_setting("catalog_og_image_b64", "")
    set_setting("catalog_og_image_mime", "")
    flash("Imagem de compartilhamento removida.", "info")
    return redirect(url_for("store_settings_view") + "#og")



@admin_bp.route("/admin/hero-images/<int:hid>/delete", methods=["POST"], endpoint="hero_images_delete")
@login_required
@require_role("admin")
def hero_images_delete(hid):
    validate_csrf_or_abort()
    with transaction() as db:
        db.execute("DELETE FROM catalog_hero_images WHERE id=%s", (hid,))
    flash("Imagem removida do hero.", "success")
    return redirect(url_for("hero_images_admin"))



@admin_bp.route("/admin/hero-images/<int:hid>/toggle", methods=["POST"], endpoint="hero_images_toggle")
@login_required
@require_role("admin")
def hero_images_toggle(hid):
    """Liga/desliga uma imagem específica sem deletar (útil para campanhas)."""
    validate_csrf_or_abort()
    with transaction() as db:
        row = db.execute(
            "SELECT is_active FROM catalog_hero_images WHERE id=%s", (hid,)
        ).fetchone()
        if row:
            new_state = 0 if int(row["is_active"]) == 1 else 1
            db.execute(
                "UPDATE catalog_hero_images SET is_active=%s, updated_at=%s "
                "WHERE id=%s",
                (new_state, utc_now_iso(), hid)
            )
    return redirect(url_for("hero_images_admin"))



@admin_bp.route("/admin/hero-images/<int:hid>/reorder", methods=["POST"], endpoint="hero_images_reorder")
@login_required
@require_role("admin")
def hero_images_reorder(hid):
    """Move imagem para cima ou para baixo na ordem do carrossel."""
    validate_csrf_or_abort()
    direction = request.form.get("direction", "")
    if direction not in ("up", "down"):
        return redirect(url_for("hero_images_admin"))
    with transaction() as db:
        current = db.execute(
            "SELECT id, sort_order FROM catalog_hero_images WHERE id=%s", (hid,)
        ).fetchone()
        if not current:
            return redirect(url_for("hero_images_admin"))
        if direction == "up":
            neighbor = db.execute(
                "SELECT id, sort_order FROM catalog_hero_images "
                "WHERE sort_order < %s ORDER BY sort_order DESC LIMIT 1",
                (current["sort_order"],)
            ).fetchone()
        else:
            neighbor = db.execute(
                "SELECT id, sort_order FROM catalog_hero_images "
                "WHERE sort_order > %s ORDER BY sort_order ASC LIMIT 1",
                (current["sort_order"],)
            ).fetchone()
        if neighbor:
            db.execute(
                "UPDATE catalog_hero_images SET sort_order=%s WHERE id=%s",
                (neighbor["sort_order"], current["id"])
            )
            db.execute(
                "UPDATE catalog_hero_images SET sort_order=%s WHERE id=%s",
                (current["sort_order"], neighbor["id"])
            )
    return redirect(url_for("hero_images_admin"))

# Rota pública para servir a imagem do hero


@admin_bp.route("/settings", methods=["GET","POST"], endpoint="store_settings_view")
@login_required
@require_role("admin")
def store_settings_view():
    db = get_db()
    if request.method == "POST":
        validate_csrf_or_abort()
        keys = [
            "store_name", "catalog_tagline", "catalog_instagram", "store_whatsapp",
            "catalog_show_prices", "responsible_phones",
            "target_margin_pct", "min_margin_alert_pct", "tax_rate_pct", "default_payment",
            "loyalty_points_per_real", "loyalty_redeem_ratio",
            "sidebar_promo_title", "sidebar_promo_sub", "sidebar_promo_url",
            "catalog_heading", "catalog_subheading",
            "catalog_hero_mode", "catalog_item_click_target",
            "label_width_mm", "label_height_mm",
            "dispatch_label_width_mm", "dispatch_label_height_mm",
            "store_legal_name", "store_tax_id", "store_state_tax_id",
            "store_tax_regime", "store_operation_nature",
            "store_contact_phone", "store_contact_email",
            "store_address_street", "store_address_number", "store_address_complement",
            "store_address_neighborhood", "store_address_city", "store_address_state",
            "store_address_zipcode",
            "atender_wa_template", "abc_default_window", "inactive_days_threshold",
            # PIX (Fase 1: copia-e-cola, sem PSP)
            "pix_key", "pix_key_type", "pix_merchant_name", "pix_merchant_city",
            # Tema sazonal (manual)
            "active_theme",
            # ─── Footer do catálogo público ────────────────────────────
            # Configurações editáveis sem mexer em código/CSS:
            "catalog_footer_credo",       # frase manifesto curta (2 linhas idealmente)
            "catalog_footer_credit",      # crédito de fotografia/styling (mono pequeno)
            "catalog_newsletter_enabled", # "1" ou "0" — exibir bloco newsletter
            "catalog_newsletter_intro",   # frase chamativa (ex: "novidades em primeira mão")
            "catalog_footer_email",       # email de contato (opcional, complementa WA)
            # ─── Hero do catálogo público ─────────────────────────────
            "catalog_hero_concept",       # frase-conceito da estação (ex: "Outono em camadas")
            "catalog_season",             # eyebrow do hero (ex: "Edição · Outono 26")
            # ─── Página /sobre ───────────────────────────────────────
            # 3 blocos de texto editorial. Defaults aceitáveis no public.py.
            "about_origin",               # origem da marca (~3 linhas)
            "about_process",              # processo / curadoria (~3 linhas)
            "about_manifesto",            # manifesto / filosofia (~3 linhas)
            # ─── Mensagem padrão WhatsApp do catálogo público ───────
            # Quando visitante clica "Conversar" no hero/header/footer,
            # abre WA já com este texto pré-preenchido.
            "catalog_wa_default",
        ]
        for k in keys:
            if k in ("catalog_show_prices", "catalog_newsletter_enabled"):
                set_setting(k, "1" if request.form.get(k) else "0")
                continue
            v = request.form.get(k, "").strip()
            set_setting(k, v)
        flash("Configurações salvas.","success")
        active_tab = (request.form.get("active_tab") or "").strip()
        valid_tabs = {"loja", "operacao", "fiscal", "impressao"}
        if active_tab in valid_tabs:
            return redirect(url_for("store_settings_view", tab=active_tab))
        return redirect(url_for("store_settings_view"))
    settings = {r["key"]:r["value"] for r in db.execute("SELECT key,value FROM store_settings").fetchall()}
    # Contadores para o card "Vitrine pública" na aba Loja
    hero_count_row = db.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) AS active "
        "FROM catalog_hero_images"
    ).fetchone()
    hero_images_count  = (hero_count_row["total"] if hero_count_row else 0) or 0
    hero_images_active = (hero_count_row["active"] if hero_count_row else 0) or 0
    return render_template("settings.html",
                           settings=settings,
                           hero_images_count=hero_images_count,
                           hero_images_active=hero_images_active)



@admin_bp.route("/settings/pix-accounts", endpoint="list_pix_accounts")
@require_role("admin")
def list_pix_accounts():
    rows = get_db().execute(
        "SELECT id, provider, label, is_active, is_default, created_at "
        "FROM pix_provider_accounts ORDER BY is_default DESC, id ASC"
    ).fetchall()
    ensure_csrf_token()
    return render_template("pix_accounts.html", accounts=rows)



@admin_bp.route("/settings/pix-accounts/new", methods=["GET", "POST"], endpoint="create_pix_account")
@require_role("admin")
@require_sudo
def create_pix_account():
    if request.method == "POST":
        validate_csrf_or_abort()
        provider = (request.form.get("provider") or "").strip().lower()
        label = (request.form.get("label") or "").strip()
        if provider not in {"manual", "inter"}:
            flash("Provider invalido.", "danger")
            return redirect(url_for("create_pix_account"))
        if not label:
            flash("Informe um rotulo para identificar a conta.", "danger")
            return redirect(url_for("create_pix_account"))

        credentials: dict = {}
        if provider == "manual":
            credentials = {
                "pix_key": (request.form.get("pix_key") or "").strip(),
                "key_type": (request.form.get("key_type") or "").strip(),
                "merchant_name": (request.form.get("merchant_name") or "").strip(),
                "merchant_city": (request.form.get("merchant_city") or "").strip(),
            }
        elif provider == "inter":
            # cert/key chegam como textareas com PEM completo
            credentials = {
                "client_id": (request.form.get("client_id") or "").strip(),
                "client_secret": (request.form.get("client_secret") or "").strip(),
                "cert_pem": (request.form.get("cert_pem") or "").strip(),
                "key_pem": (request.form.get("key_pem") or "").strip(),
                "pix_key": (request.form.get("pix_key") or "").strip(),
                "webhook_secret": (request.form.get("webhook_secret") or "").strip(),
            }

        db = get_db()
        now = utc_now_iso()
        cred_blob = psp_encrypt(json.dumps(credentials, ensure_ascii=False))
        is_default = 1 if request.form.get("is_default") == "on" else 0

        # Se marcado como default, desmarca outros do mesmo provider
        if is_default:
            db.execute(
                "UPDATE pix_provider_accounts SET is_default = 0 WHERE provider = %s",
                (provider,),
            )

        db.execute(
            "INSERT INTO pix_provider_accounts "
            "(provider, label, is_active, is_default, credentials_encrypted, settings_json, created_at, updated_at) "
            "VALUES (%s, %s, 1, %s, %s, '{}', %s, %s)",
            (provider, label, is_default, cred_blob, now, now),
        )
        db.commit()
        audit_log("pix_account_created", extra=f"provider={provider} label={label}")
        flash("Conta PIX cadastrada.", "success")
        return redirect(url_for("list_pix_accounts"))

    ensure_csrf_token()
    return render_template("pix_account_form.html")



@admin_bp.route("/settings/pix-accounts/<int:aid>/toggle", methods=["POST"], endpoint="toggle_pix_account")
@require_role("admin")
@require_sudo
def toggle_pix_account(aid: int):
    validate_csrf_or_abort()
    db = get_db()
    row = db.execute(
        "SELECT id, is_active FROM pix_provider_accounts WHERE id = %s", (aid,)
    ).fetchone()
    if not row:
        abort(404)
    new_active = 0 if int(row["is_active"]) else 1
    db.execute(
        "UPDATE pix_provider_accounts SET is_active = %s, updated_at = %s WHERE id = %s",
        (new_active, utc_now_iso(), aid),
    )
    db.commit()
    audit_log(
        "pix_account_toggled",
        target_id=aid,
        after=f"is_active={new_active}",
    )
    flash("Conta atualizada.", "success")
    return redirect(url_for("list_pix_accounts"))



@admin_bp.route("/settings/pix-accounts/<int:aid>/delete", methods=["POST"], endpoint="delete_pix_account")
@require_role("admin")
@require_sudo
def delete_pix_account(aid: int):
    validate_csrf_or_abort()
    if request.form.get("confirm") != "DELETE":
        flash("Confirmacao invalida.", "danger")
        return redirect(url_for("list_pix_accounts"))
    db = get_db()
    # Nao apaga se ha cobrancas pendentes
    pending = db.execute(
        "SELECT COUNT(*) AS n FROM pix_charges WHERE account_id = %s AND status = 'pending'",
        (aid,),
    ).fetchone()
    if pending and int(pending["n"]) > 0:
        flash("Existem cobrancas pendentes nessa conta. Desative em vez de apagar.", "warning")
        return redirect(url_for("list_pix_accounts"))
    audit_log("pix_account_deleted", target_id=aid)
    db.execute("DELETE FROM pix_provider_accounts WHERE id = %s", (aid,))
    db.commit()
    flash("Conta apagada.", "success")
    return redirect(url_for("list_pix_accounts"))



@admin_bp.route("/admin/backup", endpoint="admin_backup")
@require_role("admin")
def admin_backup():
    db = get_db()

    # Lista tabelas do schema atual em ordem (resolve dependências de FK
    # da forma mais simples: usa a ordem natural do schema).
    tables = db.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """).fetchall()

    out = io.StringIO()
    out.write("-- Tonton Cupom — backup completo\n")
    out.write(f"-- Gerado em: {utc_now_iso()}\n")
    out.write("-- Restaurar: psql $DATABASE_URL -f este_arquivo.sql\n")
    out.write("-- ATENÇÃO: este script trunca as tabelas antes de inserir.\n\n")
    out.write("BEGIN;\n")
    out.write("SET session_replication_role = 'replica';\n\n")

    for t in tables:
        tname = t["table_name"]
        # Pula tabelas de sistema/sessão se houver
        if tname.startswith("_") or tname in ("schema_migrations",):
            continue

        cols = db.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s
            ORDER BY ordinal_position
        """, (tname,)).fetchall()
        if not cols:
            continue
        col_names = [c["column_name"] for c in cols]
        col_list = ", ".join(f'"{c}"' for c in col_names)

        out.write(f"-- Tabela: {tname}\n")
        out.write(f'TRUNCATE TABLE "{tname}" RESTART IDENTITY CASCADE;\n')

        rows = db.execute(f'SELECT {col_list} FROM "{tname}"').fetchall()
        for r in rows:
            values = []
            for cn in col_names:
                v = r[cn]
                if v is None:
                    values.append("NULL")
                elif isinstance(v, bool):
                    values.append("TRUE" if v else "FALSE")
                elif isinstance(v, (int, float)):
                    values.append(str(v))
                elif isinstance(v, (bytes, bytearray, memoryview)):
                    # bytea: decodifica como hex literal Postgres
                    b = bytes(v)
                    values.append("'\\x" + b.hex() + "'::bytea")
                else:
                    # Escapa aspas simples (padrão SQL)
                    s = str(v).replace("'", "''")
                    values.append(f"'{s}'")
            out.write(f'INSERT INTO "{tname}" ({col_list}) VALUES ({", ".join(values)});\n')
        out.write("\n")

    # Restaura sequences (depois dos INSERTs com IDs explícitos)
    seqs = db.execute("""
        SELECT
            pg_get_serial_sequence(c.table_schema || '.' || c.table_name, c.column_name) AS seq,
            c.table_name, c.column_name
        FROM information_schema.columns c
        WHERE c.table_schema = current_schema()
          AND c.column_default LIKE 'nextval%%'
    """).fetchall()
    out.write("-- Sincroniza sequences\n")
    for s in seqs:
        if s["seq"]:
            out.write(
                f"SELECT setval('{s['seq']}', "
                f"COALESCE((SELECT MAX(\"{s['column_name']}\") FROM \"{s['table_name']}\"), 1));\n"
            )
    out.write("\nSET session_replication_role = 'origin';\n")
    out.write("COMMIT;\n")

    body = out.getvalue().encode("utf-8")
    from urllib.parse import quote as _urlquote
    ts = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d-%H%M")
    filename = f"male-backup-{ts}.sql"
    resp = send_file(
        io.BytesIO(body),
        mimetype="application/sql",
        as_attachment=True,
        download_name=filename,
    )
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{filename}"; '
        f"filename*=UTF-8''{_urlquote(filename, safe='')}"
    )
    resp.headers["Cache-Control"] = "private, max-age=0, no-store"
    resp.headers["Content-Length"] = str(len(body))
    current_app.logger.info("admin backup generated: %d tables, %d bytes", len(tables), len(body))
    return resp

# ─── CURVA ABC DE PRODUTOS ──────────────────────────────────
# Classifica produtos em A/B/C pela receita gerada na janela
# selecionada (default: 90 dias). Mostra giro e dias de cobertura.



# ─────────────────────────────────────────────────────────────────────────
# init_admin_blueprint — registra aliases sem prefixo "admin." para que
# url_for("list_users"), url_for("store_settings_view") etc. continuem
# funcionando em todos os templates legados.
# ─────────────────────────────────────────────────────────────────────────
def init_admin_blueprint(app):
    """
    Hook chamado por app.py após `app.register_blueprint(admin_bp)`.

    Idempotente.
    """
    if getattr(app, "_admin_bp_initialized", False):
        return

    ALIAS_MAP = {
        "list_users":             "admin.list_users",
        "create_user":            "admin.create_user",
        "toggle_user":            "admin.toggle_user",
        "delete_user":            "admin.delete_user",
        "promote_user":           "admin.promote_user",
        "send_user_reset":        "admin.send_user_reset",
        "security_audit_view":    "admin.security_audit_view",
        "hero_images_admin":      "admin.hero_images_admin",
        "hero_images_upload":     "admin.hero_images_upload",
        "og_image_upload":        "admin.og_image_upload",
        "og_image_delete":        "admin.og_image_delete",
        "hero_images_delete":      "admin.hero_images_delete",
        "hero_images_toggle":      "admin.hero_images_toggle",
        "hero_images_reorder":     "admin.hero_images_reorder",
        "store_settings_view":    "admin.store_settings_view",
        "list_pix_accounts":      "admin.list_pix_accounts",
        "create_pix_account":        "admin.create_pix_account",
        "toggle_pix_account":     "admin.toggle_pix_account",
        "delete_pix_account":     "admin.delete_pix_account",
        "admin_backup":           "admin.admin_backup",
    }

    _ROUTE_DEFS = [
        ("list_users",          "/users",                                ("GET",)),
        ("create_user",         "/users/new",                            ("GET", "POST")),
        ("toggle_user",         "/users/<int:user_id>/toggle",           ("POST",)),
        ("delete_user",         "/users/<int:user_id>/delete",           ("POST",)),
        ("promote_user",        "/users/<int:user_id>/promote",          ("POST",)),
        ("send_user_reset",     "/users/<int:user_id>/send-reset",       ("POST",)),
        ("security_audit_view", "/security/audit",                       ("GET",)),
        ("hero_images_admin",   "/admin/hero-images",                    ("GET",)),
        ("hero_images_upload",  "/admin/hero-images/upload",             ("POST",)),
        ("og_image_upload",     "/admin/og-image/upload",                ("POST",)),
        ("og_image_delete",     "/admin/og-image/delete",                ("POST",)),
        ("hero_images_delete",   "/admin/hero-images/<int:hid>/delete",   ("POST",)),
        ("hero_images_toggle",   "/admin/hero-images/<int:hid>/toggle",   ("POST",)),
        ("hero_images_reorder",  "/admin/hero-images/<int:hid>/reorder",  ("POST",)),
        ("store_settings_view", "/settings",                             ("GET", "POST")),
        ("list_pix_accounts",   "/settings/pix-accounts",                ("GET",)),
        ("create_pix_account",     "/settings/pix-accounts/new",            ("GET", "POST")),
        ("toggle_pix_account",  "/settings/pix-accounts/<int:aid>/toggle", ("POST",)),
        ("delete_pix_account",  "/settings/pix-accounts/<int:aid>/delete", ("POST",)),
        ("admin_backup",        "/admin/backup",                         ("GET",)),
    ]

    for short, full in ALIAS_MAP.items():
        view = app.view_functions.get(full)
        if view is None:
            app.logger.warning(
                "Admin blueprint: endpoint %r ausente — alias %r não registrado.",
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

    app._admin_bp_initialized = True
