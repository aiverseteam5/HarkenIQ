"""Async Redfish HTTP client for BMC communication (Doc 10 §2.3).

Manages sessions (X-Auth-Token), TLS, retries on 503, and auto-renewal
on 401. All BMC communication goes through this client.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any, Optional

import aiohttp

from harkeniq.errors import (
    RedfishAuthError,
    RedfishConnectionError,
    RedfishResponseError,
    RedfishTimeoutError,
)

logger = logging.getLogger("harkeniq.redfish.client")

# Retry config
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1  # seconds; doubles each retry (1, 2, 4)
RETRY_BACKOFF_MAX = 30


class RedfishClient:
    """Async HTTP client for Redfish API communication with a single BMC."""

    def __init__(
        self,
        host: str,
        port: int = 443,
        verify_ssl: bool = False,
        session_timeout: int = 300,
        request_timeout: int = 30,
    ):
        self.host = host
        self.port = port
        self.verify_ssl = verify_ssl
        self.session_timeout = session_timeout
        self.request_timeout = request_timeout

        self._base_url = self._build_base_url(host, port)
        self._token: Optional[str] = None
        self._session_uri: Optional[str] = None
        self._username: Optional[str] = None
        self._password: Optional[str] = None
        self._http: Optional[aiohttp.ClientSession] = None
        self._ssl_context: Optional[ssl.SSLContext] = None

    @staticmethod
    def _build_base_url(host: str, port: int) -> str:
        if host.startswith("https://") or host.startswith("http://"):
            return host.rstrip("/")
        return f"https://{host}:{port}"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, username: str, password: str) -> None:
        """Establish a Redfish session with the BMC."""
        self._username = username
        self._password = password

        if not self.verify_ssl:
            self._ssl_context = ssl.create_default_context()
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE

        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        self._http = aiohttp.ClientSession(timeout=timeout)

        await self._create_session()

    async def _create_session(self) -> None:
        """Create a Redfish session and store the auth token."""
        url = f"{self._base_url}/redfish/v1/SessionService/Sessions"
        body = {"UserName": self._username, "Password": self._password}

        try:
            async with self._http.post(url, json=body, ssl=self._ssl_context) as resp:
                if resp.status == 401 or resp.status == 403:
                    raise RedfishAuthError(self.host, resp.status)
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise RedfishResponseError(self.host, "/SessionService/Sessions", resp.status, text)

                self._token = resp.headers.get("X-Auth-Token", "")
                data = await resp.json()
                self._session_uri = data.get("@odata.id", "")
                logger.info("Redfish session created: %s", self.host)
        except aiohttp.ClientConnectorError as e:
            raise RedfishConnectionError(self.host, self.port, e) from e
        except asyncio.TimeoutError:
            raise RedfishTimeoutError(self.host, "/SessionService/Sessions", self.request_timeout)

    async def close(self) -> None:
        """Close the aiohttp session and release connections."""
        if self._http and not self._http.closed:
            await self._http.close()
            self._http = None
        self._token = None

    async def delete_session(self) -> None:
        """Close the Redfish session (DELETE the session resource)."""
        if self._session_uri and self._http and not self._http.closed:
            try:
                url = f"{self._base_url}{self._session_uri}"
                headers = {"X-Auth-Token": self._token} if self._token else {}
                async with self._http.delete(url, headers=headers, ssl=self._ssl_context) as resp:
                    logger.info("Redfish session deleted: %s (status %d)", self.host, resp.status)
            except Exception as e:
                logger.warning("Failed to delete Redfish session: %s", e)
        self._token = None
        self._session_uri = None

    # ------------------------------------------------------------------
    # HTTP methods
    # ------------------------------------------------------------------

    async def get(self, path: str) -> dict[str, Any]:
        """Execute GET request with retry and session renewal."""
        return await self._request("GET", path)

    async def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Execute PATCH request."""
        return await self._request("PATCH", path, json_body=body)

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Execute POST request."""
        return await self._request("POST", path, json_body=body)

    async def delete(self, path: str) -> dict[str, Any]:
        """Execute DELETE request (QA-034: account removal)."""
        return await self._request("DELETE", path)

    # ------------------------------------------------------------------
    # Internal request handling with retry
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Execute an HTTP request with retry on 503 and re-auth on 401."""
        url = f"{self._base_url}{path}"
        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            headers = {}
            if self._token:
                headers["X-Auth-Token"] = self._token

            try:
                kwargs: dict[str, Any] = {"headers": headers, "ssl": self._ssl_context}
                if json_body is not None:
                    kwargs["json"] = json_body

                async with self._http.request(method, url, **kwargs) as resp:
                    # 401: session expired, re-auth and retry
                    if resp.status == 401:
                        logger.info("Session expired, re-authenticating: %s", self.host)
                        try:
                            await self._create_session()
                        except RedfishAuthError:
                            raise
                        continue

                    # 503: server busy, respect Retry-After
                    if resp.status == 503:
                        retry_after = int(resp.headers.get("Retry-After", "1"))
                        wait = min(retry_after, RETRY_BACKOFF_MAX)
                        logger.info("BMC busy (503), retrying after %ds: %s", wait, path)
                        await asyncio.sleep(wait)
                        continue

                    # 404: resource not found (not retried)
                    if resp.status == 404:
                        raise RedfishResponseError(self.host, path, 404, "Not Found")

                    # Other errors (>= 400)
                    if resp.status >= 400:
                        text = await resp.text()
                        raise RedfishResponseError(self.host, path, resp.status, text)

                    # Success (DELETE and some actions return no JSON body)
                    if resp.status == 204:
                        return {}
                    try:
                        return await resp.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        return {}

            except aiohttp.ClientConnectorError as e:
                last_error = RedfishConnectionError(self.host, self.port, e)
                backoff = min(RETRY_BACKOFF_BASE * (2 ** attempt), RETRY_BACKOFF_MAX)
                logger.warning("Connection error (attempt %d/%d), retrying in %ds: %s", attempt + 1, MAX_RETRIES, backoff, e)
                await asyncio.sleep(backoff)

            except asyncio.TimeoutError:
                last_error = RedfishTimeoutError(self.host, path, self.request_timeout)
                backoff = min(RETRY_BACKOFF_BASE * (2 ** attempt), RETRY_BACKOFF_MAX)
                logger.warning("Timeout (attempt %d/%d), retrying in %ds: %s", attempt + 1, MAX_RETRIES, backoff, path)
                await asyncio.sleep(backoff)

        # All retries exhausted
        if last_error:
            raise last_error
        raise RedfishConnectionError(self.host, self.port, None)
