"""Compatibility shim for the Anthropic client."""

from .protocols.anthropic.client import *
from .protocols.anthropic.client import _normalize_base_url, _parse_sse  # noqa: F401
