"""Tests for CredentialProvider interface and implementations (R3b-3 Phase 1)."""

from __future__ import annotations

import json
import time

import pytest
import httpx

from harkeniq.security.credentials import (
    Credential,
    CredentialProviderChain,
    LocalCredentialProvider,
    MockCredentialProvider,
    VaultCredentialProvider,
)


# -- Credential dataclass tests --------------------------------------------


class TestCredential:
    def test_not_expired_no_ttl(self):
        cred = Credential(username="u", password="p", ttl=0)
        assert cred.is_expired is False

    def test_not_expired_within_ttl(self):
        cred = Credential(username="u", password="p", ttl=3600)
        assert cred.is_expired is False

    def test_expired_past_ttl(self):
        cred = Credential(
            username="u", password="p",
            ttl=10, fetched_at=time.time() - 100,
        )
        assert cred.is_expired is True

    def test_source_field(self):
        cred = Credential(username="u", password="p", source="vault")
        assert cred.source == "vault"


# -- LocalCredentialProvider tests ------------------------------------------


class TestLocalCredentialProvider:
    async def test_returns_config_credentials(self):
        provider = LocalCredentialProvider({
            "bmc": {"username": "admin", "password": "secret123"}
        })
        cred = await provider.get_credentials("device-x")
        assert cred is not None
        assert cred.username == "admin"
        assert cred.password == "secret123"
        assert cred.source == "local"

    async def test_returns_none_without_config(self):
        provider = LocalCredentialProvider({})
        cred = await provider.get_credentials("device-x")
        assert cred is None

    async def test_per_device_override(self):
        provider = LocalCredentialProvider({
            "bmc": {"username": "default", "password": "default"}
        })
        await provider.store_credentials("device-x", "custom", "custom-pw")
        cred = await provider.get_credentials("device-x")
        assert cred.username == "custom"
        assert cred.password == "custom-pw"

    async def test_fallback_to_global(self):
        provider = LocalCredentialProvider({
            "bmc": {"username": "global", "password": "global-pw"}
        })
        await provider.store_credentials("device-x", "custom", "custom-pw")
        cred = await provider.get_credentials("device-y")
        assert cred.username == "global"

    def test_provider_name(self):
        assert LocalCredentialProvider().provider_name == "local"


# -- MockCredentialProvider tests -------------------------------------------


class TestMockCredentialProvider:
    async def test_returns_default_mock(self):
        provider = MockCredentialProvider()
        cred = await provider.get_credentials("any-device")
        assert cred is not None
        assert cred.username == "mock-admin"
        assert cred.source == "mock"

    async def test_returns_configured_mock(self):
        provider = MockCredentialProvider(
            creds={"device-x": ("special-user", "special-pass")}
        )
        cred = await provider.get_credentials("device-x")
        assert cred.username == "special-user"

    async def test_store_and_retrieve(self):
        provider = MockCredentialProvider()
        assert await provider.store_credentials("d1", "u1", "p1") is True
        cred = await provider.get_credentials("d1")
        assert cred.username == "u1"

    def test_provider_name(self):
        assert MockCredentialProvider().provider_name == "mock"


# -- VaultCredentialProvider tests (with httpx mock) ------------------------


class TestVaultCredentialProvider:
    async def test_get_credentials_success(self, httpx_mock):
        """Vault returns credentials successfully."""
        httpx_mock.add_response(
            url="http://vault:8200/v1/secret/data/bmc/device-x",
            json={
                "data": {
                    "data": {
                        "username": "vault-admin",
                        "password": "vault-secret",
                        "ttl": 300,
                    }
                }
            },
        )
        provider = VaultCredentialProvider(
            vault_url="http://vault:8200",
            vault_token="test-token",
        )
        cred = await provider.get_credentials("device-x")
        assert cred is not None
        assert cred.username == "vault-admin"
        assert cred.password == "vault-secret"
        assert cred.source == "vault"
        assert cred.ttl == 300.0

    async def test_get_credentials_not_found(self, httpx_mock):
        httpx_mock.add_response(
            url="http://vault:8200/v1/secret/data/bmc/missing",
            status_code=404,
        )
        provider = VaultCredentialProvider(
            vault_url="http://vault:8200",
            vault_token="test-token",
        )
        cred = await provider.get_credentials("missing")
        assert cred is None

    async def test_get_credentials_timeout(self, httpx_mock):
        httpx_mock.add_exception(
            httpx.TimeoutException("connection timed out"),
            url="http://vault:8200/v1/secret/data/bmc/slow",
        )
        provider = VaultCredentialProvider(
            vault_url="http://vault:8200",
            vault_token="test-token",
        )
        cred = await provider.get_credentials("slow")
        assert cred is None

    async def test_store_credentials_success(self, httpx_mock):
        httpx_mock.add_response(
            url="http://vault:8200/v1/secret/data/bmc/device-y",
            json={"data": {"version": 1}},
        )
        provider = VaultCredentialProvider(
            vault_url="http://vault:8200",
            vault_token="test-token",
        )
        result = await provider.store_credentials("device-y", "u", "p")
        assert result is True

    async def test_store_credentials_failure(self, httpx_mock):
        httpx_mock.add_response(
            url="http://vault:8200/v1/secret/data/bmc/device-z",
            status_code=403,
        )
        provider = VaultCredentialProvider(
            vault_url="http://vault:8200",
            vault_token="bad-token",
        )
        result = await provider.store_credentials("device-z", "u", "p")
        assert result is False

    async def test_vault_token_header(self, httpx_mock):
        httpx_mock.add_response(
            url="http://vault:8200/v1/secret/data/bmc/device-x",
            json={"data": {"data": {"username": "u", "password": "p"}}},
        )
        provider = VaultCredentialProvider(
            vault_url="http://vault:8200",
            vault_token="my-root-token",
        )
        await provider.get_credentials("device-x")
        request = httpx_mock.get_request()
        assert request.headers["x-vault-token"] == "my-root-token"

    def test_provider_name(self):
        provider = VaultCredentialProvider("http://v:8200", "tok")
        assert provider.provider_name == "vault"


# -- CredentialProviderChain tests ------------------------------------------


class TestCredentialProviderChain:
    async def test_first_success_wins(self):
        mock1 = MockCredentialProvider(
            creds={"device-x": ("first", "first-pw")}
        )
        mock2 = MockCredentialProvider(
            creds={"device-x": ("second", "second-pw")}
        )
        chain = CredentialProviderChain([mock1, mock2])
        cred = await chain.get_credentials("device-x")
        assert cred.username == "first"

    async def test_fallback_on_first_failure(self):
        # First provider has no creds for this device (returns default mock)
        # But let's make a chain where first fails and second succeeds
        failing = LocalCredentialProvider({})  # returns None (no config)
        fallback = MockCredentialProvider(
            creds={"device-x": ("fallback", "fb-pw")}
        )
        chain = CredentialProviderChain([failing, fallback])
        cred = await chain.get_credentials("device-x")
        assert cred.username == "fallback"

    async def test_all_fail_returns_none(self):
        empty1 = LocalCredentialProvider({})
        empty2 = LocalCredentialProvider({})
        chain = CredentialProviderChain([empty1, empty2])
        cred = await chain.get_credentials("device-x")
        assert cred is None

    async def test_store_first_success(self):
        mock1 = MockCredentialProvider()
        mock2 = MockCredentialProvider()
        chain = CredentialProviderChain([mock1, mock2])
        result = await chain.store_credentials("d1", "u", "p")
        assert result is True
        # Stored in first provider
        cred = await mock1.get_credentials("d1")
        assert cred.username == "u"

    def test_provider_name(self):
        chain = CredentialProviderChain([
            MockCredentialProvider(),
            LocalCredentialProvider(),
        ])
        assert "chain" in chain.provider_name
        assert "mock" in chain.provider_name
        assert "local" in chain.provider_name


# -- pytest-httpx fixture --------------------------------------------------

@pytest.fixture
def httpx_mock():
    """Simple httpx mock for Vault HTTP tests."""
    return _HttpxMock()


class _HttpxMock:
    """Minimal httpx mock that patches AsyncClient for testing."""

    def __init__(self):
        self._responses = {}
        self._exceptions = {}
        self._requests = []
        self._original_get = None
        self._original_post = None
        self._setup()

    def _setup(self):
        import httpx as _httpx
        mock = self

        class _MockResponse:
            def __init__(self, status_code, json_data=None):
                self.status_code = status_code
                self._json = json_data or {}
                self.text = json.dumps(self._json) if self._json else ""
                self.headers = {}

            def json(self):
                return self._json

            def raise_for_status(self):
                if self.status_code >= 400:
                    resp = _httpx.Response(self.status_code, request=_httpx.Request("GET", ""))
                    raise _httpx.HTTPStatusError(
                        f"HTTP {self.status_code}",
                        request=_httpx.Request("GET", ""),
                        response=resp,
                    )

        class _MockClient:
            def __init__(self_inner, **kwargs):
                pass  # accept timeout= and other kwargs

            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *args):
                pass

            async def get(self_inner, url, **kwargs):
                mock._requests.append(_httpx.Request("GET", url, headers=kwargs.get("headers", {})))
                if url in mock._exceptions:
                    raise mock._exceptions[url]
                if url in mock._responses:
                    r = mock._responses[url]
                    return _MockResponse(r.get("status_code", 200), r.get("json"))
                return _MockResponse(404)

            async def post(self_inner, url, **kwargs):
                mock._requests.append(_httpx.Request("POST", url, headers=kwargs.get("headers", {})))
                if url in mock._exceptions:
                    raise mock._exceptions[url]
                if url in mock._responses:
                    r = mock._responses[url]
                    return _MockResponse(r.get("status_code", 200), r.get("json"))
                return _MockResponse(404)

        self._original_init = _httpx.AsyncClient.__init__
        _httpx.AsyncClient = _MockClient

    def add_response(self, url, json=None, status_code=200):
        self._responses[url] = {"json": json, "status_code": status_code}

    def add_exception(self, exc, url):
        self._exceptions[url] = exc

    def get_request(self):
        return self._requests[-1] if self._requests else None

    def __del__(self):
        pass  # cleanup handled by test teardown
