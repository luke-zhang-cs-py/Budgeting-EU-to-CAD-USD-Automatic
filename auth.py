"""
auth.py
-------
A password, a session, and a CSRF token — so this can be reached from
somewhere other than the machine it runs on.

The property everything here is arranged around: **the app refuses to start
reachable without a password.** Not "warns", not "defaults to something" —
`guard()` raises and the process exits. Every other safety measure in a web
app is a mitigation; this one is the difference between a private ledger and
a public one, and it is the only mistake here that cannot be walked back.

So the rule is mechanical rather than a matter of remembering:

    loopback, no password   -> runs, no login. What it has always done.
    loopback, password set  -> runs, asks for the password.
    anything else, no hash  -> refuses to start, and says what to set.

There is one account, because there is one person's spending in the file.
That removes user tables, registration, password reset and email entirely —
all of which are attack surface, and none of which a single-user ledger has
any use for.

Threats this actually addresses, and how:

- **Guessing the password.** scrypt via werkzeug (not a bare hash), plus a
  backoff that refuses attempts for a growing window rather than sleeping,
  because sleeping ties up a worker and is itself a denial of service.
- **A forged request from another site.** A token in the session that must
  come back in a header. 19 of this app's 34 routes change state, and three
  of them take multipart uploads, which a cross-origin HTML form can send.
  SameSite=Lax helps and is set, but it is a second lock, not the lock.
- **A stolen cookie.** HttpOnly so script cannot read it, Secure so it never
  crosses plain HTTP, and a lifetime so an abandoned session expires.

What it does not address, and cannot: anyone who can read the disk can read
the database. Hosting this means trusting the host with the file.
"""
import datetime as dt
import getpass
import hmac
import os
import secrets
import threading
import time

from flask import request, session
from werkzeug.security import check_password_hash, generate_password_hash

# The names to set. Kept together so the error messages and the documentation
# cannot drift from what is actually read.
PASSWORD_VAR = "WALLET_PASSWORD_HASH"
SECRET_VAR = "SECRET_KEY"

# Addresses that mean "only this machine". Anything else is reachable by
# somebody else and triggers the requirement.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

# How long a session lasts without activity. A ledger is not a chat app;
# a fortnight is convenient and still expires an abandoned laptop.
SESSION_DAYS = 14

# Guessing backoff. Three attempts free -- fat fingers happen -- then a
# refusal window that doubles, capped. Refused, not slept: holding the
# request open would let an attacker exhaust the worker pool for free.
FREE_ATTEMPTS = 3
FIRST_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 900

_lock = threading.Lock()
_failures = {"count": 0, "until": 0.0}


class Unsafe(RuntimeError):
    """The app was asked to be reachable without the means to protect it.

    Raised at startup, deliberately, rather than logged. A warning printed
    into a log nobody is reading is how a financial ledger ends up on the
    open internet with no password on it.
    """


# ------------------------------------------------------------- the password

def hash_password(plain):
    """A storable hash of a password. scrypt, by way of werkzeug.

    Not a bare sha256: a fast hash makes an offline attack on a leaked value
    cheap, and the whole point of storing a hash rather than the password is
    to make that expensive.
    """
    if not plain or not str(plain).strip():
        raise ValueError("a password cannot be blank")
    return generate_password_hash(str(plain), method="scrypt")


def password_matches(plain, stored):
    """Whether `plain` is the password behind `stored`. Constant-time.

    False for a missing or malformed hash rather than an exception, so a
    misconfigured deployment refuses logins instead of returning a 500 that
    tells an attacker the difference.
    """
    if not stored or not plain:
        return False
    try:
        return check_password_hash(stored, str(plain))
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------ the guard

def is_public(host):
    """Whether binding to `host` makes this reachable by someone else."""
    return str(host).strip() not in LOOPBACK


def guard(host, password_hash=None, secret_key=None):
    """Check the configuration is safe for `host`. Raises Unsafe if not.

    Returns the secret key to use, which for a loopback run with none set is
    a fresh random one -- sessions then do not survive a restart, which is a
    fair trade locally and is never the case in a deployment, where a missing
    key is refused outright.
    """
    password_hash = password_hash if password_hash is not None else os.environ.get(PASSWORD_VAR)
    secret_key = secret_key if secret_key is not None else os.environ.get(SECRET_VAR)

    if not is_public(host):
        return secret_key or secrets.token_hex(32)

    if not password_hash:
        raise Unsafe(
            f"refusing to bind {host} with no password.\n"
            f"This app holds your spending history and would be readable by "
            f"anyone who can reach that address.\n"
            f"Set {PASSWORD_VAR} to a hash from:  python -m auth\n"
            f"or bind 127.0.0.1 instead.")

    if not secret_key or len(str(secret_key)) < 32:
        raise Unsafe(
            f"refusing to bind {host} without a strong {SECRET_VAR}.\n"
            f"It signs the session cookie; a guessable one lets anybody mint "
            f"a logged-in session.\n"
            f"Set at least 32 characters, e.g. from:  "
            f"python -c \"import secrets; print(secrets.token_hex(32))\"")

    return str(secret_key)


def configure(app, host):
    """Apply the session settings for `host`, having checked it is safe."""
    app.config["SECRET_KEY"] = guard(host)
    app.config["WALLET_PASSWORD_HASH"] = os.environ.get(PASSWORD_VAR) or ""
    app.config["WALLET_PUBLIC"] = is_public(host)

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        # Lax rather than Strict: Strict would drop the cookie when arriving
        # from a link, so the app would look logged out every time it is
        # opened from a bookmark in another tab. Lax still blocks the
        # cross-site POST, which is the case that matters.
        SESSION_COOKIE_SAMESITE="Lax",
        # Only over HTTPS once this is reachable. Not set on loopback, where
        # there is no certificate and the cookie would simply never be sent.
        SESSION_COOKIE_SECURE=is_public(host),
        # A timedelta rather than a count of seconds: Flask accepts both
        # and stores what it is given, so an int leaves config holding a
        # bare number that reads as anything.
        PERMANENT_SESSION_LIFETIME=dt.timedelta(days=SESSION_DAYS),
    )
    return app


def required(app):
    """Whether this instance asks for a password at all."""
    return bool(app.config.get("WALLET_PASSWORD_HASH"))


# ------------------------------------------------------------- the session

def logged_in(app):
    """Whether this request carries a valid session.

    True when no password is configured, because that is the loopback case
    where there is nothing to log in to -- checked against the app's config
    rather than an environment variable, so a test can hold both shapes at
    once.
    """
    if not required(app):
        return True
    return session.get("in") is True


def log_in():
    session.clear()
    session["in"] = True
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True


def log_out():
    session.clear()


# ---------------------------------------------------------------- backoff

def blocked_for(now=None):
    """Seconds until another attempt is allowed. 0 when one is."""
    now = now if now is not None else time.monotonic()
    with _lock:
        return max(0, int(round(_failures["until"] - now)))


def note_failure(now=None):
    """Record a wrong password and extend the refusal window."""
    now = now if now is not None else time.monotonic()
    with _lock:
        _failures["count"] += 1
        over = _failures["count"] - FREE_ATTEMPTS
        if over > 0:
            wait = min(MAX_BACKOFF_SECONDS,
                       FIRST_BACKOFF_SECONDS * (2 ** (over - 1)))
            _failures["until"] = now + wait
        return _failures["count"]


def note_success():
    with _lock:
        _failures["count"] = 0
        _failures["until"] = 0.0


def reset_failures():
    """For tests, and for a process that has just started."""
    note_success()


# ------------------------------------------------------------------- CSRF

def csrf_token():
    """The token for this session, minted on first use.

    Also issued to a session that is not logged in, because the login form
    itself is a state-changing POST and a forged one is worth refusing even
    though the damage is small.
    """
    if not session.get("csrf"):
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_ok(req=None):
    """Whether this request carries the session's token.

    Read from a header first and a form field second. The header is what the
    page sends; the field exists because a plain HTML form cannot set one,
    and refusing those outright would rule out ever having one.
    """
    req = req or request
    expected = session.get("csrf")
    if not expected:
        return False
    sent = (req.headers.get("X-CSRF-Token")
            or (req.form.get("csrf_token") if req.form else None)
            or "")
    return hmac.compare_digest(str(expected), str(sent))


# ------------------------------------------------------------------ headers

def harden(response, public):
    """Security headers, applied to every response.

    Each is here for a reason rather than because a checklist named it:

    - nosniff, because a stored screenshot is served back by this app and a
      browser guessing it is HTML would be an XSS hole in the receipts route.
    - DENY framing, because none of this belongs in someone else's page.
    - no-referrer, so a URL carrying a category or a month does not leak to
      whatever a link points at.
    - a CSP with `script-src 'self'`, which is only possible because the page
      has no inline script at all. `style-src` has to allow inline: the charts
      set a width on a bar, and the alternative is a stylesheet with a class
      per percentage.
    - HSTS only when public, because promising HTTPS on a loopback run that
      has none would make the app unreachable in a browser that believed it.
    """
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data:",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
    ]))
    if public:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


if __name__ == "__main__":                       # pragma: no cover
    # A password is typed, never passed as an argument: a command line ends up
    # in shell history and in the process list.
    first = getpass.getpass("Password for the wallet: ")
    if first != getpass.getpass("Again: "):
        raise SystemExit("they did not match")
    print()
    print(f"{PASSWORD_VAR}={hash_password(first)}")
    print()
    print("Set that in the environment where the app runs. The password "
          "itself is not stored anywhere, so keep it in a password manager.")
