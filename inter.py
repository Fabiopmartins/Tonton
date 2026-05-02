"""
pix_providers - Registry e factory para providers PIX.

Uso:
    from pix_providers import build_provider
    provider = build_provider(account_row)
    result = provider.create_charge(ChargeRequest(amount=Decimal('99.90'), txid='ABC123'))
"""
from __future__ import annotations

import json
from typing import Any

from .base import (
    PixProvider,
    ChargeRequest,
    ChargeResult,
    StatusResult,
    ProviderError,
    ProviderNotConfigured,
    VALID_STATUSES,
)
from .manual import ManualProvider
from .inter import InterProvider


# Slugs aceitos. Mercado Pago entra aqui quando implementado.
PROVIDER_REGISTRY: dict[str, type[PixProvider]] = {
    "manual": ManualProvider,
    "inter": InterProvider,
}


def build_provider(account_row: dict, decrypted_credentials: dict) -> PixProvider:
    """Constroi o provider a partir de uma linha de pix_provider_accounts.

    Args:
        account_row: dict com campos provider, settings_json, label.
        decrypted_credentials: dict ja descriptografado pelo caller.

    Returns:
        Instancia de PixProvider concreta.

    Raises:
        ProviderError: se provider desconhecido.
        ProviderNotConfigured: se credenciais incompletas.
    """
    slug = (account_row.get("provider") or "").lower().strip()
    cls = PROVIDER_REGISTRY.get(slug)
    if not cls:
        raise ProviderError(f"Provider desconhecido: {slug!r}")
    settings_raw = account_row.get("settings_json") or "{}"
    try:
        settings = json.loads(settings_raw) if isinstance(settings_raw, str) else (settings_raw or {})
    except json.JSONDecodeError:
        settings = {}
    return cls(credentials=decrypted_credentials, settings=settings)


__all__ = [
    "PixProvider",
    "ChargeRequest",
    "ChargeResult",
    "StatusResult",
    "ProviderError",
    "ProviderNotConfigured",
    "VALID_STATUSES",
    "PROVIDER_REGISTRY",
    "build_provider",
]
