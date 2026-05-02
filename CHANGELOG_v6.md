# CHANGELOG v6 — Hardening + UX usuários + PIX (multi-PSP) + Temas

## ✦ Resumo

Sobre a base v3:
- Hardening de gestão de usuários (HMAC role, audit, sudo, MFA-ready).
- UX da tela de usuários (busca, filtros, delete, ações inline).
- PIX dinâmico copia-e-cola (Fase 1).
- **Multi-PSP com roteamento por venda** (Fase 2 parcial — Inter implementado).
- **7 temas sazonais** (manual, núcleo da marca preservado).

---

## 1. Variáveis de ambiente novas (Railway)

| Variável | Obrigatória | Descrição |
|---|---|---|
| `ROLE_INTEGRITY_PEPPER` | Recomendada | HMAC do role do usuário (32+ chars random) |
| `PSP_CREDENTIALS_KEY` | **Sim, se usar Inter** | Cifragem das credenciais PSP (32+ chars random, Fernet) |
| `INTER_BASE_URL` | Não | Override do endpoint Inter (default: produção) |

Gerar:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

---

## 2. Multi-PSP — arquitetura

### Tabelas novas

**`pix_provider_accounts`** — múltiplas contas, credenciais cifradas.
- `id, provider, label, is_active, is_default, credentials_encrypted, settings_json, created_at, updated_at`

**`pix_charges`** — desacopla cobrança de venda.
- `id, sale_id, account_id, provider, provider_charge_id, txid, amount, brcode, status, paid_at, paid_amount, payer_name, raw_webhook, created_at, updated_at`
- Status: `pending | paid | expired | cancelled | refunded`
- UNIQUE em `(provider, provider_charge_id)`.

### Módulo `pix_providers/`

```
pix_providers/
├── __init__.py    # registry, build_provider()
├── base.py        # PixProvider (ABC), ChargeRequest, ChargeResult, StatusResult
├── manual.py      # Sem PSP — só copia-e-cola
└── inter.py       # Banco Inter PJ (REST + OAuth + mTLS)
```

**Mercado Pago entra como `mercadopago.py` quando você abrir a conta.**

### Provider Inter — detalhes técnicos

- OAuth 2.0 client_credentials com mTLS (cert .p12 → cert.pem + key.pem).
- Token cacheado em memória (TTL 1h, safety 30s).
- Endpoints: `PUT /pix/v2/cob/{txid}` (cria), `GET` (consulta), webhook `POST` parseando `data["pix"][0]`.
- TXID normalizado para 26-35 chars alfanuméricos (exigência Inter).
- Status mapeado: `ATIVA→pending`, `CONCLUIDA→paid`, `REMOVIDA_PELO_PSP→expired`, etc.
- Cert/key extraídos do .p12 e gravados como textareas no form (cifrados Fernet no BD).

### Como cadastrar a conta Inter

1. Internet Banking PJ Inter → Aplicações → Cobrança PIX.
2. Crie a aplicação com escopos `cob.write cob.read webhook.write webhook.read`.
3. Baixe o .p12 emitido pelo Inter.
4. Extraia PEM:
   ```bash
   openssl pkcs12 -in inter.p12 -nokeys -out cert.pem -nodes
   openssl pkcs12 -in inter.p12 -nocerts -out key.pem -nodes
   ```
5. Em `/settings/pix-accounts/new`, escolha **Banco Inter PJ**, cole client_id/secret/cert_pem/key_pem/pix_key.
6. Configure webhook no Inter apontando para:
   ```
   https://seu-app.up.railway.app/webhooks/pix/inter
   ```
7. Faça uma venda de teste e use o QR.

### Rotas

| Rota | Método | Descrição |
|---|---|---|
| `/settings/pix-accounts` | GET | Lista de contas |
| `/settings/pix-accounts/new` | GET, POST | Cadastro (Manual ou Inter) |
| `/settings/pix-accounts/<id>/toggle` | POST | Ativa/desativa |
| `/settings/pix-accounts/<id>/delete` | POST | Apaga (bloqueado se houver cobrança pending) |
| `/sales/<id>/charge-pix` | POST | Cria cobrança (idempotente por sale+account) |
| `/pix-charges/<id>/qr.png` | GET | QR PNG da cobrança |
| `/pix-charges/<id>/status` | GET | Polling — sincroniza com PSP |
| `/pix-charges/<id>/mark-paid` | POST | Marca como pago manualmente |
| `/webhooks/pix/<provider>` | POST | Recebe notificação do PSP, valida e atualiza status |

### Fluxo na tela de venda

1. Operador abre venda — vê dropdown de contas ativas.
2. Conta padrão pré-selecionada.
3. Clica "Gerar QR PIX" → POST a `/sales/<id>/charge-pix?account_id=N`.
4. Tela recarrega mostrando QR + copia-e-cola + chip de status.
5. Polling a cada 5s (máx 12min) atualiza o chip.
6. Quando muda para `paid`, página recarrega.
7. Botão "Marcar como pago manualmente" sempre disponível como override.

### Idempotência

`/sales/<id>/charge-pix` reusa a cobrança `pending` existente para a mesma venda+conta. Não duplica.

### Webhook genérico

`/webhooks/pix/<provider>` percorre as contas ativas do provider, chama `verify_webhook` em cada, na primeira que validar parseia o body e atualiza a `pix_charge` correspondente. Idempotente: se já está `paid`, ignora.

---

## 3. Temas sazonais (manual)

7 temas: **Padrão Tonton · Primavera · Verão · Outono · Inverno · Dia das Mães · Black Friday · Natal**.

### Como funciona

- CSS variable `[data-theme="slug"]` aplicada em `<html>` via `base.html`.
- Núcleo da marca (`coral`, `coral-deep`, `magenta`, `plum`) **nunca muda**.
- Acentos sazonais (`peach`, `coral-glow`, `rose`, `grad-soft`, `grad-mesh`) são sobrescritos.
- Chip discreto no topbar do painel interno (clicável, leva ao Settings).
- Catálogo público recebe gradient e ornamentos.

### Como ativar

Settings → Operação → "Tema visual" → escolher → Salvar.

---

## 4. Hardening de usuários (do v4/v5)

Mantido. Resumo:
- HMAC do role com pepper dedicado (`ROLE_INTEGRITY_PEPPER`).
- `session_version` (revogação real).
- Audit log imutável.
- Sudo mode (re-auth 5min).
- Lockout persistente.
- Travas: último admin, auto-exclusão, auto-rebaixamento.
- UI: busca, filtros, delete inativo com `DELETE`, modal.

---

## 5. PIX Fase 1 (do v5) — preservado

A Fase 1 (copia-e-cola simples sem PSP) continua disponível para quem não quer cadastrar PSP. A Fase 2 (multi-PSP) **é independente e opcional**: se nenhuma conta for cadastrada em `/settings/pix-accounts`, a tela de venda mostra um CTA para cadastrar.

---

## 6. Validação

- `ast.parse` em `app.py` ✓
- Jinja2 parse em 10 templates ✓
- `ast.parse` em todos os providers ✓
- Lib `requests` (já em requirements.txt) usada apenas no Inter

### Pendentes

- Rodar `pytest tests/test_smoke.py` contra Postgres real.
- Testar Inter sandbox com sua conta real antes de produção.
- Webhook do Inter precisa de URL pública HTTPS (Railway já fornece).

---

## 7. Pendências conhecidas

1. **Mercado Pago** — abrir conta, depois implemento `mercadopago.py` (~1 mensagem).
2. **MFA TOTP** — colunas criadas, sem implementação ainda.
3. **PostgreSQL privilege separation** — `UPDATE` direto na coluna `role` (próximo: stored function).
4. **Auto-data dos temas** — você optou por manual; se mudar de ideia, é fácil adicionar.

---

## 8. Compatibilidade

- Backward-compatible. Migrations idempotentes.
- Sem PSP cadastrado, comportamento da Fase 1 é preservado.
- Tema "Padrão Tonton" (slug vazio) é o default — nenhum visual muda automaticamente após o upgrade.
