import base64
import hashlib
import hmac
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import app as app_module
from database import configure_database
from line_api import LineTransportError
from models import ProcessedWebhookEvent, Transaction


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
    assert "กรุณาระบุว่าเป็นรายรับหรือรายจ่าย" in replies[0][1]


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
