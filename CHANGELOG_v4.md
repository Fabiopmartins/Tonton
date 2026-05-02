# CHANGELOG v4 — Hardening de segurança + UX de usuários

## ✦ Resumo

Endurecimento da gestão de usuários sem refatorar a aplicação:
defesa em profundidade contra adulteração do BD, revogação real de
sessão, audit log imutável, re-autenticação para ações sensíveis e
UX de seleção de perfil com feedback visual inequívoco.

---

## Novas variáveis de ambiente (Railway)

| Variável | Obrigatória? | Padrão | Descrição |
|---|---|---|---|
| `ROLE_INTEGRITY_PEPPER` | Recomendada | derivada de `SECRET_KEY` | Pepper exclusivo do HMAC do role. Permite rotacionar `SECRET_KEY` sem invalidar HMACs. **Mínimo 32 chars random.** |

Gerar:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

---

## Esquema de banco — migrations idempotentes

Aplicadas automaticamente no boot via `ensure_schema_migrations`. Não
requerem ação manual em produção.

### Colunas novas em `users`

| Coluna | Tipo | Para que serve |
|---|---|---|
| `session_version` | INTEGER NOT NULL DEFAULT 1 | Revogação server-side de sessões. Incrementar invalida todas as sessões vivas do usuário. |
| `role_hmac` | TEXT | HMAC-SHA256 sobre `(id, role, session_version)` com `ROLE_INTEGRITY_PEPPER`. Garante integridade do `role` contra edição direta no BD. |
| `mfa_secret` | TEXT | Reserva para TOTP (não implementado nesta versão). |
| `mfa_enabled` | SMALLINT NOT NULL DEFAULT 0 | Reserva para TOTP. |

### Tabelas novas

**`security_audit`** — log imutável de eventos sensíveis.
- `at, actor_id, actor_email, target_id, action, before_value, after_value, ip, user_agent, extra`
- Índices em `target_id`, `action`, `at DESC`.

**`auth_failures`** — lockout persistente, multi-worker, sobrevive deploys.
- `email, ip, at`
- Índice em `(email, ip, at DESC)`.
- Limpeza oportunística de registros > 24h.

### Bootstrap automático do `role_hmac`

Na primeira execução pós-upgrade, todos os usuários sem `role_hmac`
recebem o HMAC computado. Idempotente. Logs `role_hmac bootstrap: N usuarios`.

---

## Segurança — camadas implementadas

### 1. Integridade do `role` via HMAC
- Edição direta de `users.role` no BD sem o pepper → HMAC não bate →
  sessão derrubada no próximo request → audit log `role_hmac_mismatch`.
- Validado em **todo request autenticado** dentro de `require_role`.

### 2. Revogação real de sessão
- `bump_session_version(user_id)` chamada em:
  - troca de senha (`change_password`)
  - mudança de role (`promote_user`)
  - desativação/reativação (`toggle_user`)
- `complete_login` grava `session_version` na sessão. Mismatch → logout.

### 3. Audit log imutável
Eventos registrados:
- `login_success`, `login_failure`, `login_locked`
- `user_created`, `user_deactivated`, `user_reactivated`, `user_deleted`
- `role_changed`
- `password_changed`, `password_reset_requested`
- `sudo_granted`, `sudo_denied`
- `role_hmac_mismatch` (alerta crítico)

### 4. Sudo mode (re-autenticação)
- TTL: 5 minutos.
- Aplicado em: `create_user`, `toggle_user`, `delete_user`,
  `promote_user`, `send_user_reset`.
- Tela `/sudo` com `next=` whitelisted (apenas paths internos).

### 5. Lockout persistente
- Migrado de `dict` in-memory para tabela `auth_failures`.
- Funciona com múltiplos workers gunicorn e sobrevive deploys.
- Fallback automático para in-memory se BD indisponível.

### 6. Travas de integridade operacional
- Bloqueio de auto-desativação (já existia).
- Bloqueio de auto-exclusão.
- Bloqueio de auto-rebaixamento.
- Bloqueio de desativar/rebaixar o **último admin ativo**.
- Delete só permitido para usuários **inativos**.
- Confirmação por digitação literal `DELETE` (anti-clique acidental).

---

## UX — Telas de usuários

### `create_user.html`
- Cards de perfil com `:has(input[type=radio]:checked)`.
- Estado selecionado: ring + fundo + ícone de check.
- Cor diferenciada por perfil (operator coral, admin magenta).
- Foco visível por teclado.

### `users.html`
- KPIs: ativos, admins, **inativos**.
- Busca instantânea por nome/e-mail.
- Filtros: Todos · Ativos · Inativos · Admins.
- Chip "VOCÊ" no usuário logado.
- Role badges destacados (ADMIN com ícone).
- Ações inline em cada linha:
  - Promote/demote role (ícone shield)
  - Enviar reset de senha (ícone mail)
  - Toggle ativar/desativar (ícone check/x)
  - Apagar inativo (ícone trash, com modal)
- Linha do próprio usuário com destaque sutil.
- Linhas inativas com opacidade reduzida.
- Modal de confirmação para delete: digitar `DELETE` para liberar.

### `security_audit.html` (nova)
- KPIs: total de eventos, mudanças sensíveis, **alertas críticos**.
- Busca por ação/usuário/IP.
- Filtro dropdown por tipo de ação.
- Action tags coloridos por severidade.
- Animação `pulse` em alertas críticos (`role_hmac_mismatch`).
- Layout responsivo.

### `sudo.html` (nova)
- Tela minimalista de re-autenticação.
- Explica por que a senha está sendo pedida novamente.

---

## Rotas novas / alteradas

| Rota | Método | Decoradores |
|---|---|---|
| `/sudo` | GET, POST | `@login_required` |
| `/users/<id>/delete` | POST | `@require_role("admin")` + `@require_sudo` |
| `/users/<id>/promote` | POST | `@require_role("admin")` + `@require_sudo` |
| `/security/audit` | GET | `@require_role("admin")` |
| `/users/new` | GET, POST | (+ `@require_sudo`) |
| `/users/<id>/toggle` | POST | (+ `@require_sudo`) |
| `/users/<id>/send-reset` | POST | (+ `@require_sudo`) |

---

## Testes

- Sintaxe Python validada (`ast.parse`).
- Templates Jinja2 validados (parse de todos os 5 arquivos OK).
- **Pendente:** rodar `pytest tests/test_smoke.py` contra Postgres real
  com `DATABASE_URL` antes de subir. Os testes existentes não cobrem o
  novo fluxo; recomendado adicionar.

---

## Pendências conhecidas (próximo sprint)

1. **MFA TOTP** — colunas `mfa_secret` e `mfa_enabled` criadas, mas sem
   implementação. Próximo: `pyotp` + tela de setup com QR code.
2. **Rotação periódica de `auth_failures`** — limpeza atual é
   oportunística. Considerar job dedicado se volume crescer.
3. **PostgreSQL: privilege separation** — a aplicação ainda conecta com
   um role com `UPDATE` direto na coluna `role`. Próximo: stored function
   `set_user_role(target_id, new_role)` que registra no audit e aplica a
   mudança, com role da app sem `UPDATE` direto.
4. **Testes específicos** do fluxo de auditoria/sudo/HMAC mismatch.

---

## Compatibilidade

- Backward-compatible. Migrations idempotentes.
- Bootstrap automático de `role_hmac` para usuários existentes.
- Sem breaking changes em rotas existentes; rotas novas convivem com as antigas.
- Fallback de lockout para in-memory se BD indisponível em runtime.
