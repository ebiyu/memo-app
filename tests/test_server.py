import json


def test_put_get_and_version_conflict(client):
    r = client.put("/api/notes/todo", json={"content": "a", "base_version": 0})
    assert r.status_code == 200 and r.json()["version"] == 1

    r = client.put("/api/notes/todo", json={"content": "b", "base_version": 0})
    assert r.status_code == 409
    assert r.json()["current"]["content"] == "a"

    r = client.put("/api/notes/todo", json={"content": "b", "base_version": 1})
    assert r.json()["version"] == 2
    assert client.get("/api/notes/todo").json()["content"] == "b"


def test_delete_is_tombstone(client):
    client.put("/api/notes/x", json={"content": "a", "base_version": 0})
    r = client.delete("/api/notes/x", params={"base_version": 1})
    assert r.json()["deleted"] is True
    assert client.get("/api/notes").json() == []
    assert client.get("/api/notes", params={"include_deleted": True}).json()[0]["version"] == 2


def test_invalid_slug(client):
    r = client.put("/api/notes/Bad_Slug", json={"content": "a", "base_version": 0})
    assert r.status_code == 422


def _mcp(client, method, params=None, id_=1):
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 200, r.text
    body = r.text
    if r.headers["content-type"].startswith("text/event-stream"):
        data = [l[5:].strip() for l in body.splitlines() if l.startswith("data:")]
        body = data[-1]
    return json.loads(body)


def test_mcp_tools(client):
    client.put("/api/notes/shopping", json={"content": "牛乳\n卵\n", "base_version": 0})
    init = _mcp(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    assert init["result"]["serverInfo"]["name"] == "memo"

    tools = _mcp(client, "tools/list", id_=2)["result"]["tools"]
    assert {t["name"] for t in tools} == {"list_notes", "read_note", "search_notes"}

    res = _mcp(client, "tools/call", {"name": "read_note", "arguments": {"slug": "shopping"}}, 3)
    assert "牛乳" in json.dumps(res["result"], ensure_ascii=False)

    res = _mcp(client, "tools/call", {"name": "search_notes", "arguments": {"query": "卵"}}, 4)
    assert "shopping" in json.dumps(res["result"])
