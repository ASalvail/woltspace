"""The local connector API, `/connector/v1/*`: the only routes a connector token opens.

Admission here is its own thing and touches no other route: exactly one
well-formed `Authorization: Bearer wct_...` for a live token (anything else is
401, never retried as the token-less owner), presented from the owning machine
(a valid token from anywhere else is 403). Every answer is `Cache-Control:
no-store`; nothing here is a document a browser should keep.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import SPACE_DIR
from .connectors import (
    ConnectorError,
    ConnectorStateUnreadable,
    ConnectorStore,
    Principal,
    TOKEN_PREFIX,
)
from .local_request import owner_local_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connector/v1")

CONNECTORS_DIR = SPACE_DIR / "connectors"
_store: ConnectorStore | None = None
_CHUNK = 64 * 1024
_POLL_INTERVAL = 0.2


def get_store() -> ConnectorStore:
    global _store
    if _store is None or _store.root != CONNECTORS_DIR:
        _store = ConnectorStore(CONNECTORS_DIR)
    return _store


class _Refused(Exception):
    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body


def _bearer(request: Request) -> str:
    """The one token the request presents, or nothing a token-less path may use."""
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise _Refused(401, {"error": "connector token required"})
    scheme, _, token = values[0].strip().partition(" ")
    token = token.strip()
    if scheme != "Bearer" or not token.startswith(TOKEN_PREFIX) or " " in token:
        raise _Refused(401, {"error": "connector token required"})
    return token


def _authenticate(request: Request, store: ConnectorStore) -> tuple[Principal, str]:
    token = _bearer(request)
    try:
        who = store.authenticate(token)
    except ConnectorStateUnreadable:
        # Row 1: an unreadable registry refuses every bearer token; it never
        # falls back to the ambient local caller.
        logger.error("connector registry unreadable; refusing every connector token")
        raise _Refused(401, {"error": "connector token required"}) from None
    if who is None:
        raise _Refused(401, {"error": "connector token required"})
    hostname = (request.url.hostname or "").lower().rstrip(".")
    if not owner_local_request(request.scope, hostname):
        raise _Refused(403, {"error": "connector tokens are local-only"})
    return who, token


async def _run(
    request: Request, handler: Callable[[ConnectorStore, Principal, str], Awaitable[Response]],
) -> Response:
    store = get_store()
    try:
        who, token = _authenticate(request, store)
        response = await handler(store, who, token)
    except _Refused as exc:
        response = JSONResponse(exc.body, status_code=exc.status)
    except ConnectorError as exc:
        if exc.status == 429:
            logger.warning("connector limit hit: %s", exc.reason)
        response = JSONResponse({"error": str(exc), "reason": exc.reason}, status_code=exc.status)
    except ConnectorStateUnreadable as exc:
        logger.error("connector state unreadable: %s", exc)
        response = JSONResponse({"error": "connector state unreadable"}, status_code=503)
    response.headers["Cache-Control"] = "no-store"
    return response


async def _json_body(request: Request) -> object:
    try:
        return await request.json()
    except ValueError:
        raise ConnectorError(400, "malformed_json", "body is not JSON") from None


@router.post("/events")
async def post_event(request: Request):
    async def handle(store, who, _token):
        status, body = store.accept_event(who, await _json_body(request))
        return JSONResponse(body, status_code=status)
    return await _run(request, handle)


@router.post("/uploads")
async def post_upload(request: Request):
    async def handle(store, who, _token):
        upload_id, part = store.begin_upload(who)
        received = 0
        try:
            descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as sink:
                # Counted as the bytes arrive; Content-Length is nobody's promise.
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    received += len(chunk)
                    store.account_upload_bytes(who, upload_id, received)
                    sink.write(chunk)
        except BaseException:
            store.abort_upload(who, upload_id, part)
            raise
        return JSONResponse(store.commit_upload(who, upload_id, part, request.query_params.get("name")))
    return await _run(request, handle)


@router.post("/deliveries/lease")
async def post_lease(request: Request):
    async def handle(store, who, token):
        body = await _json_body(request)
        if not isinstance(body, dict):
            raise ConnectorError(400, "malformed_lease")
        max_items = body.get("max", 1)
        wait = body.get("wait", 0)
        if isinstance(max_items, bool) or not isinstance(max_items, int):
            raise ConnectorError(400, "malformed_lease", "max must be an integer")
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0:
            raise ConnectorError(400, "malformed_lease", "wait must be a number of seconds")
        deadline = asyncio.get_running_loop().time() + min(float(wait), store.limits.lease_max_wait_seconds)
        while True:
            result = store.lease(who, max_items)
            if result["deliveries"] or result["paused"]:
                return JSONResponse(result)
            if asyncio.get_running_loop().time() >= deadline:
                return JSONResponse(result)
            await asyncio.sleep(_POLL_INTERVAL)
            # A long poll authorized before a revoke hands out nothing after
            # it; a disable in the meantime is reported as paused, not refused.
            fresh = store.authenticate(token)
            if fresh is None or (fresh.instance_id, fresh.generation) != (who.instance_id, who.generation):
                raise _Refused(401, {"error": "connector token required"})
            who = fresh
    return await _run(request, handle)


@router.post("/deliveries/{delivery_id}/ack")
async def post_ack(delivery_id: str, request: Request):
    async def handle(store, who, _token):
        return JSONResponse(store.ack(who, delivery_id, await _json_body(request)))
    return await _run(request, handle)


@router.get("/files/{file_id}")
async def get_file(file_id: str, request: Request):
    async def handle(store, who, token):
        path, attachment = store.open_file(who, file_id, request.query_params.get("attempt_id"))
        try:
            handle = open(path, "rb")
        except OSError:
            raise ConnectorError(404, "file_not_available") from None

        async def stream():
            with handle:
                while True:
                    # Revocation cuts an open stream at the next chunk.
                    if store.authenticate(token) != who:
                        return
                    chunk = handle.read(_CHUNK)
                    if not chunk:
                        return
                    yield chunk

        return StreamingResponse(
            stream(), media_type="application/octet-stream",
            headers={
                "Content-Length": str(attachment["size"]),
                "X-Content-Type-Options": "nosniff",
                "X-Woltspace-Attachment-Name": attachment["name"].encode("ascii", "replace").decode("ascii"),
                "X-Woltspace-Attachment-Kind": attachment["kind"],
            },
        )
    return await _run(request, handle)
