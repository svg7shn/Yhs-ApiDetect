"""Tests for OpenAI official baseline collection."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from relay_detector.openai.baseline import (
    build_openai_baseline_probes,
    collect_openai_official_baseline,
    extract_openai_features,
    sanitize_openai_headers,
)
from relay_detector.openai.client import OpenAIAPIError


def _responses_payload(**overrides):
    payload = {
        "id": "resp_1234567890abcdef",
        "object": "response",
        "created_at": 1741476542,
        "status": "completed",
        "completed_at": 1741476543,
        "model": "gpt-4o-mini",
        "output": [
            {
                "id": "msg_123",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "pong", "annotations": []}
                ],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 2,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 12,
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }
    payload.update(overrides)
    return payload


def _chat_payload(**overrides):
    payload = {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1741569952,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "pong",
                    "refusal": None,
                    "annotations": [],
                },
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
            "completion_tokens": 2,
            "completion_tokens_details": {
                "reasoning_tokens": 0,
                "accepted_prediction_tokens": 0,
                "rejected_prediction_tokens": 0,
            },
            "total_tokens": 12,
        },
        "system_fingerprint": "fp_abc123",
    }
    payload.update(overrides)
    return payload


def test_build_full_probes_uses_official_tool_choice_shapes():
    probes = build_openai_baseline_probes("gpt-4o-mini")
    names = [probe.name for probe in probes]
    assert names == [
        "responses_text",
        "responses_structured_output",
        "responses_tool_call",
        "chat_text",
        "chat_structured_output",
        "chat_tool_call",
    ]

    responses_tool = next(p for p in probes if p.name == "responses_tool_call")
    assert responses_tool.request["tool_choice"] == {
        "type": "function",
        "name": "get_current_weather",
    }
    assert responses_tool.request["tools"][0]["type"] == "function"
    assert responses_tool.request["tools"][0]["strict"] is True

    chat_tool = next(p for p in probes if p.name == "chat_tool_call")
    assert chat_tool.request["tool_choice"] == {
        "type": "function",
        "function": {"name": "get_current_weather"},
    }
    assert chat_tool.request["tools"][0]["function"]["strict"] is True

    responses_structured = next(
        p for p in probes if p.name == "responses_structured_output"
    )
    assert responses_structured.request["text"]["format"]["type"] == "json_schema"

    chat_structured = next(p for p in probes if p.name == "chat_structured_output")
    chat_json_schema = chat_structured.request["response_format"]["json_schema"]
    assert "type" not in chat_json_schema
    assert chat_json_schema["name"] == "relay_detector_probe"


def test_sanitize_openai_headers_keeps_diagnostics_only():
    headers = httpx.Headers(
        {
            "Authorization": "Bearer sk-secret",
            "Set-Cookie": "secret=value",
            "X-Request-ID": "req_123",
            "OpenAI-Processing-MS": "42",
            "X-RateLimit-Remaining-Tokens": "999",
        }
    )
    assert sanitize_openai_headers(headers) == {
        "openai-processing-ms": "42",
        "x-ratelimit-remaining-tokens": "999",
        "x-request-id": "req_123",
    }


def test_extract_responses_features_marks_tool_call_and_json_text():
    payload = _responses_payload(
        output=[
            {
                "id": "fc_123",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_abc",
                "name": "get_current_weather",
                "arguments": "{\"location\":\"Boston, MA\",\"unit\":\"celsius\"}",
            },
            {
                "id": "msg_123",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "{\"ok\":true}"}],
            },
        ],
        tools=[{"type": "function"}],
    )
    features = extract_openai_features("responses", payload)
    assert features["id_prefix"] == "resp_"
    assert features["function_call_seen"] is True
    assert features["function_call_names"] == ["get_current_weather"]
    assert features["function_call_id_prefixes"] == ["call_"]
    assert features["first_output_text_is_json_object"] is True


@pytest.mark.asyncio
async def test_collect_openai_official_baseline_smoke_success():
    class FakeClient:
        async def responses_create(self, **body: Any):
            return body, _responses_payload(model=body["model"]), httpx.Headers(
                {"x-request-id": "req_resp", "set-cookie": "secret=value"}
            ), 12

        async def chat_completions_create(self, **body: Any):
            return body, _chat_payload(model=body["model"]), httpx.Headers(
                {"x-request-id": "req_chat"}
            ), 15

    report = await collect_openai_official_baseline(
        FakeClient(),
        base_url="https://api.openai.com/v1",
        api_key_masked="sk-...test",
        model="gpt-4o-mini",
        wire_api="both",
        probe_set="smoke",
    )

    assert report["provider"] == "openai"
    assert report["api_key_masked"] == "sk-...test"
    assert report["summary"]["probe_count"] == 2
    assert report["summary"]["ok_count"] == 2
    assert report["summary"]["passed_count"] == 2
    assert report["probes"][0]["headers"] == {"x-request-id": "req_resp"}
    assert report["probes"][1]["validation"]["score"] == 100.0


@pytest.mark.asyncio
async def test_collect_openai_official_baseline_records_api_error():
    class FailingClient:
        async def responses_create(self, **body: Any):
            raise OpenAIAPIError(
                401,
                "{\"error\":{\"message\":\"bad key\"}}",
                httpx.Headers({"x-request-id": "req_bad", "set-cookie": "secret"}),
            )

        async def chat_completions_create(self, **body: Any):
            raise AssertionError("not called")

    report = await collect_openai_official_baseline(
        FailingClient(),
        base_url="https://api.openai.com/v1",
        api_key_masked="sk-...test",
        model="gpt-4o-mini",
        wire_api="responses",
        probe_set="smoke",
    )

    assert report["summary"]["ok_count"] == 0
    assert report["summary"]["failed_probe_names"] == ["responses_text"]
    assert report["probes"][0]["error"]["status"] == 401
    assert report["probes"][0]["headers"] == {"x-request-id": "req_bad"}
