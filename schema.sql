-- ============================================================
-- malê · Store Admin · Schema v3
-- ============================================================

CREATE TABLE IF NOT EXISTS gift_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,
    code_last4 TEXT NOT NULL,
    initial_value TEXT NOT NULL,
    current_balance TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'BRL',
    status TEXT NOT NULL CHECK (status IN ('active', 'redeemed', 'cancelled', 'expired')),
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
    is_released INTEGER NOT NULL DEFAULT 1,
    template_name TEXT,
    last_sent_at TEXT,
    last_whatsapp_at TEXT,
    encrypted_code TEXT,
    share_token TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS discount_coupons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    description TEXT,
    type TEXT NOT NULL DEFAULT 'percent' CHECK (type IN ('percent','fixed')),
    value TEXT NOT NULL,
    min_purchase TEXT NOT NULL DEFAULT '0',
    max_uses INTEGER,
    used_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gift_card_redemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gift_card_id INTEGER NOT NULL,
    amount TEXT NOT NULL,
    operator_name TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (gift_card_id) REFERENCES gift_cards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gift_card_id INTEGER,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    actor_name TEXT NOT NULL,
    ip_address TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (gift_card_id) REFERENCES gift_cards(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'operator')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT,
    reset_token TEXT,
    reset_expires_at TEXT
);

-- ============================================================
-- PRODUTOS & ESTOQUE
-- ============================================================
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    is_active INTEGER NOT NULL DEFAULT 1,
    image_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('in','out','adjust','sale','return')),
    qty INTEGER NOT NULL,
    reason TEXT,
    sale_id INTEGER,
    operator_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);

-- ============================================================
-- CLIENTES
-- ============================================================
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    instagram TEXT,
    birthday TEXT,
    notes TEXT,
    whatsapp_opt_in INTEGER NOT NULL DEFAULT 0,
    loyalty_points INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ============================================================
-- VENDAS
-- ============================================================
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER,
    operator_name TEXT NOT NULL,
    subtotal TEXT NOT NULL DEFAULT '0',
    discount_amount TEXT NOT NULL DEFAULT '0',
    discount_coupon_id INTEGER,
    gift_card_id INTEGER,
    gift_card_amount TEXT NOT NULL DEFAULT '0',
    total TEXT NOT NULL DEFAULT '0',
    payment_method TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL,
    FOREIGN KEY (discount_coupon_id) REFERENCES discount_coupons(id) ON DELETE SET NULL,
    FOREIGN KEY (gift_card_id) REFERENCES gift_cards(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS sale_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1,
    unit_price TEXT NOT NULL,
    cost_price TEXT NOT NULL DEFAULT '0',
    FOREIGN KEY (sale_id) REFERENCES sales(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

-- ============================================================
-- CONTABILIDADE
-- ============================================================
CREATE TABLE IF NOT EXISTS expense_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#b5924c'
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER,
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
    FOREIGN KEY (category_id) REFERENCES expense_categories(id) ON DELETE SET NULL
);

-- ============================================================
-- CONFIGURAÇÕES DA LOJA
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
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed')),
    whatsapp_url TEXT,
    sent_at TEXT,
    FOREIGN KEY (campaign_id) REFERENCES whatsapp_campaigns(id) ON DELETE CASCADE,
    FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
);

-- Categorias padrão
INSERT OR IGNORE INTO expense_categories (name, color) VALUES
    ('Aluguel', '#8b3a3a'),
    ('Compra de Mercadoria', '#2c6e49'),
    ('Marketing', '#b5924c'),
    ('Embalagens', '#3a6e8a'),
    ('Salários', '#5a3a8a'),
    ('Taxas e Impostos', '#8a6830'),
    ('Energia / Internet', '#3a6e6e'),
    ('Manutenção', '#6e4e3a'),
    ('Outros', '#5c5650');

-- Configurações padrão
INSERT OR IGNORE INTO store_settings (key, value, updated_at) VALUES
    ('store_name', 'Malê', datetime('now')),
    ('target_margin_pct', '60', datetime('now')),
    ('min_margin_alert_pct', '30', datetime('now')),
    ('tax_rate_pct', '0', datetime('now')),
    ('default_payment', 'Pix', datetime('now')),
    ('loyalty_points_per_real', '1', datetime('now')),
    ('loyalty_redeem_ratio', '100', datetime('now'));

-- ============================================================
-- GPT-INSPIRED ADDITIONS (v3.1)
-- ============================================================

-- Product extra attributes
-- (added via migration in ensure_schema_migrations)

-- Expense supplier + fixed/variable flag
-- (added via migration in ensure_schema_migrations)

-- API lookup tokens (already have qr_token, add api_code)

-- ============================================================
-- VENDAS v2: status, cancelamento, devoluções, crédito
-- ============================================================

-- Créditos de loja (vale-troca)
CREATE TABLE IF NOT EXISTS store_credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER,
    customer_name TEXT,          -- para crédito avulso (sem cadastro)
    amount TEXT NOT NULL,
    reason TEXT,
    source_sale_id INTEGER,      -- venda origem (devolução/troca)
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','used','expired','cancelled')),
    expires_at TEXT,
    used_at TEXT,
    used_in_sale_id INTEGER,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL,
    FOREIGN KEY (source_sale_id) REFERENCES sales(id) ON DELETE SET NULL
);

-- Devoluções de itens
CREATE TABLE IF NOT EXISTS sale_returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1,
    reason TEXT,
    restock INTEGER NOT NULL DEFAULT 1,  -- 1=volta ao estoque, 0=descarte
    credit_id INTEGER,                   -- crédito gerado (se houver)
    operator_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (sale_id) REFERENCES sales(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id),
    FOREIGN KEY (credit_id) REFERENCES store_credits(id) ON DELETE SET NULL
);

-- Notificações / alertas internos
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,         -- 'low_stock','birthday','sale_cancelled','credit_expiring'
    title TEXT NOT NULL,
    body TEXT,
    link TEXT,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- ============================================================
-- FUNCIONALIDADES NOVAS v3.2
-- ============================================================

-- Metas da loja
CREATE TABLE IF NOT EXISTS store_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL UNIQUE,  -- YYYY-MM
    revenue_goal TEXT NOT NULL DEFAULT '0',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Variantes de produto
CREATE TABLE IF NOT EXISTS product_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    size TEXT,
    color TEXT,
    sku TEXT UNIQUE,
    barcode TEXT UNIQUE,
    qr_token TEXT UNIQUE,
    stock_qty INTEGER NOT NULL DEFAULT 0,
    stock_min INTEGER NOT NULL DEFAULT 0,
    cost_price TEXT NOT NULL DEFAULT '0',
    sale_price TEXT NOT NULL DEFAULT '0',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);

-- Histórico de preços
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    field TEXT NOT NULL CHECK (field IN ('cost_price','sale_price')),
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);

-- Pedidos de reposição
CREATE TABLE IF NOT EXISTS restock_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','received','cancelled')),
    supplier TEXT,
    notes TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    received_at TEXT
);

CREATE TABLE IF NOT EXISTS restock_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    qty_ordered INTEGER NOT NULL DEFAULT 1,
    qty_received INTEGER,
    unit_cost TEXT,
    FOREIGN KEY (order_id) REFERENCES restock_orders(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id)
);
