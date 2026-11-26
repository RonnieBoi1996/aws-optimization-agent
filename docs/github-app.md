# GitHub App for the optimizer's PRs

The optimizer opens pull requests as a GitHub App. It never uses your own account
or a personal token. The App can create branches, commits and PRs. It **can't merge**:

- `main` is protected by the rulesets (docs/github-setup.md): PR, passing checks,
  code-owner approval. The App isn't a bypass actor, and it can't approve its own PR.
- The GitHub MCP server is started with exactly four tools (`list_pull_requests`,
  `create_branch`, `push_files`, `create_pull_request`), and the code refuses any
  other tool name too (`src/optimizer/iac/publisher.py`).
- The App has no **Workflows** permission, so it can't change `.github/workflows/`,
  and the PR pipeline reads its scripts (`ci/`) from the base branch.

Each `optimizer pr` asks GitHub for a 1-hour token limited to the one target repo
and to `contents` + `pull_requests` write.

## Create it (once, ~5 minutes, in the browser)

1. GitHub → your avatar → **Settings** → **Developer settings** → **GitHub Apps** →
   **New GitHub App**.
2. Fill in:
   - **GitHub App name:** anything unique, e.g. `aws-optimizer-<your username>`
   - **Homepage URL:** `https://github.com/RonnieBoi1996/aws-optimization-agent` (required by GitHub, only shown on the App page; it grants nothing)
   - **Webhook:** untick **Active** (the App doesn't receive events)
   - **Repository permissions:**
     - **Contents:** Read and write
     - **Pull requests:** Read and write
     - **Metadata:** Read-only (set automatically)
     - everything else: No access. In particular, **not** Workflows or Administration.
   - **Where can this GitHub App be installed?** Only on this account
3. **Create GitHub App.**
4. On the App's page: **Private keys** → **Generate a private key**. A `.pem` file
   downloads. Treat it like a password.
5. Left menu → **Install App** → **Install** → **Only select repositories** →
   `aws-optimization-agent` and `infra-live` → **Install**.

## Store the key

```bash
mkdir -p ~/.config/aws-optimizer
mv ~/Downloads/*.private-key.pem ~/.config/aws-optimizer/github-app.pem
chmod 600 ~/.config/aws-optimizer/github-app.pem
```

`optimizer pr` refuses to use a key file other users can read.

## IDs (not secret)

Two numbers, added to `~/.config/aws-optimizer/anthropic.env`:

| Variable | Where to find it |
| --- | --- |
| `OPTIMIZER_GITHUB_APP_ID` | the App's settings page, "App ID" |
| `OPTIMIZER_GITHUB_APP_INSTALLATION_ID` | Settings → Applications → Installed GitHub Apps → **Configure**: the number at the end of the URL (`github.com/settings/installations/<id>`). (`gh api /user/installations` does NOT work: it needs a token issued by a GitHub App, not your gh login.) |
| `OPTIMIZER_GITHUB_APP_KEY_FILE` | optional; defaults to `~/.config/aws-optimizer/github-app.pem` |

## Revoke

App settings → **Private keys** → delete the key (all tokens stop working within the
hour), or uninstall the App from the repos.
