"""Offline-Regression fuer den englischen Pokemon-30th-Stock-Kanal.

Kein Netzwerk, keine echten Discord-Posts, keine Secrets. Webhooks sind lokale
Platzhalter auf example.test. requests.get/post duerfen den Rechner nicht verlassen.
"""
import os
import tempfile
import unittest

import check
import config

NOW = "2026-11-06T12:00:00+00:00"
TIN = "Pokémon 30th Anniversary ex Tin (Englisch, Tin)"
MINI = "Pokémon 30th Anniversary Mini (Zufallsmodell)"
TIN_LINK = "https://www.galaxus.ch/de/s5/product/pokemon-30th-anniversary-ex-tin-englisch-tin-72350263"
MINI_LINK = "https://www.galaxus.ch/de/s5/product/pokemon-30th-anniversary-mini-zufallsmodell-72348004"

WEBHOOK_ATTRS = (
    "DISCORD_WEBHOOK_POKEMON",
    "DISCORD_WEBHOOK_POKEMON_PREORDER",
    "DISCORD_WEBHOOK_ONEPIECE",
    "DISCORD_WEBHOOK_ONEPIECE_PREORDER",
    "DISCORD_WEBHOOK_DRAGONBALL",
    "DISCORD_WEBHOOK_DRAGONBALL_PREORDER",
    "DISCORD_WEBHOOK_MTG",
    "DISCORD_WEBHOOK_MTG_PREORDER",
    "DISCORD_WEBHOOK_YUGIOH",
    "DISCORD_WEBHOOK_YUGIOH_PREORDER",
    "DISCORD_WEBHOOK_POKEMON_30TH",
    "DISCORD_WEBHOOK_POKEMON_30TH_PREORDER",
    "DISCORD_WEBHOOK_POKEMON_30TH_ALL",
    "DISCORD_WEBHOOK_POKEMON_30TH_ALL_PREORDER",
)


def direct_stock_html():
    return """
    <script>var i18n = "vorbestellung preorder coming soon"; var flag = {"isPreorder": false};</script>
    <div>Verkauf durch Galaxus</div>
    <div>Release: 1999</div>
    <div>Nur noch 3 Stück an Lager</div>
    <button>In den Warenkorb</button>
    """


class FakeResp:
    def __init__(self, body):
        self._body = body

    def text(self):
        return self._body


class FakePage:
    def __init__(self, body="", get_body=None, get_exc=None, goto_exc=None):
        self.body = body
        self.get_body = body if get_body is None else get_body
        self.get_exc = get_exc
        self.goto_exc = goto_exc
        self.gets = 0
        self.gotos = 0
        self.request = self

    def get(self, link, timeout=None):
        self.gets += 1
        if self.get_exc:
            raise self.get_exc
        return FakeResp(self.get_body)

    def goto(self, link, timeout=None, wait_until=None):
        self.gotos += 1
        if self.goto_exc:
            raise self.goto_exc

    def wait_for_timeout(self, ms):
        return None

    def content(self):
        return self.body


class ChannelFilterTests(unittest.TestCase):
    def setUp(self):
        self._log_path = check.LOG_PATH
        self._state_path = check.STATE_PATH
        self._tmpdir = tempfile.mkdtemp()
        check.LOG_PATH = os.path.join(self._tmpdir, "run.log")
        self._hooks = {name: getattr(config, name) for name in WEBHOOK_ATTRS}
        for name in WEBHOOK_ATTRS:
            setattr(config, name, "https://example.test/hook/" + name)
        self.posts = []
        self.network_calls = 0
        self._post = check.requests.post
        self._get = check.requests.get
        self._dry = check.DRY_RUN

        def fake_post(url, json=None, timeout=None):
            host = str(url or "").lower()
            if "discord.com" in host or "discordapp.com" in host or not host.startswith("https://example.test/hook/"):
                raise AssertionError("outbound webhook blocked")
            self.posts.append((url, (json or {}).get("content", "")))

            class Resp:
                status_code = 204
                text = ""

            return Resp()

        def fake_get(*args, **kwargs):
            self.network_calls += 1
            raise RuntimeError("network disabled")

        check.requests.post = fake_post
        check.requests.get = fake_get
        check.DRY_RUN = False

    def tearDown(self):
        check.requests.post = self._post
        check.requests.get = self._get
        check.DRY_RUN = self._dry
        check.LOG_PATH = self._log_path
        check.STATE_PATH = self._state_path
        for name, value in self._hooks.items():
            setattr(config, name, value)
        for url, _content in self.posts:
            self.assertTrue(str(url).startswith("https://example.test/hook/"))
            self.assertNotIn("discord.com", str(url).lower())
            self.assertNotIn("discordapp.com", str(url).lower())

    def msgs(self, attr):
        url = getattr(config, attr)
        return [content for posted, content in self.posts if posted == url]

    def assert_no_en_stock(self):
        self.assertEqual(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH"), [])

    def run_galaxus(self, products, page, state=None):
        state = {} if state is None else state
        original = check.fetch_stealth_category_products

        def fake_fetch(page_obj, category_url, selector, max_clicks=8):
            if "pokemon-karten" not in category_url:
                return []
            return products

        check.fetch_stealth_category_products = fake_fetch
        try:
            check.check_stealth_category_retailers(page, state, NOW)
        finally:
            check.fetch_stealth_category_products = original
        return state

    def test_classifier_direct_third_unknown_and_errors(self):
        direct = check.classify_galaxus_offer_text(direct_stock_html())
        self.assertEqual(direct["seller"], "direct")
        self.assertEqual(direct["availability"], "instock")
        self.assertIsNone(direct["error"])

        third = check.classify_galaxus_offer_text(
            "<div>Verkauf durch PokeShop GmbH</div><div>12 Stück an Lager</div>"
        )
        self.assertEqual(third["seller"], "third_party")
        self.assertEqual(third["availability"], "instock")

        unknown = check.classify_galaxus_offer_text(
            "<title>galaxus.ch</title><a>Galaxus</a><p>Nur noch 4 Stück an Lager</p>"
        )
        self.assertEqual(unknown["seller"], "unknown")
        self.assertEqual(unknown["availability"], "instock")

        ambiguous = check.classify_galaxus_offer_text(
            "<div>Verkauf durch Galaxus</div><div>Angebot vom Drittanbieter</div><div>2 Stück an Lager</div>"
        )
        self.assertEqual(ambiguous["seller"], "unknown")

        bare = check.classify_galaxus_offer_text("<div>Verkauf durch Galaxus</div><div>verfügbar</div>")
        self.assertEqual(bare["seller"], "direct")
        self.assertEqual(bare["availability"], "unknown")

        self.assertEqual(check.classify_galaxus_offer_text("")["error"], "empty")
        self.assertEqual(check.classify_galaxus_offer_text("   ")["error"], "empty")
        parsed = check.classify_galaxus_offer_text(None)
        self.assertEqual(parsed["seller"], "unknown")
        self.assertEqual(parsed["error"], "empty")

        json_direct = check.classify_galaxus_offer_text(
            '<script>{"supplierName":"Galaxus","stockStatus":"instock","isPreorder":false}</script>'
        )
        self.assertEqual(json_direct["seller"], "direct")
        self.assertEqual(json_direct["availability"], "instock")

        json_third = check.classify_galaxus_offer_text(
            '<script>{"merchantName":"Random Cards AG"}</script><div>3 Stück an Lager</div>'
        )
        self.assertEqual(json_third["seller"], "third_party")

        override = check.classify_galaxus_offer_text(
            '<script>{"supplierName":"Digitec Galaxus AG","isPreorder":true}</script>'
            "<div>Nur noch 2 Stück an Lager</div>"
        )
        self.assertEqual(override["seller"], "direct")
        self.assertEqual(override["availability"], "preorder")

        mixed = check.classify_galaxus_offer_text(
            '<script>{"supplierName":"Galaxus","merchantName":"Other Cards"}</script>'
            "<div>1 Stück an Lager</div>"
        )
        self.assertEqual(mixed["seller"], "unknown")

    def test_classifier_preorder_announced_upcoming_unavailable(self):
        samples = {
            "<div>Verkauf durch Galaxus</div><div>Vorbestellung – erscheint am 20.11.2026</div>": "preorder",
            "<div>Verkauf durch Galaxus</div><div>In einigen Tagen verfügbar</div>": "preorder",
            "<div>Verkauf durch Galaxus</div><div>voraussichtlich lieferbar</div>": "preorder",
            "<div>Verkauf durch Galaxus</div><div>Noch nicht erschienen</div>": "announced",
            "<div>Verkauf durch Galaxus</div><div>Bald verfügbar</div>": "announced",
            "<div>Verkauf durch Galaxus</div><div>Derzeit ausverkauft</div>": "unavailable",
            "<div>Verkauf durch Galaxus</div><div>nicht lieferbar</div>": "unavailable",
        }
        for html, availability in samples.items():
            verdict = check.classify_galaxus_offer_text(html)
            self.assertEqual(verdict["seller"], "direct", html)
            self.assertEqual(verdict["availability"], availability, html)
            self.assertNotEqual(verdict["availability"], "instock", html)

    def test_screenshot_tin_direct_stock_posts_restock_once(self):
        page = FakePage(direct_stock_html())
        product = {
            "title": TIN,
            "link": TIN_LINK,
            "price": "?",
            "in_stock": True,
            "preorder": False,
        }
        state = self.run_galaxus([product], page)
        stock = self.msgs("DISCORD_WEBHOOK_POKEMON_30TH")
        self.assertEqual(len(stock), 1)
        self.assertIn("RESTOCK", stock[0])
        self.assertIn("72350263", stock[0])
        self.assertIn("Preis: CHF ?", stock[0])
        self.assertNotIn("VORBESTELLUNG", stock[0])
        self.assertEqual(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_PREORDER"), [])
        self.assertEqual(state["galaxus.ch:72350263:pokemon30th"]["status"], "instock")
        # Haupt-Pokemon-Kanal bleibt unangetastet und darf denselben Direktkauf melden.
        self.assertEqual(len(self.msgs("DISCORD_WEBHOOK_POKEMON")), 1)
        self.assertIn("RESTOCK", self.msgs("DISCORD_WEBHOOK_POKEMON")[0])

        product["price"] = "12.00"
        self.run_galaxus([product], page, state)
        self.assertEqual(len(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH")), 1)

    def test_screenshot_tin_third_party_blocks_only_en_channel(self):
        page = FakePage("<div>Verkauf durch PokeShop GmbH</div><div>8 Stück an Lager</div>")
        state = self.run_galaxus(
            [{"title": TIN, "link": TIN_LINK, "price": "?", "in_stock": True, "preorder": False}],
            page,
        )
        self.assert_no_en_stock()
        self.assertEqual(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_PREORDER"), [])
        self.assertEqual(state["galaxus.ch:72350263:pokemon30th"]["status"], "marketplace")
        self.assertTrue(self.msgs("DISCORD_WEBHOOK_POKEMON"))
        self.assertTrue(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_ALL"))
        self.assertIn("RESTOCK", self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_ALL")[0])

    def test_unknown_seller_and_bare_available_fail_closed(self):
        page = FakePage("<title>galaxus.ch</title><a>Galaxus</a><p>verfügbar</p>")
        state = self.run_galaxus(
            [{"title": TIN, "link": TIN_LINK, "price": "?", "in_stock": True, "preorder": False}],
            page,
        )
        self.assert_no_en_stock()
        self.assertEqual(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_PREORDER"), [])
        self.assertEqual(state["galaxus.ch:72350263:pokemon30th"]["status"], "unverified")
        self.assertTrue(self.msgs("DISCORD_WEBHOOK_POKEMON"))

    def test_ambiguous_seller_fails_closed_for_en_only(self):
        page = FakePage(
            "<div>Verkauf durch Galaxus</div><div>vom Drittanbieter</div><div>2 Stück an Lager</div>"
        )
        state = self.run_galaxus(
            [{"title": TIN, "link": TIN_LINK, "price": "?", "in_stock": True, "preorder": False}],
            page,
        )
        self.assert_no_en_stock()
        self.assertEqual(state["galaxus.ch:72350263:pokemon30th"]["status"], "unverified")
        self.assertTrue(self.msgs("DISCORD_WEBHOOK_POKEMON"))

    def test_exact_third_party_phrase_still_skips_every_brand(self):
        page = FakePage("<div>Nur noch 2 Stück an Lager beim Drittanbieter</div>")
        state = self.run_galaxus(
            [{"title": TIN, "link": TIN_LINK, "price": "?", "in_stock": True, "preorder": False}],
            page,
        )
        self.assertEqual(self.posts, [])
        self.assertNotIn("galaxus.ch:72350263:pokemon30th", state)
        self.assertTrue(check.galaxus_is_third_party(page, TIN_LINK))
        broken = FakePage(get_exc=TimeoutError("down"), goto_exc=TimeoutError("down"))
        self.assertFalse(check.galaxus_is_third_party(broken, TIN_LINK))

    def test_fetch_and_parse_errors_fail_closed_without_en_state(self):
        page = FakePage(get_exc=TimeoutError("down"), goto_exc=TimeoutError("down"))
        state = self.run_galaxus(
            [{"title": MINI, "link": MINI_LINK, "price": "?", "in_stock": True, "preorder": False}],
            page,
        )
        self.assert_no_en_stock()
        self.assertNotIn("galaxus.ch:72348004:pokemon30th", state)
        self.assertIn("galaxus.ch:72348004:pokemon30thall", state)
        self.assertTrue(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_ALL"))

        empty = FakePage(body="   ")
        state_empty = self.run_galaxus(
            [{"title": MINI, "link": MINI_LINK, "price": "?", "in_stock": True, "preorder": False}],
            empty,
        )
        self.assertNotIn("galaxus.ch:72348004:pokemon30th", state_empty)

        parsed = FakePage(get_body=123, body=direct_stock_html())
        state_goto = self.run_galaxus(
            [{"title": TIN, "link": TIN_LINK, "price": "?", "in_stock": True, "preorder": False}],
            parsed,
        )
        self.assertEqual(len(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH")), 1)
        self.assertGreaterEqual(parsed.gotos, 1)
        self.assertEqual(state_goto["galaxus.ch:72350263:pokemon30th"]["status"], "instock")

    def test_screenshot_mini_preorder_uses_preorder_route_not_stock(self):
        page = FakePage("<div>Verkauf durch Galaxus</div><div>Vorbestellung</div>")
        state = self.run_galaxus(
            [{"title": MINI, "link": MINI_LINK, "price": "?", "in_stock": False, "preorder": True}],
            page,
        )
        pre = self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_PREORDER")
        self.assertEqual(len(pre), 1)
        self.assertIn("VORBESTELLUNG", pre[0])
        self.assertIn("72348004", pre[0])
        self.assertNotIn("RESTOCK", pre[0])
        self.assert_no_en_stock()
        self.assertEqual(state["galaxus.ch:72348004:pokemon30th"]["status"], "preorder")
        self.assertTrue(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_ALL_PREORDER"))

    def test_wrong_restock_label_is_rerouted_for_en_channel(self):
        pages = {
            "preorder": "<div>Verkauf durch Galaxus</div><div>Vorbestellung – erscheint am 20.11.2026</div>",
            "upcoming": "<div>Verkauf durch Galaxus</div><div>In einigen Tagen verfügbar</div>",
            "announced": "<div>Verkauf durch Galaxus</div><div>Noch nicht erschienen</div>",
            "unavailable": "<div>Verkauf durch Galaxus</div><div>Derzeit ausverkauft</div>",
        }
        expected = {
            "preorder": "VORBESTELLUNG",
            "upcoming": "VORBESTELLUNG",
            "announced": "ANKUENDIGUNG",
            "unavailable": None,
        }
        for name, html in pages.items():
            with self.subTest(name=name):
                self.posts.clear()
                state = self.run_galaxus(
                    [{"title": MINI, "link": MINI_LINK, "price": "?", "in_stock": True, "preorder": False}],
                    FakePage(html),
                )
                self.assert_no_en_stock()
                pre = self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_PREORDER")
                if expected[name] is None:
                    self.assertEqual(pre, [])
                    self.assertEqual(state["galaxus.ch:72348004:pokemon30th"]["status"], "outofstock")
                else:
                    self.assertEqual(len(pre), 1)
                    self.assertIn(expected[name], pre[0])
                    self.assertNotIn("RESTOCK", pre[0])
                # Andere Kategorie behaelt die Kachel-Einstufung.
                self.assertTrue(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_ALL"))
                self.assertIn("RESTOCK", self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_ALL")[0])

    def test_initial_state_change_and_price_do_not_resend(self):
        offer = {"seller": "direct", "availability": "instock", "error": None}
        state = {}
        check.maybe_notify_pokemon30th(
            state, "galaxus.ch:72350263", TIN, "outofstock", "galaxus.ch", TIN_LINK, "?", NOW,
        )
        self.assert_no_en_stock()
        check.maybe_notify_pokemon30th(
            state, "galaxus.ch:72350263", TIN, "instock", "galaxus.ch", TIN_LINK, "?", NOW,
            galaxus_offer=offer,
        )
        self.assertEqual(len(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH")), 1)
        check.maybe_notify_pokemon30th(
            state, "galaxus.ch:72350263", TIN, "instock", "galaxus.ch", TIN_LINK, "15.00", NOW,
            galaxus_offer=None,
        )
        self.assertEqual(len(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH")), 1)
        self.assertEqual(state["galaxus.ch:72350263:pokemon30th"]["status"], "instock")

    def test_direct_notify_guard_blocks_unproven_galaxus_restock(self):
        state = {}
        check.notify_status_change(
            state, "galaxus.ch:72350263:pokemon30th", TIN, "instock", None,
            "galaxus.ch", TIN_LINK, "?", "pokemon30th", NOW, galaxus_offer=None,
        )
        self.assert_no_en_stock()
        check.notify_status_change(
            state, "cardmaniac.ch:1:pokemon30th", "Pokemon 30th Anniversary Tin", "instock", None,
            "cardmaniac.ch", "https://example.test/p", "20", "pokemon30th", NOW,
        )
        self.assertEqual(len(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH")), 1)
        self.assertIn("cardmaniac.ch", self.msgs("DISCORD_WEBHOOK_POKEMON_30TH")[0])

    def test_other_shops_preorder_route_and_title_mislabels(self):
        state = {}
        check.maybe_notify_pokemon30th(
            state, "cardmaniac.ch:1", "Pokemon 30th Anniversary Elite Trainer Box", "preorder",
            "cardmaniac.ch", "https://example.test/pre", "40", NOW,
        )
        self.assert_no_en_stock()
        self.assertIn("VORBESTELLUNG", self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_PREORDER")[0])

        check.maybe_notify_pokemon30th(
            state, "amazingtoys.ch:2", "Pokemon 30th Anniversary Tin", "announced",
            "amazingtoys.ch", "https://example.test/ann", "30", NOW,
        )
        self.assertTrue(any("ANKUENDIGUNG" in msg for msg in self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_PREORDER")))
        self.assert_no_en_stock()

        check.maybe_notify_pokemon30th(
            state, "ryuland.ch:3", "Pokemon 30th Anniversary Tin Vorbestellung", "instock",
            "ryuland.ch", "https://example.test/ryu", "10", NOW,
        )
        self.assert_no_en_stock()
        self.assertTrue(any("VORBESTELLUNG" in msg and "ryuland.ch" in msg for msg in self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_PREORDER")))

        check.notify_status_change(
            {}, "op:1", "One Piece Booster Display", "preorder", None,
            "cardmaniac.ch", "https://example.test/op", "90", "onepiece", NOW,
        )
        self.assertEqual(len(self.msgs("DISCORD_WEBHOOK_ONEPIECE_PREORDER")), 1)
        self.assertEqual(self.msgs("DISCORD_WEBHOOK_ONEPIECE"), [])
        check.notify_status_change(
            {}, "pk:1", "Pokemon Elite Trainer Box", "instock", None, "galaxus.ch",
            "https://example.test/g", "5", "pokemon", NOW,
            galaxus_offer={"seller": "third_party", "availability": "instock", "error": None},
        )
        self.assertEqual(len(self.msgs("DISCORD_WEBHOOK_POKEMON")), 1)

    def test_german_30th_channel_is_not_filtered(self):
        page = FakePage("<div>Verkauf durch PokeShop GmbH</div><div>5 Stück an Lager</div>")
        self.run_galaxus(
            [{
                "title": "Pokemon 30 Jahre Booster Bundle",
                "link": "https://www.galaxus.ch/de/s5/product/pokemon-30-jahre-booster-bundle-70000001",
                "price": "20",
                "in_stock": True,
                "preorder": False,
            }],
            page,
        )
        self.assert_no_en_stock()
        self.assertEqual(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_PREORDER"), [])
        self.assertTrue(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_ALL"))
        self.assertIn("RESTOCK", self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_ALL")[0])

    def test_priority_path_blocks_preorders_and_keeps_other_brands(self):
        self.assertEqual(self.network_calls, 0)
        original_fetch = check.fetch_products
        original_soft = check.fetch_softridge_products
        original_spiel = check.fetch_spielezar_products
        original_priority = check.PRIORITY_ONLY
        original_state = check.STATE_PATH
        check.STATE_PATH = os.path.join(self._tmpdir, "state.json")
        check.PRIORITY_ONLY = True
        check.fetch_softridge_products = lambda domain: []
        check.fetch_spielezar_products = lambda domain: []

        def fetch_products(domain):
            if domain != "ryuland.ch":
                return []
            return [
                {
                    "id": 72350263,
                    "title": "Pokemon 30th Anniversary ex Tin",
                    "handle": "ex-tin",
                    "tags": ["preorder"],
                    "product_type": "",
                    "variants": [{"id": 1, "available": True, "price": "19.90"}],
                },
                {
                    "id": 2,
                    "title": "Pokemon 30th Anniversary Tin",
                    "handle": "tin",
                    "tags": [],
                    "product_type": "",
                    "variants": [{"id": 2, "available": True, "price": "21.00"}],
                },
                {
                    "id": 3,
                    "title": "Pokemon Elite Trainer Box",
                    "handle": "etb",
                    "tags": [],
                    "product_type": "",
                    "variants": [{"id": 3, "available": True, "price": "49.00"}],
                },
                {
                    "id": 4,
                    "title": "Pokemon 30th Anniversary Mini Tin Vorbestellung",
                    "handle": "mini",
                    "tags": [],
                    "product_type": "",
                    "variants": [{"id": 4, "available": True, "price": "9.00"}],
                },
                {
                    "id": 5,
                    "title": "One Piece Booster Display Pre-Order",
                    "handle": "op",
                    "tags": [],
                    "product_type": "",
                    "variants": [{"id": 5, "available": True, "price": "90"}],
                },
            ]

        check.fetch_products = fetch_products
        try:
            check.run()
        finally:
            check.fetch_products = original_fetch
            check.fetch_softridge_products = original_soft
            check.fetch_spielezar_products = original_spiel
            check.PRIORITY_ONLY = original_priority
            check.STATE_PATH = original_state

        self.assertEqual(self.network_calls, 0)
        en_stock = self.msgs("DISCORD_WEBHOOK_POKEMON_30TH")
        self.assertEqual(len(en_stock), 1)
        self.assertIn("Pokemon 30th Anniversary Tin", en_stock[0])
        self.assertNotIn("Vorbestellung", en_stock[0])
        self.assertNotIn("ex Tin", en_stock[0])
        en_pre = "\n".join(self.msgs("DISCORD_WEBHOOK_POKEMON_30TH_PREORDER"))
        self.assertIn("VORBESTELLUNG", en_pre)
        self.assertIn("ex Tin", en_pre)
        self.assertIn("Vorbestellung", en_pre)
        self.assertTrue(any("Elite Trainer Box" in msg for msg in self.msgs("DISCORD_WEBHOOK_POKEMON")))
        self.assertEqual(self.msgs("DISCORD_WEBHOOK_ONEPIECE"), [])
        self.assertEqual(len(self.msgs("DISCORD_WEBHOOK_ONEPIECE_PREORDER")), 1)


if __name__ == "__main__":
    unittest.main()
