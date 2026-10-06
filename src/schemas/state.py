"""AgentCore Platform v1.0 — MFG-C2-022 NCR Classification Agent state."""

# State must be a flat TypedDict — never a Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization, and Pydantic objects corrupt silently
# there. Extend AgentState with agent-specific fields only. Never add
# credentials, secrets, or Pydantic models.
#
# Serialization rule: fields holding dicts or lists are typed Optional[str] and
# travel through to_json() / from_json(). The producer writes to_json(value);
# the consumer reads from_json(state.get("field"), default).

import json
from typing import Optional

from framework.schemas.agent_state import AgentState


def to_json(value: object) -> Optional[str]:
    """Serialize a dict or list to a JSON string for state storage."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: object = None) -> object:
    """Deserialize a JSON string from state back to a dict or list."""
    if value is None:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


class State(AgentState):
    """Agent state for MFG-C2-022 NCR Classification Agent.

    All shared fields (user_input, input_context, status, session_id,
    node_history, error_log, hitl_*, etc.) are inherited from AgentState.

    NCR-specific domain fields are appended below. Dict/list fields are typed
    Optional[str] and JSON-serialized.
    """

    # ── Outer backbone fields ─────────────────────────────────────────────────
    validated_input: Optional[str]  # sanitised NCR text from ValidateInputNode
    enriched_context: Optional[str]  # JSON — source/channel provenance

    # ── Validated caller parameters ──────────────────────────────────────────
    # Written by ValidateInputNode from input_context; carried into the inner
    # graph by the context bridge. Absent when the caller supplied none.
    ncr_id: Optional[str]  # inert identifier, rendered in the report
    affected_units: Optional[int]  # units found nonconforming
    lot_size: Optional[int]  # units in the inspected lot
    critical_defect_rate: Optional[str]  # caller threshold override, 0.0-1.0

    # ── NCR classification domain fields ─────────────────────────────────────
    defect_category: Optional[str]  # dimensional | surface | functional |
    #   material | process | other
    defect_confidence: Optional[str]  # confidence score string (0.0–1.0)
    severity_level: Optional[str]  # critical | major | minor | observation
    severity_score: Optional[str]  # numeric severity score string (0.0–1.0)
    severity_basis: Optional[str]  # keyword_scoring | defect_rate_escalation
    defect_rate: Optional[str]  # affected_units / lot_size, 0.0–1.0
    root_cause_category: Optional[str]  # Man | Machine | Material | Method |
    #   Measurement | Environment
    root_cause_details: Optional[str]  # JSON — matched keyword evidence

    # ── Routing output fields ─────────────────────────────────────────────────
    routing_target: Optional[str]  # quality_engineering | production |
    #   supplier_quality |
    #   manufacturing_engineering |
    #   quality_assurance
    routing_priority: Optional[str]  # urgent | standard | low

    # ── Output fields ─────────────────────────────────────────────────────────
    # classification_record is the inner workflow's structured result, carried
    # into outer state for the output gate to validate. It is deliberately NOT
    # called `result`: the framework resolves an agent's caller-visible output
    # as `formatted_output or result` with no status check, so anything parked
    # in `result` before the gate runs is releasable even on an error path.
    # Only the gate writes `result`.
    classification_record: Optional[str]  # JSON — inner workflow result
    formatted_output: Optional[str]  # human-readable structured report
    result: Optional[str]  # gated report; written by the gate only
    error_code: Optional[str]
