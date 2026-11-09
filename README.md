# memo

書式なしプレーンテキスト専用の付箋メモ。サーバーと同期し、MCP で AI から読める。

```
src/memo/
  core/      slug 検証・3-way merge (git 互換コンフリクトマーカー)
  server/    FastAPI 同期 API + MCP (/mcp, 読み取り専用) + SQLite
  desktop/   wxPython 付箋 UI + ローカル保存 + 同期エンジン
tests/
```

## セットアップ

```bash
uv sync
```

## ローカルで動かす

サーバー (既定 `http://127.0.0.1:8765`, DB は `data/memo.db`):

```bash
uv run memo-server
```

デスクトップ (タスクトレイに常駐。データは `%APPDATA%\memo`):

```bash
uv run memo-desktop
```

同期とコンフリクトを試すには、別のデータフォルダでもう 1 つ起動する:

```bash
uv run memo-desktop --data-dir ./tmp/client-b --interval 5
```

### 操作

| 操作 | 方法 |
|---|---|
| 新規メモ | トレイアイコンをダブルクリック / メニュー「新規メモ…」/ 付箋で `Ctrl+N` |
| メモを開く・閉じる | トレイメニュー「メモ」 |
| 付箋を前面に出す (どこからでも) | `Ctrl+Alt+M` |
| 最前面の切替 | `Ctrl+T` |
| この付箋の変更を同期 | `Ctrl+S` |
| 全体を今すぐ同期 (保留中の付箋は除く) | トレイメニュー「今すぐ同期」 |
| 閉じる (メモは残る) | `Ctrl+W` / × |
| 削除 | `Ctrl+Shift+Delete` |

入力は 0.7 秒でローカルに自動保存される。サーバーへの同期タイミングは次の通り:

| 状況 | 手元の変更の push | リモートの変更の pull |
|---|---|---|
| 付箋がアクティブ | しない (`Ctrl+S` で即時) | 手元が未変更なら自動 (30 秒ごと) |
| 非アクティブになって 5 分未満 | しない | 同上 |
| 非アクティブになって 5 分経過 / 付箋を閉じた / 開いていない | 自動 | 自動 |

タイトルの `●` は未 push の変更あり、`⚠` はコンフリクトマーカーありを示す。

## 同期の仕組み

- サーバーは `version` による楽観ロックのみ。`PUT /api/notes/{slug}` に `base_version` を付けて送り、不一致なら 409 と現在の内容を返す。
- クライアントは `.sync/<slug>.json` に「最後にサーバーと一致した内容 (base)」を持ち、409 のとき `merge3way(base, local, remote)` する。
  - 重ならない変更は自動マージして push。
  - 重なる変更は `<<<<<<< local` / `=======` / `>>>>>>> remote` を本文に書き出し、付箋のタイトルに ⚠ と赤背景で表示。マーカーを消すまで push しない。
- 削除はサーバー側で tombstone。削除と編集がぶつかったら編集を優先する。

## MCP

`/mcp` (Streamable HTTP, stateless) にツール `list_notes` / `read_note` / `search_notes`。

Claude Code から試す:

```bash
claude mcp add --transport http memo http://127.0.0.1:8765/mcp
```

外部公開時は `MEMO_ALLOWED_HOSTS=memo.example.com` を設定する (DNS rebinding 対策で Host を検証している)。

## テスト

```bash
uv run pytest
```

## 今後

- Dockerfile と Traefik + Authelia (Bearer forwardAuth) の設定
- デスクトップの OIDC (PKCE, loopback) ログイン。現状は `--token` / `MEMO_TOKEN` で Bearer を付けられるだけ
- PyInstaller でのパッケージングとスタートアップ登録
