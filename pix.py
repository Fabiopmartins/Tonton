"""
pix.py - Gerador de BR Code EMV para PIX dinamico copia-e-cola.

Padrao: EMV QR Code Specification for Payment Systems (EMVCo) +
adaptacao BACEN para Pix (Manual de Padroes para Iniciacao do Pix).

Sem dependencia externa. CRC16-CCITT (poly 0x1021, init 0xFFFF) calculado
in-house. Compativel com qualquer banco brasileiro.

Limites segundo BACEN:
- Nome do recebedor (Merchant Name): ASCII, max 25 chars.
- Cidade (Merchant City): ASCII, max 15 chars.
- TxID: alfanumerico [A-Za-z0-9], max 25 chars.
- Chave PIX: max 77 chars.
- Valor: decimal com ponto, ate 13 chars (10 inteiros + ponto + 2 decimais).

Tipos de chave aceitos (validacao client-side ja deve filtrar):
- cpf:        11 digitos
- cnpj:       14 digitos
- email:      contem '@', max 77 chars
- phone:      formato +55 + DDD + numero, ex: +5511999998888
- evp:        UUID v4 (chave aleatoria)
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP


# ---------- IDs do payload EMV (apenas os usados) ----------
ID_PAYLOAD_FORMAT_INDICATOR     = "00"
ID_MERCHANT_ACCOUNT_INFORMATION = "26"  # PIX
ID_MERCHANT_CATEGORY_CODE       = "52"
ID_TRANSACTION_CURRENCY         = "53"  # 986 = BRL
ID_TRANSACTION_AMOUNT           = "54"
ID_COUNTRY_CODE                 = "58"  # BR
ID_MERCHANT_NAME                = "59"
ID_MERCHANT_CITY                = "60"
ID_ADDITIONAL_DATA_FIELD        = "62"
ID_CRC16                        = "63"

# Subcampos de "26" (Merchant Account Information - PIX)
ID_PIX_GUI = "00"   # br.gov.bcb.pix
ID_PIX_KEY = "01"
ID_PIX_TXID_PARENT = "05"  # vai dentro do "62"

PIX_GUI = "br.gov.bcb.pix"


# ---------- Helpers ----------
def _ascii_only(text: str) -> str:
    """Remove acentos e caracteres nao-ASCII (exigencia EMV/BACEN)."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    only_ascii = "".join(c for c in nfkd if not unicodedata.combining(c))
    return only_ascii.encode("ascii", "ignore").decode("ascii")


def _emv_field(field_id: str, value: str) -> str:
    """Codifica um campo EMV no formato ID + LEN(2) + VALUE."""
    if value is None:
        value = ""
    length = f"{len(value):02d}"
    return f"{field_id}{length}{value}"


def _crc16_ccitt(payload: str) -> str:
    """CRC16-CCITT-FALSE: poly 0x1021, init 0xFFFF, no xorout, no reflect.

    Padrao EMV. Resultado em hex maiusculo, 4 digitos.
    """
    crc = 0xFFFF
    for byte in payload.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return f"{crc:04X}"


# ---------- Validacao e sanitizacao ----------
def normalize_pix_key(raw: str, key_type: str) -> str:
    """Normaliza a chave conforme o tipo. Retorna a chave pronta para o
    BR Code ou levanta ValueError com mensagem amigavel."""
    raw = (raw or "").strip()
    key_type = (key_type or "").strip().lower()
    if not raw:
        raise ValueError("Chave PIX vazia.")

    if key_type == "cpf":
        digits = re.sub(r"\D", "", raw)
        if len(digits) != 11:
            raise ValueError("CPF deve ter 11 digitos.")
        return digits

    if key_type == "cnpj":
        digits = re.sub(r"\D", "", raw)
        if len(digits) != 14:
            raise ValueError("CNPJ deve ter 14 digitos.")
        return digits

    if key_type == "email":
        if "@" not in raw or len(raw) > 77:
            raise ValueError("E-mail invalido.")
        return raw.lower()

    if key_type == "phone":
        digits = re.sub(r"\D", "", raw)
        # BACEN: +55 + DDD + numero. Aceita formato sem +55 e adiciona.
        if len(digits) == 11:
            digits = "55" + digits
        elif len(digits) == 13 and digits.startswith("55"):
            pass
        else:
            raise ValueError("Telefone deve ter DDD + numero (ex: 11999998888).")
        return "+" + digits

    if key_type == "evp":
        # UUID v4 padrao 8-4-4-4-12
        candidate = raw.lower()
        if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", candidate):
            raise ValueError("Chave aleatoria deve ser um UUID valido.")
        return candidate

    raise ValueError(f"Tipo de chave desconhecido: {key_type}")


def _sanitize_merchant_name(name: str) -> str:
    """Nome do recebedor: ASCII, max 25 chars, uppercase recomendado."""
    cleaned = _ascii_only(name).strip().upper()
    cleaned = re.sub(r"[^A-Z0-9 ]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        cleaned = "RECEBEDOR"
    return cleaned[:25]


def _sanitize_city(city: str) -> str:
    """Cidade: ASCII, max 15 chars, uppercase."""
    cleaned = _ascii_only(city).strip().upper()
    cleaned = re.sub(r"[^A-Z0-9 ]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        cleaned = "BRASIL"
    return cleaned[:15]


def _sanitize_txid(txid: str) -> str:
    """TxID: alfanumerico, max 25, sem espacos. '***' significa 'sem txid'
    (valido por BACEN para QR estatico, mas usamos sempre um id real)."""
    if not txid:
        return "***"
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(txid))
    if not cleaned:
        return "***"
    return cleaned[:25]


def _format_amount(amount) -> str:
    """Valor com 2 decimais e ponto. Aceita Decimal, float, str."""
    if amount is None or amount == "":
        return ""
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount).replace(",", "."))
    if amount <= 0:
        return ""
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    formatted = f"{quantized:.2f}"
    if len(formatted) > 13:
        raise ValueError("Valor excede o limite do BR Code (R$ 9.999.999.999,99).")
    return formatted


# ---------- API publica ----------
def build_brcode(
    *,
    pix_key: str,
    key_type: str,
    merchant_name: str,
    merchant_city: str,
    amount=None,
    txid: str = "",
) -> str:
    """Gera o BR Code PIX (string copia-e-cola).

    Args:
        pix_key: chave PIX bruta (sera normalizada conforme key_type).
        key_type: cpf | cnpj | email | phone | evp.
        merchant_name: nome do recebedor (sera sanitizado).
        merchant_city: cidade (sera sanitizada).
        amount: Decimal/float/str. Se None ou 0, gera QR sem valor.
        txid: identificador da transacao (max 25 alfanumericos).

    Returns:
        String BR Code pronta para gerar QR.

    Raises:
        ValueError: se chave invalida para o tipo, ou valor fora do limite.
    """
    normalized_key = normalize_pix_key(pix_key, key_type)
    name = _sanitize_merchant_name(merchant_name)
    city = _sanitize_city(merchant_city)
    txid_clean = _sanitize_txid(txid)
    amount_str = _format_amount(amount)

    # Bloco 26: Merchant Account Information PIX
    pix_block_inner = (
        _emv_field(ID_PIX_GUI, PIX_GUI)
        + _emv_field(ID_PIX_KEY, normalized_key)
    )
    pix_block = _emv_field(ID_MERCHANT_ACCOUNT_INFORMATION, pix_block_inner)

    # Bloco 62: Additional Data Field (txid obrigatorio)
    additional_inner = _emv_field(ID_PIX_TXID_PARENT, txid_clean)
    additional_block = _emv_field(ID_ADDITIONAL_DATA_FIELD, additional_inner)

    # Montagem do payload (ordem importa, BACEN exige sequencial)
    parts = [
        _emv_field(ID_PAYLOAD_FORMAT_INDICATOR, "01"),
        pix_block,
        _emv_field(ID_MERCHANT_CATEGORY_CODE, "0000"),
        _emv_field(ID_TRANSACTION_CURRENCY, "986"),  # BRL
    ]
    if amount_str:
        parts.append(_emv_field(ID_TRANSACTION_AMOUNT, amount_str))
    parts.extend([
        _emv_field(ID_COUNTRY_CODE, "BR"),
        _emv_field(ID_MERCHANT_NAME, name),
        _emv_field(ID_MERCHANT_CITY, city),
        additional_block,
    ])
    payload_without_crc = "".join(parts) + ID_CRC16 + "04"
    crc = _crc16_ccitt(payload_without_crc)
    return payload_without_crc + crc


def render_qr_png(brcode: str, box_size: int = 8, border: int = 2) -> bytes:
    """Renderiza o BR Code como PNG. Usa a lib `qrcode` (ja em requirements).

    Returns:
        bytes do PNG.
    """
    import io
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    qr = qrcode.QRCode(
        version=None,  # auto
        error_correction=ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(brcode)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
