"""
pix_providers/manual.py - Sem PSP. Gera apenas o BR Code copia-e-cola.

Sem confirmacao automatica. Operador valida manualmente.
Reusa o pix.build_brcode (Fase 1) ja entregue.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone, timedelta

from .base import (
    PixProvider,
    ChargeRequest,
    ChargeResult,
    StatusResult,
    ProviderError,
    ProviderNotConfigured,
)


class ManualProvider(PixProvider):
    """Sem PSP - cobranca offline copia-e-cola."""

    name = "manual"

    def __init__(self, credentials: dict, settings: dict | None = None):
        if not credentials:
            raise ProviderNotConfigured("Configuracao manual ausente.")
        required = ["pix_key", "key_type", "merchant_name", "merchant_city"]
        missing = [k for k in required if not credentials.get(k)]
        if missing:
            raise ProviderNotConfigured(f"Configuracao manual incompleta: {', '.join(missing)}")
        self.creds = credentials

    def create_charge(self, req: ChargeRequest) -> ChargeResult:
        from pix import build_brcode  # reusa Fase 1
        try:
            brcode = build_brcode(
                pix_key=self.creds["pix_key"],
                key_type=self.creds["key_type"],
                merchant_name=self.creds["merchant_name"],
                merchant_city=self.creds["merchant_city"],
                amount=req.amount,
                txid=req.txid,
            )
        except ValueError as exc:
            raise ProviderError(str(exc))
        # provider_charge_id local: hash do txid + nonce (sem PSP, nao tem id externo)
        local_id = f"manual-{req.txid}-{secrets.token_hex(4)}"
        expires = (datetime.now(timezone.utc) + timedelta(seconds=req.expires_in_seconds)).isoformat()
        return ChargeResult(
            provider=self.name,
            provider_charge_id=local_id,
            brcode=brcode,
            qr_url=None,
            status="pending",
            expires_at=expires,
            raw={},
        )

    def get_charge(self, provider_charge_id: str) -> StatusResult:
        # Sem PSP, status nunca muda automaticamente. Retorna pending.
        # Operador atualiza manualmente via UI (rota /pix-charges/<id>/mark-paid).
        return StatusResult(
            provider=self.name,
            provider_charge_id=provider_charge_id,
            status="pending",
        )
