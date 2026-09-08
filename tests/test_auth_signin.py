"""The sign-in must not lose a token it has already obtained.

No network and no browser: the token exchange is driven through a fake session,
which is enough to pin the property that matters - that nothing between saving
the token and returning it can cost the sign-in.
"""
import json

import pytest

from pipelineinsertion import auth, config


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    """A session that answers the token exchange and records what it was sent."""

    def __init__(self, payload=None):
        self.payload = payload or {"access_token": "TOKEN-123", "expires_in": 3600}
        self.posted = []

    def post(self, url, data=None, **kwargs):
        self.posted.append((url, data))
        return FakeResponse(self.payload)


@pytest.fixture
def captured_keyring(monkeypatch):
    """Keyring writes go to a dict rather than the OS credential store."""
    store = {}
    monkeypatch.setattr(auth, "keyring_set",
                        lambda name, value: store.__setitem__(name, value))
    monkeypatch.setattr(auth, "keyring_get", lambda name: store.get(name))
    return store


@pytest.fixture
def signed_in(monkeypatch, captured_keyring):
    """Drive interactive_access_token past the browser to the token exchange."""
    monkeypatch.setattr(auth, "cached_access_token", lambda: "")
    monkeypatch.setattr(auth, "capture_loopback_authorization_code",
                        lambda url, **kw: "AUTH-CODE")
    monkeypatch.setattr(auth, "authorize_url_is_accepted",
                        lambda *a, **kw: True)
    monkeypatch.setattr(auth, "make_session", lambda: FakeSession())
    return captured_keyring


class TestNothingRunsBetweenSavingAndReturning:
    """The failure this pins.

    A cosmetic step used to sit between the keyring write and the return: a
    PowerShell process that hunted for the callback window by title and sent it
    Ctrl+W. On a machine with endpoint scanning, starting PowerShell is slow
    enough to look like a hang, and an interrupt there lost a sign-in that had
    already succeeded - the token was in the credential store and the run died
    anyway.
    """

    def test_the_token_is_returned_and_stored(self, signed_in, monkeypatch):
        session = FakeSession()
        token = auth.interactive_access_token(session)
        assert token == "TOKEN-123"
        assert signed_in[config.KEYRING_ACCESS_TOKEN_USER] == "TOKEN-123"

    def test_no_subprocess_is_spawned_during_sign_in(self, signed_in, monkeypatch):
        """Nothing in the sign-in path may launch another process.

        A spawn here is a hang waiting to happen and, in the case that was
        removed, a global keystroke sent to whichever window happened to be
        focused.
        """
        import subprocess

        def refuse(*args, **kwargs):
            raise AssertionError(f"sign-in spawned a process: {args!r}")

        monkeypatch.setattr(subprocess, "Popen", refuse)
        monkeypatch.setattr(subprocess, "run", refuse)
        assert auth.interactive_access_token(FakeSession()) == "TOKEN-123"

    def test_the_tab_closing_helper_is_gone(self):
        # Its whole job is done by the page, which closes itself.
        assert not hasattr(auth, "close_loopback_callback_tab")

    def test_auth_does_not_import_subprocess_any_more(self):
        # The word "PowerShell" still appears in the comment explaining why
        # this was removed, so the check is on the code, not the prose.
        source = (config.REPO_ROOT / "src" / "pipelineinsertion"
                  / "auth.py").read_text(encoding="utf-8-sig")
        assert "import subprocess" not in source
        assert "SendKeys" not in source
        assert "subprocess.Popen" not in source


class TestTheCallbackPageClosesItself:
    # Served over a socket, so the constant is bytes.
    PAGE = auth.LOOPBACK_SUCCESS_PAGE.decode("utf-8")

    def test_it_tries_to_close(self):
        assert "window.close()" in self.PAGE

    def test_it_says_so_when_the_browser_refuses(self):
        """Browsers refuse window.close() for a tab a script did not open.

        Without the fallback message the tab sits on "Closing this tab..."
        forever, which reads as a hung sign-in.
        """
        assert "can be closed" in self.PAGE

    def test_the_token_never_reaches_the_page(self):
        # The page is served to a browser; it carries no secret.
        for marker in ("access_token", "client_id", "grant_type"):
            assert marker not in self.PAGE


class TestTokenExchange:
    def test_a_failed_exchange_is_fatal_rather_than_a_blank_token(self, signed_in):
        session = FakeSession({"error": {"message": "bad code"}})
        with pytest.raises(RuntimeError, match="OAuth token request failed"):
            auth.interactive_access_token(session)

    def test_a_response_with_no_token_is_fatal(self, signed_in):
        session = FakeSession({"expires_in": 3600})
        with pytest.raises(RuntimeError, match="did not include"):
            auth.interactive_access_token(session)

    def test_the_expiry_is_stored_alongside_the_token(self, signed_in):
        auth.interactive_access_token(FakeSession())
        assert float(signed_in[config.KEYRING_ACCESS_TOKEN_EXPIRES_USER]) > 0
