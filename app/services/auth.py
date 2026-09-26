import os
import sys
import time
import webbrowser

import requests
import typer
from dotenv import load_dotenv

from app.services import local_credentials, storage
from app.services.storage import clear_token, save_token

load_dotenv()

# Public client ID of the Recon GitHub OAuth app. Device-flow client IDs are not
# secrets, so shipping one here lets the device flow work straight after
# `uv tool install recon-github`, with no .env file next to the user's shell.
# Existing logins on the machine are offered first, so this is only needed when
# there are none, or the user declines them.
DEFAULT_CLIENT_ID = ""

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"

NO_CREDENTIALS_MESSAGE = (
    "No GitHub login for Recon to use.\n"
    "The easiest fix is the GitHub CLI, which Recon can reuse:\n"
    "    gh auth login && recon login\n"
    "Otherwise log in with a personal access token:\n"
    "    recon login --token <token>\n"
    "or point Recon at your own OAuth app (Settings > Developer settings >\n"
    "OAuth Apps, with device flow enabled):\n"
    "    recon login --client-id <client id>\n"
    "The GITHUB_CLIENT_ID environment variable works too."
)

LOCAL_SEARCH_PROMPT = (
    "Look for GitHub logins already on this machine "
    "(GitHub CLI, $GH_TOKEN/$GITHUB_TOKEN, git credential helper)?"
)


def get_client_id():
    """Resolve the OAuth client ID: environment, then saved config, then default."""
    return os.getenv("GITHUB_CLIENT_ID") or storage.get_client_id() or DEFAULT_CLIENT_ID


def login(client_id: str | None = None, token: str | None = None):
    """
    Authenticate Recon with GitHub.

    With no options Recon asks before looking for a login already on this
    machine, then asks whether each one it finds is the user's account. It only
    falls back to its own OAuth device flow when none is accepted.
    """
    if token:
        login_with_token(token)
        return

    if not client_id and use_local_login():
        return

    if client_id:
        storage.save_client_id(client_id)
    else:
        client_id = get_client_id()

    if not client_id:
        raise RuntimeError(NO_CREDENTIALS_MESSAGE)

    device_flow_login(client_id)


def is_interactive() -> bool:
    """
    Only prompt when a person can see the question and answer it.

    Checking stdout as well as stdin matters on Windows, where the NUL device
    (`< NUL`, or `< /dev/null` in Git Bash) reports itself as a terminal.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def use_local_login() -> bool:
    """
    Offer credentials already on this machine, with the user's permission.

    Nothing is read until the user agrees to the search, and nothing is used
    until they confirm the account it belongs to is theirs.
    """
    if not is_interactive():
        return False

    if not typer.confirm(LOCAL_SEARCH_PROMPT, default=True):
        return False

    credentials = local_credentials.discover()

    if not credentials:
        print("No existing GitHub logins found on this machine.")
        return False

    for credential in credentials:
        account = fetch_login(credential.token)

        if not account:
            print(f"Skipping {credential.label}: GitHub did not accept its token.")
            continue

        if typer.confirm(
            f"Found {credential.label} logged in as {account}. "
            "Is this your account, and should Recon use it?",
            default=True,
        ):
            adopt_credential(credential, account)
            return True

    return False


def adopt_credential(credential: local_credentials.Credential, account: str):
    """
    Remember the credential the user accepted.

    A GitHub CLI token stays in gh's own secure storage and is read live, so we
    only record that the user allowed it. Other sources can disappear (an
    environment variable) or prompt every time (a keychain), so their token is
    saved the same way `recon login --token` would.
    """
    if credential.source == local_credentials.GH_CLI_SOURCE:
        clear_token()
        storage.save_credential_source(credential.source)
        print(f"Recon will use your GitHub CLI login ({account}).")
        return

    storage.clear_credential_source()
    save_token(credential.token)
    print(f"Successfully logged into GitHub as {account}!")


def fetch_login(token: str) -> str | None:
    """Return the username a token belongs to, or None if GitHub rejects it."""
    response = requests.get(
        USER_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
        },
        timeout=10,
    )

    if response.status_code in (401, 403):
        return None

    response.raise_for_status()

    return response.json()["login"]


def device_flow_login(client_id: str):
    device = request_device_code(client_id)

    print("Opening GitHub authentication...")

    webbrowser.open(device.get("verification_uri_complete", device["verification_uri"]))

    print(f"If required, enter code: {device['user_code']}")

    access_token = poll_for_token(device, client_id)

    storage.clear_credential_source()
    save_token(access_token)

    print("Successfully logged into GitHub!")


def login_with_token(token: str):
    """Store a personal access token after checking GitHub accepts it."""
    account = fetch_login(token)

    if not account:
        raise RuntimeError("GitHub rejected that token. Check it and try again.")

    storage.clear_credential_source()
    save_token(token)

    print(f"Successfully logged into GitHub as {account}!")


def request_device_code(client_id: str | None = None):
    response = requests.post(
        DEVICE_CODE_URL,
        headers={"Accept": "application/json"},
        data={
            "client_id": client_id or get_client_id(),
            "scope": "read:user repo",
        },
        timeout=10,
    )

    response.raise_for_status()

    return response.json()


def poll_for_token(device, client_id: str | None = None):
    interval = device.get("interval", 5)

    while True:
        response = requests.post(
            TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": client_id or get_client_id(),
                "device_code": device["device_code"],
                "grant_type": ("urn:ietf:params:oauth:grant-type:device_code"),
            },
            timeout=10,
        )

        data = response.json()

        if "access_token" in data:
            return data["access_token"]

        if data.get("error") != "authorization_pending":
            raise RuntimeError(data)

        time.sleep(interval)


def logout():
    """
    Forget Recon's own token and any permission to use the GitHub CLI login.

    A GitHub CLI login is not Recon's to revoke, so we point at `gh auth logout`
    rather than touching it.
    """
    had_token = storage.get_token() is not None
    used_gh_cli = storage.get_credential_source() == local_credentials.GH_CLI_SOURCE

    clear_token()
    storage.clear_credential_source()

    if not (had_token or used_gh_cli):
        print("You were not logged in.")
        return

    print("Successfully logged out of GitHub!")

    if used_gh_cli:
        print("Your GitHub CLI login is untouched. Run `gh auth logout` to end it.")
