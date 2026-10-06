"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, the gateway calls agent.invoke() directly.

import asyncio
import json
import os
import re
import secrets
from typing import Any, Dict, Optional, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.security.credential_detector import detect_credentials_in_value
from pydantic import BaseModel
from shared.secrets import factory as secrets_factory
from shared.utils.audit_logger import emit_trace_event

from src.graph.graph import NCRClassificationAgent, runtime_config

app = FastAPI(title="Agent")

# The platform registry loads config/config.yaml and passes it as
# Graph(config=...); this adapter mirrors that exactly, so a value declared in
# config.yaml is live in the standalone deployment too rather than silently
# falling back to a built-in default.
_CONFIG = runtime_config()

agent = NCRClassificationAgent(config=_CONFIG)
agent.compile()
# Namespace / agent_name match the manifest values.
agent.provision_secrets(secrets_factory(namespace="mfg-c2-022", agent_name="NCRClassificationAgent"))

# Coarse upper bound on the serialized caller context. The graph enforces the
# per-field bounds (identifier alphabet, finite numeric ranges, lot consistency);
# this stops an oversized payload from reaching it at all.
_MAX_INPUT_CONTEXT_BYTES = 262_144

# Per-request deadline, from config.yaml. Declared there, enforced here — the
# framework has no reader for it, so without this the declaration would be inert.
DEFAULT_TIMEOUT_S = 30.0


def _request_timeout_s(config: Dict[str, Any]) -> float:
    """Return the configured per-request deadline in seconds."""
    value = config.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return DEFAULT_TIMEOUT_S
    return float(value)


TIMEOUT_S = _request_timeout_s(_CONFIG)

# ── Caller-context credential screen ──────────────────────────────────────────
#
# Why this runs before invoke(), not inside a node:
#
# The framework's mandatory output gate scans every value of every node result
# for credential patterns, and the backbone's initialize node copies
# input_context verbatim into its own result. So a credential-shaped string
# anywhere in input_context makes the FIRST node of the graph fail, before any
# template code runs. What the caller gets back is an error status with the whole
# answer withheld and nothing that names input_context, the field, or the reason.
# On a hosted conversation the same context is replayed every turn, so the
# session does not recover on its own.
#
# The request cannot succeed either way. Refusing it here does not change what is
# accepted; it turns an opaque failure into an actionable one.
#
# The screen calls the SAME detector the gate calls, on the SAME assembled
# object, so what this adapter refuses and what the gate blocks are one set by
# construction — there is no local pattern list here that could drift from it.
# Scanning field by field composes exactly to scanning the whole mapping (the
# detector recurses through nested dicts and lists itself), which is what lets
# the refusal name the offending field without widening or narrowing the match.
#
# Field NAMES are caller-controlled too, so a name is echoed back only when it is
# short, inert and carries no credential shape of its own; anything else is
# reported by position. The rejected value and the matched text are never echoed
# — not in the response, not in the audit record.
_SAFE_FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _field_reference(name: object, index: int) -> str:
    """Render a caller-supplied context field name safe to put in a message."""
    if isinstance(name, str) and _SAFE_FIELD_NAME_RE.match(name) and not detect_credentials_in_value(name):
        return f"input_context.{name}"
    return f"input_context field #{index}"


def screen_input_context(input_context: Dict[str, Any]) -> Optional[str]:
    """Return a reference to the first credential-bearing field, else None.

    Walks the top-level fields in caller order and hands each value to the
    framework credential detector, which recurses on its own. Only the first
    offending field is reported: one is enough to act on, and the message stays
    bounded however many fields were sent.
    """
    for index, (name, value) in enumerate(input_context.items(), start=1):
        if detect_credentials_in_value(value):
            return _field_reference(name, index)
    return None


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured caller parameters (channel, ncr_id, affected_units, lot_size,
    # critical_defect_rate) — validated field by field inside the graph; the
    # adapter enforces only the size cap and the credential screen.
    input_context: Optional[Dict[str, Any]] = None


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)

    # Standalone caller authentication. When INVOKE_AUTH_TOKEN is set on the
    # server environment, a caller no upstream middleware vouched for (still
    # anonymous) must present it as a Bearer token to run as a verified external
    # caller. Trust already established by middleware is never demoted.
    #
    # This adapter is the entry-point auth boundary — a deployment-level caller
    # credential, not an agent secret, so the secrets provider does not apply (no
    # InvocationContext exists before authentication).
    #
    # Required here specifically: ValidateInputNode occupies the pre_process slot
    # and declares VERIFIED_EXTERNAL, which is also what the manifest declares
    # for the agent. Nothing else sets request.state.trust_level in a standalone
    # deployment, so without this boundary every request arrives anonymous, the
    # trust gate denies it at the first template node, and the agent classifies
    # nothing at all while its unit suite stays green.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of a clean 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — never leak whether the token was absent,
            # malformed or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    input_context: Dict[str, Any] = req.input_context or {}
    if input_context and len(json.dumps(input_context, default=str)) > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context exceeds the maximum allowed size.")

    offending_field = screen_input_context(input_context)
    if offending_field is not None:
        emit_trace_event(
            "input_context_credential_refused",
            {"field": offending_field},
            {"session_id": req.session_id},
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"{offending_field} contains a credential-shaped value. Remove API keys, "
                "tokens and connection strings from input_context and retry."
            ),
        )

    ctx = InvocationContext(
        session_id=req.session_id or str(uuid4()),
        caller_trust_level=trust,
        caller_id=getattr(request.state, "caller_id", ""),
    )

    def _run() -> Dict[str, Any]:
        with bound_secrets(agent._secrets_provider):
            # invoke() comes from the untyped framework package; its result is
            # the documented output mapping.
            return cast(
                Dict[str, Any],
                agent.invoke(req.input, ctx=ctx, input_context=input_context),
            )

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=TIMEOUT_S)
    except asyncio.TimeoutError:
        emit_trace_event(
            "invoke_deadline_exceeded",
            {"timeout_s": TIMEOUT_S},
            {"session_id": ctx.session_id},
        )
        raise HTTPException(
            status_code=504,
            detail="The classification did not complete within the configured deadline.",
        )


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "NCRClassificationAgent"}
