# tonton v10.3 + v10.4 — cache busting + foto na tela de variantes

**Versão anterior:** v10.2 (correção de orientação EXIF)

## v10.3 — Cache busting de imagens

### Problema resolvido

Quando você girava uma foto na galeria, o blob mudava no banco mas o navegador continuava exibindo a versão em cache (cache de 24h). Você precisava dar Ctrl+F5 para ver a mudança, e o cliente final via a versão antiga até o cache expirar.

### Solução

Cada imagem ganha um campo `image_version` (INTEGER, default 1). A cada alteração do blob, a versão incrementa. As URLs públicas e admin agora incluem `?v={image_version}` na querystring. Quando a versão muda, a URL muda, o cache do navegador é furado naturalmente.

```
Antes:  /public/product-gallery/123/45         ← cacheada por 24h
Depois: /public/product-gallery/123/45?v=2     ← URL nova, cache invalidado
```

### Migration v10.3

Adiciona `image_version INTEGER NOT NULL DEFAULT 1` em `product_images`. Idempotente, roda no boot.

### Pontos atualizados

- Catálogo público: capa hero, thumbs do grid, swatches por cor — todos com `?v=`
- Galeria admin: miniatura de cada tile com `?v=`
- Endpoint `/public/product-image/<pid>` agora prefere a primária da galeria com fallback para legado
- Rotação manual incrementa `image_version` automaticamente

### Atenção

Mudanças que alterem o blob no futuro precisam incrementar a versão. Já está coberto na rotação manual; quando implementarmos crop/espelhamento/etc, lembrar de incrementar.

## v10.4 — Foto na tela de variantes

### O que muda na UI

Tela `/products/<id>/variants` agora tem uma coluna nova **"Foto"** entre Cor e SKU.

Para cada variante:
- **Se há foto cadastrada para a cor:** mostra miniatura 44×44px clicável (clica para trocar)
- **Se não há foto e tem cor:** mostra botão "+" coral para adicionar
- **Se variante sem cor:** mostra "—"

### Modal de upload

Clicar abre modal pré-selecionando a cor da variante:
- Cabeçalho: "Foto · {nome da cor}"
- Input de arquivo (JPG/PNG/WebP, máx 5MB, orientação corrigida automaticamente)
- Botões Cancelar / Enviar
- Link no rodapé para a galeria completa (caso queira fazer mais coisas)
- ESC fecha

### Backend — rota reutilizada

Não criei rota nova. O modal aponta para `POST /products/<pid>/gallery/upload` com:
- `name="color"` pré-preenchido com a cor da variante
- `name="back"` valor `variants` para redirecionar de volta à tela de variantes (em vez da galeria)

A rota foi adaptada para detectar `back=variants` e redirecionar adequadamente. Comportamento padrão (galeria) preservado para outros chamadores.

### Princípio mantido: foto por COR (não por variante)

Quando você sobe foto pela tela de variantes, ela vai para `product_images` como foto da **cor**. Isso significa:
- Variantes P/M/G/GG da mesma cor compartilham a mesma foto (correto: tamanho não muda visual)
- A foto também aparece nos swatches do catálogo público
- Você pode gerenciar na galeria completa (ordenar, marcar como capa, deletar) — é a mesma tabela

Não há duplicação de armazenamento. Não há regressão de modelo.

### Limitações conhecidas

- A coluna nova ocupa 52px na grid. Em telas muito estreitas (<800px) pode apertar; o template já é horizontalmente scrollable nesses casos via CSS existente.
- Se a variante não tem cor cadastrada (campo `color` vazio), a coluna mostra "—" e não há como subir foto pela tela de variantes (use a galeria completa para fotos sem cor associada).

## Como testar

### v10.3 (cache busting)
1. Boot da app — log: `Migration v10.3: adicionando image_version em product_images...`
2. Abrir o catálogo, identificar uma foto
3. Ir na galeria do produto, girar a foto 90°
4. Voltar ao catálogo (sem Ctrl+F5) — foto deve estar girada imediatamente

### v10.4 (foto nas variantes)
1. Ir em `/products/<id>/variants` de um produto com variantes coloridas
2. Coluna "Foto" deve aparecer entre Cor e SKU
3. Variantes sem foto cadastrada têm botão "+" coral
4. Clicar abre modal com cor pré-selecionada
5. Subir foto → volta para tela de variantes (não para galeria)
6. Outras variantes da mesma cor devem mostrar a mesma miniatura
7. Conferir na galeria completa que a foto aparece com a cor associada

## Próximas evoluções

- Crop com proporção fixa antes de salvar
- Drag-and-drop para reordenar fotos da galeria
- Botão de espelhamento horizontal/vertical
- Pré-visualização do upload antes de confirmar
