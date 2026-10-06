# Template Design Specification — MFG-C2-022
# Manufacturing Quality NCR Classification Agent

## Position in the AgentCore architecture

| Aspect | Value |
|---|---|
| Agent class | `NCRClassificationAgent` |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Architecture pattern | Cat 2 nested: outer `AgentBaseGraph` + a `GraphNode` in the `main` slot wrapping an inner `BaseGraph` domain workflow |
| Model use | None. Every decision is a rule over the report text and the caller's figures. |

Three-layer separation:

- **State** — flat `TypedDict`. Fields holding structures are typed `Optional[str]` and travel
  through `to_json` / `from_json`, because checkpoints are serialized with msgpack.
- **Node** — `FunctionNode` subclasses implementing `execute(self, state) -> dict`. The framework
  supplies the state and nothing else; a node that needs configuration receives it at construction
  time from the graph, which is where the runtime settings live.
- **Graph** — outer `AgentBaseGraph` plus an inner `BaseGraph` reached through `GraphNode`.

## Architecture overview

### Outer backbone (`src/graph/graph.py` — `NCRClassificationAgent`)

Fixed five-node backbone; `add_edges()` is not overridden.

```
START → initialize → pre_process[ValidateInputNode]
      → main[NCRWorkflowGraphNode] → post_process[SecurityGateOutputNode]
      → finalize → END
```

### Inner domain workflow (`src/graph/domain_workflow_graph.py` — `NCRDomainWorkflowGraph`)

Six-node linear pipeline, returned by `NCRWorkflowGraphNode.get_subgraph()`:

```
START → input_validate → defect_classify → severity_assess
      → root_cause_classify → routing_decide → output_format → END
```

### Outer node configuration

| Node | Class | Trust level | Responsibility |
|---|---|---|---|
| initialize | `InitializeNode` (framework) | — | Session initialisation |
| pre_process | `ValidateInputNode` | `VERIFIED_EXTERNAL` | The caller trust gate and the whole input contract |
| main | `NCRWorkflowGraphNode` | `ANONYMOUS` | Runs the inner workflow and carries its result into outer state; skips it when the request was already declined upstream |
| post_process | `SecurityGateOutputNode` | `ANONYMOUS` | The output boundary: contract check, render, credential scan |
| finalize | `FinalizeNode` (framework) | — | Response metadata |

### Inner domain node configuration

All inner nodes declare `ANONYMOUS`. The framework passes the outer `InvocationContext` into the
subgraph unchanged, so an inner node requiring a higher trust level than the caller holds would
deny every real request while unit tests that build state by hand kept passing.

| Inner node | Class | Responsibility |
|---|---|---|
| input_validate | `InputValidateNode` | Confirms the report text crossed the boundary |
| defect_classify | `DefectClassifyNode` | Keyword-density scoring → `defect_category`, `defect_confidence` |
| severity_assess | `SeverityAssessNode` | Keyword scoring plus the measured defect rate → `severity_level`, `severity_score`, `severity_basis`, `defect_rate` |
| root_cause_classify | `RootCauseClassifyNode` | 5M1E scoring → `root_cause_category`, `root_cause_details` |
| routing_decide | `RoutingDecideNode` | Severity × defect matrix → `routing_target`, `routing_priority` |
| output_format | `OutputFormatNode` | Assembles the structured classification record |

### Caller data crossing the subgraph boundary

`GraphNode` invokes the inner graph as `subgraph.invoke(user_input, session_id=..., ctx=...)`. It
carries the text and the invocation context, and nothing else — the caller's structured parameters
do not cross on their own.

`src/graph/context_bridge.py` carries them. `NCRWorkflowGraphNode.extract_input()` runs immediately
before the inner invoke and stashes the VALIDATED payload; `NCRDomainWorkflowGraph._extra_initial_state()`
runs inside the inner invoke and seeds it into the inner state. What crosses is what
`ValidateInputNode` produced — never the raw context. Caller data is bounded exactly once, upstream
of the bridge.

Every field the inner graph reads has to travel this way. A field left behind is not a compile
error and not a test failure: the inner node reads a key absent from its own state, falls back to a
default, and the caller's setting is ignored while the run reports success.

## Runtime configuration

`config/agent.yaml` is the static manifest. `config/config.yaml` holds the runtime parameters and
is passed to the graph constructor — by the platform registry in a hosted deployment, and by
`runtime_config()` in `src/graph/graph.py` for the standalone entry point, so both see the same
settings.

| Key | Reader | Effect |
|---|---|---|
| `max_retry` | Framework backbone (`AgentBaseGraph`) | Retry routing; validated at compile time |
| `timeout_s` | `src/api/server.py` | Per-request deadline; exceeded requests return 504 |
| `ncr.min_text_chars` / `ncr.max_text_chars` | `ValidateInputNode` | Accepted report-length window |
| `ncr.critical_defect_rate` | `SeverityAssessNode`, via the inner graph | Defect rate at or above which severity escalates to critical, when the caller supplies no override |

`NCRDomainWorkflowGraph._validate_config()` refuses an `ncr` block it cannot honour — a non-finite
or out-of-range threshold, a non-positive length bound — at construction rather than falling back to
a default at the point of use. A non-finite threshold would be the worst case: every comparison
against it is false, so it would disable the escalation it configures while the run reported
success.

## Caller data contract

`POST /invoke` accepts the report text plus an optional `input_context` mapping. Every field is
optional; with none of them the agent classifies from the report text alone.

| Field | Accepted values | Rendered? |
|---|---|---|
| `channel` | `[a-z0-9_]{1,32}` | No — audit provenance only |
| `ncr_id` | `[A-Za-z0-9_-]{1,32}` | Yes |
| `affected_units` | whole number, 0–10,000,000, not greater than `lot_size` | Yes |
| `lot_size` | whole number, 1–10,000,000 | Yes |
| `critical_defect_rate` | finite number, 0.0–1.0 | No — changes the severity decision |

Rules the boundary enforces:

- **An unknown field stops the request rather than being ignored.** Ignoring a key is not stripping
  it: it stays in the mapping the framework copies into the first node's own result, where the
  mandatory output scan reads it. A caller who sent a field this agent does not read also has a
  wrong expectation about the answer, and a silent drop hides that until the answer is already
  wrong.
- **Every number goes through a finite, bounded parser.** `NaN` and the infinities parse through
  `float()` without complaint and arrive intact through raw JSON, and every comparison against
  `NaN` is false. Booleans are rejected explicitly, since `isinstance(True, int)` is true.
- **Rejected values are never echoed.** The internal audit entry names the field and a fixed reason
  code; the caller-facing sentence names what to correct and nothing else. Neither carries the value
  that failed, an internal field path, or a quoted gate message.
- **Credential shapes are refused even when the alphabet admits them.** An AWS access key ID is
  twenty uppercase alphanumerics and satisfies the reference-identifier pattern exactly. Both the
  HTTP entry point and `ValidateInputNode` screen the context with the framework's own credential
  detector, so what they refuse and what the framework's gate blocks are one set rather than two
  that can drift.

### How a rejected request ends

A rejected request ends in one of two ways, and which one applies is a property of what was
rejected — not of which node caught it.

**A value the caller can correct completes the run.** `ValidateInputNode` returns
`AgentStatus.SUCCESS` carrying a fixed code in `error_code`. The main slot then skips the inner
workflow — there is no validated text to classify, and running it would only produce a second,
vaguer reason for the same rejection — and the output boundary renders the matching sentence from
`src/services/failure_message.py` as the caller-facing body. Terminating instead would end the
calling surface's turn and surface an exception type, leaving the reason reachable only from the
audit trail; completing lets the caller fix the value and send the request again on the same
conversation.

| Condition | Reason code | The caller reads |
|---|---|---|
| `user_input` absent, blank, or not a string | `EMPTY_INPUT` | that no question was received |
| `user_input` longer than `ncr.max_text_chars` | `QUESTION_TOO_LONG` | that the request is too long |
| Cleaned text below `ncr.min_text_chars`; `input_context` not an object; an unknown context field; `channel` or `ncr_id` not an inert identifier; a count, rate or threshold outside its declared range; affected units greater than the lot | `INVALID_REQUEST` | that a value could not be accepted, and to check it against the documented format |

A code with no sentence of its own falls back to the generic one rather than putting the code
itself in front of the caller.

**Content the agent refuses terminates the run** with `AgentStatus.ERROR` and no reason code:

- a chat-template control token or an instruction-override directive, in the report text or
  anywhere in the structured context, keys included;
- a credential-shaped value in the structured context;
- a release the output boundary refuses — a field outside its closed set, a number outside its
  declared range, an absent or unparseable record, a credential shape in the rendered report;
- an upstream invariant the inner pipeline finds broken — report text that did not cross the
  subgraph boundary, or an escalation threshold that did not arrive as a finite rate.

Rewording is not a route past any of these, so none of them is reported as a value to correct. A
terminal status routes the run straight to `finalize`: where it is set before the output boundary,
the boundary never runs and the response carries no body at all; where the boundary sets it itself,
it has already cleared every content-bearing field and replaced the body with the withheld notice.
Either way no part of a classification reaches the caller.

**`error_code` is deliberately not in the envelope.** The agent returns `output`, `status`,
`trace_id`, `correlation_id` and `node_history`; the code is an internal routing marker that
decides which sentence is rendered. The reason reaches the caller through the message text alone,
so the codes stay an implementation detail rather than a published interface.

### Injection screen

`ValidateInputNode` refuses two families rather than stripping them. Removing a marker and
forwarding what surrounded it converts a detectable attack into undetectable plain text.

1. **Chat-template control tokens** — `<|…|>`, `[INST]` / `[SYS]`, `<<SYS>>`, `<system>`-style role
   markers, and ChatML-adjacent headings. Screened as a class, not as a list of literals.
2. **Instruction-override directives**, anchored on a verb plus an instruction/prompt/rule object,
   so ordinary quality prose survives — a report may legitimately say "ignore the previous revision
   of the work instruction sheet", and it does.

The screen runs over four forms of the text: raw, zero-width-stripped, compatibility-normalised,
and markup-stripped. The last matters because this node's own markup strip removes `<|im_start|>`:
screening only after it would see the directive residue as ordinary prose. Structured context is
screened depth-first including KEYS; the payload is already parsed by then, so escaped forms are
plain strings.

## Published output contract

The report renders these fields and nothing else:

| Rendered field | Contract |
|---|---|
| NCR reference | `[A-Za-z0-9_-]{1,32}`, or `unspecified` |
| Defect category | `dimensional` \| `surface` \| `functional` \| `material` \| `process` \| `other` |
| Severity level | `critical` \| `major` \| `minor` \| `observation` |
| Severity basis | `keyword_scoring` \| `defect_rate_escalation` |
| Root cause (5M1E) | `Man` \| `Machine` \| `Material` \| `Method` \| `Measurement` \| `Environment` |
| Routing target | `quality_engineering` \| `production` \| `supplier_quality` \| `manufacturing_engineering` \| `quality_assurance` |
| Routing priority | `urgent` \| `standard` \| `low` |
| Confidence, severity score, defect rate | finite, 0.0–1.0, rendered to two decimal places |
| Affected units, lot size | whole numbers, 0–10,000,000 |

Two things are deliberately absent. **No caller free text is rendered** — the only caller string in
the report is the reference identifier, over an inert alphabet. **Matched keyword evidence is not
rendered** either; it stays inside the record for internal use, so the report carries decisions
rather than the traces behind them.

**No monetary rounding grid applies here.** This report renders no monetary aggregates, so the
grid other templates use is not applicable. It would also be actively harmful: a grid that reads
any standalone three-letter uppercase word as a currency marker rewrites exactly the shape this
report exists to carry — `NCR-2026-0042` and part codes such as `SKF-6205` — so the invariant
enforced here is the closed-set and inert-identifier one instead. The boundary tests pin those
identifiers as surviving the renderer byte for byte.

## The output boundary

`SecurityGateOutputNode` is where the contract is enforced. It reads the inner workflow's record
from `classification_record`, checks every field above, renders the report from the values it has
checked, and scans the rendered text with the framework's `detect_credentials` plus two additive
patterns the framework does not carry.

It is also the node that speaks to a caller whose request was declined. When `error_code` is set
there is no record to check, so the node renders the matching sentence as both `formatted_output`
and `result`, carries the code onward, and completes. A refused release is the opposite outcome:
the node clears every content-bearing field, replaces `formatted_output` with the withheld notice,
and terminates with `AgentStatus.ERROR`.

Three framework properties shape this node:

1. **An agent's caller-visible output resolves as `formatted_output or result`, with no status
   check.** Returning an error status therefore withholds nothing on its own. On a violation the
   node overwrites every content-bearing state field and replaces `formatted_output` with a
   **truthy** notice; an empty string there re-opens the fallback it was written to close.
2. **The mandatory output scan reads every value the node returns.** Quoting a rejected value into
   an error makes that scan raise, and the wrapper then discards the node's whole delta — clearing
   included. Reasons are fixed codes for that reason.
3. **`merge_output` writes the inner record to `classification_record`, never to `result`.** Only
   the gate writes `result`. A value parked there before the gate runs is releasable even when the
   gate later refuses — and on the terminating paths, where the backbone routes the run straight to
   `finalize`, the gate never runs at all. A declined request reaches the gate normally, but it
   arrives carrying a reason code and no record, so nothing is rendered from the classification
   fields.

The cleared set is pinned as `OUTPUT_BEARING_FIELDS`, and a test asserts every domain state field
is either in it or explicitly declared inert, so a new content field cannot join the state without
a decision about whether a refusal clears it. `error_code` is one of the fields declared inert and
deliberately not cleared: it is a closed-set marker rather than caller content or a product, and a
refusal that cleared it alongside the report would leave the caller an empty body with nothing to
correct.

## State definition

| Field | Type | Owner | Purpose |
|---|---|---|---|
| `validated_input` | `Optional[str]` | ValidateInputNode | Screened, normalised report text |
| `enriched_context` | `Optional[str]` | ValidateInputNode | JSON — channel and length provenance |
| `ncr_id` | `Optional[str]` | ValidateInputNode | Validated caller reference |
| `affected_units` | `Optional[int]` | ValidateInputNode | Validated caller figure |
| `lot_size` | `Optional[int]` | ValidateInputNode | Validated caller figure |
| `critical_defect_rate` | `Optional[str]` | ValidateInputNode / inner graph | Escalation threshold in force |
| `defect_category` | `Optional[str]` | DefectClassifyNode | Closed-set label |
| `defect_confidence` | `Optional[str]` | DefectClassifyNode | 0.0–1.0 |
| `severity_level` | `Optional[str]` | SeverityAssessNode | Closed-set label |
| `severity_score` | `Optional[str]` | SeverityAssessNode | 0.0–1.0 |
| `severity_basis` | `Optional[str]` | SeverityAssessNode | Which signal decided the level |
| `defect_rate` | `Optional[str]` | SeverityAssessNode | affected_units / lot_size |
| `root_cause_category` | `Optional[str]` | RootCauseClassifyNode | 5M1E label |
| `root_cause_details` | `Optional[str]` | RootCauseClassifyNode | JSON — matched keyword evidence |
| `routing_target` | `Optional[str]` | RoutingDecideNode | Closed-set label |
| `routing_priority` | `Optional[str]` | RoutingDecideNode | Closed-set label |
| `classification_record` | `Optional[str]` | NCRWorkflowGraphNode | JSON — the inner workflow's result |
| `formatted_output` | `Optional[str]` | SecurityGateOutputNode | The released report, the withheld notice, or the sentence a declined caller reads |
| `result` | `Optional[str]` | SecurityGateOutputNode | The released report or the declined-request sentence; written by the gate only |
| `error_code` | `Optional[str]` | ValidateInputNode | `EMPTY_INPUT` \| `QUESTION_TOO_LONG` \| `INVALID_REQUEST` — carried through the main slot, read by the gate, absent from the envelope |

State constraints: flat `TypedDict` of primitives and JSON-serializable values only; no
credentials; no Pydantic models or arbitrary objects.

## Trust design

The manifest declares `required_trust_level: VERIFIED_EXTERNAL`, and `ValidateInputNode` in the
pre_process slot enforces it. Nothing sets a caller's trust level in a standalone deployment, so
`src/api/server.py` is the boundary that establishes it: when `INVOKE_AUTH_TOKEN` is configured, a
caller no upstream middleware vouched for must present the matching Bearer token to run as a
verified external caller, and trust already established upstream is never demoted.

Without that boundary every request arrives anonymous, the trust gate denies it at the first
template node, and the agent classifies nothing at all while its unit suite stays green.

## Audit events

Every node emits a domain event through `emit_trace_event(name, payload, state)`:
`ncr_validate_input`, `ncr_input_rejected`, `ncr_inner_validate`, `ncr_inner_validate_error`,
`ncr_defect_classify`, `ncr_defect_classify_error`, `ncr_severity_assess`,
`ncr_severity_assess_error`, `ncr_root_cause_classify`, `ncr_root_cause_error`,
`ncr_routing_decide`, `ncr_output_format_inner`, `ncr_output_released`, `ncr_output_withheld`,
`post_process_degraded`.
The HTTP entry point emits `input_context_credential_refused` and `invoke_deadline_exceeded`.

Event payloads carry field names, counts and closed-set labels — never a caller value that failed
a check.

## Framework usage

- `AgentBaseGraph` — outer backbone
- `GraphNode` — `NCRWorkflowGraphNode` in the `main` slot
- `BaseGraph` — inner domain workflow
- `FunctionNode` — all eight leaf nodes
- `AgentState` / `State` — flat state composition
- `detect_credentials` / `detect_credentials_in_value` — the credential detector used by both
  boundaries
- `emit_trace_event` — audit trace

## Import isolation

The template imports `framework/` and `shared/` only. It does not import the platform SDK.

## Design decision record

| Decision | Chosen | Rationale |
|---|---|---|
| Base class | `AgentBaseGraph` | Domain pipeline inheriting directly from the framework |
| Composition | Cat 2 nested | Six sequential classification steps behind one boundary |
| Inner topology | Linear | Classification is always sequential; no branching to route |
| Classification method | Keyword models plus a measured defect rate | Deterministic, testable, no external calls, and explainable to a quality engineer |
| Structured state fields | `Optional[str]` plus `to_json` / `from_json` | Checkpoint serialization compatibility |
| Inner record key | `classification_record`, not `result` | `result` is the framework's caller-visible fallback; only the gate may write it |
| Rejection outcome | A correctable value completes with a reason code; refused content terminates | A terminated turn leaves the caller an exception type and the reason only in the audit trail. Completing is right where the caller can act; presenting a refusal the same way would advertise rewording as a route past it |
| Reason delivery | The sentence reaches the caller; the code stays internal | The codes decide which sentence is rendered. Publishing them in the envelope would make an implementation detail an interface the caller depends on |
| Output invariant | Closed sets, inert identifiers, bounded numbers | The report has no monetary aggregates, and a currency grid would corrupt part and reference codes |
| Negated safety language | Not treated as an escalation | "No safety or functional impact" is the standard cosmetic sign-off; read as a bare keyword hit it escalates a scratch to critical |
