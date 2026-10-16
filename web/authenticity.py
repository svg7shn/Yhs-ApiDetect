"""Channel authenticity detection engine — PRD v1.0, Yhs-ApiDetect-native shape.

Division of labour
------------------
The **browser** owns the secret (base_url + api_key) for the lifetime of the page
only — it is never written to localStorage or any other store; the **server** owns
everything that must not be trusted to a client: source validation, cross-family
rejection, the probe suite, verdict scoring and the redacted probe trace.

Hard rules carried over from the PRD:
  * claimed_source is mandatory — never silently inferred.
  * a model may be blank only when the caller supplies the relay's model list,
    in which case a same-family model is picked; if none exists we raise a
    config error and write no record at all rather than fabricate one.
  * model and source must belong to the same family — cross-family is rejected.
  * HTTP 200 is never *sufficient* evidence of authenticity; a single timeout
    is never *sufficient* evidence of fraud.
  * transport-level failures (connect / TLS / timeout) are classified error
    and excluded from the authentic/suspect/fail denominator.
  * the probe trace never stores auth headers or API keys, and response bodies
    are truncated.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
import re
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx

# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
# Single source of truth for the source enum. The frontend fetches this list
# from the API rather than hard-coding labels, so adding a source is a
# one-line change here.

Family = Literal["claude", "gpt", "gemini"]
WireFamily = Literal["anthropic", "openai", "gemini"]
Verdict = Literal["authentic", "suspect", "fail", "error"]

SOURCE_OPTIONS: tuple[dict[str, str], ...] = (
    # --- Claude family -----------------------------------------------------
    {"id": "anthropic_direct", "label": "Claude 官方直连(Anthropic API)", "family": "claude"},
    {"id": "claude_code_max", "label": "Claude Code + Max 订阅", "family": "claude"},
    {"id": "aws_bedrock", "label": "AWS Bedrock", "family": "claude"},
    {"id": "aws_partner", "label": "AWS Partner(第三方转售)", "family": "claude"},
    {"id": "amazon_q_kiro", "label": "Amazon Q / Kiro", "family": "claude"},
    {"id": "vertex_claude", "label": "Google Vertex AI(Claude)", "family": "claude"},
    # --- OpenAI family -----------------------------------------------------
    {"id": "openai_direct", "label": "OpenAI 官方直连", "family": "gpt"},
    {"id": "azure_openai", "label": "Azure OpenAI", "family": "gpt"},
    # --- Gemini family -----------------------------------------------------
    {"id": "google_ai_studio", "label": "Google AI Studio(Gemini 官方)", "family": "gemini"},
    {"id": "vertex_gemini", "label": "Google Vertex AI(Gemini)", "family": "gemini"},
)

SOURCES_BY_ID: dict[str, dict[str, str]] = {s["id"]: s for s in SOURCE_OPTIONS}

# Every source family maps onto exactly one wire family: the line protocol is a
# property of the transport, not of what the reseller claims.
FAMILY_TO_WIRE: dict[str, str] = {
    "claude": "anthropic",
    "gpt": "openai",
    "gemini": "gemini",
}

WIRE_LABELS = {
    "anthropic": "Anthropic Messages",
    "openai": "OpenAI Chat Completions",
    "gemini": "Gemini generateContent",
}
FAMILY_LABELS = {"claude": "Claude", "gpt": "OpenAI", "gemini": "Gemini"}
VERDICT_LABELS = {
    "authentic": "真实",
    "suspect": "疑似异常",
    "fail": "判定伪造",
    "error": "传输错误",
}

# The model we request when we deliberately want the relay to fail: it makes the
# relay emit its own "unknown model" error, whose shape fingerprints the
# upstream it is really routing to.
NONEXISTENT_MODEL = "__authenticity_probe_nonexistent__"


class ConfigError(Exception):
    """A target is misconfigured — no probe ran, no record may be written."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

_GPT_RE = re.compile(r"^(gpt-|o1|o3|o4|chatgpt)", re.IGNORECASE)
_GEMINI_RE = re.compile(r"^(gemini|models/gemini)", re.IGNORECASE)


def family_from_model(model: str) -> str | None:
    """Best-effort model family. Returns None when nothing matches."""
    name = (model or "").strip()
    if not name:
        return None
    low = name.lower()
    if low.startswith("claude") or low.startswith("anthropic"):
        return "claude"
    if _GPT_RE.match(low):
        return "gpt"
    if _GEMINI_RE.match(low):
        return "gemini"
    return None


def validate_target(
    model: str,
    claimed_source: str,
    available_models: Iterable[str] | None = None,
    allow_blank_model: bool = False,
) -> dict[str, str]:
    """Validate one (model, claimed_source) pair. Raises ConfigError otherwise.

    Returns the normalised target with its resolved family / wire family.
    """
    source_id = (claimed_source or "").strip()
    model = (model or "").strip()

    if not source_id:
        raise ConfigError("missing_source", "必须显式填写声称来源,不能留空或由系统推断。")
    source = SOURCES_BY_ID.get(source_id)
    if source is None:
        raise ConfigError("unknown_source", f"未知的来源标识:{source_id}")
    if len(model) > 200:
        raise ConfigError("model_too_long", "模型名过长(上限 200 字符)。")

    family = source["family"]
    model_family = family_from_model(model)
    if model and model_family is None:
        raise ConfigError(
            "unknown_model_family",
            f"无法从模型名 {model} 识别模型族,无法校验它与来源是否同族。",
        )
    if model and model_family != family:
        raise ConfigError(
            "cross_family",
            f"模型 {model} 属于 {FAMILY_LABELS.get(model_family, model_family)} 族,"
            f"而来源「{source['label']}」属于 {FAMILY_LABELS.get(family, family)} 族,不允许跨族保存。",
        )

    if not model:
        # A blank model is legal at *save* time only (the relay's model list is
        # fetched later); at *run* time we must resolve it to a concrete name,
        # or refuse rather than fabricate a record.
        if allow_blank_model and available_models is None:
            return {
                "model": "",
                "claimed_source": source_id,
                "source_label": source["label"],
                "family": family,
                "wire_family": FAMILY_TO_WIRE[family],
            }
        if available_models is None:
            raise ConfigError(
                "missing_model",
                "模型名留空,且没有可用的模型列表;请先探测该渠道的模型列表再自动选取同族模型。",
            )
        picked = pick_same_family_model(available_models, source_id)
        if picked is None:
            raise ConfigError(
                "no_same_family_model",
                f"该渠道没有暴露任何 {FAMILY_LABELS.get(family, family)} 族模型,"
                "无法为来源「" + source["label"] + "」自动选型;请显式填写模型名。",
            )
        model = picked

    return {
        "model": model,
        "claimed_source": source_id,
        "source_label": source["label"],
        "family": family,
        "wire_family": FAMILY_TO_WIRE[family],
    }


def pick_same_family_model(available: Iterable[str], source_id: str) -> str | None:
    """Auto-select a model for a target whose model field was left blank."""
    source = SOURCES_BY_ID.get(source_id)
    if source is None:
        return None
    want = source["family"]
    for name in available:
        if family_from_model(name) == want:
            return name
    return None


def channel_id_for(base_url: str) -> str:
    """Stable short id for a channel — groups configs and records."""
    digest = hashlib.sha256((base_url or "").strip().rstrip("/").lower().encode("utf-8"))
    return digest.hexdigest()[:12]


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

_SENSITIVE_HEADERS = {
    "authorization", "x-api-key", "x-goog-api-key", "api-key", "cookie", "set-cookie",
}


def redact_headers(headers: Any) -> dict[str, str]:
    """Drop credential-bearing headers from anything we persist."""
    if not headers:
        return {}
    items = headers.items() if hasattr(headers, "items") else headers
    return {
        str(k).lower(): str(v)
        for k, v in items
        if str(k).lower() not in _SENSITIVE_HEADERS
    }


def truncate(text: Any, limit: int = 400) -> str:
    body = "" if text is None else str(text)
    return body if len(body) <= limit else body[:limit] + "...[" + str(len(body) - limit) + " more chars]"


# --------------------------------------------------------------------------
# Wire adapters — one per line protocol
# --------------------------------------------------------------------------


@dataclass
class WireHTTP:
    """Everything a probe may look at, with the body already truncated."""

    ok: bool
    status: int | None = None
    body: Any = None
    text: str = ""
    latency_ms: int = 0
    transport_error: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_transport_error(self) -> bool:
        return self.transport_error is not None


class Wire:
    """Base adapter: URL building, request shaping, reply-text extraction."""

    wire_family: str = ""
    version_segment: str = ""

    def __init__(self, base_url: str) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")

    def _join(self, path: str) -> str:
        if self.version_segment and self.base_url.endswith(self.version_segment):
            return self.base_url + path[len(self.version_segment):]
        return self.base_url + path

    def chat_url(self, model: str) -> str:
        raise NotImplementedError

    def models_url(self) -> str:
        raise NotImplementedError

    def headers(self, api_key: str) -> dict[str, str]:
        raise NotImplementedError

    def chat_body(self, model: str, prompt: str, **kw: Any) -> dict[str, Any]:
        raise NotImplementedError

    def extract_text(self, payload: Any) -> str:
        raise NotImplementedError

    def list_models(self, payload: Any) -> list[str]:
        raise NotImplementedError

    @property
    def supports_cache_probe(self) -> bool:
        return True

    @property
    def supports_thinking_probe(self) -> bool:
        return False


class AnthropicWire(Wire):
    wire_family = "anthropic"
    version_segment = "/v1"

    def chat_url(self, model: str) -> str:
        return self._join("/v1/messages")

    def models_url(self) -> str:
        return self._join("/v1/models")

    def headers(self, api_key: str) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def chat_body(self, model: str, prompt: str, **kw: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": int(kw.get("max_tokens", 64)),
            "messages": [{"role": "user", "content": prompt}],
        }
        if kw.get("system"):
            body["system"] = kw["system"]
        if kw.get("temperature") is not None:
            body["temperature"] = kw["temperature"]
        if kw.get("top_p") is not None:
            body["top_p"] = kw["top_p"]
        if kw.get("thinking"):
            budget = int(kw.get("thinking_budget", 1024))
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            body["max_tokens"] = max(body["max_tokens"], budget + 256)
        return body

    def extract_text(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        blocks = payload.get("content") or []
        if not isinstance(blocks, list):
            return ""
        return "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )

    def list_models(self, payload: Any) -> list[str]:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        return [str(m.get("id", "")) for m in data if isinstance(m, dict) and m.get("id")]

    @property
    def supports_thinking_probe(self) -> bool:
        return True


class OpenAIWire(Wire):
    wire_family = "openai"
    version_segment = "/v1"

    def chat_url(self, model: str) -> str:
        return self._join("/v1/chat/completions")

    def models_url(self) -> str:
        return self._join("/v1/models")

    def headers(self, api_key: str) -> dict[str, str]:
        return {"authorization": "Bearer " + api_key, "content-type": "application/json"}

    def chat_body(self, model: str, prompt: str, **kw: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": int(kw.get("max_tokens", 64)),
            "messages": [{"role": "user", "content": prompt}],
        }
        if kw.get("temperature") is not None:
            body["temperature"] = kw["temperature"]
        if kw.get("top_p") is not None:
            body["top_p"] = kw["top_p"]
        return body

    def extract_text(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return ""

    def list_models(self, payload: Any) -> list[str]:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        return [str(m.get("id", "")) for m in data if isinstance(m, dict) and m.get("id")]


class GeminiWire(Wire):
    wire_family = "gemini"
    version_segment = "/v1beta"

    def chat_url(self, model: str) -> str:
        return self._join("/v1beta/models/" + model + ":generateContent")

    def models_url(self) -> str:
        return self._join("/v1beta/models")

    def headers(self, api_key: str) -> dict[str, str]:
        return {"x-goog-api-key": api_key, "content-type": "application/json"}

    def chat_body(self, model: str, prompt: str, **kw: Any) -> dict[str, Any]:
        gen: dict[str, Any] = {"maxOutputTokens": int(kw.get("max_tokens", 64))}
        if kw.get("temperature") is not None:
            gen["temperature"] = kw["temperature"]
        if kw.get("top_p") is not None:
            gen["topP"] = kw["top_p"]
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen,
        }

    def extract_text(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return ""
        first = candidates[0] if isinstance(candidates[0], dict) else {}
        content = first.get("content") or {}
        parts = content.get("parts")
        if not isinstance(parts, list):
            return ""
        return "".join(
            p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
        )

    def list_models(self, payload: Any) -> list[str]:
        if not isinstance(payload, dict):
            return []
        models = payload.get("models")
        if not isinstance(models, list):
            return []
        out: list[str] = []
        for m in models:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name", ""))
            name = name.removeprefix("models/")
            if name:
                out.append(name)
        return out


_WIRES: dict[str, type[Wire]] = {
    "anthropic": AnthropicWire,
    "openai": OpenAIWire,
    "gemini": GeminiWire,
}


def wire_for(wire_family: str, base_url: str) -> Wire:
    cls = _WIRES.get(wire_family)
    if cls is None:
        raise ConfigError("unknown_wire", "未知的线协议:" + str(wire_family))
    return cls(base_url)


# --------------------------------------------------------------------------
# Raw request helpers
# --------------------------------------------------------------------------


async def raw_post(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float = 45.0,
) -> WireHTTP:
    """POST and keep everything the error-injection probe needs.

    Transport failures come back as transport_error rather than raising,
    because "the socket died" and "the relay said no" mean different things in
    the verdict model.
    """
    started = time.perf_counter()
    try:
        resp = await client.post(url, headers=headers, json=body, timeout=timeout)
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
        return WireHTTP(
            ok=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            transport_error=type(exc).__name__ + ": " + str(exc),
        )
    latency = int((time.perf_counter() - started) * 1000)
    try:
        payload = resp.json()
    except Exception:
        payload = None
    return WireHTTP(
        ok=200 <= resp.status_code < 300,
        status=resp.status_code,
        body=payload,
        text=resp.text or "",
        latency_ms=latency,
        headers=redact_headers(resp.headers),
    )


async def raw_get(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], timeout: float = 30.0
) -> WireHTTP:
    started = time.perf_counter()
    try:
        resp = await client.get(url, headers=headers, timeout=timeout)
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
        return WireHTTP(
            ok=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            transport_error=type(exc).__name__ + ": " + str(exc),
        )
    latency = int((time.perf_counter() - started) * 1000)
    try:
        payload = resp.json()
    except Exception:
        payload = None
    return WireHTTP(
        ok=200 <= resp.status_code < 300,
        status=resp.status_code,
        body=payload,
        text=resp.text or "",
        latency_ms=latency,
        headers=redact_headers(resp.headers),
    )


# --------------------------------------------------------------------------
# Capability questions — generated per run, never a fixed public list
# --------------------------------------------------------------------------

_WORDS = (
    "verification", "authenticity", "transparency", "reliability", "consistency",
    "protocol", "signature", "latency", "throughput", "integrity",
)


def _normalise_answer(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", (text or "").lower())


@dataclass
class CapabilityQuestion:
    id: str
    prompt: str
    answer: str

    def check(self, reply: str) -> bool:
        return _normalise_answer(reply) == _normalise_answer(self.answer)


def build_capability_questions(
    count: int = 3, rng: random.Random | None = None
) -> list[CapabilityQuestion]:
    """Generate verifiable questions on the fly.

    A fixed bank of easy questions is exactly what a relay can special-case, so
    every run draws fresh operands and a fresh word.
    """
    rng = rng or random.Random()
    kinds = ["count_letter", "arithmetic", "reverse", "digit_sum"]
    rng.shuffle(kinds)
    questions: list[CapabilityQuestion] = []
    for index in range(max(1, count)):
        kind = kinds[index % len(kinds)]
        if kind == "count_letter":
            word = rng.choice(_WORDS)
            letter = rng.choice(sorted(set(word)))
            questions.append(
                CapabilityQuestion(
                    id="q" + str(index + 1) + "_count_letter",
                    prompt=(
                        "How many times does the letter " + repr(letter) + " appear in the word "
                        + repr(word) + "? Reply with the number only."
                    ),
                    answer=str(word.count(letter)),
                )
            )
        elif kind == "arithmetic":
            a, b, c = rng.randint(120, 980), rng.randint(37, 89), rng.randint(11, 29)
            questions.append(
                CapabilityQuestion(
                    id="q" + str(index + 1) + "_arithmetic",
                    prompt="Compute (" + str(a) + " * " + str(b) + ") + " + str(c) + ". Reply with the number only.",
                    answer=str(a * b + c),
                )
            )
        elif kind == "reverse":
            word = rng.choice(_WORDS)
            questions.append(
                CapabilityQuestion(
                    id="q" + str(index + 1) + "_reverse",
                    prompt="Reverse the string " + repr(word) + " character by character. Reply with the reversed string only.",
                    answer=word[::-1],
                )
            )
        else:
            digits = "".join(str(rng.randint(1, 9)) for _ in range(7))
            questions.append(
                CapabilityQuestion(
                    id="q" + str(index + 1) + "_digit_sum",
                    prompt="What is the sum of the digits of " + digits + "? Reply with the number only.",
                    answer=str(sum(int(d) for d in digits)),
                )
            )
    return questions


IDENTITY_PROMPT = (
    "In one short line, state the exact model name and the company that trained you. "
    "Example format: model=<name>; company=<company>. Do not refuse, do not explain."
)

_FAMILY_KEYWORDS = {
    "claude": ("claude", "anthropic"),
    "gpt": ("gpt", "openai", "chatgpt"),
    "gemini": ("gemini", "google", "deepmind"),
}


def identity_matches(reply: str, family: str) -> bool:
    low = (reply or "").lower()
    return any(kw in low for kw in _FAMILY_KEYWORDS.get(family, ()))


def identity_contradicts(reply: str, family: str) -> str | None:
    """Return the conflicting family when the model names a different vendor."""
    low = (reply or "").lower()
    if not low or identity_matches(reply, family):
        return None
    for other, kws in _FAMILY_KEYWORDS.items():
        if other != family and any(kw in low for kw in kws):
            return other
    return None


# --------------------------------------------------------------------------
# Probes — each returns a signal dict, plus trace entries for the report
# --------------------------------------------------------------------------


def _signal(name: str, status: str, score: float | None, detail: str, **extra: Any) -> dict[str, Any]:
    """status: pass | warn | fail | skip | unavailable."""
    payload = {"signal": name, "status": status, "score": score, "detail": detail}
    payload.update(extra)
    return payload


def _trace(step: str, status: Any, latency_ms: int | None = None, note: str = "", **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"step": step, "status": status, "latency_ms": latency_ms, "note": note}
    entry.update(extra)
    return entry


async def probe_models(client: httpx.AsyncClient, wire: Wire, api_key: str) -> tuple[list[str], WireHTTP]:
    """Read the relay's advertised model list — used to auto-pick a model."""
    result = await raw_get(client, wire.models_url(), wire.headers(api_key))
    return wire.list_models(result.body), result


def classify_error_shape(status: int | None, payload: Any, text: str) -> dict[str, Any]:
    """Fingerprint an error reply: whose error envelope is this really?

    A relay that speaks Anthropic on the way in but answers with an OpenAI-shaped
    error body has told us something about what it is actually routing to.
    """
    evidence: dict[str, Any] = {"http_status": status}
    if payload is None:
        looks_html = "<html" in (text or "").lower() or "<!doctype" in (text or "").lower()
        evidence["shape"] = "html_or_plaintext" if looks_html else "unparseable"
        evidence["excerpt"] = truncate(text, 240)
        return evidence

    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, str):
        evidence["shape"] = "error_string"
        evidence["message"] = truncate(err, 240)
        return evidence
    if not isinstance(err, dict):
        evidence["shape"] = "unknown_json"
        evidence["keys"] = sorted(payload.keys())[:12] if isinstance(payload, dict) else []
        return evidence

    keys = {str(k) for k in err.keys()}
    evidence["keys"] = sorted(keys)[:12]
    evidence["message"] = truncate(err.get("message", ""), 240)
    evidence["error_type"] = str(err.get("type", ""))[:80]
    evidence["error_code"] = str(err.get("code", ""))[:80]

    # Google/Gemini style: error.code is an int + error.status is an enum string
    if isinstance(err.get("code"), int) and "status" in keys:
        evidence["shape"] = "gemini_error"
        evidence["error_status"] = str(err.get("status", ""))[:60]
        return evidence
    etype = str(err.get("type", ""))
    top_type = str(payload.get("type", "")) if isinstance(payload, dict) else ""
    # Anthropic always wraps its error: {"type": "error", "error": {"type": ..., "message": ...}}
    if top_type == "error":
        evidence["shape"] = "anthropic_error"
        return evidence
    # OpenAI style: the error object carries code/param next to message.
    # NOTE: OpenAI also uses type values such as invalid_request_error, so these
    # OpenAI-shaped checks must run *before* the _error-suffix rule below —
    # otherwise a genuine OpenAI error body gets fingerprinted as Anthropic.
    if "message" in keys and (keys & {"param", "code"}):
        evidence["shape"] = "openai_error"
        return evidence
    error_only_envelope = isinstance(payload, dict) and {str(k) for k in payload.keys()} == {"error"}
    if "message" in keys and error_only_envelope:
        evidence["shape"] = "openai_error"
        return evidence
    # Anthropic style: error.type like invalid_request_error / not_found_error
    if etype.endswith("_error") or etype in {"authentication_error", "permission_error", "rate_limit_error"}:
        evidence["shape"] = "anthropic_error"
        return evidence
    if "message" in keys:
        evidence["shape"] = "generic_json_error"
        return evidence
    evidence["shape"] = "unknown_json"
    return evidence


_EXPECTED_ERROR_SHAPE = {
    "anthropic": "anthropic_error",
    "openai": "openai_error",
    "gemini": "gemini_error",
}


async def probe_error_injection(client: httpx.AsyncClient, wire: Wire, api_key: str) -> dict[str, Any]:
    """Ask for a model that cannot exist and fingerprint the resulting error."""
    body = wire.chat_body(NONEXISTENT_MODEL, "ping", max_tokens=8)
    result = await raw_post(client, wire.chat_url(NONEXISTENT_MODEL), wire.headers(api_key), body)
    if result.is_transport_error:
        return _signal(
            "error_injection",
            "unavailable",
            None,
            "错误注入探针遇到传输级失败:" + str(result.transport_error),
            trace=_trace("error_injection", "transport_error", result.latency_ms, str(result.transport_error)),
        )

    shape = classify_error_shape(result.status, result.body, result.text)
    expected = _EXPECTED_ERROR_SHAPE.get(wire.wire_family)
    matched = shape.get("shape") == expected

    if result.ok:
        detail = "请求不存在的模型竟然返回成功,该中转站未按协议校验模型名。"
        status, score = "fail", 0.0
    elif matched:
        detail = "错误响应形态与 " + WIRE_LABELS[wire.wire_family] + " 协议一致(HTTP " + str(result.status) + ")。"
        status, score = "pass", 1.0
    elif shape.get("shape") in {"generic_json_error", "error_string"}:
        detail = "返回了 JSON 错误但字段形态不属于任一官方协议,无法据此确认上游来源。"
        status, score = "warn", 0.4
    else:
        detail = "错误响应形态为 " + str(shape.get("shape")) + ",与所声明的协议不符。"
        status, score = "warn", 0.1

    return _signal(
        "error_injection",
        status,
        score,
        detail,
        fingerprint=shape,
        trace=_trace("error_injection", result.status, result.latency_ms, detail, fingerprint=shape),
    )


def _usage_of(payload: Any, wire_family: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if isinstance(usage, dict):
        out = dict(usage)
        # OpenAI nests cache detail one level down
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            out["_cached_tokens"] = details.get("cached_tokens")
        return out
    meta = payload.get("usageMetadata")
    if isinstance(meta, dict):
        return dict(meta)
    return {}


async def probe_cache(client: httpx.AsyncClient, wire: Wire, api_key: str, model: str) -> dict[str, Any]:
    """Cache probe: a stable prefix, sent twice; is the second call cached?

    A cache *miss* is never on its own evidence of fraud — some relays disable
    prompt caching, and the fields are optional. It only becomes a signal when
    the protocol clearly exposes cache counters and they stay at zero.
    """
    if not wire.supports_cache_probe:
        return _signal("cache", "skip", None, "该协议不支持缓存探针。")

    prefix = ("STABLE PREFIX FOR CACHE PROBE. " * 40).strip()
    kwargs: dict[str, Any] = {"max_tokens": 16}
    if wire.wire_family == "anthropic":
        kwargs["system"] = [{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}]
    else:
        kwargs["system"] = prefix

    first = await raw_post(client, wire.chat_url(model), wire.headers(api_key), wire.chat_body(model, "Reply with OK only.", **kwargs))
    if first.is_transport_error or not first.ok:
        return _signal(
            "cache",
            "unavailable",
            None,
            "缓存探针第一轮未成功,跳过缓存判定。",
            trace=_trace("cache_round1", first.status, first.latency_ms, "未成功"),
        )
    second = await raw_post(client, wire.chat_url(model), wire.headers(api_key), wire.chat_body(model, "Reply with OK only.", **kwargs))
    if second.is_transport_error or not second.ok:
        return _signal(
            "cache",
            "unavailable",
            None,
            "缓存探针第二轮未成功,跳过缓存判定。",
            trace=_trace("cache_round2", second.status, second.latency_ms, "未成功"),
        )

    usage2 = _usage_of(second.body, wire.wire_family)
    cache_fields = {
        k: v
        for k, v in usage2.items()
        if "cache" in str(k).lower() or k in {"cached_tokens", "_cached_tokens", "cachedContentTokenCount"}
    }
    hit = _cache_hit_value(usage2)

    if not cache_fields:
        return _signal(
            "cache",
            "unavailable",
            None,
            "响应中没有任何缓存计数字段(该渠道或该协议未暴露缓存信息),不据此判定。",
            trace=_trace("cache_round2", second.status, second.latency_ms, "无缓存字段"),
        )

    detail = "第二轮命中缓存 " + str(hit) + " token(字段:" + ",".join(sorted(map(str, cache_fields.keys()))) + ")。"
    status_score = "pass" if (isinstance(hit, int) and hit > 0) else "warn"
    return _signal(
        "cache",
        status_score,
        1.0 if status_score == "pass" else 0.5,
        detail,
        cache_fields=cache_fields,
        trace=_trace("cache_round2", second.status, second.latency_ms, detail, cache_fields=cache_fields),
    )


def _cache_hit_value(usage: dict[str, Any]) -> int | None:
    for key in (
        "cache_read_input_tokens",
        "cached_tokens",
        "_cached_tokens",
        "cachedContentTokenCount",
        "cache_creation_input_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int):
            return value
    return None


async def probe_params(client: httpx.AsyncClient, wire: Wire, api_key: str, model: str) -> dict[str, Any]:
    """Inject a parameter the model family forbids and see whether it is rejected."""
    body = wire.chat_body(model, "ping", max_tokens=8, temperature=99.0)
    result = await raw_post(client, wire.chat_url(model), wire.headers(api_key), body)
    if result.is_transport_error:
        return _signal(
            "parameter_validation",
            "unavailable",
            None,
            "参数校验探针遇到传输级失败,跳过。",
            trace=_trace("parameter_validation", "transport_error", result.latency_ms, str(result.transport_error)),
        )
    if result.ok:
        return _signal(
            "parameter_validation",
            "warn",
            0.3,
            "temperature=99 这类越界参数被静默接受,该渠道未做参数校验。",
            trace=_trace("parameter_validation", result.status, result.latency_ms, "越界参数被接受"),
        )
    shape = classify_error_shape(result.status, result.body, result.text)
    expected = _EXPECTED_ERROR_SHAPE.get(wire.wire_family)
    ok_shape = shape.get("shape") == expected
    return _signal(
        "parameter_validation",
        "pass" if ok_shape else "warn",
        1.0 if ok_shape else 0.4,
        "越界参数被拒绝,错误形态" + ("与协议一致。" if ok_shape else "与协议不符。"),
        fingerprint=shape,
        trace=_trace("parameter_validation", result.status, result.latency_ms, str(shape.get("shape"))),
    )


async def probe_capability(
    client: httpx.AsyncClient,
    wire: Wire,
    api_key: str,
    model: str,
    questions: list[CapabilityQuestion],
    enable_thinking: bool,
) -> dict[str, Any]:
    """Run generated capability questions, plus the identity self-report.

    For Anthropic wire we open with thinking enabled: if the relay accepts it and
    returns signed thinking blocks that is strong evidence, and if it rejects it
    with a clear "not supported" error we retry without thinking instead of
    calling the channel fake (PRD: thinking-down then retry, never judge on it).
    """
    results: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    thinking_supported: bool | None = None
    thinking_signed: bool | None = None
    downgraded = False

    for index, question in enumerate(questions):
        kwargs: dict[str, Any] = {"max_tokens": 128}
        want_thinking = bool(enable_thinking and wire.supports_thinking_probe and index == 0)
        if want_thinking:
            kwargs["thinking"] = True
            kwargs["thinking_budget"] = 1024

        result = await raw_post(client, wire.chat_url(model), wire.headers(api_key), wire.chat_body(model, question.prompt, **kwargs))

        if want_thinking and not result.is_transport_error and not result.ok:
            mention = (result.text or "").lower()
            if any(token in mention for token in ("thinking", "not supported", "unsupported", "invalid_request")):
                downgraded = True
                thinking_supported = False
                result = await raw_post(client, wire.chat_url(model), wire.headers(api_key), wire.chat_body(model, question.prompt, max_tokens=128))
            else:
                thinking_supported = False
        elif want_thinking and result.ok:
            thinking_supported = True
            blocks = result.body.get("content") if isinstance(result.body, dict) else None
            if isinstance(blocks, list):
                signed = [
                    b for b in blocks
                    if isinstance(b, dict) and b.get("type") == "thinking" and b.get("signature")
                ]
                thinking_signed = bool(signed)

        if result.is_transport_error:
            results.append({"id": question.id, "outcome": "transport_error", "reply": "", "expected": question.answer})
            trace.append(_trace("capability:" + question.id, "transport_error", result.latency_ms, str(result.transport_error)))
            continue

        reply = wire.extract_text(result.body) if result.ok else ""
        correct = result.ok and question.check(reply)
        results.append(
            {
                "id": question.id,
                "outcome": "correct" if correct else ("wrong" if result.ok else "http_" + str(result.status)),
                "reply": truncate(reply, 120),
                "expected": question.answer,
            }
        )
        trace.append(
            _trace(
                "capability:" + question.id,
                result.status,
                result.latency_ms,
                ("答对" if correct else "未答对") + " expected=" + question.answer,
                reply=truncate(reply, 120),
            )
        )

    # Identity self-report rides along with the capability probe.
    identity_reply = ""
    ident_result = await raw_post(client, wire.chat_url(model), wire.headers(api_key), wire.chat_body(model, IDENTITY_PROMPT, max_tokens=80))
    if not ident_result.is_transport_error:
        identity_reply = wire.extract_text(ident_result.body) if ident_result.ok else truncate(ident_result.text, 160)
        trace.append(_trace("identity", ident_result.status, ident_result.latency_ms, truncate(identity_reply, 100)))

    answered = [r for r in results if r["outcome"] in {"correct", "wrong"}]
    correct = [r for r in results if r["outcome"] == "correct"]
    accuracy = (len(correct) / len(answered)) if answered else None

    if answered and accuracy is not None:
        if accuracy >= 0.8:
            status, score = "pass", 1.0
        elif accuracy >= 0.5:
            status, score = "warn", 0.6
        elif accuracy > 0:
            status, score = "warn", 0.3
        else:
            status, score = "fail", 0.0
        detail = "能力题 " + str(len(correct)) + "/" + str(len(answered)) + " 答对。"
    else:
        status, score, detail = "unavailable", None, "能力题全部未获得可判定的响应。"

    return _signal(
        "capability",
        status,
        score,
        detail,
        accuracy=accuracy,
        answers=results,
        identity_reply=truncate(identity_reply, 200),
        thinking_supported=thinking_supported,
        thinking_signed=thinking_signed,
        thinking_downgraded=downgraded,
        trace=trace,
    )


def signal_identity(reply: str, family: str, thinking_signed: bool | None) -> dict[str, Any]:
    """Turn the self-report (plus a signed thinking block, if any) into a signal."""
    if not reply:
        base = _signal("identity", "unavailable", None, "身份自报题未获得响应,不作为证据。")
    else:
        conflict = identity_contradicts(reply, family)
        if conflict:
            base = _signal(
                "identity",
                "fail",
                0.0,
                "模型自报身份为 " + FAMILY_LABELS.get(conflict, conflict) + " 族,与声称来源的 "
                + FAMILY_LABELS.get(family, family) + " 族矛盾。",
                self_reported_family=conflict,
            )
        elif identity_matches(reply, family):
            base = _signal("identity", "pass", 1.0, "模型自报身份与声称来源族一致(仅作辅助证据,不单独定论)。")
        else:
            base = _signal("identity", "warn", 0.5, "模型自报身份无法确认来源族,不计入正证。")

    if thinking_signed is True:
        base["detail"] = base["detail"] + " 另有签名 thinking 块作为强证据。"
    return base


def signal_thinking(thinking_supported: bool | None, thinking_signed: bool | None, wire_family: str) -> dict[str, Any]:
    if wire_family != "anthropic":
        return _signal("thinking_signature", "skip", None, "该协议没有 thinking 签名机制,不参与评分。")
    if thinking_supported is None:
        return _signal("thinking_signature", "unavailable", None, "未能确定 thinking 支持情况,不参与评分。")
    if thinking_supported and thinking_signed:
        return _signal("thinking_signature", "pass", 1.0, "思考块带非空加密签名,中转站难以伪造。")
    if thinking_supported and not thinking_signed:
        return _signal("thinking_signature", "warn", 0.5, "接受 thinking 参数但未返回带签名的思考块。")
    return _signal("thinking_signature", "warn", 0.6, "渠道不支持 thinking 参数;已降级重试,不据此判假。")


def signal_model_echo(payload: Any, requested: str, wire_family: str) -> dict[str, Any]:
    """Does the success reply echo the model we asked for (or an alias of it)?"""
    if not isinstance(payload, dict):
        return _signal("model_echo", "unavailable", None, "没有可用的成功响应来核对 model 字段。")
    echoed = payload.get("model")
    if not echoed and wire_family == "gemini":
        echoed = payload.get("modelVersion")
    if not echoed:
        return _signal("model_echo", "warn", 0.5, "成功响应未回显 model 字段。")
    echoed = str(echoed)
    if echoed == requested:
        return _signal("model_echo", "pass", 1.0, "响应回显的模型名与请求一致:" + echoed)
    if family_from_model(echoed) == family_from_model(requested):
        return _signal("model_echo", "warn", 0.7, "响应回显的是同族别名:" + echoed + "(请求 " + requested + ")")
    return _signal(
        "model_echo",
        "fail",
        0.0,
        "响应回显的模型 " + echoed + " 与请求的 " + requested + " 不同族。",
        echoed=echoed,
    )


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

SIGNAL_WEIGHTS: dict[str, int] = {
    "capability": 40,
    "identity": 18,
    "error_injection": 12,
    "thinking_signature": 10,
    "model_echo": 8,
    "parameter_validation": 7,
    "cache": 5,
}

VERDICT_THRESHOLDS = (80.0, 50.0)  # authentic >= 80 > suspect >= 50 > fail


def score_signals(signals: list[dict[str, Any]]) -> tuple[float, float, dict[str, Any]]:
    """Weighted 0–100 score plus a confidence ratio.

    Signals with status skip/unavailable are dropped from the denominator, so an
    unsupported feature never counts as a deduction. Confidence is the share of
    the full weight that actually produced evidence.
    """
    weighted = 0.0
    effective = 0.0
    total_weight = float(sum(SIGNAL_WEIGHTS.values()))
    for sig in signals:
        weight = SIGNAL_WEIGHTS.get(str(sig.get("signal")), 0)
        if not weight:
            continue
        if sig.get("status") in {"skip", "unavailable"} or sig.get("score") is None:
            continue
        effective += weight
        weighted += weight * float(sig["score"])
    score = (weighted / effective * 100.0) if effective else 0.0
    confidence = (effective / total_weight) if total_weight else 0.0
    return round(score, 1), round(confidence, 3), {"effective_weight": effective, "total_weight": total_weight}


def decide_verdict(
    score: float,
    signals: list[dict[str, Any]],
    transport_error: str | None,
    identity_conflict: str | None,
) -> Verdict:
    """Transport failure wins; otherwise thresholds, with identity conflict capping."""
    if transport_error:
        return "error"
    if identity_conflict:
        return "fail" if score < VERDICT_THRESHOLDS[1] else "suspect"
    high, low = VERDICT_THRESHOLDS
    if score >= high:
        return "authentic"
    if score >= low:
        return "suspect"
    return "fail"


def summarize(verdict: str, model: str, source_label: str, signals: list[dict[str, Any]], accuracy: float | None) -> str:
    head = VERDICT_LABELS.get(verdict, verdict)
    parts = [head + ":模型 " + model + ",声称来源「" + source_label + "」。"]
    if accuracy is not None:
        parts.append("能力题正确率 " + str(round(accuracy * 100)) + "%。")
    for sig in signals:
        if sig.get("status") in {"fail", "warn"}:
            parts.append(str(sig.get("detail", "")))
    return " ".join(p for p in parts if p)[:600]


# --------------------------------------------------------------------------
# Record persistence
# --------------------------------------------------------------------------

AUTH_DIR = Path(os.environ.get("Yhs-ApiDetect_AUTHENTICITY_DIR", "/opt/Yhs-ApiDetect/web_data/authenticity"))


def _records_dir() -> Path:
    path = AUTH_DIR / "records"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record_id() -> str:
    return secrets.token_urlsafe(6)


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def save_record(record: dict[str, Any]) -> str:
    """Persist one redacted record. Never writes an API key."""
    record = dict(record)
    record.setdefault("record_id", _record_id())
    record.setdefault("time", _iso_now())
    record.pop("api_key", None)
    path = _records_dir() / (record["record_id"] + ".json")
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(record["record_id"])


def list_records(hours: int | None = 168, limit: int = 200) -> list[dict[str, Any]]:
    cutoff = time.time() - hours * 3600 if hours else None
    out: list[dict[str, Any]] = []
    for path in _records_dir().glob("*.json"):
        try:
            if cutoff is not None and path.stat().st_mtime < cutoff:
                continue
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda r: str(r.get("time", "")), reverse=True)
    return out[:limit]


def aggregate_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-family statistics.

    error records never enter any rate denominator; the completed count
    (authentic + suspect + fail) is the denominator, and the raw request total
    is reported alongside.
    """
    families: dict[str, dict[str, Any]] = {}
    for rec in records:
        family = str(rec.get("family", "unknown"))
        bucket = families.setdefault(
            family,
            {
                "family": family,
                "family_label": FAMILY_LABELS.get(family, family),
                "total": 0,
                "authentic": 0,
                "suspect": 0,
                "fail": 0,
                "error": 0,
                "completed": 0,
                "authentic_rate": None,
                "suspect_rate": None,
                "capability_accuracy_avg": None,
                "latency_avg_ms": None,
            },
        )
        bucket["total"] += 1
        verdict = str(rec.get("verdict", "error"))
        if verdict in {"authentic", "suspect", "fail"}:
            bucket[verdict] += 1
            bucket["completed"] += 1
        else:
            bucket["error"] += 1
        bucket.setdefault("_acc", []).append(rec.get("capability_accuracy"))
        bucket.setdefault("_lat", []).append(rec.get("latency_ms"))

    for bucket in families.values():
        completed = bucket["completed"]
        if completed:
            bucket["authentic_rate"] = round(bucket["authentic"] / completed, 4)
            bucket["suspect_rate"] = round(bucket["suspect"] / completed, 4)
        accs = [a for a in bucket.pop("_acc", []) if isinstance(a, (int, float))]
        lats = [l for l in bucket.pop("_lat", []) if isinstance(l, (int, float))]
        if accs:
            bucket["capability_accuracy_avg"] = round(sum(accs) / len(accs), 4)
        if lats:
            bucket["latency_avg_ms"] = int(sum(lats) / len(lats))

    totals = {
        "total": sum(b["total"] for b in families.values()),
        "authentic": sum(b["authentic"] for b in families.values()),
        "suspect": sum(b["suspect"] for b in families.values()),
        "fail": sum(b["fail"] for b in families.values()),
        "error": sum(b["error"] for b in families.values()),
    }
    totals["completed"] = totals["authentic"] + totals["suspect"] + totals["fail"]
    totals["authentic_rate"] = (
        round(totals["authentic"] / totals["completed"], 4) if totals["completed"] else None
    )
    totals["suspect_rate"] = (
        round(totals["suspect"] / totals["completed"], 4) if totals["completed"] else None
    )
    return {"by_family": sorted(families.values(), key=lambda b: str(b["family"])), "totals": totals}


CSV_COLUMNS = (
    "time", "channel_name", "base_url", "source", "model", "family", "wire_family",
    "verdict", "confidence", "score", "capability_accuracy", "latency_ms", "summary", "error_msg",
)


def export_csv(records: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        writer.writerow({k: rec.get(k, "") for k in CSV_COLUMNS})
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


async def run_target(
    base_url: str,
    api_key: str,
    model: str,
    claimed_source: str,
    channel_name: str = "",
    question_count: int = 3,
    error_probe: bool = True,
    cache_probe: bool = True,
    param_probe: bool = True,
    timeout: float = 45.0,
    client: httpx.AsyncClient | None = None,
    available_models: Iterable[str] | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Run the full probe suite for one target and return a record dict.

    Validation runs first and raises ConfigError — in that case nothing is
    probed and no record is written.
    """
    target = validate_target(model, claimed_source, available_models)
    wire = wire_for(target["wire_family"], base_url)
    model = target["model"]
    rng = rng or random.Random(secrets.randbits(32))

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True)

    started = time.perf_counter()
    signals: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    transport_error: str | None = None

    try:
        questions = build_capability_questions(question_count, rng)
        capability = await probe_capability(
            client, wire, api_key, model, questions, enable_thinking=True
        )
        trace.extend(capability.pop("trace", []))
        signals.append(capability)
        signals.append(
            signal_identity(
                str(capability.get("identity_reply", "")),
                target["family"],
                capability.get("thinking_signed"),
            )
        )
        signals.append(
            signal_thinking(
                capability.get("thinking_supported"),
                capability.get("thinking_signed"),
                target["wire_family"],
            )
        )
        accuracy = capability.get("accuracy")

        # Model echo is read off the identity round-trip's payload shape, which
        # we do not retain; re-probe cheaply with the first capability question.
        echo_result = await raw_post(
            client, wire.chat_url(model), wire.headers(api_key), wire.chat_body(model, "Reply with OK only.", max_tokens=8)
        )
        if echo_result.is_transport_error:
            transport_error = echo_result.transport_error
            signals.append(_signal("model_echo", "unavailable", None, "回显核对请求遇到传输级失败。"))
            trace.append(_trace("model_echo", "transport_error", echo_result.latency_ms, str(echo_result.transport_error)))
        else:
            signals.append(signal_model_echo(echo_result.body, model, target["wire_family"]))
            trace.append(_trace("model_echo", echo_result.status, echo_result.latency_ms, "核对响应 model 字段"))

        if error_probe:
            injected = await probe_error_injection(client, wire, api_key)
            probe_trace = injected.pop("trace", None)
            if probe_trace is not None:
                trace.append(probe_trace)
            signals.append(injected)
        else:
            signals.append(_signal("error_injection", "skip", None, "错误注入探针已关闭。"))

        if param_probe:
            params = await probe_params(client, wire, api_key, model)
            probe_trace = params.pop("trace", None)
            if probe_trace is not None:
                trace.append(probe_trace)
            signals.append(params)
        else:
            signals.append(_signal("parameter_validation", "skip", None, "参数校验探针已关闭。"))

        if cache_probe:
            cache = await probe_cache(client, wire, api_key, model)
            probe_trace = cache.pop("trace", None)
            if probe_trace is not None:
                trace.append(probe_trace)
            signals.append(cache)
        else:
            signals.append(_signal("cache", "skip", None, "缓存探针已关闭。"))
    finally:
        if owns_client:
            await client.aclose()

    latency_ms = int((time.perf_counter() - started) * 1000)
    score, confidence, _weights = score_signals(signals)
    identity_signal = next((s for s in signals if s.get("signal") == "identity"), {})
    conflict = identity_signal.get("self_reported_family") if identity_signal.get("status") == "fail" else None

    # A transport-level failure anywhere in the essentials makes the whole run
    # an "error" record rather than a fraud verdict.
    capability_status = next((s.get("status") for s in signals if s.get("signal") == "capability"), None)
    if transport_error is None and capability_status == "unavailable":
        failed = [
            t for t in trace if t.get("status") == "transport_error"
        ]
        if failed and len(failed) >= max(1, question_count):
            transport_error = str(failed[0].get("note", "transport error"))

    verdict = decide_verdict(score, signals, transport_error, conflict)
    accuracy_out = accuracy if isinstance(accuracy, (int, float)) else None

    record: dict[str, Any] = {
        "record_id": _record_id(),
        "channel_name": channel_name,
        "channel_id": channel_id_for(base_url),
        "base_url": (base_url or "").strip().rstrip("/"),
        "source": target["claimed_source"],
        "source_label": target["source_label"],
        "model": model,
        "family": target["family"],
        "wire_family": target["wire_family"],
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS.get(verdict, verdict),
        "confidence": confidence,
        "score": score,
        "summary": summarize(verdict, model, target["source_label"], signals, accuracy_out),
        "signals": signals,
        "capability_accuracy": accuracy_out,
        "probe_trace": trace,
        "latency_ms": latency_ms,
        "error_msg": transport_error,
        "time": _iso_now(),
    }
    return record


__all__ = [
    "FAMILY_LABELS",
    "FAMILY_TO_WIRE",
    "NONEXISTENT_MODEL",
    "SIGNAL_WEIGHTS",
    "SOURCES_BY_ID",
    "SOURCE_OPTIONS",
    "VERDICT_LABELS",
    "WIRE_LABELS",
    "AnthropicWire",
    "CapabilityQuestion",
    "ConfigError",
    "GeminiWire",
    "OpenAIWire",
    "WireHTTP",
    "aggregate_report",
    "build_capability_questions",
    "channel_id_for",
    "classify_error_shape",
    "decide_verdict",
    "export_csv",
    "family_from_model",
    "identity_contradicts",
    "identity_matches",
    "list_records",
    "pick_same_family_model",
    "probe_cache",
    "probe_capability",
    "probe_channel_models",
    "probe_error_injection",
    "probe_models",
    "probe_params",
    "raw_get",
    "raw_post",
    "redact_headers",
    "run_target",
    "save_record",
    "score_signals",
    "truncate",
    "validate_target",
    "wire_for",
]


async def probe_channel_models(
    base_url: str,
    api_key: str,
    wire_families: Iterable[str] = ("anthropic", "openai", "gemini"),
    timeout: float = 30.0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Read a relay's model list, trying each wire protocol until one answers.

    Needed to resolve targets whose model field was left blank: we must see the
    relay's own list before a same-family model can be picked.
    """
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True)
    attempts: list[dict[str, Any]] = []
    try:
        for wire_family in wire_families:
            try:
                wire = wire_for(wire_family, base_url)
            except ConfigError:
                continue
            models, result = await probe_models(client, wire, api_key)
            attempts.append(
                {
                    "wire_family": wire_family,
                    "status": result.status,
                    "transport_error": result.transport_error,
                    "count": len(models),
                }
            )
            if models:
                return {"ok": True, "wire_family": wire_family, "models": models, "attempts": attempts}
    finally:
        if owns_client:
            await client.aclose()
    return {"ok": False, "wire_family": None, "models": [], "attempts": attempts}


