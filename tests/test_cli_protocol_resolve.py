"""CLI --protocol resolution: explicit flag and model-name heuristic."""

from __future__ import annotations

import pytest
import typer

from relay_detector.cli import _resolve_protocol
from relay_detector.models import Protocol


def test_explicit_protocol_arg():
    assert _resolve_protocol("anthropic", "irrelevant") == Protocol.ANTHROPIC
    assert _resolve_protocol("openai", "irrelevant") == Protocol.OPENAI
    assert _resolve_protocol("gemini", "irrelevant") == Protocol.GEMINI


def test_explicit_protocol_arg_case_insensitive():
    assert _resolve_protocol("Anthropic", "x") == Protocol.ANTHROPIC
    assert _resolve_protocol("OPENAI", "x") == Protocol.OPENAI


def test_explicit_protocol_invalid_exits():
    with pytest.raises(typer.Exit):
        _resolve_protocol("openrouter", "x")


def test_auto_detect_claude_models():
    assert _resolve_protocol(None, "claude-haiku-4-5") == Protocol.ANTHROPIC
    assert _resolve_protocol(None, "claude-opus-4-7") == Protocol.ANTHROPIC
    assert _resolve_protocol(None, "claude-sonnet-4-6-20251001") == Protocol.ANTHROPIC


def test_auto_detect_openai_models():
    assert _resolve_protocol(None, "gpt-4o") == Protocol.OPENAI
    assert _resolve_protocol(None, "gpt-4o-mini") == Protocol.OPENAI
    assert _resolve_protocol(None, "gpt-4.1") == Protocol.OPENAI
    assert _resolve_protocol(None, "o1-mini") == Protocol.OPENAI
    assert _resolve_protocol(None, "o3") == Protocol.OPENAI
    assert _resolve_protocol(None, "chatgpt-4o-latest") == Protocol.OPENAI


def test_auto_detect_gemini_models():
    assert _resolve_protocol(None, "gemini-2.5-flash") == Protocol.GEMINI
    assert _resolve_protocol(None, "gemini-2.5-pro") == Protocol.GEMINI
    assert _resolve_protocol(None, "models/gemini-1.5-pro") == Protocol.GEMINI


def test_auto_detect_unknown_falls_back_to_anthropic():
    """Unknown / legacy aliases don't crash; fall back to anthropic which
    is the historical default for relay-detector."""
    assert _resolve_protocol(None, "mystery-model-9000") == Protocol.ANTHROPIC
    assert _resolve_protocol(None, "") == Protocol.ANTHROPIC


def test_explicit_overrides_heuristic():
    """If user passes --protocol openai but model name says claude-X,
    explicit wins. Catches relays that route claude-X to GPT for testing."""
    assert _resolve_protocol("openai", "claude-haiku-4-5") == Protocol.OPENAI
    assert _resolve_protocol("anthropic", "gpt-4o") == Protocol.ANTHROPIC
