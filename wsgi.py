"""
wsgi.py
-------
The entry point for a real server. `gunicorn wsgi:application`.

Separate from app.py because the two starts differ in ways that matter.
app.py's `__main__` block runs Werkzeug's development server, fetches the
rate history if it is missing, and starts the folder watcher in a thread.
None of that belongs in a deployment:

- **The dev server is not for this.** It says so itself on startup. It is
  single-threaded by default, so one slow rate fetch blocks every other
  request, and it has no request limits of any kind.
- **The watcher must not run per worker.** gunicorn forks several; each one
  would poll the same folder and try to import the same file. The duplicate
  rule in the database would catch it, but the right number of threads
  writing to one SQLite file is one.
- **Fetching on boot is wrong here.** A platform health check has a timeout,
  and downloading 640 KB from the ECB inside the first request is a good way
  to fail it. The rate cache is seeded by a running instance, or shipped on
  the volume.

The host comes from HOST, and auth.guard raises if that is reachable without
a password -- which happens here, at import, so a misconfigured deployment
fails to start rather than serving an unprotected ledger.
"""
import os

import app as wallet

# Bound by the platform, so the app has to hear about it: on a PaaS the
# process is expected to listen on 0.0.0.0 and let the proxy terminate TLS.
# Defaulting to loopback here would be safe and also completely broken, so
# the variable is read and the guard is left to do its job.
HOST = os.environ.get("HOST", "0.0.0.0")

application = wallet.create_app(host=HOST)

# gunicorn looks for `app` in some configurations and `application` in others.
app = application
