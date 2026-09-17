# CHAU Restock-Monitor Konfiguration
import os

# Discord-Webhooks: EIN Hauptserver "CHAU" (Kategorien pro Sub-Marke), hier Kategorie
# "Chau's Hype World" -> Kanaele #pokemon-restock-en / #onepiece-restock-en
# (siehe Memory project_restock_monitor_bot.md)
# Werden AUSSCHLIESSLICH aus Umgebungsvariablen gelesen (Windows-Umgebungsvariablen lokal,
# GitHub Actions Secrets in der Cloud) - damit die echten URLs NIE im (oeffentlichen) Git-Repo
# landen. Ohne gesetzte Variablen bleibt der Wert leer und send_discord() ueberspringt den Post.
DISCORD_WEBHOOK_POKEMON = os.environ.get("DISCORD_WEBHOOK_POKEMON", "")
DISCORD_WEBHOOK_ONEPIECE = os.environ.get("DISCORD_WEBHOOK_ONEPIECE", "")
# Separate Kanaele fuer Vorbestellungen (Nutzerwunsch 2026-09-16 #3), gleiche Kategorie "Chau's Hype World"
DISCORD_WEBHOOK_POKEMON_PREORDER = os.environ.get("DISCORD_WEBHOOK_POKEMON_PREORDER", "")
DISCORD_WEBHOOK_ONEPIECE_PREORDER = os.environ.get("DISCORD_WEBHOOK_ONEPIECE_PREORDER", "")

# Marker, die auf eine Vorbestellung (statt sofort verfuegbar) hinweisen
PREORDER_MARKERS = [
    "vorbestellung", "vorbestellen", "vorbesteller", "pre-order", "preorder",
    "pre order", "coming soon", "release date", "erscheint am", "erscheinungsdatum",
]

# Schweizer Haendler, bei denen ein Shopify-Standard-Endpoint (/products.json) probiert wird.
RETAILERS = [
    "cardmaniac.ch",
    "zadoys.ch",
    "ryuland.ch",
    "zockbar.ch",
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
]

# Nicht (mehr) erreichbar/kein passender Endpoint gefunden, Stand 2026-09-17:
# goodgames.ch (SSL-Zertifikat zeigt auf falsche/fremde Domain, Seite technisch kaputt) -
# siehe Memory project_restock_monitor_bot.md

# Grosse Haendler, die per echtem Browser (Playwright) geprueft werden, da sie
# kein einfaches /products.json haben. Jeder Eintrag: Name, Such-URL-Template
# (mit {query} Platzhalter), CSS-Selektor fuer Produktlinks.
# Stand 2026-09-17: Coop, Coop City, Interdiscount, Microspot, Mueller, Digitec, Brack,
# World of Games, Franz Carl Weber konnten NICHT eingebunden werden (starker
# Bot-Schutz blockt auch echten Headless-Browser, oder Seite technisch nicht
# erreichbar/falsche URL-Struktur) - siehe Memory project_restock_monitor_bot.md.
BROWSER_RETAILERS = [
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
]

STATE_FILE = "state.json"
LOG_FILE = "run.log"

REQUEST_TIMEOUT = 12
