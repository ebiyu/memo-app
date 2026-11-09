"""同期 API (/api) と MCP (/mcp) を 1 つの ASGI アプリにまとめる。

認証はこのアプリでは扱わない。本番では Traefik + Authelia (forwardAuth) の後ろに置く。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

from memo.core import InvalidSlug, validate_slug

from .db import NoteStore, VersionConflict
from .mcp_tools import build_mcp


class PutBody(BaseModel):
    content: str
    base_version: int


def _slug(slug: str) -> str:
    try:
        return validate_slug(slug)
    except InvalidSlug as e:
        raise HTTPException(422, str(e))


def _transport_security() -> TransportSecuritySettings:
    hosts = [h.strip() for h in os.environ.get("MEMO_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not hosts:
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "testserver"]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts)


def create_app(db_path: str | None = None) -> FastAPI:
    store = NoteStore(db_path or os.environ.get("MEMO_DB", "data/memo.db"))
    mcp = build_mcp(store)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=_transport_security(),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="memo", lifespan=lifespan)
    app.state.store = store

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/api/notes")
    def list_notes(include_deleted: bool = False):
        return [n.meta() for n in store.list(include_deleted=include_deleted)]

    @app.get("/api/notes/{slug}")
    def get_note(slug: str):
        n = store.get(_slug(slug))
        if n is None:
            raise HTTPException(404, "not found")
        return n.to_dict()

    @app.put("/api/notes/{slug}")
    def put_note(slug: str, body: PutBody):
        try:
            return store.put(_slug(slug), body.content, body.base_version).to_dict()
        except VersionConflict as e:
            return _conflict(e)

    @app.delete("/api/notes/{slug}")
    def delete_note(slug: str, base_version: int = Query(...)):
        try:
            return store.put(_slug(slug), "", base_version, deleted=True).to_dict()
        except VersionConflict as e:
            return _conflict(e)

    # /mcp は最後にマウント (API ルートを優先させる)
    app.mount("/", mcp_app)
    return app


def _conflict(e: VersionConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": "version_conflict",
            "current": e.current.to_dict() if e.current else None,
        },
    )
