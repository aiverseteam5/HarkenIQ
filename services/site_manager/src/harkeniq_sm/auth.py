"""Bearer-token auth for both transports (spec §7 R2a, A1.2).

One site token, checked with a constant-time compare. gRPC gets a server
interceptor; FastAPI gets a dependency. mTLS and per-user RBAC arrive
with Keycloak in R2b — do not grow interim user handling here.
"""

from __future__ import annotations

import hmac

import grpc


def token_matches(provided: str, expected: str) -> bool:
    """Constant-time compare; empty expected never matches."""
    if not expected:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


#: The one bootstrap RPC that cannot yet hold the site token: CC calls it
#: to OBTAIN the token. It authenticates via license fingerprint inside
#: the handler instead (spec §3: the license binds the pair at deploy).
#: QA-037: requiring the token here made site registration impossible —
#: a chicken-and-egg that shipped because no test drove CC->SM over the
#: real wire.
_BOOTSTRAP_METHODS = ("/harkeniq.v1.SiteManagerService/RegisterSite",)


class BearerTokenInterceptor(grpc.aio.ServerInterceptor):
    """Rejects any RPC without ``authorization: Bearer <site_token>``."""

    def __init__(self, site_token: str) -> None:
        self._expected = f"Bearer {site_token}"

        async def abort(request, context):  # noqa: ANN001 - grpc signature
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid site token")

        self._reject = grpc.unary_unary_rpc_method_handler(abort)

    async def intercept_service(self, continuation, handler_call_details):
        if handler_call_details.method in _BOOTSTRAP_METHODS:
            return await continuation(handler_call_details)
        metadata = dict(handler_call_details.invocation_metadata or ())
        provided = metadata.get("authorization", "")
        if hmac.compare_digest(provided.encode(), self._expected.encode()):
            return await continuation(handler_call_details)
        return self._reject
