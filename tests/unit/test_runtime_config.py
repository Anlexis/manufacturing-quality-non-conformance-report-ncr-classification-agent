"""MFG-C2-022 — the declared runtime configuration is live, not decorative.

A declared value that equals the framework default is indistinguishable from a
value nothing reads. These tests pin each declared key to the reader that
consumes it, and prove the reader rejects a declaration it cannot honour.
"""

import importlib

import pytest
import yaml
from framework.errors import ConfigError

from src.graph.domain_workflow_graph import NCRDomainWorkflowGraph
from src.graph.graph import NCRClassificationAgent, runtime_config
from src.nodes.pre_process_node import ValidateInputNode


class TestShippedConfiguration:
    def test_the_shipped_file_parses_and_declares_the_documented_keys(self):
        config = runtime_config()
        assert set(config) == {"max_retry", "timeout_s", "ncr"}
        assert set(config["ncr"]) == {"min_text_chars", "max_text_chars", "critical_defect_rate"}

    def test_the_file_on_disk_is_what_runtime_config_returns(self):
        from src.graph.graph import _CONFIG_PATH

        assert runtime_config() == yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))

    def test_a_missing_file_degrades_to_defaults_rather_than_failing_start_up(self, monkeypatch):
        import src.graph.graph as graph_module

        monkeypatch.setattr(graph_module, "_CONFIG_PATH", graph_module._CONFIG_PATH.with_name("absent.yaml"))
        assert graph_module.runtime_config() == {}


class TestBackboneReadsMaxRetry:
    def test_the_agent_is_constructed_with_the_declared_settings(self):
        from src.graph.graph import build_agent

        assert build_agent().config == runtime_config()

    def test_an_unusable_retry_setting_is_refused_at_compile(self):
        agent = NCRClassificationAgent(config={"max_retry": -1})
        with pytest.raises(ConfigError):
            agent.compile()

    def test_a_retry_setting_above_the_framework_ceiling_is_refused(self):
        agent = NCRClassificationAgent(config={"max_retry": 99})
        with pytest.raises(ConfigError):
            agent.compile()


class TestInnerGraphReadsTheDomainBlock:
    @pytest.mark.parametrize(
        "block",
        [
            {"critical_defect_rate": float("nan")},
            {"critical_defect_rate": float("inf")},
            {"critical_defect_rate": 1.5},
            {"critical_defect_rate": -0.1},
            {"critical_defect_rate": "0.05"},
            {"critical_defect_rate": True},
            {"min_text_chars": 0},
            {"max_text_chars": "long"},
        ],
    )
    def test_an_unusable_domain_setting_is_refused_at_compile(self, block):
        """A NaN threshold compares False against every rate.

        Accepting it would disable the escalation it configures, silently, on the
        exact decision this agent exists to make.
        """
        graph = NCRDomainWorkflowGraph(config={"ncr": block})
        with pytest.raises(ConfigError):
            graph.compile()

    def test_the_shipped_settings_compile(self):
        NCRDomainWorkflowGraph(config=runtime_config()).compile()

    def test_the_declared_threshold_is_seeded_into_the_inner_state(self):
        from src.graph.context_bridge import clear_caller_payload

        clear_caller_payload()
        graph = NCRDomainWorkflowGraph(config={"ncr": {"critical_defect_rate": 0.25}})
        assert float(graph._extra_initial_state()["critical_defect_rate"]) == 0.25

    def test_a_caller_threshold_takes_precedence_over_the_declared_one(self):
        from src.graph.context_bridge import set_caller_payload

        set_caller_payload({"critical_defect_rate": "0.9"})
        graph = NCRDomainWorkflowGraph(config={"ncr": {"critical_defect_rate": 0.05}})
        assert float(graph._extra_initial_state()["critical_defect_rate"]) == 0.9


class TestInputBoundsComeFromTheDeclaredBlock:
    def test_the_node_enforces_the_declared_window(self):
        node = ValidateInputNode(config={"ncr": {"min_text_chars": 5, "max_text_chars": 12}})
        assert node._min_text_chars == 5
        assert node._max_text_chars == 12

    def test_the_agent_hands_the_declared_block_to_the_node(self):
        agent = NCRClassificationAgent(config={"ncr": {"max_text_chars": 77}})
        agent.compile()
        assert agent._nodes["pre_process"]._max_text_chars == 77


class TestEntryPointDeadline:
    def test_the_declared_deadline_is_the_one_the_adapter_uses(self, monkeypatch):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", "x")
        import src.api.server as server

        server = importlib.reload(server)
        assert server.TIMEOUT_S == runtime_config()["timeout_s"]

    @pytest.mark.parametrize("declared", [0, -1, "soon", True, None])
    def test_an_unusable_deadline_falls_back_to_the_documented_default(self, declared):
        import src.api.server as server

        assert server._request_timeout_s({"timeout_s": declared}) == server.DEFAULT_TIMEOUT_S

    def test_a_run_past_the_deadline_returns_a_gateway_timeout(self, monkeypatch):
        """The framework has no reader for this key, so the adapter is the reader."""
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", "x")
        import src.api.server as server

        server = importlib.reload(server)
        monkeypatch.setattr(server, "TIMEOUT_S", 0.01)
        monkeypatch.setattr(server, "emit_trace_event", lambda *a, **k: None)

        def slow(*args, **kwargs):
            import time

            time.sleep(0.5)
            return {}

        monkeypatch.setattr(server.agent, "invoke", slow)

        from tests.http_probe import request

        response = request(
            server.app,
            "POST",
            "/invoke",
            json_body={"input": "Surface scratch on part B234 during inspection."},
            headers={"Authorization": "Bearer x"},
        )
        assert response.status_code == 504
