import base64
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import requests
import secrets
import smtplib
import textwrap
import unicodedata
from urllib.parse import urljoin
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extensions
import psycopg2.extras
from authlib.integrations.flask_client import OAuth
from cryptography.fernet import Fernet, InvalidToken
from PIL import Image, ImageDraw, ImageFont, ImageOps

# Registra suporte a HEIC/HEIF (formato padrão do iPhone). Se a lib não estiver
# instalada (ambiente local antigo), o app continua funcionando para JPEG/PNG/WebP.
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HEIF_SUPPORT = True
except ImportError:
    _HEIF_SUPPORT = False

from flask import (
    Flask,
    current_app,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
    Response,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

# ---------- DB (Postgres-only via psycopg2) ----------
from db import (
    DBConnection,
    IntegrityError,
    close_db,
    get_db,
    insert_returning_id,
    transaction,
)

BASE_DIR = Path(__file__).resolve().parent
UTC = timezone.utc
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
INSTAGRAM_URL = "https://www.instagram.com/tontonlojainfantil"
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
CARD_TEMPLATES_DIR = BASE_DIR / "static" / "card_templates"


# ─── Catálogo: mapa de cores (nome → hex) ─────────────────────────────────────
# Substitui o uso inseguro de `background: {{ color|lower }}` no template
# (CSS injection + cores erradas para nomes pt-BR como "marsala", "bordô").
# Cores em masculino E feminino — produtos podem cadastrar de ambos os jeitos.
CATALOG_COLOR_MAP: dict[str, str] = {
    # neutros
    "preto": "#1a0f0a", "preta": "#1a0f0a",
    "branco": "#fafaf7", "branca": "#fafaf7",
    "off-white": "#f1ece2", "off white": "#f1ece2",
    "cru": "#ede4d3", "areia": "#dccbb1",
    "bege": "#d8c4a8", "nude": "#e8d3c0",
    "caramelo": "#b87a4e", "camel": "#b87a4e",
    "marrom": "#5a3a26", "chocolate": "#3e2418",
    "cinza": "#8a8682", "cinza claro": "#c9c5bf",
    "cinza chumbo": "#46443f", "grafite": "#2e2c29",

    # vermelhos / rosas
    "vermelho": "#a8281f", "vermelha": "#a8281f",
    "bordô": "#5a1a22", "bordo": "#5a1a22",
    "marsala": "#7a2c2a", "vinho": "#4a1820",
    "rosa": "#e7a3b6", "rosa claro": "#f4d4dc",
    "pink": "#d6336c", "magenta": "#a81e50",
    "coral": "#ff6b3a",
    "salmão": "#ec9b7a", "salmao": "#ec9b7a",

    # quentes
    "laranja": "#e8501a", "ferrugem": "#a85428",
    "mostarda": "#c89642", "amarelo": "#e8c84a",
    "amarela": "#e8c84a", "ocre": "#b08a3c",

    # frios
    "azul": "#2c4a6e",
    "azul marinho": "#1a2840", "marinho": "#1a2840",
    "azul claro": "#a8c2d8",
    "azul céu": "#86a8c8", "azul ceu": "#86a8c8",
    "verde": "#3a5a3e",
    "verde militar": "#4a5238",
    "verde oliva": "#6a6a3a", "oliva": "#6a6a3a",
    "verde água": "#a8c8c0", "verde agua": "#a8c8c0",
    "menta": "#b8d4c4", "azulado": "#5e7e96",
    "lilás": "#b89cc8", "lilas": "#b89cc8",
    "lavanda": "#c4b8d8",
    "violeta": "#5a3a6a", "roxo": "#4a2848", "roxa": "#4a2848",

    # padrões / metálicos
    "estampado": "#cdbfa5", "estampada": "#cdbfa5",
    "listrado": "#cdbfa5", "listrada": "#cdbfa5",
    "xadrez": "#a89478", "floral": "#e8b8c0",
    "animal print": "#a8845a",
    "onça": "#a8845a", "onca": "#a8845a",
    "dourado": "#b89846", "dourada": "#b89846",
    "prateado": "#b8b8b8", "prateada": "#b8b8b8",
    "prata": "#b8b8b8",

    # fallback
    "padrão": "#d8ccc2", "padrao": "#d8ccc2",
    "default": "#d8ccc2",
}


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


# ============================================================
# Onda 1 · Ficha técnica de roupa
# ============================================================
FIBER_ENUM = (
    "algodão", "poliéster", "elastano", "viscose", "linho",
    "lã", "poliamida", "modal", "acrílico", "seda", "tencel", "cupro",
)
CARE_WASH_ENUM = ("wash_30", "wash_40", "wash_60", "hand_wash", "do_not_wash")
FIT_ENUM       = ("slim", "regular", "oversized", "plus", "ajustada", "solta")
LENGTH_ENUM    = ("curto", "medio", "longo", "midi", "mini", "maxi")
FABRIC_ENUM    = ("malha", "plano", "jeans", "moletom", "tricot", "renda", "couro", "alfaiataria")

CARE_WASH_LABELS = {
    "wash_30":     "Lavar até 30 °C",
    "wash_40":     "Lavar até 40 °C",
    "wash_60":     "Lavar até 60 °C",
    "hand_wash":   "Lavar à mão",
    "do_not_wash": "Não lavar",
}


def _parse_composition(form) -> str | None:
    """Lê comp_fiber[]/comp_pct[], valida soma=100% e devolve JSON ou None."""
    fibers = form.getlist("comp_fiber[]")
    pcts   = form.getlist("comp_pct[]")
    if not fibers:
        return None
    rows: list[dict] = []
    seen: set[str] = set()
    for fiber, pct in zip(fibers, pcts):
        f = (fiber or "").strip().lower()
        if not f or f not in FIBER_ENUM:
            continue
        try:
            p = int(pct)
        except (TypeError, ValueError):
            raise ValueError(f"Percentual inválido para «{f}».")
        if p < 1 or p > 100:
            raise ValueError(f"Percentual de «{f}» fora do intervalo 1–100.")
        if f in seen:
            raise ValueError(f"Fibra «{f}» duplicada.")
        seen.add(f)
        rows.append({"fibra": f, "percentual": p})
    if not rows:
        return None
    total = sum(r["percentual"] for r in rows)
    if total != 100:
        raise ValueError(f"Soma da composição é {total}%, deve ser exatamente 100%.")
    return json.dumps(rows, ensure_ascii=False)


def _clean_enum(value: str | None, allowed: tuple) -> str | None:
    v = (value or "").strip().lower()
    return v if v in allowed else None


def _clean_int_optional(value: str | None, lo: int = 1, hi: int = 2000) -> int | None:
    try:
        n = int(value or "")
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _read_clothing_fields(form):
    """Bloco reutilizável: lê e valida os 7 campos da ficha técnica."""
    composition       = _parse_composition(form)  # pode levantar ValueError
    care_wash         = _clean_enum(form.get("care_wash"),    CARE_WASH_ENUM)
    fabric_type       = _clean_enum(form.get("fabric_type"),  FABRIC_ENUM)
    fit               = _clean_enum(form.get("fit"),          FIT_ENUM)
    length_class      = _clean_enum(form.get("length_class"), LENGTH_ENUM)
    fabric_weight_gsm = _clean_int_optional(form.get("fabric_weight_gsm"), 30, 1500)
    country_of_origin = _clean_optional(form.get("country_of_origin")) or "BR"
    return (composition, care_wash, fabric_type, fabric_weight_gsm,
            fit, length_class, country_of_origin)


def _composition_pretty(value) -> str:
    """JSONB→'95% poliéster · 5% elastano'. Aceita None, str ou list."""
    if not value:
        return ""
    try:
        data = json.loads(value) if isinstance(value, str) else value
        if not isinstance(data, list):
            return ""
        return " · ".join(f"{r['percentual']}% {r['fibra']}" for r in data)
    except Exception:
        return ""


def _is_clothing_complete(p) -> bool:
    """True se a ficha técnica tem o mínimo (composição) preenchido."""
    try:
        return bool(p["composition"])
    except (KeyError, TypeError, IndexError):
        return False


# ---------- Generic helpers ----------
def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def is_promo_active(promo_until) -> bool:
    """True se promoção ainda está vigente (ou não tem data de término)."""
    if not promo_until:
        return True
    try:
        # Aceita 'YYYY-MM-DD' ou ISO completo
        s = str(promo_until).strip()
        if len(s) == 10:
            end = datetime.fromisoformat(s + "T23:59:59+00:00")
        else:
            end = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=UTC)
        return end >= utc_now()
    except Exception:
        return False


def effective_price(row) -> str:
    """Retorna preço efetivo: promo_price se ativa, senão sale_price.
    Aceita dict-row ou Mapping. Sempre retorna string para format_money."""
    if row is None:
        return "0"
    try:
        promo_price = row["promo_price"] if "promo_price" in row.keys() else None
    except Exception:
        promo_price = row.get("promo_price") if hasattr(row, "get") else None
    try:
        promo_until = row["promo_until"] if "promo_until" in row.keys() else None
    except Exception:
        promo_until = row.get("promo_until") if hasattr(row, "get") else None
    try:
        sale_price = row["sale_price"]
    except Exception:
        sale_price = row.get("sale_price", "0") if hasattr(row, "get") else "0"

    if promo_price and str(promo_price).strip() and is_promo_active(promo_until):
        try:
            if Decimal(str(promo_price)) > 0:
                return str(promo_price)
        except Exception:
            pass
    return str(sale_price or "0")


# Ordenação semântica de tamanhos: PP < P < M < G < GG < XG
# Numéricos (ex: 36, 38, 40) ordenam pelo valor.
# Desconhecidos vão para o final, alfabéticos.
_SIZE_ORDER = {
    "PP": 10, "P": 20, "M": 30, "G": 40, "GG": 50,
    "XG": 60, "XGG": 70, "EXG": 80,
    "U": 5, "UNICO": 5, "ÚNICO": 5,
}

def size_sort_key(size: str):
    """Chave de ordenação: (categoria, valor_numerico, fallback_alfabetico).
    categoria: 0=conhecido textual, 1=numérico, 2=desconhecido."""
    s = (size or "").strip().upper()
    if not s:
        return (3, 0, "")
    if s in _SIZE_ORDER:
        return (0, _SIZE_ORDER[s], s)
    try:
        return (1, float(s.replace(",", ".")), s)
    except ValueError:
        return (2, 0, s)


def has_active_promo(row) -> bool:
    if row is None:
        return False
    try:
        promo_price = row["promo_price"] if "promo_price" in row.keys() else None
        promo_until = row["promo_until"] if "promo_until" in row.keys() else None
    except Exception:
        promo_price = getattr(row, "get", lambda *_: None)("promo_price")
        promo_until = getattr(row, "get", lambda *_: None)("promo_until")
    if not promo_price or not str(promo_price).strip():
        return False
    try:
        if Decimal(str(promo_price)) <= 0:
            return False
    except Exception:
        return False
    return is_promo_active(promo_until)


def format_money(value) -> str:
    try:
        if value is None:
            amount = Decimal("0")
        elif isinstance(value, Decimal):
            amount = value
        else:
            # Strip whitespace, handle empty string, handle already-formatted strings
            s = str(value).strip()
            if not s or s in ("-", "—", "N/A"):
                amount = Decimal("0")
            else:
                # Remove thousand separators (dots used in pt-BR) if present
                # e.g. "1.234,56" → "1234.56"
                if "," in s and "." in s:
                    s = s.replace(".", "").replace(",", ".")
                elif "," in s:
                    s = s.replace(",", ".")
                amount = Decimal(s)
    except Exception:
        amount = Decimal("0")
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_datetime_local(raw_value: str | None) -> str:
    if not raw_value:
        return "-"
    try:
        dt = datetime.fromisoformat(raw_value).astimezone(LOCAL_TZ)
        return dt.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return raw_value


def format_date_local(raw_value: str | None) -> str:
    if not raw_value:
        return "Sem validade"
    try:
        dt = datetime.fromisoformat(raw_value).astimezone(LOCAL_TZ)
        return dt.strftime("%d/%m/%Y")
    except ValueError:
        return raw_value


def parse_money(raw_value: str):
    if raw_value is None:
        return None
    sanitized = raw_value.strip().replace("R$", "").replace(" ", "")
    sanitized = sanitized.replace(".", "").replace(",", ".")
    try:
        return Decimal(sanitized).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def month_bounds_local(dt: datetime | None = None) -> tuple[str, str]:
    base = dt.astimezone(LOCAL_TZ) if dt and dt.tzinfo else (dt or datetime.now(LOCAL_TZ))
    start = base.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def month_bounds_utc(dt: datetime | None = None) -> tuple[str, str]:
    base = dt.astimezone(LOCAL_TZ) if dt and dt.tzinfo else (dt or datetime.now(LOCAL_TZ))
    start_local = base.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_local = (start_local.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start_local.astimezone(UTC).isoformat(), end_local.astimezone(UTC).isoformat()


def validate_coupon_row(coupon, subtotal: Decimal, now_local: datetime | None = None):
    if not coupon:
        return False, "Cupom inválido", Decimal("0")
    now_local = now_local or datetime.now(LOCAL_TZ)
    if not coupon["is_active"]:
        return False, "Cupom inativo", Decimal("0")
    if coupon["expires_at"] and coupon["expires_at"] < now_local.strftime("%Y-%m-%d"):
        return False, "Cupom expirado", Decimal("0")
    if coupon["max_uses"] and coupon["used_count"] >= coupon["max_uses"]:
        return False, "Limite de usos atingido", Decimal("0")
    min_purchase = Decimal(str(coupon["min_purchase"] or 0))
    if subtotal < min_purchase:
        return False, f"Compra mínima: R$ {format_money(min_purchase)}", Decimal("0")
    if coupon["type"] == "percent":
        discount = (subtotal * Decimal(str(coupon["value"] or 0)) / Decimal("100")).quantize(Decimal("0.01"))
        message = f"{coupon['value']}% de desconto"
    else:
        discount = Decimal(str(coupon["value"] or 0)).quantize(Decimal("0.01"))
        message = f"R$ {format_money(coupon['value'])} de desconto"
    discount = min(discount, subtotal)
    return True, message, discount


def parse_expiry(raw_value: str):
    if not raw_value:
        return None
    try:
        dt = datetime.strptime(raw_value, "%Y-%m-%d")
        local_dt = dt.replace(hour=23, minute=59, second=59, tzinfo=LOCAL_TZ)
        return local_dt.astimezone(UTC).isoformat()
    except ValueError:
        return None


def normalize_code(raw: str) -> str:
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def normalize_text(raw: str) -> str:
    return "".join(ch for ch in raw.upper().strip() if ch.isalnum())


def normalize_phone(raw: str) -> str:
    digits = ''.join(ch for ch in (raw or '') if ch.isdigit())
    if not digits:
        return ''
    if digits.startswith('00'):
        digits = digits[2:]
    if digits.startswith('55'):
        return digits
    if len(digits) in {10, 11}:
        return '55' + digits
    return digits


def slugify(raw: str, max_length: int = 80) -> str:
    """Gera slug URL-friendly em kebab-case a partir de texto livre.

    Exemplos:
      "Cinto couro Lore"       → "cinto-couro-lore"
      "Vestido Midi Açaí"      → "vestido-midi-acai"
      "Calça BMQ Camila — P"   → "calca-bmq-camila-p"
      ""                        → "produto"

    Mantém só ASCII letras, números e hífens. Hífens duplicados são reduzidos
    a um único e hífens nas pontas são removidos. Se o resultado ficar vazio
    (entrada com só símbolos), retorna 'produto' como fallback seguro."""
    if not raw:
        return "produto"
    # 1. Remove acentos preservando o caractere base
    text = unicodedata.normalize("NFKD", str(raw))
    text = text.encode("ascii", "ignore").decode("ascii")
    # 2. Lowercase
    text = text.lower()
    # 3. Substitui qualquer não-alfanumérico por hífen
    text = re.sub(r"[^a-z0-9]+", "-", text)
    # 4. Remove hífens das pontas
    text = text.strip("-")
    # 5. Trunca preservando palavras inteiras quando possível
    if len(text) > max_length:
        text = text[:max_length].rstrip("-")
        # Se truncou no meio de uma palavra longa, recua até o último hífen
        if "-" in text and len(text) == max_length:
            text = text.rsplit("-", 1)[0]
    return text or "produto"


def format_phone_display(raw: str | None) -> str:
    digits = normalize_phone(raw or '')
    if not digits:
        return '-'
    if digits.startswith('55') and len(digits) in {12, 13}:
        country = digits[:2]
        area = digits[2:4]
        if len(digits) == 13:
            prefix = digits[4:9]
            suffix = digits[9:]
        else:
            prefix = digits[4:8]
            suffix = digits[8:]
        return f'+{country} ({area}) {prefix}-{suffix}'
    return '+' + digits


def build_whatsapp_url(phone: str | None, card: dict | None = None, public_base_url: str | None = None, visible_code: str | None = None) -> str | None:
    digits = normalize_phone(phone or '')
    if not digits:
        return None
    recipient = (card['recipient_name'] if card else '') or 'você'
    value = format_money(card['current_balance']) if card else ''
    message = f"Oi, {recipient}. Seu vale-presente Tonton está pronto."
    if card:
        message += f"\nValor: R$ {value}"
        if visible_code:
            message += f"\nCódigo: {visible_code}"
        message += f"\n\nPara usar o vale, apresente o código na loja."
    return f"https://wa.me/{digits}?text={quote_plus(message)}"

def build_tel_url(phone: str | None) -> str | None:
    digits = normalize_phone(phone or '')
    return f"tel:+{digits}" if digits else None


def _derive_fernet_key(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode('utf-8')).digest()
    return base64.urlsafe_b64encode(digest)


def get_code_cipher(app: Flask) -> Fernet:
    source = app.config.get('CODE_ENCRYPTION_KEY') or app.config.get('CODE_PEPPER') or app.config['SECRET_KEY']
    return Fernet(_derive_fernet_key(source))


def encrypt_visible_code(app: Flask, visible_code: str) -> str:
    return get_code_cipher(app).encrypt(visible_code.encode('utf-8')).decode('utf-8')


def decrypt_visible_code(app: Flask, encrypted_code: str | None) -> str | None:
    if not encrypted_code:
        return None
    try:
        return get_code_cipher(app).decrypt(encrypted_code.encode('utf-8')).decode('utf-8')
    except (InvalidToken, ValueError):
        return None


# ---------- Cipher dedicado para credenciais PSP (multi-PSP) ----------
# Rotacao independente: trocar PSP_CREDENTIALS_KEY nao afeta gift cards
# nem PII. Forca o admin a ter chaves separadas em producao.
def _get_psp_cipher() -> Fernet:
    source = (
        os.environ.get("PSP_CREDENTIALS_KEY", "").strip()
        or current_app.config.get("CODE_PEPPER")
        or current_app.config.get("SECRET_KEY")
    )
    return Fernet(_derive_fernet_key(source))


def psp_encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_psp_cipher().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def psp_decrypt(ciphertext: str | None) -> str:
    if not ciphertext:
        return ""
    try:
        return _get_psp_cipher().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


# ---------- Fonts / imaging ----------
def _first_existing_font(candidates: list[Path], size: int):
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return None


def get_ui_font(size: int, bold: bool = False):
    preferred = [
        BASE_DIR / "static" / "fonts" / ("AcherusGrotesque-Bold.otf" if bold else "AcherusGrotesque-Regular.otf"),
        BASE_DIR / "static" / "fonts" / ("AcherusGrotesque-Bold.ttf" if bold else "AcherusGrotesque-Regular.ttf"),
        BASE_DIR / "static" / "fonts" / ("AcherusGrotesque-Bold.OTF" if bold else "AcherusGrotesque-Regular.OTF"),
        BASE_DIR / "static" / "fonts" / ("AcherusGrotesque-Bold.TTF" if bold else "AcherusGrotesque-Regular.TTF"),
        Path("/usr/share/fonts/opentype/inter/Inter-Bold.otf" if bold else "/usr/share/fonts/opentype/inter/Inter-Regular.otf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    return _first_existing_font(preferred, size) or ImageFont.load_default()


def get_card_value_font(size: int):
    preferred = [
        BASE_DIR / "static" / "fonts" / "Pagella-Bold.otf",
        BASE_DIR / "static" / "fonts" / "AcherusGrotesque-Bold.otf",
        Path("/usr/share/texmf/fonts/opentype/public/tex-gyre/texgyrepagella-bold.otf"),
        BASE_DIR / "static" / "fonts" / "DejaVuSerif-Bold.ttf",
        BASE_DIR / "static" / "fonts" / "DejaVuSans-Bold.ttf",
    ]
    return _first_existing_font(preferred, size) or ImageFont.load_default()


def get_card_name_font(size: int):
    preferred = [
        BASE_DIR / "static" / "fonts" / "Lora-Italic.ttf",
        BASE_DIR / "static" / "fonts" / "Lora-Bold.ttf",
        Path("/usr/share/fonts/truetype/google-fonts/Lora-Italic-Variable.ttf"),
        BASE_DIR / "static" / "fonts" / "DejaVuSerif-Bold.ttf",
        BASE_DIR / "static" / "fonts" / "DejaVuSans.ttf",
    ]
    return _first_existing_font(preferred, size) or ImageFont.load_default()


def fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int, min_size: int = 16, font_loader=None):
    size = start_size
    loader = font_loader or (lambda s: get_ui_font(size=s, bold=False))
    while size >= min_size:
        font = loader(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font
        size -= 2
    return loader(min_size)


def format_card_value(value) -> str:
    try:
        return f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "0,00"


def sanitize_filename_part(value: str | None, fallback: str = "vale") -> str:
    import re
    raw = (value or fallback).strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw, flags=re.IGNORECASE)
    raw = raw.strip("-")
    return raw or fallback


def get_card_code_font(size: int):
    preferred = [
        BASE_DIR / "static" / "fonts" / "Poppins-Light.ttf",
        BASE_DIR / "static" / "fonts" / "Poppins-Medium.ttf",
        BASE_DIR / "static" / "fonts" / "AcherusGrotesque-Regular.otf",
        BASE_DIR / "static" / "fonts" / "AcherusGrotesque-Regular.ttf",
        Path("/usr/share/fonts/truetype/google-fonts/Poppins-Light.ttf"),
        BASE_DIR / "static" / "fonts" / "DejaVuSans.ttf",
    ]
    return _first_existing_font(preferred, size) or ImageFont.load_default()


def get_card_palette(template_name: str | None) -> dict[str, tuple[int, int, int]]:
    template = (template_name or "").lower()
    if "base_500" in template or "male-2" in template:
        # Pink/hot-pink template — deep plum number, white texts
        return {
            "value": (90, 20, 108),
            "name": (247, 241, 238),
            "code": (247, 241, 238),
        }
    if "base_250" in template or "male-1" in template:
        # Purple template — deep plum number, white texts
        return {
            "value": (86, 24, 103),
            "name": (247, 241, 238),
            "code": (247, 241, 238),
        }
    return {
        "value": (86, 24, 103),
        "name": (247, 241, 238),
        "code": (247, 241, 238),
    }

def available_template_paths() -> list[Path]:
    if not CARD_TEMPLATES_DIR.exists():
        return []
    allowed = {".png", ".jpg", ".jpeg", ".webp"}
    # Only use templates that have a barcode in the white strip (x=100..173, varying dark rows).
    # Templates with a plain white strip (base_250.png) are excluded.
    BARCODE_TEMPLATES = {"base_500.png", "vale-presente-male-1.png", "vale-presente-male-2.png"}
    paths = []
    for p in CARD_TEMPLATES_DIR.iterdir():
        if p.suffix.lower() in allowed and p.name in BARCODE_TEMPLATES:
            paths.append(p)
    # Fallback: if none found (e.g. custom template set), use all
    if not paths:
        for p in CARD_TEMPLATES_DIR.iterdir():
            if p.suffix.lower() in allowed:
                paths.append(p)
    return sorted(paths)


def select_template_path(template_name: str | None = None) -> Path:
    templates = available_template_paths()
    if not templates:
        return CARD_TEMPLATES_DIR / "base_250.png"
    if template_name:
        explicit = CARD_TEMPLATES_DIR / template_name
        if explicit.exists():
            return explicit
    return random.choice(templates)


def generate_card_image(card: dict) -> io.BytesIO:
    # Canvas: 1064 x 591 px — pixel-analyzed layout (base_500 / vale-presente templates):
    #   White strip + barcode:   x=0..253
    #   R$ glyph on template:    x=335..408, y=247..339  height=92px  center_y=293
    #   Value number:            baseline-aligned with R$ bottom (y=339), left=425
    #   Name + code zone:        x=335..888, y=355..488
    #   @tontonlojainfantil (on template): y=496..524  ← hard stop at y=488
    #   Logo circle (template):  x=895..1064, y=429..591

    template_path = select_template_path(card["template_name"])
    base = Image.open(template_path).convert("RGBA")
    W, H = base.size
    draw = ImageDraw.Draw(base)

    palette = get_card_palette(card["template_name"])

    # ── Pixel-accurate layout constants (per template) ─────────────
    # Measured from actual template images:
    #   base_250 / base_500 / vale-1: R$ right_x=407, rs_top=248, rs_bot=338
    #   vale-presente-male-2:         R$ right_x=470, rs_top=250, rs_bot=344
    _tname = (card["template_name"] or "").lower()
    if "male-2" in _tname:
        RS_TOP       = 250
        RS_BOTTOM    = 344
        VALUE_LEFT   = 478   # 470 (R$ right edge) + 8px gap
        VALUE_RIGHT  = 888   # logo circle at x~895
    else:
        RS_TOP       = 248
        RS_BOTTOM    = 338
        VALUE_LEFT   = 415   # 407 (R$ right edge) + 8px gap
        VALUE_RIGHT  = 1030  # clear space to right

    RS_HEIGHT    = RS_BOTTOM - RS_TOP
    VALUE_MAX_W  = VALUE_RIGHT - VALUE_LEFT
    VALUE_TARGET_H = RS_HEIGHT   # match digit height to R$ height

    BOTTOM_LEFT  = 335
    BOTTOM_RIGHT = 888
    BOTTOM_W     = BOTTOM_RIGHT - BOTTOM_LEFT  # 553 px
    SAFE_BOTTOM  = 488   # @tontonlojainfantil starts at y=496

    # ── Value number — aligned to R$ ─────────────────────────────
    value_text = format_card_value(card["initial_value"])

    # Find the font size whose cap-height (measured on "0") matches RS_HEIGHT.
    # Then anchor: draw_y = RS_TOP - cap_top_offset
    # This aligns the top of digits exactly with the top of the R$ glyph.
    def find_aligned_font(target_cap_h: int, loader):
        for sz in range(target_cap_h + 50, 30, -2):
            f = loader(sz)
            bb = draw.textbbox((0, 0), "0", font=f)
            cap_h  = bb[3] - bb[1]   # rendered height of "0"
            cap_top = bb[1]           # top bearing (pixels below draw origin)
            if cap_h <= target_cap_h:
                return f, cap_h, cap_top
        f = loader(32)
        bb = draw.textbbox((0, 0), "0", font=f)
        return f, bb[3]-bb[1], bb[1]

    value_font, cap_h, cap_top = find_aligned_font(VALUE_TARGET_H, get_card_value_font)

    # Ensure full value string fits horizontally; shrink if needed
    vb = draw.textbbox((0, 0), value_text, font=value_font)
    if (vb[2] - vb[0]) > VALUE_MAX_W:
        value_font = fit_text(draw, value_text, VALUE_MAX_W, value_font.size, 36, get_card_value_font)
        vb = draw.textbbox((0, 0), value_text, font=value_font)
        bb2 = draw.textbbox((0, 0), "0", font=value_font)
        cap_h, cap_top = bb2[3]-bb2[1], bb2[1]

    # Align cap-top to RS_TOP: draw_y + cap_top = RS_TOP  →  draw_y = RS_TOP - cap_top
    value_y = RS_TOP - cap_top
    value_bottom = value_y + (vb[3] - vb[1])
    draw.text((VALUE_LEFT, value_y), value_text, font=value_font, fill=palette["value"])

    # ── Name + code zone ─────────────────────────────────────────
    cursor_y = max(355, value_bottom + 16)

    recipient_text = (card["recipient_name"] or "").strip()
    visible_code   = decrypt_visible_code(current_app, card["encrypted_code"]) or ""

    CODE_H_RESERVE = 28

    if recipient_text:
        name_max_h = SAFE_BOTTOM - cursor_y - (CODE_H_RESERVE + 5 if visible_code else 0)
        name_start = min(40, max(16, name_max_h))
        name_font = fit_text(draw, recipient_text, BOTTOM_W, name_start, 14, get_card_name_font)
        nb = draw.textbbox((0, 0), recipient_text, font=name_font)
        name_h = nb[3] - nb[1]
        if cursor_y + name_h <= SAFE_BOTTOM - (CODE_H_RESERVE if visible_code else 0):
            draw.text((BOTTOM_LEFT, cursor_y), recipient_text, font=name_font, fill=palette["name"])
            cursor_y += name_h + 5

    if visible_code:
        available_h = SAFE_BOTTOM - cursor_y
        code_start = min(24, max(12, available_h - 4))
        code_font = fit_text(draw, visible_code, BOTTOM_W, code_start, 11, get_card_code_font)
        cb = draw.textbbox((0, 0), visible_code, font=code_font)
        if cursor_y + (cb[3] - cb[1]) <= SAFE_BOTTOM:
            draw.text((BOTTOM_LEFT, cursor_y), visible_code, font=code_font, fill=palette["code"])

    output = io.BytesIO()
    base.save(output, format="PNG")
    output.seek(0)
    return output


# ---------- Auth / users ----------
def ensure_csrf_token() -> None:
    session.setdefault("csrf_token", secrets.token_urlsafe(24))


def validate_csrf_or_abort() -> None:
    form_token = request.form.get("csrf_token", "")
    session_token = session.get("csrf_token", "")
    if not form_token or not session_token or not hmac.compare_digest(form_token, session_token):
        abort(400, description="Falha de validação CSRF")


def is_authenticated() -> bool:
    return bool(session.get("user_id"))


def get_current_user() -> dict | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    row = get_db().execute(
        "SELECT id, email, display_name, role, is_active, "
        "COALESCE(session_version,1) AS session_version, role_hmac, "
        "COALESCE(mfa_enabled,0) AS mfa_enabled "
        "FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    return row


def _session_is_valid(user: dict) -> bool:
    """Defesa em profundidade contra adulteracao do BD ou sessao stale.

    1. session_version: se incrementada server-side (ex: admin trocou role
       ou desativou), todas as sessoes vivas do user sao invalidadas.
    2. role_hmac: se alguem editou `role` direto no BD sem o pepper, o HMAC
       nao bate -> sessao caida + alerta no audit log.

    Tolerancia a sessoes legacy: usuarios que logaram ANTES do upgrade
    v6 nao tem `user_sv` na sessao. Em vez de derrubar, migra a sessao
    silenciosamente (grava user_sv = db_ver atual). Caso contrario, o
    hardening derrubaria todo mundo no proximo deploy.
    """
    db_ver = int(user.get("session_version") or 1)
    sess_raw = session.get("user_sv")
    if sess_raw is None:
        # Sessao legacy: migra para o esquema novo sem invalidar.
        session["user_sv"] = db_ver
    else:
        try:
            if int(sess_raw) != db_ver:
                return False
        except (TypeError, ValueError):
            session["user_sv"] = db_ver

    stored_hmac = user.get("role_hmac") or ""
    if stored_hmac:
        expected = compute_role_hmac(user["id"], user["role"], db_ver)
        if not hmac.compare_digest(stored_hmac, expected):
            try:
                audit_log(
                    "role_hmac_mismatch",
                    target_id=user["id"],
                    extra=f"role={user['role']} sv={db_ver}",
                )
                current_app.logger.error(
                    "ALERT role_hmac mismatch user_id=%s role=%s ip=%s",
                    user["id"], user["role"], client_ip(),
                )
            except Exception:
                pass
            return False
    return True


def require_role(*roles: str):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            # Importante: usar code=303 nos redirects de auth para forcar
            # o cliente a converter POST -> GET. Sem isso, alguns clientes
            # mandam POST para /login e geram 405 Method Not Allowed.
            if not is_authenticated():
                flash("Entre para continuar.", "warning")
                return redirect(url_for("login"), code=303)
            user = get_current_user()
            if not user or not int(user["is_active"]):
                session.clear()
                flash("Sua sessao nao e mais valida.", "warning")
                return redirect(url_for("login"), code=303)
            if not _session_is_valid(user):
                session.clear()
                flash("Sua sessao foi encerrada por seguranca. Entre novamente.", "warning")
                return redirect(url_for("login"), code=303)
            ensure_csrf_token()
            if roles and user["role"] not in roles:
                flash("Voce nao tem permissao para isso.", "danger")
                return redirect(url_for("dashboard"), code=303)
            return view_func(*args, **kwargs)
        return wrapped
    return decorator


def login_required(view_func):
    return require_role()(view_func)


def create_password_reset_token() -> str:
    return secrets.token_urlsafe(32)


def password_is_strong(password: str) -> bool:
    if len(password) < 10:
        return False
    has_letter = any(ch.isalpha() for ch in password)
    has_digit = any(ch.isdigit() for ch in password)
    return has_letter and has_digit


def resync_product_stock(db, product_id: int) -> int:
    """Recalcula products.stock_qty = SUM(product_variants.stock_qty) para
    UM produto especifico. Chamar sempre que estoque de variante mudar.

    - Se o produto NAO tem variantes: nao toca em nada (mantem o valor manual).
    - Se tem variantes: products.stock_qty vira a soma das variantes.

    Retorna o novo total (ou -1 se nao mexeu por nao ter variantes).
    """
    has_variants = db.execute(
        "SELECT 1 FROM product_variants WHERE product_id = %s LIMIT 1",
        (product_id,),
    ).fetchone()
    if not has_variants:
        return -1
    row = db.execute(
        "SELECT COALESCE(SUM(stock_qty), 0) AS total FROM product_variants WHERE product_id = %s",
        (product_id,),
    ).fetchone()
    new_total = int(row["total"] if row and row["total"] is not None else 0)
    db.execute(
        "UPDATE products SET stock_qty = %s, updated_at = %s WHERE id = %s",
        (new_total, utc_now_iso(), product_id),
    )
    return new_total


# ---------- Security: role integrity, audit log, sudo mode ----------
def _role_integrity_pepper() -> bytes:
    """Pepper exclusivo para HMAC do role. Nunca reutilizar SECRET_KEY puro.

    Em producao: configure ROLE_INTEGRITY_PEPPER (>= 32 chars random).
    Sem ele, deriva de SECRET_KEY + sufixo fixo - funciona, mas perde a
    propriedade de rotacionar SECRET_KEY sem invalidar HMACs.
    """
    explicit = os.environ.get("ROLE_INTEGRITY_PEPPER", "").strip()
    if explicit:
        return explicit.encode("utf-8")
    base = current_app.config.get("SECRET_KEY", "") or "fallback"
    return hashlib.sha256(f"{base}|role-integrity|v1".encode("utf-8")).digest()


def compute_role_hmac(user_id: int, role: str, session_version: int) -> str:
    """HMAC-SHA256 sobre (id, role, session_version). Liga o role a
    identidade e ao versionamento de sessao. Edicao direta da coluna `role`
    no BD sem recomputar este HMAC = sessao derrubada no proximo request."""
    msg = f"{int(user_id)}|{role}|{int(session_version)}".encode("utf-8")
    return hmac.new(_role_integrity_pepper(), msg, hashlib.sha256).hexdigest()


def refresh_user_role_hmac(db, user_id: int) -> None:
    """Recalcula e grava o role_hmac do usuario. Chamar sempre que role
    ou session_version mudar."""
    row = db.execute(
        "SELECT id, role, session_version FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    if not row:
        return
    new_hmac = compute_role_hmac(row["id"], row["role"], int(row["session_version"] or 1))
    db.execute(
        "UPDATE users SET role_hmac = %s, updated_at = %s WHERE id = %s",
        (new_hmac, utc_now_iso(), user_id),
    )


def bump_session_version(db, user_id: int) -> None:
    """Incrementa session_version: derruba todas as sessoes vivas do user."""
    db.execute(
        "UPDATE users SET session_version = COALESCE(session_version,1) + 1, updated_at = %s WHERE id = %s",
        (utc_now_iso(), user_id),
    )
    refresh_user_role_hmac(db, user_id)


def audit_log(
    action: str,
    *,
    target_id: int | None = None,
    before: str | None = None,
    after: str | None = None,
    extra: str | None = None,
) -> None:
    """Registra evento de seguranca. Best-effort: nunca derruba a request."""
    try:
        actor = get_current_user() if is_authenticated() else None
        get_db().execute(
            "INSERT INTO security_audit "
            "(at, actor_id, actor_email, target_id, action, before_value, after_value, ip, user_agent, extra) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                utc_now_iso(),
                actor["id"] if actor else None,
                actor["email"] if actor else None,
                target_id,
                action,
                before,
                after,
                client_ip(),
                (request.headers.get("User-Agent") or "")[:500],
                extra,
            ),
        )
        get_db().commit()
    except Exception as exc:
        try:
            current_app.logger.warning("audit_log failed: %s", exc)
        except Exception:
            pass


# ----- Sudo mode: re-auth recente para acoes sensiveis -----
SUDO_TTL_SECONDS = 5 * 60  # 5 minutos


def sudo_is_fresh() -> bool:
    ts = session.get("sudo_at")
    if not ts:
        return False
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() < SUDO_TTL_SECONDS
    except Exception:
        return False


def mark_sudo_fresh() -> None:
    session["sudo_at"] = utc_now_iso()


def require_sudo(view_func):
    """Decorator: exige re-auth recente. Se nao fresca, redireciona para
    /sudo com next=URL atual."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not sudo_is_fresh():
            return redirect(url_for("sudo", next=request.full_path))
        return view_func(*args, **kwargs)
    return wrapped


# ---------- Coupon helpers ----------
def generate_human_code() -> str:
    return "-".join([secrets.token_hex(2).upper(), secrets.token_hex(2).upper(), secrets.token_hex(2).upper()])


def hash_code(code: str, pepper: str) -> str:
    normalized = normalize_code(code)
    return hmac.new(pepper.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def is_card_expired(card: dict) -> bool:
    expires_at = card["expires_at"]
    if not expires_at:
        return False
    expiry = datetime.fromisoformat(expires_at)
    return utc_now() > expiry


def card_verification_matches(card: dict, submitted_check: str) -> bool:
    submitted = normalize_text(submitted_check)
    # If left blank, skip verification — operator confirms identity manually
    if not submitted:
        return True
    allowed = {
        normalize_text(card["buyer_name"] or ""),
        normalize_text(card["buyer_phone"] or ""),
        normalize_text(card["order_reference"] or ""),
    }
    allowed.discard("")
    # If card has no verification data at all, also allow blank
    if not allowed:
        return True
    return submitted in allowed


def record_failed_lookup(app: Flask) -> None:
    failed_count = int(session.get("failed_lookup_count", 0)) + 1
    session["failed_lookup_count"] = failed_count
    if failed_count >= app.config["MAX_FAILED_LOOKUPS"]:
        lock_until = utc_now() + timedelta(minutes=app.config["LOCK_MINUTES"])
        session["lookup_lock_until"] = lock_until.isoformat()


def reset_failed_lookups() -> None:
    session.pop("failed_lookup_count", None)
    session.pop("lookup_lock_until", None)


def is_lookup_locked() -> bool:
    lock_until = session.get("lookup_lock_until")
    if not lock_until:
        return False
    return datetime.fromisoformat(lock_until) > utc_now()


def write_audit(db: object, gift_card_id: int | None, action: str, details: str) -> None:
    actor_name = session.get("display_name") or session.get("user_email") or "system"
    db.execute(
        """
        INSERT INTO audit_logs (gift_card_id, action, details, actor_name, ip_address, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            gift_card_id,
            action,
            details,
            actor_name,
            request.headers.get("X-Forwarded-For", request.remote_addr),
            utc_now_iso(),
        ),
    )


# ---------- Email ----------
def smtp_is_configured(app: Flask) -> bool:
    return bool(app.config.get("BREVO_API_KEY") or (app.config["SMTP_HOST"] and app.config["MAIL_FROM"]))


def send_email_via_brevo(app: Flask, to_email: str, to_name: str, subject: str, html_body: str, text_body: str, attachments: list[tuple[str, bytes, str]] | None = None) -> None:
    """Send transactional email via Brevo REST API (/v3/smtp/email)."""
    api_key = app.config.get("BREVO_API_KEY", "")
    if not api_key:
        raise RuntimeError("BREVO_API_KEY nao configurado")

    payload: dict = {
        "sender": {
            "name": app.config.get("MAIL_FROM_NAME", "Tonton"),
            "email": app.config["MAIL_FROM"],
        },
        "to": [{"email": to_email, "name": to_name or to_email}],
        "subject": subject,
        "htmlContent": html_body,
        "textContent": text_body,
    }

    if app.config.get("MAIL_REPLY_TO"):
        payload["replyTo"] = {"email": app.config["MAIL_REPLY_TO"]}

    if attachments:
        payload["attachment"] = [
            {
                "name": fname,
                "content": base64.b64encode(content).decode("ascii"),
            }
            for fname, content, _mime in attachments
        ]

    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=payload,
        timeout=25,
    )
    if not resp.ok:
        raise RuntimeError(f"Brevo API erro {resp.status_code}: {resp.text[:200]}")


def send_email_via_smtp(app: Flask, to_email: str, subject: str, html_body: str, text_body: str, attachments: list[tuple[str, bytes, str]] | None = None) -> None:
    if not app.config["SMTP_HOST"] or not app.config["MAIL_FROM"]:
        raise RuntimeError("SMTP nao configurado")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = app.config["MAIL_FROM"]
    msg["To"] = to_email
    if app.config["MAIL_REPLY_TO"]:
        msg["Reply-To"] = app.config["MAIL_REPLY_TO"]
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    for filename, content, mime in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)

    if app.config["SMTP_USE_SSL"]:
        with smtplib.SMTP_SSL(app.config["SMTP_HOST"], app.config["SMTP_PORT"], timeout=20) as server:
            if app.config["SMTP_USERNAME"]:
                server.login(app.config["SMTP_USERNAME"], app.config["SMTP_PASSWORD"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(app.config["SMTP_HOST"], app.config["SMTP_PORT"], timeout=20) as server:
            if app.config["SMTP_USE_TLS"]:
                server.starttls()
            if app.config["SMTP_USERNAME"]:
                server.login(app.config["SMTP_USERNAME"], app.config["SMTP_PASSWORD"])
            server.send_message(msg)


def send_email(app: Flask, to_email: str, to_name: str, subject: str, html_body: str, text_body: str, attachments: list[tuple[str, bytes, str]] | None = None) -> None:
    """Unified send: prefers Brevo API if BREVO_API_KEY is set, falls back to SMTP."""
    if app.config.get("BREVO_API_KEY"):
        send_email_via_brevo(app, to_email, to_name, subject, html_body, text_body, attachments)
    else:
        send_email_via_smtp(app, to_email, subject, html_body, text_body, attachments)


def build_card_email(card: dict, public_base_url: str, visible_code: str | None = None) -> tuple[str, str, str]:
    subject = f"Seu vale-presente Tonton · {format_money(card['current_balance'])}"
    recipient = card["recipient_name"] or "você"
    image_url = f"{public_base_url.rstrip('/')}/shared/card/{card['share_token']}/image" if card['share_token'] else ''
    code_line_html = f"<strong>Código:</strong> {visible_code}<br>" if visible_code else ''
    code_line_text = f"Código: {visible_code}\n" if visible_code else ''
    image_line_html = f"<strong>Vale:</strong> <a href='{image_url}'>{image_url}</a><br>" if image_url else ''
    image_line_text = f"Vale: {image_url}\n" if image_url else ''
    html = f"""
    <div style='font-family:Arial,sans-serif;line-height:1.6;color:#2b2b2b;'>
      <p>Oi, {recipient}.</p>
      <p>Seu vale-presente Tonton está pronto e segue em anexo.</p>
      <p>{code_line_html}<strong>Saldo:</strong> {format_money(card['current_balance'])}<br>
         <strong>Validade:</strong> {format_date_local(card['expires_at'])}<br>
         {image_line_html}</p>
      <p>Qualquer coisa, chama a Tonton.<br>
         <a href='https://www.instagram.com/tontonlojainfantil?igsh=NDNkaHhhNGc3dnlo' style='color:#b5924c'>@tontonlojainfantil</a></p>
    </div>
    """
    text = (
        f"Oi, {recipient}.\n\n"
        f"Seu vale-presente Tonton está pronto e segue em anexo.\n"
        f"{code_line_text}"
        f"Saldo: {format_money(card['current_balance'])}\n"
        f"Validade: {format_date_local(card['expires_at'])}\n"
        f"{image_line_text}"
        f"\nQualquer coisa, chama a Tonton.\n"
        f"Instagram: https://www.instagram.com/tontonlojainfantil?igsh=NDNkaHhhNGc3dnlo\n"
    )
    return subject, html, text


def send_pdf_download(pdf_bytes: bytes, filename: str):
    """Return a Flask response that RELIABLY downloads a PDF across browsers.

    Problems this fixes:
      - Global `Cache-Control: no-store` prevents mobile browsers (Chrome Android,
        Samsung Internet, in-app WebViews) from materializing the file in the
        Downloads folder. Override with a short private cache.
      - Without `as_attachment=True`, some mobile browsers try to render PDFs
        inline and fail silently. Forcing Content-Disposition: attachment
        guarantees the download prompt.
      - Missing Content-Length causes some browsers to abort "unknown-size"
        downloads.
      - Filenames com acentos/espaços precisam do parâmetro filename* (RFC 5987)
        para que iOS Safari e Samsung Internet preservem o nome no Downloads.
    """
    from urllib.parse import quote as _urlquote

    # Sanitiza nome ASCII (fallback) e codifica versão UTF-8 (filename*)
    ascii_name = "".join(c if ord(c) < 128 and c not in '"\\' else "_" for c in filename) or "download.pdf"
    utf8_name = _urlquote(filename, safe="")

    resp = send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=ascii_name,
    )
    # Sobrescreve Content-Disposition com filename* para máxima compatibilidade
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'
    )
    resp.headers["Cache-Control"] = "private, max-age=60"
    resp.headers["Content-Length"] = str(len(pdf_bytes))
    # Hint para WebViews que insistem em renderizar inline
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp



# ---------- Migrations ----------

# ─────────────────────────────────────────────────────────────
# DB init & seeds (Postgres-only)
# ─────────────────────────────────────────────────────────────
def _schema_is_valid(db) -> bool:
    """
    Valida que o schema está íntegro — ou seja, que todas as tabelas críticas
    existem COM as colunas que o código espera.
    Usa colunas adicionadas tardiamente no schema (qr_token em products,
    order_reference em gift_cards) como canários: se estão lá, o schema
    inteiro está correto; se faltam, algum deploy anterior aplicou o schema
    pela metade.
    """
    canary_checks = [
        ("products", "qr_token"),
        ("gift_cards", "order_reference"),
        ("gift_cards", "share_token"),
        ("products", "image_blob"),
    ]
    for table, column in canary_checks:
        row = db.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "  AND table_name = %s AND column_name = %s",
            (table, column),
        ).fetchone()
        if row is None:
            return False
    return True


def _apply_schema_file() -> None:
    """
    Aplica schema_pg.sql no banco atual. Idempotente por design — o schema
    usa CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS, INSERT ... ON
    CONFLICT DO NOTHING. Se qualquer tabela não bater com o definido no
    arquivo, derruba o schema inteiro e recria do zero.

    NUNCA chamar em produção se houver dados reais — é destrutivo.
    Design: esta aplicação assume fresh start; dados reais vivem apenas após
    o schema estar estável.
    """
    schema_file = BASE_DIR / "schema_pg.sql"
    if not schema_file.exists():
        raise RuntimeError(
            f"schema_pg.sql não encontrado em {schema_file}. "
            "Deploy incompleto."
        )

    sql = schema_file.read_text(encoding="utf-8")

    db = get_db()

    # Drop + recreate de schema public. Garante estado consistente.
    # Por ser DDL, precisa commit explícito após cada bloco.
    with db.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("CREATE SCHEMA public")
        cur.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        cur.execute("GRANT ALL ON SCHEMA public TO public")
    db.commit()

    # Aplica schema_pg.sql inteiro. psycopg2 aceita multi-statement em
    # cursor.execute() — diferente de SQLAlchemy text() que só executa o
    # primeiro. Esta é a razão pela qual funciona em psycopg2 direto e
    # falhava na versão com SQLAlchemy.
    with db.cursor() as cur:
        cur.execute(sql)
    db.commit()


def init_db() -> None:
    """
    Valida que o schema Postgres está aplicado e íntegro.

    Se ausente: aplica schema_pg.sql (cenário de banco recém-criado).
    Se inconsistente: PARA O BOOT com erro claro, sem apagar dados.

    Diferente de versões anteriores, NÃO faz drop+recreate em schemas
    inconsistentes — agora que há dados reais, isso seria destrutivo.
    Migrations devem ser aplicadas manualmente via psql.
    """
    db = get_db()

    gift_cards_exists = db.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = 'gift_cards'"
    ).fetchone() is not None

    if not gift_cards_exists:
        current_app.logger.info(
            "Schema Postgres ausente. Aplicando schema_pg.sql automaticamente..."
        )
        _apply_schema_file()
        current_app.logger.info("Schema aplicado com sucesso.")
        return

    if not _schema_is_valid(db):
        raise RuntimeError(
            "Schema Postgres inconsistente — colunas críticas faltando. "
            "NÃO foi feito drop automático para preservar dados. "
            "Aplique migration manualmente via psql ou contate suporte."
        )

    # Migrations incrementais idempotentes (v9+).
    # Cada bloco verifica antes de alterar; pode rodar N vezes sem efeito.
    _apply_incremental_migrations(db)


def _apply_incremental_migrations(db) -> None:
    """Migrations idempotentes que rodam em todo boot.
    Cada uma deve ser silenciosa se já foi aplicada."""
    # === v9: payment_status em sales ===
    has_payment_status = db.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'sales' AND column_name = 'payment_status'"
    ).fetchone() is not None

    if not has_payment_status:
        current_app.logger.info("Migration v9: adicionando payment_status em sales...")
        db.execute("ALTER TABLE sales ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'paid'")
        db.execute("ALTER TABLE sales ADD COLUMN payment_confirmed_at TEXT")
        db.execute("ALTER TABLE sales ADD COLUMN payment_confirmed_by TEXT")
        db.execute(
            "ALTER TABLE sales ADD CONSTRAINT sales_payment_status_check "
            "CHECK (payment_status IN ('paid','pending','failed','refunded'))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sales_payment_pending ON sales(payment_status) "
            "WHERE payment_status = 'pending'"
        )
        # Vendas existentes: assume tudo pago (created_at como timestamp de confirmação)
        db.execute(
            "UPDATE sales SET payment_confirmed_at = created_at, "
            "payment_confirmed_by = 'migration_v9' "
            "WHERE payment_confirmed_at IS NULL"
        )
        current_app.logger.info("Migration v9 aplicada.")

    # === v10: galeria multi-foto por produto ===
    has_product_images = db.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = 'product_images'"
    ).fetchone() is not None

    if not has_product_images:
        current_app.logger.info("Migration v10: criando product_images...")
        db.execute("""
            CREATE TABLE product_images (
                id BIGSERIAL PRIMARY KEY,
                product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                color TEXT,
                image_blob BYTEA NOT NULL,
                image_mime TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_primary SMALLINT NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("CREATE INDEX idx_product_images_pid ON product_images(product_id)")
        db.execute("CREATE INDEX idx_product_images_color ON product_images(product_id, color)")
        # Migra image_blob legado para a galeria como foto primária sem cor
        db.execute("""
            INSERT INTO product_images (product_id, color, image_blob, image_mime,
                                        sort_order, is_primary, created_at)
            SELECT id, NULL, image_blob, image_mime, 0, 1, created_at
              FROM products
             WHERE image_blob IS NOT NULL AND image_mime IS NOT NULL
        """)
        current_app.logger.info("Migration v10 aplicada.")

    # === v10.2: normaliza orientação EXIF de imagens existentes ===
    # Garantia de execução única: tabela _migrations_runtime registra a flag.
    db.execute("""
        CREATE TABLE IF NOT EXISTS _migrations_runtime (
            key TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    already_run = db.execute(
        "SELECT 1 FROM _migrations_runtime WHERE key=%s",
        ("v10_2_normalize_existing_images",)
    ).fetchone() is not None

    if not already_run:
        current_app.logger.info(
            "Migration v10.2: normalizando orientação EXIF de imagens existentes..."
        )
        # Normaliza fotos da galeria
        rows = db.execute(
            "SELECT id, image_blob, image_mime FROM product_images"
        ).fetchall()
        normalized_g = 0
        failed_g = 0
        for r in rows:
            try:
                blob = bytes(r["image_blob"]) if r["image_blob"] else None
                if not blob:
                    continue
                new_blob, new_mime = _normalize_uploaded_image(blob, r["image_mime"])
                # Só atualiza se mudou (evita escrita desnecessária)
                if new_blob != blob or new_mime != r["image_mime"]:
                    db.execute(
                        "UPDATE product_images SET image_blob=%s, image_mime=%s WHERE id=%s",
                        (new_blob, new_mime, r["id"])
                    )
                    normalized_g += 1
            except Exception as e:
                failed_g += 1
                current_app.logger.warning(
                    f"Migration v10.2: falha ao normalizar product_images.id={r['id']}: {e}"
                )

        # Normaliza fotos legadas em products.image_blob (caso ainda existam)
        rows = db.execute(
            "SELECT id, image_blob, image_mime FROM products "
            "WHERE image_blob IS NOT NULL"
        ).fetchall()
        normalized_p = 0
        failed_p = 0
        for r in rows:
            try:
                blob = bytes(r["image_blob"]) if r["image_blob"] else None
                if not blob:
                    continue
                new_blob, new_mime = _normalize_uploaded_image(blob, r["image_mime"])
                if new_blob != blob or new_mime != r["image_mime"]:
                    db.execute(
                        "UPDATE products SET image_blob=%s, image_mime=%s WHERE id=%s",
                        (new_blob, new_mime, r["id"])
                    )
                    normalized_p += 1
            except Exception as e:
                failed_p += 1
                current_app.logger.warning(
                    f"Migration v10.2: falha ao normalizar products.id={r['id']}: {e}"
                )

        db.execute(
            "INSERT INTO _migrations_runtime (key, applied_at) VALUES (%s, %s)",
            ("v10_2_normalize_existing_images", utc_now_iso())
        )
        current_app.logger.info(
            f"Migration v10.2 aplicada. Galeria: {normalized_g} normalizadas, "
            f"{failed_g} falharam. Legado: {normalized_p} normalizadas, {failed_p} falharam."
        )

    # === v10.3: image_version em product_images (cache busting) ===
    has_image_version = db.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'product_images' AND column_name = 'image_version'"
    ).fetchone() is not None

    if not has_image_version:
        current_app.logger.info("Migration v10.3: adicionando image_version em product_images...")
        db.execute(
            "ALTER TABLE product_images ADD COLUMN image_version INTEGER NOT NULL DEFAULT 1"
        )
        current_app.logger.info("Migration v10.3 aplicada.")

    # === v10.5: sincroniza fotos legacy órfãs (products.image_blob sem entrada
    # correspondente em product_images). Necessário porque produtos editados
    # entre v10 e v10.5 ficaram com foto só no legacy — catálogo público não
    # encontra. Esta migração é IDEMPOTENTE e segura: só insere quando falta.
    orphans = db.execute("""
        SELECT p.id, p.image_blob, p.image_mime, p.created_at
        FROM products p
        WHERE p.image_blob IS NOT NULL
          AND p.image_mime IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM product_images pi
            WHERE pi.product_id = p.id AND pi.is_primary = 1
          )
    """).fetchall()
    if orphans:
        current_app.logger.info(
            f"Migration v10.5: sincronizando {len(orphans)} foto(s) legacy órfã(s) "
            f"para a galeria..."
        )
        for r in orphans:
            try:
                blob = bytes(r["image_blob"]) if r["image_blob"] else None
                if not blob:
                    continue
                next_order = db.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n "
                    "FROM product_images WHERE product_id=%s", (r["id"],)
                ).fetchone()["n"]
                db.execute(
                    "INSERT INTO product_images "
                    "(product_id, color, image_blob, image_mime, sort_order, "
                    " is_primary, created_at, image_version) "
                    "VALUES (%s, NULL, %s, %s, %s, 1, %s, 1)",
                    (r["id"], blob, r["image_mime"], next_order,
                     r["created_at"] or utc_now_iso())
                )
            except Exception as e:
                current_app.logger.warning(
                    f"Migration v10.5: falha em products.id={r['id']}: {e}"
                )
        current_app.logger.info("Migration v10.5 aplicada.")

    # === v10.6: coluna `slug` em products + backfill ===========================
    # URLs amigáveis no catálogo público (/catalogo/<slug>) e SEO.
    # Slug é IMUTÁVEL após gerado — mudar quebra links salvos por clientes.
    has_slug = db.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'products' AND column_name = 'slug'"
    ).fetchone() is not None

    if not has_slug:
        current_app.logger.info("Migration v10.6: adicionando slug em products...")
        db.execute("ALTER TABLE products ADD COLUMN slug TEXT")
        # Index único parcial — permite múltiplos NULLs (durante backfill)
        # mas garante unicidade quando preenchido.
        db.execute(
            "CREATE UNIQUE INDEX idx_products_slug "
            "ON products(slug) WHERE slug IS NOT NULL"
        )
        current_app.logger.info("Migration v10.6: coluna slug criada.")

    # Backfill: gera slug para produtos sem slug.
    # Idempotente — só toca em products que tem slug NULL.
    pending_slug = db.execute(
        "SELECT id, name FROM products WHERE slug IS NULL"
    ).fetchall()
    if pending_slug:
        current_app.logger.info(
            f"Migration v10.6: gerando slug para {len(pending_slug)} produto(s)..."
        )
        used_slugs = set(
            r["slug"] for r in db.execute(
                "SELECT slug FROM products WHERE slug IS NOT NULL"
            ).fetchall()
        )
        for r in pending_slug:
            base = slugify(r["name"] or f"produto-{r['id']}")
            candidate = base
            n = 2
            # Resolve colisão com sufixo numérico (ex: cinto-2, cinto-3)
            while candidate in used_slugs:
                candidate = f"{base}-{n}"
                n += 1
            try:
                db.execute(
                    "UPDATE products SET slug=%s WHERE id=%s",
                    (candidate, r["id"])
                )
                used_slugs.add(candidate)
            except Exception as e:
                current_app.logger.warning(
                    f"Migration v10.6: falha em products.id={r['id']}: {e}"
                )
        current_app.logger.info("Migration v10.6 backfill aplicado.")

    # === v10.7: tabela catalog_hero_images ====================================
    # Imagens da loja (vitrine, fachada, ambiente) que aparecem no carrossel do
    # topo do catálogo público. Substituem a peça destacada no hero, exceto
    # quando há produto marcado com is_featured (que sempre vence).
    has_hero_table = db.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'catalog_hero_images'"
    ).fetchone() is not None

    if not has_hero_table:
        current_app.logger.info("Migration v10.7: criando catalog_hero_images...")
        db.execute("""
            CREATE TABLE catalog_hero_images (
                id            SERIAL PRIMARY KEY,
                image_blob    BYTEA NOT NULL,
                image_mime    TEXT NOT NULL,
                caption       TEXT,
                sort_order    INTEGER NOT NULL DEFAULT 0,
                is_active     SMALLINT NOT NULL DEFAULT 1,
                image_version INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
                updated_at    TEXT
            )
        """)
        db.execute(
            "CREATE INDEX idx_hero_active_order "
            "ON catalog_hero_images (is_active, sort_order, id)"
        )
        current_app.logger.info("Migration v10.7 aplicada.")


def get_setting(key: str, default: str = "") -> str:
    db = get_db()
    row = db.execute("SELECT value FROM store_settings WHERE key=%s", (key,)).fetchone()
    return row["value"] if row else default


# ---------- Temas sazonais ----------
# Lista curada. Painel interno usa apenas o chip; identidade preservada.
AVAILABLE_THEMES = [
    {"slug": "",              "name": "Padrão Tonton",   "icon": "✦"},
    {"slug": "primavera",     "name": "Primavera",     "icon": "🌸"},
    {"slug": "verao",         "name": "Verão",         "icon": "☀️"},
    {"slug": "outono",        "name": "Outono",        "icon": "🍂"},
    {"slug": "inverno",       "name": "Inverno",       "icon": "❄️"},
    {"slug": "dia-maes",      "name": "Dia das Mães",  "icon": "🌷"},
    {"slug": "black-friday",  "name": "Black Friday",  "icon": "🛍️"},
    {"slug": "natal",         "name": "Natal",         "icon": "🎄"},
]
_VALID_THEME_SLUGS = {t["slug"] for t in AVAILABLE_THEMES}


def get_active_theme() -> dict:
    """Retorna o tema ativo (slug, name, icon). Fallback seguro para padrao."""
    slug = (get_setting("active_theme", "") or "").strip().lower()
    if slug not in _VALID_THEME_SLUGS:
        slug = ""
    for t in AVAILABLE_THEMES:
        if t["slug"] == slug:
            return t
    return AVAILABLE_THEMES[0]


def _table_exists(db, table_name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = %s",
        (table_name,),
    ).fetchone()
    return bool(row)


def set_setting(key: str, value: str) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO store_settings(key,value,updated_at) VALUES(%s,%s,%s) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, utc_now_iso()),
    )
    db.commit()


def _mm_setting(key: str, default: str) -> float:
    raw = (get_setting(key, default) or default).strip().replace(",", ".")
    try:
        value = float(raw)
    except Exception:
        value = float(str(default).replace(",", "."))
    return max(5.0, value)


def _ensure_product_qr_tokens(db: object) -> None:
    rows = db.execute(
        "SELECT id FROM products WHERE qr_token IS NULL OR TRIM(COALESCE(qr_token,''))=''"
    ).fetchall()
    for row in rows:
        db.execute(
            "UPDATE products SET qr_token=%s, updated_at=%s WHERE id=%s",
            (secrets.token_urlsafe(18), utc_now_iso(), row["id"]),
        )


def _product_qr_payload(product: dict | dict) -> str:
    token = ((product["qr_token"] if product and product["qr_token"] else "") or "").strip()
    return f"TONTON-PROD-{token}"


def _sale_shipping_snapshot(sale: dict | dict, customer: dict | dict | None = None) -> dict:
    def _pick(*values):
        for value in values:
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    sale_keys = set(sale.keys()) if hasattr(sale, "keys") else set(sale)
    return {
        "name": _pick(sale["shipping_name"] if "shipping_name" in sale_keys else "", sale["buyer_name_free"] if "buyer_name_free" in sale_keys else "", customer["name"] if customer else ""),
        "document": _pick(sale["shipping_document"] if "shipping_document" in sale_keys else ""),
        "cep": _pick(sale["shipping_cep"] if "shipping_cep" in sale_keys else ""),
        "street": _pick(sale["shipping_street"] if "shipping_street" in sale_keys else ""),
        "number": _pick(sale["shipping_number"] if "shipping_number" in sale_keys else ""),
        "complement": _pick(sale["shipping_complement"] if "shipping_complement" in sale_keys else ""),
        "neighborhood": _pick(sale["shipping_neighborhood"] if "shipping_neighborhood" in sale_keys else ""),
        "city": _pick(sale["shipping_city"] if "shipping_city" in sale_keys else ""),
        "state": _pick(sale["shipping_state"] if "shipping_state" in sale_keys else ""),
    }


def _shipping_label_has_data(shipping: dict) -> bool:
    required = [shipping.get("name", ""), shipping.get("street", ""), shipping.get("city", ""), shipping.get("state", "")]
    return all(bool(str(item).strip()) for item in required)


def _shipping_lines(shipping: dict) -> list[str]:
    line2_parts = [shipping.get("street", "").strip()]
    if shipping.get("number", "").strip():
        line2_parts.append(f"nº {shipping['number'].strip()}")
    line2 = ", ".join([part for part in line2_parts if part])

    if shipping.get("complement", "").strip():
        line2 = f"{line2} · {shipping['complement'].strip()}" if line2 else shipping["complement"].strip()

    line3_parts = [shipping.get("neighborhood", "").strip(), shipping.get("city", "").strip(), shipping.get("state", "").strip()]
    line3 = " · ".join([part for part in line3_parts if part])

    lines = [shipping.get("name", "").strip()]
    if shipping.get("document", "").strip():
        lines.append(f"CPF/CNPJ {shipping['document'].strip()}")
    lines.extend([line2, line3])
    if shipping.get("cep", "").strip():
        lines.append(f"CEP {shipping['cep'].strip()}")
    return [line for line in lines if line]


def _store_invoice_profile() -> dict:
    return {
        "trade_name": (get_setting("store_name", "Tonton") or "Tonton").strip(),
        "legal_name": (get_setting("store_legal_name", "") or "").strip(),
        "tax_id": (get_setting("store_tax_id", "") or "").strip(),
        "state_tax_id": (get_setting("store_state_tax_id", "") or "").strip(),
        "tax_regime": (get_setting("store_tax_regime", "MEI") or "MEI").strip(),
        "operation_nature": (get_setting("store_operation_nature", "Venda de mercadoria") or "Venda de mercadoria").strip(),
        "email": (get_setting("store_contact_email", "") or "").strip(),
        "phone": (get_setting("store_contact_phone", "") or "").strip(),
        "street": (get_setting("store_address_street", "") or "").strip(),
        "number": (get_setting("store_address_number", "") or "").strip(),
        "complement": (get_setting("store_address_complement", "") or "").strip(),
        "neighborhood": (get_setting("store_address_neighborhood", "") or "").strip(),
        "city": (get_setting("store_address_city", "") or "").strip(),
        "state": (get_setting("store_address_state", "") or "").strip(),
        "zipcode": (get_setting("store_address_zipcode", "") or "").strip(),
    }


def _store_invoice_lines(profile: dict) -> list[str]:
    name = profile.get("legal_name") or profile.get("trade_name") or "Tonton"
    docs = []
    if profile.get("tax_id"):
        docs.append(f"CNPJ {profile['tax_id']}")
    if profile.get("state_tax_id"):
        docs.append(f"IE {profile['state_tax_id']}")
    line2_parts = [profile.get("street", "")]
    if profile.get("number"):
        line2_parts.append(f"nº {profile['number']}")
    line2 = ", ".join([p for p in line2_parts if p])
    if profile.get("complement"):
        line2 = f"{line2} · {profile['complement']}" if line2 else profile["complement"]
    line3 = " · ".join([p for p in [profile.get("neighborhood", ""), profile.get("city", ""), profile.get("state", "")] if p])
    if profile.get("zipcode"):
        line3 = f"{line3} · CEP {profile['zipcode']}" if line3 else f"CEP {profile['zipcode']}"
    contact = " · ".join([p for p in [profile.get("phone", ""), profile.get("email", "")] if p])

    lines = [name]
    if docs:
        lines.append(" · ".join(docs))
    if line2:
        lines.append(line2)
    if line3:
        lines.append(line3)
    if contact:
        lines.append(contact)
    return lines


def _wrap_for_pdf(draw, text: str, font, max_width: int) -> list[str]:
    words = (text or "").split()
    if not words:
        return []
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _prefatura_payload(sale: dict | dict, items: list[dict], shipping: dict, store: dict) -> dict:
    sale_number = sale["sale_number"] if sale["sale_number"] else sale["id"]
    item_rows = []
    for idx, item in enumerate(items, start=1):
        qty = int(item["qty"] or 0)
        unit_price = Decimal(str(item["unit_price"] or 0))
        line_total = (unit_price * Decimal(qty)).quantize(Decimal("0.01"))
        item_rows.append({
            "item": idx,
            "sku": item["sku"] or "",
            "descricao": item["product_name"],
            "ncm": item["ncm"] if "ncm" in item.keys() else "",
            "cfop": item["cfop"] if "cfop" in item.keys() else "",
            "origem": item["origin_code"] if "origin_code" in item.keys() else "",
            "unidade": item["unit"] or "un",
            "quantidade": qty,
            "valor_unitario": str(unit_price),
            "valor_total": str(line_total),
        })

    customer_name = shipping.get("name") or sale["customer_name"] or sale["buyer_name_free"] or "Consumidor final"
    return {
        "documento": {
            "tipo": "pre-nota",
            "numero_pedido": sale_number,
            "natureza_operacao": store.get("operation_nature") or "Venda de mercadoria",
            "regime_tributario_loja": store.get("tax_regime") or "MEI",
            "emitido_em": sale["created_at"],
            "forma_pagamento": sale["payment_method"] or "",
        },
        "emitente": {
            "nome_fantasia": store.get("trade_name", ""),
            "razao_social": store.get("legal_name", ""),
            "cnpj": store.get("tax_id", ""),
            "inscricao_estadual": store.get("state_tax_id", ""),
            "telefone": store.get("phone", ""),
            "email": store.get("email", ""),
            "logradouro": store.get("street", ""),
            "numero": store.get("number", ""),
            "complemento": store.get("complement", ""),
            "bairro": store.get("neighborhood", ""),
            "cidade": store.get("city", ""),
            "uf": store.get("state", ""),
            "cep": store.get("zipcode", ""),
        },
        "destinatario": {
            "nome": customer_name,
            "cpf_cnpj": shipping.get("document", ""),
            "telefone": sale["customer_phone"] if "customer_phone" in sale.keys() else "",
            "email": sale["customer_email"] if "customer_email" in sale.keys() else "",
            "logradouro": shipping.get("street", ""),
            "numero": shipping.get("number", ""),
            "complemento": shipping.get("complement", ""),
            "bairro": shipping.get("neighborhood", ""),
            "cidade": shipping.get("city", ""),
            "uf": shipping.get("state", ""),
            "cep": shipping.get("cep", ""),
        },
        "totais": {
            "subtotal": str(Decimal(str(sale["subtotal"] or 0)).quantize(Decimal("0.01"))),
            "desconto": str(Decimal(str(sale["discount_amount"] or 0)).quantize(Decimal("0.01"))),
            "total": str(Decimal(str(sale["total"] or 0)).quantize(Decimal("0.01"))),
        },
        "itens": item_rows,
        "observacoes": sale["notes"] or "",
    }


def ensure_schema_migrations(app: Flask) -> None:
    """
    Postgres-only. Schema completo aplicado via `psql -f schema_pg.sql` antes
    do primeiro deploy. Aqui só rodam seeds dinâmicos dependentes de runtime:
      * tokens QR em produtos sem token
      * usuário admin inicial (se nenhum admin existir)
      * calendário de datas comerciais (idempotente)
    """
    db = get_db()

    # Migrations idempotentes (colunas adicionadas após v3.2)
    for sql in (
        "ALTER TABLE product_variants ADD COLUMN IF NOT EXISTS promo_price TEXT",
        "ALTER TABLE product_variants ADD COLUMN IF NOT EXISTS promo_until TEXT",
        "ALTER TABLE products         ADD COLUMN IF NOT EXISTS promo_price TEXT",
        "ALTER TABLE products         ADD COLUMN IF NOT EXISTS promo_until TEXT",
        # Onda 1 · Ficha técnica
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS composition JSONB",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS care_wash TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS fabric_type TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS fabric_weight_gsm INTEGER",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS fit TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS length_class TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS country_of_origin TEXT DEFAULT 'BR'",
        "CREATE INDEX IF NOT EXISTS idx_products_composition_gin ON products USING GIN (composition)",
        # Onda 2 · Endurecimento de auth
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS role_hmac TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_secret TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_enabled SMALLINT NOT NULL DEFAULT 0",
        """
        CREATE TABLE IF NOT EXISTS security_audit (
            id BIGSERIAL PRIMARY KEY,
            at TEXT NOT NULL,
            actor_id BIGINT,
            actor_email TEXT,
            target_id BIGINT,
            action TEXT NOT NULL,
            before_value TEXT,
            after_value TEXT,
            ip TEXT,
            user_agent TEXT,
            extra TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_security_audit_target ON security_audit(target_id)",
        "CREATE INDEX IF NOT EXISTS idx_security_audit_action ON security_audit(action)",
        "CREATE INDEX IF NOT EXISTS idx_security_audit_at ON security_audit(at DESC)",
        """
        CREATE TABLE IF NOT EXISTS auth_failures (
            id BIGSERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            ip TEXT NOT NULL,
            at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_auth_failures_lookup ON auth_failures(email, ip, at DESC)",
        # Onda 3 · Multi-PSP PIX
        """
        CREATE TABLE IF NOT EXISTS pix_provider_accounts (
            id BIGSERIAL PRIMARY KEY,
            provider TEXT NOT NULL CHECK (provider IN ('inter','mercadopago','manual')),
            label TEXT NOT NULL,
            is_active SMALLINT NOT NULL DEFAULT 1,
            is_default SMALLINT NOT NULL DEFAULT 0,
            credentials_encrypted TEXT,
            settings_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_pix_accounts_active ON pix_provider_accounts(is_active, provider)",
        """
        CREATE TABLE IF NOT EXISTS pix_charges (
            id BIGSERIAL PRIMARY KEY,
            sale_id BIGINT REFERENCES sales(id) ON DELETE CASCADE,
            account_id BIGINT REFERENCES pix_provider_accounts(id) ON DELETE SET NULL,
            provider TEXT NOT NULL,
            provider_charge_id TEXT,
            txid TEXT NOT NULL,
            amount TEXT NOT NULL,
            brcode TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','paid','expired','cancelled','refunded')),
            paid_at TEXT,
            paid_amount TEXT,
            payer_name TEXT,
            raw_webhook TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_pix_charges_sale ON pix_charges(sale_id)",
        "CREATE INDEX IF NOT EXISTS idx_pix_charges_status ON pix_charges(status, created_at DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pix_charges_provider_id ON pix_charges(provider, provider_charge_id) WHERE provider_charge_id IS NOT NULL",
        # Onda 4 - Audit de estoque por variante
        "ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS variant_id BIGINT REFERENCES product_variants(id) ON DELETE SET NULL",
        "CREATE INDEX IF NOT EXISTS idx_stock_movements_variant ON stock_movements(variant_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_stock_movements_product_created ON stock_movements(product_id, created_at DESC)",
        # Onda 5 - Estoque com fonte unica (variantes mandam quando existem)
        "ALTER TABLE sale_items ADD COLUMN IF NOT EXISTS variant_id BIGINT REFERENCES product_variants(id) ON DELETE SET NULL",
        "CREATE INDEX IF NOT EXISTS idx_sale_items_variant ON sale_items(variant_id)",
    ):
        try:
            db.execute(sql)
        except Exception as exc:
            current_app.logger.warning("Schema migration ignorada (%s): %s", sql, exc)

    # tokens QR faltantes
    _ensure_product_qr_tokens(db)

    # Bootstrap idempotente do role_hmac para usuarios existentes.
    # Roda uma vez por usuario sem HMAC (apos add da coluna). Idempotente:
    # qualquer execucao posterior so pega quem ainda nao tiver o HMAC.
    try:
        pending = db.execute(
            "SELECT id, role, COALESCE(session_version,1) AS session_version "
            "FROM users WHERE role_hmac IS NULL OR role_hmac = ''"
        ).fetchall()
        for u in pending:
            h = compute_role_hmac(u["id"], u["role"], int(u["session_version"]))
            db.execute(
                "UPDATE users SET role_hmac = %s WHERE id = %s",
                (h, u["id"]),
            )
        if pending:
            current_app.logger.info("role_hmac bootstrap: %d usuarios", len(pending))
    except Exception as exc:
        current_app.logger.warning("role_hmac bootstrap ignorado: %s", exc)

    # Bootstrap idempotente: recalcula products.stock_qty a partir da soma
    # das variantes para produtos que possuem pelo menos 1 variante.
    # Roda toda vez, mas so atualiza linhas onde ha divergencia. Idempotente.
    try:
        result = db.execute("""
            WITH variant_totals AS (
                SELECT product_id, COALESCE(SUM(stock_qty), 0) AS total
                FROM product_variants
                GROUP BY product_id
            )
            UPDATE products p
               SET stock_qty = vt.total,
                   updated_at = %s
              FROM variant_totals vt
             WHERE p.id = vt.product_id
               AND p.stock_qty IS DISTINCT FROM vt.total
            RETURNING p.id
        """, (utc_now_iso(),))
        updated_rows = result.fetchall() if result else []
        if updated_rows:
            current_app.logger.info(
                "stock_qty resync: %d produto(s) com variantes ressincronizado(s)",
                len(updated_rows),
            )
    except Exception as exc:
        current_app.logger.warning("stock_qty resync ignorado: %s", exc)

    # admin inicial
    existing_admin = db.execute(
        "SELECT id FROM users WHERE role='admin' LIMIT 1"
    ).fetchone()
    if not existing_admin:
        email = os.environ.get("ADMIN_USERNAME", "admin@male.local")
        if "@" not in email:
            email = f"{email}@male.local"
        pw_hash = os.environ.get(
            "ADMIN_PASSWORD_HASH",
            generate_password_hash("Troque-esta-senha"),
        )
        now = utc_now_iso()
        db.execute(
            "INSERT INTO users (email,display_name,password_hash,role,is_active,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,1,%s,%s)",
            (email, "Admin Tonton", pw_hash, "admin", now, now),
        )
        new_admin = db.execute(
            "SELECT id FROM users WHERE email = %s", (email,)
        ).fetchone()
        if new_admin:
            refresh_user_role_hmac(db, new_admin["id"])

    # calendário de datas comerciais
    now_iso = utc_now_iso()
    cal_events = [
        ("Dia das Mães", "2026-05-10", "data", "Segundo domingo de maio"),
        ("Dia dos Namorados", "2026-06-12", "data", "Campanha 2-3 semanas antes"),
        ("Dia dos Pais (gifts femininos)", "2026-08-09", "data", ""),
        ("Dia do Cliente", "2026-09-15", "data", "Desconto especial para fidelizadas"),
        ("Black Friday", "2026-11-27", "evento", "Preparar campanha desde outubro"),
        ("Natal", "2026-12-25", "data", "Vendas de presente pico 10-22/12"),
    ]
    for title, dt, kind, note in cal_events:
        exists = db.execute(
            "SELECT id FROM fashion_calendar WHERE title=%s AND event_date=%s",
            (title, dt),
        ).fetchone()
        if not exists:
            db.execute(
                "INSERT INTO fashion_calendar(title,event_date,kind,notes,is_active,created_at) "
                "VALUES(%s,%s,%s,%s,1,%s)",
                (title, dt, kind, note, now_iso),
            )
    db.commit()


def _configure_logging(app: Flask) -> None:
    """Send Flask's logger output to stdout so gunicorn/PaaS capture it.
    Set LOG_LEVEL=DEBUG locally to see more detail."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    # Prefer gunicorn's handlers if running under gunicorn
    gunicorn_logger = logging.getLogger("gunicorn.error")
    if gunicorn_logger.handlers:
        app.logger.handlers = gunicorn_logger.handlers
        app.logger.setLevel(gunicorn_logger.level or level)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        app.logger.handlers = [handler]
        app.logger.setLevel(level)
    app.logger.propagate = False


def client_ip() -> str:
    """Return the caller's best-known IP. ProxyFix already rewrote remote_addr
    from X-Forwarded-For, so request.remote_addr is reliable."""
    return request.remote_addr or "0.0.0.0"


# Simple in-memory per-process rate limiter. Good enough to block abuse on
# small deployments (1-2 workers). For larger setups, switch to Redis/Memcached.
_RATE_LIMIT_BUCKETS: dict[str, list[float]] = {}


def rate_limited(key: str, max_hits: int, window_seconds: int) -> bool:
    """Return True when the caller should be throttled. Sliding window."""
    import time as _time
    now = _time.monotonic()
    bucket = _RATE_LIMIT_BUCKETS.setdefault(key, [])
    cutoff = now - window_seconds
    # Drop stale timestamps
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= max_hits:
        return True
    bucket.append(now)
    # Bound memory: keep at most max_hits*2 entries per key
    if len(bucket) > max_hits * 2:
        del bucket[:-max_hits]
    return False


# Track failed login attempts per email+IP. Lock after N misses in the window.
# Bucket in-memory mantido como fallback. O caminho preferencial usa a
# tabela auth_failures (persistente, multi-worker, sobrevive deploys).
_LOGIN_FAILURES: dict[str, list[float]] = {}


def _login_key(email: str) -> str:
    return f"{(email or '').strip().lower()}|{client_ip()}"


def _lockout_use_db() -> bool:
    """True quando temos request context + DB disponivel.
    Em testes isolados pode nao ter; cai para o dict in-memory."""
    try:
        get_db()
        return True
    except Exception:
        return False


def login_is_locked(email: str, max_failures: int, lock_minutes: int) -> bool:
    norm_email = (email or "").strip().lower()
    ip = client_ip()
    if _lockout_use_db():
        try:
            cutoff = (utc_now() - timedelta(minutes=lock_minutes)).isoformat()
            row = get_db().execute(
                "SELECT COUNT(*) AS n FROM auth_failures "
                "WHERE email = %s AND ip = %s AND at >= %s",
                (norm_email, ip, cutoff),
            ).fetchone()
            return int(row["n"]) >= max_failures
        except Exception as exc:
            current_app.logger.warning("auth_failures query falhou, fallback dict: %s", exc)
    # Fallback in-memory
    import time as _time
    now = _time.monotonic()
    key = _login_key(email)
    bucket = _LOGIN_FAILURES.get(key, [])
    cutoff = now - (lock_minutes * 60)
    bucket = [t for t in bucket if t >= cutoff]
    _LOGIN_FAILURES[key] = bucket
    return len(bucket) >= max_failures


def record_login_failure(email: str) -> None:
    norm_email = (email or "").strip().lower()
    ip = client_ip()
    if _lockout_use_db():
        try:
            db = get_db()
            db.execute(
                "INSERT INTO auth_failures (email, ip, at) VALUES (%s, %s, %s)",
                (norm_email, ip, utc_now_iso()),
            )
            db.commit()
            # Limpeza oportunistica: registros > 24h sao inuteis
            cutoff = (utc_now() - timedelta(hours=24)).isoformat()
            db.execute("DELETE FROM auth_failures WHERE at < %s", (cutoff,))
            db.commit()
            return
        except Exception as exc:
            current_app.logger.warning("auth_failures insert falhou, fallback dict: %s", exc)
    import time as _time
    _LOGIN_FAILURES.setdefault(_login_key(email), []).append(_time.monotonic())


def reset_login_failures(email: str) -> None:
    norm_email = (email or "").strip().lower()
    ip = client_ip()
    if _lockout_use_db():
        try:
            db = get_db()
            db.execute(
                "DELETE FROM auth_failures WHERE email = %s AND ip = %s",
                (norm_email, ip),
            )
            db.commit()
        except Exception as exc:
            current_app.logger.warning("auth_failures cleanup falhou: %s", exc)
    _LOGIN_FAILURES.pop(_login_key(email), None)


def _get_stable_secret_key() -> str:
    """Return a SECRET_KEY that stays stable across worker restarts.

    Priority:
      1. FLASK_SECRET_KEY env var (recomendado em produção).
      2. Arquivo persistido em /tmp (sobrevive workers, não deploys).
      3. Gerar em memória (não sobrevive restart).

    Em produção, SEMPRE configure FLASK_SECRET_KEY no Railway.
    """
    env_key = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if env_key:
        return env_key

    # Fallback: persistir em /tmp para que workers do mesmo deploy compartilhem
    key_file = os.path.join("/tmp", "male_secret_key")
    try:
        if os.path.exists(key_file):
            with open(key_file, "r") as f:
                k = f.read().strip()
                if k:
                    return k
        k = secrets.token_hex(32)
        with open(key_file, "w") as f:
            f.write(k)
        try:
            os.chmod(key_file, 0o600)
        except Exception:
            pass
        return k
    except Exception:
        # Último fallback: gerar em memória (sessions não sobrevivem restart)
        return secrets.token_hex(32)


def _detect_secure_cookies() -> bool:
    """True when running behind HTTPS. Honour explicit FORCE_HTTPS env, then
    fall back to known PaaS markers. Set FORCE_HTTPS=true in production."""
    explicit = os.environ.get("FORCE_HTTPS", "").strip().lower()
    if explicit in ("1", "true", "yes", "on"):
        return True
    if explicit in ("0", "false", "no", "off"):
        return False
    return (
        os.environ.get("RENDER") == "true"
        or bool(os.environ.get("RAILWAY_ENVIRONMENT"))
        or bool(os.environ.get("DYNO"))  # Heroku
        or bool(os.environ.get("FLY_APP_NAME"))  # Fly.io
    )


def create_app() -> Flask:
    app = Flask(__name__)

    # Behind a PaaS reverse proxy (Railway/Render/Heroku/Fly), trust the first
    # hop so that request.is_secure, request.remote_addr and url_for(..., _external=True)
    # reflect the real client connection instead of the proxy.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    app.config.update(
        SECRET_KEY=_get_stable_secret_key(),
        CODE_PEPPER=os.environ.get("CODE_PEPPER", secrets.token_hex(32)),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=_detect_secure_cookies(),
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=6),
        MAX_FAILED_LOOKUPS=int(os.environ.get("MAX_FAILED_LOOKUPS", "5")),
        LOCK_MINUTES=int(os.environ.get("LOCK_MINUTES", "15")),
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:5000"),
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024,
        SMTP_HOST=os.environ.get("SMTP_HOST", ""),
        SMTP_PORT=int(os.environ.get("SMTP_PORT", "587")),
        SMTP_USERNAME=os.environ.get("SMTP_USERNAME", ""),
        SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD", ""),
        SMTP_USE_TLS=os.environ.get("SMTP_USE_TLS", "true").lower() == "true",
        SMTP_USE_SSL=os.environ.get("SMTP_USE_SSL", "false").lower() == "true",
        MAIL_FROM=os.environ.get("MAIL_FROM", ""),
        MAIL_FROM_NAME=os.environ.get("MAIL_FROM_NAME", "Tonton"),
        MAIL_REPLY_TO=os.environ.get("MAIL_REPLY_TO", ""),
        BREVO_API_KEY=os.environ.get("BREVO_API_KEY", ""),
        GOOGLE_CLIENT_ID=os.environ.get("GOOGLE_CLIENT_ID", ""),
        GOOGLE_CLIENT_SECRET=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        GOOGLE_DISCOVERY_URL=os.environ.get("GOOGLE_DISCOVERY_URL", GOOGLE_DISCOVERY_URL),
        CODE_ENCRYPTION_KEY=os.environ.get("CODE_ENCRYPTION_KEY", ""),
    )

    # ---------- Logging ----------
    # Under gunicorn, attach to gunicorn's logger so everything shows up in
    # the platform's log stream. Locally, log to stdout with a clean format.
    _configure_logging(app)

    oauth = OAuth(app)
    if app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"]:
        oauth.register(
            name="google",
            server_metadata_url=app.config["GOOGLE_DISCOVERY_URL"],
            client_id=app.config["GOOGLE_CLIENT_ID"],
            client_secret=app.config["GOOGLE_CLIENT_SECRET"],
            client_kwargs={"scope": "openid email profile"},
        )
    # Expor oauth no app.extensions para blueprints (auth) consumirem.
    app.extensions["oauth"] = oauth

    @app.before_request
    def before_request() -> None:
        session.permanent = True

    @app.after_request
    def set_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; img-src 'self' data: https://www.instagram.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' data: https://fonts.gstatic.com; connect-src 'self' https://accounts.google.com https://oauth2.googleapis.com https://openidconnect.googleapis.com; frame-ancestors 'none';"
        # no-store is the right default for HTML (session security), but breaks
        # downloads on mobile browsers. Only apply to HTML; skip when the
        # endpoint already set a specific Cache-Control (e.g. PDFs, images).
        is_downloadable = (
            response.mimetype.startswith("application/pdf")
            or response.mimetype.startswith("image/")
            or "attachment" in response.headers.get("Content-Disposition", "")
        )
        if not is_downloadable and not response.headers.get("Cache-Control"):
            response.headers["Cache-Control"] = "no-store"
        # HSTS whenever we're serving over HTTPS (ProxyFix makes this reliable).
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    # ---------- Error handlers ----------
    # Friendly pages for errors; never leak stack traces. Returning an accept-
    # aware JSON for API paths and HTML for the rest.
    def _wants_json() -> bool:
        if request.path.startswith("/api/"):
            return True
        accept = request.headers.get("Accept", "")
        return "application/json" in accept and "text/html" not in accept

    @app.errorhandler(400)
    def err_400(e):
        msg = getattr(e, "description", "Requisição inválida.")
        if _wants_json():
            return {"ok": False, "error": "bad_request", "message": str(msg)}, 400
        return render_template("error.html", code=400, title="Requisição inválida",
                               message=msg), 400

    @app.errorhandler(403)
    def err_403(e):
        if _wants_json():
            return {"ok": False, "error": "forbidden"}, 403
        return render_template("error.html", code=403, title="Acesso negado",
                               message="Você não tem permissão para ver essa página."), 403

    @app.errorhandler(404)
    def err_404(e):
        if _wants_json():
            return {"ok": False, "error": "not_found"}, 404
        return render_template("error.html", code=404, title="Página não encontrada",
                               message="O endereço acessado não existe ou foi movido."), 404

    @app.errorhandler(413)
    def err_413(e):
        msg = "O arquivo é maior que o limite permitido."
        if _wants_json():
            return {"ok": False, "error": "payload_too_large"}, 413
        return render_template("error.html", code=413, title="Arquivo grande demais",
                               message=msg), 413

    @app.errorhandler(429)
    def err_429(e):
        if _wants_json():
            return {"ok": False, "error": "rate_limited"}, 429
        return render_template("error.html", code=429, title="Muitas requisições",
                               message="Aguarde alguns instantes e tente de novo."), 429

    @app.errorhandler(500)
    def err_500(e):
        current_app.logger.exception("Unhandled 500 on %s", request.path)
        if _wants_json():
            return {"ok": False, "error": "server_error"}, 500
        return render_template("error.html", code=500, title="Algo deu errado",
                               message="Tivemos um problema aqui. O erro foi registrado."), 500

    @app.errorhandler(Exception)
    def err_unhandled(e):
        # Re-raise HTTP exceptions so their dedicated handler runs.
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        current_app.logger.exception("Unhandled exception on %s", request.path)
        if _wants_json():
            return {"ok": False, "error": "server_error"}, 500
        return render_template("error.html", code=500, title="Algo deu errado",
                               message="Tivemos um problema aqui. O erro foi registrado."), 500

    def _row_to_dict(row):
        """Converte dict em dict para uso seguro em Jinja.
        Rows não têm .get(), e o Jinja às vezes tenta esse fallback,
        resultando em UndefinedError. Dict cobre os dois padrões."""
        if row is None:
            return None
        try:
            return {k: row[k] for k in row.keys()}
        except Exception:
            return row

    @app.context_processor
    def inject_helpers():
        def ui_text(key: str, default: str = "") -> str:
            value = get_setting(key, default)
            if value is None:
                return default
            if isinstance(value, str) and not value.strip():
                return default
            return value

        def asset_v(filename: str) -> str:
            """Retorna mtime do arquivo estático para cache-busting.
            Usa em templates: url_for('static', filename='x.css', v=asset_v('x.css')).
            Quando o arquivo é editado, mtime muda e browser baixa de novo —
            sem precisar de hard-refresh nem de versão manual."""
            try:
                import os
                p = os.path.join(app.static_folder or "static", filename)
                return str(int(os.path.getmtime(p)))
            except Exception:
                return "0"

        def _safe_pending_count():
            """Conta pendentes; retorna 0 silenciosamente se a coluna ainda
            não existe (durante deploy/migration)."""
            try:
                db = get_db()
                row = db.execute(
                    "SELECT COUNT(*) AS cnt FROM sales "
                    "WHERE status='active' AND payment_status='pending'"
                ).fetchone()
                return int(row["cnt"] or 0)
            except Exception:
                return 0

        return {
            "format_money": format_money,
            "format_datetime_local": format_datetime_local,
            "format_date_local": format_date_local,
            "format_phone_display": format_phone_display,
            "build_tel_url": build_tel_url,
            "build_whatsapp_url": build_whatsapp_url,
            "public_base_url": app.config["PUBLIC_BASE_URL"],
            "instagram_url": INSTAGRAM_URL,
            "current_user": _row_to_dict(get_current_user()),
            "smtp_ready": smtp_is_configured(app),
            "google_login_enabled": bool(app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"]),
            "_margin_color": _margin_color,
            "_shipping_lines": _shipping_lines,
            "ui_text": ui_text,
            "asset_v": asset_v,
            "effective_price": effective_price,
            "has_active_promo": has_active_promo,
            "composition_pretty": _composition_pretty,
            "care_wash_label": lambda c: CARE_WASH_LABELS.get(c, ""),
            "is_clothing_complete": _is_clothing_complete,
            "unread_notifications": (lambda db=None: db.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0] if db else 0)(db=get_db() if True else None),
            "pending_payments_count": (lambda: _safe_pending_count())(),
            "active_theme": get_active_theme(),
            "available_themes": AVAILABLE_THEMES,
            # Permite a templates verificarem se um endpoint existe antes de
            # chamar url_for, evitando BuildError quando blueprints opcionais
            # não estão registrados (ex.: lookbook em ambientes legados).
            "has_endpoint": lambda name: name in current_app.view_functions,
        }

    @app.teardown_appcontext
    def _close_db(error=None):
        close_db(error)

    with app.app_context():
        init_db()
        ensure_schema_migrations(app)

    @app.route("/")
    def index():
        return redirect(url_for("dashboard" if is_authenticated() else "login"))

    def complete_login(user: dict, provider: str = "password"):
        session.clear()
        db = get_db()
        # Garante role_hmac atualizado (cobre upgrades de versao + edicao
        # manual no BD: se HMAC nao existir/bater, recalcula, mas registra).
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

    # ─────────────────────────────────────────────────────────────
    # Rotas de autenticação foram extraídas para blueprints/auth.py
    # (Onda 2 — Etapa B). Endpoints preservam nomes globais:
    # login, logout, login_google, auth_google_callback,
    # forgot_password, reset_password, change_password, sudo.
    # Registro do blueprint acontece no fim de create_app().
    # ─────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────
    # Rotas extraídas para blueprints/admin.py (Onda 2 — Etapa D).
    # Endpoints preservam nomes globais; aliases via init_admin_blueprint.
    # ─────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────
    # Vale-presente / gift cards extraídos para blueprints/marketing.py
    # (Onda 2 — Etapa E). Endpoints preservam nomes globais.
    # ─────────────────────────────────────────────────────────────


    # ─────────────────────────────────────────────────────────────
    # Onda 2 (fechamento): register_v3_routes e register_v32_routes
    # foram desmontadas. As rotas que estavam nelas foram extraídas
    # para blueprints (auth, public, admin, marketing, customers,
    # products, sales, operations) ou inlinedas aqui.
    # ─────────────────────────────────────────────────────────────

    # ─── DASHBOARD (mantido em app.py — é o "home" da app) ──────
    @app.route("/dashboard")
    @login_required
    def dashboard():
        db = get_db()
        now_local = datetime.now(LOCAL_TZ)

        # ─── Mês de referência ───────────────────────────────────────
        # Default: mês corrente. Aceita ?month=YYYY-MM para análise
        # histórica. Bound de defesa: parsing inválido → fallback para
        # mês corrente; meses no futuro também caem para o corrente
        # (não faz sentido estatística de mês que não rolou).
        ref_month_param = (request.args.get("month") or "").strip()
        if ref_month_param:
            try:
                ref_month_dt = datetime.strptime(ref_month_param, "%Y-%m").replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=LOCAL_TZ
                )
                # Mês no futuro? Volta pro corrente.
                if ref_month_dt.replace(day=1) > now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0):
                    ref_month_dt = now_local
            except (ValueError, TypeError):
                ref_month_dt = now_local
        else:
            ref_month_dt = now_local

        # Flag: o mês de referência é o corrente?
        is_current_month = (
            ref_month_dt.year == now_local.year
            and ref_month_dt.month == now_local.month
        )

        # Janelas (mês de referência e mês anterior).
        month_start, month_end = month_bounds_utc(ref_month_dt)
        prev_month_dt = (ref_month_dt.replace(day=1) - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # Garante timezone (datetime - timedelta perde tzinfo em alguns paths)
        if prev_month_dt.tzinfo is None:
            prev_month_dt = prev_month_dt.replace(tzinfo=LOCAL_TZ)
        prev_month, _ = month_bounds_utc(prev_month_dt)

        # Próximo e mês anterior em formato YYYY-MM (para template construir links).
        prev_month_param = prev_month_dt.strftime("%Y-%m")
        next_month_dt = (ref_month_dt.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if next_month_dt.tzinfo is None:
            next_month_dt = next_month_dt.replace(tzinfo=LOCAL_TZ)
        next_month_param = next_month_dt.strftime("%Y-%m") if not is_current_month else None
        ref_month_param_str = ref_month_dt.strftime("%Y-%m")

        # Lista de meses para o dropdown (últimos 12 incluindo o corrente).
        # Helper local — mesma aritmética de _shift_months usada no chart.
        def _shift_months_local(base, delta):
            year = base.year
            month = base.month - delta
            while month <= 0:
                month += 12
                year -= 1
            while month > 12:
                month -= 12
                year += 1
            return base.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)

        _months_pt = ["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"]
        month_options = []
        for i in range(12):
            d = _shift_months_local(now_local, i)
            month_options.append({
                "param": d.strftime("%Y-%m"),
                "label": f"{_months_pt[d.month - 1]} {d.year}",
                "is_current": (i == 0),
                "is_selected": d.strftime("%Y-%m") == ref_month_param_str,
            })

        # Threshold de gift cards expirando: relativo ao "agora real",
        # não ao mês de referência (não faz sentido procurar cards que
        # expiravam em abril enquanto estamos em maio).
        expiring_th = (now_local + timedelta(days=7)).astimezone(UTC).isoformat()

        # Gift card stats (original)
        gc_stats = db.execute("""
            SELECT
                COUNT(*) AS total_cards,
                COALESCE(SUM(CASE WHEN status='active' THEN CAST(current_balance AS DOUBLE PRECISION) ELSE 0 END),0) AS active_balance,
                COALESCE(SUM(CASE WHEN status='redeemed' THEN 1 ELSE 0 END),0) AS redeemed_count,
                COALESCE(SUM(CASE WHEN expires_at IS NOT NULL AND status='active' AND expires_at<=%s THEN 1 ELSE 0 END),0) AS expiring_soon_count,
                COALESCE(SUM(CASE WHEN created_at>=%s AND status!='cancelled' THEN CAST(initial_value AS DOUBLE PRECISION) ELSE 0 END),0) AS month_issued
            FROM gift_cards
        """, (expiring_th, month_start)).fetchone()

        # Sales this month vs last month — apenas vendas com pagamento confirmado
        sales_month = db.execute("SELECT COALESCE(SUM(CASE WHEN status!='cancelled' AND payment_status='paid' THEN CAST(total AS DOUBLE PRECISION) ELSE 0 END),0) as rev, SUM(CASE WHEN status!='cancelled' AND payment_status='paid' THEN 1 ELSE 0 END) as cnt FROM sales WHERE created_at>=%s AND created_at<%s", (month_start, month_end)).fetchone()
        sales_prev  = db.execute("SELECT COALESCE(SUM(CASE WHEN status!='cancelled' AND payment_status='paid' THEN CAST(total AS DOUBLE PRECISION) ELSE 0 END),0) as rev FROM sales WHERE created_at>=%s AND created_at<%s", (prev_month, month_start)).fetchone()
        gc_prev = db.execute("SELECT COALESCE(SUM(CASE WHEN created_at>=%s AND created_at<%s AND status!='cancelled' THEN CAST(initial_value AS DOUBLE PRECISION) ELSE 0 END),0) AS rev FROM gift_cards", (prev_month, month_start)).fetchone()

        # COGS this month — só de vendas pagas (receita reconhecida)
        cogs = db.execute("""
            SELECT COALESCE(SUM(si.qty * CAST(si.cost_price AS DOUBLE PRECISION)),0) as cogs
            FROM sale_items si JOIN sales s ON s.id=si.sale_id
            WHERE s.created_at>=%s AND s.created_at<%s AND s.status!='cancelled' AND s.payment_status='paid'
        """, (month_start, month_end)).fetchone()

        # Expenses do mês de referência (active only)
        month_start_local, month_end_local = month_bounds_local(ref_month_dt)
        expenses_month = db.execute(
            "SELECT COALESCE(SUM(CAST(amount AS DOUBLE PRECISION)),0) as total FROM expenses WHERE expense_date>=%s AND expense_date<%s AND status='active'",
            (month_start_local, month_end_local)
        ).fetchone()

        # v9: KPI "A receber" — vendas ativas com pagamento pendente
        pending_pay = db.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(CAST(total AS DOUBLE PRECISION)),0) AS amt "
            "FROM sales WHERE status='active' AND payment_status='pending'"
        ).fetchone()

        sales_rev = Decimal(str(sales_month["rev"]))
        gc_rev    = Decimal(str(gc_stats["month_issued"]))
        rev       = sales_rev + gc_rev
        cogs_v    = Decimal(str(cogs["cogs"]))
        exp_v     = Decimal(str(expenses_month["total"]))
        gross     = rev - cogs_v
        op        = gross - exp_v
        gross_pct = (gross / rev * 100) if rev > 0 else Decimal("0")
        op_pct    = (op / rev * 100) if rev > 0 else Decimal("0")

        prev_rev = Decimal(str(sales_prev["rev"])) + Decimal(str(gc_prev["rev"]))
        rev_growth = ((rev - prev_rev) / prev_rev * 100) if prev_rev > 0 else None

        # Low stock
        low_stock = db.execute(
            "SELECT * FROM products WHERE stock_qty<=stock_min AND is_active=1 ORDER BY (stock_qty*1.0/NULLIF(stock_min,0)) ASC LIMIT 8"
        ).fetchall()

        # Recent sales
        recent_sales = db.execute("""
            SELECT s.id, s.sale_number, s.total, s.created_at, s.payment_method,
                   c.name as customer_name,
                   STRING_AGG(p.name, ', ') as items
            FROM sales s
            LEFT JOIN customers c ON c.id=s.customer_id
            LEFT JOIN sale_items si ON si.sale_id=s.id
            LEFT JOIN products p ON p.id=si.product_id
            WHERE s.status!='cancelled'
            GROUP BY s.id, s.sale_number, s.total, s.created_at, s.payment_method, c.name
            ORDER BY s.created_at DESC LIMIT 6
        """).fetchall()

        # Recent stock movements
        recent_movements = db.execute("""
            SELECT sm.*, p.name as product_name
            FROM stock_movements sm JOIN products p ON p.id=sm.product_id
            ORDER BY sm.created_at DESC LIMIT 6
        """).fetchall()

        # Top products by gross profit no mês de referência (mês corrente
        # ou o que o usuário selecionou no seletor do topo do dashboard).
        top_products = db.execute("""
            SELECT p.name, p.sku,
                   SUM(si.qty) as units_sold,
                   SUM(CASE WHEN s.status!='cancelled' THEN si.qty * CAST(si.unit_price AS DOUBLE PRECISION) ELSE 0 END) as revenue,
                   SUM(CASE WHEN s.status!='cancelled' THEN si.qty * (CAST(si.unit_price AS DOUBLE PRECISION) - CAST(si.cost_price AS DOUBLE PRECISION)) ELSE 0 END) as gross_profit,
                   CASE WHEN SUM(si.qty*CAST(si.unit_price AS DOUBLE PRECISION))>0
                        THEN (SUM(si.qty*(CAST(si.unit_price AS DOUBLE PRECISION)-CAST(si.cost_price AS DOUBLE PRECISION)))/SUM(si.qty*CAST(si.unit_price AS DOUBLE PRECISION)))*100
                        ELSE 0 END as margin_pct
            FROM sale_items si
            JOIN products p ON p.id=si.product_id
            JOIN sales s ON s.id=si.sale_id
            WHERE s.created_at>=%s AND s.created_at<%s
            GROUP BY p.id ORDER BY gross_profit DESC LIMIT 5
        """, (month_start, month_end)).fetchall()

        # ─── Janela do gráfico ─────────────────────────────────────
        # window=6 (default) | 12 | ytd
        # Os botões "6 meses / 12 meses / YTD" no template enviam ?window=...
        # Se o usuário escolheu mês de referência diferente do corrente,
        # gráfico volta N meses a partir desse mês (não a partir de hoje).
        chart_window = (request.args.get("window", "6") or "6").strip().lower()
        if chart_window == "ytd":
            # Year-to-date: do mês 1 do ano do mês de referência até o próprio.
            n_months = ref_month_dt.month
        elif chart_window == "12":
            n_months = 12
        else:
            chart_window = "6"  # normaliza fallback
            n_months = 6

        # Monthly chart data — exclui vendas canceladas, mostra resultado operacional REAL.
        # Plot pode ser negativo (mês com perda); o template trata o eixo Y adaptativo.
        # Reaproveita _shift_months_local definido no início desta view.
        chart_months = []
        for i in range(n_months - 1, -1, -1):
            d = _shift_months_local(ref_month_dt, i)
            ms, me = month_bounds_utc(d)
            ms_local, me_local = month_bounds_local(d)
            r = db.execute("SELECT COALESCE(SUM(CASE WHEN status!='cancelled' THEN CAST(total AS DOUBLE PRECISION) ELSE 0 END),0) as rev FROM sales WHERE created_at>=%s AND created_at<%s", (ms, me)).fetchone()
            c = db.execute("""
                SELECT COALESCE(SUM(si.qty * CAST(si.cost_price AS DOUBLE PRECISION)),0) as cogs
                FROM sale_items si JOIN sales s ON s.id=si.sale_id
                WHERE s.created_at>=%s AND s.created_at<%s AND s.status!='cancelled'
            """, (ms, me)).fetchone()
            e = db.execute("SELECT COALESCE(SUM(CAST(amount AS DOUBLE PRECISION)),0) as exp FROM expenses WHERE expense_date>=%s AND expense_date<%s AND status='active'",
                           (ms_local, me_local)).fetchone()
            rev_val = float(r["rev"])
            cogs_val = float(c["cogs"])
            exp_val = float(e["exp"])
            result_val = round(rev_val - cogs_val - exp_val, 2)
            chart_months.append({
                "label": d.strftime("%b"),
                "rev": rev_val,
                "cogs": cogs_val,
                "exp": exp_val,
                "result": result_val,
                # plot agora é o resultado REAL (positivo ou negativo).
                # Antes era max(result, 0), o que zerava meses com perda — bug
                # que escondia exatamente os meses que mais precisam de atenção.
                "plot": result_val,
            })

        expiring_cards = db.execute("""
            SELECT id, code_last4, recipient_name, current_balance, expires_at
            FROM gift_cards WHERE expires_at IS NOT NULL AND status='active' AND expires_at<=%s
            ORDER BY expires_at ASC LIMIT 5
        """, (expiring_th,)).fetchall()

        customer_count = db.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        new_customers  = db.execute("SELECT COUNT(*) FROM customers WHERE created_at>=%s", (month_start,)).fetchone()[0]

        # Clientes inativos: compraram alguma vez, não compram há mais de N dias
        try:
            inactive_days = int(get_setting("inactive_days_threshold", "60"))
        except (ValueError, TypeError):
            inactive_days = 60
        inactive_cutoff = (utc_now() - timedelta(days=inactive_days)).isoformat()
        inactive_customers = db.execute("""
            SELECT
                c.id, c.name, c.phone,
                MAX(s.created_at) AS last_purchase,
                COUNT(s.id) AS sale_count,
                COALESCE(SUM(CAST(s.total AS DOUBLE PRECISION)),0) AS total_spent
            FROM customers c
            JOIN sales s ON s.customer_id = c.id AND s.status <> 'cancelled'
            WHERE c.phone IS NOT NULL AND TRIM(c.phone) <> ''
            GROUP BY c.id, c.name, c.phone
            HAVING MAX(s.created_at) < %s
            ORDER BY total_spent DESC, last_purchase ASC
            LIMIT 8
        """, (inactive_cutoff,)).fetchall()

        # Eixo Y adaptativo: precisa lidar com plot negativo (mês com perda).
        # chart_min_v e chart_max_v definem a faixa visível; chart_ticks são
        # gerados em 5 pontos uniformes entre min e max (com 0 sempre presente
        # se a faixa cruza zero, para a baseline visual).
        plot_values = [m["plot"] for m in chart_months] or [0.0]
        chart_max_v = max(plot_values + [0])  # garante 0 visível
        chart_min_v = min(plot_values + [0])  # negativo se há mês com perda
        # Faixa total do eixo Y; evita divisão por zero.
        chart_range = (chart_max_v - chart_min_v) or 1
        # 5 ticks uniformes; se min<0 e max>0, posição de 0 cai naturalmente.
        chart_ticks = [
            round(chart_max_v - (chart_range * p / 4), 2)
            for p in (0, 1, 2, 3, 4)
        ]

        # ─── Comparação com mês anterior ───────────────────────────
        # Quando o mês corrente acabou de virar, KPIs ficam todos em 0.
        # Adicionamos os valores do mês anterior para o template exibir
        # como referência ao lado de cada KPI.
        prev_sales_month = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN status!='cancelled' AND payment_status='paid' THEN CAST(total AS DOUBLE PRECISION) ELSE 0 END),0) as rev, "
            "SUM(CASE WHEN status!='cancelled' AND payment_status='paid' THEN 1 ELSE 0 END) as cnt "
            "FROM sales WHERE created_at>=%s AND created_at<%s",
            (prev_month, month_start)
        ).fetchone()
        prev_gc_issued = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN status!='cancelled' THEN CAST(initial_value AS DOUBLE PRECISION) ELSE 0 END),0) as rev "
            "FROM gift_cards WHERE created_at>=%s AND created_at<%s",
            (prev_month, month_start)
        ).fetchone()
        prev_cogs = db.execute(
            "SELECT COALESCE(SUM(si.qty*CAST(si.cost_price AS DOUBLE PRECISION)),0) as cogs "
            "FROM sale_items si JOIN sales s ON s.id=si.sale_id "
            "WHERE s.created_at>=%s AND s.created_at<%s "
            "AND s.status!='cancelled' AND s.payment_status='paid'",
            (prev_month, month_start)
        ).fetchone()
        prev_month_local_start, prev_month_local_end = month_bounds_local(prev_month_dt)
        prev_expenses = db.execute(
            "SELECT COALESCE(SUM(CAST(amount AS DOUBLE PRECISION)),0) as total "
            "FROM expenses WHERE expense_date>=%s AND expense_date<%s AND status='active'",
            (prev_month_local_start, prev_month_local_end)
        ).fetchone()

        prev_rev_full = Decimal(str(prev_sales_month["rev"])) + Decimal(str(prev_gc_issued["rev"]))
        prev_cogs_v   = Decimal(str(prev_cogs["cogs"]))
        prev_exp_v    = Decimal(str(prev_expenses["total"]))
        prev_gross    = prev_rev_full - prev_cogs_v
        prev_gross_pct = (prev_gross / prev_rev_full * 100) if prev_rev_full > 0 else Decimal("0")
        prev_sales_count = int(prev_sales_month["cnt"] or 0)
        # Label do mês anterior em pt-BR (era %b, retornava "Apr" em inglês).
        prev_label = _months_pt[prev_month_dt.month - 1]

        # Label do mês de referência (para o seletor exibir "abr 2026", etc.)
        ref_label = f"{_months_pt[ref_month_dt.month - 1]} {ref_month_dt.year}"

        return render_template("dashboard.html",
            gc_stats=gc_stats,
            sales_rev=sales_rev, gc_rev=gc_rev,
            chart_max_v=chart_max_v, chart_min_v=chart_min_v,
            rev=rev, cogs_v=cogs_v, exp_v=exp_v, gross=gross, op=op,
            gross_pct=gross_pct, op_pct=op_pct, rev_growth=rev_growth,
            sales_count=sales_month["cnt"],
            pending_count=int(pending_pay["cnt"] or 0),
            pending_amount=Decimal(str(pending_pay["amt"] or 0)),
            low_stock=low_stock, recent_sales=recent_sales,
            recent_movements=recent_movements, top_products=top_products,
            chart_months=chart_months, expiring_cards=expiring_cards,
            chart_window=chart_window,
            customer_count=customer_count, new_customers=new_customers,
            inactive_customers=inactive_customers,
            inactive_days=inactive_days,
            template_count=len(available_template_paths()),
            chart_ticks=chart_ticks,
            # `now` agora é o mês de REFERÊNCIA (não o agora real). Page sub-label
            # e o YTD label do gráfico usam essa data.
            now=ref_month_dt,
            _margin_color=_margin_color,
            # Comparativos do mês anterior — sempre mostrados ao lado dos KPIs.
            prev_rev=prev_rev_full,
            prev_sales_count=prev_sales_count,
            prev_gross=prev_gross,
            prev_gross_pct=prev_gross_pct,
            prev_label=prev_label,
            # Seletor de mês — variáveis usadas pelo template.
            ref_label=ref_label,
            ref_month_param=ref_month_param_str,
            prev_month_param=prev_month_param,
            next_month_param=next_month_param,
            is_current_month=is_current_month,
            month_options=month_options,
        )


    return app



# =============================================================================
# MÓDULO: PRODUTOS & ESTOQUE
# =============================================================================

# ═══════════════════════════════════════════════════════════════
# MÓDULOS V3 — PRODUTOS, CLIENTES, VENDAS, CONTABILIDADE,
#              CUPONS DE DESCONTO, CAMPANHAS, CONFIGURAÇÕES
# ═══════════════════════════════════════════════════════════════


# ── v3.1 helpers (GPT-inspired improvements) ─────────────────
import base64 as _b64

def _make_thumbnail(raw: bytes, max_size=(900, 900)) -> bytes:
    img = Image.open(io.BytesIO(raw))
    # v10.2: rotaciona pixels conforme EXIF antes de processar
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    if img.mode == "RGBA":
        bg = Image.new("RGBA", img.size, "white")
        bg.alpha_composite(img)
        img = bg.convert("RGB")
    img.thumbnail(max_size)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88, optimize=True)
    return out.getvalue()


def _normalize_uploaded_image(blob: bytes, mime: str) -> tuple[bytes, str]:
    """Normaliza imagem do upload:
    1. Aplica orientação EXIF (foto fica "em pé" para todos os browsers)
    2. Redimensiona para máximo 1600px no maior lado (mantém proporção)
    3. Recomprime: JPEG quality 85, WebP quality 85, PNG mantém formato
    4. Strip do EXIF (não preserva metadados — privacidade + tamanho menor)
    5. HEIC/HEIF (iPhone) → convertido para JPEG (browsers não renderizam HEIC)

    Retorna (novo_blob, novo_mime).
    Levanta ValueError se o arquivo não for uma imagem válida — assim a rota
    pode mostrar mensagem útil ao usuário em vez de salvar dados quebrados.
    """
    if not blob:
        return blob, mime

    mime_low = (mime or "").lower()
    is_heic = mime_low in ("image/heic", "image/heif") or mime_low == ""

    try:
        src = io.BytesIO(blob)
        img = Image.open(src)
        # Verifica integridade antes de prosseguir (detecta arquivos corrompidos)
        img.verify()
        # verify() consome o stream — reabre
        src.seek(0)
        img = Image.open(src)
    except Exception as e:
        # Se for HEIC e o plugin não está disponível, mensagem específica
        if is_heic and not _HEIF_SUPPORT:
            raise ValueError(
                "Formato HEIC do iPhone não suportado neste servidor. "
                "Converta a foto para JPG antes de enviar, ou peça ao "
                "administrador para instalar pillow-heif."
            ) from e
        raise ValueError(
            "Arquivo inválido ou não é uma imagem reconhecida "
            "(formatos aceitos: JPG, PNG, WebP, HEIC)."
        ) from e

    try:
        # 1. EXIF transpose — rotaciona pixels conforme metadado e zera o EXIF
        img = ImageOps.exif_transpose(img)

        # 2. Resize se exceder 1600px no maior lado
        MAX_DIM = 1600
        if max(img.size) > MAX_DIM:
            img.thumbnail((MAX_DIM, MAX_DIM), Image.Resampling.LANCZOS)

        # 3. Recompressão por formato. HEIC sempre vira JPEG (compatibilidade browser).
        out = io.BytesIO()
        if mime_low == "image/png":
            # PNG: preserva (inclui transparência); só redimensionou
            if img.mode not in ("RGBA", "RGB", "L", "P"):
                img = img.convert("RGBA")
            img.save(out, format="PNG", optimize=True)
            new_mime = "image/png"
        elif mime_low == "image/webp":
            if img.mode == "P":
                img = img.convert("RGBA")
            img.save(out, format="WEBP", quality=85, method=6)
            new_mime = "image/webp"
        else:
            # JPEG (default + HEIC convertido): RGBA → RGB com fundo branco
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.save(out, format="JPEG", quality=85, optimize=True, progressive=True)
            new_mime = "image/jpeg"

        return out.getvalue(), new_mime
    except Exception as e:
        # Falha na recompressão (raríssimo): erro explícito, não silencioso
        raise ValueError(f"Falha ao processar imagem: {e}") from e


def _rotate_image_blob(blob: bytes, mime: str, degrees: int = 90) -> tuple[bytes, str]:
    """Rotaciona imagem em N graus (sentido horário). Usado pelo botão manual
    da galeria. Preserva o formato MIME original."""
    if not blob:
        return blob, mime
    try:
        img = Image.open(io.BytesIO(blob))
        img = ImageOps.exif_transpose(img)  # garante baseline correto
        # PIL: rotate é anti-horário; para horário use -degrees
        img = img.rotate(-degrees, expand=True)

        out = io.BytesIO()
        mime_low = (mime or "").lower()
        if mime_low == "image/png":
            if img.mode not in ("RGBA", "RGB", "L", "P"):
                img = img.convert("RGBA")
            img.save(out, format="PNG", optimize=True)
            return out.getvalue(), "image/png"
        elif mime_low == "image/webp":
            if img.mode == "P":
                img = img.convert("RGBA")
            img.save(out, format="WEBP", quality=85, method=6)
            return out.getvalue(), "image/webp"
        else:
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.save(out, format="JPEG", quality=85, optimize=True, progressive=True)
            return out.getvalue(), "image/jpeg"
    except Exception:
        return blob, mime


def _sync_legacy_to_gallery(db, pid: int, blob: bytes, mime: str) -> None:
    """Sincroniza foto do campo legacy `products.image_blob` para a tabela
    `product_images` (galeria). Necessário para o catálogo público encontrar
    a foto: o template/query do catálogo só lê de `product_images`.

    Se já existir foto primária para o produto, atualiza-a. Senão, cria.
    Esta função é idempotente — pode ser chamada várias vezes sem efeito colateral.
    """
    if not blob or not mime:
        return
    now = utc_now_iso()
    primary = db.execute(
        "SELECT id FROM product_images WHERE product_id=%s AND is_primary=1 LIMIT 1",
        (pid,)
    ).fetchone()
    if primary:
        db.execute(
            "UPDATE product_images SET image_blob=%s, image_mime=%s, "
            "image_version=COALESCE(image_version,1)+1 "
            "WHERE id=%s",
            (blob, mime, primary["id"])
        )
    else:
        next_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n "
            "FROM product_images WHERE product_id=%s", (pid,)
        ).fetchone()["n"]
        db.execute(
            "INSERT INTO product_images "
            "(product_id, color, image_blob, image_mime, sort_order, is_primary, created_at) "
            "VALUES (%s, NULL, %s, %s, %s, 1, %s)",
            (pid, blob, mime, next_order, now)
        )


def _product_image_uri(product_row) -> str | None:
    """Retorna data URI da foto principal do produto.
    v10.4 fix: prefere a foto primária da galeria; fallback para image_blob legado.
    Necessário porque rotações/edições só alteram a galeria."""
    pid = product_row["id"] if "id" in product_row.keys() else None
    blob = None
    mime = None

    # 1) Tenta foto primária da galeria
    if pid is not None:
        try:
            db = get_db()
            row = db.execute(
                "SELECT image_blob, image_mime FROM product_images "
                "WHERE product_id=%s AND is_primary=1 LIMIT 1", (pid,)
            ).fetchone()
            if not row:
                row = db.execute(
                    "SELECT image_blob, image_mime FROM product_images "
                    "WHERE product_id=%s ORDER BY sort_order ASC, id ASC LIMIT 1", (pid,)
                ).fetchone()
            if row and row["image_blob"]:
                blob = bytes(row["image_blob"])
                mime = row["image_mime"]
        except Exception:
            pass

    # 2) Fallback: image_blob legado
    if not blob:
        legacy_blob = product_row["image_blob"] if "image_blob" in product_row.keys() else None
        if not legacy_blob:
            return None
        blob = bytes(legacy_blob) if not isinstance(legacy_blob, bytes) else legacy_blob
        mime = (product_row["image_mime"] if "image_mime" in product_row.keys() else None) or "image/jpeg"

    if not mime:
        mime = "image/jpeg"
    return f"data:{mime};base64,{_b64.b64encode(blob).decode()}"


def _truncate_text(text: str, max_len: int = 160) -> str:
    """Trunca texto preservando palavras inteiras, com elipse Unicode no fim."""
    if not text:
        return ""
    text = " ".join(str(text).split())  # normaliza whitespace
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0]
    return cut + "…"


def build_product_seo(product: dict, store_name: str, public_base_url: str) -> dict:
    """Monta dicionário com metadados SEO para a página individual de produto.

    Retorna chaves: title, description, og_image, canonical, jsonld.
    Tudo pronto para o template renderizar sem lógica adicional."""
    name = product.get("name") or "Peça"
    category = product.get("category") or ""
    description_raw = product.get("description") or ""
    sale_price = product.get("sale_price") or "0"
    promo_price = product.get("promo_price") or None
    has_promo = has_active_promo(product) if isinstance(product, dict) else False

    title_parts = [name]
    if category:
        title_parts.append(category)
    title_parts.append(store_name)
    title = " · ".join(title_parts)
    title = _truncate_text(title, 70)

    if description_raw:
        description = _truncate_text(description_raw, 160)
    else:
        description = _truncate_text(
            f"{name} — peça selecionada da {store_name}. "
            f"Tecidos honestos, modelagens estudadas.", 160
        )

    base = (public_base_url or "").rstrip("/")
    slug = product.get("slug") or str(product.get("id") or "")
    canonical = f"{base}/catalogo/{slug}" if slug else f"{base}/catalogo"
    og_image = f"{base}/public/product-image/{product.get('id')}" if product.get("id") else None

    # JSON-LD Schema.org Product (rich snippet do Google)
    effective_price = (promo_price if has_promo and promo_price else sale_price) or "0"
    try:
        price_value = float(str(effective_price).replace(",", "."))
    except (ValueError, TypeError):
        price_value = 0.0
    in_stock = (product.get("total_stock") or product.get("stock_qty") or 0)
    try:
        in_stock = int(in_stock)
    except (ValueError, TypeError):
        in_stock = 0

    jsonld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "description": _truncate_text(description_raw or name, 280),
        "url": canonical,
        "brand": {"@type": "Brand", "name": store_name},
    }
    if og_image:
        jsonld["image"] = og_image
    if category:
        jsonld["category"] = category
    if price_value > 0:
        jsonld["offers"] = {
            "@type": "Offer",
            "url": canonical,
            "priceCurrency": "BRL",
            "price": f"{price_value:.2f}",
            "availability": (
                "https://schema.org/InStock" if in_stock > 0
                else "https://schema.org/OutOfStock"
            ),
        }

    return {
        "title": title,
        "description": description,
        "og_image": og_image,
        "canonical": canonical,
        "jsonld": jsonld,
    }


def _compute_avg_fixed_expense(db) -> Decimal:
    """Average fixed expense per active product — used in pricing overhead."""
    try:
        total = db.execute(
            "SELECT COALESCE(SUM(CAST(amount AS DOUBLE PRECISION)),0) FROM expenses WHERE is_fixed=1 AND status='active'"
        ).fetchone()[0]
    except Exception:
        # is_fixed column may not exist yet (pre-migration)
        total = db.execute(
            "SELECT COALESCE(SUM(CAST(amount AS DOUBLE PRECISION)),0) FROM expenses WHERE status='active'"
        ).fetchone()[0]
    count = db.execute("SELECT COUNT(*) FROM products WHERE is_active=1").fetchone()[0] or 1
    return Decimal(str(total or 0)) / Decimal(count)


def _pricing_metrics(cost_price, target_margin_pct, sale_price, overhead: Decimal) -> dict:
    """Full pricing model: cost + overhead + variable rate → suggested price."""
    TWOPLACES = Decimal("0.01")
    try:
        cost = Decimal(str(cost_price or 0))
    except Exception:
        cost = Decimal("0")
    try:
        margin = min(Decimal(str(target_margin_pct or 60)) / 100, Decimal("0.94"))
    except Exception:
        margin = Decimal("0.60")
    var_rate = Decimal("0.05")
    denom    = max(Decimal("1") - margin - var_rate, Decimal("0.05"))
    oh       = overhead if isinstance(overhead, Decimal) else Decimal(str(overhead or 0))
    suggested = ((cost + oh) / denom).quantize(TWOPLACES, rounding="ROUND_HALF_UP")
    try:
        actual = Decimal(str(sale_price)) if sale_price else suggested
    except Exception:
        actual = suggested
    real_profit = actual - cost - oh - (actual * var_rate)
    real_margin = (real_profit / actual * 100).quantize(TWOPLACES) if actual > 0 else Decimal("0")
    return {
        "cost": cost,
        "overhead": oh,
        "suggested_price": suggested,
        "real_profit": real_profit.quantize(TWOPLACES),
        "real_margin_pct": real_margin,
        "variable_rate_pct": (var_rate * 100).quantize(TWOPLACES),
    }


def _make_sale_number() -> str:
    return "V" + utc_now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(2).upper()


def _margin_color(pct) -> str:
    """Return badge color class based on margin vs configured targets.
    Safe to call at any time — falls back to sensible defaults if DB unavailable."""
    try:
        p = float(pct)
    except Exception:
        return "muted"
    target, alert = 60.0, 30.0
    try:
        db = get_db()
        row_t = db.execute("SELECT value FROM store_settings WHERE key='target_margin_pct'").fetchone()
        row_a = db.execute("SELECT value FROM store_settings WHERE key='min_margin_alert_pct'").fetchone()
        if row_t: target = float(row_t["value"])
        if row_a: alert  = float(row_a["value"])
    except Exception:
        pass  # use defaults if DB not available
    if p >= target:  return "success"
    if p >= alert:   return "warning"
    return "danger"


def _patch_edit_product_for_price_history(app):
    """Monkey-patch the edit_product route to record price changes."""
    # This runs after register_v3_routes has already registered edit_product.
    # We add a wrapper that intercepts the POST.
    original = app.view_functions.get("edit_product")
    if not original:
        return

    from functools import wraps
    @wraps(original)
    def patched_edit_product(pid):
        from flask import request as req, g
        if req.method == "POST":
            db = get_db()
            p  = db.execute("SELECT * FROM products WHERE id=%s", (pid,)).fetchone()
            if p:
                old_cost = str(p["cost_price"])
                old_sale = str(p["sale_price"])
                result   = original(pid)
                # Check if prices changed after the edit
                p2 = db.execute("SELECT * FROM products WHERE id=%s", (pid,)).fetchone()
                if p2:
                    user = get_current_user()
                    uname = user["display_name"] if user else "sistema"
                    try:
                        new_cost = str(p2["cost_price"]); new_sale = str(p2["sale_price"])
                        if old_cost != new_cost:
                            db.execute("""INSERT INTO price_history(product_id,field,old_value,new_value,changed_by,created_at)
                                VALUES(%s,%s,%s,%s,%s,%s)""", (pid,"cost_price",old_cost,new_cost,uname,utc_now_iso()))
                            db.commit()
                        if old_sale != new_sale:
                            db.execute("""INSERT INTO price_history(product_id,field,old_value,new_value,changed_by,created_at)
                                VALUES(%s,%s,%s,%s,%s,%s)""", (pid,"sale_price",old_sale,new_sale,uname,utc_now_iso()))
                            db.commit()
                    except Exception:
                        pass
                return result
        return original(pid)

    # Após Onda 2/G: edit_product foi extraído para blueprints/products.py.
    # O endpoint existe sob DOIS nomes em app.view_functions:
    #   - "edit_product"          (alias curto registrado por init_products_blueprint)
    #   - "products.edit_product" (nome prefixado do blueprint)
    # Ambos apontam para a MESMA função. Substituir nos dois nomes.
    app.view_functions["edit_product"] = patched_edit_product
    if "products.edit_product" in app.view_functions:
        app.view_functions["products.edit_product"] = patched_edit_product


# ── Override create_app again ────────────────────────────────



# ── Jinja date filters (needed by calendar.html) ────────────
from datetime import date as _date_cls
def _as_date_filter(v):
    if not v: return _date_cls(2000,1,1)
    if isinstance(v, _date_cls): return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except Exception:
        return _date_cls(2000,1,1)

def _format_br_date(v):
    try:
        d = _as_date_filter(v)
        meses = ["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"]
        return f"{d.day:02d} {meses[d.month-1]} {d.year}"
    except Exception:
        return str(v)


app = create_app()
app.jinja_env.filters["as_date"] = _as_date_filter
app.jinja_env.filters["format_br_date"] = _format_br_date

# ── Lookbook blueprint registration ─────────────────────────
# Templates (base.html) referenciam url_for('lookbook.list_looks').
# Falha ao registrar é falha de boot — não engolir exceções aqui.
from lookbook import lookbook_bp, init_lookbook_db  # noqa: E402

if "lookbook" not in app.blueprints:
    app.register_blueprint(lookbook_bp)

# Inicialização de schema é OPT-IN.
# - Em produção/PaaS: rode `flask init-lookbook-db` no release-step (ou
#   defina LOOKBOOK_INIT_DB=1 para migração automática transitória).
# - Em testes/CI sem banco: permanece silente, permitindo import da app
#   sem dependência de Postgres ativo.
@app.cli.command("init-lookbook-db")
def _cli_init_lookbook_db():
    """Cria/atualiza tabelas do módulo Lookbook (idempotente)."""
    init_lookbook_db(app)
    print("Lookbook: schema verificado.")

if os.environ.get("LOOKBOOK_INIT_DB") == "1":
    init_lookbook_db(app)


# ── Auth blueprint registration ─────────────────────────────
# Rotas extraídas em Onda 2/Etapa B — blueprints/auth.py.
# Endpoints preservam nomes globais (login, logout, sudo, ...) para
# compatibilidade com templates existentes.
from blueprints.auth import auth_bp, init_auth_blueprint  # noqa: E402

if "auth" not in app.blueprints:
    app.register_blueprint(auth_bp)

init_auth_blueprint(app)


# ── Public blueprint registration ───────────────────────────
# Rotas do catálogo público extraídas em Onda 2/Etapa C —
# blueprints/public.py. Endpoints preservam nomes globais
# (public_catalog, public_product, public_sitemap, ...) para
# compatibilidade com templates legados.
from blueprints.public import public_bp, init_public_blueprint  # noqa: E402

if "public" not in app.blueprints:
    app.register_blueprint(public_bp)

init_public_blueprint(app)


# ── Admin blueprint registration ────────────────────────────
# Rotas administrativas (users, settings, hero-images, pix-accounts,
# security audit, backup) extraídas em Onda 2/Etapa D —
# blueprints/admin.py. Endpoints preservam nomes globais
# (list_users, store_settings_view, ...) para compatibilidade.
from blueprints.admin import admin_bp, init_admin_blueprint  # noqa: E402

if "admin" not in app.blueprints:
    app.register_blueprint(admin_bp)

init_admin_blueprint(app)


# ── Marketing blueprint registration ────────────────────────
# Vale-presente, cupons e campanhas extraídos em Onda 2/Etapa E —
# blueprints/marketing.py. Endpoints preservam nomes globais
# (list_cards, list_coupons, list_campaigns, ...) para compatibilidade.
from blueprints.marketing import marketing_bp, init_marketing_blueprint  # noqa: E402

if "marketing" not in app.blueprints:
    app.register_blueprint(marketing_bp)

init_marketing_blueprint(app)


# ── Customers blueprint registration ────────────────────────
# Clientes, créditos, aniversários e interesses extraídos em
# Onda 2/Etapa F — blueprints/customers.py. Endpoints preservam
# nomes globais (list_customers, birthdays, interest_dashboard, ...).
from blueprints.customers import customers_bp, init_customers_blueprint  # noqa: E402

if "customers" not in app.blueprints:
    app.register_blueprint(customers_bp)

init_customers_blueprint(app)


# ── Products blueprint registration ─────────────────────────
# Produtos, variantes, galeria, reposição e ABC extraídos em
# Onda 2/Etapa G — blueprints/products.py. Endpoints preservam
# nomes globais (list_products, edit_product, ...).
from blueprints.products import products_bp, init_products_blueprint  # noqa: E402

if "products" not in app.blueprints:
    app.register_blueprint(products_bp)

init_products_blueprint(app)

# Patch de histórico de preço sobre edit_product — DEVE rodar APÓS
# init_products_blueprint para que o alias `edit_product` exista no
# app.view_functions. Antes da Onda 2/G, este patch rodava dentro
# de create_app(); agora migrou para cá pela mesma razão.
_patch_edit_product_for_price_history(app)


# ── Sales blueprint registration ────────────────────────────
# Vendas, PIX, pré-nota e pendências extraídas em Onda 2/Etapa H —
# blueprints/sales.py. Endpoints preservam nomes globais
# (list_sales, create_sale, sale_detail, ...).
from blueprints.sales import sales_bp, init_sales_blueprint  # noqa: E402

if "sales" not in app.blueprints:
    app.register_blueprint(sales_bp)

init_sales_blueprint(app)


# ── Operations blueprint registration ───────────────────────
# PDV (redeem, atender), financeiro (accounting, expenses, pricing),
# notificações, relatórios, metas, calendário comercial — extraídos
# em Onda 2/Etapa I (fechamento). Endpoints preservam nomes globais.
from blueprints.operations import operations_bp, init_operations_blueprint  # noqa: E402

if "operations" not in app.blueprints:
    app.register_blueprint(operations_bp)

init_operations_blueprint(app)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)
