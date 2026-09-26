"""Find GitHub credentials that already exist on this machine."""

import os
import subprocess
from dataclasses import dataclass

from app.services import gh_cli

GH_CLI_SOURCE = gh_cli.SOURCE
ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN")
TIMEOUT_SECONDS = 10

# When no helper has a login, git falls back to asking the user. Terminal
# prompts are easy to switch off, but GIT_TERMINAL_PROMPT does not stop git
# running an askpass program (SSH_ASKPASS is common on Linux desktops), which
# would pop up a password dialog. Pointing GIT_ASKPASS at git itself makes that
# step fail instantly on every platform, since git is necessarily on the PATH.
NO_PROMPT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "git",
    "GCM_INTERACTIVE": "never",
}


@dataclass(frozen=True)
class Credential:
    source: str
    label: str
    token: str


def from_gh_cli() -> Credential | None:
    token = gh_cli.get_token()

    if not token:
        return None

    return Credential(GH_CLI_SOURCE, "GitHub CLI (gh)", token)


def from_env() -> list[Credential]:
    credentials = []

    for name in ENV_VARS:
        token = os.getenv(name, "").strip()

        if token:
            credentials.append(Credential(f"env:{name}", f"${name}", token))

    return credentials


def from_git_credential_helper() -> Credential | None:
    """
    Ask git's configured credential helper (Keychain, Git Credential Manager,
    libsecret...) for a github.com password without ever prompting the user.
    """
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, **NO_PROMPT_ENV},
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    fields = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    token = fields.get("password", "").strip()

    if not token:
        return None

    return Credential("git_credential", "git credential helper", token)


def discover() -> list[Credential]:
    """Return every distinct credential found, most trustworthy source first."""
    found = [from_gh_cli(), *from_env(), from_git_credential_helper()]

    credentials = []
    seen_tokens = set()

    for credential in found:
        if credential and credential.token not in seen_tokens:
            seen_tokens.add(credential.token)
            credentials.append(credential)

    return credentials
