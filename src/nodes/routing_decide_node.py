"""AgentCore Platform v1.0 — MFG-C2-022 inner domain: RoutingDecideNode.

Inner domain node — ANONYMOUS trust (required; never VERIFIED_EXTERNAL/INTERNAL).
"""

from typing import Any, ClassVar, Dict, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

# Routing rules: (severity_level, defect_category) → (routing_target, routing_priority)
# More specific rules (severity+category) take precedence over severity-only defaults.
_ROUTING_MATRIX: Dict[Tuple[str, str], Tuple[str, str]] = {
    # Critical — always quality engineering, urgent
    ("critical", "dimensional"): ("quality_engineering", "urgent"),
    ("critical", "surface"): ("quality_engineering", "urgent"),
    ("critical", "functional"): ("quality_engineering", "urgent"),
    ("critical", "material"): ("quality_engineering", "urgent"),
    ("critical", "process"): ("quality_engineering", "urgent"),
    ("critical", "other"): ("quality_engineering", "urgent"),
    # Major — quality engineering or specialist team
    ("major", "functional"): ("quality_engineering", "standard"),
    ("major", "material"): ("supplier_quality", "standard"),
    ("major", "process"): ("manufacturing_engineering", "standard"),
    ("major", "dimensional"): ("quality_engineering", "standard"),
    ("major", "surface"): ("quality_engineering", "standard"),
    ("major", "other"): ("quality_engineering", "standard"),
    # Minor — production team or specialist
    ("minor", "surface"): ("production", "standard"),
    ("minor", "dimensional"): ("quality_engineering", "standard"),
    ("minor", "functional"): ("quality_engineering", "standard"),
    ("minor", "material"): ("supplier_quality", "standard"),
    ("minor", "process"): ("manufacturing_engineering", "low"),
    ("minor", "other"): ("production", "low"),
    # Observation — routed to the QA team for trend monitoring
    ("observation", "dimensional"): ("quality_assurance", "low"),
    ("observation", "surface"): ("quality_assurance", "low"),
    ("observation", "functional"): ("quality_assurance", "low"),
    ("observation", "material"): ("quality_assurance", "low"),
    ("observation", "process"): ("quality_assurance", "low"),
    ("observation", "other"): ("quality_assurance", "low"),
}

# Severity-only fallback (used when defect_category is unknown or "other")
_SEVERITY_FALLBACK: Dict[str, Tuple[str, str]] = {
    "critical": ("quality_engineering", "urgent"),
    "major": ("quality_engineering", "standard"),
    "minor": ("production", "standard"),
    "observation": ("quality_assurance", "low"),
}


def _decide_routing(severity_level: str, defect_category: str) -> Tuple[str, str]:
    """Lookup routing target and priority from the decision matrix."""
    key = (severity_level, defect_category)
    if key in _ROUTING_MATRIX:
        return _ROUTING_MATRIX[key]
    return _SEVERITY_FALLBACK.get(severity_level, ("quality_assurance", "standard"))


class RoutingDecideNode(FunctionNode):
    """Inner domain node — determines routing target and priority.

    Uses a severity × defect-category decision matrix.
    Routing targets: quality_engineering | production | supplier_quality |
                     manufacturing_engineering | quality_assurance.
    Priority: urgent | standard | low.

    Both outputs are drawn from the matrix above and never from caller text: the
    published output contract states that every routing field is one of these
    closed-set values, and the output gate enforces it.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        severity_level = state.get("severity_level") or "minor"
        defect_category = state.get("defect_category") or "other"

        routing_target, routing_priority = _decide_routing(severity_level, defect_category)

        emit_trace_event(
            "ncr_routing_decide",
            {
                "severity_level": severity_level,
                "defect_category": defect_category,
                "routing_target": routing_target,
                "routing_priority": routing_priority,
            },
            state,
        )

        return {
            "routing_target": routing_target,
            "routing_priority": routing_priority,
            "status": AgentStatus.SUCCESS.value,
        }
