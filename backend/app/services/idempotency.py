"""Idempotency for state-changing requests (design 8).

A client sends a request id with every create. The first call records
(request_id, action, fingerprint) in the *same transaction* as the action, so a failed action
leaves no record and a retry is simply a first call. An identical replay returns the stored
result; the same id with another action or payload is a client bug and is refused.
"""
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Context, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


_NORMALISE = Context(prec=200)  # the thread's default (28 digits) would merge values that differ further out


class IdempotencyConflict(Exception):
    """The request id was already used for a different action or payload (or has no result)."""


@dataclass(frozen=True)
class Replay:
    entity_type: str
    entity_id: int


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError(f"cannot fingerprint non-finite decimal {value}")
        return format(value.normalize(_NORMALISE), "f")  # 40, 40.000 and 4E+1 all become "40"
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        items = [_canonical(v) for v in value]
        if value and all(isinstance(v, Mapping) and "item_id" in v for v in value):
            order = sorted(range(len(value)), key=lambda i: (str(value[i]["item_id"]), json.dumps(items[i], sort_keys=True)))
            items = [items[i] for i in order]  # line lists have no meaningful order
        return items
    if isinstance(value, (UUID, date, datetime)):
        return str(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise TypeError(f"cannot fingerprint a value of type {type(value).__name__}")


def fingerprint(action: str, payload: Mapping) -> str:
    """SHA-256 of the canonical JSON of (action, payload)."""
    text_form = json.dumps({"action": action, "payload": _canonical(payload)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text_form.encode("utf-8")).hexdigest()


def begin_request(s: Session, *, request_id: UUID, action: str, payload: Mapping) -> Replay | None:
    """None for a first call (the record is inserted); the stored result for an identical replay.

    A concurrent first call with the same id blocks on the primary key until the other transaction ends,
    then sees its committed row (READ COMMITTED gives the next statement a fresh snapshot).
    """
    fp = fingerprint(action, payload)
    inserted = s.execute(
        text("INSERT INTO request_log (request_id, action, fingerprint) VALUES (:r, :a, :f) ON CONFLICT (request_id) DO NOTHING RETURNING request_id"),
        {"r": request_id, "a": action, "f": fp}).first()
    if inserted is not None:
        return None
    row = s.execute(text("SELECT action, fingerprint, result_entity_type, result_entity_id FROM request_log WHERE request_id = :r"),
                    {"r": request_id}).one()
    if row.action != action or row.fingerprint != fp:
        raise IdempotencyConflict(f"request {request_id} was already used for a different action or payload")
    if row.result_entity_type is None:
        raise IdempotencyConflict(f"request {request_id} has no recorded result")
    return Replay(row.result_entity_type, row.result_entity_id)


def finish_request(s: Session, request_id: UUID, entity_type: str, entity_id: int) -> None:
    updated = s.execute(
        text("UPDATE request_log SET result_entity_type = :t, result_entity_id = :i WHERE request_id = :r AND result_entity_type IS NULL"),
        {"t": entity_type, "i": entity_id, "r": request_id})
    if updated.rowcount != 1:
        raise LookupError(f"request {request_id} was never begun or already has a result")
