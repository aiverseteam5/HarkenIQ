"""SQLite checkpoint manager (Doc 06 §7, Doc 13 §7, Doc 10 §2.14).

Persists agent state (sensor readings, baselines, verdicts, peers, meta,
log cursors, audit trail) to a single SQLite database in WAL mode so that
baselines and the action queue survive restarts.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from harkeniq.errors import CheckpointReadError, CheckpointWriteError
from harkeniq.models import (
    Action,
    ActionOutcome,
    ActionStatus,
    ActionType,
    Baseline,
    Evidence,
    Peer,
    PeerStatus,
    RegressionState,
    Verdict,
    VerdictSeverity,
)

logger = logging.getLogger("harkeniq.state.checkpoint")

VERDICT_HISTORY_PER_SENSOR = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sensor_readings (
    sensor_id TEXT PRIMARY KEY,
    sensor_type TEXT NOT NULL,
    reading_json TEXT NOT NULL,
    health TEXT NOT NULL,
    collected_at TEXT NOT NULL
);

-- Doc 13 §7.1 (extended form of Doc 06 §7.2)
CREATE TABLE IF NOT EXISTS baselines (
    sensor_id       TEXT PRIMARY KEY,
    mean            REAL NOT NULL,
    variance        REAL NOT NULL,
    stddev          REAL NOT NULL,
    m2              REAL NOT NULL,
    min_val         REAL NOT NULL,
    max_val         REAL NOT NULL,
    sample_count    INTEGER NOT NULL,
    first_sample_at TEXT NOT NULL,
    last_sample_at  TEXT NOT NULL,
    degraded_baseline INTEGER NOT NULL DEFAULT 0,
    samples_json    TEXT NOT NULL,
    reg_sum_x       REAL NOT NULL DEFAULT 0.0,
    reg_sum_y       REAL NOT NULL DEFAULT 0.0,
    reg_sum_xy      REAL NOT NULL DEFAULT 0.0,
    reg_sum_x2      REAL NOT NULL DEFAULT 0.0,
    reg_sum_y2      REAL NOT NULL DEFAULT 0.0,
    reg_n           INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    verdict TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    produced_at TEXT NOT NULL,
    reported_to_sm BOOLEAN DEFAULT 0
);

CREATE TABLE IF NOT EXISTS peers (
    peer_id TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    last_heartbeat_at TEXT,
    state TEXT NOT NULL DEFAULT 'unknown',
    last_known_health_json TEXT
);

CREATE TABLE IF NOT EXISTS log_cursors (
    log_source TEXT PRIMARY KEY,
    last_entry_id TEXT NOT NULL,
    last_poll_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    sensor_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    verdict TEXT NOT NULL,
    action_type TEXT NOT NULL,
    params_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    proposed_at TEXT NOT NULL,
    approved_at TEXT,
    completed_at TEXT,
    outcome_json TEXT,
    approved_by TEXT
);

CREATE TABLE IF NOT EXISTS playbook_executions (
    execution_id TEXT PRIMARY KEY,
    playbook_id TEXT NOT NULL,
    status TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    authorization TEXT,
    outcome TEXT NOT NULL,
    evidence_json TEXT,
    logged_at TEXT NOT NULL,
    -- R4-2 P12: SHA-256 hash chain (seq 1..N, prev links, see harkeniq.audit.chain)
    seq INTEGER,
    prev_hash TEXT,
    entry_hash TEXT
);

-- R3a: per-agent Ed25519 identity (spec A2.4)
CREATE TABLE IF NOT EXISTS agent_identity (
    agent_id TEXT PRIMARY KEY,
    public_key_pem BLOB NOT NULL,
    private_key_enc BLOB NOT NULL,
    sm_public_key_pem BLOB,
    sm_certificate BLOB,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- R3a: cached authorization lease from SM (spec A2.2)
CREATE TABLE IF NOT EXISTS authorization_lease (
    id INTEGER PRIMARY KEY,
    lease_data BLOB NOT NULL,
    signature BLOB NOT NULL,
    lease_expiry TEXT NOT NULL,
    grace_expiry TEXT NOT NULL,
    received_at TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CheckpointManager:
    """SQLite checkpoint manager with WAL mode (Doc 10 §2.14)."""

    def __init__(self, db_path: Path | str, max_baseline_age_days: int = 30) -> None:
        self.db_path = Path(db_path)
        self.max_baseline_age_days = max_baseline_age_days
        # Dirty tracking: sensor_id -> (sample_count, last_sample_at)
        self._written_baselines: dict[str, tuple[int, Optional[str]]] = {}
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._migrate_audit_chain_columns()
            self._conn.commit()
        except (OSError, sqlite3.Error) as e:
            raise CheckpointWriteError(f"Cannot open checkpoint db {db_path}: {e}")

    def _migrate_audit_chain_columns(self) -> None:
        """Add R4-2 chain columns to audit_log on pre-R4-2 checkpoint files.

        CREATE TABLE IF NOT EXISTS never alters an existing table, so
        checkpoints created before R4-2 lack seq/prev_hash/entry_hash.
        Pre-chain rows keep NULL chain columns; the chain starts at the
        first post-migration entry (verification skips NULL-seq rows).
        """
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(audit_log)")
        }
        for name, decl in (
            ("seq", "INTEGER"), ("prev_hash", "TEXT"), ("entry_hash", "TEXT"),
        ):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE audit_log ADD COLUMN {name} {decl}")

    @property
    def conn(self) -> sqlite3.Connection:
        """Expose the SQLite connection for identity/lease persistence (R3a)."""
        return self._conn

    # -- checkpoint sweep ---------------------------------------------------

    async def save_checkpoint(
        self,
        sensor_readings: dict[str, dict],
        baselines: dict[str, Baseline],
        verdicts: list[Verdict],
        peers: list[Peer],
        agent_meta: dict[str, str],
        log_cursors: dict[str, str],
    ) -> None:
        """Persist all agent state in a single transaction (Doc 06 §7.3)."""
        now = _utc_now()
        try:
            with self._conn:
                for key, value in agent_meta.items():
                    self._conn.execute(
                        "INSERT INTO agent_meta (key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                        "updated_at=excluded.updated_at",
                        (key, str(value), now),
                    )

                for sensor_id, reading in sensor_readings.items():
                    self._conn.execute(
                        "INSERT OR REPLACE INTO sensor_readings "
                        "(sensor_id, sensor_type, reading_json, health, collected_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            sensor_id,
                            reading.get("sensor_type", ""),
                            json.dumps(reading.get("reading", {})),
                            reading.get("health", "Unknown"),
                            reading.get("collected_at", now),
                        ),
                    )

                for sensor_id, baseline in baselines.items():
                    if not self._is_dirty(sensor_id, baseline):
                        continue
                    self._write_baseline(sensor_id, baseline, now)

                for verdict in verdicts:
                    self._conn.execute(
                        "INSERT INTO verdicts "
                        "(sensor_id, skill_name, verdict, evidence_json, produced_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            verdict.sensor_id,
                            verdict.skill_name,
                            verdict.severity.value,
                            json.dumps({
                                "message": verdict.message,
                                "evidence": [dataclasses.asdict(e) for e in verdict.evidence],
                            }),
                            verdict.timestamp or now,
                        ),
                    )
                # Prune verdict history to last N per sensor (Doc 06 §7.2)
                self._conn.execute(
                    "DELETE FROM verdicts WHERE id IN ("
                    "  SELECT id FROM ("
                    "    SELECT id, ROW_NUMBER() OVER ("
                    "      PARTITION BY sensor_id ORDER BY id DESC) AS rn"
                    "    FROM verdicts"
                    "  ) WHERE rn > ?)",
                    (VERDICT_HISTORY_PER_SENSOR,),
                )

                for peer in peers:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO peers "
                        "(peer_id, host, port, last_heartbeat_at, state, last_known_health_json) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            peer.peer_id,
                            peer.host,
                            peer.port,
                            peer.last_heartbeat,
                            peer.status.value.lower(),
                            json.dumps(peer.last_known_health)
                            if peer.last_known_health is not None else None,
                        ),
                    )

                for source, last_entry_id in log_cursors.items():
                    self._conn.execute(
                        "INSERT OR REPLACE INTO log_cursors "
                        "(log_source, last_entry_id, last_poll_at) VALUES (?, ?, ?)",
                        (source, last_entry_id, now),
                    )
        except sqlite3.Error as e:
            raise CheckpointWriteError(f"Checkpoint write failed: {e}")

        logger.info(
            "Checkpoint saved: %d readings, %d baselines, %d verdicts, %d peers",
            len(sensor_readings), len(baselines), len(verdicts), len(peers),
        )

    async def load_checkpoint(self) -> dict[str, Any]:
        """Load all persisted state (Doc 06 §7.3, Doc 13 §7.2)."""
        try:
            meta = {
                row["key"]: row["value"]
                for row in self._conn.execute("SELECT key, value FROM agent_meta")
            }

            readings: dict[str, dict] = {}
            for row in self._conn.execute("SELECT * FROM sensor_readings"):
                readings[row["sensor_id"]] = {
                    "sensor_type": row["sensor_type"],
                    "reading": json.loads(row["reading_json"]),
                    "health": row["health"],
                    "collected_at": row["collected_at"],
                }

            baselines = self._load_baselines()

            verdicts: list[Verdict] = []
            for row in self._conn.execute(
                "SELECT * FROM verdicts WHERE id IN "
                "(SELECT MAX(id) FROM verdicts GROUP BY sensor_id)"
            ):
                payload = json.loads(row["evidence_json"])
                verdicts.append(Verdict(
                    sensor_id=row["sensor_id"],
                    skill_name=row["skill_name"],
                    severity=VerdictSeverity(row["verdict"]),
                    message=payload.get("message", ""),
                    evidence=[Evidence(**e) for e in payload.get("evidence", [])],
                    timestamp=row["produced_at"],
                ))

            peers: list[Peer] = []
            for row in self._conn.execute("SELECT * FROM peers"):
                health = row["last_known_health_json"]
                peers.append(Peer(
                    peer_id=row["peer_id"],
                    host=row["host"],
                    port=row["port"],
                    last_heartbeat=row["last_heartbeat_at"],
                    status=PeerStatus(row["state"].upper()),
                    last_known_health=json.loads(health) if health else None,
                ))

            cursors = {
                row["log_source"]: row["last_entry_id"]
                for row in self._conn.execute("SELECT * FROM log_cursors")
            }
        except (sqlite3.Error, ValueError, KeyError, json.JSONDecodeError) as e:
            raise CheckpointReadError(f"Checkpoint load failed: {e}")

        return {
            "agent_meta": meta,
            "sensor_readings": readings,
            "baselines": baselines,
            "verdicts": verdicts,
            "peers": peers,
            "log_cursors": cursors,
        }

    # -- actions ------------------------------------------------------------

    async def save_actions(self, actions: list[Action]) -> None:
        """Persist actions to the checkpoint (Doc 06 §7.2)."""
        try:
            with self._conn:
                for action in actions:
                    outcome_json = None
                    if action.outcome is not None:
                        payload = dataclasses.asdict(action.outcome)
                        payload["type"] = action.outcome.type.value
                        outcome_json = json.dumps(payload)
                    self._conn.execute(
                        "INSERT OR REPLACE INTO actions "
                        "(id, sensor_id, skill_name, verdict, action_type, params_json, "
                        " status, proposed_at, approved_at, completed_at, outcome_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            action.id,
                            action.sensor_id,
                            action.skill_name,
                            action.verdict_severity.value,
                            action.type.value,
                            json.dumps(action.params),
                            action.status.value.lower(),
                            action.proposed_at,
                            action.approved_at,
                            action.completed_at,
                            outcome_json,
                        ),
                    )
        except sqlite3.Error as e:
            raise CheckpointWriteError(f"Action write failed: {e}")

    async def load_actions(self) -> list[Action]:
        """Load persisted actions, oldest first.

        Actions found in EXECUTING state crashed mid-execution: their
        result is unknowable, so they are recorded as FAILED with an
        ``unknown`` outcome (Doc 06 §11A.3).
        """
        actions: list[Action] = []
        recovered: list[Action] = []
        try:
            for row in self._conn.execute(
                "SELECT * FROM actions ORDER BY proposed_at, rowid"
            ):
                outcome = None
                if row["outcome_json"]:
                    payload = json.loads(row["outcome_json"])
                    payload["type"] = ActionType(payload["type"])
                    outcome = ActionOutcome(**payload)
                action = Action(
                    id=row["id"],
                    type=ActionType(row["action_type"]),
                    params=json.loads(row["params_json"] or "{}"),
                    status=ActionStatus(row["status"].upper()),
                    sensor_id=row["sensor_id"],
                    skill_name=row["skill_name"],
                    verdict_severity=VerdictSeverity(row["verdict"]),
                    proposed_at=row["proposed_at"],
                    approved_at=row["approved_at"],
                    completed_at=row["completed_at"],
                    outcome=outcome,
                )
                if action.status == ActionStatus.EXECUTING:
                    action.status = ActionStatus.FAILED
                    action.completed_at = _utc_now()
                    action.outcome = ActionOutcome(
                        action_id=action.id,
                        type=action.type,
                        target=action.params.get("target", ""),
                        success=False,
                        error_message="unknown: agent restarted during execution",
                        timestamp=action.completed_at,
                    )
                    recovered.append(action)
                actions.append(action)
        except (sqlite3.Error, ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            raise CheckpointReadError(f"Action load failed: {e}")

        if recovered:
            logger.warning(
                "Recovered %d action(s) interrupted mid-execution (outcome unknown)",
                len(recovered),
            )
            await self.save_actions(recovered)
        return actions

    # -- audit / cursors ----------------------------------------------------

    async def save_playbook_execution(self, execution) -> None:
        """Persist a PlaybookExecution (QA-031: crash-safe resume state)."""
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO playbook_executions "
                    "(execution_id, playbook_id, status, state_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        execution.execution_id,
                        execution.playbook_id,
                        execution.status.value,
                        json.dumps(execution.to_dict()),
                        _utc_now(),
                    ),
                )
        except sqlite3.Error as e:
            raise CheckpointWriteError(f"Playbook execution write failed: {e}")

    async def load_playbook_executions(self) -> list:
        """Load persisted playbook executions (QA-031).

        Executions found RUNNING crashed mid-flight: their in-flight
        step's result is unknowable, so they come back PAUSED with an
        explanatory error (mirrors load_actions' EXECUTING handling) and
        the flip is persisted.
        """
        from harkeniq.actions.playbook import PlaybookExecution
        from harkeniq.models import PlaybookStatus

        executions: list[PlaybookExecution] = []
        try:
            rows = self._conn.execute(
                "SELECT state_json FROM playbook_executions ORDER BY updated_at"
            ).fetchall()
        except sqlite3.Error as e:
            raise CheckpointWriteError(f"Playbook execution read failed: {e}")
        for row in rows:
            try:
                execution = PlaybookExecution.from_dict(
                    json.loads(row["state_json"])
                )
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                logger.warning("Skipping unreadable playbook execution: %s", e)
                continue
            if execution.status == PlaybookStatus.RUNNING:
                execution.status = PlaybookStatus.PAUSED
                execution.error_message = (
                    "interrupted by agent restart; in-flight step outcome "
                    "unknown"
                )
                await self.save_playbook_execution(execution)
            executions.append(execution)
        return executions

    async def save_audit_entry(
        self,
        action: str,
        target: str,
        outcome: str,
        authorization: Optional[str] = None,
        evidence_json: Optional[str] = None,
    ) -> None:
        """Append to the audit trail (append-only, never pruned; Doc 06 §7.2).

        R4-2 P12: every entry is hash-chained (harkeniq.audit.chain). The
        read-tail-then-insert pair runs inside one transaction; the agent
        is single-process/single-loop, so no cross-process appender race.
        """
        from harkeniq.audit.chain import next_link

        logged_at = _utc_now()
        payload = {
            "action": action,
            "target": target,
            "authorization": authorization,
            "outcome": outcome,
            "evidence_json": evidence_json,
            "logged_at": logged_at,
        }
        try:
            with self._conn:
                tail = self._conn.execute(
                    "SELECT seq, entry_hash FROM audit_log "
                    "WHERE seq IS NOT NULL ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                tail_seq = tail["seq"] if tail else 0
                tail_hash = tail["entry_hash"] if tail else None
                seq, prev_hash, entry_hash = next_link(tail_seq, tail_hash, payload)
                self._conn.execute(
                    "INSERT INTO audit_log "
                    "(action, target, authorization, outcome, evidence_json, "
                    " logged_at, seq, prev_hash, entry_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (action, target, authorization, outcome, evidence_json,
                     logged_at, seq, prev_hash, entry_hash),
                )
        except sqlite3.Error as e:
            raise CheckpointWriteError(f"Audit write failed: {e}")

    async def list_audit_entries(self) -> list[dict]:
        """All audit entries as dicts, chained rows in seq order first."""
        rows = self._conn.execute(
            "SELECT * FROM audit_log ORDER BY seq IS NULL, seq, id"
        ).fetchall()
        return [dict(row) for row in rows]

    async def verify_audit_chain(self):
        """Verify the hash chain over all chained audit rows (R4-2 P12).

        Pre-R4-2 rows (NULL seq, written before the chain existed) are
        outside the chain and skipped. Returns a ChainVerification.
        """
        from harkeniq.audit.chain import verify_chain

        rows = self._conn.execute(
            "SELECT * FROM audit_log WHERE seq IS NOT NULL ORDER BY seq"
        ).fetchall()
        entries = (
            (
                row["seq"],
                row["prev_hash"],
                row["entry_hash"],
                {
                    "action": row["action"],
                    "target": row["target"],
                    "authorization": row["authorization"],
                    "outcome": row["outcome"],
                    "evidence_json": row["evidence_json"],
                    "logged_at": row["logged_at"],
                },
            )
            for row in rows
        )
        return verify_chain(entries)

    async def update_log_cursor(self, log_source: str, last_entry_id: str) -> None:
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO log_cursors "
                    "(log_source, last_entry_id, last_poll_at) VALUES (?, ?, ?)",
                    (log_source, last_entry_id, _utc_now()),
                )
        except sqlite3.Error as e:
            raise CheckpointWriteError(f"Log cursor write failed: {e}")

    async def get_log_cursor(self, log_source: str) -> Optional[str]:
        try:
            row = self._conn.execute(
                "SELECT last_entry_id FROM log_cursors WHERE log_source = ?",
                (log_source,),
            ).fetchone()
        except sqlite3.Error as e:
            raise CheckpointReadError(f"Log cursor read failed: {e}")
        return row["last_entry_id"] if row else None

    async def close(self) -> None:
        self._conn.close()

    # -- internals ----------------------------------------------------------

    def _is_dirty(self, sensor_id: str, baseline: Baseline) -> bool:
        marker = (baseline.sample_count, baseline.last_sample_at)
        if self._written_baselines.get(sensor_id) == marker:
            return False
        self._written_baselines[sensor_id] = marker
        return True

    def _write_baseline(self, sensor_id: str, baseline: Baseline, now: str) -> None:
        reg = baseline.regression_state
        self._conn.execute(
            "INSERT OR REPLACE INTO baselines "
            "(sensor_id, mean, variance, stddev, m2, min_val, max_val, sample_count, "
            " first_sample_at, last_sample_at, degraded_baseline, samples_json, "
            " reg_sum_x, reg_sum_y, reg_sum_xy, reg_sum_x2, reg_sum_y2, reg_n, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sensor_id,
                baseline.mean,
                baseline.variance,
                baseline.stddev,
                baseline.m2,
                baseline.min_val,
                baseline.max_val,
                baseline.sample_count,
                baseline.first_sample_at or now,
                baseline.last_sample_at or now,
                int(baseline.degraded_baseline),
                json.dumps([[ts, v] for ts, v in baseline.ring_buffer]),
                reg.sum_x, reg.sum_y, reg.sum_xy, reg.sum_x2, reg.sum_y2, reg.n,
                now,
            ),
        )

    def _load_baselines(self) -> dict[str, Baseline]:
        """Reconstruct Baseline objects; discard stale ones (Doc 13 §7.2)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.max_baseline_age_days)
        baselines: dict[str, Baseline] = {}
        stale: list[str] = []
        for row in self._conn.execute("SELECT * FROM baselines"):
            last = datetime.strptime(
                row["last_sample_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            if last < cutoff:
                stale.append(row["sensor_id"])
                continue
            baselines[row["sensor_id"]] = Baseline(
                sensor_id=row["sensor_id"],
                mean=row["mean"],
                variance=row["variance"],
                stddev=row["stddev"],
                m2=row["m2"],
                min_val=row["min_val"],
                max_val=row["max_val"],
                sample_count=row["sample_count"],
                ring_buffer=[(ts, v) for ts, v in json.loads(row["samples_json"])],
                first_sample_at=row["first_sample_at"],
                last_sample_at=row["last_sample_at"],
                degraded_baseline=bool(row["degraded_baseline"]),
                regression_state=RegressionState(
                    sum_x=row["reg_sum_x"],
                    sum_y=row["reg_sum_y"],
                    sum_xy=row["reg_sum_xy"],
                    sum_x2=row["reg_sum_x2"],
                    sum_y2=row["reg_sum_y2"],
                    n=row["reg_n"],
                ),
            )
        if stale:
            with self._conn:
                self._conn.executemany(
                    "DELETE FROM baselines WHERE sensor_id = ?",
                    [(s,) for s in stale],
                )
            logger.info("Discarded %d stale baselines (> %d days old)",
                        len(stale), self.max_baseline_age_days)
        return baselines
