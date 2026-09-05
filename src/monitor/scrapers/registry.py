"""スクレイパーの登録簿.

新しいショップを足すときは、ここに1行追加して config.yml に定義を書くだけ
（仕様書 非機能要件4: 拡張性）。
"""

from __future__ import annotations

from ..config import ShopConfig
from ..http import PoliteSession
from .base import Scraper
from .blocked_sites import FarfetchScraper, MrPorterScraper, SsenseScraper
from .endclothing import EndClothingScraper
from .ourlegacy import OurLegacyScraper

SCRAPERS: dict[str, type[Scraper]] = {
    "ourlegacy": OurLegacyScraper,
    "endclothing": EndClothingScraper,
    "ssense": SsenseScraper,
    "farfetch": FarfetchScraper,
    "mrporter": MrPorterScraper,
}


def build_scraper(
    shop: ShopConfig,
    session: PoliteSession,
    known_last_checked: dict[str, str] | None = None,
) -> Scraper:
    try:
        cls = SCRAPERS[shop.scraper]
    except KeyError as exc:
        known = ", ".join(sorted(SCRAPERS))
        raise KeyError(f"未知のスクレイパー '{shop.scraper}'（利用可能: {known}）") from exc
    return cls(shop, session, known_last_checked)
