# tonton v9 — payment_status (refator estrutural)

**Versão anterior:** v8 (ux fixes — dashboard mobile, settings, autocomplete cliente, modal PIX, ordem P/M/G, botão sair)

## Conceito

Separa **status da venda** (existência) do **status de pagamento** (cobrança). Antes da v9, criar uma venda significava que o dinheiro entrou. Agora, vendas em PIX nascem **pendentes** até o operador confirmar manualmente.

```
sale.status         : active | cancelled | returned
sale.payment_status : paid | pending | failed | refunded   ← NOVO
```

## Comportamento por método de pagamento

| Método | payment_status inicial | Por quê |
|---|---|---|
| Dinheiro | `paid` | Operador recebe na hora |
| Cartão (maquininha) | `paid` | Maquininha aprova na hora |
| PIX | `pending` | Aguarda confirmação manual |

## Mudanças no DRE

**Receita só é reconhecida quando paga** (princípio CPC 47 / IFRS 15). Vendas pendentes:
- ❌ NÃO contam no faturamento mensal
- ❌ NÃO contam no CMV (COGS) do mês
- ✅ APARECEM no KPI separado "A receber"
- ✅ Estoque já foi baixado (reserva)

Ao confirmar pagamento, a venda passa a contar normalmente.
Ao cancelar por não-pagamento, estoque é reposto e cupom revertido.

## Novas rotas

```
GET  /sales/pending              → lista vendas com pagamento pendente
POST /sales/<sid>/payment/confirm → marca como paga
POST /sales/<sid>/payment/cancel  → cancela venda por não-pagamento (devolve estoque)
```

## Novos elementos de UI

- **Banner no dashboard** quando há pendentes (clicável, leva à lista)
- **Item "A receber" na sidebar** com badge contador (visível em qualquer página)
- **Card "Status do pagamento"** no detalhe da venda com botões "Confirmar" e "Cancelar por não-pagamento"
- **Tela /sales/pending** com KPIs (count, total, atrasadas), lista com idade de cada venda, botão "Copiar link PIX" para reenviar ao cliente

## Migration

Idempotente, roda em todo boot. Adiciona 3 colunas em `sales`:
- `payment_status TEXT NOT NULL DEFAULT 'paid'`
- `payment_confirmed_at TEXT`
- `payment_confirmed_by TEXT`

Mais um CHECK constraint e um índice parcial para a query da tela de pendentes.

**Vendas existentes ficam como `paid`** — não reescreve histórico contábil.

## Configurações

- **Alerta de atraso:** vendas pendentes há ≥ **8 horas** ganham chip "atrasada" na lista. Não cancela automaticamente.
- **Sem job de expiração automática** — operador decide quando cancelar pendentes velhas.

## Riscos e cuidados

1. **Backup do Postgres antes do primeiro deploy.** ALTER TABLE é seguro mas é prudente.
2. **PIX manual continua exigindo confirmação manual** — isso é por design, não bug. Sem integração bancária (Inter, Mercado Pago, etc.), não há como confirmar automaticamente.
3. **Relatórios secundários ainda contam vendas pendentes como receita.** Os pontos críticos (dashboard, accounting) foram corrigidos. Se houver discrepância em algum relatório específico, é um ponto a corrigir numa próxima rodada.

## Mudanças não incluídas (próximas rodadas)

- Job automático de expiração (cancelamento por timeout)
- Notificação push/email quando pagamento confirma via webhook
- Integração com Mercado Pago (hoje só Inter e manual)
- Histórico de tentativas de pagamento (quem confirmou, quando, com qual método)

## Como testar

1. Subir a v9 e verificar log: `Migration v9: adicionando payment_status em sales...`
2. Fazer venda em **dinheiro** → deve aparecer "✓ Pagamento confirmado" no detalhe
3. Fazer venda em **PIX** → deve aparecer "⏳ Aguardando pagamento" + 2 botões
4. Conferir o **banner do dashboard** e a **sidebar** — devem mostrar 1 pendente
5. Em `/sales/pending`, conferir cálculo de idade e botões
6. Clicar "Confirmar pagamento" → venda some da lista de pendentes
7. Conferir DRE: receita do mês deve refletir só vendas pagas
