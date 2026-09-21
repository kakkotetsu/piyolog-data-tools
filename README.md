# ぴよログデータ連携ツール

ぴよログのデータフィードをローカルにJSONファイルとして保存し、必要に応じて各種ツールと連携するツールです。  
現在は連携先ツールとしてVictoriaLogsをサポートしています。

1. `fetch-piyolog.sh` — ぴよログから取得したJSONデータをそのままファイルに保存
2. `sync_victorialogs.py` — 保存済みJSONを重複排除してVictoriaLogsへ送信

フィードの作成方法、利用可能な取得期間、JSON仕様は[ぴよログ公式のデータフィードガイド](https://www.piyolog.com/app/piyolog/data_feed/ja/)を参照してください。

## 1. ぴよログフィードの取得と保存

### 設定

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

12時間ごとなど、24時間より短い間隔で実行すると、取得範囲の境界やキャッシュによる取りこぼしを重複で補えます。

### 定期実行

#### cron

cronを使う場合は、`crontab -e` で次の行を追加します。例中の `/home/kotetsu/piyolog-data-tools` は、このプロジェクトを置いた実際のパスに置き換えてください。

```cron
15 0,12 * * * cd /home/kotetsu/piyolog-data-tools && ./fetch-piyolog.sh >> data/piyolog/fetch.log 2>&1
```

#### systemd timer

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
`sync_victorialogs.py` は保存済みJSONからイベントIDごとに最も新しいスナップショットを選び、新規または内容が変化したイベントだけをVictoriaLogsのJSON Lines APIへ送信します。送信済み状態は `data/piyolog/victorialogs-state.sqlite3` に保存します。

### 設定

VictoriaLogsの接続先を `.env` に追加します。ローカルのDocker Compose構成では、指定しなければ既定値の `http://127.0.0.1:9428` を使います。

```dotenv
VICTORIALOGS_URL=http://127.0.0.1:9428
```

### 手動実行

Python標準ライブラリだけで動作します。プロジェクト配下のvenvを使って実行してください。

```bash
.venv/bin/python sync_victorialogs.py --dry-run
```

`--dry-run` で送信件数を確認した後、最初の1回だけ `--debug` でVictoriaLogsの受信形式を検証できます。`--debug` はVictoriaLogsへデータを送りますが保存せず、送信済み状態も更新しません。

```bash
.venv/bin/python sync_victorialogs.py --debug
.venv/bin/python sync_victorialogs.py
```

通常実行が成功した後は、未送信または変更されたイベントだけが送信されます。通信失敗時は送信済み状態を更新しないため、次回に再送されます。

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
