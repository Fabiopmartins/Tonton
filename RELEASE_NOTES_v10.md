# tonton v10 — galeria multi-foto por cor

**Versão anterior:** v9 (payment_status — cobrança desacoplada da venda)

## Conceito

Antes da v10, cada produto tinha apenas **uma foto**. Agora um produto pode ter **até 5 fotos**, e cada foto pode estar associada a uma **cor de variante** ou ser uma foto **geral** (caimento, detalhe de tecido).

No catálogo público, o produto continua aparecendo **uma única vez** — mas o cliente clica num swatch de cor e a foto principal troca para a foto daquela cor.

## Modelagem

```
product_images
├── id (PK)
├── product_id (FK → products)
├── color (NULL = foto geral)
├── image_blob (BYTEA)
├── image_mime
├── sort_order
├── is_primary (1 = capa do catálogo)
└── created_at
```

**Por que foto por COR e não por (cor + tamanho)?** Tamanho não muda visual relevante (uma camiseta P, M e G são idênticas na foto). Cor é a variação visual que o cliente quer ver.

## Migration

Idempotente, roda no boot:
1. Cria tabela `product_images` se não existe
2. **Migra automaticamente** o `products.image_blob` legado para `product_images` como foto primária sem cor
3. Produtos sem foto antes continuam sem foto

Resultado: zero ação manual após deploy. Catálogo continua exibindo as mesmas fotos de antes.

## Novas rotas

```
GET  /products/<pid>/gallery              → tela admin da galeria
GET  /products/<pid>/gallery/<img_id>     → serve blob (admin)
POST /products/<pid>/gallery/upload       → upload (5 MB max, JPG/PNG/WebP)
POST /products/<pid>/gallery/<img_id>/delete       → remove foto
POST /products/<pid>/gallery/<img_id>/set-primary  → define capa
GET  /public/product-gallery/<pid>/<img_id>        → serving público (cache 24h)
```

A rota `/products/<pid>/image` agora **prefere a foto primária da galeria** com fallback para `image_blob` legado, então links antigos continuam funcionando.

## UI Admin — Galeria

Tela acessada via botão **"Galeria"** no detalhe do produto:
- Grid de fotos com badge "capa" na primária
- Cada foto mostra a cor associada (ou "geral")
- Botões: definir como capa (★) e remover (🗑)
- Formulário de upload com:
  - Input de arquivo (JPG/PNG/WebP, máx 5 MB)
  - Select de cor populado automaticamente com as cores das variantes do produto
  - Limite de 5 fotos (formulário desabilita ao atingir)

## UI Catálogo público — Swatches

No card de cada produto:
- Chips de cor que **antes eram decorativos** agora são **clicáveis** (quando há foto associada à cor)
- Hover destaca os chips clicáveis
- Clicar troca a foto principal do card sem recarregar a página
- Swatch ativo ganha outline coral
- Chips sem foto associada continuam decorativos (não clicam, não destacam no hover)

## Comportamento esperado

**Cenário 1: produto antigo sem fotos novas**
- Catálogo mostra a foto legada (vinda do `image_blob`)
- Chips de cor são decorativos (sem foto vinculada)

**Cenário 2: produto novo com 3 cores e 3 fotos (uma por cor)**
- Capa: a primeira foto cadastrada (ou a marcada como primária)
- Chips de cor clicáveis trocam a foto

**Cenário 3: produto com 1 foto geral + 2 fotos por cor**
- Capa: a foto marcada como primária (geralmente a geral)
- Chips com foto: clicáveis. Chip sem foto: decorativo.

## Riscos e cuidados

1. **Performance no Postgres** — fotos são armazenadas como BYTEA. Para 100 produtos × 3 fotos × ~500 KB = 150 MB no banco. Postgres aguenta tranquilo, mas se passar de 1 GB, considerar mover para storage externo (S3/R2).
2. **Cache do navegador** — fotos públicas têm `Cache-Control: public, max-age=86400` (24h). Se trocar uma foto, usuários com cache verão a antiga até expirar. Para forçar atualização imediata, mude o nome do produto ou adicione versionamento na URL (não implementado nesta versão).
3. **Migration cria 1 INSERT por produto com foto antiga** — em bases muito grandes (>10k produtos com foto), pode levar alguns minutos no primeiro boot. Recomendo rodar a migration em horário de baixo tráfego.

## Testes recomendados

1. Boot da app → log deve mostrar `Migration v10: criando product_images...` seguido de `Migration v10 aplicada.`
2. Ir num produto antigo → catálogo deve continuar mostrando a foto de antes
3. Em `/products/<id>/gallery`, subir 2 fotos com cores diferentes
4. Acessar catálogo público → swatches dessas cores devem ficar clicáveis (com hover destacado)
5. Clicar num swatch → foto principal troca

## Próximas evoluções (não incluídas nesta versão)

- Drag-and-drop para reordenar fotos
- Upload em lote (múltiplos arquivos de uma vez)
- Compressão automática server-side (hoje confia no upload do usuário)
- Mover fotos para storage externo (S3/R2) quando o banco crescer
- Carrossel adicional de fotos extras na ficha técnica
