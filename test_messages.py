from datetime import date

from messages import (
    format_amount,
    format_help_message,
    format_monthly_summary,
    format_recent_transactions,
    format_savings_goal,
    format_transaction_confirmation,
    format_unknown_message,
    limit_line_text,
)


def test_format_amount_keeps_only_meaningful_decimals() -> None:
    assert format_amount(1250) == "1,250"
    assert format_amount("1250.50") == "1,250.5"
    assert format_amount(None) == "0"


def test_confirmation_contains_clean_transaction_fields() -> None:
    message = format_transaction_confirmation(
        {
            "type": "expense",
            "amount": "80.00",
            "category": "อาหาร",
            "date": date(2026, 9, 14),
            "note": "ข้าวกลางวัน\nโรงอาหาร",
        }
    )

    assert "บันทึกรายจ่ายแล้ว" in message
    assert "จำนวน: 80 บาท" in message
    assert "หมวด: อาหาร" in message
    assert "วันที่: 14/09/2026" in message
    assert "ข้าวกลางวัน โรงอาหาร" in message


def test_recent_transactions_handles_empty_and_mixed_items() -> None:
    assert "ยังไม่มีรายการ" in format_recent_transactions([])

    message = format_recent_transactions(
        [
            {"type": "income", "amount": 500, "category": "ค่าขนม"},
            {"type": "expense", "amount": 42.5, "category": "เดินทาง"},
        ]
    )
    assert "+500 บาท" in message
    assert "−42.5 บาท" in message


def test_monthly_summary_calculates_balance_and_optional_categories() -> None:
    message = format_monthly_summary(
        {
            "month": "เดือนกันยายน 2569",
            "income": 3000,
            "expense": 1250,
            "transaction_count": 12,
            "top_categories": [{"category": "อาหาร", "amount": 900}],
        }
    )
    assert "รายรับ: 3,000 บาท" in message
    assert "รายจ่าย: 1,250 บาท" in message
    assert "คงเหลือ: 1,750 บาท" in message
    assert "ทั้งหมด: 12 รายการ" in message
    assert "อาหาร: 900 บาท" in message


def test_savings_progress_is_bounded_and_handles_zero_target() -> None:
    complete = format_savings_goal(
        {"name": "ซื้อหนังสือ", "target": 1000, "saved": 1200}
    )
    assert "100%" in complete
    assert "สำเร็จตามเป้าหมายแล้ว" in complete

    zero_target = format_savings_goal({"name": "ยังไม่กำหนด", "target": 0, "saved": 0})
    assert "ความคืบหน้า: 0%" in zero_target


def test_help_unknown_and_long_messages_are_safe() -> None:
    assert "ช่วยเหลือ" in format_unknown_message()
    assert "กรุณาระบุจำนวนเงิน" in format_unknown_message("กรุณาระบุจำนวนเงิน")
    assert "จ่าย 50" in format_help_message()
    limited = limit_line_text("ก" * 6000)
    assert len(limited) == 4900
    assert limited.endswith("…")
