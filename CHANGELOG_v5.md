# CHANGELOG v5 — Hardening de segurança + UX usuários + PIX dinâmico

## ✦ Resumo

Sobre a base v3: hardening de gestão de usuários, UX da tela de
usuários, e PIX dinâmico copia-e-cola na tela de venda.

---

## 1. Segurança da gestão de usuários

### Variáveis de ambiente (Railway)

| Variável | Obrigatória? | Descrição |
|---|---|---|
| `ROLE_INTEGRITY_PEPPER` | Recomendada | Pepper exclusivo do HMAC do role. Mín. 32 chars random. Sem ela, deriva de `SECRET_KEY`. |

Gerar:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Migrations idempotentes (auto no boot)

**Colunas novas em `users`:**
- `session_version INTEGER NOT NULL DEFAULT 1` — revogação server-side de sessões.
- `role_hmac TEXT` — HMAC sobre `(id, role, session_version)`.
- `mfa_secret TEXT`, `mfa_enabled SMALLINT` — reserva para TOTP.

**Tabelas novas:**
- `security_audit` — log imutável (at, actor, target, action, before, after, ip, user_agent).
- `auth_failures` — lockout persistente, multi-worker, sobrevive deploys.

**Bootstrap:** `role_hmac` populado automaticamente para usuários existentes.

### Camadas implementadas

1. **HMAC do role validado em todo request.** Edição direta do BD sem o pepper → sessão derrubada + alerta `role_hmac_mismatch`.
2. **`session_version` bump** ao trocar senha, role ou status — derruba todas as sessões vivas.
3. **Audit log** em login_success/failure/locked, user_created/deactivated/reactivated/deleted, role_changed, password_changed, password_reset_requested, sudo_granted/denied, role_hmac_mismatch.
4. **Sudo mode** — re-auth de 5 min para criar/apagar/promover/toggle/reset.
5. **Lockout persistente** no Postgres (fallback in-memory se BD off).
6. **Travas operacionais**: bloqueio do último admin, auto-exclusão, auto-rebaixamento. Delete só de inativos, com confirmação `DELETE`.

### Rotas

| Rota | Decoradores |
|---|---|
| `/sudo` | `@login_required` |
| `/users/<id>/delete` | `admin` + `sudo` |
| `/users/<id>/promote` | `admin` + `sudo` |
| `/security/audit` | `admin` |
| `/users/new`, `/users/<id>/toggle`, `/users/<id>/send-reset` | + `sudo` |

---

## 2. UX dos templates

### `create_user.html`
- Cards de perfil com `:has(input[type=radio]:checked)`.
- Estado selecionado: ring + fundo + ícone de check; cor por perfil (operator coral, admin magenta).

### `users.html`
- KPIs de ativos / admins / inativos.
- Busca instantânea + filtros (Todos · Ativos · Inativos · Admins).
- Chip "VOCÊ", role badges destacados.
- Ações inline por linha (promote · reset · toggle · delete).
- Modal de delete com digitação literal `DELETE`.

### `security_audit.html` (nova)
- KPIs (total, mudanças sensíveis, alertas críticos com pulse).
- Busca + filtro dropdown por ação.
- Action tags coloridos por severidade.

### `sudo.html` (nova)
- Tela minimalista de re-autenticação.

### `base.html`
- Adicionado ícone `i-shield`.

---

## 3. PIX dinâmico copia-e-cola (Fase 1)

### Como funciona

Sem PSP, sem taxa, sem webhook. Gera o BR Code EMV (padrão BACEN) com
valor da venda e `txid` = `sale_number`. Cliente paga lendo o QR; operador
confirma manualmente no app do banco.

### Configuração (Settings → Operação → PIX)

| Campo | Obrigatório | Notas |
|---|---|---|
| Tipo da chave | Sim | CPF · CNPJ · E-mail · Telefone · EVP |
| Chave PIX | Sim | Validada por tipo (CPF 11 dígitos, CNPJ 14, etc) |
| Nome do recebedor | Não | Padrão: razão social ou nome da loja. Max 25 ASCII. |
| Cidade | Não | Padrão: cidade do endereço fiscal. Max 15 ASCII. |

### Módulo `pix.py`

- Gerador EMV puro, **zero dependência externa**.
- CRC16-CCITT in-house (poly 0x1021, init 0xFFFF).
- Validação por tipo, sanitização ASCII automática (acentos removidos), uppercase forçado.
- Renderização PNG via biblioteca `qrcode` (já no `requirements.txt`).
- Validado: estrutura EMV, CRC roundtrip, casos de borda (chave inválida, valor sem amount, acentos, masks).

### Rotas

| Rota | Retorno |
|---|---|
| `GET /sales/<id>/pix.txt` | text/plain com BR Code |
| `GET /sales/<id>/pix.png` | image/png do QR |
| `GET /sales/<id>/pix-info.json` | JSON `{ok, brcode, amount, txid, merchant_name, qr_url}` |

Todas exigem `@login_required`. Erros: 400 (validação), 404 (PIX não configurado).

### `sale_detail.html`

Card PIX após Totais. Estados:
1. **Auto-load** se `payment_method == 'pix'`.
2. **Botão "Gerar QR PIX desta venda"** se for outro método (operador pode oferecer PIX mesmo assim).
3. **Erro de configuração** com link direto para Settings.
4. **Resultado**: QR + recebedor + valor + textarea com copia-e-cola + botão "Copiar" (Clipboard API com fallback).

### Fora do escopo desta versão (Fase 2)

- Confirmação automática via PSP (Mercado Pago / Asaas / Efí).
- A arquitetura está pronta para receber: campo `provider` em tabela de cobranças, status `pending → paid` via webhook.
- Quando você abrir conta no PSP, eu pluggo a integração sem mexer na UI.

---

## 4. Compatibilidade

- Backward-compatible. Migrations idempotentes.
- Sem breaking changes em rotas existentes.
- Lockout faz fallback in-memory se Postgres indisponível.
- PIX só aparece se admin cadastrar a chave em Settings — invisível por padrão.

---

## 5. Validação

- **Sintaxe Python**: `ast.parse` ✓
- **Templates Jinja2**: parse de 7 arquivos ✓
- **Módulo PIX**: testes de roundtrip + 4 casos de erro + ASCII + truncamento ✓

### Testes ainda pendentes (rodar antes de subir)

```bash
export DATABASE_URL='postgresql://...'
psql $DATABASE_URL -f schema_pg.sql
pytest tests/test_smoke.py -v
```

A suíte existente cobre o BD, não cobre os fluxos novos. Recomendado
adicionar testes específicos antes do próximo sprint.

---

## 6. Pendências conhecidas

1. **MFA TOTP** — colunas criadas, sem implementação.
2. **PostgreSQL privilege separation** — app ainda tem `UPDATE` direto na coluna `role`. Próximo: stored function `set_user_role()` que aplica + audita.
3. **PIX Fase 2** — webhook de PSP. Aguarda você abrir conta.
4. **Cleanup `auth_failures`** — atualmente oportunístico. Job dedicado se volume crescer.
