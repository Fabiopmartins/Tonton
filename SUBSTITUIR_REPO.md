# Como substituir o repositório inteiro

Você vai apagar o conteúdo atual do repositório GitHub e colocar este projeto completo no lugar.

**Tempo:** ~5 min. Não afeta produção ainda (só muda o código fonte no GitHub).

---

## Antes de começar

- [ ] **IMPORTANTE:** certifique-se que a aplicação atual em produção está usando SQLite (estado atual, com `DATABASE_URL` removida). Este código novo é Postgres-only — se Railway estiver configurado pra auto-deploy da `main`, assim que você commitar na `main`, Railway vai tentar buildar o código novo. Se não houver `DATABASE_URL` anexada ainda, o app vai falhar no boot.
- [ ] Opção recomendada: desativar auto-deploy temporariamente, commitar, e só então seguir o `DEPLOY.md`.

---

## Opção A — via branch (recomendada, mais segura)

Não mexe na `main` até você decidir. Zero risco de deploy acidental.

```bash
cd /caminho/do/seu/repo

# backup por garantia
git branch backup-sqlite-antes-postgres

# criar branch de trabalho
git checkout -b postgres-only

# apagar TODO o conteúdo da branch
git rm -rf .
git clean -fdx

# colar o novo projeto (ajustar caminho)
cp -r /caminho/para/male_final/* /caminho/para/male_final/.gitignore .

# verificar
ls -la
python3 -m py_compile app.py db.py   # sanity de sintaxe

# commit
git add -A
git commit -m "Reescrita completa: Postgres-only, psycopg2 direto, sem SQLite"
git push origin postgres-only
```

No GitHub, vai aparecer a branch `postgres-only`. **Não faça merge ainda.** Siga o `DEPLOY.md` — merge acontece só no passo 7.

---

## Opção B — substituir direto na main (pressa + aceitar risco)

Se você está num cenário de fresh start e já tem Railway pronto para o switch:

```bash
cd /caminho/do/seu/repo
git checkout main

# backup da versão anterior
git tag pre-postgres-reset

# apagar tudo
git rm -rf .
git clean -fdx

# colar novo projeto
cp -r /caminho/para/male_final/* /caminho/para/male_final/.gitignore .

# sanity check
python3 -m py_compile app.py db.py

# commit e push
git add -A
git commit -m "Reescrita completa: Postgres-only"
git push origin main
```

Railway vai tentar deploy automático. **Vai falhar** (boot do app sem `DATABASE_URL` anexada). Siga o `DEPLOY.md` a partir do passo 2 imediatamente para configurar e recuperar.

---

## Opção C — criar repositório novo do zero

Se você prefere começar com um repositório completamente limpo (sem histórico do antigo):

```bash
# localmente
mkdir male-postgres
cd male-postgres
cp -r /caminho/para/male_final/* /caminho/para/male_final/.gitignore .

git init
git add -A
git commit -m "Initial commit: Tonton Painel Tonton · Postgres"

# criar novo repo no GitHub (via web ou gh cli)
gh repo create male-postgres --private --source=. --push

# reconectar o serviço Railway ao novo repo
# Railway → serviço web → Settings → Source → Disconnect → Connect → male-postgres
```

Trade-off: perde histórico git. Ganha repo limpo.

---

## Estrutura final do repo

Depois da substituição, seu repositório vai ter exatamente:

```
.
├── .gitignore
├── Procfile
├── README.md
├── DEPLOY.md
├── SUBSTITUIR_REPO.md          ← este arquivo (pode deletar após ler)
├── railway.toml
├── requirements.txt
├── pytest.ini
├── schema_pg.sql               ← aplicar no PG antes do 1º deploy
├── app.py                      ← Flask app Postgres-only
├── db.py                       ← camada psycopg2
├── static/                     ← CSS, fontes, imagens de marca
├── templates/                  ← Jinja2 templates
└── tests/
    └── test_smoke.py           ← smoke tests contra Postgres
```

**Arquivos que deixaram de existir (não copiar):**
- `schema.sql` (era SQLite, substituído por `schema_pg.sql`)
- `giftcards.db` (SQLite local, não deve mais existir)
- `instance/male_store.db` (SQLite antigo)
- `DEPLOY_RAILWAY.md`, `PRODUCTION_CHECKLIST.md`, `SECURITY_NOTES.md`, `RELEASE_NOTES_FIXES.txt` (substituídos por `README.md` + `DEPLOY.md`)
- `render.yaml` (deploy Render — não usamos)
- `notifications.html` na raiz (template órfão — o ativo está em `templates/notifications.html`)
- `__pycache__/` (cache Python, ignorado via `.gitignore`)

---

## Depois de substituir

1. Leia `README.md` para entender a estrutura.
2. Siga `DEPLOY.md` passo a passo para subir em produção.
3. Apague este arquivo (`SUBSTITUIR_REPO.md`) — serviu seu propósito:
   ```bash
   git rm SUBSTITUIR_REPO.md
   git commit -m "Remove instruções de substituição (já aplicadas)"
   ```
