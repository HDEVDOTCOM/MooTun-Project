import base64
import hashlib
import hmac
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

import app as app_module
from database import configure_database
from line_api import LineTransportError
from models import PendingAction, PendingTransaction, ProcessedWebhookEvent, Transaction


SECRET = "test-channel-secret"


def _signed_body(payload: object) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    signature = base64.b64encode(
        hmac.new(SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()
    return body, {
        "content-type": "application/json",
        "x-line-signature": signature,
    }


def _text_event(
    event_id: str,
    user_id: str,
    text: str,
    *,
    timestamp: int = 1_789_359_600_000,
) -> dict[str, object]:
    return {
        "type": "message",
        "webhookEventId": event_id,
        "timestamp": timestamp,
        "replyToken": f"reply-{event_id}",
        "source": {"type": "user", "userId": user_id},
        "message": {"type": "text", "id": f"message-{event_id}", "text": text},
    }


def _post_text(
    client: TestClient,
    event_id: str,
    user_id: str,
    text: str,
    *,
    timestamp: int = 1_789_359_600_000,
):
    body, headers = _signed_body(
        {"events": [_text_event(event_id, user_id, text, timestamp=timestamp)]}
    )
    return client.post("/webhook", content=body, headers=headers)


def _row_state(item):
    return tuple(getattr(item, column.name) for column in item.__table__.columns)


@pytest.fixture()
def webhook_client(tmp_path, monkeypatch):
    engine = configure_database(f"sqlite:///{tmp_path / 'webhook-test.db'}")
    replies: list[tuple[str, str, str]] = []

    async def fake_reply(reply_token: str, text: str, token: str) -> None:
        replies.append((reply_token, text, token))

    monkeypatch.setattr(app_module, "LINE_CHANNEL_SECRET", SECRET)
    monkeypatch.setattr(app_module, "LINE_CHANNEL_ACCESS_TOKEN", "test-access-token")
    monkeypatch.setattr(app_module, "reply_text", fake_reply)

    with TestClient(app_module.app) as client:
        yield client, engine, replies
    engine.dispose()


def test_signature_is_required_before_any_database_write(webhook_client):
    client, engine, replies = webhook_client
    body, _ = _signed_body({"events": [_text_event("evt-invalid", "U-alice", "จ่าย 50 อาหาร")]})

    response = client.post(
        "/webhook",
        content=body,
        headers={"content-type": "application/json", "x-line-signature": "invalid"},
    )

    assert response.status_code == 400
    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 0
        assert session.scalar(select(func.count(ProcessedWebhookEvent.webhook_event_id))) == 0
    assert replies == []


def test_redelivery_is_idempotent_but_distinct_same_text_is_recorded(webhook_client):
    client, engine, replies = webhook_client
    first = {"events": [_text_event("evt-1", "U-alice", "จ่าย 50 อาหาร")]}
    same_text_new_event = {"events": [_text_event("evt-2", "U-alice", "จ่าย 50 อาหาร")]}

    for payload in (first, first, same_text_new_event):
        body, headers = _signed_body(payload)
        assert client.post("/webhook", content=body, headers=headers).status_code == 200

    with Session(engine) as session:
        items = list(session.scalars(select(Transaction).order_by(Transaction.id)))
        assert [item.amount_satang for item in items] == [5000, 5000]
        assert session.scalar(select(func.count(ProcessedWebhookEvent.webhook_event_id))) == 2
    assert [reply[0] for reply in replies] == ["reply-evt-1", "reply-evt-2"]


def test_postgresql_claim_does_not_depend_on_insert_rowcount(
    webhook_client,
    monkeypatch,
):
    client, engine, replies = webhook_client
    original_execute = Session.execute

    class InsertResultWithUnknownRowcount:
        rowcount = -1

        def __init__(self, result):
            self._result = result

        def scalar_one_or_none(self):
            return self._result.scalar_one_or_none()

        def __getattr__(self, name):
            return getattr(self._result, name)

    def hide_insert_rowcount(session, statement, *args, **kwargs):
        result = original_execute(session, statement, *args, **kwargs)
        if (
            getattr(statement, "is_insert", False)
            and statement.table.name == "processed_webhook_events"
        ):
            return InsertResultWithUnknownRowcount(result)
        return result

    monkeypatch.setattr(engine.dialect, "name", "postgresql")
    monkeypatch.setattr(Session, "execute", hide_insert_rowcount)

    assert _post_text(client, "evt-postgresql", "U-alice", "ข้าว 1234").status_code == 200
    with Session(engine) as session:
        item = session.scalar(select(Transaction))
        event = session.get(ProcessedWebhookEvent, "evt-postgresql")
        assert item is not None
        assert item.line_user_id == "U-alice"
        assert item.amount_satang == 123400
        assert event is not None
        assert event.response_text
        assert event.reply_sent is True
    assert [reply[0] for reply in replies] == ["reply-evt-postgresql"]

    assert _post_text(client, "evt-postgresql", "U-alice", "ข้าว 1234").status_code == 200
    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 1
    assert [reply[0] for reply in replies] == ["reply-evt-postgresql"]


def test_reply_failure_returns_502_and_redelivery_retries_saved_response(
    webhook_client, monkeypatch
):
    client, engine, _ = webhook_client
    attempts: list[tuple[str, str, str]] = []

    async def fail_once(reply_token: str, text: str, token: str) -> None:
        attempts.append((reply_token, text, token))
        if len(attempts) == 1:
            raise LineTransportError("temporary failure")

    monkeypatch.setattr(app_module, "reply_text", fail_once)
    body, headers = _signed_body(
        {"events": [_text_event("evt-retry", "U-alice", "จ่าย 75 อาหาร")]}
    )

    first_response = client.post("/webhook", content=body, headers=headers)
    assert first_response.status_code == 502

    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 1
        processed = session.get(ProcessedWebhookEvent, "evt-retry")
        assert processed is not None
        assert processed.response_text
        assert processed.reply_sent is False

    redelivery_response = client.post("/webhook", content=body, headers=headers)
    assert redelivery_response.status_code == 200

    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 1
        processed = session.get(ProcessedWebhookEvent, "evt-retry")
        assert processed is not None
        assert processed.reply_sent is True

    assert len(attempts) == 2
    assert attempts[0] == attempts[1]


def test_webhook_uses_bangkok_date_and_ignores_group_messages(webhook_client):
    client, engine, replies = webhook_client
    # 2026-09-13 17:05 UTC is 2026-09-14 00:05 in Bangkok.
    direct_event = _text_event(
        "evt-boundary",
        "U-alice",
        "เมื่อวานจ่าย 40 เดินทาง",
        timestamp=1_789_319_100_000,
    )
    group_event = _text_event("evt-group", "U-alice", "จ่าย 999 อาหาร")
    group_event["source"] = {
        "type": "group",
        "groupId": "G-classroom",
        "userId": "U-alice",
    }
    body, headers = _signed_body({"events": [direct_event, group_event]})

    assert client.post("/webhook", content=body, headers=headers).status_code == 200

    with Session(engine) as session:
        items = list(session.scalars(select(Transaction)))
        assert len(items) == 1
        assert items[0].line_user_id == "U-alice"
        assert items[0].occurred_on == date(2026, 9, 13)
        assert items[0].amount_satang == 4000
    assert [reply[0] for reply in replies] == ["reply-evt-boundary"]


def test_natural_language_entry_records_item_for_correct_user(webhook_client):
    client, engine, replies = webhook_client
    payload = {
        "events": [
            _text_event("evt-natural-a", "U-alice", "ข้าวมันไก่ 50"),
            _text_event("evt-natural-b", "U-bob", "เงินเดือน 20000"),
        ]
    }
    body, headers = _signed_body(payload)

    assert client.post("/webhook", content=body, headers=headers).status_code == 200

    with Session(engine) as session:
        items = list(session.scalars(select(Transaction).order_by(Transaction.id)))
        assert [
            (
                item.line_user_id,
                item.transaction_type,
                item.amount_satang,
                item.category,
                item.description,
            )
            for item in items
        ] == [
            ("U-alice", "expense", 5000, "อาหาร", "ข้าวมันไก่"),
            ("U-bob", "income", 2_000_000, "เงินเดือน", "เงินเดือน"),
        ]
    assert "รายการ: ข้าวมันไก่" in replies[0][1]
    assert "หมวด: อาหาร" in replies[0][1]
    assert "รายการ: เงินเดือน" in replies[1][1]


def test_ambiguous_natural_language_does_not_write_transaction(webhook_client):
    client, engine, replies = webhook_client
    body, headers = _signed_body(
        {"events": [_text_event("evt-amount-only", "U-alice", "500")]}
    )

    assert client.post("/webhook", content=body, headers=headers).status_code == 200

    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 0
        assert session.get(PendingTransaction, "U-alice") is not None
    assert replies[0][1] == "500 บาท เป็นรายรับหรือรายจ่ายครับ?"


def test_item_then_amount_completes_pending_transaction(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-item", "U-alice", "ข้าว").status_code == 200
    assert replies[-1][1] == "ข้าว ราคาเท่าไหร่ครับ? เช่น 50"

    assert _post_text(client, "evt-amount", "U-alice", "50").status_code == 200
    with Session(engine) as session:
        item = session.scalar(select(Transaction))
        assert item is not None
        assert item.transaction_type == "expense"
        assert item.amount_satang == 5000
        assert item.description == "ข้าว"
        assert session.get(PendingTransaction, "U-alice") is None


def test_amount_then_direction_uses_fallback_transaction_data(webhook_client):
    client, engine, _ = webhook_client
    assert _post_text(client, "evt-amount", "U-alice", "500").status_code == 200
    assert _post_text(client, "evt-direction", "U-alice", "รายจ่าย").status_code == 200

    with Session(engine) as session:
        item = session.scalar(select(Transaction))
        assert item is not None
        assert item.transaction_type == "expense"
        assert item.amount_satang == 50000
        assert item.category == "อื่นๆ"
        assert item.description == "ไม่ระบุรายการ"


def test_edit_latest_replaces_only_saved_transaction(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-seed", "U-alice", "ข้าว 50").status_code == 200
    with Session(engine) as session:
        seeded = session.scalar(select(Transaction))
        assert seeded is not None
        original_date = seeded.occurred_on

    assert _post_text(client, "evt-edit", "U-alice", "แก้ไข ข้าวมันไก่ 120").status_code == 200

    with Session(engine) as session:
        items = list(session.scalars(select(Transaction)))
        assert len(items) == 1
        assert items[0].amount_satang == 12000
        assert items[0].category == "อาหาร"
        assert items[0].description == "ข้าวมันไก่"
        assert items[0].occurred_on == original_date
    assert "แก้ไขรายจ่ายล่าสุดแล้ว" in replies[-1][1]
    assert "จำนวน: 120 บาท" in replies[-1][1]


def test_edit_latest_without_saved_transaction_is_rejected(webhook_client):
    client, engine, replies = webhook_client

    assert _post_text(client, "evt-no-latest", "U-alice", "แก้ไข ข้าว 60").status_code == 200

    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 0
        assert session.get(PendingTransaction, "U-alice") is None
    assert replies[-1][1] == "ยังไม่มีรายการให้แก้ไข"


def test_edit_latest_inherits_or_overrides_date(webhook_client):
    client, engine, _ = webhook_client
    assert _post_text(client, "evt-dated", "U-alice", "ข้าว 50 01/09/2569").status_code == 200

    assert _post_text(client, "evt-edit-inherit", "U-alice", "แก้ ข้าว 60").status_code == 200
    with Session(engine) as session:
        item = session.scalar(select(Transaction))
        assert item is not None
        assert item.occurred_on == date(2026, 9, 1)

    assert _post_text(client, "evt-edit-date", "U-alice", "แก้ ข้าว 70 02/09/2569").status_code == 200
    with Session(engine) as session:
        item = session.scalar(select(Transaction))
        assert item is not None
        assert item.amount_satang == 7000
        assert item.occurred_on == date(2026, 9, 2)


def test_edit_latest_does_not_mutate_pending_draft(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-saved", "U-alice", "ข้าว 50").status_code == 200
    assert _post_text(client, "evt-draft", "U-alice", "กาแฟ").status_code == 200

    with Session(engine) as session:
        pending = session.get(PendingTransaction, "U-alice")
        assert pending is not None
        original_pending = tuple(
            getattr(pending, column.name) for column in PendingTransaction.__table__.columns
        )

    assert _post_text(client, "evt-edit-while-pending", "U-alice", "แก้ ข้าวมันไก่ 120").status_code == 200

    with Session(engine) as session:
        item = session.scalar(select(Transaction))
        assert item is not None
        assert item.description == "ข้าวมันไก่"
        assert item.amount_satang == 12000
        pending = session.get(PendingTransaction, "U-alice")
        assert pending is not None
        assert tuple(
            getattr(pending, column.name) for column in PendingTransaction.__table__.columns
        ) == original_pending

    assert _post_text(client, "evt-followup-after-edit", "U-alice", "50").status_code == 200
    assert "รายการ: กาแฟ" in replies[-1][1]
    with Session(engine) as session:
        items = list(session.scalars(select(Transaction).order_by(Transaction.id)))
        assert [(item.description, item.amount_satang) for item in items] == [
            ("ข้าวมันไก่", 12000),
            ("กาแฟ", 5000),
        ]
        assert session.get(PendingTransaction, "U-alice") is None


def test_incomplete_edit_preserves_saved_transaction_and_pending_draft(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-saved-invalid", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-draft-invalid", "U-alice", "กาแฟ").status_code == 200

    with Session(engine) as session:
        saved = session.scalar(select(Transaction))
        pending = session.get(PendingTransaction, "U-alice")
        assert saved is not None
        assert pending is not None
        original_saved = tuple(
            getattr(saved, column.name) for column in Transaction.__table__.columns
        )
        original_pending = tuple(
            getattr(pending, column.name) for column in PendingTransaction.__table__.columns
        )

    for index, text in enumerate(("แก้ 50", "แก้ ข้าว"), start=1):
        assert _post_text(
            client, f"evt-invalid-edit-{index}", "U-alice", text
        ).status_code == 200
        assert replies[-1][1].startswith(
            "คำสั่งแก้ไขต้องระบุรายการและจำนวนเงินให้ครบในข้อความเดียวครับ"
        )
        with Session(engine) as session:
            saved = session.scalar(select(Transaction))
            pending = session.get(PendingTransaction, "U-alice")
            assert saved is not None
            assert pending is not None
            assert tuple(
                getattr(saved, column.name) for column in Transaction.__table__.columns
            ) == original_saved
            assert tuple(
                getattr(pending, column.name)
                for column in PendingTransaction.__table__.columns
            ) == original_pending

    assert _post_text(client, "evt-followup-after-invalid", "U-alice", "50").status_code == 200
    assert "รายการ: กาแฟ" in replies[-1][1]
    with Session(engine) as session:
        items = list(session.scalars(select(Transaction).order_by(Transaction.id)))
        assert [(item.description, item.amount_satang) for item in items] == [
            ("ข้าว", 4000),
            ("กาแฟ", 5000),
        ]
        assert session.get(PendingTransaction, "U-alice") is None


def test_edit_latest_redelivery_does_not_edit_intervening_latest(
    webhook_client,
    monkeypatch,
):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-seed-idem", "U-alice", "ข้าว 50").status_code == 200

    body, headers = _signed_body(
        {"events": [_text_event("evt-edit-idem", "U-alice", "แก้ ข้าวมันไก่ 120")]}
    )
    original_reply = app_module.reply_text
    attempted_responses = []

    async def fail_edit_reply(reply_token, response_text, token):
        attempted_responses.append(response_text)
        raise LineTransportError("simulated edit reply failure")

    monkeypatch.setattr(app_module, "reply_text", fail_edit_reply)
    assert client.post("/webhook", content=body, headers=headers).status_code == 502
    monkeypatch.setattr(app_module, "reply_text", original_reply)

    assert _post_text(client, "evt-new-latest", "U-alice", "BTS 47").status_code == 200
    assert client.post("/webhook", content=body, headers=headers).status_code == 200
    assert replies[-1][1] == attempted_responses[0]
    reply_count = len(replies)
    assert client.post("/webhook", content=body, headers=headers).status_code == 200
    assert len(replies) == reply_count

    with Session(engine) as session:
        items = list(session.scalars(select(Transaction).order_by(Transaction.id)))
        assert [(item.description, item.amount_satang) for item in items] == [
            ("ข้าวมันไก่", 12000),
            ("BTS", 4700),
        ]
        event = session.get(ProcessedWebhookEvent, "evt-edit-idem")
        assert event is not None
        assert event.response_text == attempted_responses[0]
        assert event.reply_sent is True
        assert session.scalar(select(func.count(ProcessedWebhookEvent.webhook_event_id))) == 3
    assert [reply[0] for reply in replies].count("reply-evt-edit-idem") == 1


def test_active_delete_confirmation_cancel_preserves_pending_draft(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-delete-seed", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-delete-request", "U-alice", "ลบล่าสุด").status_code == 200
    assert "ยืนยันลบรายการนี้หรือไม่" in replies[-1][1]
    assert _post_text(client, "evt-delete-draft", "U-alice", "กาแฟ").status_code == 200
    with Session(engine) as session:
        pending = session.get(PendingTransaction, "U-alice")
        assert pending is not None
        pending_state = _row_state(pending)

    assert _post_text(client, "evt-delete-cancel", "U-alice", "ยกเลิก").status_code == 200
    assert replies[-1][1] == "ยกเลิกการลบแล้ว"
    with Session(engine) as session:
        pending = session.get(PendingTransaction, "U-alice")
        assert pending is not None
        assert _row_state(pending) == pending_state
        assert session.get(PendingAction, "U-alice") is None
        assert session.scalar(select(func.count(Transaction.id))) == 1


@pytest.mark.parametrize("control", ["ยกเลิก", "ยืนยัน"])
def test_expired_delete_control_preserves_transaction_and_draft(
    webhook_client,
    control,
):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-expired-seed", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-expired-request", "U-alice", "ลบล่าสุด").status_code == 200
    assert _post_text(client, "evt-expired-draft", "U-alice", "กาแฟ").status_code == 200
    with Session(engine) as session:
        action = session.get(PendingAction, "U-alice")
        pending = session.get(PendingTransaction, "U-alice")
        transaction = session.scalar(select(Transaction))
        assert action is not None
        assert pending is not None
        assert transaction is not None
        action.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        pending_state = _row_state(pending)
        transaction_state = _row_state(transaction)
        session.commit()

    assert _post_text(
        client,
        f"evt-expired-{control}",
        "U-alice",
        control,
    ).status_code == 200
    assert replies[-1][1] == (
        "คำสั่งลบหมดอายุแล้ว กรุณาส่ง 'ลบล่าสุด' อีกครั้งหากต้องการลบ"
    )
    with Session(engine) as session:
        pending = session.get(PendingTransaction, "U-alice")
        transaction = session.scalar(select(Transaction))
        assert pending is not None
        assert transaction is not None
        assert _row_state(pending) == pending_state
        assert _row_state(transaction) == transaction_state
        assert session.get(PendingAction, "U-alice") is None


def test_confirm_without_pending_action_preserves_unknown_behavior(webhook_client):
    client, engine, replies = webhook_client

    assert _post_text(client, "evt-confirm-absent", "U-alice", "ยืนยัน").status_code == 200

    assert replies[-1][1].startswith("ยังไม่เข้าใจข้อความนี้")
    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 0


def test_delete_confirmation_deletes_bound_target_only(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-bound-seed-1", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-bound-seed-2", "U-alice", "BTS 47").status_code == 200
    assert _post_text(client, "evt-bound-request", "U-alice", "ลบล่าสุด").status_code == 200
    with Session(engine) as session:
        action = session.get(PendingAction, "U-alice")
        assert action is not None
        bound_target_id = action.target_transaction_id
        newer = Transaction(
            line_user_id="U-alice",
            transaction_type="income",
            amount_satang=50000,
            category="ค่าขนม",
            description="แม่ให้",
            occurred_on=date(2026, 9, 23),
            created_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        session.add(newer)
        session.commit()
        newer_id = newer.id

    assert _post_text(client, "evt-bound-confirm", "U-alice", "ยืนยัน").status_code == 200
    assert "ลบรายการเรียบร้อยแล้ว" in replies[-1][1]
    with Session(engine) as session:
        assert session.get(Transaction, bound_target_id) is None
        assert session.get(Transaction, newer_id) is not None
        assert session.scalar(select(func.count(Transaction.id))) == 2
        undo = session.get(PendingAction, "U-alice")
        assert undo is not None
        assert undo.action_type == "undo_delete"
        assert undo.snapshot_transaction_id == bound_target_id


def test_delete_confirmation_is_user_isolated(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-isolate-a", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-isolate-b", "U-bob", "เงินเดือน 20000").status_code == 200
    assert _post_text(client, "evt-isolate-request", "U-alice", "ลบล่าสุด").status_code == 200

    assert _post_text(client, "evt-isolate-b-confirm", "U-bob", "ยืนยัน").status_code == 200
    assert replies[-1][1].startswith("ยังไม่เข้าใจข้อความนี้")
    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 2

    assert _post_text(client, "evt-isolate-a-confirm", "U-alice", "ยืนยัน").status_code == 200
    with Session(engine) as session:
        items = list(session.scalars(select(Transaction)))
        assert [(item.line_user_id, item.description) for item in items] == [
            ("U-bob", "เงินเดือน")
        ]


def test_repeated_delete_replaces_action_with_current_latest(webhook_client):
    client, engine, _ = webhook_client
    assert _post_text(client, "evt-repeat-seed", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-repeat-first", "U-alice", "ลบล่าสุด").status_code == 200
    with Session(engine) as session:
        first_action = session.get(PendingAction, "U-alice")
        assert first_action is not None
        first_action_id = first_action.action_id
        first_target_id = first_action.target_transaction_id
        newer = Transaction(
            line_user_id="U-alice",
            transaction_type="expense",
            amount_satang=4700,
            category="เดินทาง",
            description="BTS",
            occurred_on=date(2026, 9, 23),
            created_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        session.add(newer)
        session.commit()
        newer_id = newer.id

    assert _post_text(client, "evt-repeat-second", "U-alice", "ลบล่าสุด").status_code == 200
    with Session(engine) as session:
        replacement = session.get(PendingAction, "U-alice")
        assert replacement is not None
        assert replacement.action_id != first_action_id
        assert replacement.target_transaction_id == newer_id
        assert replacement.target_transaction_id != first_target_id
        assert session.scalar(select(func.count(Transaction.id))) == 2


def test_repeated_delete_with_missing_only_target_leaves_no_action(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-empty-seed", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-empty-first", "U-alice", "ลบล่าสุด").status_code == 200
    with Session(engine) as session:
        action = session.get(PendingAction, "U-alice")
        assert action is not None
        session.execute(delete(Transaction).where(Transaction.id == action.target_transaction_id))
        session.commit()

    assert _post_text(client, "evt-empty-second", "U-alice", "ลบล่าสุด").status_code == 200
    assert replies[-1][1] == "ยังไม่มีรายการให้ลบ"
    with Session(engine) as session:
        assert session.get(PendingAction, "U-alice") is None
        assert session.scalar(select(func.count(Transaction.id))) == 0


@pytest.mark.parametrize(
    ("mutation", "expected_descriptions"),
    [
        ("BTS 47", ["ข้าว", "BTS"]),
        ("แก้ ข้าว 60", ["ข้าว"]),
    ],
)
def test_transaction_mutation_invalidates_delete_confirmation(
    webhook_client,
    mutation,
    expected_descriptions,
):
    client, engine, _ = webhook_client
    assert _post_text(client, "evt-invalidate-seed", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-invalidate-request", "U-alice", "ลบล่าสุด").status_code == 200

    assert _post_text(client, "evt-invalidate-mutation", "U-alice", mutation).status_code == 200

    with Session(engine) as session:
        assert session.get(PendingAction, "U-alice") is None
        items = list(session.scalars(select(Transaction).order_by(Transaction.id)))
        assert [item.description for item in items] == expected_descriptions
        if mutation.startswith("แก้"):
            assert items[0].amount_satang == 6000


def test_pending_completion_invalidates_delete_confirmation(webhook_client):
    client, engine, _ = webhook_client
    assert _post_text(client, "evt-complete-seed", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-complete-request", "U-alice", "ลบล่าสุด").status_code == 200
    assert _post_text(client, "evt-complete-draft", "U-alice", "กาแฟ").status_code == 200

    assert _post_text(client, "evt-complete-amount", "U-alice", "50").status_code == 200

    with Session(engine) as session:
        assert session.get(PendingAction, "U-alice") is None
        assert session.get(PendingTransaction, "U-alice") is None
        items = list(session.scalars(select(Transaction).order_by(Transaction.id)))
        assert [(item.description, item.amount_satang) for item in items] == [
            ("ข้าว", 4000),
            ("กาแฟ", 5000),
        ]


def test_stateless_commands_preserve_delete_confirmation(webhook_client):
    client, engine, _ = webhook_client
    assert _post_text(client, "evt-stateless-seed", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-stateless-request", "U-alice", "ลบล่าสุด").status_code == 200
    with Session(engine) as session:
        action = session.get(PendingAction, "U-alice")
        assert action is not None
        original_state = _row_state(action)

    for index, command in enumerate(
        ("ช่วยเหลือ", "รายการล่าสุด", "สรุปเดือนนี้", "เป้าหมายการออม")
    ):
        assert _post_text(
            client,
            f"evt-stateless-{index}",
            "U-alice",
            command,
        ).status_code == 200
        with Session(engine) as session:
            action = session.get(PendingAction, "U-alice")
            assert action is not None
            assert _row_state(action) == original_state


def test_missing_delete_target_consumes_action_and_preserves_draft(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-missing-seed", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-missing-request", "U-alice", "ลบล่าสุด").status_code == 200
    assert _post_text(client, "evt-missing-draft", "U-alice", "กาแฟ").status_code == 200
    with Session(engine) as session:
        action = session.get(PendingAction, "U-alice")
        pending = session.get(PendingTransaction, "U-alice")
        assert action is not None
        assert pending is not None
        pending_state = _row_state(pending)
        session.execute(delete(Transaction).where(Transaction.id == action.target_transaction_id))
        session.commit()

    assert _post_text(client, "evt-missing-confirm", "U-alice", "ยืนยัน").status_code == 200
    assert replies[-1][1] == "ไม่พบรายการที่ต้องการลบ (อาจถูกลบไปแล้ว)"
    with Session(engine) as session:
        pending = session.get(PendingTransaction, "U-alice")
        assert pending is not None
        assert _row_state(pending) == pending_state
        assert session.get(PendingAction, "U-alice") is None


def test_pending_action_invalidation_occ_loser_is_cached(webhook_client, monkeypatch):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-occ-seed", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-occ-request", "U-alice", "ลบล่าสุด").status_code == 200
    calls = 0

    def lose_invalidation(*args, **kwargs):
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(app_module, "invalidate_pending_action", lose_invalidation)
    body, headers = _signed_body(
        {"events": [_text_event("evt-action-occ-loser", "U-alice", "แก้ ข้าว 60")]}
    )
    assert client.post("/webhook", content=body, headers=headers).status_code == 200
    loser_reply = replies[-1][1]
    assert loser_reply == "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"
    assert calls == 1

    assert client.post("/webhook", content=body, headers=headers).status_code == 200
    assert calls == 1
    with Session(engine) as session:
        item = session.scalar(select(Transaction))
        action = session.get(PendingAction, "U-alice")
        event = session.get(ProcessedWebhookEvent, "evt-action-occ-loser")
        assert item is not None
        assert item.amount_satang == 4000
        assert action is not None
        assert event is not None
        assert event.response_text == loser_reply
        assert event.reply_sent is True


@pytest.mark.parametrize("control", ["ยกเลิก", "ยืนยัน"])
def test_pending_action_lazy_expiry_occ_loser_is_cached_and_not_reinterpreted(
    webhook_client,
    monkeypatch,
    control,
):
    client, engine, _ = webhook_client
    now = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)
    replacement_expiry = now + timedelta(minutes=10)
    old_action_id = "a" * 32
    replacement_action_id = "b" * 32
    with Session(engine) as session:
        target = Transaction(
            line_user_id="U-alice",
            transaction_type="expense",
            amount_satang=4000,
            category="อาหาร",
            description="ข้าว",
            occurred_on=now.date(),
            created_at=now,
        )
        session.add(target)
        session.flush()
        session.add_all(
            [
                PendingAction(
                    line_user_id="U-alice",
                    action_id=old_action_id,
                    version=1,
                    action_type="confirm_delete",
                    target_transaction_id=target.id,
                    created_at=now - timedelta(minutes=20),
                    expires_at=now - timedelta(minutes=10),
                ),
                PendingTransaction(
                    line_user_id="U-alice",
                    draft_id="c" * 32,
                    version=3,
                    transaction_type="expense",
                    amount_satang=None,
                    category="อาหาร",
                    description="กาแฟ",
                    inference_rule="expense.food",
                    occurred_on=now.date(),
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                ),
            ]
        )
        session.commit()
        target_id = target.id

    original_execute = Session.execute
    replaced = False

    def replace_before_cleanup(session, statement, *args, **kwargs):
        nonlocal replaced
        is_action_delete = (
            getattr(statement, "is_delete", False)
            and statement.table.name == "pending_actions"
        )
        if is_action_delete and not replaced:
            replaced = True
            original_execute(
                session,
                delete(PendingAction)
                .where(PendingAction.line_user_id == "U-alice")
                .execution_options(synchronize_session=False),
            )
            original_execute(
                session,
                insert(PendingAction).values(
                    line_user_id="U-alice",
                    action_id=replacement_action_id,
                    version=7,
                    action_type="confirm_delete",
                    target_transaction_id=target_id,
                    created_at=now,
                    expires_at=replacement_expiry,
                ),
            )
        return original_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(app_module, "_utc_now", lambda: now)
    monkeypatch.setattr(Session, "execute", replace_before_cleanup)
    attempts = []

    async def fail_first_reply(reply_token, response_text, token):
        attempts.append(response_text)
        if len(attempts) == 1:
            raise LineTransportError("simulated reply failure")

    monkeypatch.setattr(app_module, "reply_text", fail_first_reply)
    assert _post_text(client, "evt-action-expiry-loser", "U-alice", control).status_code == 502
    retry_reply = "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"
    assert attempts == [retry_reply]

    with Session(engine) as session:
        replacement = session.get(PendingAction, "U-alice")
        pending = session.get(PendingTransaction, "U-alice")
        event = session.get(ProcessedWebhookEvent, "evt-action-expiry-loser")
        assert replacement is not None
        assert replacement.action_id == replacement_action_id
        assert replacement.version == 7
        assert pending is not None
        assert pending.draft_id == "c" * 32
        assert pending.version == 3
        assert session.get(Transaction, target_id) is not None
        assert event is not None
        assert event.response_text == retry_reply
        assert event.reply_sent is False

    assert _post_text(client, "evt-action-expiry-loser", "U-alice", control).status_code == 200
    assert attempts == [retry_reply, retry_reply]
    with Session(engine) as session:
        assert session.get(PendingAction, "U-alice").action_id == replacement_action_id
        assert session.get(PendingTransaction, "U-alice").draft_id == "c" * 32
        assert session.get(Transaction, target_id) is not None
        assert session.get(ProcessedWebhookEvent, "evt-action-expiry-loser").reply_sent is True


def test_delete_confirm_cancel_webhook_redelivery_is_idempotent(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-idem-seed", "U-alice", "ข้าว 40").status_code == 200

    delete_body, delete_headers = _signed_body(
        {"events": [_text_event("evt-idem-delete", "U-alice", "ลบล่าสุด")]}
    )
    assert client.post("/webhook", content=delete_body, headers=delete_headers).status_code == 200
    with Session(engine) as session:
        first_action_id = session.get(PendingAction, "U-alice").action_id
    assert client.post("/webhook", content=delete_body, headers=delete_headers).status_code == 200
    with Session(engine) as session:
        assert session.get(PendingAction, "U-alice").action_id == first_action_id
    assert [reply[0] for reply in replies].count("reply-evt-idem-delete") == 1

    cancel_body, cancel_headers = _signed_body(
        {"events": [_text_event("evt-idem-cancel", "U-alice", "ยกเลิก")]}
    )
    assert client.post("/webhook", content=cancel_body, headers=cancel_headers).status_code == 200
    assert client.post("/webhook", content=cancel_body, headers=cancel_headers).status_code == 200
    with Session(engine) as session:
        assert session.get(PendingAction, "U-alice") is None
        assert session.scalar(select(func.count(Transaction.id))) == 1
    assert [reply[0] for reply in replies].count("reply-evt-idem-cancel") == 1

    assert _post_text(client, "evt-idem-delete-2", "U-alice", "ลบล่าสุด").status_code == 200
    confirm_body, confirm_headers = _signed_body(
        {"events": [_text_event("evt-idem-confirm", "U-alice", "ยืนยัน")]}
    )
    assert client.post("/webhook", content=confirm_body, headers=confirm_headers).status_code == 200
    assert client.post("/webhook", content=confirm_body, headers=confirm_headers).status_code == 200
    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 0
        undo = session.get(PendingAction, "U-alice")
        assert undo is not None
        assert undo.action_type == "undo_delete"
    assert [reply[0] for reply in replies].count("reply-evt-idem-confirm") == 1


def test_conflict_preserves_pending_state_until_valid_followup(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-draft", "U-alice", "ข้าว").status_code == 200
    with Session(engine) as session:
        pending = session.get(PendingTransaction, "U-alice")
        original_state = (pending.version, pending.expires_at)

    assert _post_text(client, "evt-conflict", "U-alice", "รายรับ").status_code == 200
    assert replies[-1][1].startswith("ข้อมูลนี้ขัดกับรายการที่ค้างไว้ครับ")
    with Session(engine) as session:
        pending = session.get(PendingTransaction, "U-alice")
        assert (pending.version, pending.expires_at) == original_state
        assert session.scalar(select(func.count(Transaction.id))) == 0

    assert _post_text(client, "evt-valid", "U-alice", "50").status_code == 200
    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 1
        assert session.get(PendingTransaction, "U-alice") is None


def test_stateless_command_preserves_pending_state(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-draft", "U-alice", "500").status_code == 200
    with Session(engine) as session:
        pending = session.get(PendingTransaction, "U-alice")
        original_state = (pending.version, pending.expires_at)

    assert _post_text(client, "evt-summary", "U-alice", "สรุปเดือนนี้").status_code == 200
    assert replies[-1][1].startswith("📊")
    with Session(engine) as session:
        pending = session.get(PendingTransaction, "U-alice")
        assert (pending.version, pending.expires_at) == original_state


def test_complete_transaction_atomically_replaces_pending_draft(webhook_client):
    client, engine, _ = webhook_client
    assert _post_text(client, "evt-draft", "U-alice", "500").status_code == 200
    assert _post_text(client, "evt-complete", "U-alice", "ข้าว 50").status_code == 200
    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 1
        assert session.get(PendingTransaction, "U-alice") is None


def test_transaction_failure_rolls_back_pending_deletion_and_event_claim(
    webhook_client,
    monkeypatch,
):
    client, engine, _ = webhook_client
    assert _post_text(client, "evt-draft", "U-alice", "ข้าว").status_code == 200

    def fail_transaction(*args, **kwargs):
        raise RuntimeError("simulated transaction insert failure")

    monkeypatch.setattr(app_module, "add_transaction", fail_transaction)
    with pytest.raises(RuntimeError, match="simulated transaction insert failure"):
        _post_text(client, "evt-complete-failure", "U-alice", "50")

    with Session(engine) as session:
        assert session.get(PendingTransaction, "U-alice") is not None
        assert session.get(ProcessedWebhookEvent, "evt-complete-failure") is None
        assert session.scalar(select(func.count(Transaction.id))) == 0


def test_cancel_is_conditional_on_an_active_pending_draft(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-no-draft", "U-alice", "ยกเลิก").status_code == 200
    assert "ข้อความนี้เป็นการปฏิเสธ" in replies[-1][1]

    assert _post_text(client, "evt-draft", "U-alice", "ข้าว").status_code == 200
    assert _post_text(client, "evt-cancel", "U-alice", "ยกเลิก").status_code == 200
    assert replies[-1][1] == "ยกเลิกรายการที่ค้างไว้แล้วครับ"
    with Session(engine) as session:
        assert session.get(PendingTransaction, "U-alice") is None


def test_expired_draft_is_not_used_as_followup_state(webhook_client):
    client, engine, _ = webhook_client
    assert _post_text(client, "evt-old", "U-alice", "ข้าว").status_code == 200
    with Session(engine) as session:
        pending = session.get(PendingTransaction, "U-alice")
        pending.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()

    assert _post_text(client, "evt-after-expiry", "U-alice", "50").status_code == 200
    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 0
        pending = session.get(PendingTransaction, "U-alice")
        assert pending is not None
        assert pending.amount_satang == 5000
        assert pending.transaction_type is None


def test_draft_expiring_between_read_and_mutation_is_not_completed(
    webhook_client,
    monkeypatch,
):
    client, engine, replies = webhook_client
    start = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)
    clock = iter(
        (
            start,
            start + timedelta(minutes=59),
            start + timedelta(minutes=61),
        )
    )
    monkeypatch.setattr(app_module, "_utc_now", lambda: next(clock))

    assert _post_text(client, "evt-expiry-draft", "U-alice", "ข้าว").status_code == 200
    assert _post_text(client, "evt-expiry-complete", "U-alice", "50").status_code == 200
    assert replies[-1][1] == (
        "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"
    )

    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 0
        assert session.get(PendingTransaction, "U-alice") is not None


@pytest.mark.parametrize("text", ["50", "ยกเลิก"])
def test_lazy_expiry_occ_loser_does_not_reinterpret_against_replacement(
    webhook_client,
    monkeypatch,
    text,
):
    client, engine, _ = webhook_client
    now = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)
    replacement_expiry = now + timedelta(hours=1)
    old_draft_id = "a" * 32
    replacement_draft_id = "b" * 32
    with Session(engine) as session:
        session.add(
            PendingTransaction(
                line_user_id="U-alice",
                draft_id=old_draft_id,
                version=1,
                transaction_type="expense",
                amount_satang=None,
                category="อาหาร",
                description="ข้าว",
                inference_rule="expense.food",
                occurred_on=now.date(),
                created_at=now - timedelta(hours=2),
                expires_at=now - timedelta(hours=1),
            )
        )
        session.commit()

    original_execute = Session.execute
    replaced = False

    def replace_before_cleanup(session, statement, *args, **kwargs):
        nonlocal replaced
        is_pending_delete = (
            getattr(statement, "is_delete", False)
            and statement.table.name == "pending_transactions"
        )
        if is_pending_delete and not replaced:
            replaced = True
            original_execute(
                session,
                delete(PendingTransaction)
                .where(PendingTransaction.line_user_id == "U-alice")
                .execution_options(synchronize_session=False),
            )
            original_execute(
                session,
                insert(PendingTransaction).values(
                    line_user_id="U-alice",
                    draft_id=replacement_draft_id,
                    version=7,
                    transaction_type="income",
                    amount_satang=None,
                    category="เงินเดือน",
                    description="เงินเดือน",
                    inference_rule="income.salary",
                    occurred_on=now.date(),
                    created_at=now,
                    expires_at=replacement_expiry,
                ),
            )
        return original_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(app_module, "_utc_now", lambda: now)
    monkeypatch.setattr(Session, "execute", replace_before_cleanup)
    attempts = []

    async def fail_first_reply(reply_token, response_text, token):
        attempts.append(response_text)
        if len(attempts) == 1:
            raise LineTransportError("simulated reply failure")

    monkeypatch.setattr(app_module, "reply_text", fail_first_reply)

    assert _post_text(client, "evt-expiry-loser", "U-alice", text).status_code == 502
    retry_reply = "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"
    assert attempts == [retry_reply]

    with Session(engine) as session:
        replacement = session.get(PendingTransaction, "U-alice")
        event = session.get(ProcessedWebhookEvent, "evt-expiry-loser")
        assert replacement is not None
        assert replacement.draft_id == replacement_draft_id
        assert replacement.version == 7
        assert replacement.transaction_type == "income"
        assert replacement.amount_satang is None
        assert replacement.description == "เงินเดือน"
        assert replacement.expires_at.replace(tzinfo=timezone.utc) == replacement_expiry
        assert session.scalar(select(func.count(Transaction.id))) == 0
        assert event is not None
        assert event.response_text == retry_reply
        assert event.reply_sent is False

    assert _post_text(client, "evt-expiry-loser", "U-alice", text).status_code == 200
    assert attempts == [retry_reply, retry_reply]
    with Session(engine) as session:
        replacement = session.get(PendingTransaction, "U-alice")
        event = session.get(ProcessedWebhookEvent, "evt-expiry-loser")
        assert replacement is not None
        assert replacement.draft_id == replacement_draft_id
        assert replacement.version == 7
        assert session.scalar(select(func.count(Transaction.id))) == 0
        assert event is not None
        assert event.response_text == retry_reply
        assert event.reply_sent is True


def test_followup_relative_date_uses_followup_event_time(webhook_client):
    client, engine, replies = webhook_client
    first_day = int(datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc).timestamp() * 1000)
    next_day = int(datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc).timestamp() * 1000)

    assert _post_text(
        client,
        "evt-date-draft",
        "U-alice",
        "ข้าว",
        timestamp=first_day,
    ).status_code == 200
    assert _post_text(
        client,
        "evt-date-conflict",
        "U-alice",
        "วันนี้ 50",
        timestamp=next_day,
    ).status_code == 200
    assert replies[-1][1].startswith("ข้อมูลนี้ขัดกับรายการที่ค้างไว้ครับ")

    assert _post_text(
        client,
        "evt-date-complete",
        "U-alice",
        "เมื่อวาน 50",
        timestamp=next_day,
    ).status_code == 200
    with Session(engine) as session:
        item = session.scalar(select(Transaction))
        assert item is not None
        assert item.occurred_on == date(2026, 9, 14)
        assert session.get(PendingTransaction, "U-alice") is None


def test_pending_transactions_are_isolated_by_user(webhook_client):
    client, engine, _ = webhook_client
    assert _post_text(client, "evt-alice", "U-alice", "ข้าว").status_code == 200
    assert _post_text(client, "evt-bob", "U-bob", "500").status_code == 200
    assert _post_text(client, "evt-alice-complete", "U-alice", "50").status_code == 200

    with Session(engine) as session:
        item = session.scalar(select(Transaction))
        assert item.line_user_id == "U-alice"
        assert session.get(PendingTransaction, "U-alice") is None
        assert session.get(PendingTransaction, "U-bob") is not None


def test_explicit_pending_direction_survives_semantic_followup(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-explicit", "U-alice", "จ่าย").status_code == 200
    assert _post_text(client, "evt-semantic", "U-alice", "เงินเดือน").status_code == 200
    assert replies[-1][1] == "เงินเดือน ราคาเท่าไหร่ครับ? เช่น 50"
    assert _post_text(client, "evt-amount", "U-alice", "50").status_code == 200

    with Session(engine) as session:
        item = session.scalar(select(Transaction))
        assert item is not None
        assert item.transaction_type == "expense"
        assert item.description == "เงินเดือน"
        assert session.get(PendingTransaction, "U-alice") is None


def test_occ_loser_reply_is_cached_and_not_reinterpreted(
    webhook_client, monkeypatch
):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-draft", "U-alice", "ข้าว").status_code == 200
    calls = 0

    def lose_delete(*args, **kwargs):
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(app_module, "delete_pending_transaction", lose_delete)
    assert _post_text(client, "evt-loser", "U-alice", "50").status_code == 200
    loser_reply = replies[-1][1]
    assert loser_reply == "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"
    assert calls == 1

    with Session(engine) as session:
        event = session.get(ProcessedWebhookEvent, "evt-loser")
        assert event.response_text == loser_reply
        assert session.scalar(select(func.count(Transaction.id))) == 0

    assert _post_text(client, "evt-loser", "U-alice", "50").status_code == 200
    assert calls == 1


def test_unsafe_rule_interactions_do_not_write_transactions(webhook_client):
    client, engine, replies = webhook_client
    payload = {
        "events": [
            _text_event("evt-negated", "U-alice", "ไม่ได้สอนพิเศษ 500"),
            _text_event("evt-interrupted-negation", "U-alice", "ขายข้าวไม่ได้ 50"),
            _text_event("evt-suffixed-negation", "U-alice", "ขายข้าวไม่ได้ครับ 50"),
            _text_event("evt-unknown-negation-suffix", "U-alice", "ขายข้าวไม่ได้หรอก 50"),
            _text_event("evt-conflict", "U-alice", "จ่ายข้าวแล้วได้เงิน 50"),
            _text_event("evt-attached-baht", "U-alice", "จ่ายข้าวแล้ว 50 บาทได้เงิน"),
            _text_event("evt-punctuated-amount", "U-alice", "จ่ายข้าวแล้ว (50 บาท) ได้เงิน"),
            _text_event("evt-wrapped-linked", "U-alice", "จ่ายข้าวแล้ว (ได้เงิน 50 บาท)"),
            _text_event("evt-wrapped-future", "U-alice", "เงินเดือน (จะเข้า 20000 บาท)"),
            _text_event("evt-prefix", "U-alice", "รับทราบ 50"),
        ]
    }
    body, headers = _signed_body(payload)

    assert client.post("/webhook", content=body, headers=headers).status_code == 200

    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 0
        assert session.scalar(select(func.count(ProcessedWebhookEvent.webhook_event_id))) == 10
    assert len(replies) == 10


UNDO_EXPIRED_REPLY = "หมดเวลาย้อนกลับแล้วครับ รายการที่ลบไปแล้วไม่สามารถกู้คืนได้"
UNDO_UNAVAILABLE_REPLY = "ไม่มีรายการให้ย้อนกลับครับ"
DELETE_NOT_CONFIRMED_REPLY = "ยังไม่มีการลบให้ย้อนกลับ กรุณายืนยันการลบก่อน"
UNDO_ID_COLLISION_REPLY = "ไม่สามารถย้อนกลับได้เนื่องจากมีข้อมูลอื่นทับซ้อน"


def _create_undo_snapshot(
    client,
    engine,
    *,
    user: str = "U-alice",
    prefix: str = "evt-undo",
) -> tuple[int, object]:
    assert _post_text(client, f"{prefix}-seed", user, "ข้าว 40").status_code == 200
    with Session(engine) as session:
        original = session.scalar(
            select(Transaction).where(Transaction.line_user_id == user)
        )
        original_id = original.id
        original_created_at = original.created_at
    assert _post_text(client, f"{prefix}-request", user, "ลบล่าสุด").status_code == 200
    assert _post_text(client, f"{prefix}-confirm", user, "ยืนยัน").status_code == 200
    with Session(engine) as session:
        action = session.get(PendingAction, user)
        assert action is not None
        assert action.action_type == "undo_delete"
    return original_id, original_created_at


def _expire_pending_action(engine, user: str = "U-alice") -> None:
    with Session(engine) as session:
        action = session.get(PendingAction, user)
        assert action is not None
        action.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()


def test_undo_restores_exact_original_transaction(webhook_client):
    client, engine, replies = webhook_client
    original_id, original_created_at = _create_undo_snapshot(client, engine)

    assert _post_text(client, "evt-undo-apply", "U-alice", "เลิกทำ").status_code == 200
    assert replies[-1][1].startswith("↩️ กู้คืนรายการเรียบร้อยแล้ว")
    with Session(engine) as session:
        items = list(session.scalars(select(Transaction)))
        assert len(items) == 1
        assert items[0].id == original_id
        assert items[0].created_at == original_created_at
        assert items[0].description == "ข้าว"
        assert items[0].amount_satang == 4000
        assert session.get(PendingAction, "U-alice") is None


def test_undo_restores_position_with_newer_transactions_present(webhook_client):
    client, engine, replies = webhook_client
    original_id, _ = _create_undo_snapshot(client, engine)
    assert _post_text(client, "evt-undo-newer", "U-alice", "BTS 47").status_code == 200
    with Session(engine) as session:
        newer_id = session.scalar(
            select(Transaction.id).where(Transaction.description == "BTS")
        )
        assert newer_id is not None

    assert _post_text(client, "evt-undo-older", "U-alice", "เลิกทำ").status_code == 200
    with Session(engine) as session:
        restored = session.get(Transaction, original_id)
        assert restored is not None
        assert restored.id < newer_id
        assert session.scalar(select(func.count(Transaction.id))) == 2


def test_undo_id_collision_consumes_action_and_reports_conflict(webhook_client):
    client, engine, replies = webhook_client
    original_id, _ = _create_undo_snapshot(client, engine)
    with Session(engine) as session:
        session.execute(
            insert(Transaction).values(
                id=original_id,
                line_user_id="U-alice",
                transaction_type="expense",
                amount_satang=9999,
                category="อื่นๆ",
                description="ทับซ้อน",
                occurred_on=date(2026, 9, 23),
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    assert _post_text(client, "evt-undo-collide", "U-alice", "เลิกทำ").status_code == 200
    assert replies[-1][1] == UNDO_ID_COLLISION_REPLY
    with Session(engine) as session:
        assert session.get(PendingAction, "U-alice") is None
        items = list(session.scalars(select(Transaction)))
        assert len(items) == 1
        assert items[0].description == "ทับซ้อน"


def test_cancel_with_active_undo_preserves_undo_and_cancels_draft(webhook_client):
    client, engine, replies = webhook_client
    _create_undo_snapshot(client, engine)
    assert _post_text(client, "evt-undo-cancel-draft", "U-alice", "กาแฟ").status_code == 200
    with Session(engine) as session:
        undo_state = _row_state(session.get(PendingAction, "U-alice"))

    assert _post_text(client, "evt-undo-cancel", "U-alice", "ยกเลิก").status_code == 200
    assert replies[-1][1] == "ยกเลิกรายการที่ค้างไว้แล้วครับ"
    with Session(engine) as session:
        action = session.get(PendingAction, "U-alice")
        assert action is not None
        assert _row_state(action) == undo_state
        assert session.get(PendingTransaction, "U-alice") is None
        assert session.scalar(select(func.count(Transaction.id))) == 0


def test_confirm_with_active_undo_preserves_undo_without_deleting(webhook_client):
    client, engine, replies = webhook_client
    _create_undo_snapshot(client, engine)
    with Session(engine) as session:
        undo_state = _row_state(session.get(PendingAction, "U-alice"))

    assert _post_text(client, "evt-undo-confirm-again", "U-alice", "ยืนยัน").status_code == 200
    assert replies[-1][1].startswith("ยังไม่เข้าใจข้อความนี้")
    with Session(engine) as session:
        action = session.get(PendingAction, "U-alice")
        assert action is not None
        assert _row_state(action) == undo_state
        assert session.scalar(select(func.count(Transaction.id))) == 0


@pytest.mark.parametrize(
    "mutation",
    ["BTS 47", "แก้ ข้าว 60", "กาแฟ"],
)
def test_active_undo_survives_unrelated_financial_work(webhook_client, mutation):
    client, engine, _ = webhook_client
    _create_undo_snapshot(client, engine)
    with Session(engine) as session:
        undo_state = _row_state(session.get(PendingAction, "U-alice"))

    assert _post_text(
        client, "evt-undo-unrelated", "U-alice", mutation
    ).status_code == 200
    with Session(engine) as session:
        action = session.get(PendingAction, "U-alice")
        assert action is not None
        assert _row_state(action) == undo_state


def test_stateless_commands_preserve_active_undo(webhook_client):
    client, engine, _ = webhook_client
    _create_undo_snapshot(client, engine)
    with Session(engine) as session:
        undo_state = _row_state(session.get(PendingAction, "U-alice"))

    for index, command in enumerate(
        ("ช่วยเหลือ", "รายการล่าสุด", "สรุปเดือนนี้", "เป้าหมายการออม")
    ):
        assert _post_text(
            client, f"evt-undo-stateless-{index}", "U-alice", command
        ).status_code == 200
        with Session(engine) as session:
            action = session.get(PendingAction, "U-alice")
            assert action is not None
            assert _row_state(action) == undo_state


def test_delete_latest_without_transactions_preserves_active_undo(webhook_client):
    client, engine, replies = webhook_client
    _create_undo_snapshot(client, engine)
    with Session(engine) as session:
        undo_state = _row_state(session.get(PendingAction, "U-alice"))

    assert _post_text(client, "evt-undo-empty-delete", "U-alice", "ลบล่าสุด").status_code == 200
    assert replies[-1][1] == "ยังไม่มีรายการให้ลบ"
    with Session(engine) as session:
        action = session.get(PendingAction, "U-alice")
        assert action is not None
        assert _row_state(action) == undo_state


def test_delete_latest_replaces_active_undo_with_new_confirm(webhook_client):
    client, engine, _ = webhook_client
    _create_undo_snapshot(client, engine)
    assert _post_text(client, "evt-undo-replace-new", "U-alice", "BTS 47").status_code == 200
    with Session(engine) as session:
        bts_id = session.scalar(
            select(Transaction.id).where(Transaction.description == "BTS")
        )

    assert _post_text(client, "evt-undo-replace", "U-alice", "ลบล่าสุด").status_code == 200
    with Session(engine) as session:
        action = session.get(PendingAction, "U-alice")
        assert action is not None
        assert action.action_type == "confirm_delete"
        assert action.target_transaction_id == bts_id
        assert action.snapshot_transaction_id is None
        assert action.snapshot_created_at is None
        assert session.get(Transaction, bts_id) is not None


def test_delete_latest_confirm_at_active_undo_is_rejected(webhook_client):
    client, engine, replies = webhook_client
    _create_undo_snapshot(client, engine)
    with Session(engine) as session:
        undo_state = _row_state(session.get(PendingAction, "U-alice"))

    assert _post_text(client, "evt-undo-cannot-confirm", "U-alice", "เลิกทำ").status_code == 200
    assert replies[-1][1].startswith("↩️")
    assert _post_text(client, "evt-undo-confirm-after", "U-alice", "ยืนยัน").status_code == 200
    assert replies[-1][1].startswith("ยังไม่เข้าใจข้อความนี้")


def test_active_confirm_delete_rejects_undo_until_confirmed(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-confirm-undo-seed", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-confirm-undo-request", "U-alice", "ลบล่าสุด").status_code == 200

    assert _post_text(client, "evt-confirm-undo-attempt", "U-alice", "เลิกทำ").status_code == 200
    assert replies[-1][1] == DELETE_NOT_CONFIRMED_REPLY
    with Session(engine) as session:
        action = session.get(PendingAction, "U-alice")
        assert action is not None
        assert action.action_type == "confirm_delete"
        assert session.scalar(select(func.count(Transaction.id))) == 1


def test_undo_without_pending_action_reports_unavailable(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-undo-none", "U-alice", "เลิกทำ").status_code == 200
    assert replies[-1][1] == UNDO_UNAVAILABLE_REPLY
    with Session(engine) as session:
        assert session.get(PendingAction, "U-alice") is None
        assert session.scalar(select(func.count(Transaction.id))) == 0


def test_expired_undo_reports_expiry_and_preserves_draft(webhook_client):
    client, engine, replies = webhook_client
    _create_undo_snapshot(client, engine)
    assert _post_text(client, "evt-expired-undo-draft", "U-alice", "กาแฟ").status_code == 200
    _expire_pending_action(engine)

    assert _post_text(client, "evt-expired-undo", "U-alice", "เลิกทำ").status_code == 200
    assert replies[-1][1] == UNDO_EXPIRED_REPLY
    with Session(engine) as session:
        assert session.get(PendingAction, "U-alice") is None
        assert session.get(PendingTransaction, "U-alice") is not None
        assert session.scalar(select(func.count(Transaction.id))) == 0


def test_expired_undo_cancel_falls_through_to_draft_cancellation(webhook_client):
    client, engine, replies = webhook_client
    _create_undo_snapshot(client, engine)
    assert _post_text(client, "evt-expired-undo-cancel-draft", "U-alice", "กาแฟ").status_code == 200
    _expire_pending_action(engine)

    assert _post_text(client, "evt-expired-undo-cancel", "U-alice", "ยกเลิก").status_code == 200
    assert replies[-1][1] != UNDO_EXPIRED_REPLY
    assert replies[-1][1] == "ยกเลิกรายการที่ค้างไว้แล้วครับ"
    with Session(engine) as session:
        assert session.get(PendingAction, "U-alice") is None
        assert session.get(PendingTransaction, "U-alice") is None


def test_expired_undo_confirm_is_not_reinterpreted(webhook_client):
    client, engine, replies = webhook_client
    _create_undo_snapshot(client, engine)
    _expire_pending_action(engine)

    assert _post_text(client, "evt-expired-undo-confirm", "U-alice", "ยืนยัน").status_code == 200
    assert replies[-1][1].startswith("ยังไม่เข้าใจข้อความนี้")
    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 0


def test_expired_confirm_undo_command_reports_unavailable(webhook_client):
    client, engine, replies = webhook_client
    assert _post_text(client, "evt-expired-confirm-seed", "U-alice", "ข้าว 40").status_code == 200
    assert _post_text(client, "evt-expired-confirm-request", "U-alice", "ลบล่าสุด").status_code == 200
    _expire_pending_action(engine)

    assert _post_text(client, "evt-expired-confirm-undo", "U-alice", "เลิกทำ").status_code == 200
    assert replies[-1][1] == UNDO_UNAVAILABLE_REPLY
    with Session(engine) as session:
        assert session.get(PendingAction, "U-alice") is None
        assert session.scalar(select(func.count(Transaction.id))) == 1


def test_pending_completion_preserves_active_undo(webhook_client):
    client, engine, _ = webhook_client
    _create_undo_snapshot(client, engine)
    with Session(engine) as session:
        undo_state = _row_state(session.get(PendingAction, "U-alice"))

    assert _post_text(client, "evt-undo-complete-draft", "U-alice", "กาแฟ").status_code == 200
    assert _post_text(client, "evt-undo-complete-amount", "U-alice", "50").status_code == 200
    with Session(engine) as session:
        action = session.get(PendingAction, "U-alice")
        assert action is not None
        assert _row_state(action) == undo_state
        assert session.get(PendingTransaction, "U-alice") is None
        assert session.scalar(select(func.count(Transaction.id))) == 1


def test_undo_occ_loser_is_cached_and_not_reinterpreted(webhook_client, monkeypatch):
    client, engine, replies = webhook_client
    _create_undo_snapshot(client, engine)
    calls = 0

    def lose_restore(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise app_module.PendingActionConflictError("simulated undo race")

    monkeypatch.setattr(app_module, "restore_deleted_transaction", lose_restore)
    body, headers = _signed_body(
        {"events": [_text_event("evt-undo-occ", "U-alice", "เลิกทำ")]}
    )
    assert client.post("/webhook", content=body, headers=headers).status_code == 200
    retry_reply = "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"
    assert replies[-1][1] == retry_reply
    assert calls == 1

    assert client.post("/webhook", content=body, headers=headers).status_code == 200
    assert calls == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 0
        action = session.get(PendingAction, "U-alice")
        assert action is not None
        assert action.action_type == "undo_delete"
        event = session.get(ProcessedWebhookEvent, "evt-undo-occ")
        assert event.response_text == retry_reply
