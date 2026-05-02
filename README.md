# Tonton · Painel Administrativo

Sistema administrativo para a **Tonton Loja Infantil** — loja online de moda infantil em Timóteo/MG. Gestão de produtos (com tamanhos por faixa etária), vendas, clientes, vale-presentes, cupons, estoque, relatórios e campanhas de WhatsApp.

> Adaptado a partir do Painel `Malê`, com nova identidade visual (paleta laranja Tonton + verde mintado + amarelo sol), fonts brincalhonas (Fredoka + Nunito) e formulários ajustados para o varejo infantil (faixas etárias 2/4/6/8/10 anos no lugar de P/M/G).

**Stack:** Flask · Postgres · psycopg2 · Gunicorn. Deploy em Railway.

---

## Arquitetura

```
app.py                Aplicação Flask (rotas, lógica de negócio, templates)
db.py                 Camada de dados psycopg2 (conexão, transações, helpers)
schema_pg.sql         DDL Postgres (aplicado via psql, não pelo app)
requirements.txt      Dependências Python
Procfile              Comando de start no Railway (gunicorn app:app)
railway.toml          Configuração do builder (nixpacks)
templates/            Jinja2 templates (Jinja é o engine padrão do Flask)
static/               CSS, fontes, imagens de marca, templates de cartão
tests/                Smoke tests contra Postgres real
```

**Decisões de design:**
- Postgres-only. Sem fallback para SQLite.
- Queries SQL raw via psycopg2, sem ORM. Simples, legível, performático.
- `DATABASE_URL` é obrigatória — app falha ao iniciar sem ela.
- Schema é aplicado externamente via `psql -f schema_pg.sql`, não pelo app. Boot do app valida que o schema existe.

---

## Setup de desenvolvimento local

Pré-requisitos: Python 3.12+ e Postgres 14+ rodando local (ou Postgres remoto acessível).

```bash
# 1. Ambiente Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Criar banco local
createdb male_dev

# 3. Aplicar schema
export DATABASE_URL='postgresql://SEU_USUARIO@localhost:5432/male_dev'
psql "$DATABASE_URL" -f schema_pg.sql

# 4. Rodar smoke tests
pytest tests/test_smoke.py -v

# 5. Iniciar servidor de dev
export FLASK_SECRET_KEY='qualquer-string-longa-aleatoria'
python3 app.py
# ou: gunicorn app:app
```

Login inicial (primeiro acesso):
- Usuário: `admin@male.local`
- Senha: `Troque-esta-senha`

**Troque imediatamente em Configurações.**

Alternativamente, antes do primeiro boot, configure:
```bash
export ADMIN_USERNAME='seu@email.com'
export ADMIN_PASSWORD_HASH="$(python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('SuaSenhaForte'))")"
```

---

## Deploy em produção (Railway)

Ver [DEPLOY.md](DEPLOY.md) para procedimento completo passo a passo.

Resumo:
1. Criar serviço Postgres no projeto Railway.
2. Aplicar `schema_pg.sql` via `psql` usando a URL pública do Postgres.
3. Configurar variáveis no serviço web (`DATABASE_URL`, `FLASK_SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`).
4. Push da branch → Railway faz build e deploy automático.

---

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `DATABASE_URL` | sim | URL de conexão Postgres. Railway injeta via reference. |
| `FLASK_SECRET_KEY` | sim | Chave para assinar cookies de sessão. 32+ chars aleatórios. |
| `ADMIN_USERNAME` | não | Email do admin inicial. Default: `admin@male.local`. |
| `ADMIN_PASSWORD_HASH` | não | Hash werkzeug da senha inicial. Default: hash de `Troque-esta-senha`. |
| `GOOGLE_CLIENT_ID` | não | Para login via Google (OAuth). |
| `GOOGLE_CLIENT_SECRET` | não | Para login via Google. |
| `ENCRYPTION_KEY` | não | Fernet key para criptografar códigos de gift card em repouso. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | não | Para envio de email de gift cards. |
| `PUBLIC_BASE_URL` | não | URL pública da loja para links em emails/WhatsApp. |

---

## Testes

```bash
export DATABASE_URL='postgresql://...' # banco dedicado de teste, NÃO produção
psql "$DATABASE_URL" -f schema_pg.sql
pytest tests/test_smoke.py -v
```

Cobertura: conexão, CRUD, upsert, funções de tempo Postgres nativas, BLOB round-trip, FK cascade, rollback em exceção.

---

## Migração de schema

Schema vive em `schema_pg.sql`. Para aplicar mudanças em produção:

1. Desenvolver alteração localmente editando `schema_pg.sql`.
2. Criar migration script manual (ex: `migrations/001_add_col_foo.sql`).
3. Em produção, rodar o script via `psql` durante janela de manutenção.
4. Commit e deploy da mudança no código que usa a nova coluna.

Não existe migration framework automatizado — por design, para manter controle explícito sobre schema.

---

## Licença

Uso interno Tonton.
