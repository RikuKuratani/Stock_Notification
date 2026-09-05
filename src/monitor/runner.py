"""1回分の監視サイクルを実行する（仕様書 4.1〜4.6 の結線）."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .config import Config, ShopConfig
from .events import Event, ShopFailure
from .http import build_session
from .notify import SlackNotifier
from .report import ReportBuilder
from .scrapers import build_scraper
from .state import StateStore

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    shops_ok: int = 0
    shops_failed: int = 0
    products_seen: int = 0
    events_detected: int = 0
    notifications_sent: int = 0
    requests_made: int = 0
    charts_generated: int = 0
    warnings: list[str] = field(default_factory=list)
    failures: list[ShopFailure] = field(default_factory=list)

    def as_text(self) -> str:
        return (
            f"ショップ {self.shops_ok} 成功 / {self.shops_failed} 失敗, "
            f"商品 {self.products_seen} 件, イベント {self.events_detected} 件, "
            f"通知 {self.notifications_sent} 件, HTTP {self.requests_made} 回, "
            f"グラフ {self.charts_generated} 枚"
        )


class Runner:
    def __init__(
        self,
        config: Config,
        state: StateStore,
        notifier: SlackNotifier,
        only_shops: list[str] | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.notifier = notifier
        self.only_shops = set(only_shops) if only_shops else None

    # ------------------------------------------------------------------
    def run(self) -> RunSummary:
        summary = RunSummary()
        pending: list[Event] = []

        for shop in self.config.enabled_shops():
            if self.only_shops and shop.id not in self.only_shops:
                continue
            events = self._run_shop(shop, summary)
            pending.extend(events)

        summary.events_detected = len(pending)

        # グラフ → 通知 → ダッシュボード の順。通知に最新のグラフURLを添えるため、
        # Slack送信より先に画像を生成しておく必要がある（仕様書 4.6）。
        builder = ReportBuilder(self.config.report, self.state)
        charted = builder.build_charts(priority_keys={e.product.key for e in pending})
        summary.charts_generated = builder.generated

        notified = self._notify(pending, summary, builder)
        self.state.log_events(notified)
        builder.write_dashboard(charted)

        if summary.failures:
            self.notifier.notify_failures(summary.failures)

        summary.notifications_sent = self.notifier.sent
        return summary

    # ------------------------------------------------------------------
    def _run_shop(self, shop: ShopConfig, summary: RunSummary) -> list[Event]:
        started = time.monotonic()
        session = build_session(shop.id, self.config)
        bootstrap = not self.state.has_products(shop.id)

        log.info("[%s] 開始%s", shop.id, "（初回スキャン）" if bootstrap else "")
        try:
            scraper = build_scraper(shop, session, self.state.last_checked_map(shop.id))
            result = scraper.run()
        except Exception as exc:  # noqa: BLE001 - 1ショップの失敗で他を止めない
            meta = self.state.record_failure(shop.id, f"{type(exc).__name__}: {exc}")
            summary.shops_failed += 1
            summary.requests_made += session.request_count
            log.exception("[%s] 取得に失敗しました", shop.id)
            summary.failures.append(
                ShopFailure(
                    shop_id=shop.id,
                    shop_name=shop.name,
                    message=f"{type(exc).__name__}: {exc}",
                    consecutive_failures=int(meta.get("consecutive_failures", 1)),
                    last_success_at=meta.get("last_success_at"),
                )
            )
            return []

        events = self.state.apply(result, shop.name, bootstrap=bootstrap)
        self.state.record_success(shop.id, len(result.products), bootstrapped=bootstrap)

        summary.shops_ok += 1
        summary.products_seen += len(result.products)
        summary.requests_made += session.request_count
        summary.warnings.extend(f"[{shop.id}] {w}" for w in result.warnings)

        log.info(
            "[%s] 完了: 商品 %d 件 / イベント %d 件 / HTTP %d 回 / %.1fs",
            shop.id,
            len(result.products),
            len(events),
            session.request_count,
            time.monotonic() - started,
        )

        if bootstrap and self.config.notify.bootstrap_summary_only:
            self.notifier.notify_bootstrap(shop.name, len(result.products))
        return events

    # ------------------------------------------------------------------
    def _notify(
        self, events: list[Event], summary: RunSummary, builder: ReportBuilder
    ) -> list[Event]:
        """クールダウンと上限を適用して Slack に送る（仕様書 4.4）."""
        cfg = self.config.notify
        sendable = [
            event
            for event in events
            if cfg.wants(event.type) and self.state.should_notify(event, cfg.cooldown_hours)
        ]
        # 最安値更新 > 再入荷 > 新規入荷 の順に、上限まで送る
        priority = {"lowest_price": 0, "restock": 1, "new_arrival": 2}
        sendable.sort(key=lambda e: priority.get(e.type, 9))

        capped = sendable[: cfg.max_messages_per_run]
        for event in capped:
            entry = self.state.data.get("products", {}).get(event.product.key, {})
            chart_url = builder.chart_url(event.product.key, entry)
            if self.notifier.notify_event(event, chart_url=chart_url):
                self.state.mark_notified(event)

        overflow = len(sendable) - len(capped)
        if overflow > 0:
            message = (
                f"通知上限（{cfg.max_messages_per_run}件/回）に達したため、"
                f"残り {overflow} 件は次回に持ち越します。"
            )
            summary.warnings.append(message)
            self.notifier.notify_run_summary(f":information_source: {message}")
        return capped
