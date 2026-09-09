"""Shared test setup: a clean environment, and a client that carries a token.

Two things every test in this suite needs and none of them should have to
think about.
"""
import os
import secrets
import sys

import pytest
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Variables that change how the app protects itself. A test must see the same
# values whatever the developer happens to have exported -- the transit
# project in this family had a test asserting a default while reading the
# environment, and it passed locally and failed for anyone with HOST set.
CONFIG_VARS = ("HOST", "PORT", "FLASK_DEBUG", "SECRET_KEY",
               "WALLET_PASSWORD_HASH", "WALLET_INBOX")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Unset every configuration variable, for every test.

    A test that wants one sets it explicitly, which also makes it obvious in
    the test what shape of app is being exercised.
    """
    for name in CONFIG_VARS:
        monkeypatch.delenv(name, raising=False)


class CsrfClient(FlaskClient):
    """A test client that carries the session's CSRF token.

    A browser reads the token from a meta tag the server rendered; a test
    client has no page to read one from, so it seeds the session directly.

    Written as a client rather than as an argument on every call for two
    reasons. The tests read exactly as they did before CSRF existed, so the
    diff that added it is not sixty lines of noise. And the tests that check
    the protection actually works have to opt *out* deliberately, which means
    they cannot pass by accident.
    """

    def open(self, *args, **kwargs):
        if not kwargs.pop("no_csrf", False):
            with self.session_transaction() as stored:
                token = stored.get("csrf")
                if not token:
                    token = secrets.token_urlsafe(32)
                    stored["csrf"] = token
            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("X-CSRF-Token", token)
            kwargs["headers"] = headers
        return super().open(*args, **kwargs)
