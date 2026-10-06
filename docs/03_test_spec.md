# Test Specification — MFG-C2-022

## Strategy

The suite is layered on purpose, because the two layers answer different questions.

**Direct `execute()` calls** prove which layer refused something. An end-to-end run cannot
distinguish a template refusal from a framework one, so every guarantee the template owns —
the input contract, the injection screen, the output contract, the clearing on refusal — is
asserted by calling the node directly, with no framework wrapper in front.

**Boundary tests through the real HTTP entry point** prove the agent actually works. They drive
the ASGI app in `src/api/server.py` against a compiled agent: the adapter, the trust boundary, the
nested graph, and the output gate, in the arrangement a deployment runs. A suite that only calls
`execute()` can be green on an agent that cannot serve a single request.

Run the whole suite with:

```bash
python -m pytest tests/ -v
```

## Test modules

| Module | What it covers |
|---|---|
| `tests/unit/test_agent.py` | Each domain node's contract, classification tables, record shape |
| `tests/unit/test_pre_process_node.py` | The caller-data contract: injection screen, inert identifiers, finite numerics, configured bounds, and which rejections terminate rather than complete |
| `tests/unit/test_post_process_node.py` | The published output contract, the credential scan, and what a refusal withholds |
| `tests/unit/test_runtime_config.py` | Every declared configuration key pinned to the reader that consumes it |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | The framework's security gates are not overridden |
| `tests/proof_of_boundary/test_pb_public_path.py` | The public path end to end through `/invoke` |
| `tests/proof_of_boundary/test_pb_output_containment.py` | What a caller receives when the output gate refuses, and when a request is declined before any classification exists |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | Backbone execution order and the reference payload |
| `tests/proof_of_boundary/test_import_isolation.py` | No platform-SDK imports |
| `tests/proof_of_boundary/test_state_safety.py` | State carries no credential-named fields or unserializable types |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | Human-in-the-loop propagation — skipped; this agent does not use it |

## Framework compliance

| TC-ID | Test | Expected result |
|---|---|---|
| TC-01 | State is a flat TypedDict | No Pydantic or dataclass fields |
| TC-02 | Unacceptable input stops the run | A value the caller can correct completes carrying its reason code; content the agent refuses terminates with an error status. Neither carries a classification |
| TC-03 | No credentials in state | Credential scan: 0 violations |
| TC-04 | Invocation context is not carried in state | No context object in the state schema |
| TC-05 | No duplicate lifecycle events inside `execute()` | The framework emits them; nodes do not |
| TC-06 | The framework input gate is not overridden | Enforced at class definition |
| TC-07 | The framework output gate is not overridden | Enforced at class definition |
| TC-08 | `required_trust_level` is enforced | A caller below the declared level classifies nothing |
| TC-09 | Domain input checks run in the node that owns the contract | The injection screen terminates and the bounds decline, both inside the node |
| TC-10 | Domain output checks run at the boundary | Contract check and credential scan refuse directly |
| TC-11 | Each node emits at least one domain audit event | Verified by the audit-trace gate |

## Proof-of-boundary

| PB-ID | Boundary | Expected result |
|---|---|---|
| PB-1 | Node → audit sink | A domain event is emitted on every invocation path |
| PB-2 | State serialization | Post-invoke state holds primitives only |
| PB-4 | Import isolation | AST scan: 0 platform-SDK imports |
| PB-5 | Checkpoint safety | No credential-named or unserializable fields |
| PB-6 | Backbone execution order | `initialize → pre_process → main → post_process → finalize`, with the reference payload matching `deploy/invoke_payload.json` |
| PB-7 | Human-in-the-loop propagation | Skipped — not enabled for this agent |

## Business logic

| ID | Test | Input | Expected result |
|---|---|---|---|
| BL-01 | Defect category from report text | Five reports, one per category | The matching closed-set label |
| BL-02 | 5M1E root cause from report text | Five reports, one per category | The matching 5M1E label |
| BL-03 | Routing matrix | Severity × defect pairs | The declared team and priority |
| BL-04 | Measured defect rate escalates severity | 5 affected of 40, default threshold | `critical`, basis `defect_rate_escalation` |
| BL-05 | A caller threshold changes the outcome | Same figures, threshold 0.9 | Not critical |
| BL-06 | Negated safety language does not escalate | "No safety or functional impact" | Not critical |
| BL-07 | Absent caller data degrades to the text-only baseline | No `input_context` | Classification succeeds, no lot line rendered |
| BL-08 | Caller figures reach the inner workflow | Lot figures supplied | The rendered report changes |

## Input contract

Two outcomes are asserted separately, never through one shared helper: a **refusal** terminates
with an error status, and a **decline** completes with `AgentStatus.SUCCESS` and a fixed reason
code. Collapsed into a single assertion, a refusal downgraded to a correctable value would still
satisfy every row below, so each row states which outcome it expects.

| ID | Test | Expected result |
|---|---|---|
| IN-01 | Chat-template control tokens (`<\|…\|>`, `[INST]`, `<<SYS>>`, role markers, ChatML headings) | Refused: the run terminates and releases nothing |
| IN-02 | A control token hidden by the node's own markup strip | Refused, terminates |
| IN-03 | Zero-width-split directive | Refused, terminates |
| IN-04 | Ordinary quality prose containing screened words | Accepted |
| IN-05 | Directive in a context KEY or value | Refused, terminates |
| IN-06 | Non-finite numerics per field (`NaN`, `±Infinity`, raw floats) | Declined, fail closed: completes with `INVALID_REQUEST` |
| IN-07 | Out-of-range and non-integer counts | Declined with `INVALID_REQUEST` |
| IN-08 | Affected units greater than lot size | Declined with `INVALID_REQUEST` |
| IN-09 | Free text in the rendered reference field, or in `channel` | Declined with `INVALID_REQUEST` |
| IN-10 | Unknown context field | Declined with `INVALID_REQUEST`, not ignored |
| IN-11 | Credential-shaped context value | Refused at the entry point (400), and terminates at the node |
| IN-12 | Rejected value never appears in the error | The refusal's internal reason carries the field and a fixed code only — no value, from any of the credential-shaped parameters tried |
| IN-13 | Declared length window is the one enforced | Bounds come from `config/config.yaml`; over the cap declines with `QUESTION_TOO_LONG`, under the minimum with `INVALID_REQUEST` |

## Output contract

| ID | Test | Expected result |
|---|---|---|
| OUT-01 | Every rendered field against its closed set | A value outside it is withheld |
| OUT-02 | Rates and counts outside their declared range | Withheld |
| OUT-03 | Rates render to two decimal places | Full precision never reaches the report |
| OUT-04 | Every credential shape the framework knows | Withheld, and the matched value never appears |
| OUT-05 | A missing or unparseable record | Withheld — never rendered from defaults |
| OUT-06 | The key the gate reads is the key the merge writes | Asserted against `merge_output` itself |
| OUT-07 | The merge never writes the caller-visible fallback field | Asserted against `merge_output` itself |
| OUT-08 | Every content-bearing field present AND empty in the refusal delta | Partial deltas merge, so absence is not clearing |
| OUT-09 | The replacement notice is truthy | A falsy one re-opens the fallback |
| OUT-10 | The cleared inventory covers every domain state field | A new field must be classified |
| OUT-11 | Identifiers survive the renderer byte for byte | `NCR-2026-0042`, `SKF-6205`, `STU-1234` |
| OUT-12 | No caller free text reaches the report | Marker absent from the released report |

## Containment

| ID | Test | Expected result |
|---|---|---|
| CT-01 | Clean-path control | The same request still produces its real classification, with the gate node in the history |
| CT-02 | Drifted producer feeding caller text into a closed-set field | Error status, nothing from the drifted field in the envelope |
| CT-03 | The caller is told the output was withheld | Truthy notice, not an empty output |
| CT-04 | The block happened at the gate | Gate node present in the node history |
| CT-05 | No traceback or source path in the envelope | Absent |
| CT-06 | Every path that produces no classification, enumerated rather than sampled | The injection refusal terminates with an empty output; the five declined requests — a count out of range, a non-finite threshold, an empty document, an unknown field, text below the minimum length — each complete carrying their sentence. None ships any part of a report |
| CT-07 | A run that never reaches the gate | Releases nothing: the inner record is not written to the fallback field |
