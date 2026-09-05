"""通知・レポート・オーケストレーションの検証."""

import json

import conftest  # noqa: F401
import pytest

from monitor.config import Config, NotifyConfig, ReportConfig, ShopConfig, load_config
from monitor.events import LOWEST_PRICE, NEW_ARRIVAL, RESTOCK, Event, ShopFailure
from monitor.models import Product, ScrapeResult, slugify
from monitor.notify import SlackNotifier, format_price
from monitor.report import ReportBuilder
from monitor.runner import Runner
from monitor.state import StateStore


class RecordingNotifier(SlackNotifier):
    def __init__(self, **kwargs):
        super().__init__(webhook_url="https://hooks.example/x", **kwargs)
        self.payloads: list[dict] = []

    def _post(self, payload, webhook_url):
        self.payloads.append(payload)
        self.sent += 1
        return True


def make_product(**kwargs) -> Product:
    defaults = dict(
        shop_id="shop", product_id="p1", product_url="https://example.com/p1",
        product_name="Camion Pants", price=100.0, currency="EUR",
        sizes_in_stock=["46"], in_stock=True,
    )
    defaults.update(kwargs)
    return Product(**defaults)


# ----------------------------------------------------------------------
# 表示・整形
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "price,currency,expected",
    [
        (45000, "JPY", "¥45,000"),
        (260.0, "EUR", "€260"),
        (395.5, "USD", "$395.5"),
        (400, "GBP", "£400"),
        (100, "SEK", "100 SEK"),
        (None, "JPY", "価格不明"),
    ],
)
def test_format_price(price, currency, expected):
    assert format_price(price, currency) == expected


def test_slugify_keeps_paths_safe_and_bounded():
    assert slugify("Camion Pants / Black") == "camion-pants-black"
    long = slugify("x" * 200)
    assert len(long) <= 60 and "/" not in long


# ----------------------------------------------------------------------
# Slack ペイロード
# ----------------------------------------------------------------------
def test_new_arrival_payload_contains_link_price_and_sizes():
    notifier = RecordingNotifier()
    notifier.notify_event(Event(NEW_ARRIVAL, make_product(image_url="https://img/x.jpg"), "SSENSE"))

    payload = notifier.payloads[0]
    body = json.dumps(payload, ensure_ascii=False)
    assert "新規入荷" in payload["blocks"][0]["text"]["text"]
    assert "SSENSE" in body
    assert "https://example.com/p1" in body
    assert "€100" in body
    assert "46" in body
    assert payload["blocks"][1]["accessory"]["image_url"] == "https://img/x.jpg"


def test_lowest_price_payload_shows_previous_low_and_chart():
    notifier = RecordingNotifier()
    event = Event(LOWEST_PRICE, make_product(price=80.0), "END.",
                  detail={"previous_lowest": 100.0})
    notifier.notify_event(event, chart_url="https://pages.example/charts/shop/p1.png")

    body = json.dumps(notifier.payloads[0], ensure_ascii=False)
    assert "過去最安値を更新" in body
    assert "€100" in body            # これまでの最安値
    assert "-€20" in body            # 下落幅
    image_blocks = [b for b in notifier.payloads[0]["blocks"] if b["type"] == "image"]
    assert image_blocks[0]["image_url"].endswith("/p1.png")


def test_restock_payload_lists_revived_sizes_only():
    notifier = RecordingNotifier()
    notifier.notify_event(
        Event(RESTOCK, make_product(sizes_in_stock=["46", "50"]), "Shop", detail={"sizes": ["50"]})
    )
    body = json.dumps(notifier.payloads[0], ensure_ascii=False)
    assert "復活したサイズ" in body and '"*復活したサイズ*\\n50"' in body


def test_failure_notification_lists_each_shop():
    notifier = RecordingNotifier()
    notifier.notify_failures([
        ShopFailure("ssense", "SSENSE", "HTTPError: 403", 3, "2026-09-01T10:00:00+09:00"),
    ])
    text = notifier.payloads[0]["text"]
    assert "SSENSE" in text and "3回連続失敗" in text and "403" in text


def test_notifier_without_webhook_does_not_raise():
    notifier = SlackNotifier(webhook_url="", dry_run=False)
    assert notifier.configured is False
    assert notifier.notify_event(Event(NEW_ARRIVAL, make_product(), "Shop")) is True


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------
class StubScraper:
    def __init__(self, products, full_coverage=True, error=None):
        self.products = products
        self.full_coverage = full_coverage
        self.error = error

    def run(self):
        if self.error:
            raise self.error
        return ScrapeResult("shop", self.products, self.full_coverage)


def build_runner(monkeypatch, tmp_path, scraper, notify_config=None):
    config = Config(
        shops=[ShopConfig(id="shop", name="Shop", scraper="stub")],
        notify=notify_config or NotifyConfig(),
        report=ReportConfig(enabled=False, output_dir=str(tmp_path / "docs")),
    )
    state = StateStore(tmp_path / "state.json")
    notifier = RecordingNotifier()
    monkeypatch.setattr("monitor.runner.build_scraper", lambda *a, **k: scraper)
    return Runner(config, state, notifier), state, notifier


def test_runner_bootstraps_then_detects(monkeypatch, tmp_path):
    scraper = StubScraper([make_product()])
    runner, state, notifier = build_runner(monkeypatch, tmp_path, scraper)

    summary = runner.run()
    assert summary.shops_ok == 1 and summary.events_detected == 0
    assert "初回スキャン" in notifier.payloads[0]["text"]

    # 2回目: 新商品が増えたら通知される
    scraper.products = [make_product(), make_product(product_id="p2", product_name="Box Shirt")]
    summary = runner.run()
    assert summary.events_detected == 1
    assert "Box Shirt" in json.dumps(notifier.payloads[-1], ensure_ascii=False)


def test_runner_records_failure_and_notifies(monkeypatch, tmp_path):
    runner, state, notifier = build_runner(
        monkeypatch, tmp_path, StubScraper([], error=RuntimeError("HTTP 403"))
    )
    summary = runner.run()

    assert summary.shops_failed == 1 and summary.shops_ok == 0
    assert state.shop_meta("shop")["consecutive_failures"] == 1
    assert "取得に失敗した" in notifier.payloads[-1]["text"]


def test_runner_caps_messages_per_run(monkeypatch, tmp_path):
    products = [make_product(product_id=f"p{i}") for i in range(10)]
    runner, state, notifier = build_runner(
        monkeypatch, tmp_path, StubScraper(products),
        notify_config=NotifyConfig(max_messages_per_run=3, bootstrap_summary_only=False),
    )
    state.data["products"]["shop:seed"] = {"shop_id": "shop"}  # 初回扱いを避ける

    summary = runner.run()
    assert summary.events_detected == 10
    event_messages = [p for p in notifier.payloads if "blocks" in p]
    assert len(event_messages) == 3
    assert "持ち越します" in notifier.payloads[-1]["text"]


def test_runner_prioritises_lowest_price_over_new_arrival(monkeypatch, tmp_path):
    runner, state, notifier = build_runner(
        monkeypatch, tmp_path,
        StubScraper([make_product(product_id="cheap"), make_product(product_id="fresh")]),
        notify_config=NotifyConfig(max_messages_per_run=1, bootstrap_summary_only=False),
    )
    # cheap は既知で高値、fresh は未知
    state.data["products"]["shop:cheap"] = {
        "shop_id": "shop", "in_stock": True, "sizes_in_stock": ["46"],
        "price": 999.0, "currency": "EUR", "lowest_price_ever": 999.0,
    }
    runner.run()
    sent = [p for p in notifier.payloads if "blocks" in p]
    assert len(sent) == 1
    assert "過去最安値" in json.dumps(sent[0], ensure_ascii=False)


# ----------------------------------------------------------------------
# レポート
# ----------------------------------------------------------------------
def test_report_generates_chart_and_dashboard(tmp_path):
    state = StateStore(tmp_path / "state.json")
    state.data["products"]["shop:p1"] = {
        "shop_id": "shop", "product_id": "p1", "product_name": "Camion Pants",
        "product_url": "https://example.com/p1", "price": 80.0, "currency": "EUR",
        "lowest_price_ever": 80.0, "in_stock": True, "last_checked_at": "2026-09-05T10:00:00+09:00",
        "price_history": [
            {"date": "2026-08-01T09:00:00+09:00", "price": 100.0, "in_stock": True},
            {"date": "2026-09-05T10:00:00+09:00", "price": 80.0, "in_stock": True},
        ],
    }
    state.data["shops"]["shop"] = {"product_count": 1, "last_success_at": "2026-09-05T10:00:00+09:00",
                                  "consecutive_failures": 0, "last_error": None}
    state.data["recent_events"] = [{
        "at": "2026-09-05T10:00:00+09:00", "type": LOWEST_PRICE, "shop_name": "Shop",
        "name": "Camion Pants", "url": "https://example.com/p1", "price": 80.0, "currency": "EUR",
    }]

    config = ReportConfig(enabled=True, output_dir=str(tmp_path / "docs"),
                          base_url="https://user.github.io/repo")
    builder = ReportBuilder(config, state)
    charted = builder.build_charts()
    builder.write_dashboard(charted)

    chart = tmp_path / "docs" / "charts" / "shop" / "p1.png"
    assert chart.exists() and chart.stat().st_size > 1000
    assert builder.chart_url("shop:p1", state.data["products"]["shop:p1"]) == (
        "https://user.github.io/repo/charts/shop/p1.png"
    )

    html = (tmp_path / "docs" / "index.html").read_text(encoding="utf-8")
    assert "Camion Pants" in html and "過去最安値を更新" in html
    assert "charts/shop/p1.png" in html

    # 履歴が伸びていなければ再生成しない
    builder2 = ReportBuilder(config, state)
    builder2.build_charts()
    assert builder2.generated == 0


def test_report_skips_products_without_enough_history(tmp_path):
    state = StateStore(tmp_path / "state.json")
    state.data["products"]["shop:p1"] = {
        "shop_id": "shop", "product_id": "p1", "product_name": "X",
        "price_history": [{"date": "2026-09-05T10:00:00+09:00", "price": 10.0}],
    }
    config = ReportConfig(enabled=True, output_dir=str(tmp_path / "docs"))
    builder = ReportBuilder(config, state)
    assert builder.build_charts() == []
    assert builder.chart_url("shop:p1", state.data["products"]["shop:p1"]) == ""


def test_report_html_escapes_product_names(tmp_path):
    state = StateStore(tmp_path / "state.json")
    state.data["recent_events"] = [{
        "at": "2026-09-05T10:00:00+09:00", "type": NEW_ARRIVAL, "shop_name": "Shop",
        "name": "<script>alert(1)</script>", "url": "https://x", "price": 1.0, "currency": "EUR",
    }]
    builder = ReportBuilder(ReportConfig(output_dir=str(tmp_path / "docs")), state)
    builder.write_dashboard([])
    html = (tmp_path / "docs" / "index.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ----------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------
def test_load_config_reads_shops_and_env(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        "brand: Our Legacy\n"
        "http:\n  min_interval_seconds: 2.0\n  unknown_key: ignored\n"
        "notify:\n  cooldown_hours: 6\n"
        "shops:\n"
        "  - id: ourlegacy\n    name: 公式\n    scraper: ourlegacy\n    enabled: true\n"
        "  - id: ssense\n    name: SSENSE\n    scraper: ssense\n    enabled: false\n",
        encoding="utf-8",
    )
    config = load_config(path, env={"SLACK_WEBHOOK_URL": "https://hooks/x"})

    assert config.http.min_interval_seconds == 2.0   # 未知キーがあっても落ちない
    assert config.notify.cooldown_hours == 6
    assert config.slack_webhook_url == "https://hooks/x"
    assert [s.id for s in config.enabled_shops()] == ["ourlegacy"]


def test_repo_config_matches_registry():
    """同梱の config.yml が実在のスクレイパーだけを参照していること."""
    from monitor.scrapers import SCRAPERS

    config = load_config("config.yml", env={})
    assert config.shops, "config.yml にショップ定義がありません"
    for shop in config.shops:
        assert shop.scraper in SCRAPERS, f"{shop.id}: 未登録のスクレイパー {shop.scraper}"


def test_report_prefers_event_products_when_over_the_cap(tmp_path):
    """上限を超えるときは、今回イベントが出た商品を確実にグラフ化する."""
    state = StateStore(tmp_path / "state.json")
    for i in range(5):
        state.data["products"][f"shop:p{i}"] = {
            "shop_id": "shop", "product_id": f"p{i}", "product_name": f"Item {i}",
            "currency": "EUR", "last_checked_at": f"2026-09-0{i + 1}T10:00:00+09:00",
            "price_history": [
                {"date": "2026-08-01T09:00:00+09:00", "price": 100.0},
                {"date": "2026-09-05T10:00:00+09:00", "price": 90.0},
            ],
        }
    config = ReportConfig(enabled=True, output_dir=str(tmp_path / "docs"), max_charts=2)
    charted = ReportBuilder(config, state).build_charts(priority_keys={"shop:p0"})

    keys = [key for key, _ in charted]
    assert len(keys) == 2
    assert keys[0] == "shop:p0"          # 最終確認は最も古いが、イベントがあるので優先
    assert keys[1] == "shop:p4"          # 残りは最近確認した順


# ----------------------------------------------------------------------
# HTTPクライアント: リトライとレート制限
# ----------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code, text="ok", headers=None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as rq
            raise rq.HTTPError(f"HTTP {self.status_code}")


class FakeHttpSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.responses.pop(0) if self.responses else FakeResponse(200)


def test_http_retries_then_succeeds(monkeypatch):
    from monitor.config import HttpConfig
    from monitor.http import PoliteSession

    slept = []
    monkeypatch.setattr("monitor.http.time.sleep", lambda s: slept.append(s))
    http = HttpConfig(min_interval_seconds=0, max_retries=3, respect_robots_txt=False,
                      rate_limit_backoff_seconds=20.0)
    session = PoliteSession("shop", http, session=FakeHttpSession([
        FakeResponse(429), FakeResponse(503), FakeResponse(200, "done"),
    ]))

    assert session.get_text("https://x/y") == "done"
    assert session.request_count == 3
    # 429 は長め(20秒)、5xx は通常(4秒)のバックオフ
    assert slept == [20.0, 4.0]


def test_http_honours_retry_after_header(monkeypatch):
    from monitor.config import HttpConfig
    from monitor.http import PoliteSession

    slept = []
    monkeypatch.setattr("monitor.http.time.sleep", lambda s: slept.append(s))
    http = HttpConfig(min_interval_seconds=0, max_retries=2, respect_robots_txt=False)
    session = PoliteSession("shop", http, session=FakeHttpSession([
        FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200),
    ]))

    session.get("https://x/y")
    assert slept == [7.0]   # 相手の指示に従う


def test_http_gives_up_after_max_retries(monkeypatch):
    import requests as rq

    from monitor.config import HttpConfig
    from monitor.http import PoliteSession

    monkeypatch.setattr("monitor.http.time.sleep", lambda s: None)
    http = HttpConfig(min_interval_seconds=0, max_retries=1, respect_robots_txt=False)
    session = PoliteSession("shop", http, session=FakeHttpSession([FakeResponse(429)] * 5))

    with pytest.raises(rq.HTTPError):
        session.get("https://x/y")
    assert session.request_count == 2   # 初回 + リトライ1回


def test_http_does_not_retry_404(monkeypatch):
    import requests as rq

    from monitor.config import HttpConfig
    from monitor.http import PoliteSession

    monkeypatch.setattr("monitor.http.time.sleep", lambda s: None)
    http = HttpConfig(min_interval_seconds=0, max_retries=3, respect_robots_txt=False)
    session = PoliteSession("shop", http, session=FakeHttpSession([FakeResponse(404)]))

    with pytest.raises(rq.HTTPError):
        session.get("https://x/y")
    assert session.request_count == 1


def test_robots_txt_blocks_disallowed_url():
    from monitor.config import HttpConfig
    from monitor.http import BlockedByRobotsError, PoliteSession

    robots = FakeResponse(200, "User-agent: *\nDisallow: /secret/\n")
    http = HttpConfig(min_interval_seconds=0, respect_robots_txt=True)
    session = PoliteSession("shop", http, session=FakeHttpSession([robots, FakeResponse(200)]))

    with pytest.raises(BlockedByRobotsError):
        session.get("https://x/secret/page")
    assert session.allowed("https://x/public/page") is True
