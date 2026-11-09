from memo.core import has_conflict_markers
from memo.desktop.store import LocalStore
from memo.desktop.sync import SyncEngine


def make(client, tmp_path, name):
    changes = []
    store = LocalStore(tmp_path / name)
    eng = SyncEngine(store, client, lambda s, t: changes.append((s, t)))
    return store, eng, changes


def test_push_and_pull(client, tmp_path):
    a, ea, _ = make(client, tmp_path, "a")
    b, eb, changes = make(client, tmp_path, "b")
    a.write("memo", "hello\n")
    assert ea.sync_all().pushed == ["memo"]
    assert eb.sync_all().pulled == ["memo"]
    assert b.read("memo") == "hello\n"
    assert changes == [("memo", "hello\n")]
    # 2 回目は何もしない
    r = eb.sync_all()
    assert not (r.pushed or r.pulled)


def test_concurrent_edits_auto_merge(client, tmp_path):
    a, ea, _ = make(client, tmp_path, "a")
    b, eb, _ = make(client, tmp_path, "b")
    a.write("m", "1\n2\n3\n4\n")
    ea.sync_all()
    eb.sync_all()

    a.write("m", "1 A\n2\n3\n4\n")
    b.write("m", "1\n2\n3\n4 B\n")
    ea.sync_all()
    assert eb.sync_all().merged == ["m"]
    assert b.read("m") == "1 A\n2\n3\n4 B\n"
    ea.sync_all()
    assert a.read("m") == "1 A\n2\n3\n4 B\n"


def test_conflict_markers_then_resolve(client, tmp_path):
    a, ea, _ = make(client, tmp_path, "a")
    b, eb, _ = make(client, tmp_path, "b")
    a.write("m", "x\n")
    ea.sync_all()
    eb.sync_all()

    a.write("m", "A\n")
    b.write("m", "B\n")
    ea.sync_all()
    assert eb.sync_all().conflicts == ["m"]
    text = b.read("m")
    assert has_conflict_markers(text)
    # マーカーがある間は push されない
    assert not eb.sync_all().pushed
    assert client.get("/api/notes/m").json()["content"] == "A\n"

    b.write("m", "A and B\n")
    assert eb.sync_all().pushed == ["m"]
    ea.sync_all()
    assert a.read("m") == "A and B\n"


def test_force_pushes_conflict_markers(client, tmp_path):
    a, ea, _ = make(client, tmp_path, "a")
    b, eb, _ = make(client, tmp_path, "b")
    a.write("m", "x\n")
    ea.sync_all()
    eb.sync_all()

    a.write("m", "A\n")
    b.write("m", "B\n")
    ea.sync_all()
    # Ctrl+S で同期した結果コンフリクト -> その回の force はマーカー入り push に使わない
    eb.force("m")
    assert eb.sync_all().conflicts == ["m"]
    assert not eb.sync_all().pushed

    # もう一度 Ctrl+S -> マーカー入りのまま push
    text = b.read("m")
    eb.force("m")
    assert eb.sync_all().pushed == ["m"]
    assert client.get("/api/notes/m").json()["content"] == text
    assert not b.get_state("m").conflict
    ea.sync_all()
    assert a.read("m") == text


def test_delete_propagates(client, tmp_path):
    a, ea, _ = make(client, tmp_path, "a")
    b, eb, changes = make(client, tmp_path, "b")
    a.write("m", "x")
    ea.sync_all()
    eb.sync_all()
    a.delete("m")
    assert ea.sync_all().deleted == ["m"]
    assert eb.sync_all().deleted == ["m"]
    assert b.read("m") is None
    assert changes[-1] == ("m", None)


def test_edit_wins_over_delete(client, tmp_path):
    a, ea, _ = make(client, tmp_path, "a")
    b, eb, _ = make(client, tmp_path, "b")
    a.write("m", "x")
    ea.sync_all()
    eb.sync_all()
    b.write("m", "edited")
    eb.sync_all()
    a.delete("m")
    ea.sync_all()
    assert a.read("m") == "edited"


def test_recreate_after_remote_delete(client, tmp_path):
    a, ea, _ = make(client, tmp_path, "a")
    a.write("m", "x")
    ea.sync_all()
    a.delete("m")
    ea.sync_all()
    a.write("m", "again")
    ea.sync_all()
    assert client.get("/api/notes/m").json()["content"] == "again"


def test_held_note_is_not_pushed_until_forced(client, tmp_path):
    a, ea, _ = make(client, tmp_path, "a")
    a.write("m", "v1")
    ea.sync_all()

    ea.hold("m")
    a.write("m", "v2")
    assert not ea.sync_all().pushed
    assert client.get("/api/notes/m").json()["content"] == "v1"

    ea.force("m")  # Ctrl+S
    assert ea.sync_all().pushed == ["m"]
    # force は 1 回きり。hold は続いている
    a.write("m", "v3")
    assert not ea.sync_all().pushed

    ea.release("m")  # 非アクティブ 5 分
    assert ea.sync_all().pushed == ["m"]


def test_held_but_unchanged_note_still_pulls(client, tmp_path):
    a, ea, _ = make(client, tmp_path, "a")
    b, eb, _ = make(client, tmp_path, "b")
    a.write("m", "v1")
    ea.sync_all()
    eb.sync_all()
    eb.hold("m")
    a.write("m", "v2")
    ea.sync_all()
    assert eb.sync_all().pulled == ["m"]
    assert b.read("m") == "v2"
