"""Compatibility shim for Anthropic baseline comparison."""

from .protocols.anthropic.comparator import *
from .protocols.anthropic.comparator import _compare_one  # noqa: F401
