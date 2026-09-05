"""Our Legacy 公式サイト（ourlegacy.com）.

構造:
  - Next.js + Centra バックエンド。カテゴリ一覧は Depict によるクライアント
    レンダリングのため HTML には商品が出てこないが、**商品ページ**は SSR で
    ``__NEXT_DATA__`` に完全な商品データ（価格・サイズ別在庫）を持つ。
  - 商品URLの列挙は sitemap から行う。

巡回戦略:
  全商品は約2,500件あり毎時全件は取得できないため、1回の実行では
  ``max_product_fetches`` 件だけ商品ページを見る。優先度は
    1. まだ一度も取得していない商品（= 新規入荷）
    2. ウォッチリストに一致する商品
    3. 最終確認が古い順
  sitemap 自体は毎回全件読むので、**新規入荷の検知は常に即時**。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ..models import Product
from .base import Scraper

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)

#: Centra が返すサイズ在庫の値。"no" 以外は購入可能。
_OUT_OF_STOCK_VALUES = {"no", "0", "", "none"}


class OurLegacyScraper(Scraper):
    #: 予算内で一部の商品しか見ないため、未取得＝取扱終了とはみなさない
    full_coverage = False

    def fetch_products(self) -> list[Product]:
        index_url = self.options.get(
            "sitemap_index_url", "https://www.ourlegacy.com/sitemap/sitemap.xml"
        )
        budget = int(self.options.get("max_product_fetches", 150))
        watchlist = [str(w).lower() for w in (self.options.get("watchlist") or [])]

        # sitemap は数KBの静的XMLが80本ほどあるため、商品ページより短い間隔で取る
        sitemap_interval = float(self.options.get("sitemap_min_interval_seconds", 0.4))
        urls = self._collect_product_urls(index_url, sitemap_interval)
        if not urls:
            raise RuntimeError("sitemap から商品URLを1件も取得できませんでした")

        self.catalog_size = len(urls)
        ordered = self._prioritise(urls, watchlist)
        products: list[Product] = []
        failures = 0
        for url in ordered[:budget]:
            try:
                product = self._fetch_product(url)
            except Exception as exc:  # noqa: BLE001 - 1商品の失敗で全体を止めない
                failures += 1
                if failures <= 5:
                    self.warn(f"商品ページの取得に失敗: {url} ({exc})")
                if failures > max(10, budget // 5):
                    raise RuntimeError(
                        f"商品ページの失敗が多すぎます（{failures}件）。サイト構成の変更を疑ってください"
                    ) from exc
                continue
            if product is not None:
                products.append(product)

        self._note_coverage(len(urls), len(ordered[:budget]), len(products))
        return products

    # ------------------------------------------------------------------
    def _collect_product_urls(self, index_url: str, min_interval: float = 0.4) -> list[str]:
        """sitemap index -> 商品sitemap -> 商品URL."""
        index_xml = self.session.get_text(index_url, min_interval=min_interval)
        sitemaps = [u for u in _LOC_RE.findall(index_xml) if "/product/" in u]
        if not sitemaps:
            self.warn("sitemap index に商品sitemapが見つかりません")
            return []

        urls: list[str] = []
        seen: set[str] = set()
        for sitemap_url in sitemaps:
            try:
                xml = self.session.get_text(sitemap_url, min_interval=min_interval)
            except Exception as exc:  # noqa: BLE001
                self.warn(f"sitemap の取得に失敗: {sitemap_url} ({exc})")
                continue
            for url in _LOC_RE.findall(xml):
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
        return urls

    def _prioritise(self, urls: Iterable[str], watchlist: list[str]) -> list[str]:
        """未取得 > ウォッチリスト > 最終確認が古い順、に並べ替える."""
        checked = self.known_last_checked

        def sort_key(url: str) -> tuple[int, str]:
            pid = self.product_id_from_url(url)
            last = checked.get(pid)
            if not last:
                return (0, url)  # 未取得（新規入荷候補）を最優先
            if watchlist and any(w in url.lower() for w in watchlist):
                return (1, last)
            return (2, last)  # 最終確認が古い順

        return sorted(urls, key=sort_key)

    def _note_coverage(self, total: int, attempted: int, ok: int) -> None:
        if attempted < total:
            self.warn(
                f"全{total}商品のうち今回{attempted}件を確認（{ok}件成功）。"
                f"1周には約{-(-total // max(attempted, 1))}回の実行が必要です"
            )

    @staticmethod
    def product_id_from_url(url: str) -> str:
        """URL末尾のスラッグを商品IDとして使う."""
        return url.rstrip("/").rsplit("/", 1)[-1]

    # ------------------------------------------------------------------
    def _fetch_product(self, url: str) -> Product | None:
        html = self.session.get_text(url)
        return self.parse_product_page(html, url, self.shop_id, self.options)

    @classmethod
    def parse_product_page(
        cls,
        html: str,
        url: str,
        shop_id: str,
        options: dict[str, Any] | None = None,
    ) -> Product | None:
        """商品ページのHTMLから Product を組み立てる（テストから直接呼べる）."""
        options = options or {}
        data = Scraper.next_data(html)
        page_props = data.get("props", {}).get("pageProps", {})
        inner = page_props.get("pageProps", page_props)
        centra = inner.get("centra") or {}
        raw = centra.get("product")
        if not raw:
            return None

        sizes_in_stock = [
            str(item.get("name", "")).strip()
            for item in (raw.get("sizes") or raw.get("items") or [])
            if str(item.get("stock", "")).strip().lower() not in _OUT_OF_STOCK_VALUES
        ]
        sizes_in_stock = [s for s in sizes_in_stock if s]

        price, currency = cls._parse_price(raw, options.get("currency", ""))

        # available が False でもサイズ在庫が残ることがあるため両方を見る
        in_stock = bool(sizes_in_stock) and bool(raw.get("available", True))

        image_url = ""
        media = raw.get("media") or {}
        for bucket in ("standard", "full"):
            images = media.get(bucket) or []
            if images:
                image_url = str(images[0])
                break

        return Product(
            shop_id=shop_id,
            product_id=cls.product_id_from_url(url),
            product_url=url,
            product_name=cls._display_name(raw),
            brand=str(raw.get("brandName") or "Our Legacy").title(),
            price=price,
            currency=currency,
            sizes_in_stock=sizes_in_stock,
            in_stock=in_stock,
            image_url=image_url,
            extra={
                "sku": raw.get("sku", ""),
                "collection": raw.get("collectionName", ""),
                "category": raw.get("categoryUri", ""),
                "created_at": raw.get("createdAt", ""),
                "on_sale": bool(raw.get("showAsOnSale")),
            },
        )

    @staticmethod
    def _display_name(raw: dict[str, Any]) -> str:
        name = str(raw.get("name") or "").strip()
        variant = str(raw.get("variantName") or "").strip()
        if variant and variant.lower() not in name.lower():
            return f"{name} - {variant}".strip(" -")
        return name or str(raw.get("uri", ""))

    @staticmethod
    def _parse_price(raw: dict[str, Any], fallback_currency: str) -> tuple[float | None, str]:
        value = raw.get("priceAsNumber")
        currency = fallback_currency
        # "260.00 EUR" のような文字列から通貨記号を拾う
        text = str(raw.get("price") or "")
        match = re.search(r"([A-Z]{3})\s*$", text.strip())
        if match:
            currency = match.group(1)
        if value is None and text:
            number = re.search(r"[\d.,]+", text)
            if number:
                try:
                    value = float(number.group(0).replace(",", ""))
                except ValueError:
                    value = None
        return (float(value) if value is not None else None), currency
