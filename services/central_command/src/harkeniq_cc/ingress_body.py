"""A24.14: bound an external body BEFORE it is parsed.

Schema field limits reject a payload the server has already read into
memory and parsed. Confirmed against the running app: a 5MB body returned
422, meaning every byte was received and the whole document parsed before
anything refused it. For an authenticated internal caller that is merely
wasteful; for the external machine-write boundary A6 opens it is an
unauthenticated-by-volume allocation channel.

WHAT THIS COUNTS
----------------
Bytes actually delivered by the ASGI `receive` channel, accumulated across
every `http.request` message. That is the only measure that is correct for
all of:

* a declared `Content-Length` -- refused early, before reading, when the
  declaration alone is already over;
* a chunked body with no declared length -- caught while streaming;
* a body that UNDERSTATES its declared length -- the declaration is a
  hint, never the authority, so the running total decides;
* whitespace-heavy or otherwise malformed JSON -- this layer never parses,
  so a document that would explode a parser is refused by size first.

Scoped to the ingress path rather than applied globally: other CC routes
legitimately take larger bodies (a CVE feed import, a config policy), and
a platform-wide ceiling chosen for a four-field object would break them.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("harkeniq.cc.ingress_body")

#: The A6 submission body carries four short fields. 16 KiB is orders of
#: magnitude above any honest one and far below anything worth allocating.
MAX_INGRESS_BODY_BYTES = 16 * 1024

#: Only the external machine-write surface. A ceiling sized for this body
#: would be wrong for the rest of the API.
_GUARDED_SUFFIX = "/proposals"
_GUARDED_PREFIX = "/api/operational-agents/"


def _is_guarded(scope) -> bool:
    if scope.get("type") != "http" or scope.get("method") != "POST":
        return False
    path = scope.get("path", "") or ""
    return path.startswith(_GUARDED_PREFIX) and path.endswith(_GUARDED_SUFFIX)


def _too_large(limit: int):
    body = json.dumps({
        "detail": (
            f"request body exceeds the {limit}-byte limit for this endpoint"
        )
    }).encode("utf-8")
    return body


class IngressBodyLimit:
    """Pure-ASGI middleware: refuse an oversized body before it is parsed.

    BUFFER, DECIDE, THEN REPLAY. The first implementation wrapped
    `receive` and returned `http.disconnect` once the running total went
    over. That worked only when `Content-Length` declared the size
    honestly; measured, the other cases came back 400, not 413:

        honest Content-Length ......... 413
        chunked / no length ........... 400  <- the app saw a truncated body
        understated Content-Length .... 400  <- and answered before we could

    A disconnect mid-stream is indistinguishable, to Starlette, from a
    client that hung up, so FastAPI produced its own parse failure and
    responded first -- and once the app has sent a response, the 413 can
    no longer be sent. The limit was being enforced by whoever answered
    first, which is not enforcement.

    So this never hands the application a body it might refuse to parse.
    It reads at most `limit + 1` bytes; over the limit it answers 413
    itself, having never invoked the app; within the limit it replays the
    buffered messages verbatim, so the application sees exactly the
    request the client sent.

    Buffering is bounded by the limit itself -- 16 KiB -- which is the
    property that makes reading-before-deciding safe here.
    """

    def __init__(self, app, limit: int = MAX_INGRESS_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send) -> None:
        if not _is_guarded(scope):
            await self.app(scope, receive, send)
            return

        # A declared length already over the ceiling is refused without
        # reading a byte. A shortcut only: an understated or absent
        # declaration is caught by the running total below, which is why
        # the loop cannot be skipped when this passes.
        if _declared_length(scope) > self.limit:
            await _refuse(send, self.limit)
            return

        buffered: list[dict] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                buffered.append(message)
                break
            body = message.get("body", b"") or b""
            total += len(body)
            if total > self.limit:
                # Answered here, with the application never invoked, so
                # nothing else can respond first and no parser ever sees
                # these bytes.
                await _refuse(send, self.limit)
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break

        pending = iter(buffered)

        async def replay():
            try:
                return next(pending)
            except StopIteration:
                # The stream is exhausted. An empty terminal chunk rather
                # than a disconnect: the body was complete and well
                # formed, and a disconnect here would make the app think
                # the client hung up.
                return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)


def _declared_length(scope) -> int:
    for name, value in scope.get("headers", []) or []:
        if name.lower() == b"content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


async def _refuse(send, limit: int) -> None:
    body = _too_large(limit)
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})
