-- ============================================================
-- tonton · Painel Admin · Schema Postgres v3.2 (consolidado)
-- Equivalente a schema.sql + ensure_schema_migrations()
-- ============================================================
-- Convenções:
--   * BIGSERIAL preserva comportamento de AUTOINCREMENT
--   * SMALLINT 0/1 mantido onde o código faz "col=1" (não trocar por BOOLEAN)
--   * TEXT para datas ISO-8601 (código trata strings, não timestamptz)
--   * BYTEA para image_blob (BLOB do SQLite)
-- ============================================================

CREATE TABLE IF NOT EXISTS gift_cards (
    id BIGSERIAL PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    code_last4 TEXT NOT NULL,
    initial_value TEXT NOT NULL,
    current_balance TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'BRL',
    status TEXT NOT NULL CHECK (status IN ('active','redeemed','cancelled','expired')),
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    recipient_name TEXT,
    recipient_email TEXT,
    recipient_phone TEXT,
    buyer_name TEXT,
    buyer_phone TEXT,
    order_reference TEXT,
    notes TEXT,
    qr_token TEXT UNIQUE,
    is_released SMALLINT NOT NULL DEFAULT 1,
    template_name TEXT,
    last_sent_at TEXT,
    last_whatsapp_at TEXT,
    encrypted_code TEXT,
    share_token TEXT UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_gift_cards_order ON gift_cards(order_reference);

CREATE TABLE IF NOT EXISTS discount_coupons (
    id BIGSERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    description TEXT,
    type TEXT NOT NULL DEFAULT 'percent' CHECK (type IN ('percent','fixed')),
    value TEXT NOT NULL,
    min_purchase TEXT NOT NULL DEFAULT '0',
    max_uses INTEGER,
    used_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    is_active SMALLINT NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gift_card_redemptions (
    id BIGSERIAL PRIMARY KEY,
    gift_card_id BIGINT NOT NULL REFERENCES gift_cards(id) ON DELETE CASCADE,
    amount TEXT NOT NULL,
    operator_name TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGSERIAL PRIMARY KEY,
    gift_card_id BIGINT REFERENCES gift_cards(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    actor_name TEXT NOT NULL,
    ip_address TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','operator')),
    is_active SMALLINT NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT,
    reset_token TEXT,
    reset_expires_at TEXT,
    google_sub TEXT,
    last_auth_provider TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_reset ON users(reset_token);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google ON users(google_sub) WHERE google_sub IS NOT NULL;

-- ============================================================
-- PRODUTOS & ESTOQUE
-- ============================================================
CREATE TABLE IF NOT EXISTS products (
    id BIGSERIAL PRIMARY KEY,
    sku TEXT UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    category TEXT,
    cost_price TEXT NOT NULL DEFAULT '0',
    sale_price TEXT NOT NULL DEFAULT '0',
    stock_qty INTEGER NOT NULL DEFAULT 0,
    stock_min INTEGER NOT NULL DEFAULT 2,
    unit TEXT NOT NULL DEFAULT 'un',
    barcode TEXT UNIQUE,
    qr_token TEXT UNIQUE,
    is_active SMALLINT NOT NULL DEFAULT 1,
    image_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    brand TEXT,
    size TEXT,
    color TEXT,
    target_margin_pct TEXT NOT NULL DEFAULT '60',
    image_blob BYTEA,
    image_mime TEXT,
    ncm TEXT,
    cfop TEXT,
    origin_code TEXT,
    is_featured SMALLINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('in','out','adjust','sale','return')),
    qty INTEGER NOT NULL,
    reason TEXT,
    sale_id BIGINT,
    operator_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- ============================================================
-- CLIENTES
-- ============================================================
CREATE TABLE IF NOT EXISTS customers (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    instagram TEXT,
    birthday TEXT,
    notes TEXT,
    whatsapp_opt_in SMALLINT NOT NULL DEFAULT 0,
    loyalty_points INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    marketing_opt_in SMALLINT NOT NULL DEFAULT 0
);

-- ============================================================
-- VENDAS
-- ============================================================
CREATE TABLE IF NOT EXISTS sales (
    id BIGSERIAL PRIMARY KEY,
    customer_id BIGINT REFERENCES customers(id) ON DELETE SET NULL,
    operator_name TEXT NOT NULL,
    subtotal TEXT NOT NULL DEFAULT '0',
    discount_amount TEXT NOT NULL DEFAULT '0',
    discount_coupon_id BIGINT REFERENCES discount_coupons(id) ON DELETE SET NULL,
    gift_card_id BIGINT REFERENCES gift_cards(id) ON DELETE SET NULL,
    gift_card_amount TEXT NOT NULL DEFAULT '0',
    total TEXT NOT NULL DEFAULT '0',
    payment_method TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    sale_number TEXT,
    buyer_name_free TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    cancelled_at TEXT,
    cancelled_by TEXT,
    cancel_reason TEXT,
    credit_id BIGINT,
    shipping_name TEXT,
    shipping_document TEXT,
    shipping_cep TEXT,
    shipping_street TEXT,
    shipping_number TEXT,
    shipping_complement TEXT,
    shipping_neighborhood TEXT,
    shipping_city TEXT,
    shipping_state TEXT
);

-- v9: status de pagamento desacoplado do status da venda
ALTER TABLE sales ADD COLUMN IF NOT EXISTS payment_status TEXT NOT NULL DEFAULT 'paid';
ALTER TABLE sales ADD COLUMN IF NOT EXISTS payment_confirmed_at TEXT;
ALTER TABLE sales ADD COLUMN IF NOT EXISTS payment_confirmed_by TEXT;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.constraint_column_usage
                 WHERE table_name = 'sales' AND constraint_name = 'sales_payment_status_check') THEN
    ALTER TABLE sales ADD CONSTRAINT sales_payment_status_check
      CHECK (payment_status IN ('paid','pending','failed','refunded'));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_sales_payment_pending ON sales(payment_status)
  WHERE payment_status = 'pending';

CREATE TABLE IF NOT EXISTS sale_items (
    id BIGSERIAL PRIMARY KEY,
    sale_id BIGINT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL DEFAULT 1,
    unit_price TEXT NOT NULL,
    cost_price TEXT NOT NULL DEFAULT '0'
);

-- ============================================================
-- CONTABILIDADE
-- ============================================================
CREATE TABLE IF NOT EXISTS expense_categories (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#b5924c'
);

CREATE TABLE IF NOT EXISTS expenses (
    id BIGSERIAL PRIMARY KEY,
    category_id BIGINT REFERENCES expense_categories(id) ON DELETE SET NULL,
    description TEXT NOT NULL,
    amount TEXT NOT NULL,
    expense_date TEXT NOT NULL,
    recurrence TEXT NOT NULL DEFAULT 'once' CHECK (recurrence IN ('once','monthly','weekly')),
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled')),
    cancelled_at TEXT,
    cancelled_by TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    is_fixed SMALLINT NOT NULL DEFAULT 1,
    supplier TEXT
);

-- ============================================================
-- CONFIGURAÇÕES
-- ============================================================
CREATE TABLE IF NOT EXISTS store_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ============================================================
-- CAMPANHAS WHATSAPP
-- ============================================================
CREATE TABLE IF NOT EXISTS whatsapp_campaigns (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    message_template TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','sent','scheduled')),
    sent_count INTEGER NOT NULL DEFAULT 0,
    target_filter TEXT,
    scheduled_at TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_logs (
    id BIGSERIAL PRIMARY KEY,
    campaign_id BIGINT NOT NULL REFERENCES whatsapp_campaigns(id) ON DELETE CASCADE,
    customer_id BIGINT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed')),
    whatsapp_url TEXT,
    sent_at TEXT,
    UNIQUE (campaign_id, customer_id)  -- viabiliza INSERT ... ON CONFLICT DO NOTHING
);

-- ============================================================
-- v3 adicionais
-- ============================================================
CREATE TABLE IF NOT EXISTS store_credits (
    id BIGSERIAL PRIMARY KEY,
    customer_id BIGINT REFERENCES customers(id) ON DELETE SET NULL,
    customer_name TEXT,
    amount TEXT NOT NULL,
    reason TEXT,
    source_sale_id BIGINT REFERENCES sales(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','used','expired','cancelled')),
    expires_at TEXT,
    used_at TEXT,
    used_in_sale_id BIGINT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sale_returns (
    id BIGSERIAL PRIMARY KEY,
    sale_id BIGINT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL DEFAULT 1,
    reason TEXT,
    restock SMALLINT NOT NULL DEFAULT 1,
    credit_id BIGINT REFERENCES store_credits(id) ON DELETE SET NULL,
    operator_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id BIGSERIAL PRIMARY KEY,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    link TEXT,
    is_read SMALLINT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS store_goals (
    id BIGSERIAL PRIMARY KEY,
    month TEXT NOT NULL UNIQUE,
    revenue_goal TEXT NOT NULL DEFAULT '0',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_variants (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    size TEXT,
    color TEXT,
    sku TEXT UNIQUE,
    barcode TEXT UNIQUE,
    qr_token TEXT UNIQUE,
    stock_qty INTEGER NOT NULL DEFAULT 0,
    stock_min INTEGER NOT NULL DEFAULT 0,
    cost_price TEXT NOT NULL DEFAULT '0',
    sale_price TEXT NOT NULL DEFAULT '0',
    promo_price TEXT,
    promo_until TEXT,
    is_active SMALLINT NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- Migrations idempotentes (bases já existentes)
ALTER TABLE product_variants ADD COLUMN IF NOT EXISTS promo_price TEXT;
ALTER TABLE product_variants ADD COLUMN IF NOT EXISTS promo_until TEXT;
ALTER TABLE products         ADD COLUMN IF NOT EXISTS promo_price TEXT;
ALTER TABLE products         ADD COLUMN IF NOT EXISTS promo_until TEXT;

-- Onda 1 · Ficha técnica de roupa
ALTER TABLE products ADD COLUMN IF NOT EXISTS composition JSONB;
ALTER TABLE products ADD COLUMN IF NOT EXISTS care_wash TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS fabric_type TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS fabric_weight_gsm INTEGER;
ALTER TABLE products ADD COLUMN IF NOT EXISTS fit TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS length_class TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS country_of_origin TEXT DEFAULT 'BR';
CREATE INDEX IF NOT EXISTS idx_products_composition_gin ON products USING GIN (composition);

-- v10: galeria de imagens por produto (multi-foto, agrupada por cor)
CREATE TABLE IF NOT EXISTS product_images (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    color TEXT,                     -- NULL = foto genérica (caimento, detalhe)
    image_blob BYTEA NOT NULL,
    image_mime TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_primary SMALLINT NOT NULL DEFAULT 0,
    image_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_images_pid ON product_images(product_id);
CREATE INDEX IF NOT EXISTS idx_product_images_color ON product_images(product_id, color);

CREATE TABLE IF NOT EXISTS price_history (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    field TEXT NOT NULL CHECK (field IN ('cost_price','sale_price')),
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS restock_orders (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','received','cancelled')),
    supplier TEXT,
    notes TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    received_at TEXT
);

CREATE TABLE IF NOT EXISTS restock_order_items (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES restock_orders(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id),
    qty_ordered INTEGER NOT NULL DEFAULT 1,
    qty_received INTEGER,
    unit_cost TEXT
);

CREATE TABLE IF NOT EXISTS catalog_interest (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT REFERENCES products(id) ON DELETE CASCADE,
    customer_phone TEXT,
    customer_name TEXT,
    ip TEXT,
    user_agent TEXT,
    contacted SMALLINT NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interest_product ON catalog_interest(product_id);
CREATE INDEX IF NOT EXISTS idx_interest_created ON catalog_interest(created_at DESC);

CREATE TABLE IF NOT EXISTS fashion_calendar (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    event_date TEXT NOT NULL,
    kind TEXT,
    notes TEXT,
    is_active SMALLINT NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE (title, event_date)
);

-- ============================================================
-- SEEDS
-- ============================================================
INSERT INTO expense_categories (name, color) VALUES
    ('Aluguel', '#8b3a3a'),
    ('Compra de Mercadoria', '#2c6e49'),
    ('Marketing', '#b5924c'),
    ('Embalagens', '#3a6e8a'),
    ('Salários', '#5a3a8a'),
    ('Taxas e Impostos', '#8a6830'),
    ('Energia / Internet', '#3a6e6e'),
    ('Manutenção', '#6e4e3a'),
    ('Outros', '#5c5650')
ON CONFLICT (name) DO NOTHING;

INSERT INTO store_settings (key, value, updated_at) VALUES
    ('store_name', 'Malê', (NOW() AT TIME ZONE 'UTC')::text),
    ('target_margin_pct', '60', (NOW() AT TIME ZONE 'UTC')::text),
    ('min_margin_alert_pct', '30', (NOW() AT TIME ZONE 'UTC')::text),
    ('tax_rate_pct', '0', (NOW() AT TIME ZONE 'UTC')::text),
    ('default_payment', 'Pix', (NOW() AT TIME ZONE 'UTC')::text),
    ('loyalty_points_per_real', '1', (NOW() AT TIME ZONE 'UTC')::text),
    ('loyalty_redeem_ratio', '100', (NOW() AT TIME ZONE 'UTC')::text)
ON CONFLICT (key) DO NOTHING;
