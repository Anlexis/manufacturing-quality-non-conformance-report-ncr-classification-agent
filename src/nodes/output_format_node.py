"""AgentCore Platform v1.0 — MFG-C2-022 inner domain: OutputFormatNode.

Inner domain node — ANONYMOUS trust (required; never VERIFIED_EXTERNAL/INTERNAL).
"""

import json
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

_PRIORITY_LABELS = {"urgent": "[URGENT]", "standard": "[STANDARD]", "low": "[LOW]"}


def _priority_label(priority: str) -> str:
    return _PRIORITY_LABELS.get(priority, "[STANDARD]")


class OutputFormatNode(FunctionNode):
    """Assemble the classification into a structured record.

    The record is the inner workflow's result. It is handed to the outer output
    gate, which checks every field against the published output contract before
    anything is rendered for a caller — this node does not decide what is
    releasable.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        defect_category = state.get("defect_category") or "other"
        defect_confidence = state.get("defect_confidence") or "0.0"
        severity_level = state.get("severity_level") or "observation"
        severity_score = state.get("severity_score") or "0.0"
        severity_basis = state.get("severity_basis") or "keyword_scoring"
        root_cause_category = state.get("root_cause_category") or "Method"
        root_cause_details_raw: Optional[str] = state.get("root_cause_details")
        routing_target = state.get("routing_target") or "quality_assurance"
        routing_priority = state.get("routing_priority") or "standard"

        root_cause_details: Any = from_json(root_cause_details_raw, {})

        record: Dict[str, Any] = {
            "ncr_id": state.get("ncr_id") or "unspecified",
            "defect": {
                "category": defect_category,
                "confidence": defect_confidence,
            },
            "severity": {
                "level": severity_level,
                "score": severity_score,
                "basis": severity_basis,
            },
            "lot": {
                "affected_units": state.get("affected_units"),
                "lot_size": state.get("lot_size"),
                "defect_rate": state.get("defect_rate"),
            },
            "root_cause": {
                "category_5m1e": root_cause_category,
                "evidence": root_cause_details,
            },
            "routing": {
                "target": routing_target,
                "priority": routing_priority,
                "priority_label": _priority_label(routing_priority),
            },
        }

        formatted_output = json.dumps(record, ensure_ascii=False, indent=2)

        emit_trace_event(
            "ncr_output_format_inner",
            {
                "defect_category": defect_category,
                "severity_level": severity_level,
                "severity_basis": severity_basis,
                "root_cause_category": root_cause_category,
                "routing_target": routing_target,
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
