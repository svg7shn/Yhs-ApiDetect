"""Repair malformed ``NO_PROXY`` entries before httpx reads them.

httpx builds its proxy routing table from the environment when a client is
*constructed* (`httpx._utils.get_environment_proxies`). Every ``no_proxy``
entry becomes a `URLPattern` key, and the pattern httpx derives from a
**bracketed** IPv6 literal is not parseable::

    NO_PROXY=[::1]   ->   mount key "all://*[::1]"   ->   URLPattern(...)
                     ->   httpx.InvalidURL: Invalid port: ':1]'

The failure happens inside `httpx.AsyncClient(...)`, before a single request is
sent, so no application-level retry can recover from it: the caller sees a bare
"InvalidURL: Invalid port" instead of an HTTP result. Some proxy tools write the
bracketed form (e.g. `no_proxy=...,[::1]`), which is why one machine can fail
while a clean server works fine.

``sanitize_no_proxy`` drops only the entries httpx cannot parse. A bare `::1`
is kept on purpose -- httpx wraps it into `all://[::1]`, which does parse -- and
`http_proxy` / `https_proxy` are never touched, so users who must reach the
internet through a proxy keep that behaviour.
"""

from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping

__all__ = [
    "MALFORMED_MARKERS",
    "NO_PROXY_KEYS",
    "repair_proxy_environment",
    "sanitize_no_proxy",
]

#: Characters whose presence makes an httpx ``no_proxy`` pattern unparseable.
MALFORMED_MARKERS = ("[", "]")

#: Both spellings are valid on POSIX; Windows' ``os.environ`` folds them into
#: a single upper-cased key, so the second lookup resolves to the same slot.
NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")

_LOGGER = logging.getLogger("Yhs-ApiDetect")


def sanitize_no_proxy(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Remove NO_PROXY entries that httpx cannot turn into a URL pattern.

    Mutates *environ* in place (defaults to `os.environ`, which is what httpx
    reads) and returns the removed entries in their original order. A key whose
    remaining value would be empty is deleted rather than set to "".

    Idempotent: a second call returns an empty list.
    """

    if environ is None:
        environ = os.environ

    removed: list[str] = []
    handled_values: set[str] = set()

    for key in NO_PROXY_KEYS:
        if key not in environ:
            continue
        raw = environ.get(key) or ""
        if not isinstance(raw, str):
            continue
        # On Windows os.environ maps both spellings onto one variable; skip the
        # second pass instead of reporting the same entry twice.
        if raw in handled_values:
            continue
        handled_values.add(raw)

        kept: list[str] = []
        dropped: list[str] = []
        for chunk in raw.split(","):
            entry = chunk.strip()
            if not entry:
                continue
            if any(marker in entry for marker in MALFORMED_MARKERS):
                dropped.append(entry)
            else:
                kept.append(entry)

        if not dropped:
            continue

        removed.extend(dropped)
        if kept:
            environ[key] = ",".join(kept)
        else:
            environ.pop(key, None)

    return removed


def repair_proxy_environment(logger: logging.Logger | None = None) -> list[str]:
    """Sanitize the live process environment, logging what was dropped.

    Called automatically when ``relay_detector`` is imported and by the web
    entrypoint / test bootstrap, so the repair happens before the first httpx
    client is built. Returns the removed entries (empty when nothing was wrong).
    """

    removed = sanitize_no_proxy()
    if removed:
        (logger or _LOGGER).warning(
            "ignoring unparseable NO_PROXY entr%s %s: httpx cannot build a client "
            "for a bracketed IPv6 literal (InvalidURL: Invalid port)",
            "y" if len(removed) == 1 else "ies",
            ", ".join(removed),
        )
    return removed
