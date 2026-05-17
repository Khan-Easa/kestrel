"""Phase 6 substep 3: WebSocket route for streaming session executes (STUB).

Substep 3 locks the wire format and lifecycle: route URL, auth handshake,
session validation, close-code semantics, single-execute-per-connection
shape. Substep 4 will wire the body to a real ``SessionRuntime.execute_stream()``
async generator; substep 3 stubs the execute with an empty placeholder
``result`` message so the wire format can be exercised end-to-end before
the streaming runtime exists.

Why this lives in its own file (and not ``sessions.py``):
- WebSocket handlers have a different shape (no ``response_model``, no
``Depends(require_api_key)`` router-level dependency, ``WebSocket`` parameter)
- The streaming runtime client lands here in substep 4 — likely to grow
substantially
- Clean separation of HTTP request/response vs WebSocket lifecycle
"""
from __future__ import annotations

import secrets

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from kestrel.config import Settings, get_settings
from kestrel.execution.session_registry import SessionNotFound, SessionRegistry

logger = structlog.get_logger()

router = APIRouter(prefix="/sessions", tags=["sessions-stream"])

def get_session_registry(websocket: WebSocket) -> SessionRegistry:
    """DI provider — pulls the registry attached to ``app.state`` in the lifespan.

    WebSocket variant of the HTTP provider in ``sessions.py``. FastAPI's
    dependency-injection system fills the parameter based on its type
    annotation; the HTTP one takes ``Request``, this one takes ``WebSocket``,
    each is fed by FastAPI in its respective handler context.
    """
    return websocket.app.state.registry


def _extract_token(websocket: WebSocket) -> str | None:
    """Get bearer token from Authorization header OR ``token`` query param.

    HTTP-style ``Authorization: Bearer <token>`` is the preferred channel —
    works from curl, the Python ``websockets`` library, and most non-browser
    clients. Browsers can't set custom headers on the JavaScript ``WebSocket``
    API, so we accept ``?token=...`` as a fallback. Both are documented;
    clients pick whichever fits their environment.
    """
    auth_header = websocket.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:]
    return websocket.query_params.get("token")


def _auth_ok(provided: str | None, expected: str) -> bool:
    """Constant-time bearer comparison. Empty expected = auth disabled."""
    if expected == "":
        return True
    if not provided:
        return False
    return secrets.compare_digest(provided, expected)


@router.websocket("/{session_id}/execute/stream")
async def execute_in_session_stream(
    websocket: WebSocket,
    session_id: str,
    registry: SessionRegistry = Depends(get_session_registry),
    settings: Settings = Depends(get_settings),
) -> None:
    """Phase 6 substep 3: streaming execute (STUB).

    Substep 3 contract:
    - Auth at handshake: bearer header OR ``token`` query param.
    - 4401 on auth failure; 4404 on unknown session.
    - Accept connection, receive one execute-request message, send one
    ``result`` message, close with 1000.
    - Substep 4 will replace the placeholder result with real streaming.
    """
    token = _extract_token(websocket)
    if not _auth_ok(token, settings.dev_api_key):
        await websocket.close(code=4401, reason="auth_failed")
        return

    try:
        await registry.get_info(session_id)
    except SessionNotFound:
        await websocket.close(code=4404, reason="session_not_found")
        return

    await websocket.accept()
    logger.info(
        "session_stream_accepted",
        session_id_prefix=session_id[:8],
    )

    try:
        # Receive one execute-request message (JSON {"code": "..."}).
        # Substep 4 will parse this and feed to SessionRuntime.execute_stream();
        # substep 3 just drains it so the wire shape is exercised.
        await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info(
            "session_stream_client_disconnect_before_request",
            session_id_prefix=session_id[:8],
        )
        return

    # Substep 3 stub: send an empty placeholder result.
    # Field names mirror SessionExecuteResponse / StreamResult exactly.
    await websocket.send_json({
        "type": "result",
        "stdout": "",
        "stderr": "",
        "exit_code": 0,
        "duration_ms": 0,
        "timed_out": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "outputs": [],
        "dropped_outputs": [],
    })
    await websocket.close(code=1000)
    logger.info(
        "session_stream_closed",
        session_id_prefix=session_id[:8],
        code=1000,
    )