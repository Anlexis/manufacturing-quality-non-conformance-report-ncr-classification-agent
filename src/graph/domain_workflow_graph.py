"""AgentCore Platform v1.0 — MFG-C2-022 inner domain workflow graph.

NCRDomainWorkflowGraph — returned by NCRWorkflowGraphNode.get_subgraph().
Inherits BaseGraph (custom linear topology, no forced backbone).
Pipeline: START → input_validate → defect_classify → severity_assess
              → root_cause_classify → routing_decide → output_format → END
"""

import math
from typing import Any, Dict, Optional

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from langgraph.graph import END, START

from src.graph.context_bridge import get_caller_payload
from src.nodes.defect_classify_node import DefectClassifyNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.nodes.root_cause_classify_node import RootCauseClassifyNode
from src.nodes.routing_decide_node import RoutingDecideNode
from src.nodes.severity_assess_node import SeverityAssessNode
from src.schemas.state import State


class NCRDomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for MFG-C2-022 NCR classification.

    Implements the multi-step classification pipeline via all seven BaseGraph
    abstract methods. Instantiated by NCRWorkflowGraphNode.get_subgraph() on
    every invocation, with the runtime settings forwarded from the outer graph.

    The outer InvocationContext is passed into this inner graph unchanged, so all
    inner nodes declare ANONYMOUS and admit any outer caller. The caller's
    validated parameters do NOT travel that way — the framework does not forward
    input_context across the subgraph boundary — so they arrive through the
    context bridge, seeded here in _extra_initial_state().
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "NCRDomainWorkflowGraph"

    @property
    def state_schema(self) -> type:
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Reject a runtime configuration this workflow cannot honour.

        The declared bounds decide what the pipeline accepts and when it
        escalates, so an out-of-range or non-finite declaration is refused at
        construction rather than silently replaced by a default at the point of
        use — a NaN threshold compares False against every rate and would
        disable the escalation it configures.
        """
        ncr = self.config.get("ncr")
        if ncr is None:
            return
        if not isinstance(ncr, dict):
            raise ConfigError(f"[{self.__class__.__name__}] 'ncr' must be a mapping, got: {ncr!r}")

        rate = ncr.get("critical_defect_rate")
        if rate is not None:
            if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                raise ConfigError(
                    f"[{self.__class__.__name__}] 'ncr.critical_defect_rate' must be a number, got: {rate!r}"
                )
            if not math.isfinite(float(rate)) or not 0.0 <= float(rate) <= 1.0:
                raise ConfigError(
                    f"[{self.__class__.__name__}] 'ncr.critical_defect_rate' must be a finite value "
                    f"between 0.0 and 1.0, got: {rate!r}"
                )

        for key in ("min_text_chars", "max_text_chars"):
            value = ncr.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"[{self.__class__.__name__}] 'ncr.{key}' must be a positive integer, got: {value!r}")

    # ── Caller payload hand-off ───────────────────────────────────────────────

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the validated caller parameters into the inner initial state.

        The framework builds the inner state from the user_input string alone, so
        without this hook every caller parameter is absent from every inner node
        and the pipeline classifies from the report text only — while reporting
        success. The values seeded here were bounded once by the outer
        ValidateInputNode; nothing raw crosses.

        The configured escalation threshold is seeded alongside them so the
        severity node has one place to read it from, whether or not the caller
        overrode it.
        """
        payload = get_caller_payload()
        seeded: Dict[str, Any] = {
            "ncr_id": payload.get("ncr_id"),
            "affected_units": payload.get("affected_units"),
            "lot_size": payload.get("lot_size"),
            "critical_defect_rate": payload.get("critical_defect_rate"),
        }
        if seeded["critical_defect_rate"] is None:
            configured = self._configured_threshold()
            if configured is not None:
                seeded["critical_defect_rate"] = repr(configured)
        return seeded

    def _configured_threshold(self) -> Optional[float]:
        """Return the declared escalation threshold, or None when unset."""
        ncr = self.config.get("ncr")
        if not isinstance(ncr, dict):
            return None
        rate = ncr.get("critical_defect_rate")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            return None
        return float(rate)

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all inner domain nodes (no super() call — BaseGraph is abstract)."""
        self._nodes["input_validate"] = InputValidateNode()
        self._nodes["defect_classify"] = DefectClassifyNode()
        self._nodes["severity_assess"] = SeverityAssessNode()
        self._nodes["root_cause_classify"] = RootCauseClassifyNode()
        self._nodes["routing_decide"] = RoutingDecideNode()
        self._nodes["output_format"] = OutputFormatNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear NCR classification pipeline."""
        self._sg.add_edge(START, "input_validate")
        self._sg.add_edge("input_validate", "defect_classify")
        self._sg.add_edge("defect_classify", "severity_assess")
        self._sg.add_edge("severity_assess", "root_cause_classify")
        self._sg.add_edge("root_cause_classify", "routing_decide")
        self._sg.add_edge("routing_decide", "output_format")
        self._sg.add_edge("output_format", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: State) -> str:
        """Required by the BaseGraph contract. Not reached in a linear topology.

        Annotated with this graph's own State: the runtime reads a routing
        callable's annotation as its input schema and projects away every field
        the annotation does not declare, so an AgentState annotation here would
        make the domain fields invisible to the decision.
        """
        return END if state.get("status") == AgentStatus.ERROR.value else "output_format"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: State) -> Dict[str, Any]:
        """Shape the dict returned to NCRWorkflowGraphNode.merge_output().

        `record` carries the structured classification; the individual fields are
        surfaced alongside it so the outer output gate can check the rendered
        report against the same values the record reports, rather than trusting
        the record to describe itself.
        """
        return {
            # the reason must leave the subgraph or the outer graph cannot report it
            "error_code": state.get("error_code"),
            "record": state.get("formatted_output"),
            "status": state.get("status"),
            "defect_category": state.get("defect_category"),
            "defect_confidence": state.get("defect_confidence"),
            "severity_level": state.get("severity_level"),
            "severity_score": state.get("severity_score"),
            "severity_basis": state.get("severity_basis"),
            "defect_rate": state.get("defect_rate"),
            "root_cause_category": state.get("root_cause_category"),
            "root_cause_details": state.get("root_cause_details"),
            "routing_target": state.get("routing_target"),
            "routing_priority": state.get("routing_priority"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
        }
