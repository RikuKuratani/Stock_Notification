"""SSENSE / Farfetch / MR PORTER.

いずれも 2026-09-05 時点の実測で、GitHub Actions からはもちろん通常の回線からも
素の HTTP GET に対して **403** を返す（MR PORTER は robots.txt すら 403）。
そのため config.yml では既定で ``enabled: false`` にしてある。

有効化の手順:
  1. config.yml の ``scraping.proxy.enabled`` を true にし、``shops`` に
     対象のショップIDを追加する
  2. リポジトリのシークレットに ``SCRAPER_PROXY_API_KEY`` を登録する
  3. 当該ショップの ``enabled`` を true にして workflow_dispatch で試す
  4. 商品が取れなければ ``listing_urls`` とパース箇所を実データに合わせて直す

JSON-LD を共通の入口にしているのは、この3サイトがいずれもSEO目的で
schema.org の Product を出力しているためだが、**実データでの検証は未了**。
"""

from __future__ import annotations

import re
from typing import Any

from .jsonld import JsonLdListingScraper

_SSENSE_ID_RE = re.compile(r"/(\d+)/?$")


class SsenseScraper(JsonLdListingScraper):
    site_name = "SSENSE"

    def product_id(self, node: dict[str, Any], url: str) -> str:
        # SSENSE の商品URLは末尾が数値の商品ID
        match = _SSENSE_ID_RE.search(url)
        if match:
            return match.group(1)
        return super().product_id(node, url)


class FarfetchScraper(JsonLdListingScraper):
    site_name = "Farfetch"

    def product_id(self, node: dict[str, Any], url: str) -> str:
        # Farfetch の商品URLは ".../item-12345678.aspx"
        match = re.search(r"item-(\d+)\.aspx", url)
        if match:
            return match.group(1)
        return super().product_id(node, url)


class MrPorterScraper(JsonLdListingScraper):
    site_name = "MR PORTER"

    def product_id(self, node: dict[str, Any], url: str) -> str:
        # MR PORTER の商品URLは ".../product/.../1234567890123456"
        match = re.search(r"/(\d{10,})", url)
        if match:
            return match.group(1)
        return super().product_id(node, url)
