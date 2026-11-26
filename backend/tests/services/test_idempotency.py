"""Idempotency records against real PostgreSQL with real commits."""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.services.idempotency import IdempotencyConflict, Replay, begin_request, fingerprint, finish_request

D = Decimal
PAYLOAD = {"customer_id": 7, "lines": [{"item_id": 2, "qty": D("40")}, {"item_id": 1, "qty": D("10.5")}]}


def _count(s):
    return s.execute(text("SELECT count(*) FROM request_log")).scalar_one()


def test_first_call_returns_none(db_session):
    assert begin_request(db_session, request_id=uuid.uuid4(), action="order.create", payload=PAYLOAD) is None


def test_same_request_same_payload_returns_replay(db_session):
    rid = uuid.uuid4()
    assert begin_request(db_session, request_id=rid, action="order.create", payload=PAYLOAD) is None
    finish_request(db_session, rid, "order", 41)
    db_session.commit()
    assert begin_request(db_session, request_id=rid, action="order.create", payload=PAYLOAD) == Replay("order", 41)
    assert _count(db_session) == 1


def test_same_request_different_payload_raises_conflict(db_session):
    rid = uuid.uuid4()
    begin_request(db_session, request_id=rid, action="order.create", payload=PAYLOAD)
    finish_request(db_session, rid, "order", 41)
    db_session.commit()
    with pytest.raises(IdempotencyConflict):
        begin_request(db_session, request_id=rid, action="order.create", payload={**PAYLOAD, "customer_id": 8})


def test_request_id_reused_for_other_action_raises_conflict(db_session):
    rid = uuid.uuid4()
    begin_request(db_session, request_id=rid, action="order.create", payload=PAYLOAD)
    finish_request(db_session, rid, "order", 41)
    db_session.commit()
    with pytest.raises(IdempotencyConflict):
        begin_request(db_session, request_id=rid, action="payment.create", payload=PAYLOAD)


def test_failed_transaction_leaves_no_request_row(db_session):
    rid = uuid.uuid4()
    begin_request(db_session, request_id=rid, action="order.create", payload=PAYLOAD)
    db_session.rollback()
    assert _count(db_session) == 0
    assert begin_request(db_session, request_id=rid, action="order.create", payload=PAYLOAD) is None  # a retry is a first call


def test_a_request_that_never_finished_cannot_be_replayed(db_session):
    rid = uuid.uuid4()
    begin_request(db_session, request_id=rid, action="order.create", payload=PAYLOAD)
    db_session.commit()
    with pytest.raises(IdempotencyConflict, match="no recorded result"):
        begin_request(db_session, request_id=rid, action="order.create", payload=PAYLOAD)


def test_fingerprint_ignores_line_order_and_decimal_scale():
    a = {"lines": [{"item_id": 2, "qty": D("40")}, {"item_id": 1, "qty": D("10.5")}]}
    b = {"lines": [{"qty": D("10.500"), "item_id": 1}, {"qty": D("40.000"), "item_id": 2}]}
    assert fingerprint("order.create", a) == fingerprint("order.create", b)


def test_fingerprint_separates_actions_and_values():
    assert fingerprint("a", PAYLOAD) != fingerprint("b", PAYLOAD)
    assert fingerprint("a", {"qty": D("1")}) != fingerprint("a", {"qty": D("1.001")})


def test_fingerprint_is_a_sha256_hex_digest():
    assert len(fingerprint("a", {})) == 64 and set(fingerprint("a", {})) <= set("0123456789abcdef")


def test_fingerprint_rejects_values_it_cannot_canonicalise():
    with pytest.raises(TypeError):
        fingerprint("a", {"x": object()})


def test_finishing_an_unknown_request_is_an_error(db_session):
    with pytest.raises(LookupError):
        finish_request(db_session, uuid.uuid4(), "order", 1)
