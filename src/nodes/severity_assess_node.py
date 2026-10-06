"""AgentCore Platform v1.0 — MFG-C2-022 inner domain: SeverityAssessNode.

Inner domain node — ANONYMOUS trust (required; never VERIFIED_EXTERNAL/INTERNAL).
"""

import math
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

# Severity scoring keywords, ordered from most to least severe.
_SEVERITY_RULES: List[Tuple[str, List[str], float]] = [
    # (level, keywords, base_score)
    (
        "critical",
        [
            "safety",
            "injury",
            "recall",
            "regulatory",
            "non-functional",
            "all units",
            "life threatening",
            "fire",
            "explosion",
            "chemical hazard",
            "electrical shock",
            "structural failure",
            "critical failure",
            "immediate action",
            "stop shipment",
            "iso 14001",
            "osha",
            "regulatory requirement",
        ],
        0.9,
    ),
    (
        "major",
        [
            "significant",
            "cannot operate",
            "large quantity",
            "shipment hold",
            "production stop",
            "100%",
            "entire lot",
            "systematic",
            "repeated",
            "customer complaint",
            "return",
            "warranty",
            "customer hold",
            "high volume",
            "production line",
        ],
        0.7,
    ),
    (
        "minor",
        [
            "cosmetic",
            "small quantity",
            "workaround",
            "limited",
            "few units",
            "isolated",
            "infrequent",
            "occasional",
            "minor defect",
            "rework",
            "repair",
            "acceptable",
            "documentation",
        ],
        0.4,
    ),
    (
        "observation",
        [
            "potential",
            "monitoring",
            "trend",
            "observation",
            "preventive",
            "improvement",
            "suggestion",
            "recommend",
            "consider",
            "opportunity",
        ],
        0.2,
    ),
]

# Terms that escalate to critical outright when the report asserts them.
_CRITICAL_OVERRIDE_TERMS = ("safety", "injury", "regulatory", "recall")

# A nonconformance report states what was ruled out as often as what was found:
# "no safety or functional impact" is the standard sign-off on a cosmetic defect.
# Read as a bare keyword hit, that sentence escalates a scratch to critical and
# routes it to urgent — measured on this agent's own reference report. An
# occurrence only counts when it is not inside a negated clause.
_NEGATION_RE = re.compile(
    r"\b(?:no|not|non|without|nil|zero|never|free\s+of|absent)\b[^.;\n]{0,32}$",
    re.IGNORECASE,
)


def _asserted(text_lower: str, term: str) -> bool:
    """True when *term* appears in *text_lower* outside a negated clause."""
    for match in re.finditer(re.escape(term), text_lower):
        preceding = text_lower[max(0, match.start() - 48) : match.start()]
        if not _NEGATION_RE.search(preceding):
            return True
    return False


def _assess_severity(text: str, defect_category: str) -> Tuple[str, float]:
    """Score severity levels from the report text; functional defects escalate."""
    lower = text.lower()
    scores: Dict[str, float] = {level: 0.0 for level, _, _ in _SEVERITY_RULES}

    for level, keywords, base in _SEVERITY_RULES:
        hits = sum(1 for kw in keywords if _asserted(lower, kw))
        if hits > 0:
            raw = min(base + (hits / max(len(keywords) * 0.3, 1)) * (1.0 - base), 1.0)
            scores[level] = round(raw, 4)

    if not any(v > 0 for v in scores.values()):
        if defect_category == "functional":
            return "major", 0.6
        return "minor", 0.3

    if any(_asserted(lower, term) for term in _CRITICAL_OVERRIDE_TERMS):
        return "critical", max(scores.get("critical", 0.0), 0.9)

    best = max(scores, key=lambda k: scores[k])
    return best, scores[best]


def _finite(value: Any) -> Optional[float]:
    """Parse *value* as a finite float, else None. Rejects bools and NaN/Inf."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    return number if math.isfinite(number) else None


class SeverityAssessNode(FunctionNode):
    """Assess the severity of a nonconformance.

    Two independent signals. The report text is scored against the keyword model
    above; when the caller supplied lot figures, the measured defect rate is
    compared against the escalation threshold. Whichever is more severe wins, and
    the basis is reported so a reader can tell a text judgement from a measured
    one.

    Severity levels: critical | major | minor | observation.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        ncr_text = state.get("validated_input") or state.get("user_input", "") or ""
        defect_category = state.get("defect_category") or "other"

        if not ncr_text.strip():
            emit_trace_event("ncr_severity_assess_error", {"reason": "empty_text"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["SeverityAssessNode: report text is empty"],
            }

        severity_level, severity_score = _assess_severity(ncr_text, defect_category)
        severity_basis = "keyword_scoring"

        affected_units = state.get("affected_units")
        lot_size = state.get("lot_size")
        defect_rate: Optional[float] = None
        if isinstance(affected_units, int) and isinstance(lot_size, int) and lot_size > 0:
            defect_rate = round(affected_units / lot_size, 4)

        raw_threshold = state.get("critical_defect_rate")
        threshold: Optional[float] = None
        if raw_threshold is not None:
            threshold = _finite(raw_threshold)
            if threshold is None or not 0.0 <= threshold <= 1.0:
                # Fail CLOSED: a threshold that cannot be compared would disable
                # the escalation silently, and every comparison against NaN is
                # False — the pipeline stops instead.
                emit_trace_event(
                    "ncr_severity_assess_error",
                    {"reason": "threshold_not_finite"},
                    state,
                )
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        "SeverityAssessNode: escalation threshold rejected " "(critical_defect_rate:not_finite)"
                    ],
                }

        if defect_rate is not None and threshold is not None and defect_rate >= threshold:
            if severity_level != "critical":
                severity_basis = "defect_rate_escalation"
                severity_level = "critical"
                severity_score = max(severity_score, 0.9)

        emit_trace_event(
            "ncr_severity_assess",
            {
                "severity_level": severity_level,
                "severity_score": severity_score,
                "severity_basis": severity_basis,
                "defect_rate": defect_rate,
            },
            state,
        )

        return {
            "severity_level": severity_level,
            "severity_score": str(round(severity_score, 4)),
            "severity_basis": severity_basis,
            "defect_rate": None if defect_rate is None else str(defect_rate),
            "status": AgentStatus.SUCCESS.value,
        }
