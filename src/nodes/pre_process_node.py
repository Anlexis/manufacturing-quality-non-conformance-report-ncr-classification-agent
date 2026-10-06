"""AgentCore Platform v1.0 — MFG-C2-022 outer backbone: ValidateInputNode.

The single gate for caller data. Everything downstream — the main slot, the whole
inner workflow, the renderer — consumes only what this node has validated.
Nothing here echoes a rejected value: an error names the field and a fixed reason
code, never the content that failed.
"""

import math
import re
import unicodedata
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials_in_value
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress

from src.schemas.state import to_json

# Fallback bounds. The live values arrive from config/config.yaml through the
# constructor; these apply only when a bound is absent from that file.
DEFAULT_MIN_TEXT_CHARS = 10
DEFAULT_MAX_TEXT_CHARS = 8_000

# Largest lot or affected-unit count accepted. Beyond this the request is refused
# rather than clamped, so a caller never silently gets a different defect rate
# from the one their figures imply.
MAX_UNIT_COUNT = 10_000_000

# Every context key this agent consumes. Anything else a CALLER sends is
# refused rather than ignored: a validator that merely ignores an unknown key
# leaves it in the mapping for whatever reads it next, and the framework's own
# output scan reads every value of every node result.
ALLOWED_CONTEXT_KEYS = frozenset({"channel", "ncr_id", "affected_units", "lot_size", "critical_defect_rate"})

# Fields the hosting runtime places on the context channel itself. They are not
# caller parameters, this agent consumes none of them, and they arrive on every
# invocation served that way — refusing them would refuse all of those, and the
# caller cannot remove a field it never added. They are accepted and ignored,
# which is sound precisely because no constraint is attached to them: nothing
# is read from them and nothing is promised about them, so there is no wrong
# expectation for the refusal above to correct.
#
# They are also held out of both refusal screens below, and for the same
# reason those screens exist: they guard values this agent reads, renders or
# carries forward. This field is dropped before any of that, so screening it
# would turn an ordinary earlier turn into a refusal without protecting
# anything.
RUNTIME_CONTEXT_KEYS = frozenset({"conversation_history"})

# Caller strings are locked to inert alphabets. `channel` is audit-only;
# `ncr_id` is rendered into the report, so free text there would be output
# injection.
_CHANNEL_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_NCR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# Strip C0 control characters except tab and newline; the full JA / EN unicode
# range is left intact.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Zero-width and soft-hyphen characters, used to split a directive so a naive
# substring screen no longer sees it.
_ZERO_WIDTH_RE = re.compile(r"[​‌‍⁠﻿­]")

# Markup strip. Applied AFTER the refusal screen, never before: removing a marker
# and forwarding what surrounded it turns a detectable token attack into
# undetectable plain text, because the directive still reads as an instruction
# and nothing downstream can tell it from the caller's own words.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_WS_RE = re.compile(r"\s{2,}")

# ---------------------------------------------------------------------------
# Refusal screen — two families, refused rather than stripped
# ---------------------------------------------------------------------------
#
# Family 1 — chat-template control tokens. These forge a turn boundary in a
# prompt, and no nonconformance report contains one. Screened as a CLASS, not as
# a list of literals, so a template dialect not seen before is still caught.
_CONTROL_TOKEN_PATTERNS: Tuple["re.Pattern[str]", ...] = (
    # <|im_start|>, <|im_end|>, <|system|>, <|endoftext|> and any other <|...|>
    re.compile(r"<\|[^|>]{0,64}\|>"),
    # [INST] / [/INST] / [SYS] / [/SYS]
    re.compile(r"\[/?(?:INST|SYS)\]", re.IGNORECASE),
    # <<SYS>> / <</SYS>>
    re.compile(r"<</?SYS>>", re.IGNORECASE),
    # <system> / </assistant> style role markers
    re.compile(r"<\s*/?\s*(?:system|user|assistant)\s*>", re.IGNORECASE),
    # ChatML-adjacent heading form
    re.compile(r"###\s*(?:system|instruction)\b", re.IGNORECASE),
)

# Family 2 — instruction-override directives. Anchored on a verb plus its object
# so ordinary quality prose survives: a nonconformance report may legitimately
# say "ignore the previous revision of the work instruction sheet", and it does,
# because the object has to be an instruction/prompt/rule word standing alone.
_DIRECTIVE_PATTERNS: Tuple["re.Pattern[str]", ...] = (
    re.compile(
        r"(?i)\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+)*"
        r"(?:previous|prior|above|earlier|system)?\s*"
        r"(?:instruction|instructions|prompt|prompts|rule|rules)\b"
        r"(?!\s+(?:sheet|sheets|document|documents|revision|manual|card))"
    ),
    re.compile(r"(?i)\byou\s+are\s+now\s+(?:a|an|the)\b"),
    re.compile(r"(?i)\bact\s+as\s+(?:a|an)\s+[a-z ]{0,24}\b(?:jailbroken|unrestricted|unfiltered|dan)\b"),
    re.compile(r"(?i)\b(?:reveal|print|show|repeat)\s+(?:me\s+)?(?:your|the)\s+system\s+prompt\b"),
    re.compile(r"(?:以前|前|上記|これまで)の(?:指示|命令|ルール)を?(?:無視|忘れ)"),
    re.compile(r"システムプロンプトを?(?:表示|教え|出力)"),
)


def _screen_forms(text: str) -> List[str]:
    """Return the forms of *text* an injection screen has to inspect.

    A single pass over the raw string is not enough. Zero-width characters and
    compatibility look-alikes are both ways to write a directive that reads
    normally to a person and matches nothing literal, and the markup strip this
    node applies is itself a way to hide one — so the same screen runs over the
    raw text, the zero-width-free text, the compatibility-normalised form, and
    the markup-stripped form.
    """
    forms = [text]
    for candidate in (
        _ZERO_WIDTH_RE.sub("", text),
        unicodedata.normalize("NFKC", _ZERO_WIDTH_RE.sub("", text)),
        _HTML_TAG_RE.sub(" ", text),
    ):
        if candidate not in forms:
            forms.append(candidate)
    return forms


def _screen_reason(text: str) -> Optional[str]:
    """Return a fixed reason code when *text* must be refused, else None.

    The reason codes are constants. They never carry the matched text: the
    framework's output gate scans every value a node returns for credential
    shapes, and quoting caller content into an error makes that gate raise and
    discard the node's whole delta — including any clearing it performed.
    """
    for form in _screen_forms(text):
        for pattern in _CONTROL_TOKEN_PATTERNS:
            if pattern.search(form):
                return "control_token"
        for pattern in _DIRECTIVE_PATTERNS:
            if pattern.search(form):
                return "instruction_override"
    return None


def _screen_structure(value: Any, path: str, depth: int = 0) -> Optional[str]:
    """Depth-first screen over a parsed structure, KEYS included.

    Keys are caller data too, and a directive hidden in a key name reaches the
    same places the values do. Escaped forms cannot evade this: the payload is
    already parsed by the time it arrives, so a \\u-escaped directive is the
    plain string by now.

    Returns "<path>:<reason>" for the first offending position, else None.
    """
    if depth > 6:
        return f"{path}:nesting_depth"
    if isinstance(value, str):
        reason = _screen_reason(value)
        return f"{path}:{reason}" if reason else None
    if isinstance(value, dict):
        for index, (key, item) in enumerate(value.items(), start=1):
            label = key if isinstance(key, str) and _CHANNEL_RE.match(key) else f"#{index}"
            if isinstance(key, str):
                reason = _screen_reason(key)
                if reason:
                    return f"{path}.{label}:key_{reason}"
            found = _screen_structure(item, f"{path}.{label}", depth + 1)
            if found:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _screen_structure(item, f"{path}[{index}]", depth + 1)
            if found:
                return found
        return None
    return None


def _finite_in_range(value: Any, low: float, high: float) -> Optional[float]:
    """Parse *value* as a finite number inside [low, high], else None.

    Rejects bools (isinstance(True, int) is True in Python), non-numerics, and
    NaN / ±Infinity. Those three parse through float() without complaint, and
    every comparison against NaN is False — so a NaN threshold would silently
    disable the escalation it configures. Fails CLOSED.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not math.isfinite(number):
        return None
    if number < low or number > high:
        return None
    return number


def _whole_number(value: Any, low: int, high: int) -> Optional[int]:
    """Parse *value* as a whole number inside [low, high], else None."""
    parsed = _finite_in_range(value, low, high)
    if parsed is None or parsed != int(parsed):
        return None
    return int(parsed)


class ValidateInputNode(FunctionNode):
    """Outer backbone pre_process node — the caller trust gate and input contract.

    Input state keys:
        user_input:    str  — the nonconformance report text
        input_context: dict — optional structured caller parameters:
            channel              str   — inert identifier, recorded for audit
            ncr_id               str   — inert identifier, rendered in the report
            affected_units       int   — units found nonconforming, 0..10,000,000
            lot_size             int   — units in the inspected lot, 1..10,000,000
            critical_defect_rate float — escalation threshold override, 0.0..1.0
        A field the hosting runtime places on that channel itself is accepted
        and dropped; it is not a caller parameter and nothing here reads it.

    Output state keys (partial dict):
        validated_input   str        — screened, normalised report text
        enriched_context  str        — JSON provenance for audit
        ncr_id / affected_units / lot_size / critical_defect_rate — validated
        status / error_log
    """

    # Outer backbone input gate — external callers only. Inner domain nodes use
    # ANONYMOUS (never this value).
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Bind the runtime bounds this node enforces.

        The bounds arrive from config/config.yaml through the graph, so a value
        declared there is the value enforced here rather than a constant that
        happens to agree with it.
        """
        super().__init__()
        ncr: Dict[str, Any] = (config or {}).get("ncr") or {}
        self._min_text_chars = int(ncr.get("min_text_chars", DEFAULT_MIN_TEXT_CHARS))
        self._max_text_chars = int(ncr.get("max_text_chars", DEFAULT_MAX_TEXT_CHARS))

    def _reject(self, reason: str, code: str = "INVALID_REQUEST") -> Dict[str, Any]:
        """Build the rejection delta. *reason* is a field name plus a fixed code.

        Two ways to stop, and the caller can act on only one of them. A value
        the caller can correct completes the run carrying ``code``, so the
        reason reaches the caller and a corrected request can be sent on the
        same conversation. Content the agent refuses outright passes ``code=""``
        and terminates, so a refusal is never presented as something a reworded
        request would get past.

        The branch is chosen by the call site through ``code``, never by
        reading *reason*: the screen and the credential scan pass ``code=""``.
        """
        if code:
            # A value the caller can correct: the run COMPLETES carrying the
            # reason so the request can be sent again on the same conversation.
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": code,
                "error_log": [f"ValidateInputNode: input rejected ({reason})"],
            }
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"ValidateInputNode: input rejected ({reason})"],
        }

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        raw_context = state.get("input_context", {})  # read-only

        if not isinstance(user_input, str) or not user_input.strip():
            emit_trace_event("ncr_input_rejected", {"field": "user_input", "reason": "empty"}, state)
            return self._reject("user_input:empty", code="EMPTY_INPUT")

        if len(user_input) > self._max_text_chars:
            emit_trace_event("ncr_input_rejected", {"field": "user_input", "reason": "length_cap"}, state)
            return self._reject("user_input:length_cap", code="QUESTION_TOO_LONG")

        reason = _screen_reason(user_input)
        if reason:
            emit_trace_event("ncr_input_rejected", {"field": "user_input", "reason": reason}, state)
            # Terminal: a refusal, not a correctable value. Rewording the
            # request must not be presented as a route past it.
            return self._reject(f"user_input:{reason}", code="")

        # Normalise only after the screen has run on every form of the raw text.
        clean = _CONTROL_CHARS_RE.sub("", user_input)
        clean = _HTML_TAG_RE.sub(" ", clean)
        clean = _MULTI_WS_RE.sub(" ", clean).strip()

        if len(clean) < self._min_text_chars:
            emit_trace_event("ncr_input_rejected", {"field": "user_input", "reason": "too_short"}, state)
            return self._reject("user_input:too_short")

        if not isinstance(raw_context, dict):
            emit_trace_event("ncr_input_rejected", {"field": "input_context", "reason": "not_an_object"}, state)
            return self._reject("input_context:not_an_object")

        # Only the caller-authored portion is screened and validated; the
        # runtime's own fields are neither read nor carried forward.
        caller_context = {k: v for k, v in raw_context.items() if k not in RUNTIME_CONTEXT_KEYS}

        # Both refusal screens run BEFORE the unknown-key contract check.
        # A directive or a credential written into an undeclared key matches two
        # rules at once, and only one of them is the real reason: checked the
        # other way round it reads as "a field this agent does not read" - a
        # correctable value - and the refusal is never reported as one.
        offending = _screen_structure(caller_context, "input_context")
        if offending:
            field, _, code = offending.rpartition(":")
            emit_trace_event("ncr_input_rejected", {"field": field, "reason": code}, state)
            # Terminal for the same reason as the user_input screen: a directive
            # hidden in the structured channel is the same refusal.
            return self._reject(offending, code="")

        # An inert alphabet is not a credential screen. An AWS access key ID is
        # twenty uppercase alphanumerics and satisfies the identifier pattern
        # below exactly, so a reference of that shape would pass the contract and
        # be rendered into the report. The entry point screens the same mapping
        # with the same detector, but this node owns the guarantee: it has to
        # hold when the agent is reached by any other route.
        for index, (key, value) in enumerate(caller_context.items(), start=1):
            if detect_credentials_in_value(value):
                label = key if isinstance(key, str) and key in ALLOWED_CONTEXT_KEYS else f"#{index}"
                emit_trace_event("ncr_input_rejected", {"field": label, "reason": "credential_shape"}, state)
                # Terminal: a detected credential is not a value to correct and
                # must never be reported as one.
                return self._reject(f"input_context.{label}:credential_shape", code="")

        unknown = set(caller_context) - ALLOWED_CONTEXT_KEYS
        if unknown:
            # Named rather than silently dropped: a caller who sent a field this
            # agent does not read has a wrong expectation about the answer, and a
            # silent drop hides that until the answer is already wrong.
            emit_trace_event("ncr_input_rejected", {"field": "input_context", "reason": "unknown_field"}, state)
            return self._reject("input_context:unknown_field")

        channel = caller_context.get("channel", "unknown")
        if not isinstance(channel, str) or not _CHANNEL_RE.match(channel):
            if "channel" in caller_context:
                return self._reject("input_context.channel:not_an_identifier")
            channel = "unknown"

        ncr_id: Optional[str] = None
        if "ncr_id" in caller_context:
            candidate = caller_context["ncr_id"]
            if not isinstance(candidate, str) or not _NCR_ID_RE.match(candidate):
                return self._reject("input_context.ncr_id:not_an_identifier")
            ncr_id = candidate

        affected_units: Optional[int] = None
        if "affected_units" in caller_context:
            affected_units = _whole_number(caller_context["affected_units"], 0, MAX_UNIT_COUNT)
            if affected_units is None:
                return self._reject("input_context.affected_units:out_of_bounds")

        lot_size: Optional[int] = None
        if "lot_size" in caller_context:
            lot_size = _whole_number(caller_context["lot_size"], 1, MAX_UNIT_COUNT)
            if lot_size is None:
                return self._reject("input_context.lot_size:out_of_bounds")

        if affected_units is not None and lot_size is not None and affected_units > lot_size:
            return self._reject("input_context.affected_units:exceeds_lot_size")

        threshold: Optional[str] = None
        if "critical_defect_rate" in caller_context:
            parsed = _finite_in_range(caller_context["critical_defect_rate"], 0.0, 1.0)
            if parsed is None:
                return self._reject("input_context.critical_defect_rate:out_of_bounds")
            threshold = repr(parsed)

        enriched = to_json(
            {
                "source": "NCRClassificationAgent",
                "channel": channel,
                "original_length": len(user_input),
                "cleaned_length": len(clean),
                "lot_data_supplied": affected_units is not None and lot_size is not None,
            }
        )

        emit_trace_event(
            "ncr_validate_input",
            {
                "channel": channel,
                "cleaned_length": len(clean),
                "ncr_id_supplied": ncr_id is not None,
                "lot_data_supplied": affected_units is not None and lot_size is not None,
                "threshold_override": threshold is not None,
            },
            state,
        )

        return {
            "validated_input": clean,
            "enriched_context": enriched,
            "ncr_id": ncr_id,
            "affected_units": affected_units,
            "lot_size": lot_size,
            "critical_defect_rate": threshold,
            "status": AgentStatus.SUCCESS.value,
        }
