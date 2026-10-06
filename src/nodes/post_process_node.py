"""AgentCore Platform v1.0 — MFG-C2-022 outer backbone: SecurityGateOutputNode.

The agent's output boundary. It checks the inner workflow's structured result
against the published output contract, renders the caller-facing report from the
values it has checked, and scans that report once more before anything leaves the
agent.

The published contract, in one line: every rendered field is a closed-set label,
an inert identifier, a whole count inside a declared range, or a rate between 0
and 1. No caller free text is rendered, and no value the contract does not name
is rendered at all.

Two properties of the framework decide the shape of this node:

  * An agent's caller-visible output resolves as `formatted_output or result`,
    with NO status check. Returning an error status is therefore not withholding
    anything; the fields that carry the answer have to be overwritten, and the
    replacement has to be truthy — an empty string re-opens the fallback it was
    written to close.
  * The mandatory output scan reads every value this node returns. Quoting a
    rejected value into an error message makes that scan raise, and the wrapper
    then discards this node's whole delta, clearing included. Reasons are fixed
    codes for that reason, not only for tidiness.
"""

import math
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

from src.schemas.state import from_json

# ── The published output contract ────────────────────────────────────────────

DEFECT_CATEGORIES = frozenset({"dimensional", "surface", "functional", "material", "process", "other"})
SEVERITY_LEVELS = frozenset({"critical", "major", "minor", "observation"})
SEVERITY_BASES = frozenset({"keyword_scoring", "defect_rate_escalation"})
ROOT_CAUSE_CATEGORIES = frozenset({"Man", "Machine", "Material", "Method", "Measurement", "Environment"})
ROUTING_TARGETS = frozenset(
    {
        "quality_engineering",
        "production",
        "supplier_quality",
        "manufacturing_engineering",
        "quality_assurance",
    }
)
ROUTING_PRIORITIES = frozenset({"urgent", "standard", "low"})

# The one caller-supplied string the report renders. Free text here would be
# caller-controlled output injection, so it is locked to an inert alphabet at the
# input boundary AND checked again here — the gate does not assume the boundary
# ran.
NCR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
NCR_ID_ABSENT = "unspecified"

MAX_UNIT_COUNT = 10_000_000

# Credential shapes the framework's own detector does not carry. The framework
# set is the FLOOR: a local set narrower than it is a containment bypass, because
# a value the framework catches and this node misses makes the framework's scan
# raise after execute() returns, and the wrapper then discards this node's whole
# delta — clearing included — leaving the un-gated record in state for the output
# resolver to fall back to. These entries are additive only.
_EXTRA_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("prefixed_key", re.compile(r"\b(?:pk|ak)-[A-Za-z0-9]{16,}")),
    (
        "credential_assignment",
        re.compile(
            r"\b(?:password|passwd|secret|api[_-]?key|token|access[_-]?key|private[_-]?key)" r"\s*[:=]\s*\S{8,}",
            re.IGNORECASE,
        ),
    ),
)

# Released in place of a withheld report. TRUTHY on purpose.
WITHHELD_NOTICE = (
    "The classification was withheld by the output gate and has not been released. "
    "No content from it is included in this response."
)

# Every state field of this agent that can carry released report content.
# Pinned so a test can assert the inventory, and so a new content-bearing field
# cannot join the state without a visible decision about whether the gate clears
# it. Inert provenance (status, node_history, trace ids) is deliberately absent.
OUTPUT_BEARING_FIELDS: List[str] = [
    "result",
    "classification_record",
    "validated_input",
    "enriched_context",
    "ncr_id",
    "affected_units",
    "lot_size",
    "defect_rate",
    "defect_category",
    "defect_confidence",
    "severity_level",
    "severity_score",
    "severity_basis",
    "root_cause_category",
    "root_cause_details",
    "routing_target",
    "routing_priority",
]


def _scan(text: str) -> Optional[str]:
    """Return the violation TYPE when *text* carries credential-shaped content.

    Only the type is returned — never the matched value.
    """
    findings = detect_credentials(text)
    if findings:
        return str(findings[0]["type"])
    for name, pattern in _EXTRA_PATTERNS:
        if pattern.search(text):
            return name
    return None


def _rate(value: Any) -> Optional[float]:
    """Parse *value* as a finite rate in [0.0, 1.0], else None."""
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
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return None
    return number


def _count(value: Any, low: int) -> Optional[int]:
    """Parse *value* as a whole count in [low, MAX_UNIT_COUNT], else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number != int(number):
        return None
    if not low <= int(number) <= MAX_UNIT_COUNT:
        return None
    return int(number)


def _closed(value: Any, allowed: "frozenset[str]") -> bool:
    return isinstance(value, str) and value in allowed


def _section(record: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Return one section of the record, or an empty mapping when it is not one.

    An absent or malformed section then fails the closed-set check below rather
    than raising, so a record that does not have the documented shape is withheld
    like any other contract violation.
    """
    value = record.get(key)
    return value if isinstance(value, dict) else {}


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class SecurityGateOutputNode(FunctionNode):
    """Outer backbone post_process node — validates, renders and gates the report."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def _withhold(self, reason: str, state: Dict[str, Any]) -> Dict[str, Any]:
        """Refuse the release: ERROR, every content-bearing field overwritten.

        The reason is a field name plus a fixed code. It never carries the value
        that failed.
        """
        emit_trace_event("ncr_output_withheld", {"reason": reason}, state)
        cleared: Dict[str, Any] = {field: None for field in OUTPUT_BEARING_FIELDS}
        cleared["result"] = ""
        cleared["formatted_output"] = WITHHELD_NOTICE
        cleared["status"] = AgentStatus.ERROR.value
        cleared["error_log"] = [f"SecurityGateOutputNode: output withheld ({reason})"]
        return cleared

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "formatted_output": message,
                "result": message,
            }
        # The inner workflow's result reaches outer state under this key, written
        # by NCRWorkflowGraphNode.merge_output(). Reading a key the merge does not
        # write would compare against nothing on every real invocation, so the
        # absent case is a refusal rather than a set of rendered defaults.
        record_raw = state.get("classification_record")
        if not isinstance(record_raw, str) or not record_raw.strip():
            return self._withhold("classification_record:missing", state)

        parsed = from_json(record_raw, None)
        if not isinstance(parsed, dict):
            return self._withhold("classification_record:not_an_object", state)
        record: Dict[str, Any] = parsed

        defect = _section(record, "defect")
        severity = _section(record, "severity")
        lot = _section(record, "lot")
        root_cause = _section(record, "root_cause")
        routing = _section(record, "routing")

        ncr_id = record.get("ncr_id")
        if ncr_id != NCR_ID_ABSENT and not (isinstance(ncr_id, str) and NCR_ID_RE.match(ncr_id)):
            return self._withhold("ncr_id:not_an_identifier", state)

        checks: List[Tuple[str, bool]] = [
            ("defect.category", _closed(defect.get("category"), DEFECT_CATEGORIES)),
            ("severity.level", _closed(severity.get("level"), SEVERITY_LEVELS)),
            ("severity.basis", _closed(severity.get("basis"), SEVERITY_BASES)),
            ("root_cause.category", _closed(root_cause.get("category_5m1e"), ROOT_CAUSE_CATEGORIES)),
            ("routing.target", _closed(routing.get("target"), ROUTING_TARGETS)),
            ("routing.priority", _closed(routing.get("priority"), ROUTING_PRIORITIES)),
        ]
        for field, ok in checks:
            if not ok:
                return self._withhold(f"{field}:not_in_closed_set", state)

        confidence = _rate(defect.get("confidence"))
        if confidence is None:
            return self._withhold("defect.confidence:out_of_range", state)
        score = _rate(severity.get("score"))
        if score is None:
            return self._withhold("severity.score:out_of_range", state)

        affected_units: Optional[int] = None
        lot_size: Optional[int] = None
        defect_rate: Optional[float] = None
        if lot.get("affected_units") is not None:
            affected_units = _count(lot.get("affected_units"), 0)
            if affected_units is None:
                return self._withhold("lot.affected_units:out_of_range", state)
        if lot.get("lot_size") is not None:
            lot_size = _count(lot.get("lot_size"), 1)
            if lot_size is None:
                return self._withhold("lot.lot_size:out_of_range", state)
        if lot.get("defect_rate") is not None:
            defect_rate = _rate(lot.get("defect_rate"))
            if defect_rate is None:
                return self._withhold("lot.defect_rate:out_of_range", state)

        lines = [
            "=== NCR Classification Report ===",
            f"NCR Reference    : {ncr_id}",
            f"Defect Category  : {str(defect['category']).upper()} (confidence: {confidence:.2f})",
            f"Severity Level   : {str(severity['level']).upper()} (score: {score:.2f})",
            f"Severity Basis   : {severity['basis']}",
        ]
        if affected_units is not None and lot_size is not None and defect_rate is not None:
            lines.append(
                f"Lot Impact       : {affected_units} of {lot_size} units " f"(defect rate: {defect_rate:.2f})"
            )
        lines += [
            f"Root Cause (5M1E): {root_cause['category_5m1e'].upper()}",
            f"Routing Target   : {routing['target']}",
            f"Routing Priority : {str(routing['priority']).upper()}",
            "=================================",
        ]
        formatted = "\n".join(lines)

        # Independent of the contract checks above: a value can satisfy every
        # shape rule and still be a credential, and the closed sets could be
        # widened by a future change without this scan being revisited.
        violation = _scan(formatted)
        if violation:
            return self._withhold(f"report:credential_shape:{violation}", state)

        emit_trace_event(
            "ncr_output_released",
            {
                "defect_category": defect["category"],
                "severity_level": severity["level"],
                "severity_basis": severity["basis"],
                "routing_target": routing["target"],
                "lot_data_rendered": defect_rate is not None,
            },
            state,
        )

        return {
            "formatted_output": formatted,
            "result": formatted,
            "status": AgentStatus.SUCCESS.value,
        }


def render_contract() -> Dict[str, Any]:
    """The published output contract, as data.

    Exposed so the documentation, the tests and the gate cannot drift apart:
    each is a reader of this one definition rather than a separate copy of it.
    """
    return {
        "defect.category": sorted(DEFECT_CATEGORIES),
        "severity.level": sorted(SEVERITY_LEVELS),
        "severity.basis": sorted(SEVERITY_BASES),
        "root_cause.category": sorted(ROOT_CAUSE_CATEGORIES),
        "routing.target": sorted(ROUTING_TARGETS),
        "routing.priority": sorted(ROUTING_PRIORITIES),
        "ncr_id": NCR_ID_RE.pattern,
        "rates": "finite, 0.0-1.0, rendered to two decimal places",
        "counts": f"whole numbers, 0-{MAX_UNIT_COUNT}",
    }
