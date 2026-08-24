"""Structured JSON logging + request-id propagation (R4-0 Phase 3).

Provides:
  - JSON log formatter for all HarkenIQ services
  - Request-ID context variable for cross-service tracing
  - FastAPI middleware for request-id injection/propagation
  - gRPC metadata propagation for Agent -> SM tracing

No external dependencies (uses stdlib logging + json).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Optional

# Context variable for request-id propagation across async boundaries
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

REQUEST_ID_HEADER = "X-Request-Id"
GRPC_REQUEST_ID_KEY = "x-request-id"


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter.

    Output format:
        {"ts": "2026-08-24T10:30:00Z", "level": "INFO", "msg": "...",
         "logger": "harkeniq.sm", "request_id": "abc123", "service": "sm"}
    """

    def __init__(self, service: str = "") -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "ts": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)
            ),
            "level": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name,
        }
        # Add request-id if available
        rid = request_id_var.get("")
        if rid:
            log_entry["request_id"] = rid
        if self._service:
            log_entry["service"] = self._service
        if record.exc_info and record.exc_info[1]:
            log_entry["error"] = str(record.exc_info[1])
        return json.dumps(log_entry)


def configure_logging(
    service: str = "",
    level: str = "INFO",
    json_output: bool = True,
) -> None:
    """Configure structured logging for a HarkenIQ service.

    Args:
        service: Service name (e.g., "sm", "cc", "console", "agent").
        level: Log level (DEBUG, INFO, WARNING, ERROR).
        json_output: If True, use JSON formatter. If False, use standard text.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    if json_output:
        handler.setFormatter(JSONFormatter(service=service))
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)-30s %(message)s"
        ))
    root.addHandler(handler)


def generate_request_id() -> str:
    """Generate a new request-id."""
    return uuid.uuid4().hex[:12]


def get_request_id() -> str:
    """Get the current request-id from context."""
    return request_id_var.get("")


def set_request_id(rid: str) -> None:
    """Set the request-id in the current async context."""
    request_id_var.set(rid)


# -- FastAPI Middleware ----------------------------------------------------

def request_id_middleware(app):
    """FastAPI middleware to inject/propagate X-Request-Id header.

    Usage:
        app = FastAPI()
        app.middleware("http")(request_id_middleware(app))
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    class RequestIdMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            rid = request.headers.get(REQUEST_ID_HEADER, "")
            if not rid:
                rid = generate_request_id()
            request_id_var.set(rid)
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = rid
            return response

    return RequestIdMiddleware
