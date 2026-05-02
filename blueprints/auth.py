"""
blueprints/auth.py — Blueprint de autenticação.

Escopo:
    /login, /logout, /login/google, /auth/google/callback,
    /forgot-password, /reset-password/<token>, /account/password, /sudo

Padrão de extração (Onda 2):
    1. Endpoints PRESERVAM seus nomes globais. O blueprint é registrado
       SEM url_prefix e SEM name="auth" para que `url_for("login")`,
       `url_for("logout")` etc. continuem funcionando exatamente como
       antes em todos os templates. Esta foi escolha deliberada para
       não invalidar 1) histórico de URLs, 2) referências em base.html,
       3) testes existentes.
    2. Helpers (login_required, get_db, audit_log, etc.) ficam em app.py
       por ora. Importação é LAZY (dentro de cada view) para evitar
       circularidade durante boot do módulo.
    3. A instância OAuth é lida de current_app.extensions["oauth"]; o
       app expõe ela ali no create_app.

Migração futura (Onda 3):
    Helpers compartilhados → blueprints/_helpers/auth.py
    Eliminação total da dependência circular sobre app.py.
"""
from __future__ import annotations

import hmac
import secrets
from datetime import timedelta, datetime
from urllib.parse import urljoin

from flask import (
    Blueprint, current_app, flash, redirect, render_template,
    request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


auth_bp = Blueprint("auth", __name__, template_folder="../templates")


# ─────────────────────────────────────────────────────────────────────────
# Lazy helpers — evitam ciclo de import com app.py.
# Cada função importa o que precisa só na primeira execução.
# ─────────────────────────────────────────────────────────────────────────
def _app_helpers():
    """Retorna dict com helpers de app.py — chamada interna lazy."""
    from app import (
        login_required, validate_csrf_or_abort, ensure_csrf_token,
        get_current_user, audit_log, mark_sudo_fresh,
        bump_session_version, password_is_strong, login_is_locked,
        reset_login_failures, record_login_failure, client_ip,
        create_password_reset_token, smtp_is_configured, send_email,
        utc_now, utc_now_iso, compute_role_hmac,
    )
    from db import get_db
    # complete_login é nested em create_app() em app.py — não exportável.
    # Replicado localmente abaixo. Adicionado ao dict para que as views
    # continuem usando h["complete_login"](...) sem mudança.
    complete_login = _complete_login
    return locals()


def _complete_login(user: dict, provider: str = "password"):
    """Replicada VERBATIM de app.py:2570 (era nested em create_app, não
    exportável pelo nome). Bug exposto na primeira tentativa de login após
    a Onda 2/Etapa B (auth blueprint). Comportamento idêntico ao original."""
    import secrets
    from flask import session, current_app
    from app import (
        compute_role_hmac, utc_now_iso, audit_log,
    )
    from db import get_db

    session.clear()
    db = get_db()
    # Garante role_hmac atualizado (cobre upgrades de versão + edição
    # manual no BD: se HMAC não existir/bater, recalcula, mas registra).
    try:
        current_sv = int(user["session_version"]) if "session_version" in user.keys() and user["session_version"] is not None else 1
    except Exception:
        current_sv = 1
    expected_hmac = compute_role_hmac(user["id"], user["role"], current_sv)
    stored_hmac = user["role_hmac"] if "role_hmac" in user.keys() else None
    if stored_hmac and stored_hmac != expected_hmac:
        current_app.logger.error(
            "ALERT role_hmac mismatch on login user_id=%s role=%s",
            user["id"], user["role"],
        )
    if not stored_hmac or stored_hmac != expected_hmac:
        db.execute(
            "UPDATE users SET role_hmac = %s WHERE id = %s",
            (expected_hmac, user["id"]),
        )
    session["user_id"] = user["id"]
    session["user_email"] = user["email"]
    session["display_name"] = user["display_name"]
    session["user_sv"] = current_sv
    session["csrf_token"] = secrets.token_urlsafe(24)
    db.execute(
        "UPDATE users SET last_login_at = %s, updated_at = %s, last_auth_provider = %s WHERE id = %s",
        (utc_now_iso(), utc_now_iso(), provider, user["id"]),
    )
    db.commit()
    try:
        audit_log("login_success", target_id=user["id"], extra=f"provider={provider}")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────
# Login com Google (OAuth)
# ─────────────────────────────────────────────────────────────────────────
@auth_bp.route("/login/google", endpoint="login_google")
def login_google():
    cfg = current_app.config
    if not (cfg["GOOGLE_CLIENT_ID"] and cfg["GOOGLE_CLIENT_SECRET"]):
        flash("Login com Google não está configurado ainda.", "warning")
        return redirect(url_for("login"))
    redirect_uri = urljoin(
        cfg["PUBLIC_BASE_URL"].rstrip('/') + '/',
        url_for("auth_google_callback").lstrip('/'),
    )
    nonce = secrets.token_urlsafe(24)
    session["google_nonce"] = nonce
    oauth = current_app.extensions["oauth"]
    return oauth.google.authorize_redirect(
        redirect_uri=redirect_uri, nonce=nonce, prompt="select_account",
    )


@auth_bp.route("/auth/google/callback", endpoint="auth_google_callback")
def auth_google_callback():
    cfg = current_app.config
    if not (cfg["GOOGLE_CLIENT_ID"] and cfg["GOOGLE_CLIENT_SECRET"]):
        return redirect(url_for("login"))

    h = _app_helpers()
    oauth = current_app.extensions["oauth"]

    try:
        token = oauth.google.authorize_access_token()
        nonce = session.pop("google_nonce", None)
        userinfo = token.get("userinfo") or oauth.google.parse_id_token(token, nonce=nonce)
    except Exception:
        flash("Não deu para concluir o login com Google.", "danger")
        return redirect(url_for("login"))

    email = (userinfo.get("email") or "").strip().lower()
    google_sub = userinfo.get("sub") or ""
    if not email or not userinfo.get("email_verified"):
        flash("A conta Google precisa ter e-mail verificado.", "danger")
        return redirect(url_for("login"))

    db = h["get_db"]()
    user = db.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
    if not user or not int(user["is_active"]):
        flash("Esse e-mail Google ainda não está autorizado no painel.", "danger")
        return redirect(url_for("login"))
    if user["google_sub"] and user["google_sub"] != google_sub:
        flash("Esta conta Google não bate com a que já foi vinculada para esse usuário.", "danger")
        return redirect(url_for("login"))

    if google_sub and not user["google_sub"]:
        db.execute(
            "UPDATE users SET google_sub = %s, updated_at = %s WHERE id = %s",
            (google_sub, h["utc_now_iso"](), user["id"]),
        )
        db.commit()
        user = db.execute("SELECT * FROM users WHERE id = %s", (user["id"],)).fetchone()

    h["complete_login"](user, provider="google")
    flash(f"Oi, {user['display_name']}.", "success")
    return redirect(url_for("dashboard"))


# ─────────────────────────────────────────────────────────────────────────
# Login / Logout (senha)
# ─────────────────────────────────────────────────────────────────────────
@auth_bp.route("/login", methods=["GET", "POST"], endpoint="login")
def login():
    h = _app_helpers()

    if request.method == "POST":
        # CSRF gentil no /login: token inválido = redireciona com aviso,
        # não aborta com 400 (cookies stale, sessão perdida, worker novo).
        form_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not form_token or not session_token or not hmac.compare_digest(form_token, session_token):
            session["csrf_token"] = secrets.token_urlsafe(24)
            flash("Sessão expirou. Por favor, entre novamente.", "warning")
            return redirect(url_for("login"))

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        max_fail = int(current_app.config.get("MAX_FAILED_LOOKUPS", 5))
        lock_min = int(current_app.config.get("LOCK_MINUTES", 15))
        if h["login_is_locked"](email, max_fail, lock_min):
            current_app.logger.warning("login locked: email=%s ip=%s", email, h["client_ip"]())
            h["audit_log"]("login_locked", extra=f"email={email}")
            flash(f"Muitas tentativas. Tente novamente em {lock_min} minutos.", "danger")
            h["ensure_csrf_token"]()
            return render_template("login.html")

        db = h["get_db"]()
        user = db.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        if user and int(user["is_active"]) and check_password_hash(user["password_hash"], password):
            h["reset_login_failures"](email)
            h["complete_login"](user, provider="password")
            current_app.logger.info("login ok: email=%s ip=%s", email, h["client_ip"]())
            flash(f"Oi, {user['display_name']}.", "success")
            return redirect(url_for("dashboard"))
        h["record_login_failure"](email)
        current_app.logger.warning("login fail: email=%s ip=%s", email, h["client_ip"]())
        h["audit_log"]("login_failure", target_id=(user["id"] if user else None), extra=f"email={email}")
        flash("Login inválido. Confere e tenta de novo.", "danger")

    h["ensure_csrf_token"]()
    return render_template("login.html")


@auth_bp.route("/logout", methods=["POST"], endpoint="logout")
def logout():
    from app import login_required, validate_csrf_or_abort
    # login_required é aplicado via wrapper — ver bottom of file.
    # Aqui mantemos a validação de CSRF.
    validate_csrf_or_abort()
    session.clear()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("login"))


# ─────────────────────────────────────────────────────────────────────────
# Recuperação de senha
# ─────────────────────────────────────────────────────────────────────────
@auth_bp.route("/forgot-password", methods=["GET", "POST"], endpoint="forgot_password")
def forgot_password():
    h = _app_helpers()
    if request.method == "POST":
        h["validate_csrf_or_abort"]()
        email = request.form.get("email", "").strip().lower()
        db = h["get_db"]()
        user = db.execute(
            "SELECT * FROM users WHERE email = %s AND is_active = 1", (email,),
        ).fetchone()
        if user:
            token = h["create_password_reset_token"]()
            expires = (h["utc_now"]() + timedelta(hours=1)).isoformat()
            db.execute(
                "UPDATE users SET reset_token = %s, reset_expires_at = %s, updated_at = %s WHERE id = %s",
                (token, expires, h["utc_now_iso"](), user["id"]),
            )
            db.commit()
            if h["smtp_is_configured"](current_app):
                reset_url = f"{current_app.config['PUBLIC_BASE_URL'].rstrip('/')}{url_for('reset_password', token=token)}"
                subject = "Redefinição de senha · Tonton"
                html = (
                    f"<p>Oi, {user['display_name']}.</p>"
                    f"<p>Para criar uma nova senha, clique no link abaixo:</p>"
                    f"<p><a href='{reset_url}'>{reset_url}</a></p>"
                    f"<p>Esse link expira em 1 hora.</p>"
                )
                text = (
                    f"Oi, {user['display_name']}.\n\n"
                    f"Crie uma nova senha aqui: {reset_url}\n"
                    f"Esse link expira em 1 hora."
                )
                try:
                    h["send_email"](current_app, user["email"], user["display_name"], subject, html, text)
                except Exception:
                    pass
        flash("Se esse e-mail estiver cadastrado, você vai receber as instruções.", "info")
        return redirect(url_for("login"))
    h["ensure_csrf_token"]()
    return render_template("forgot_password.html")


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"], endpoint="reset_password")
def reset_password(token: str):
    h = _app_helpers()
    db = h["get_db"]()
    user = db.execute("SELECT * FROM users WHERE reset_token = %s", (token,)).fetchone()
    if (not user
            or not user["reset_expires_at"]
            or datetime.fromisoformat(user["reset_expires_at"]) < h["utc_now"]()):
        flash("Esse link não é mais válido.", "warning")
        return redirect(url_for("login"))
    if request.method == "POST":
        h["validate_csrf_or_abort"]()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")
        if password != password_confirm:
            flash("As senhas não batem.", "danger")
        elif not h["password_is_strong"](password):
            flash("Use pelo menos 10 caracteres, com letras e números.", "danger")
        else:
            db.execute(
                "UPDATE users SET password_hash = %s, reset_token = NULL, "
                "reset_expires_at = NULL, updated_at = %s WHERE id = %s",
                (generate_password_hash(password), h["utc_now_iso"](), user["id"]),
            )
            db.commit()
            flash("Senha atualizada. Agora é só entrar.", "success")
            return redirect(url_for("login"))
    h["ensure_csrf_token"]()
    return render_template("reset_password.html")


# ─────────────────────────────────────────────────────────────────────────
# Troca de senha (autenticado)
# ─────────────────────────────────────────────────────────────────────────
@auth_bp.route("/account/password", methods=["GET", "POST"], endpoint="change_password")
def change_password():
    h = _app_helpers()
    user = h["get_current_user"]()
    if request.method == "POST":
        h["validate_csrf_or_abort"]()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        db = h["get_db"]()
        full_user = db.execute("SELECT * FROM users WHERE id = %s", (user["id"],)).fetchone()
        if not check_password_hash(full_user["password_hash"], current_password):
            flash("A senha atual não confere.", "danger")
        elif new_password != confirm_password:
            flash("As novas senhas não batem.", "danger")
        elif not h["password_is_strong"](new_password):
            flash("Use pelo menos 10 caracteres, com letras e números.", "danger")
        else:
            db.execute(
                "UPDATE users SET password_hash = %s, updated_at = %s WHERE id = %s",
                (generate_password_hash(new_password), h["utc_now_iso"](), user["id"]),
            )
            h["bump_session_version"](db, user["id"])
            db.commit()
            session["user_sv"] = int(h["get_current_user"]()["session_version"])
            h["audit_log"]("password_changed", target_id=user["id"])
            flash("Senha alterada. Outras sessões foram encerradas.", "success")
            return redirect(url_for("dashboard"))
    h["ensure_csrf_token"]()
    return render_template("change_password.html")


# ─────────────────────────────────────────────────────────────────────────
# Sudo (re-auth para ações sensíveis)
# ─────────────────────────────────────────────────────────────────────────
@auth_bp.route("/sudo", methods=["GET", "POST"], endpoint="sudo")
def sudo():
    h = _app_helpers()
    next_url = request.args.get("next") or request.form.get("next") or url_for("dashboard")
    # whitelist: só caminhos internos
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("dashboard")
    if request.method == "POST":
        h["validate_csrf_or_abort"]()
        password = request.form.get("password", "")
        user = h["get_current_user"]()
        db = h["get_db"]()
        full_user = db.execute(
            "SELECT password_hash FROM users WHERE id = %s", (user["id"],),
        ).fetchone()
        if full_user and check_password_hash(full_user["password_hash"], password):
            h["mark_sudo_fresh"]()
            h["audit_log"]("sudo_granted", target_id=user["id"])
            return redirect(next_url)
        h["audit_log"]("sudo_denied", target_id=user["id"])
        flash("Senha incorreta.", "danger")
    h["ensure_csrf_token"]()
    return render_template("sudo.html", next_url=next_url)


# ─────────────────────────────────────────────────────────────────────────
# Aplicação dos decorators @login_required em views autenticadas + aliases.
# Feito após o registro do blueprint via init_auth_blueprint(app).
# ─────────────────────────────────────────────────────────────────────────
def init_auth_blueprint(app):
    """
    Hook chamado por app.py após `app.register_blueprint(auth_bp)`.

    Faz duas coisas:
    1) Registra aliases sem prefixo (login, logout, sudo, ...) → templates
       que usam url_for('login') continuam funcionando sem alteração.
    2) Aplica @login_required nas views que precisam (logout, sudo,
       change_password). Não pode ser decorator nativo no blueprint
       porque login_required vive em app.py — circular.

    Idempotente.
    """
    if getattr(app, "_auth_bp_initialized", False):
        return

    from app import login_required as _login_required

    # Mapeamento endpoint global → endpoint do blueprint.
    # CADA TEMPLATE que chama url_for('login'), url_for('logout'), etc.
    # depende destes aliases. Não remover sem grep total nos templates.
    ALIAS_MAP = {
        "login": "auth.login",
        "logout": "auth.logout",
        "login_google": "auth.login_google",
        "auth_google_callback": "auth.auth_google_callback",
        "forgot_password": "auth.forgot_password",
        "reset_password": "auth.reset_password",
        "change_password": "auth.change_password",
        "sudo": "auth.sudo",
    }

    # Endpoints que exigem login_required.
    PROTECTED = {"logout", "change_password", "sudo"}

    for short, full in ALIAS_MAP.items():
        view = app.view_functions.get(full)
        if view is None:
            current_app_logger = app.logger
            current_app_logger.warning(
                "Auth blueprint: endpoint %r não encontrado — alias %r não registrado.",
                full, short,
            )
            continue
        # Aplica login_required em rotas protegidas (substituindo a view).
        if short in PROTECTED:
            view = _login_required(view)
            app.view_functions[full] = view  # também atualiza o nome com prefixo
        # Cria alias sem prefixo.
        app.view_functions[short] = view

    # Registra também as URL rules sob o nome curto, para que url_for(short)
    # consiga construir a URL. Usa add_url_rule com o mesmo path/methods.
    _ROUTE_DEFS = [
        ("login",                  "/login",                       ("GET", "POST")),
        ("logout",                 "/logout",                      ("POST",)),
        ("login_google",           "/login/google",                ("GET",)),
        ("auth_google_callback",   "/auth/google/callback",        ("GET",)),
        ("forgot_password",        "/forgot-password",             ("GET", "POST")),
        ("reset_password",         "/reset-password/<token>",      ("GET", "POST")),
        ("change_password",        "/account/password",            ("GET", "POST")),
        ("sudo",                   "/sudo",                        ("GET", "POST")),
    ]
    existing_endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    for endpoint, rule, methods in _ROUTE_DEFS:
        if endpoint in existing_endpoints:
            continue  # já existe (idempotência)
        view = app.view_functions.get(endpoint)
        if view is None:
            continue
        app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=list(methods))

    app._auth_bp_initialized = True
