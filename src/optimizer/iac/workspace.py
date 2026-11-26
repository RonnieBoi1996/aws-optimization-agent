"""Local checkouts of target repos. Read-only clones over public HTTPS: no credentials.

(Private target repos would need the GitHub App token here; both repos are
public today, so the clone holds no secret at all.)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

MAX_STACK_FILE_CHARS = 60_000


def clone(owner: str, repo: str, branch: str, dest: Path) -> str:
    """Shallow-clone `branch` into dest; return the commit SHA."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")}
    env["GIT_TERMINAL_PROMPT"] = "0"  # never hang on a password prompt
    subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", branch,
                    f"https://github.com/{owner}/{repo}.git", str(dest)],
                   check=True, env=env, timeout=180, capture_output=True)
    return subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def copy_checkout(src: Path, dest: Path) -> Path:
    """A scratch copy to edit, without .git or provider downloads."""
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", ".terraform"), symlinks=True)
    return dest


def stack_files(root: Path, stack: str) -> dict[str, str]:
    """{repo-relative path: text} for every .tf file in the stack, sorted (stable prompt bytes)."""
    base = root / stack
    if not base.is_dir():
        return {}
    out = {}
    for p in sorted(base.rglob("*.tf")):
        if ".terraform" in p.parts or p.is_symlink():
            continue
        out[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8")[:MAX_STACK_FILE_CHARS]
    return out


def joined(files: dict[str, str]) -> str:
    return "\n".join(files[k] for k in sorted(files))
