"""AgentCore Platform v1.0 — MFG-C2-022 inner domain: InputValidateNode.

Inner domain node — ANONYMOUS trust (required; never VERIFIED_EXTERNAL/INTERNAL).
The framework passes the outer InvocationContext into the subgraph unchanged, so
the effective caller trust is whatever the outer gate admitted. ANONYMOUS here
means an inner node never re-gates a caller the outer boundary already accepted.
"""

from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_MIN_LEN = 10


class InputValidateNode(FunctionNode):
    """Confirm the report text survived the boundary before the pipeline runs.

    Reads the text from `validated_input` (written by the outer gate) or falls
    back to `user_input`, the string the framework seeds into the inner graph's
    initial state. A run that reaches here with nothing usable is a wiring fault,
    not a caller error, and stops the pipeline rather than classifying an empty
    document into a confident-looking category.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        ncr_text = state.get("validated_input") or state.get("user_input", "") or ""
        ncr_text = ncr_text.strip()

        if len(ncr_text) < _MIN_LEN:
            emit_trace_event(
                "ncr_inner_validate_error",
                {"reason": "text_too_short", "length": len(ncr_text)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InputValidateNode: report text did not reach the pipeline"],
            }

        emit_trace_event(
            "ncr_inner_validate",
            {
                "text_length": len(ncr_text),
                "lot_data_present": state.get("lot_size") is not None,
            },
            state,
        )

        return {
            "validated_input": ncr_text,
            "status": AgentStatus.SUCCESS.value,
        }
