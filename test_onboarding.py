"""Phase 1I command boundaries, stateless routing, and follow webhook safety."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import app as app_module
import parser as parser_module
from database import configure_database
from line_api import LINE_TEXT_LIMIT, LineTransportError
from messages import (
    format_help_message,
    format_onboarding_usage,
    format_onboarding_welcome,
)
from models import (
    ExportToken,
    PendingAction,
    PendingTransaction,
    ProcessedWebhookEvent,
    SavingsGoal,
    Transaction,
    UserFeedback,
)
from parser import (
    CommandKind,
    IncompleteCommand,
    InvalidOnboardingCommand,
    SimpleCommand,
    TransactionCommand,
    UnresolvedCommand,
    parse_command,
    parse_followup,
    parse_onboarding,
)
from repository import get_user_data_summary
from test_webhook import (
    SECRET,
    _create_undo_snapshot,
    _post_text,
    _row_state,
    _signed_body,
    _text_event,
)


NOW = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)
USER = "U-alice"
BOB = "U-alice-other"
WELCOME = (
    "🐷 สวัสดีครับ หมูตุ๋นช่วยจดรายรับ รายจ่าย และเงินออมผ่านแชตครับ\n\n"
    "เริ่มได้เลย เช่น\n"
    "• ข้าว 50\n"
    "• เงินเดือน 20000\n"
    "• สรุปเดือนนี้\n\n"
    "ดูคำสั่งเพิ่มเติม: ช่วยเหลือ\n"
    "ดูข้อความเริ่มต้นนี้อีกครั้ง: เริ่มใช้งาน\n"
    "ส่งข้อเสนอแนะ: เสนอแนะ 5 ใช้ง่ายดีครับ\n\n"
    "ข้อมูลของคุณแยกตาม LINE user ID และลบข้อมูลของคุณได้ด้วย `ลบข้อมูลทั้งหมด` ครับ"
)
USAGE = "คำสั่งเริ่มใช้งานไม่ต้องใส่ข้อมูลเพิ่มเติมครับ พิมพ์ 'เริ่มใช้งาน' ได้เลย"
VALID_INPUTS = ["เริ่มใช้งาน", " เริ่มใช้งาน ", "เริ่มใช้งาน    ", "\t\nเริ่มใช้งาน\r\n\t"]
MALFORMED_INPUTS = [
    "เริ่มใช้งาน 100", "เริ่มใช้งาน abc", "เริ่มใช้งาน เพิ่มเติม",
    "เริ่มใช้งาน\t100", "เริ่มใช้งาน\nจ่าย 100 ข้าว",
    " \tเริ่มใช้งาน\u00a0100\n", "เริ่มใช้งาน รับ 100 เงินเดือน",
]
BUSINESS_MODELS = (
    Transaction, SavingsGoal, PendingTransaction, PendingAction, UserFeedback, ExportToken,
)


@pytest.fixture()
def onboarding_client(tmp_path, monkeypatch):
    engine = configure_database(f"sqlite:///{tmp_path / 'onboarding.db'}")
    replies = []

    async def fake_reply(reply_token, text, token):
        replies.append((reply_token, text, token))

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://mootoon.example")
    monkeypatch.setattr(app_module, "LINE_CHANNEL_SECRET", SECRET)
    monkeypatch.setattr(app_module, "LINE_CHANNEL_ACCESS_TOKEN", "test-access-token")
    monkeypatch.setattr(app_module, "reply_text", fake_reply)
    monkeypatch.setattr(app_module, "_utc_now", lambda: NOW)
    with TestClient(app_module.app) as client:
        yield client, engine, replies
    engine.dispose()


def _follow_event(event_id, user=USER):
    return {
        "type": "follow",
        "webhookEventId": event_id,
        "replyToken": f"reply-{event_id}",
        "source": {"type": "user", "userId": user},
        "timestamp": int(NOW.timestamp() * 1000),
    }


def _post_event(client, item):
    body, headers = _signed_body({"events": [item]})
    return client.post("/webhook", content=body, headers=headers)


def _onboarding_event(trigger, event_id="onboarding", user=USER):
    if trigger == "follow":
        return _follow_event(event_id, user)
    return _text_event(event_id, user, trigger)


def _business_state(engine):
    with Session(engine) as session:
        return {
            model.__tablename__: [
                _row_state(row) for row in session.scalars(
                    select(model).order_by(*model.__table__.primary_key.columns)
                )
            ]
            for model in BUSINESS_MODELS
        }


def _seed_pending(client, engine, action_type, *, user=USER, expired=False):
    prefix = f"seed-{user}"
    if action_type == "undo_delete":
        _create_undo_snapshot(client, engine, user=user, prefix=prefix)
    elif action_type == "confirm_delete":
        assert _post_text(client, prefix, user, "ข้าว 50").status_code == 200
        assert _post_text(client, prefix + "-delete", user, "ลบล่าสุด").status_code == 200
    assert _post_text(client, prefix + "-draft", user, "กาแฟ").status_code == 200
    if action_type == "confirm_delete_all":
        assert _post_text(client, prefix + "-delete-all", user, "ลบข้อมูลทั้งหมด").status_code == 200
    with Session(engine) as session:
        draft = session.get(PendingTransaction, user)
        assert draft is not None
        assert draft.expires_at.replace(tzinfo=timezone.utc) > NOW
        action = session.get(PendingAction, user)
        if action_type is not None:
            assert action is not None and action.action_type == action_type
            assert action.expires_at.replace(tzinfo=timezone.utc) > NOW
            if action_type == "undo_delete":
                assert action.snapshot_amount_satang is not None
                assert action.snapshot_created_at is not None
        if expired:
            draft.expires_at = NOW - timedelta(seconds=1)
            if action is not None:
                action.expires_at = NOW - timedelta(seconds=1)
            session.commit()


def _forbidden(*args, **kwargs):
    pytest.fail("Onboarding must bypass financial parsing and business-state reads")


def test_messages_match_exact_approved_copy_and_persona():
    assert format_onboarding_welcome() == format_onboarding_welcome() == WELCOME
    assert format_onboarding_usage() == format_onboarding_usage() == USAGE
    for text in (WELCOME, USAGE):
        assert "ครับ" in text
        assert "ค่ะ" not in text and "นะคะ" not in text
        assert 0 < len(text) < LINE_TEXT_LIMIT
        assert USER not in text and BOB not in text
    assert "ProcessedWebhookEvent" not in WELCOME


@pytest.mark.parametrize("text", VALID_INPUTS)
def test_exact_onboarding_and_surrounding_whitespace_are_typed(text):
    expected = SimpleCommand(CommandKind.ONBOARDING)
    assert parse_onboarding(text) == expected
    assert parse_command(text, now=NOW) == expected


@pytest.mark.parametrize("text", MALFORMED_INPUTS)
def test_malformed_original_keyword_is_never_financial_evidence(text, monkeypatch):
    monkeypatch.setattr(parser_module, "_normalize", _forbidden)
    monkeypatch.setattr(parser_module, "_extract_amount", _forbidden)
    assert parse_onboarding(text) == InvalidOnboardingCommand()
    result = parse_command(text, now=NOW)
    assert result == InvalidOnboardingCommand()
    assert not isinstance(result, (TransactionCommand, IncompleteCommand, UnresolvedCommand))


@pytest.mark.parametrize("text", [
    "เริ่ม", "start", "welcome", "เริ่มใช้งานใหม่", "เริ่มใช้งานใหม่ 100",
    "เริ่มใช้งาน100", "เริ่มใช้งาน: 100", "เริ่มใช้งาน\u200b100",
    "x" * len("เริ่มใช้งาน") + " 100", "", "ข้าว 100",
])
def test_aliases_near_misses_and_unrelated_prefixes_are_not_onboarding(text):
    assert parse_onboarding(text) is None
    result = parse_command(text, now=NOW)
    assert result != SimpleCommand(CommandKind.ONBOARDING)
    assert not isinstance(result, InvalidOnboardingCommand)


@pytest.mark.parametrize("text", VALID_INPUTS + MALFORMED_INPUTS)
def test_followup_defensively_rejects_valid_and_malformed_onboarding(text):
    draft = IncompleteCommand("expense", None, "อาหาร", date(2026, 10, 9), "กาแฟ", "food.coffee")
    assert isinstance(parse_followup(draft, text, now=NOW), UnresolvedCommand)


@pytest.mark.parametrize("text", VALID_INPUTS + MALFORMED_INPUTS)
def test_manual_onboarding_returns_early_and_creates_no_business_data(onboarding_client, monkeypatch, text):
    client, engine, replies = onboarding_client
    before = _business_state(engine)
    assert not any(before.values())
    for name in ("get_pending_action", "get_pending_transaction", "parse_command", "parse_followup"):
        monkeypatch.setattr(app_module, name, _forbidden)
    for index in range(2):
        assert _post_text(client, f"manual-{index}", USER, text).status_code == 200
    expected = WELCOME if text in VALID_INPUTS else USAGE
    assert [reply[1] for reply in replies] == [expected, expected]
    assert _business_state(engine) == before


@pytest.mark.parametrize("text", ["เริ่มใช้งานใหม่", "เริ่มใช้งานใหม่ 100"])
def test_near_miss_uses_normal_routing_not_welcome_or_usage(onboarding_client, text):
    client, engine, replies = onboarding_client
    assert _post_text(client, "near-miss", USER, text).status_code == 200
    assert replies[-1][1] not in (WELCOME, USAGE)
    assert "พิมพ์ “ช่วยเหลือ”" in replies[-1][1]


@pytest.mark.parametrize("trigger", ["follow", "เริ่มใช้งาน", "เริ่มใช้งาน 100", "เริ่มใช้งาน abc", "เริ่มใช้งาน เพิ่มเติม"])
@pytest.mark.parametrize("action_type", [None, "confirm_delete", "undo_delete", "confirm_delete_all"])
@pytest.mark.parametrize("expired", [False, True])
def test_onboarding_preserves_every_pending_column_without_lazy_cleanup(
    onboarding_client, monkeypatch, trigger, action_type, expired,
):
    client, engine, replies = onboarding_client
    _seed_pending(client, engine, action_type, expired=expired)
    before = _business_state(engine)
    for name in ("get_pending_action", "get_pending_transaction", "parse_command", "parse_followup"):
        monkeypatch.setattr(app_module, name, _forbidden)
    if trigger == "follow":
        monkeypatch.setattr(app_module, "handle_text_message", _forbidden)
        monkeypatch.setattr(app_module, "parse_onboarding", _forbidden)
    assert _post_event(client, _onboarding_event(trigger)).status_code == 200
    assert replies[-1][1] == (WELCOME if trigger in ("follow", "เริ่มใช้งาน") else USAGE)
    assert _business_state(engine) == before


@pytest.mark.parametrize("message", ["absent", None, {}, {"type": "text", "text": "จ่าย 100"}])
def test_signed_follow_bypasses_text_handler_parser_and_business_queries(onboarding_client, monkeypatch, message):
    client, engine, replies = onboarding_client
    item = _follow_event("follow")
    if message != "absent":
        item["message"] = message
    for name in ("handle_text_message", "parse_command", "parse_onboarding", "parse_followup"):
        monkeypatch.setattr(app_module, name, _forbidden)
    statements = []

    def record_sql(connection, cursor, statement, params, context, many):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        assert _post_event(client, item).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    assert replies == [("reply-follow", WELCOME, "test-access-token")]
    assert not any(model.__tablename__ in sql for model in BUSINESS_MODELS for sql in statements)
    with Session(engine) as session:
        cached = session.get(ProcessedWebhookEvent, "follow")
        assert cached.response_text == WELCOME and cached.reply_sent is True


def test_bad_signature_follow_performs_no_processing_or_reply(onboarding_client, monkeypatch):
    client, engine, replies = onboarding_client
    monkeypatch.setattr(app_module, "mark_webhook_processed", _forbidden)
    monkeypatch.setattr(app_module, "format_onboarding_welcome", _forbidden)
    body, headers = _signed_body({"events": [_follow_event("invalid")]})
    headers["x-line-signature"] = "invalid"
    assert client.post("/webhook", content=body, headers=headers).status_code == 400
    with Session(engine) as session:
        assert session.get(ProcessedWebhookEvent, "invalid") is None
    assert replies == []


@pytest.mark.parametrize("changes", [
    {"type": "unfollow"}, {"type": "join"}, {"type": "postback"}, {"type": None},
    {"type": "message", "message": {"type": "image"}},
    {"type": "message", "message": None}, {"type": "message"},
    {"source": {"type": "group", "userId": USER}},
    {"source": {"type": "room", "userId": USER}},
    {"source": {"type": "user"}}, {"source": {"type": "user", "userId": 100}},
    {"source": None}, {"webhookEventId": None},
])
def test_unsupported_event_source_or_identity_is_ignored(onboarding_client, monkeypatch, changes):
    client, engine, replies = onboarding_client
    monkeypatch.setattr(app_module, "mark_webhook_processed", _forbidden)
    item = _follow_event("ignored")
    item.update(changes)
    assert _post_event(client, item).status_code == 200
    assert replies == []
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ProcessedWebhookEvent)) == 0


@pytest.mark.parametrize("trigger", ["follow", "เริ่มใช้งาน", "เริ่มใช้งาน 100"])
def test_sent_duplicate_is_skipped_and_distinct_events_are_repeatable(onboarding_client, monkeypatch, trigger):
    client, engine, replies = onboarding_client
    expected = USAGE if trigger == "เริ่มใช้งาน 100" else WELCOME
    calls = []
    name = "format_onboarding_usage" if expected == USAGE else "format_onboarding_welcome"
    original = getattr(app_module, name)

    def count_generation():
        calls.append(True)
        return original()

    monkeypatch.setattr(app_module, name, count_generation)
    for event_id in ("first", "first", "distinct"):
        assert _post_event(client, _onboarding_event(trigger, event_id)).status_code == 200
    assert len(calls) == 2
    assert replies == [
        ("reply-first", expected, "test-access-token"),
        ("reply-distinct", expected, "test-access-token"),
    ]
    with Session(engine) as session:
        assert session.get(ProcessedWebhookEvent, "first").reply_sent is True
        assert session.get(ProcessedWebhookEvent, "first").response_text == expected
        assert session.scalar(select(func.count()).select_from(ProcessedWebhookEvent)) == 2
    assert not any(_business_state(engine).values())


@pytest.mark.parametrize("trigger", ["follow", "เริ่มใช้งาน", "เริ่มใช้งาน 100"])
def test_reply_failure_retries_exact_cached_text_without_regeneration(onboarding_client, monkeypatch, trigger):
    client, engine, replies = onboarding_client
    attempts = []
    expected = USAGE if trigger == "เริ่มใช้งาน 100" else WELCOME

    async def fail_once(reply_token, text, token):
        # The response must be committed before the external reply is attempted.
        with Session(engine) as session:
            cached = session.get(ProcessedWebhookEvent, "retry")
            assert cached is not None and cached.response_text == text
        attempts.append((reply_token, text, token))
        if len(attempts) == 1:
            raise LineTransportError("temporary failure")

    monkeypatch.setattr(app_module, "reply_text", fail_once)
    item = _onboarding_event(trigger, "retry")
    assert _post_event(client, item).status_code == 502
    with Session(engine) as session:
        cached = session.get(ProcessedWebhookEvent, "retry")
        assert cached.response_text == expected and cached.reply_sent is False
        # Prove the resend uses persisted text, not even the current formatter.
        cached.response_text = "คำตอบที่บันทึกไว้ครับ"
        session.commit()
    for name in ("format_onboarding_welcome", "format_onboarding_usage", "handle_text_message", "parse_onboarding"):
        monkeypatch.setattr(app_module, name, _forbidden)
    assert _post_event(client, item).status_code == 200
    assert attempts[-1] == ("reply-retry", "คำตอบที่บันทึกไว้ครับ", "test-access-token")
    with Session(engine) as session:
        assert session.get(ProcessedWebhookEvent, "retry").reply_sent is True
        assert session.scalar(select(func.count()).select_from(ProcessedWebhookEvent)) == 1
    assert _post_event(client, item).status_code == 200
    assert len(attempts) == 2


def test_processed_follow_without_cached_response_keeps_existing_skip_behavior(onboarding_client, monkeypatch):
    client, engine, replies = onboarding_client
    with Session(engine) as session:
        session.add(ProcessedWebhookEvent(webhook_event_id="no-response", reply_sent=False))
        session.commit()
    monkeypatch.setattr(app_module, "format_onboarding_welcome", _forbidden)
    assert _post_event(client, _follow_event("no-response")).status_code == 200
    assert replies == []


@pytest.mark.parametrize("trigger", ["follow", "เริ่มใช้งาน", "เริ่มใช้งาน 100"])
@pytest.mark.parametrize("failure_point", ["cache", "commit"])
def test_database_failure_rolls_back_claim_and_cache_and_preserves_business_state(
    onboarding_client, monkeypatch, trigger, failure_point,
):
    client, engine, replies = onboarding_client
    _seed_pending(client, engine, "undo_delete")
    before = _business_state(engine)
    replies.clear()
    original_commit = Session.commit

    def fail_cache(connection, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith("UPDATE PROCESSED_WEBHOOK_EVENTS"):
            raise OperationalError(statement, params, RuntimeError("cache failure"))

    def fail_commit(session):
        session.flush()
        raise OperationalError("COMMIT", {}, RuntimeError("commit failure"))

    with monkeypatch.context() as patch:
        if failure_point == "cache":
            event.listen(engine, "before_cursor_execute", fail_cache)
        else:
            patch.setattr(Session, "commit", fail_commit)
        try:
            with pytest.raises(OperationalError):
                _post_event(client, _onboarding_event(trigger, "failed"))
        finally:
            if failure_point == "cache":
                event.remove(engine, "before_cursor_execute", fail_cache)
    assert Session.commit is original_commit
    assert replies == []
    with Session(engine) as session:
        assert session.get(ProcessedWebhookEvent, "failed") is None
    assert _business_state(engine) == before
    assert _post_event(client, _onboarding_event(trigger, "failed")).status_code == 200
    assert len(replies) == 1


@pytest.mark.parametrize("trigger", ["follow", "เริ่มใช้งาน"])
def test_postgresql_returning_claim_deduplicates_onboarding(onboarding_client, monkeypatch, trigger):
    client, engine, replies = onboarding_client
    original_execute = Session.execute
    statements = []

    class UnknownRowcount:
        rowcount = -1

        def __init__(self, result):
            self.result = result

        def scalar_one_or_none(self):
            return self.result.scalar_one_or_none()

    def hide_rowcount(session, statement, *args, **kwargs):
        result = original_execute(session, statement, *args, **kwargs)
        if getattr(statement, "is_insert", False) and statement.table.name == "processed_webhook_events":
            statements.append(str(statement))
            return UnknownRowcount(result)
        return result

    monkeypatch.setattr(engine.dialect, "name", "postgresql")
    monkeypatch.setattr(Session, "execute", hide_rowcount)
    for event_id in ("postgres", "postgres", "distinct"):
        assert _post_event(client, _onboarding_event(trigger, event_id)).status_code == 200
    assert len(replies) == 2
    assert len(statements) == 2 and all("RETURNING" in sql for sql in statements)
    with Session(engine) as session:
        assert session.get(ProcessedWebhookEvent, "postgres").reply_sent is True


@pytest.mark.parametrize("scenario", ["same_follow", "distinct_follow", "distinct_manual", "follow_and_text"])
def test_concurrent_events_use_only_existing_event_claims(onboarding_client, monkeypatch, scenario):
    _, engine, replies = onboarding_client
    barrier = Barrier(2)
    original_claim = app_module.mark_webhook_processed
    calls = []
    original_welcome = app_module.format_onboarding_welcome

    def claim_together(event_id, **kwargs):
        # Both handlers have checked absence before either attempts the unique claim.
        barrier.wait(timeout=10)
        return original_claim(event_id, **kwargs)

    def count_welcome():
        calls.append(True)
        return original_welcome()

    monkeypatch.setattr(app_module, "mark_webhook_processed", claim_together)
    monkeypatch.setattr(app_module, "format_onboarding_welcome", count_welcome)
    if scenario == "same_follow":
        items = [_follow_event("same"), _follow_event("same")]
    elif scenario == "distinct_follow":
        items = [_follow_event("first"), _follow_event("second")]
    elif scenario == "distinct_manual":
        items = [_text_event(key, USER, "เริ่มใช้งาน") for key in ("first", "second")]
    else:
        items = [_follow_event("follow"), _text_event("transaction", USER, "ข้าว 50")]

    def submit(item):
        # Independent portals let both webhook requests overlap, without re-running startup.
        client = TestClient(app_module.app)
        try:
            return _post_event(client, item).status_code
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(submit, items)) == [200, 200]
    expected_welcomes = 1 if scenario in ("same_follow", "follow_and_text") else 2
    assert len(calls) == expected_welcomes
    assert len([reply for reply in replies if reply[1] == WELCOME]) == expected_welcomes
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ProcessedWebhookEvent)) == (1 if scenario == "same_follow" else 2)
        assert session.scalar(select(func.count()).select_from(Transaction)) == int(scenario == "follow_and_text")
        assert session.get(PendingTransaction, USER) is None
        assert session.get(PendingAction, USER) is None


@pytest.mark.parametrize("follow_first", [False, True])
def test_first_transaction_is_never_replaced_or_decorated_by_welcome(onboarding_client, follow_first):
    client, engine, replies = onboarding_client
    if follow_first:
        assert _post_event(client, _follow_event("follow")).status_code == 200
    assert _post_text(client, "transaction", USER, "ข้าว 50").status_code == 200
    transaction_reply = replies[-1][1]
    assert transaction_reply.startswith("✅ บันทึกรายจ่ายแล้ว")
    assert WELCOME not in transaction_reply and "สวัสดีครับ" not in transaction_reply
    if not follow_first:
        assert len(replies) == 1
        assert _post_event(client, _follow_event("follow")).status_code == 200
    with Session(engine) as session:
        row = session.scalar(select(Transaction))
        assert row.line_user_id == USER and row.amount_satang == 5000
        assert session.scalar(select(func.count()).select_from(Transaction)) == 1


def test_draft_continues_normally_after_valid_malformed_and_follow(onboarding_client):
    client, engine, replies = onboarding_client
    assert _post_text(client, "draft", USER, "กาแฟ").status_code == 200
    before = _business_state(engine)
    for index, trigger in enumerate(("follow", "เริ่มใช้งาน", "เริ่มใช้งาน 100")):
        assert _post_event(client, _onboarding_event(trigger, f"onboarding-{index}")).status_code == 200
        assert _business_state(engine) == before
    assert _post_text(client, "complete", USER, "35").status_code == 200
    with Session(engine) as session:
        assert session.get(PendingTransaction, USER) is None
        row = session.scalar(select(Transaction))
        assert row.description == "กาแฟ" and row.amount_satang == 3500


@pytest.mark.parametrize("trigger", ["follow", "เริ่มใช้งาน"])
def test_welcome_only_user_has_no_delete_all_or_export_data(onboarding_client, trigger):
    client, engine, replies = onboarding_client
    assert _post_event(client, _onboarding_event(trigger)).status_code == 200
    with Session(engine) as session:
        assert not get_user_data_summary(USER, now=NOW, session=session).has_any_data
    assert _post_text(client, "delete-all", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    assert replies[-1][1] == "ไม่มีข้อมูลให้ลบครับ"
    assert _post_text(client, "export", USER, "ส่งออกข้อมูล").status_code == 200
    assert replies[-1][1] == "ไม่มีข้อมูลสำหรับส่งออกครับ"
    assert not any(_business_state(engine).values())


def test_feedback_csv_and_confirmed_delete_all_survive_onboarding(onboarding_client):
    client, engine, replies = onboarding_client
    assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
    assert _post_text(client, "feedback", USER, "เสนอแนะ 4 ดี   มาก").status_code == 200
    assert replies[-1][1].startswith("บันทึกข้อเสนอแนะระดับ 4 ดาว")
    assert _post_text(client, "export", USER, "ส่งออกข้อมูล").status_code == 200
    url = replies[-1][1].splitlines()[-1]
    csv_before = client.get(url).content
    assert "ดี   มาก" not in csv_before.decode("utf-8-sig")
    before = _business_state(engine)
    for index, trigger in enumerate(("follow", "เริ่มใช้งาน", "เริ่มใช้งาน 100")):
        assert _post_event(client, _onboarding_event(trigger, f"guide-{index}")).status_code == 200
        assert _business_state(engine) == before
        assert client.get(url).content == csv_before
    assert _post_text(client, "delete-all", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    before_confirmation = _business_state(engine)
    assert _post_text(client, "replay", USER, "เริ่มใช้งาน").status_code == 200
    assert _business_state(engine) == before_confirmation
    assert _post_text(client, "confirm", USER, "ยืนยัน").status_code == 200
    assert replies[-1][1].startswith("🗑️ ลบข้อมูลทั้งหมดเรียบร้อยแล้ว")
    assert replies[-1][1] != WELCOME
    assert not any(_business_state(engine).values())
    assert client.get(url).status_code == 404
    with Session(engine) as session:
        assert session.get(ProcessedWebhookEvent, "onboarding") is None
        assert session.get(ProcessedWebhookEvent, "guide-0") is not None
    assert _post_text(client, "after-wipe", USER, "เริ่มใช้งาน").status_code == 200
    assert replies[-1][1] == WELCOME
    assert not any(_business_state(engine).values())


@pytest.mark.parametrize("trigger", ["follow", "เริ่มใช้งาน", "เริ่มใช้งาน 100"])
def test_user_a_onboarding_leaves_all_user_b_data_unchanged(onboarding_client, trigger):
    client, engine, replies = onboarding_client
    assert _post_text(client, "bob-feedback", BOB, "เสนอแนะ 5 private").status_code == 200
    assert _post_text(client, "bob-seed", BOB, "ข้าว 50").status_code == 200
    assert _post_text(client, "bob-export", BOB, "ส่งออกข้อมูล").status_code == 200
    _seed_pending(client, engine, "confirm_delete_all", user=BOB)
    before = _business_state(engine)
    assert all(before[model.__tablename__] for model in (Transaction, UserFeedback, PendingTransaction, PendingAction))
    assert _post_event(client, _onboarding_event(trigger, "alice-guide", USER)).status_code == 200
    assert replies[-1] == (
        "reply-alice-guide", USAGE if trigger == "เริ่มใช้งาน 100" else WELCOME, "test-access-token",
    )
    assert _business_state(engine) == before


def test_help_exposes_only_minimal_onboarding_and_feedback_entries(onboarding_client):
    client, engine, replies = onboarding_client
    assert _post_text(client, "help", USER, "ช่วยเหลือ").status_code == 200
    assert replies[-1][1] == format_help_message()
    assert "ดูข้อความเริ่มต้น: เริ่มใช้งาน" in replies[-1][1]
    assert "ส่งข้อเสนอแนะ: เสนอแนะ 5 ใช้ง่ายดีครับ" in replies[-1][1]
    assert len(replies[-1][1]) < LINE_TEXT_LIMIT
