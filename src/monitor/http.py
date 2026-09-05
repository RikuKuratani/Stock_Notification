"""レート制限・リトライ・robots.txt 遵守を組み込んだHTTPクライアント.

仕様書 非機能要件1（アクセス頻度への遠慮）と2（利用規約の確認）に対応する。

ブラウザ偽装（impersonate）について:
  SSENSE / Farfetch / MR PORTER は、HTTPヘッダではなく **TLSフィンガープリント**
  （通信の握手のしかたの癖）を見て、Pythonの requests や curl からのアクセスを
  403 で弾く。curl_cffi は実ブラウザと同じTLS挙動を再現できるため、
  ``impersonate`` を指定したショップではこちらを使う。

  これは「User-Agentを詐称する」のとは別の話で、実際にブラウザで開けるページを
  ブラウザと同じ作法で1時間に1回取得しているだけである。アクセス間隔の遠慮
  （min_interval_seconds）と robots.txt の遵守は偽装時も変わらず適用される。
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from dataclasses import replace
from urllib.parse import urlparse, urlunparse

import requests

from .config import Config, HttpConfig, ProxyConfig

try:  # curl_cffi は impersonate を使うショップでのみ必要
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - 未インストールでも他のショップは動く
    curl_requests = None

log = logging.getLogger(__name__)

#: リトライする価値のあるHTTPステータス（429=レート制限、5xx=サーバー側の一時障害）
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _retry_after_seconds(resp) -> float | None:
    """``Retry-After`` ヘッダがあれば、その秒数に従う（相手の指示が最優先）."""
    raw = resp.headers.get("Retry-After") if resp.headers else None
    if not raw:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        return None  # HTTP-date 形式は解釈せず、通常のバックオフに任せる
    return min(max(seconds, 0.0), 120.0)  # 待ちすぎてジョブが詰まらないよう上限を設ける


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
        impersonate: str = "",
    ) -> None:
        self.shop_id = shop_id
        self.http = http
        self.proxy = proxy
        self.impersonate = impersonate
        self.session = session or self._build_session(impersonate)
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if not impersonate:
            # 偽装時は curl_cffi が用意する一貫したヘッダ一式をそのまま使う。
            # ここで User-Agent だけ上書きすると、TLSの癖とヘッダが食い違って弾かれる。
            headers["User-Agent"] = http.user_agent
        self.session.headers.update(headers)
        self._last_request_at = 0.0
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.request_count = 0

    @staticmethod
    def _build_session(impersonate: str):
        """impersonate 指定時は curl_cffi、それ以外は通常の requests を使う."""
        if not impersonate:
            return requests.Session()
        if curl_requests is None:
            raise RuntimeError(
                "impersonate を使うには curl_cffi が必要です: pip install curl_cffi"
            )
        return curl_requests.Session(impersonate=impersonate)

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
            if resp.status_code == 200:  # noqa: PLR2004
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(resp.text.splitlines())
            else:
                # 取得できない場合は判断材料がないため素通しし、記録だけ残す。
                log.warning("[%s] robots.txt %s -> HTTP %s (判定をスキップ)", self.shop_id, robots_url, resp.status_code)
        except Exception as exc:  # noqa: BLE001
            if not _is_transport_error(exc):
                raise
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
        retry_after: float | None = None
        for attempt in range(self.http.max_retries + 1):
            self._throttle(min_interval)
            try:
                self.request_count += 1
                resp = self.session.get(request_url, **kwargs)
            except Exception as exc:  # noqa: BLE001 - curl_cffi は独自の例外型を投げる
                if not _is_transport_error(exc):
                    raise
                last_exc = exc
                retry_after = None
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code in RETRYABLE_STATUSES and attempt < self.http.max_retries:
                    last_exc = requests.HTTPError(f"HTTP {resp.status_code} for {url}")
                    retry_after = _retry_after_seconds(resp)
                else:
                    resp.raise_for_status()
                    return resp
            if attempt < self.http.max_retries:
                backoff = retry_after if retry_after else self._backoff(last_exc, attempt)
                log.info(
                    "[%s] retry %s/%s in %.1fs: %s",
                    self.shop_id, attempt + 1, self.http.max_retries, backoff, url,
                )
                time.sleep(backoff)

        assert last_exc is not None
        raise last_exc

    def _backoff(self, exc: Exception | None, attempt: int) -> float:
        """待ち時間を決める。429（レート制限）は相手に嫌われているので長めに待つ."""
        if exc is not None and "429" in str(exc):
            return self.http.rate_limit_backoff_seconds * (2**attempt)
        return 2.0 * (2**attempt)

    def get_text(self, url: str, min_interval: float | None = None, **kwargs) -> str:
        resp = self.get(url, min_interval=min_interval, **kwargs)
        # requests は文字コードを推測しそこねることがあるので明示する。
        # curl_cffi の Response は encoding を持たないことがあるため getattr で見る。
        if getattr(resp, "encoding", None) is None and hasattr(resp, "encoding"):
            resp.encoding = "utf-8"
        return resp.text

    def get_json(self, url: str, **kwargs):
        return self.get(url, **kwargs).json()


def _is_transport_error(exc: Exception) -> bool:
    """通信レベルの失敗（リトライしてよいもの）かどうか."""
    if isinstance(exc, (requests.RequestException, OSError)):
        return True
    # curl_cffi.requests.errors.RequestsError など、独自例外を名前で判定する
    return type(exc).__name__ in {"RequestsError", "CurlError", "Timeout"}


def build_session(
    shop_id: str,
    config: Config,
    impersonate: str = "",
    min_interval_seconds: float | None = None,
) -> PoliteSession:
    """ショップ用のセッションを作る.

    ``min_interval_seconds`` を渡すと、そのショップだけアクセス間隔を変えられる。
    Farfetch のようにレート制限（429）が厳しいサイト向け。
    """
    http = config.http
    if min_interval_seconds is not None:
        http = replace(http, min_interval_seconds=float(min_interval_seconds))
    return PoliteSession(
        shop_id=shop_id, http=http, proxy=config.proxy, impersonate=impersonate
    )
