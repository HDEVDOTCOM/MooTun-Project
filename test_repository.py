from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from models import Base
from database import init_db
from repository import (
    add_savings_progress,
    add_transaction,
    baht_to_satang,
    delete_latest_transaction,
    get_savings_goal,
    get_webhook_event,
    list_recent_transactions,
    mark_webhook_processed,
    mark_webhook_replied,
    monthly_summary,
    set_savings_goal,
    set_webhook_response,
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
