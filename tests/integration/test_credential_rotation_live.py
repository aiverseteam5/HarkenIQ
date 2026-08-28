"""Integration: blue-green credential rotation against a live mock BMC (QA-034).

The R3b-3 unit tests exercised the orchestration in mock mode (client=None);
these drive the REAL Redfish AccountService calls end to end: create over
POST, verify by opening a fresh session AS the new account, disable the old
account, and rollback (delete) when verification fails.
"""

from __future__ import annotations

import pytest

from harkeniq.mock.simulator import MockSimulator
from harkeniq.redfish.client import RedfishClient
from harkeniq.security.credential_rotation import CredentialRotator, RotationStatus
from harkeniq.security.credentials import MockCredentialProvider


@pytest.fixture
async def sim():
    simulator = MockSimulator(device="dell-r750", port=0)
    await simulator.start()
    yield simulator
    await simulator.stop()


@pytest.fixture
async def client(sim):
    c = RedfishClient(host=sim.url, verify_ssl=False)
    await c.connect("admin", "password")
    yield c
    await c.delete_session()
    await c.close()


def _account(sim, username):
    return next(
        (a for a in sim.accounts.values() if a["UserName"] == username), None
    )


async def test_full_rotation_success(sim, client):
    creds = MockCredentialProvider()
    rotator = CredentialRotator(
        credential_provider=creds, redfish_client=client
    )
    event = await rotator.rotate("dev-1", "admin", new_username="harken-rot1")

    assert event.status == RotationStatus.SUCCESS
    new = _account(sim, "harken-rot1")
    assert new is not None and new["Enabled"]
    old = _account(sim, "admin")
    assert old is not None and not old["Enabled"]

    stored = await creds.get_credentials("dev-1")
    assert stored.username == "harken-rot1"
    assert stored.password == new["Password"]

    # The stored password really authenticates against the BMC.
    check = RedfishClient(host=sim.url, verify_ssl=False)
    try:
        await check.connect(stored.username, stored.password)
    finally:
        await check.delete_session()
        await check.close()


async def test_verify_failure_rolls_back(sim, client):
    # Point verification at a dead endpoint: the new account is created but
    # can never verify, so rotation must roll back and delete it.
    def dead_client():
        return RedfishClient(host="127.0.0.1", port=1, verify_ssl=False,
                             request_timeout=2)

    rotator = CredentialRotator(
        redfish_client=client, verify_client_factory=dead_client
    )
    event = await rotator.rotate("dev-1", "admin", new_username="harken-bad")

    assert event.status == RotationStatus.ROLLED_BACK
    assert _account(sim, "harken-bad") is None  # rollback deleted it
    old = _account(sim, "admin")
    assert old is not None and old["Enabled"]  # old account untouched


async def test_duplicate_username_fails_cleanly(sim, client):
    rotator = CredentialRotator(redfish_client=client)
    event = await rotator.rotate("dev-1", "admin", new_username="admin")

    assert event.status == RotationStatus.FAILED
    old = _account(sim, "admin")
    assert old is not None and old["Enabled"]  # nothing changed


async def test_simulator_password_never_echoed(sim, client):
    await client.post(
        "/redfish/v1/AccountService/Accounts",
        {"UserName": "probe", "Password": "s3cret!", "RoleId": "ReadOnly"},
    )
    listing = await client.get("/redfish/v1/AccountService/Accounts")
    for member in listing["Members"]:
        body = await client.get(member["@odata.id"])
        assert "Password" not in body
