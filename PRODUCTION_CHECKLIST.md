# Checklist de Produção — tonton · Painel

Este documento cobre o que foi **implementado** no código e o que ainda
**depende de configuração** na hospedagem (Railway / Render / outro PaaS).

---

## ✅ O que já está no código

| Área | Item |
|---|---|
| **Sessão** | `SECRET_KEY` persistido em arquivo (ou via `FLASK_SECRET_KEY`), cookies `HttpOnly` + `SameSite=Lax` + `Secure` automático em HTTPS |
| **Proxy** | `ProxyFix` aplicado — `request.remote_addr`, `is_secure` e URLs externas corretos atrás de Railway/Render/Heroku/Fly |
| **SQLite** | Modo **WAL** + `busy_timeout=30s` — suporta múltiplos workers gunicorn sem "database is locked" |
| **CSRF** | Validação em todas rotas POST autenticadas (44 rotas) |
| **Login** | Proteção brute-force: `MAX_FAILED_LOOKUPS` tentativas em `LOCK_MINUTES` minutos bloqueia email+IP |
| **Logs** | Logger do Flask conectado ao gunicorn stderr; `LOG_LEVEL` env controla verbosidade |
| **Erros** | Handlers 400/403/404/413/429/500 + catch-all. HTML em rotas normais, JSON em `/api/*`. Nunca vaza stack trace |
| **Uploads** | `MAX_CONTENT_LENGTH=10 MB` (configurável via `MAX_UPLOAD_MB`) |
| **Headers** | CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, HSTS em HTTPS |
| **Rate limit** | `/api/catalog/interest` limitado a 20 req/min por IP |
| **Catálogo público** | Valida existência do produto antes de registrar interesse |

---

## ⚙️ Variáveis de ambiente que você **precisa** configurar

Obrigatórias em produção:

```
FLASK_SECRET_KEY=<32+ bytes aleatórios>
CODE_PEPPER=<32+ bytes aleatórios>
CODE_ENCRYPTION_KEY=<gere com Fernet.generate_key().decode()>
PUBLIC_BASE_URL=https://seu-dominio.com.br
FORCE_HTTPS=true
DB_PATH=/data/giftcards.db         # caminho PERSISTENTE
```

> **Importante:** No Render free tier o filesystem do container é efêmero.
> Se você está em produção de verdade, use um disco persistente montado em
> `/data` ou migre para Postgres. SQLite sem persistência = perda de dados
> a cada deploy.

Opcionais (só se estiver usando):

```
GOOGLE_CLIENT_ID=...            # login com Google
GOOGLE_CLIENT_SECRET=...
SMTP_HOST=...                   # envio de e-mails
SMTP_PORT=587
SMTP_USERNAME=...
SMTP_PASSWORD=...
SMTP_USE_TLS=true
MAIL_FROM=no-reply@seu-dominio.com.br
BREVO_API_KEY=...               # alternativa a SMTP
MAX_FAILED_LOOKUPS=5            # default
LOCK_MINUTES=15                 # default
MAX_UPLOAD_MB=10                # default
LOG_LEVEL=INFO                  # DEBUG para desenvolver
```

---

## 🚀 Recomendações de infraestrutura

**Gunicorn (já no `render.yaml`):**

```
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
```

Com SQLite em WAL, **2 workers** funciona bem. Não escale além de 4 workers
sem migrar para Postgres.

**Backups:** um cron diário copiando `giftcards.db` (+ `-wal` + `-shm`) para
S3/Backblaze usando `sqlite3 giftcards.db ".backup /tmp/backup.db"` (backup
consistente mesmo com escrita concorrente).

**Monitoramento:** o logger agora emite `login fail`, `login locked`,
`login ok`. Mande o log para Better Stack / Axiom / Grafana Loki para ver
padrões de ataque.

---

## ⚠️ Pontos que ainda valem atenção (fora do escopo desta revisão)

1. **Multi-tenant / isolamento** — a app assume uma única loja por
   instância. Se for virar SaaS multi-loja, tem refatoração grande.
2. **Postgres** — SQLite é ótimo até ~50 reqs/seg com WAL. Acima disso,
   migrar. Conta também o volume de escrita de `catalog_interest`.
3. **Redis para rate-limit** — o limitador atual é em memória por processo.
   Com 2 workers, o limite efetivo dobra. Para precisão, plugar Redis.
4. **Testes automatizados** — hoje não existem. Considere criar uma suíte
   com `pytest + app.test_client()` espelhando os fluxos críticos (login,
   venda, devolução, geração de vale-presente).
5. **CSP `'unsafe-inline'`** — ainda presente em `script-src` e
   `style-src`. Remover exige extrair todos os `<script>` e `style=""`
   inline para arquivos + hashes/nonces. É trabalhoso mas importante.
