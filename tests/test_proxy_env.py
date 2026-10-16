"""Tests for relay_detector.proxy_env.

Context: httpx compiles every ``NO_PROXY`` entry into a URL pattern while a
client is being constructed. A *bracketed* IPv6 literal ([::1]) produces a
pattern that cannot be parsed, so construction raises
``httpx.InvalidURL: Invalid port: ':1]'`` and no request is ever sent -- the
caller only sees a confusing error. sanitize_no_proxy() drops exactly those
entries and leaves everything else (including a bare ::1, which httpx wraps
correctly) untouched.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import httpx
import pytest

from relay_detector.proxy_env import repair_proxy_environment, sanitize_no_proxy

POISONED = "192.168.1.161,.local,localhost,127.0.0.1,::1,[::1]"
CLEANED = "192.168.1.161,.local,localhost,127.0.0.1,::1"


def test_drops_bracketed_ipv6_and_keeps_everything_else():
    env = {"NO_PROXY": POISONED}
    assert sanitize_no_proxy(env) == ["[::1]"]
    assert env["NO_PROXY"] == CLEANED


def test_bare_ipv6_is_kept_because_httpx_can_parse_it():
    env = {"NO_PROXY": "127.0.0.1,::1,fe80::1"}
    assert sanitize_no_proxy(env) == []
    assert env["NO_PROXY"] == "127.0.0.1,::1,fe80::1"


def test_absent_or_clean_key_is_untouched():
    empty: dict[str, str] = {}
    assert sanitize_no_proxy(empty) == []
    clean = {"NO_PROXY": "localhost,127.0.0.1,example.com"}
    assert sanitize_no_proxy(clean) == []
    assert clean["NO_PROXY"] == "localhost,127.0.0.1,example.com"


def test_key_is_deleted_when_every_entry_is_malformed():
    env = {"NO_PROXY": "[::1]"}
    assert sanitize_no_proxy(env) == ["[::1]"]
    assert "NO_PROXY" not in env


def test_whitespace_and_empty_entries_are_normalised():
    env = {"NO_PROXY": "  , [::1] ,,localhost, "}
    assert sanitize_no_proxy(env) == ["[::1]"]
    assert env["NO_PROXY"] == "localhost"


def test_lowercase_spelling_is_handled():
    env = {"no_proxy": "localhost,[fe80::1]"}
    assert sanitize_no_proxy(env) == ["[fe80::1]"]
    assert env["no_proxy"] == "localhost"


def test_is_idempotent():
    env = {"NO_PROXY": POISONED}
    assert sanitize_no_proxy(env) == ["[::1]"]
    assert sanitize_no_proxy(env) == []


def test_scoped_forms_keep_their_meaning():
    """.example.com and a CIDR range are valid NO_PROXY syntax -> keep them."""
    env = {"NO_PROXY": ".example.com,192.168.0.0/16,[::1]"}
    assert sanitize_no_proxy(env) == ["[::1]"]
    assert env["NO_PROXY"] == ".example.com,192.168.0.0/16"


def test_repair_proxy_environment_rewrites_os_environ(monkeypatch):
    monkeypatch.setenv("NO_PROXY", POISONED)
    assert repair_proxy_environment() == ["[::1]"]
    assert os.environ["NO_PROXY"] == CLEANED
    assert repair_proxy_environment() == []


def test_repair_proxy_environment_logs_what_it_dropped(monkeypatch, caplog):
    monkeypatch.setenv("NO_PROXY", POISONED)
    with caplog.at_level(logging.WARNING, logger="Yhs-ApiDetect"):
        removed = repair_proxy_environment()
    assert removed == ["[::1]"]
    assert any("[::1]" in record.getMessage() for record in caplog.records)


def test_httpx_client_can_be_constructed_after_the_repair(monkeypatch):
    """End-to-end: reproduce the real failure, then show the fix clears it."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,[::1]")
    with pytest.raises(httpx.InvalidURL):
        httpx.Client()

    assert repair_proxy_environment() == ["[::1]"]

    with httpx.Client() as client:
        assert client is not None


def test_a_bracketed_ipv6_range_is_the_actual_poison(monkeypatch):
    """Guards the test above: the bracketed entry is what breaks the client."""
    monkeypatch.setenv("NO_PROXY", "[2001:db8::1]")
    with pytest.raises(httpx.InvalidURL) as excinfo:
        httpx.Client()
    assert "Invalid port" in str(excinfo.value)


def test_importing_the_package_repairs_a_poisoned_environment():
    """`import relay_detector` is the CLI/web repair hook -- prove it in a child
    process so this test does not depend on what else already imported it."""
    code = "import os, relay_detector; print(os.environ.get('NO_PROXY'))"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "NO_PROXY": POISONED},
        check=True,
    )
    assert proc.stdout.strip() == CLEANED
