# Our Legacy 入荷監視システム（MVP）

ブランド「Our Legacy」の商品を公式サイトと3つのセレクトショップ（END. / SSENSE / Farfetch）で監視し、**新規入荷 / 再入荷（サイズ復活を含む）/ 過去最安値の更新**を Slack に通知します。
GitHub Actions で1時間ごとに動き、状態は `state/state.json` に、価格推移グラフは GitHub Pages に出します。サーバーは不要です。

仕様書: [`ourlegacy_monitor_spec.md`](ourlegacy_monitor_spec.md)

---

## MVP対象5サイトの到達性（実測）

仕様書 3.2.1 が挙げた5サイトについて、実際にリクエストして確認した結果です（2026-09-05 時点）。

| サイト | 状態 | 取得件数 | 取得できるデータ | 必要な対策 |
|---|---|---|---|---|
| Our Legacy 公式 | ✅ 稼働 | 全2,067件を巡回 | 価格・**サイズ別在庫**・SKU | なし |
| END. Clothing | ✅ 稼働 | 140件（全件） | 価格・在庫数・画像 | なし |
| SSENSE | ✅ 稼働 | 508件（全件） | 価格(USD)・在庫・画像 | ブラウザ偽装 |
| Farfetch | ✅ 稼働 | 最大384件 | 価格(**JPY**)・在庫・画像 | ブラウザ偽装 + 低速化 |
| MR PORTER | ❌ 未対応 | — | — | フェーズ3（下記） |

### なぜ最初3サイトが403だったのか

SSENSE / Farfetch / MR PORTER は、**TLSフィンガープリント**を見てアクセスを弾いていました。

かみ砕くと、HTTPS通信を始めるときの「握手」の手順には、ソフトウェアごとに細かい癖があります。Chrome の癖、Safari の癖、Python の `requests` の癖はそれぞれ違い、サーバー側はこの癖を見るだけで「これはブラウザではなくプログラムだ」と判別できます。User-Agent（自己申告の名札）をブラウザに変えても、握手の癖は変わらないので見抜かれます。

対策として [`curl_cffi`](https://github.com/lexiforest/curl_cffi) を使っています。これは**実ブラウザと同じ握手の仕方を再現する**ライブラリです。`config.yml` の `impersonate` に `chrome` や `safari` を指定すると、そのショップだけこの方式で通信します。

```yaml
- id: farfetch
  options:
    impersonate: "safari"        # chrome だとチャレンジページが返る
    min_interval_seconds: 6.0    # 429が出やすいので間隔を長めに
```

サイトによって効くプロファイルが違うため（SSENSE は `chrome`、Farfetch は `safari`）、変更したら必ず `--dry-run` で確認してください。

なお、これは「本来アクセスできないものをこじ開ける」のではなく、**ブラウザで普通に開けるページを、ブラウザと同じ作法で1時間に1回取得している**だけです。アクセス間隔の遠慮と `robots.txt` の遵守は偽装時も同じように効いています（両サイトとも該当ページは `robots.txt` で許可されていることを確認済み）。

### MR PORTER が残っている理由

MR PORTER は偽装しても、商品一覧の代わりに**2.6KBのJavaScriptチャレンジページ**が返ります。ブラウザ上でJavaScriptを実行して初めて本物のページが表示される仕組みで、HTTPリクエストだけでは突破できません。選択肢は次の3つです。

| 方法 | 費用 | 難易度 | 備考 |
|---|---|---|---|
| **Playwright**（本物のブラウザを動かす） | 無料 | 中 | GitHub Actions上でも動く。実行時間とメモリを食う。それでも弾かれる可能性あり |
| **スクレイピング代行サービス**（ScraperAPI / ZenRows） | 月$0〜49 | 低 | 無料枠は月1,000〜5,000リクエスト程度。設定を書くだけで済む |
| **自宅のMacで実行**（家庭用IPを使う） | 無料 | 中 | データセンターIPより通りやすい。Macを起動しておく必要がある |

初級者の方には**代行サービスの無料枠**が最も手軽です。MR PORTERだけなら1時間1回×24時間×30日＝月720リクエストで、無料枠に収まります。手順は [Bot対策サイトの有効化](#bot対策サイトの有効化) を参照してください。

## セットアップ

### 1. Slack の準備

1. 通知用チャンネルを作る（例: `#ourlegacy-alerts`）
2. [Slack API](https://api.slack.com/apps) → *Create New App* → *Incoming Webhooks* を有効化
3. *Add New Webhook to Workspace* でチャンネルを選び、Webhook URL をコピー

エラー通知を別チャンネルに分けたい場合は、2本目の Webhook も作っておきます。

### 2. GitHub リポジトリの作成

```bash
git init
git add .
git commit -m "feat: Our Legacy 入荷監視システム MVP"
gh repo create ourlegacy-monitor --private --source=. --push
```

> **⚠️ 実行時間の無料枠について**（プライベートリポジトリの場合は必読）
>
> GitHub Actions は**パブリックリポジトリなら無料無制限**ですが、**プライベートは月2,000分**までです。
> 4ショップ全部を回すと1回あたり **8〜13分**（Farfetchのレート制限待ちが大半）かかるため、
> 毎時実行すると月6,000〜9,000分となり、**プライベートでは大幅に超過します**（超過分は従量課金）。
>
> 対処は次のいずれかです。おすすめは上から順:
>
> | 方法 | 月あたり | 手順 |
> |---|---|---|
> | **リポジトリをパブリックにする** | 無料無制限 | *Settings > General > Change visibility*。Webhook URLはSecretsにあるので公開されません。公開されるのは監視対象の商品リストとグラフだけです |
> | **実行を3時間ごとにする** | 約2,000〜3,000分 | `.github/workflows/monitor.yml` の cron を `'0 */3 * * *'` に変更 |
> | **重いショップだけ間引く** | 約1,500分 | Farfetchの `max_pages` を `3` に下げる、または `enabled: false` にする |
> | **自分のMacで動かす** | 無料 | Actionsを使わず `launchd` / `cron` でローカル実行（Macの起動が必要） |

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

## 稼働を始めたあとの進め方

| 時期 | やること | 見るところ |
|---|---|---|
| 初回実行の直後 | Slackに「初回スキャンが完了しました」が4件（4ショップ分）届くのを確認 | Slackチャンネル |
| 1〜2時間後 | 2回目以降で差分通知が動き出す。Actionsが緑になっているか確認 | *Actions* タブ |
| 翌日 | ダッシュボードに各ショップの「最終成功」が並ぶか確認 | GitHub Pages |
| 1週間後 | 通知が多すぎ/少なすぎないか調整（`cooldown_hours`、`max_messages_per_run`） | `config.yml` |
| 随時 | 欲しい商品が決まったら公式サイトの `watchlist` に追加 | `config.yml` |

うまくいかないときの見方:

- **Slackに何も来ない** → *Actions* タブでワークフローが緑か確認。赤ならログの「取得に失敗」を読む。緑なのに来ないなら `SLACK_WEBHOOK_URL` の登録名を確認する
- **特定のショップだけ失敗し続ける** → ダッシュボードの「連続失敗」欄を見る。サイト改修で構造が変わったか、`impersonate` のプロファイルが効かなくなった可能性
- **通知が多すぎる** → `notify.cooldown_hours` を伸ばす、`notify.events` で種別を絞る
- **グラフが出ない** → 価格が2回以上記録されるまでグラフは作られません（変化がなければ記録もされません）。数日待つ

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
| `http.rate_limit_backoff_seconds` | `20` | 429を受けたときの初回待機秒数（以降は倍々） |
| `shops[].enabled` | — | ショップごとのON/OFF |
| `shops[].options.impersonate` | — | ブラウザ偽装のプロファイル（`chrome` / `safari` など） |
| `shops[].options.min_interval_seconds` | — | そのショップだけアクセス間隔を変える |
| `shops[].options.max_pages` | — | 一覧ページを何ページまで辿るか |
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

#### SSENSE / Farfetch（設定済み・追加作業なし）

`impersonate` の指定だけで動きます。プロファイルが効かなくなったら `config.yml` の値を
`chrome` / `safari` / `chrome131` / `safari18_0` などに変えて `--dry-run` で試してください。

#### MR PORTER（代行サービスが必要）

1. [ScraperAPI](https://www.scraperapi.com/) か [ZenRows](https://www.zenrows.com/) で無料アカウントを作り、APIキーを取得する
2. リポジトリの *Settings > Secrets and variables > Actions* に `SCRAPER_PROXY_API_KEY` として登録する
3. `config.yml` を編集する:
   ```yaml
   scraping:
     proxy:
       enabled: true
       url_template: "https://api.scraperapi.com/?api_key={key}&url={url}&render=true"
       shops: [mrporter]
   ```
   （ZenRows の場合: `"https://api.zenrows.com/v1/?apikey={key}&url={url}&js_render=true"`）
4. `mrporter` の `enabled` を `true` に変える
5. `python -m monitor run --dry-run --shop mrporter` で商品が取れるか確認する

`render=true` / `js_render=true` は「代行サービス側でブラウザを起動してJavaScriptを実行してから返す」オプションで、MR PORTERにはこれが必須です。1リクエストあたりの消費が大きいプランが多いので、無料枠の残量に注意してください。

商品が取れたら `src/monitor/scrapers/blocked_sites.py` の `MrPorterScraper` を実データに合わせて調整します（現状はJSON-LDを読む汎用実装のままで、**MR PORTERの実データでは未検証**です）。

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

- **公式サイトは全商品を毎時は見られません**。約2,000商品あり、1回150件ずつ巡回します。新規入荷は sitemap を毎回全件読むので即時ですが、既存商品の値下げ・再入荷の検知には最大14時間かかります（`max_product_fetches` で調整可能）。
- **END. と SSENSE / Farfetch はサイズ別の在庫が取れません**。一覧ページが総在庫しか返さないためで、「在庫あり/なし」単位の判定になります。サイズ単位で追えるのは公式サイトのみです。
- **通貨がショップごとに違います**（公式=EUR、END.=GBP、SSENSE=USD、Farfetch=JPY）。過去最安値の比較は同一ショップ内でのみ行うので実害はありませんが、ショップ間の価格比較はできません。
- **Farfetch はレート制限（429）が厳しい**です。8秒間隔・最大4ページ（384件）に抑え、429を受けたら20秒→40秒→80秒と待ちます。それでも失敗する回はあり、失敗はSlackに通知されて次の実行で自動復帰します。短時間に何度も手動実行すると数十分ブロックされるので注意してください。
- **MR PORTER は未対応**です（上記）。
- GitHub Actions の cron は指定時刻から数分〜十数分ずれることがあります。

## スコープ外

自動購入・自動チェックアウトは実装しません（仕様書 非機能要件2）。本システムは通知のみを行います。

## 新しいショップを追加する

1. `src/monitor/scrapers/` に `Scraper` を継承したクラスを作り、`fetch_products() -> list[Product]` を実装
2. `src/monitor/scrapers/registry.py` の `SCRAPERS` に1行追加
3. `config.yml` の `shops` に定義を追加

一覧ページが商品ごとに1回しか見られない（全件を毎回は取れない）場合は、クラス属性 `full_coverage = False` を付けてください。「一覧に無い＝取扱終了」とみなさなくなります。
