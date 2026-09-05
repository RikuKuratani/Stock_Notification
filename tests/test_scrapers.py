"""実サイトのレスポンスから作った縮小フィクスチャでパースを検証する."""

import pytest
from conftest import fixture

from monitor.config import ShopConfig
from monitor.scrapers.endclothing import EndClothingScraper
from monitor.scrapers.jsonld import iter_products, offer_of, parse_availability, parse_price
from monitor.scrapers.ourlegacy import OurLegacyScraper


class FakeSession:
    """URL -> 本文 の辞書を返すだけのセッション."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []
        self.request_count = 0

    def get_text(self, url: str, **kwargs) -> str:
        self.requested.append(url)
        self.request_count += 1
        try:
            return self.pages[url]
        except KeyError:
            raise RuntimeError(f"unexpected URL: {url}") from None


# ----------------------------------------------------------------------
# Our Legacy 公式
# ----------------------------------------------------------------------
def test_ourlegacy_parses_price_sizes_and_stock():
    product = OurLegacyScraper.parse_product_page(
        fixture("ourlegacy_product.html"),
        "https://www.ourlegacy.com/elongated-longsleeve-cloudbone-ripple-rib",
        "ourlegacy",
    )
    assert product is not None
    assert product.product_id == "elongated-longsleeve-cloudbone-ripple-rib"
    assert product.price == 260.0
    assert product.currency == "EUR"          # "260.00 EUR" から抽出する
    assert product.brand == "Our Legacy"
    assert product.in_stock is True
    # stock が "no" のサイズは在庫ありに含めない
    assert product.sizes_in_stock
    assert all(size for size in product.sizes_in_stock)
    assert product.extra["sku"]


def test_ourlegacy_sold_out_product_is_not_in_stock():
    import json
    import re

    html = fixture("ourlegacy_product.html")
    data = json.loads(re.search(r'>(\{.*\})</script>', html, re.S).group(1))
    raw = data["props"]["pageProps"]["pageProps"]["centra"]["product"]
    for size in raw["sizes"]:
        size["stock"] = "no"
    sold_out = html.replace(
        re.search(r'>(\{.*\})</script>', html, re.S).group(1), json.dumps(data)
    )
    product = OurLegacyScraper.parse_product_page(sold_out, "https://x/y", "ourlegacy")
    assert product.in_stock is False
    assert product.sizes_in_stock == []


def test_ourlegacy_prioritises_unseen_then_stale():
    shop = ShopConfig(id="ourlegacy", name="OL", scraper="ourlegacy", options={"watchlist": ["camion"]})
    scraper = OurLegacyScraper(
        shop,
        FakeSession({}),
        known_last_checked={"old-item": "2026-01-01T00:00:00+09:00",
                            "fresh-item": "2026-09-05T00:00:00+09:00",
                            "camion-bag": "2026-09-05T00:00:00+09:00"},
    )
    ordered = scraper._prioritise(
        ["https://x/fresh-item", "https://x/old-item", "https://x/brand-new", "https://x/camion-bag"],
        ["camion"],
    )
    assert ordered[0].endswith("brand-new")   # 未取得が最優先
    assert ordered[1].endswith("camion-bag")  # 次にウォッチリスト
    assert ordered[2].endswith("old-item")    # あとは最終確認が古い順
    assert ordered[3].endswith("fresh-item")


def test_ourlegacy_missing_next_data_raises():
    with pytest.raises(ValueError, match="__NEXT_DATA__"):
        OurLegacyScraper.parse_product_page("<html><body>nope</body></html>", "https://x/y", "ourlegacy")


# ----------------------------------------------------------------------
# END. Clothing
# ----------------------------------------------------------------------
def test_endclothing_parses_listing_and_follows_pagination():
    base = "https://www.endclothing.com/gb/brands/our-legacy"
    session = FakeSession({
        base: fixture("endclothing_listing.html"),
        f"{base}?page=2": fixture("endclothing_listing_page2.html"),
    })
    shop = ShopConfig(
        id="endclothing", name="END.", scraper="endclothing",
        options={"listing_urls": [base], "price_field": "final_price_1", "currency": "GBP"},
    )
    products = EndClothingScraper(shop, session).fetch_products()

    assert session.requested == [base, f"{base}?page=2"]
    assert len(products) == 4  # 1ページ目3件 + 2ページ目1件
    first = products[0]
    assert first.currency == "GBP"
    assert first.price and first.price > 0
    assert first.product_url.startswith("https://www.endclothing.com/gb/products/")
    assert first.image_url.startswith("https://media.endclothing.com/")
    # stock が 0 の商品は在庫なし
    out_of_stock = [p for p in products if p.product_id == "9999001"]
    assert out_of_stock and out_of_stock[0].in_stock is False


def test_endclothing_page_url_keeps_existing_query():
    assert (
        EndClothingScraper._page_url("https://e.com/gb/brands/x?foo=1&page=9", 3)
        == "https://e.com/gb/brands/x?foo=1&page=3"
    )


def test_endclothing_empty_result_raises():
    base = "https://www.endclothing.com/gb/brands/our-legacy"
    empty = '<script id="__NEXT_DATA__" type="application/json">{"props":{"initialProps":{"pageProps":{"initialAlgoliaState":{"results":{"hits":[],"nbHits":0,"nbPages":1}}}}}}</script>'
    shop = ShopConfig(id="endclothing", name="END.", scraper="endclothing",
                      options={"listing_urls": [base]})
    with pytest.raises(RuntimeError, match="1件も取得できませんでした"):
        EndClothingScraper(shop, FakeSession({base: empty})).fetch_products()


# ----------------------------------------------------------------------
# JSON-LD 汎用パーサ（SSENSE / Farfetch / MR PORTER 用）
# ----------------------------------------------------------------------
JSONLD_HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ItemList","itemListElement":[
 {"@type":"Product","name":"Camion Pants","url":"https://www.ssense.com/en-us/men/product/our-legacy/camion-pants/12345678",
  "sku":"OL-CAMION","brand":{"@type":"Brand","name":"Our Legacy"},"image":"https://img/x.jpg",
  "offers":{"@type":"Offer","price":"395.00","priceCurrency":"USD","availability":"https://schema.org/InStock"}},
 {"@type":"Product","name":"Box Shirt","url":"https://www.ssense.com/en-us/men/product/our-legacy/box-shirt/87654321",
  "brand":"Our Legacy",
  "offers":{"@type":"AggregateOffer","lowPrice":"280","priceCurrency":"USD","availability":"https://schema.org/OutOfStock"}}]}
</script></head><body></body></html>
"""


def test_jsonld_extracts_products_prices_and_availability():
    products = list(iter_products(JSONLD_HTML))
    assert len(products) == 2

    first = offer_of(products[0])
    assert parse_price(first) == 395.0
    assert parse_availability(first) is True

    second = offer_of(products[1])
    assert parse_price(second) == 280.0
    assert parse_availability(second) is False


def test_ssense_uses_numeric_url_id():
    from monitor.scrapers.blocked_sites import SsenseScraper

    shop = ShopConfig(id="ssense", name="SSENSE", scraper="ssense",
                      options={"listing_urls": ["https://www.ssense.com/x"], "currency": "USD"})
    scraper = SsenseScraper(shop, FakeSession({"https://www.ssense.com/x": JSONLD_HTML}))
    products = scraper.fetch_products()
    assert {p.product_id for p in products} == {"12345678", "87654321"}
    assert products[0].brand == "Our Legacy"


def test_jsonld_scraper_raises_when_blocked():
    from monitor.scrapers.blocked_sites import FarfetchScraper

    shop = ShopConfig(id="farfetch", name="Farfetch", scraper="farfetch",
                      options={"listing_urls": ["https://ff/x"]})
    scraper = FarfetchScraper(shop, FakeSession({"https://ff/x": "<html>403</html>"}))
    with pytest.raises(RuntimeError, match="Bot対策"):
        scraper.fetch_products()
