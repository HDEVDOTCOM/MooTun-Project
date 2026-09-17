"""Thai text-message formatting for MooToon.

The formatters accept ``TypedDict``-shaped mappings, but at runtime ordinary
dictionaries are enough.  This keeps database and webhook integration simple.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final, Iterable, Mapping, NotRequired, TypedDict


SAFE_LINE_TEXT_LIMIT: Final = 4_900


class Transaction(TypedDict):
    amount: int | float | Decimal | str
    type: str
    category: NotRequired[str]
    date: NotRequired[date | datetime | str]
    note: NotRequired[str]


class CategoryTotal(TypedDict):
    category: str
    amount: int | float | Decimal | str


class MonthlySummary(TypedDict):
    income: int | float | Decimal | str
    expense: int | float | Decimal | str
    month: NotRequired[str]
    balance: NotRequired[int | float | Decimal | str]
    transaction_count: NotRequired[int]
    top_categories: NotRequired[list[CategoryTotal]]


class SavingsGoal(TypedDict):
    name: str
    target: int | float | Decimal | str
    saved: int | float | Decimal | str
    due_date: NotRequired[date | datetime | str]


def format_amount(value: object) -> str:
    """Format a baht amount with separators and only meaningful decimals."""

    amount = _decimal(value)
    if amount == amount.to_integral_value():
        return f"{amount:,.0f}"
    rendered = f"{amount:,.2f}".rstrip("0").rstrip(".")
    return rendered


def buddhist_year(year: int) -> int:
    return year + 543


def format_buddhist_date(value: date | datetime) -> str:
    return f"{value.day:02d}/{value.month:02d}/{buddhist_year(value.year)}"


def format_buddhist_month(value: date | datetime) -> str:
    return f"เดือน {value.month}/{buddhist_year(value.year)}"


def format_transaction_confirmation(transaction: Mapping[str, object]) -> str:
    """Confirm one saved income or expense transaction in Thai."""

    kind = _kind(transaction.get("type"))
    amount = abs(_decimal(transaction.get("amount", 0)))
    category = _clean(transaction.get("category"), "ไม่ระบุหมวด")
    happened_on = _format_date(transaction.get("date"))

    lines = [
        f"✅ บันทึก{kind}แล้ว",
    ]
    note = _clean(transaction.get("note"), "")
    if note and note != "ไม่ระบุรายการ":
        lines.append(f"รายการ: {note}")
    lines.extend([
        f"จำนวน: {format_amount(amount)} บาท",
        f"หมวด: {category}",
    ])
    if happened_on:
        lines.append(f"วันที่: {happened_on}")
    lines.append("พิมพ์ “ลบล่าสุด” หากบันทึกผิด")
    return limit_line_text("\n".join(lines))


def format_recent_transactions(
    transactions: Iterable[Mapping[str, object]],
    *,
    limit: int = 10,
) -> str:
    """Render recent transactions as a compact numbered list."""

    if limit <= 0:
        return "ยังไม่มีรายการให้แสดง"

    items = list(transactions)[:limit]
    if not items:
        return "ยังไม่มีรายการรายรับรายจ่าย ลองพิมพ์ “ข้าว 50”"

    lines = ["🧾 รายการล่าสุด"]
    for index, item in enumerate(items, start=1):
        kind = _kind(item.get("type"))
        sign = "+" if kind == "รายรับ" else "−"
        icon = "🟢" if kind == "รายรับ" else "🔴"
        amount = format_amount(abs(_decimal(item.get("amount", 0))))
        category = _clean(item.get("category"), "ไม่ระบุหมวด")
        happened_on = _format_date(item.get("date")) or "ไม่ระบุวันที่"
        lines.append(f"{index}. {icon} {happened_on} · {category} · {sign}{amount} บาท")

    return limit_line_text("\n".join(lines))


def format_monthly_summary(summary: Mapping[str, object]) -> str:
    """Render totals and optional leading expense categories for a month."""

    income = _decimal(summary.get("income", 0))
    expense = abs(_decimal(summary.get("expense", 0)))
    balance_value = summary.get("balance")
    balance = (
        _decimal(balance_value) if balance_value is not None else income - expense
    )
    month = _clean(summary.get("month"), "เดือนนี้")

    lines = [
        f"📊 สรุป{month}",
        f"รายรับ: {format_amount(income)} บาท",
        f"รายจ่าย: {format_amount(expense)} บาท",
        f"คงเหลือ: {format_amount(balance)} บาท",
    ]
    count = summary.get("transaction_count")
    if isinstance(count, int) and count >= 0:
        lines.append(f"ทั้งหมด: {count:,} รายการ")

    categories = summary.get("top_categories")
    if isinstance(categories, list) and categories:
        lines.append("\nหมวดรายจ่ายสูงสุด")
        for item in categories[:3]:
            if not isinstance(item, Mapping):
                continue
            category = _clean(item.get("category"), "ไม่ระบุหมวด")
            amount = format_amount(abs(_decimal(item.get("amount", 0))))
            lines.append(f"• {category}: {amount} บาท")

    return limit_line_text("\n".join(lines))


def format_savings_goal(goal: Mapping[str, object]) -> str:
    """Render one savings goal with bounded progress and remaining amount."""

    name = _clean(goal.get("name"), "เป้าหมายการออม")
    target = max(_decimal(goal.get("target", 0)), Decimal("0"))
    saved = max(_decimal(goal.get("saved", 0)), Decimal("0"))
    remaining = max(target - saved, Decimal("0"))
    percent = Decimal("0") if target == 0 else saved * 100 / target
    display_percent = min(percent, Decimal("100"))

    lines = [
        f"🎯 {name}",
        f"ออมแล้ว: {format_amount(saved)} / {format_amount(target)} บาท",
        f"ความคืบหน้า: {format_amount(display_percent)}%",
    ]
    if remaining:
        lines.append(f"เหลืออีก: {format_amount(remaining)} บาท")
    elif target > 0:
        lines.append("สำเร็จตามเป้าหมายแล้ว 🎉")

    due_date = _format_date(goal.get("due_date"))
    if due_date:
        lines.append(f"กำหนด: {due_date}")
    return limit_line_text("\n".join(lines))


# Both names are convenient at integration sites.
format_savings_progress = format_savings_goal


def format_unknown_message(reason: str | None = None) -> str:
    """Explain an unresolved command, optionally including the parser reason."""

    heading = _clean(reason, "ยังไม่เข้าใจข้อความนี้")
    return (
        f"{heading}\nลองพิมพ์ เช่น\n"
        "• ข้าว 50\n"
        "• เงินเดือน 20000\n"
        "• จ่าย 500\n"
        "• สรุปเดือนนี้\n\n"
        "พิมพ์ “ช่วยเหลือ” เพื่อดูคำสั่งทั้งหมด"
    )


def format_help_message() -> str:
    return (
        "🐷 วิธีใช้หมูตุ๋น\n\n"
        "พิมพ์รายการและจำนวนเงินได้เลย\n"
        "จดรายจ่าย: ข้าว 50 หรือ BTS 47\n"
        "จดรายรับ: เงินเดือน 20000 หรือ แม่ให้ 500\n"
        "ระบุเองได้: จ่าย 50 หรือ รับ 500\n"
        "ดูรายการ: รายการล่าสุด\n"
        "ดูสรุป: สรุปเดือนนี้\n"
        "ลบรายการ: ลบล่าสุด\n"
        "ตั้งเป้าหมาย: ตั้งเป้า 1500 ซื้อหนังสือ\n"
        "เพิ่มเงินออม: ออม 100\n"
        "ดูเป้าหมาย: เป้าหมายการออม"
    )


def limit_line_text(text: str, limit: int = SAFE_LINE_TEXT_LIMIT) -> str:
    """Keep generated text under LINE's limit without cutting silently."""

    if limit < 2:
        raise ValueError("limit must be at least 2")
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        return Decimal("0")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return number if number.is_finite() else Decimal("0")


def _kind(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"income", "รายรับ", "รับ", "+", "credit"}:
        return "รายรับ"
    return "รายจ่าย"


def _clean(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value).strip().replace("\n", " ")
    return text or fallback


def _format_date(value: object) -> str:
    if isinstance(value, datetime):
        return format_buddhist_date(value)
    if isinstance(value, date):
        return format_buddhist_date(value)
    return _clean(value, "")
