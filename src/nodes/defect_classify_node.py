"""AgentCore Platform v1.0 — MFG-C2-022 inner domain: DefectClassifyNode.

Inner domain node — ANONYMOUS trust (required; never VERIFIED_EXTERNAL/INTERNAL).
"""

from typing import Any, ClassVar, Dict, List, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

# Defect classification rules: category → keyword patterns (lowercase)
_DEFECT_RULES: List[Tuple[str, List[str]]] = [
    (
        "dimensional",
        [
            "tolerance",
            "dimension",
            "diameter",
            "size",
            "length",
            "width",
            "thickness",
            "gap",
            "clearance",
            "out of spec",
            "oversize",
            "undersize",
            "out-of-spec",
            "measurement",
            "offset",
            "warpage",
            "warped",
            "bow",
            "camber",
        ],
    ),
    (
        "surface",
        [
            "scratch",
            "burr",
            "rust",
            "corrosion",
            "crack",
            "dent",
            "paint",
            "coating",
            "finish",
            "pitting",
            "delamination",
            "blister",
            "stain",
            "discoloration",
            "surface defect",
            "visual defect",
            "cosmetic",
            "mark",
            "nick",
        ],
    ),
    (
        "functional",
        [
            "failure",
            "malfunction",
            "does not work",
            "broken",
            "defective",
            "not function",
            "cannot operate",
            "incorrect operation",
            "no output",
            "stuck",
            "intermittent",
            "electrical failure",
            "short circuit",
            "open circuit",
            "performance",
        ],
    ),
    (
        "material",
        [
            "wrong material",
            "contamination",
            "composition",
            "hardness",
            "tensile",
            "yield strength",
            "material spec",
            "chemical",
            "alloy",
            "grade",
            "impurity",
            "foreign material",
            "foreign object",
            "inclusion",
        ],
    ),
    (
        "process",
        [
            "assembly",
            "alignment",
            "torque",
            "weld",
            "solder",
            "crimp",
            "press",
            "heat treat",
            "plating",
            "anodize",
            "machining",
            "drilling",
            "process error",
            "procedure",
            "sequence",
            "misassembled",
            "missing part",
            "wrong part",
        ],
    ),
]

_CATEGORY_WEIGHTS: Dict[str, float] = {
    "dimensional": 1.0,
    "surface": 1.0,
    "functional": 1.2,  # functional failures weighted slightly higher (safety impact)
    "material": 1.1,
    "process": 1.0,
}


def _classify_defect(text: str) -> Tuple[str, float]:
    """Score defect categories against the report text; return category and confidence."""
    lower = text.lower()
    scores: Dict[str, float] = {cat: 0.0 for cat, _ in _DEFECT_RULES}

    for category, keywords in _DEFECT_RULES:
        hits = sum(1 for kw in keywords if kw in lower)
        if hits > 0:
            # Confidence: hits / total keywords, capped at 1.0, weighted by category
            raw = min(hits / max(len(keywords) * 0.2, 1), 1.0)
            scores[category] = raw * _CATEGORY_WEIGHTS.get(category, 1.0)

    if not any(v > 0 for v in scores.values()):
        return "other", 0.3

    best = max(scores, key=lambda k: scores[k])
    confidence = round(min(scores[best], 1.0), 4)
    return best, confidence


class DefectClassifyNode(FunctionNode):
    """Inner domain node — classifies the nonconformance defect type.

    Categories: dimensional | surface | functional | material | process | other.
    Uses keyword-density scoring over the validated report text.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        ncr_text = state.get("validated_input") or state.get("user_input", "") or ""

        if not ncr_text.strip():
            emit_trace_event(
                "ncr_defect_classify_error",
                {"reason": "empty_text"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["DefectClassifyNode: report text is empty"],
            }

        category, confidence = _classify_defect(ncr_text)

        emit_trace_event(
            "ncr_defect_classify",
            {"defect_category": category, "defect_confidence": confidence},
            state,
        )

        return {
            "defect_category": category,
            "defect_confidence": str(confidence),
            "status": AgentStatus.SUCCESS.value,
        }
