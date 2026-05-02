# 🧡 Tonton · Painel Administrativo

Esta é a versão da aplicação `Male_cupom` adaptada para a **Tonton Loja Infantil** (@tontonlojainfantil) — Timóteo/MG.

## O que mudou em relação ao app original

### 🎨 Identidade visual completamente refeita

| Elemento | Antes (Malê) | Agora (Tonton) |
|---|---|---|
| **Cor principal** | Coral `#ff6b3a` | Laranja Tonton `#F38A35` |
| **Cor secundária** | Magenta `#d6336c` | Verde mintado `#3DB48F` |
| **Acento** | Pink/Rose | Amarelo sol `#FCC630` |
| **Fundo cream** | `#fff8f2` | `#FFF6E6` (mais quente) |
| **Texto** | Marrom-preto | Marrom quente `#3A2410` |
| **Display font** | Instrument Serif (italic, editorial) | Fredoka (rounded, brincalhão) |
| **Corpo** | Inter | Nunito |
| **Logo** | "m." monograma serif | "tt" duplo (amarelo+laranja) com coraçãozinho verde |
| **Wordmark** | "malê" italic | "tonton" empilhado, no estilo do logo do Instagram |

### 🔧 Adaptações funcionais

- **Tamanhos**: placeholders dos produtos agora sugerem `2, 4, 6, 8, 10 (anos)` no lugar de `P, M, G, GG`.
- **Modelagem**: opções incluem `bebê`, `infantil`, `juvenil` (no lugar de `slim`, `plus`).
- **Tipo de tecido**: adicionado `plush`, `algodão pima`, `suedine` — comuns em moda infantil.
- **Instagram**: URL atualizada para `https://www.instagram.com/tontonlojainfantil`.
- **Mensagens automáticas** (vale-presente WhatsApp/email): assinam "Tonton" no lugar de "Malê".
- **Domínio sugerido nos placeholders**: `tontonlojainfantil.com.br`.
- **Default `MAIL_FROM_NAME`** e `store_name`: `Tonton`.

### 🗂️ Estrutura preservada

A arquitetura, schema do banco e lógica de negócio do app original foram preservadas integralmente — a Tonton herda **todas** as features:

- Catálogo público com fotos, variações cor×tamanho, preço promocional, Instagram, WhatsApp
- Vendas (PDV) com múltiplas formas de pagamento, pix automático (Banco Inter)
- Vale-presentes (gift cards) com PDF, WhatsApp/email, redenção via QR code
- Cupons de desconto com regras (percentual, valor fixo, primeira compra, etc.)
- Cadastro de clientes com aniversariantes, histórico de compras
- Campanhas de marketing por WhatsApp (template messages)
- Relatórios contábeis, ABC de produtos, controle de despesas, metas
- Lookbook (montagem de looks combinando peças)
- Reposição de estoque, histórico de preços
- Multi-usuário com permissões (admin, atendente)
- Notificações internas, calendário, dashboard

### 📂 Arquivos da nova marca

- `static/brand/favicon.svg` — favicon "tt" com coraçãozinho
- `static/brand/tonton-monogram.svg` / `male-monogram.jpg` — monograma quadrado
- `static/brand/tonton-wordmark.svg` / `male-wordmark.jpg` — wordmark "loja infantil tonton"
- Arquivos legacy (`logo-male-*.png`, `male-monogram-light.jpg`, etc.) foram **substituídos in-place** pelo novo design — os templates continuam referenciando os mesmos paths sem precisar ser refatorados.

> Os nomes dos arquivos foram mantidos (`male-*.jpg`) para evitar refactor em massa nos 30+ templates que os referenciam. O **conteúdo visual** já é 100% Tonton.

## ▶️ Como rodar

Idêntico ao app original — requer Postgres:

```bash
export DATABASE_URL="postgresql://user:pass@host:port/db"
export FLASK_SECRET="alguma-string-aleatoria-longa"
psql "$DATABASE_URL" -f schema_pg.sql      # apenas na primeira vez
pip install -r requirements.txt
python bootstrap_admin.py                  # cria primeiro admin
gunicorn app:app                           # ou flask run para dev
```

Veja `README.md`, `MANUAL_DE_USO.md` e `DEPLOY.md` para detalhes.

## 🚧 Próximos passos sugeridos

Coisas que ficaram fora do escopo deste rebrand mas que valem futuramente:
- Adicionar campo "Gênero" (Menino / Menina / Unissex) como categoria nativa do produto, em vez de tag livre.
- Customizar os emails transacionais (templates HTML) com o cabeçalho visual da Tonton.
- Trocar as fotos de exemplo do hero (catálogo público) pelas fotos da própria Tonton.
- Considerar campos específicos para infantil: idade recomendada, segurança (sem botões soltos, etc.).

---

Adaptado em maio/2026.
