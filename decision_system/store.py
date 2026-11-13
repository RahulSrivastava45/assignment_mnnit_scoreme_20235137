"""SQLite persistence: requests, state history and audit events.

Every state change goes through checkpoint(), which writes the new state, the
history row and the audit events in a single transaction. Either all of it is
saved or none of it is, so a failed save never leaves a half-recorded step.
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone

from .workflow import can_transition, RECEIVED


class StoreError(Exception):
    """Storage failed; the operation was rolled back and can be retried."""


class DuplicateKey(Exception):
    pass


class InvalidTransition(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    workflow TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    workflow_snapshot TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    current_stage TEXT,
    context TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    from_stage TEXT,
    to_stage TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    stage TEXT,
    event TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_req ON history(request_id, seq);
CREATE INDEX IF NOT EXISTS idx_audit_req ON audit(request_id, seq);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path=":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)
        self._fail_next = 0  # test hook, see inject_failures()

    def inject_failures(self, count=1):
        """Make the next `count` checkpoints blow up half way (used by tests)."""
        self._fail_next = count

    # -- helpers ----------------------------------------------------------

    def _row_to_request(self, row):
        if row is None:
            return None
        return {
            "id": row["id"],
            "idempotency_key": row["idempotency_key"],
            "payload_hash": row["payload_hash"],
            "workflow": row["workflow"],
            "workflow_version": row["workflow_version"],
            "workflow_snapshot": json.loads(row["workflow_snapshot"]),
            "payload": json.loads(row["payload"]),
            "status": row["status"],
            "current_stage": row["current_stage"],
            "context": json.loads(row["context"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _next_seq(self, table, request_id):
        row = self._conn.execute(
            f"SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM {table} WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return row["n"]

    def _insert_audit(self, request_id, stage, events, ts):
        seq = self._next_seq("audit", request_id)
        for event in events:
            event = dict(event)
            name = event.pop("event")
            self._conn.execute(
                "INSERT INTO audit (request_id, seq, at, stage, event, detail) VALUES (?,?,?,?,?,?)",
                (request_id, seq, ts, stage, name, json.dumps(event)),
            )
            seq += 1

    # -- writes -----------------------------------------------------------

    def create_request(self, request_id, key, payload_hash, definition, payload):
        ts = now()
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                self._conn.execute(
                    "INSERT INTO requests (id, idempotency_key, payload_hash, workflow, workflow_version,"
                    " workflow_snapshot, payload, status, current_stage, context, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (request_id, key, payload_hash, definition.name, definition.version,
                     json.dumps(definition.raw), json.dumps(payload), RECEIVED,
                     definition.start, "{}", ts, ts),
                )
                self._conn.execute(
                    "INSERT INTO history (request_id, seq, at, from_status, to_status, from_stage, to_stage, note)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (request_id, 1, ts, None, RECEIVED, None, definition.start, "request accepted"),
                )
                self._insert_audit(request_id, None, [{
                    "event": "request_received",
                    "workflow": definition.name,
                    "workflow_version": definition.version,
                    "idempotency_key": key,
                    "input": payload,
                }], ts)
                self._conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._conn.execute("ROLLBACK")
                raise DuplicateKey(key) from exc
            except sqlite3.Error as exc:
                self._rollback()
                raise StoreError(str(exc)) from exc
        return self.get(request_id)

    def checkpoint(self, request_id, *, status, stage, context, events, note=None):
        """Atomically move a request to a new state and record why."""
        ts = now()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
                if row is None:
                    raise KeyError(request_id)
                if not can_transition(row["status"], status):
                    raise InvalidTransition(f"{row['status']} -> {status} is not allowed")

                seq = self._next_seq("history", request_id)
                self._conn.execute(
                    "INSERT INTO history (request_id, seq, at, from_status, to_status, from_stage, to_stage, note)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (request_id, seq, ts, row["status"], status, row["current_stage"], stage, note),
                )
                self._insert_audit(request_id, stage, events, ts)

                if self._fail_next > 0:
                    self._fail_next -= 1
                    raise sqlite3.OperationalError("simulated write failure")

                self._conn.execute(
                    "UPDATE requests SET status = ?, current_stage = ?, context = ?, updated_at = ? WHERE id = ?",
                    (status, stage, json.dumps(context), ts, request_id),
                )
                self._conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback()
                raise StoreError(str(exc)) from exc
            except Exception:
                self._rollback()
                raise
        return self.get(request_id)

    def _rollback(self):
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # nothing to roll back

    # -- reads ------------------------------------------------------------

    def get(self, request_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        return self._row_to_request(row)

    def get_by_key(self, key):
        with self._lock:
            row = self._conn.execute("SELECT * FROM requests WHERE idempotency_key = ?", (key,)).fetchone()
        return self._row_to_request(row)

    def history(self, request_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, at, from_status, to_status, from_stage, to_stage, note"
                " FROM history WHERE request_id = ? ORDER BY seq", (request_id,)).fetchall()
        return [dict(r) for r in rows]

    def audit(self, request_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, at, stage, event, detail FROM audit WHERE request_id = ? ORDER BY seq",
                (request_id,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["detail"] = json.loads(item["detail"])
            out.append(item)
        return out
