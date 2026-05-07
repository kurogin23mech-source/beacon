# Beacon 仕様書

## 概要

Beacon は、AI駆動開発においてマイルストーンベースのプロジェクト進捗を常時可視化し、開発者が方向性を見失わずに実装を進められるツール。

## アーキテクチャ

```
beacon (bin/beacon)        - CLI エントリポイント (bash)
lib/commands.py            - サブコマンド実装 (Python)
lib/dashboard.py           - tmux左ペイン用リアルタイムダッシュボード (Python)
.beacon/project.json       - プロジェクト状態ファイル (JSON)
```

## 起動フロー

### 重要: Claude Code との併用

Beacon の tmux ダッシュボードと Claude Code を同時に使う場合、以下の順序で起動する:

1. ターミナルで `beacon` を実行 → tmux セッションが起動（左: ダッシュボード、右: シェル）
2. 右ペイン（作業用シェル）で `claude` を起動

**注意**: Claude Code 内から `! beacon`（引数なし）を実行してはならない。`tmux attach-session` が Claude Code のプロセスを破壊する。

Claude Code 内からステータスを確認したい場合は `! beacon status` を使う。

## CLI コマンド

| コマンド | 説明 |
|---------|------|
| `beacon` | tmux ダッシュボード + シェルを起動 |
| `beacon init` | `.beacon/` をカレントディレクトリに初期化 |
| `beacon status` | マイルストーン一覧をテキスト表示（tmux不要） |
| `beacon milestone add` | マイルストーンを追加（対話式） |
| `beacon milestone list` | マイルストーン一覧 |
| `beacon milestone start <id>` | マイルストーンをアクティブに設定 |
| `beacon milestone done <id>` | マイルストーンを完了に設定 |
| `beacon log [-m <ms-id>] [message]` | 現在の HEAD コミットをマイルストーンに記録。複数 in_progress 時は -m 必須 |
| `beacon sync` | 直近 git コミットをアクティブマイルストーンに自動同期 |
| `beacon task add [-m <ms-id>] [-t <type>] "説明"` | エントリを追加（デフォルトtype: task） |
| `beacon task done <entry-id>` | エントリを完了にする |
| `beacon task list [-m <ms-id>]` | マイルストーンのエントリ一覧 |

## データモデル (.beacon/project.json)

```json
{
  "name": "プロジェクト名",
  "objective": "大目的",
  "milestones": [
    {
      "id": "ms-1",
      "title": "マイルストーンタイトル",
      "status": "todo | in_progress | done",
      "target_date": "YYYY-MM-DD | null",
      "entries": [
        {
          "id": "e-1",
          "type": "commit | task | decision | meeting | ...",
          "description": "エントリの説明",
          "date": "YYYY-MM-DD",
          "status": "todo | done",
          "meta": {
            "hash": "(commit時) 7文字短縮ハッシュ",
            "message": "(commit時) コミットメッセージ"
          }
        }
      ]
    }
  ]
}
```

### エントリ type 一覧

| type | 用途 | 例 |
|------|------|---|
| `commit` | コード変更 | beacon log で自動追加 |
| `task` | コミットに紐づかない作業 | ドキュメント更新、設定変更 |
| `decision` | 意思決定 | 設計方針の決定、技術選定 |
| `meeting` | ミーティング | チーム合意、レビュー |

type は自由に追加可能。上記は組み込みの推奨値。

### ステータスライフサイクル

マイルストーン:
```
todo → in_progress → done
```

エントリ:
```
todo → done
```

## ダッシュボード (lib/dashboard.py)

- tmux 左ペインで常時表示
- `project.json` のファイルハッシュを 2 秒間隔でポーリング
- 変更検出時に自動再描画
- ツリー形式でマイルストーンとエントリを表示
- エントリは type 別にアイコン・色を変えて表示

## tmux セッション構成

```
+------------------+----------------------------------------+
| Dashboard (30%)  |  Working Shell (70%)                   |
| (dashboard.py)   |  ← ここで claude を起動               |
+------------------+----------------------------------------+
```

セッション名: `beacon-<ディレクトリ名>`
