"""レート制限・リトライ・robots.txt 遵守を組み込んだHTTPクライアント.

仕様書 非機能要件1（アクセス頻度への遠慮）と2（利用規約の確認）に対応する。
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from urllib.parse import urlparse, urlunparse

import requests

from .config import Config, HttpConfig, ProxyConfig

log = logging.getLogger(__name__)


class BlockedByRobotsError(RuntimeError):
    """robots.txt が当該URLの取得を禁止している."""


class PoliteSession:
    """1ショップにつき1つ作る、行儀のよいHTTPセッション.

    - 同一ホストへの連続アクセスの間隔を空ける
    - 5xx / 429 に対して指数バックオフでリトライする
    - robots.txt を1ホストにつき1回だけ取得して Disallow を尊重する
    """

    def __init__(
        self,
        shop_id: str,
        http: HttpConfig,
        proxy: ProxyConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.shop_id = shop_id
        self.http = http
        self.proxy = proxy
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": http.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._last_request_at = 0.0
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.request_count = 0

    # ------------------------------------------------------------------
    # robots.txt
    # ------------------------------------------------------------------
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._robots:
            return self._robots[origin]

        parser: urllib.robotparser.RobotFileParser | None = None
        robots_url = urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
        try:
            self._throttle()
            resp = self.session.get(robots_url, timeout=self.http.timeout_seconds)
            if resp.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(resp.text.splitlines())
            else:
                # 取得できない場合は判断材料がないため素通しし、記録だけ残す。
                log.warning("[%s] robots.txt %s -> HTTP %s (判定をスキップ)", self.shop_id, robots_url, resp.status_code)
        except requests.RequestException as exc:
            log.warning("[%s] robots.txt 取得に失敗: %s", self.shop_id, exc)

        self._robots[origin] = parser
        return parser

    def allowed(self, url: str) -> bool:
        if not self.http.respect_robots_txt:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.http.user_agent, url)

    # ------------------------------------------------------------------
    # 取得
    # ------------------------------------------------------------------
    def _throttle(self, min_interval: float | None = None) -> None:
        interval = self.http.min_interval_seconds if min_interval is None else min_interval
        wait = interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def get(self, url: str, min_interval: float | None = None, **kwargs) -> requests.Response:
        """1件取得する。robots.txt で禁止されていれば例外を投げる.

        ``min_interval`` を渡すと、その1リクエストだけ待ち時間を変えられる。
        sitemap のような軽量な静的ファイルをまとめて取るときに使う。
        """
        if not self.allowed(url):
            raise BlockedByRobotsError(f"robots.txt により取得が禁止されています: {url}")

        request_url = url
        if self.proxy is not None and self.proxy.applies_to(self.shop_id):
            request_url = self.proxy.build_url(url)

        kwargs.setdefault("timeout", self.http.timeout_seconds)
        last_exc: Exception | None = None
        for attempt in range(self.http.max_retries + 1):
            self._throttle(min_interval)
            try:
                self.request_count += 1
                resp = self.session.get(request_url, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.http.max_retries:
                    last_exc = requests.HTTPError(f"HTTP {resp.status_code} for {url}")
                else:
                    resp.raise_for_status()
                    return resp
            if attempt < self.http.max_retries:
                backoff = 2.0 * (2**attempt)
                log.info("[%s] retry %s/%s in %.1fs: %s", self.shop_id, attempt + 1, self.http.max_retries, backoff, url)
                time.sleep(backoff)

        assert last_exc is not None
        raise last_exc

    def get_text(self, url: str, min_interval: float | None = None, **kwargs) -> str:
        resp = self.get(url, min_interval=min_interval, **kwargs)
        resp.encoding = resp.encoding or "utf-8"
        return resp.text

    def get_json(self, url: str, **kwargs):
        return self.get(url, **kwargs).json()


def build_session(shop_id: str, config: Config) -> PoliteSession:
    return PoliteSession(shop_id=shop_id, http=config.http, proxy=config.proxy)
