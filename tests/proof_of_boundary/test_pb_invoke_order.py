"""PB — backbone invoke execution order.

A full Graph().invoke() over a SUCCESS-yielding payload must traverse the five
backbone slots in order:
  InitializeNode → ValidateInputNode → NCRWorkflowGraphNode
                 → SecurityGateOutputNode → FinalizeNode

Trust rule: the context is built with a VERIFIED_EXTERNAL caller, never an
internal one. An internal context always clears every trust check, which hides
the case the deployment actually runs.

Status assertions compare against AgentStatus.SUCCESS.value: invoke() returns the
serialized output mapping, where enum members are already their string values.

Output assertions read result["output"]. invoke() returns the shaped output
mapping, not the raw state, so formatted_output is not a key here.
"""

import importlib

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.services.failure_message import EMPTY_INPUT

_MAIN_SLOT_NODE = "NCRWorkflowGraphNode"

# The reference payload. Kept byte-identical to deploy/invoke_payload.json so the
# deployment smoke test and this test exercise the same request.
_VALID_PAYLOAD = (
    "Surface scratch found on part B234 painted coating during final inspection. "
    "Cosmetic defect only. Three units affected. No safety or functional impact. "
    "Rework possible at production."
)

_NODE_MODULES = [
    "src.nodes.pre_process_node",
    "src.nodes.post_process_node",
    "src.nodes.input_validate_node",
    "src.nodes.defect_classify_node",
    "src.nodes.severity_assess_node",
    "src.nodes.root_cause_classify_node",
    "src.nodes.routing_decide_node",
    "src.nodes.output_format_node",
]


def _mute_emit(monkeypatch):
    for mod_path in _NODE_MODULES:
        monkeypatch.setattr(importlib.import_module(mod_path), "emit_trace_event", lambda *a, **k: None)


def _invoke(payload: str = _VALID_PAYLOAD, input_context=None) -> dict:
    from src.graph.graph import build_agent

    agent = build_agent()
    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return agent.invoke(payload, ctx=ctx, input_context=input_context or {})


class TestBackboneInvokeOrder:
    def test_invoke_succeeds_with_the_reference_payload(self, monkeypatch):
        _mute_emit(monkeypatch)
        result = _invoke()
        assert result.get("status") == AgentStatus.SUCCESS.value, result.get("error_log")

    def test_backbone_node_history_order(self, monkeypatch):
        _mute_emit(monkeypatch)
        result = _invoke()
        history = result.get("node_history", [])
        assert history, "node_history is empty — the backbone recorded no node"
        assert history[0] == "InitializeNode", history
        assert history[-1] == "FinalizeNode", history
        assert _MAIN_SLOT_NODE in history, history

        pre_idx = next((i for i, n in enumerate(history) if "Validate" in n), -1)
        main_idx = history.index(_MAIN_SLOT_NODE)
        post_idx = next((i for i, n in enumerate(history) if "SecurityGate" in n), -1)
        assert 0 <= pre_idx < main_idx < post_idx, history

    def test_the_shaped_output_carries_the_report(self, monkeypatch):
        _mute_emit(monkeypatch)
        result = _invoke()
        output = result.get("output")
        assert isinstance(output, str) and output
        assert "NCR Classification Report" in output

    def test_the_reference_payload_matches_the_deployment_smoke_request(self):
        import json
        import pathlib

        payload_path = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "invoke_payload.json"
        assert json.loads(payload_path.read_text(encoding="utf-8"))["input"] == _VALID_PAYLOAD

    def test_empty_input_returns_a_reason_rather_than_raising(self, monkeypatch):
        """An empty request completes carrying the reason, and classifies nothing.

        Not raising is what this proves. The run ends readably instead of as a
        fault, because the caller can send the question and try again on the
        same conversation - but no classification was produced, so none ships.
        """
        _mute_emit(monkeypatch)
        result = _invoke(payload="")
        assert result.get("status") == AgentStatus.SUCCESS.value, result
        assert result.get("output") == EMPTY_INPUT, result
        assert "Classification Report" not in str(result)

    @pytest.mark.parametrize("trust", [TrustLevel.ANONYMOUS])
    def test_a_caller_below_the_declared_trust_level_classifies_nothing(self, monkeypatch, trust):
        """The manifest declares VERIFIED_EXTERNAL; the pre_process slot enforces it.

        The entry point is what turns an authenticated caller into that trust
        level — without it every request lands here.
        """
        _mute_emit(monkeypatch)
        from src.graph.graph import build_agent

        agent = build_agent()
        result = agent.invoke(_VALID_PAYLOAD, ctx=InvocationContext(caller_trust_level=trust))
        assert result.get("status") == AgentStatus.ERROR.value
        assert not result.get("output")
