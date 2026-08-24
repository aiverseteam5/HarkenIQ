"""Air-gapped LLM serving tests (R4-3 P18, OQ-18).

Covers: model file integrity verification, the startup gate that
disables the LLM on a bad model, and the SM health endpoint carrying
model metadata (the R4-0 HealthChecker finally wired into /healthz).
"""

from __future__ import annotations

import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq_sm.app import create_app
from harkeniq_sm.config import SMConfig, load_sm_config
from harkeniq_sm.model_integrity import ModelInfo, sha256_file, verify_model
from harkeniq_sm.runtime import make_state


@pytest.fixture
def model_file(tmp_path):
    path = tmp_path / "model.gguf"
    path.write_bytes(b"GGUF" + b"\x00" * 1024)
    return path


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestModelIntegrity:
    def test_unconfigured(self):
        info = verify_model("", "")
        assert info.status == "unconfigured"
        assert info.ok is False  # unconfigured is not "verified ok"

    def test_ok(self, model_file):
        info = verify_model(str(model_file), _sha(model_file))
        assert info.status == "ok"
        assert info.ok is True
        assert info.actual_sha256 == _sha(model_file)
        assert info.size_bytes == 1028

    def test_checksum_case_insensitive(self, model_file):
        info = verify_model(str(model_file), _sha(model_file).upper())
        assert info.status == "ok"

    def test_mismatch(self, model_file):
        info = verify_model(str(model_file), "0" * 64)
        assert info.status == "checksum_mismatch"
        assert info.actual_sha256 == _sha(model_file)

    def test_missing_file(self, tmp_path):
        info = verify_model(str(tmp_path / "absent.gguf"), "0" * 64)
        assert info.status == "missing"

    def test_path_without_checksum_rejected(self, model_file):
        # An air-gapped deployment must declare its expected checksum.
        info = verify_model(str(model_file), "")
        assert info.status == "checksum_mismatch"
        assert "required" in info.detail

    def test_sha256_file_matches_hashlib(self, model_file):
        assert sha256_file(model_file) == _sha(model_file)


def _config(**overrides) -> SMConfig:
    base = dict(insecure=True, llm_enabled=True,
                llm_api_url="http://llama:8080/v1", llm_model="local-gguf")
    base.update(overrides)
    return SMConfig(**base)


def _has_llm_reasoner(state) -> bool:
    return any(
        type(p).__name__ == "LLMReasoner"
        for p in state.ingest.reasoning_pipeline._providers
    )


class TestStartupGate:
    async def test_valid_model_enables_llm(self, model_file):
        state = await make_state(_config(
            llm_model_path=str(model_file),
            llm_model_sha256=_sha(model_file),
        ))
        try:
            assert state.model_info.status == "ok"
            assert _has_llm_reasoner(state)
        finally:
            await state.engine.dispose()

    async def test_corrupt_model_disables_llm(self, model_file):
        state = await make_state(_config(
            llm_model_path=str(model_file),
            llm_model_sha256="0" * 64,
        ))
        try:
            assert state.model_info.status == "checksum_mismatch"
            assert not _has_llm_reasoner(state)
        finally:
            await state.engine.dispose()

    async def test_missing_model_disables_llm(self, tmp_path):
        state = await make_state(_config(
            llm_model_path=str(tmp_path / "absent.gguf"),
            llm_model_sha256="0" * 64,
        ))
        try:
            assert not _has_llm_reasoner(state)
        finally:
            await state.engine.dispose()

    async def test_connected_mode_unaffected(self):
        # No model path configured: cloud/connected mode works as before.
        state = await make_state(_config())
        try:
            assert state.model_info.status == "unconfigured"
            assert _has_llm_reasoner(state)
        finally:
            await state.engine.dispose()


class TestHealthEndpoint:
    async def _client(self, state):
        app = create_app(state)
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    async def test_healthy_with_model_metadata(self, model_file):
        state = await make_state(_config(
            llm_model_path=str(model_file),
            llm_model_sha256=_sha(model_file),
        ))
        try:
            async with await self._client(state) as c:
                r = await c.get("/healthz")
            data = r.json()
            assert data["status"] == "ok"
            assert data["checks"]["database"] is True
            assert data["checks"]["llm_model"] is True
            assert data["llm_model"]["status"] == "ok"
            assert data["llm_model"]["size_bytes"] == 1028
            assert data["llm_model"]["actual_sha256"] == _sha(model_file)
        finally:
            await state.engine.dispose()

    async def test_degraded_on_bad_model(self, model_file):
        state = await make_state(_config(
            llm_model_path=str(model_file),
            llm_model_sha256="0" * 64,
        ))
        try:
            async with await self._client(state) as c:
                r = await c.get("/healthz")
            data = r.json()
            assert data["status"] == "degraded"
            assert data["checks"]["llm_model"] is False
            assert data["llm_model"]["status"] == "checksum_mismatch"
        finally:
            await state.engine.dispose()


class TestConfig:
    def test_model_env_vars(self):
        config = load_sm_config(env={
            "HARKEN_SM_LLM_MODEL_PATH": "/models/m.gguf",
            "HARKEN_SM_LLM_MODEL_SHA256": "ab" * 32,
        })
        assert config.llm_model_path == "/models/m.gguf"
        assert config.llm_model_sha256 == "ab" * 32


class TestComposeService:
    def test_llama_service_defined(self):
        import yaml
        from pathlib import Path

        compose = yaml.safe_load(
            (Path(__file__).parents[3] / "deploy" / "full-stack"
             / "docker-compose.yml").read_text()
        )
        llama = compose["services"]["llama"]
        assert "llama.cpp" in llama["image"]
        assert llama["profiles"] == ["airgap-llm"]
        assert any("/models" in v for v in llama["volumes"])
        assert "healthcheck" in llama
