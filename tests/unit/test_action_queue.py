"""Action queue lifecycle + checkpoint persistence tests (Doc 06 §11A.2)."""

import pytest
from click.testing import CliRunner

from harkeniq.actions.queue import ActionQueue
from harkeniq.cli import main as cli_main
from harkeniq.errors import ActionError
from harkeniq.models import ActionStatus, ActionType, VerdictSeverity
from harkeniq.state.checkpoint import CheckpointManager


def enqueue(queue, action_type=ActionType.IDENTIFY_LED, sensor_id="disk:sda",
            skill="disk-health", severity=VerdictSeverity.CRITICAL, params=None):
    return queue.enqueue(action_type, sensor_id, skill, severity,
                         params if params is not None else {"target": "sda"})


class TestEnqueue:
    def test_enqueue_creates_pending_action(self):
        queue = ActionQueue()
        action = enqueue(queue)
        assert action is not None
        assert action.id.startswith("act-")
        assert action.status == ActionStatus.PENDING
        assert action.type == ActionType.IDENTIFY_LED
        assert action.sensor_id == "disk:sda"
        assert action.skill_name == "disk-health"
        assert action.params == {"target": "sda"}
        assert action.proposed_at

    def test_pending_is_fifo(self):
        queue = ActionQueue()
        a1 = enqueue(queue, sensor_id="disk:sda")
        a2 = enqueue(queue, sensor_id="disk:sdb")
        assert [a.id for a in queue.pending()] == [a1.id, a2.id]

    def test_duplicate_suppressed_while_pending(self):
        queue = ActionQueue()
        assert enqueue(queue) is not None
        assert enqueue(queue) is None
        assert len(queue.pending()) == 1

    def test_duplicate_suppressed_while_approved(self):
        queue = ActionQueue()
        action = enqueue(queue)
        queue.approve(action.id)
        assert enqueue(queue) is None

    def test_different_type_or_sensor_not_duplicate(self):
        queue = ActionQueue()
        enqueue(queue)
        assert enqueue(queue, action_type=ActionType.COLLECT_DIAGNOSTICS) is not None
        assert enqueue(queue, sensor_id="disk:sdb") is not None

    def test_reproposal_blocked_after_denial(self):
        # Denied is final (D16): the same sensor+type must not be re-asked.
        queue = ActionQueue()
        action = enqueue(queue)
        queue.deny(action.id)
        assert enqueue(queue) is None

    def test_reproposal_allowed_after_completion(self):
        queue = ActionQueue()
        action = enqueue(queue)
        queue.approve(action.id)
        action.status = ActionStatus.COMPLETED
        assert enqueue(queue) is not None


class TestApproveDeny:
    def test_approve_sets_status_and_timestamp(self):
        queue = ActionQueue()
        action = enqueue(queue)
        approved = queue.approve(action.id)
        assert approved.status == ActionStatus.APPROVED
        assert approved.approved_at
        assert queue.approved() == [approved]
        assert queue.pending() == []

    def test_deny_is_final(self):
        queue = ActionQueue()
        action = enqueue(queue)
        denied = queue.deny(action.id)
        assert denied.status == ActionStatus.DENIED
        assert denied.completed_at
        with pytest.raises(ActionError, match="DENIED"):
            queue.approve(action.id)

    def test_approve_unknown_id_raises(self):
        with pytest.raises(ActionError, match="Unknown action"):
            ActionQueue().approve("act-nope")

    def test_deny_approved_action_raises(self):
        queue = ActionQueue()
        action = enqueue(queue)
        queue.approve(action.id)
        with pytest.raises(ActionError, match="APPROVED"):
            queue.deny(action.id)


class TestPersistence:
    async def test_round_trip(self, tmp_path):
        queue = ActionQueue()
        a1 = enqueue(queue)
        a2 = enqueue(queue, action_type=ActionType.COLLECT_DIAGNOSTICS,
                     sensor_id="fan:Fan1", skill="fan-health",
                     severity=VerdictSeverity.WARNING, params={"reason": "fan failure"})
        queue.approve(a2.id)

        cp = CheckpointManager(tmp_path / "checkpoint.db")
        await cp.save_actions(queue.all())
        loaded = await cp.load_actions()
        await cp.close()

        assert [a.id for a in loaded] == [a1.id, a2.id]
        first, second = loaded
        assert first.status == ActionStatus.PENDING
        assert first.params == {"target": "sda"}
        assert first.verdict_severity == VerdictSeverity.CRITICAL
        assert second.status == ActionStatus.APPROVED
        assert second.approved_at == a2.approved_at
        assert second.type == ActionType.COLLECT_DIAGNOSTICS

    async def test_restore_into_new_queue(self, tmp_path):
        queue = ActionQueue()
        action = enqueue(queue)
        cp = CheckpointManager(tmp_path / "checkpoint.db")
        await cp.save_actions(queue.all())

        queue2 = ActionQueue()
        queue2.restore(await cp.load_actions())
        await cp.close()
        assert queue2.get(action.id).status == ActionStatus.PENDING
        # dedup still applies after restore
        assert enqueue(queue2) is None

    async def test_executing_action_recovered_as_unknown(self, tmp_path):
        queue = ActionQueue()
        action = enqueue(queue)
        queue.approve(action.id)
        action.status = ActionStatus.EXECUTING

        cp = CheckpointManager(tmp_path / "checkpoint.db")
        await cp.save_actions([action])
        loaded = await cp.load_actions()
        assert loaded[0].status == ActionStatus.FAILED
        assert "unknown" in loaded[0].outcome.error_message
        assert loaded[0].outcome.success is False
        # recovery is persisted back to the DB
        reloaded = await cp.load_actions()
        await cp.close()
        assert reloaded[0].status == ActionStatus.FAILED

    async def test_outcome_round_trip(self, tmp_path):
        from harkeniq.models import ActionOutcome

        queue = ActionQueue()
        action = enqueue(queue)
        action.status = ActionStatus.COMPLETED
        action.outcome = ActionOutcome(
            action_id=action.id, type=action.type, target="sda",
            success=True, duration_ms=12.5, timestamp="2026-08-19T00:00:00Z",
        )
        cp = CheckpointManager(tmp_path / "checkpoint.db")
        await cp.save_actions([action])
        loaded = await cp.load_actions()
        await cp.close()
        outcome = loaded[0].outcome
        assert outcome.success is True
        assert outcome.type == ActionType.IDENTIFY_LED
        assert outcome.duration_ms == 12.5


class TestCli:
    def _seed(self, tmp_path):
        import asyncio

        queue = ActionQueue()
        action = enqueue(queue)
        db = str(tmp_path / "checkpoint.db")

        async def _save():
            cp = CheckpointManager(db)
            await cp.save_actions(queue.all())
            await cp.close()

        asyncio.run(_save())
        return db, action

    def test_action_list(self, tmp_path):
        db, action = self._seed(tmp_path)
        result = CliRunner().invoke(cli_main, ["action", "list", "--checkpoint", db])
        assert result.exit_code == 0, result.output
        assert action.id in result.output
        assert "IDENTIFY_LED" in result.output

    def test_action_approve(self, tmp_path):
        import asyncio

        db, action = self._seed(tmp_path)
        result = CliRunner().invoke(
            cli_main, ["action", "approve", action.id, "--checkpoint", db, "--by", "vinod"]
        )
        assert result.exit_code == 0, result.output
        assert "Approved" in result.output

        async def _load():
            cp = CheckpointManager(db)
            actions = await cp.load_actions()
            audit = cp._conn.execute("SELECT * FROM audit_log").fetchall()
            await cp.close()
            return actions, audit

        actions, audit = asyncio.run(_load())
        assert actions[0].status == ActionStatus.APPROVED
        assert audit[-1]["outcome"] == "approved"
        assert audit[-1]["authorization"] == "vinod"

    def test_action_deny(self, tmp_path):
        db, action = self._seed(tmp_path)
        result = CliRunner().invoke(cli_main, ["action", "deny", action.id, "--checkpoint", db])
        assert result.exit_code == 0, result.output
        assert "Denied" in result.output
        # denied action no longer listed as pending
        result = CliRunner().invoke(cli_main, ["action", "list", "--checkpoint", db])
        assert "No pending actions" in result.output
        result = CliRunner().invoke(cli_main, ["action", "list", "--checkpoint", db, "--all"])
        assert "DENIED" in result.output

    def test_approve_unknown_id_fails(self, tmp_path):
        db, _ = self._seed(tmp_path)
        result = CliRunner().invoke(cli_main, ["action", "approve", "act-nope", "--checkpoint", db])
        assert result.exit_code != 0
        assert "Unknown action" in result.output
