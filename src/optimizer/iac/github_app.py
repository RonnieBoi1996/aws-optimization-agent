"""GitHub App credentials for opening PRs (rule 3: create branches and PRs, never merge).

The App's private key lives outside the repo (default
~/.config/aws-optimizer/github-app.pem, chmod 600). Each `optimizer pr` call:
  1. signs a 9-minute JWT with the key (proves "I am the App"),
  2. trades it for an installation token limited to ONE repo and to
     contents:write + pull_requests:write, valid for 1 hour,
  3. hands that token to the GitHub MCP server through its environment only
     (never a command-line argument, never a log line).

Setup: docs/github-app.md.
"""

from __future__ import annotations

import json
import os
import stat
import time
import urllib.request
from pathlib import Path

import jwt

APP_ID_ENV = "OPTIMIZER_GITHUB_APP_ID"
INSTALLATION_ENV = "OPTIMIZER_GITHUB_APP_INSTALLATION_ID"
KEY_FILE_ENV = "OPTIMIZER_GITHUB_APP_KEY_FILE"
DEFAULT_KEY_FILE = Path.home() / ".config" / "aws-optimizer" / "github-app.pem"
API = "https://api.github.com"
TOKEN_PERMISSIONS = {"contents": "write", "pull_requests": "write"}


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set (see docs/github-app.md)")
    return value


def load_key(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"GitHub App private key not found at {path} (see docs/github-app.md)")
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError(f"{path} is readable by other users; run: chmod 600 {path}")
    return path.read_text(encoding="utf-8")


def app_jwt(app_id: str, private_key: str, now: float | None = None) -> str:
    now = int(now if now is not None else time.time())
    # iat 60s in the past absorbs clock drift; GitHub allows at most 10 minutes.
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": app_id}, private_key, algorithm="RS256")


def installation_token(repo: str, opener=urllib.request.urlopen) -> str:
    app_id, installation = _env(APP_ID_ENV), _env(INSTALLATION_ENV)
    key = load_key(Path(os.environ.get(KEY_FILE_ENV) or DEFAULT_KEY_FILE).expanduser())
    req = urllib.request.Request(
        f"{API}/app/installations/{installation}/access_tokens", method="POST",
        data=json.dumps({"repositories": [repo], "permissions": TOKEN_PERMISSIONS}).encode(),
        headers={"Authorization": f"Bearer {app_jwt(app_id, key)}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "aws-optimizer"})
    try:
        with opener(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub refused the App token request ({e.code}): is the App installed on "
                           f"{repo}, with Contents and Pull requests write?") from None
    return body["token"]
