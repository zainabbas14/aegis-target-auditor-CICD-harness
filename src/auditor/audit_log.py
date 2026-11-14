"""Append-only, hash-chained audit log of every decision and its outcome.

Each entry records who (authenticated principal), did what (action + details),
to which run, when, and what resulted (status, verdict, finding codes, payment).
Entries are chained: ``hash = sha256(canonical(entry) + prev_hash)``. Editing or
deleting any row breaks every hash after it, which ``verify()`` reports.

Defence in depth for the Postgres implementation:
  1. the application role can only INSERT and SELECT - no UPDATE, DELETE, TRUNCATE;
  2. a trigger rejects UPDATE/DELETE/TRUNCATE even for privileged roles (accidents);
  3. the hash chain makes deliberate tampering by a database owner *detectable*.
It cannot make tampering impossible for someone who controls the database; for
that, ship entries to write-once storage. This is stated, not hidden.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

GENESIS = "0" * 64


class AuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    at: str
    run_id: str
    actor: str
    action: str
    details: dict[str, Any]
    outcome: dict[str, Any]
    prev_hash: str
    hash: str


class ChainVerification(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    entries: int
    first_bad_seq: int | None = None


def entry_hash(
    *, seq: int, at: str, run_id: str, actor: str, action: str, details: Any, outcome: Any, prev_hash: str
) -> str:
    body = json.dumps(
        {
            "seq": seq,
            "at": at,
            "run_id": run_id,
            "actor": actor,
            "action": action,
            "details": details,
            "outcome": outcome,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def make_entry(
    *, seq: int, prev_hash: str, run_id: str, actor: str, action: str, details: dict[str, Any], outcome: dict[str, Any]
) -> AuditEntry:
    # Round-trip through JSON so what is hashed is exactly what storage will return.
    details = json.loads(json.dumps(details, default=str))
    outcome = json.loads(json.dumps(outcome, default=str))
    at = datetime.now(UTC).isoformat(timespec="microseconds")
    digest = entry_hash(
        seq=seq, at=at, run_id=run_id, actor=actor, action=action, details=details, outcome=outcome, prev_hash=prev_hash
    )
    return AuditEntry(
        seq=seq,
        at=at,
        run_id=run_id,
        actor=actor,
        action=action,
        details=details,
        outcome=outcome,
        prev_hash=prev_hash,
        hash=digest,
    )


def verify_chain(entries: list[AuditEntry]) -> ChainVerification:
    prev = GENESIS
    for i, e in enumerate(sorted(entries, key=lambda x: x.seq), start=1):
        expected = entry_hash(
            seq=e.seq,
            at=e.at,
            run_id=e.run_id,
            actor=e.actor,
            action=e.action,
            details=e.details,
            outcome=e.outcome,
            prev_hash=prev,
        )
        if e.seq != i or e.prev_hash != prev or e.hash != expected:
            return ChainVerification(valid=False, entries=len(entries), first_bad_seq=e.seq)
        prev = e.hash
    return ChainVerification(valid=True, entries=len(entries))


class AuditLog(Protocol):
    def append(
        self, *, run_id: str, actor: str, action: str, details: dict[str, Any], outcome: dict[str, Any]
    ) -> AuditEntry: ...

    def entries(self, *, run_id: str | None = None, limit: int = 500) -> list[AuditEntry]: ...

    def verify(self) -> ChainVerification: ...

    def close(self) -> None: ...


class InMemoryAuditLog:
    """For dev and tests. Lost on restart, like the in-memory checkpointer it pairs with."""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()

    def append(
        self, *, run_id: str, actor: str, action: str, details: dict[str, Any], outcome: dict[str, Any]
    ) -> AuditEntry:
        with self._lock:
            prev = self._entries[-1].hash if self._entries else GENESIS
            entry = make_entry(
                seq=len(self._entries) + 1,
                prev_hash=prev,
                run_id=run_id,
                actor=actor,
                action=action,
                details=details,
                outcome=outcome,
            )
            self._entries.append(entry)
        logger.info("Audit entry", extra={"seq": entry.seq, "action": action, "actor": actor, "run_id": run_id})
        return entry

    def entries(self, *, run_id: str | None = None, limit: int = 500) -> list[AuditEntry]:
        rows = [e for e in self._entries if run_id is None or e.run_id == run_id]
        return rows[-limit:]

    def verify(self) -> ChainVerification:
        return verify_chain(list(self._entries))

    def close(self) -> None:
        """Nothing to release."""


class PostgresAuditLog:
    """Writes to ``audit.audit_log`` (db/postgres/005_audit_log.sql) as an INSERT/SELECT-only role."""

    _LOCK_KEY = 0x4155_4449_54  # constant advisory-lock key serialising appends ("AUDIT")

    def __init__(self, conninfo: str) -> None:
        from psycopg_pool import ConnectionPool  # noqa: PLC0415

        self._pool: Any = ConnectionPool(conninfo, min_size=1, max_size=2, open=True, name="audit")

    def append(
        self, *, run_id: str, actor: str, action: str, details: dict[str, Any], outcome: dict[str, Any]
    ) -> AuditEntry:
        from psycopg.types.json import Jsonb  # noqa: PLC0415

        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (self._LOCK_KEY,))
            last = conn.execute("SELECT seq, hash FROM audit.audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (last[0] + 1, last[1]) if last else (1, GENESIS)
            entry = make_entry(
                seq=seq, prev_hash=prev, run_id=run_id, actor=actor, action=action, details=details, outcome=outcome
            )
            conn.execute(
                "INSERT INTO audit.audit_log (seq, at, run_id, actor, action, details, outcome, prev_hash, hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    entry.seq,
                    entry.at,
                    entry.run_id,
                    entry.actor,
                    entry.action,
                    Jsonb(entry.details),
                    Jsonb(entry.outcome),
                    entry.prev_hash,
                    entry.hash,
                ),
            )
        logger.info("Audit entry", extra={"seq": entry.seq, "action": action, "actor": actor, "run_id": run_id})
        return entry

    def _select(self, where: str, params: tuple[Any, ...], limit: int | None = None) -> list[AuditEntry]:
        # `where` is one of two constant fragments chosen in entries(); values are always parameters.
        sql = "SELECT seq, at, run_id, actor, action, details, outcome, prev_hash, hash FROM audit.audit_log " + where  # noqa: S608
        sql += " ORDER BY seq" + (" DESC LIMIT %s" if limit else "")
        with self._pool.connection() as conn:
            rows = conn.execute(sql, (*params, limit) if limit else params).fetchall()
        keys = ("seq", "at", "run_id", "actor", "action", "details", "outcome", "prev_hash", "hash")
        out = [AuditEntry(**dict(zip(keys, r, strict=True))) for r in rows]
        return sorted(out, key=lambda e: e.seq)

    def entries(self, *, run_id: str | None = None, limit: int = 500) -> list[AuditEntry]:
        if run_id is None:
            return self._select("", (), limit)
        return self._select("WHERE run_id = %s", (run_id,), limit)

    def verify(self) -> ChainVerification:
        return verify_chain(self._select("", ()))

    def close(self) -> None:
        self._pool.close()
