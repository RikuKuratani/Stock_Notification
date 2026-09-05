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


def test_jsonld_scraper_paginates_and_stops_when_exhausted():
    """新しい商品が出てこなくなったらページ送りを止める."""
    from monitor.scrapers.blocked_sites import SsenseScraper

    page1 = JSONLD_HTML
    page2 = JSONLD_HTML.replace("12345678", "11111111").replace("87654321", "22222222")
    session = FakeSession({
        "https://s/x": page1,
        "https://s/x?page=2": page2,
        "https://s/x?page=3": page2,  # 重複（1回だけなら続行する）
        "https://s/x?page=4": page2,  # 2回連続で新規なし = ここで打ち切る
        "https://s/x?page=5": page1,
    })
    shop = ShopConfig(id="ssense", name="SSENSE", scraper="ssense",
                      options={"listing_urls": ["https://s/x"], "max_pages": 5})
    products = SsenseScraper(shop, session).fetch_products()

    assert len(session.requested) == 4          # 4ページ目で打ち切る
    assert len(products) == 4


def test_jsonld_scraper_tolerates_one_duplicate_page():
    """1ページ分の重複で止めると取りこぼすため、2ページ連続まで許容する."""
    from monitor.scrapers.blocked_sites import SsenseScraper

    page1 = JSONLD_HTML
    page3 = JSONLD_HTML.replace("12345678", "33333333").replace("87654321", "44444444")
    session = FakeSession({
        "https://s/x": page1,
        "https://s/x?page=2": page1,   # 同じページが返ってきた
        "https://s/x?page=3": page3,   # その先にまだ商品がある
        "https://s/x?page=4": page3,
        "https://s/x?page=5": page3,
    })
    shop = ShopConfig(id="ssense", name="SSENSE", scraper="ssense",
                      options={"listing_urls": ["https://s/x"], "max_pages": 5})
    products = SsenseScraper(shop, session).fetch_products()

    assert {p.product_id for p in products} == {"12345678", "87654321", "33333333", "44444444"}


def test_jsonld_scraper_warns_when_page_budget_is_used_up():
    from monitor.scrapers.blocked_sites import SsenseScraper

    pages = {"https://s/x": JSONLD_HTML}
    for n in (2, 3):
        pages[f"https://s/x?page={n}"] = JSONLD_HTML.replace("1234", f"{n}999").replace("8765", f"{n}888")
    shop = ShopConfig(id="ssense", name="SSENSE", scraper="ssense",
                      options={"listing_urls": ["https://s/x"], "max_pages": 3})
    scraper = SsenseScraper(shop, FakeSession(pages))
    scraper.fetch_products()
    assert any("打ち切りました" in w for w in scraper.warnings)


def test_farfetch_reads_url_from_offers():
    """Farfetch は商品URLを offers の中にしか持たない."""
    from monitor.scrapers.blocked_sites import FarfetchScraper

    html = """<script type="application/ld+json">
    {"@context":"https://schema.org","@type":"ItemList","numberOfItems":1,"itemListElement":[
     {"@type":"Product","position":"1","name":"\\u30ec\\u30b6\\u30fc\\u30d6\\u30fc\\u30c4",
      "image":["https://cdn/x.jpg"],"brand":{"@type":"Brand","name":"OUR LEGACY"},
      "offers":{"@type":"Offer","price":75800,"priceCurrency":"JPY",
       "url":"/jp/shopping/men/our-legacy-boots-item-31083391.aspx",
       "availability":"https://schema.org/InStock"}}]}
    </script>"""
    shop = ShopConfig(id="farfetch", name="Farfetch", scraper="farfetch",
                      options={"listing_urls": ["https://www.farfetch.com/shopping/men/our-legacy/items.aspx"]})
    products = FarfetchScraper(shop, FakeSession({
        "https://www.farfetch.com/shopping/men/our-legacy/items.aspx": html,
    })).fetch_products()

    assert len(products) == 1
    product = products[0]
    assert product.product_id == "31083391"
    assert product.price == 75800.0 and product.currency == "JPY"
    assert product.in_stock is True
    # 相対URLは一覧URLを基準に絶対化する
    assert product.product_url == "https://www.farfetch.com/jp/shopping/men/our-legacy-boots-item-31083391.aspx"
