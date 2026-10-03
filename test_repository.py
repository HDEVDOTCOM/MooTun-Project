from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, delete, inspect, insert, select, text, update
from sqlalchemy.orm import Session

from models import Base, PendingAction, PendingTransaction, Transaction
from database import init_db
from repository import (
    PENDING_ACTION_TTL,
    UNDO_DELETE_TTL,
    PendingActionConflictError,
    PendingActionState,
    PendingTransactionConflictError,
    RestoreOutcome,
    add_savings_progress,
    add_transaction,
    baht_to_satang,
    confirm_delete_transaction,
    create_pending_action,
    create_pending_transaction,
    delete_latest_transaction,
    delete_pending_action,
    delete_pending_transaction,
    get_latest_transaction,
    get_pending_action,
    get_savings_goal,
    get_pending_transaction,
    get_webhook_event,
    list_recent_transactions,
    mark_webhook_processed,
    mark_webhook_replied,
    monthly_summary,
    restore_deleted_transaction,
    set_savings_goal,
    set_webhook_response,
    update_latest_transaction,
    update_pending_transaction,
)


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.rollback()
    engine.dispose()


def test_money_is_stored_as_exact_integer_satang(db_session):
    item = add_transaction(
        "U-alice", "expense", Decimal("19.99"), "อาหาร", session=db_session
    )

    assert item.amount_satang == 1999
    assert item.amount == Decimal("19.99")
    with pytest.raises(ValueError):
        baht_to_satang("1.001")
    with pytest.raises(TypeError):
        baht_to_satang(19.99)


def test_transactions_are_isolated_by_line_user_id(db_session):
    add_transaction("U-alice", "expense", "50", "อาหาร", session=db_session)
    add_transaction("U-bob", "income", "500", "ค่าขนม", session=db_session)

    alice_items = list_recent_transactions("U-alice", session=db_session)
    bob_items = list_recent_transactions("U-bob", session=db_session)

    assert [item.amount for item in alice_items] == [Decimal("50")]
    assert [item.amount for item in bob_items] == [Decimal("500")]


def test_recent_delete_and_monthly_summary(db_session):
    add_transaction(
        "U-alice", "income", "1000", "ค่าขนม", occurred_on=date(2026, 9, 1), session=db_session
    )
    add_transaction(
        "U-alice", "expense", "120.50", "อาหาร", occurred_on=date(2026, 9, 3), session=db_session
    )
    add_transaction(
        "U-alice", "expense", "80", "เดินทาง", occurred_on=date(2026, 9, 4), session=db_session
    )
    add_transaction(
        "U-alice", "expense", "999", "อื่นๆ", occurred_on=date(2026, 8, 31), session=db_session
    )

    summary = monthly_summary("U-alice", 2026, 9, session=db_session)
    assert summary == {
        "year": 2026,
        "month": 9,
        "income": Decimal("1000"),
        "expense": Decimal("200.5"),
        "balance": Decimal("799.5"),
        "transaction_count": 3,
    }

    deleted = delete_latest_transaction("U-alice", session=db_session)
    assert deleted is not None
    assert deleted.amount == Decimal("999")
    assert delete_latest_transaction("U-bob", session=db_session) is None
    assert len(list_recent_transactions("U-alice", session=db_session)) == 3


def test_update_latest_transaction_replaces_only_the_newest_row(db_session):
    older = add_transaction(
        "U-alice",
        "expense",
        "50",
        "อาหาร",
        description="ข้าว",
        occurred_on=date(2026, 9, 1),
        session=db_session,
    )
    newer = add_transaction(
        "U-alice",
        "expense",
        "80",
        "เดินทาง",
        description="BTS",
        occurred_on=date(2026, 9, 2),
        session=db_session,
    )

    updated = update_latest_transaction(
        "U-alice",
        "income",
        "999",
        "ค่าขนม",
        description="แม่ให้",
        occurred_on=date(2026, 9, 3),
        session=db_session,
    )

    assert updated is not None
    assert updated.id == newer.id
    assert updated.transaction_type == "income"
    assert updated.amount == Decimal("999")
    assert updated.category == "ค่าขนม"
    assert updated.description == "แม่ให้"
    assert updated.occurred_on == date(2026, 9, 3)

    assert older.transaction_type == "expense"
    assert older.amount == Decimal("50")
    assert older.occurred_on == date(2026, 9, 1)


def test_update_latest_transaction_inherits_date_when_none(db_session):
    add_transaction(
        "U-alice",
        "expense",
        "50",
        "อาหาร",
        description="ข้าว",
        occurred_on=date(2026, 9, 1),
        session=db_session,
    )

    updated = update_latest_transaction(
        "U-alice", "expense", "60", "อาหาร", session=db_session
    )

    assert updated is not None
    assert updated.occurred_on == date(2026, 9, 1)
    assert updated.description is None


def test_update_latest_transaction_is_isolated_and_handles_no_rows(db_session):
    add_transaction("U-alice", "expense", "50", "อาหาร", session=db_session)
    add_transaction("U-bob", "income", "500", "ค่าขนม", session=db_session)

    assert (
        update_latest_transaction(
            "U-carol", "expense", "10", "อื่นๆ", session=db_session
        )
        is None
    )

    update_latest_transaction("U-bob", "expense", "20", "อื่นๆ", session=db_session)

    alice_items = list_recent_transactions("U-alice", session=db_session)
    bob_items = list_recent_transactions("U-bob", session=db_session)
    assert [(item.transaction_type, item.amount) for item in alice_items] == [
        ("expense", Decimal("50"))
    ]
    assert [(item.transaction_type, item.amount) for item in bob_items] == [
        ("expense", Decimal("20"))
    ]


def test_update_latest_transaction_uses_created_at_then_id(db_session):
    earlier_created = add_transaction(
        "U-alice",
        "expense",
        "10",
        "อื่นๆ",
        occurred_on=date(2026, 12, 31),
        session=db_session,
    )
    later_created = add_transaction(
        "U-alice",
        "expense",
        "20",
        "อื่นๆ",
        occurred_on=date(2026, 1, 1),
        session=db_session,
    )
    earlier_created.created_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    later_created.created_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
    db_session.flush()

    updated = update_latest_transaction(
        "U-alice", "income", "30", "ค่าขนม", session=db_session
    )

    assert updated is not None
    assert updated.id == later_created.id
    assert earlier_created.amount == Decimal("10")

    tied_at = datetime(2026, 9, 3, tzinfo=timezone.utc)
    earlier_created.created_at = tied_at
    later_created.created_at = tied_at
    db_session.flush()

    updated = update_latest_transaction(
        "U-alice", "expense", "40", "อาหาร", session=db_session
    )

    assert updated is not None
    assert updated.id == later_created.id
    assert later_created.id > earlier_created.id
    assert earlier_created.amount == Decimal("10")


def test_savings_goal_is_isolated_and_keeps_progress_when_updated(db_session):
    goal = set_savings_goal(
        "U-alice", "ซื้อหนังสือ", "1500", deadline=date(2026, 12, 1), session=db_session
    )
    add_savings_progress("U-alice", "250.25", session=db_session)
    updated = set_savings_goal(
        "U-alice", "ซื้อหนังสือชุดใหม่", "2000", session=db_session
    )

    assert goal.id == updated.id
    assert updated.target_amount == Decimal("2000")
    assert updated.saved_amount == Decimal("250.25")
    assert updated.remaining_amount == Decimal("1749.75")
    assert get_savings_goal("U-bob", session=db_session) is None

    with pytest.raises(LookupError):
        add_savings_progress("U-bob", "100", session=db_session)


def test_webhook_event_can_only_be_claimed_once(db_session):
    assert mark_webhook_processed("01HLINEEVENT", session=db_session) is True
    assert mark_webhook_processed("01HLINEEVENT", session=db_session) is False
    assert mark_webhook_processed("01HLINEEVENT-2", session=db_session) is True

    pending = set_webhook_response(
        "01HLINEEVENT", "บันทึกแล้ว", session=db_session
    )
    assert pending.reply_sent is False
    assert get_webhook_event("01HLINEEVENT", session=db_session).response_text == "บันทึกแล้ว"
    mark_webhook_replied("01HLINEEVENT", session=db_session)
    assert get_webhook_event("01HLINEEVENT", session=db_session).reply_sent is True


def test_pending_action_lifecycle_and_fixed_ttl(db_session):
    now = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)
    target = add_transaction(
        "U-alice", "expense", "50", "อาหาร", session=db_session
    )

    absent = get_pending_action("U-alice", now=now, session=db_session)
    assert absent.state == PendingActionState.ABSENT
    assert absent.action is None
    assert create_pending_action(
        "U-alice",
        action_type="confirm_delete",
        target_transaction_id=target.id,
        now=now,
        session=db_session,
    )
    assert not create_pending_action(
        "U-alice",
        action_type="confirm_delete",
        target_transaction_id=target.id,
        now=now,
        session=db_session,
    )

    active = get_pending_action("U-alice", now=now, session=db_session)
    assert active.state == PendingActionState.ACTIVE
    assert active.action is not None
    assert len(active.action.action_id) == 32
    assert active.action.version == 1
    assert active.action.created_at.replace(tzinfo=timezone.utc) == now
    assert active.action.expires_at.replace(tzinfo=timezone.utc) == (
        now + PENDING_ACTION_TTL
    )

    assert not delete_pending_action(
        "U-alice",
        2,
        expected_action_id=active.action.action_id,
        now=now,
        session=db_session,
    )
    assert delete_pending_action(
        "U-alice",
        1,
        expected_action_id=active.action.action_id,
        now=now,
        session=db_session,
    )


def test_pending_action_expiry_is_distinct_from_absence(db_session):
    now = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)
    target = add_transaction(
        "U-alice", "expense", "50", "อาหาร", session=db_session
    )
    assert create_pending_action(
        "U-alice",
        action_type="confirm_delete",
        target_transaction_id=target.id,
        now=now,
        session=db_session,
    )

    expired = get_pending_action(
        "U-alice", now=now + PENDING_ACTION_TTL, session=db_session
    )
    assert expired.state == PendingActionState.EXPIRED_CLEANED
    assert expired.action is None
    assert get_pending_action(
        "U-alice", now=now + PENDING_ACTION_TTL, session=db_session
    ).state == PendingActionState.ABSENT


def test_confirm_delete_is_atomic_user_scoped_and_handles_missing_target(db_session):
    now = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)
    target = add_transaction(
        "U-alice", "expense", "50", "อาหาร", session=db_session
    )
    assert create_pending_action(
        "U-alice",
        action_type="confirm_delete",
        target_transaction_id=target.id,
        now=now,
        session=db_session,
    )
    action = db_session.get(PendingAction, "U-alice")
    original_version = action.version

    with pytest.raises(PendingActionConflictError):
        confirm_delete_transaction(
            "U-alice",
            action.version + 1,
            expected_action_id=action.action_id,
            target_transaction_id=target.id,
            now=now,
            session=db_session,
        )
    assert db_session.get(Transaction, target.id) is target
    assert db_session.get(PendingAction, "U-alice") is action

    with pytest.raises(PendingActionConflictError):
        confirm_delete_transaction(
            "U-bob",
            action.version,
            expected_action_id=action.action_id,
            target_transaction_id=target.id,
            now=now,
            session=db_session,
        )
    assert db_session.get(Transaction, target.id) is target
    assert db_session.get(PendingAction, "U-alice") is action

    deleted = confirm_delete_transaction(
        "U-alice",
        action.version,
        expected_action_id=action.action_id,
        target_transaction_id=target.id,
        now=now,
        session=db_session,
    )
    assert deleted is target
    assert db_session.get(Transaction, target.id) is None

    undo = db_session.get(PendingAction, "U-alice")
    db_session.refresh(undo)
    assert undo.action_type == "undo_delete"
    assert undo.version == original_version + 1
    assert undo.target_transaction_id == target.id
    assert undo.snapshot_transaction_id == target.id
    assert undo.snapshot_transaction_type == target.transaction_type
    assert undo.snapshot_amount_satang == target.amount_satang
    assert undo.snapshot_category == target.category
    assert undo.snapshot_description == target.description
    assert undo.snapshot_occurred_on == target.occurred_on
    assert undo.snapshot_created_at is not None
    assert undo.expires_at.replace(tzinfo=timezone.utc) == now + UNDO_DELETE_TTL

    assert delete_pending_action(
        "U-alice",
        undo.version,
        expected_action_id=undo.action_id,
        now=now,
        session=db_session,
    )
    missing_id = target.id + 100
    assert create_pending_action(
        "U-alice",
        action_type="confirm_delete",
        target_transaction_id=missing_id,
        now=now,
        session=db_session,
    )
    missing_action = db_session.get(PendingAction, "U-alice")
    db_session.refresh(missing_action)
    assert confirm_delete_transaction(
        "U-alice",
        missing_action.version,
        expected_action_id=missing_action.action_id,
        target_transaction_id=missing_id,
        now=now,
        session=db_session,
    ) is None
    db_session.expire_all()
    assert db_session.get(PendingAction, "U-alice") is None


def test_get_latest_transaction_uses_created_at_then_id(db_session):
    first = add_transaction(
        "U-alice",
        "expense",
        "10",
        "อื่นๆ",
        occurred_on=date(2026, 12, 31),
        session=db_session,
    )
    second = add_transaction(
        "U-alice",
        "expense",
        "20",
        "อื่นๆ",
        occurred_on=date(2026, 1, 1),
        session=db_session,
    )
    first.created_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
    second.created_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    db_session.flush()

    assert get_latest_transaction("U-alice", session=db_session).id == first.id

    second.created_at = first.created_at
    db_session.flush()
    assert second.id > first.id
    assert get_latest_transaction("U-alice", session=db_session).id == second.id


def test_pending_action_lazy_expiry_detects_replacement(db_session, monkeypatch):
    now = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)
    target = add_transaction(
        "U-alice", "expense", "50", "อาหาร", session=db_session
    )
    old_action_id = "a" * 32
    replacement_action_id = "b" * 32
    db_session.add(
        PendingAction(
            line_user_id="U-alice",
            action_id=old_action_id,
            version=1,
            action_type="confirm_delete",
            target_transaction_id=target.id,
            created_at=now - timedelta(minutes=20),
            expires_at=now - timedelta(minutes=10),
        )
    )
    db_session.flush()
    original_execute = db_session.execute
    replaced = False

    def replace_before_cleanup(statement, *args, **kwargs):
        nonlocal replaced
        is_action_delete = (
            getattr(statement, "is_delete", False)
            and statement.table.name == "pending_actions"
        )
        if is_action_delete and not replaced:
            replaced = True
            original_execute(
                delete(PendingAction)
                .where(PendingAction.line_user_id == "U-alice")
                .execution_options(synchronize_session=False)
            )
            original_execute(
                insert(PendingAction).values(
                    line_user_id="U-alice",
                    action_id=replacement_action_id,
                    version=7,
                    action_type="confirm_delete",
                    target_transaction_id=target.id,
                    created_at=now,
                    expires_at=now + PENDING_ACTION_TTL,
                )
            )
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", replace_before_cleanup)
    with pytest.raises(
        PendingActionConflictError,
        match="pending action changed during expiry cleanup",
    ):
        get_pending_action("U-alice", now=now, session=db_session)

    db_session.expire_all()
    replacement = db_session.get(PendingAction, "U-alice")
    assert replacement is not None
    assert replacement.action_id == replacement_action_id
    assert replacement.version == 7
    assert db_session.get(Transaction, target.id) is not None


def test_pending_action_occ_delete_uses_reliable_rowcount_for_postgresql(
    db_session,
    monkeypatch,
):
    now = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)
    target = add_transaction(
        "U-alice", "expense", "50", "อาหาร", session=db_session
    )
    original_execute = db_session.execute

    class InsertResultWithUnknownRowcount:
        rowcount = -1

        def __init__(self, result):
            self._result = result

        def __getattr__(self, name):
            return getattr(self._result, name)

    def hide_insert_rowcount(statement, *args, **kwargs):
        result = original_execute(statement, *args, **kwargs)
        if (
            getattr(statement, "is_insert", False)
            and statement.table.name == "pending_actions"
        ):
            return InsertResultWithUnknownRowcount(result)
        return result

    monkeypatch.setattr(db_session.get_bind().dialect, "name", "postgresql")
    monkeypatch.setattr(db_session, "execute", hide_insert_rowcount)
    assert create_pending_action(
        "U-alice",
        action_type="confirm_delete",
        target_transaction_id=target.id,
        now=now,
        session=db_session,
    )
    action = db_session.get(PendingAction, "U-alice")

    assert delete_pending_action(
        "U-alice",
        action.version,
        expected_action_id=action.action_id,
        now=now,
        session=db_session,
    )


def test_init_db_upgrades_legacy_webhook_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE processed_webhook_events ("
                "webhook_event_id VARCHAR(128) PRIMARY KEY, "
                "processed_at DATETIME NOT NULL)"
            )
        )

    init_db(engine)

    columns = {item["name"] for item in inspect(engine).get_columns("processed_webhook_events")}
    assert {"response_text", "reply_sent"}.issubset(columns)
    engine.dispose()


def test_init_db_creates_pending_action_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pending-action.db'}")

    init_db(engine)

    assert inspect(engine).has_table("pending_actions")
    columns = {
        column["name"] for column in inspect(engine).get_columns("pending_actions")
    }
    assert columns == {
        "line_user_id",
        "action_id",
        "version",
        "action_type",
        "target_transaction_id",
        "created_at",
        "expires_at",
        "snapshot_transaction_id",
        "snapshot_transaction_type",
        "snapshot_amount_satang",
        "snapshot_category",
        "snapshot_description",
        "snapshot_occurred_on",
        "snapshot_created_at",
    }
    engine.dispose()


def test_fresh_sqlite_does_not_reuse_deleted_highest_transaction_id(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'undo-id-allocation.db'}")
    init_db(engine)
    with engine.connect() as connection:
        ddl = connection.scalar(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transactions'")
        )
        assert ddl is not None and "AUTOINCREMENT" in ddl.upper()
        assert connection.scalar(
            text("SELECT count(*) FROM sqlite_master WHERE name = 'sqlite_sequence'")
        ) == 1

    with Session(engine) as session:
        first = add_transaction("U-alice", "expense", "10", "อาหาร", session=session)
        second = add_transaction("U-alice", "expense", "20", "อาหาร", session=session)
        assert (first.id, second.id) == (1, 2)
        original_id = second.id
        original_created_at = second.created_at
        original_occurred_on = second.occurred_on
        session.delete(second)
        session.flush()

        newer = add_transaction("U-alice", "income", "30", "รายรับอื่นๆ", session=session)
        assert newer.id > original_id, "SQLite reused the deleted ID; undo would collide"
        assert newer.id == 3

        restored = Transaction(
            id=original_id,
            line_user_id="U-alice",
            transaction_type="expense",
            amount_satang=2000,
            category="อาหาร",
            occurred_on=original_occurred_on,
            created_at=original_created_at,
        )
        session.add(restored)
        session.flush()
        assert restored.id == 2
        assert session.scalar(select(Transaction).where(Transaction.id == 2)) is restored
        assert newer.id == 3
        session.commit()

    engine.dispose()


def test_init_db_upgrades_pending_transaction_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-pending.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE pending_transactions ("
                "line_user_id VARCHAR(128) PRIMARY KEY)"
            )
        )

    init_db(engine)

    columns = {item["name"] for item in inspect(engine).get_columns("pending_transactions")}
    assert "inference_rule" in columns
    engine.dispose()


def test_pending_transaction_lifecycle_uses_version_and_expiry(db_session):
    now = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)
    assert create_pending_transaction(
        "U-alice",
        transaction_type="expense",
        amount=None,
        category="อาหาร",
        description="ข้าว",
        occurred_on=date(2026, 9, 14),
        now=now,
        session=db_session,
    )
    pending = get_pending_transaction("U-alice", now=now, session=db_session)
    assert pending is not None
    assert pending.version == 1
    draft_id = pending.draft_id
    original_created_at = pending.created_at

    assert update_pending_transaction(
        "U-alice",
        1,
        expected_draft_id=draft_id,
        transaction_type="expense",
        amount="50",
        category="อาหาร",
        description="ข้าว",
        now=now + timedelta(minutes=30),
        session=db_session,
    )
    assert not update_pending_transaction(
        "U-alice",
        1,
        expected_draft_id=draft_id,
        transaction_type="income",
        amount="500",
        category="รายรับอื่นๆ",
        description=None,
        now=now + timedelta(minutes=31),
        session=db_session,
    )
    pending = db_session.get(PendingTransaction, "U-alice")
    db_session.refresh(pending)
    assert pending.version == 2
    assert pending.created_at == original_created_at
    assert pending.amount_satang == 5000
    assert pending.expires_at.replace(tzinfo=timezone.utc) == now + timedelta(minutes=90)
    assert not delete_pending_transaction(
        "U-alice",
        1,
        expected_draft_id=draft_id,
        now=now + timedelta(minutes=31),
        session=db_session,
    )
    assert delete_pending_transaction(
        "U-alice",
        2,
        expected_draft_id=draft_id,
        now=now + timedelta(minutes=31),
        session=db_session,
    )


def test_pending_creation_is_first_successful_create_wins(db_session):
    now = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)
    arguments = {
        "transaction_type": None,
        "amount": "500",
        "category": None,
        "description": None,
        "occurred_on": date(2026, 9, 14),
        "now": now,
        "session": db_session,
    }
    assert create_pending_transaction("U-alice", **arguments)
    assert not create_pending_transaction("U-alice", **arguments)
    assert db_session.get(PendingTransaction, "U-alice").version == 1


def test_recreated_pending_transaction_rejects_stale_incarnation(db_session):
    now = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)
    arguments = {
        "transaction_type": None,
        "amount": "500",
        "category": None,
        "description": None,
        "occurred_on": date(2026, 9, 14),
        "session": db_session,
    }
    assert create_pending_transaction("U-alice", now=now, **arguments)
    original = db_session.get(PendingTransaction, "U-alice")
    original_draft_id = original.draft_id
    assert delete_pending_transaction(
        "U-alice",
        original.version,
        expected_draft_id=original_draft_id,
        now=now,
        session=db_session,
    )

    assert create_pending_transaction(
        "U-alice",
        now=now + timedelta(minutes=1),
        **arguments,
    )
    replacement = db_session.get(PendingTransaction, "U-alice")
    db_session.refresh(replacement)
    assert replacement.version == 1
    assert replacement.draft_id != original_draft_id

    assert not update_pending_transaction(
        "U-alice",
        1,
        expected_draft_id=original_draft_id,
        transaction_type="expense",
        amount="500",
        category="อื่นๆ",
        description=None,
        now=now + timedelta(minutes=2),
        session=db_session,
    )
    assert not delete_pending_transaction(
        "U-alice",
        1,
        expected_draft_id=original_draft_id,
        now=now + timedelta(minutes=2),
        session=db_session,
    )
    assert db_session.get(PendingTransaction, "U-alice").draft_id == replacement.draft_id


def test_expired_pending_transaction_cannot_be_updated_or_deleted(db_session):
    now = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)
    assert create_pending_transaction(
        "U-alice",
        transaction_type=None,
        amount="500",
        category=None,
        description=None,
        occurred_on=date(2026, 9, 14),
        now=now,
        session=db_session,
    )
    pending = db_session.get(PendingTransaction, "U-alice")
    expires_at = now + timedelta(hours=1)

    assert not update_pending_transaction(
        "U-alice",
        pending.version,
        expected_draft_id=pending.draft_id,
        transaction_type="expense",
        amount="500",
        category="อื่นๆ",
        description=None,
        now=expires_at,
        session=db_session,
    )
    assert not delete_pending_transaction(
        "U-alice",
        pending.version,
        expected_draft_id=pending.draft_id,
        now=expires_at,
        session=db_session,
    )


def test_expired_pending_transaction_is_lazily_deleted(db_session):
    now = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)
    assert create_pending_transaction(
        "U-alice",
        transaction_type=None,
        amount="500",
        category=None,
        description=None,
        occurred_on=date(2026, 9, 14),
        now=now,
        session=db_session,
    )
    assert get_pending_transaction(
        "U-alice", now=now + timedelta(hours=1), session=db_session
    ) is None
    assert db_session.get(PendingTransaction, "U-alice") is None


def test_failed_lazy_expiry_delete_surfaces_concurrent_modification(
    db_session,
    monkeypatch,
):
    now = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)
    assert create_pending_transaction(
        "U-alice",
        transaction_type=None,
        amount="500",
        category=None,
        description=None,
        occurred_on=date(2026, 9, 14),
        now=now,
        session=db_session,
    )
    original_execute = db_session.execute
    refreshed_expiry = now + timedelta(hours=2)

    def refresh_before_delete(statement, *args, **kwargs):
        if getattr(statement, "is_delete", False):
            original_execute(
                update(PendingTransaction)
                .where(PendingTransaction.line_user_id == "U-alice")
                .values(version=2, expires_at=refreshed_expiry)
                .execution_options(synchronize_session=False)
            )
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", refresh_before_delete)
    with pytest.raises(
        PendingTransactionConflictError,
        match="pending transaction changed during expiry cleanup",
    ):
        get_pending_transaction(
            "U-alice",
            now=now + timedelta(hours=1),
            session=db_session,
        )

    db_session.expire_all()
    pending = db_session.get(PendingTransaction, "U-alice")
    assert pending is not None
    assert pending.version == 2
    assert pending.expires_at.replace(tzinfo=timezone.utc) == refreshed_expiry
    get_latest_transaction,
    get_pending_action,


def test_init_db_upgrades_legacy_pending_action_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-action.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE pending_actions ("
                "line_user_id VARCHAR(128) PRIMARY KEY, "
                "action_id VARCHAR(32) NOT NULL, "
                "version INTEGER NOT NULL, "
                "action_type VARCHAR(32) NOT NULL, "
                "target_transaction_id INTEGER NOT NULL, "
                "created_at DATETIME NOT NULL, "
                "expires_at DATETIME NOT NULL)"
            )
        )

    init_db(engine)

    columns = {
        column["name"] for column in inspect(engine).get_columns("pending_actions")
    }
    assert {
        "snapshot_transaction_id",
        "snapshot_transaction_type",
        "snapshot_amount_satang",
        "snapshot_category",
        "snapshot_description",
        "snapshot_occurred_on",
        "snapshot_created_at",
    }.issubset(columns)
    engine.dispose()


def test_restore_deleted_transaction_round_trip_and_id_collision(db_session):
    now = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)
    target = add_transaction(
        "U-alice", "expense", "50", "อาหาร", description="ข้าว", session=db_session
    )
    db_session.refresh(target)
    original_id = target.id
    original_created_at = target.created_at

    assert create_pending_action(
        "U-alice",
        action_type="confirm_delete",
        target_transaction_id=original_id,
        now=now,
        session=db_session,
    )
    action = db_session.get(PendingAction, "U-alice")
    db_session.refresh(action)
    assert confirm_delete_transaction(
        "U-alice",
        action.version,
        expected_action_id=action.action_id,
        target_transaction_id=original_id,
        now=now,
        session=db_session,
    ) is not None

    undo = db_session.get(PendingAction, "U-alice")
    db_session.refresh(undo)
    result = restore_deleted_transaction(
        "U-alice",
        undo.version,
        expected_action_id=undo.action_id,
        now=now,
        session=db_session,
    )
    assert result.outcome is RestoreOutcome.RESTORED
    assert result.transaction is not None
    assert result.transaction.id == original_id
    assert result.transaction.created_at == original_created_at
    db_session.expire_all()
    assert db_session.get(PendingAction, "U-alice") is None

    other = add_transaction("U-alice", "expense", "70", "อื่นๆ", session=db_session)
    assert create_pending_action(
        "U-alice",
        action_type="confirm_delete",
        target_transaction_id=other.id,
        now=now,
        session=db_session,
    )
    other_action = db_session.get(PendingAction, "U-alice")
    db_session.refresh(other_action)
    assert confirm_delete_transaction(
        "U-alice",
        other_action.version,
        expected_action_id=other_action.action_id,
        target_transaction_id=other.id,
        now=now,
        session=db_session,
    ) is not None

    undo = db_session.get(PendingAction, "U-alice")
    db_session.refresh(undo)
    snapshot_id = undo.snapshot_transaction_id
    db_session.execute(
        insert(Transaction).values(
            id=snapshot_id,
            line_user_id="U-alice",
            transaction_type="expense",
            amount_satang=1234,
            category="อื่นๆ",
            description="ทับซ้อน",
            occurred_on=date(2026, 9, 23),
            created_at=now,
        )
    )
    db_session.flush()

    outcome = restore_deleted_transaction(
        "U-alice",
        undo.version,
        expected_action_id=undo.action_id,
        now=now,
        session=db_session,
    )
    assert outcome.outcome is RestoreOutcome.ID_COLLISION
    assert outcome.transaction is None
    db_session.expire_all()
    assert db_session.get(PendingAction, "U-alice") is None
    assert db_session.get(Transaction, snapshot_id).description == "ทับซ้อน"
