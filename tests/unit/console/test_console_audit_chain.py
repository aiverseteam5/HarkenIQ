"""Console audit hash chain tests (R4-2 P12)."""

from __future__ import annotations

from sqlalchemy import update

from harkeniq.audit.chain import GENESIS_HASH
from harkeniq_console.db.models import ConsoleAuditLog
from harkeniq_console.db.repos import AuditRepo


class TestConsoleAuditChain:
    async def test_appends_chain(self, session):
        repo = AuditRepo(session)
        for i in range(4):
            await repo.append(
                actor_id=None, actor_email="admin@example.com",
                action=f"tenant.update.{i}", subject_type="tenant",
                subject_id=f"t{i}", tenant_id="ten-1", detail={"n": i},
            )
        await session.commit()
        result = await repo.verify_chain()
        assert result.valid is True
        assert result.length == 4

    async def test_genesis_and_links(self, session):
        repo = AuditRepo(session)
        r1 = await repo.append(None, "a@x.com", "one")
        r2 = await repo.append(None, "a@x.com", "two")
        await session.commit()
        assert r1.seq == 1 and r1.prev_hash == GENESIS_HASH
        assert r2.seq == 2 and r2.prev_hash == r1.entry_hash

    async def test_tamper_detected(self, session):
        repo = AuditRepo(session)
        for i in range(3):
            await repo.append(None, "a@x.com", f"act.{i}")
        await session.commit()
        await session.execute(
            update(ConsoleAuditLog).where(ConsoleAuditLog.seq == 1)
            .values(action="forged")
        )
        await session.commit()
        result = await repo.verify_chain()
        assert result.valid is False
        assert result.first_bad_seq == 1
