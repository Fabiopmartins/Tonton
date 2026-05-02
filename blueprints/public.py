"""
blueprints/public.py — Blueprint do catálogo público.

Escopo (rotas anônimas, sem login):
    GET  /catalogo                       → public_catalog
    GET  /catalogo/<slug>                → public_product
    GET  /catalogo/cat/<cat_slug>        → public_catalog_category
    GET  /sitemap.xml                    → public_sitemap
    GET  /robots.txt                     → public_robots
    POST /api/catalog/interest           → log_interest
    GET  /public/product-image/<int:pid> → public_product_image

Notas (Onda 2 — Etapa C):

    1. Endpoints PRESERVAM nomes globais via aliases registrados em
       init_public_blueprint(). Templates seguem usando
       url_for(\'public_catalog\'), etc.

    2. Helpers (get_db, get_setting, effective_price, slugify,
       build_product_seo, client_ip, rate_limited, utc_now_iso) são
       importados de app.py em top-level. O ciclo é seguro porque o
       blueprint é importado depois de app.py estar totalmente
       carregado (no fim de app.py, após `app = create_app()`).
       Mesma técnica usada em lookbook.py.

    3. /redeem (interno, login_required) NÃO está aqui — fica em
       app.py até a extração do blueprint operations.

    4. /interest (admin dashboard) também NÃO está aqui — é admin-only.

    5. Código das views é EXTRAÇÃO VERBATIM de app.py. Comportamento
       idêntico ao original. Apenas o decorator @app.route foi trocado
       por @public_bp.route(..., endpoint="...").
"""
from __future__ import annotations

import re
from decimal import Decimal
from xml.sax.saxutils import escape as xml_escape

from flask import (
    Blueprint, current_app, request, render_template, redirect, url_for,
    abort, Response, jsonify, session, flash,
)

# Helpers do app principal — import top-level. Funciona porque public.py
# é importado em app.py APÓS `app = create_app()`, garantindo que app.py
# já completou seu loading. Mesma técnica usada em lookbook.py.
from app import (
    get_db, get_setting, effective_price, slugify, build_product_seo,
    client_ip, rate_limited, utc_now_iso, size_sort_key,
    INSTAGRAM_URL, CATALOG_COLOR_MAP,
)


public_bp = Blueprint("public", __name__, template_folder="../templates")


@public_bp.route("/catalogo", endpoint="public_catalog")
def public_catalog():
    db   = get_db()
    q    = request.args.get("q","").strip()
    cat  = request.args.get("cat","").strip()
    sort = request.args.get("sort", "").strip().lower()
    show_prices = get_setting("catalog_show_prices","1") == "1"

    # Produtos ativos cujo ESTOQUE AGREGADO (pai + variantes ativas) > 0
    sql = """
        SELECT p.*,
               COALESCE(p.stock_qty, 0) +
               COALESCE((SELECT SUM(v.stock_qty) FROM product_variants v
                         WHERE v.product_id = p.id AND v.is_active = 1), 0) AS total_stock
        FROM products p
        WHERE p.is_active = 1
          AND (
            COALESCE(p.stock_qty, 0) +
            COALESCE((SELECT SUM(v.stock_qty) FROM product_variants v
                      WHERE v.product_id = p.id AND v.is_active = 1), 0)
          ) > 0
    """
    params = []
    if q:
        sql += " AND (p.name ILIKE %s OR p.description ILIKE %s)"; params += [f"%{q}%",f"%{q}%"]
    if cat:
        sql += " AND p.category=%s"; params.append(cat)
    if sort == "price_asc":
        sql += " ORDER BY CAST(COALESCE(p.sale_price, '0') AS DOUBLE PRECISION) ASC, p.name ASC"
    elif sort == "price_desc":
        sql += " ORDER BY CAST(COALESCE(p.sale_price, '0') AS DOUBLE PRECISION) DESC, p.name ASC"
    else:
        sql += " ORDER BY p.created_at DESC, p.id DESC"
    rows = db.execute(sql, params).fetchall()

    # Para cada produto, agrega tamanhos/cores/faixa de preço a partir das variantes
    products = []
    for r in rows:
        p_dict = dict(r)
        variants = db.execute("""
            SELECT size, color, stock_qty, sale_price, promo_price, promo_until
            FROM product_variants
            WHERE product_id = %s AND is_active = 1 AND stock_qty > 0
            ORDER BY size, color
        """, (p_dict["id"],)).fetchall()

        sizes_set = []
        colors_set = []
        sizes_by_color = {}  # v10.10: {cor_lower: [tamanhos com estoque>0]}
        effective_prices = []

        for v in variants:
            sz = (v["size"] or "").strip()
            cl = (v["color"] or "").strip()
            if sz and sz not in sizes_set:
                sizes_set.append(sz)
            if cl and cl not in colors_set:
                colors_set.append(cl)
            # Mapa cor → tamanhos com estoque (já filtrado por stock_qty > 0 no SQL)
            if cl and sz:
                key = cl.lower()
                if key not in sizes_by_color:
                    sizes_by_color[key] = []
                if sz not in sizes_by_color[key]:
                    sizes_by_color[key].append(sz)
            try:
                eff = Decimal(str(effective_price(v)))
                if eff > 0:
                    effective_prices.append(eff)
            except Exception:
                pass

        # Ordena tamanhos de cada cor pela mesma chave semântica
        sizes_by_color = {
            cor: sorted(szs, key=size_sort_key)
            for cor, szs in sizes_by_color.items()
        }

        # Se não houver variantes, usa preço efetivo do pai
        if not effective_prices:
            try:
                eff_parent = Decimal(str(effective_price(p_dict)))
                if eff_parent > 0:
                    effective_prices.append(eff_parent)
            except Exception:
                pass

        p_dict["available_sizes"] = sorted(sizes_set, key=size_sort_key)
        p_dict["available_colors"] = colors_set
        p_dict["sizes_by_color"] = sizes_by_color
        p_dict["has_variants"] = len(variants) > 0

        # v10: mapeia cor → URL da foto (para swatches no catálogo)
        # v10.3: anexa ?v={image_version} para invalidar cache após rotações
        gallery = db.execute(
            "SELECT id, color, image_version, is_primary FROM product_images "
            "WHERE product_id=%s ORDER BY is_primary DESC, sort_order ASC, id ASC",
            (p_dict["id"],)
        ).fetchall()
        images_by_color = {}
        primary_version = None
        for g in gallery:
            if g["is_primary"] and primary_version is None:
                primary_version = g["image_version"]
            key = (g["color"] or "").strip().lower()
            if key and key not in images_by_color:
                images_by_color[key] = url_for(
                    "public_product_gallery_image",
                    pid=p_dict["id"], img_id=g["id"], v=g["image_version"]
                )
        p_dict["images_by_color"] = images_by_color
        p_dict["primary_image_version"] = primary_version or 1
        # has_image: true se há entrada na galeria OU foto legacy.
        # O template usa esta flag (não image_mime) para decidir se mostra
        # a tag <img> ou o placeholder. Resolve o caso de produto cuja foto
        # foi cadastrada via aba Galeria (legacy fica vazio).
        p_dict["has_image"] = (len(gallery) > 0) or bool(p_dict.get("image_mime"))
        if effective_prices:
            p_dict["min_price"] = str(min(effective_prices))
            p_dict["max_price"] = str(max(effective_prices))
        else:
            p_dict["min_price"] = p_dict.get("sale_price") or "0"
            p_dict["max_price"] = p_dict.get("sale_price") or "0"

        products.append(p_dict)

    categories = db.execute("""
        SELECT DISTINCT category FROM products
        WHERE is_active=1 AND category IS NOT NULL
          AND (
            COALESCE(stock_qty,0) +
            COALESCE((SELECT SUM(v.stock_qty) FROM product_variants v
                      WHERE v.product_id = products.id AND v.is_active = 1), 0)
          ) > 0
        ORDER BY category
    """).fetchall()

    # Hero do catálogo público (ordem de prioridade):
    # 1. Peça com is_featured=1 → hero full-bleed dessa peça (vence tudo)
    # 2. Imagens da loja cadastradas → carrossel ('store') no hero
    # 3. Sem peças marcadas e sem imagens da loja → modo adaptativo por tamanho:
    #    * 1–4 peças → 'minimal'  (direto ao ponto)
    #    * 5–14      → 'compact'  (faixa fina tipográfica)
    #    * 15+       → 'cover'    (split com 1ª peça à direita)
    # Setting `catalog_hero_mode='off'` força modo minimal independente de tudo.
    manual_mode = (get_setting("catalog_hero_mode", "") or "").strip().lower()
    hero_product = None
    hero_images = []

    # Prioridade 1: peça com estrelinha vence sempre
    featured = [p for p in products if p.get("is_featured")]
    if featured:
        hero_product = featured[0]
        hero_mode = "photo"
    elif manual_mode == "off":
        hero_mode = "minimal"
    else:
        # Prioridade 2: imagens da loja cadastradas
        hero_image_rows = db.execute("""
            SELECT id, caption, image_version, sort_order
            FROM catalog_hero_images
            WHERE is_active = 1
            ORDER BY sort_order ASC, id ASC
        """).fetchall()
        hero_images = [dict(r) for r in hero_image_rows]
        if hero_images:
            hero_mode = "store"
        elif manual_mode == "auto" and products:
            # Modo manual auto (legado)
            hero_product = products[0]
            hero_mode = "cover"
        else:
            # Prioridade 3: auto pelo tamanho do catálogo
            n = len(products)
            if n == 0:
                hero_mode = "solo"
            elif n <= 4:
                hero_mode = "minimal"
            elif n <= 14:
                hero_mode = "compact"
            else:
                hero_mode = "cover"
                hero_product = products[0]

    store_name  = get_setting("store_name","tonton")
    wa_number   = get_setting("store_whatsapp","")
    resp_phones = get_setting("responsible_phones","").strip()
    tagline     = get_setting("catalog_tagline","moda feminina atemporal")
    instagram   = get_setting("catalog_instagram","@tontonlojainfantil")
    click_target = get_setting("catalog_item_click_target", "whatsapp").strip().lower()
    if click_target not in {"whatsapp", "instagram"}:
        click_target = "whatsapp"
    resp_list = [p.strip() for p in resp_phones.replace("\n",",").split(",") if p.strip()]

    # ─── Footer (configurável via /settings) ────────────────────
    footer_credo = get_setting(
        "catalog_footer_credo",
        "Coleções curtas, tecidos honestos.\nCada peça escolhida para durar mais que uma estação."
    ).strip()
    footer_credit = get_setting("catalog_footer_credit", "").strip()
    newsletter_enabled = get_setting("catalog_newsletter_enabled", "0") == "1"
    newsletter_intro = get_setting(
        "catalog_newsletter_intro",
        "primeira a saber das próximas peças"
    ).strip()
    footer_email = get_setting("catalog_footer_email", "").strip()

    # OG image: True quando admin subiu imagem fixa (preferida pelo template).
    og_image_set = bool(get_setting("catalog_og_image_b64", ""))

    # WhatsApp link com mensagem padrão pré-preenchida.
    # Quando visitante clica "Conversar" no hero/header/footer, já vem texto.
    # Vazio = não aplica (link direto sem ?text=).
    wa_default_msg = get_setting(
        "catalog_wa_default",
        "Olá! Vim pelo catálogo da Tonton — gostaria de saber mais."
    ).strip()
    if wa_number and wa_default_msg:
        from urllib.parse import quote
        wa_link = f"https://wa.me/{wa_number}?text={quote(wa_default_msg)}"
    elif wa_number:
        wa_link = f"https://wa.me/{wa_number}"
    else:
        wa_link = ""

    return render_template("public_catalog.html",
        products=products, categories=categories,
        hero_product=hero_product, hero_mode=hero_mode,
        hero_images=hero_images,
        q=q, selected_cat=cat,
        show_prices=show_prices, store_name=store_name,
        wa_number=wa_number, wa_link=wa_link, resp_phones=resp_list,
        tagline=tagline, instagram=instagram,
        click_target=click_target,
        color_map=CATALOG_COLOR_MAP,
        # Footer
        footer_credo=footer_credo,
        footer_credit=footer_credit,
        newsletter_enabled=newsletter_enabled,
        newsletter_intro=newsletter_intro,
        footer_email=footer_email,
        # OG (preview de link em redes sociais)
        og_image_set=og_image_set,
    )



@public_bp.route("/catalogo/<slug>", endpoint="public_product")
def public_product(slug):
    """Página pública individual de uma peça, identificada por slug.
    URL amigável (`/catalogo/cinto-couro-lore`) — compartilhável,
    indexável pelo Google, com SEO completo (Open Graph + JSON-LD)."""
    db = get_db()
    # Slug deve ser sanitizado (proteção paranóica — Flask já restringe)
    slug = (slug or "").strip().lower()
    if not slug or not re.match(r"^[a-z0-9][a-z0-9-]*$", slug):
        abort(404)

    # Recupera produto + agrega variantes/galeria (mesma lógica do catálogo)
    row = db.execute("""
        SELECT p.*,
               COALESCE(p.stock_qty, 0) +
               COALESCE((SELECT SUM(v.stock_qty) FROM product_variants v
                         WHERE v.product_id = p.id AND v.is_active = 1), 0) AS total_stock
        FROM products p
        WHERE p.slug = %s AND p.is_active = 1
        LIMIT 1
    """, (slug,)).fetchone()
    if not row:
        abort(404)

    p = dict(row)
    pid = p["id"]

    # Variantes ativas com estoque
    variants = db.execute("""
        SELECT size, color, stock_qty, sale_price, promo_price, promo_until
        FROM product_variants
        WHERE product_id = %s AND is_active = 1 AND stock_qty > 0
        ORDER BY size, color
    """, (pid,)).fetchall()

    sizes_set, colors_set, sizes_by_color = [], [], {}
    effective_prices = []
    for v in variants:
        sz = (v["size"] or "").strip()
        cl = (v["color"] or "").strip()
        if sz and sz not in sizes_set: sizes_set.append(sz)
        if cl and cl not in colors_set: colors_set.append(cl)
        if cl and sz:
            key = cl.lower()
            sizes_by_color.setdefault(key, [])
            if sz not in sizes_by_color[key]:
                sizes_by_color[key].append(sz)
        try:
            eff = Decimal(str(effective_price(v)))
            if eff > 0: effective_prices.append(eff)
        except Exception:
            pass
    sizes_by_color = {
        cor: sorted(szs, key=size_sort_key)
        for cor, szs in sizes_by_color.items()
    }
    if not effective_prices:
        try:
            eff_parent = Decimal(str(effective_price(p)))
            if eff_parent > 0: effective_prices.append(eff_parent)
        except Exception:
            pass

    # Galeria completa (todas as fotos, ordenadas)
    gallery_rows = db.execute(
        "SELECT id, color, image_version, is_primary, sort_order "
        "FROM product_images WHERE product_id=%s "
        "ORDER BY is_primary DESC, sort_order ASC, id ASC",
        (pid,)
    ).fetchall()
    gallery = []
    images_by_color = {}
    primary_version = None
    for g in gallery_rows:
        if g["is_primary"] and primary_version is None:
            primary_version = g["image_version"]
        url = url_for("public_product_gallery_image",
                      pid=pid, img_id=g["id"], v=g["image_version"])
        gallery.append({
            "id": g["id"],
            "color": (g["color"] or "").strip().lower(),
            "url": url,
            "is_primary": bool(g["is_primary"]),
        })
        key = (g["color"] or "").strip().lower()
        if key and key not in images_by_color:
            images_by_color[key] = url

    p["available_sizes"] = sorted(sizes_set, key=size_sort_key)
    p["available_colors"] = colors_set
    p["sizes_by_color"] = sizes_by_color
    p["has_variants"] = len(variants) > 0
    p["images_by_color"] = images_by_color
    p["primary_image_version"] = primary_version or 1
    p["has_image"] = (len(gallery) > 0) or bool(p.get("image_mime"))
    if effective_prices:
        p["min_price"] = str(min(effective_prices))
        p["max_price"] = str(max(effective_prices))
    else:
        p["min_price"] = p.get("sale_price") or "0"
        p["max_price"] = p.get("sale_price") or "0"

    # Settings da loja
    store_name = get_setting("store_name", "tonton")
    wa_number = get_setting("store_whatsapp", "")
    instagram = get_setting("catalog_instagram", "@tontonlojainfantil")
    tagline = get_setting("catalog_tagline", "moda que fica")
    click_target = get_setting("catalog_item_click_target", "whatsapp").strip().lower()
    if click_target not in {"whatsapp", "instagram"}:
        click_target = "whatsapp"
    show_prices = get_setting("catalog_show_prices", "1") == "1"

    # Public base URL para canonical/og
    public_base = (
        current_app.config.get("PUBLIC_BASE_URL")
        or request.url_root.rstrip("/")
    )
    seo = build_product_seo(p, store_name, public_base)

    # Peças relacionadas: mesma categoria, exclui a atual, máx 3.
    related = []
    if p.get("category"):
        related_rows = db.execute("""
            SELECT id, name, slug, image_mime, sale_price, promo_price, promo_until,
                   category,
                   COALESCE(stock_qty, 0) +
                   COALESCE((SELECT SUM(v.stock_qty) FROM product_variants v
                             WHERE v.product_id = products.id AND v.is_active = 1), 0) AS total_stock
            FROM products
            WHERE is_active=1 AND category=%s AND id<>%s
              AND slug IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 3
        """, (p["category"], pid)).fetchall()
        for rr in related_rows:
            rd = dict(rr)
            gver = db.execute(
                "SELECT image_version FROM product_images "
                "WHERE product_id=%s AND is_primary=1 LIMIT 1", (rd["id"],)
            ).fetchone()
            rd["primary_image_version"] = gver["image_version"] if gver else 1
            rd["has_image"] = (
                db.execute(
                    "SELECT 1 FROM product_images WHERE product_id=%s LIMIT 1",
                    (rd["id"],)
                ).fetchone() is not None
            ) or bool(rd.get("image_mime"))
            related.append(rd)

    return render_template(
        "public_product.html",
        product=p,
        gallery=gallery,
        related=related,
        seo=seo,
        store_name=store_name,
        wa_number=wa_number,
        tagline=tagline,
        instagram=instagram,
        click_target=click_target,
        show_prices=show_prices,
        color_map=CATALOG_COLOR_MAP,
    )


# ─── CATEGORIA: URL bonita (/catalogo/cat/<categoria>) ────


@public_bp.route("/catalogo/cat/<cat_slug>", endpoint="public_catalog_category")
def public_catalog_category(cat_slug):
    """Listagem por categoria com URL amigável.
    Resolve slug → nome real da categoria (ex: 'vestidos' → 'Vestidos')
    e reaproveita a rota `public_catalog` por redirecionamento interno."""
    db = get_db()
    cat_slug = (cat_slug or "").strip().lower()
    if not cat_slug or not re.match(r"^[a-z0-9][a-z0-9-]*$", cat_slug):
        abort(404)
    # Tenta achar a categoria correspondente
    rows = db.execute("""
        SELECT DISTINCT category FROM products
        WHERE is_active=1 AND category IS NOT NULL
    """).fetchall()
    target = None
    for r in rows:
        if slugify(r["category"]) == cat_slug:
            target = r["category"]
            break
    if not target:
        abort(404)
    # Redireciona para `/catalogo?cat=<nome real>` mantendo q/sort se vierem
    params = {"cat": target}
    if request.args.get("q"):    params["q"] = request.args.get("q")
    if request.args.get("sort"): params["sort"] = request.args.get("sort")
    return redirect(url_for("public_catalog", **params))


# ─── PÁGINA SOBRE A MARCA ───────────────────────────────────


@public_bp.route("/sobre", endpoint="public_about")
def public_about():
    """Página /sobre — história, processo e manifesto da marca.

    Conteúdo editável em /settings (3 blocos: origem, processo, manifesto).
    Estrutura editorial fixa, conteúdo dinâmico via store_settings.
    Rota anônima — pode ser linkada do Instagram, perfil, etc.
    """
    store_name = get_setting("store_name", "tonton")
    tagline = get_setting("catalog_tagline", "moda feminina atemporal")
    instagram = get_setting("catalog_instagram", "@tontonlojainfantil")
    wa_number = get_setting("store_whatsapp", "")
    footer_email = get_setting("catalog_footer_email", "").strip()
    footer_credit = get_setting("catalog_footer_credit", "").strip()
    footer_credo = get_setting(
        "catalog_footer_credo",
        "Coleções curtas, tecidos honestos.\nCada peça escolhida para durar mais que uma estação."
    ).strip()
    newsletter_enabled = get_setting("catalog_newsletter_enabled", "0") == "1"
    newsletter_intro = get_setting(
        "catalog_newsletter_intro",
        "primeira a saber das próximas peças"
    ).strip()
    og_image_set = bool(get_setting("catalog_og_image_b64", ""))

    # WhatsApp link com mensagem padrão pré-preenchida (mesma lógica do catálogo).
    wa_default_msg = get_setting(
        "catalog_wa_default",
        "Olá! Vim pelo catálogo da Tonton — gostaria de saber mais."
    ).strip()
    if wa_number and wa_default_msg:
        from urllib.parse import quote
        wa_link = f"https://wa.me/{wa_number}?text={quote(wa_default_msg)}"
    elif wa_number:
        wa_link = f"https://wa.me/{wa_number}"
    else:
        wa_link = ""

    # Conteúdo editorial — 3 blocos editáveis em /settings.
    # Defaults: rascunho que reflete a alma observada (curadoria,
    # fotografia íntima, paleta editorial). Edite à vontade.
    about_origin = get_setting(
        "about_origin",
        "Tonton nasceu da convicção de que vestir bem não precisa ser ruidoso. "
        "Cada coleção é pensada como uma carta — curta, honesta, escrita à "
        "mão. Trabalhamos com poucas peças por vez para que cada uma seja "
        "olhada de perto, escolhida com tempo."
    ).strip()
    about_process = get_setting(
        "about_process",
        "Selecionamos tecidos com toque vivo: algodões macios, viscoses que "
        "caem, modelagens que respeitam o corpo real. As peças passam por "
        "atelier antes de chegar até você. Sem coleção infinita, sem foto "
        "falsa de catálogo — você vê a peça vestida, na luz natural."
    ).strip()
    about_manifesto = get_setting(
        "about_manifesto",
        "Acreditamos em comprar menos e melhor. Em peças que ficam — não "
        "porque são caras, mas porque foram feitas com tempo. Em mostrar a "
        "loja como ela é: pequena, próxima, com nome, com voz."
    ).strip()

    return render_template(
        "public_about.html",
        store_name=store_name,
        tagline=tagline,
        instagram=instagram,
        wa_number=wa_number,
        wa_link=wa_link,
        footer_email=footer_email,
        footer_credit=footer_credit,
        footer_credo=footer_credo,
        newsletter_enabled=newsletter_enabled,
        newsletter_intro=newsletter_intro,
        og_image_set=og_image_set,
        about_origin=about_origin,
        about_process=about_process,
        about_manifesto=about_manifesto,
        # base.html / footer compartilhado precisam de:
        categories=[],
        hero_mode='minimal',
        hero_product=None,
        hero_images=[],
    )


# ─── SITEMAP XML ─────────────────────────────────────────


@public_bp.route("/sitemap.xml", endpoint="public_sitemap")
def public_sitemap():
    """Sitemap XML para indexação no Google.
    Lista catálogo + cada peça pública com slug + páginas de categoria."""
    db = get_db()
    public_base = (
        current_app.config.get("PUBLIC_BASE_URL")
        or request.url_root.rstrip("/")
    )
    urls = []
    # Home pública
    urls.append({"loc": f"{public_base}/catalogo", "priority": "1.0", "changefreq": "daily"})
    # Sobre a marca
    urls.append({"loc": f"{public_base}/sobre", "priority": "0.7", "changefreq": "monthly"})
    # Categorias
    cats = db.execute("""
        SELECT DISTINCT category FROM products
        WHERE is_active=1 AND category IS NOT NULL
    """).fetchall()
    for r in cats:
        urls.append({
            "loc": f"{public_base}/catalogo/cat/{slugify(r['category'])}",
            "priority": "0.8",
            "changefreq": "weekly",
        })
    # Produtos
    prods = db.execute("""
        SELECT slug, updated_at FROM products
        WHERE is_active=1 AND slug IS NOT NULL
          AND (
            COALESCE(stock_qty,0) +
            COALESCE((SELECT SUM(v.stock_qty) FROM product_variants v
                      WHERE v.product_id = products.id AND v.is_active = 1), 0)
          ) > 0
    """).fetchall()
    for r in prods:
        urls.append({
            "loc": f"{public_base}/catalogo/{r['slug']}",
            "lastmod": (r["updated_at"] or "")[:10],
            "priority": "0.9",
            "changefreq": "weekly",
        })
    # Render XML manualmente (mais simples que template + escapa nada perigoso)
    from xml.sax.saxutils import escape as xml_escape
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        parts.append("  <url>")
        parts.append(f"    <loc>{xml_escape(u['loc'])}</loc>")
        if u.get("lastmod"):
            parts.append(f"    <lastmod>{xml_escape(u['lastmod'])}</lastmod>")
        parts.append(f"    <changefreq>{u['changefreq']}</changefreq>")
        parts.append(f"    <priority>{u['priority']}</priority>")
        parts.append("  </url>")
    parts.append("</urlset>")
    from flask import Response
    return Response("\n".join(parts), mimetype="application/xml")


# ─── ROBOTS.TXT ──────────────────────────────────────────


@public_bp.route("/robots.txt", endpoint="public_robots")
def public_robots():
    public_base = (
        current_app.config.get("PUBLIC_BASE_URL")
        or request.url_root.rstrip("/")
    )
    body = (
        "User-agent: *\n"
        "Allow: /catalogo\n"
        "Disallow: /admin\n"
        "Disallow: /login\n"
        "Disallow: /dashboard\n"
        "Disallow: /sudo\n"
        "Disallow: /api/\n"
        f"Sitemap: {public_base}/sitemap.xml\n"
    )
    from flask import Response
    return Response(body, mimetype="text/plain")


# ─── REGISTRAR INTERESSE NO CATÁLOGO ────────────────────


@public_bp.route("/api/catalog/interest", methods=["POST"], endpoint="log_interest")
def log_interest():
    """Log when a customer clicks 'Tenho interesse' in public catalog.
    Called via fetch() from the catalog page."""
    # Rate limit: at most 20 interests per minute per IP. Protects against
    # someone scripting thousands of fake leads.
    if rate_limited(f"interest:{client_ip()}", max_hits=20, window_seconds=60):
        return {"ok": False, "error": "rate_limited"}, 429
    try:
        data = request.get_json(silent=True) or {}
        pid  = int(data.get("product_id") or 0)
        if not pid: return {"ok": False}, 400
        # Verify the product actually exists and is active before logging.
        db = get_db()
        ok = db.execute("SELECT 1 FROM products WHERE id=%s AND is_active=1", (pid,)).fetchone()
        if not ok:
            return {"ok": False, "error": "not_found"}, 404
        name  = (data.get("customer_name") or "").strip()[:100] or None
        phone = (data.get("customer_phone") or "").strip()[:30] or None
        ua    = request.headers.get("User-Agent","")[:200]
        ip    = client_ip()[:45]
        db.execute("""INSERT INTO catalog_interest(product_id,customer_phone,customer_name,ip,user_agent,created_at)
            VALUES(%s,%s,%s,%s,%s,%s)""", (pid, phone, name, ip, ua, utc_now_iso()))
        db.commit()
    except Exception:
        # Never echo internal error details back to a public endpoint.
        current_app.logger.exception("log_interest failed")
        return {"ok": False, "error": "server_error"}, 500
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────
# Newsletter — captura de e-mails do footer.
# Tabela criada lazy na primeira chamada (idempotente). Sem integração
# com Brevo/Mailchimp ainda — apenas armazena. Você pode exportar
# depois via SELECT email FROM newsletter_subscribers.
# ─────────────────────────────────────────────────────────────────────────
import re as _re_email
_EMAIL_RE = _re_email.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _ensure_newsletter_table(db):
    """Cria a tabela na primeira chamada. Idempotente."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS newsletter_subscribers (
            id BIGSERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            ip TEXT,
            user_agent TEXT,
            source TEXT DEFAULT 'catalog_footer',
            confirmed SMALLINT NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            unsubscribed_at TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_newsletter_email ON newsletter_subscribers(email)")
    db.commit()


@public_bp.route("/api/newsletter/subscribe", methods=["POST"], endpoint="newsletter_subscribe")
def newsletter_subscribe():
    """Captura e-mail do footer do catálogo.

    Validações:
      - Rate limit por IP (5/min) — evita spam.
      - Regex de e-mail mínima.
      - INSERT idempotente via ON CONFLICT (email é UNIQUE).

    Retorna sempre {ok: true} mesmo para e-mail já cadastrado, evitando
    enumerar quem está/não está na lista (privacidade).
    """
    if rate_limited(f"newsletter:{client_ip()}", max_hits=5, window_seconds=60):
        return {"ok": False, "error": "rate_limited"}, 429
    try:
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()[:120]
        if not email or not _EMAIL_RE.match(email):
            return {"ok": False, "error": "invalid_email"}, 400

        db = get_db()
        _ensure_newsletter_table(db)
        ua = request.headers.get("User-Agent", "")[:200]
        ip = client_ip()[:45]
        # ON CONFLICT DO NOTHING: silenciosamente ignora duplicatas.
        db.execute(
            "INSERT INTO newsletter_subscribers (email, ip, user_agent, source, created_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (email) DO NOTHING",
            (email, ip, ua, "catalog_footer", utc_now_iso()),
        )
        db.commit()
    except Exception:
        current_app.logger.exception("newsletter_subscribe failed")
        return {"ok": False, "error": "server_error"}, 500
    return {"ok": True}


# ─── PEÇAS COBIÇADAS (dashboard interno) ────────────────


@public_bp.route("/public/product-image/<int:pid>", endpoint="public_product_image")
def public_product_image(pid):
    db = get_db()
    # Verifica produto ativo
    prod = db.execute(
        "SELECT 1 FROM products WHERE id=%s AND is_active=1", (pid,)
    ).fetchone()
    if not prod:
        abort(404)
    # v10.3: prefere primária da galeria (com cache controlado por image_version no client)
    img = db.execute(
        "SELECT image_blob, image_mime FROM product_images "
        "WHERE product_id=%s AND is_primary=1 LIMIT 1", (pid,)
    ).fetchone()
    if not img:
        img = db.execute(
            "SELECT image_blob, image_mime FROM product_images "
            "WHERE product_id=%s ORDER BY sort_order ASC, id ASC LIMIT 1", (pid,)
        ).fetchone()
    if not img:
        # Fallback: blob legado em products
        img = db.execute(
            "SELECT image_blob, image_mime FROM products WHERE id=%s", (pid,)
        ).fetchone()
    if not img or not img["image_blob"]:
        abort(404)
    from flask import Response
    return Response(bytes(img["image_blob"]), mimetype=(img["image_mime"] or "image/jpeg"),
                    headers={"Cache-Control":"public, max-age=3600"})


@public_bp.route("/public/hero-image/<int:hid>", endpoint="public_hero_image")
def public_hero_image(hid):
    """Serve imagem do hero do catálogo público (cache 24h).
    Movida da app.py em Onda 2/Etapa I por ser rota anônima — mesma
    natureza de public_product_image."""
    db = get_db()
    row = db.execute(
        "SELECT image_blob, image_mime FROM catalog_hero_images WHERE id=%s",
        (hid,)
    ).fetchone()
    if not row or not row["image_blob"]:
        abort(404)
    resp = Response(bytes(row["image_blob"]), mimetype=row["image_mime"])
    # Cache forte (com cache busting via image_version no template)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@public_bp.route("/public/og-image", endpoint="public_og_image")
def public_og_image():
    """Serve a imagem de Open Graph (preview do link em redes sociais).
    Armazenada em store_settings como base64. Se não houver, retorna 404
    e o template cai no fallback (foto da peça-capa ou imagem genérica)."""
    import base64
    encoded = get_setting("catalog_og_image_b64", "")
    mime = get_setting("catalog_og_image_mime", "image/jpeg")
    if not encoded:
        abort(404)
    try:
        blob = base64.b64decode(encoded)
    except Exception:
        abort(404)
    resp = Response(blob, mimetype=mime)
    # Cache curto (1h) — usuário pode trocar a imagem e querer ver rápido.
    # Para invalidar de vez: trocar a imagem em /settings.
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp



# ─────────────────────────────────────────────────────────────────────────
# init_public_blueprint — registra aliases curtos no app.view_functions.
# Templates legados usam url_for('public_catalog') sem o prefixo 'public.';
# aqui montamos os aliases para que esses chamados continuem funcionando.
# ─────────────────────────────────────────────────────────────────────────
def init_public_blueprint(app):
    """
    Hook chamado por app.py após `app.register_blueprint(public_bp)`.

    Registra aliases sem prefixo para preservar compatibilidade com
    templates que usam url_for('public_catalog'), etc.

    Idempotente.
    """
    if getattr(app, "_public_bp_initialized", False):
        return

    ALIAS_MAP = {
        "public_catalog":          "public.public_catalog",
        "public_about":            "public.public_about",
        "public_product":          "public.public_product",
        "public_catalog_category": "public.public_catalog_category",
        "public_sitemap":          "public.public_sitemap",
        "public_robots":           "public.public_robots",
        "log_interest":            "public.log_interest",
        "newsletter_subscribe":    "public.newsletter_subscribe",
        "public_product_image":    "public.public_product_image",
        "public_hero_image":       "public.public_hero_image",
        "public_og_image":         "public.public_og_image",
    }

    _ROUTE_DEFS = [
        ("public_catalog",          "/catalogo",                       ("GET",)),
        ("public_about",            "/sobre",                          ("GET",)),
        ("public_product",          "/catalogo/<slug>",                ("GET",)),
        ("public_catalog_category", "/catalogo/cat/<cat_slug>",        ("GET",)),
        ("public_sitemap",          "/sitemap.xml",                    ("GET",)),
        ("public_robots",           "/robots.txt",                     ("GET",)),
        ("log_interest",            "/api/catalog/interest",           ("POST",)),
        ("newsletter_subscribe",    "/api/newsletter/subscribe",       ("POST",)),
        ("public_product_image",    "/public/product-image/<int:pid>", ("GET",)),
        ("public_hero_image",       "/public/hero-image/<int:hid>",    ("GET",)),
        ("public_og_image",         "/public/og-image",                ("GET",)),
    ]

    for short, full in ALIAS_MAP.items():
        view = app.view_functions.get(full)
        if view is None:
            app.logger.warning(
                "Public blueprint: endpoint %r ausente — alias %r não registrado.",
                full, short,
            )
            continue
        app.view_functions[short] = view

    existing_endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    for endpoint, rule, methods in _ROUTE_DEFS:
        if endpoint in existing_endpoints:
            continue
        view = app.view_functions.get(endpoint)
        if view is None:
            continue
        app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=list(methods))

    app._public_bp_initialized = True
