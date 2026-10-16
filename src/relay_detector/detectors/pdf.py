"""Compatibility shim for Anthropic PDFDetector."""

from relay_detector.protocols.anthropic.detectors.pdf import *
from relay_detector.protocols.anthropic.detectors.pdf import _load_pdf_b64  # noqa: F401
