from unittest.mock import Mock

import pytest

from app.services import auth, local_credentials, storage

# Captured before conftest's autouse fixture stubs it out.
REAL_IS_INTERACTIVE = auth.is_interactive


def test_login_requires_credentials(monkeypatch):
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    monkeypatch.setattr(auth.storage, "get_client_id", Mock(return_value=None))
    monkeypatch.setattr(auth, "DEFAULT_CLIENT_ID", "")
    with pytest.raises(RuntimeError, match="No GitHub login for Recon to use"):
        auth.login()


def test_login_falls_back_to_device_flow_without_gh(monkeypatch):
    monkeypatch.setattr(auth, "DEFAULT_CLIENT_ID", "shipped")
    device_flow = Mock()
    monkeypatch.setattr(auth, "device_flow_login", device_flow)

    auth.login()

    device_flow.assert_called_once_with("shipped")


def test_explicit_client_id_skips_local_login(monkeypatch):
    use_local_login = Mock(return_value=True)
    monkeypatch.setattr(auth, "use_local_login", use_local_login)
    device_flow = Mock()
    monkeypatch.setattr(auth, "device_flow_login", device_flow)
    monkeypatch.setattr(auth.storage, "save_client_id", Mock())

    auth.login(client_id="abc123")

    use_local_login.assert_not_called()
    device_flow.assert_called_once_with("abc123")


def test_client_id_precedence(monkeypatch):
    monkeypatch.setattr(auth, "DEFAULT_CLIENT_ID", "default")
    monkeypatch.setattr(auth.storage, "get_client_id", Mock(return_value=None))
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    assert auth.get_client_id() == "default"

    monkeypatch.setattr(auth.storage, "get_client_id", Mock(return_value="saved"))
    assert auth.get_client_id() == "saved"

    monkeypatch.setenv("GITHUB_CLIENT_ID", "from-env")
    assert auth.get_client_id() == "from-env"


def test_login_saves_client_id_option(monkeypatch):
    save_client_id = Mock()
    monkeypatch.setattr(auth.storage, "save_client_id", save_client_id)
    monkeypatch.setattr(
        auth,
        "request_device_code",
        Mock(return_value={"verification_uri": "u", "user_code": "c"}),
    )
    monkeypatch.setattr(auth.webbrowser, "open", Mock())
    monkeypatch.setattr(auth, "poll_for_token", Mock(return_value="tok"))
    save_token = Mock()
    monkeypatch.setattr(auth, "save_token", save_token)

    auth.login(client_id="abc123")

    save_client_id.assert_called_once_with("abc123")
    save_token.assert_called_once_with("tok")


def test_login_with_token_verifies_and_saves(monkeypatch):
    get = Mock(return_value=Mock(status_code=200, json=lambda: {"login": "octo"}))
    save_token = Mock()
    monkeypatch.setattr(auth.requests, "get", get)
    monkeypatch.setattr(auth, "save_token", save_token)

    auth.login(token="pat")

    save_token.assert_called_once_with("pat")


def test_login_with_bad_token_raises(monkeypatch):
    monkeypatch.setattr(auth.requests, "get", Mock(return_value=Mock(status_code=401)))
    save_token = Mock()
    monkeypatch.setattr(auth, "save_token", save_token)

    with pytest.raises(RuntimeError, match="rejected that token"):
        auth.login(token="bad")

    save_token.assert_not_called()


GH_CREDENTIAL = local_credentials.Credential("gh_cli", "GitHub CLI (gh)", "gho_token")
ENV_CREDENTIAL = local_credentials.Credential("env:GH_TOKEN", "$GH_TOKEN", "env_token")


def interactive(monkeypatch, answers, credentials, accounts):
    """Run the local-login flow with scripted prompt answers and GitHub accounts."""
    confirm = Mock(side_effect=answers)
    monkeypatch.setattr(auth, "is_interactive", lambda: True)
    monkeypatch.setattr(auth.typer, "confirm", confirm)
    discover = Mock(return_value=credentials)
    monkeypatch.setattr(auth.local_credentials, "discover", discover)
    monkeypatch.setattr(auth, "fetch_login", lambda token: accounts.get(token))
    device_flow = Mock()
    monkeypatch.setattr(auth, "device_flow_login", device_flow)
    monkeypatch.setattr(auth, "DEFAULT_CLIENT_ID", "shipped")
    return confirm, device_flow, discover


def test_local_login_asks_before_searching(monkeypatch):
    confirm, device_flow, discover = interactive(
        monkeypatch, [False], [GH_CREDENTIAL], {}
    )

    auth.login()

    discover.assert_not_called()
    assert "Look for GitHub logins" in confirm.call_args.args[0]
    device_flow.assert_called_once_with("shipped")


def test_local_login_uses_confirmed_gh_account_without_copying(monkeypatch, capsys):
    confirm, device_flow, _ = interactive(
        monkeypatch, [True, True], [GH_CREDENTIAL], {"gho_token": "octo"}
    )

    auth.login()

    assert "logged in as octo" in confirm.call_args.args[0]
    device_flow.assert_not_called()
    assert storage.get_credential_source() == "gh_cli"
    # The gh token is read live from gh on each call, never copied.
    assert storage.get_token() is None
    assert "octo" in capsys.readouterr().out


def test_local_login_moves_on_when_account_is_not_yours(monkeypatch):
    _, device_flow, _ = interactive(
        monkeypatch,
        [True, False, True],
        [GH_CREDENTIAL, ENV_CREDENTIAL],
        {"gho_token": "work-account", "env_token": "octo"},
    )

    auth.login()

    device_flow.assert_not_called()
    assert storage.get_credential_source() is None
    assert storage.get_token() == "env_token"


def test_local_login_falls_back_when_every_account_is_declined(monkeypatch):
    _, device_flow, _ = interactive(
        monkeypatch, [True, False], [GH_CREDENTIAL], {"gho_token": "octo"}
    )

    auth.login()

    device_flow.assert_called_once_with("shipped")
    assert storage.get_credential_source() is None
    assert storage.get_token() is None


def test_local_login_skips_rejected_tokens(monkeypatch, capsys):
    confirm, device_flow, _ = interactive(monkeypatch, [True], [GH_CREDENTIAL], {})

    auth.login()

    assert confirm.call_count == 1
    assert "did not accept" in capsys.readouterr().out
    device_flow.assert_called_once_with("shipped")


def test_local_login_reports_when_nothing_is_found(monkeypatch, capsys):
    _, device_flow, _ = interactive(monkeypatch, [True], [], {})

    auth.login()

    assert "No existing GitHub logins" in capsys.readouterr().out
    device_flow.assert_called_once_with("shipped")


def test_local_login_never_prompts_without_a_terminal(monkeypatch):
    confirm = Mock()
    monkeypatch.setattr(auth.typer, "confirm", confirm)

    assert auth.use_local_login() is False
    confirm.assert_not_called()


def test_adopting_gh_replaces_a_saved_token(monkeypatch):
    storage.save_token("old")

    auth.adopt_credential(GH_CREDENTIAL, "octo")

    assert storage.get_token() is None
    assert storage.get_credential_source() == "gh_cli"


def test_fetch_login(monkeypatch):
    monkeypatch.setattr(
        auth.requests,
        "get",
        Mock(return_value=Mock(status_code=200, json=lambda: {"login": "octo"})),
    )
    assert auth.fetch_login("tok") == "octo"

    monkeypatch.setattr(auth.requests, "get", Mock(return_value=Mock(status_code=403)))
    assert auth.fetch_login("tok") is None


def test_poll_for_token_returns_immediately(monkeypatch):
    monkeypatch.setattr(
        auth.requests,
        "post",
        Mock(return_value=Mock(json=lambda: {"access_token": "tok"})),
    )
    assert auth.poll_for_token({"device_code": "code"}) == "tok"


def test_poll_for_token_retries_pending(monkeypatch):
    post = Mock(
        side_effect=[
            Mock(json=lambda: {"error": "authorization_pending"}),
            Mock(json=lambda: {"access_token": "tok"}),
        ]
    )
    sleep = Mock()
    monkeypatch.setattr(auth.requests, "post", post)
    monkeypatch.setattr(auth.time, "sleep", sleep)
    assert auth.poll_for_token({"device_code": "code", "interval": 2}) == "tok"
    sleep.assert_called_once_with(2)


def test_poll_for_token_other_error_raises(monkeypatch):
    monkeypatch.setattr(
        auth.requests,
        "post",
        Mock(return_value=Mock(json=lambda: {"error": "expired_token"})),
    )
    with pytest.raises(RuntimeError, match="expired_token"):
        auth.poll_for_token({"device_code": "code"})


def test_logout_clears_token(monkeypatch):
    storage.save_token("tok")
    auth.logout()
    assert storage.get_token() is None


def test_logout_revokes_gh_permission_but_leaves_gh_alone(monkeypatch, capsys):
    storage.save_credential_source("gh_cli")

    auth.logout()

    assert storage.get_credential_source() is None
    assert "gh auth logout" in capsys.readouterr().out


def test_logout_when_not_logged_in(capsys):
    auth.logout()
    assert "not logged in" in capsys.readouterr().out


def test_is_interactive_needs_a_terminal_on_both_ends(monkeypatch):
    for stdin_tty, stdout_tty, expected in (
        (True, True, True),
        # Windows reports the NUL device as a terminal, so stdin alone is not
        # enough to know someone is there to answer.
        (True, False, False),
        (False, True, False),
    ):
        monkeypatch.setattr(auth.sys.stdin, "isatty", lambda v=stdin_tty: v)
        monkeypatch.setattr(auth.sys.stdout, "isatty", lambda v=stdout_tty: v)
        assert REAL_IS_INTERACTIVE() is expected
