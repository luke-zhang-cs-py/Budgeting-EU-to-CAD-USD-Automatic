"""
paths.py
--------
Where this app keeps its data. One decision, in one place.

`db.data_dir` and `fxrates.cache_path` each resolved this independently, and
each read WALLET_DATA for itself. Two copies of one decision is a shotgun
surgery waiting to happen, but the specific failure is worse than the
duplication: nothing forced the two to agree, so the database and the rate
cache could end up in different directories. The ledger would then be read
from one place and converted with rates from another, and neither would look
wrong.

The face-recognition project in this family had exactly this -- seven modules
that each resolved their own paths, and a `use()` that consequently moved
none of them.

Resolved per call, never captured at import, so a test can point the whole app
at a temporary directory and have every module follow.
"""
import os

ENV_VAR = "WALLET_DATA"
DEFAULT_DIRNAME = "data"


def data_dir(directory=None):
    """The directory holding the database and the rate cache.

    Order: an explicit argument, then WALLET_DATA, then `data/` beside the
    code. The explicit argument comes first so a caller that knows where it
    wants to work is never overridden by an environment variable it did not
    set.
    """
    if directory:
        return directory
    return os.environ.get(ENV_VAR) or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), DEFAULT_DIRNAME)
