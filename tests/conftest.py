import pytest


@pytest.fixture(autouse=True)
def _fake_account_id(monkeypatch):
    # Tests never use the real account ID. 123456789012 is AWS's documented example.
    monkeypatch.setenv("OPTIMIZER_AWS_ACCOUNT_ID", "123456789012")
