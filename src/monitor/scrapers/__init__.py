"""ショップ別スクレイパー."""

from .base import Scraper
from .registry import SCRAPERS, build_scraper

__all__ = ["Scraper", "SCRAPERS", "build_scraper"]
