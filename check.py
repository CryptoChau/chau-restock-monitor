"""
CHAU Restock-Monitor
Prueft Schweizer TCG-Haendler auf Restocks von Pokemon (EN: Elite Trainer Box,
Booster Display, Booster Bundle, Ultra Premium Collection) und One Piece
Sealed-Produkten. Postet neue Restocks sofort an die jeweiligen Discord-Webhooks.

Gedacht fuer den Aufruf alle 5 Minuten via Windows-Aufgabenplanung.
"""
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import traceback
import unicodedata
import urllib.parse
from datetime import datetime, timezone

import requests

import config

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

try:
    from camoufox.sync_api import Camoufox
except ImportError:
    Camoufox = None

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
        # Franzoesische Sprachbezeichnungen (franzoesischsprachige Haendler wie pokealp.ch
        # schreiben "Edition japonaise"/"Boite Japonaise"/"Japonais" statt "(JP)" - gefunden
        # 2026-09-18, ging vorher als englisches Produkt durch)
        "japonais", "japonaise", "chinois", "chinoise", "coreen", "coreenne",
        "allemand", "allemande", "italien", "italienne",
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


# Sichtbarer Text auf einer Galaxus-Seite. "verfuegbar" allein reicht NICHT: die Kachel
# kippt bei Vorbestellungen und Marketplace-Angeboten genau auf dieses Wort (User-Report
# 2026-11, Produkte 72350263 / 72348004) und wurde dadurch als RESTOCK gelabelt.
_GALAXUS_UNAVAILABLE_MARKERS = (
    "nicht verfugbar", "nicht lieferbar", "ausverkauft", "vergriffen",
    "nicht auf lager", "nicht an lager", "nicht mehr lieferbar", "nicht mehr bestellbar",
    "out of stock", "sold out", "unavailable",
)
_GALAXUS_ANNOUNCED_MARKERS = (
    "noch nicht erschienen", "bald verfugbar", "upcoming", "angekundigt", "ankundigung",
    "announced", "demnachst", "in kurze",
)
_GALAXUS_PAGE_PREORDER_RE = re.compile(
    r"\bvor(?:bestellung|bestellen|besteller)\b|"
    r"\bpre[\s\-]?order\b|"
    r"\bcoming soon\b|"
    r"\berscheint am\b|"
    r"\berscheinungsdatum\b|"
    r"\brelease date\b|"
    r"\bin einigen tagen\b|"
    r"\bvoraussichtlich\b|"
    r"\bin \d+\s*tagen\b",
)
_GALAXUS_POSITIVE_STOCK_RE = re.compile(
    r"\b\d+\s*(?:stuck|stueck)\s+an lager\b|"
    r"\bnur noch\s+\d+\s*(?:stuck|stueck)\b|"
    r"\bsofort lieferbar\b|"
    r"\bsofort verfugbar\b|"
    r"\bmorgen bei dir\b|"
    r"\ban lager\b|"
    r"\bauf lager\b",
)
_GALAXUS_SELLER_LABEL_RE = re.compile(
    r"(?:Verkauf(?:\s+und\s+Versand)?\s+durch|Verkauft\s+(?:von|durch)|"
    r"Anbieter\s*[:\-]|Händler\s*[:\-]|Haendler\s*[:\-])\s*([^\n]{2,80})",
    re.IGNORECASE,
)
_GALAXUS_JSON_SELLER_RE = re.compile(
    r'"(?:supplierName|merchantName|sellerName)"\s*:\s*"([^"\\]{1,80})"',
    re.IGNORECASE,
)
_GALAXUS_JSON_SUPPLIER_NAME_RE = re.compile(
    r'"supplier"\s*:\s*\{(?:(?!\{).){0,400}?"name"\s*:\s*"([^"\\]{1,80})"',
    re.IGNORECASE | re.DOTALL,
)
_GALAXUS_THIRD_PARTY_RE = re.compile(
    r"beim drittanbieter|\bdrittanbieter\b|marketplace-angebot|marktplatzangebot|"
    r"externer anbieter|marktplatzverkaeufer|marktplatz-verkaeufer",
)
_GALAXUS_JSON_PREORDER_TRUE_RE = re.compile(
    r'"(?:isPreorder|isComingSoon)"\s*:\s*true\b',
    re.IGNORECASE,
)
_GALAXUS_JSON_PREORDER_VALUE_RE = re.compile(
    r'"(?:availability|stockStatus|offerType)"\s*:\s*"(preorder|pre-order|announced|upcoming)"',
    re.IGNORECASE,
)
_GALAXUS_JSON_INSTOCK_VALUE_RE = re.compile(
    r'"(?:availability|stockStatus)"\s*:\s*"(instock|in_stock|available)"',
    re.IGNORECASE,
)
_GALAXUS_DIRECT_SELLER_NAMES = {
    "galaxus",
    "galaxus ag",
    "digitec galaxus",
    "digitec galaxus ag",
}


def _html_to_visible(raw):
    """Sichtbarer Seitentext. Script/Style bleiben aussen, damit i18n-Keys wie
    "vorbestellung" im JS-Bundle eine echte Lagerzeile nicht zur Vorbestellung machen."""
    text = raw or ""
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article|span)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(text)


def nonavailable_kind(text):
    """preorder/announced/outofstock, wenn der kurze Titel selbst schon sagt, dass nichts
    sofort kaufbar ist. None = kein solcher Hinweis. Nur fuer Titel, nicht fuer ganzes HTML
    (sonst trifft z.B. "release:" in einer Produkttabelle jedes Galaxus-Angebot)."""
    if not text:
        return None
    t = normalize(text)
    if any(m in t for m in _GALAXUS_UNAVAILABLE_MARKERS):
        return "outofstock"
    if is_preorder(text) or _GALAXUS_PAGE_PREORDER_RE.search(t):
        return "preorder"
    if any(m in t for m in _GALAXUS_ANNOUNCED_MARKERS):
        return "announced"
    return None


def galaxus_visible_availability(visible):
    """instock nur bei positivem Lagertext. Vorbestellung/Ankuendigung/nicht lieferbar
    gewinnen immer. Blosses "verfuegbar" bleibt unknown (fail-closed)."""
    t = normalize(visible or "")
    if not t.strip():
        return "unknown"
    if any(m in t for m in _GALAXUS_UNAVAILABLE_MARKERS):
        return "unavailable"
    if _GALAXUS_PAGE_PREORDER_RE.search(t):
        return "preorder"
    if any(m in t for m in _GALAXUS_ANNOUNCED_MARKERS):
        return "announced"
    if _GALAXUS_POSITIVE_STOCK_RE.search(t):
        return "instock"
    return "unknown"


def _clean_galaxus_seller(name):
    name = html.unescape(name or "")
    name = re.split(r"[\n|,]", name, maxsplit=1)[0]
    name = re.split(
        r"\s+(?:ab|inkl|nur|in|chf|für|fur|versand)\b",
        name,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return re.sub(r"\s+", " ", name).strip(" .:-–—")


def _is_direct_galaxus_seller(name):
    n = normalize(name)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n in _GALAXUS_DIRECT_SELLER_NAMES


def galaxus_seller_from_page(raw, visible):
    """direct nur bei positiv benanntem Galaxus-Verkaeufer.
    Spaetere weitere Angebote auf derselben Seite aendern den ersten "Verkauf durch"-Namen
    nicht. Widerspruch zu einem Drittanbieter-Hinweis ist unknown (fail-closed)."""
    labels = []
    for match in _GALAXUS_SELLER_LABEL_RE.finditer(visible or ""):
        cleaned = _clean_galaxus_seller(match.group(1))
        if cleaned:
            labels.append(cleaned)
    json_names = []
    for regex in (_GALAXUS_JSON_SELLER_RE, _GALAXUS_JSON_SUPPLIER_NAME_RE):
        for match in regex.finditer(raw or ""):
            cleaned = _clean_galaxus_seller(match.group(1))
            if cleaned:
                json_names.append(cleaned)
    third = bool(_GALAXUS_THIRD_PARTY_RE.search(normalize(visible or "")))
    if labels:
        primary_direct = _is_direct_galaxus_seller(labels[0])
        if primary_direct and third:
            return "unknown"
        return "direct" if primary_direct else "third_party"
    if json_names:
        directs = [_is_direct_galaxus_seller(n) for n in json_names]
        if third and any(directs):
            return "unknown"
        if third or not any(directs):
            return "third_party"
        if all(directs):
            return "direct"
        return "unknown"
    if third:
        return "third_party"
    return "unknown"


def _apply_galaxus_json_availability(raw, availability):
    value_match = _GALAXUS_JSON_PREORDER_VALUE_RE.search(raw or "")
    json_flag = bool(_GALAXUS_JSON_PREORDER_TRUE_RE.search(raw or ""))
    json_pre = bool(value_match) or json_flag
    json_in = bool(_GALAXUS_JSON_INSTOCK_VALUE_RE.search(raw or ""))
    if json_pre and json_in and availability == "unknown":
        return "unknown"
    if json_pre and availability in ("instock", "unknown"):
        if value_match and value_match.group(1).lower() in ("announced", "upcoming"):
            return "announced"
        return "preorder"
    if json_in and availability == "unknown" and not json_pre:
        return "instock"
    return availability


def classify_galaxus_offer_text(text):
    """Verkaufs- und Lieferurteil einer Galaxus-Produktseite.
    seller: direct | third_party | unknown
    availability: instock | preorder | announced | unavailable | unknown
    error: None oder kurzer Grund. Leerer/unbrauchbarer Text ist ein Fehler (fail-closed)."""
    if not isinstance(text, str) or not text.strip():
        return {"seller": "unknown", "availability": "unknown", "error": "empty"}
    visible = _html_to_visible(text)
    availability = galaxus_visible_availability(visible)
    availability = _apply_galaxus_json_availability(text, availability)
    return {
        "seller": galaxus_seller_from_page(text, visible),
        "availability": availability,
        "error": None,
    }


def _response_text(resp):
    raw = resp.text() if callable(getattr(resp, "text", None)) else resp.text
    if callable(raw):
        raw = raw()
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        raise TypeError("galaxus response text")
    return raw


def load_galaxus_offer(page, link):
    """Liest die Produktseite und klassifiziert sie. Netzwerk- und Parsefehler werden
    zu unknown + error, nie zu 'Galaxus verkauft und lieferbar'."""
    try:
        resp = page.request.get(link, timeout=20000)
        text = _response_text(resp)
    except Exception:
        try:
            page.goto(link, timeout=25000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            text = page.content()
        except Exception as e:
            log(f"  galaxus Direktverkauf-Check fehlgeschlagen ({type(e).__name__})")
            return {"seller": "unknown", "availability": "unknown", "error": type(e).__name__}
    try:
        if not isinstance(text, str):
            raise TypeError("galaxus page content")
        return classify_galaxus_offer_text(text)
    except Exception as e:
        log(f"  galaxus Direktverkauf-Parse fehlgeschlagen ({type(e).__name__})")
        return {"seller": "unknown", "availability": "unknown", "error": type(e).__name__}


def pokemon30th_stock_allowed(domain, title, galaxus_offer):
    """Ob ein RESTOCK in den englischen 30th-Stock-Kanal darf.
    Galaxus: nur direkter Galaxus-Verkaeufer UND positiv verfuegbar. Drittpartei,
    unbekannter Verkaeufer, fehlende Seite und Parse-/Ladefehler sind verboten."""
    if nonavailable_kind(title):
        return False
    if domain == "galaxus.ch":
        if not galaxus_offer or galaxus_offer.get("error"):
            return False
        if galaxus_offer.get("seller") != "direct":
            return False
        if galaxus_offer.get("availability") != "instock":
            return False
    return True


def resolve_pokemon30th_en_status(domain, status, title, galaxus_offer, prev_status):
    """Status nur fuer den englischen 30th-Kanal.
    Rueckgabe: (effective_status, update_state, notify).
    update_state False = Zustand nicht anfassen und nichts senden (erneuter Versuch spaeter)."""
    if domain == "galaxus.ch" and status == "instock":
        # Schon geprueft und die Kachel bleibt "instock": nicht erneut senden und nicht bei
        # jedem Lauf neu laden. Preisupdates ohne Statuswechsel erzeugen keine Meldung.
        # Ein neuer Beleg kommt, sobald die Kachel den Status verlaesst und zurueckkehrt.
        if galaxus_offer is None and prev_status in ("instock", "marketplace", "unverified"):
            return prev_status, True, False
        if pokemon30th_stock_allowed(domain, title, galaxus_offer):
            return "instock", True, True
        # Ladefehler / leere Antwort: Zustand nicht festschreiben, naechster Lauf versucht es erneut.
        if not galaxus_offer or galaxus_offer.get("error") or galaxus_offer.get("seller") not in ("direct", "third_party", "unknown"):
            return None, False, False
        if galaxus_offer.get("seller") == "third_party":
            return "marketplace", True, True
        title_kind = nonavailable_kind(title)
        avail = galaxus_offer.get("availability")
        if title_kind == "outofstock" or avail == "unavailable":
            return "outofstock", True, True
        if title_kind == "announced" or avail == "announced":
            return "announced", True, True
        if title_kind == "preorder" or avail == "preorder":
            return "preorder", True, True
        # Seite gelesen, aber kein positiver Direktverkauf mit echter Lieferbarkeit.
        return "unverified", True, True

    if status == "instock":
        kind = nonavailable_kind(title)
        if kind:
            return kind, True, True
    return status, True, True


def maybe_notify_pokemon30th(state, product_key, title, status, domain, link, price, now, cart_link=None, galaxus_offer=None):
    """Zusaetzliche Meldung(en) an die Pokemon-30th-Anniversary-Unterkanaele, unabhaengig vom
    normalen Pokemon-Kanal getrackt (eigene state.json-Keys), damit alle Kanaele unabhaengig
    ihre eigene Neu-Erkennung haben. Zwei Kanaele: nur-Englisch, und alle Sprachen.

    Nur der englische Kanal (pokemon30th) filtert Vorbestellungen/Ankuendigungen aus dem
    Stock-Webhook und verlangt bei Galaxus einen positiv belegten Direktverkauf plus echte
    Lieferbarkeit. Der All-Sprachen-Kanal und alle anderen Marken bleiben unveraendert."""
    if matches_pokemon_30th(title):
        key30 = product_key + ":pokemon30th"
        prev = state.get(key30)
        prev_status = prev.get("status") if prev else None
        effective, update_state, do_notify = resolve_pokemon30th_en_status(
            domain, status, title, galaxus_offer, prev_status
        )
        if update_state:
            state[key30] = {"title": title, "status": effective, "last_checked": now}
        else:
            log(f"  pokemon30th EN fail-closed: {title[:60]} bei {domain}")
        if update_state and do_notify:
            notify_status_change(
                state, key30, title, effective, prev_status, domain, link, price,
                POKEMON_30TH_EN_BRAND, now, cart_link=cart_link, galaxus_offer=galaxus_offer,
            )

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

# Englischer 30th-Stock-/Deal-Kanal (#pokemon-30th-en). Vorbestellungen dieser Marke
# duerfen nur an einen nachweislich anderen Discord-Kanal. Die Kanal-ID ist oeffentlich
# (kein Secret). Webhook-URLs werden dafuer nicht geloggt und nicht verglichen ausser
# als Identitaet "derselbe Webhook".
POKEMON_30TH_EN_BRAND = "pokemon30th"
POKEMON_30TH_EN_CHANNEL_ID = "1550133206831857785"
_WEBHOOK_URL_RE = re.compile(
    r"^https://(?:discord\.com|discordapp\.com)/api(?:/v\d+)?/webhooks/(\d+)/([^/?#\s]+)/?(?:\?[^#]*)?$",
    re.IGNORECASE,
)
_webhook_channel_cache = {}


def brand_webhooks(brand):
    """Webhook-Paar (Restock, Vorbestellung) zum Sendezeitpunkt aus config lesen.
    Nicht beim Import einfrieren, damit Tests und spaet gesetzte Werte dieselbe Quelle nutzen.
    Die Funktion vergleicht keine Secret-Werte und loggt sie nicht."""
    table = {
        "pokemon": (config.DISCORD_WEBHOOK_POKEMON, config.DISCORD_WEBHOOK_POKEMON_PREORDER),
        "onepiece": (config.DISCORD_WEBHOOK_ONEPIECE, config.DISCORD_WEBHOOK_ONEPIECE_PREORDER),
        "dragonball": (config.DISCORD_WEBHOOK_DRAGONBALL, config.DISCORD_WEBHOOK_DRAGONBALL_PREORDER),
        "mtg": (config.DISCORD_WEBHOOK_MTG, config.DISCORD_WEBHOOK_MTG_PREORDER),
        "yugioh": (config.DISCORD_WEBHOOK_YUGIOH, config.DISCORD_WEBHOOK_YUGIOH_PREORDER),
        "pokemon30th": (config.DISCORD_WEBHOOK_POKEMON_30TH, config.DISCORD_WEBHOOK_POKEMON_30TH_PREORDER),
        "pokemon30thall": (config.DISCORD_WEBHOOK_POKEMON_30TH_ALL, config.DISCORD_WEBHOOK_POKEMON_30TH_ALL_PREORDER),
    }
    return table[brand]


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
_SHOPWARE_AVAIL_RE = re.compile(r'<span class="at-avail-pill">([^<]+)</span>')


def fetch_shopware_products(domain):
    """Holt Produkte ueber die Shopware-Widget-Suche (/widgets/search?search=&p=), HTML-Parsing.
    Liefert Liste von dicts mit title/link/price/in_stock (kompatibel zu den anderen Fetch-Funktionen).
    order=erscheinungsdatum erzwingt Sortierung nach Neuzugaengen statt Relevanz - bei riesigen
    Katalogen wie amazingtoys.ch (89'590 Produkte) matcht die Volltextsuche fuer "pokemon" ueber
    49'000 Treffer (praktisch der ganze Katalog, kaputte/zu breite Relevanzsuche), sodass ein
    einzelnes neues Produkt in den Standardergebnissen (Sortierung "Beste Ergebnisse") auf
    Seite 50+ untergehen und mit dem 8-Seiten-Limit nie gesehen werden konnte (siehe Memory,
    30th Celebration Booster Bundle 2026-09-18). Mit Sortierung nach Neuzugang landet ein
    frisch gelistetes Produkt zuverlaessig auf Seite 1, unabhaengig von der Trefferzahl."""
    products = []
    seen_ids = set()
    for term in ["pokemon", "one piece"]:
        page = 1
        while page <= 8:  # Sicherheitslimit
            url = f"https://{domain}/widgets/search"
            try:
                r = requests.get(
                    url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT,
                    params={"search": term, "p": page, "order": "erscheinungsdatum"},
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
                # Vorbestellungs-Badge (class="at-avail at-avail--pre") wird separat vom
                # normalen "in den Warenkorb"-Status geprueft - Titel selbst enthaelt bei
                # amazingtoys.ch/twomoons.ch NIE ein Vorbestellungs-Wort (is_preorder() auf den
                # Titel allein greift hier also nie), das ist ausschliesslich am Badge-Pill-Text
                # erkennbar. "Bald verfuegbar"/"in Kuerze" = angekuendigt, Vorbestellung noch
                # nicht offen; "Vorbestellung" = jetzt vorbestellbar. Fehlte komplett -> ME06
                # Delta Reign Display wurde nie gemeldet (siehe Memory, 2026-09-18).
                avail_m = _SHOPWARE_AVAIL_RE.search(b)
                pill_text = avail_m.group(1).lower() if avail_m else ""
                if "vorbestellung" in pill_text or "vorbestellen" in pill_text:
                    preorder_hint = "open"
                elif "bald verf" in pill_text or "in k" in pill_text or "demn" in pill_text:
                    preorder_hint = "announced"
                else:
                    preorder_hint = None
                products.append({
                    "id": pid,
                    "title": info.get("name", ""),
                    "link": link,
                    "price": info.get("price"),
                    "in_stock": in_stock,
                    "preorder_hint": preorder_hint,
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


ALERT_COOLDOWN_SECONDS = 24 * 3600


def _alert_suppressed(state, product_key, status, now):
    """Pro Produkt und Status hoechstens eine Discord-Meldung innerhalb von 24h.
    Hintergrund: galaxus.ch pendelt bei manchen Produkten im Stundentakt zwischen
    "verfuegbar" und "in einigen Tagen verfuegbar" (instock <-> preorder), jeder Wechsel
    loeste eine neue Meldung aus -> User-Report "immer die gleichen Deals" (2026-10-08).
    Das Log liegt unter dem Sonderkey "_alert_log" in state.json (kein Produkt-Eintrag,
    load_state() ignoriert ihn, weil weder "status" noch "in_stock" vorhanden)."""
    log_map = state.setdefault("_alert_log", {})
    key = f"{product_key}|{status}"
    try:
        now_dt = datetime.fromisoformat(now)
    except Exception:
        return False
    last = log_map.get(key)
    if last:
        try:
            if (now_dt - datetime.fromisoformat(last)).total_seconds() < ALERT_COOLDOWN_SECONDS:
                log(f"  Meldung unterdrueckt (Cooldown 24h): {product_key} -> {status}")
                return True
        except Exception:
            pass
    log_map[key] = now
    if len(log_map) > 5000:
        cutoff = now_dt.timestamp() - 7 * 24 * 3600
        for k in [k for k, v in log_map.items() if datetime.fromisoformat(v).timestamp() < cutoff]:
            del log_map[k]
    return False


def clear_webhook_channel_cache():
    """Nur fuer Tests. Der Cache haelt ausschliesslich Kanal-IDs, keine URLs."""
    _webhook_channel_cache.clear()


def _webhook_identity(url):
    """(id, token) eines Incoming-Webhooks, oder None. Token wird nicht geloggt."""
    if not isinstance(url, str):
        return None
    match = _WEBHOOK_URL_RE.match(url.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def _same_webhook(left, right):
    """True, wenn beide Angaben denselben Webhook meinen (nicht nur denselben Kanal)."""
    left_id = _webhook_identity(left)
    right_id = _webhook_identity(right)
    if left_id and right_id:
        return left_id == right_id
    if isinstance(left, str) and isinstance(right, str):
        left_s, right_s = left.strip(), right.strip()
        return bool(left_s) and left_s == right_s
    return False


def _webhook_metadata_endpoint(url):
    """Dokumentierte Adresse GET /webhooks/{id}/{token}. Kein Redirect, keine Query."""
    identity = _webhook_identity(url)
    if not identity:
        return None
    webhook_id, token = identity
    return f"https://discord.com/api/webhooks/{webhook_id}/{token}"


def _snowflake(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value <= 0:
            return None
        return str(value)
    if isinstance(value, str) and value.isdigit() and value != "0":
        return value
    return None


def _fetch_webhook_channel_id(endpoint):
    """Kanal-ID ueber die dokumentierte Webhook-Metadaten-Antwort.
    Fehlschlag, Redirect oder fehlende channel_id -> None (Aufrufer unterdrueckt).
    Antwortkoerper und URL werden nicht geloggt (die Antwort enthaelt das Webhook-Token)."""
    try:
        response = requests.get(endpoint, timeout=config.REQUEST_TIMEOUT, allow_redirects=False)
    except Exception:
        return None
    try:
        if getattr(response, "status_code", None) != 200:
            return None
        data = response.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return _snowflake(data.get("channel_id"))


def webhook_channel_id(url):
    """Kanal-ID des uebergebenen Webhooks, oder None wenn sie nicht sicher lesbar ist."""
    endpoint = _webhook_metadata_endpoint(url)
    if not endpoint:
        return None
    key = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    cached = _webhook_channel_cache.get(key)
    if cached:
        return cached
    channel_id = _fetch_webhook_channel_id(endpoint)
    if channel_id:
        _webhook_channel_cache[key] = channel_id
    return channel_id


def pokemon30th_preorder_destination_allowed(preorder_url, stock_url):
    """True nur, wenn die Vorbestellung nachweislich nicht in #pokemon-30th-en landet.
    Gleicher Webhook wie der Stock-Kanal, geschuetzte Kanal-ID, oder nicht lesbares Ziel
    -> False. Ein anderer, per Metadaten bestaetigter Kanal -> True."""
    if _same_webhook(preorder_url, stock_url):
        return False
    channel_id = webhook_channel_id(preorder_url)
    if not channel_id or channel_id == POKEMON_30TH_EN_CHANNEL_ID:
        return False
    return True


def _pokemon30th_preorder_blocked(brand, preorder_webhook):
    """True = diese pokemon30th-Vorbestellung/Ankuendigung nicht senden.
    Andere Marken: immer False. Unterdrueckung verbraucht keinen Alert-Cooldown."""
    if brand != POKEMON_30TH_EN_BRAND:
        return False
    stock_webhook, _ = brand_webhooks(brand)
    if pokemon30th_preorder_destination_allowed(preorder_webhook, stock_webhook):
        return False
    log("  pokemon30th EN: Vorbestellung/Ankuendigung nicht gesendet (Ziel nicht als anderer Kanal verifiziert)")
    return True


def notify_status_change(state, product_key, title, status, prev_status, domain, link, price, brand, now, cart_link=None, galaxus_offer=None):
    """Gemeinsame Notify-Logik: meldet neue Vorbestellungen/Restocks an den passenden Discord-Kanal.
    brand: "pokemon"/"onepiece"/"dragonball" (siehe brand_webhooks()/BRAND_LABELS).
    cart_link (optional): direkter "in den Warenkorb legen"-Link (Shopify /cart/add), damit der Nutzer
    beim Restock nur noch auf Kaufen klicken muss statt selbst zu suchen (Nutzerwunsch 2026-09-17).
    galaxus_offer: nur fuer den englischen 30th-Stock-Kanal ausgewertet. Andere Marken ignorieren ihn."""
    label = BRAND_LABELS[brand]
    restock_webhook, preorder_webhook = brand_webhooks(brand)
    cart_line = f"\U0001F6D2 Direkt in den Warenkorb: {cart_link}\n" if cart_link else ""
    # Ankuendigung nur, wenn das Produkt neu ist oder vorher ausverkauft war - ein "Downgrade"
    # preorder/instock -> announced (z.B. nach Einfuehrung des wog.ch-Detail-Checks) ist keine News.
    if status == "announced" and prev_status in (None, "outofstock"):
        if _pokemon30th_preorder_blocked(brand, preorder_webhook):
            return
        if _alert_suppressed(state, product_key, status, now):
            return
        msg = (
            f"\U0001F514 ANKUENDIGUNG ({label}): **{title}**\n"
            f"Haendler: {domain}\n"
            f"Preis: CHF {price}\n"
            f"Link: {link}\n"
            f"Vorbestellung noch nicht offen - wird hier angekuendigt, sobald verfuegbar.\n"
            f"Zeit: {now}"
        )
        log(f"  ANKUENDIGUNG gefunden: {title} bei {domain}")
        send_discord(preorder_webhook, msg)
    elif status == "preorder" and prev_status != "preorder":
        if _pokemon30th_preorder_blocked(brand, preorder_webhook):
            return
        if _alert_suppressed(state, product_key, status, now):
            return
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
        # Einziger Pfad, der den Stock-Webhook trifft (Erstfund, Statuswechsel, erneuter
        # Aufruf aus dem Priority-Lauf). Preisaenderungen ohne Statuswechsel rufen diesen
        # Zweig nicht auf. Der englische 30th-Kanal bekommt hier weder Vorbestellung noch
        # unbewiesene Galaxus-Angebote, auch wenn der Aufrufer "instock" behauptet.
        if brand == POKEMON_30TH_EN_BRAND and not pokemon30th_stock_allowed(domain, title, galaxus_offer):
            log(f"  pokemon30th EN: kein RESTOCK (nicht positiv lieferbar oder kein Direktverkauf): {title[:60]}")
            return
        if _alert_suppressed(state, product_key, status, now):
            return
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


def _check_retailer_list(page, state, now, retailers):
    """Kernlogik fuer Browser-basierte Haendler, unabhaengig davon ob die Seite von normalem
    Playwright oder von camoufox (Stealth-Browser, siehe check_stealth_browser_retailers)
    kommt - beide liefern ein Playwright-kompatibles page-Objekt."""
    terms = [(t, "pokemon") for t in config.BROWSER_SEARCH_TERMS_POKEMON] + \
            [(t, "onepiece") for t in config.BROWSER_SEARCH_TERMS_ONEPIECE] + \
            [(t, "dragonball") for t in config.BROWSER_SEARCH_TERMS_DRAGONBALL] + \
            [(t, "mtg") for t in config.BROWSER_SEARCH_TERMS_MTG] + \
            [(t, "yugioh") for t in config.BROWSER_SEARCH_TERMS_YUGIOH]

    if True:
        for retailer in retailers:
            domain = retailer["name"]
            log(f"  Browser-Haendler: {domain} ...")
            # Treffer ueber alle Suchbegriffe pro href sammeln (dasselbe Produkt taucht bei
            # mehreren Begriffen auf) - Status/Notify erst danach, damit ein optionaler
            # Detailseiten-Check (detail_stock_selector) pro Produkt nur EINMAL laeuft.
            matched = {}
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
                    # Query-String abtrennen (z.B. "?imageIndex=1") - sonst zaehlt dasselbe
                    # Produkt als mehrere verschiedene product_keys, siehe Memory galaxus.ch.
                    # URL-Encoding normalisieren: wog.ch liefert dieselbe Produkt-URL mal mit
                    # "-" und mal mit "%2D" -> ohne unquote zwei Keys pro Produkt (doppelte Alerts).
                    href = urllib.parse.unquote(href.split("?")[0])
                    if href not in best_by_href or len(text) > len(best_by_href[href]):
                        best_by_href[href] = text

                for href, text in best_by_href.items():

                    # Echte Titelzeile finden: laengste der ersten paar Zeilen (Marke wie "Pokemon"
                    # allein oder "Empty"-Platzhalter werden so uebersprungen). Zeilen, die nur aus
                    # einer Sprachbeschreibung im Format "Englisch, Kategorie" bestehen (galaxus.ch-
                    # Format), werden dabei ausgeschlossen, da sie sonst faelschlich als "laengste
                    # Zeile" den echten Produktnamen ueberstimmen (siehe Memory).
                    lines = [ln.strip() for ln in text.split("\n") if ln.strip() and ln.strip().lower() != "empty"]
                    _lang_desc_re = re.compile(r"^(Englisch|Deutsch|Franz(ö|oe)sisch|Italienisch|Japanisch|English|German|French|Italian|Japanese)\s*,", re.IGNORECASE)
                    candidates = [ln for ln in lines[:5] if len(ln) >= 15 and not _lang_desc_re.match(ln)]
                    title_line = max(candidates, key=len) if candidates else (lines[0] if lines else "")
                    if not title_line or len(title_line) < 10:
                        continue

                    brand = detect_brand(text)
                    is_30th = matches_pokemon_30th(text) or matches_pokemon_30th_all(text)
                    if not brand and not is_30th:
                        continue

                    # Stock-Heuristik aus der Suchkachel: kein "nicht verfuegbar"/"ausverkauft"-Marker
                    t_norm = normalize(text)
                    out_markers = ["nicht verfugbar", "ausverkauft", "zurzeit nicht", "not available", "vergriffen", "nicht auf lager", "nicht mehr lieferbar", "nicht mehr bestellbar"]
                    in_stock = not any(m in t_norm for m in out_markers)
                    preorder = is_preorder(text)
                    status = "preorder" if preorder else ("instock" if in_stock else "outofstock")

                    if href not in matched:
                        matched[href] = {"title": title_line, "brand": brand, "status": status, "preorder": preorder}

            # Optionaler Detailseiten-Check: bei wog.ch tragen die Suchkacheln KEINEN Lagerstatus
            # (nur Titel/Preis/"Release: <Datum>"), d.h. eine offene, eine ausverkaufte und eine
            # geschlossene Vorbestellung sehen in der Kachel identisch aus -> der Bot sah die
            # 30th-Celebration-Premium-Collections seit 17.09. dauerhaft als "preorder" und hatte
            # nie einen Wechsel zu melden (User-Report 2026-10-08). Der echte Status steht nur im
            # Kaufbereich der Produktseite (wog: div.atc, Klassen atc__stock--stock-0..4).
            detail_sel = retailer.get("detail_stock_selector")
            if detail_sel and matched:
                # Nur dort nachschauen, wo die Kachel nichts aussagt UND ein Wechsel relevant ist:
                # Vorbestellungen (Release-Marker) sowie Produkte, die zuletzt als Vorbestellung,
                # angekuendigt oder ausverkauft galten (Wiederverfuegbarkeit). Normale Lagerware
                # bleibt bei der Kachel-Heuristik - alle 220 Treffer zu laden kostete ~6 Min
                # und brachte den Lauf auf 24 Min (Cloud-Lauf 2026-10-08 11:20 UTC).
                to_check = {
                    h: m for h, m in matched.items()
                    if m["preorder"] or (state.get(f"{domain}:{h}") or {}).get("status") in ("preorder", "announced", "outofstock")
                }
                log(f"  {domain}: Detail-Check fuer {len(to_check)} von {len(matched)} Produkten ...")
                for href, m in to_check.items():
                    detail_status = _detail_stock_status(page, href, detail_sel, m["preorder"])
                    if detail_status:
                        m["status"] = detail_status

            for href, m in matched.items():
                title_line, brand, status = m["title"], m["brand"], m["status"]
                product_key = f"{domain}:{href}"
                prev = state.get(product_key)
                prev_status = prev.get("status") if prev else None

                state[product_key] = {
                    "title": title_line,
                    "status": status,
                    "last_checked": now,
                }

                if brand:
                    notify_status_change(state, product_key, title_line, status, prev_status, domain, href, "?", brand, now)
                maybe_notify_pokemon30th(state, product_key, title_line, status, domain, href, "?", now)


def _detail_stock_status(page, url, selector, is_preorder_item):
    """Liest den echten Lagerstatus aus dem Kaufbereich einer Produktseite (siehe
    detail_stock_selector in config.BROWSER_RETAILERS). Rueckgabe: "instock"/"preorder"/
    "announced"/"outofstock" oder None (Seite/Element nicht lesbar -> Kachel-Status behalten).
    wog.ch-Texte: "In den Warenkorb ... verfuegbar/bestellbar" = bestellbar; "noch nicht
    erschienen" ohne Warenkorb-Button = angekuendigt; "nicht mehr bestellbar" /
    "derzeit ausverkauft" / "nicht bestellbar/lieferbar" = ausverkauft."""
    try:
        page.goto(url, timeout=15000, wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        txt = page.eval_on_selector(selector, "e => e.innerText") or ""
    except Exception:
        return None
    t = normalize(txt)
    if "in den warenkorb" in t:
        return "preorder" if is_preorder_item else "instock"
    if "noch nicht erschienen" in t:
        return "announced"
    if any(mk in t for mk in ("nicht mehr bestellbar", "nicht bestellbar", "ausverkauft", "nicht verfugbar", "nicht lieferbar")):
        return "outofstock"
    return None


def check_browser_retailers(state, now):
    """Prueft grosse Haendler ohne /products.json per echtem Headless-Browser (Playwright)."""
    if sync_playwright is None:
        log("  Playwright nicht installiert, Browser-Haendler uebersprungen")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-http2"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        page = ctx.new_page()
        _check_retailer_list(page, state, now, config.BROWSER_RETAILERS)
        browser.close()


def fetch_stealth_woocommerce_products(page, domain):
    """Wie fetch_woocommerce_products(), aber ueber eine bereits geoeffnete camoufox-Seite,
    fuer Haendler mit JS-Challenge-Bot-Check vor dem WooCommerce Store API (z.B. cardcollectors.ch,
    401 auf jeden requests.get, auch auf /wp-json/ selbst - die Challenge setzt ein Cookie,
    das camoufox nach einem Erstbesuch der Startseite mitbringt)."""
    products = []
    seen_ids = set()
    for term in ["pokemon", "one piece", "dragon ball", "magic", "yu-gi-oh"]:
        url = f"https://{domain}/wp-json/wc/store/v1/products?search={term.replace(' ', '%20')}&per_page=100"
        try:
            resp = page.goto(url, timeout=20000)
        except Exception as e:
            log(f"  {domain}: Fehler beim Abruf ({term}), uebersprungen ({type(e).__name__})")
            continue
        if resp is None or resp.status != 200:
            log(f"  {domain}: HTTP {resp.status if resp else '?'} bei Suche '{term}', uebersprungen")
            continue
        try:
            batch = json.loads(page.evaluate("document.body.innerText"))
        except Exception:
            log(f"  {domain}: Antwort kein JSON, uebersprungen")
            continue
        for p in batch:
            if p.get("id") not in seen_ids:
                seen_ids.add(p.get("id"))
                products.append(p)
    return products


def check_stealth_woocommerce_retailers(page, state, now):
    """Verarbeitet config.STEALTH_WOOCOMMERCE_RETAILERS ueber eine bereits offene camoufox-Seite
    (dieselbe Browser-Instanz wie check_stealth_browser_retailers, spart einen zweiten Start)."""
    for domain in config.STEALTH_WOOCOMMERCE_RETAILERS:
        log(f"Pruefe {domain} (Stealth-WooCommerce, camoufox) ...")
        products = fetch_stealth_woocommerce_products(page, domain)
        if not products:
            continue

        for p in products:
            title = html.unescape(p.get("name", ""))
            product_key = f"{domain}:{p.get('id')}"
            brand = detect_brand(title)
            is_30th = matches_pokemon_30th(title) or matches_pokemon_30th_all(title)
            if not brand and not is_30th:
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
            if brand:
                notify_status_change(state, product_key, title, status, prev_status, domain, link, price, brand, now)
            maybe_notify_pokemon30th(state, product_key, title, status, domain, link, price, now)


def fetch_stealth_category_products(page, category_url, product_link_selector, max_clicks=8):
    """Blaettert eine Kategorieseite durch Klicken auf 'Mehr anzeigen' durch (NICHT per Scroll -
    Scroll allein laedt bei galaxus.ch nichts nach, siehe config.STEALTH_CATEGORY_RETAILERS).
    Liefert Liste von dicts mit title/link/price/in_stock/preorder, title kommt aus dem
    aria-label des Produktlinks (sauberer als der verschachtelte Karten-Text)."""
    try:
        page.goto(category_url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
    except Exception as e:
        log(f"  Kategorie {category_url}: Laden fehlgeschlagen ({type(e).__name__})")
        return []

    for _ in range(max_clicks):
        try:
            page.keyboard.press("End")
            page.wait_for_timeout(700)
            page.mouse.wheel(0, -1200)
            page.wait_for_timeout(500)
            clicked = page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll('button, a, div[role=button]'));
                const t = els.find(e => e.textContent.trim() === 'Mehr anzeigen');
                if (t) { t.scrollIntoView(); t.click(); return true; }
                return false;
            }""")
        except Exception:
            break
        if not clicked:
            break
        page.wait_for_timeout(1400)

    try:
        cards = page.eval_on_selector_all(
            product_link_selector,
            """els => {
                const seen = new Set();
                const out = [];
                for (const el of els) {
                    const href = el.href.split('?')[0];
                    if (seen.has(href)) continue;
                    const label = el.getAttribute('aria-label');
                    if (!label) continue;
                    seen.add(href);
                    const article = el.closest('article');
                    const priceEl = article ? article.querySelector('[class*=yRGTUHk]') : null;
                    const iconEl = article ? article.querySelector('svg[aria-label]') : null;
                    out.push({
                        href, title: label,
                        price: priceEl ? priceEl.textContent.trim() : null,
                        avail: iconEl ? iconEl.getAttribute('aria-label') : null,
                    });
                }
                return out;
            }"""
        )
    except Exception as e:
        log(f"  Kategorie {category_url}: Auslesen fehlgeschlagen ({type(e).__name__})")
        return []

    products = []
    for c in cards:
        avail = (c.get("avail") or "").lower()
        in_stock = avail == "verfuegbar" or avail == "verfügbar"
        preorder = ("verfügbar" in avail or "verfuegbar" in avail) and not in_stock
        products.append({
            "title": c["title"],
            "link": c["href"],
            "price": (c.get("price") or "?").replace("CHF", "").strip(),
            "in_stock": in_stock,
            "preorder": preorder,
        })
    return products


def check_stealth_category_retailers(page, state, now):
    """Verarbeitet config.STEALTH_CATEGORY_RETAILERS (Kategorie-Paging statt kaputter
    Volltextsuche, siehe fetch_stealth_category_products())."""
    for entry in config.STEALTH_CATEGORY_RETAILERS:
        domain = entry["name"]
        log(f"  Kategorie-Haendler: {domain} ({entry['category_url'].rsplit('/', 1)[-1]}) ...")
        products = fetch_stealth_category_products(
            page, entry["category_url"], entry["product_link_selector"], entry.get("max_clicks", 8)
        )
        if not products:
            continue

        for p in products:
            title = html.unescape(p["title"])
            # Stabile numerische Produkt-ID statt des ganzen Slugs verwenden: galaxus.ch haengt
            # je nach Quellseite (Suche vs. Kategorie) ein anderes Kategorie-Wort an dieselbe
            # Produkt-ID an (z.B. "...-box-set-collection-72348007" vs.
            # "...-elite-top-trainer-box-72348007") - bei vollem Slug als Key zaehlte das als
            # zwei verschiedene Produkte und erzeugte doppelte Meldungen (siehe Memory, 2026-10-02).
            id_match = re.search(r"-(\d+)$", p["link"])
            stable_id = id_match.group(1) if id_match else p["link"]
            product_key = f"{domain}:{stable_id}"
            brand = detect_brand(title)
            is_30th = matches_pokemon_30th(title) or matches_pokemon_30th_all(title)
            if not brand and not is_30th:
                continue

            status = "preorder" if p["preorder"] else ("instock" if p["in_stock"] else "outofstock")

            prev = state.get(product_key)
            prev_status = prev.get("status") if prev else None

            state[product_key] = {
                "title": title,
                "status": status,
                "last_checked": now,
            }

            # Galaxus-Marketplace: Angebote von Drittanbietern (ueberteuert, Nutzerwunsch
            # 2026-10-18) nicht melden, nur Ware die Galaxus selbst verkauft.
            # Unveraendert fail-open und nur auf die exakte Phrase - gilt fuer alle Marken.
            # Der englische 30th-Kanal hat zusaetzlich load_galaxus_offer() (fail-closed).
            if domain == "galaxus.ch" and status != "outofstock" and prev_status != status:
                if galaxus_is_third_party(page, p["link"]):
                    log(f"  galaxus.ch Drittanbieter uebersprungen: {title[:60]}")
                    continue

            galaxus_offer = None
            if domain == "galaxus.ch" and status == "instock" and matches_pokemon_30th(title):
                key30_prev = (state.get(product_key + ":pokemon30th") or {}).get("status")
                if key30_prev not in ("instock", "marketplace", "unverified"):
                    galaxus_offer = load_galaxus_offer(page, p["link"])

            if brand:
                notify_status_change(state, product_key, title, status, prev_status, domain, p["link"], p["price"], brand, now)
            maybe_notify_pokemon30th(
                state, product_key, title, status, domain, p["link"], p["price"], now,
                galaxus_offer=galaxus_offer,
            )


def galaxus_is_third_party(page, link):
    """True, wenn die Galaxus-Produktseite den Bestand 'beim Drittanbieter' ausweist
    (Marketplace-Verkaeufer). Galaxus-eigene Ware hat nur 'N Stueck an Lager' ohne diesen
    Zusatz. Bei Ladefehler False (lieber einmal zu viel melden als eine echte Chance verpassen)."""
    try:
        resp = page.request.get(link, timeout=20000)
        return "beim Drittanbieter" in resp.text()
    except Exception:
        try:
            page.goto(link, timeout=25000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            return "beim Drittanbieter" in page.content()
        except Exception as e:
            log(f"  galaxus Drittanbieter-Check fehlgeschlagen ({type(e).__name__})")
            return False


def check_stealth_browser_retailers(state, now):
    """Prueft Haendler mit starker Fingerprint-basierter Bot-Erkennung (digitec/brack/mueller-
    Familie), die normales Playwright/patchright sofort auf TLS-/HTTP2-Ebene blocken
    (net::ERR_HTTP2_PROTOCOL_ERROR direkt beim goto(), noch vor jeglicher Seiten-JS - siehe
    Memory). camoufox (Firefox-Basis, GitHub github.com/daijro/camoufox) hat einen komplett
    anderen Netzwerk-Fingerabdruck und kommt durch, wo Chromium-basierte Tools blockiert werden.
    Separate Funktion/Browser-Instanz, da camoufox deutlich schwerer/langsamer ist als normales
    Playwright und die bereits funktionierenden Haendler nicht davon betroffen sein sollen.
    Deckt ausserdem WooCommerce-Haendler mit JS-Challenge ab (STEALTH_WOOCOMMERCE_RETAILERS,
    z.B. cardcollectors.ch), die dieselbe camoufox-Instanz mitbenutzen."""
    if Camoufox is None:
        log("  camoufox nicht installiert, Stealth-Browser-Haendler uebersprungen")
        return
    if not config.STEALTH_BROWSER_RETAILERS and not config.STEALTH_WOOCOMMERCE_RETAILERS and not config.STEALTH_CATEGORY_RETAILERS:
        return

    try:
        camoufox_ctx = Camoufox(headless=True)
        browser = camoufox_ctx.__enter__()
    except Exception as e:
        # Darf den kompletten Lauf NICHT abreissen - sonst gehen alle bereits gesammelten
        # Treffer der vorherigen Haendler (Shopify/WooCommerce/Shopware/Softridge/Spielezar)
        # verloren, weil save_state() dann nie erreicht wird (siehe Memory,
        # CamoufoxNotInstalled durch GitHub-API-Rate-Limit 2026-09-21).
        log(f"  camoufox konnte nicht gestartet werden, Stealth-Haendler uebersprungen ({type(e).__name__}: {e})")
        return

    try:
        page = browser.new_page()
        if config.STEALTH_WOOCOMMERCE_RETAILERS:
            for domain in config.STEALTH_WOOCOMMERCE_RETAILERS:
                try:
                    page.goto(f"https://{domain}/", timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_timeout(6000)
                except Exception as e:
                    log(f"  {domain}: Startseite/Challenge fehlgeschlagen ({type(e).__name__})")
            check_stealth_woocommerce_retailers(page, state, now)
        if config.STEALTH_CATEGORY_RETAILERS:
            check_stealth_category_retailers(page, state, now)
        if config.STEALTH_BROWSER_RETAILERS:
            _check_retailer_list(page, state, now, config.STEALTH_BROWSER_RETAILERS)
    except Exception as e:
        log(f"  Fehler bei Stealth-Haendlern, Rest wird uebersprungen ({type(e).__name__}: {e})")
    finally:
        try:
            camoufox_ctx.__exit__(None, None, None)
        except Exception:
            pass


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
            # Sprach-/Franchise-Tags (z.B. "JP") stehen bei manchen Haendlern (pokelight.ch) NUR
            # im tags-Feld, nicht im Titel selbst - Erkennung deshalb auf Titel+Tags+Typ pruefen,
            # nicht nur auf den Titel (sonst rutschen z.B. japanische Produkte als "englisch" durch)
            tags = p.get("tags", "")
            tags_text_part = " ".join(tags) if isinstance(tags, list) else str(tags)
            tags_text = f"{title} {tags_text_part} {p.get('product_type', '')}"
            brand = detect_brand(tags_text)
            # Pokemon-30th-Unterkanaele NICHT hinter der allgemeinen Marken-Erkennung gaten:
            # Titel wie "First Partners Illustration Collection" enthalten keines der normalen
            # POKEMON_KEYWORDS (kein "Booster"/"Collection Box"/etc.), wuerden also von
            # detect_brand() als None eingestuft und hier uebersprungen, bevor
            # maybe_notify_pokemon30th() ueberhaupt drankommt - Bug gefunden 2026-09-17
            # (Kanal blieb leer trotz existierender 30th-Anniversary-Treffer).
            is_30th = matches_pokemon_30th(tags_text) or matches_pokemon_30th_all(tags_text)
            if not brand and not is_30th:
                continue

            in_stock, variant = product_in_stock(p)
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
            if brand:
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
            is_30th = matches_pokemon_30th(title) or matches_pokemon_30th_all(title)
            if not brand and not is_30th:
                continue

            # Regionsfeld der API zusaetzlich pruefen (zuverlässiger als Titel-Parsing, siehe
            # fetch_softridge_products): softridge nutzt Ein-Buchstaben-Suffixe wie "-D-" statt
            # "(DE)", die der generische Sprachfilter in matches_*() nicht zuverlässig erkennt.
            if brand:
                allowed_regions = {"EN", "DE"} if brand == "mtg" else {"EN"}
                region = p.get("region")
                if region and region not in allowed_regions:
                    brand = None
                    if not is_30th:
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
            if brand:
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
            is_30th = matches_pokemon_30th(title) or matches_pokemon_30th_all(title)
            if not brand and not is_30th:
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

            if brand:
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
            is_30th = matches_pokemon_30th(title) or matches_pokemon_30th_all(title)
            if not brand and not is_30th:
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
            if brand:
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
            is_30th = matches_pokemon_30th(title) or matches_pokemon_30th_all(title)
            if not brand and not is_30th:
                continue

            preorder_hint = p.get("preorder_hint")
            if preorder_hint == "open" or is_preorder(title):
                status = "preorder"
            elif preorder_hint == "announced":
                status = "announced"
            else:
                status = "instock" if p["in_stock"] else "outofstock"

            prev = state.get(product_key)
            prev_status = prev.get("status") if prev else None

            state[product_key] = {
                "title": title,
                "status": status,
                "last_checked": now,
            }

            price = p.get("price")
            price_str = f"{price:.2f}" if isinstance(price, (int, float)) else "?"
            if brand:
                notify_status_change(state, product_key, title, status, prev_status, domain, p["link"], price_str, brand, now)
            maybe_notify_pokemon30th(state, product_key, title, status, domain, p["link"], price_str, now)

    log("Pruefe grosse Haendler (Browser) ...")
    check_browser_retailers(state, now)

    log("Pruefe Stealth-Haendler (camoufox) ...")
    check_stealth_browser_retailers(state, now)

    save_state(state)
    log("=== Restock-Check beendet ===")


RUNNING_IN_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def git_sync_pull():
    """Lokale Laeufe holen vor jedem Check den neuesten state.json-Stand aus dem Repo, damit
    lokaler Task und GitHub-Actions-Cloud-Job (die inzwischen BEIDE gelegentlich laufen, siehe
    Memory Duplikat-Bug 2026-09-17) sich einen gemeinsamen Stand teilen statt unabhaengig
    voneinander abzudriften. GitHub Actions selbst macht das schon per Workflow-Step, hier nur
    fuer lokale Laeufe noetig. Rein best-effort - schlaegt der Pull fehl, laeuft der Check
    trotzdem mit dem lokalen Stand weiter."""
    if RUNNING_IN_GITHUB_ACTIONS:
        return
    try:
        subprocess.run(
            ["git", "checkout", "--", "state.json"],
            cwd=BASE_DIR, capture_output=True, timeout=15,
        )
        r = subprocess.run(
            ["git", "pull", "--rebase", "origin", "master"],
            cwd=BASE_DIR, capture_output=True, timeout=30, text=True,
        )
        if r.returncode != 0:
            log(f"  git pull fehlgeschlagen (state.json bleibt lokal): {r.stderr[:200]}")
    except Exception as e:
        log(f"  git pull uebersprungen: {type(e).__name__}")


def git_sync_push():
    """Committet/pusht state.json nach einem lokalen Lauf zurueck, damit der naechste Cloud-
    oder lokale Lauf den aktuellen Stand sieht. Rein best-effort, siehe git_sync_pull()."""
    if RUNNING_IN_GITHUB_ACTIONS:
        return
    try:
        subprocess.run(["git", "add", "state.json"], cwd=BASE_DIR, capture_output=True, timeout=15)
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR, capture_output=True, timeout=15,
        )
        if diff.returncode == 0:
            return  # keine Aenderung
        subprocess.run(
            ["git", "commit", "-m", "Update state (local run) [skip ci]"],
            cwd=BASE_DIR, capture_output=True, timeout=15,
        )
        r = subprocess.run(
            ["git", "push"], cwd=BASE_DIR, capture_output=True, timeout=30, text=True,
        )
        if r.returncode != 0:
            log(f"  git push fehlgeschlagen: {r.stderr[:200]}")
    except Exception as e:
        log(f"  git push uebersprungen: {type(e).__name__}")


if __name__ == "__main__":
    if not acquire_lock():
        log("Ein anderer Lauf ist bereits aktiv (Lock vorhanden) - dieser Lauf wird uebersprungen.")
        sys.exit(0)
    try:
        git_sync_pull()
        run()
        git_sync_push()
    except Exception:
        log("FEHLER:\n" + traceback.format_exc())
        sys.exit(1)
    finally:
        release_lock()
