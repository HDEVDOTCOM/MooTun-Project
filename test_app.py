from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import handle_text_message
from models import Base
from repository import list_recent_transactions, monthly_summary


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app-test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.rollback()
    engine.dispose()


def test_multi_user_chat_flow_is_isolated(db_session):
    moment = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)

    alice_reply = handle_text_message("U-alice", "จ่าย 50 อาหาร", moment, db_session)
    bob_reply = handle_text_message("U-bob", "รับ 500 ค่าขนม", moment, db_session)

    assert "50 บาท" in alice_reply
    assert "500 บาท" in bob_reply
    assert [item.amount_satang for item in list_recent_transactions("U-alice", session=db_session)] == [5000]
    assert [item.amount_satang for item in list_recent_transactions("U-bob", session=db_session)] == [50000]


def test_monthly_summary_and_delete_follow_chat_commands(db_session):
    moment = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)
    handle_text_message("U-test", "รับ 1000 ค่าขนม", moment, db_session)
    handle_text_message("U-test", "จ่าย 120.50 อาหาร", moment, db_session)

    summary_reply = handle_text_message("U-test", "สรุปเดือนนี้", moment, db_session)
    assert "เดือน 9/2569" in summary_reply
    assert "รายรับ: 1,000 บาท" in summary_reply
    assert "รายจ่าย: 120.5 บาท" in summary_reply
    assert "คงเหลือ: 879.5 บาท" in summary_reply

    delete_reply = handle_text_message("U-test", "ลบล่าสุด", moment, db_session)
    assert "ลบรายการล่าสุดแล้ว" in delete_reply
    assert "14/09/2569" in delete_reply
    summary = monthly_summary("U-test", 2026, 9, session=db_session)
    assert summary["expense"] == 0


def test_savings_goal_flow(db_session):
    moment = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)

    assert "ตั้งเป้าหมายเรียบร้อยแล้ว" in handle_text_message(
        "U-test", "ตั้งเป้า 1500 ซื้อหนังสือ", moment, db_session
    )
    assert "บันทึกเงินออมเรียบร้อยแล้ว" in handle_text_message(
        "U-test", "ออม 100", moment, db_session
    )
    status = handle_text_message("U-test", "เป้าหมาย", moment, db_session)
    assert "100 / 1,500 บาท" in status
