"""Phase 1H parser safety, privacy, action OCC, and webhook idempotency."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, create_mock_engine, event, func, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app as app_module
from database import configure_database, init_db
from line_api import LineTransportError
from messages import (
    format_feedback_comment_too_long,
    format_feedback_instruction,
    format_feedback_invalid_rating,
    format_feedback_success,
)
from models import (
    Base, ExportToken, PendingAction, PendingTransaction, ProcessedWebhookEvent,
    SavingsGoal, Transaction, UserFeedback,
)
from parser import (
    CommandKind, FeedbackCommand, FeedbackError, IncompleteCommand,
    InvalidFeedbackCommand, UnresolvedCommand, parse_command, parse_followup,
)
from repository import (
    PendingActionConflictError, add_transaction, add_user_feedback,
    create_pending_action, delete_pending_action, execute_delete_all,
    get_user_data_summary,
)
from test_webhook import SECRET, _create_undo_snapshot, _post_text, _row_state, _signed_body, _text_event


NOW = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)
USER = "U-alice"
BOB = "U-alice-other"
INSTRUCTION = (
    "หากต้องการส่งข้อเสนอแนะ กรุณาพิมพ์ 'เสนอแนะ [คะแนน 1-5]' "
    "หรือ 'เสนอแนะ [คะแนน] [ข้อความ]' เช่น:\n- เสนอแนะ 5\n- เสนอแนะ 4 ใช้ง่ายดีครับ"
)
INVALID_RATING = "คะแนนข้อเสนอแนะต้องเป็นตัวเลข 1 ถึง 5 เท่านั้นครับ เช่น 'เสนอแนะ 5'"
TOO_LONG = "ข้อความเสนอแนะยาวเกินไป (สูงสุด 1000 ตัวอักษร) กรุณาสรุปข้อความแล้วส่งใหม่อีกครั้งครับ"


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'feedback-unit.db'}")
    init_db(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.rollback()
    engine.dispose()


@pytest.fixture()
def feedback_client(tmp_path, monkeypatch):
    engine = configure_database(f"sqlite:///{tmp_path / 'feedback-webhook.db'}")
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


def _success(rating):
    return f"บันทึกข้อเสนอแนะระดับ {rating} ดาวเรียบร้อย ขอบคุณที่ช่วยพัฒนาหมูตุ๋นครับ 🐷"


def _count(session, model=UserFeedback, user=USER):
    return session.scalar(select(func.count()).select_from(model).where(model.line_user_id == user))


def _start_delete_all(session):
    assert create_pending_action(
        USER, action_type="confirm_delete_all", target_transaction_id=0,
        now=NOW, session=session,
    )
    action = session.get(PendingAction, USER)
    return action.action_id, action.version


def _seed_active_state(client, engine, action_type):
    if action_type == "undo_delete":
        _create_undo_snapshot(client, engine)
    elif action_type == "confirm_delete":
        assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
        assert _post_text(client, "delete", USER, "ลบล่าสุด").status_code == 200
    assert _post_text(client, "draft", USER, "กาแฟ").status_code == 200
    if action_type == "confirm_delete_all":
        assert _post_text(client, "delete-all", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    with Session(engine) as session:
        draft = session.get(PendingTransaction, USER)
        assert draft.expires_at.replace(tzinfo=timezone.utc) > NOW
        action = session.get(PendingAction, USER)
        if action_type is not None:
            assert action.action_type == action_type
            assert action.expires_at.replace(tzinfo=timezone.utc) > NOW
        return _row_state(draft), _row_state(action) if action is not None else None


@pytest.mark.parametrize(
    ("text", "rating", "comment"),
    [
        ("เสนอแนะ 1", 1, None),
        ("เสนอแนะ 5", 5, None),
        ("เสนอแนะ 4 ใช้ง่ายดี", 4, "ใช้ง่ายดี"),
        ("เสนอแนะ 4 ดี   มาก", 4, "ดี   มาก"),
        (" \tเสนอแนะ 4   Thai 🐷, !?  GOOD\n \t", 4, "Thai 🐷, !?  GOOD"),
        ("เสนอแนะ\t3\nดี\tมาก\nลองต่อ", 3, "ดี\tมาก\nลองต่อ"),
        ("เสนอแนะ 2 \t\n", 2, None),
        ("เสนอแนะ 5 จ่าย 50 อาหาร", 5, "จ่าย 50 อาหาร"),
        ("เสนอแนะ 5 " + "🐷" * 1000, 5, "🐷" * 1000),
        ("เสนอแนะ 4 \t" + "ก" * 1000 + "\n", 4, "ก" * 1000),
    ],
)
def test_parser_preserves_original_comments_and_rating_boundaries(text, rating, comment):
    assert parse_command(text, now=NOW) == FeedbackCommand(CommandKind.FEEDBACK, rating, comment)


INVALID_INPUTS = [
    "เสนอแนะ 0", "เสนอแนะ 6", "เสนอแนะ -1", "เสนอแนะ 5.5", "เสนอแนะ 05",
    "เสนอแนะ ห้า", "เสนอแนะ ๕", "เสนอแนะ ５", "เสนอแนะ +5", "เสนอแนะ 5ดี",
    "เสนอแนะ5", "เสนอแนะ: 5", "เสนอแนะข้าว 50", "เสนอแนะ 5.5 ข้าว 50",
]


@pytest.mark.parametrize("text", INVALID_INPUTS)
def test_parser_invalid_feedback_never_becomes_financial_evidence(text):
    result = parse_command(text, now=NOW)
    assert result == InvalidFeedbackCommand(FeedbackError.INVALID_RATING)
    assert not isinstance(result, (IncompleteCommand, UnresolvedCommand))


def test_parser_bare_command_and_over_limit():
    assert parse_command(" \nเสนอแนะ\t ") == InvalidFeedbackCommand(FeedbackError.INSTRUCTION)
    assert parse_command("เสนอแนะ 4 " + "ก" * 1001) == InvalidFeedbackCommand(FeedbackError.COMMENT_TOO_LONG)


@pytest.mark.parametrize("text", ["feedback 5", "rating 5", "ให้คะแนน 5", "ความคิดเห็น 4", "แนะนำ 5"])
def test_feedback_aliases_are_not_supported(text):
    assert not isinstance(parse_command(text), (FeedbackCommand, InvalidFeedbackCommand))


@pytest.mark.parametrize("text", ["เสนอแนะ 5", "เสนอแนะ5", "เสนอแนะ"])
def test_parse_followup_defensively_rejects_feedback(text):
    draft = IncompleteCommand("expense", None, "อาหาร", date(2026, 10, 9), "ข้าว", "food.rice")
    assert isinstance(parse_followup(draft, text, now=NOW), UnresolvedCommand)


def test_messages_exactly_match_approved_persona():
    for rating in (1, 5):
        assert format_feedback_success(rating) == _success(rating)
    assert format_feedback_instruction() == INSTRUCTION
    assert format_feedback_invalid_rating() == INVALID_RATING
    assert format_feedback_comment_too_long() == TOO_LONG


@pytest.mark.parametrize("existing", [False, True])
def test_create_all_adds_only_new_feedback_table_and_repeated_init_is_safe(tmp_path, existing):
    engine = create_engine(f"sqlite:///{tmp_path / 'feedback-schema.db'}")
    if existing:
        Base.metadata.create_all(
            engine, tables=[table for table in Base.metadata.sorted_tables if table.name != "user_feedback"]
        )
        with Session(engine) as session:
            add_transaction(USER, "expense", "50", "อาหาร", session=session)
            session.commit()
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, p, ctx, many: statements.append(sql))
    init_db(engine)
    with Session(engine) as session:
        add_user_feedback(USER, 4, comment="ดี", now=NOW, session=session)
        session.commit()
    init_db(engine)
    columns = {column["name"]: column for column in inspect(engine).get_columns("user_feedback")}
    assert set(columns) == {"id", "line_user_id", "rating", "comment", "created_at"}
    assert columns["line_user_id"]["type"].length == 128
    assert columns["comment"]["type"].length == 1000
    assert columns["comment"]["nullable"] is True
    assert all(not columns[name]["nullable"] for name in ("id", "line_user_id", "rating", "created_at"))
    assert UserFeedback.__table__.c.created_at.type.timezone is True
    assert any(i["column_names"] == ["line_user_id"] for i in inspect(engine).get_indexes("user_feedback"))
    assert sum("CREATE TABLE user_feedback" in sql for sql in statements) == 1
    assert not any("ALTER TABLE" in sql.upper() for sql in statements)
    with Session(engine) as session:
        assert _count(session) == 1
        assert _count(session, Transaction) == int(existing)
    engine.dispose()


def test_postgresql_feedback_schema_compiles_without_migration():
    statements = []
    engine = create_mock_engine(
        "postgresql+psycopg://",
        lambda sql, *args, **kwargs: statements.append(str(sql.compile(dialect=engine.dialect))),
    )
    Base.metadata.create_all(engine)
    ddl = next(sql for sql in statements if "CREATE TABLE user_feedback" in sql)
    assert "id SERIAL NOT NULL" in ddl
    assert "line_user_id VARCHAR(128) NOT NULL" in ddl
    assert "rating INTEGER NOT NULL" in ddl
    assert "comment VARCHAR(1000)" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE NOT NULL" in ddl
    assert "CHECK (rating >= 1 AND rating <= 5)" in ddl
    assert any("CREATE INDEX ix_user_feedback_line_user_id" in sql for sql in statements)
    assert not any("ALTER TABLE" in sql for sql in statements)


@pytest.mark.parametrize("rating", [0, 6, -1])
def test_database_check_rejects_invalid_rating(db_session, rating):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(UserFeedback(line_user_id=USER, rating=rating, created_at=NOW))
            db_session.flush()
    assert _count(db_session) == 0


@pytest.mark.parametrize("moment", [NOW, NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=7)))])
def test_repository_persists_comment_and_utc_timestamp(db_session, moment):
    feedback = add_user_feedback(USER, 4, comment="  ดี   มาก\t🐷\nYES!  ", now=moment, session=db_session)
    assert feedback.created_at == NOW
    assert feedback.created_at.tzinfo == timezone.utc
    db_session.refresh(feedback)
    assert feedback.line_user_id == USER
    assert feedback.rating == 4
    assert feedback.comment == "ดี   มาก\t🐷\nYES!"
    assert feedback.created_at.replace(tzinfo=timezone.utc) == NOW
    assert _count(db_session, user=BOB) == 0


@pytest.mark.parametrize("rating", [0, 6, -1, 5.5, "5", True])
def test_repository_validates_before_action_mutation(db_session, rating):
    action_id, version = _start_delete_all(db_session)
    before = _row_state(db_session.get(PendingAction, USER))
    with pytest.raises(ValueError):
        add_user_feedback(
            USER, rating, expected_action_id=action_id, expected_version=version,
            now=NOW, session=db_session,
        )
    db_session.expire_all()
    assert _row_state(db_session.get(PendingAction, USER)) == before
    assert _count(db_session) == 0


def test_repository_comment_limit_and_expected_guard_pair(db_session):
    action_id, version = _start_delete_all(db_session)
    with pytest.raises(ValueError, match="1000"):
        add_user_feedback(USER, 5, comment="ก" * 1001, expected_action_id=action_id, expected_version=version, now=NOW, session=db_session)
    with pytest.raises(ValueError, match="together"):
        add_user_feedback(USER, 5, expected_action_id=action_id, session=db_session)
    with pytest.raises(ValueError, match="together"):
        add_user_feedback(USER, 5, expected_version=version, session=db_session)
    assert _count(db_session) == 0
    assert db_session.get(PendingAction, USER).action_id == action_id
    feedback = add_user_feedback(USER, 5, comment=" " + "ก" * 1000 + " ", session=db_session)
    assert feedback.comment == "ก" * 1000


@pytest.mark.parametrize("text", INVALID_INPUTS + ["เสนอแนะ", "เสนอแนะ 4 " + "ก" * 1001])
def test_recognized_invalid_feedback_creates_no_financial_or_feedback_rows(feedback_client, text):
    client, engine, replies = feedback_client
    assert _post_text(client, "invalid", USER, text).status_code == 200
    expected = INSTRUCTION if text == "เสนอแนะ" else TOO_LONG if len(text) > 1000 else INVALID_RATING
    assert replies[-1][1] == expected
    with Session(engine) as session:
        assert _count(session) == 0
        assert _count(session, Transaction) == 0
        assert session.get(PendingTransaction, USER) is None


@pytest.mark.parametrize("action_type", [None, "confirm_delete", "undo_delete", "confirm_delete_all"])
@pytest.mark.parametrize("text", ["เสนอแนะ", "เสนอแนะ 5.5", "เสนอแนะข้าว 50", "เสนอแนะ 4 " + "ก" * 1001])
def test_invalid_feedback_preserves_every_active_pending_column(feedback_client, monkeypatch, action_type, text):
    client, engine, replies = feedback_client
    draft_before, action_before = _seed_active_state(client, engine, action_type)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid feedback must never invoke mutation or follow-up")

    monkeypatch.setattr(app_module, "add_user_feedback", forbidden)
    monkeypatch.setattr(app_module, "parse_followup", forbidden)
    with Session(engine) as session:
        transaction_count = _count(session, Transaction)
    assert _post_text(client, "invalid-feedback", USER, text).status_code == 200
    with Session(engine) as session:
        assert _row_state(session.get(PendingTransaction, USER)) == draft_before
        action = session.get(PendingAction, USER)
        assert (_row_state(action) if action is not None else None) == action_before
        assert _count(session) == 0
        assert _count(session, Transaction) == transaction_count


@pytest.mark.parametrize("action_type", [None, "confirm_delete", "undo_delete"])
def test_valid_feedback_preserves_draft_other_actions_and_allows_completion(feedback_client, action_type):
    client, engine, replies = feedback_client
    draft_before, action_before = _seed_active_state(client, engine, action_type)
    assert _post_text(client, "feedback", USER, "เสนอแนะ 4 ดี   มาก 🐷").status_code == 200
    assert replies[-1][1] == _success(4)
    with Session(engine) as session:
        assert _row_state(session.get(PendingTransaction, USER)) == draft_before
        action = session.get(PendingAction, USER)
        assert (_row_state(action) if action is not None else None) == action_before
        feedback = session.scalar(select(UserFeedback))
        assert feedback.comment == "ดี   มาก 🐷"
        assert feedback.created_at.replace(tzinfo=timezone.utc) == NOW
        before_transactions = _count(session, Transaction)
    assert _post_text(client, "finish-draft", USER, "35").status_code == 200
    with Session(engine) as session:
        assert session.get(PendingTransaction, USER) is None
        assert _count(session, Transaction) == before_transactions + 1


def test_valid_feedback_invalidates_only_delete_all_and_preserves_draft(feedback_client):
    client, engine, replies = feedback_client
    draft_before, _ = _seed_active_state(client, engine, "confirm_delete_all")
    assert _post_text(client, "feedback", USER, "เสนอแนะ 5").status_code == 200
    assert replies[-1][1] == _success(5)
    with Session(engine) as session:
        assert session.get(PendingAction, USER) is None
        assert _row_state(session.get(PendingTransaction, USER)) == draft_before
        assert _count(session) == 1
        assert session.scalar(select(UserFeedback.comment)) is None


@pytest.mark.parametrize("action_type", [None, "confirm_delete", "undo_delete", "confirm_delete_all"])
def test_expired_actions_and_absence_follow_existing_mutation_conventions(feedback_client, action_type):
    client, engine, replies = feedback_client
    draft_before, _ = _seed_active_state(client, engine, action_type)
    if action_type is not None:
        with Session(engine) as session:
            session.get(PendingAction, USER).expires_at = NOW
            session.commit()
    assert _post_text(client, "feedback", USER, "เสนอแนะ 5").status_code == 200
    assert replies[-1][1] == _success(5)
    with Session(engine) as session:
        assert session.get(PendingAction, USER) is None
        assert _row_state(session.get(PendingTransaction, USER)) == draft_before
        assert _count(session) == 1


def test_feedback_only_delete_all_gate_cancel_and_exact_user_deletion(feedback_client):
    client, engine, replies = feedback_client
    assert _post_text(client, "feedback-a", USER, "เสนอแนะ 5 A").status_code == 200
    assert _post_text(client, "feedback-b", BOB, "เสนอแนะ 4 B").status_code == 200
    with Session(engine) as session:
        summary = get_user_data_summary(USER, now=NOW, session=session)
        assert summary.has_feedback and summary.has_any_data
        assert summary.transaction_count == 0
        assert not any((summary.has_savings_goal, summary.has_pending_transaction, summary.has_pending_action, summary.has_live_export_token))
        assert not get_user_data_summary("U-empty", now=NOW, session=session).has_any_data
        bob_before = _row_state(session.scalar(select(UserFeedback).where(UserFeedback.line_user_id == BOB)))
        own_before = _row_state(session.scalar(select(UserFeedback).where(UserFeedback.line_user_id == USER)))
    assert _post_text(client, "trigger", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    assert "รายการ 0 รายการ" in replies[-1][1]
    assert _post_text(client, "cancel", USER, "ยกเลิก").status_code == 200
    with Session(engine) as session:
        assert _row_state(session.scalar(select(UserFeedback).where(UserFeedback.line_user_id == USER))) == own_before
    assert _post_text(client, "trigger-again", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    assert _post_text(client, "confirm", USER, "ยืนยัน").status_code == 200
    with Session(engine) as session:
        assert _count(session) == 0
        assert _row_state(session.scalar(select(UserFeedback).where(UserFeedback.line_user_id == BOB))) == bob_before
        assert session.get(PendingAction, USER) is None
        assert session.get(ProcessedWebhookEvent, "feedback-a") is not None
    assert _post_text(client, "empty-delete", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    assert replies[-1][1] == "ไม่มีข้อมูลให้ลบครับ"


@pytest.mark.parametrize("mismatch", ["user", "identity", "version", "type", "expiry"])
def test_feedback_occ_checks_every_guard_and_loser_inserts_nothing(db_session, mismatch):
    action_id, version = _start_delete_all(db_session)
    user = USER
    if mismatch == "user":
        user = BOB
    elif mismatch == "identity":
        action_id = "f" * 32
    elif mismatch == "version":
        version += 1
    elif mismatch == "type":
        db_session.get(PendingAction, USER).action_type = "confirm_delete"
    else:
        db_session.get(PendingAction, USER).expires_at = NOW
    db_session.flush()
    before = _row_state(db_session.get(PendingAction, USER))
    with pytest.raises(PendingActionConflictError):
        add_user_feedback(user, 4, expected_action_id=action_id, expected_version=version, now=NOW, session=db_session)
    db_session.expire_all()
    assert _row_state(db_session.get(PendingAction, USER)) == before
    assert _count(db_session) == _count(db_session, user=BOB) == 0


def test_two_feedback_observations_same_confirmation_one_wins(db_session):
    engine = db_session.get_bind()
    action_id, version = _start_delete_all(db_session)
    db_session.commit()
    # Two readers observe the same immutable action identity before either mutates.
    with Session(engine) as first, Session(engine) as second:
        a = first.get(PendingAction, USER)
        b = second.get(PendingAction, USER)
        observation_a = (a.action_id, a.version)
        observation_b = (b.action_id, b.version)
    assert observation_a == observation_b == (action_id, version)
    with Session(engine) as first, first.begin():
        add_user_feedback(USER, 5, expected_action_id=observation_a[0], expected_version=observation_a[1], now=NOW, session=first)
    with pytest.raises(PendingActionConflictError):
        with Session(engine) as second, second.begin():
            add_user_feedback(USER, 4, expected_action_id=observation_b[0], expected_version=observation_b[1], now=NOW, session=second)
    with Session(engine) as check:
        assert check.get(PendingAction, USER) is None
        assert list(check.scalars(select(UserFeedback.rating))) == [5]


@pytest.mark.parametrize("with_confirmation", [False, True])
def test_concurrent_feedback_is_append_only_or_one_occ_winner(db_session, with_confirmation):
    engine = db_session.get_bind()
    if with_confirmation:
        _start_delete_all(db_session)
    db_session.commit()
    barrier = Barrier(2)

    def submit(rating):
        with Session(engine) as read:
            observed = read.get(PendingAction, USER)
            action_id = observed.action_id if observed is not None else None
            version = observed.version if observed is not None else None
        barrier.wait(timeout=10)
        try:
            with Session(engine) as write, write.begin():
                add_user_feedback(USER, rating, expected_action_id=action_id, expected_version=version, now=NOW, session=write)
            return "inserted"
        except PendingActionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, (4, 5)))
    assert sorted(outcomes) == (["conflict", "inserted"] if with_confirmation else ["inserted", "inserted"])
    with Session(engine) as check:
        assert _count(check) == (1 if with_confirmation else 2)
        assert check.get(PendingAction, USER) is None


@pytest.mark.parametrize("winner", ["cancel", "delete_all"])
def test_committed_control_wins_stale_feedback_race(db_session, winner):
    engine = db_session.get_bind()
    add_user_feedback(USER, 1, comment="old", now=NOW, session=db_session)
    action_id, version = _start_delete_all(db_session)
    db_session.commit()
    with Session(engine) as control, control.begin():
        if winner == "cancel":
            assert delete_pending_action(USER, version, expected_action_id=action_id, now=NOW, session=control)
        else:
            execute_delete_all(USER, version, expected_action_id=action_id, now=NOW, session=control)
    with pytest.raises(PendingActionConflictError):
        with Session(engine) as stale, stale.begin():
            add_user_feedback(USER, 5, expected_action_id=action_id, expected_version=version, now=NOW, session=stale)
    with Session(engine) as check:
        assert _count(check) == int(winner == "cancel")
        assert check.get(PendingAction, USER) is None
    # A later fresh event that observes no confirmation may append new feedback.
    with Session(engine) as later, later.begin():
        add_user_feedback(USER, 4, comment="new", now=NOW, session=later)
    with Session(engine) as check:
        assert _count(check) == 1 + int(winner == "cancel")


@pytest.mark.parametrize("control", ["cancel", "delete_all"])
def test_feedback_wins_then_stale_control_loses(db_session, control):
    engine = db_session.get_bind()
    action_id, version = _start_delete_all(db_session)
    db_session.commit()
    with Session(engine) as feedback, feedback.begin():
        add_user_feedback(USER, 5, expected_action_id=action_id, expected_version=version, now=NOW, session=feedback)
    with Session(engine) as stale:
        if control == "cancel":
            assert not delete_pending_action(USER, version, expected_action_id=action_id, now=NOW, session=stale)
        else:
            with pytest.raises(PendingActionConflictError):
                execute_delete_all(USER, version, expected_action_id=action_id, now=NOW, session=stale)
        stale.commit()
    with Session(engine) as check:
        assert _count(check) == 1
        assert check.get(PendingAction, USER) is None


def test_feedback_invalidation_and_insert_roll_back_together(db_session):
    engine = db_session.get_bind()
    action_id, version = _start_delete_all(db_session)
    db_session.commit()
    with pytest.raises(RuntimeError, match="outer failure"):
        with Session(engine) as write, write.begin():
            add_user_feedback(USER, 5, expected_action_id=action_id, expected_version=version, now=NOW, session=write)
            raise RuntimeError("outer failure")
    with Session(engine) as check:
        assert check.get(PendingAction, USER).action_id == action_id
        assert _count(check) == 0


def test_feedback_insert_failure_rolls_back_confirmation_and_webhook_claim(feedback_client):
    client, engine, replies = feedback_client
    _seed_active_state(client, engine, "confirm_delete_all")
    with Session(engine) as session:
        before_action = _row_state(session.get(PendingAction, USER))
        before_draft = _row_state(session.get(PendingTransaction, USER))

    def fail_insert(mapper, connection, target):
        raise RuntimeError("feedback insert failure")

    event.listen(UserFeedback, "before_insert", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="feedback insert failure"):
            _post_text(client, "failed-feedback", USER, "เสนอแนะ 5")
    finally:
        event.remove(UserFeedback, "before_insert", fail_insert)
    with Session(engine) as session:
        assert _row_state(session.get(PendingAction, USER)) == before_action
        assert _row_state(session.get(PendingTransaction, USER)) == before_draft
        assert _count(session) == 0
        assert session.get(ProcessedWebhookEvent, "failed-feedback") is None


def test_delete_all_feedback_and_export_deletion_roll_back_together(feedback_client):
    client, engine, replies = feedback_client
    assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
    assert _post_text(client, "feedback", USER, "เสนอแนะ 5").status_code == 200
    assert _post_text(client, "export", USER, "ส่งออกข้อมูล").status_code == 200
    url = replies[-1][1].splitlines()[-1]
    assert _post_text(client, "trigger", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    with pytest.raises(RuntimeError, match="outer failure"):
        with Session(engine) as session, session.begin():
            action = session.get(PendingAction, USER)
            execute_delete_all(USER, action.version, expected_action_id=action.action_id, now=NOW, session=session)
            raise RuntimeError("outer failure")
    with Session(engine) as session:
        assert _count(session) == _count(session, ExportToken) == _count(session, Transaction) == 1
        assert session.get(PendingAction, USER).action_type == "confirm_delete_all"
    assert client.get(url).status_code == 200


@pytest.mark.parametrize("race", ["cancel", "delete_all", "replacement"])
def test_feedback_app_occ_loser_is_cached_and_never_reinterpreted(feedback_client, monkeypatch, race):
    client, engine, replies = feedback_client
    _seed_active_state(client, engine, "confirm_delete_all")
    original = app_module.add_user_feedback
    calls = []

    def competing_mutation(user, rating, **kwargs):
        action_id, version = kwargs["expected_action_id"], kwargs["expected_version"]
        calls.append((action_id, version))
        session = kwargs["session"]
        # Deterministic interleaving between captured observation and guarded DML.
        if race == "cancel":
            assert delete_pending_action(user, version, expected_action_id=action_id, now=NOW, session=session)
        elif race == "delete_all":
            execute_delete_all(user, version, expected_action_id=action_id, now=NOW, session=session)
        else:
            session.execute(update(PendingAction).where(PendingAction.line_user_id == user).values(
                action_id="e" * 32, version=version + 1, action_type="confirm_delete",
            ).execution_options(synchronize_session=False))
        return original(user, rating, **kwargs)

    monkeypatch.setattr(app_module, "add_user_feedback", competing_mutation)
    assert _post_text(client, "stale-feedback", USER, "เสนอแนะ 5").status_code == 200
    assert replies[-1][1] == app_module.PENDING_ACTION_RETRY_REPLY
    assert len(calls) == 1 and calls[0][0] is not None
    with Session(engine) as session:
        assert _count(session) == 0
        assert session.get(ProcessedWebhookEvent, "stale-feedback").response_text == app_module.PENDING_ACTION_RETRY_REPLY
        if race == "replacement":
            action = session.get(PendingAction, USER)
            assert action.action_id == "e" * 32 and action.action_type == "confirm_delete"
        else:
            assert session.get(PendingAction, USER) is None
    assert _post_text(client, "stale-feedback", USER, "เสนอแนะ 5").status_code == 200
    assert len(calls) == 1
    with Session(engine) as session:
        assert _count(session) == 0


def test_feedback_dedup_sent_reply_and_distinct_events(feedback_client):
    client, engine, replies = feedback_client
    for event_id in ("feedback", "feedback", "distinct"):
        assert _post_text(client, event_id, USER, "เสนอแนะ 5 ดี").status_code == 200
    assert [reply[0] for reply in replies] == ["reply-feedback", "reply-distinct"]
    with Session(engine) as session:
        assert _count(session) == 2
        cached = session.get(ProcessedWebhookEvent, "feedback")
        assert cached.reply_sent is True and cached.response_text == _success(5)


def test_unsent_cached_feedback_reply_retries_without_inserting_again(feedback_client, monkeypatch):
    client, engine, replies = feedback_client
    attempts = []

    async def fail_once(reply_token, text, token):
        attempts.append((reply_token, text, token))
        if len(attempts) == 1:
            raise LineTransportError("temporary failure")

    monkeypatch.setattr(app_module, "reply_text", fail_once)
    assert _post_text(client, "feedback", USER, "เสนอแนะ 5").status_code == 502
    with Session(engine) as session:
        assert _count(session) == 1
        cached = session.get(ProcessedWebhookEvent, "feedback")
        assert cached.reply_sent is False and cached.response_text == _success(5)

    def forbidden(*args, **kwargs):
        pytest.fail("Cached redelivery must not invoke feedback mutation")

    monkeypatch.setattr(app_module, "add_user_feedback", forbidden)
    assert _post_text(client, "feedback", USER, "เสนอแนะ 5").status_code == 200
    assert attempts[0] == attempts[1]
    with Session(engine) as session:
        assert _count(session) == 1
        assert session.get(ProcessedWebhookEvent, "feedback").reply_sent is True


def test_feedback_requires_verified_webhook_signature(feedback_client):
    client, engine, replies = feedback_client
    body, _ = _signed_body({"events": [_text_event("bad", USER, "เสนอแนะ 5")]})
    response = client.post("/webhook", content=body, headers={"content-type": "application/json", "x-line-signature": "invalid"})
    assert response.status_code == 400
    with Session(engine) as session:
        assert _count(session) == 0
        assert session.get(ProcessedWebhookEvent, "bad") is None


def test_feedback_does_not_touch_export_tokens_or_enter_csv(feedback_client):
    client, engine, replies = feedback_client
    assert _post_text(client, "only-feedback", USER, "เสนอแนะ 5 private-comment").status_code == 200
    assert _post_text(client, "empty-export", USER, "ส่งออกข้อมูล").status_code == 200
    assert replies[-1][1] == "ไม่มีข้อมูลสำหรับส่งออกครับ"
    with Session(engine) as session:
        assert _count(session, ExportToken) == 0
    assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
    assert _post_text(client, "export", USER, "ส่งออกข้อมูล").status_code == 200
    url = replies[-1][1].splitlines()[-1]
    with Session(engine) as session:
        before_tokens = [_row_state(item) for item in session.scalars(select(ExportToken))]
    assert _post_text(client, "new-feedback", USER, "เสนอแนะ 4 another-private-comment").status_code == 200
    with Session(engine) as session:
        assert [_row_state(item) for item in session.scalars(select(ExportToken))] == before_tokens
    response = client.get(url)
    assert response.status_code == 200
    assert "private-comment" not in response.text
    assert len(response.content.decode("utf-8-sig").splitlines()) == 2
    assert _post_text(client, "trigger", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    assert _post_text(client, "confirm", USER, "ยืนยัน").status_code == 200
    assert client.get(url).status_code == 404
    with Session(engine) as session:
        for model in (UserFeedback, ExportToken, Transaction, SavingsGoal, PendingAction, PendingTransaction):
            assert _count(session, model) == 0


def test_postgresql_webhook_returning_claim_also_deduplicates_feedback(feedback_client, monkeypatch):
    client, engine, replies = feedback_client
    original_execute = Session.execute

    class UnknownRowcount:
        rowcount = -1

        def __init__(self, result):
            self.result = result

        def scalar_one_or_none(self):
            return self.result.scalar_one_or_none()

    def hide_rowcount(session, statement, *args, **kwargs):
        result = original_execute(session, statement, *args, **kwargs)
        if getattr(statement, "is_insert", False) and statement.table.name == "processed_webhook_events":
            return UnknownRowcount(result)
        return result

    monkeypatch.setattr(engine.dialect, "name", "postgresql")
    monkeypatch.setattr(Session, "execute", hide_rowcount)
    for _ in range(2):
        assert _post_text(client, "feedback", USER, "เสนอแนะ 5").status_code == 200
    with Session(engine) as session:
        assert _count(session) == 1
        assert session.get(ProcessedWebhookEvent, "feedback").reply_sent is True
    assert len(replies) == 1
