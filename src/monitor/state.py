"""state.json の読み書きと差分検知（仕様書 4.2 / 4.4 / 4.6）.

state.json はリポジトリにコミットして永続化する。使い捨てコンテナで動く
GitHub Actions でも、前回までに確認済みの商品を覚えておくための唯一の記憶。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .events import LOWEST_PRICE, NEW_ARRIVAL, RESTOCK, Event
from .models import Product, ScrapeResult

log = logging.getLogger(__name__)

STATE_VERSION = 1
JST = timezone(timedelta(hours=9))


def now_jst() -> datetime:
    return datetime.now(JST)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class StateStore:
    """商品ごとの最終状態・価格履歴・ショップごとの成否を保持する."""

    def __init__(self, path: str | Path = "state/state.json") -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {
            "version": STATE_VERSION,
            "products": {},
            "shops": {},
            "recent_events": [],
        }
        self.dirty = False

    # ------------------------------------------------------------------
    # 入出力
    # ------------------------------------------------------------------
    def load(self) -> StateStore:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{self.path} が壊れています: {exc}") from exc
            self.data = {
                "version": loaded.get("version", STATE_VERSION),
                "products": loaded.get("products", {}),
                "shops": loaded.get("shops", {}),
                "recent_events": loaded.get("recent_events", []),
            }
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = iso(now_jst())
        # 商品を1行1件で書き出す。git diff が「変わった商品だけ」になるようにする。
        products = self.data.get("products", {})
        lines = ['{', f'  "version": {json.dumps(self.data["version"])},',
                 f'  "updated_at": {json.dumps(self.data["updated_at"])},',
                 f'  "shops": {json.dumps(self.data.get("shops", {}), ensure_ascii=False, sort_keys=True)},',
                 f'  "recent_events": {json.dumps(self.data.get("recent_events", []), ensure_ascii=False)},',
                 '  "products": {']
        keys = sorted(products)
        for i, key in enumerate(keys):
            body = json.dumps(products[key], ensure_ascii=False, sort_keys=True)
            comma = "," if i < len(keys) - 1 else ""
            lines.append(f"    {json.dumps(key)}: {body}{comma}")
        lines.append("  }")
        lines.append("}")
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------
    # ショップ単位のメタ情報（仕様書 4.5: 可観測性）
    # ------------------------------------------------------------------
    def shop_meta(self, shop_id: str) -> dict[str, Any]:
        return self.data.setdefault("shops", {}).setdefault(
            shop_id,
            {
                "last_run_at": None,
                "last_success_at": None,
                "last_error": None,
                "consecutive_failures": 0,
                "product_count": 0,
                "bootstrapped": False,
            },
        )

    def bootstrap_complete(self, shop_id: str, full_coverage: bool | None = None) -> bool:
        """そのショップの全商品を一度は見終わっているか.

        公式サイトのように1回で一部しか巡回しないショップでは、初回の一巡が
        終わるまで「state に無い商品」は新規入荷ではなく単なる未巡回なので、
        この判定が False の間は通知を出さない。

        ``full_coverage`` は、このフラグを持たない旧 state から移行するときだけ
        使う。1回で全件取れるショップなら過去の初回スキャンで一巡が済んでいる
        ので完了とみなし、一部しか巡回しないショップはやり直す。
        """
        meta = self.shop_meta(shop_id)
        if "bootstrap_complete" in meta:
            return bool(meta["bootstrap_complete"])
        if full_coverage is None:
            full_coverage = bool(meta.get("full_coverage", True))
        return bool(meta.get("bootstrapped")) and full_coverage

    def mark_bootstrap_complete(
        self, shop_id: str, known: int, catalog_size: int | None
    ) -> bool:
        """一巡が終わっていればフラグを立て、今回終わったかどうかを返す.

        いちど立てたら下ろさない。取扱商品が増えて known < catalog_size に
        戻ったときに、本物の新規入荷を握りつぶさないようにするため。
        """
        meta = self.shop_meta(shop_id)
        if meta.get("bootstrap_complete"):
            return False
        finished = catalog_size is None or known >= catalog_size
        meta["bootstrap_complete"] = finished
        meta["catalog_size"] = catalog_size
        self.dirty = True
        return finished

    def has_products(self, shop_id: str) -> bool:
        prefix = f"{shop_id}:"
        return any(k.startswith(prefix) for k in self.data.get("products", {}))

    def products_for(self, shop_id: str) -> dict[str, dict[str, Any]]:
        prefix = f"{shop_id}:"
        return {k: v for k, v in self.data.get("products", {}).items() if k.startswith(prefix)}

    def record_success(
        self,
        shop_id: str,
        product_count: int,
        bootstrapped: bool,
        full_coverage: bool = True,
    ) -> None:
        meta = self.shop_meta(shop_id)
        stamp = iso(now_jst())
        meta.update(
            full_coverage=full_coverage,
            last_run_at=stamp,
            last_success_at=stamp,
            last_error=None,
            consecutive_failures=0,
            product_count=product_count,
            bootstrapped=bootstrapped or meta.get("bootstrapped", False),
        )
        if not bootstrapped:
            # 通常モードで走り切ったなら一巡は確実に済んでいる。ここで明示的に
            # 記録しておかないと、次に全件取得できなかった回（例: ページ送りが
            # 429で途切れた Farfetch）に初回スキャン扱いへ逆戻りし、本物の
            # 入荷通知を黙って捨ててしまう。
            meta["bootstrap_complete"] = True
        self.dirty = True

    def record_failure(self, shop_id: str, message: str) -> dict[str, Any]:
        meta = self.shop_meta(shop_id)
        meta["last_run_at"] = iso(now_jst())
        meta["last_error"] = message[:500]
        meta["consecutive_failures"] = int(meta.get("consecutive_failures", 0)) + 1
        self.dirty = True
        return meta

    # ------------------------------------------------------------------
    # 差分検知
    # ------------------------------------------------------------------
    def apply(
        self,
        result: ScrapeResult,
        shop_name: str,
        *,
        bootstrap: bool,
        now: datetime | None = None,
    ) -> list[Event]:
        """スクレイピング結果を state に反映し、通知すべきイベントを返す.

        ``bootstrap`` が True（そのショップの初回実行）のときは、既存在庫を
        まるごと新規入荷として通知しないようイベントを返さない。
        """
        now = now or now_jst()
        stamp = iso(now)
        products = self.data.setdefault("products", {})
        events: list[Event] = []
        seen_keys: set[str] = set()

        # 商品ごとの「最終確認時刻」は、公式サイトのように毎回一部しか巡回できない
        # ショップで巡回順を決めるためだけに要る。毎回全件取れるショップで持つと、
        # 中身が何も変わっていない商品まで毎時書き換わり、リポジトリが無駄に膨らむ。
        track_checked_at = not result.full_coverage

        for product in result.products:
            key = product.key
            seen_keys.add(key)
            prev = products.get(key)
            entry, product_events = self._merge(
                product, prev, shop_name, stamp, track_checked_at
            )
            products[key] = entry
            if not bootstrap:
                events.extend(product_events)

        if result.full_coverage and self._harvest_looks_complete(result, len(seen_keys)):
            # 一覧から消えた商品は取り扱い終了/完売とみなす。
            # 「在庫あり」のまま残すと、次に現れたときに再入荷を取りこぼす。
            for key, entry in products.items():
                if not key.startswith(f"{result.shop_id}:") or key in seen_keys:
                    continue
                if entry.get("in_stock"):
                    entry["in_stock"] = False
                    entry["sizes_in_stock"] = []
                    entry["status"] = "gone"
                    entry["stock_signature"] = ""

        self.dirty = True
        return events

    #: 前回より取得数がこの割合を下回ったら、取得が不完全だったとみなす
    HARVEST_FLOOR = 0.8

    def _harvest_looks_complete(self, result: ScrapeResult, fetched: int) -> bool:
        """今回の取得が「その店の全在庫」と呼べる量かどうか.

        ページ送りの失敗などで一部しか取れなかった回に完売判定をすると、
        次の回で戻ってきた商品がまとめて再入荷として通知されてしまう。
        前回の8割を下回ったら不完全とみなし、完売判定を見送る。
        """
        previous = int(self.shop_meta(result.shop_id).get("product_count", 0) or 0)
        if previous <= 0:
            return True
        if fetched >= previous * self.HARVEST_FLOOR:
            return True
        log.warning(
            "[%s] 取得数が前回より大きく減りました（%d → %d）。"
            "取得もれの可能性があるため、完売の判定は見送ります",
            result.shop_id, previous, fetched,
        )
        result.warnings.append(
            f"取得数が前回より大きく減りました（{previous} → {fetched}件）。完売判定を見送りました"
        )
        return False

    def _merge(
        self,
        product: Product,
        prev: dict[str, Any] | None,
        shop_name: str,
        stamp: str,
        track_checked_at: bool = True,
    ) -> tuple[dict[str, Any], list[Event]]:
        events: list[Event] = []
        entry: dict[str, Any] = dict(prev or {})

        entry.update(
            shop_id=product.shop_id,
            product_id=product.product_id,
            product_url=product.product_url,
            brand=product.brand,
            product_name=product.product_name,
            price=product.price,
            currency=product.currency,
            sizes_in_stock=list(product.sizes_in_stock),
            in_stock=product.in_stock,
            stock_count=product.stock_count,
            image_url=product.image_url,
            stock_signature=product.stock_signature(),
        )
        if track_checked_at:
            entry["last_checked_at"] = stamp
        else:
            # 過去に記録したぶんは一度だけ消す。以降、変化のない商品は
            # 書き出す内容が前回と1バイトも変わらず、差分に出てこなくなる。
            entry.pop("last_checked_at", None)
        if product.extra:
            entry["extra"] = product.extra

        # --- 新規入荷 -------------------------------------------------
        if prev is None:
            entry["first_seen_at"] = stamp
            entry["status"] = "new" if product.in_stock else "new_out_of_stock"
            if product.in_stock:
                events.append(Event(NEW_ARRIVAL, product, shop_name))
        else:
            entry.setdefault("first_seen_at", stamp)
            # --- 再入荷（サイズ復活を含む） -------------------------
            was_in_stock = bool(prev.get("in_stock"))
            prev_sizes = set(prev.get("sizes_in_stock") or [])
            now_sizes = set(product.sizes_in_stock)
            revived = sorted(now_sizes - prev_sizes)

            if product.in_stock and not was_in_stock:
                entry["status"] = "restocked"
                events.append(
                    Event(RESTOCK, product, shop_name, detail={"sizes": sorted(now_sizes)})
                )
            elif product.in_stock and revived:
                entry["status"] = "restocked"
                events.append(Event(RESTOCK, product, shop_name, detail={"sizes": revived}))
            elif product.in_stock:
                entry["status"] = "in_stock"
            else:
                entry["status"] = "sold_out"

        # --- 価格履歴と過去最安値（仕様書 4.6） ----------------------
        history: list[dict[str, Any]] = list(entry.get("price_history") or [])
        if product.price is not None:
            prev_price = None if prev is None else prev.get("price")
            prev_currency = None if prev is None else prev.get("currency")
            currency_changed = prev_currency is not None and prev_currency != product.currency
            changed = prev is None or currency_changed or prev_price != product.price
            # 在庫状況の変化も履歴に残す（グラフ上で完売期間が分かるように）
            stock_changed = prev is not None and bool(prev.get("in_stock")) != product.in_stock
            if currency_changed:
                # 通貨が変わったら過去の履歴とは比較できないので作り直す
                history = []
            if changed or stock_changed:
                history.append(
                    {"date": stamp, "price": product.price, "in_stock": product.in_stock}
                )
                entry["price_history"] = history

            lowest = entry.get("lowest_price_ever")
            if currency_changed:
                lowest = None  # 通貨が変わったら最安値の比較基準をリセットする
            if lowest is None:
                entry["lowest_price_ever"] = product.price
                entry["lowest_price_seen_at"] = stamp
            elif product.price < float(lowest):
                entry["lowest_price_ever"] = product.price
                entry["lowest_price_seen_at"] = stamp
                if prev is not None:
                    events.append(
                        Event(
                            LOWEST_PRICE,
                            product,
                            shop_name,
                            detail={"previous_lowest": float(lowest)},
                        )
                    )

        return entry, events

    # ------------------------------------------------------------------
    # 通知の重複抑止（仕様書 4.4）
    # ------------------------------------------------------------------
    def should_notify(self, event: Event, cooldown_hours: float, now: datetime | None = None) -> bool:
        now = now or now_jst()
        entry = self.data.get("products", {}).get(event.product.key, {})
        notified = entry.get("notified") or {}
        record = notified.get(event.type)
        if not record:
            return True
        # 同じ在庫指紋のまま再通知しない
        if record.get("signature") == event.product.stock_signature():
            return False
        last = parse_iso(record.get("at"))
        if last and (now - last) < timedelta(hours=cooldown_hours):
            return False
        return True

    def mark_notified(self, event: Event, now: datetime | None = None) -> None:
        now = now or now_jst()
        entry = self.data.setdefault("products", {}).setdefault(event.product.key, {})
        notified = entry.setdefault("notified", {})
        notified[event.type] = {
            "at": iso(now),
            "signature": event.product.stock_signature(),
        }
        entry["last_notified_at"] = iso(now)
        self.dirty = True

    # ------------------------------------------------------------------
    # 通知履歴（ダッシュボード用 / 仕様書 4.6）
    # ------------------------------------------------------------------
    def log_events(self, events: Iterable[Event], limit: int = 200, now: datetime | None = None) -> None:
        now = now or now_jst()
        log_entries: list[dict[str, Any]] = list(self.data.setdefault("recent_events", []))
        for event in events:
            log_entries.append(
                {
                    "at": iso(now),
                    "type": event.type,
                    "shop_id": event.product.shop_id,
                    "shop_name": event.shop_name,
                    "key": event.product.key,
                    "name": event.product.product_name,
                    "url": event.product.product_url,
                    "price": event.product.price,
                    "currency": event.product.currency,
                    "detail": event.detail,
                }
            )
        self.data["recent_events"] = log_entries[-limit:]
        self.dirty = True

    # ------------------------------------------------------------------
    # 巡回の優先順位（公式サイトのように全件を毎回見られないショップ向け）
    # ------------------------------------------------------------------
    def last_checked_map(self, shop_id: str) -> dict[str, str]:
        out: dict[str, str] = {}
        prefix = f"{shop_id}:"
        for key, entry in self.data.get("products", {}).items():
            if key.startswith(prefix):
                out[key[len(prefix) :]] = entry.get("last_checked_at") or ""
        return out

    def iter_products(self, shop_ids: Iterable[str] | None = None):
        allow = None if shop_ids is None else set(shop_ids)
        for key, entry in self.data.get("products", {}).items():
            if allow is None or entry.get("shop_id") in allow:
                yield key, entry
