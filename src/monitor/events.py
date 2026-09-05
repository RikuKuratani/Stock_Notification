"""検知イベントの定義."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Product

NEW_ARRIVAL = "new_arrival"
RESTOCK = "restock"
LOWEST_PRICE = "lowest_price"

EVENT_LABELS = {
    NEW_ARRIVAL: "新規入荷",
    RESTOCK: "再入荷",
    LOWEST_PRICE: "過去最安値を更新",
}

EVENT_EMOJI = {
    NEW_ARRIVAL: ":new:",
    RESTOCK: ":arrows_counterclockwise:",
    LOWEST_PRICE: ":rotating_light:",
}


@dataclass
class Event:
    type: str
    product: Product
    shop_name: str
    #: 通知本文に添える補足（復活したサイズ、旧最安値など）
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return EVENT_LABELS.get(self.type, self.type)

    @property
    def emoji(self) -> str:
        return EVENT_EMOJI.get(self.type, ":bell:")


@dataclass
class ShopFailure:
    shop_id: str
    shop_name: str
    message: str
    consecutive_failures: int
    last_success_at: str | None
