# OurLegacy 入荷監視・通知システム 仕様書

version 0.2 / 2026-09-05 更新（GitHub Actionsでの運用方針、過去最安値更新通知、価格推移グラフ、MVP対象店舗の確定を反映）

## 1. 目的

ブランド「Our Legacy」の商品が、公式オンラインストアおよび世界中のセレクトショップ（ECサイト）に入荷・再入荷した際に、Slackへ自動通知する。新規入荷・再入荷（在庫復活）の両方を通知対象とする。

## 2. 要件サマリ（ヒアリング結果）

| 項目 | 決定事項 |
|---|---|
| 通知先 | Slack（Incoming Webhook、既存/新規どちらのワークスペースでも可） |
| 監視対象 | 主要セレクトショップを固定リストで監視（自動発見はフェーズ2） |
| 実行環境 | GitHub Actionsに決定（4.1参照） |
| 実行頻度 | 1時間ごと |
| 検知粒度 | 新規入荷・再入荷（サイズ復活含む）に加え、過去最安値の更新を通知 |
| 可視化 | 商品ごとの価格推移グラフを確認できるようにする（4.6参照） |
| MVP対象 | 店舗数を絞った構成でスタート（3.2.1参照） |
| 絞り込み条件 | 現時点では未実装。将来応じ追加する前提でフェーズ2に計画 |
| リポジトリ | 新規作成 |

## 3. 監視対象

### 3.1 公式サイト（ourlegacy.com）

簡易調査の結果、以下が確認できた。

- バックエンドにSanity CMS（`cdn.sanity.io`）を使用している痕跡があり、SSR（サーバー側で商品情報を含むHTMLを返す）とみられる。requestsベースのスクレイピングで済ませられる可能性が高いが、実装時に生HTML/JSONレスポンスの確認が必要。
- カテゴリ別の新着ページが存在する：
  - `/mens/new-arrivals`
  - `/womens/new-arrivals`
  - `/footwear/mens-footwear`
  - `/accessories/new-arrivals`
  - `/accessories/bags`
  - `/workshop/dickies`（ワークショップ/コラボライン）
- RSSフィードやsitemap.xmlの存在は未確認。実装着手時に `robots.txt` / `sitemap.xml` を直接確認する。

参考: [Our Legacy - Official Online Shop](https://www.ourlegacy.com/)

### 3.2 初期監視対象セレクトショップ（案）

公式の [Stores ページ](https://www.ourlegacy.com/stores) と [curatedmenswear.com の取扱店一覧](https://www.curatedmenswear.com/brands/our-legacy/) を基にした初期リスト。実装前にユーザー側で優先順位・要不要を確認する想定。

**大手マーケットプレイス系（在庫データが比較的取得しやすい傾向）**

| ショップ | URL | 備考 |
|---|---|---|
| SSENSE | ssense.com | カナダ拠点、日本語UIあり |
| Farfetch | farfetch.com | マルチブランドEC最大手 |
| MR PORTER | mrporter.com | メンズ専門 |
| Nordstrom | shop.nordstrom.com | 米国 |
| Shopbop | shopbop.com | Amazon系 |
| Mytheresa | mytheresa.com | ドイツ |
| YOOX | yoox.com | イタリア |
| LUISAVIAROMA | luisaviaroma.com | イタリア |

**セレクトショップ・百貨店系（Bot対策が強い場合あり）**

| ショップ | URL | 備考 |
|---|---|---|
| END. Clothing | endclothing.com | 英国 |
| HBX | hbx.com | 香港 |
| Liberty London | liberty.co.uk | 英国百貨店 |
| Dover Street Market | shop.doverstreetmarket.com | |
| Browns Fashion | brownsfashion.com | |
| Flannels | flannels.com | 英国 |
| Harrods | harrods.com | Akamai等の防御が強い可能性 |
| Selfridges | selfridges.com | |
| Harvey Nichols | harveynichols.com | |
| Tessabit | tessabit.com | イタリア |
| Slam Jam | slamjam.com | イタリア |
| The Corner | thecorner.com | |
| Solebox | solebox.com | ドイツ |
| ANTONIA | antonia.it | イタリア |
| Kith | kith.com | 米国、ドロップ制でトラフィック集中しやすい |

> 上記は事前調査による暫定リストであり、実在庫ページのURL構造・Bot対策の有無は実装時に個別確認が必要。すべての店舗を初期スコープに含めるのはコスト（開発工数・ブロック対策）とのバランスで取捨選択することを推奨。

### 3.2.1 MVP対象ショップ（確定）

比較的Bot対策が緩く、価格・在庫情報がHTMLから取得しやすいと見込まれる以下5サイトをMVPの監視対象とする。

1. Our Legacy 公式サイト（ourlegacy.com）
2. SSENSE
3. END. Clothing
4. Farfetch
5. MR PORTER

3.2の残りの店舗は、MVPで動作確認ができた後に、フェーズ1として順次追加する。

### 3.3 将来拡張（フェーズ2以降・今回は仕様外）

- Google検索やSERP APIを用いた新規取扱店の自動発見
- ブランド名＋"stockist"等のキーワードでの定期クロール
- 上記が必要になった場合は別途仕様を追加する

## 4. アーキテクチャ提案

### 4.1 実行環境（GitHub Actions：推奨）

理由：

- サーバー管理・費用が不要（個人利用の頻度であれば無料枠内に収まりやすい）
- cronスケジュールで定期実行が可能
- Claude Codeで実装したコードをそのままリポジトリ化でき、実装と運用の距離が近い

制約・注意点：

- 実行頻度は **1時間ごと**（`cron: '0 * * * *'`）に決定。
- ジョブは都度使い捨てのコンテナで実行されるため、前回まで確認済みの商品情報を記録した状態（ステート）を外部に永続化する必要がある（4.2で後述）。
- Cloudflare/Akamai等のBot対策が強いショップ（Harrods等）は、素のHTTPリクエストでは弾かれる可能性が高い。その場合の対応方針は8.3で後述。
- リポジトリは新規作成する前提。Slack Webhook URLはコードに直接書かず、リポジトリの `Settings > Secrets and variables > Actions` にシークレットとして登録し、ワークフロー内で環境変数として参照する。
- 動作確認をスケジュール待ちせずに行えるよう、ワークフローには `workflow_dispatch`（手動実行トリガー）を設定しておくと開発時に便利。

**代替案（参考）**

| 案 | メリット | デメリット |
|---|---|---|
| GitHub Actions（推奨） | 無料・管理不要 | 実行間隔・実行時間に制約、IP がGitHub共有範囲でブロックされやすい可能性 |
| 自分のPC/Macで常時 or cron | 柔軟、ローカルデバッグしやすい | PCを常時起動する必要、外出中に止まる |
| VPS（Fly.io, Lightsail等） | 常時稼働・柔軟なスケジュール、専用IP | 月額費用が発生 |

まずはGitHub Actionsで小さく始め、Bot対策やレート制限で行き詰まる店舗が出てきたら、その店舗だけVPS実行やスクレイピング代行API（後述）に切り出す、という段階的な構成を推奨する。

### 4.2 状態管理（差分検知用ストレージ）

- 監視対象ごとに「商品URL（または商品ID）」「商品名」「価格」「サイズ別在庫」「最終確認時刻」「状態（新規/在庫あり/売り切れ）」を記録する。
- 実装コストを抑えるため、初期はJSONファイル1つ（例: `state.json`）をリポジトリにコミットして永続化する方式を推奨（GitHub Actions内でcheckout→更新→commit&push、が一般的なパターン）。
- 将来的にデータ量が増える、または複数ワークフローから同時更新する必要が出た場合は、SQLite（Git LFS管理）やSupabase等の外部DBへの移行を検討する。

### 4.3 スクレイピング方式

- 通常のHTTPリクエスト（Python: `requests` + `BeautifulSoup`、または `httpx`）で取得できるショップはそちらを優先し、実行コストを抑える。
- JavaScriptレンダリングが必須、またはBot対策で弾かれるショップはPlaywright（ヘッドレスブラウザ）で対応する。GitHub Actions上でもPlaywrightは実行可能だが、実行時間・リソース消費が増える点に注意。
- それでも継続的にブロックされる場合は、ScraperAPI/ZenRows等のスクレイピング代行サービスの利用を検討する（費用が発生するため、必要になった時点で個別検討）。

### 4.4 通知（Slack）

- Slack Incoming Webhookを使用し、専用チャンネルへ通知する。既存のSlackワークスペースがあればそこに新しいチャンネル（例：`#ourlegacy-alerts`）を作ってWebhookを発行すればよく、必ずしも新規ワークスペースを作る必要はない（個人用に新しいワークスペースを作っても問題ない）。
- 通知メッセージに含める情報（案）：商品名、ブランド、ショップ名、価格、通貨、対象サイズ（再入荷の場合はどのサイズが復活したか）、商品画像、商品ページへのリンク、検知種別（新規入荷／再入荷／過去最安値更新）。
- 同一商品・同一状態での重複通知を防ぐため、状態ストア側でのハッシュ比較を行う。

### 4.5 エラー・サイト構造変更への対応

- 各ショップのスクレイピングロジックはサイトの改修で壊れる前提で設計し、失敗時は例外を握りつぶさず「そのショップの取得に失敗した」旨の通知をSlackの別チャンネル（または同一チャンネルの別スレッド）に送る。
- 監視対象ショップが増えるほどメンテナンスコストも増えるため、ショップごとに「最終成功日時」を記録し、一定期間失敗が続いているショップを可視化できるようにする。

### 4.6 価格推移の記録と過去最安値更新通知

- 毎時のチェックすべてを価格履歴として保存すると状態ファイルが肥大化し、GitHub Actionsによるコミットも頻発するため、前回チェック時から価格・在庫状況に変化があった場合のみ、履歴に1件追記する方式を基本とする。
- 商品ごとに「これまでの最安値（`lowest_price_ever`）」を保持し、新たに取得した価格がこれを下回った場合は、通常の入荷通知とは区別して「過去最安値を更新」という形で強調通知する。
- 価格推移の可視化は、GitHub Actionsのワークフロー内で価格履歴（JSON）から簡易グラフ（PNG、matplotlib等で生成）を作成し、GitHub Pagesに自動デプロイして簡易ダッシュボードとして提供する（追加のサーバーやホスティング費用は不要）。
- 値下げ時・最安値更新のSlack通知には、該当商品の価格推移グラフ画像（GitHub Pagesでホストしたもの）のURLを添付し、通知だけでグラフを確認できるようにする。

## 5. データモデル（案）

```json
{
  "shop_id": "ssense",
  "product_id": "SSENSE-1234567",
  "product_url": "https://www.ssense.com/...",
  "brand": "Our Legacy",
  "product_name": "...",
  "price": 45000,
  "currency": "JPY",
  "sizes_in_stock": ["S", "M"],
  "status": "restocked",
  "first_seen_at": "2026-09-05T10:00:00+09:00",
  "last_checked_at": "2026-09-05T10:30:00+09:00",
  "last_notified_at": "2026-09-05T10:30:00+09:00",
  "lowest_price_ever": 39800,
  "lowest_price_seen_at": "2026-08-01T09:00:00+09:00",
  "price_history": [
    {"date": "2026-08-01T09:00:00+09:00", "price": 39800},
    {"date": "2026-09-05T10:30:00+09:00", "price": 45000}
  ]
}
```

## 6. 非機能要件

1. **アクセス頻度への遠慮**：各ショップへのリクエスト間隔を空け、同時多発的なアクセスを避ける。User-Agentは実ブラウザに近いものを設定しつつ、過度な偽装は行わない。
2. **利用規約の確認**：各ショップの `robots.txt` および利用規約を確認し、明確にスクレイピングを禁止している箇所は個別に対応方針を検討する（プロキシ経由で購入手続きに介入するような自動購入・自動チェックアウトは本仕様のスコープ外）。
3. **可観測性**：どのショップがいつ最後に正常取得できたか、ログ・状態ファイルで追跡できること。
4. **拡張性**：新しいショップを追加する際に、共通のスクレイパーインターフェース（例：`fetch_products() -> list[Product]`）に従うだけで追加できる設計にする。

## 7. 開発フェーズ案

| フェーズ | 内容 |
|---|---|
| MVP | 3.2.1の5サイト（公式＋SSENSE・END. Clothing・Farfetch・MR PORTER）を対象に、GitHub Actions（1時間毎） + JSON状態ファイル + Slack通知（新規入荷・再入荷・過去最安値更新）+ GitHub Pagesでの簡易価格推移グラフで動作させる |
| フェーズ1 | 3.2の残りの店舗を順次追加、失敗検知・通知の整備 |
| フェーズ2 | 自動発見（新規取扱店の検出）、価格帯/サイズ/カテゴリでの絞り込み設定（必須実装） |
| フェーズ3 | Bot対策の強い店舗への対応（Playwright化やスクレイピング代行サービスの検討） |

## 8. 未確定事項（Claude Codeでの実装着手前に決めておきたいこと）

- Slack Webhook URLの発行・通知先チャンネルの用意（4.4参照。既存/新規どちらのワークスペースでも可）
- 価格履歴をどの粒度で残すか（変化があった時のみを基本方針としているが、実装時に微調整の余地あり）
- 絞り込み条件（カテゴリ・サイズ・価格帯）は将来応じ実装する前提。具体的な条件はフェーズ2着手時に決定

---

### 参考情報源

- [Our Legacy - Official Online Shop](https://www.ourlegacy.com/)
- [Our Legacy - Stores](https://www.ourlegacy.com/stores)
- [Our Legacy Stockists & Where to Buy - curatedmenswear.com](https://www.curatedmenswear.com/brands/our-legacy/)
