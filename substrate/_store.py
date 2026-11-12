"""The append-only event store (SQLite, WAL)."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from dataclasses import replace

from substrate import _ulid, upcasters
from substrate._schemas import (SchemaViolation, UnknownVersion, latest_version, validate_envelope,
                                validate_payload)
from substrate.types import Event

ENVELOPE_V = 1
_SUBJECT = re.compile(r"^[a-z]+:.+$")

_DDL = """
CREATE TABLE IF NOT EXISTS events (
  seq      INTEGER PRIMARY KEY AUTOINCREMENT,
  v        INTEGER NOT NULL,
  id       TEXT    NOT NULL UNIQUE,
  ts       TEXT    NOT NULL,
  type     TEXT    NOT NULL,
  type_v   INTEGER NOT NULL,
  subject  TEXT    NOT NULL,
  actor    TEXT    NOT NULL,
  cause    TEXT,
  payload  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS events_subject ON events(subject, seq);
CREATE INDEX IF NOT EXISTS events_type    ON events(type, seq);
CREATE INDEX IF NOT EXISTS events_cause   ON events(cause);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""

class Busy(RuntimeError):
    """The write lock was not available within the timeout. Safe to retry."""


_COLUMNS = "seq, v, id, ts, type, type_v, subject, actor, cause, payload"


def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, path: Path, *, timeout: float = 10.0):
        self.path = Path(path)
        self.timeout = timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_DDL)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        db.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    # ── write ──────────────────────────────────────────────────────────
    def append(self, type: str, payload: dict, *, subject: str, actor: dict,
               cause: str | None = None, type_v: int | None = None, id: str | None = None) -> str:
        """Append one event. A caller-supplied `id` makes the write idempotent: appending the same id
        again returns it without writing, so a client can retry after losing the reply."""
        if type_v is None:
            type_v = latest_version(type)
        if not isinstance(subject, str) or not _SUBJECT.match(subject):
            raise SchemaViolation(f"subject must look like 'prefix:rest', got {subject!r}")
        validate_payload(type, type_v, payload)
        env = {"v": ENVELOPE_V, "id": id or _ulid.new_ulid(), "ts": _ulid.utc_now_iso(), "type": type,
               "type_v": type_v, "subject": subject, "actor": actor, "cause": cause, "payload": payload}
        validate_envelope(env)
        try:
            actor_json, payload_json = _dumps(actor), _dumps(payload)
        except ValueError as e:  # NaN / Infinity: not valid JSON, would poison every reader
            raise SchemaViolation(f"{type}: {e}") from e
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO events (v, id, ts, type, type_v, subject, actor, cause, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ENVELOPE_V, env["id"], env["ts"], type, type_v, subject, actor_json, cause, payload_json))
            db.execute("COMMIT")
        except sqlite3.IntegrityError:
            if db.in_transaction:
                db.execute("ROLLBACK")
            if id is not None and db.execute("SELECT 1 FROM events WHERE id = ?", (id,)).fetchone():
                return id  # already written by an earlier attempt
            raise
        except sqlite3.OperationalError as e:
            if db.in_transaction:
                db.execute("ROLLBACK")
            if "locked" in str(e) or "busy" in str(e):
                raise Busy(f"write lock not available within {self.timeout}s") from e
            raise
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
        return env["id"]

    # ── read ───────────────────────────────────────────────────────────
    def _query(self, where: str, args: list, limit: int | None = None) -> list[tuple]:
        sql = f"SELECT {_COLUMNS} FROM events {where} ORDER BY seq"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        db = self._connect()
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    @staticmethod
    def _types_clause(types: list[str] | None, clauses: list[str], args: list) -> None:
        if types:
            clauses.append(f"type IN ({','.join('?' * len(types))})")
            args.extend(types)

    def _to_event(self, row: tuple) -> Event:
        seq, v, id_, ts, type_, type_v, subject, actor, cause, payload = row
        if v != ENVELOPE_V:
            raise UnknownVersion(f"event {id_} has envelope v{v}; this reader knows v{ENVELOPE_V}")
        return Event(seq, v, id_, ts, type_, type_v, subject, json.loads(actor), cause, json.loads(payload))

    def _read(self, where: str, args: list, limit: int | None = None) -> list[Event]:
        """Events with payloads upcast to the latest version of their type."""
        out = []
        for row in self._query(where, args, limit):
            ev = self._to_event(row)
            type_v, payload = upcasters.upcast(ev.type, ev.type_v, ev.payload)
            out.append(replace(ev, type_v=type_v, payload=payload))
        return out

    def history(self, subject: str, *, types: list[str] | None = None) -> list[Event]:
        clauses, args = ["subject = ?"], [subject]
        self._types_clause(types, clauses, args)
        return self._read("WHERE " + " AND ".join(clauses), args)

    def since(self, cursor: int | None, *, types: list[str] | None = None,
              limit: int = 1000) -> tuple[list[Event], int]:
        start = cursor or 0
        clauses, args = ["seq > ?"], [start]
        self._types_clause(types, clauses, args)
        events = self._read("WHERE " + " AND ".join(clauses), args, limit)
        return events, (events[-1].seq if events else start)

    def raw_rows(self, types: list[str] | None = None) -> list[Event]:
        clauses, args = [], []
        self._types_clause(types, clauses, args)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return [self._to_event(r) for r in self._query(where, args)]
