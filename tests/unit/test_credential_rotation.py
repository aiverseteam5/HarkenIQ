"""Tests for CredentialRotator (R3b-3 Phase 6)."""

from __future__ import annotations

import pytest

from harkeniq.security.credential_rotation import (
    CredentialRotator,
    RotationEvent,
    RotationStatus,
    generate_password,
)
from harkeniq.security.credentials import MockCredentialProvider


class TestGeneratePassword:
    def test_length(self):
        pw = generate_password(24)
        assert len(pw) == 24

    def test_contains_mixed_chars(self):
        pw = generate_password(100)
        assert any(c.isupper() for c in pw)
        assert any(c.islower() for c in pw)
        assert any(c.isdigit() for c in pw)

    def test_unique(self):
        passwords = {generate_password() for _ in range(10)}
        assert len(passwords) == 10  # all unique


class TestCredentialRotation:
    async def test_successful_rotation(self):
        mock_creds = MockCredentialProvider()
        rotator = CredentialRotator(credential_provider=mock_creds)
        event = await rotator.rotate("device-x", "old-admin")
        assert event.status == RotationStatus.SUCCESS
        assert event.old_username == "old-admin"
        assert event.new_username.startswith("harken-")
        assert event.completed_at is not None

    async def test_custom_new_username(self):
        rotator = CredentialRotator()
        event = await rotator.rotate("device-x", "old", new_username="new-admin")
        assert event.new_username == "new-admin"
        assert event.status == RotationStatus.SUCCESS

    async def test_credentials_stored_after_rotation(self):
        mock_creds = MockCredentialProvider()
        rotator = CredentialRotator(credential_provider=mock_creds)
        event = await rotator.rotate("device-x", "old-admin")
        # New credentials should be stored
        cred = await mock_creds.get_credentials("device-x")
        assert cred.username == event.new_username

    async def test_rotation_history(self):
        rotator = CredentialRotator()
        await rotator.rotate("dev-1", "admin1")
        await rotator.rotate("dev-2", "admin2")
        assert len(rotator.rotation_history) == 2


class TestRotationEvent:
    def test_complete(self):
        event = RotationEvent(
            device_id="dev-1",
            old_username="old",
            new_username="new",
            status=RotationStatus.SUCCESS,
        )
        event.complete(RotationStatus.FAILED, "test error")
        assert event.status == RotationStatus.FAILED
        assert event.error_message == "test error"
        assert event.completed_at is not None
        assert event.duration_ms >= 0
