import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sync", ROOT / "pipelines" / "sync.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def test_infra_live_workflows_are_up_to_date():
    for name in sync.REPLACEMENTS:
        committed = (ROOT / "pipelines" / "workflows" / name).read_text(encoding="utf-8")
        assert committed == sync.render(name), f"run: python3 pipelines/sync.py ({name} is stale)"


def test_every_action_is_pinned_to_a_sha():
    import re
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        for line in wf.read_text().splitlines():
            m = re.search(r"uses:\s*([^\s#]+)", line)
            if m and not m.group(1).startswith("./"):
                assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", m.group(1)), f"{wf.name}: {line.strip()}"
