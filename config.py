# CHAU Restock-Monitor Konfiguration
import os

# Discord-Webhooks: EIN Hauptserver "CHAU" (Kategorien pro Sub-Marke), hier Kategorie
# "Chau's Hype World" -> Kanaele #pokemon-restock-en / #onepiece-restock-en
# (siehe Memory project_restock_monitor_bot.md)
# Werden AUSSCHLIESSLICH aus Umgebungsvariablen gelesen (Windows-Umgebungsvariablen lokal,
# GitHub Actions Secrets in der Cloud) - damit die echten URLs NIE im (oeffentlichen) Git-Repo
# landen. Ohne gesetzte Variablen bleibt der Wert leer und send_discord() ueberspringt den Post.
#
# Windows-Sonderfall (Bug gefunden 2026-09-17): Die Windows-Aufgabenplanung uebernimmt neu per
# [Environment]::SetEnvironmentVariable(...,"User") gesetzte Variablen NICHT sofort in bereits
# laufende/registrierte geplante Taks - sie nutzt die beim Login zwischengespeicherte Umgebung,
# nicht die aktuelle Registry. Dadurch schlug der erste Dragon-Ball-Post mit "Invalid URL ''" fehl,
# obwohl die Variable laut `[Environment]::GetEnvironmentVariable` bereits korrekt gesetzt war.
# Fallback: fehlt eine Variable in os.environ, wird sie unter Windows direkt aus der Registry
# (HKCU\Environment) nachgeladen - das ist immer aktuell, unabhaengig vom Task-Cache.
def _get_webhook(name):
    val = os.environ.get(name, "")
    if val or os.name != "nt":
        return val
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            val, _ = winreg.QueryValueEx(key, name)
            return val
    except Exception:
        return ""


DISCORD_WEBHOOK_POKEMON = _get_webhook("DISCORD_WEBHOOK_POKEMON")
DISCORD_WEBHOOK_ONEPIECE = _get_webhook("DISCORD_WEBHOOK_ONEPIECE")
# Separate Kanaele fuer Vorbestellungen (Nutzerwunsch 2026-09-16 #3), gleiche Kategorie "Chau's Hype World"
DISCORD_WEBHOOK_POKEMON_PREORDER = _get_webhook("DISCORD_WEBHOOK_POKEMON_PREORDER")
DISCORD_WEBHOOK_ONEPIECE_PREORDER = _get_webhook("DISCORD_WEBHOOK_ONEPIECE_PREORDER")
# Dragon Ball Super Fusion World (Nutzerwunsch 2026-09-17), gleiche Kategorie "Chau's Hype World"
DISCORD_WEBHOOK_DRAGONBALL = _get_webhook("DISCORD_WEBHOOK_DRAGONBALL")
DISCORD_WEBHOOK_DRAGONBALL_PREORDER = _get_webhook("DISCORD_WEBHOOK_DRAGONBALL_PREORDER")
# Yu-Gi-Oh! (Nutzerwunsch 2026-09-17), gleiche Kategorie "Chau's Hype World"
DISCORD_WEBHOOK_YUGIOH = _get_webhook("DISCORD_WEBHOOK_YUGIOH")
DISCORD_WEBHOOK_YUGIOH_PREORDER = _get_webhook("DISCORD_WEBHOOK_YUGIOH_PREORDER")
# Pokemon 30th Anniversary Set, nur Englisch (Nutzerwunsch 2026-09-17), Unterkanal von Pokemon
DISCORD_WEBHOOK_POKEMON_30TH = _get_webhook("DISCORD_WEBHOOK_POKEMON_30TH")
DISCORD_WEBHOOK_POKEMON_30TH_PREORDER = _get_webhook("DISCORD_WEBHOOK_POKEMON_30TH_PREORDER")
# Pokemon 30-Jahre-Jubilaeum, ALLE Sprachen (Nutzerwunsch 2026-09-17), zweiter Unterkanal
DISCORD_WEBHOOK_POKEMON_30TH_ALL = _get_webhook("DISCORD_WEBHOOK_POKEMON_30TH_ALL")
DISCORD_WEBHOOK_POKEMON_30TH_ALL_PREORDER = _get_webhook("DISCORD_WEBHOOK_POKEMON_30TH_ALL_PREORDER")
# Magic: The Gathering (Nutzerwunsch 2026-09-17), gleiche Kategorie "Chau's Hype World"
DISCORD_WEBHOOK_MTG = _get_webhook("DISCORD_WEBHOOK_MTG")
DISCORD_WEBHOOK_MTG_PREORDER = _get_webhook("DISCORD_WEBHOOK_MTG_PREORDER")

# Marker, die auf eine Vorbestellung (statt sofort verfuegbar) hinweisen
PREORDER_MARKERS = [
    "vorbestellung", "vorbestellen", "vorbesteller", "pre-order", "preorder",
    "pre order", "coming soon", "release date", "erscheint am", "erscheinungsdatum",
    "release:",  # wog.ch zeigt bei kuenftigen Produkten nur "Release: <Datum>" ohne weiteren Text
]

# Schweizer Haendler, bei denen ein Shopify-Standard-Endpoint (/products.json) probiert wird.
RETAILERS = [
    "cardmaniac.ch",
    "zadoys.ch",
    "ryuland.ch",
    "zockbar.ch",
    "lunivault.ch",
    "laschocards.ch",
    "snyffer.ch",
]

# WooCommerce-Haendler: oeffentliche Store-API unter /wp-json/wc/store/v1/products
# (kein Login noetig, liefert sauberes JSON inkl. is_in_stock). Stand 2026-09-16 bestaetigt erreichbar.
WOOCOMMERCE_RETAILERS = [
    "theuncommonshop.ch",
    "naxoria.ch",
    "nooks.ch",  # braucht "www." Praefix, siehe fetch_woocommerce_products()
    "onitorashop.com",
]

# Shopware-Haendler: HTML-Widget-Suche unter /widgets/search?search=&p=<seite>
# (kein Login noetig, liefert HTML mit data-product-information JSON pro Produktkarte)
SHOPWARE_RETAILERS = [
    "amazingtoys.ch",
    "twomoons.ch",
]

# Softridge.ch: eigenes CMS ("ppadmin"), aber mit sauberer interner JSON-Suche-API
# (/api/shop/products?searchTerms=...), siehe fetch_softridge_products(). Liefert regionCode
# (EN/DE/JP/...) explizit im JSON - zuverlässiger als Titel-Sprachfilter (Nutzerwunsch 2026-09-17).
SOFTRIDGE_RETAILERS = [
    "softridge.ch",
]
SOFTRIDGE_SEARCH_TERMS = [
    "pokemon elite trainer box", "pokemon booster display", "pokemon booster bundle",
    "pokemon ultra premium collection",
    "one piece booster display", "one piece booster bundle",
    "dragon ball fusion world booster",
    "magic the gathering booster display", "magic the gathering bundle", "magic the gathering commander deck",
    "yu-gi-oh booster box", "yu-gi-oh booster display",
]

# Nicht (mehr) erreichbar/kein passender Endpoint gefunden, Stand 2026-09-17:
# goodgames.ch (SSL-Zertifikat zeigt auf falsche/fremde Domain, Seite technisch kaputt) -
# siehe Memory project_restock_monitor_bot.md

# Grosse Haendler, die per echtem Browser (Playwright) geprueft werden, da sie
# kein einfaches /products.json haben. Jeder Eintrag: Name, Such-URL-Template
# (mit {query} Platzhalter), CSS-Selektor fuer Produktlinks.
# Stand 2026-09-17 (erneut geprueft): digitec.ch/brack.ch/mueller.ch laden zwar problemlos in
# einem echten interaktiven Browser, blocken aber gezielt den Playwright-Automatisierungs-
# Browser (net::ERR_HTTP2_PROTOCOL_ERROR direkt beim goto()) - Fingerprint-basierte
# Bot-Erkennung, kein simpler IP-Block. Coop City: Suche laedt nicht ueber normale URL-Parameter
# (braucht weitere Recherche). World of Games (wog.ch): keine eigene Ergebnisseite, nur ein
# Autocomplete-Dropdown - technisch aufwendiger, noch nicht umgesetzt. Franz Carl Weber (fcw.ch):
# kein eigener Online-Shop mehr, nur Marken-Uebersichtsseite (gehoert zu Müller Handels AG,
# Sortiment laeuft über mueller.ch). Conforama.ch funktioniert einwandfrei mit Playwright.
BROWSER_RETAILERS = [
    {
        "name": "conforama.ch",
        "search_url": "https://www.conforama.ch/de/search?q={query}",
        "product_link_selector": "a[href*='/product/']",
        "card_selector": None,
    },
    {
        # World of Games: keine normale ?q=-Suche (die gibt 500), echte Ergebnisseite ueber
        # /index.cfm/search/searchTerm/<query>/orderBy/relevance gefunden (2026-09-17).
        "name": "wog.ch",
        "search_url": "https://www.wog.ch/de/index.cfm/search/searchTerm/{query}/orderBy/relevance",
        "product_link_selector": "a[href*='/details/product/']",
        "card_selector": "div.product-tile",
        # Kartenansicht schneidet lange Titel per JS ab ("...") und verliert dabei die
        # Sprachkennung (-EN-/-DE-) - img[alt] enthaelt immer den vollstaendigen Titel.
        "name_selector": "img",
        "name_attr": "alt",
    },
    {
        "name": "mediamarkt.ch",
        "search_url": "https://www.mediamarkt.ch/de/search.html?query={query}",
        "product_link_selector": "a[href*='/de/product/']",
        "card_selector": "article",
    },
    {
        "name": "manor.ch",
        "search_url": "https://www.manor.ch/de/search?query={query}",
        "product_link_selector": "a[href*='/de/p/']",
        "card_selector": None,  # kein zuverlaessiger Stock-Badge auf der Listenseite - siehe Memory
    },
    {
        "name": "migros.ch",
        "search_url": "https://www.migros.ch/de/search?query={query}",
        "product_link_selector": "a[href*='/de/product/']",
        "card_selector": None,
        "name_selector": "mo-product-name",  # sauberer Produktname, siehe Memory
    },
    {
        "name": "orellfuessli.ch",
        "search_url": "https://www.orellfuessli.ch/suche?sq={query}",
        "product_link_selector": "a[href*='artikeldetails']",
        "card_selector": None,
    },
    {
        "name": "carab.ch",
        "search_url": "https://www.carab.ch/shop?q={query}",
        "product_link_selector": "a[href*='/shop/product/']",
        "card_selector": None,  # kein zuverlaessiger Stock-Badge auf der Listenseite - siehe Memory
    },
    {
        "name": "toytans.ch",
        "search_url": "https://www.toytans.ch/de/suche?controller=search&s={query}",
        "product_link_selector": "a[href*='.html']",
        "card_selector": None,  # klarer Lagerstatus-Text "Auf Lager"/"Nicht auf Lager", siehe Memory
    },
]

# Suchbegriffe fuer die Browser-Haendler (pro Marke separat, damit Treffer klar zugeordnet werden koennen)
BROWSER_SEARCH_TERMS_POKEMON = [
    "pokemon elite trainer box",
    "pokemon booster display",
    "pokemon booster bundle",
    "pokemon ultra premium collection",
]
BROWSER_SEARCH_TERMS_ONEPIECE = [
    "one piece booster display",
    "one piece booster bundle",
]
BROWSER_SEARCH_TERMS_DRAGONBALL = [
    "dragon ball fusion world booster",
    "dragon ball fusion world starter deck",
]
BROWSER_SEARCH_TERMS_MTG = [
    "magic the gathering booster display",
    "magic the gathering bundle",
    "magic the gathering commander deck",
]
BROWSER_SEARCH_TERMS_YUGIOH = [
    "yu-gi-oh booster box",
    "yu-gi-oh booster display",
]

# Pokemon: alle ENGLISCHEN Sealed-Produkte (erweitert 2026-09-16 auf Nutzerwunsch:
# "alle englische produkte duerfen gefunden werden auch blister usw.")
POKEMON_KEYWORDS = [
    "elite trainer box",
    "booster display",
    "booster bundle",
    "booster pack",
    "booster box",
    "ultra premium collection",
    "premium collection",
    "collection box",
    "blister",
    "tin",
    "battle deck",
    "theme deck",
    "build & battle",
    "special box",
    "sleeved booster",
    "trainer box",
]
# Sprach-Ausschluss: Titel darf NICHT diese Marker enthalten (nur-englische Produkte, Nutzerwunsch 2026-09-16)
POKEMON_EXCLUDE = [
    "(DE)", "[DE]", " DE ", "Deutsch",
    "(JP)", "[JP]", "Japanisch",
    "(FR)", "[FR]", "Franzosisch",
    "(IT)", "[IT]", "Italienisch",
    "(CN)", "[CN]", "Chinesisch",
    "(KR)", "[KR]", "Koreanisch",
    "- JPN", "- JP", " JPN", "- CHN", "- KOR", "- FRA",
    "Protecc", "Sleeve", "Playmat", "Binder", "Toploader", "Deck Box",
    "PSA", "BGS", "CGC", "graded", "Vinyl Figur", "POP!", "Plueschfigur", "Plüschfigur",
    "Display Case", "Acryl", "Evoretro", "Gehäuse", "Gehaeuse",
]

ONEPIECE_KEYWORDS = [
    "one piece",
]
# erweitert 2026-09-16: alle englischen One-Piece-Sealed-Produkte, nicht nur Booster Display/Bundle
ONEPIECE_MUST_ALSO_CONTAIN = [
    "booster", "display", "bundle", "pack", "box", "deck", "blister", "tin", "collection",
]
# Nur ENGLISCHE One Piece Sealed-Produkte (Nutzerwunsch 2026-09-16), gleiche Logik wie Pokemon
ONEPIECE_EXCLUDE = [
    "(DE)", "[DE]", " DE ", "Deutsch",
    "(JP)", "[JP]", "Japanisch", " JP ",
    "(FR)", "[FR]", "Franzosisch",
    "(IT)", "[IT]", "Italienisch",
    "(CN)", "[CN]", "Chinesisch",
    "(KR)", "[KR]", "Koreanisch",
    "- JPN", "- JP", " JPN", "- CHN", "- KOR", "- FRA",
    "Protecc", "Sleeve", "Playmat", "Binder", "Toploader", "Deck Box", "Dice",
    "PSA", "BGS", "CGC", "graded", "Vinyl Figur", "POP!",
    "Display Case", "Acryl", "Evoretro", "Gehäuse", "Gehaeuse",
]

# Magic: The Gathering: ENGLISCHE UND DEUTSCHE Sealed-Produkte (Nutzerwunsch 2026-09-17,
# im Unterschied zu den anderen Marken hier explizit beide Sprachen gewuenscht).
MTG_KEYWORDS = [
    "magic",
]
MTG_MUST_ALSO_CONTAIN = [
    "booster display", "sammler-booster-display", "sammler-booster", "play-booster-display",
    "play-booster", "jumpstart-booster-display", "jumpstart-booster", "bundle", "commander-decks",
    "commander-deck", "commander deck", "szenenbox", "szenenboxen", "einsteigerbox", "kodex-bundle",
    "booster box", "draft booster", "set booster", "collector booster", "starter kit", "precon",
    "draft night",
]
# Nur JP/FR/IT/CN/KR ausschliessen (nicht DE, da DE explizit erwuenscht ist), plus Zubehoer/Graded
MTG_EXCLUDE = [
    "(JP)", "[JP]", "Japanisch", " JP ",
    "(FR)", "[FR]", "Franzosisch",
    "(IT)", "[IT]", "Italienisch",
    "(CN)", "[CN]", "Chinesisch",
    "(KR)", "[KR]", "Koreanisch",
    "- JPN", "- JP", " JPN", "- CHN", "- KOR", "- FRA",
    "Playmat", "Sleeve", "Album", "Life Counter", "Boulder", "Sidewinder", "Xenoskin",
    "Squire", "Bastion", "Sidekick", "Zip-Up", "Slipcase", "Deck Box", "Binder", "Toploader",
    "PSA", "BGS", "CGC", "graded", "Vinyl Figur", "POP!",
    "Display Case", "Acryl", "Evoretro", "Gehäuse", "Gehaeuse",
]

# Yu-Gi-Oh!: nur ENGLISCHE Sealed-Produkte (Nutzerwunsch 2026-09-17), gleiche Logik wie Dragon Ball
YUGIOH_KEYWORDS = [
    "yu-gi-oh", "yugioh", "yu gi oh",
]
YUGIOH_MUST_ALSO_CONTAIN = [
    "booster", "display", "bundle", "box", "deck", "tin", "collection",
]
YUGIOH_EXCLUDE = [
    "(DE)", "[DE]", " DE ", "Deutsch",
    "(JP)", "[JP]", "Japanisch", " JP ",
    "(FR)", "[FR]", "Franzosisch",
    "(IT)", "[IT]", "Italienisch",
    "(CN)", "[CN]", "Chinesisch",
    "(KR)", "[KR]", "Koreanisch",
    "- JPN", "- JP", " JPN", "- CHN", "- KOR", "- FRA",
    "Protecc", "Sleeve", "Playmat", "Binder", "Toploader", "Deck Box", "Dice",
    "PSA", "BGS", "CGC", "graded", "Vinyl Figur", "POP!",
    "Display Case", "Acryl", "Evoretro", "Gehäuse", "Gehaeuse",
    # Konsolen-/Videospiele faelschlich matchen ausschliessen (z.B. Nintendo DS Titel)
    "Nintendo", "PlayStation", "Xbox", "Videospiel", "Video Game",
]

# Dragon Ball Super Fusion World: nur ENGLISCHE Sealed-Produkte, gleiche Logik wie One Piece
DRAGONBALL_KEYWORDS = [
    "fusion world",
]
DRAGONBALL_MUST_ALSO_CONTAIN = [
    "booster", "display", "bundle", "pack", "box", "deck", "blister", "tin", "collection", "starter",
]
DRAGONBALL_EXCLUDE = [
    "(DE)", "[DE]", " DE ", "Deutsch",
    "(JP)", "[JP]", "Japanisch", " JP ",
    "(FR)", "[FR]", "Franzosisch",
    "(IT)", "[IT]", "Italienisch",
    "(CN)", "[CN]", "Chinesisch",
    "(KR)", "[KR]", "Koreanisch",
    "- JPN", "- JP", " JPN", "- CHN", "- KOR", "- FRA",
    "Protecc", "Sleeve", "Playmat", "Binder", "Toploader", "Deck Box", "Dice",
    "PSA", "BGS", "CGC", "graded", "Vinyl Figur", "POP!",
    "Display Case", "Acryl", "Evoretro", "Gehäuse", "Gehaeuse",
]

STATE_FILE = "state.json"
LOG_FILE = "run.log"

REQUEST_TIMEOUT = 12
