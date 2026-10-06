"""AgentCore Platform v1.0 — MFG-C2-022 NCR Classification Agent outer graph.

Cat 2 NESTED architecture:
  Outer: NCRClassificationAgent(AgentBaseGraph) — fixed 5-node backbone.
  Main slot: NCRWorkflowGraphNode(GraphNode) — wraps inner NCRDomainWorkflowGraph.
  Inner: NCRDomainWorkflowGraph(BaseGraph) — 6-node NCR classification pipeline.

Backbone pipeline (fixed — do NOT override add_edges()):
  START → initialize → pre_process[ValidateInputNode] → main[NCRWorkflowGraphNode]
        → post_process[SecurityGateOutputNode] → finalize → END
             ↓ (RETRY, max_retry from config/config.yaml)
          pre_process

Trust-level design:
  - ValidateInputNode (pre_process):  TrustLevel.VERIFIED_EXTERNAL — outer entry gate
  - NCRWorkflowGraphNode (main):      ANONYMOUS (subgraph boundary; already gated)
  - SecurityGateOutputNode (post_process): ANONYMOUS
  - All inner domain nodes:           ANONYMOUS (never VERIFIED_EXTERNAL/INTERNAL)

Directory layout:
  src/graph/graph.py                 ← outer graph (this file)
  src/graph/domain_workflow_graph.py ← inner graph
  src/graph/context_bridge.py        ← caller-payload hand-off across the boundary
"""

import pathlib
from typing import Any, ClassVar, Dict, Optional, cast

import yaml
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.graph.context_bridge import set_caller_payload
from src.nodes.post_process_node import SecurityGateOutputNode
from src.nodes.pre_process_node import ValidateInputNode
from src.schemas.state import State

_CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def runtime_config() -> Dict[str, Any]:
    """Read the runtime parameters from config/config.yaml.

    This is the same file the platform registry loads and hands to the graph
    constructor. The standalone entry point reads it through this function so a
    registry-loaded agent and a directly deployed one see identical settings.
    Without it, every value declared in config.yaml is inert in one of the two
    deployments and the difference is invisible: nothing fails, the framework
    simply falls back to its own defaults.

    Returns an empty mapping — never raises — when the file is absent or does not
    parse, so a missing file degrades to defaults rather than breaking start-up.
    """
    if not _CONFIG_PATH.exists():
        return {}
    try:
        loaded = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


class NCRWorkflowGraphNode(GraphNode):
    """GraphNode wrapper for the inner NCRDomainWorkflowGraph.

    Assigned to the `main` slot of NCRClassificationAgent. Implements the three
    GraphNode contract methods:
      get_subgraph()    — instantiate NCRDomainWorkflowGraph with the runtime config
      extract_input()   — hand the validated NCR text to the inner graph and stash
                          the validated caller payload on the context bridge
      merge_output()    — map inner sub_result fields back into the outer state
    """

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner NCRDomainWorkflowGraph.

        The lazy import avoids a circular import at module load time. The runtime
        settings are forwarded so a value declared in config.yaml is live in the
        inner graph as well as the outer one.
        """
        from src.graph.domain_workflow_graph import NCRDomainWorkflowGraph

        return NCRDomainWorkflowGraph(config=self._parent_config())

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the declared runtime settings to the inner graph.

        Kept as its own method so the inner graph's configuration has one
        documented source rather than being reconstructed at each call site.
        """
        return runtime_config()

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Return the validated NCR text passed into the inner graph.

        Also stashes the VALIDATED caller payload on the context bridge. The
        framework's GraphNode does not forward input_context into the inner
        graph, so without this the caller's lot figures and threshold would be
        absent from every inner node and the agent would classify from the text
        alone while the unit suite stayed green.

        What crosses is what ValidateInputNode produced, never the raw context:
        caller data is bounded exactly once, upstream of this point.
        """
        set_caller_payload(
            {
                "ncr_id": state.get("ncr_id"),
                "affected_units": state.get("affected_units"),
                "lot_size": state.get("lot_size"),
                "critical_defect_rate": state.get("critical_defect_rate"),
            }
        )
        validated = state.get("validated_input")
        if isinstance(validated, str) and validated:
            return validated
        return cast(str, state.get("user_input", "") or "")

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner sub_result fields into the outer state. Only changed keys.

        Key coupling (designed with NCRDomainWorkflowGraph.get_output()):
          inner get_output() emits "record" plus each classification field;
          this method writes them to the outer state under the same names.

        The inner workflow's structured result is written to
        `classification_record`, NOT to `result`. The framework resolves an
        agent's caller-visible output as `formatted_output or result` with no
        status check, so a value parked in `result` before the output gate has
        run is releasable even when the gate later refuses — including on the
        path where the framework's own output scan discards the gate's delta.
        `result` is written by the output gate and by nothing else.

        A non-SUCCESS inner run contributes no classification at all.
        """
        # Outer reason wins: a reason settled before the inner run is the real
        # one, and a plain sub_result.get() would erase it.
        marker = state.get("error_code") or sub_result.get("error_code", "")
        status = sub_result.get("status")
        if status != AgentStatus.SUCCESS.value:
            return {"classification_record": None, "status": status, "error_code": marker}
        return {
            "error_code": marker,
            "classification_record": sub_result.get("record"),
            "status": status,
            "defect_category": sub_result.get("defect_category"),
            "defect_confidence": sub_result.get("defect_confidence"),
            "severity_level": sub_result.get("severity_level"),
            "severity_score": sub_result.get("severity_score"),
            "severity_basis": sub_result.get("severity_basis"),
            "defect_rate": sub_result.get("defect_rate"),
            "root_cause_category": sub_result.get("root_cause_category"),
            "root_cause_details": sub_result.get("root_cause_details"),
            "routing_target": sub_result.get("routing_target"),
            "routing_priority": sub_result.get("routing_priority"),
        }


class NCRClassificationAgent(AgentBaseGraph):
    """Outer graph for the MFG-C2-022 nonconformance-report classification agent.

    Cat 2 NESTED: delegates domain logic to NCRWorkflowGraphNode in the `main`
    slot. The backbone pipeline (5 nodes) is fixed by AgentBaseGraph — do NOT
    override add_edges().

    Construct with the runtime settings — `NCRClassificationAgent(config=runtime_config())` —
    exactly as the platform registry does. The backbone consumes `max_retry` from
    that mapping for retry routing, and the caller-data bounds travel from the
    same mapping into the node that enforces them, so the declared values are
    live in both deployments.
    """

    @property
    def name(self) -> str:
        return "NCRClassificationAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode

        self._nodes["pre_process"] = ValidateInputNode(config=self.config)
        self._nodes["main"] = NCRWorkflowGraphNode()
        self._nodes["post_process"] = SecurityGateOutputNode()


def build_agent(config: Optional[Dict[str, Any]] = None) -> NCRClassificationAgent:
    """Return a compiled agent wired to the declared runtime settings."""
    agent = NCRClassificationAgent(config=runtime_config() if config is None else config)
    agent.compile()
    return agent
