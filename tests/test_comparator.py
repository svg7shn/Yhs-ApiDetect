"""Comparator unit tests — verify each per-detector severity classification."""

from __future__ import annotations

from relay_detector.comparator import (
    Severity,
    _compare_one,
    compare,
)


def _result(name, score=100.0, status="pass", details=None):
    return {
        "name": name,
        "display_name": name,
        "status": status,
        "score": score,
        "weight": 5.0,
        "details": details or {},
        "duration_ms": 100,
        "error": None,
    }


# --- thinking_signature: most consequential ---------------------------------


def test_cmp_thinking_signature_block_missing_is_critical():
    b = _result("thinking_signature", 100.0, details={
        "thinking_block_seen": True,
        "signature_length": 600,
    })
    r = _result("thinking_signature", 0.0, status="fail", details={
        "thinking_block_seen": False,
        "signature_length": 0,
    })
    c = _compare_one("thinking_signature", b, r)
    assert c.severity == Severity.CRITICAL
    assert any("thinking 块" in f for f in c.findings)


def test_cmp_thinking_signature_short_signature_is_major():
    b = _result("thinking_signature", 100.0, details={
        "thinking_block_seen": True,
        "signature_length": 800,
    })
    r = _result("thinking_signature", 70.0, details={
        "thinking_block_seen": True,
        "signature_length": 100,  # < 30% of 800
    })
    c = _compare_one("thinking_signature", b, r)
    assert c.severity == Severity.MAJOR


def test_cmp_thinking_signature_aligned_is_ok():
    b = _result("thinking_signature", 100.0, details={
        "thinking_block_seen": True,
        "signature_length": 700,
    })
    r = _result("thinking_signature", 100.0, details={
        "thinking_block_seen": True,
        "signature_length": 720,
    })
    c = _compare_one("thinking_signature", b, r)
    assert c.severity == Severity.OK
    assert c.findings == []


# --- pdf -------------------------------------------------------------------


def test_cmp_pdf_empty_response_is_critical():
    b = _result("pdf", 100.0, details={"evaluation": "magic_found"})
    r = _result("pdf", 0.0, status="fail", details={"evaluation": "empty_response"})
    c = _compare_one("pdf", b, r)
    assert c.severity == Severity.CRITICAL
    assert any("PDF" in f for f in c.findings)


# --- structured_output -----------------------------------------------------


def test_cmp_structured_output_no_tool_use_block_is_critical():
    b = _result("structured_output", 100.0, details={
        "content_block_types": ["tool_use"],
    })
    r = _result("structured_output", 0.0, status="fail", details={
        "content_block_types": ["text"],
        "stop_reason": "end_turn",
    })
    c = _compare_one("structured_output", b, r)
    assert c.severity == Severity.CRITICAL


# --- consistency -----------------------------------------------------------


def test_cmp_consistency_model_mismatch_is_critical():
    b = _result("consistency", 100.0, details={"model_match": True, "stability_cv": 0.01})
    r = _result("consistency", 60.0, details={
        "request_model": "claude-haiku-4-5",
        "response_model": "claude-opus-4-5",
        "model_match": False,
        "stability_cv": 0.02,
    })
    c = _compare_one("consistency", b, r)
    assert c.severity == Severity.CRITICAL


def test_cmp_consistency_high_cv_is_major():
    b = _result("consistency", 100.0, details={"model_match": True, "stability_cv": 0.01})
    r = _result("consistency", 90.0, details={"model_match": True, "stability_cv": 0.5})
    c = _compare_one("consistency", b, r)
    assert c.severity == Severity.MAJOR
    assert any("CV=0.500" in f or "0.5" in f for f in c.findings)


def test_cmp_consistency_suspicious_cv_shows_seq():
    """relay CV in 'suspicious' (0.10-0.30) range — should surface value + seq."""
    b = _result("consistency", 100.0, details={
        "model_match": True, "stability_cv": 0.02,
        "output_tokens_seq": [50, 50, 51],
    })
    r = _result("consistency", 80.0, details={
        "model_match": True, "stability_cv": 0.13,
        "output_tokens_seq": [44, 57, 60],
    })
    c = _compare_one("consistency", b, r)
    assert c.severity == Severity.MINOR
    finding = " ".join(c.findings)
    assert "0.130" in finding or "0.13" in finding
    assert "[44, 57, 60]" in finding


def test_cmp_structured_output_wrong_id_prefix_shows_value():
    """tool_use present but id is 'tool_1' — should show actual value as MAJOR."""
    b = _result("structured_output", 100.0, details={
        "content_block_types": ["tool_use"],
        "sub_checks": {
            "has_tool_use_block": {"value": True, "pass": True},
            "id_prefix": {"value": "toolu_01ABC", "pass": True},
            "name": {"value": "get_weather", "pass": True},
        },
    })
    r = _result("structured_output", 80.0, details={
        "content_block_types": ["tool_use"],
        "sub_checks": {
            "has_tool_use_block": {"value": True, "pass": True},
            "id_prefix": {"value": "tool_1", "pass": False},
            "name": {"value": "get_weather", "pass": True},
        },
    })
    c = _compare_one("structured_output", b, r)
    assert c.severity == Severity.MAJOR
    assert any("tool_1" in f for f in c.findings)


def test_cmp_structured_output_no_tool_use_shows_text_response():
    """When tool_use absent, show the model's actual text reply."""
    b = _result("structured_output", 100.0, details={
        "content_block_types": ["tool_use"],
    })
    r = _result("structured_output", 0.0, status="fail", details={
        "content_block_types": ["text"],
        "stop_reason": "end_turn",
        "text_response": "I don't have real-time access to current weather data.",
    })
    c = _compare_one("structured_output", b, r)
    assert c.severity == Severity.CRITICAL
    finding = " ".join(c.findings)
    assert "real-time access" in finding


def test_cmp_integrity_input_tokens_shows_numerics():
    """integrity input_tokens fail — show ns/stream/diff numbers."""
    b = _result("integrity", 100.0, details={
        "sub_checks": {
            "input_tokens": {"ns": 27, "stream": 27, "diff": 0, "pass": True},
        },
    })
    r = _result("integrity", 80.0, details={
        "sub_checks": {
            "input_tokens": {"ns": 58, "stream": 30, "diff": 28, "tolerance": 11, "pass": False},
        },
    })
    c = _compare_one("integrity", b, r)
    assert c.severity == Severity.MINOR
    finding = " ".join(c.findings)
    assert "ns=58" in finding
    assert "stream=30" in finding
    assert "diff=28" in finding


def test_cmp_knowledge_shows_failed_answers():
    """Knowledge fail — surface the bad answer text, not just the qid."""
    b = _result("knowledge", 100.0, details={
        "passes": 5, "total": 5,
        "per_question": [{"id": f"q{i}", "passed": True, "answer": "ok"} for i in range(5)],
    })
    r = _result("knowledge", 80.0, details={
        "passes": 4, "total": 5,
        "per_question": [
            {"id": "q1", "passed": True, "answer": "Dario Amodei"},
            {"id": "q2", "passed": False, "answer": "I don't know"},
            {"id": "q3", "passed": True, "answer": "2023"},
            {"id": "q4", "passed": True, "answer": "San Francisco"},
            {"id": "q5", "passed": True, "answer": "Daniela Amodei"},
        ],
    })
    c = _compare_one("knowledge", b, r)
    assert c.severity == Severity.MINOR
    finding = " ".join(c.findings)
    assert "q2" in finding
    assert "I don't know" in finding


# --- identity --------------------------------------------------------------


def test_cmp_identity_competitor_keyword_is_critical():
    b = _result("identity", 100.0, details={
        "required_hits": ["claude", "anthropic"],
        "competitor_hits": [],
    })
    r = _result("identity", 30.0, details={
        "required_hits": ["claude"],
        "competitor_hits": ["openai"],
    })
    c = _compare_one("identity", b, r)
    assert c.severity == Severity.CRITICAL


def test_cmp_identity_brand_detection_amazon_q_is_critical():
    """Real-world case: relay says 'I'm Amazon Q built by AWS'."""
    b = _result("identity", 100.0, details={
        "required_hits": ["claude", "anthropic"],
        "competitor_hits": [],
        "detected_non_anthropic_brands": [],
    })
    r = _result("identity", 0.0, details={
        "required_hits": [],
        "competitor_hits": [],
        "detected_non_anthropic_brands": ["Amazon Q", "AWS"],
    })
    c = _compare_one("identity", b, r)
    assert c.severity == Severity.CRITICAL
    assert any("Amazon Q" in f for f in c.findings)
    assert any("AWS" in f for f in c.findings)


def test_cmp_identity_brand_only_new_brands_flagged():
    """If baseline already mentions AWS (e.g. answered 'I'm Claude on AWS'),
    relay mentioning AWS isn't itself a signal — only NEW brands count."""
    b = _result("identity", 100.0, details={
        "required_hits": ["claude", "anthropic"],
        "competitor_hits": [],
        "detected_non_anthropic_brands": ["AWS"],
    })
    r = _result("identity", 100.0, details={
        "required_hits": ["claude", "anthropic"],
        "competitor_hits": [],
        "detected_non_anthropic_brands": ["AWS"],
    })
    c = _compare_one("identity", b, r)
    assert c.severity == Severity.OK
    assert c.findings == []


# --- message_id ------------------------------------------------------------


def test_cmp_message_id_uuid_id_prefix_is_major():
    b = _result("message_id", 100.0, details={"violations": [], "samples": {}})
    r = _result("message_id", 75.0, details={
        "violations": ["id_prefix_invalid"],
        "samples": {"id_prefix_invalid": "'a-uuid-here'"},
    })
    c = _compare_one("message_id", b, r)
    assert c.severity == Severity.MAJOR


# --- protocol --------------------------------------------------------------


def test_cmp_protocol_new_issues_is_minor():
    b = _result("protocol", 100.0, details={"issues": []})
    r = _result("protocol", 80.0, details={
        "issues": ["header_missing:foo", "stop_reason_invalid"],
    })
    c = _compare_one("protocol", b, r)
    assert c.severity == Severity.MINOR


# --- skip / error handling -------------------------------------------------


def test_cmp_relay_skip_when_baseline_ran_is_major():
    b = _result("pdf", 100.0)
    r = _result("pdf", 0.0, status="skip", details={"skip_reason": "test"})
    c = _compare_one("pdf", b, r)
    assert c.severity == Severity.MAJOR


def test_cmp_relay_error_is_major():
    b = _result("structured_output", 100.0)
    r = {**_result("structured_output", 0.0, status="error"), "error": "HTTP 500: foo"}
    c = _compare_one("structured_output", b, r)
    assert c.severity == Severity.MAJOR


# --- top-level compare() ---------------------------------------------------


def test_compare_overall_summary_aggregates_severities():
    baseline = {
        "target_model": "claude-haiku-4-5",
        "mode": "full",
        "total_score": 100.0,
        "results": [
            _result("thinking_signature", 100.0, details={
                "thinking_block_seen": True, "signature_length": 700,
            }),
            _result("pdf", 100.0, details={"evaluation": "magic_found"}),
            _result("message_id", 100.0, details={"violations": []}),
        ],
    }
    relay = {
        "target_model": "claude-haiku-4-5",
        "mode": "full",
        "total_score": 35.0,
        "results": [
            _result("thinking_signature", 0.0, status="fail", details={
                "thinking_block_seen": False, "signature_length": 0,
            }),
            _result("pdf", 0.0, status="fail", details={"evaluation": "empty_response"}),
            _result("message_id", 75.0, details={
                "violations": ["id_prefix_invalid"],
                "samples": {"id_prefix_invalid": "'uuid'"},
            }),
        ],
    }
    cmp = compare(baseline, relay)
    assert cmp.overall_severity == Severity.CRITICAL
    # Verify each detector gets the right severity
    by_name = {d.name: d for d in cmp.detectors}
    assert by_name["thinking_signature"].severity == Severity.CRITICAL
    assert by_name["pdf"].severity == Severity.CRITICAL
    assert by_name["message_id"].severity == Severity.MAJOR
    # Summary should mention severities
    assert "严重" in cmp.summary or "critical" in cmp.summary.lower()


def test_compare_aligned_reports_overall_ok():
    baseline = {
        "target_model": "claude-haiku-4-5",
        "mode": "full",
        "total_score": 100.0,
        "results": [
            _result("thinking_signature", 100.0, details={
                "thinking_block_seen": True, "signature_length": 700,
            }),
        ],
    }
    relay = {
        "target_model": "claude-haiku-4-5",
        "mode": "full",
        "total_score": 100.0,
        "results": [
            _result("thinking_signature", 100.0, details={
                "thinking_block_seen": True, "signature_length": 720,
            }),
        ],
    }
    cmp = compare(baseline, relay)
    assert cmp.overall_severity == Severity.OK


def test_compare_model_mismatch_in_summary():
    baseline = {
        "target_model": "claude-haiku-4-5",
        "mode": "full",
        "total_score": 100.0,
        "results": [],
    }
    relay = {
        "target_model": "claude-opus-4-7",
        "mode": "full",
        "total_score": 100.0,
        "results": [],
    }
    cmp = compare(baseline, relay)
    assert "模型不一致" in cmp.summary
