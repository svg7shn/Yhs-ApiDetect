"""pytest setup that runs before test module imports.

web/jobs.py creates JOBS_DIR at import time. In production this is
/opt/Yhs-ApiDetect/web_data/jobs (root-owned), which a developer laptop or CI
container can't write to. We override the path with a per-session temp dir so
the import doesn't fail.

The temp dirs are anchored under .pytest-tmp/ inside the repo instead of the
system temp root: when TMPDIR/TEMP is unwritable (sandboxed or locked-down
Windows sessions) Python's tempfile falls back to the *current working
directory* and scatters tmpXXXXXX directories into the checkout.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from relay_detector.proxy_env import repair_proxy_environment

_TMP_ROOT = Path(__file__).resolve().parent.parent / ".pytest-tmp"
_TMP_ROOT.mkdir(exist_ok=True)


def _mkdtemp(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=prefix, dir=_TMP_ROOT)


def _mkstemp(prefix: str, suffix: str = "") -> str:
    return tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=_TMP_ROOT)[1]


os.environ.setdefault("Yhs-ApiDetect_JOBS_DIR", _mkdtemp("jobs-"))
os.environ.setdefault("Yhs-ApiDetect_WISHLIST_PATH", _mkstemp("wishlist-", ".txt"))
# web/authenticity.py resolves its record directory at import time (same reason
# as JOBS_DIR above), and the channel config file defaults to the same tree.
os.environ.setdefault("Yhs-ApiDetect_AUTHENTICITY_DIR", _mkdtemp("authenticity-"))
os.environ.setdefault("Yhs-ApiDetect_AUTHENTICITY_CONFIG", _mkstemp("auth-config-", ".json"))

# httpx reads NO_PROXY when a client is constructed, and a bracketed IPv6 entry
# makes that raise `InvalidURL: Invalid port` -- every respx-based test would
# then fail on the developer's machine instead of exercising the code under
# test. relay_detector repairs this on import for the CLI and web paths; pytest
# imports test modules before conftest's fixtures run, so do it explicitly here.
repair_proxy_environment()
