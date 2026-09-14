"""Rule-based Thai command parser for MooToon.

The parser deliberately does not call an LLM.  It only returns a transaction
when both its direction and amount can be established from the message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import TypeAlias
from zoneinfo import ZoneInfo


BANGKOK = ZoneInfo("Asia/Bangkok")


class CommandKind(str, Enum):
    EXPENSE = "expense"
    INCOME = "income"
    HELP = "help"
    RECENT = "recent"
    MONTHLY_SUMMARY = "monthly_summary"
    DELETE_LATEST = "delete_latest"
    SET_SAVINGS_GOAL = "set_savings_goal"
    ADD_SAVINGS = "add_savings"
    SAVINGS_STATUS = "savings_status"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class TransactionCommand:
    kind: CommandKind
    amount: Decimal
    category: str
    transaction_date: date


@dataclass(frozen=True)
class SimpleCommand:
    kind: CommandKind


@dataclass(frozen=True)
class SavingsGoalCommand:
    kind: CommandKind
    amount: Decimal
    description: str


@dataclass(frozen=True)
class SavingsProgressCommand:
    kind: CommandKind
    amount: Decimal


@dataclass(frozen=True)
class UnresolvedCommand:
    kind: CommandKind
    reason: str


ParsedCommand: TypeAlias = (
    TransactionCommand
    | SimpleCommand
    | SavingsGoalCommand
    | SavingsProgressCommand
    | UnresolvedCommand
)


_AMOUNT_PATTERN = r"(?<![\d.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?![\d.])"
_EXPENSE_WORDS = ("รายจ่าย", "จ่าย", "ซื้อ", "เสีย")
_INCOME_WORDS = ("รายรับ", "ได้รับ", "เงินเข้า", "รับ")
_TRANSACTION_WORDS = sorted(_EXPENSE_WORDS + _INCOME_WORDS, key=len, reverse=True)


def _local_date(now: datetime | date | None) -> date:
    if now is None:
        return datetime.now(BANGKOK).date()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=BANGKOK).date()
        return now.astimezone(BANGKOK).date()
    return now


def _amount(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    return value if value > 0 else None


def _extract_amount(text: str) -> tuple[Decimal | None, str, int]:
    matches = list(re.finditer(_AMOUNT_PATTERN, text))
    if len(matches) != 1:
        return None, text, len(matches)
    match = matches[0]
    value = _amount(match.group())
    cleaned = f"{text[:match.start()]} {text[match.end():]}"
    return value, cleaned, 1


def _parse_written_date(raw: str) -> date | None:
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", raw):
            year, month, day = (int(part) for part in raw.split("-"))
        else:
            day, month, year = (int(part) for part in re.split(r"[/-]", raw))
            if year < 100:
                year += 2000
            elif year >= 2400:  # Thai Buddhist Era, e.g. 14/09/2569.
                year -= 543
        return date(year, month, day)
    except (TypeError, ValueError):
        return None


def _extract_date(text: str, today: date) -> tuple[date | None, str, str | None]:
    relative_words = [word for word in ("วันนี้", "เมื่อวาน") if word in text]
    written = list(
        re.finditer(r"(?<!\d)(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})(?!\d)", text)
    )
    if len(relative_words) + len(written) > 1:
        return None, text, "พบวันที่มากกว่าหนึ่งค่า"

    if written:
        match = written[0]
        parsed = _parse_written_date(match.group())
        if parsed is None:
            return None, text, "วันที่ไม่ถูกต้อง"
        cleaned = f"{text[:match.start()]} {text[match.end():]}"
        return parsed, cleaned, None

    if relative_words:
        word = relative_words[0]
        parsed = today if word == "วันนี้" else today - timedelta(days=1)
        return parsed, text.replace(word, " ", 1), None

    return today, text, None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _starts_with_command_word(text: str, words: tuple[str, ...]) -> bool:
    """Match a leading verb without treating Thai substrings as commands."""

    without_relative_date = re.sub(r"^(?:วันนี้|เมื่อวาน)\s*", "", text)
    return any(without_relative_date.startswith(word) for word in words)


def _contains_linked_second_command(
    text: str,
    first_words: tuple[str, ...],
    second_words: tuple[str, ...],
) -> bool:
    without_relative_date = re.sub(r"^(?:วันนี้|เมื่อวาน)\s*", "", text)
    first = next(
        (word for word in sorted(first_words, key=len, reverse=True) if without_relative_date.startswith(word)),
        None,
    )
    if first is None:
        return False
    remainder = without_relative_date[len(first) :]
    second_pattern = "|".join(re.escape(word) for word in sorted(second_words, key=len, reverse=True))
    return bool(re.match(rf"^(?:แล้ว|และ|จากนั้น)\s*(?:{second_pattern})", remainder))


def _strip_transaction_words(text: str) -> str:
    text = text.strip()
    for word in _TRANSACTION_WORDS:
        if text.startswith(word):
            text = text[len(word) :]
            break
    text = re.sub(r"\b(?:บาท|หมวด)\b", " ", text)
    cleaned = re.sub(r"\s+", " ", text).strip(" :-")
    return re.sub(r"^ไป(?:\s+|$)", "", cleaned).strip()


def _unresolved(reason: str, *, ambiguous: bool = True) -> UnresolvedCommand:
    return UnresolvedCommand(
        kind=CommandKind.AMBIGUOUS if ambiguous else CommandKind.UNKNOWN,
        reason=reason,
    )


def parse_command(text: str, now: datetime | date | None = None) -> ParsedCommand:
    """Parse one Thai chat message into a typed command.

    ``now`` may be a date or datetime for deterministic callers/tests. Aware
    datetimes are converted to Asia/Bangkok; naive datetimes are interpreted as
    Bangkok local time.
    """

    normalized = _normalize(text)
    if not normalized:
        return _unresolved("ไม่มีข้อความให้ประมวลผล", ambiguous=False)

    # Specific commands must be checked before their shorter words (for
    # example, ลบล่าสุด contains ล่าสุด).
    if normalized in {"ลบล่าสุด", "ลบรายการล่าสุด"}:
        return SimpleCommand(CommandKind.DELETE_LATEST)
    if normalized in {"ล่าสุด", "รายการล่าสุด", "ดูรายการล่าสุด"}:
        return SimpleCommand(CommandKind.RECENT)
    if normalized in {"สรุปเดือนนี้", "ดูสรุปเดือนนี้", "รายงานเดือนนี้"}:
        return SimpleCommand(CommandKind.MONTHLY_SUMMARY)
    if normalized in {"ช่วยเหลือ", "ช่วยด้วย", "วิธีใช้", "help", "เมนู"}:
        return SimpleCommand(CommandKind.HELP)
    if normalized in {"เป้าหมาย", "เป้าหมายการออม", "ดูเป้าหมาย"}:
        return SimpleCommand(CommandKind.SAVINGS_STATUS)

    if normalized.startswith(("ตั้งเป้า ", "ตั้งเป้าหมาย ")):
        body = re.sub(r"^ตั้งเป้า(?:หมาย)?\s+", "", normalized)
        amount, remainder, count = _extract_amount(body)
        if count == 0 or amount is None:
            return _unresolved("กรุณาระบุจำนวนเงินของเป้าหมาย")
        if count > 1:
            return _unresolved("พบจำนวนเงินมากกว่าหนึ่งค่า")
        description = re.sub(r"\bบาท\b", " ", remainder).strip(" :-")
        if len(description) > 200:
            return _unresolved("ชื่อเป้าหมายยาวเกิน 200 ตัวอักษร")
        return SavingsGoalCommand(CommandKind.SET_SAVINGS_GOAL, amount, description)

    if normalized.startswith(("ออม ", "เก็บเงิน ")):
        body = re.sub(r"^(?:ออม|เก็บเงิน)\s+", "", normalized)
        amount, remainder, count = _extract_amount(body)
        if count == 0 or amount is None:
            return _unresolved("กรุณาระบุจำนวนเงินที่ออม")
        if count > 1:
            return _unresolved("พบจำนวนเงินมากกว่าหนึ่งค่า")
        if re.sub(r"\bบาท\b", " ", remainder).strip(" :-"):
            return _unresolved("คำสั่งเพิ่มเงินออมมีข้อมูลที่ไม่รู้จัก")
        return SavingsProgressCommand(CommandKind.ADD_SAVINGS, amount)

    expense = _starts_with_command_word(normalized, _EXPENSE_WORDS)
    income = _starts_with_command_word(normalized, _INCOME_WORDS)
    if expense and _contains_linked_second_command(normalized, _EXPENSE_WORDS, _INCOME_WORDS):
        income = True
    if income and _contains_linked_second_command(normalized, _INCOME_WORDS, _EXPENSE_WORDS):
        expense = True
    has_any_amount = bool(re.search(_AMOUNT_PATTERN, normalized))

    if expense and income:
        return _unresolved("พบทั้งรายรับและรายจ่ายในข้อความเดียวกัน")
    if not expense and not income:
        if has_any_amount:
            return _unresolved("กรุณาระบุว่าเป็นรายรับหรือรายจ่าย")
        return _unresolved("ไม่รู้จักคำสั่ง", ambiguous=False)

    transaction_date, without_date, date_error = _extract_date(
        normalized, _local_date(now)
    )
    if date_error:
        return _unresolved(date_error)

    amount, without_amount, count = _extract_amount(without_date)
    if count == 0 or amount is None:
        return _unresolved("กรุณาระบุจำนวนเงิน")
    if count > 1:
        return _unresolved("พบจำนวนเงินมากกว่าหนึ่งค่า")

    category = _strip_transaction_words(without_amount) or "อื่นๆ"
    if len(category) > 100:
        return _unresolved("ชื่อหมวดยาวเกิน 100 ตัวอักษร")
    kind = CommandKind.EXPENSE if expense else CommandKind.INCOME
    assert transaction_date is not None
    return TransactionCommand(kind, amount, category, transaction_date)


__all__ = [
    "CommandKind",
    "ParsedCommand",
    "SavingsGoalCommand",
    "SavingsProgressCommand",
    "SimpleCommand",
    "TransactionCommand",
    "UnresolvedCommand",
    "parse_command",
]
