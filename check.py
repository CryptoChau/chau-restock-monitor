"""
CHAU Restock-Monitor
Prueft Schweizer TCG-Haendler auf Restocks von Pokemon (EN: Elite Trainer Box,
Booster Display, Booster Bundle, Ultra Premium Collection) und One Piece
Sealed-Produkten. Postet neue Restocks sofort an die jeweiligen Discord-Webhooks.

Gedacht fuer den Aufruf alle 5 Minuten via Windows-Aufgabenplanung.
"""
import html
import json
import os
import re
import sys
import time
import traceback
import unicodedata
from datetime import datetime, timezone

import requests

import config

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, config.STATE_FILE)
LOG_PATH = os.path.join(BASE_DIR, config.LOG_FILE)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CHAU-Restock-Monitor/1.0"
}


def log(msg):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode("ascii"))
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def normalize(text):
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            return {}
        # Migration: altes Schema hatte "in_stock" (bool) statt "status" (str)
        for entry in state.values():
            if "status" not in entry and "in_stock" in entry:
                entry["status"] = "instock" if entry["in_stock"] else "outofstock"
        return state
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# Sprachcode als eigenstaendiges "Wort" am Ende oder umgeben von Nicht-Buchstaben,
# damit z.B. "...Box DE" erkannt wird, aber "Deck"/"Display" nicht faelschlich matchen.
NON_ENGLISH_LANG_CODES = ["de", "jp", "jpn", "fr", "fra", "it", "cn", "chn", "kr", "kor", "ko"]
_LANG_SUFFIX_RE = re.compile(r"(?:^|[^a-z])(" + "|".join(NON_ENGLISH_LANG_CODES) + r")(?:$|[^a-z])")


def has_non_english_marker(text):
    t = normalize(text)
    if _LANG_SUFFIX_RE.search(t):
        return True
    # Deutsche UND englische Sprachbezeichnungen (manche Haendler wie lunivault.ch schreiben
    # z.B. "Simplified Chinese" statt "(CN)" - gefunden 2026-09-17 bei "Pokemon ... Booster
    # Display Simplified Chinese", das faelschlich als englisches Produkt durchging)
    words = [
        "deutsch", "japanisch", "franzosisch", "italienisch", "chinesisch", "koreanisch",
        "japanese", "korean", "french", "german", "italian", "japan", "korea",
        "chinese", "simplified chinese", "traditional chinese",
    ]
    return any(w in t for w in words)


def _contains_keyword(text, keyword):
    """Substring-Check fuer mehrteilige Keywords (z.B. 'booster display'), Wortgrenzen-Check
    fuer einzelne kurze Woerter (z.B. 'deck', 'box', 'pack') - verhindert False-Positives wie
    'Fleecedecke' (enthaelt 'deck' als Teilstring) oder 'Packung'."""
    if " " in keyword:
        return keyword in text
    return re.search(r"(?:^|[^a-z])" + re.escape(keyword) + r"(?:$|[^a-z])", text) is not None


def matches_pokemon(title):
    t = normalize(title)  # "pokemon" statt "pokémon", entfernt Akzente
    if "pokemon" not in t:
        return False
    if not any(_contains_keyword(t, k) for k in config.POKEMON_KEYWORDS):
        return False
    if has_non_english_marker(title):
        return False
    for ex in config.POKEMON_EXCLUDE:
        if normalize(ex) in t:
            return False
    return True


def matches_onepiece(title):
    t = normalize(title)
    if "one piece" not in t:
        return False
    if not any(_contains_keyword(t, k) for k in config.ONEPIECE_MUST_ALSO_CONTAIN):
        return False
    if has_non_english_marker(title):
        return False
    for ex in config.ONEPIECE_EXCLUDE:
        if normalize(ex) in t:
            return False
    return True


def matches_dragonball(title):
    t = normalize(title)
    if not any(_contains_keyword(t, k) for k in config.DRAGONBALL_KEYWORDS):
        return False
    if not any(_contains_keyword(t, k) for k in config.DRAGONBALL_MUST_ALSO_CONTAIN):
        return False
    if has_non_english_marker(title):
        return False
    for ex in config.DRAGONBALL_EXCLUDE:
        if normalize(ex) in t:
            return False
    return True


def matches_mtg(title):
    """Magic: The Gathering - im Unterschied zu den anderen Marken werden hier EN UND DE
    Produkte akzeptiert (Nutzerwunsch 2026-09-17), also KEIN has_non_english_marker()-Check."""
    t = normalize(title)
    if not any(_contains_keyword(t, k) for k in config.MTG_KEYWORDS):
        return False
    if not any(_contains_keyword(t, k) for k in config.MTG_MUST_ALSO_CONTAIN):
        return False
    for ex in config.MTG_EXCLUDE:
        if normalize(ex) in t:
            return False
    return True


def matches_yugioh(title):
    t = normalize(title)
    if not any(_contains_keyword(t, k) for k in config.YUGIOH_KEYWORDS):
        return False
    if not any(_contains_keyword(t, k) for k in config.YUGIOH_MUST_ALSO_CONTAIN):
        return False
    if has_non_english_marker(title):
        return False
    for ex in config.YUGIOH_EXCLUDE:
        if normalize(ex) in t:
            return False
    return True


def matches_pokemon_30th(title):
    """Pokemon 30th Anniversary Set, nur ENGLISCHE Produkte (Nutzerwunsch 2026-09-17).
    Bewusst NUR die Phrase "30th anniversary" (nicht das deutsche "30 Jahre") als Marker
    verwendet: Haendler wie cardmaniac.ch listen das deutsche Set als "... 30 Jahre" OHNE
    jede Sprachkennzeichnung (auch keine deutschen Pokemon-Namen als Indiz), waehrend
    ryuland.ch/lunivault.ch fuer beide Sprachen konsequent "30th Anniversary" + [EN]/Deutsch-
    Marker nutzen. So bleibt der Filter zuverlaessig englisch-only."""
    t = normalize(title)
    if "pokemon" not in t:
        return False
    if "30th anniversary" not in t:
        return False
    if has_non_english_marker(title):
        return False
    for ex in config.POKEMON_EXCLUDE:
        if normalize(ex) in t:
            return False
    return True


def matches_pokemon_30th_all(title):
    """Pokemon 30-Jahre-Jubilaeumsset, ALLE Sprachen (Nutzerwunsch 2026-09-17, zweiter/
    breiterer Unterkanal neben dem englisch-only Kanal). Matcht sowohl die deutsche Phrase
    "30 Jahre" (z.B. cardmaniac.ch) als auch "30th Anniversary" (ryuland.ch/lunivault.ch),
    keine Sprachfilterung - nur normale Zubehoer-/Graded-Ausschluesse."""
    t = normalize(title)
    if "pokemon" not in t:
        return False
    if "30 jahre" not in t and "30th anniversary" not in t:
        return False
    # Bewusst NICHT POKEMON_EXCLUDE (enthaelt Sprachausschluesse wie "Deutsch"/"(DE)") -
    # dieser Kanal will explizit alle Sprachen. Nur Zubehoer/Graded ausschliessen.
    accessory_exclude = [
        "Sleeve", "Playmat", "Binder", "Toploader", "Deck Box",
        "PSA", "BGS", "CGC", "graded", "Display Case", "Acryl", "Evoretro", "Gehäuse", "Gehaeuse",
    ]
    for ex in accessory_exclude:
        if normalize(ex) in t:
            return False
    return True


def maybe_notify_pokemon30th(state, product_key, title, status, domain, link, price, now, cart_link=None):
    """Zusaetzliche Meldung(en) an die Pokemon-30th-Anniversary-Unterkanaele, unabhaengig vom
    normalen Pokemon-Kanal getrackt (eigene state.json-Keys), damit alle Kanaele unabhaengig
    ihre eigene Neu-Erkennung haben. Zwei Kanaele: nur-Englisch, und alle Sprachen."""
    if matches_pokemon_30th(title):
        key30 = product_key + ":pokemon30th"
        prev = state.get(key30)
        prev_status = prev.get("status") if prev else None
        state[key30] = {"title": title, "status": status, "last_checked": now}
        notify_status_change(state, key30, title, status, prev_status, domain, link, price, "pokemon30th", now, cart_link=cart_link)

    if matches_pokemon_30th_all(title):
        key30all = product_key + ":pokemon30thall"
        prev = state.get(key30all)
        prev_status = prev.get("status") if prev else None
        state[key30all] = {"title": title, "status": status, "last_checked": now}
        notify_status_change(state, key30all, title, status, prev_status, domain, link, price, "pokemon30thall", now, cart_link=cart_link)


def detect_brand(title):
    """Erkennt die Franchise aus dem Titel, gibt "pokemon"/"onepiece"/"dragonball"/"mtg"/"yugioh" oder None zurueck."""
    if matches_pokemon(title):
        return "pokemon"
    if matches_onepiece(title):
        return "onepiece"
    if matches_dragonball(title):
        return "dragonball"
    if matches_mtg(title):
        return "mtg"
    if matches_yugioh(title):
        return "yugioh"
    return None


BRAND_LABELS = {
    "pokemon": "Pokemon", "onepiece": "One Piece", "dragonball": "Dragon Ball Super Fusion World",
    "mtg": "Magic: The Gathering", "yugioh": "Yu-Gi-Oh!",
    "pokemon30th": "Pokemon 30th Anniversary (EN)",
    "pokemon30thall": "Pokemon 30-Jahre-Jubilaeum (alle Sprachen)",
}
BRAND_WEBHOOKS = {
    "pokemon": (config.DISCORD_WEBHOOK_POKEMON, config.DISCORD_WEBHOOK_POKEMON_PREORDER),
    "onepiece": (config.DISCORD_WEBHOOK_ONEPIECE, config.DISCORD_WEBHOOK_ONEPIECE_PREORDER),
    "dragonball": (config.DISCORD_WEBHOOK_DRAGONBALL, config.DISCORD_WEBHOOK_DRAGONBALL_PREORDER),
    "mtg": (config.DISCORD_WEBHOOK_MTG, config.DISCORD_WEBHOOK_MTG_PREORDER),
    "yugioh": (config.DISCORD_WEBHOOK_YUGIOH, config.DISCORD_WEBHOOK_YUGIOH_PREORDER),
    "pokemon30th": (config.DISCORD_WEBHOOK_POKEMON_30TH, config.DISCORD_WEBHOOK_POKEMON_30TH_PREORDER),
    "pokemon30thall": (config.DISCORD_WEBHOOK_POKEMON_30TH_ALL, config.DISCORD_WEBHOOK_POKEMON_30TH_ALL_PREORDER),
}


def fetch_products(domain):
    """Holt Produkte ueber den Shopify-Standard-Endpoint /products.json (Pagination)."""
    products = []
    page = 1
    while page <= 10:  # Sicherheitslimit
        url = f"https://{domain}/products.json?limit=250&page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
        except Exception as e:
            log(f"  {domain}: Fehler beim Abruf, uebersprungen ({type(e).__name__})")
            return products
        if r.status_code != 200:
            if page == 1:
                log(f"  {domain}: HTTP {r.status_code} - vermutlich kein Shopify-Shop oder blockiert, uebersprungen")
            break
        try:
            data = r.json()
        except Exception:
            log(f"  {domain}: Antwort kein JSON, uebersprungen")
            break
        batch = data.get("products", [])
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 250:
            break
        page += 1
    return products


def fetch_woocommerce_products(domain):
    """Holt Produkte ueber die WooCommerce Store API (/wp-json/wc/store/v1/products?search=)."""
    base = domain if domain != "nooks.ch" else "www.nooks.ch"  # nooks.ch braucht www-Praefix
    products = []
    seen_ids = set()
    for term in ["pokemon", "one piece"]:
        url = f"https://{base}/wp-json/wc/store/v1/products"
        try:
            r = requests.get(
                url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT,
                params={"search": term, "per_page": 100},
            )
        except Exception as e:
            log(f"  {domain}: Fehler beim Abruf ({term}), uebersprungen ({type(e).__name__})")
            continue
        if r.status_code != 200:
            log(f"  {domain}: HTTP {r.status_code} bei Suche '{term}', uebersprungen")
            continue
        try:
            batch = r.json()
        except Exception:
            log(f"  {domain}: Antwort kein JSON, uebersprungen")
            continue
        for p in batch:
            if p.get("id") not in seen_ids:
                seen_ids.add(p.get("id"))
                products.append(p)
    return products


_SHOPWARE_BOX_RE = re.compile(r'(?=<div class="card product-box)')
_SHOPWARE_INFO_RE = re.compile(r'data-product-information="([^"]+)"')
_SHOPWARE_LINK_RE = re.compile(r'href="(https://[^"]+)"')


def fetch_shopware_products(domain):
    """Holt Produkte ueber die Shopware-Widget-Suche (/widgets/search?search=&p=), HTML-Parsing.
    Liefert Liste von dicts mit title/link/price/in_stock (kompatibel zu den anderen Fetch-Funktionen)."""
    products = []
    seen_ids = set()
    for term in ["pokemon", "one piece"]:
        page = 1
        while page <= 8:  # Sicherheitslimit
            url = f"https://{domain}/widgets/search"
            try:
                r = requests.get(
                    url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT,
                    params={"search": term, "p": page},
                )
            except Exception as e:
                log(f"  {domain}: Fehler beim Abruf ({term} S.{page}), uebersprungen ({type(e).__name__})")
                break
            if r.status_code != 200:
                if page == 1:
                    log(f"  {domain}: HTTP {r.status_code} bei Suche '{term}', uebersprungen")
                break
            html_text = r.text
            boxes = _SHOPWARE_BOX_RE.split(html_text)[1:]
            if not boxes:
                break
            for b in boxes:
                m = _SHOPWARE_INFO_RE.search(b)
                if not m:
                    continue
                try:
                    info = json.loads(html.unescape(m.group(1)))
                except Exception:
                    continue
                pid = info.get("id")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                link_m = _SHOPWARE_LINK_RE.search(b)
                link = link_m.group(1) if link_m else f"https://{domain}/"
                b_lower = b.lower()
                in_stock = "in den warenkorb" in b_lower and "ausverkauft" not in b_lower
                products.append({
                    "id": pid,
                    "title": info.get("name", ""),
                    "link": link,
                    "price": info.get("price"),
                    "in_stock": in_stock,
                })
            if len(boxes) < 24:  # amazingtoys.ch zeigt 24 pro Seite
                break
            page += 1
    return products


_SPIELEZAR_BLOCK_RE = re.compile(
    r'data-id-product="(\d+)"[\s\S]{0,300}?<a class="no-underline" href="([^"]+)" title="([^"]+)"'
)
_SPIELEZAR_STOCK_RE = re.compile(r'availability_statut"\s+class="([^"]+)"')


def fetch_spielezar_products(domain):
    """Holt Produkte per HTML-Suche unter /suche?q=<term> (eigenes CMS "genzo", plain HTML,
    kein Playwright noetig). Lagerstatus steht NICHT auf der Trefferliste, nur auf der
    Produktseite selbst (Badge-Klasse "badge_success"/"badge_danger" neben "Lagerbestand:") -
    daher wird fuer jeden ueber Marke+Sprache erkannten Treffer zusaetzlich die Produktseite
    abgerufen (spart Requests, da nur tatsaechliche Treffer nachgeprueft werden)."""
    products = []
    seen_ids = set()
    for term in config.SOFTRIDGE_SEARCH_TERMS:  # gleiche kombinierte Suchbegriffsliste wie softridge.ch
        url = f"https://www.{domain}/suche"
        try:
            r = requests.get(url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT, params={"q": term})
        except Exception as e:
            log(f"  {domain}: Fehler beim Abruf ({term}), uebersprungen ({type(e).__name__})")
            continue
        if r.status_code != 200:
            log(f"  {domain}: HTTP {r.status_code} bei Suche '{term}', uebersprungen")
            continue
        for m in _SPIELEZAR_BLOCK_RE.finditer(r.text):
            pid, link, title = m.group(1), m.group(2), html.unescape(m.group(3))
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            title_clean = title
            if not detect_brand(title_clean):
                continue  # nur echte Treffer bekommen einen teuren Produktseiten-Request
            try:
                pr = requests.get(link, headers=HEADERS, timeout=config.REQUEST_TIMEOUT)
                stock_m = _SPIELEZAR_STOCK_RE.search(pr.text) if pr.status_code == 200 else None
            except Exception:
                stock_m = None
            badge = stock_m.group(1) if stock_m else ""
            in_stock = "badge_success" in badge
            products.append({
                "id": pid,
                "title": title_clean,
                "link": link,
                "price": None,
                "in_stock": in_stock,
            })
    return products


def fetch_softridge_products(domain):
    """Holt Produkte ueber softridges interne Such-API (/api/shop/products?searchTerms=...).
    Liefert Liste von dicts mit id/title/link/price/in_stock/preorder/region (region=explizites
    Sprachfeld aus der API, zuverlässiger als Titel-Parsing - siehe Memory)."""
    products = []
    seen_ids = set()
    for term in config.SOFTRIDGE_SEARCH_TERMS:
        url = f"https://www.{domain}/api/shop/products"
        try:
            r = requests.get(
                url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT,
                params={
                    "loadingType": 79, "languageId": 2, "navigationId": 25982,
                    "filterByAllCategories": "True", "onlineExclusive": "",
                    "displayType": 1, "searchTerms": term, "page": 1,
                },
            )
        except Exception as e:
            log(f"  {domain}: Fehler beim Abruf ({term}), uebersprungen ({type(e).__name__})")
            continue
        if r.status_code != 200:
            log(f"  {domain}: HTTP {r.status_code} bei Suche '{term}', uebersprungen")
            continue
        try:
            data = r.json()
        except Exception:
            log(f"  {domain}: Antwort kein JSON, uebersprungen")
            continue
        for p in data.get("products", []):
            pid = p.get("id")
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            products.append({
                "id": pid,
                "title": p.get("fullTitle", ""),
                "link": "https://www." + domain + p.get("linkUrl", ""),
                "price": p.get("salesPrice"),
                "in_stock": p.get("statusColor") == "Green",
                "preorder": bool(p.get("isComingSoon")),
                "region": p.get("regionCode"),
            })
    return products


def product_in_stock(product):
    for v in product.get("variants", []):
        if v.get("available"):
            return True, v
    return False, None


def is_preorder(text):
    t = normalize(text)
    return any(normalize(m) in t for m in config.PREORDER_MARKERS)


DRY_RUN = os.environ.get("CHAU_DRY_RUN") == "1"


def notify_status_change(state, product_key, title, status, prev_status, domain, link, price, brand, now, cart_link=None):
    """Gemeinsame Notify-Logik: meldet neue Vorbestellungen/Restocks an den passenden Discord-Kanal.
    brand: "pokemon"/"onepiece"/"dragonball" (siehe BRAND_WEBHOOKS/BRAND_LABELS).
    cart_link (optional): direkter "in den Warenkorb legen"-Link (Shopify /cart/add), damit der Nutzer
    beim Restock nur noch auf Kaufen klicken muss statt selbst zu suchen (Nutzerwunsch 2026-09-17)."""
    label = BRAND_LABELS[brand]
    restock_webhook, preorder_webhook = BRAND_WEBHOOKS[brand]
    cart_line = f"\U0001F6D2 Direkt in den Warenkorb: {cart_link}\n" if cart_link else ""
    if status == "preorder" and prev_status != "preorder":
        msg = (
            f"\U0001F7E1 VORBESTELLUNG ({label}): **{title}**\n"
            f"Haendler: {domain}\n"
            f"Preis: CHF {price}\n"
            f"Link: {link}\n"
            f"{cart_line}"
            f"Zeit: {now}"
        )
        log(f"  VORBESTELLUNG gefunden: {title} bei {domain}")
        send_discord(preorder_webhook, msg)
    elif status == "instock" and prev_status != "instock":
        msg = (
            f"\U0001F7E2 RESTOCK ({label}): **{title}**\n"
            f"Haendler: {domain}\n"
            f"Preis: CHF {price}\n"
            f"Link: {link}\n"
            f"{cart_line}"
            f"Zeit: {now}"
        )
        log(f"  RESTOCK gefunden: {title} bei {domain}")
        send_discord(restock_webhook, msg)


def send_discord(webhook_url, content):
    if DRY_RUN:
        log(f"  [DRY RUN] wuerde senden: {content[:150]}")
        return
    try:
        r = requests.post(webhook_url, json={"content": content}, timeout=config.REQUEST_TIMEOUT)
        if r.status_code not in (200, 204):
            log(f"  Discord-Post fehlgeschlagen: HTTP {r.status_code} {r.text[:200]}")
        else:
            log(f"  -> Discord-Nachricht gesendet")
    except Exception as e:
        log(f"  Discord-Post Fehler: {e}")


def check_browser_retailers(state, now):
    """Prueft grosse Haendler ohne /products.json per echtem Headless-Browser (Playwright)."""
    if sync_playwright is None:
        log("  Playwright nicht installiert, Browser-Haendler uebersprungen")
        return

    terms = [(t, "pokemon") for t in config.BROWSER_SEARCH_TERMS_POKEMON] + \
            [(t, "onepiece") for t in config.BROWSER_SEARCH_TERMS_ONEPIECE] + \
            [(t, "dragonball") for t in config.BROWSER_SEARCH_TERMS_DRAGONBALL] + \
            [(t, "mtg") for t in config.BROWSER_SEARCH_TERMS_MTG] + \
            [(t, "yugioh") for t in config.BROWSER_SEARCH_TERMS_YUGIOH]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-http2"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        page = ctx.new_page()

        for retailer in config.BROWSER_RETAILERS:
            domain = retailer["name"]
            log(f"  Browser-Haendler: {domain} ...")
            for term, brand_hint in terms:
                url = retailer["search_url"].format(query=term.replace(" ", "+"))
                try:
                    page.goto(url, timeout=20000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2500)
                except Exception as e:
                    log(f"  {domain} ({term}): Fehler beim Laden, uebersprungen ({type(e).__name__})")
                    continue

                name_selector = retailer.get("name_selector")
                name_attr = retailer.get("name_attr")  # z.B. "alt" - fuer ungekuerzten Titel aus <img alt>
                # card_selector: CSS-Selektor fuer die umschliessende Produktkarte, deren Text
                # gelesen wird (statt nur des <a>-Elements selbst, das oft nur ein Bild ohne
                # Text enthaelt). Frueher war dies fest auf 'article' verdrahtet und wurde vom
                # konfigurierten card_selector ueberhaupt nicht gelesen - echter Bug, gefunden
                # 2026-09-17 bei wog.ch (keine <article>-Tags, Titel blieb leer). Default bleibt
                # 'article' fuer Abwaertskompatibilitaet mit bereits funktionierenden Haendlern.
                card_sel = retailer.get("card_selector") or "article"
                try:
                    if name_selector and name_attr:
                        # Titel aus einem Attribut lesen (z.B. img[alt]) statt innerText - noetig
                        # wenn die Kartenansicht lange Titel per JS abschneidet ("...") und dabei
                        # die Sprachkennung (-EN-/-DE-) verloren geht, siehe wog.ch (Memory).
                        # Stock-Text (out_markers/Preorder) kommt weiterhin aus card_sel-innerText.
                        links = page.eval_on_selector_all(
                            retailer["product_link_selector"],
                            """(els, args) => { const [sel, attr, cardSel] = args; return els.map(e => {
                                const nameEl = e.querySelector(sel) || e.closest('li,article,div')?.querySelector(sel);
                                const card = e.closest(cardSel) || e;
                                const fullTitle = nameEl ? nameEl.getAttribute(attr) : null;
                                return {href: e.href, text: (fullTitle || '') + '\\n' + card.innerText};
                            }); }""",
                            [name_selector, name_attr, card_sel],
                        )
                    elif name_selector:
                        # sauberer Produktname aus dediziertem Element (z.B. migros.ch mo-product-name)
                        links = page.eval_on_selector_all(
                            retailer["product_link_selector"],
                            """(els, sel) => els.map(e => {
                                const nameEl = e.querySelector(sel) || e.closest('li,article,div')?.querySelector(sel);
                                return {href: e.href, text: (nameEl ? nameEl.innerText : e.innerText)};
                            })""",
                            name_selector,
                        )
                    else:
                        links = page.eval_on_selector_all(
                            retailer["product_link_selector"],
                            """(els, sel) => els.map(e => ({href: e.href, text: (e.closest(sel)||e).innerText}))""",
                            card_sel,
                        )
                except Exception as e:
                    log(f"  {domain} ({term}): Fehler beim Auslesen, uebersprungen ({type(e).__name__})")
                    continue

                # Mehrere <a>-Elemente koennen auf dieselbe Produkt-URL zeigen (z.B. ein
                # Bild-Link ohne Text + ein Text-Link) - pro href die Version mit dem laengsten
                # Text behalten statt blind die erste (sonst leerer Titel, siehe Memory conforama.ch)
                best_by_href = {}
                for item in links:
                    href = item.get("href", "")
                    text = item.get("text", "") or ""
                    if not href:
                        continue
                    if href not in best_by_href or len(text) > len(best_by_href[href]):
                        best_by_href[href] = text

                for href, text in best_by_href.items():

                    # Echte Titelzeile finden: laengste der ersten paar Zeilen (Marke wie "Pokemon"
                    # allein oder "Empty"-Platzhalter werden so uebersprungen)
                    lines = [ln.strip() for ln in text.split("\n") if ln.strip() and ln.strip().lower() != "empty"]
                    candidates = [ln for ln in lines[:5] if len(ln) >= 15]
                    title_line = max(candidates, key=len) if candidates else (lines[0] if lines else "")
                    if not title_line or len(title_line) < 10:
                        continue

                    brand = detect_brand(text)
                    if not brand:
                        continue

                    # Stock-Heuristik: "in den warenkorb" vorhanden UND kein "nicht verfuegbar"/"ausverkauft"
                    t_norm = normalize(text)
                    out_markers = ["nicht verfugbar", "ausverkauft", "zurzeit nicht", "not available", "vergriffen", "nicht auf lager", "nicht mehr lieferbar", "nicht mehr bestellbar"]
                    in_stock = not any(m in t_norm for m in out_markers)
                    preorder = is_preorder(text)
                    status = "preorder" if preorder else ("instock" if in_stock else "outofstock")

                    product_key = f"{domain}:{href}"
                    prev = state.get(product_key)
                    prev_status = prev.get("status") if prev else None

                    state[product_key] = {
                        "title": title_line,
                        "status": status,
                        "last_checked": now,
                    }

                    label = BRAND_LABELS[brand]
                    restock_webhook, preorder_webhook = BRAND_WEBHOOKS[brand]
                    if status == "preorder" and prev_status != "preorder":
                        msg = (
                            f"\U0001F7E1 VORBESTELLUNG ({label}): **{title_line}**\n"
                            f"Haendler: {domain}\n"
                            f"Link: {href}\n"
                            f"Zeit: {now}"
                        )
                        log(f"  VORBESTELLUNG gefunden: {title_line} bei {domain}")
                        send_discord(preorder_webhook, msg)
                    elif status == "instock" and prev_status != "instock":
                        msg = (
                            f"\U0001F7E2 RESTOCK ({label}): **{title_line}**\n"
                            f"Haendler: {domain}\n"
                            f"Link: {href}\n"
                            f"Zeit: {now}"
                        )
                        log(f"  RESTOCK gefunden: {title_line} bei {domain}")
                        send_discord(restock_webhook, msg)

                    maybe_notify_pokemon30th(state, product_key, title_line, status, domain, href, "?", now)

        browser.close()


LOCK_PATH = os.path.join(BASE_DIR, "check.lock")


def acquire_lock():
    """Einfache Datei-Sperre, damit sich der Scheduled Task und ein manueller Testlauf
    nicht gleichzeitig state.json ueberschreiben (Race Condition, siehe Memory)."""
    if os.path.exists(LOCK_PATH):
        try:
            age = time.time() - os.path.getmtime(LOCK_PATH)
        except OSError:
            age = 9999
        if age < 1350:  # 22.5 Min - etwas ueber dem Task-Scheduler-Zeitlimit (20 Min), damit die
            # interne Sperre nicht VOR dem harten Kill durch Windows als "veraltet" durchgeht
            # und einen ueberlappenden Zweitlauf zulaesst (siehe Memory, Duplikat-Bug 2026-09-17)
            return False
    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


PRIORITY_ONLY = os.environ.get("CHAU_PRIORITY_ONLY") == "1"


def run():
    log("=== Restock-Check gestartet ===" + (" (PRIORITY_ONLY)" if PRIORITY_ONLY else ""))
    state = load_state()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for domain in config.RETAILERS:
        log(f"Pruefe {domain} ...")
        products = fetch_products(domain)
        if not products:
            continue

        for p in products:
            title = p.get("title", "")
            handle = p.get("handle", "")
            product_key = f"{domain}:{p.get('id')}"
            brand = detect_brand(title)
            if not brand:
                continue

            in_stock, variant = product_in_stock(p)
            tags = p.get("tags", "")
            tags_text_part = " ".join(tags) if isinstance(tags, list) else str(tags)
            tags_text = f"{title} {tags_text_part} {p.get('product_type', '')}"
            preorder = is_preorder(tags_text)
            status = "preorder" if preorder else ("instock" if in_stock else "outofstock")

            prev = state.get(product_key)
            prev_status = prev.get("status") if prev else None

            state[product_key] = {
                "title": title,
                "status": status,
                "last_checked": now,
            }

            price = variant.get("price", "?") if variant else "?"
            link = f"https://{domain}/products/{handle}"
            # Direkter Shopify-Warenkorb-Link (nur wenn Variante verfuegbar ist), spart dem Nutzer
            # den Klick auf die Produktseite - Nutzerwunsch 2026-09-17 (schnelleres manuelles Kaufen)
            cart_link = f"https://{domain}/cart/add?id={variant.get('id')}&quantity=1" if variant else None
            notify_status_change(state, product_key, title, status, prev_status, domain, link, price, brand, now, cart_link=cart_link)
            maybe_notify_pokemon30th(state, product_key, title, status, domain, link, price, now, cart_link=cart_link)

    for domain in config.SOFTRIDGE_RETAILERS:
        log(f"Pruefe {domain} (Softridge) ...")
        products = fetch_softridge_products(domain)
        if not products:
            continue

        for p in products:
            title = p["title"]
            product_key = f"{domain}:{p['id']}"
            brand = detect_brand(title)
            if not brand:
                continue

            # Regionsfeld der API zusaetzlich pruefen (zuverlässiger als Titel-Parsing, siehe
            # fetch_softridge_products): softridge nutzt Ein-Buchstaben-Suffixe wie "-D-" statt
            # "(DE)", die der generische Sprachfilter in matches_*() nicht zuverlässig erkennt.
            allowed_regions = {"EN", "DE"} if brand == "mtg" else {"EN"}
            region = p.get("region")
            if region and region not in allowed_regions:
                continue

            preorder = p["preorder"]
            status = "preorder" if preorder else ("instock" if p["in_stock"] else "outofstock")

            prev = state.get(product_key)
            prev_status = prev.get("status") if prev else None

            state[product_key] = {
                "title": title,
                "status": status,
                "last_checked": now,
            }

            price = p.get("price")
            price_str = f"{price:.2f}" if isinstance(price, (int, float)) else "?"
            notify_status_change(state, product_key, title, status, prev_status, domain, p["link"], price_str, brand, now)
            maybe_notify_pokemon30th(state, product_key, title, status, domain, p["link"], price_str, now)

    for domain in config.SPIELEZAR_RETAILERS:
        log(f"Pruefe {domain} (Spielezar/genzo) ...")
        products = fetch_spielezar_products(domain)
        if not products:
            continue

        for p in products:
            title = p["title"]
            product_key = f"{domain}:{p['id']}"
            brand = detect_brand(title)
            if not brand:
                continue

            preorder = is_preorder(title)
            status = "preorder" if preorder else ("instock" if p["in_stock"] else "outofstock")

            prev = state.get(product_key)
            prev_status = prev.get("status") if prev else None

            state[product_key] = {
                "title": title,
                "status": status,
                "last_checked": now,
            }

            notify_status_change(state, product_key, title, status, prev_status, domain, p["link"], "?", brand, now)
            maybe_notify_pokemon30th(state, product_key, title, status, domain, p["link"], "?", now)

    if PRIORITY_ONLY:
        # Schneller 1-Minuten-Check (kein Playwright, nur Shopify-Haendler) - deckt nur die
        # RETAILERS-Liste ab, siehe .github/workflows/priority-check.yml
        save_state(state)
        log("=== Restock-Check beendet (PRIORITY_ONLY) ===")
        return

    for domain in config.WOOCOMMERCE_RETAILERS:
        log(f"Pruefe {domain} (WooCommerce) ...")
        products = fetch_woocommerce_products(domain)
        if not products:
            continue

        for p in products:
            title = html.unescape(p.get("name", ""))
            product_key = f"{domain}:{p.get('id')}"
            brand = detect_brand(title)
            if not brand:
                continue

            in_stock = bool(p.get("is_in_stock"))
            preorder = is_preorder(title)
            status = "preorder" if preorder else ("instock" if in_stock else "outofstock")

            prev = state.get(product_key)
            prev_status = prev.get("status") if prev else None

            state[product_key] = {
                "title": title,
                "status": status,
                "last_checked": now,
            }

            prices = p.get("prices", {}) or {}
            minor_unit = prices.get("currency_minor_unit", 2)
            raw_price = prices.get("price")
            try:
                price = f"{int(raw_price) / (10 ** minor_unit):.2f}" if raw_price is not None else "?"
            except (ValueError, TypeError):
                price = "?"
            link = p.get("permalink", f"https://{domain}/")
            notify_status_change(state, product_key, title, status, prev_status, domain, link, price, brand, now)
            maybe_notify_pokemon30th(state, product_key, title, status, domain, link, price, now)

    for domain in config.SHOPWARE_RETAILERS:
        log(f"Pruefe {domain} (Shopware) ...")
        products = fetch_shopware_products(domain)
        if not products:
            continue

        for p in products:
            title = p["title"]
            product_key = f"{domain}:{p['id']}"
            brand = detect_brand(title)
            if not brand:
                continue

            preorder = is_preorder(title)
            status = "preorder" if preorder else ("instock" if p["in_stock"] else "outofstock")

            prev = state.get(product_key)
            prev_status = prev.get("status") if prev else None

            state[product_key] = {
                "title": title,
                "status": status,
                "last_checked": now,
            }

            price = p.get("price")
            price_str = f"{price:.2f}" if isinstance(price, (int, float)) else "?"
            notify_status_change(state, product_key, title, status, prev_status, domain, p["link"], price_str, brand, now)
            maybe_notify_pokemon30th(state, product_key, title, status, domain, p["link"], price_str, now)

    log("Pruefe grosse Haendler (Browser) ...")
    check_browser_retailers(state, now)

    save_state(state)
    log("=== Restock-Check beendet ===")


if __name__ == "__main__":
    if not acquire_lock():
        log("Ein anderer Lauf ist bereits aktiv (Lock vorhanden) - dieser Lauf wird uebersprungen.")
        sys.exit(0)
    try:
        run()
    except Exception:
        log("FEHLER:\n" + traceback.format_exc())
        sys.exit(1)
    finally:
        release_lock()
