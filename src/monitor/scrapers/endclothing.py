"""END. Clothing（endclothing.com）.

ブランド一覧ページは Next.js の SSR で Algolia の検索結果を
``props.initialProps.pageProps.initialAlgoliaState.results.hits`` に埋め込む。
1リクエストで最大120件、価格と在庫数がまとめて手に入る。

ページ送り:
  ``?page=N``（1始まり）で2ページ目以降も SSR される。``nbPages`` を見て
  必要なぶんだけ追加取得する（``?p=N`` は効かないので注意）。

注意:
  サイズ別在庫は一覧に含まれないので ``stock``（総在庫数）で判定する。
  サイズ単位の再入荷を追いたい場合は商品ページの取得が別途必要。
"""

from __future__ import annotations

from typing import Any

from ..models import Product
from .base import Scraper

_IMAGE_BASE = "https://media.endclothing.com/media/catalog/product"


class EndClothingScraper(Scraper):
    full_coverage = True

    def fetch_products(self) -> list[Product]:
        listing_urls = self.options.get("listing_urls") or []
        if not listing_urls:
            raise RuntimeError("listing_urls が設定されていません")

        price_field = str(self.options.get("price_field", "final_price_1"))
        currency = str(self.options.get("currency", "GBP"))

        max_pages = int(self.options.get("max_pages", 10))
        products: dict[str, Product] = {}

        for url in listing_urls:
            base = self._store_base(url)
            hits, total, pages = self.parse_listing(self.session.get_text(url))
            collected = list(hits)

            for page in range(2, min(pages, max_pages) + 1):
                page_url = self._page_url(url, page)
                try:
                    more, _, _ = self.parse_listing(self.session.get_text(page_url))
                except Exception as exc:  # noqa: BLE001 - 途中のページ失敗で全体を捨てない
                    self.warn(f"{page_url}: {page}ページ目の取得に失敗 ({exc})")
                    break
                if not more:
                    break
                collected.extend(more)

            if total > len(collected):
                self.warn(f"{url}: 全{total}件中{len(collected)}件のみ取得しました")

            for hit in collected:
                product = self._to_product(hit, base, price_field, currency)
                if product is not None:
                    products[product.product_id] = product

        if not products:
            raise RuntimeError("商品を1件も取得できませんでした（サイト構成の変更を疑ってください）")
        return list(products.values())

    # ------------------------------------------------------------------
    @staticmethod
    def _page_url(url: str, page: int) -> str:
        """一覧URLに ``?page=N`` を付ける（既存のクエリは保持する）."""
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

        parts = urlparse(url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "page"]
        query.append(("page", str(page)))
        return urlunparse(parts._replace(query=urlencode(query)))

    @staticmethod
    def parse_listing(html: str) -> tuple[list[dict[str, Any]], int, int]:
        """SSR の Algolia 結果から hits・総件数・総ページ数を取り出す."""
        data = Scraper.next_data(html)
        try:
            results = data["props"]["initialProps"]["pageProps"]["initialAlgoliaState"]["results"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Algolia の検索結果が見つかりません: {exc}") from exc
        hits = results.get("hits") or []
        return hits, int(results.get("nbHits", len(hits))), int(results.get("nbPages", 1))

    @staticmethod
    def _store_base(listing_url: str) -> str:
        """"https://www.endclothing.com/gb/brands/..." -> "https://www.endclothing.com/gb"."""
        parts = listing_url.split("/")
        if len(parts) >= 4:
            return "/".join(parts[:4])
        return "https://www.endclothing.com/gb"

    @classmethod
    def _to_product(
        cls,
        hit: dict[str, Any],
        store_base: str,
        price_field: str,
        currency: str,
    ) -> Product | None:
        object_id = str(hit.get("objectID") or "").strip()
        url_key = str(hit.get("url_key") or "").strip()
        if not object_id or not url_key:
            return None

        raw_price = hit.get(price_field)
        try:
            price = float(raw_price) if raw_price is not None else None
        except (TypeError, ValueError):
            price = None

        try:
            stock_count = int(hit.get("stock", 0))
        except (TypeError, ValueError):
            stock_count = 0

        image = str(hit.get("small_image") or "")
        image_url = f"{_IMAGE_BASE}{image}" if image.startswith("/") else image

        return Product(
            shop_id="endclothing",
            product_id=object_id,
            product_url=f"{store_base}/products/{url_key}",
            product_name=str(hit.get("name") or url_key),
            brand=str(hit.get("brand") or "Our Legacy"),
            price=price,
            currency=currency,
            sizes_in_stock=[],  # 一覧にサイズ別在庫は含まれない
            in_stock=stock_count > 0 and bool(hit.get("for_sale_online", 1)),
            stock_count=stock_count,
            image_url=image_url,
            extra={
                "sku": hit.get("sku", ""),
                "colour": hit.get("actual_colour", ""),
                "category": " > ".join(hit.get("department_hierarchy") or [])[:120],
            },
        )
