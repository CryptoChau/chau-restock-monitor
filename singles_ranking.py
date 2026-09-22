"""Taegliche Rangliste fuer Pokemon TCG: 30th Celebration Einzelkarten (welche
Karte man eventuell kaufen sollte, weil sie im Preis aufwaerts statt abwaerts geht).

Warum eigene Preis-Historie statt fremder Trend-Felder: pokemontcg.io (die einzige
kostenlose API, die TCGPlayer+Cardmarket-Preise kombiniert liefert - siehe Memory)
ist deprecated, oft instabil (haeufig HTTP 500) und hat fuer ein 6 Tage altes Set
noch keine eigenen Trend-Felder (avg7/avg30) befuellt - ein Preistrend braucht
zwangslaeufig mehrere Tage Historie, egal welche Quelle man nimmt. eBay-Scraping
wurde geprueft und verworfen (harter 403-Bot-Block, nur mit camoufox umgehbar,
bei ~190 Karten pro Tag viel zu langsam/riskant). Deshalb: Dieses Skript laeuft
1x/Tag, holt den aktuellen Preis jeder Karte und haengt ihn an eine selbst
gefuehrte Historie (singles_cache.json) an - der Trend wird daraus lokal berechnet,
sobald genug Tage vorhanden sind. Bis dahin dient die Kartenseltenheit
(rarity) als Ersatz-Ranking-Signal (bekannter Heuristik: Special Illustration
Rare/Hyper Rare erholen sich nach dem Post-Release-Preisverfall meist zuerst).
"""
import json
import os
import sys
import time

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
    ("illustration rare", 4),
    ("ace spec rare", 3),
    ("ultra rare", 3),
    ("double rare", 2),
    ("rare holo", 1),
    ("rare", 1),
]

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
    dass ein langes Backoff pro Karte bei ~190 Karten den 25-Minuten-Workflow-Timeout
    sprengt (siehe Memory, erster Testlauf 2026-09-22 lief komplett durch, ohne
    auch nur eine Karte fertig geloggt zu haben). Eine fehlgeschlagene Karte wird
    morgen beim naechsten taeglichen Lauf automatisch erneut versucht, es lohnt sich
    also nicht, heute lange dafuer zu warten - lieber schnell weiter zur naechsten."""
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
    zurueck, wenn Cardmarket (noch) keine Daten hat."""
    cm = (data.get("cardmarket") or {}).get("prices") or {}
    if cm.get("trendPrice"):
        return cm["trendPrice"], "cardmarket"
    tcg = (data.get("tcgplayer") or {}).get("prices") or {}
    for variant in tcg.values():
        if isinstance(variant, dict) and variant.get("market"):
            return variant["market"], "tcgplayer"
    return None, None


TIME_BUDGET_SECONDS = 18 * 60  # Job-Timeout ist 25 Min - bei Ueberschreitung sauber abbrechen
                                # (speichern + posten) statt vom Runner hart gekillt zu werden


def update_snapshot():
    """Holt fuer jede Karte im Set den aktuellen Preis und haengt ihn an die
    lokale Historie an. Wird 1x/Tag aufgerufen (siehe singles-ranking.yml)."""
    cache = load_cache()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ok, failed = 0, 0
    start = time.monotonic()
    total_cards = sum(SET_TOTALS.values())
    processed = 0

    for set_id, total in SET_TOTALS.items():
        for n in range(1, total + 1):
            if time.monotonic() - start > TIME_BUDGET_SECONDS:
                log(f"Zeitbudget erreicht nach {processed}/{total_cards} Karten - breche kontrolliert ab, Rest folgt morgen")
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
                # Keinen zweiten Punkt am selben Tag anhaengen (Skript kann bei
                # Retries/manuellen Laeufen mehrfach am Tag laufen)
                today = now[:10]
                if not history or history[-1]["t"][:10] != today:
                    history.append({"t": now, "p": price})
                    history = history[-MAX_HISTORY_POINTS:]

            cache[card_id] = {
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
            }

    save_cache(cache)
    log(f"Snapshot aktualisiert: {ok} Karten OK, {failed} fehlgeschlagen (uebersprungen, alter Stand bleibt)")
    return cache


def compute_score(entry):
    """Score fuer die Rangliste: sobald genug Preis-Historie vorhanden ist (>=2
    Tage), zaehlt die Preisentwicklung in % seit dem aeltesten Datenpunkt (max.
    MAX_HISTORY_POINTS Tage zurueck) am staerksten. Ohne genug Historie dient die
    Seltenheit als Ersatzsignal, damit die Liste vom ersten Tag an nicht leer ist."""
    history = entry.get("price_history") or []
    trend_pct = None
    if len(history) >= 2 and history[0]["p"]:
        trend_pct = (history[-1]["p"] - history[0]["p"]) / history[0]["p"] * 100

    r_weight = rarity_weight(entry.get("rarity"))
    if trend_pct is not None:
        score = trend_pct * 3 + r_weight  # Trend dominiert, Rarity als Tiebreak
    else:
        score = r_weight
    return score, trend_pct


def build_ranking(top_n=15):
    """Karten OHNE Preisdaten werden NICHT ausgefiltert (bei einem frisch
    erschienenen Set haben anfangs ALLE Karten price_now=None - ein Filter darauf
    wuerde die Liste komplett leer machen statt auf Rarity zurueckzufallen)."""
    cache = load_cache()
    rows = []
    for card_id, entry in cache.items():
        if not entry.get("name"):
            continue
        score, trend_pct = compute_score(entry)
        rows.append((score, trend_pct, card_id, entry))
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows[:top_n], len(rows)


def build_header(total_tracked, days_of_history):
    lines = [
        f"\U0001F4C8 **30th Celebration Einzelkarten - Kauf-Rangliste** ({total_tracked} Karten getrackt, {days_of_history} Tag(e) Preis-Historie)",
    ]
    if days_of_history < 3:
        lines.append(
            "_Noch wenig Preis-Historie - Ranking basiert momentan hauptsaechlich auf "
            "Kartenseltenheit, nicht auf echtem Trend. Wird taeglich genauer._"
        )
    lines.append(
        "Hinweis: Preise ohne Marktpreise-Anspruch, aus TCGPlayer/Cardmarket-API "
        "(pokemontcg.io), taeglich selbst getrackt."
    )
    return "\n".join(lines)


def build_embeds(rows):
    """Ein Embed pro Karte mit Bild, statt reiner Textliste - Nutzerwunsch."""
    embeds = []
    for i, (score, trend_pct, card_id, entry) in enumerate(rows, 1):
        name = entry.get("name", card_id)
        rarity = entry.get("rarity") or "?"
        price = entry.get("price_now")
        price_str = f"{price:.2f}" if isinstance(price, (int, float)) else "noch kein Marktpreis"
        source = entry.get("price_source") or "?"
        if trend_pct is not None:
            arrow = "\U0001F7E2▲" if trend_pct > 0 else ("\U0001F534▼" if trend_pct < 0 else "⚪")
            trend_str = f"{arrow} {trend_pct:+.1f}%"
        else:
            trend_str = "⚪ noch kein Trend"

        fields = [
            {"name": "Seltenheit", "value": rarity, "inline": True},
            {"name": "Preis", "value": f"{price_str}" + (f" ({source})" if isinstance(price, (int, float)) else ""), "inline": True},
            {"name": "Trend", "value": trend_str, "inline": True},
        ]
        embed = {
            "title": f"{i}. {name}",
            "fields": fields,
            "color": 0x7C3AED,
        }
        image = entry.get("image")
        if image:
            embed["thumbnail"] = {"url": image}
        tcg_url = entry.get("tcgplayer_url")
        if tcg_url:
            embed["url"] = tcg_url
            embed["footer"] = {"text": "Klick auf den Titel fuer TCGplayer"}
        embeds.append(embed)
    return embeds


def send_discord(webhook_url, header, embeds):
    if not webhook_url:
        log("Kein Discord-Webhook konfiguriert (DISCORD_WEBHOOK_POKEMON_30TH_SINGLES), ueberspringe Post")
        return
    try:
        r = requests.post(webhook_url, json={"content": header}, timeout=REQUEST_TIMEOUT)
        if r.status_code not in (200, 204):
            log(f"Discord-Header-Post fehlgeschlagen: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        log(f"Discord-Header-Post Fehler: {type(e).__name__}: {e}")

    # Discord erlaubt max. 10 Embeds pro Nachricht - in Batches senden.
    for batch_start in range(0, len(embeds), 10):
        batch = embeds[batch_start:batch_start + 10]
        try:
            r = requests.post(webhook_url, json={"embeds": batch}, timeout=REQUEST_TIMEOUT)
            if r.status_code not in (200, 204):
                log(f"Discord-Embed-Post fehlgeschlagen: HTTP {r.status_code} {r.text[:200]}")
        except Exception as e:
            log(f"Discord-Embed-Post Fehler: {type(e).__name__}: {e}")
        time.sleep(1)  # Rate-Limit-Puffer zwischen Batches


if __name__ == "__main__":
    update_snapshot()
    top_rows, total_tracked = build_ranking(top_n=15)
    max_history = 0
    cache_for_count = load_cache()
    for entry in cache_for_count.values():
        max_history = max(max_history, len(entry.get("price_history") or []))
    header = build_header(total_tracked, max_history)
    embeds = build_embeds(top_rows)
    log(header)
    log(f"{len(embeds)} Embeds vorbereitet")
    send_discord(os.environ.get("DISCORD_WEBHOOK_POKEMON_30TH_SINGLES"), header, embeds)
