"""config.yml の読み込みと既定値の解決."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class HttpConfig:
    min_interval_seconds: float = 1.5
    timeout_seconds: float = 30.0
    max_retries: int = 2
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
    respect_robots_txt: bool = True


@dataclass
class NotifyConfig:
    cooldown_hours: float = 24.0
    max_messages_per_run: int = 30
    bootstrap_summary_only: bool = True
    events: dict[str, bool] = field(
        default_factory=lambda: {
            "new_arrival": True,
            "restock": True,
            "lowest_price": True,
        }
    )

    def wants(self, event_type: str) -> bool:
        return bool(self.events.get(event_type, True))


@dataclass
class ReportConfig:
    enabled: bool = True
    output_dir: str = "docs"
    base_url: str = ""
    max_charts: int = 300
    recent_events: int = 100


@dataclass
class ProxyConfig:
    enabled: bool = False
    url_template: str = ""
    shops: list[str] = field(default_factory=list)
    api_key: str = ""

    def applies_to(self, shop_id: str) -> bool:
        return bool(self.enabled and self.api_key and self.url_template and shop_id in self.shops)

    def build_url(self, url: str) -> str:
        from urllib.parse import quote

        return self.url_template.format(key=self.api_key, url=quote(url, safe=""))


@dataclass
class ShopConfig:
    id: str
    name: str
    scraper: str
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    brand: str = "Our Legacy"
    http: HttpConfig = field(default_factory=HttpConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    shops: list[ShopConfig] = field(default_factory=list)
    #: 通知先。環境変数から読む（仕様書 4.1: Webhook URLはコードに書かない）
    slack_webhook_url: str = ""
    slack_error_webhook_url: str = ""

    def enabled_shops(self) -> list[ShopConfig]:
        return [s for s in self.shops if s.enabled]


def _filter_kwargs(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """dataclass が受け取れるキーだけ通す（config.yml の未知キーで落ちないように）."""
    allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return {k: v for k, v in (data or {}).items() if k in allowed}


def load_config(path: str | Path = "config.yml", env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    scraping = raw.get("scraping") or {}
    proxy_raw = dict(scraping.get("proxy") or {})
    proxy = ProxyConfig(**_filter_kwargs(ProxyConfig, proxy_raw))
    proxy.api_key = env.get("SCRAPER_PROXY_API_KEY", "")

    shops = [
        ShopConfig(
            id=str(s["id"]),
            name=str(s.get("name", s["id"])),
            scraper=str(s.get("scraper", s["id"])),
            enabled=bool(s.get("enabled", True)),
            options=dict(s.get("options") or {}),
        )
        for s in (raw.get("shops") or [])
    ]

    return Config(
        brand=str(raw.get("brand", "Our Legacy")),
        http=HttpConfig(**_filter_kwargs(HttpConfig, raw.get("http") or {})),
        notify=NotifyConfig(**_filter_kwargs(NotifyConfig, raw.get("notify") or {})),
        report=ReportConfig(**_filter_kwargs(ReportConfig, raw.get("report") or {})),
        proxy=proxy,
        shops=shops,
        slack_webhook_url=env.get("SLACK_WEBHOOK_URL", ""),
        slack_error_webhook_url=env.get("SLACK_ERROR_WEBHOOK_URL", ""),
    )
