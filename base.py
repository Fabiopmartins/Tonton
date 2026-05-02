"""
pix_providers/inter.py - Banco Inter PJ.

Documentacao: https://developers.inter.co/references/pix

Fluxo:
1. POST /oauth/v2/token  (client_credentials, scope=cob.write cob.read webhook.write webhook.read)
   -> retorna access_token (TTL 60min). Cachear em memoria.
2. PUT /pix/v2/cob/{txid}  -> cria cobranca imediata
3. GET /pix/v2/cob/{txid}  -> consulta status
4. PUT /pix/v2/webhook/{chave} -> registra webhook
5. Webhook entra em POST {url} com payload {pix: [{endToEndId, txid, valor, ...}]}

Auth: mTLS com certificado .p12 emitido pelo Inter Internet Banking PJ.
Token Bearer obtido via client_credentials.

Credenciais necessarias (gravadas cifradas em pix_provider_accounts.credentials_encrypted):
{
  "client_id": "...",
  "client_secret": "...",
  "cert_pem": "-----BEGIN CERTIFICATE----- ...",   # extraido do .p12
  "key_pem":  "-----BEGIN PRIVATE KEY----- ...",   # extraido do .p12
  "pix_key":  "chave PIX vinculada a conta Inter",
  "webhook_secret": "<usado para validar header customizado, opcional>"
}

Para extrair PEM do .p12 (admin faz uma vez):
    openssl pkcs12 -in inter.p12 -nokeys -out cert.pem -nodes
    openssl pkcs12 -in inter.p12 -nocerts -out key.pem -nodes

Por seguranca: nunca logar credenciais. Em erros, a mensagem ao admin
deve ser generica - detalhe vai apenas para o logger.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any

from .base import (
    PixProvider,
    ChargeRequest,
    ChargeResult,
    StatusResult,
    ProviderError,
    ProviderNotConfigured,
)


log = logging.getLogger(__name__)

# Endpoints. Producao por padrao; sandbox via env INTER_BASE_URL.
DEFAULT_BASE_URL = "https://cdpj.partners.bancointer.com.br"


@dataclass
class _TokenCache:
    token: str
    expires_at: float  # epoch seconds

    def valid(self) -> bool:
        return self.token and time.time() < (self.expires_at - 30)  # 30s safety


class InterProvider(PixProvider):
    """Banco Inter PJ - PIX Cobranca Imediata."""

    name = "inter"

    def __init__(self, credentials: dict, settings: dict | None = None):
        if not credentials:
            raise ProviderNotConfigured("Credenciais Inter ausentes.")
        required = ["client_id", "client_secret", "cert_pem", "key_pem", "pix_key"]
        missing = [k for k in required if not credentials.get(k)]
        if missing:
            raise ProviderNotConfigured(f"Credenciais Inter incompletas: {', '.join(missing)}")
        self.creds = credentials
        self.settings = settings or {}
        self.base_url = (
            self.settings.get("base_url")
            or os.environ.get("INTER_BASE_URL", "")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self._token: _TokenCache | None = None
        self._cert_files: tuple[str, str] | None = None

    # ----- mTLS: escreve cert+key em arquivos temporarios (requests precisa de path) -----
    def _ensure_cert_files(self) -> tuple[str, str]:
        if self._cert_files:
            cert_path, key_path = self._cert_files
            if os.path.exists(cert_path) and os.path.exists(key_path):
                return self._cert_files
        # Cria arquivos temp 600. Limpa no __del__ (best-effort).
        cert_fd, cert_path = tempfile.mkstemp(prefix="inter_cert_", suffix=".pem")
        key_fd, key_path = tempfile.mkstemp(prefix="inter_key_", suffix=".pem")
        try:
            os.write(cert_fd, self.creds["cert_pem"].encode("utf-8"))
            os.write(key_fd, self.creds["key_pem"].encode("utf-8"))
        finally:
            os.close(cert_fd)
            os.close(key_fd)
        os.chmod(cert_path, 0o600)
        os.chmod(key_path, 0o600)
        self._cert_files = (cert_path, key_path)
        return self._cert_files

    def __del__(self):
        if self._cert_files:
            for p in self._cert_files:
                try:
                    os.unlink(p)
                except Exception:
                    pass

    # ----- Token OAuth -----
    def _get_token(self) -> str:
        if self._token and self._token.valid():
            return self._token.token
        import requests
        cert = self._ensure_cert_files()
        try:
            resp = requests.post(
                f"{self.base_url}/oauth/v2/token",
                data={
                    "client_id": self.creds["client_id"],
                    "client_secret": self.creds["client_secret"],
                    "grant_type": "client_credentials",
                    "scope": "cob.write cob.read webhook.write webhook.read",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                cert=cert,
                timeout=15,
            )
        except requests.exceptions.RequestException as exc:
            log.error("Inter token network error: %s", exc)
            raise ProviderError("Falha de rede ao autenticar no Inter.")
        if resp.status_code != 200:
            log.error("Inter token error %s: %s", resp.status_code, resp.text[:300])
            raise ProviderError("Inter rejeitou as credenciais.")
        data = resp.json()
        token = data.get("access_token")
        ttl = int(data.get("expires_in", 3600))
        if not token:
            raise ProviderError("Resposta de token Inter sem access_token.")
        self._token = _TokenCache(token=token, expires_at=time.time() + ttl)
        return token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ----- Operacoes principais -----
    def create_charge(self, req: ChargeRequest) -> ChargeResult:
        import requests
        # Inter exige txid 26-35 chars alfanumericos. Garante.
        txid = self._normalize_txid(req.txid)
        amount_str = f"{Decimal(str(req.amount)).quantize(Decimal('0.01'))}"
        payload = {
            "calendario": {"expiracao": int(req.expires_in_seconds)},
            "valor": {"original": amount_str},
            "chave": self.creds["pix_key"],
            "solicitacaoPagador": (req.description or "Pagamento")[:140],
        }
        if req.payer_document:
            doc_clean = "".join(c for c in req.payer_document if c.isdigit())
            if len(doc_clean) == 11:
                payload["devedor"] = {"cpf": doc_clean, "nome": req.payer_name or "Pagador"}
            elif len(doc_clean) == 14:
                payload["devedor"] = {"cnpj": doc_clean, "nome": req.payer_name or "Pagador"}

        cert = self._ensure_cert_files()
        try:
            resp = requests.put(
                f"{self.base_url}/pix/v2/cob/{txid}",
                json=payload,
                headers=self._headers(),
                cert=cert,
                timeout=15,
            )
        except requests.exceptions.RequestException as exc:
            log.error("Inter create_charge network error: %s", exc)
            raise ProviderError("Falha de rede ao criar cobranca no Inter.")
        if resp.status_code not in (200, 201):
            log.error("Inter create_charge %s: %s", resp.status_code, resp.text[:500])
            raise ProviderError(f"Inter rejeitou a cobranca (HTTP {resp.status_code}).")
        data = resp.json()
        # Inter retorna pixCopiaECola e location (URL). loc.id = identificador
        # numerico para gerar o QR via /loc/{id}/qrcode.
        brcode = data.get("pixCopiaECola") or ""
        loc = data.get("loc") or {}
        return ChargeResult(
            provider=self.name,
            provider_charge_id=txid,
            brcode=brcode,
            qr_url=loc.get("location"),
            status=self._map_status(data.get("status")),
            expires_at=self._compute_expiry(data, req.expires_in_seconds),
            raw=data,
        )

    def get_charge(self, provider_charge_id: str) -> StatusResult:
        import requests
        cert = self._ensure_cert_files()
        try:
            resp = requests.get(
                f"{self.base_url}/pix/v2/cob/{provider_charge_id}",
                headers=self._headers(),
                cert=cert,
                timeout=15,
            )
        except requests.exceptions.RequestException as exc:
            log.error("Inter get_charge network error: %s", exc)
            raise ProviderError("Falha de rede ao consultar Inter.")
        if resp.status_code == 404:
            raise ProviderError("Cobranca nao encontrada no Inter.")
        if resp.status_code != 200:
            log.error("Inter get_charge %s: %s", resp.status_code, resp.text[:300])
            raise ProviderError(f"Inter retornou HTTP {resp.status_code}.")
        data = resp.json()
        status = self._map_status(data.get("status"))
        # Pix recebido aparece em data["pix"] (lista). Pegar o primeiro pago.
        paid_at = None
        paid_amount = None
        payer_name = None
        for pix in (data.get("pix") or []):
            if pix.get("horario"):
                paid_at = pix["horario"]
                try:
                    paid_amount = Decimal(str(pix.get("valor", "0")))
                except Exception:
                    pass
                payer_name = (pix.get("pagador") or {}).get("nome")
                break
        return StatusResult(
            provider=self.name,
            provider_charge_id=provider_charge_id,
            status=status,
            paid_at=paid_at,
            paid_amount=paid_amount,
            payer_name=payer_name,
            raw=data,
        )

    # ----- Webhook -----
    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        """Inter usa mTLS para o webhook tambem (mesma cadeia de cert).
        Em producao, valide na camada de proxy/Railway que a request veio
        com client cert do Inter. Aqui validamos um secret opcional no
        header X-Inter-Webhook-Secret se configurado."""
        expected = (self.creds.get("webhook_secret") or "").strip()
        if not expected:
            # Sem secret configurado: aceita. mTLS deve ser garantido upstream.
            return True
        received = headers.get("X-Inter-Webhook-Secret", "")
        return bool(received) and received == expected

    def parse_webhook(self, headers: dict, body: bytes) -> StatusResult | None:
        """Payload do Inter: {"pix": [{"endToEndId","txid","valor","horario","pagador":{...}}]}.
        Retorna o primeiro pix da lista (geralmente vem 1)."""
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            log.warning("Webhook Inter: JSON invalido")
            return None
        pix_list = data.get("pix") or []
        if not pix_list:
            return None
        first = pix_list[0]
        txid = first.get("txid")
        if not txid:
            return None
        try:
            amount = Decimal(str(first.get("valor", "0")))
        except Exception:
            amount = None
        return StatusResult(
            provider=self.name,
            provider_charge_id=txid,
            status="paid",  # webhook do Inter so notifica pagamento confirmado
            paid_at=first.get("horario"),
            paid_amount=amount,
            payer_name=(first.get("pagador") or {}).get("nome"),
            raw=data,
        )

    # ----- Helpers internos -----
    @staticmethod
    def _normalize_txid(raw: str) -> str:
        """Inter exige txid alfanumerico, 26-35 chars."""
        cleaned = "".join(c for c in (raw or "") if c.isalnum())
        if len(cleaned) >= 26:
            return cleaned[:35]
        # Pad com 'M' ate 26 - melhor que zero a esquerda para legibilidade
        return (cleaned + "M" * 35)[:max(26, len(cleaned) + 26)][:35]

    @staticmethod
    def _map_status(inter_status: str | None) -> str:
        """Inter -> normalizado.
        ATIVA / CONCLUIDA / REMOVIDA_PELO_USUARIO_RECEBEDOR / REMOVIDA_PELO_PSP"""
        m = {
            "ATIVA": "pending",
            "CONCLUIDA": "paid",
            "REMOVIDA_PELO_USUARIO_RECEBEDOR": "cancelled",
            "REMOVIDA_PELO_PSP": "expired",
        }
        return m.get((inter_status or "").upper(), "pending")

    @staticmethod
    def _compute_expiry(data: dict, fallback_seconds: int) -> str | None:
        cal = data.get("calendario") or {}
        criacao = cal.get("criacao")
        expiracao = cal.get("expiracao") or fallback_seconds
        if criacao:
            try:
                dt = datetime.fromisoformat(criacao.replace("Z", "+00:00"))
                return (dt + timedelta(seconds=int(expiracao))).isoformat()
            except Exception:
                pass
        return (datetime.now(timezone.utc) + timedelta(seconds=int(expiracao))).isoformat()
