"""AgentCore Platform v1.0 — MFG-C2-022 outer → inner caller-payload hand-off."""

# Why this module exists:
#
# The framework's GraphNode invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)`. It does not forward the
# outer state's input_context, and it cannot — the inner graph builds a fresh
# initial state of its own. Without a bridge, every inner-node read of a caller
# parameter sees nothing at all, through the whole nested graph, while unit tests
# that hand a node a populated state keep passing. The failure is silent: the
# node falls back to a default, the report says success, and the caller's
# numbers were never used.
#
# The two sanctioned subclass hooks carry it across:
#
#   NCRWorkflowGraphNode.extract_input(state)
#       [runs BEFORE subgraph.invoke]  -> set_caller_payload(...)
#   NCRDomainWorkflowGraph._extra_initial_state()
#       [runs INSIDE subgraph.invoke]  -> returns the stashed payload
#
# EVERY validated caller field the inner graph reads has to travel here. A field
# left behind is not a compile error and not a test failure.
#
# What crosses is the VALIDATED payload the outer ValidateInputNode produced —
# never the raw input_context. Caller data is bounded exactly once, upstream of
# this bridge.
#
# A ContextVar keeps the hand-off correct per thread and per task, so concurrent
# invocations in one process cannot see each other's caller data.

from contextvars import ContextVar
from typing import Any, Dict, Optional

_CALLER_PAYLOAD: ContextVar[Optional[Dict[str, Any]]] = ContextVar("mfg_c2_022_caller_payload", default=None)

# The keys the inner graph seeds from this bridge. Pinned so a test can assert
# the set, and so adding an inner-graph reader without extending the bridge is a
# visible change rather than a silent default.
CALLER_PAYLOAD_KEYS = ("ncr_id", "affected_units", "lot_size", "critical_defect_rate")


def set_caller_payload(payload: Optional[Dict[str, Any]]) -> None:
    """Stash the validated caller payload for the imminent inner-graph invoke."""
    if not payload:
        _CALLER_PAYLOAD.set({})
        return
    _CALLER_PAYLOAD.set({k: payload[k] for k in CALLER_PAYLOAD_KEYS if payload.get(k) is not None})


def get_caller_payload() -> Dict[str, Any]:
    """Read (without consuming) the stashed payload; empty when none was set."""
    return dict(_CALLER_PAYLOAD.get() or {})


def clear_caller_payload() -> None:
    """Drop the stashed payload. Used by tests to assert the absent-data path."""
    _CALLER_PAYLOAD.set(None)
