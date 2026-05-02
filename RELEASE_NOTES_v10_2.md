# tonton v10.2 — correção de orientação de imagens

**Versão anterior:** v10.1 (galeria multi-foto + remoção do modal PIX redundante)

## Problema resolvido

Fotos tiradas de celular contêm metadado EXIF de orientação. O Postgres armazenava o blob bruto, e dependendo do navegador a foto aparecia rotacionada (deitada quando deveria estar em pé). O bug afetava tanto a foto principal do produto quanto a galeria nova.

## Solução

Em vez de confiar no navegador para ler o EXIF, **rotaciona os pixels fisicamente** no upload e remove o EXIF. A foto sai do servidor já na orientação visual correta.

## O que foi adicionado

### 1. Função `_normalize_uploaded_image(blob, mime)`

Aplicada em todos os uploads novos:
- **Aplica orientação EXIF** (rotaciona pixels conforme metadado)
- **Redimensiona** para máximo 1600px no maior lado (mantém proporção)
- **Recomprime**:
  - JPEG quality 85, progressive
  - WebP quality 85
  - PNG preservado (incluindo transparência)
- **Strip de EXIF** — privacidade (geolocalização, modelo de câmera) + arquivo menor

Em caso de erro em qualquer etapa, devolve o original — não bloqueia o upload.

### 2. Função `_rotate_image_blob(blob, mime, degrees)`

Rotação manual usada pelo botão "Girar 90°" da galeria. Preserva o formato MIME original.

### 3. `_make_thumbnail` corrigido

A função que gera a foto principal do produto agora aplica `ImageOps.exif_transpose` antes de redimensionar. Resolve o problema da foto principal torta sem nenhuma migration.

### 4. Botão "Girar 90°" na galeria

Cada tile da galeria admin tem um botão extra (ícone seta) que rotaciona a foto 90° em sentido horário e salva. Útil para os 1% de casos onde o EXIF está errado ou a foto subiu certa mas você prefere outro enquadramento.

### 5. Nova rota

```
POST /products/<pid>/gallery/<img_id>/rotate  → rotaciona e salva
```

### 6. Migration v10.2 retroativa

**Aplica retroativamente em todas as fotos já existentes** no banco:
- Galeria (`product_images`)
- Foto legada (`products.image_blob`)

Cada foto é lida, processada com `_normalize_uploaded_image` e regravada **só se mudou** (evita escrita inútil).

**Garantia de execução única:** nova tabela `_migrations_runtime` armazena uma flag por migration. Após a primeira execução bem-sucedida, a flag é gravada e a migration não roda mais.

**Tratamento de erro por imagem:** se uma imagem corrompida falhar, é pulada e logada como warning; a migration continua. Ao final, o log mostra:
```
Migration v10.2 aplicada. Galeria: X normalizadas, Y falharam.
                          Legado: X normalizadas, Y falharam.
```

## Atenção operacional para o deploy

A migration retroativa lê **todas as imagens uma vez** no boot, processa em RAM e regrava. Estimativa:

| Quantidade de fotos | Tempo estimado |
|---|---|
| 100 fotos | ~30 segundos |
| 500 fotos | ~2 minutos |
| 1000 fotos | ~5 minutos |
| 5000 fotos | ~25 minutos |

**Recomendação:** se sua base tem mais de 500 fotos, faça o deploy fora de horário de pico. Durante a migration, requests para upload ficam mais lentos por contenção de I/O do banco.

A migration é **idempotente** — se o servidor cair no meio, na próxima inicialização as fotos já processadas (commitadas individualmente) ficam como estão; as restantes serão processadas. Após o término, a flag é gravada e não roda mais.

## Compatibilidade

- ✅ Pillow já estava no requirements (v11.3.0) — sem nova dependência
- ✅ Schema legado intacto — só adiciona `_migrations_runtime`
- ✅ Backup recomendado (`pg_dump`) antes do primeiro boot pós-deploy

## Como testar pós-deploy

1. Abrir log do servidor — deve mostrar `Migration v10.2: normalizando orientação EXIF...` seguido do total
2. Abrir o catálogo público e conferir que fotos antes tortas estão em pé
3. Tirar foto nova com celular (em modo retrato, comum gerar EXIF)
4. Subir na galeria — deve aparecer corretamente orientada de cara
5. Clicar no botão "Girar 90°" numa foto — confirma que rotação manual funciona
6. Conferir tamanho do banco antes/depois — deve diminuir (compressão JPEG q85 + strip EXIF)

## Decisões técnicas justificadas

- **JPEG quality 85** — limite onde olho humano não distingue do original (ref: estudos de compressão Apple/Wirecutter)
- **1600px max** — padrão Mercado Livre/Shopify para mobile; suficiente para zoom; mantém arquivo leve
- **Strip de EXIF** — privacidade (não vaza GPS/modelo de câmera dos clientes que mandam foto) + arquivos ~5% menores
- **Tabela `_migrations_runtime`** — extensível: futuras migrations retroativas usam o mesmo mecanismo só mudando a `key`. Padrão de migrations idempotentes em produção.

## Próximas evoluções (não nesta versão)

- Botões adicionais: rotação anti-horária, espelho horizontal/vertical
- Recorte (crop) com proporção fixa (1:1, 4:5)
- Drag-and-drop para reordenar galeria
- Pre-visualização antes de salvar a rotação manual
