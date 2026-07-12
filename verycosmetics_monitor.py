"""
Very Cosmetics Monitor
Monitors the entire Shopify storefront at https://www.verycosmetics.co.uk/

Uses Shopify's public /products.json API for the full catalogue, plus
individual product page scraping for exact stock ("Quantity available: N")
and volume/tiered pricing where available.

Detects (Discord alerts fire ONLY for these):
  - New product listings (in stock only)
  - Price drops (decreased >1% and >£0.02)
  - Restocks (stock increased meaningfully) / Back in stock

Does NOT alert on: price increases, stock decreases, going OOS.

Deps: pip install requests beautifulsoup4
"""

import json
import os
import re
import time
import random
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL       = "https://www.verycosmetics.co.uk"
SNAPSHOT_FILE  = "snapshot_verycosmetics.json"
BASELINE_FLAG  = "baseline_done_verycosmetics.txt"
PAGE_SIZE      = 250
REQUEST_DELAY  = 1.0
RUN_ONCE       = os.getenv("RUN_ONCE", "false").lower() == "true"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))  # 30 min

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Discord embed colours
COLOUR_NEW     = 0xE91E8C   # pink — new listing
COLOUR_RESTOCK = 0x3498DB   # blue — restock
COLOUR_BACK    = 0x9B59B6   # purple — back in stock
# Price drop colours are tiered by severity — see notify_price_change()

# ---------------------------------------------------------------------------
# SHOPIFY PRODUCTS.JSON
# ---------------------------------------------------------------------------

def fetch_all_products():
    """Fetch every product on the storefront via the public products.json API."""
    all_products = []
    page = 1
    while True:
        url = f"{BASE_URL}/products.json"
        params = {"limit": PAGE_SIZE, "page": page}
        try:
            r = SESSION.get(url, params=params, timeout=20)
            r.raise_for_status()
            batch = r.json().get("products", [])
        except Exception as e:
            print(f"  [!] Fetch error (page {page}): {e}")
            break
        if not batch:
            break
        all_products.extend(batch)
        print(f"  Page {page}: {len(batch)} products (total: {len(all_products)})")
        if len(batch) < PAGE_SIZE:
            break
        page += 1
        time.sleep(REQUEST_DELAY + random.uniform(0, 0.5))
    return all_products


def parse_product(item):
    """Parse a Shopify product JSON object into our format."""
    variants = item.get("variants", [])
    available_variants = [v for v in variants if v.get("available")]
    variant = available_variants[0] if available_variants else (variants[0] if variants else {})

    price         = variant.get("price", "")
    compare_price = variant.get("compare_at_price", "")
    sku           = variant.get("sku", "")
    barcode       = variant.get("barcode", "")

    in_stock = any(v.get("available") for v in variants) if variants else False

    images = item.get("images", [])
    image  = images[0].get("src", "") if images else ""

    handle = item.get("handle", "")

    return {
        "id":         str(item.get("id", "")),
        "variant_id": str(variant.get("id", "")) if variant else "",
        "handle":     handle,
        "title":      item.get("title", ""),
        "url":        f"{BASE_URL}/products/{handle}",
        "image":      image,
        "sku":        sku or "",
        "barcode":    barcode or "",
        "price":      price,
        "compare_price": compare_price if compare_price and compare_price != price else "",
        "in_stock":   in_stock,
        "stock":      None,            # filled via page scrape
        "vendor":     item.get("vendor", ""),
        "product_type": item.get("product_type", ""),
    }

# ---------------------------------------------------------------------------
# PRODUCT PAGE SCRAPE — exact stock + volume pricing (best effort)
# ---------------------------------------------------------------------------

def scrape_product_page(handle, retries=3):
    """
    Fetch the product page HTML and extract:
      - exact stock count ("Quantity available: N")
      - barcode (fallback if not in products.json)
      - volume/tiered pricing, if the page exposes it in a recognisable form

    Returns a dict: {stock, barcode}
    """
    url = f"{BASE_URL}/products/{handle}"
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=15)
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                print(f"  [!] Rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            html = r.text
            break
        except Exception as e:
            print(f"  [!] Page fetch error ({handle}): {e} — attempt {attempt+1}/{retries}")
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
            else:
                return {"stock": None, "barcode": ""}
    else:
        return {"stock": None, "barcode": ""}

    result = {"stock": None, "barcode": ""}

    # Exact stock — "var QTY = N" (legacy theme variable, still present on
    # this storefront) and "Quantity available: N" (rendered text fallback)
    m = re.search(r"var\s+QTY\s*=\s*(\d+)", html)
    if m:
        result["stock"] = int(m.group(1))
    else:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        m2 = re.search(r"Quantity available:\s*(\d+)", text, re.IGNORECASE)
        if m2:
            result["stock"] = int(m2.group(1))

    # Barcode fallback
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    bc_m = re.search(r"Barcode:\s*([0-9A-Za-z\-]{6,20})", text)
    if bc_m:
        result["barcode"] = bc_m.group(1)

    return result

# ---------------------------------------------------------------------------
# PRICING HELPERS
# ---------------------------------------------------------------------------

def vat_price(price_str):
    try:
        return f"{float(price_str) * 1.2:.2f}"
    except (ValueError, TypeError):
        return price_str


def safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def selleramp_url(barcode, cost_price_str):
    if not barcode:
        return None
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={barcode}&sas_cost_price={vat_price(cost_price_str)}"
    )


# ---------------------------------------------------------------------------
# DISCORD EMBEDS
# ---------------------------------------------------------------------------

def _base_fields(product):
    sku      = product.get("sku", "")
    barcode  = product.get("barcode", "")
    stock    = product.get("stock")
    in_stock = product.get("in_stock", True)
    price    = product.get("price", "")
    sas_url  = selleramp_url(barcode or sku, price)

    if stock is not None:
        stock_val = f"**{stock}** units"
    elif in_stock:
        stock_val = "✅ In stock"
    else:
        stock_val = "❌ Out of stock"

    fields = [
        {"name": "🔢 Barcode", "value": f"`{barcode}`" if barcode else "-", "inline": True},
        {"name": "🔖 SKU",     "value": f"`{sku}`" if sku else "-",         "inline": True},
        {"name": "📊 Stock",   "value": stock_val,                          "inline": True},
    ]

    if sas_url:
        fields.append({"name": "🔍 SellerAmp SAS", "value": f"[Open in SellerAmp]({sas_url})", "inline": False})
    return fields


def _send_embed(embed):
    payload = {"embeds": [embed]}
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            time.sleep(wait)
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
    except Exception as e:
        print(f"  [!] Discord error: {e}")


def _thumbnail(product):
    image = product.get("image", "")
    return {"url": image} if image else None


def _price_display(product):
    price   = product.get("price", "")
    compare = product.get("compare_price", "")
    if compare:
        return f"£{compare} -> **£{price}**"
    return f"**£{price}**" if price else "-"


def notify_new(product):
    fields = [
        {"name": "💰 Price (ex. VAT)",  "value": _price_display(product),                "inline": True},
        {"name": "💷 Price (inc. VAT)", "value": f"£{vat_price(product.get('price', ''))}" if product.get("price") else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🆕  NEW LISTING — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_NEW,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Very Cosmetics Monitor • verycosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: NEW — {product.get('title', '')[:60]}")


def notify_price_change(product, old_price, new_price, pct_change):
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)
    diff  = f"£{abs(new_f - old_f):.2f}" if old_f and new_f else "?"
    pct_display = f"{pct_change * 100:.1f}%"

    if pct_change >= 0.20:
        colour = 0x00C853
        tier   = "🔥"
    elif pct_change >= 0.10:
        colour = 0x2ECC71
        tier   = "💰"
    else:
        colour = 0x82E0AA
        tier   = "💵"

    fields = [
        {"name": "💰 Old Price", "value": f"£{old_price}",     "inline": True},
        {"name": "💰 New Price", "value": f"**£{new_price}**", "inline": True},
        {"name": "📉 Drop",      "value": f"↓ {diff} (**{pct_display}**)", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"{tier}  PRICE DROP -{pct_display} — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Very Cosmetics Monitor • verycosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: PRICE DROP -{pct_display} — {product.get('title', '')[:50]}")


def notify_stock_change(product, old_stock, new_stock):
    """Restock only — stock decreases are no longer tracked."""
    diff = (new_stock - old_stock) if (new_stock is not None and old_stock is not None) else "?"
    fields = [
        {"name": "📊 Old Stock", "value": f"{old_stock} units",     "inline": True},
        {"name": "📊 New Stock", "value": f"**{new_stock} units**", "inline": True},
        {"name": "📈 Change",    "value": f"↑ +{diff} units" if isinstance(diff, int) else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🟢  RESTOCK — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_RESTOCK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Very Cosmetics Monitor • verycosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: RESTOCK — {product.get('title', '')[:50]}")


def notify_back_in_stock(product):
    fields = [
        {"name": "💰 Price (ex. VAT)",  "value": _price_display(product),                "inline": True},
        {"name": "💷 Price (inc. VAT)", "value": f"£{vat_price(product.get('price', ''))}" if product.get("price") else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🟢  BACK IN STOCK — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_BACK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Very Cosmetics Monitor • verycosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: BACK IN STOCK — {product.get('title', '')[:60]}")

# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError as exc:
            backup = f"{SNAPSHOT_FILE}.corrupted.{int(time.time())}"
            print(f"  [!] Snapshot corrupted ({exc}) — backing up to {backup} and starting fresh")
            try:
                os.rename(SNAPSHOT_FILE, backup)
            except OSError:
                pass
            return {}
    return {}


def save_snapshot(data):
    tmp = f"{SNAPSHOT_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SNAPSHOT_FILE)


def snapshot_entry(product):
    return {
        "title":         product.get("title", ""),
        "url":           product.get("url", ""),
        "image":         product.get("image", ""),
        "sku":           product.get("sku", ""),
        "barcode":       product.get("barcode", ""),
        "price":         product.get("price", ""),
        "compare_price": product.get("compare_price", ""),
        "in_stock":      product.get("in_stock", True),
        "stock":         product.get("stock"),
        "first_seen":    product.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated":  datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# CHANGE DETECTION
# ---------------------------------------------------------------------------

def check_changes(product, old):
    """
    Only fires alerts for:
      - Back in stock (was OOS, now has stock) — takes priority
      - Restock (stock increased meaningfully while already in stock)
      - Price drop (decreased by more than 1% AND more than £0.02)
    No alerts for: price increases, stock decreases, going OOS.
    """
    old_price    = old.get("price", "")
    new_price    = product.get("price", "")
    old_stock    = old.get("stock")
    new_stock    = product.get("stock")
    was_in_stock = old.get("in_stock", True)
    now_in_stock = product.get("in_stock", True)

    for key in ("image", "sku", "barcode"):
        if not product.get(key):
            product[key] = old.get(key, "")
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)

    if not was_in_stock and now_in_stock:
        notify_back_in_stock(product)
        time.sleep(1)
        return

    if old_f and new_f and old_f > 0:
        pct_change = (old_f - new_f) / old_f
        abs_change = old_f - new_f
        if pct_change > 0.01 and abs_change > 0.02:
            notify_price_change(product, old_price, new_price, pct_change)
            time.sleep(1)

    if old_stock is not None and new_stock is not None and was_in_stock and now_in_stock:
        threshold = max(5, int(old_stock * 0.2))
        if new_stock > old_stock + threshold:
            notify_stock_change(product, old_stock, new_stock)
            time.sleep(1)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run_check():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] Checking Very Cosmetics...")

    snapshot      = load_snapshot()
    known_ids     = set(snapshot.keys())
    baseline_done = os.path.exists(BASELINE_FLAG)
    is_first_run  = not baseline_done

    print("  Fetching full catalogue via products.json...")
    all_products = fetch_all_products()
    if not all_products:
        print("  [!] No products fetched")
        return

    parsed = [parse_product(p) for p in all_products]
    current_ids = {p["id"] for p in parsed}
    new_ids     = current_ids - known_ids

    if is_first_run:
        print(f"  First run — building baseline from {len(parsed)} products (no alerts)...")
    else:
        print(f"  {len(parsed)} products fetched, {len(new_ids)} new")

    for i, product in enumerate(parsed, 1):
        pid = product["id"]

        # Enrich with page scrape (exact stock, barcode fallback, volume tiers)
        # for: first-run baseline (in-stock items only), new listings, and
        # existing products so we can detect restocks/back-in-stock accurately.
        should_scrape = (
            (is_first_run and product.get("in_stock")) or
            (pid in new_ids and product.get("in_stock")) or
            (not is_first_run and pid not in new_ids)
        )
        if should_scrape and product.get("handle"):
            time.sleep(REQUEST_DELAY + random.uniform(0, 0.5))
            page_data = scrape_product_page(product["handle"])
            product["stock"] = page_data["stock"]
            if page_data["barcode"]:
                product["barcode"] = page_data["barcode"]
            if product["stock"] is not None:
                product["in_stock"] = product["stock"] > 0

        if is_first_run:
            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
        elif pid in new_ids:
            if product.get("in_stock", True):
                print(f"  -> NEW: {product['title'][:60]}")
                notify_new(product)
                time.sleep(1.5)
            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
        else:
            old = snapshot[pid]
            check_changes(product, old)
            entry = snapshot_entry(product)
            entry["first_seen"] = old.get("first_seen", entry["first_seen"])
            snapshot[pid] = entry

        if i % 50 == 0:
            save_snapshot(snapshot)
            print(f"  Auto-saved at {i}/{len(parsed)}")

    save_snapshot(snapshot)

    if is_first_run:
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline complete — {len(snapshot)} products recorded. No alerts sent.")
    else:
        print(f"  Snapshot saved ({len(snapshot)} products tracked)")


def main():
    print("=" * 55)
    print("  Very Cosmetics Monitor (whole site)")
    print(f"  Watching: {BASE_URL}")
    print("  Tracking: new listings, price drops, restocks")
    print("=" * 55)

    if RUN_ONCE:
        run_check()
    else:
        while True:
            try:
                run_check()
            except Exception as e:
                print(f"  [!] Unexpected error: {e}")
            print(f"  Sleeping {CHECK_INTERVAL}s...")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
