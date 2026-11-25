"""Tiny on-disk JSON cache with a time-to-live.

Used for Cost Explorer (charged per request, so cache for hours) and the
Pricing API (slow, and prices rarely change). Cache files live in .cache/,
which is git-ignored.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from optimizer.config import REPO_ROOT



def cache_dir() -> Path:
    # Read on every call so tests can point it at a temp dir (OPTIMIZER_CACHE_DIR).
    return Path(os.environ.get("OPTIMIZER_CACHE_DIR") or REPO_ROOT / ".cache")


def cached(namespace: str, key: dict, ttl_seconds: int, fetch: Callable[[], Any],
           now: Callable[[], float] = time.time) -> tuple[Any, bool]:
    """Return (value, was_cached). `key` must be JSON-serializable."""
    digest = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:32]
    path = cache_dir() / namespace / f"{digest}.json"
    if path.exists():
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if now() - entry["stored_at"] < ttl_seconds:
                return entry["value"], True
        except (json.JSONDecodeError, KeyError):
            pass  # corrupt entry: just refetch
    value = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stored_at": now(), "key": key, "value": value}, default=str),
                    encoding="utf-8")
    return value, False
