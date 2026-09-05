"""価格推移グラフと簡易ダッシュボードの生成（仕様書 4.6）.

GitHub Actions のワークフロー内で ``docs/`` 以下にPNGとHTMLを書き出し、
そのままコミットして GitHub Pages で配信する。追加のサーバーは要らない。
Slack通知には、ここで生成したPNGの公開URLを添付する。
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ReportConfig
from .events import EVENT_LABELS
from .models import slugify
from .notify import format_price
from .state import StateStore, parse_iso

log = logging.getLogger(__name__)

def _last_history_date(entry: dict[str, Any]) -> str:
    """その商品の価格が最後に動いた日時。グラフを選ぶ優先度に使う."""
    history = entry.get("price_history") or []
    return str(history[-1].get("date", "")) if history else ""


_plotting: tuple[Any, Any] | None = None


def _pyplot() -> tuple[Any, Any]:
    """matplotlib はグラフを描くときだけ読み込む.

    通知だけを行う実行（notify-test など）で重い描画ライブラリを要求しないため。
    以前はモジュール先頭で読み込んでいたため、matplotlib が入っていない環境では
    Slackのテスト送信すら起動できなかった。
    """
    global _plotting
    if _plotting is None:
        import matplotlib

        matplotlib.use("Agg")  # ヘッドレス環境（GitHub Actions）で描画する
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt

        # 商品名に日本語が混ざっても豆腐にならないよう、CJKフォントがあれば使う。
        # GitHub Actions の ubuntu-latest には無いので、最後は DejaVu Sans に落ちる。
        plt.rcParams["font.sans-serif"] = [
            "Hiragino Sans",
            "Noto Sans CJK JP",
            "IPAexGothic",
            "DejaVu Sans",
        ]
        plt.rcParams["axes.unicode_minus"] = False
        _plotting = (plt, mdates)
    return _plotting

_INK = "#1c1c1c"
_ACCENT = "#c2410c"
_GRID = "#e5e5e5"


class ReportBuilder:
    def __init__(self, config: ReportConfig, state: StateStore) -> None:
        self.config = config
        self.state = state
        self.root = Path(config.output_dir)
        self.charts_dir = self.root / "charts"
        self.generated = 0

    # ------------------------------------------------------------------
    def chart_relpath(self, key: str, entry: dict[str, Any]) -> str:
        shop = slugify(entry.get("shop_id") or "shop", 30)
        name = slugify(entry.get("product_id") or key, 60)
        return f"charts/{shop}/{name}.png"

    def chart_url(self, key: str, entry: dict[str, Any]) -> str:
        """Slack に貼るための公開URL。base_url 未設定なら空文字."""
        base = self.config.base_url.rstrip("/")
        if not base:
            return ""
        if not (self.root / self.chart_relpath(key, entry)).exists():
            return ""
        return f"{base}/{self.chart_relpath(key, entry)}"

    # ------------------------------------------------------------------
    def build_charts(self, priority_keys: set[str] | None = None) -> list[tuple[str, dict[str, Any]]]:
        """価格推移グラフを生成し、ダッシュボードに載せる商品リストを返す.

        通知にグラフURLを添えるため、Slack送信より **前** に呼ぶ。
        ``priority_keys`` は今回イベントが出た商品で、上限に収まらない場合でも
        必ず生成する。
        """
        if not self.config.enabled:
            return []
        self.root.mkdir(parents=True, exist_ok=True)
        priority_keys = priority_keys or set()

        chartable = [
            (key, entry)
            for key, entry in self.state.iter_products()
            if len(entry.get("price_history") or []) >= 2
        ]
        # 上限に収まるよう「今回イベントが出た商品 → 最近確認した商品」の順に選ぶ。
        # 2段階に分けているのは、Python の sort が安定なのを利用して
        # 「イベント優先」を保ったまま「新しい順」を維持するため。
        chartable.sort(key=lambda kv: _last_history_date(kv[1]), reverse=True)
        chartable.sort(key=lambda kv: kv[0] not in priority_keys)
        selected = chartable[: self.config.max_charts]

        for key, entry in selected:
            try:
                self._render_chart(key, entry)
            except Exception as exc:  # noqa: BLE001 - グラフ生成の失敗で監視を止めない
                log.warning("グラフ生成に失敗 %s: %s", key, exc)
        return selected

    def write_dashboard(self, charted: list[tuple[str, dict[str, Any]]]) -> None:
        """ダッシュボードHTMLを書き出す（通知履歴を含むので送信の後に呼ぶ）."""
        if not self.config.enabled:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_dashboard(charted)

    # ------------------------------------------------------------------
    def _render_chart(self, key: str, entry: dict[str, Any]) -> None:
        history = entry.get("price_history") or []
        revision = len(history)
        chart_meta = entry.get("chart") or {}
        relpath = self.chart_relpath(key, entry)
        target = self.root / relpath

        # 履歴が伸びていなければ再生成しない（リポジトリの肥大化を防ぐ）
        if chart_meta.get("rev") == revision and target.exists():
            return

        points = [
            (parse_iso(h.get("date")), h.get("price"))
            for h in history
            if parse_iso(h.get("date")) and h.get("price") is not None
        ]
        if len(points) < 2:
            return
        points.sort(key=lambda p: p[0])
        xs = [p[0] for p in points]
        ys = [float(p[1]) for p in points]

        plt, mdates = _pyplot()
        target.parent.mkdir(parents=True, exist_ok=True)
        fig, ax = plt.subplots(figsize=(7.2, 3.2), dpi=110)
        ax.step(xs, ys, where="post", color=_ACCENT, linewidth=2, marker="o", markersize=4)

        lowest = entry.get("lowest_price_ever")
        if lowest is not None:
            ax.axhline(float(lowest), color=_INK, linewidth=1, linestyle="--", alpha=0.45)
            ax.annotate(
                # フォント非依存にするため、グラフ内のラベルはASCIIで書く
                f"Lowest {format_price(float(lowest), entry.get('currency', ''))}",
                xy=(xs[0], float(lowest)),
                xytext=(2, 4),
                textcoords="offset points",
                fontsize=8,
                color=_INK,
                alpha=0.7,
            )

        ax.set_title(str(entry.get("product_name", ""))[:70], fontsize=10, color=_INK, loc="left")
        ax.set_ylabel(entry.get("currency", ""), fontsize=8, color=_INK)
        ax.grid(True, color=_GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        # 同日中に複数点が並ぶ場合、日付だけだと目盛りが全部同じに見えるので時刻も出す
        span_days = (xs[-1] - xs[0]).total_seconds() / 86400
        ax.xaxis.set_major_formatter(
            mdates.DateFormatter("%m/%d %H:%M" if span_days < 3 else "%m/%d")
        )
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=2, maxticks=6))
        fig.autofmt_xdate(rotation=0, ha="center")
        fig.tight_layout()
        fig.savefig(target, facecolor="white")
        plt.close(fig)

        entry["chart"] = {"rev": revision, "path": relpath}
        self.state.dirty = True
        self.generated += 1

    # ------------------------------------------------------------------
    def _write_dashboard(self, charted: list[tuple[str, dict[str, Any]]]) -> None:
        e = html.escape
        shops = self.state.data.get("shops", {})
        events = list(reversed(self.state.data.get("recent_events", [])))[
            : self.config.recent_events
        ]
        products = self.state.data.get("products", {})
        in_stock = sum(1 for v in products.values() if v.get("in_stock"))

        rows_shop = "\n".join(
            f"<tr><td>{e(shop_id)}</td>"
            f"<td>{e(str(meta.get('product_count', 0)))}</td>"
            f"<td>{e(str(meta.get('last_success_at') or '—'))}</td>"
            f"<td class='{'bad' if meta.get('consecutive_failures') else 'ok'}'>"
            f"{'連続失敗 ' + str(meta.get('consecutive_failures')) + ' 回' if meta.get('consecutive_failures') else '正常'}</td>"
            f"<td class='err'>{e(str(meta.get('last_error') or ''))[:200]}</td></tr>"
            for shop_id, meta in sorted(shops.items())
        )

        rows_event = "\n".join(
            f"<tr><td>{e(str(ev.get('at', ''))[:16].replace('T', ' '))}</td>"
            f"<td><span class='tag t-{e(str(ev.get('type')))}'>"
            f"{e(EVENT_LABELS.get(str(ev.get('type')), str(ev.get('type'))))}</span></td>"
            f"<td>{e(str(ev.get('shop_name', '')))}</td>"
            f"<td><a href='{e(str(ev.get('url', '')))}' target='_blank' rel='noopener'>"
            f"{e(str(ev.get('name', '')))}</a></td>"
            f"<td>{e(format_price(ev.get('price'), str(ev.get('currency', ''))))}</td></tr>"
            for ev in events
        ) or "<tr><td colspan='5'>まだイベントはありません</td></tr>"

        cards = "\n".join(
            f"<figure><a href='{e(str(entry.get('product_url', '')))}' target='_blank' rel='noopener'>"
            f"<img loading='lazy' src='{e(self.chart_relpath(key, entry))}' alt='{e(str(entry.get('product_name', '')))}'></a>"
            f"<figcaption>{e(str(entry.get('product_name', '')))}<br>"
            f"<small>{e(str(entry.get('shop_id', '')))} / 現在 "
            f"{e(format_price(entry.get('price'), str(entry.get('currency', ''))))} / 最安 "
            f"{e(format_price(entry.get('lowest_price_ever'), str(entry.get('currency', ''))))}</small>"
            f"</figcaption></figure>"
            for key, entry in charted
            if (self.root / self.chart_relpath(key, entry)).exists()
        ) or "<p>価格が2回以上記録された商品が出ると、ここにグラフが並びます。</p>"

        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.root.joinpath("index.html").write_text(
            _TEMPLATE.format(
                generated_at=e(generated_at),
                total=len(products),
                in_stock=in_stock,
                shop_count=len(shops),
                event_count=len(events),
                rows_shop=rows_shop or "<tr><td colspan='5'>—</td></tr>",
                rows_event=rows_event,
                cards=cards,
            ),
            encoding="utf-8",
        )
        self.root.joinpath(".nojekyll").write_text("", encoding="utf-8")


_TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Our Legacy 入荷監視ダッシュボード</title>
<style>
  :root {{ color-scheme: light dark; --ink:#1c1c1c; --muted:#6b6b6b; --line:#e5e5e5; --bg:#fbfaf8; --card:#fff; --accent:#c2410c; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --ink:#ececec; --muted:#a0a0a0; --line:#333; --bg:#161514; --card:#1f1e1d; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.6 -apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif; }}
  .wrap {{ max-width:1100px; margin:0 auto; padding:32px 20px 64px; }}
  h1 {{ font-size:22px; margin:0 0 4px; letter-spacing:.02em; }}
  h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:40px 0 12px; font-weight:600; }}
  .meta {{ color:var(--muted); font-size:13px; margin-bottom:24px; }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
  .stat b {{ display:block; font-size:26px; font-variant-numeric:tabular-nums; }}
  .stat span {{ color:var(--muted); font-size:12px; }}
  .scroll {{ overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:10px; font-size:13px; }}
  th, td {{ text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.05em; }}
  tr:last-child td {{ border-bottom:0; }}
  td.ok {{ color:#166534; }} td.bad {{ color:#b91c1c; font-weight:600; }}
  td.err {{ color:var(--muted); font-family:ui-monospace,monospace; font-size:11px; max-width:320px; }}
  a {{ color:inherit; }}
  .tag {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px; border:1px solid var(--line); white-space:nowrap; }}
  .t-lowest_price {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
  .t-restock {{ border-color:var(--ink); }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:16px; }}
  figure {{ margin:0; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  figure img {{ width:100%; display:block; }}
  figcaption {{ padding:10px 12px; font-size:13px; }}
  figcaption small {{ color:var(--muted); }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Our Legacy 入荷監視ダッシュボード</h1>
  <p class="meta">最終更新 {generated_at}</p>

  <div class="stats">
    <div class="stat"><b>{total}</b><span>追跡中の商品</span></div>
    <div class="stat"><b>{in_stock}</b><span>在庫あり</span></div>
    <div class="stat"><b>{shop_count}</b><span>監視ショップ</span></div>
    <div class="stat"><b>{event_count}</b><span>直近のイベント</span></div>
  </div>

  <h2>ショップの取得状況</h2>
  <div class="scroll"><table>
    <thead><tr><th>ショップ</th><th>商品数</th><th>最終成功</th><th>状態</th><th>直近のエラー</th></tr></thead>
    <tbody>{rows_shop}</tbody>
  </table></div>

  <h2>直近のイベント</h2>
  <div class="scroll"><table>
    <thead><tr><th>日時</th><th>種別</th><th>ショップ</th><th>商品</th><th>価格</th></tr></thead>
    <tbody>{rows_event}</tbody>
  </table></div>

  <h2>価格推移</h2>
  <div class="grid">{cards}</div>
</div>
</body>
</html>
"""
