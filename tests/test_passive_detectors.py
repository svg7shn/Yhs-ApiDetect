"""Unit tests for ProtocolDetector and MessageIDDetector (M2)."""

from __future__ import annotations

import httpx

from relay_detector.detectors.message_id import MessageIDDetector
from relay_detector.detectors.protocol import ProtocolDetector

# --- Fixtures --------------------------------------------------------------


def _clean_response(**overrides):
    base = {
        "id": "msg_01ABCDEFGHIJKLMNOPQRSTUV",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }
    base.update(overrides)
    return base


def _good_headers() -> httpx.Headers:
    return httpx.Headers(
        {
            "anthropic-request-id": "req_xyz",
            "content-type": "application/json",
        }
    )


# --- ProtocolDetector ------------------------------------------------------


def test_protocol_clean_response_scores_100():
    d = ProtocolDetector()
    d.observe({}, _clean_response(), _good_headers(), 100)
    r = d.finalize()
    assert r.status == "pass"
    assert r.score == 100.0
    assert r.details["issues"] == []


def test_protocol_clean_with_no_response_headers_still_passes():
    """We deliberately do not penalize missing anthropic-request-id —
    the official Anthropic API doesn't return it either (empirically verified).
    """
    d = ProtocolDetector()
    d.observe({}, _clean_response(), httpx.Headers({}), 100)
    r = d.finalize()
    assert r.score == 100.0
    assert r.details["issues"] == []


def test_protocol_uuid_id_is_not_a_protocol_issue():
    """UUID-form id is technically a non-empty string, so ProtocolDetector
    should not flag it. (MessageIDDetector handles prefix conventions.)"""
    d = ProtocolDetector()
    resp = _clean_response(id="0b68fbd0-91e5-4aa6-a715-e206d8daae1c")
    d.observe({}, resp, _good_headers(), 100)
    r = d.finalize()
    # No protocol-level issue from the UUID alone.
    assert r.score == 100.0


def test_protocol_invalid_stop_reason_caught():
    d = ProtocolDetector()
    d.observe({}, _clean_response(stop_reason="finished"), _good_headers(), 100)
    r = d.finalize()
    assert r.score < 100.0
    assert any(i.startswith("stop_reason_invalid") for i in r.details["issues"])


def test_protocol_unknown_content_block_type_caught():
    d = ProtocolDetector()
    resp = _clean_response(content=[{"type": "weird_block", "stuff": 1}])
    d.observe({}, resp, _good_headers(), 100)
    r = d.finalize()
    assert any(
        i.startswith("content_block_unknown_type") for i in r.details["issues"]
    )


def test_protocol_negative_input_tokens_caught():
    d = ProtocolDetector()
    resp = _clean_response(usage={"input_tokens": -1, "output_tokens": 1})
    d.observe({}, resp, _good_headers(), 100)
    r = d.finalize()
    assert "usage_input_tokens_invalid" in r.details["issues"]


def test_protocol_skip_when_no_observations():
    d = ProtocolDetector()
    r = d.finalize()
    assert r.status == "skip"


def test_protocol_repeated_issue_only_counted_once():
    d = ProtocolDetector()
    bad = _clean_response(stop_reason="finished")
    d.observe({}, bad, _good_headers(), 100)
    d.observe({}, bad, _good_headers(), 100)
    r = d.finalize()
    # same issue across 2 obs -> still one penalty
    assert r.details["issue_count"] == 1
    assert r.score == 90.0


# --- MessageIDDetector -----------------------------------------------------


def test_message_id_clean_response_scores_100():
    d = MessageIDDetector()
    d.observe({}, _clean_response(), _good_headers(), 100)
    r = d.finalize()
    assert r.status == "pass"
    assert r.score == 100.0
    assert r.details["violations"] == []


def test_message_id_uuid_id_is_caught():
    """The exact issue we saw on router.8864k.com: id is a UUID, not msg_*."""
    d = MessageIDDetector()
    resp = _clean_response(id="0b68fbd0-91e5-4aa6-a715-e206d8daae1c")
    d.observe({}, resp, _good_headers(), 100)
    r = d.finalize()
    assert "id_prefix_invalid" in r.details["violations"]
    assert "0b68fbd0" in r.details["samples"]["id_prefix_invalid"]
    # one base violation -> -25 -> 75
    assert r.score == 75.0


def test_message_id_non_claude_model_caught():
    d = MessageIDDetector()
    resp = _clean_response(model="gpt-4o-mini")
    d.observe({}, resp, _good_headers(), 100)
    r = d.finalize()
    assert "model_not_claude" in r.details["violations"]
    assert r.score == 75.0


def test_message_id_tool_use_prefix_violation():
    d = MessageIDDetector()
    resp = _clean_response(
        content=[{"type": "tool_use", "id": "tool-abc", "name": "x", "input": {}}]
    )
    d.observe({}, resp, _good_headers(), 100)
    r = d.finalize()
    assert "tool_use_id_prefix_invalid" in r.details["violations"]
    assert r.score == 75.0


def test_message_id_skip_when_no_observations():
    d = MessageIDDetector()
    r = d.finalize()
    assert r.status == "skip"


def test_message_id_violations_collapse_across_observations():
    d = MessageIDDetector()
    bad = _clean_response(id="0b68fbd0-91e5-4aa6-a715-e206d8daae1c")
    for _ in range(3):
        d.observe({}, bad, _good_headers(), 100)
    r = d.finalize()
    # same kind of violation seen 3 times still counts once
    assert r.score == 75.0
    assert r.details["observation_count"] == 3
