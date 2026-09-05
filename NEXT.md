# 次の実装候補

現状の課題整理・アイデア出し。優先度や着手順は未確定。

## 1. 運用パラメータの調整

- サイトごとの巡回上限・間隔（`max_product_fetches` / `max_pages` / `min_interval_seconds` /
  `rate_limit_backoff_seconds`）を、実行ログ（所要時間・取得件数・429発生率）を見ながら見直す
  - 特にFarfetchは打ち切ると完売判定を見送ってしまうトレードオフあり（config.yml参照）
- GitHub Actions の `timeout-minutes: 40` に対する実行時間の余裕を定期確認

## 2. 対象サイトの拡大

- MR PORTER 対応（README記載の3案: Playwright / スクレイピング代行 / 自宅Mac実行）
- 仕様書3.2の未着手候補から次の対象を選定
  （Nordstrom, Mytheresa, YOOX, LUISAVIAROMA, HBX, Liberty London,
  Dover Street Market, Browns Fashion, Flannels, Harrods, Selfridges 等）
- 新サイト追加時にBot対策の状態変化（403化・チャレンジページ化）を検知するヘルスチェック

## 3. 通知・比較UIの改善

- 通貨統一表示（EUR/GBP/USD/JPYが混在しており、店舗間の価格比較がしづらい）
- 同一商品を店舗横断で紐付け、「どこが一番安いか」を一目で分かるようにする
- ダッシュボードの検索・フィルタ・並び替え機能
- 通知が大量発生したときのSlackスレッド化（チャンネルが埋まる問題への対応）

## 4. その他

- サイズ・価格帯によるユーザー条件フィルタ、全ショップ共通のウォッチリスト
  （現状 `watchlist` はOur Legacy公式のみ対応）
- `state/state.json` の肥大化対策（取り扱い終了商品のアーカイブ/削除ポリシー）
- 新サイト追加を「テストfixture必須」で運用するルール化
- スクレイピング代行サービス利用時のAPI使用量・コスト監視
