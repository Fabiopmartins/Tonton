"""
pix_providers/base.py - Interface abstrata para PSPs PIX.

Cada provider implementa:
- create_charge(amount, txid, description) -> dict com brcode, charge_id, expires_at
- get_charge(charge_id) -> dict com status atual
- verify_webhook(headers, body, secret) -> bool

Modelo de status interno (normalizado):
    pending  - cobranca criada, aguardando pagamento
    paid     - pagamento confirmado
    expired  - prazo de pagamento passou
    cancelled- cobranca cancelada manualmente
    refunded - estornada apos pagamento
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


VALID_STATUSES = {"pending", "paid", "expired", "cancelled", "refunded"}


@dataclass
class ChargeRequest:
    amount: Decimal
    txid: str
    description: str = ""
    payer_name: str = ""
    payer_document: str = ""
    expires_in_seconds: int = 3600


@dataclass
class ChargeResult:
    provider: str                       # 'inter' | 'mercadopago' | 'manual'
    provider_charge_id: str             # id do PSP (pode ser igual ao txid no Inter)
    brcode: str                         # copia-e-cola
    qr_url: str | None = None           # URL externa do QR (alguns PSPs fornecem)
    status: str = "pending"
    expires_at: str | None = None       # ISO 8601
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StatusResult:
    provider: str
    provider_charge_id: str
    status: str                         # normalizado
    paid_at: str | None = None
    paid_amount: Decimal | None = None
    payer_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PixProvider(ABC):
    """Interface comum. Implementacoes lancam ProviderError em falha."""

    name: str = ""

    @abstractmethod
    def create_charge(self, req: ChargeRequest) -> ChargeResult: ...

    @abstractmethod
    def get_charge(self, provider_charge_id: str) -> StatusResult: ...

    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        """Valida assinatura/origem do webhook. Override por provider."""
        return False

    def parse_webhook(self, headers: dict, body: bytes) -> StatusResult | None:
        """Extrai status normalizado do payload. Override por provider."""
        return None


class ProviderError(Exception):
    """Falha ao chamar o PSP. Sempre amigavel para flash() do Flask."""
    pass


class ProviderNotConfigured(ProviderError):
    """Credenciais ausentes ou invalidas."""
    pass
