"""OpenAI protocol templates and validators.

This package is intentionally separate from the Anthropic/Claude detector
pipeline. OpenAI-compatible relays expose different wire APIs and need their
own protocol baselines.
"""

from .baseline import (
    build_openai_baseline_probes,
    collect_openai_official_baseline,
    extract_openai_features,
    sanitize_openai_headers,
    summarize_openai_baseline,
)
from .client import DEFAULT_OPENAI_BASE_URL, OpenAIAPIError, OpenAIClient
from .protocol_templates import (
    CHAT_COMPLETIONS_TEMPLATE,
    RESPONSES_TEMPLATE,
    ProtocolIssue,
    ProtocolTemplate,
    TemplateValidation,
    validate_chat_completion,
    validate_openai_payload,
    validate_responses_api,
)

__all__ = [
    "CHAT_COMPLETIONS_TEMPLATE",
    "DEFAULT_OPENAI_BASE_URL",
    "RESPONSES_TEMPLATE",
    "OpenAIAPIError",
    "OpenAIClient",
    "ProtocolIssue",
    "ProtocolTemplate",
    "TemplateValidation",
    "build_openai_baseline_probes",
    "collect_openai_official_baseline",
    "extract_openai_features",
    "sanitize_openai_headers",
    "summarize_openai_baseline",
    "validate_chat_completion",
    "validate_openai_payload",
    "validate_responses_api",
]
