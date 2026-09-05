"""差分検知・価格履歴・重複通知抑止の検証（仕様書 4.2 / 4.4 / 4.6）."""

from datetime import timedelta

import conftest  # noqa: F401  - sys.path を通す
import pytest

from monitor.events import LOWEST_PRICE, NEW_ARRIVAL, RESTOCK, Event
from monitor.models import Product, ScrapeResult
from monitor.state import StateStore, now_jst


def make_product(**kwargs) -> Product:
    defaults = dict(
        shop_id="shop",
        product_id="p1",
        product_url="https://example.com/p1",
        product_name="Camion Pants",
        price=100.0,
        currency="EUR",
        sizes_in_stock=["46", "48"],
        in_stock=True,
    )
    defaults.update(kwargs)
    return Product(**defaults)


def apply(store: StateStore, products, *, bootstrap=False, full_coverage=True, now=None):
    result = ScrapeResult(shop_id="shop", products=products, full_coverage=full_coverage)
    return store.apply(result, "Shop", bootstrap=bootstrap, now=now)


@pytest.fixture
def store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.json")


# ----------------------------------------------------------------------
# 新規入荷
# ----------------------------------------------------------------------
def test_first_sighting_emits_new_arrival(store):
    events = apply(store, [make_product()])
    assert [e.type for e in events] == [NEW_ARRIVAL]
    assert store.data["products"]["shop:p1"]["status"] == "new"


def test_bootstrap_run_records_without_notifying(store):
    events = apply(store, [make_product(), make_product(product_id="p2")], bootstrap=True)
    assert events == []
    assert len(store.data["products"]) == 2  # 記録はされる


def test_sold_out_new_product_does_not_notify(store):
    events = apply(store, [make_product(in_stock=False, sizes_in_stock=[])])
    assert events == []
    assert store.data["products"]["shop:p1"]["status"] == "new_out_of_stock"


# ----------------------------------------------------------------------
# 再入荷
# ----------------------------------------------------------------------
def test_restock_after_sold_out(store):
    apply(store, [make_product()])
    apply(store, [make_product(in_stock=False, sizes_in_stock=[])])
    events = apply(store, [make_product()])

    assert [e.type for e in events] == [RESTOCK]
    assert events[0].detail["sizes"] == ["46", "48"]
    assert store.data["products"]["shop:p1"]["status"] == "restocked"


def test_size_revival_counts_as_restock(store):
    apply(store, [make_product(sizes_in_stock=["46"])])
    events = apply(store, [make_product(sizes_in_stock=["46", "50"])])

    assert [e.type for e in events] == [RESTOCK]
    assert events[0].detail["sizes"] == ["50"]  # 復活したサイズだけを伝える


def test_size_disappearing_is_not_a_restock(store):
    apply(store, [make_product(sizes_in_stock=["46", "48"])])
    events = apply(store, [make_product(sizes_in_stock=["46"])])
    assert events == []


def test_unchanged_product_emits_nothing(store):
    apply(store, [make_product()])
    assert apply(store, [make_product()]) == []


def test_product_missing_from_full_listing_is_marked_gone(store):
    apply(store, [make_product(), make_product(product_id="p2")])
    apply(store, [make_product()], full_coverage=True)

    assert store.data["products"]["shop:p2"]["in_stock"] is False
    assert store.data["products"]["shop:p2"]["status"] == "gone"

    # 戻ってきたら再入荷として検知できる
    events = apply(store, [make_product(), make_product(product_id="p2")])
    assert [e.type for e in events] == [RESTOCK]


def test_partial_coverage_does_not_mark_absent_products_gone(store):
    """公式サイトのように毎回一部しか巡回しないショップでは在庫を消さない."""
    apply(store, [make_product(), make_product(product_id="p2")])
    apply(store, [make_product()], full_coverage=False)
    assert store.data["products"]["shop:p2"]["in_stock"] is True


# ----------------------------------------------------------------------
# 価格履歴と過去最安値
# ----------------------------------------------------------------------
def test_price_history_only_grows_on_change(store):
    apply(store, [make_product(price=100.0)])
    apply(store, [make_product(price=100.0)])
    apply(store, [make_product(price=90.0)])
    apply(store, [make_product(price=90.0)])

    history = store.data["products"]["shop:p1"]["price_history"]
    assert [h["price"] for h in history] == [100.0, 90.0]


def test_lowest_price_ever_updates_and_notifies(store):
    apply(store, [make_product(price=100.0)])
    events = apply(store, [make_product(price=80.0)])

    assert [e.type for e in events] == [LOWEST_PRICE]
    assert events[0].detail["previous_lowest"] == 100.0
    entry = store.data["products"]["shop:p1"]
    assert entry["lowest_price_ever"] == 80.0


def test_price_rising_back_keeps_lowest(store):
    apply(store, [make_product(price=100.0)])
    apply(store, [make_product(price=80.0)])
    events = apply(store, [make_product(price=120.0)])

    assert events == []
    assert store.data["products"]["shop:p1"]["lowest_price_ever"] == 80.0


def test_first_sighting_sets_lowest_without_lowest_price_event(store):
    events = apply(store, [make_product(price=100.0)])
    assert [e.type for e in events] == [NEW_ARRIVAL]
    assert store.data["products"]["shop:p1"]["lowest_price_ever"] == 100.0


def test_currency_change_resets_lowest_price_baseline(store):
    """EUR 100 → JPY 16000 を「値上がり」と誤認しないこと."""
    apply(store, [make_product(price=100.0, currency="EUR")])
    events = apply(store, [make_product(price=16000.0, currency="JPY")])

    assert [e.type for e in events] == []
    assert store.data["products"]["shop:p1"]["lowest_price_ever"] == 16000.0


def test_missing_price_does_not_break_history(store):
    apply(store, [make_product(price=None)])
    entry = store.data["products"]["shop:p1"]
    assert entry.get("price_history") in (None, [])
    assert entry.get("lowest_price_ever") is None


# ----------------------------------------------------------------------
# 重複通知の抑止
# ----------------------------------------------------------------------
def test_same_event_is_not_notified_twice(store):
    product = make_product()
    event = Event(NEW_ARRIVAL, product, "Shop")
    apply(store, [product])

    assert store.should_notify(event, cooldown_hours=24) is True
    store.mark_notified(event)
    assert store.should_notify(event, cooldown_hours=24) is False


def test_stock_change_within_cooldown_is_still_suppressed(store):
    apply(store, [make_product(sizes_in_stock=["46"])])
    first = Event(RESTOCK, make_product(sizes_in_stock=["46"]), "Shop")
    store.mark_notified(first)

    changed = Event(RESTOCK, make_product(sizes_in_stock=["46", "50"]), "Shop")
    assert store.should_notify(changed, cooldown_hours=24) is False
    # クールダウンを過ぎれば、在庫が変わっているので通知してよい
    assert store.should_notify(changed, cooldown_hours=24, now=now_jst() + timedelta(hours=25)) is True


def test_different_event_types_are_tracked_separately(store):
    product = make_product()
    apply(store, [product])
    store.mark_notified(Event(NEW_ARRIVAL, product, "Shop"))
    assert store.should_notify(Event(RESTOCK, product, "Shop"), cooldown_hours=24) is True


# ----------------------------------------------------------------------
# 永続化
# ----------------------------------------------------------------------
def test_state_roundtrip(store):
    apply(store, [make_product(), make_product(product_id="p2", price=50.0)])
    store.record_success("shop", 2, bootstrapped=True)
    store.log_events([Event(NEW_ARRIVAL, make_product(), "Shop")])
    store.save()

    reloaded = StateStore(store.path).load()
    assert set(reloaded.data["products"]) == {"shop:p1", "shop:p2"}
    assert reloaded.data["shops"]["shop"]["product_count"] == 2
    assert reloaded.data["recent_events"][0]["type"] == NEW_ARRIVAL
    assert reloaded.has_products("shop") is True
    assert reloaded.has_products("other") is False


def test_state_file_has_one_line_per_product(store):
    """git diff が「変わった商品だけ」になるようにしている."""
    apply(store, [make_product(), make_product(product_id="p2")])
    store.save()
    lines = store.path.read_text(encoding="utf-8").splitlines()
    assert sum(1 for line in lines if line.startswith('    "shop:')) == 2


def test_corrupt_state_file_raises_clearly(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="壊れています"):
        StateStore(path).load()


def test_failure_tracking_increments(store):
    meta = store.record_failure("shop", "HTTP 403")
    assert meta["consecutive_failures"] == 1
    meta = store.record_failure("shop", "HTTP 403")
    assert meta["consecutive_failures"] == 2

    store.record_success("shop", 5, bootstrapped=False)
    assert store.shop_meta("shop")["consecutive_failures"] == 0
    assert store.shop_meta("shop")["last_error"] is None


def test_currency_change_resets_price_history(store):
    """通貨が変わったら、比較できない過去の値を混ぜたまま残さない."""
    apply(store, [make_product(price=100.0, currency="EUR")])
    apply(store, [make_product(price=16000.0, currency="JPY")])

    history = store.data["products"]["shop:p1"]["price_history"]
    assert [h["price"] for h in history] == [16000.0]


# ----------------------------------------------------------------------
# 誤検知の防止（取得もれを完売と誤判定しない）
# ----------------------------------------------------------------------
def test_partial_harvest_does_not_mark_products_sold_out(store):
    """取得数が大きく減った回は完売判定を見送る.

    ページ送りの失敗で一部しか取れなかった回に完売扱いすると、次の回で
    戻ってきた商品がまとめて「再入荷」として通知されてしまう。
    """
    products = [make_product(product_id=f"p{i}") for i in range(10)]
    apply(store, products)
    store.record_success("shop", 10, bootstrapped=True)

    # 3件しか取れなかった回（前回の8割=8件を大きく下回る）
    events = apply(store, products[:3], full_coverage=True)
    assert events == []
    assert all(store.data["products"][f"shop:p{i}"]["in_stock"] for i in range(10))

    # 戻ってきても「再入荷」が大量発生しない
    assert apply(store, products, full_coverage=True) == []


def test_small_drop_still_marks_products_sold_out(store):
    """通常の売り切れ（少数が一覧から消える）はこれまでどおり検知する."""
    products = [make_product(product_id=f"p{i}") for i in range(10)]
    apply(store, products)
    store.record_success("shop", 10, bootstrapped=True)

    apply(store, products[:9], full_coverage=True)   # 1件だけ消えた
    assert store.data["products"]["shop:p9"]["in_stock"] is False
    assert store.data["products"]["shop:p9"]["status"] == "gone"

    events = apply(store, products, full_coverage=True)
    assert [e.type for e in events] == [RESTOCK]


def test_first_run_is_not_treated_as_a_partial_harvest(store):
    """前回の記録が無い初回は、件数を比べる相手がいないので通常どおり扱う."""
    events = apply(store, [make_product()], full_coverage=True)
    assert [e.type for e in events] == [NEW_ARRIVAL]
