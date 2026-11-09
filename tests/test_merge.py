from memo.core import has_conflict_markers, merge3way


def test_trivial_cases():
    assert merge3way("a", "a", "b").text == "b"
    assert merge3way("a", "b", "a").text == "b"
    assert merge3way("a", "c", "c").text == "c"


def test_non_overlapping_edits_merge_cleanly():
    base = "1\n2\n3\n4\n5\n"
    local = "1 local\n2\n3\n4\n5\n"
    remote = "1\n2\n3\n4\n5 remote\n"
    r = merge3way(base, local, remote)
    assert not r.conflict
    assert r.text == "1 local\n2\n3\n4\n5 remote\n"


def test_overlapping_edits_produce_markers():
    r = merge3way("買い物\n牛乳\n", "買い物\n卵\n", "買い物\nパン\n")
    assert r.conflict
    assert r.text == (
        "買い物\n<<<<<<< local\n卵\n=======\nパン\n>>>>>>> remote\n"
    )
    assert has_conflict_markers(r.text)


def test_missing_trailing_newline_does_not_conflict():
    r = merge3way("a\nb", "a\nb\nc", "x\na\nb")
    assert not r.conflict
    assert r.text == "x\na\nb\nc"


def test_has_conflict_markers_requires_all_three():
    assert not has_conflict_markers("=======\n")
    assert not has_conflict_markers("plain text")
