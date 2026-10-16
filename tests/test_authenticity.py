"""Engine tests for web/authenticity.py.

The engine is deliberately free of FastAPI so the detection rules can be
asserted here: which targets are refused, how the evidence is weighted, and
which numbers the report is allowed to divide by.
"""

from __future__ import annotations

import random
import re

import pytest

from web import authenticity as A

# ---------------------------------------------------------------------------
# family / channel identity
# ---------------------------------------------------------------------------


def test_family_from_model_recognises_all_three_families():
    assert A.family_from_model("claude-sonnet-4-5") == "claude"
    assert A.family_from_model("gpt-5.5") == "gpt"
    assert A.family_from_model("o3-mini") == "gpt"
    assert A.family_from_model("gemini-2.5-pro") == "gemini"
    assert A.family_from_model("models/gemini-2.5-flash") == "gemini"


def test_family_from_model_rejects_unknown_names():
    assert A.family_from_model("llama-3-70b") is None
    assert A.family_from_model("") is None
    assert A.family_from_model("   ") is None


def test_channel_id_is_stable_and_case_insensitive():
    a = A.channel_id_for("https://api.example.com/v1")
    b = A.channel_id_for("  HTTPS://API.EXAMPLE.COM/v1/  ")
    assert a == b
    assert len(a) == 12
    assert re.fullmatch(r"[0-9a-f]{12}", a)


def test_channel_id_differs_per_base_url():
    assert A.channel_id_for("https://a.example.com") != A.channel_id_for("https://b.example.com")


# ---------------------------------------------------------------------------
# validate_target — the save-time rules the PRD calls hard requirements
# ---------------------------------------------------------------------------


def test_blank_source_is_refused():
    with pytest.raises(A.ConfigError) as err:
        A.validate_target("claude-sonnet-4-5", "")
    assert err.value.code == "missing_source"


def test_unknown_source_is_refused():
    with pytest.raises(A.ConfigError) as err:
        A.validate_target("claude-sonnet-4-5", "not_a_real_source")
    assert err.value.code == "unknown_source"


def test_cross_family_target_is_refused():
    """A Claude model cannot claim an OpenAI source — that is a config error,
    not something to silently "fix" by picking a different model."""
    with pytest.raises(A.ConfigError) as err:
        A.validate_target("claude-sonnet-4-5", "openai_direct")
    assert err.value.code == "cross_family"

    with pytest.raises(A.ConfigError) as err2:
        A.validate_target("gpt-5.5", "anthropic_direct")
    assert err2.value.code == "cross_family"


def test_unknown_model_family_is_refused():
    with pytest.raises(A.ConfigError) as err:
        A.validate_target("mystery-model-9000", "openai_direct")
    assert err.value.code == "unknown_model_family"


def test_blank_model_is_legal_at_save_time():
    target = A.validate_target("", "anthropic_direct", allow_blank_model=True)
    assert target["model"] == ""
    assert target["family"] == "claude"
    assert target["wire_family"] == "anthropic"
    assert target["source_label"]


def test_blank_model_without_a_model_list_is_refused():
    """At run time a blank model must be resolved; otherwise the run refuses
    instead of inventing a model name."""
    with pytest.raises(A.ConfigError) as err:
        A.validate_target("", "anthropic_direct")
    assert err.value.code == "missing_model"


def test_blank_model_auto_picks_a_same_family_model():
    available = ["gpt-5.5", "claude-haiku-4-5", "gemini-2.5-pro"]
    target = A.validate_target("", "anthropic_direct", available_models=available)
    assert target["model"] == "claude-haiku-4-5"
    assert target["wire_family"] == "anthropic"


def test_blank_model_with_no_same_family_model_is_refused():
    with pytest.raises(A.ConfigError) as err:
        A.validate_target("", "anthropic_direct", available_models=["gpt-5.5", "gemini-2.5-pro"])
    assert err.value.code == "no_same_family_model"


def test_model_name_length_is_capped():
    with pytest.raises(A.ConfigError) as err:
        A.validate_target("claude-" + "x" * 300, "anthropic_direct")
    assert err.value.code == "model_too_long"


def test_valid_target_reports_wire_family():
    target = A.validate_target("gemini-2.5-pro", "vertex_gemini")
    assert target["family"] == "gemini"
    assert target["wire_family"] == "gemini"
    assert target["claimed_source"] == "vertex_gemini"


def test_every_source_option_maps_to_a_known_family():
    for source in A.SOURCE_OPTIONS:
        assert source["family"] in A.FAMILY_TO_WIRE
        assert A.SOURCES_BY_ID[source["id"]] is source


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def test_redact_headers_drops_every_credential_header():
    redacted = A.redact_headers(
        {
            "Authorization": "Bearer sk-secret",
            "x-api-key": "sk-ant-secret",
            "X-Goog-Api-Key": "AIza-secret",
            "Content-Type": "application/json",
        }
    )
    joined = " ".join(redacted.keys()).lower()
    assert "authorization" not in joined
    assert "api-key" not in joined
    assert "goog" not in joined
    assert any(k.lower() == "content-type" for k in redacted)
    assert "secret" not in " ".join(redacted.values())


def test_truncate_marks_that_it_cut():
    long_text = "x" * 1000
    out = A.truncate(long_text, 100)
    assert out.startswith("x" * 100)
    assert out.endswith("[900 more chars]")
    assert A.truncate("short", 100) == "short"
    assert A.truncate(None, 10) == ""


# ---------------------------------------------------------------------------
# capability questions — must be generated, never a fixed bank
# ---------------------------------------------------------------------------


def test_questions_are_not_a_fixed_bank():
    prompts = set()
    for seed in range(40):
        for question in A.build_capability_questions(3, random.Random(seed)):
            prompts.add(question.prompt)
    assert len(prompts) > 20, "capability questions look static; a relay could special-case them"


def test_question_count_is_respected():
    assert len(A.build_capability_questions(1, random.Random(1))) == 1
    assert len(A.build_capability_questions(5, random.Random(1))) == 5
    assert len(A.build_capability_questions(0, random.Random(1))) == 1


def test_arithmetic_answers_are_actually_correct():
    rng = random.Random(7)
    checked = 0
    for seed in range(30):
        for question in A.build_capability_questions(8, random.Random(seed)):
            match = re.search(r"Compute \((\d+) \* (\d+)\) \+ (\d+)", question.prompt)
            if not match:
                continue
            a, b, c = (int(g) for g in match.groups())
            assert question.answer == str(a * b + c)
            checked += 1
    assert checked > 0, "no arithmetic question was generated to check"


def test_question_checking_tolerates_noise_but_not_wrong_answers():
    for seed in range(10):
        for question in A.build_capability_questions(4, random.Random(seed)):
            assert question.check(" " + question.answer + " ")
            assert not question.check("zzzz-not-an-answer")


# ---------------------------------------------------------------------------
# identity self-report
# ---------------------------------------------------------------------------


def test_identity_matches_same_family():
    assert A.identity_matches("model=claude-sonnet-4-5; company=Anthropic", "claude")
    assert A.identity_matches("I am Gemini, made by Google DeepMind", "gemini")


def test_identity_contradicts_a_different_vendor():
    conflict = A.identity_contradicts("model=gpt-4o; company=OpenAI", "claude")
    assert conflict == "gpt"


def test_identity_is_neutral_when_the_reply_says_nothing():
    """A refusal is not evidence of fraud — it must not be treated as a conflict."""
    assert A.identity_contradicts("", "claude") is None
    assert A.identity_contradicts("I cannot help with that.", "claude") is None


def test_identity_signal_statuses():
    passing = A.signal_identity("model=claude-sonnet-4-5; company=Anthropic", "claude", None)
    assert passing["status"] == "pass"
    conflicting = A.signal_identity("model=gpt-4o; company=OpenAI", "claude", None)
    assert conflicting["status"] == "fail"
    assert conflicting["self_reported_family"] == "gpt"
    unavailable = A.signal_identity("", "claude", None)
    assert unavailable["status"] == "unavailable"
    assert unavailable["score"] is None


def test_signed_thinking_never_changes_a_neutral_reply_into_a_conflict():
    signal = A.signal_identity("I cannot help with that.", "claude", True)
    assert signal["status"] == "warn"


# ---------------------------------------------------------------------------
# error fingerprint
# ---------------------------------------------------------------------------


def test_classify_anthropic_error_shape():
    shape = A.classify_error_shape(404, {"type": "error", "error": {"type": "not_found_error", "message": "model not found"}}, "")
    assert shape["shape"] == "anthropic_error"


def test_classify_openai_error_shape():
    """OpenAI also uses type values ending in _error, so the extra code/param
    fields — not the suffix — are what identify its envelope."""
    shape = A.classify_error_shape(
        404,
        {"error": {"message": "The model does not exist", "type": "invalid_request_error", "param": None, "code": "model_not_found"}},
        "",
    )
    assert shape["shape"] == "openai_error"


def test_classify_openai_error_shape_without_code_fields():
    shape = A.classify_error_shape(
        404, {"error": {"message": "model not found", "type": "invalid_request_error"}}, ""
    )
    assert shape["shape"] == "openai_error"


def test_classify_anthropic_and_openai_errors_are_told_apart():
    anthropic = A.classify_error_shape(
        404, {"type": "error", "error": {"type": "not_found_error", "message": "model: x"}}, ""
    )
    openai = A.classify_error_shape(
        404, {"error": {"message": "model: x", "type": "invalid_request_error", "code": "model_not_found"}}, ""
    )
    assert anthropic["shape"] == "anthropic_error"
    assert openai["shape"] == "openai_error"
    assert anthropic["shape"] != openai["shape"]


def test_classify_gemini_error_shape():
    shape = A.classify_error_shape(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "model not found"}}, "")
    assert shape["shape"] == "gemini_error"


def test_classify_non_json_replies():
    html = A.classify_error_shape(502, None, "<html><body>Bad gateway</body></html>")
    assert html["shape"] == "html_or_plaintext"
    plain = A.classify_error_shape(None, None, "connection reset by peer")
    assert plain["shape"] == "unparseable"


def test_expected_shape_per_wire_family():
    assert A._EXPECTED_ERROR_SHAPE == {
        "anthropic": "anthropic_error",
        "openai": "openai_error",
        "gemini": "gemini_error",
    }


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def test_unsupported_features_leave_the_denominator():
    """A skipped cache probe must not cost points — score stays at 100."""
    signals = [
        {"signal": "capability", "status": "pass", "score": 1.0},
        {"signal": "cache", "status": "skip", "score": None},
        {"signal": "thinking_signature", "status": "unavailable", "score": None},
    ]
    score, confidence, meta = A.score_signals(signals)
    assert score == 100.0
    assert meta["effective_weight"] == A.SIGNAL_WEIGHTS["capability"]
    assert 0 < confidence < 1


def test_all_signals_unavailable_scores_zero_with_no_confidence():
    score, confidence, meta = A.score_signals(
        [{"signal": name, "status": "skip", "score": None} for name in A.SIGNAL_WEIGHTS]
    )
    assert score == 0.0
    assert confidence == 0.0
    assert meta["effective_weight"] == 0


def test_partial_credit_lowers_the_score():
    signals = [
        {"signal": "capability", "status": "pass", "score": 1.0},
        {"signal": "identity", "status": "warn", "score": 0.5},
    ]
    score, _, _ = A.score_signals(signals)
    assert 80 < score < 100


def test_unknown_signal_names_are_ignored():
    score, _, _ = A.score_signals([{"signal": "not_a_signal", "status": "fail", "score": 0.0}])
    assert score == 0.0


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------


def test_transport_error_outranks_a_perfect_score():
    """A dropped socket is an error, not a forged relay."""
    assert A.decide_verdict(100.0, [], "connect timeout", None) == "error"


def test_identity_conflict_caps_an_otherwise_authentic_score():
    assert A.decide_verdict(95.0, [], None, "gpt") == "suspect"
    assert A.decide_verdict(20.0, [], None, "gpt") == "fail"


def test_thresholds_split_the_three_verdicts():
    assert A.decide_verdict(80.0, [], None, None) == "authentic"
    assert A.decide_verdict(79.9, [], None, None) == "suspect"
    assert A.decide_verdict(50.0, [], None, None) == "suspect"
    assert A.decide_verdict(49.9, [], None, None) == "fail"


def test_summary_mentions_verdict_model_and_source():
    text = A.summarize("authentic", "claude-sonnet-4-5", "Claude 官方直连", [], 1.0)
    assert A.VERDICT_LABELS["authentic"] in text
    assert "claude-sonnet-4-5" in text
    assert "Claude 官方直连" in text
    assert "100%" in text


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def _record(family="claude", verdict="authentic", **overrides):
    record = {
        "family": family,
        "verdict": verdict,
        "capability_accuracy": 1.0,
        "latency_ms": 1200,
        "time": "2026-05-05T00:00:00+08:00",
        "model": "claude-sonnet-4-5",
        "source": "anthropic_direct",
    }
    record.update(overrides)
    return record


def test_error_records_never_enter_a_denominator():
    records = [
        _record(verdict="authentic"),
        _record(verdict="fail"),
        _record(verdict="error"),
        _record(verdict="error"),
    ]
    report = A.aggregate_report(records)
    bucket = report["by_family"][0]
    assert bucket["total"] == 4
    assert bucket["error"] == 2
    assert bucket["completed"] == 2
    # 1 authentic out of 2 completed — errors are excluded from the denominator
    assert bucket["authentic_rate"] == 0.5
    assert bucket["suspect_rate"] == 0.0


def test_totals_keep_the_raw_request_count_alongside_the_completed_count():
    report = A.aggregate_report([_record(verdict="authentic"), _record(verdict="error")])
    totals = report["totals"]
    assert totals["total"] == 2
    assert totals["completed"] == 1
    assert totals["authentic_rate"] == 1.0


def test_families_are_reported_separately():
    records = [
        _record(family="claude", verdict="authentic"),
        _record(family="gpt", verdict="fail"),
        _record(family="gemini", verdict="suspect"),
    ]
    report = A.aggregate_report(records)
    families = {b["family"]: b for b in report["by_family"]}
    assert set(families) == {"claude", "gpt", "gemini"}
    assert families["claude"]["authentic"] == 1
    assert families["gpt"]["fail"] == 1
    assert families["gemini"]["suspect"] == 1
    assert families["claude"]["family_label"] == "Claude"


def test_empty_report_is_renderable():
    report = A.aggregate_report([])
    assert report["by_family"] == []
    assert report["totals"]["completed"] == 0
    assert report["totals"]["authentic_rate"] is None


def test_csv_export_has_no_credential_column():
    text = A.export_csv([_record(record_id="r1")])
    header = text.splitlines()[0]
    for column in A.CSV_COLUMNS:
        assert column in header
    assert "api_key" not in header
    assert "authorization" not in header.lower()


def test_csv_export_ignores_unknown_record_fields():
    text = A.export_csv([_record(record_id="r1", api_key="sk-should-not-appear")])
    assert "sk-should-not-appear" not in text


# ---------------------------------------------------------------------------
# record persistence
# ---------------------------------------------------------------------------


def test_saved_record_never_contains_an_api_key():
    record_id = A.save_record(_record(api_key="sk-ant-super-secret"))
    stored = A.list_records(hours=None, limit=50)
    assert any(r["record_id"] == record_id for r in stored)
    dumped = repr(stored)
    assert "sk-ant-super-secret" not in dumped


def test_list_records_respects_the_limit():
    for index in range(3):
        A.save_record(_record(model="claude-sonnet-4-5-" + str(index)))
    assert len(A.list_records(hours=None, limit=2)) == 2
