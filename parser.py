"""Rule-based Thai command parser for MooToon.

The parser deliberately does not call an LLM.  It accepts an explicit
income/expense verb or a strong deterministic taxonomy match, and leaves
messages unresolved when their type or amount is still ambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import TypeAlias
from zoneinfo import ZoneInfo

from taxonomy import TransactionType, classify_item, fallback_category


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
    description: str
    inference_rule: str


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
_ATTACHED_COMMAND_PREFIXES = {
    "รับ": ("ค่า", "เงิน"),
    "เสีย": ("ค่า", "เงิน"),
}
_NEGATED_TRANSACTION_PHRASES = (
    "ไม่ได้ซื้อ",
    "ไม่ได้จ่าย",
    "ไม่ได้รับ",
    "ไม่ซื้อ",
    "ไม่จ่าย",
    "ไม่รับ",
    "ไม่ขาย",
    "ไม่เคยซื้อ",
    "ไม่เคยจ่าย",
    "ไม่เคยรับ",
    "ไม่เคยขาย",
    "ยังไม่ได้",
    "ยกเลิก",
)
_FUTURE_TRANSACTION_PHRASES = (
    "พรุ่งนี้",
    "มะรืน",
    "จะซื้อ",
    "จะจ่าย",
    "จะได้รับ",
    "จะได้เงิน",
    "จะขาย",
    "กำลังจะซื้อ",
    "กำลังจะจ่าย",
    "กำลังจะรับ",
    "กำลังจะขาย",
    "วางแผนซื้อ",
    "วางแผนจ่าย",
    "วางแผนรับ",
    "วางแผนขาย",
    "สัปดาห์หน้า",
    "เดือนหน้า",
    "ปีหน้า",
)


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


def _invalid_amount_reason(text: str) -> str | None:
    if re.search(rf"(?<![\d/])[-−]\s*{_AMOUNT_PATTERN}", text):
        return "จำนวนเงินต้องมากกว่า 0"
    if re.search(r"(?<!\d)\d+\.\d{3,}(?!\d)", text):
        return "จำนวนเงินมีทศนิยมได้ไม่เกิน 2 ตำแหน่ง"
    if re.search(r"(?<![\d,])\d{1,3},\d{1,2}(?!\d)", text):
        return "รูปแบบจำนวนเงินไม่ถูกต้อง"
    numeric_fragments = re.findall(
        r"(?<![\d.,])(?:[.,]\d+|\d[\d.,]*)(?![\d.,])",
        text,
    )
    for fragment in numeric_fragments:
        if ("." in fragment or "," in fragment) and not re.fullmatch(
            _AMOUNT_PATTERN,
            fragment,
        ):
            return "รูปแบบจำนวนเงินไม่ถูกต้อง"
    return None


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


def _display_text(text: str) -> str:
    """Collapse whitespace while preserving the user's item capitalization."""

    return re.sub(r"\s+", " ", text.strip())


def _leading_command_word(text: str, words: tuple[str, ...]) -> str | None:
    """Return a leading verb without treating Thai substrings as commands."""

    for word in sorted(words, key=len, reverse=True):
        if not text.startswith(word):
            continue
        remainder = text[len(word) :]
        attached_prefixes = _ATTACHED_COMMAND_PREFIXES.get(word)
        if attached_prefixes is None:
            return word
        if (
            not remainder
            or remainder[0].isspace()
            or remainder[0].isdigit()
            or remainder.startswith(("แล้ว", "และ", "จากนั้น"))
            or remainder.startswith(attached_prefixes)
        ):
            return word
    return None


def _contains_linked_second_command(
    text: str,
    first_words: tuple[str, ...],
    second_words: tuple[str, ...],
) -> bool:
    without_relative_date = re.sub(r"^(?:วันนี้|เมื่อวาน)\s*", "", text)
    first = _leading_command_word(without_relative_date, first_words)
    if first is None:
        return False
    remainder = without_relative_date[len(first) :]
    for linked in re.finditer(r"(?:แล้ว|และ|จากนั้น)\s*", remainder):
        if _leading_command_word(remainder[linked.end() :], second_words):
            return True
    return False


def _strip_transaction_words(text: str, leading_word: str | None = None) -> str:
    text = text.strip()
    if leading_word and text.startswith(leading_word):
        text = text[len(leading_word) :]
    text = re.sub(r"\b(?:บาท|หมวด)\b", " ", text)
    cleaned = re.sub(r"\s+", " ", text).strip(" :-")
    cleaned = re.sub(r"^ไป(?:\s+|$)", "", cleaned).strip()
    return re.sub(r"^(?:แล้ว|และ|จากนั้น)\s*", "", cleaned).strip()


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
    display_text = _display_text(text)
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
        if invalid_reason := _invalid_amount_reason(body):
            return _unresolved(invalid_reason)
        amount, remainder, count = _extract_amount(body)
        if count > 1:
            return _unresolved("พบจำนวนเงินมากกว่าหนึ่งค่า")
        if count == 0 or amount is None:
            return _unresolved("กรุณาระบุจำนวนเงินของเป้าหมาย")
        description = re.sub(r"\bบาท\b", " ", remainder).strip(" :-")
        if len(description) > 200:
            return _unresolved("ชื่อเป้าหมายยาวเกิน 200 ตัวอักษร")
        return SavingsGoalCommand(CommandKind.SET_SAVINGS_GOAL, amount, description)

    if normalized.startswith(("ออม ", "เก็บเงิน ")):
        body = re.sub(r"^(?:ออม|เก็บเงิน)\s+", "", normalized)
        if invalid_reason := _invalid_amount_reason(body):
            return _unresolved(invalid_reason)
        amount, remainder, count = _extract_amount(body)
        if count > 1:
            return _unresolved("พบจำนวนเงินมากกว่าหนึ่งค่า")
        if count == 0 or amount is None:
            return _unresolved("กรุณาระบุจำนวนเงินที่ออม")
        if re.sub(r"\bบาท\b", " ", remainder).strip(" :-"):
            return _unresolved("คำสั่งเพิ่มเงินออมมีข้อมูลที่ไม่รู้จัก")
        return SavingsProgressCommand(CommandKind.ADD_SAVINGS, amount)

    if any(phrase in normalized for phrase in _NEGATED_TRANSACTION_PHRASES):
        return _unresolved("ข้อความนี้เป็นการปฏิเสธ จึงยังไม่บันทึก")
    if any(phrase in normalized for phrase in _FUTURE_TRANSACTION_PHRASES):
        return _unresolved("ข้อความนี้ดูเป็นรายการที่ยังไม่เกิดขึ้น จึงยังไม่บันทึก")

    transaction_date, without_date, date_error = _extract_date(
        normalized, _local_date(now)
    )
    if date_error:
        return _unresolved(date_error)
    _, display_without_date, _ = _extract_date(
        display_text,
        _local_date(now),
    )

    expense_word = _leading_command_word(without_date.strip(), _EXPENSE_WORDS)
    income_word = _leading_command_word(without_date.strip(), _INCOME_WORDS)
    expense = expense_word is not None
    income = income_word is not None
    if expense and _contains_linked_second_command(without_date.strip(), _EXPENSE_WORDS, _INCOME_WORDS):
        income = True
    if income and _contains_linked_second_command(without_date.strip(), _INCOME_WORDS, _EXPENSE_WORDS):
        expense = True
    if expense and income:
        return _unresolved("พบทั้งรายรับและรายจ่ายในข้อความเดียวกัน")

    if invalid_reason := _invalid_amount_reason(without_date):
        return _unresolved(invalid_reason)

    amount, without_amount, count = _extract_amount(without_date)
    _, display_without_amount, _ = _extract_amount(display_without_date)
    if count > 1:
        return _unresolved("พบจำนวนเงินมากกว่าหนึ่งค่า")
    if count == 1 and amount is None:
        return _unresolved("จำนวนเงินต้องมากกว่า 0")
    if count == 0:
        item_without_command = _strip_transaction_words(
            display_without_date,
            expense_word or income_word,
        )
        semantic = classify_item(item_without_command)
        if expense or income or semantic is not None:
            item_label = item_without_command.strip(" :-")
            if item_label:
                return _unresolved(f"กรุณาระบุจำนวนเงินของ {item_label}")
            return _unresolved("กรุณาระบุจำนวนเงิน")
        return _unresolved("ไม่รู้จักคำสั่ง", ambiguous=False)
    leading_word = expense_word or income_word
    description = _strip_transaction_words(display_without_amount, leading_word)
    explicit_type: TransactionType | None = None
    if expense:
        explicit_type = "expense"
    elif income:
        explicit_type = "income"

    semantic = classify_item(description, explicit_type=explicit_type)
    if explicit_type is None and semantic is None:
        return _unresolved("กรุณาระบุว่าเป็นรายรับหรือรายจ่าย")

    transaction_type: TransactionType = (
        explicit_type if explicit_type is not None else semantic.transaction_type
    )
    category = (
        semantic.category
        if semantic is not None
        else fallback_category(transaction_type)
    )
    if len(category) > 100:
        return _unresolved("ชื่อหมวดยาวเกิน 100 ตัวอักษร")
    if len(description) > 500:
        return _unresolved("ชื่อรายการยาวเกิน 500 ตัวอักษร")
    kind = (
        CommandKind.EXPENSE
        if transaction_type == "expense"
        else CommandKind.INCOME
    )
    inference_rule = (
        f"explicit.{leading_word}"
        if explicit_type is not None
        else semantic.rule_id
    )
    assert transaction_date is not None
    return TransactionCommand(
        kind,
        amount,
        category,
        transaction_date,
        description or "ไม่ระบุรายการ",
        inference_rule,
    )


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
