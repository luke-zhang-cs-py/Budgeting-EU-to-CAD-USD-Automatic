"""
fetch.py
--------
One way to get bytes off the network.

Extracted from fxrates, which had it as a private `_download`. A second
module now needs it (fxlive, for the latest published rate), and the two
honest options were to reach into that private name or to write the fallback
twice. Both are the things the structural tests in this repository exist to
stop, so it moved here instead.

The two-attempt shape is not a preference. On this machine urllib cannot
verify the certificate chain, because a local inspecting CA sits in the
middle and is not strict-OpenSSL clean -- certifi does not help, since the
interception is the thing that fails. curl verifies differently and succeeds.
The transit project in this family needed the same fallback for its realtime
feed, which is how the cause is known rather than guessed.
"""
import subprocess
import urllib.error
import urllib.request

# Generous, because the ECB history is a 638 KB zip on a slow connection.
# Callers wanting a small JSON document pass something much shorter -- a page
# should not hang for two minutes waiting for an exchange rate it can do
# without.
DEFAULT_TIMEOUT = 120

# curl is given a little longer than urllib was, so a timeout in the first
# attempt does not leave the second no time to work.
CURL_GRACE = 20


def get(url, timeout=DEFAULT_TIMEOUT):
    """The bytes at `url`, or None.

    Never raises. Every caller here is fetching something it can either cache
    or do without, so a network failure is an answer -- "no rate right now" --
    rather than an exception that takes a page down with it.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        done = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout), url],
            capture_output=True, timeout=timeout + CURL_GRACE)
        return done.stdout if done.returncode == 0 and done.stdout else None
    except (OSError, subprocess.SubprocessError):
        return None
