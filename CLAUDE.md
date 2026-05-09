# Beacon Project

## Skill連携
- **セッション開始時**: `/beacon-session-start` Skill を使ってプロジェクト状態を取得・提示すること（手動で project.json を読まない）
- **コミット後**: `/beacon-log` Skill を使って進捗を記録すること
- **タスク操作**: `/beacon-task` Skill を使うこと
- **セッション終了時**: `/beacon-session-end` Skill を使うこと

## 開発ルール（Skillでカバーされないもの）
- マイルストーンの追加・完了は `beacon milestone` コマンドで直接管理する（Skill未対応）
- 同じ課題に2回以上コミットが発生したら、タスクにまとめることをユーザーに提案する
