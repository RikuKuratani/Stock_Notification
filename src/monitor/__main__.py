"""コマンドラインエントリポイント.

    python -m monitor run                 # 1サイクル実行（通知あり）
    python -m monitor run --dry-run       # Slackに送らずログ出力だけ
    python -m monitor run --shop ourlegacy
    python -m monitor check                # 設定と到達性の確認
    python -m monitor notify-test          # Slackにテスト通知を1件送る
    python -m monitor report               # stateからグラフとHTMLを作り直す
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import load_config
from .http import build_session
from .notify import SlackNotifier
from .report import ReportBuilder
from .runner import Runner
from .scrapers import SCRAPERS
from .state import StateStore

log = logging.getLogger("monitor")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state = StateStore(args.state).load()

    notifier = SlackNotifier(
        webhook_url=config.slack_webhook_url,
        error_webhook_url=config.slack_error_webhook_url,
        dry_run=args.dry_run,
    )
    if not notifier.configured and not args.dry_run:
        log.warning("SLACK_WEBHOOK_URL が未設定です。通知内容はログにのみ出力します。")

    summary = Runner(config, state, notifier, only_shops=args.shop).run()

    for warning in summary.warnings:
        log.warning(warning)
    log.info("実行結果: %s", summary.as_text())

    if not args.no_save:
        state.save()
        log.info("state を保存しました: %s", state.path)

    # すべてのショップが失敗したときだけ異常終了させる（部分的な失敗は通知で扱う）
    if summary.shops_ok == 0 and summary.shops_failed > 0:
        log.error("すべてのショップで取得に失敗しました")
        return 1
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state = StateStore(args.state).load()

    print(f"ブランド        : {config.brand}")
    print(f"state           : {state.path} ({'あり' if state.path.exists() else 'なし'})")
    print(f"追跡中の商品    : {len(state.data.get('products', {}))} 件")
    print(f"Slack Webhook   : {'設定済み' if config.slack_webhook_url else '未設定'}")
    print(f"Pages base_url  : {config.report.base_url or '未設定（通知にグラフは付きません）'}")
    print(f"代行プロキシ    : {'有効' if config.proxy.api_key and config.proxy.enabled else '無効'}")
    print("\nショップ:")

    exit_code = 0
    for shop in config.shops:
        mark = "有効" if shop.enabled else "無効"
        known = "OK" if shop.scraper in SCRAPERS else "スクレイパー未登録!"
        meta = state.data.get("shops", {}).get(shop.id, {})
        status = ""
        if meta.get("consecutive_failures"):
            status = f" / 連続失敗 {meta['consecutive_failures']} 回"
        elif meta.get("last_success_at"):
            status = f" / 最終成功 {meta['last_success_at']}"
        print(f"  [{mark}] {shop.id:<14} {shop.scraper:<14} {known}{status}")
        if shop.scraper not in SCRAPERS:
            exit_code = 1

    if args.probe:
        print("\n到達性チェック:")
        for shop in config.enabled_shops():
            session = build_session(shop.id, config, str(shop.options.get("impersonate", "")),
                                shop.options.get("min_interval_seconds"))
            urls = shop.options.get("listing_urls") or [shop.options.get("sitemap_index_url")]
            for url in [u for u in urls if u][:1]:
                try:
                    allowed = session.allowed(url)
                    resp = session.get(url)
                    print(f"  {shop.id:<14} HTTP {resp.status_code} / {len(resp.content):,} bytes"
                          f" / robots: {'許可' if allowed else '不許可'}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  {shop.id:<14} 失敗: {type(exc).__name__}: {exc}")
                    exit_code = 1
    return exit_code


def cmd_notify_test(args: argparse.Namespace) -> int:
    """Slackへの経路が通っているかだけを確かめる（切り分け用）."""
    config = load_config(args.config)
    url = config.slack_webhook_url

    if not url:
        print("❌ SLACK_WEBHOOK_URL が設定されていません。")
        print("   ローカル : export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'")
        print("   Actions  : Settings > Secrets and variables > Actions に登録")
        return 1
    if not url.startswith("https://hooks.slack.com/"):
        print(f"❌ Webhook URL の形式が不正です: {url[:40]}...")
        print("   'https://hooks.slack.com/services/...' の形式である必要があります。")
        return 1

    print(f"Webhook URL: https://hooks.slack.com/services/…{url[-6:]}")

    import requests

    payload = {"text": ":white_check_mark: Our Legacy 入荷監視システムからのテスト通知です。"
                       "これが見えていれば、通知の経路は正常です。"}
    try:
        resp = requests.post(url, json=payload, timeout=15)
    except requests.RequestException as exc:
        print(f"❌ 送信に失敗しました: {exc}")
        return 1

    if resp.status_code == 200 and resp.text.strip() == "ok":
        print("✅ 送信しました。Slackのチャンネルを確認してください。")
        return 0

    print(f"❌ Slack がエラーを返しました: HTTP {resp.status_code} / {resp.text[:200]}")
    hints = {
        "invalid_token": "Webhook が無効化されています。Slack Appで再発行してください。",
        "no_service": "URL が間違っているか、Webhook が削除されています。",
        "channel_not_found": "通知先チャンネルが存在しません（削除・改名されていませんか）。",
        "no_text": "送信内容が空でした（バグの可能性があります）。",
    }
    hint = hints.get(resp.text.strip())
    if hint:
        print(f"   → {hint}")
    return 1


def cmd_report(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state = StateStore(args.state).load()
    builder = ReportBuilder(config.report, state)
    charted = builder.build_charts()
    builder.write_dashboard(charted)
    if state.dirty and not args.no_save:
        state.save()
    print(f"{Path(config.report.output_dir) / 'index.html'} を生成しました"
          f"（グラフ {builder.generated} 枚を更新 / 掲載 {len(charted)} 件）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="monitor", description="Our Legacy 入荷監視システム")
    parser.add_argument("--config", default="config.yml", help="設定ファイル（既定: config.yml）")
    parser.add_argument("--state", default="state/state.json", help="状態ファイル")
    parser.add_argument("-v", "--verbose", action="store_true", help="デバッグログを出す")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="1サイクル実行する")
    run.add_argument("--dry-run", action="store_true", help="Slackに送らずログに出す")
    run.add_argument("--shop", action="append", help="対象ショップID（複数指定可）")
    run.add_argument("--no-save", action="store_true", help="state を保存しない")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="設定を確認する")
    check.add_argument("--probe", action="store_true", help="各ショップへ1回だけ実アクセスする")
    check.set_defaults(func=cmd_check)

    notify_test = sub.add_parser("notify-test", help="Slackにテスト通知を1件送る")
    notify_test.set_defaults(func=cmd_notify_test)

    report = sub.add_parser("report", help="stateからグラフとダッシュボードを作り直す")
    report.add_argument("--no-save", action="store_true")
    report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
