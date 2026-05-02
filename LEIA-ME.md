# v9.7 — Vitrine integrada em Configurações

Substitui completamente os zips anteriores.

## O que mudou em relação à v9.6.1

### Reorganização da Vitrine

**Antes**: "Hero da vitrine" era um item separado no menu lateral (Sistema → Hero da vitrine).

**Agora**: integrado em **Configurações → aba Loja**, com card "Vitrine pública" que mostra contador de imagens cadastradas + botão "Gerenciar imagens →" que leva para a página dedicada.

### Mudanças

1. **Item "Hero da vitrine" removido do menu lateral** — agora você acessa via Configurações
2. **Card "Vitrine pública" adicionado em Configurações → Loja** (coluna lateral direita, junto com "Abrir vitrine pública" e "Telefones responsáveis")
3. **Página de gerenciar imagens** ganhou breadcrumb "← Configurações" no topo
4. **Título da página** simplificado: "Hero da vitrine" → "Vitrine pública"
5. Configurações ainda destaca o item ativo no menu quando você está na página de gerenciar imagens (continuidade visual)

## Fluxo de uso

1. Menu lateral → **Configurações**
2. Aba **Loja** → role até a coluna direita → card **"Vitrine pública"**
3. Clica **"Gerenciar imagens →"**
4. Página dedicada de upload e organização
5. Quando terminar, clica **"← Configurações"** no topo para voltar

## Estrutura

```
male-v9-7/
├── app.py                              ← edição: passa contadores hero para settings
├── requirements.txt
├── templates/
│   ├── base.html                       ← edição: removeu link Hero do sidebar
│   ├── public_catalog.html
│   ├── public_product.html
│   ├── products.html
│   ├── product_detail.html
│   ├── settings.html                   ← edição: card Vitrine pública na aba Loja
│   └── hero_images_admin.html          ← edição: breadcrumb voltar
└── static/
    ├── catalog.css
    ├── catalog-product.css
    └── style.css
```

## Como aplicar

GitHub → Add file → Upload files → arrasta os 12 arquivos respeitando as pastas.
Branch sugerida: `feat/v9-7-vitrine-em-settings`.

## Tudo continua funcionando

- Carrossel de imagens da loja no hero (v9.6)
- Estrelinha de destaque vence o carrossel (v9.6)
- Logos maiores no admin e catálogo (v9.6.1)
- Cores nos swatches (v9.3)
- Labels térmicos otimizados (v9.5)
- Modal com ícones discretos (v9.1)
- Página individual de produto com URL própria (v9)
- Tudo das versões anteriores

## Rollback

GitHub → Pull requests → seu PR → **Revert**.
