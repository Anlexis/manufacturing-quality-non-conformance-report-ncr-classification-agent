"""AgentCore Platform v1.0 — MFG-C2-022 inner domain: RootCauseClassifyNode.

Inner domain node — ANONYMOUS trust (required; never VERIFIED_EXTERNAL/INTERNAL).
"""

from typing import Any, ClassVar, Dict, List, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

# 5M1E root-cause taxonomy (Ishikawa fishbone categories)
_ROOT_CAUSE_RULES: List[Tuple[str, List[str]]] = [
    (
        "Man",
        [
            "operator",
            "worker",
            "technician",
            "human error",
            "mistake",
            "training",
            "instruction",
            "skill",
            "knowledge",
            "fatigue",
            "inattention",
            "oversight",
            "manual",
            "personnel",
            "staff",
            "operator error",
            "human factor",
            "competency",
        ],
    ),
    (
        "Machine",
        [
            "machine",
            "equipment",
            "tool",
            "fixture",
            "jig",
            "calibration",
            "maintenance",
            "breakdown",
            "worn",
            "damaged",
            "malfunction",
            "tooling",
            "mold",
            "die",
            "press",
            "robot",
            "cnc",
            "machine error",
            "equipment failure",
            "fixture",
            "sensor",
        ],
    ),
    (
        "Material",
        [
            "material",
            "raw material",
            "component",
            "part",
            "supplier",
            "incoming",
            "incoming inspection",
            "material defect",
            "wrong material",
            "contamination",
            "composition",
            "alloy",
            "specification",
            "grade",
            "batch",
            "lot",
        ],
    ),
    (
        "Method",
        [
            "process",
            "procedure",
            "work instruction",
            "sop",
            "specification",
            "method",
            "standard",
            "parameter",
            "sequence",
            "flow",
            "process control",
            "control plan",
            "production method",
            "incorrect procedure",
            "wrong process",
            "setup",
        ],
    ),
    (
        "Measurement",
        [
            "inspection",
            "gauge",
            "test",
            "measurement",
            "sensor",
            "metrology",
            "calibration",
            "measuring instrument",
            "inspection error",
            "test failure",
            "measurement error",
            "gage",
            "cmm",
            "incorrect measurement",
            "inspection method",
        ],
    ),
    (
        "Environment",
        [
            "temperature",
            "humidity",
            "dust",
            "vibration",
            "environment",
            "contamination",
            "cleanliness",
            "esd",
            "static",
            "lighting",
            "workspace",
            "climate",
            "environmental",
            "ambient",
            "environmental condition",
            "shop floor",
        ],
    ),
]


def _classify_root_cause(text: str) -> Tuple[str, Dict[str, List[str]]]:
    """Score 5M1E categories against the report text; return category and evidence."""
    lower = text.lower()
    scores: Dict[str, float] = {}
    evidence: Dict[str, List[str]] = {}

    for category, keywords in _ROOT_CAUSE_RULES:
        matched = [kw for kw in keywords if kw in lower]
        hits = len(matched)
        if hits > 0:
            raw = min(hits / max(len(keywords) * 0.2, 1), 1.0)
            scores[category] = round(raw, 4)
            evidence[category] = matched[:5]  # keep at most 5 evidence items

    if not scores:
        return "Method", {"Method": ["no_keywords_matched"]}

    best = max(scores, key=lambda k: scores[k])
    return best, evidence


class RootCauseClassifyNode(FunctionNode):
    """Inner domain node — classifies the root cause using the 5M1E taxonomy.

    Categories: Man | Machine | Material | Method | Measurement | Environment.
    Uses keyword-density scoring; the matched-keyword evidence travels as a JSON
    string, because state fields holding structures must stay serialization-safe.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        ncr_text = state.get("validated_input") or state.get("user_input", "") or ""

        if not ncr_text.strip():
            emit_trace_event(
                "ncr_root_cause_error",
                {"reason": "empty_text"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["RootCauseClassifyNode: report text is empty"],
            }

        root_cause_category, evidence_dict = _classify_root_cause(ncr_text)
        root_cause_details = to_json(evidence_dict)

        emit_trace_event(
            "ncr_root_cause_classify",
            {"root_cause_category": root_cause_category},
            state,
        )

        return {
            "root_cause_category": root_cause_category,
            "root_cause_details": root_cause_details,
            "status": AgentStatus.SUCCESS.value,
        }
