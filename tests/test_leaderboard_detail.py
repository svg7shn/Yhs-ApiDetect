"""Per-domain leaderboard tests — aggregate_one() + is_valid_domain()."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from web import leaderboard


@pytest.fixture
def fake_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stage 4 reports across 2 domains, multiple protocols."""
    proto_dir = tmp_path / "anthropic"
    proto_dir.mkdir()
    openai_dir = tmp_path / "openai"
    openai_dir.mkdir()

    reports = [
        ("aaa1.json", proto_dir, {
            "base_url": "https://relay-a.example.com/v1",
            "protocol": "anthropic",
            "target_model": "claude-haiku-4-5",
            "total_score": 88.0,
            "verdict": "passed",
            "timestamp": "2026-04-15T10:00:00Z",
            "results": [
                {"name": "thinking_signature", "status": "pass"},
                {"name": "pdf", "status": "pass"},
            ],
        }),
        ("aaa2.json", proto_dir, {
            "base_url": "https://relay-a.example.com",
            "protocol": "anthropic",
            "target_model": "claude-opus-4-7",
            "total_score": 72.0,
            "verdict": "passed",
            "timestamp": "2026-04-20T10:00:00Z",
            "results": [{"name": "pdf", "status": "fail"}],
        }),
        ("bbb1.json", openai_dir, {
            "base_url": "https://relay-a.example.com/v1",
            "protocol": "openai",
            "target_model": "gpt-4o",
            "total_score": 60.0,
            "verdict": "marginal",
            "timestamp": "2026-04-25T10:00:00Z",
            "results": [
                {"name": "token_billing", "status": "fail"},
                {"name": "function_calling", "status": "fail"},
            ],
        }),
        ("ccc1.json", proto_dir, {
            "base_url": "https://relay-b.example.com",
            "protocol": "anthropic",
            "target_model": "claude-haiku-4-5",
            "total_score": 95.0,
            "verdict": "passed",
            "timestamp": "2026-04-26T10:00:00Z",
            "results": [{"name": "thinking_signature", "status": "pass"}],
        }),
    ]
    for name, d, body in reports:
        (d / name).write_text(json.dumps(body), encoding="utf-8")

    monkeypatch.setattr(leaderboard, "REPORT_DIRS", [proto_dir, openai_dir])
    return tmp_path


def test_is_valid_domain_accepts_real_hosts():
    assert leaderboard.is_valid_domain("api.example.com")
    assert leaderboard.is_valid_domain("relay-1.example.co.uk")
    assert leaderboard.is_valid_domain("xn--example-9oa.com")  # punycode


def test_is_valid_domain_rejects_garbage():
    bad = [
        "",                       # empty
        "no-dot",                 # no TLD separator
        "..bad",                  # leading dot
        "bad..",                  # trailing dot
        "-bad.com",               # leading hyphen
        "bad.com-",               # trailing hyphen
        "evil.com/etc/passwd",    # path traversal
        "evil.com?x=1",           # query string
        "Evil.COM",               # uppercase (we always normalise to lower)
        "evil com.com",           # space
        "evil\u200bcom.com",      # zero-width
        "a" * 254 + ".com",       # too long
    ]
    for d in bad:
        assert not leaderboard.is_valid_domain(d), d


def test_aggregate_one_returns_history_for_domain(fake_reports: Path):
    result = leaderboard.aggregate_one("relay-a.example.com")
    assert result is not None
    relay, history = result

    assert relay.domain == "relay-a.example.com"
    assert relay.total_count == 3
    assert set(relay.by_protocol.keys()) == {"anthropic", "openai"}
    assert relay.by_protocol["anthropic"].count == 2
    assert relay.by_protocol["openai"].count == 1

    assert len(history) == 3
    # Newest first.
    dates = [j.timestamp for j in history if j.timestamp]
    assert dates == sorted(dates, reverse=True)
    assert history[0].job_id == "bbb1"


def test_aggregate_one_unknown_domain_returns_none(fake_reports: Path):
    assert leaderboard.aggregate_one("not-a-relay.example.org") is None


def test_aggregate_one_invalid_domain_returns_none(fake_reports: Path):
    assert leaderboard.aggregate_one("../etc/passwd") is None
    assert leaderboard.aggregate_one("") is None


def test_aggregate_one_failed_detectors_aggregated(fake_reports: Path):
    result = leaderboard.aggregate_one("relay-a.example.com")
    assert result is not None
    relay, _ = result
    openai_failed = relay.by_protocol["openai"].failed_detectors
    assert openai_failed["token_billing"] == 1
    assert openai_failed["function_calling"] == 1


def test_all_domains_lists_each_unique_host(fake_reports: Path):
    domains = set(leaderboard.all_domains())
    assert "relay-a.example.com" in domains
    assert "relay-b.example.com" in domains
    assert len(domains) == 2
