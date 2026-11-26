"""Resources the agents must never propose changes to (rule 4, enforced in code).

Two kinds:
1. The optimizer's own identity and safety net, which is fixed and the same
   in every account: its IAM roles and policies, the GitHub OIDC provider, the
   state and audit buckets, the trail, the alert rules and topics.
2. Per-service `do_not_touch` entries from constraints.yaml: an ARN, an fnmatch
   pattern over ARNs, or `tag:Key=Value`. Tag entries exist because many ARNs
   (EBS volumes, instances) contain only a random ID that isn't known until the
   resource exists and changes if it's recreated; a Name tag is stable.

`validate` drops matches before anything else sees them, and the rule engine's
R1 (M7) will call the same function, so there is one definition of "protected".
"""

from __future__ import annotations

from fnmatch import fnmatchcase

# fnmatch patterns over full ARNs. "*" also matches "/" here, on purpose.
OWN_IDENTITY_PATTERNS = (
    "arn:aws:iam::*:role/optimizer/*",
    "arn:aws:iam::*:policy/optimizer/*",
    "arn:aws:iam::*:oidc-provider/token.actions.githubusercontent.com",
    "arn:aws:s3:::tfstate-*",
    "arn:aws:s3:::cloudtrail-*",
    "arn:aws:cloudtrail:*:*:trail/optimizer-*",
    "arn:aws:events:*:*:rule/optimizer-*",
    "arn:aws:sns:*:*:optimizer-*",
)


TAG_PREFIX = "tag:"


def protection_reason(arn: str, do_not_touch: list[str] | tuple[str, ...] = (),
                      tags: dict[str, str] | None = None) -> str | None:
    """Return why `arn` is protected, or None if agents may propose changes to it.

    `tags` must be the resource's real tags (from tag:GetResources), never
    tags the model repeated back.
    """
    for pattern in OWN_IDENTITY_PATTERNS:
        if fnmatchcase(arn, pattern):
            return f"optimizer's own identity or safety net ({pattern})"
    tags = tags or {}
    for pattern in do_not_touch:
        if pattern.startswith(TAG_PREFIX):
            key, sep, value = pattern[len(TAG_PREFIX):].partition("=")
            # Exact match only: "tag:Name=db" must not protect "db-old" by accident.
            if sep and key in tags and tags[key] == value:
                return f"listed in do_not_touch ({pattern})"
        elif arn == pattern or fnmatchcase(arn, pattern):
            return f"listed in do_not_touch ({pattern})"
    return None
