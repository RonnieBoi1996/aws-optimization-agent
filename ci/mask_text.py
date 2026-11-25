#!/usr/bin/env python3
"""Replace sensitive values in files, in place, before they're posted publicly.

GitHub masks secrets in workflow *logs* automatically, but not in files that
we later post as PR comments. Values come from the MASK_VALUES env var
(one per line, e.g. the account ID and alert email), never from arguments,
so they don't appear in the process list.

  MASK_VALUES="$ACCOUNT_ID"$'\\n'"$ALERT_EMAIL" python3 mask_text.py plan.txt apply.txt
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def mask(text: str, values: list[str]) -> str:
    # Longest first, so a value that contains another one is masked whole.
    for v in sorted((v for v in values if len(v) >= 4), key=len, reverse=True):
        text = text.replace(v, "***")
    return text


def main(argv: list[str]) -> int:
    values = [v.strip() for v in os.environ.get("MASK_VALUES", "").splitlines() if v.strip()]
    for name in argv:
        path = Path(name)
        if path.exists():
            path.write_text(mask(path.read_text(encoding="utf-8", errors="replace"), values),
                            encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
