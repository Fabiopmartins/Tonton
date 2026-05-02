# Deploy em Railway — Tonton

Procedimento simplificado: o app aplica o schema Postgres automaticamente no primeiro boot.

**Tempo estimado:** 5-10 min.
**Pré-requisito:** conta Railway.

---

## 1. Provisionar serviços no Railway

1. Criar novo projeto no Railway.
2. Adicionar serviço **Postgres** — dashboard → "New" → "Database" → "PostgreSQL".
3. Adicionar serviço **web** apontando para este repositório — dashboard → "New" → "GitHub Repo" → selecionar.

---

## 2. Gerar secrets

**Flask secret key** (obrigatório):
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Admin password hash** (recomendado):
```bash
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('SuaSenhaForte'))"
```

**Fernet key para criptografar códigos de gift card** (recomendado):
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Anote os valores.

---

## 3. Configurar variáveis no serviço web

Railway → serviço **web** → aba "Variables".

### Obrigatórias

| Variável | Como configurar |
|---|---|
| `DATABASE_URL` | Clique em "Add Reference" → Service: `Postgres`, Variable: `DATABASE_URL`. Isso injeta a URL interna (`postgres.railway.internal`). |
| `FLASK_SECRET_KEY` | Colar o valor gerado no passo 2. |

### Recomendadas

| Variável | Valor |
|---|---|
| `ADMIN_USERNAME` | Seu email de admin |
| `ADMIN_PASSWORD_HASH` | Hash werkzeug gerado no passo 2 |
| `ENCRYPTION_KEY` | Fernet key gerada no passo 2 |
| `PUBLIC_BASE_URL` | URL pública da loja (ex: `https://male.up.railway.app`) |

### Opcionais

| Variável | Valor |
|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth Google |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | Envio de email |
| `LOG_LEVEL` | `DEBUG` para troubleshooting, `INFO` em produção |

---

## 4. Deploy

Se o repositório já está conectado, Railway faz deploy automático a cada push.

No primeiro boot após a conexão do Postgres:
1. App detecta schema ausente ou inconsistente.
2. App aplica `schema_pg.sql` automaticamente.
3. App cria usuário admin inicial.
4. App começa a servir requests.

Os logs vão mostrar:
```
INFO: Schema Postgres ausente. Aplicando schema_pg.sql automaticamente...
INFO: Schema aplicado com sucesso.
[INFO] Listening at: http://0.0.0.0:8080
```

---

## 5. Primeiro login

Acessar a URL pública da loja. Tela de login.

Credenciais:
- Se você configurou `ADMIN_USERNAME` + `ADMIN_PASSWORD_HASH` no passo 3 → use as suas.
- Caso contrário → `admin@male.local` / `Troque-esta-senha` (**troque imediatamente**).

---

## 6. Smoke test funcional

- [ ] Dashboard abre sem erro
- [ ] Criar produto com imagem (valida BYTEA)
- [ ] Criar cliente
- [ ] Criar gift card
- [ ] Registrar venda
- [ ] Cancelar venda
- [ ] Acessar relatórios
- [ ] Acessar `/interest`

---

## 7. Pós-deploy

- [ ] Monitorar logs por 30-60 min.
- [ ] Habilitar backups automáticos do Postgres (Railway plano pago).

---

## Troubleshooting

### Logs mostram `RuntimeError: schema_pg.sql não encontrado`
O arquivo `schema_pg.sql` não foi incluído no deploy. Verificar que está na raiz do repositório.

### Logs mostram `psycopg2.OperationalError: could not connect`
`DATABASE_URL` não configurada ou aponta para URL errada. Usar "Add Reference" em vez de valor hardcoded.

### Logs mostram `column ... does not exist` após deploy
O app deveria ter detectado e corrigido. Se persistir, force reset via psql:

```bash
# pegar URL pública: Railway → Postgres → Connect → Public Network
psql "$DATABASE_URL_PUB" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
# próximo boot aplica schema do zero
```

---

## Reset completo (apaga todos os dados)

O app só aplica schema quando detecta banco vazio ou inconsistente. Para forçar reaplicação com banco já válido, é preciso limpar manualmente:

```bash
psql "$DATABASE_URL_PUB" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
```

Próximo deploy vai reaplicar `schema_pg.sql`.

---

## Manutenção

**Backup:**
```bash
pg_dump "$DATABASE_URL_PUB" > backup_$(date +%Y%m%d).sql
```

**Restore:**
```bash
psql "$DATABASE_URL_PUB" < backup_YYYYMMDD.sql
```

**Tabelas com mais linhas:**
```sql
SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC;
```
