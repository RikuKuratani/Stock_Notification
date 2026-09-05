"""JSON-LD（schema.org）ベースの汎用一覧スクレイパー.

SSENSE と Farfetch は、``config.yml`` の ``impersonate`` でブラウザのTLS挙動を
再現すると取得できる（2026-09-05 実測）。どちらもSEOのため商品一覧に
``application/ld+json`` を出力しており、価格・在庫・商品URLが揃っている。

  - SSENSE  : ``@type: Product`` を1商品ずつ、1ページ120件
  - Farfetch: ``@type: ItemList`` の中に Product、1ページ96件、価格はJPY

MR PORTER だけは偽装しても JavaScript チャレンジのページが返るため未対応
（仕様書フェーズ3）。

サイトごとの差分（商品IDの取り方など）はサブクラスで吸収する。
"""

from __future__ import annotations

import json
import re
from collections import deque
from typing import Any, Iterator

from ..models import Product
from .base import Scraper

_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

_IN_STOCK_TOKENS = {"instock", "limitedavailability", "presale", "backorder"}


def iter_jsonld(html: str) -> Iterator[dict[str, Any]]:
    """HTML中のすべての JSON-LD ブロックを辞書として列挙する."""
    for match in _LD_RE.finditer(html):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # 幅優先で、ドキュメント順を保ったまま辿る（順序が変わると商品の並びが揺れる）
        queue: deque[Any] = deque([data])
        while queue:
            node = queue.popleft()
            if isinstance(node, list):
                queue.extendleft(reversed(node))
            elif isinstance(node, dict):
                yield node
                for value in node.values():
                    if isinstance(value, (list, dict)):
                        queue.append(value)


def iter_products(html: str) -> Iterator[dict[str, Any]]:
    """JSON-LD から @type=Product のノードを取り出す.

    SSENSE のように Product を直接並べるサイトと、Farfetch のように ItemList の
    itemListElement に入れるサイトの両方に対応する（走査は再帰的なので、
    ItemList の中の Product もそのまま拾える）。
    """
    for node in iter_jsonld(html):
        types = node.get("@type")
        types = [types] if isinstance(types, str) else (types or [])
        if any(str(t).lower() == "product" for t in types):
            yield node


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def offer_of(node: dict[str, Any]) -> dict[str, Any]:
    offers = _first(node.get("offers")) or {}
    if isinstance(offers, dict) and offers.get("@type") == "AggregateOffer":
        nested = _first(offers.get("offers"))
        if isinstance(nested, dict):
            merged = dict(offers)
            merged.update(nested)
            return merged
    return offers if isinstance(offers, dict) else {}


def parse_availability(offer: dict[str, Any]) -> bool:
    raw = str(offer.get("availability") or "").rsplit("/", 1)[-1].strip().lower()
    return raw in _IN_STOCK_TOKENS if raw else True


def parse_price(offer: dict[str, Any]) -> float | None:
    for key in ("price", "lowPrice", "highPrice"):
        value = offer.get(key)
        if value in (None, ""):
            continue
        try:
            return float(str(value).replace(",", ""))
        except ValueError:
            continue
    return None


class JsonLdListingScraper(Scraper):
    """一覧ページの JSON-LD をそのまま Product に写す汎用スクレイパー."""

    full_coverage = True
    #: サブクラスで上書きする
    site_name = "unknown"

    def fetch_products(self) -> list[Product]:
        listing_urls = self.options.get("listing_urls") or []
        if not listing_urls:
            raise RuntimeError("listing_urls が設定されていません")
        currency = str(self.options.get("currency", ""))
        max_pages = int(self.options.get("max_pages", 5))

        found: dict[str, Product] = {}
        for url in listing_urls:
            # 総ページ数がHTMLに出ないため、新しい商品が出てこなくなるまで進む。
            # ただし1ページ分の重複で止めると取りこぼす（同じページが返ってくる
            # ことが実際にある）ので、2ページ連続で新規ゼロのときだけ打ち切る。
            barren_pages = 0
            for page in range(1, max_pages + 1):
                page_url = url if page == 1 else self.page_url(url, page)
                try:
                    html = self.session.get_text(page_url)
                except Exception as exc:  # noqa: BLE001 - 2ページ目以降の失敗は打ち切り
                    if page == 1:
                        raise
                    self.warn(f"{page_url}: {page}ページ目の取得に失敗 ({exc})")
                    break

                before = len(found)
                for node in iter_products(html):
                    product = self.to_product(node, page_url, currency)
                    if product is not None:
                        found[product.product_id] = product
                if len(found) == before:
                    barren_pages += 1
                    if barren_pages >= 2:
                        break  # 2ページ続けて新規なし = 最終ページとみなす
                else:
                    barren_pages = 0
            else:
                # ループを最後まで使い切った = まだ先のページがあるかもしれない
                self.warn(
                    f"{url}: {max_pages}ページ分（{len(found)}件）で打ち切りました。"
                    "取りこぼしが疑われる場合は max_pages を増やしてください"
                )

        if not found:
            raise RuntimeError(
                f"{self.site_name}: JSON-LD から商品を取得できませんでした。"
                "Bot対策でブロックされたか、ページ構成が変わった可能性があります "
                "（config.yml の scraping.proxy か Playwright の利用を検討してください）"
            )
        return list(found.values())

    # ------------------------------------------------------------------
    def to_product(self, node: dict[str, Any], listing_url: str, currency: str) -> Product | None:
        offer = offer_of(node)
        # Farfetch は商品URLを offers の中にしか持たない
        url = str(node.get("url") or node.get("@id") or offer.get("url") or "").strip()
        name = str(node.get("name") or "").strip()
        if not url or not name:
            return None

        image = _first(node.get("image")) or ""
        if isinstance(image, dict):
            image = image.get("url", "")

        brand = _first(node.get("brand")) or {}
        brand_name = brand.get("name") if isinstance(brand, dict) else str(brand or "")

        return Product(
            shop_id=self.shop_id,
            product_id=self.product_id(node, url),
            product_url=self.absolute_url(url, listing_url),
            product_name=name,
            brand=str(brand_name or "Our Legacy"),
            price=parse_price(offer),
            currency=str(offer.get("priceCurrency") or currency),
            sizes_in_stock=self.sizes(node),
            in_stock=parse_availability(offer),
            image_url=str(image or ""),
            extra={"sku": str(node.get("sku") or node.get("mpn") or "")},
        )

    @staticmethod
    def page_url(url: str, page: int) -> str:
        """一覧URLに ``?page=N`` を付ける（既存のクエリは保持する）."""
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

        parts = urlparse(url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "page"]
        query.append(("page", str(page)))
        return urlunparse(parts._replace(query=urlencode(query)))

    def product_id(self, node: dict[str, Any], url: str) -> str:
        sku = str(node.get("productID") or node.get("sku") or "").strip()
        return sku or url.rstrip("/").rsplit("/", 1)[-1]

    def sizes(self, node: dict[str, Any]) -> list[str]:
        """在庫のあるサイズ。AggregateOffer に個別オファーがある場合のみ拾える."""
        offers = node.get("offers")
        if isinstance(offers, dict):
            offers = offers.get("offers")
        if not isinstance(offers, list):
            return []
        sizes: list[str] = []
        for offer in offers:
            if not isinstance(offer, dict) or not parse_availability(offer):
                continue
            size = offer.get("size") or (offer.get("itemOffered") or {}).get("size")
            if size:
                sizes.append(str(size))
        return sizes

    @staticmethod
    def absolute_url(url: str, base: str) -> str:
        if url.startswith("http"):
            return url
        from urllib.parse import urljoin

        return urljoin(base, url)
