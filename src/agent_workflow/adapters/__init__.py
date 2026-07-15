"""Versioned contracts for structured capability adapters."""

from .contracts import build_adapter_request, validate_adapter_request, validate_adapter_result

__all__ = ["build_adapter_request", "validate_adapter_request", "validate_adapter_result"]
