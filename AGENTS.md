# 開発メモ

- `.env`、`data/piyolog/`、SQLite状態DBには秘密情報・個人情報が含まれる。内容を表示、コミット、外部送信しない。
- `data/piyolog/*.json` は原本である。スクリプトやテストは読み取り専用とし、編集・削除しない。
- 検証には `.venv/bin/python -m unittest discover -s tests -v` を実行する。
- `PIYOLOG_TEST_VICTORIALOGS_URL` を指定する統合テストは、ローカルVictoriaLogsへの読み取り専用テストである。
- Grafanaダッシュボードのイベント集計・表示では、`event_id` ごとに最新の `snapshot_at` を選ぶ。更新前のイベントを集計・表示しない。
