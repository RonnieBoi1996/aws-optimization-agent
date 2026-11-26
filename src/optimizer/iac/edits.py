"""Apply the model's search-and-replace edits to a checkout, and diff them.

Edits are exact string replacements: `old` must appear exactly once in the
file. That keeps the change small and reviewable, and an edit that doesn't
match fails loudly instead of landing somewhere unexpected. `old == ""` creates
a new file (which must not exist yet).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from optimizer.iac.paths import check_file

MAX_FILE_CHARS = 200_000


class EditError(ValueError):
    pass


@dataclass(frozen=True)
class Edit:
    path: str
    old: str
    new: str


def parse_edits(raw: object) -> list[Edit]:
    if not isinstance(raw, list) or not raw:
        raise EditError("edits must be a non-empty list")
    out = []
    for i, e in enumerate(raw):
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ("path", "old", "new")):
            raise EditError(f"edit {i}: needs string fields path, old, new")
        out.append(Edit(e["path"], e["old"], e["new"]))
    return out


def apply_edits(root: Path, stack: str, edits: list[Edit]) -> dict[str, tuple[str | None, str]]:
    """Apply in order; return {path: (before or None if new, after)}. Raises EditError/PathRefused."""
    changed: dict[str, tuple[str | None, str]] = {}
    for i, e in enumerate(edits):
        path = check_file(e.path, stack, root)
        f = root / path
        current = changed[path][1] if path in changed else (f.read_text(encoding="utf-8") if f.exists() else None)
        if e.old == "":
            if current is not None:
                raise EditError(f"edit {i}: {path} already exists; use a non-empty `old` to change it")
            after = e.new
        else:
            if current is None:
                raise EditError(f"edit {i}: {path} doesn't exist")
            n = current.count(e.old)
            if n != 1:
                raise EditError(f"edit {i}: `old` must appear exactly once in {path}, found {n} times")
            after = current.replace(e.old, e.new)
        if len(after) > MAX_FILE_CHARS:
            raise EditError(f"edit {i}: {path} would be too large")
        before = changed[path][0] if path in changed else current
        changed[path] = (before, after)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(after, encoding="utf-8")
    return changed


def unified_diff(changed: dict[str, tuple[str | None, str]]) -> str:
    parts = []
    for path in sorted(changed):
        before, after = changed[path]
        parts.extend(difflib.unified_diff(
            (before or "").splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile="/dev/null" if before is None else f"a/{path}", tofile=f"b/{path}"))
    return "".join(parts)
