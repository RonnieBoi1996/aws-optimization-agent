import pytest


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    # Tests never use the real account ID. 123456789012 is AWS's documented example.
    monkeypatch.setenv("OPTIMIZER_AWS_ACCOUNT_ID", "123456789012")
    # Never touch real AWS: drop any profile and use moto's dummy credentials.
    for var in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    # Cost Explorer / Pricing cache goes to a temp dir, not the repo's .cache/
    monkeypatch.setenv("OPTIMIZER_CACHE_DIR", str(tmp_path / "cache"))
