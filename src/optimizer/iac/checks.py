"""Code checks on a synthesized change. Deterministic; the model can't argue with them.

1. Plan changes: well-formed, every address names a block that exists in the
   stack's code (before or after the edit), and there is exactly one: a
   proposal is about one resource, so its PR changes one resource. That also
   keeps shared blocks (for_each over a map) from dragging neighbours along,
   e.g. a do_not_touch volume that sits in the same map.
2. No IAM: an IAM change needs its own review path (M6 + rule R2).
3. Snapshot before delete: if the proposal needs a snapshot, an EBS volume may
   only be deleted when its code ALREADY has `final_snapshot = true` (applied in
   an earlier PR). Setting it in the same PR doesn't work: Terraform destroys
   using the setting stored in state, which would still be false.
4. terraform fmt (rewrites formatting) and terraform validate.

The CI pipeline then plans the change and its scope guard compares the real
plan with the manifest built from (1).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ACTIONS = {"create", "update", "replace", "delete"}
BLOCK_RE = re.compile(r'^\s*(resource|data|module)\s+"([^"]+)"(?:\s+"([^"]+)")?\s*\{', re.M)
ADDRESS_RE = re.compile(r'^(?:module\.([A-Za-z0-9_-]+)\.)?(?:(data)\.)?([A-Za-z0-9_]+)\.([A-Za-z0-9_-]+)(\[.+\])?$')


@dataclass(frozen=True)
class PlanChange:
    address: str
    actions: tuple[str, ...]


def parse_plan_changes(raw: object) -> list[PlanChange]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("plan_changes must be a non-empty list")
    out = []
    for i, c in enumerate(raw):
        if not isinstance(c, dict) or not isinstance(c.get("address"), str) or not isinstance(c.get("actions"), list):
            raise ValueError(f"plan_changes[{i}]: needs address (string) and actions (list)")
        bad = set(c["actions"]) - ACTIONS
        if bad or not c["actions"]:
            raise ValueError(f"plan_changes[{i}]: actions must be from {sorted(ACTIONS)}, got {c['actions']}")
        out.append(PlanChange(c["address"], tuple(sorted(set(c["actions"])))))
    return out


def blocks_in(code: str) -> set[tuple[str, str, str]]:
    """(kind, type_or_module_name, name) for every top-level resource/data/module block."""
    return {(m.group(1), m.group(2), m.group(3) or "") for m in BLOCK_RE.finditer(code)}


MAX_ADDRESSES = 1


def check_addresses(changes: list[PlanChange], code_before: str, code_after: str) -> list[str]:
    if len(changes) > MAX_ADDRESSES:
        return [f"the change touches {len(changes)} resources ({', '.join(c.address for c in changes)}); "
                "change ONLY the proposal's resource. With for_each/count, limit the edit to its key "
                '(e.g. `final_snapshot = each.key == "data"`) so other instances get no plan change']
    known = blocks_in(code_before) | blocks_in(code_after)
    resources = {(t, n) for k, t, n in known if k == "resource"}
    datas = {(t, n) for k, t, n in known if k == "data"}
    modules = {t for k, t, _ in known if k == "module"}
    problems = []
    for c in changes:
        m = ADDRESS_RE.match(c.address)
        if not m:
            problems.append(f"`{c.address}` isn't a Terraform resource address")
            continue
        module, is_data, rtype, name = m.group(1), m.group(2), m.group(3), m.group(4)
        if module:
            if module not in modules:
                problems.append(f"`{c.address}`: no module \"{module}\" in this stack")
        elif is_data:
            problems.append(f"`{c.address}`: data sources aren't changed by apply; don't list them")
        elif (rtype, name) not in resources:
            problems.append(f"`{c.address}`: no resource \"{rtype}\" \"{name}\" in this stack")
    return problems


def check_no_iam(diff: str) -> list[str]:
    touched = [l for l in diff.splitlines()
               if l[:1] in "+-" and not l.startswith(("+++", "---")) and "aws_iam_" in l]
    return ["IAM resources may not be changed by cost proposals (they need the IAM review path)"] if touched else []


def block_text(code: str, rtype: str, name: str) -> str | None:
    """Source of `resource "rtype" "name" { ... }` by brace matching (good enough for HCL we write)."""
    m = re.search(rf'^\s*resource\s+"{re.escape(rtype)}"\s+"{re.escape(name)}"\s*\{{', code, re.M)
    if not m:
        return None
    depth, i = 0, m.end() - 1
    while i < len(code):
        depth += {"{": 1, "}": -1}.get(code[i], 0)
        if depth == 0:
            return code[m.start():i + 1]
        i += 1
    return code[m.start():]


def check_snapshot_rule(changes: list[PlanChange], requires_snapshot: bool, code_before: str) -> list[str]:
    if not requires_snapshot:
        return []
    problems = []
    for c in changes:
        m = ADDRESS_RE.match(c.address)
        if not m or m.group(3) != "aws_ebs_volume" or not ({"delete", "replace"} & set(c.actions)):
            continue
        block = block_text(code_before, "aws_ebs_volume", m.group(4)) or ""
        if not re.search(r"^\s*final_snapshot\s*=\s*true\b", block, re.M):
            problems.append(
                f"`{c.address}` is deleted but the proposal requires a snapshot, and the current code "
                "doesn't set `final_snapshot = true` on it. Do step 1 only: set `final_snapshot = true` "
                "(plan action: update) and leave the volume in place; it can be removed in a later PR.")
    return problems


# --------------------------------------------------------------------------- #
# terraform fmt / validate
# --------------------------------------------------------------------------- #

def _tf_env(plugin_cache: Path) -> dict[str, str]:
    plugin_cache.mkdir(parents=True, exist_ok=True)
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    # No AWS or GitHub credentials: fmt and validate never need them.
    return {**keep, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1",
            "TF_PLUGIN_CACHE_DIR": str(plugin_cache)}


def terraform_fmt_validate(stack_dir: Path, plugin_cache: Path, timeout: int = 300) -> tuple[bool, str]:
    """Format in place, then init (no backend) + validate. Returns (ok, log)."""
    env = _tf_env(plugin_cache)
    run = lambda *args: subprocess.run(["terraform", f"-chdir={stack_dir}", *args], env=env,
                                       capture_output=True, text=True, timeout=timeout)
    fmt = run("fmt", "-no-color")
    if fmt.returncode != 0:
        return False, f"terraform fmt failed (syntax error?):\n{fmt.stderr or fmt.stdout}"
    init = run("init", "-backend=false", "-input=false", "-no-color")
    if init.returncode != 0:
        return False, f"terraform init failed:\n{init.stderr or init.stdout}"
    val = run("validate", "-json", "-no-color")
    try:
        result = json.loads(val.stdout)
    except json.JSONDecodeError:
        return False, f"terraform validate failed:\n{val.stderr or val.stdout}"
    if result.get("valid"):
        return True, f"fmt: {'reformatted ' + fmt.stdout.strip() if fmt.stdout.strip() else 'ok'}\nvalidate: ok"
    msgs = [f"- {d.get('severity')}: {d.get('summary')}: {d.get('detail', '')} "
            f"({(d.get('range') or {}).get('filename', '?')}:{((d.get('range') or {}).get('start') or {}).get('line', '?')})"
            for d in result.get("diagnostics", [])]
    return False, "terraform validate found problems:\n" + "\n".join(msgs)
