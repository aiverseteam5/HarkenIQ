"""SM audit hash chain tests (R4-2 P12)."""

from __future__ import annotations

from sqlalchemy import update

from harkeniq.audit.chain import GENESIS_HASH
from harkeniq_sm.db.models import AuditLogRow
from harkeniq_sm.db.repos import AuditRepo


class TestSMAuditChain:
    async def test_appends_chain(self, session):
        repo = AuditRepo(session)
        for i in range(4):
            await repo.append("operator", f"action.{i}", subject=f"s{i}",
                              detail={"n": i})
        await session.commit()
        rows = await repo.list_all()
        assert [r.seq for r in rows] == [1, 2, 3, 4]
        assert rows[0].prev_hash == GENESIS_HASH
        assert rows[2].prev_hash == rows[1].entry_hash
        result = await repo.verify_chain()
        assert result.valid is True
        assert result.length == 4

    async def test_tamper_detected(self, session):
        repo = AuditRepo(session)
        for i in range(3):
            await repo.append("operator", f"action.{i}")
        await session.commit()
        await session.execute(
            update(AuditLogRow).where(AuditLogRow.seq == 2)
            .values(actor="attacker")
        )
        await session.commit()
        result = await repo.verify_chain()
        assert result.valid is False
        assert result.first_bad_seq == 2

    async def test_existing_writers_still_work(self, session):
        # The R3b-era call shape (no chain args) is unchanged.
        row = await repo_append_legacy_shape(session)
        assert row.seq == 1
        assert row.entry_hash


async def repo_append_legacy_shape(session):
    return await AuditRepo(session).append(
        "operator", "domain.confirm", "rack-1"
    )
