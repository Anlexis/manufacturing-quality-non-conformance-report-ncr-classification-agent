"""Minimal in-process HTTP driver for the agent's ASGI app.

Used by the boundary tests so they exercise the real entry point — routing,
request model, auth, error responses — rather than calling the graph directly.

httpx drives the app over its ASGI interface directly. Nothing here starts a
server or opens a socket.
"""

import asyncio
from typing import Any, Dict, Optional

import httpx


class Response:
    """The parts of an HTTP response these tests assert on."""

    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def request(
    app: Any,
    method: str,
    path: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    content: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Response:
    """Send one request to *app* and return its status and decoded body."""

    async def _send() -> Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            response = await client.request(
                method,
                path,
                json=json_body,
                content=content,
                headers=headers,
            )
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        return Response(response.status_code, payload)

    return asyncio.run(_send())
