"""Compliance audit hash chain tests (R4-2 P12, OQ-20).

Covers the shared chain primitives (harkeniq.audit.chain) and the agent
checkpoint integration, including migration of pre-R4-2 checkpoint
files and tamper detection. Service-store integrations are tested in
tests/unit/sm, tests/unit/cc, tests/unit/console.
"""

from __future__ import annotations

import sqlite3

import pytest

from harkeniq.audit.chain import (
    GENESIS_HASH,
    canonical_json,
    compute_entry_hash,
    next_link,
    verify_chain,
)
from harkeniq.state.checkpoint import CheckpointManager


class TestPrimitives:
    def test_canonical_json_deterministic(self):
        a = canonical_json({"b": 1, "a": {"y": 2, "x": 3}})
        b = canonical_json({"a": {"x": 3, "y": 2}, "b": 1})
        assert a == b == b'{"a":{"x":3,"y":2},"b":1}'

    def test_entry_hash_is_sha256_hex(self):
        h = compute_entry_hash(1, GENESIS_HASH, {"k": "v"})
        assert len(h) == 64
        assert int(h, 16) >= 0

    def test_entry_hash_binds_position(self):
        payload = {"k": "v"}
        assert compute_entry_hash(1, GENESIS_HASH, payload) != \
            compute_entry_hash(2, GENESIS_HASH, payload)

    def test_next_link_genesis(self):
        seq, prev, h = next_link(0, None, {"k": "v"})
        assert seq == 1
        assert prev == GENESIS_HASH
        assert h == compute_entry_hash(1, GENESIS_HASH, {"k": "v"})

    def _build(self, payloads):
        entries = []
        tail_seq, tail_hash = 0, None
        for p in payloads:
            seq, prev, h = next_link(tail_seq, tail_hash, p)
            entries.append((seq, prev, h, p))
            tail_seq, tail_hash = seq, h
        return entries

    def test_verify_empty_chain_valid(self):
        result = verify_chain([])
        assert result.valid is True
        assert result.length == 0

    def test_verify_valid_chain(self):
        entries = self._build([{"n": i} for i in range(5)])
        result = verify_chain(entries)
        assert result.valid is True
        assert result.length == 5

    def test_tampered_payload_detected(self):
        entries = self._build([{"n": i} for i in range(5)])
        seq, prev, h, _ = entries[2]
        entries[2] = (seq, prev, h, {"n": 999})
        result = verify_chain(entries)
        assert result.valid is False
        assert result.first_bad_seq == 3
        assert "entry_hash mismatch" in result.error

    def test_deleted_entry_detected(self):
        entries = self._build([{"n": i} for i in range(5)])
        del entries[1]
        result = verify_chain(entries)
        assert result.valid is False
        assert result.first_bad_seq == 3
        assert "sequence gap" in result.error

    def test_reordered_entries_detected(self):
        entries = self._build([{"n": i} for i in range(3)])
        entries[1], entries[2] = entries[2], entries[1]
        result = verify_chain(entries)
        assert result.valid is False

    def test_rewritten_hash_breaks_next_link(self):
        # An attacker who recomputes entry 2's hash after tampering still
        # breaks the link from entry 3 (whose prev_hash no longer matches).
        entries = self._build([{"n": i} for i in range(4)])
        seq, prev, _, _ = entries[1]
        forged = compute_entry_hash(seq, prev, {"n": 999})
        entries[1] = (seq, prev, forged, {"n": 999})
        result = verify_chain(entries)
        assert result.valid is False
        assert result.first_bad_seq == 3
        assert "broken link" in result.error


class TestAgentCheckpointChain:
    async def _cp(self, tmp_path):
        return CheckpointManager(tmp_path / "checkpoint.db")

    async def test_entries_chain_and_verify(self, tmp_path):
        cp = await self._cp(tmp_path)
        for i in range(4):
            await cp.save_audit_entry(
                action="IDENTIFY_LED", target=f"disk:{i}", outcome="success",
                evidence_json='{"k":"v"}',
            )
        result = await cp.verify_audit_chain()
        assert result.valid is True
        assert result.length == 4
        entries = await cp.list_audit_entries()
        assert [e["seq"] for e in entries] == [1, 2, 3, 4]
        assert entries[0]["prev_hash"] == GENESIS_HASH
        assert entries[1]["prev_hash"] == entries[0]["entry_hash"]
        await cp.close()

    async def test_tamper_detected(self, tmp_path):
        cp = await self._cp(tmp_path)
        for i in range(3):
            await cp.save_audit_entry(
                action="SEL_CLEAR", target=f"t{i}", outcome="success",
            )
        cp.conn.execute(
            "UPDATE audit_log SET outcome = 'refused' WHERE seq = 2"
        )
        cp.conn.commit()
        result = await cp.verify_audit_chain()
        assert result.valid is False
        assert result.first_bad_seq == 2
        await cp.close()

    async def test_deleted_row_detected(self, tmp_path):
        cp = await self._cp(tmp_path)
        for i in range(3):
            await cp.save_audit_entry(
                action="FAN_RESET", target=f"t{i}", outcome="success",
            )
        cp.conn.execute("DELETE FROM audit_log WHERE seq = 2")
        cp.conn.commit()
        result = await cp.verify_audit_chain()
        assert result.valid is False
        await cp.close()

    async def test_pre_r42_checkpoint_migrates(self, tmp_path):
        """A checkpoint created before R4-2 gains chain columns on open;
        its pre-chain rows stay outside the chain."""
        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE audit_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, "
            "target TEXT NOT NULL, authorization TEXT, outcome TEXT NOT NULL, "
            "evidence_json TEXT, logged_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO audit_log (action, target, outcome, logged_at) "
            "VALUES ('OLD', 'legacy', 'success', '2026-08-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        cp = CheckpointManager(db)
        await cp.save_audit_entry(action="NEW", target="t", outcome="success")
        entries = await cp.list_audit_entries()
        assert len(entries) == 2
        chained = [e for e in entries if e["seq"] is not None]
        assert len(chained) == 1 and chained[0]["seq"] == 1
        result = await cp.verify_audit_chain()
        assert result.valid is True
        assert result.length == 1
        await cp.close()
