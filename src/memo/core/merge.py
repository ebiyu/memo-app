"""git 互換のコンフリクトマーカーを出す 3-way merge."""

from dataclasses import dataclass
import re

import merge3

START = "<<<<<<< local"
MID = "======="
END = ">>>>>>> remote"

_MARKER_RE = re.compile(r"^(<<<<<<<|=======|>>>>>>>)( |$)", re.MULTILINE)


@dataclass(frozen=True)
class MergeResult:
    text: str
    conflict: bool


def _lines(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    # 末尾改行の有無が違うだけで衝突しないよう、最終行にも改行を揃える
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines


def merge3way(base: str, local: str, remote: str) -> MergeResult:
    if local == remote:
        return MergeResult(local, False)
    if local == base:
        return MergeResult(remote, False)
    if remote == base:
        return MergeResult(local, False)

    m = merge3.Merge3(_lines(base), _lines(local), _lines(remote))
    conflict = any(group[0] == "conflict" for group in m.merge_groups())
    out = "".join(
        m.merge_lines(
            start_marker=START,
            mid_marker=MID,
            end_marker=END,
        )
    )
    # 両者とも末尾改行なしなら、それに合わせる
    if not local.endswith("\n") and not remote.endswith("\n") and out.endswith("\n"):
        out = out[:-1]
    return MergeResult(out, conflict)


def has_conflict_markers(text: str) -> bool:
    found = {m.group(1) for m in _MARKER_RE.finditer(text)}
    return {"<<<<<<<", "=======", ">>>>>>>"} <= found
