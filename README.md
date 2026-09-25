# ぴよログデータ連携ツール

ぴよログのデータフィードをローカルにJSONファイルとして保存し、必要に応じて各種ツールと連携するツールです。  
現在は連携先ツールとしてVictoriaLogsをサポートしています。

1. `fetch-piyolog.sh` — ぴよログから取得したJSONデータをそのままファイルに保存
2. `sync_victorialogs.py` — 保存済みJSONを重複排除してVictoriaLogsへ送信

フィードの作成方法、利用可能な取得期間、JSON仕様は[ぴよログ公式のデータフィードガイド](https://www.piyolog.com/app/piyolog/data_feed/ja/)を参照してください。

## 1. ぴよログフィードの取得と保存

### 設定

取得スクリプトには Bash、`curl`、`jq`、`flock`（util-linux）が必要です。

プロジェクト直下に `.env` を作成し、ぴよログアプリで発行したフィードURLを設定します。

```dotenv
PIYOLOG_FEED_URL=https://feed.piyolog.com/v1/feed/24h/＜feed_id＞/＜secret＞
```

`PIYOLOG_FEED_URL` の値は秘密情報です。空白や引用符を加えず、URLだけを設定してください。

プロキシ環境では、必要に応じて以下も同じ `.env` に設定できます。取得・VictoriaLogs連携の両方で使われます。

```dotenv
HTTPS_PROXY=http://proxy.example.com:8080
NO_PROXY=localhost,127.0.0.1
```

### 手動実行

```bash
./fetch-piyolog.sh
```

保存先は `data/piyolog/YYYY-MM-DDTHH-MM-SS+0900.json` です。ファイル名は取得したローカル時刻で、実際の対象期間は各JSONの `range.from` と `range.to`（UTC）で確認できます。

同時実行は `flock` で防止します。ロックはプロセス終了時に自動解放されるため、強制終了やサーバ再起動後も再実行できます。`data/piyolog/.fetch.flock` は残るのが正常なので削除しないでください。

12時間ごとなど、24時間より短い間隔で実行すると、取得範囲の境界やキャッシュによる取りこぼしを重複で補えます。

### 定期実行（systemd timer）

同梱のuser systemd unitは、毎日00:15と12:15に取得します。停止・スリープ中に予定時刻を過ぎた場合は、次回のuser systemd起動時に1回補完します。

```bash
mkdir -p ~/.config/systemd/user
cp systemd/user/piyolog-fetch.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now piyolog-fetch.timer
```

```bash
systemctl --user list-timers piyolog-fetch.timer
journalctl --user -u piyolog-fetch.service
```

## 2. VictoriaLogs連携

データをVictoriaLogsに連携する機能です。  
`sync_victorialogs.py` は保存済みJSONからイベントIDごとに最も新しいスナップショットを選び、新規または内容が変化したイベントだけをVictoriaLogsのJSON Lines APIへ送信します。送信済み状態は接続先URLごとに `data/piyolog/victorialogs-state.sqlite3` に保存します。別のURLへ切り替えるとその宛先へ未送信のイベントを送り、元のURLへ戻すと以前の送信済み状態を使います（末尾の `/`、ホスト名の大文字・小文字、既定ポートの表記差は同一視します）。

### 設定

VictoriaLogsの接続先を `.env` に追加します。指定しなければデフォルトの `http://127.0.0.1:9428` を使います。

```dotenv
VICTORIALOGS_URL=http://127.0.0.1:9428
```

### 手動実行

Python標準ライブラリだけで動作します。プロジェクト配下のvenvを使って実行してください。

```bash
.venv/bin/python sync_victorialogs.py --dry-run
```

`--dry-run` は未送信・変更済みイベントの件数を確認します。`--debug` は送信済みかどうかに関係なく、イベントIDごとの最新版からイベント日時が新しい最大5件を `debug=1` で送信します。検索用データとしては保存せず、送信済み状態DBも参照・更新しません。検証対象が0件なら通信せずエラー終了します。

`debug=1` はVictoriaLogsの取り込みAPIが提供する機能で、受信データを解析してサーバログに出力し、検索用データとしては保存しません。

`--debug` の成功表示はHTTP応答の確認です。解析結果はVictoriaLogs側のログで確認してください。メモなどの内容もサーバログに出力される点に注意してください。

```bash
.venv/bin/python sync_victorialogs.py --debug
.venv/bin/python sync_victorialogs.py
```

通常実行が成功した後は、未送信または変更されたイベントだけが送信されます。通信失敗時は送信済み状態を更新しないため、次回に再送されます。

同じ状態DBを使う同期処理は、標準ライブラリの `fcntl.flock` で同時実行を防止します。実行中に別の同期を開始するとエラーで終了します。DBの隣に残る `.lock` ファイルは削除しないでください。別の `--state-file` を使う実行同士は排他されません。また、送信成功後に状態を保存する前に停止した場合など、再送による重複は起こり得ます。

### 定期実行（systemd timer）

取得timerの10分後、毎日00:25と12:25に同期します。

```bash
cp systemd/user/piyolog-victorialogs-sync.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now piyolog-victorialogs-sync.timer
```

```bash
systemctl --user list-timers piyolog-victorialogs-sync.timer
journalctl --user -u piyolog-victorialogs-sync.service
```

### Grafanaダッシュボード

[`grafana/dashboards/piyolog-overview.json`](grafana/dashboards/piyolog-overview.json) は、記録数・授乳回数・おしっこ回数・離乳食とメモ・体温・最新記録を表示するダッシュボードです。

Grafanaの **Dashboards** → **New** → **Import** からJSONファイルをアップロードし、VictoriaLogsデータソースを選択してimportしてください。JSONにはデータソースの固定UIDを含めていないため、環境ごとに同じ手順で利用できます。初期表示期間は直近24時間です。

離乳食パネルは選択期間内の記録を「日時」「メモ」の2列で表示します。日時はブラウザのタイムゾーンで表示し、メモなしの記録も空欄で表示します。体温は時系列グラフ、その他のイベントは下部の「最新の記録」パネルで確認できます。

VictoriaLogsでは、編集済みイベントを送信しても古い行は上書きされず残ります。このダッシュボードでは全パネルで `event_id` ごとに `snapshot_at`（フィードの生成日時）が最新の行を選び、重複集計・表示を防ぎます。離乳食のメモや体温も最新の行の内容を使います。
