"""Slack Incoming Webhook への通知（仕様書 4.4 / 4.5）."""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

import requests

from .events import Event, ShopFailure

log = logging.getLogger(__name__)

_CURRENCY_SYMBOLS = {"JPY": "¥", "USD": "$", "EUR": "€", "GBP": "£"}


def format_price(price: float | None, currency: str) -> str:
    if price is None:
        return "価格不明"
    symbol = _CURRENCY_SYMBOLS.get(currency.upper(), "")
    if currency.upper() in ("JPY", "KRW"):
        body = f"{price:,.0f}"
    else:
        body = f"{price:,.2f}".rstrip("0").rstrip(".")
    return f"{symbol}{body}" if symbol else f"{body} {currency}".strip()


class SlackNotifier:
    """Webhook が未設定でも落ちないようにし、その場合はログに出すだけにする."""

    def __init__(
        self,
        webhook_url: str,
        error_webhook_url: str = "",
        dry_run: bool = False,
        session: requests.Session | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.webhook_url = webhook_url
        self.error_webhook_url = error_webhook_url or webhook_url
        self.dry_run = dry_run
        self.session = session or requests.Session()
        self.timeout = timeout
        self.sent = 0

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    # ------------------------------------------------------------------
    def _post(self, payload: dict[str, Any], webhook_url: str) -> bool:
        if self.dry_run or not webhook_url:
            reason = "dry-run" if self.dry_run else "SLACK_WEBHOOK_URL 未設定"
            log.info("[slack:%s] %s", reason, json.dumps(payload, ensure_ascii=False)[:800])
            self.sent += 1
            return True
        try:
            resp = self.session.post(webhook_url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Slack への送信に失敗しました: %s", exc)
            return False
        self.sent += 1
        return True

    # ------------------------------------------------------------------
    def notify_event(self, event: Event, chart_url: str = "") -> bool:
        return self._post(self._event_payload(event, chart_url), self.webhook_url)

    def _event_payload(self, event: Event, chart_url: str) -> dict[str, Any]:
        product = event.product
        headline = f"{event.emoji} {event.label} | {event.shop_name}"
        price_text = format_price(product.price, product.currency)

        fields = [
            {"type": "mrkdwn", "text": f"*ブランド*\n{product.brand}"},
            {"type": "mrkdwn", "text": f"*ショップ*\n{event.shop_name}"},
            {"type": "mrkdwn", "text": f"*価格*\n{price_text}"},
        ]

        if event.type == "lowest_price":
            previous = event.detail.get("previous_lowest")
            if previous is not None:
                diff = previous - (product.price or previous)
                fields.append(
                    {
                        "type": "mrkdwn",
                        "text": f"*これまでの最安値*\n{format_price(previous, product.currency)}"
                        f"（-{format_price(diff, product.currency)}）",
                    }
                )

        sizes = event.detail.get("sizes") or product.sizes_in_stock
        if sizes:
            label = "*復活したサイズ*" if event.type == "restock" else "*在庫サイズ*"
            fields.append({"type": "mrkdwn", "text": f"{label}\n{', '.join(sizes)}"})
        elif product.stock_count is not None:
            fields.append({"type": "mrkdwn", "text": f"*在庫数*\n{product.stock_count}"})

        section: dict[str, Any] = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*<{product.product_url}|{product.product_name}>*"},
            "fields": fields[:10],
        }
        if product.image_url:
            section["accessory"] = {
                "type": "image",
                "image_url": product.image_url,
                "alt_text": product.product_name[:150] or "product",
            }

        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": headline[:150], "emoji": True}},
            section,
        ]

        # 価格推移グラフ（GitHub Pages にホストしたPNG / 仕様書 4.6）
        if chart_url:
            blocks.append(
                {
                    "type": "image",
                    "image_url": chart_url,
                    "alt_text": "価格推移",
                    "title": {"type": "plain_text", "text": "価格推移"},
                }
            )

        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"<{product.product_url}|商品ページを開く>"}],
            }
        )
        return {"text": f"{headline}: {product.product_name} / {price_text}", "blocks": blocks}

    # ------------------------------------------------------------------
    def notify_bootstrap(
        self,
        shop_name: str,
        registered: int,
        catalog_size: int | None = None,
        finished: bool = True,
    ) -> bool:
        """初回スキャンの進捗を伝える.

        公式サイトのように一巡に何回もかかるショップでは、終わるまで進捗を出す。
        一巡が終わるまでは「未知の商品＝未巡回」なので差分通知は始まらない。
        """
        if finished:
            text = (
                f":inbox_tray: *{shop_name}* の初回スキャンが完了しました "
                f"（{registered:,}件を登録）。次回以降、差分だけを通知します。"
            )
        else:
            total = f"{catalog_size:,}" if catalog_size else "?"
            percent = f"{registered / catalog_size * 100:.0f}%" if catalog_size else "?"
            text = (
                f":hourglass_flowing_sand: *{shop_name}* の初回スキャン中です "
                f"（{registered:,} / {total}件・{percent}）。"
                "全商品を一巡するまで、差分通知は始まりません。"
            )
        return self._post({"text": text}, self.webhook_url)

    def notify_failures(self, failures: Sequence[ShopFailure]) -> bool:
        """ショップ単位の取得失敗をまとめて通知する（仕様書 4.5）."""
        if not failures:
            return True
        lines = [":warning: *取得に失敗したショップがあります*"]
        for failure in failures:
            last = failure.last_success_at or "なし"
            lines.append(
                f"• *{failure.shop_name}* — {failure.consecutive_failures}回連続失敗 "
                f"（最終成功: {last}）\n　`{failure.message[:300]}`"
            )
        return self._post({"text": "\n".join(lines)}, self.error_webhook_url)

    def notify_run_summary(self, text: str) -> bool:
        return self._post({"text": text}, self.webhook_url)
