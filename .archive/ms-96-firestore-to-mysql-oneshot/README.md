# ms-96 Firestore → MySQL 一回限り移行スクリプト (退避済み)

## これは何か
`migrate_firestore_to_mysql.py` — ms-96 (e-2379) で Firestore に入っていた
Beacon の全データ (projects / users / treks とその subcollection 群) を
mysql_client の JSON-blob スキーマへ移すための **一回限り (one-shot)** の移行
スクリプト。export / import / verify の 3 subcommand を持つ。

## なぜ退避したか
本番はすでに MySQL で稼働しており、この移行は完了済み。役目を終えた one-shot
であり、リポジトリ本体 (`server/`) に置いておくと「今も使う運用スクリプト」と
誤読される。パラダイム移行 (target 中心化) とは無関係の純粋な棚卸し衛生。

コード内から `migrate_firestore_to_mysql` への import / 呼び出し / CI 参照は
**ゼロ** であることを確認済 (退避時点)。

## 削除でなく退避の理由
将来 Firestore→MySQL の移行手順を監査・参照したくなった時のため、`rm` せず
`.archive/` に残す (データ不変性の原則 = 全状態変更をトレーサブルに)。

## 退避の出所
- 退避タスク: ms-109 統合リファクタ / e-5526 (C12 migration hygiene)
- 退避日: 2026-08-27
- 元パス: `server/migrate_firestore_to_mysql.py`
