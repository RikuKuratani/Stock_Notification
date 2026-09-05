"""スクレイパーの共通インターフェース（仕様書 非機能要件4）.

新しいショップを追加するときは Scraper を継承して ``fetch_products()`` を
実装し、``registry.py`` の SCRAPERS に登録するだけでよい。
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

from ..config import ShopConfig
from ..http import PoliteSession
from ..models import Product, ScrapeResult

log = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


class Scraper(ABC):
    """1ショップ分の取得ロジック."""

    #: 返した商品リストが「そのショップの全取扱商品」を表すか。
    #: False の場合、リストに無い商品は「取扱終了」ではなく「今回未確認」となる。
    full_coverage: bool = True

    def __init__(
        self,
        shop: ShopConfig,
        session: PoliteSession,
        known_last_checked: dict[str, str] | None = None,
    ) -> None:
        self.shop = shop
        self.session = session
        self.options: dict[str, Any] = shop.options
        #: product_id -> 最終確認時刻(ISO)。巡回順の決定に使う。
        self.known_last_checked = known_last_checked or {}
        self.warnings: list[str] = []
        #: 全取扱商品数（分かる場合のみ設定する）
        self.catalog_size: int | None = None

    @property
    def shop_id(self) -> str:
        return self.shop.id

    @abstractmethod
    def fetch_products(self) -> list[Product]:
        """商品スナップショットのリストを返す."""

    def run(self) -> ScrapeResult:
        products = self.fetch_products()
        return ScrapeResult(
            shop_id=self.shop_id,
            products=products,
            full_coverage=self.full_coverage,
            warnings=list(self.warnings),
            catalog_size=self.catalog_size if self.catalog_size is not None else len(products),
        )

    # -- 便利メソッド --------------------------------------------------
    def warn(self, message: str) -> None:
        log.warning("[%s] %s", self.shop_id, message)
        self.warnings.append(message)

    @staticmethod
    def next_data(html: str) -> dict[str, Any]:
        """Next.js の __NEXT_DATA__ を取り出す（公式サイト・END. で共通）."""
        match = _NEXT_DATA_RE.search(html)
        if not match:
            raise ValueError("__NEXT_DATA__ が見つかりません（サイト構成が変わった可能性）")
        return json.loads(match.group(1))
