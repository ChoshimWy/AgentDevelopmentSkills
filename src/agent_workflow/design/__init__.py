"""Platform-neutral design evidence, gateway and compiler contracts."""

from .compiler import compile_canonical_ir, compile_design_system_registry, slice_agent_packet, validate_packet_freshness
from .contracts import (
    design_fingerprint,
    validate_canonical_ui_ir,
    validate_design_agent_packet,
    validate_design_evidence,
    validate_design_source_request,
    validate_design_system_registry,
    validate_ui_validation_report,
)
from .gateway import DesignSourceGateway, WriteApproval
from .validation import build_ui_validation_report

__all__ = [
    "DesignSourceGateway",
    "WriteApproval",
    "compile_canonical_ir",
    "compile_design_system_registry",
    "build_ui_validation_report",
    "design_fingerprint",
    "slice_agent_packet",
    "validate_packet_freshness",
    "validate_canonical_ui_ir",
    "validate_design_agent_packet",
    "validate_design_evidence",
    "validate_design_source_request",
    "validate_design_system_registry",
    "validate_ui_validation_report",
]
