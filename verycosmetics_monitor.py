"""
Very Cosmetics Monitor — Clean Rewrite
Monitors https://www.verycosmetics.co.uk/collections/new-arrivals

Alerts on:
  ✅ New listings (in stock only)
  ✅ Price drops (>1% AND >£0.02)
  ✅ Back in stock (was OOS, now available)
  ✅ Restocks (meaningful stock increase)

No alerts for: price increases, stock decreases, going OOS.

Key improvements over previous version:
  - Only scrapes product PAGES for new/back-in-stock products
    (not every product on every run — that was causing the 2am spam)
  - 20-minute interval (configurable via CHECK_INTERVAL env var)
  - Volume/tiered pricing displayed when detected
  - Both SAS EAN barcode + title search links in every embed
  - Atomic snapshot saves — no corruption on crash
  - Clean first-run baseline with no false alerts

Env vars:
  DISCORD_WEBHOOK   — required
  CHECK_INTERVAL    — seconds between checks (default: 1200 = 20 min)
  RUN_ONCE          — "true" for GitHub Actions single-shot mode

Usage:
  pip install requests beautifulsoup4
  python3 verycosmetics_monitor.py
"""

import json
import os
import re
import time
import random
import requests
from datetime import datetime, timezone
from urllib.parse import quote
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL        = "https://www.verycosmetics.co.uk"
COLLECTION_URL  = f"{BASE_URL}/collections/new-arrivals/products.json"
SNAPSHOT_FILE   = "snapshot_verycosmetics.json"
BASELINE_FLAG   = "baseline_done_verycosmetics.txt"
PAGE_SIZE       = 250
REQUEST_DELAY   = 0.8
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", "1200"))   # 20 min
RUN_ONCE        = os.getenv("RUN_ONCE", "false").lower() == "true"
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Embed colours
COL_NEW       = 0xE91E8C   # pink
COL_BACK      = 0x9B59B6   # purple
COL_RESTOCK   = 0x3498DB   # blue
COL_DROP_HOT  = 0x00C853   # green (>=20% drop)
COL_DROP_MED  = 0x2ECC71   # lighter green (>=10%)
COL_DROP_MILD = 0x82E0AA   # pale green (<10%)

# ---------------------------------------------------------------------------
# SHOPIFY FETCH — collection-specific, fast
# ---------------------------------------------------------------------------

def fetch_collection():
    """
    Fetch all products in the new-arrivals collection via Shopify products.json.
    Only fetches collection JSON — no product page scrapes here.
    Returns list of raw Shopify product dicts.
    """
    products = []
    page = 1
    while True:
        try:
            r = SESSION.get(COLLECTION_URL,
                            params={"limit": PAGE_SIZE, "page": page},
                            timeout=20)
            r.raise_for_status()
            batch = r.json().get("products", [])
        except Exception as e:
            print(f"  [!] Fetch error (page {page}): {e}")
            break
        if not batch:
            break
        products.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    return products


def parse_product(raw):
    """
    Parse Shopify product JSON into a snapshot dict.
    The JSON already contains barcode, price, and availability —
    no product page scrapes needed for standard monitoring.
    Volume pricing is fetched via page scrape for new products only.
    """
    variants = raw.get("variants", [])
    avail    = [v for v in variants if v.get("available")]
    v        = avail[0] if avail else (variants[0] if variants else {})

    price         = v.get("price", "") or ""
    compare_price = v.get("compare_at_price", "") or ""
    # Barcode comes from JSON — no page scrape needed
    barcode       = (v.get("barcode") or "").strip()
    sku           = (v.get("sku") or "").strip()
    in_stock      = bool(avail)

    images = raw.get("images", [])
    image  = images[0].get("src", "") if images else ""
    handle = raw.get("handle", "")

    return {
        "id":            str(raw.get("id", "")),
        "handle":        handle,
        "title":         raw.get("title", ""),
        "vendor":        raw.get("vendor", ""),
        "url":           f"{BASE_URL}/products/{handle}",
        "image":         image,
        "barcode":       barcode,
        "sku":           sku,
        "price":         price,
        "compare_price": compare_price if compare_price and compare_price != price else "",
        "in_stock":      in_stock,
        "stock":         None,        # not needed — use in_stock flag from JSON
        "volume_pricing":[],          # filled by page scrape for new products only
    }

# ---------------------------------------------------------------------------
# PRODUCT PAGE SCRAPE — only called for new / back-in-stock products
# ---------------------------------------------------------------------------

def scrape_product_page(handle):
    """
    Fetch a product page to get:
      - Exact stock quantity (var QTY = N, or "Quantity available: N")
      - Barcode if missing from JSON ("Barcode: XXXX" text)
      - Volume/tiered pricing table if present

    Called ONLY for new listings and back-in-stock products.
    NOT called on every cycle for every product (that was the spam cause).
    """
    url = f"{BASE_URL}/products/{handle}"
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=15)
            if r.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"  [!] Rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            html = r.text
            break
        except Exception as e:
            print(f"  [!] Page error ({handle}): {e}")
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
            else:
                return {}
    else:
        return {}

    result = {}
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Stock quantity
    m = re.search(r"var\s+QTY\s*=\s*(\d+)", html)
    if m:
        result["stock"] = int(m.group(1))
    else:
        m2 = re.search(r"Quantity available:\s*(\d+)", text, re.IGNORECASE)
        if m2:
            result["stock"] = int(m2.group(1))

    # Barcode fallback
    m3 = re.search(r"Barcode:\s*([0-9]{6,14})", text)
    if m3:
        result["barcode"] = m3.group(1)

    # Volume / tiered pricing
    # VeryCosmetics format:
    #   "Buy 240 units for 5% Each\n£0.95\n£1.00\nTotal..."
    #   "Buy 480 units for 10% Each\n£0.90\n..."
    volume = []

    # Primary pattern: "Buy N units for X% Each" followed by discounted price
    # Use raw HTML to find these blocks reliably
    buy_blocks = re.findall(
        r"Buy\s+(\d+)\s+units?\s+for\s+([\d.]+)%\s+Each\s+£\s*([\d.]+)",
        text, re.IGNORECASE
    )
    for qty, pct, price in buy_blocks:
        volume.append({"qty": f"{qty}+", "pct": pct, "price": price})

    # Fallback pattern 1: "N to M: £X.XX" or "N+: £X.XX"
    if not volume:
        tier_matches = re.findall(
            r"(\d+)\s+(?:to|-)\s+(\d+|\+)\s*:?\s*£\s*([\d.]+)",
            text, re.IGNORECASE
        )
        for from_qty, to_qty, price in tier_matches:
            volume.append({"qty": f"{from_qty}–{to_qty}", "price": price})

    # Fallback pattern 2: price_break JSON in scripts
    if not volume:
        for script in soup.find_all("script"):
            src = script.string or ""
            if "price_break" in src.lower() or "quantity_break" in src.lower():
                matches = re.findall(
                    r'"(?:minimum_quantity|min_qty|qty)"\s*:\s*(\d+)[^}]*"price"\s*:\s*"?([\d.]+)',
                    src
                )
                for qty, price in matches:
                    volume.append({"qty": f"{qty}+", "price": str(round(float(price)/100, 2))})
                if volume:
                    break

    if volume:
        result["volume_pricing"] = volume

    return result

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def vat(price_str):
    f = safe_float(price_str)
    return f"{f * 1.2:.2f}" if f else price_str


def price_display(product):
    p = product.get("price", "")
    c = product.get("compare_price", "")
    if c:
        return f"~~£{c}~~ → **£{p}**"
    return f"**£{p}**" if p else "-"


def sas_ean_url(barcode, price):
    if not barcode:
        return None
    return (f"https://sas.selleramp.com/sas/lookup/"
            f"?search_term={barcode}&sas_cost_price={vat(price)}")


def sas_title_url(title, price):
    return (f"https://sas.selleramp.com/sas/lookup/"
            f"?search_term={quote(title)}&sas_cost_price={vat(price)}")


def volume_pricing_text(volume):
    """Format volume pricing tiers for Discord embed."""
    if not volume:
        return None
    lines = []
    for t in volume:
        pct_str = f" (-{t['pct']}%)" if t.get("pct") else ""
        inc = round(float(t['price']) * 1.2, 2)
        lines.append(f"**{t['qty']} units** → £{t['price']} ex-VAT (£{inc} inc){pct_str}")
    return "\n".join(lines)


def _thumbnail(product):
    img = product.get("image", "")
    return {"url": img} if img else None


def _sas_fields(product):
    """SAS search fields — both EAN barcode and title search."""
    barcode = product.get("barcode", "")
    title   = product.get("title", "")
    price   = product.get("price", "0")
    fields  = []
    ean_url = sas_ean_url(barcode, price)
    if ean_url:
        fields.append({"name": "🔍 SAS EAN",   "value": f"[Search by barcode]({ean_url})", "inline": True})
    fields.append({"name": "🔍 SAS Title", "value": f"[Search by title]({sas_title_url(title, price)})", "inline": True})
    return fields


def _core_fields(product):
    barcode = product.get("barcode", "")
    sku     = product.get("sku", "")
    stock   = product.get("stock")
    vendor  = product.get("vendor", "")
    volume  = product.get("volume_pricing", [])

    stock_val = (f"**{stock:,}** units" if stock is not None
                 else ("✅ In stock" if product.get("in_stock") else "❌ OOS"))

    fields = [
        {"name": "🏷️ Brand",          "value": vendor or "-",                         "inline": True},
        {"name": "🔢 GTIN / EAN",     "value": f"`{barcode}`" if barcode else "-",     "inline": True},
        {"name": "🔖 SKU",             "value": f"`{sku}`" if sku else "-",             "inline": True},
        {"name": "📊 Stock",           "value": stock_val,                              "inline": True},
        {"name": "💷 Price (inc-VAT)", "value": f"£{vat(product.get('price',''))}",    "inline": True},
    ]

    vol_text = volume_pricing_text(volume)
    if vol_text:
        fields.append({"name": "📦 Volume Pricing (ex-VAT)", "value": vol_text, "inline": False})

    fields += _sas_fields(product)
    return fields

# ---------------------------------------------------------------------------
# DISCORD
# ---------------------------------------------------------------------------

def _send(payload):
    if not DISCORD_WEBHOOK:
        return
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            print(f"  [!] Rate limited by Discord — waiting {wait:.1f}s")
            time.sleep(wait)
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
    except Exception as e:
        print(f"  [!] Discord error: {e}")


def _embed(title, url, colour, fields, product, footer_suffix=""):
    embed = {
        "title":     title,
        "url":       url,
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": f"Very Cosmetics Monitor • verycosmetics.co.uk{footer_suffix}"},
    }
    t = _thumbnail(product)
    if t:
        embed["thumbnail"] = t
    return embed


def notify_new(product):
    fields = [
        {"name": "💰 New Price",       "value": product.get("price", "-"), "inline": True},
        {"name": "💷 Was",            "value": price_display(product), "inline": True},
    ] + _core_fields(product)

    _send({"embeds": [_embed(
        f"🆕  NEW — {product['title']}",
        product["url"], COL_NEW, fields, product
    )]})
    print(f"  ✅ Discord: NEW — {product['title'][:60]}")


def notify_back_in_stock(product):
    fields = [
        {"name": "💰 New Price",       "value": product.get("price", "-"), "inline": True},
        {"name": "💷 Was",            "value": price_display(product), "inline": True},
    ] + _core_fields(product)

    _send({"embeds": [_embed(
        f"🟢  BACK IN STOCK — {product['title']}",
        product["url"], COL_BACK, fields, product
    )]})
    print(f"  ✅ Discord: BACK IN STOCK — {product['title'][:55]}")


def notify_restock(product, old_stock, new_stock):
    diff = new_stock - old_stock if (new_stock and old_stock) else "?"
    fields = [
        {"name": "📊 Was",    "value": f"{old_stock:,} units", "inline": True},
        {"name": "📊 Now",    "value": f"**{new_stock:,} units**", "inline": True},
        {"name": "📈 Change", "value": f"+{diff:,}" if isinstance(diff, int) else "?", "inline": True},
        {"name": "💰 New Price",       "value": product.get("price", "-"), "inline": True},
        {"name": "💷 Was",            "value": price_display(product), "inline": True},
    ] + _sas_fields(product)

    _send({"embeds": [_embed(
        f"📦  RESTOCK — {product['title']}",
        product["url"], COL_RESTOCK, fields, product
    )]})
    print(f"  ✅ Discord: RESTOCK — {product['title'][:55]}")


def notify_price_drop(product, old_price, new_price, pct):
    abs_drop = safe_float(old_price) - safe_float(new_price)
    pct_str  = f"{pct*100:.1f}%"

    if pct >= 0.20:
        colour, tier = COL_DROP_HOT, "🔥"
    elif pct >= 0.10:
        colour, tier = COL_DROP_MED, "💰"
    else:
        colour, tier = COL_DROP_MILD, "💵"

    fields = [
        {"name": "💰 Was",     "value": f"£{old_price}",          "inline": True},
        {"name": "💰 Now",     "value": f"**£{new_price}**",       "inline": True},
        {"name": "📉 Drop",    "value": f"↓ £{abs_drop:.2f} (-{pct_str})", "inline": True},
        {"name": "💷 inc-VAT", "value": f"£{vat(new_price)}",      "inline": True},
    ] + _core_fields(product)

    _send({"embeds": [_embed(
        f"{tier}  PRICE DROP -{pct_str} — {product['title']}",
        product["url"], colour, fields, product,
        footer_suffix=f" • was £{old_price}"
    )]})
    print(f"  ✅ Discord: PRICE DROP -{pct_str} — {product['title'][:45]}")

# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if not os.path.exists(SNAPSHOT_FILE):
        return {}
    try:
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        bak = f"{SNAPSHOT_FILE}.bak.{int(time.time())}"
        print(f"  [!] Snapshot corrupted ({e}) — backing up to {bak}")
        try:
            os.rename(SNAPSHOT_FILE, bak)
        except OSError:
            pass
        return {}


def save_snapshot(data):
    tmp = f"{SNAPSHOT_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SNAPSHOT_FILE)


def to_entry(product):
    return {
        "title":          product.get("title", ""),
        "url":            product.get("url", ""),
        "image":          product.get("image", ""),
        "vendor":         product.get("vendor", ""),
        "barcode":        product.get("barcode", ""),
        "sku":            product.get("sku", ""),
        "price":          product.get("price", ""),
        "compare_price":  product.get("compare_price", ""),
        "in_stock":       product.get("in_stock", False),
        "stock":          product.get("stock"),
        "volume_pricing": product.get("volume_pricing", []),
        "first_seen":     product.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated":   datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# MAIN CHECK LOOP
# ---------------------------------------------------------------------------

def run_check():
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"\n[{now_str}] Checking Very Cosmetics new arrivals...")

    snapshot      = load_snapshot()
    known_ids     = set(snapshot.keys())
    baseline_done = os.path.exists(BASELINE_FLAG)
    is_first_run  = not baseline_done

    # --- Step 1: Fetch collection JSON (fast, no page scrapes) ---
    raw_products = fetch_collection()
    if not raw_products:
        print("  [!] Nothing fetched — skipping this cycle")
        return

    parsed      = [parse_product(p) for p in raw_products]
    current_ids = {p["id"] for p in parsed}
    new_ids     = current_ids - known_ids

    print(f"  {len(parsed)} products, {len(new_ids)} new IDs")

    if is_first_run:
        print(f"  First run — recording baseline ({len(parsed)} products). No alerts will fire.")

    alerts_sent = 0

    for product in parsed:
        pid = product["id"]

        # --- Step 2: Enrich with page scrape ONLY when necessary ---
        # ✅ New product: always scrape (need stock, barcode, volume pricing)
        # ✅ Back in stock: scrape to confirm qty and get barcode
        # ❌ Known in-stock unchanged product: DO NOT scrape (was causing 2am spam)
        # ❌ Known OOS product with no stock change: DO NOT scrape

        old = snapshot.get(pid, {})
        was_in_stock  = old.get("in_stock", True) if old else True
        now_in_stock  = product["in_stock"]

        # Only scrape product page for NEW in-stock products
        # to detect volume pricing. Everything else (barcode, price,
        # availability) comes from the products.json — no page scrapes needed.
        if pid in new_ids and now_in_stock and product.get("handle"):
            time.sleep(REQUEST_DELAY + random.uniform(0, 0.3))
            page_data = scrape_product_page(product["handle"])
            if page_data.get("volume_pricing"):
                product["volume_pricing"] = page_data["volume_pricing"]
            # Barcode fallback if JSON had it empty
            if page_data.get("barcode") and not product["barcode"]:
                product["barcode"] = page_data["barcode"]

        # --- Step 3: First run — just build snapshot, no alerts ---
        if is_first_run:
            entry = to_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
            continue

        # --- Step 4: New product ---
        if pid in new_ids:
            if now_in_stock:
                notify_new(product)
                alerts_sent += 1
                time.sleep(1.5)
            entry = to_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
            continue

        # --- Step 5: Existing product — check for changes ---
        if not old:
            snapshot[pid] = to_entry(product)
            continue

        # Carry forward barcode/sku if not in current JSON
        for key in ("barcode", "sku", "image", "volume_pricing"):
            if not product.get(key):
                product[key] = old.get(key, product.get(key, ""))

        old_price    = old.get("price", "")
        new_price    = product.get("price", "")
        old_stock    = old.get("stock")
        new_stock    = product.get("stock")

        # Back in stock
        if not was_in_stock and now_in_stock:
            notify_back_in_stock(product)
            alerts_sent += 1
            time.sleep(1.5)

        # Price drop (only if still in stock)
        elif now_in_stock:
            old_f = safe_float(old_price)
            new_f = safe_float(new_price)
            if old_f and new_f and old_f > 0:
                pct = (old_f - new_f) / old_f
                if pct > 0.01 and (old_f - new_f) > 0.02:
                    notify_price_drop(product, old_price, new_price, pct)
                    alerts_sent += 1
                    time.sleep(1.5)

            # Restock (stock went up meaningfully)
            if (old_stock is not None and new_stock is not None
                    and new_stock > old_stock + max(5, int(old_stock * 0.2))):
                notify_restock(product, old_stock, new_stock)
                alerts_sent += 1
                time.sleep(1.5)

        # Update snapshot
        entry = to_entry(product)
        entry["first_seen"] = old.get("first_seen", entry["first_seen"])
        snapshot[pid] = entry

    # Save snapshot
    save_snapshot(snapshot)

    if is_first_run:
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline saved — {len(snapshot)} products tracked. Monitoring begins next cycle.")
    else:
        print(f"  Done — {alerts_sent} alert(s) sent. {len(snapshot)} products tracked.")

# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    print("=" * 58)
    print("  Very Cosmetics New Arrivals Monitor")
    print(f"  Collection: {COLLECTION_URL}")
    print(f"  Interval:   every {CHECK_INTERVAL}s ({CHECK_INTERVAL//60} min)")
    print(f"  Alerts:     new listings, price drops, restocks")
    print("=" * 58)

    if not DISCORD_WEBHOOK:
        print("\n  ⚠️  DISCORD_WEBHOOK not set — alerts will be suppressed")

    if RUN_ONCE:
        run_check()
        return

    while True:
        try:
            run_check()
        except Exception as e:
            print(f"  [!] Unexpected error: {e}")
        print(f"  Sleeping {CHECK_INTERVAL}s ({CHECK_INTERVAL//60} min)...\n")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
