# ぴよログ フィード保存

`fetch-piyolog.sh` は、`.env` の `PIYOLOG_FEED_URL` からぴよログのデータフィードを取得し、レスポンスを変更せず JSON ファイルとして保存します。

フィードの作成方法、利用可能な取得期間、JSON仕様は[ぴよログ公式のデータフィードガイド](https://www.piyolog.com/app/piyolog/data_feed/ja/)を参照してください。

## 設定

プロジェクト直下に `.env` を作成し、ぴよログアプリで発行したフィードURLを次の形式で設定します。

```dotenv
PIYOLOG_FEED_URL=https://feed.piyolog.com/v1/feed/24h/＜feed_id＞/＜secret＞
```

`PIYOLOG_FEED_URL` の値は秘密情報です。空白や引用符を加えず、URLだけを設定してください。

## 手動実行

```bash
./fetch-piyolog.sh
```

保存先は `data/piyolog/YYYY-MM-DDTHH-MM-SS+0900.json` です。ファイル名は取得したローカル時刻で、実際の対象期間は各JSONの `range.from` と `range.to`（UTC）で確認できます。

12時間ごとなど、24時間より短い間隔で実行すると、取得範囲の境界やキャッシュによる取りこぼしを重複で補えます。

## 定期実行

### cron

cronを使う場合は、`crontab -e` で次の行を追加します。例中の `/home/kotetsu/piyolog-data-tools` は、このプロジェクトを置いた実際のパスに置き換えてください。

```cron
15 0,12 * * * cd /home/kotetsu/piyolog-data-tools && ./fetch-piyolog.sh >> data/piyolog/fetch.log 2>&1
```

### systemd timer

同梱の user systemd unit は、毎日00:15と12:15に取得します。停止・スリープ中に予定時刻を過ぎた場合は、次回のuser systemd起動時に1回補完します。

```bash
mkdir -p ~/.config/systemd/user
cp systemd/user/piyolog-fetch.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now piyolog-fetch.timer
```

状態と直近の実行ログは次で確認できます。

```bash
systemctl --user list-timers piyolog-fetch.timer
journalctl --user -u piyolog-fetch.service
```

ログアウト中にも実行する必要がある場合は、一度だけ `loginctl enable-linger "$USER"` を実行してください。

フィードURLと保存データはいずれも個人情報です。`.env` と `data/piyolog/` を公開リポジトリに含めないでください。
