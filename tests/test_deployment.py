"""The deployment configs, checked against the things they have to agree with.

`fly.toml` and `render.yaml` have never been deployed from this repository --
there is no platform account attached to it, and both files say so. That makes
them the least trustworthy code here: nothing has ever executed them.

What *can* be checked is that they agree with the Dockerfile and with the app,
which is where the mistakes in a config like this actually live. A port that
does not match the one the container listens on, a volume mounted somewhere
other than where the data is written, or a health check pointed at a route
that needs a session are each a deploy that comes up looking broken for a
reason the logs do not explain.

The health check is the one worth having most: `/health` is open because it is
named in `app.OPEN_ENDPOINTS`, and every other route needs a session. Rename
it and both platforms would start restarting a container that is working
perfectly.

YAML is not parsed. `render.yaml` is read with targeted patterns for the three
values that have to match something else, because adding a YAML parser to a
project that needs none, to check five lines, is a worse trade than a regex
that says what it is looking for.
"""
import io
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app as wallet   # noqa: E402


def read(name):
    with io.open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def dockerfile():
    return read("Dockerfile")


@pytest.fixture(scope="module")
def fly():
    """fly.toml, parsed.

    tomllib arrived in 3.11, and this project supports 3.10, so on the older
    interpreter this skips rather than pulling in a dependency for one file.
    CI runs both, so the check still happens on every push.
    """
    try:
        import tomllib
    except ImportError:                                   # pragma: no cover
        pytest.skip("tomllib needs Python 3.11; the 3.12 job covers this")
    with io.open(os.path.join(ROOT, "fly.toml"), "rb") as handle:
        return tomllib.load(handle)


def docker_env(dockerfile, name):
    found = re.search(r"^ENV %s=(\S+)" % name, dockerfile, re.MULTILINE)
    assert found, f"the Dockerfile does not set {name}"
    return found.group(1)


def test_the_container_and_fly_agree_on_the_port(dockerfile, fly):
    """Fly routes to internal_port. If the container listens elsewhere the
    deploy comes up and every request times out."""
    exposed = re.search(r"^EXPOSE (\d+)", dockerfile, re.MULTILINE)
    assert exposed, "the Dockerfile does not EXPOSE a port"

    assert fly["http_service"]["internal_port"] == int(exposed.group(1)), (
        "fly.toml routes to a different port than the container exposes")
    assert fly["env"]["PORT"] == docker_env(dockerfile, "PORT"), (
        "fly.toml and the Dockerfile disagree about PORT, and gunicorn binds "
        "the one in the environment")


def test_the_volume_is_mounted_where_the_data_is_written(dockerfile, fly):
    """The single most likely way to lose the data: WALLET_DATA pointing
    somewhere the volume is not."""
    data = docker_env(dockerfile, "WALLET_DATA")

    assert re.search(r'^VOLUME \["%s"\]' % re.escape(data), dockerfile,
                     re.MULTILINE), (
        f"the Dockerfile sets WALLET_DATA={data} but declares no volume there")

    assert fly["env"]["WALLET_DATA"] == data, (
        "fly.toml and the Dockerfile disagree about where the data lives")

    mounts = fly.get("mounts") or []
    assert mounts, "fly.toml declares no volume, so a deploy destroys the data"
    assert mounts[0]["destination"] == data, (
        f"the volume is mounted at {mounts[0]['destination']} but the app "
        f"writes to {data}")


def test_render_mounts_its_disk_where_the_data_is_written(dockerfile):
    render = read("render.yaml")
    data = docker_env(dockerfile, "WALLET_DATA")

    assert re.search(r"^\s*mountPath: %s\s*$" % re.escape(data), render,
                     re.MULTILINE), (
        f"render.yaml does not mount its disk at {data}")
    assert re.search(r"^\s*- key: WALLET_DATA\s*\n\s*value: %s\s*$"
                     % re.escape(data), render, re.MULTILINE), (
        "render.yaml does not set WALLET_DATA to the mount path")


def test_both_configs_ask_for_the_password_hash(dockerfile):
    """The app refuses to start on a public host without it, so a config that
    does not mention it describes a deploy that cannot boot."""
    render = read("render.yaml")
    assert "WALLET_PASSWORD_HASH" in render, (
        "render.yaml never mentions the variable the app requires")
    assert "sync: false" in render, (
        "the password hash must be marked sync: false rather than committed")

    # Fly takes it through `fly secrets set` rather than the toml, so the
    # walkthrough is where it has to appear.
    readme = read("README.md")
    assert "fly secrets set WALLET_PASSWORD_HASH" in readme, (
        "the Fly walkthrough does not set the password hash, and the deploy "
        "would fail at boot with Unsafe")


def test_the_health_check_points_at_a_route_that_needs_no_session(fly):
    """Both platforms restart a container whose health check fails, and every
    route here needs a session except the four in OPEN_ENDPOINTS. A health
    check on any other route would restart a working app for ever."""
    checks = fly["http_service"].get("checks") or []
    assert checks, "fly.toml has no health check"
    path = checks[0]["path"]

    render = read("render.yaml")
    assert re.search(r"^\s*healthCheckPath: %s\s*$" % re.escape(path), render,
                     re.MULTILINE), (
        "the two platforms check different paths")

    application = wallet.create_app(host="127.0.0.1")
    match = None
    for rule in application.url_map.iter_rules():
        if str(rule) == path:
            match = rule
    assert match, f"{path} is not a route this app serves"
    assert match.endpoint in wallet.OPEN_ENDPOINTS, (
        f"{path} is served by {match.endpoint}, which is not in "
        f"OPEN_ENDPOINTS — the health check would be redirected to the login "
        f"page and the platform would call the app unhealthy")


def test_one_instance_only(fly):
    """The ledger is a single SQLite file. Both configs pin one instance, and
    a second writer is where "database is locked" comes from."""
    assert fly["http_service"]["min_machines_running"] <= 1, (
        "fly.toml would keep more than one machine running")

    render = read("render.yaml")
    assert re.search(r"^\s*numInstances: 1\s*$", render, re.MULTILINE), (
        "render.yaml does not pin a single instance")


def test_the_configs_say_they_were_never_deployed():
    """They were not, and saying so is the difference between untested config
    and config somebody has reason to trust. If that stops being true, this
    is the reminder to say so instead."""
    for name in ("fly.toml", "render.yaml"):
        assert "deployed" in read(name), (
            f"{name} no longer says whether it has been deployed from here")
