"""スクレイパーとステートストアが受け渡すデータ構造."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Product:
    """1ショップ・1商品の、ある時点でのスナップショット.

    各スクレイパーはこの型のリストを返す（仕様書 非機能要件4）。
    サイズ別在庫を出さないショップは ``sizes_in_stock`` を空にし、
    ``in_stock`` と ``stock_count`` だけを埋めればよい。
    """

    shop_id: str
    product_id: str
    product_url: str
    product_name: str
    brand: str = "Our Legacy"
    price: float | None = None
    currency: str = ""
    sizes_in_stock: list[str] = field(default_factory=list)
    in_stock: bool = True
    stock_count: int | None = None
    image_url: str = ""
    # 参考情報（通知本文やダッシュボードに使う）
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """state.json 内で商品を一意に指すキー."""
        return f"{self.shop_id}:{self.product_id}"

    def stock_signature(self) -> str:
        """在庫状態の指紋。再入荷判定と重複通知の抑止に使う（仕様書 4.4）."""
        payload = "|".join(
            [
                "1" if self.in_stock else "0",
                ",".join(sorted(self.sizes_in_stock)),
                "" if self.stock_count is None else str(self.stock_count),
            ]
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


@dataclass
class ScrapeResult:
    """1ショップ・1回分のスクレイピング結果."""

    shop_id: str
    products: list[Product]
    #: True のとき「返さなかった商品は取り扱いが消えた」とみなしてよい。
    #: 予算内で一部だけ巡回するスクレイパー（公式サイト）は False を返す。
    full_coverage: bool = True
    #: 実行中に起きた非致命的な問題（ダッシュボードとログに出す）
    warnings: list[str] = field(default_factory=list)
    #: そのショップの全取扱商品数。予算内で一部しか巡回しないスクレイパーが
    #: 「初回の一巡が終わったか」を判断するために使う。分からなければ None。
    catalog_size: int | None = None


def slugify(value: str, max_length: int = 60) -> str:
    """商品IDなどをファイル名に使える形へ変換する."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-").lower()
    if len(slug) > max_length:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug[: max_length - 9]}-{digest}"
    return slug or "item"
