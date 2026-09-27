"""Taegliche Rangliste fuer Pokemon TCG: 30th Celebration Einzelkarten.

V2 (2026-09-27, Nutzer-Feedback "bisher keine Daten, Ranking nutzlos"): Die reine
pokemontcg.io-Preisabfrage (V1) lieferte fuer JEDE Karte "kein Marktpreis" - das
Set ist zu neu, weder Cardmarket noch TCGplayer haben Preisfelder befuellt. Bloss
Rarity als Ranking-Signal ist kein echtes Datum. Jetzt: echtes eBay.ch-Scraping
fuer die "Chase"-Karten (Special Illustration Rare, Illustration Rare, Futuristic
Rare, Pikachu Rare - 60 von 191 Karten, siehe CHASE_RARITIES). Das beantwortet
die Nutzerfragen mit echten Daten statt Heuristik:
  - "meistverkauft/beliebt" -> Anzahl AKTIVER Sofort-Kaufen-Angebote pro Karte
    (Naeherung fuer Nachfrage: mehr Angebote nach dem Hype-Release = mehr Pulls/
    Interesse). ACHTUNG: eBay.ch verlangt fuer echte "verkaufte Artikel"
    (LH_Sold=1) zwingend Login - ohne Account nicht automatisierbar (kein Bot-
    Block, sondern eBay-Policy, siehe ebay_sold_search()-Docstring). Keine echten
    Verkaufszahlen, sondern Angebots-/Interesse-Naeherung.
  - "Wertsteigerungs-Potenzial" -> Trend des taeglich getrackten Median-Preises
    dieser aktiven Angebote (Asking-Price-Trend, keine bestaetigten Verkaeufe).
Technik identisch zum bereits funktionierenden eBay-Deal-Finder (siehe
D:\\Shopify CHW\\ebay-holo-deals\\vintage_deal_finder.py, Memory
project_ebay_holo_ranking): eBay blockt echten Headless-Chrome (403), ein
"headed" Chromium unter xvfb (siehe singles-ranking.yml) kommt aber durch. Bei
191 Karten waere das zu langsam - deshalb nur die 60 Chase-Karten, nicht das
ganze Set (Commons/Uncommons interessieren fuers Investment ohnehin niemanden).
Rarity/Bild/Name kommen weiterhin guenstig ueber pokemontcg.io (Metadaten-Cache).
"""
import json
import os
import re
import statistics
import sys
import time
import urllib.parse

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "singles_cache.json")

# Set-IDs und Kartenzahl von pokemontcg.io (Stand 2026-09-22). Die Suchendpunkt
# (?q=set.id:...) ist bei diesem deprecateten Dienst durchgehend kaputt (HTTP 500),
# einzelne Karten per ID (/v2/cards/<id>) funktionieren aber (wenn auch flaky) -
# deshalb wird pro Karten-ID einzeln abgefragt statt gesucht.
SET_TOTALS = {
    "me55": 161,   # 30th Celebration (Hauptset)
    "me55c": 30,   # 30th Celebration: Classic Collection (Promo-Reprints)
}

RARITY_WEIGHT = [
    ("special illustration rare", 6),
    ("hyper rare", 6),
    ("secret rare", 6),
    ("rainbow rare", 6),
    ("futuristic rare", 6),
    ("pikachu rare", 5),
    ("illustration rare", 4),
    ("ace spec rare", 3),
    ("ultra rare", 3),
    ("double rare", 2),
    ("rare holo", 1),
    ("rare", 1),
]

# Nur diese Rarities werden taeglich per eBay gescraped (Investment-relevant,
# siehe Docstring). Alle anderen Karten behalten nur ihre pokemontcg.io-Metadaten
# (Name/Bild/Rarity) fuers Set-Tracking, aber keine eBay-Historie.
CHASE_RARITIES = {"special illustration rare", "illustration rare", "futuristic rare", "pikachu rare"}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; chau-restock-monitor/1.0)"}
REQUEST_TIMEOUT = 8
MAX_HISTORY_POINTS = 90  # ~3 Monate taegliche Snapshots


def log(msg):
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Lokale Windows-Konsolen (cp1252) koennen Emojis nicht darstellen - passiert
        # auf dem Linux-GitHub-Runner (UTF-8) nicht, hier nur fuers lokale Testen.
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)


def rarity_weight(rarity):
    if not rarity:
        return 0
    r = rarity.lower()
    for key, w in RARITY_WEIGHT:
        if key in r:
            return w
    return 0


def fetch_card(card_id, retries=2):
    """Holt eine einzelne Karte. Kurze Retries, KEIN langes Backoff: die API ist so
    instabil (haeufig HTTP 500, deprecated/wenig gepflegt - siehe Modul-Docstring),
    dass ein langes Backoff pro Karte bei ~190 Karten den Workflow-Timeout sprengt.
    Eine fehlgeschlagene Karte wird morgen beim naechsten taeglichen Lauf automatisch
    erneut versucht, es lohnt sich also nicht, heute lange dafuer zu warten."""
    for attempt in range(retries):
        try:
            r = requests.get(
                f"https://api.pokemontcg.io/v2/cards/{card_id}",
                headers=HEADERS, timeout=REQUEST_TIMEOUT,
            )
            if r.status_code == 200:
                return r.json().get("data")
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(1)
    return None


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def extract_price(data):
    """Bevorzugt Cardmarket-Trendpreis (EU-naeher), faellt auf TCGPlayer-Market
    zurueck, wenn Cardmarket (noch) keine Daten hat. Rein informativ/Bonus -
    bei diesem Set aktuell fast immer leer (siehe Docstring), die eigentliche
    Preis-Historie kommt seit V2 aus eBay (siehe update_ebay_snapshot)."""
    cm = (data.get("cardmarket") or {}).get("prices") or {}
    if cm.get("trendPrice"):
        return cm["trendPrice"], "cardmarket"
    tcg = (data.get("tcgplayer") or {}).get("prices") or {}
    for variant in tcg.values():
        if isinstance(variant, dict) and variant.get("market"):
            return variant["market"], "tcgplayer"
    return None, None


TIME_BUDGET_SECONDS = 8 * 60  # Metadaten-Fetch bekommt nur einen Teil des Gesamt-Zeitbudgets,
                               # der Rest ist fuer das (langsamere) eBay-Scraping reserviert.


def update_snapshot():
    """Holt fuer jede Karte im Set Name/Bild/Rarity (+ TCGplayer/Cardmarket-Preis
    als Bonus, meist leer). Wird 1x/Tag aufgerufen (siehe singles-ranking.yml)."""
    cache = load_cache()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ok, failed = 0, 0
    start = time.monotonic()
    total_cards = sum(SET_TOTALS.values())
    processed = 0

    for set_id, total in SET_TOTALS.items():
        for n in range(1, total + 1):
            if time.monotonic() - start > TIME_BUDGET_SECONDS:
                log(f"Zeitbudget (Metadaten) erreicht nach {processed}/{total_cards} Karten - Rest folgt morgen")
                save_cache(cache)
                return cache
            processed += 1
            if processed % 20 == 0:
                log(f"Fortschritt: {processed}/{total_cards} Karten geprueft ({ok} OK, {failed} fehlgeschlagen)")

            card_id = f"{set_id}-{n}"
            data = fetch_card(card_id)
            if data is None:
                failed += 1
                continue
            ok += 1

            price, source = extract_price(data)
            entry = cache.get(card_id, {})
            history = entry.get("price_history", [])
            if price is not None:
                today = now[:10]
                if not history or history[-1]["t"][:10] != today:
                    history.append({"t": now, "p": price})
                    history = history[-MAX_HISTORY_POINTS:]

            entry.update({
                "name": data.get("name"),
                "rarity": data.get("rarity"),
                "number": data.get("number"),
                "set_name": (data.get("set") or {}).get("name"),
                "image": (data.get("images") or {}).get("large") or (data.get("images") or {}).get("small"),
                "tcgplayer_url": (data.get("tcgplayer") or {}).get("url"),
                "price_now": price,
                "price_source": source,
                "price_history": history,
                "last_updated": now,
            })
            cache[card_id] = entry

    save_cache(cache)
    log(f"Metadaten aktualisiert: {ok} Karten OK, {failed} fehlgeschlagen (uebersprungen, alter Stand bleibt)")
    return cache


# ------------------------------------------------------------- eBay-Sold-Scraping
# Identische Technik wie D:\Shopify CHW\ebay-holo-deals\vintage_deal_finder.py:
# eBay blockt echten Headless-Chrome - nur ein "headed" Chromium (unter xvfb-run
# auf dem GitHub-Actions-Runner, siehe singles-ranking.yml) kommt durch.
EBAY_EXTRACT_JS = """() => [...document.querySelectorAll('li.s-item, li.s-card')].map(li => {
  const a = li.querySelector('a[href*="/itm/"]');
  const id = a ? (a.href.match(/itm[/](\\d+)/) || [])[1] : '';
  const t = (li.querySelector('.s-item__title, .s-card__title') || {}).textContent || '';
  const p = (li.querySelector('.s-item__price, .s-card__price') || {}).textContent || '';
  return {id, t: t.replace('Wird in neuem Fenster oder Tab geöffnet', '').trim(), p: p.trim()};
}).filter(x => x.id && x.id !== '123456')"""


def parse_chf(s):
    if not s:
        return None
    m = re.search(r"CHF\s*([\d'.]+),(\d{2})", s)
    if not m:
        m2 = re.search(r"CHF\s*([\d'.]+)", s)
        return float(m2.group(1).replace("'", "").replace(".", "")) if m2 else None
    return float(m.group(1).replace("'", "").replace(".", "") + "." + m.group(2))


def ebay_sold_search(page, name, number, retries=2):
    """Sucht AKTIVE Sofort-Kaufen-Angebote fuer eine Karte auf eBay.ch (Naeherungswert
    fuer Nachfrage/Wert - siehe Docstring-Update). Kartenname + Kartennummer im
    Suchbegriff, um Karten mit identischem Namen (z.B. die 30 verschiedenen
    "Pikachu Rare"-Artvarianten) etwas einzugrenzen - eBay-Titel sind hier nicht
    perfekt disambiguierbar, das Verfahren ist ein Naeherungswert, kein exakter
    Karten-Match.

    WICHTIG (Bug-Fund 2026-09-27, erster Testlauf): "Verkaufte Artikel"
    (LH_Sold=1&LH_Complete=1) verlangt bei eBay.ch zwingend einen eingeloggten
    Account - ohne Session landet man nur auf der Login-Seite (0 Treffer fuer
    ALLE 60 Karten im ersten Testlauf, kein Bot-Block). Da ein automatisierter
    eBay-Login nicht vertretbar ist (Kontodaten, ToS), wird stattdessen die
    Anzahl AKTIVER Sofort-Kaufen-Angebote als Nachfrage-Naeherung verwendet
    (mehr aktive Angebote nach einem Hype-Release deutet auf mehr Pulls/
    Interesse hin) und der Median-Preis dieser aktiven Angebote taeglich
    getrackt (Trend = Wertentwicklung der Verkaufspreise, keine bestaetigten
    Verkaeufe)."""
    query = f'pokemon "{name}" 30th celebration {number}'
    url = ("https://www.ebay.ch/sch/i.html?_nkw=" + urllib.parse.quote(query) +
           "&_sacat=183454&LH_BIN=1&_sop=12&_ipg=60")
    for attempt in range(retries):
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000 * (attempt + 1))
            rows = page.evaluate(EBAY_EXTRACT_JS)
            if rows:
                return rows
        except Exception as e:
            log(f"eBay-Fehler bei '{query}' (Versuch {attempt + 1}): {e}")
        time.sleep(2)
    return []


EBAY_TIME_BUDGET_SECONDS = 32 * 60  # Rest des Gesamt-Zeitbudgets (Job-Timeout 45 Min, siehe YAML)


def update_ebay_snapshot(cache):
    """Fuer jede Chase-Karte (siehe CHASE_RARITIES): eBay-Sold-Suche, Median-Preis
    + Anzahl verkaufter Angebote in die Historie anhaengen. Braucht ein echtes
    Chromium-Fenster (Playwright) - Import hier drin, damit update_snapshot()
    (Metadaten) auch ohne installiertes Playwright lokal laufen kann."""
    from playwright.sync_api import sync_playwright

    targets = [(cid, e) for cid, e in cache.items() if (e.get("rarity") or "").lower() in CHASE_RARITIES]
    log(f"{len(targets)} Chase-Karten fuer eBay-Abgleich (aktive Angebote)")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    today = now[:10]
    start = time.monotonic()
    done, skipped = 0, 0

    launch_args = ["--window-size=1280,900"]
    if os.name == "nt":
        launch_args.insert(0, "--window-position=-2400,-2400")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=launch_args)
        ctx = browser.new_context(locale="de-CH", viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        for cid, entry in targets:
            if time.monotonic() - start > EBAY_TIME_BUDGET_SECONDS:
                log(f"eBay-Zeitbudget erreicht nach {done}/{len(targets)} Karten - Rest folgt morgen")
                skipped = len(targets) - done
                break
            rows = ebay_sold_search(page, entry.get("name") or cid, entry.get("number") or "")
            prices = [p_ for r in rows if (p_ := parse_chf(r["p"])) is not None]
            ebay_hist = entry.get("ebay_sold_history", [])
            if prices:
                median = round(statistics.median(prices), 2)
                if not ebay_hist or ebay_hist[-1]["t"][:10] != today:
                    ebay_hist.append({"t": now, "median": median, "n": len(prices)})
                    ebay_hist = ebay_hist[-MAX_HISTORY_POINTS:]
                entry["ebay_sold_history"] = ebay_hist
                entry["ebay_median_now"] = median
                entry["ebay_sold_count_now"] = len(prices)
            else:
                entry["ebay_sold_count_now"] = 0
            cache[cid] = entry
            done += 1
            if done % 10 == 0:
                log(f"eBay-Fortschritt: {done}/{len(targets)} Chase-Karten geprueft")
            time.sleep(1.0)
        browser.close()

    save_cache(cache)
    log(f"eBay-Sold-Abgleich fertig: {done} geprueft, {skipped} uebersprungen (Zeitbudget)")
    return cache


def compute_trend(entry):
    """Preistrend in % seit dem aeltesten eBay-Sold-Datenpunkt (max.
    MAX_HISTORY_POINTS Tage zurueck) - das eigentliche Wertsteigerungs-Signal."""
    history = entry.get("ebay_sold_history") or []
    if len(history) >= 2 and history[0]["median"]:
        return (history[-1]["median"] - history[0]["median"]) / history[0]["median"] * 100
    return None


def build_ranking_popularity(cache, top_n=10):
    """'Meistverkauft/beliebt': sortiert nach Anzahl verkaufter eBay-Angebote in
    der letzten Suche (echtes Nachfrage-Signal, kein Rarity-Ersatz)."""
    rows = [(cid, e) for cid, e in cache.items() if e.get("ebay_sold_count_now", 0) > 0]
    rows.sort(key=lambda x: x[1]["ebay_sold_count_now"], reverse=True)
    return rows[:top_n]


def build_ranking_growth(cache, top_n=10):
    """'Wertsteigerungs-Potenzial': sortiert nach echtem eBay-Preistrend, mit
    Rarity als Tiebreak. Solange keine 2 Tage Historie da sind, faellt die Liste
    auf Chase-Rarity + aktuellen Sold-Median zurueck (nicht leer am 1. Tag)."""
    rows = []
    for cid, e in cache.items():
        if (e.get("rarity") or "").lower() not in CHASE_RARITIES:
            continue
        trend = compute_trend(e)
        score = trend if trend is not None else rarity_weight(e.get("rarity"))
        rows.append((score, trend, cid, e))
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows[:top_n]


def build_header(cache):
    chase_n = sum(1 for e in cache.values() if (e.get("rarity") or "").lower() in CHASE_RARITIES)
    max_hist = max((len(e.get("ebay_sold_history") or []) for e in cache.values()), default=0)
    lines = [
        f"\U0001F4C8 **30th Celebration Einzelkarten - Rangliste** ({chase_n} Chase-Karten getrackt, {max_hist} Tag(e) eBay-Preis-Historie)",
    ]
    if max_hist < 3:
        lines.append(
            "_Noch wenig Preis-Historie - der Wertsteigerungs-Trend wird taeglich genauer. "
            "Das Nachfrage-Ranking basiert bereits auf echten eBay.ch-Angeboten von heute._"
        )
    lines.append(
        "Hinweis: eBay.ch AKTIVE Sofort-Kaufen-Angebote (Naeherungswert, Kartenname+Nummer als "
        "Suche, keine 1:1-Garantie pro Druckvariante) - eBay verlangt fuer 'verkaufte Artikel' "
        "zwingend Login, daher keine echten Verkaufszahlen. Taeglich selbst getrackt. Keine "
        "offiziellen Marktpreise/Cardmarket-Daten."
    )
    return "\n".join(lines)


def build_embeds(rows, kind):
    """Ein Embed pro Karte mit Bild. kind='popularity' oder 'growth' waehlt die
    Feld-Beschriftung passend zur jeweiligen Liste."""
    embeds = []
    for i, item in enumerate(rows, 1):
        if kind == "popularity":
            cid, entry = item
            trend = compute_trend(entry)
        else:
            score, trend, cid, entry = item
        name = entry.get("name", cid)
        number = entry.get("number") or "?"
        rarity = entry.get("rarity") or "?"
        sold_count = entry.get("ebay_sold_count_now", 0)
        median = entry.get("ebay_median_now")
        median_str = f"CHF {median:.2f}" if isinstance(median, (int, float)) else "?"
        if trend is not None:
            arrow = "\U0001F7E2▲" if trend > 0 else ("\U0001F534▼" if trend < 0 else "⚪")
            trend_str = f"{arrow} {trend:+.1f}%"
        else:
            trend_str = "⚪ noch kein Trend"

        fields = [
            {"name": "Seltenheit", "value": f"{rarity} (#{number})", "inline": True},
            {"name": "eBay Angebote (aktiv)", "value": f"{sold_count}x, Median {median_str}", "inline": True},
            {"name": "Trend", "value": trend_str, "inline": True},
        ]
        embed = {"title": f"{i}. {name}", "fields": fields, "color": 0x7C3AED}
        image = entry.get("image")
        if image:
            embed["thumbnail"] = {"url": image}
        tcg_url = entry.get("tcgplayer_url")
        if tcg_url:
            embed["url"] = tcg_url
            embed["footer"] = {"text": "Klick auf den Titel fuer TCGplayer"}
        embeds.append(embed)
    return embeds


def send_discord(webhook_url, content=None, embeds=None):
    if not webhook_url:
        log("Kein Discord-Webhook konfiguriert (DISCORD_WEBHOOK_POKEMON_30TH_SINGLES), ueberspringe Post")
        return
    # embeds kann eine leere Liste sein (z.B. 0 Treffer) - dann NICHT posten, sonst
    # lehnt Discord mit HTTP 400 "Cannot send an empty message" ab (Bug im ersten
    # Testlauf 2026-09-27, als die Sold-Suche wegen des Login-Requirements ueberall
    # 0 Treffer lieferte).
    if not content and not embeds:
        log("Nichts zu posten (leerer Inhalt), ueberspringe.")
        return
    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    try:
        r = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code not in (200, 204):
            log(f"Discord-Post fehlgeschlagen: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        log(f"Discord-Post Fehler: {type(e).__name__}: {e}")


if __name__ == "__main__":
    cache = update_snapshot()
    cache = update_ebay_snapshot(cache)

    webhook = os.environ.get("DISCORD_WEBHOOK_POKEMON_30TH_SINGLES")
    header = build_header(cache)
    log(header)
    send_discord(webhook, content=header)
    time.sleep(1)

    pop_rows = build_ranking_popularity(cache, top_n=10)
    if pop_rows:
        send_discord(webhook, content="\U0001F525 **Meistgefragt (aktive eBay.ch-Angebote heute)**")
        send_discord(webhook, embeds=build_embeds(pop_rows, "popularity"))
    else:
        send_discord(webhook, content="\U0001F525 **Meistgefragt:** heute keine aktiven eBay.ch-Angebote gefunden.")
    time.sleep(1)

    growth_rows = build_ranking_growth(cache, top_n=10)
    send_discord(webhook, content="\U0001F4B0 **Wertsteigerungs-Potenzial (eBay-Preistrend seit Tracking-Start)**")
    send_discord(webhook, embeds=build_embeds(growth_rows, "growth"))

    log(f"Fertig: {len(pop_rows)} Meistverkauft-Embeds, {len(growth_rows)} Wertsteigerungs-Embeds gepostet")
