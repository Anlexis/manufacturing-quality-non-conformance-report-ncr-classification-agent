# Quality NCR Classification Agent

AI agent for classifying manufacturing quality non-conformance reports, built with Agentic Star.

> **Category**: Cat 2 (domain-specific manufacturing quality pipeline)
> **Industry**: Manufacturing
> **Template ID**: MFG-C2-022

## Overview

Classifies a manufacturing nonconformance report (NCR) and says who should act on it. Given the
free-text report — and, optionally, the lot figures behind it — the agent returns the defect
category, a severity level, the 5M1E root-cause category, and the team and priority the
nonconformance should be routed to.

The classification is deterministic and rule-based: keyword models over the report text, plus a
measured defect rate when the caller supplies the affected-unit and lot-size figures. No language
model is called, so the same report always produces the same classification, and the reason for
every decision can be read out of the source.

The report the caller receives renders closed-set labels, two-decimal rates and whole counts only.
No caller free text is echoed into it. Everything the caller sends is validated once at the entry
boundary, and the output boundary independently checks every rendered field against the same
published contract before releasing it.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails during
graph compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Calling the agent

`POST /invoke` takes the report text plus an optional structured context:

```json
{
  "input": "Surface scratch found on part B234 painted coating during final inspection. Cosmetic defect only.",
  "session_id": "shopfloor-001",
  "input_context": {
    "channel": "shopfloor",
    "ncr_id": "NCR-2026-0042",
    "affected_units": 5,
    "lot_size": 40,
    "critical_defect_rate": 0.05
  }
}
```

Every `input_context` field is optional; with none of them the agent classifies from the report
text alone. `ncr_id` is the only caller string rendered into the report and is restricted to
`[A-Za-z0-9_-]{1,32}`. `affected_units` and `lot_size` are whole numbers; when both are present
the agent computes the defect rate and escalates severity to critical at or above
`critical_defect_rate` (default in `config/config.yaml`). Any field outside its declared bounds is
refused, naming the field and never echoing the value.

When `INVOKE_AUTH_TOKEN` is set on the server environment, `/invoke` requires the matching
`Authorization: Bearer` header from callers no upstream middleware has already vouched for.

## Project Structure

```
src/          agent implementation (nodes, graphs, schemas, HTTP entry point)
tests/        unit and boundary tests
config/       agent manifest and runtime parameters
docs/         design and test documentation
```

See `docs/02_design.md` for the architecture and the published output contract, and
`docs/03_test_spec.md` for what the suite covers.

## Customising

1. Adjust `config/config.yaml` for your own bounds — accepted report length, escalation threshold,
   retry count and request deadline.
2. Replace the keyword models in `src/nodes/` with the vocabulary your plants actually use; the
   defect, severity and 5M1E tables are plain data.
3. Extend the routing matrix in `src/nodes/routing_decide_node.py` with your own teams — and widen
   the matching closed set in `src/nodes/post_process_node.py`, which is what the output boundary
   enforces.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
