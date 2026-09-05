# Our Legacy 入荷監視システム（MVP）

ブランド「Our Legacy」の商品を公式サイトとセレクトショップで監視し、**新規入荷 / 再入荷（サイズ復活を含む）/ 過去最安値の更新**を Slack に通知します。
GitHub Actions で1時間ごとに動き、状態は `state/state.json` に、価格推移グラフは GitHub Pages に出します。サーバーは不要です。

仕様書: [`ourlegacy_monitor_spec.md`](ourlegacy_monitor_spec.md)

---

## ⚠️ まず読んでください: MVP対象5サイトの到達性

仕様書 3.2.1 が挙げた5サイトについて、実際にリクエストして確認した結果です（2026-09-05 時点）。

| サイト | 結果 | 取得できるデータ | 既定 |
|---|---|---|---|
| Our Legacy 公式 | ✅ HTTP 200 | 価格・**サイズ別在庫**・入荷日・SKU | 有効 |
| END. Clothing | ✅ HTTP 200 | 価格・在庫数・商品画像（全140件） | 有効 |
| SSENSE | ❌ HTTP 403 | — | 無効 |
| Farfetch | ❌ HTTP 403 | — | 無効 |
| MR PORTER | ❌ HTTP 403 | — （`robots.txt` すら 403） | 無効 |

後者3サイトは仕様書が「比較的Bot対策が緩い」と見込んでいましたが、**通常回線からの素のHTTPリクエストで既にブロックされます**。GitHub Actions の IP からはさらに厳しくなる想定です。

そのため本MVPでは:

- **5サイトすべてのスクレイパーを実装済み**です
- 検証できた2サイトを `enabled: true`、403の3サイトを `enabled: false` にしてあります
- 403の3サイトは、スクレイピング代行サービス（ScraperAPI / ZenRows 等）を挟めば**設定変更だけで有効化**できる作りです（→ [Bot対策サイトの有効化](#bot対策サイトの有効化)）

有効な2サイトだけでも、公式の新規入荷とサイズ別再入荷、END. の全140商品の価格・在庫は追えます。

---

## セットアップ

### 1. Slack の準備

1. 通知用チャンネルを作る（例: `#ourlegacy-alerts`）
2. [Slack API](https://api.slack.com/apps) → *Create New App* → *Incoming Webhooks* を有効化
3. *Add New Webhook to Workspace* でチャンネルを選び、Webhook URL をコピー

エラー通知を別チャンネルに分けたい場合は、2本目の Webhook も作っておきます。

### 2. GitHub リポジトリの作成

```bash
cd "$(dirname "$0")"          # このディレクトリ
git init
git add .
git commit -m "feat: Our Legacy 入荷監視システム MVP"
gh repo create ourlegacy-monitor --private --source=. --push
```

> **公開/非公開について**: GitHub Actions は**パブリックリポジトリなら実行時間が無料無制限**、プライベートだと無料枠は月2,000分です。本システムは1回あたり約5分なので、毎時実行すると月約3,600分となり**プライベートでは無料枠を超えます**。プライベートで運用する場合は `config.yml` の `max_product_fetches` を下げるか、cron を2〜3時間ごとにしてください。

### 3. シークレットの登録

リポジトリの *Settings > Secrets and variables > Actions* で登録します。

| 名前 | 必須 | 用途 |
|---|---|---|
| `SLACK_WEBHOOK_URL` | ✅ | 入荷通知の送信先 |
| `SLACK_ERROR_WEBHOOK_URL` | | 失敗通知の送信先（未設定なら上と同じ） |
| `SCRAPER_PROXY_API_KEY` | | スクレイピング代行サービスのAPIキー |

### 4. GitHub Pages（価格推移グラフ）

1. *Settings > Pages* → Source を **Deploy from a branch**、ブランチを `main` / フォルダを `/docs` に設定
2. 表示された公開URL（`https://<ユーザー名>.github.io/<リポジトリ名>`）を `config.yml` の `report.base_url` に書く

`base_url` を設定すると、Slack の最安値更新通知に価格推移グラフの画像が添付されます。未設定でも監視自体は動きます。

### 5. 初回実行

*Actions > 入荷監視 > Run workflow* から手動実行します。初回はそのショップの全商品を「既知」として登録するだけで、個別通知は飛ばしません（サマリのみ）。**差分通知は2回目以降**から始まります。

---

## ローカルでの実行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export PYTHONPATH=src

python -m monitor check --probe          # 設定と各サイトへの到達性を確認
python -m monitor run --dry-run          # Slackに送らずログ出力だけ
python -m monitor run --shop endclothing # 特定のショップだけ実行
python -m monitor report                 # stateからグラフとダッシュボードを作り直す
python -m pytest tests/ -q               # テスト
```

`--dry-run` は Slack に送る代わりに、送るはずだったペイロードをログに出します。

---

## 設定 (`config.yml`)

主に触るところ:

| 項目 | 既定 | 説明 |
|---|---|---|
| `http.min_interval_seconds` | `1.5` | 同一ショップへの連続アクセスの最小間隔（秒） |
| `http.respect_robots_txt` | `true` | `robots.txt` が禁止しているURLは取得しない |
| `notify.cooldown_hours` | `24` | 同じ商品・同じ種別を再通知しない時間 |
| `notify.max_messages_per_run` | `30` | 1回の実行で送るメッセージの上限（暴発防止） |
| `notify.events` | 全て `true` | 通知する種別を個別にON/OFF |
| `report.base_url` | 空 | GitHub Pages の公開URL |
| `shops[].enabled` | — | ショップごとのON/OFF |
| `shops[].options.max_product_fetches` | `150` | 公式サイトで1回に見る商品ページ数（後述） |
| `shops[].options.watchlist` | `[]` | 公式サイトで優先的に毎回チェックする商品（部分一致） |

### 公式サイトの巡回について

公式サイトの商品一覧はクライアント側で描画されるため、価格と在庫は**商品ページ**を見ないと分かりません。全商品は約2,000件あり毎時全件は現実的でないので、次の優先順位で `max_product_fetches` 件ずつ巡回します。

1. **まだ一度も見ていない商品** = 新規入荷（sitemap は毎回全件読むので、**新規入荷の検知は常に即時**）
2. `watchlist` に一致する商品
3. 最終確認が古い順

既定の150件だと全商品を一巡するのに約14時間かかります。狙っている商品がある場合は `watchlist` に入れると毎回チェックされます。

```yaml
watchlist:
  - "camion"
  - "borrowed shirt"
```

### Bot対策サイトの有効化

SSENSE / Farfetch / MR PORTER を動かすには、代行サービスを挟みます。

1. ScraperAPI か ZenRows でAPIキーを取得し、`SCRAPER_PROXY_API_KEY` に登録
2. `config.yml` を編集:
   ```yaml
   scraping:
     proxy:
       enabled: true
       url_template: "https://api.scraperapi.com/?api_key={key}&url={url}&render=true"
       shops: [ssense, farfetch, mrporter]
   ```
3. 対象ショップの `enabled` を `true` にする
4. `python -m monitor run --dry-run --shop ssense` で確認

この3サイトのパース処理は JSON-LD（schema.org）を読む汎用実装で、**実データでの検証は未了**です。到達できるようになった時点で、実際のHTMLに合わせて `src/monitor/scrapers/blocked_sites.py` を調整してください。

---

## 仕組み

```
GitHub Actions (毎時)
  └─ python -m monitor run
       ├─ 各ショップの Scraper.fetch_products() -> list[Product]
       ├─ StateStore.apply()   前回の state と突き合わせてイベントを検出
       ├─ ReportBuilder        価格推移グラフ(PNG) + ダッシュボード(HTML) を docs/ に生成
       ├─ SlackNotifier        クールダウンと上限を適用して通知
       └─ state.json / docs/ をコミット & push
```

| ファイル | 役割 |
|---|---|
| `src/monitor/scrapers/` | ショップ別の取得ロジック |
| `src/monitor/state.py` | `state.json` の読み書きと差分検知（システムの中核） |
| `src/monitor/notify.py` | Slack Block Kit のメッセージ組み立て |
| `src/monitor/report.py` | グラフとダッシュボードの生成 |
| `src/monitor/runner.py` | 上記の結線 |
| `state/state.json` | 商品ごとの最終状態・価格履歴・通知履歴 |
| `docs/` | GitHub Pages で配信するダッシュボード |

### 検知ロジック

| イベント | 条件 |
|---|---|
| 新規入荷 | state に無い商品が、在庫ありで現れた |
| 再入荷 | 在庫なし→在庫あり、または**在庫サイズが増えた**（サイズ復活） |
| 過去最安値を更新 | 価格が `lowest_price_ever` を下回った |

重複通知は、商品ごとに「イベント種別 + 在庫状態のハッシュ + 通知時刻」を記録して抑止します。同じ在庫状態のままなら再通知せず、状態が変わってもクールダウン中は送りません。

価格履歴は**価格または在庫状態が変わったときだけ**1件追記します（`state.json` の肥大化とコミット頻発を避けるため）。`state.json` は商品1件＝1行で書き出すので、`git diff` は変化した商品だけが並びます。

### 失敗したとき

- 1ショップの失敗では他のショップを止めません
- 失敗はショップごとに `consecutive_failures` として記録され、Slack のエラー通知にまとめて送られます
- ダッシュボードに各ショップの最終成功時刻と直近のエラーが出ます
- 全ショップが失敗したときだけワークフローが異常終了します

---

## 既知の制約

- **公式サイトは全商品を毎時は見られません**。新規入荷は即時ですが、既存商品の値下げ・再入荷の検知には最大14時間かかります（`max_product_fetches` で調整可能）。
- **END. はサイズ別の在庫が取れません**。一覧APIが総在庫数しか返さないため、「在庫あり/なし」と在庫数での判定になります。サイズ単位で追うには商品ページの取得が別途必要です。
- **公式サイトの価格は EUR 表示**です。`robots.txt` が `/jp-ja` `/jp-en` `/global-en` を禁止しているため、地域別の価格は取得していません。
- **SSENSE / Farfetch / MR PORTER は未検証**です（上記）。
- GitHub Actions の cron は指定時刻から数分〜十数分ずれることがあります。

## スコープ外

自動購入・自動チェックアウトは実装しません（仕様書 非機能要件2）。本システムは通知のみを行います。

## 新しいショップを追加する

1. `src/monitor/scrapers/` に `Scraper` を継承したクラスを作り、`fetch_products() -> list[Product]` を実装
2. `src/monitor/scrapers/registry.py` の `SCRAPERS` に1行追加
3. `config.yml` の `shops` に定義を追加

一覧ページが商品ごとに1回しか見られない（全件を毎回は取れない）場合は、クラス属性 `full_coverage = False` を付けてください。「一覧に無い＝取扱終了」とみなさなくなります。
