"""The password, the session, the token and the headers.

The test this file exists for is the first one: **the app refuses to start
reachable without a password.** Everything else here is a mitigation that can
be argued about. That one is the difference between a private ledger and a
public one, and it is the only mistake in this file that cannot be undone
once made.

The rest follow a theme worth stating: each checks a *refusal*. Security code
that has only been tested doing the permitted thing has not been tested.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import conftest     # noqa: E402
import app as web   # noqa: E402
import auth         # noqa: E402

PASSWORD = "a long enough password to be worth having"
STRONG_KEY = "k" * 64


@pytest.fixture(autouse=True)
def no_leftover_failures():
    auth.reset_failures()
    yield
    auth.reset_failures()


@pytest.fixture
def hashed():
    return auth.hash_password(PASSWORD)


def locked_app(tmp_path, monkeypatch, hashed, host="127.0.0.1"):
    """An app that asks for a password, on `host`."""
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    monkeypatch.setenv("WALLET_PASSWORD_HASH", hashed)
    monkeypatch.setenv("SECRET_KEY", STRONG_KEY)
    application = web.create_app(str(tmp_path), host=host)
    application.config["TESTING"] = True
    application.test_client_class = conftest.CsrfClient
    return application


def sign_in(client, password=PASSWORD, **extra):
    return client.post("/login", data=dict(password=password, **extra),
                       follow_redirects=False)


# =============================================== the refusal that matters

def test_it_refuses_to_bind_a_public_address_with_no_password(tmp_path):
    """The one that cannot be walked back.

    Not a warning in a log nobody reads: the process does not start. A
    financial ledger reachable from the internet with no password on it is
    not a degraded mode, it is a disclosure.
    """
    with pytest.raises(auth.Unsafe) as refused:
        web.create_app(str(tmp_path), host="0.0.0.0")

    said = str(refused.value)
    assert "WALLET_PASSWORD_HASH" in said, "it must say what to set"
    assert "python -m auth" in said, "and how to produce it"
    assert "127.0.0.1" in said, "and the way out"


@pytest.mark.parametrize("host", [
    "0.0.0.0", "::", "192.168.1.40", "10.0.0.5", "wallet.example.com",
])
def test_every_reachable_address_is_refused(tmp_path, host):
    with pytest.raises(auth.Unsafe):
        web.create_app(str(tmp_path), host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_still_runs_with_nothing_configured(tmp_path, host):
    """What it has always done, and must keep doing: no password, no login,
    no ceremony on the machine it runs on."""
    application = web.create_app(str(tmp_path), host=host)
    assert auth.required(application) is False
    application.config["TESTING"] = True
    application.test_client_class = conftest.CsrfClient
    assert application.test_client().get("/").status_code == 200


def test_a_public_address_also_needs_a_strong_secret(tmp_path, monkeypatch,
                                                     hashed):
    """The cookie is signed with it. A guessable key lets anyone mint a
    logged-in session, which makes the password beside the point."""
    monkeypatch.setenv("WALLET_PASSWORD_HASH", hashed)
    for weak in ("", "dev", "secret", "x" * 31):
        monkeypatch.setenv("SECRET_KEY", weak)
        with pytest.raises(auth.Unsafe) as refused:
            web.create_app(str(tmp_path), host="0.0.0.0")
        assert "SECRET_KEY" in str(refused.value)


def test_a_public_address_with_both_is_allowed(tmp_path, monkeypatch, hashed):
    application = locked_app(tmp_path, monkeypatch, hashed, host="0.0.0.0")
    assert auth.required(application) is True
    assert application.config["WALLET_PUBLIC"] is True


def test_loopback_gets_a_working_key_without_one_being_set(tmp_path):
    """Sessions then do not survive a restart, which is a fair trade locally
    and never the case in a deployment, where a missing key is refused."""
    application = web.create_app(str(tmp_path), host="127.0.0.1")
    assert len(application.config["SECRET_KEY"]) >= 32


# ==================================================== hashing the password

def test_the_password_is_stored_as_scrypt_not_as_itself(hashed):
    assert hashed.startswith("scrypt:"), hashed.split(":")[0]
    assert PASSWORD not in hashed


def test_the_same_password_hashes_differently_every_time():
    """Salted. Two identical passwords must not produce the same hash, or a
    leaked file tells you which accounts share one."""
    assert auth.hash_password(PASSWORD) != auth.hash_password(PASSWORD)


def test_it_matches_the_right_password_and_nothing_else(hashed):
    assert auth.password_matches(PASSWORD, hashed) is True
    for wrong in (PASSWORD + " ", PASSWORD.upper(), PASSWORD[:-1], "", None):
        assert auth.password_matches(wrong, hashed) is False


def test_a_blank_password_cannot_be_set():
    for blank in ("", "   ", None):
        with pytest.raises(ValueError):
            auth.hash_password(blank)


@pytest.mark.parametrize("stored", [
    None, "", "not-a-hash", "scrypt:garbage",
    # These two make werkzeug raise ValueError rather than return False --
    # the parameters parse as a hash and then do not work as one. Without the
    # except clause a mangled environment variable is a 500 on every login,
    # which tells an attacker its configuration is broken.
    "scrypt:1:2:3$salt$bad", "scrypt:notanumber:8:1$s$h",
])
def test_a_missing_or_broken_hash_refuses_rather_than_raising(stored):
    """A misconfigured deployment should refuse logins, not return a 500."""
    assert auth.password_matches(PASSWORD, stored) is False


# ============================================================== logging in

def test_the_right_password_signs_you_in(tmp_path, monkeypatch, hashed):
    client = locked_app(tmp_path, monkeypatch, hashed).test_client()
    client.get("/login")
    reply = sign_in(client)
    assert reply.status_code == 302
    assert reply.headers["Location"] == "/"
    assert client.get("/api/overview").status_code == 200


def test_the_wrong_password_does_not(tmp_path, monkeypatch, hashed):
    client = locked_app(tmp_path, monkeypatch, hashed).test_client()
    client.get("/login")
    reply = sign_in(client, password="nope")
    assert reply.status_code == 401
    assert client.get("/api/overview").status_code == 401


def test_the_message_is_the_same_however_it_was_wrong(tmp_path, monkeypatch,
                                                      hashed):
    """Anything that differs between "no such password" and "close" is a way
    to learn about the password."""
    client = locked_app(tmp_path, monkeypatch, hashed).test_client()
    client.get("/login")
    said = set()
    for wrong in ("", "x", PASSWORD[:-1], PASSWORD.upper()):
        auth.reset_failures()
        said.add(sign_in(client, password=wrong).get_data(as_text=True))
    assert len(said) == 1, "the reply differs depending on the guess"


def test_signing_out_ends_the_session(tmp_path, monkeypatch, hashed):
    client = locked_app(tmp_path, monkeypatch, hashed).test_client()
    client.get("/login")
    sign_in(client)
    assert client.get("/api/overview").status_code == 200
    assert client.post("/logout").status_code == 302
    assert client.get("/api/overview").status_code == 401


def test_the_login_page_is_skipped_when_there_is_no_password(tmp_path):
    application = web.create_app(str(tmp_path), host="127.0.0.1")
    application.config["TESTING"] = True
    application.test_client_class = conftest.CsrfClient
    reply = application.test_client().get("/login")
    assert reply.status_code == 302
    assert reply.headers["Location"] == "/"


# ================================================================= backoff

def test_guessing_is_slowed_down_after_a_few_tries(tmp_path, monkeypatch,
                                                   hashed):
    """Three free, because fat fingers happen. Then a refusal window, so an
    online guessing attack costs time it does not have."""
    client = locked_app(tmp_path, monkeypatch, hashed).test_client()
    client.get("/login")

    for _ in range(auth.FREE_ATTEMPTS):
        assert sign_in(client, password="nope").status_code == 401
    assert auth.blocked_for() == 0, "the free attempts should not block"

    assert sign_in(client, password="nope").status_code == 401
    assert auth.blocked_for() > 0, "the fourth should"
    assert sign_in(client).status_code == 429, "even the right one waits"


def test_the_window_grows_and_is_capped():
    for _ in range(auth.FREE_ATTEMPTS):
        auth.note_failure(now=0)
    assert auth.blocked_for(now=0) == 0

    auth.note_failure(now=0)
    first = auth.blocked_for(now=0)
    auth.note_failure(now=0)
    second = auth.blocked_for(now=0)
    assert second > first, "it has to grow or it is not a deterrent"

    for _ in range(40):
        auth.note_failure(now=0)
    assert auth.blocked_for(now=0) == auth.MAX_BACKOFF_SECONDS, (
        "uncapped, it would eventually lock the owner out for years")


def test_it_refuses_rather_than_sleeping():
    """Holding the request open would let an attacker exhaust the worker pool
    for free, which turns a guessing defence into a denial of service."""
    import inspect
    source = inspect.getsource(auth)
    assert "time.sleep" not in source


def test_a_success_clears_the_record(tmp_path, monkeypatch, hashed):
    client = locked_app(tmp_path, monkeypatch, hashed).test_client()
    client.get("/login")
    for _ in range(auth.FREE_ATTEMPTS):
        sign_in(client, password="nope")
    sign_in(client)
    assert auth.blocked_for() == 0


# ==================================================================== CSRF

def test_a_state_changing_request_without_a_token_is_refused(tmp_path):
    """The check that matters once there is a cookie: a page on another site
    can make your browser POST here, and three of these routes take multipart
    uploads, which a plain cross-origin form can send."""
    application = web.create_app(str(tmp_path), host="127.0.0.1")
    application.config["TESTING"] = True
    application.test_client_class = conftest.CsrfClient
    client = application.test_client()

    reply = client.post("/api/transaction", json={
        "date": "2026-09-08", "description": "FORGED", "amount": "999"},
        no_csrf=True)
    assert reply.status_code == 403
    assert client.get("/api/transactions").get_json()["transactions"] == []


@pytest.mark.parametrize("method", ["post", "delete"])
def test_every_unsafe_method_is_covered(tmp_path, method):
    application = web.create_app(str(tmp_path), host="127.0.0.1")
    application.config["TESTING"] = True
    application.test_client_class = conftest.CsrfClient
    client = application.test_client()
    call = getattr(client, method)
    assert call("/api/transaction/1", no_csrf=True).status_code == 403


def test_reading_needs_no_token(tmp_path):
    """GET is exempt, because it is not supposed to change anything. If one
    ever does, that is the bug rather than the missing token."""
    application = web.create_app(str(tmp_path), host="127.0.0.1")
    application.config["TESTING"] = True
    application.test_client_class = conftest.CsrfClient
    assert application.test_client().get(
        "/api/overview", no_csrf=True).status_code == 200


def test_somebody_elses_token_does_not_work(tmp_path):
    application = web.create_app(str(tmp_path), host="127.0.0.1")
    application.config["TESTING"] = True
    application.test_client_class = conftest.CsrfClient
    client = application.test_client()
    client.get("/")
    reply = client.post("/api/transaction",
                        json={"date": "2026-09-08", "description": "X",
                              "amount": "1"},
                        headers={"X-CSRF-Token": "a" * 43}, no_csrf=True)
    assert reply.status_code == 403


def test_the_login_form_is_guarded_too(tmp_path, monkeypatch, hashed):
    client = locked_app(tmp_path, monkeypatch, hashed).test_client()
    client.get("/login")
    reply = client.post("/login", data={"password": PASSWORD}, no_csrf=True)
    assert reply.status_code == 403


def test_the_page_carries_the_token_for_the_scripts(tmp_path, monkeypatch,
                                                    hashed):
    client = locked_app(tmp_path, monkeypatch, hashed).test_client()
    client.get("/login")
    sign_in(client)
    page = client.get("/").get_data(as_text=True)
    assert 'name="csrf-token"' in page
    assert 'content=""' not in page, "the tag is there but empty"


# ============================================================== the gate

def test_an_api_call_without_a_session_is_a_401_it_can_act_on(tmp_path,
                                                              monkeypatch,
                                                              hashed):
    """Not a redirect to HTML: a fetch() following one would fail parsing
    JSON, and the page would show a parse error instead of "signed out"."""
    client = locked_app(tmp_path, monkeypatch, hashed).test_client()
    reply = client.get("/api/overview")
    assert reply.status_code == 401
    assert reply.get_json()["login"] is True


def test_a_page_without_a_session_goes_to_the_login_screen(tmp_path,
                                                           monkeypatch,
                                                           hashed):
    client = locked_app(tmp_path, monkeypatch, hashed).test_client()
    reply = client.get("/")
    assert reply.status_code == 302
    assert "/login" in reply.headers["Location"]


def test_every_route_is_closed_unless_it_is_named_open(tmp_path, monkeypatch,
                                                       hashed):
    """The guarantee worth having: a route added next year is protected
    because it was not opted out, rather than exposed because somebody forgot
    to opt it in.

    Walks the real url_map instead of a list, so a new endpoint cannot be
    added without either being covered here or appearing in OPEN_ENDPOINTS.
    """
    application = locked_app(tmp_path, monkeypatch, hashed)
    client = application.test_client()

    checked = 0
    for rule in application.url_map.iter_rules():
        if rule.endpoint in web.OPEN_ENDPOINTS:
            continue
        if "GET" not in rule.methods:
            continue
        # Fill any <int:tid> style placeholder with something harmless.
        path = rule.rule
        for name in rule.arguments:
            path = path.replace(f"<int:{name}>", "1").replace(
                f"<{name}>", "1")
        reply = client.get(path)
        assert reply.status_code in (302, 401), (
            f"{rule.endpoint} answered {reply.status_code} with no session")
        checked += 1

    assert checked > 15, f"only {checked} routes were actually checked"


def test_the_open_list_is_short_and_deliberate():
    """A negative control on the exemption. It is the one place a route can
    be reachable without a session, so it should be small enough to read."""
    assert web.OPEN_ENDPOINTS == {"static", "log_in", "log_out", "health"}


def test_the_health_check_says_nothing_about_the_data(tmp_path, monkeypatch,
                                                      hashed):
    client = locked_app(tmp_path, monkeypatch, hashed).test_client()
    body = client.get("/health").get_json()
    assert body == {"ok": True}


# ======================================================= the open redirect

@pytest.mark.parametrize("target,expected", [
    ("/api/overview", "/api/overview"),
    ("//evil.example", "/"),
    ("https://evil.example", "/"),
    ("http://evil.example", "/"),
    ("evil.example", "/"),
    ("", "/"),
    (None, "/"),
])
def test_it_will_not_send_you_off_the_site_after_signing_in(target, expected):
    """`//evil.example` is the one that catches people: it looks like a path
    and is protocol-relative."""
    assert web._safe_next(target) == expected


def test_the_redirect_is_honoured_for_a_real_path(tmp_path, monkeypatch,
                                                  hashed):
    client = locked_app(tmp_path, monkeypatch, hashed).test_client()
    client.get("/login?next=/api/overview")
    reply = sign_in(client, next="/api/overview")
    assert reply.headers["Location"] == "/api/overview"


# ================================================================ headers

def test_the_hardening_headers_are_on_every_response(tmp_path):
    application = web.create_app(str(tmp_path), host="127.0.0.1")
    application.config["TESTING"] = True
    application.test_client_class = conftest.CsrfClient
    reply = application.test_client().get("/")

    assert reply.headers["X-Content-Type-Options"] == "nosniff"
    assert reply.headers["X-Frame-Options"] == "DENY"
    assert reply.headers["Referrer-Policy"] == "no-referrer"

    policy = reply.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy
    assert "'unsafe-inline'" not in policy.split("script-src")[1].split(";")[0], (
        "inline script must stay forbidden -- the page has none")
    assert "frame-ancestors 'none'" in policy


def test_https_is_only_promised_when_there_is_https(tmp_path, monkeypatch,
                                                    hashed):
    """HSTS on a loopback run would make the app unreachable in a browser
    that believed it, because there is no certificate."""
    local = web.create_app(str(tmp_path), host="127.0.0.1")
    local.config["TESTING"] = True
    local.test_client_class = conftest.CsrfClient
    assert "Strict-Transport-Security" not in local.test_client().get("/").headers

    public = locked_app(tmp_path, monkeypatch, hashed, host="0.0.0.0")
    reply = public.test_client().get("/login")
    assert "max-age=" in reply.headers["Strict-Transport-Security"]


def test_the_cookie_is_locked_down_as_far_as_the_transport_allows(
        tmp_path, monkeypatch, hashed):
    local = web.create_app(str(tmp_path), host="127.0.0.1")
    assert local.config["SESSION_COOKIE_HTTPONLY"] is True
    assert local.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert local.config["SESSION_COOKIE_SECURE"] is False, (
        "Secure on loopback would stop the cookie being sent at all")

    public = locked_app(tmp_path, monkeypatch, hashed, host="0.0.0.0")
    assert public.config["SESSION_COOKIE_SECURE"] is True


def test_the_session_expires(tmp_path):
    application = web.create_app(str(tmp_path), host="127.0.0.1")
    assert application.config["PERMANENT_SESSION_LIFETIME"].days == \
        auth.SESSION_DAYS


def test_the_login_page_asks_not_to_be_indexed(tmp_path, monkeypatch, hashed):
    client = locked_app(tmp_path, monkeypatch, hashed).test_client()
    assert "noindex" in client.get("/login").get_data(as_text=True)


def test_a_fresh_session_is_given_a_token(tmp_path):
    """The mint-on-first-use path. A browser arriving with no cookie has to
    leave with a token, or the first thing it tries to save is a 403."""
    application = web.create_app(str(tmp_path), host="127.0.0.1")
    application.config["TESTING"] = True
    application.test_client_class = conftest.CsrfClient
    client = application.test_client()

    page = client.get("/", no_csrf=True).get_data(as_text=True)
    token = page.split('name="csrf-token" content="')[1].split('"')[0]
    assert len(token) > 20

    # And it is the one the session will accept.
    reply = client.post("/api/transaction",
                        json={"date": "2026-09-08", "description": "REAL",
                              "amount": "1.00"},
                        headers={"X-CSRF-Token": token}, no_csrf=True)
    assert reply.status_code == 201


# =========================================================== the deployment

def test_the_wsgi_entry_point_refuses_without_a_password(monkeypatch):
    """The one that matters most in a deployment: gunicorn imports this
    module, so a missing password stops the process rather than serving an
    unprotected ledger."""
    import importlib
    monkeypatch.delenv("WALLET_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("HOST", "0.0.0.0")
    sys.modules.pop("wsgi", None)
    with pytest.raises(auth.Unsafe):
        importlib.import_module("wsgi")
    sys.modules.pop("wsgi", None)


def test_the_wsgi_entry_point_builds_an_app_when_configured(monkeypatch,
                                                            tmp_path, hashed):
    import importlib
    monkeypatch.setenv("WALLET_DATA", str(tmp_path))
    monkeypatch.setenv("WALLET_PASSWORD_HASH", hashed)
    monkeypatch.setenv("SECRET_KEY", STRONG_KEY)
    monkeypatch.setenv("HOST", "0.0.0.0")
    sys.modules.pop("wsgi", None)
    module = importlib.import_module("wsgi")
    try:
        assert module.application.config["WALLET_PUBLIC"] is True
        # gunicorn is configured either way in the wild.
        assert module.app is module.application
    finally:
        sys.modules.pop("wsgi", None)


def test_the_wsgi_entry_point_does_not_start_the_folder_watcher():
    """gunicorn forks several workers. Each one polling the same folder and
    importing the same file is the wrong number of threads writing to one
    SQLite file -- the right number is one.

    The file is read rather than imported: importing it builds an app, which
    is a side effect a test about the source should not have, and which fails
    outright without a password configured.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "wsgi.py"), encoding="utf-8") as handle:
        source = handle.read()
    code = source.split('"""', 2)[-1]        # past the module docstring
    assert "start_watching" not in code
    assert "app.run(" not in code
    assert "create_app" in code, "the reader found the wrong thing"
