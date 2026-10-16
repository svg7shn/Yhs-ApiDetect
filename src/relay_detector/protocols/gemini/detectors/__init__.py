"""Gemini detector registry."""

from .base import ActiveDetector, BaseDetector, PassiveDetector
from .basic_request import BasicRequestDetector
from .function_calling import FunctionCallingDetector
from .integrity import IntegrityDetector
from .model_info import ModelInfoDetector
from .protocol import ProtocolDetector
from .structured_output import StructuredOutputDetector
from .token_usage import TokenUsageDetector


def build_all() -> list[BaseDetector]:
    return [
        BasicRequestDetector(),
        ModelInfoDetector(),
        FunctionCallingDetector(),
        StructuredOutputDetector(),
        ProtocolDetector(),
        IntegrityDetector(),
        TokenUsageDetector(),
    ]


__all__ = [
    "ActiveDetector",
    "BaseDetector",
    "BasicRequestDetector",
    "FunctionCallingDetector",
    "IntegrityDetector",
    "ModelInfoDetector",
    "PassiveDetector",
    "ProtocolDetector",
    "StructuredOutputDetector",
    "TokenUsageDetector",
    "build_all",
]
