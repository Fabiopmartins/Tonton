# Manual de Uso — Sistema tonton

> Sistema de gestão para loja de moda · admin de produtos, vendas, vale-presentes, clientes e relatórios.

**Versão do manual:** 1.0 · abril 2026
**Compatível com:** tonton admin v3.2+ (com módulo de variantes e promoções)

---

## Sumário

1. [Primeiro acesso](#1-primeiro-acesso)
2. [Cadastro de produtos](#2-cadastro-de-produtos)
3. [Variantes (tamanho × cor)](#3-variantes-tamanho--cor)
4. [Promoções](#4-promoções)
5. [Ponto de venda (PDV)](#5-ponto-de-venda-pdv)
6. [Clientes e fidelização](#6-clientes-e-fidelização)
7. [Vale-presentes (gift cards)](#7-vale-presentes-gift-cards)
8. [Cupons de desconto](#8-cupons-de-desconto)
9. [Reposição de estoque](#9-reposição-de-estoque)
10. [Relatórios](#10-relatórios)
11. [Catálogo público](#11-catálogo-público)
12. [Configurações da loja](#12-configurações-da-loja)
13. [Solução de problemas](#13-solução-de-problemas)

---

## 1. Primeiro acesso

### Login
- Acesse a URL do sistema (ex: `https://sualoja.app`)
- Use o e-mail e senha cadastrados pelo administrador
- Em caso de senha esquecida, use **"Esqueci minha senha"** para receber link por e-mail

### Trocar a senha
1. Menu superior → ícone de perfil → **Alterar senha**
2. Senha atual + nova senha (mínimo 8 caracteres)

### Papéis de usuário
- **Admin**: acesso total, incluindo configurações e usuários
- **Operador**: vendas, clientes, consulta de produtos. Não acessa configurações nem relatórios financeiros sensíveis

---

## 2. Cadastro de produtos

### Criar um produto novo
**Caminho:** Produtos → **Novo produto**

**Campos obrigatórios:**
- **Nome** — descritivo (ex: "Vestido linho tangerina")
- **Custo** — quanto a peça custou para a loja
- **Preço de venda** — valor cobrado do cliente

**Campos opcionais (mas recomendados):**
- **SKU** — código interno. Se vazio, o sistema gera automaticamente
- **Código de barras (EAN-13)** — para leitura por scanner
- **Categoria** — usada em filtros e relatórios ABC (ex: vestidos, saias, acessórios)
- **Marca** — útil para multi-marca
- **NCM, CFOP, Origem** — necessários para emissão de pré-nota fiscal
- **Estoque inicial** e **estoque mínimo** — o mínimo dispara alerta de reposição
- **Foto** — JPEG/PNG até 5 MB. Aparece no PDV, catálogo e etiquetas

### Editar produto
Lista de produtos → menu **"⋯"** → **Editar**

### Excluir produto
Lista de produtos → menu **"⋯"** → **Apagar produto**

> **Atenção:** se o produto tiver vendas no histórico, ele é apenas **desativado** (não aparece mais nas listas), preservando o histórico contábil. Exclusão definitiva só ocorre se nunca houve movimentação.

### Histórico de preços
Cada alteração de custo ou preço é registrada automaticamente. Para consultar:
Lista de produtos → menu **"⋯"** → **Histórico de preços**

---

## 3. Variantes (tamanho × cor)

Use variantes quando o mesmo modelo tem versões diferentes — por exemplo, 4P, 3M, 2G, 1GG da mesma saia.

### Cadastro em lote ao criar o produto
Na tela **Novo produto**, na seção **Variações**:

1. **Tamanhos** — digite separados por vírgula: `P, M, G, GG`
2. **Cores** — digite separadas por vírgula: `tangerina, preto`. Deixe vazio se não houver variação de cor
3. **Matriz** — preenche a quantidade de cada combinação

**Exemplo:** 10 saias em tamanhos diferentes, todas tangerina:

| Tamanho \ Cor | tangerina |
|---|---|
| P | 4 |
| M | 3 |
| G | 2 |
| GG | 1 |

Ao salvar, o sistema cria 4 variantes vinculadas ao produto-pai, cada uma com SKU próprio (ex: `VL001-P-TAN-A3`), estoque próprio e movimento de entrada.

### Adicionar variantes a um produto existente
Lista de produtos → menu **"⋯"** → **Variantes** → preencher linhas no painel direito → **Salvar variantes**

### Ajustar estoque de uma variante
Tela de variantes → linha da variante → seletor `+ / −` + quantidade → **OK**

### Pontos importantes
- **Não some o estoque do produto-pai com o das variantes.** Se você cadastrou variantes, deixe `Estoque inicial` do pai como `0` para não duplicar contagem.
- **Custo e preço** das variantes herdam do produto-pai ao criar. Você pode ajustar individualmente depois.
- **SKU automático** segue o padrão `{SKU-pai}-{TAM}-{COR}-{hash}`. Exemplo: `VL001-GG-PRE-7B`.

---

## 4. Promoções

Promoções funcionam tanto no produto-pai quanto em variantes específicas. Útil para queimar tamanhos parados (ex: GG na liquidação) ou cores fora de estação.

### Promover uma variante específica
1. Tela de variantes do produto
2. Botão **Promover** na linha desejada
3. No modal, escolha:
   - **% desconto** — informe percentual (ex: 30) e veja o preço final em tempo real
   - **R$ fixo** — informe diretamente o preço promocional
4. **Validade** (opcional) — após esta data, sistema volta ao preço cheio automaticamente
5. **Aplicar**

### Promover várias variantes em lote
1. Tela de variantes
2. Marque o checkbox de cada variante (ou clique no checkbox do cabeçalho para todas)
3. Barra inferior aparece com "**N variante(s) selecionada(s)**"
4. Informe **% desconto** e (opcional) **data limite**
5. **Aplicar promoção**

> Cada variante recebe seu próprio preço promocional calculado sobre seu preço cheio individual. Funciona corretamente mesmo se as variantes tiverem preços diferentes entre si.

### Remover promoção
- **Por linha:** botão **Editar promo** → **Remover promoção**
- **Em lote:** marque as variantes → barra inferior → **Remover promoção**

### Como a promoção aparece
- **Lista de produtos:** preço cheio riscado, preço promocional em destaque magenta
- **Detalhe do produto:** "de R$ 199,00 por R$ 139,30" + data de validade
- **Catálogo público:** mesmo padrão "de/por"
- **PDV:** preço promocional já aparece no card e é cobrado automaticamente

### Expiração automática
Quando passa da data de validade, o sistema **automaticamente** volta a usar o preço cheio. Não há job/cron — a verificação é feita a cada consulta. Você não precisa "desligar" a promoção manualmente.

### Regras de validação
- Desconto percentual deve estar **entre 0% e 100%** (exclusivos)
- Preço fixo deve ser **maior que zero** e **menor que o preço cheio**
- Sistema bloqueia valores inválidos com mensagem clara

---

## 5. Ponto de venda (PDV)

**Caminho:** Vendas → **Nova venda**

### Fluxo básico
1. **Buscar produto** — barra de busca por nome, SKU ou categoria. Pode usar leitor de código de barras
2. **Clicar no card** do produto para adicionar ao carrinho
3. **Ajustar quantidade** no carrinho à direita
4. **Cliente** (opcional) — vincular a uma cliente cadastrada para fidelização
5. **Cupom de desconto** (opcional) — campo `Cupom`, sistema valida regras
6. **Desconto manual** (opcional) — valor em R$ aplicado ao subtotal
7. **Forma de pagamento** — dinheiro, PIX, cartão, vale-presente
8. **Finalizar venda**

### Promoções no PDV
- Produtos em promoção mostram preço **com desconto já aplicado**
- O preço cobrado é **sempre revalidado no servidor** — mesmo que alguém manipule o frontend, o sistema usa o preço promocional vigente
- Se a promoção expirou entre o momento de adicionar ao carrinho e finalizar, o preço cheio é cobrado

### Vale-presente como pagamento
- Forma de pagamento **Vale-presente** → informe o código → sistema valida saldo
- Se o saldo for menor que o total, complete com outra forma

### Cancelamento
Após finalizar:
- Vendas → abrir a venda → **Cancelar venda**
- Estoque é restituído automaticamente
- Status fica `cancelled`, mantém registro

---

## 6. Clientes e fidelização

### Cadastro
Clientes → **Nova cliente**

**Campos úteis:**
- Nome, telefone (WhatsApp), e-mail
- Aniversário — usado em campanhas e tela de aniversariantes
- Endereço completo — preenche dados de envio na venda
- Observações — preferências, restrições, histórico relevante

### Tela de detalhe da cliente
- Histórico completo de compras
- Produtos preferidos (categorias mais compradas)
- Saldo de créditos (devoluções)
- Botões para WhatsApp e ligação direta

### Aniversariantes
Marketing → **Aniversariantes** → lista do mês com botão de WhatsApp para envio rápido de mensagem personalizada

### Créditos (devoluções)
Quando uma cliente devolve um item, você pode gerar um crédito ao invés de devolver dinheiro:
1. Detalhe da cliente → **Novo crédito**
2. Valor + motivo
3. Crédito fica disponível para usar em próximas vendas

---

## 7. Vale-presentes (gift cards)

### Criar um vale
Vale-presentes → **Novo vale-presente**

**Campos:**
- **Valor inicial** — saldo do cartão
- **Comprador** — quem está pagando
- **Destinatário** — para quem é o presente (nome, e-mail, WhatsApp)
- **Validade** — data de expiração
- **Template** — design visual do cartão (ex: aniversário, romântico, neutro)

### Liberação
Por padrão, o vale é **liberado imediatamente**. Você pode criar como **não liberado** se quiser entregar fisicamente em data específica.

### Envio
- **Por e-mail** — botão **Enviar por e-mail** dispara o cartão com código
- **Por WhatsApp** — botão **WhatsApp** abre conversa pré-preenchida com link de visualização
- **Imprimir** — gerar PDF do cartão para impressão física

### Resgate
Cliente apresenta o código → no PDV, forma de pagamento **Vale-presente** → digite o código → saldo é debitado

### Saldo parcial
Se o vale tinha R$ 200 e a compra foi R$ 150, o vale fica com R$ 50 de saldo para próxima compra.

---

## 8. Cupons de desconto

**Caminho:** Marketing → **Cupons**

### Tipos
- **Percentual** — ex: 10% off
- **Valor fixo** — ex: R$ 30 off

### Regras configuráveis
- **Compra mínima** — só vale se o subtotal for >= valor X
- **Máximo de usos** — limite global (todas as clientes)
- **Validade** — data de expiração

### Aplicação
No PDV, cliente informa o código → sistema valida regras → desconto aparece no resumo da venda.

> **Cumulativo com promoção:** o cupom incide sobre o preço já com promoção. Se quiser bloquear essa combinação, peça customização.

---

## 9. Reposição de estoque

Para registrar entrada de mercadoria de fornecedor:

**Caminho:** Estoque → **Novo pedido de reposição**

### Fluxo
1. **Status inicial:** `pending` (pedido criado, ainda não enviado)
2. Adicione produtos com quantidade e custo unitário
3. Quando enviar para o fornecedor: **Marcar como enviado** → `sent`
4. Quando receber a mercadoria: **Receber** → estoque é incrementado, custos médios são atualizados, status vira `received`
5. Pode cancelar antes de receber

### Alertas de baixo estoque
Dashboard → seção **Alertas de estoque** mostra produtos com `stock_qty <= stock_min`. Use isso para gerar reposição.

---

## 10. Relatórios

**Caminho:** Relatórios

### Disponíveis
- **Vendas** — receita, lucro bruto, ticket médio, por período
- **Produtos ABC** — classifica produtos por contribuição na receita (curva ABC)
- **Contábil** — receita, custo, margem, formas de pagamento
- **Aniversariantes** — para campanhas de marketing
- **Metas** — comparação receita realizada vs meta mensal

### Filtros comuns
- Período (data inicial / final)
- Categoria
- Vendedora (operador que registrou a venda)

### Exportação
A maioria dos relatórios pode ser exportada em **CSV** ou impressa em **PDF**.

---

## 11. Catálogo público

URL pública: `/catalog/<token>`

### O que aparece
- Produtos ativos com foto
- Preço (ou "Sob consulta" se desativado)
- Promoções com "de R$ X por R$ Y"
- Botão de WhatsApp ou Instagram para contato direto

### O que NÃO aparece
- Custo
- Margem
- Estoque
- SKU interno

### Compartilhamento
Use o link em:
- Bio do Instagram
- Status do WhatsApp
- E-mail marketing
- QR code para vitrine física

### Configuração
Configurações → seção **Catálogo público** controla:
- Mostrar preços ou "Sob consulta"
- Botão principal: WhatsApp ou Instagram
- Texto de boas-vindas
- Categorias visíveis

---

## 12. Configurações da loja

**Caminho:** Configurações (apenas admins)

### Principais seções
- **Identidade** — nome, logo, cores
- **Contato** — telefone, WhatsApp, Instagram, endereço
- **Fiscal** — CNPJ, regime tributário, NCM padrão
- **Margem-alvo** — percentual sugerido em novos produtos (ex: 60%)
- **Catálogo público** — visibilidade e comportamento
- **E-mail (SMTP)** — credenciais para envio de vales-presente e notificações
- **Google login** — habilitar entrada via Google
- **Metas mensais** — receita esperada por mês

### Usuários
Configurações → **Usuários** → criar/editar/desativar usuários e papéis

---

## 13. Solução de problemas

### "Estoque insuficiente" no PDV mas eu sei que tem
- Verifique se você cadastrou variantes. **Se cadastrou, o estoque está nas variantes, não no produto-pai.** No PDV ainda não há seleção automática de variante (limitação atual).

### Promoção não aparece
- Confirme que `promo_price` foi informado e é menor que `sale_price`
- Confirme que `promo_until` (se informado) é uma data **futura**
- Limpe cache do navegador (F5)

### Não consigo deletar um produto
- Se há vendas vinculadas, o sistema **desativa** ao invés de deletar. Isso é correto e protege o histórico contábil.
- Para "esconder" um produto sem deletar, use **Editar** → desmarcar **Ativo**

### E-mail de vale-presente não chega
- Verifique configuração SMTP em **Configurações → E-mail**
- Cheque pasta de spam do destinatário
- Botão **Reenviar** na tela do vale

### Esqueci a senha do admin
Como admin de servidor, defina via variável de ambiente `ADMIN_PASSWORD_HASH` e reinicie. Como admin do sistema com outro usuário admin, peça reset por outro admin.

### Lista de produtos lenta com muitos itens
- Use os filtros (categoria, busca por nome/SKU) para reduzir a query
- Considere desativar produtos antigos que não vendem mais

---

## Apêndice A — Atalhos úteis

| Ação | Caminho rápido |
|---|---|
| Nova venda | Botão flutuante **+ Venda** no canto inferior direito |
| Novo produto | Produtos → **Novo produto** |
| Buscar produto | PDV → barra de busca, ou leitor de código de barras |
| Ver alertas de estoque | Dashboard → painel **Alertas** |
| Aniversariantes do mês | Marketing → **Aniversariantes** |

---

## Apêndice B — Boas práticas operacionais

1. **Cadastre o SKU desde o início** — facilita conferência física, etiquetagem e integração com fornecedores futuros
2. **Use estoque mínimo realista** — muito alto gera alerta inútil, muito baixo deixa faltar
3. **Promoções com data de validade** — evita esquecer e vender abaixo do custo
4. **Backup do banco** — combine com seu provedor de hospedagem (Railway, Render, etc.) backup diário automático
5. **Não compartilhe login** — crie um usuário por pessoa, mesmo que todas sejam admin. Auditoria fica clara
6. **Revise relatórios mensalmente** — curva ABC mostra o que vale a pena repor e o que está parado

---

*Manual gerado para o sistema tonton · em caso de dúvida técnica, consulte o desenvolvedor responsável.*
