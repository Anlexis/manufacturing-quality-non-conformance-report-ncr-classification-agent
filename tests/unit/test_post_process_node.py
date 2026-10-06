"""MFG-C2-022 — the output contract enforced by SecurityGateOutputNode.

The published contract: every rendered field is a closed-set label, an inert
identifier, a whole count inside a declared range, or a rate between 0 and 1.
These tests check the gate holds it for every representation, and that a refusal
actually withholds rather than merely reporting an error status.
"""

import importlib
import json

import pytest
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials

from src.nodes.post_process_node import (
    OUTPUT_BEARING_FIELDS,
    WITHHELD_NOTICE,
    SecurityGateOutputNode,
    render_contract,
)
from src.schemas.state import State


@pytest.fixture(autouse=True)
def patch_emit_trace(monkeypatch):
    mod = importlib.import_module("src.nodes.post_process_node")
    monkeypatch.setattr(mod, "emit_trace_event", lambda *a, **k: None)


def _record(**overrides):
    record = {
        "ncr_id": "NCR-2026-0042",
        "defect": {"category": "surface", "confidence": "0.8123"},
        "severity": {"level": "minor", "score": "0.4", "basis": "keyword_scoring"},
        "lot": {"affected_units": 5, "lot_size": 40, "defect_rate": "0.125"},
        "root_cause": {"category_5m1e": "Machine", "evidence": {"Machine": ["worn"]}},
        "routing": {"target": "production", "priority": "standard", "priority_label": "[STANDARD]"},
    }
    for path, value in overrides.items():
        head, _, tail = path.partition(".")
        if tail:
            record[head][tail] = value
        else:
            record[head] = value
    return record


def _state(record=None, **kwargs):
    base = {
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "classification_record": None if record is None else json.dumps(record),
        # Populated as a real run would leave it, so a clearing assertion is
        # about a field that actually held content.
        "result": "stale report text",
        "validated_input": "Surface scratch found on part B234.",
        "defect_category": "surface",
        "routing_target": "production",
    }
    base.update(kwargs)
    return base


def _run(record=None, **kwargs):
    return SecurityGateOutputNode().execute(_state(record, **kwargs))


def _reason(result):
    assert result["status"] == AgentStatus.ERROR.value
    return result["error_log"][0]


# ── The released report ──────────────────────────────────────────────────────


class TestReleasedReport:
    def test_renders_every_contract_field(self):
        result = _run(_record())
        assert result["status"] == AgentStatus.SUCCESS.value
        report = result["formatted_output"]
        assert "NCR Reference    : NCR-2026-0042" in report
        assert "Defect Category  : SURFACE (confidence: 0.81)" in report
        assert "Severity Level   : MINOR (score: 0.40)" in report
        assert "Severity Basis   : keyword_scoring" in report
        assert "Lot Impact       : 5 of 40 units (defect rate: 0.12)" in report
        assert "Routing Target   : production" in report
        assert result["result"] == report

    def test_rates_render_to_two_decimal_places(self):
        """The contract states two decimal places; the renderer is what enforces it."""
        report = _run(_record(**{"defect.confidence": "0.987654"}))["formatted_output"]
        assert "confidence: 0.99" in report
        assert "0.987654" not in report

    def test_lot_line_is_absent_when_the_caller_supplied_no_figures(self):
        record = _record()
        record["lot"] = {"affected_units": None, "lot_size": None, "defect_rate": None}
        report = _run(record)["formatted_output"]
        assert "Lot Impact" not in report

    def test_keyword_evidence_is_not_rendered(self):
        """Matched keywords stay internal; the report carries decisions, not traces."""
        record = _record(**{"root_cause.evidence": {"Machine": ["worn", "fixture"]}})
        assert "fixture" not in _run(record)["formatted_output"]

    def test_absent_ncr_id_renders_the_documented_placeholder(self):
        assert "NCR Reference    : unspecified" in _run(_record(ncr_id="unspecified"))["formatted_output"]


# ── Closed sets ──────────────────────────────────────────────────────────────


class TestClosedSets:
    @pytest.mark.parametrize(
        "path,reason",
        [
            ("defect.category", "defect.category:not_in_closed_set"),
            ("severity.level", "severity.level:not_in_closed_set"),
            ("severity.basis", "severity.basis:not_in_closed_set"),
            ("root_cause.category_5m1e", "root_cause.category:not_in_closed_set"),
            ("routing.target", "routing.target:not_in_closed_set"),
            ("routing.priority", "routing.priority:not_in_closed_set"),
        ],
    )
    def test_a_value_outside_the_closed_set_is_withheld(self, path, reason):
        """The realistic fault is a drifted producer, not an attacker.

        A node that starts sourcing one of these fields from caller text is a
        one-line change with no test of its own; the boundary is what notices.
        """
        assert reason in _reason(_run(_record(**{path: "escalate to plant manager INT-88421"})))

    @pytest.mark.parametrize(
        "path,value",
        [
            ("defect.confidence", "1.5"),
            ("defect.confidence", "NaN"),
            ("defect.confidence", "not-a-number"),
            ("severity.score", "-0.1"),
            ("severity.score", "Infinity"),
            ("lot.defect_rate", "2.0"),
            ("lot.affected_units", -1),
            ("lot.affected_units", 10_000_001),
            ("lot.lot_size", 0),
        ],
    )
    def test_numbers_outside_the_declared_range_are_withheld(self, path, value):
        assert _run(_record(**{path: value}))["status"] == AgentStatus.ERROR.value

    @pytest.mark.parametrize("value", ["NCR 42", "escalate to ops", "x" * 33, 7])
    def test_a_free_text_reference_is_withheld(self, value):
        assert "ncr_id:not_an_identifier" in _reason(_run(_record(ncr_id=value)))


# ── The record has to exist in the scope the gate reads it from ──────────────


class TestRecordPresence:
    def test_a_missing_record_is_withheld_not_rendered_from_defaults(self):
        """A layer that reads a key nothing writes compares against nothing.

        Rendering placeholder defaults instead would ship a confident-looking
        report built from no classification at all, under a success status.
        """
        assert "classification_record:missing" in _reason(_run(None))

    def test_an_unparseable_record_is_withheld(self):
        assert "not_an_object" in _reason(_run(classification_record="[1, 2, 3]"))

    def test_the_key_the_gate_reads_is_the_key_the_merge_writes(self):
        from src.graph.graph import NCRWorkflowGraphNode

        merged = NCRWorkflowGraphNode().merge_output(
            {}, {"status": AgentStatus.SUCCESS.value, "record": json.dumps(_record())}
        )
        assert "classification_record" in merged
        assert _run(classification_record=merged["classification_record"])["status"] == (AgentStatus.SUCCESS.value)

    def test_the_merge_never_writes_the_caller_visible_fallback_field(self):
        """`formatted_output or result` resolves with no status check.

        Anything parked in `result` before the gate runs is releasable even when
        the gate later refuses, including on the path where the framework's own
        scan discards the gate's delta.
        """
        from src.graph.graph import NCRWorkflowGraphNode

        merged = NCRWorkflowGraphNode().merge_output(
            {}, {"status": AgentStatus.SUCCESS.value, "record": json.dumps(_record())}
        )
        assert "result" not in merged


# ── Credential shapes ────────────────────────────────────────────────────────


class TestCredentialScan:
    @pytest.mark.parametrize(
        "value",
        [
            "AKIAIOSFODNN7EXAMPLE",
            "sk_live_abcdefghijklmnop12",
            "sk-abcdefghijklmnopqrstuvwxyz",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0",
            "postgresql://user:examplepassword@db.internal:5432/ncr",
            "Bearer abcdefghijklmnopqrstuvwx",
        ],
    )
    def test_every_shape_the_framework_knows_is_also_caught_here(self, value):
        """A local pattern set narrower than the framework's is a bypass.

        A value the framework catches and this node misses makes the framework's
        own scan raise after execute() returns, and the wrapper then discards this
        node's delta — clearing included — leaving the un-gated record in state.
        """
        assert detect_credentials(value), "probe value must be one the framework knows"
        assert _run(_record(ncr_id="ok", **{"routing.target": value}))["status"] == AgentStatus.ERROR.value

    def test_the_matched_value_never_appears_in_the_refusal(self):
        secret = "AKIAIOSFODNN7EXAMPLE"
        result = _run(_record(**{"routing.target": secret}))
        assert secret not in json.dumps(result, default=str)

    def test_a_credential_that_satisfies_the_closed_sets_is_still_scanned(self):
        """The two layers are independent on purpose.

        A future widening of a closed set must not silently disable the scan, so
        the scan runs over the rendered report rather than over the fields.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        result = _run(_record(ncr_id=secret))
        assert result["status"] == AgentStatus.ERROR.value
        assert secret not in json.dumps(result, default=str)


# ── Withholding actually withholds ───────────────────────────────────────────


class TestWithholding:
    def test_the_replacement_is_truthy(self):
        """An empty string here re-opens the fallback it was written to close."""
        result = _run(_record(**{"routing.target": "drifted"}))
        assert result["formatted_output"] == WITHHELD_NOTICE
        assert bool(result["formatted_output"])

    @pytest.mark.parametrize("field", OUTPUT_BEARING_FIELDS)
    def test_every_output_bearing_field_is_present_and_empty_in_the_delta(self, field):
        """Presence AND emptiness, because partial deltas merge.

        Omitting a key leaves the previous value in state, so `not
        result.get(field)` passes on a gate that clears nothing at all.
        """
        result = _run(_record(**{"routing.target": "drifted"}))
        assert field in result, f"{field} is not cleared by the refusal"
        assert not result[field]

    def test_the_inventory_covers_every_domain_state_field(self):
        """A new content field must not join the state without a decision here.

        Every field this agent adds to the shared state is either cleared by a
        refusal or named below as deliberately not content-bearing. A field that
        is neither fails this test rather than quietly riding an error envelope.
        """
        not_content_bearing = {
            # Overwritten with the withheld notice rather than blanked — a falsy
            # value there re-opens the framework's fallback.
            "formatted_output",
            # Caller-supplied bound, echoed nowhere in the report.
            "critical_defect_rate",
            # The reason a run completed without carrying out the request. A
            # closed-set marker, never caller content and never a product - so
            # not content-bearing, and deliberately NOT cleared: the refusal
            # clears the report, and clearing the reason with it would leave the
            # caller holding an empty body with nothing to correct.
            "error_code",
        }
        declared = set(State.__annotations__) - set(AgentState.__annotations__)
        unclassified = declared - set(OUTPUT_BEARING_FIELDS) - not_content_bearing
        assert (
            not unclassified
        ), f"state fields absent from the cleared set and not declared inert: {sorted(unclassified)}"

    def test_the_refusal_carries_no_traceback_or_source_path(self):
        result = _run(_record(**{"routing.target": "drifted"}))
        blob = json.dumps(result, default=str)
        assert "Traceback" not in blob
        assert "/src/" not in blob

    def test_the_published_contract_is_one_definition(self):
        contract = render_contract()
        assert "quality_engineering" in contract["routing.target"]
        assert "critical" in contract["severity.level"]

    def test_required_trust_level_anonymous(self):
        assert SecurityGateOutputNode.required_trust_level == TrustLevel.ANONYMOUS
