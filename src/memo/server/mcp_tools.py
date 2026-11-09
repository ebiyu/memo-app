"""AI から読むための MCP サーバー (読み取り専用)。"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .db import NoteStore

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:80]
    return ""


def _snippet(text: str, query: str, width: int = 60) -> str:
    i = text.lower().find(query.lower())
    if i < 0:
        return _first_line(text)
    start = max(0, i - width)
    end = min(len(text), i + len(query) + width)
    s = text[start:end].replace("\n", " ")
    return ("…" if start > 0 else "") + s + ("…" if end < len(text) else "")


def build_mcp(store: NoteStore) -> MCPServer:
    mcp = MCPServer(
        name="memo",
        instructions=(
            "ユーザーの付箋メモ (書式なしプレーンテキスト) を読むためのサーバー。"
            "メモは slug で識別される。まず list_notes か search_notes で探し、"
            "read_note で本文を取得する。"
        ),
    )

    @mcp.tool(annotations=READ_ONLY)
    def list_notes() -> list[dict]:
        """全メモの一覧 (slug, 更新日時, 先頭行) を新しい順に返す。"""
        return [
            {"slug": n.slug, "updated_at": n.updated_at, "first_line": _first_line(n.content)}
            for n in store.list()
        ]

    @mcp.tool(annotations=READ_ONLY)
    def read_note(slug: str) -> dict:
        """slug を指定してメモ本文を返す。"""
        n = store.get(slug)
        if n is None or n.deleted:
            raise ValueError(f"note not found: {slug}")
        return {"slug": n.slug, "updated_at": n.updated_at, "content": n.content}

    @mcp.tool(annotations=READ_ONLY)
    def search_notes(query: str, limit: int = 20) -> list[dict]:
        """本文または slug に query を含むメモを検索し、slug と該当箇所の抜粋を返す。"""
        return [
            {"slug": n.slug, "updated_at": n.updated_at, "snippet": _snippet(n.content, query)}
            for n in store.search(query, limit=max(1, min(limit, 100)))
        ]

    return mcp
