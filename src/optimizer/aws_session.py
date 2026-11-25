"""AWS session for the agents, with a hard identity check.

The aws-insights servers must only ever run as optimizer-agent-read (section 5).
Rather than trusting that AWS_PROFILE is set right, we ask STS who we are and
refuse to start otherwise, so running with the admin profile by mistake fails
loudly instead of silently using admin rights.
"""

from __future__ import annotations

import boto3

AGENT_ROLE_NAME = "optimizer-agent-read"


class WrongIdentity(RuntimeError):
    pass


def check_identity(arn: str, account: str, expected_account: str) -> None:
    if account != expected_account:
        raise WrongIdentity(f"Credentials are for account ...{account[-4:]}, expected "
                            f"...{expected_account[-4:]} (OPTIMIZER_AWS_ACCOUNT_ID).")
    # Assumed-role ARN: arn:aws:sts::<acct>:assumed-role/<role-name>/<session>
    if f":assumed-role/{AGENT_ROLE_NAME}/" not in arn:
        who = arn.split(":", 5)[-1]
        raise WrongIdentity(f"Running as '{who}', but the agents may only run as "
                            f"{AGENT_ROLE_NAME}. Use AWS_PROFILE=agent-dev locally.")


def agent_session(region: str, expected_account: str) -> boto3.Session:
    """A boto3 session verified to be optimizer-agent-read in the right account."""
    session = boto3.Session(region_name=region)
    ident = session.client("sts").get_caller_identity()
    check_identity(ident["Arn"], ident["Account"], expected_account)
    return session
