"""Logging: human-readable lines on stderr + structured JSON lines per run.

- `setup_logging()` configures normal Python logging for the console.
- `RunLog` appends one JSON object per event to runs/<run_id>.jsonl. Later
  milestones write proposals, rule decisions and human decisions there, and
  the `report` command (M9) reads these files back.

Every event goes through `scrub_obj` first, so a secret that slipped into a
tool result never lands on disk.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from optimizer.scrub import scrub, scrub_obj


class _ScrubbingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ScrubbingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # the SDK's HTTP logs are noisy at INFO
    for noisy in ("httpx", "httpx2"):  # anthropic SDK 2.x uses httpx2
        logging.getLogger(noisy).setLevel(logging.WARNING)


def new_run_id() -> str:
    # Sortable by time, unique enough for a single-user tool.
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


class RunLog:
    """Append-only JSON-lines log for one run."""

    def __init__(self, runs_dir: Path, run_id: str):
        self.run_id = run_id
        runs_dir.mkdir(parents=True, exist_ok=True)
        self.path = runs_dir / f"{run_id}.jsonl"

    def event(self, kind: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "kind": kind,
            **scrub_obj(fields),
        }
        # default=str so dates/Decimals don't crash logging
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, sort_keys=True) + "\n")
