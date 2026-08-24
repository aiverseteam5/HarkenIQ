"""Air-gapped LLM model integrity (R4-3 P18, OQ-18).

The local inference model is copied to the SM host by an operator
(USB/offline transfer -- no phone-home, no auto-download). Before the
SM trusts it, the file's SHA-256 must match the operator-declared
checksum: a truncated or corrupted GGUF must never serve.

Verification runs once at SM startup; the result feeds provider
selection (mismatch -> NullLLMProvider) and the /healthz model probe.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("harkeniq.sm.model_integrity")

_CHUNK = 1024 * 1024


@dataclass
class ModelInfo:
    """Result of verifying the local model file."""

    status: str  # "ok" | "checksum_mismatch" | "missing" | "unconfigured"
    path: str = ""
    expected_sha256: str = ""
    actual_sha256: str = ""
    size_bytes: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "path": self.path,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "size_bytes": self.size_bytes,
            "detail": self.detail,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(model_path: str, expected_sha256: str) -> ModelInfo:
    """Verify the local model file against its declared checksum.

    - no model_path configured -> "unconfigured" (connected-mode SM;
      not an error)
    - file absent               -> "missing"
    - checksum differs          -> "checksum_mismatch"
    - checksum matches          -> "ok"

    An empty expected_sha256 WITH a path is treated as a mismatch: an
    air-gapped deployment must declare what it expects, otherwise the
    integrity check is theater.
    """
    if not model_path:
        return ModelInfo(status="unconfigured")
    path = Path(model_path)
    if not path.is_file():
        return ModelInfo(
            status="missing", path=model_path,
            expected_sha256=expected_sha256,
            detail=f"model file not found: {model_path}",
        )
    actual = sha256_file(path)
    size = path.stat().st_size
    expected = (expected_sha256 or "").strip().lower()
    if not expected or actual != expected:
        detail = (
            "llm_model_sha256 is required when llm_model_path is set"
            if not expected else
            "model file checksum does not match declared llm_model_sha256"
        )
        logger.error("Model integrity FAILED for %s: %s", model_path, detail)
        return ModelInfo(
            status="checksum_mismatch", path=model_path,
            expected_sha256=expected, actual_sha256=actual,
            size_bytes=size, detail=detail,
        )
    logger.info("Model integrity OK: %s (%d bytes)", model_path, size)
    return ModelInfo(
        status="ok", path=model_path, expected_sha256=expected,
        actual_sha256=actual, size_bytes=size,
    )
