# -*- coding: utf-8 -*-
"""
CHAU Einzelkarten-Deal-Finder (30th Celebration Chase-Karten)
================================================================
Nutzerwunsch 2026-09-27: fuer die 60 "Chase"-Karten aus singles_ranking.py
(Special Illustration Rare/Illustration Rare/Futuristic Rare/Pikachu Rare) nicht
nur ein Ranking zeigen, sondern konkrete Kauf-Links liefern - Angebote unter dem
Marktwert UND bald endende, guenstige Auktionen, unabhaengig vom eigenen PC.

Durchsucht eBay.ch (Sofort-Kaufen + Auktionen) und Ricardo.ch pro Chase-Karte,
berechnet je Karte einen Referenzpreis (Median aus aktuellen Angeboten + eigener
Preishistorie ueber die Zeit, gleiches Prinzip wie booster_deal_finder.py) und
meldet zwei Kategorien in Discord:
  - "Deal": Preis inkl. Versand <= DEAL_RATIO des Medians
  - "Auktion endet bald": Auktion endet innerhalb AUCTION_SOON_HOURS Stunden UND
    aktuelles Gebot <= Median (kein garantierter Deal, Preis kann bis Ende noch
    steigen - deshalb separat gekennzeichnet, nicht als "Deal" verkauft)

Technik identisch zu booster_deal_finder.py / vintage_deal_finder.py: eBay UND
Ricardo blocken echten Headless-Chrome, nur "headed" Chromium (lokal ausserhalb
des Bildschirms, in der Cloud unter xvfb-run) kommt durch. Ricardo braucht pro
Suche einen FRISCHEN Browser-Context (siehe fetch_ricardo()-Docstring).

Aufruf:
    python singles_deal_finder.py              -> normaler Lauf, postet neue Deals
    python singles_deal_finder.py --dry-run    -> nur Log/Ausgabe, kein Discord, kein State
    python singles_deal_finder.py --reset      -> vergisst bereits gemeldete Angebote
"""
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "singles_cache.json")
STATE_FILE = os.path.join(HERE, "singles_deal_state.json")
LOG_FILE = os.path.join(HERE, "singles_deal_finder.log")
LOCK_FILE = os.path.join(HERE, "singles_deal_finder.lock")

DRY_RUN = "--dry-run" in sys.argv
RESET = "--reset" in sys.argv

CHASE_RARITIES = {"special illustration rare", "illustration rare", "futuristic rare", "pikachu rare"}
# Classic Collection (Set-Praefix "me55c") traegt klassische Rarity-Namen ("Rare
# Holo"/"Rare" statt "Special Illustration Rare" etc.) und wuerde ueber
# CHASE_RARITIES allein nie erfasst - Nutzerwunsch 2026-09-30, siehe
# singles_ranking.py::is_chase() (identische Logik, hier dupliziert weil
# eigenstaendiges Skript ohne Import voneinander).
CHASE_SET_PREFIXES = {"me55c"}


def is_chase(card_id, entry):
    if card_id.split("-")[0] in CHASE_SET_PREFIXES:
        return True
    return (entry.get("rarity") or "").lower() in CHASE_RARITIES

MIN_TOTAL = 1.0
MAX_TOTAL = 1200.0       # Mew ex SIR liegt schon jetzt bei > CHF 170, Puffer nach oben
MIN_MEDIAN = 8.0         # Filtert die guenstigsten Pikachu-Rare-Karten raus (Median oft nur
                         # CHF 2-4) - dort markiert DEAL_RATIO fast jedes Angebot faelschlich
                         # als "Deal" (Bug-Fund erster Testlauf: 61 Treffer aus 5 Karten, fast
                         # alle CHF 1-2 Pikachu-Rare-Rauschen statt echter Investment-Deals).
DEAL_RATIO = 0.75
# Sicherheitsuntergrenze: Rabatte >85% (ratio < 0.15) sind bei einem seit Tagen
# beobachteten, mehrfach bestaetigten Median so unwahrscheinlich, dass sie mit
# hoher Wahrscheinlichkeit ein Scraping-Fehler sind (falscher Preis/Versand
# geparst, falsches Produkt trotz Textfilter) statt ein echtes Schnaeppchen -
# Dritter Voll-Testlauf zeigte mehrere Ricardo-Treffer bei 5-11% des Medians,
# die sich nicht verifizieren liessen (Ricardo blockt automatisierte Pruefung
# per Cloudflare-Captcha, siehe Memory feedback_cardmarket_nur_brave). Lieber
# etwas zu vorsichtig filtern als dem Nutzer einen falschen Preis melden.
MIN_RATIO_SANITY = 0.15
AUCTION_SOON_HOURS = 24
MIN_SAMPLES = 3
MAX_POSTS_PER_RUN = 20
PAGE_WAIT_MS = 2500

# Bug-Fund zweiter Voll-Testlauf: mehrere "Deals" bei CHF 6.33 fuer verschiedene
# Karten (Mew ex/Mewtwo ex/Sylveon ex) waren in Wahrheit KEINE Karten, sondern
# Merchandise ("Novelty Keychain") mit dem Kartennamen im Titel - die Karten-
# suche matcht Titel-Text, nicht das tatsaechliche Produkt. Alle Titel mit
# diesen Merch-/Fake-Markern werden deshalb schon beim Einlesen verworfen,
# bevor sie ueberhaupt in den Median-Pool oder die Deal-Pruefung gelangen.
TITLE_EXCLUDE = re.compile(
    r"keychain|schl[uü]sselanh[aä]nger|plush|pluesch|plüsch|funko|figur(e|ine)?\b|"
    r"\bpin\b|anstecknadel|magnet|poster|sticker|aufkleber|charm\b|badge|"
    r"acrylic|standee|\bcase\b|"  # generisch "Case": zweiter Voll-Testlauf fand einen
    # "Extended Artwork Case" (leere Schutzhuelle/Display-Case eines Zubehoer-Sellers,
    # nicht die Karte selbst) faelschlich als 93%-Deal - Kartentitel enthalten "Case"
    # so gut wie nie fuer die Karte selbst, nur fuer Huellen/Vitrinen dazu.
    r"proxy|custom|fake|replica|orica|repro\b|"
    r"\blot\b|bundle|sammlung|\d{2,}\s*x\b|\d{2,}\s*stk|\d{2,}\s*pcs|"
    r"code\b|codes\b|tcg\s*live|digital|instant\s*delivery|"
    r"sleeve\b|binder|playmat|toploader|deck\s*box|"
    r"\b(psa|bgs|cgc|ace)\s*-?\s*(10|9\.5|9|8\.5|8|7\.5|7|6\.5|6|5\.5|5)\b|graded|authenticated|slab",
    re.I,
)
RICARDO_SHIP_ESTIMATE = 5.0  # CHF - Ricardo-Kartenansicht zeigt keine Versandkosten, grobe Schaetzung
HISTORY_MAX_DAYS = 60

# Nutzerwunsch 2026-10-02: nur Englisch oder Japanisch vergleichen - andere
# Sprachversionen handeln auf eBay/Ricardo oft zu abweichenden Preisen und
# wuerden den Median verzerren. Titel OHNE Sprachmarker gelten als Englisch
# (Standardverhalten, Verkaeufer labeln praktisch nie explizit "(EN)").
LANGUAGE_EXCLUDE = re.compile(
    r"\bgerman\b|\bdeutsch\b|allemand|\bfrench\b|fran[çc]ais|\bitalian\b|italiano|"
    r"\bkorean\b|koreanisch|\bchinese\b|chinesisch|simplified|traditional\s*chinese|"
    r"\bspanish\b|espa[ñn]ol|\bportuguese\b|portugiesisch|"
    r"\(DE\)|\(FR\)|\(IT\)|\(KR\)|\(CN\)|\(ES\)|\(PT\)",
    re.I,
)
LANGUAGE_NEGATIVE_QUERY = (
    " -german -deutsch -french -français -italian -italiano "
    "-korean -chinese -spanish -español -portuguese"
)

# Nur fuer schnelle Testlaeufe: SINGLES_TEST_LIMIT=5 begrenzt auf die ersten N Chase-Karten.
_test_limit = os.environ.get("SINGLES_TEST_LIMIT")


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_chase_cards():
    """Liest die 60 Chase-Karten (Name+Nummer+Bild+Rarity) aus dem von
    singles_ranking.py gepflegten Cache - keine doppelte Datenpflege noetig."""
    cache = json.load(open(CACHE_FILE, encoding="utf-8"))
    cards = []
    for cid, e in cache.items():
        if is_chase(cid, e) and e.get("name"):
            cards.append((cid, e))
    if _test_limit:
        cards = cards[: int(_test_limit)]
    return cards


def parse_chf(s):
    if not s:
        return None
    m = re.search(r"([\d'.]+),(\d{2})\b", s)
    if m:
        return float(m.group(1).replace("'", "").replace(".", "") + "." + m.group(2))
    m2 = re.search(r"([\d'.]+)\.(\d{2})\b", s)
    if m2:
        return float(m2.group(1).replace("'", "").replace(",", ""))
    m3 = re.search(r"([\d'.]+)", s)
    return float(m3.group(1).replace("'", "").replace(",", "")) if m3 else None


def parse_shipping_ebay(s, full=""):
    """Bug-Fund dritter Voll-Testlauf: eBay hat auf einen Teil der Suchergebnisse
    ein neues Markup umgestellt (Klasse 'su-styled-text secondary large' statt
    '.s-item__shipping' etc.) - der dedizierte Selektor traf dort ins Leere,
    Versand wurde stillschweigend als CHF 0 gewertet (CHF 11.70 Versand
    verschwand komplett, echte Karte wirkte faelschlich wie ein 93%-Deal).
    Fallback: wenn der dedizierte Selektor nichts liefert, den kompletten
    sichtbaren Kartentext nach einer 'X Versand'-Zeile durchsuchen (robuster
    gegen Markup-Wechsel als eine einzelne CSS-Klasse)."""
    if re.search(r"kostenlos|gratis|free", s, re.I):
        return 0.0
    v = parse_chf(s) if s else None
    if v is not None:
        return v
    m = re.search(r"([\d'.,]+)\s*Versand", full)
    if m:
        return parse_chf("CHF " + m.group(1)) or 0.0
    if re.search(r"kostenlos|gratis|free", full, re.I):
        return 0.0
    return 0.0


def parse_hours_left(s):
    """eBay.ch zeigt Restzeit als z.B. '2T 4Std', '5Std 12Min', '38Min' - in
    Stunden umrechnen. None wenn kein Auktions-Zeitfeld vorhanden (=Sofort-Kaufen)."""
    if not s:
        return None
    d = re.search(r"(\d+)\s*T\b", s)
    h = re.search(r"(\d+)\s*Std", s)
    m = re.search(r"(\d+)\s*Min", s)
    if not (d or h or m):
        return None
    total_h = (int(d.group(1)) * 24 if d else 0) + (int(h.group(1)) if h else 0) + (int(m.group(1)) / 60 if m else 0)
    return total_h


EBAY_EXTRACT_JS = r"""() => [...document.querySelectorAll('li.s-item, li.s-card')].map(li => {
  const a = li.querySelector('a[href*="/itm/"]');
  const id = a ? (a.href.match(/itm[/](\d+)/) || [])[1] : '';
  const t = (li.querySelector('.s-item__title, .s-card__title') || {}).textContent || '';
  const p = (li.querySelector('.s-item__price, .s-card__price') || {}).textContent || '';
  const s = (li.querySelector('.s-item__shipping, .s-item__logisticsCost, .s-card__shipping, [class*="shipping"], [class*="logistics"]') || {}).textContent || '';
  const bidsEl = li.querySelector('.s-item__bids, .s-item__bidCount, [class*="bid"]');
  const bids = bidsEl ? bidsEl.textContent : '';
  const timeEl = li.querySelector('.s-item__time-left, [class*="time-left"], [class*="timeLeft"]');
  const timeLeft = timeEl ? timeEl.textContent : '';
  const img = li.querySelector('img');
  const full = (li.innerText || '').replace(/\s+/g, ' ').trim();
  return {id, t: t.replace('Wird in neuem Fenster oder Tab geöffnet', '').trim(), p: p.trim(), s: s.trim(),
          bids: bids.trim(), timeLeft: timeLeft.trim(), full,
          img: img ? (img.src || img.getAttribute('data-src') || '') : ''};
}).filter(x => x.id && x.id !== '123456')"""

RICARDO_EXTRACT_JS = r"""() => [...document.querySelectorAll("a[href*='/a/']")].map(a => {
  const lines = (a.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
  const img = a.querySelector('img');
  return {href: a.href, lines, img: img ? (img.src || img.getAttribute('data-src') || '') : ''};
}).filter(x => x.lines.length)"""


def fetch_ebay(page, cards):
    """Pro Chase-Karte EINE Suche, sortiert nach Preis aufsteigend (_sop=15) -
    surft damit gleichzeitig die guenstigsten Sofort-Kaufen-Angebote UND
    Auktionen mit niedrigstem aktuellen Gebot nach oben (ein Durchlauf statt
    zwei getrennter Suchen je Karte, haelt die Laufzeit bei 60 Karten im
    Rahmen). KEIN LH_BIN-Filter - Auktionen sollen bewusst mit auftauchen."""
    out = {}
    for cid, entry in cards:
        name = entry.get("name") or cid
        number = entry.get("number") or ""
        q = f'pokemon "{name}" 30th celebration {number}' + LANGUAGE_NEGATIVE_QUERY
        url = ("https://www.ebay.ch/sch/i.html?_nkw=" + urllib.parse.quote(q) +
               f"&_sacat=183454&_sop=15&_udlo={int(MIN_TOTAL)}&_udhi={int(MAX_TOTAL)}&_ipg=60")
        rows = []
        for attempt in range(2):
            try:
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(PAGE_WAIT_MS * (attempt + 1))
                rows = page.evaluate(EBAY_EXTRACT_JS)
            except Exception as e:
                log(f"eBay Fehler bei '{q}' (Versuch {attempt + 1}): {e}")
            if rows:
                break
            time.sleep(2)
        new = 0
        for r in rows:
            key = "ebay:" + r["id"]
            if key in out or TITLE_EXCLUDE.search(r["t"]) or LANGUAGE_EXCLUDE.search(r["t"]):
                continue
            price = parse_chf(r["p"])
            if price is None:
                continue
            ship = parse_shipping_ebay(r["s"], r.get("full", ""))
            hours_left = parse_hours_left(r["timeLeft"]) or parse_hours_left(r.get("full", ""))
            is_auction = bool(r["bids"]) or hours_left is not None or "gebot" in r.get("full", "").lower()
            img = re.sub(r"/s-l\d+\.(webp|jpg)", "/s-l1600.jpg", r["img"] or "")
            out[key] = dict(
                id=key, card_id=cid, card_name=name, title=r["t"], price=price, ship=ship,
                total=round(price + ship, 2), img=img, url=f"https://www.ebay.ch/itm/{r['id']}",
                source="eBay.ch", is_auction=is_auction, hours_left=hours_left, query=q,
            )
            new += 1
        log(f"eBay '{q}': {len(rows)} Treffer, {new} neu")
        time.sleep(0.8)
    return out


def _ricardo_price_and_title(lines):
    if "Sofort kaufen" not in lines:
        return None, None
    idx = lines.index("Sofort kaufen")
    if idx == 0:
        return None, None
    price = parse_chf(lines[idx - 1])
    if price is None:
        return None, None
    bad_exact = {"Sofort kaufen", "Pokémon", "Boost", "Beliebt", "Neu", "Top-Artikel", "|"}
    candidates = [
        l for l in lines
        if len(l) >= 10 and l not in bad_exact and "Gebot" not in l
        and not re.match(r"^[\d.,']+$", l) and not re.search(r"^\(.*\)$", l)
        and not re.search(r"Heute|Morgen|Gestern|,\s*\d{1,2}:\d{2}", l)
    ]
    title = candidates[0] if candidates else lines[0]
    return price, title


def fetch_ricardo(browser, cards):
    """Bug-Lektion aus booster_deal_finder.py: die Cloudflare-Freigabe von
    Ricardo.ch ist an den Browser-CONTEXT gebunden und wird nach mehreren
    schnellen Folge-Suchen entzogen - deshalb pro Suche ein komplett frischer
    Context. Bei 60 Karten dauert das entsprechend lang, aber laut Memory
    project_ebay_holo_ranking bleibt Ricardo ohnehin nur eine unzuverlaessige
    Bonus-Quelle (Erfolgsrate ~13/28 bei aehnlichem Umfang), nicht die primaere."""
    out = {}
    for cid, entry in cards:
        name = entry.get("name") or cid
        number = entry.get("number") or ""
        q = f'pokemon "{name}" 30th celebration {number}'
        url = "https://www.ricardo.ch/de/s/" + urllib.parse.quote(q) + "/"
        rows = []
        for attempt in range(2):
            ctx = browser.new_context(locale="de-CH", viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            try:
                page.goto(url, timeout=40000, wait_until="domcontentloaded")
                page.wait_for_timeout(PAGE_WAIT_MS * (attempt + 1))
                rows = page.evaluate(RICARDO_EXTRACT_JS)
            except Exception as e:
                log(f"Ricardo Fehler bei '{q}' (Versuch {attempt + 1}): {e}")
            finally:
                ctx.close()
            if rows:
                break
            time.sleep(3)
        new = 0
        for r in rows:
            price, title = _ricardo_price_and_title(r["lines"])
            # Ricardo unterstuetzt keine eBay-"-wort"-Ausschluss-Syntax in der
            # Suche selbst, deshalb hier nur der nachtraegliche Titel-Filter.
            if price is None or not title or TITLE_EXCLUDE.search(title) or LANGUAGE_EXCLUDE.search(title):
                continue
            m = re.search(r"-(\d+)/?$", r["href"].rstrip("/"))
            item_id = m.group(1) if m else r["href"]
            key = "ricardo:" + item_id
            if key in out:
                continue
            total = round(price + RICARDO_SHIP_ESTIMATE, 2)
            out[key] = dict(
                id=key, card_id=cid, card_name=name, title=title, price=price, ship=RICARDO_SHIP_ESTIMATE,
                total=total, img=r["img"] or "", url=r["href"], source="Ricardo.ch",
                is_auction=False, hours_left=None, query=q,
            )
            new += 1
        log(f"Ricardo '{q}': {len(rows)} Treffer, {new} neu")
        time.sleep(2.0)
    return out


def load_state():
    if RESET or not os.path.exists(STATE_FILE):
        return {"seen": {}, "history": {}}
    try:
        st = json.load(open(STATE_FILE, encoding="utf-8"))
        st.setdefault("seen", {})
        st.setdefault("history", {})
        return st
    except Exception:
        return {"seen": {}, "history": {}}


def save_state(st):
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=0)


def update_history(history, items):
    """NUR Sofort-Kaufen-Angebote (nicht Auktionen) fliessen in die Preis-Historie
    ein - ein aktuelles Auktions-Gebot ist erst kurz vor Ende ein verlaesslicher
    Preis, weit vor Ende sagt es nichts aus und wuerde den Referenzpreis massiv
    nach unten verzerren (Bug-Fund erster Voll-Testlauf: 2 CHF Gebot bei 6 Tagen
    Restzeit fuer eine Karte mit echtem Median CHF 78 wurde faelschlich als
    'Deal' gemeldet)."""
    today = datetime.now().strftime("%Y-%m-%d")
    for r in items.values():
        if r["is_auction"]:
            continue
        bucket = history.setdefault(r["card_id"], {})
        bucket[r["id"]] = {"total": r["total"], "last_seen": today}
    cutoff = datetime.now().timestamp() - HISTORY_MAX_DAYS * 86400
    for cid in list(history.keys()):
        bucket = history[cid]
        for lid in list(bucket.keys()):
            try:
                ts = datetime.strptime(bucket[lid]["last_seen"], "%Y-%m-%d").timestamp()
            except Exception:
                ts = 0
            if ts < cutoff:
                del bucket[lid]
        if not bucket:
            del history[cid]


def find_deals(items, history):
    """Referenzpreis (Median) wird AUSSCHLIESSLICH aus Sofort-Kaufen-Angeboten
    gebildet (live + Historie) - Auktions-Gebote fliessen nie in den Median ein
    (siehe update_history()-Docstring). Kategorien:
      - 'deal': NUR Sofort-Kaufen-Angebote, Preis <= DEAL_RATIO des Medians.
      - 'auction_soon': NUR Auktionen, die innerhalb AUCTION_SOON_HOURS enden
        UND deren aktuelles Gebot <= Median liegt - ausdruecklich kein
        garantierter Deal (siehe post_discord()), nur ein Hinweis, dass der
        aktuelle Stand guenstig ist. Auktionen mit mehr Restzeit werden NICHT
        gemeldet (ein niedriges Gebot Tage vor Ende sagt nichts aus - Bug-Fund
        erster Voll-Testlauf: CHF 1 Gebot bei 6 Tagen Restzeit wurde faelschlich
        als 99%-Deal gemeldet, obwohl die Karte real ~CHF 78 wert ist)."""
    by_card = {}
    for r in items.values():
        if r["is_auction"]:
            continue
        by_card.setdefault(r["card_id"], []).append(r["total"])
    for cid, bucket in history.items():
        vals = [v["total"] for v in bucket.values()]
        by_card.setdefault(cid, [])
        by_card[cid] = by_card[cid] + vals

    deals = []
    for r in items.values():
        vals = sorted(by_card.get(r["card_id"], []))
        if len(vals) < MIN_SAMPLES:
            continue
        med = statistics.median(vals)
        if med < MIN_MEDIAN:
            continue
        ratio = r["total"] / med
        if ratio < MIN_RATIO_SANITY:
            continue
        if r["is_auction"]:
            if r["hours_left"] is None or r["hours_left"] > AUCTION_SOON_HOURS or ratio > 1.0:
                continue
            category = "auction_soon"
        else:
            if not (MIN_TOTAL <= r["total"] <= MAX_TOTAL) or ratio > DEAL_RATIO:
                continue
            category = "deal"
        deals.append(dict(r, median=round(med, 2), n=len(vals), ratio=round(ratio, 2), category=category))
    # Echte Deals zuerst (guenstigster zuerst), dann bald endende Auktionen (dringendste zuerst)
    deals.sort(key=lambda d: (0 if d["category"] == "deal" else 1,
                              d["ratio"] if d["category"] == "deal" else (d["hours_left"] or 999)))
    return deals


def post_discord(webhook, deals):
    embeds = []
    for d in deals:
        pct = int(round((1 - d["ratio"]) * 100))
        if d["category"] == "deal":
            headline = f"\U0001F4B8 **DEAL: {pct}% unter Median**"
        else:
            hl = d["hours_left"] or 0
            headline = f"⏰ **Auktion endet in {hl:.1f} Std - aktuell {int(round((1 - d['ratio']) * 100)) if d['ratio'] < 1 else 0}% unter/am Median**"
        ship_note = "" if d["source"] == "eBay.ch" else " (Versand geschaetzt)"
        verify_note = "\n⚠️ Preis/Versand vor dem Kauf auf der Zielseite gegenpruefen (automatisiert erfasst)." if d["ratio"] < 0.4 else ""
        desc = (f"{headline}\n"
                f"**CHF {d['total']:.2f}** inkl. Versand{ship_note} (Preis {d['price']:.2f} + Versand {d['ship']:.2f})\n"
                f"Karte: **{d['card_name']}** - Quelle: **{d['source']}**\n"
                f"Median CHF {d['median']:.2f} aus {d['n']} beobachteten Angeboten"
                f"{verify_note}\n"
                f"[Zum Angebot]({d['url']})")
        color = 0x2ECC71 if d["category"] == "deal" else 0xF1C40F
        embeds.append({
            "title": d["title"][:240],
            "url": d["url"],
            "description": desc,
            "color": color,
            "image": {"url": d["img"]} if d["img"] else None,
            "footer": {"text": f"{d['source']} - gefunden {datetime.now():%d.%m.%Y %H:%M}"},
        })
    for e in embeds:
        if e.get("image") is None:
            e.pop("image", None)
    for i in range(0, len(embeds), 8):
        payload = {"username": "CHAU Einzelkarten-Deals", "embeds": embeds[i:i + 8]}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json", "User-Agent": "chau-singles-deal-finder/1.0"})
        with urllib.request.urlopen(req) as r:
            log(f"Discord {r.status} ({len(embeds[i:i + 8])} Embeds)")
        time.sleep(1.5)


def acquire_lock():
    if os.path.exists(LOCK_FILE):
        if time.time() - os.path.getmtime(LOCK_FILE) < 3600:
            return False
    open(LOCK_FILE, "w").write(str(os.getpid()))
    return True


def main():
    if not acquire_lock():
        log("Ein anderer Lauf ist bereits aktiv, abgebrochen.")
        return
    try:
        webhook = os.environ.get("DISCORD_WEBHOOK_POKEMON_30TH_SINGLES") or ""
        if not webhook and not DRY_RUN:
            log("Webhook DISCORD_WEBHOOK_POKEMON_30TH_SINGLES fehlt.")
            return

        cards = load_chase_cards()
        log(f"{len(cards)} Chase-Karten geladen aus singles_cache.json")
        st = load_state()

        launch_args = ["--window-size=1280,900"]
        if os.name == "nt":
            launch_args.insert(0, "--window-position=-2400,-2400")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=launch_args)
            ctx = browser.new_context(locale="de-CH", viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            items = {}
            items.update(fetch_ebay(page, cards))
            items.update(fetch_ricardo(browser, cards))
            browser.close()

        deals = find_deals(items, st["history"])
        log(f"{len(items)} Angebote geladen (eBay+Ricardo), {len(deals)} Deals/bald-endende Auktionen gesamt")
        new = [d for d in deals if d["id"] not in st["seen"]][:MAX_POSTS_PER_RUN]
        for d in deals[:30]:
            log(f"  {'NEU ' if d['id'] not in st['seen'] else '    '}[{d['category']}] {d['ratio']:.2f}x CHF {d['total']:.2f} (Median {d['median']:.2f}, n={d['n']}) {d['card_name']} [{d['source']}] | {d['url']}")

        if DRY_RUN:
            log(f"Dry-Run: {len(new)} neue Deals/Auktionen wuerden gepostet.")
            return
        if new:
            post_discord(webhook, new)
        update_history(st["history"], items)
        for d in new:
            st["seen"][d["id"]] = datetime.now().strftime("%Y-%m-%d")
        cutoff = datetime.now().timestamp() - 14 * 86400
        st["seen"] = {k: v for k, v in st["seen"].items() if datetime.strptime(v, "%Y-%m-%d").timestamp() > cutoff}
        save_state(st)
        log(f"{len(new)} neue Deals/Auktionen gepostet.")
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
