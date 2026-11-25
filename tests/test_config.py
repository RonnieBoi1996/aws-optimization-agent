import pytest
import yaml
from pydantic import ValidationError

from optimizer.config import DEFAULT_CONFIG_PATH, load_config


def test_repo_config_loads_with_section_6_defaults():
    c = load_config()
    assert c.thresholds.max_p99_increase_ms == 10
    assert c.thresholds.max_sla_headroom_fraction == 0.25
    assert c.thresholds.max_latency_fix_monthly_cost_usd == 20
    assert c.guardrails.max_agent_turns == 10
    assert c.guardrails.dry_run_default is True
    # every configured model has a price, so cost estimates never silently drop calls
    for role in ("fast", "smart"):
        assert getattr(c.models, role).id in c.pricing_usd_per_mtok


def test_account_id_comes_from_env(monkeypatch):
    assert load_config().aws.account_id == "123456789012"
    monkeypatch.delenv("OPTIMIZER_AWS_ACCOUNT_ID")
    with pytest.raises(RuntimeError, match="OPTIMIZER_AWS_ACCOUNT_ID"):
        load_config()


def test_bad_account_id_rejected(monkeypatch):
    monkeypatch.setenv("OPTIMIZER_AWS_ACCOUNT_ID", "12345")
    with pytest.raises(ValidationError):
        load_config()


def test_account_id_in_yaml_is_refused(tmp_path):
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    raw["aws"]["account_id"] = "123456789012"
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="public"):
        load_config(p)


def test_unknown_key_is_rejected(tmp_path):
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    raw["thresholds"]["max_p99_increse_ms"] = 5  # typo
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError):
        load_config(p)
