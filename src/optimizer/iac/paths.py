"""Where the synthesizer may write (rule 4, enforced in code).

Two layers:
1. config.yaml `github.targets`: repo (the iac_repo tag) -> stack patterns.
2. Fixed rules here that config can't loosen:
   - In the optimizer's own repo, only `sandbox/` is ever editable. infra/
     holds the agent's own roles, .github/ and ci/ hold the pipeline that
     checks its PRs.
   - In every repo, `.github/` (workflows) and `.optimizer/` (manifests, which
     code writes, never the model) are off-limits.

The tag values that pick the repo and stack come from AWS, so they're
untrusted too: anyone who can tag a resource could point it at infra/. These
checks are what stops that.
"""

from __future__ import annotations

import posixpath
from fnmatch import fnmatchcase
from pathlib import Path

OWN_REPO = "aws-optimization-agent"
OWN_REPO_STACKS = ("sandbox",)
NEVER_TOP_LEVEL = (".github", ".optimizer", ".git")


class PathRefused(ValueError):
    pass


def normalize(rel: str) -> str:
    """Repo-relative POSIX path, or PathRefused if it escapes the repo."""
    if not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        raise PathRefused(f"bad path: {rel!r}")
    norm = posixpath.normpath(rel)
    if norm == "." or norm.startswith("../") or norm == "..":
        raise PathRefused(f"path escapes the repo: {rel!r}")
    return norm


def check_stack(repo: str, stack: str, targets: dict[str, list[str]]) -> str:
    """Return the normalized stack dir if the synthesizer may edit it."""
    stack = normalize(stack.strip("/"))
    if repo not in targets:
        raise PathRefused(f"repo {repo!r} is not a configured target (config.yaml github.targets)")
    if stack.split("/", 1)[0] in NEVER_TOP_LEVEL:
        raise PathRefused(f"{stack!r} is never editable")
    if repo == OWN_REPO and stack not in OWN_REPO_STACKS:
        raise PathRefused(f"in {OWN_REPO} only {', '.join(OWN_REPO_STACKS)} is editable, not {stack!r} "
                          "(the optimizer's own identity and pipeline are off-limits)")
    if not any(fnmatchcase(stack, pat) for pat in targets[repo]):
        raise PathRefused(f"stack {stack!r} doesn't match {targets[repo]} for {repo}")
    return stack


def check_file(path: str, stack: str, root: Path) -> str:
    """A file the model wants to edit: a .tf file inside the stack, no symlink games."""
    norm = normalize(path)
    if not norm.startswith(stack + "/"):
        raise PathRefused(f"{path!r} is outside the stack directory {stack}/")
    if not norm.endswith(".tf"):
        raise PathRefused(f"{path!r}: only .tf files may be edited")
    real = (root / norm).resolve()
    if not real.is_relative_to((root / stack).resolve()):
        raise PathRefused(f"{path!r} resolves outside {stack}/ (symlink?)")
    return norm
