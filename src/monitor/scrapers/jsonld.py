"""JSON-LD（schema.org）ベースの汎用一覧スクレイパー.

SSENSE / Farfetch / MR PORTER はいずれも素のHTTPリクエストでは 403 を返すため
（2026-09-05 時点で実測）、本モジュールの実装は**実データで検証できていない**。
仕様書フェーズ3のとおり、スクレイピング代行サービス（config.yml の
``scraping.proxy``）か Playwright を挟んで到達できるようになった時点で
`enabled: true` にして検証する前提のコードである。

これらのサイトはSEOのため商品一覧に ``application/ld+json`` の ItemList /
Product を出力しているのが一般的なので、そこを共通の入口にしている。
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
    """JSON-LD から @type=Product のノードを取り出す."""
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

        found: dict[str, Product] = {}
        for url in listing_urls:
            html = self.session.get_text(url)
            for node in iter_products(html):
                product = self.to_product(node, url, currency)
                if product is not None:
                    found[product.product_id] = product

        if not found:
            raise RuntimeError(
                f"{self.site_name}: JSON-LD から商品を取得できませんでした。"
                "Bot対策でブロックされたか、ページ構成が変わった可能性があります "
                "（config.yml の scraping.proxy か Playwright の利用を検討してください）"
            )
        return list(found.values())

    # ------------------------------------------------------------------
    def to_product(self, node: dict[str, Any], listing_url: str, currency: str) -> Product | None:
        url = str(node.get("url") or node.get("@id") or "").strip()
        name = str(node.get("name") or "").strip()
        if not url or not name:
            return None

        offer = offer_of(node)
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

    def product_id(self, node: dict[str, Any], url: str) -> str:
        sku = str(node.get("sku") or node.get("productID") or "").strip()
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
