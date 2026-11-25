"""Secret scrubbing and untrusted-data quoting (rule 7).

Everything from AWS, repo files, tags and docs is untrusted. Two defences:
1. `scrub()` replaces anything that looks like a secret before it reaches a
   prompt or a log line.
2. `untrusted_block()` wraps data in clearly labelled tags, so the model treats
   it as data to analyse rather than instructions to follow.

The regexes are deliberately a bit greedy: a false positive costs us a
"[REDACTED]" in a prompt, while a false negative leaks a credential.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# (name, pattern). Order matters: PEM blocks first, since they span lines.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    # AWS access key IDs: AKIA (long-term) / ASIA (temporary) + 16 chars.
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[A-Z0-9]{16}\b")),
    # AWS secret keys are 40 chars of base64; only match next to a telling key name,
    # otherwise we would redact every 40-char hash in the account.
    ("aws_secret_access_key", re.compile(
        r"(?i)(aws_secret_access_key|secret_access_key|secretaccesskey)(\W{0,5})[A-Za-z0-9/+=]{40}")),
    ("aws_session_token", re.compile(
        r"(?i)(aws_session_token|sessiontoken)(\W{0,5})[A-Za-z0-9/+=]{100,}")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # password=..., "password": "...", DB_PASSWORD: ... and friends.
    ("password_assignment", re.compile(
        r"(?i)((?:password|passwd|pwd|secret|api_?key|token)[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]{4,})")),
    # Credentials embedded in URLs: scheme://user:pass@host
    ("url_credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s@/]+)(@)")),
]


def scrub(text: str) -> str:
    """Return `text` with anything secret-looking replaced by [REDACTED]."""
    if not text:
        return text
    for name, pattern in _PATTERNS:
        if name in ("aws_secret_access_key", "aws_session_token"):
            # keep the key name so the reader knows what was there
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        elif name == "password_assignment":
            text = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
        elif name == "url_credentials":
            text = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def scrub_obj(obj):
    """Recursively scrub every string inside dicts/lists (for JSON log events)."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v) for v in obj]
    return obj


def untrusted_block(source: str, content: str) -> str:
    """Wrap scrubbed data in tags that mark it as data, not instructions.

    Any closing tag inside the content is neutralised so the data cannot
    "escape" the block and pretend to be instructions.
    """
    safe = scrub(content).replace("</untrusted_data", "&lt;/untrusted_data")
    safe_source = re.sub(r"[^A-Za-z0-9_./:@-]", "_", source)
    return f'<untrusted_data source="{safe_source}">\n{safe}\n</untrusted_data>'
