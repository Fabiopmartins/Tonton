# Runbook — Cutover Tonton · SQLite → Postgres (Railway)

Janela planejada: **~30 min** com loja em read-only por ~10 min.

---

## 0. Pré-requisitos (fazer ANTES da janela)

- [ ] Serviço Postgres provisionado no mesmo projeto Railway da app.
- [ ] Variável `DATABASE_URL` disponível no projeto (não anexada ao web ainda).
- [ ] Novos arquivos no repositório (branch separado, não merged):
  - `schema_pg.sql`
  - `db.py`
  - `migrate_sqlite_to_pg.py`
  - `app.py` (versão reescrita)
  - `requirements.txt` (com SQLAlchemy + psycopg2-binary)
  - `tests/test_parity.py`
- [ ] Testes locais passando em SQLite (sanity):
  ```
  cd projeto/
  pip install -r requirements.txt pytest
  pytest tests/test_parity.py -v
  ```
- [ ] Testes locais passando em Postgres local (ou num banco Railway dedicado de staging):
  ```
  DATABASE_URL=postgresql://... pytest tests/test_parity.py -v
  ```
- [ ] Backup local do SQLite atual baixado do volume Railway.
- [ ] Variáveis de rollback anotadas: saber qual commit/branch restaurar se precisar voltar.

---

## 1. Aplicar o schema no Postgres novo (sem tocar no web ainda)

```bash
export DATABASE_URL='postgresql://postgres:SENHA@postgres.railway.internal:5432/railway'
# OU a URL externa do Postgres se você estiver rodando da sua máquina
export DATABASE_URL_EXTERNA='postgresql://postgres:SENHA@HOST_PUBLICO:PORTA/railway'
```

Rodar de onde você tiver `psql`:

```bash
psql "$DATABASE_URL_EXTERNA" -f schema_pg.sql
```

Validação:
```bash
psql "$DATABASE_URL_EXTERNA" -c "\dt"   # lista tabelas
psql "$DATABASE_URL_EXTERNA" -c "SELECT count(*) FROM expense_categories"  # deve ser 9
psql "$DATABASE_URL_EXTERNA" -c "SELECT count(*) FROM store_settings"      # deve ser 7
```

---

## 2. Inicia a janela (T+0) — loja em read-only

Opções pra colocar a loja em read-only, em ordem de preferência:

**A. Banner + flag no Railway (mais limpo):**

Adicionar variável `READ_ONLY_MODE=1` no web e no código um middleware que retorna 503 pra POST/PUT/DELETE. (Se você não tem esse flag pronto, vai direto pra B.)

**B. Desligar o web service temporariamente (mais simples):**

No Railway, no serviço web: Settings → "Suspend" ou escalar réplicas pra 0. Loja fica fora do ar, mas ninguém grava dados novos durante o dump.

Este runbook assume **opção B**.

---

## 3. Baixar o SQLite atual (T+2)

Do volume Railway `/data/giftcards.db`, baixe uma cópia:

```bash
railway run --service=web "cat /data/giftcards.db" > giftcards_cutover.db
# ou via SSH no volume, conforme seu setup
```

Validar que o arquivo tem ~396 KB e abre corretamente:

```bash
sqlite3 giftcards_cutover.db ".tables"
sqlite3 giftcards_cutover.db "SELECT COUNT(*) FROM gift_cards; SELECT COUNT(*) FROM sales; SELECT COUNT(*) FROM products"
```

Anote esses números — vão ser conferidos depois.

---

## 4. Rodar a migração (T+5)

```bash
pip install psycopg2-binary
export DATABASE_URL="$DATABASE_URL_EXTERNA"
python migrate_sqlite_to_pg.py giftcards_cutover.db --truncate
```

Saída esperada (resumo):
```
[migrate] Lendo SQLite: giftcards_cutover.db
[migrate] Conectando ao Postgres...
[migrate] Tabelas a migrar (25): [...]
[migrate] TRUNCATE CASCADE em todas as tabelas alvo...
[migrate]   users                        origem=     1  inserido=     1
[migrate]   expense_categories           origem=     9  inserido=     9
[migrate]   ...
[migrate] Resetando sequences...
[migrate] Verificando contagens...
[migrate] Migração concluída com sucesso.
```

**Se falhar por divergência de contagem:** o script já fez `rollback()`. Investigue os avisos de colunas e rode novamente. Não prossiga até o "sucesso".

---

## 5. Validação pós-migração (T+10)

```bash
psql "$DATABASE_URL_EXTERNA" <<'SQL'
SELECT 'gift_cards' t, COUNT(*) n FROM gift_cards
UNION ALL SELECT 'sales', COUNT(*) FROM sales
UNION ALL SELECT 'products', COUNT(*) FROM products
UNION ALL SELECT 'customers', COUNT(*) FROM customers
UNION ALL SELECT 'users', COUNT(*) FROM users
UNION ALL SELECT 'sale_items', COUNT(*) FROM sale_items;
SQL
```

Confira contra os números do passo 3.

```bash
# Testar que sequences ficaram corretas
psql "$DATABASE_URL_EXTERNA" -c "SELECT MAX(id), (SELECT last_value FROM products_id_seq) FROM products"
# last_value deve ser >= MAX(id)+1
```

---

## 6. Switch do web para Postgres (T+15)

No Railway → serviço web:

1. Settings → Variables → adicionar `DATABASE_URL` referenciando o serviço Postgres interno:
   ```
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   ```
   (ou o valor direto `postgresql://postgres:SENHA@postgres.railway.internal:5432/railway`)

2. Fazer deploy da branch com o código reescrito (app.py novo, requirements.txt atualizado).

3. Railway vai fazer build e deploy automático.

4. Se estiver usando opção B (suspend), religar o serviço agora.

---

## 7. Smoke test em produção (T+20)

Testes manuais críticos, nessa ordem:

- [ ] GET `/` → redireciona pra `/login` (app subiu sem erro 500).
- [ ] Login com admin → dashboard abre, conta vendas/gift cards corretas.
- [ ] `/gift_cards` → lista vê os cupons antigos com IDs idênticos ao SQLite.
- [ ] `/products` → imagens de produtos carregam (BLOB). Abrir um produto que tinha foto — se ela aparecer, o BYTEA migrou certo.
- [ ] `/sales` → últimas vendas aparecem, datas corretas no fuso local.
- [ ] Criar uma venda de teste de valor mínimo → confirmar que `sale_number` e `id` foram gerados.
- [ ] Cancelar a venda de teste → confirmar que credit_id apareceu corretamente.
- [ ] `/interest` (catálogo público de interesses) → contagem "últimos 30 dias" coerente (valida o `last_n_days_expr`).
- [ ] `/reports` → gráfico por dia da semana e hora (valida `dow_expr` / `hour_local_expr`).

Se tudo OK, encerre a janela e comunique a retomada.

---

## 8. Rollback (se algo der errado)

**Até o passo 6 (antes do switch):** não há rollback necessário — basta religar o web sem mudar `DATABASE_URL` e investigar offline.

**Depois do passo 6:**

1. Railway → serviço web → Variables → **remover** `DATABASE_URL`.
2. Fazer redeploy da branch (ou reverter pro commit anterior se já mergeou).
3. Web volta a ler `/data/giftcards.db` (SQLite original, intacto).

⚠️ **Importante:** qualquer gravação que tenha acontecido no Postgres após o switch **não volta** pro SQLite. Por isso só libere escrita depois do smoke test OK. Se você religou cedo demais e escreveram dados novos no Postgres antes de decidir reverter, vai precisar exportar esses dados manualmente antes do rollback.

**Critério de decisão de rollback:** erros 500 persistentes em endpoints de leitura, ou qualquer erro em cálculo de totais/saldos. Erros cosméticos ou de endpoints secundários → corrige em hotfix sem rollback.

---

## 9. Pós-cutover (T+30 em diante)

- [ ] Monitorar logs do Railway por 30–60 min — `grep -i error` no output do gunicorn.
- [ ] Validar performance das queries de relatório (Postgres pode reordenar planos; nada grave esperado pro volume atual).
- [ ] Backup do banco Postgres configurado (Railway faz snapshots automáticos no plano pago; confira se está ativo).
- [ ] Arquivar `giftcards_cutover.db` localmente (fonte de verdade histórica).
- [ ] Agendar remoção do volume `/data` do web só **depois de 1 semana** rodando sem incidente.

---

## Anexo — comandos úteis durante o cutover

Conectar rápido ao Postgres:
```bash
psql "$DATABASE_URL_EXTERNA"
```

Ver tamanho por tabela:
```sql
SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC;
```

Reset manual de uma sequence (se precisar):
```sql
SELECT setval(pg_get_serial_sequence('products','id'),
              COALESCE((SELECT MAX(id) FROM products), 0) + 1, false);
```

Comparar contagem SQLite vs Postgres numa tabela:
```bash
SQ=$(sqlite3 giftcards_cutover.db "SELECT COUNT(*) FROM sales")
PG=$(psql "$DATABASE_URL_EXTERNA" -tAc "SELECT COUNT(*) FROM sales")
echo "sqlite=$SQ  postgres=$PG"
```
