"""Rule-based Thai command parser for MooToon.

The parser deliberately does not call an LLM.  It accepts an explicit
income/expense verb or a strong deterministic taxonomy match, and leaves
messages unresolved when their type or amount is still ambiguous.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import TypeAlias
from zoneinfo import ZoneInfo

from taxonomy import (
    SEMANTIC_ACTION_PHRASES,
    TransactionType,
    classify_item,
    fallback_category,
    has_supported_item_prefix,
)


BANGKOK = ZoneInfo("Asia/Bangkok")


class CommandKind(str, Enum):
    EXPENSE = "expense"
    INCOME = "income"
    HELP = "help"
    RECENT = "recent"
    MONTHLY_SUMMARY = "monthly_summary"
    DELETE_LATEST = "delete_latest"
    EDIT_LATEST = "edit_latest"
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
class EditLatestCommand:
    kind: CommandKind
    transaction_type: TransactionType
    amount: Decimal
    category: str
    description: str
    transaction_date: date
    explicit_occurred_on: bool
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


@dataclass(frozen=True)
class UnresolvedEditCommand(UnresolvedCommand):
    """An edit-prefixed command that cannot safely replace a transaction."""


@dataclass(frozen=True)
class IncompleteCommand:
    transaction_type: TransactionType | None
    amount: Decimal | None
    category: str | None
    transaction_date: date
    description: str | None
    inference_rule: str | None


ParsedCommand: TypeAlias = (
    TransactionCommand
    | EditLatestCommand
    | IncompleteCommand
    | SimpleCommand
    | SavingsGoalCommand
    | SavingsProgressCommand
    | UnresolvedCommand
)


_AMOUNT_PATTERN = r"(?<![\d.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?![\d.])"
_WRITTEN_DATE_PATTERN = r"(?<!\d)(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})(?!\d)"
_EXPENSE_WORDS = ("รายจ่าย", "จ่าย", "ซื้อ", "เสีย")
_INCOME_WORDS = ("รายรับ", "ได้รับ", "เงินเข้า", "รับ")
_BOUNDARY_SENSITIVE_COMMANDS = {"รับ", "เสีย", "ได้รับ"}
_LINK_WORDS = ("แล้ว", "และ", "จากนั้น")
_GUARD_SIGNALS = tuple(
    sorted(
        {
            *_EXPENSE_WORDS,
            *_INCOME_WORDS,
            *(phrase for phrases in SEMANTIC_ACTION_PHRASES.values() for phrase in phrases),
            "โบนัส",
            "เงินเดือน",
            "ให้",
        },
        key=len,
        reverse=True,
    )
)
_TRANSACTION_SIGNAL_PATTERN = "(?:" + "|".join(map(re.escape, _GUARD_SIGNALS)) + ")"
_NEGATED_TRANSACTION_PATTERNS = (
    rf"(?:ไม่ได้|ไม่เคย|ไม่){_TRANSACTION_SIGNAL_PATTERN}",
    rf"{_TRANSACTION_SIGNAL_PATTERN}(?:ยัง)?(?:ไม่ได้|ไม่ออก)",
    r"(?:เงินเดือน|โบนัส|รายรับ)(?:ยัง)?ไม่(?:เคย)?(?:เข้า|ได้|ออก)",
    r"ยังไม่ได้",
    r"ยกเลิก",
)
_FUTURE_TRANSACTION_PATTERNS = (
    rf"(?:กำลังจะ|วางแผน|จะ)(?:ไป)?{_TRANSACTION_SIGNAL_PATTERN}",
    r"(?:เงินเดือน|โบนัส|รายรับ)(?:กำลัง)?จะ(?:เข้า|ได้|ออก)",
    r"(?:พรุ่งนี้|มะรืน|สัปดาห์หน้า|เดือนหน้า|ปีหน้า)",
)
_CLAUSE_NEGATED_ACTION_PATTERN = (
    rf"{_TRANSACTION_SIGNAL_PATTERN}.+(?:ยัง)?(?:ไม่ได้|ไม่ออก)"
)

# Phase 1C edit prefixes demand whitespace so ordinary items such as "แก้ว 60"
# or "แก้ข้าว 60" are never reinterpreted as edits. "แก้ไข " is matched first.
_EDIT_PREFIX_PATTERN = re.compile(r"^(แก้ไข|แก้)\s+")


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


def _extract_amount(
    text: str,
    *,
    for_analysis: bool = False,
) -> tuple[Decimal | None, str, int]:
    matches = list(re.finditer(_AMOUNT_PATTERN, text))
    if len(matches) != 1:
        return None, text, len(matches)
    match = matches[0]
    value = _amount(match.group())
    start = match.start()
    end = match.end()
    if for_analysis:
        while start > 0 and _is_analysis_separator(text[start - 1]):
            start -= 1
        while end < len(text) and _is_analysis_separator(text[end]):
            end += 1
        if text.startswith("บาท", end):
            end += len("บาท")
            while end < len(text) and _is_analysis_separator(text[end]):
                end += 1
    cleaned = f"{text[:start]} {text[end:]}"
    if for_analysis:
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return value, cleaned, 1


def _is_analysis_separator(character: str) -> bool:
    return character.isspace() or unicodedata.category(character).startswith("P")


def _normalize_intent_analysis(text: str) -> str:
    normalized = "".join(
        " " if _is_analysis_separator(character) else character
        for character in text
    )
    return re.sub(r"\s+", " ", normalized).strip()


def _invalid_amount_reason(text: str) -> str | None:
    if re.search(rf"(?<![\d/])[-−]\s*{_AMOUNT_PATTERN}", text):
        return "จำนวนเงินต้องมากกว่า 0"
    if re.search(r"(?<!\d)\d+\.\d{3,}(?!\d)", text):
        return "จำนวนเงินมีทศนิยมได้ไม่เกิน 2 ตำแหน่ง"
    if re.search(r"(?<![\d,])\d{1,3},\d{1,2}(?!\d)", text):
        return "รูปแบบจำนวนเงินไม่ถูกต้อง"
    numeric_fragments = re.findall(r"[.,]*\d[\d.,]*", text)
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
    written = list(re.finditer(_WRITTEN_DATE_PATTERN, text))
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


def _has_explicit_date(text: str) -> bool:
    return any(word in text for word in ("วันนี้", "เมื่อวาน")) or bool(
        re.search(_WRITTEN_DATE_PATTERN, text)
    )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _display_text(text: str) -> str:
    """Collapse whitespace while preserving the user's item capitalization."""

    return re.sub(r"\s+", " ", text.strip())


def _transaction_intent_rejection(text: str) -> str | None:
    intent_text = re.sub(r"\s+", "", _normalize_intent_analysis(text))
    if any(re.search(pattern, intent_text) for pattern in _NEGATED_TRANSACTION_PATTERNS):
        return "ข้อความนี้เป็นการปฏิเสธ จึงยังไม่บันทึก"

    clauses = re.split("|".join(map(re.escape, _LINK_WORDS)), intent_text)
    if any(re.search(_CLAUSE_NEGATED_ACTION_PATTERN, clause) for clause in clauses):
        return "ข้อความนี้เป็นการปฏิเสธ จึงยังไม่บันทึก"

    if any(re.search(pattern, intent_text) for pattern in _FUTURE_TRANSACTION_PATTERNS):
        return "ข้อความนี้ดูเป็นรายการที่ยังไม่เกิดขึ้น จึงยังไม่บันทึก"
    return None


def _leading_command_word(text: str, words: tuple[str, ...]) -> str | None:
    """Return a leading verb only at a supported command/item boundary."""

    for word in sorted(words, key=len, reverse=True):
        if not text.startswith(word):
            continue
        remainder = text[len(word) :]
        if word not in _BOUNDARY_SENSITIVE_COMMANDS:
            return word
        if (
            not remainder
            or remainder[0].isspace()
            or remainder.startswith(_LINK_WORDS)
            or has_supported_item_prefix(remainder)
        ):
            return word
    return None


def _leading_action_direction(text: str) -> TransactionType | None:
    normalized = re.sub(
        r"^(?:(?:ก็|ไป)\s*|บาท(?:\s+|$))+",
        "",
        text.strip(),
    )
    if _leading_command_word(normalized, _EXPENSE_WORDS):
        return "expense"
    if _leading_command_word(normalized, _INCOME_WORDS):
        return "income"
    for direction, phrases in SEMANTIC_ACTION_PHRASES.items():
        if any(normalized.startswith(phrase) for phrase in phrases):
            return direction
    return None


def _has_linked_direction_conflict(text: str) -> bool:
    clauses = re.split(
        r"(?:แล้ว|และ|จากนั้น)\s*",
        _normalize_intent_analysis(text),
    )
    if len(clauses) < 2:
        return False
    directions = {
        direction
        for clause in clauses
        if (direction := _leading_action_direction(clause)) is not None
    }
    return len(directions) > 1


def _has_unsupported_boundary_sensitive_command_prefix(text: str) -> bool:
    normalized = text.strip()
    if not any(normalized.startswith(word) for word in _BOUNDARY_SENSITIVE_COMMANDS):
        return False
    if _leading_command_word(normalized, tuple(_BOUNDARY_SENSITIVE_COMMANDS)):
        return False
    return not any(
        normalized.startswith(phrase)
        for phrases in SEMANTIC_ACTION_PHRASES.values()
        for phrase in phrases
    )


def _strip_transaction_words(text: str, leading_word: str | None = None) -> str:
    text = text.strip()
    text = re.sub(r"^บาท\s*", "", text)
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


def _unresolved_edit(reason: str) -> UnresolvedEditCommand:
    return UnresolvedEditCommand(CommandKind.AMBIGUOUS, reason)


FOLLOWUP_CONFLICT_REASON = "ข้อมูลนี้ขัดกับรายการที่ค้างไว้ครับ กรุณาส่งข้อมูลที่ขาด หรือพิมพ์ \"ยกเลิก\""

EDIT_MISSING_DETAILS_REASON = "กรุณาระบุข้อมูลใหม่หลังคำว่า \"แก้ไข\" ครับ"
EDIT_INCOMPLETE_REASON = "คำสั่งแก้ไขต้องระบุรายการและจำนวนเงินให้ครบในข้อความเดียวครับ"


def parse_command(text: str, now: datetime | date | None = None) -> ParsedCommand:
    """Parse one Thai chat message into a typed command.

    Edit prefixes (``แก้ `` / ``แก้ไข ``) are intercepted here and parsed with the
    bounded ``_parse_command_core`` helper so edit parsing never recurses into
    itself.  ``now`` may be a date or datetime for deterministic callers/tests.
    Aware datetimes are converted to Asia/Bangkok; naive datetimes are
    interpreted as Bangkok local time.
    """

    edit_text = text.lstrip()
    if _EDIT_PREFIX_PATTERN.match(edit_text):
        return _parse_edit_command(edit_text, now)
    return _parse_command_core(text, now)


def _parse_edit_command(text: str, now: datetime | date | None) -> ParsedCommand:
    """Build an ``EditLatestCommand`` from a validated edit prefix."""

    match = _EDIT_PREFIX_PATTERN.match(text)
    assert match is not None
    remainder = text[match.end() :].strip()
    if not remainder:
        return _unresolved_edit(EDIT_MISSING_DETAILS_REASON)

    evidence = _parse_command_core(remainder, now)
    if isinstance(evidence, UnresolvedCommand):
        return _unresolved_edit(evidence.reason)
    if not isinstance(evidence, TransactionCommand):
        return _unresolved_edit(EDIT_INCOMPLETE_REASON)

    transaction_type: TransactionType = (
        "income" if evidence.kind == CommandKind.INCOME else "expense"
    )
    return EditLatestCommand(
        CommandKind.EDIT_LATEST,
        transaction_type,
        evidence.amount,
        evidence.category,
        evidence.description,
        evidence.transaction_date,
        _has_explicit_date(remainder),
        evidence.inference_rule,
    )


def _parse_command_core(text: str, now: datetime | date | None = None) -> ParsedCommand:
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

    if intent_rejection := _transaction_intent_rejection(normalized):
        return _unresolved(intent_rejection)

    transaction_date, without_date, date_error = _extract_date(
        normalized, _local_date(now)
    )
    if date_error:
        return _unresolved(date_error)
    _, display_without_date, _ = _extract_date(
        display_text,
        _local_date(now),
    )

    if invalid_reason := _invalid_amount_reason(without_date):
        return _unresolved(invalid_reason)

    amount, without_amount, count = _extract_amount(without_date)
    _, intent_without_amount, _ = _extract_amount(
        without_date,
        for_analysis=True,
    )
    _, display_without_amount, _ = _extract_amount(display_without_date)
    if count > 1:
        return _unresolved("พบจำนวนเงินมากกว่าหนึ่งค่า")
    if count == 1 and amount is None:
        return _unresolved("จำนวนเงินต้องมากกว่า 0")
    if intent_rejection := _transaction_intent_rejection(intent_without_amount):
        return _unresolved(intent_rejection)
    command_text = (
        intent_without_amount.strip() if count == 1 else without_date.strip()
    )
    command_text = re.sub(r"^บาท\s*", "", command_text)
    expense_word = _leading_command_word(command_text, _EXPENSE_WORDS)
    income_word = _leading_command_word(command_text, _INCOME_WORDS)
    expense = expense_word is not None
    income = income_word is not None
    if _has_linked_direction_conflict(command_text):
        return _unresolved("พบทั้งรายรับและรายจ่ายในข้อความเดียวกัน")
    if _has_unsupported_boundary_sensitive_command_prefix(command_text):
        return _unresolved("กรุณาระบุว่าเป็นรายรับหรือรายจ่าย")
    explicit_type: TransactionType | None = None
    if expense:
        explicit_type = "expense"
    elif income:
        explicit_type = "income"

    if count == 0:
        item_without_command = _strip_transaction_words(
            display_without_date,
            expense_word or income_word,
        )
        semantic = classify_item(item_without_command, explicit_type=explicit_type)
        if expense or income or semantic is not None:
            item_label = item_without_command.strip(" :-")
            if len(item_label) > 500:
                return _unresolved("ชื่อรายการยาวเกิน 500 ตัวอักษร")
            transaction_type = (
                explicit_type if explicit_type is not None else semantic.transaction_type
            )
            category = (
                semantic.category
                if semantic is not None
                else fallback_category(transaction_type)
            )
            rule = (
                f"explicit.{expense_word or income_word}"
                if explicit_type is not None
                else semantic.rule_id
            )
            assert transaction_date is not None
            return IncompleteCommand(
                transaction_type,
                None,
                category,
                transaction_date,
                item_label or None,
                rule,
            )
        return _unresolved("ไม่รู้จักคำสั่ง", ambiguous=False)
    leading_word = expense_word or income_word
    description = _strip_transaction_words(display_without_amount, leading_word)

    semantic = classify_item(description, explicit_type=explicit_type)
    if explicit_type is None and semantic is None:
        if not description:
            assert transaction_date is not None
            return IncompleteCommand(
                None,
                amount,
                None,
                transaction_date,
                None,
                None,
            )
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


def parse_followup(
    draft: IncompleteCommand,
    text: str,
    *,
    now: datetime | date,
) -> TransactionCommand | IncompleteCommand | UnresolvedCommand:
    """Safety-check and merge structured evidence into an incomplete transaction."""

    evidence = parse_command(text, now=now)
    if isinstance(
        evidence,
        (SimpleCommand, SavingsGoalCommand, SavingsProgressCommand, EditLatestCommand),
    ):
        return _unresolved("ยังไม่มีข้อมูลธุรกรรมที่ใช้เติมรายการ")
    if isinstance(evidence, UnresolvedCommand):
        return evidence

    if isinstance(evidence, TransactionCommand):
        evidence_type: TransactionType = evidence.kind.value
        evidence_amount = evidence.amount
        evidence_category: str | None = evidence.category
        evidence_description = (
            None if evidence.description == "ไม่ระบุรายการ" else evidence.description
        )
        evidence_rule: str | None = evidence.inference_rule
        evidence_date = evidence.transaction_date
    else:
        evidence_type = evidence.transaction_type
        evidence_amount = evidence.amount
        evidence_category = evidence.category
        evidence_description = evidence.description
        evidence_rule = evidence.inference_rule
        evidence_date = evidence.transaction_date

    if _has_explicit_date(text) and evidence_date != draft.transaction_date:
        return _unresolved(FOLLOWUP_CONFLICT_REASON)
    if (
        draft.transaction_type is not None
        and evidence_type is not None
        and draft.transaction_type != evidence_type
        and not (
            (draft.inference_rule or "").startswith("explicit.")
            and not (evidence_rule or "").startswith("explicit.")
        )
    ):
        return _unresolved(FOLLOWUP_CONFLICT_REASON)
    if (
        draft.amount is not None
        and evidence_amount is not None
        and draft.amount != evidence_amount
    ):
        return _unresolved(FOLLOWUP_CONFLICT_REASON)
    if (
        draft.description is not None
        and evidence_description is not None
        and draft.description != evidence_description
    ):
        return _unresolved(FOLLOWUP_CONFLICT_REASON)

    transaction_type = draft.transaction_type or evidence_type
    amount = draft.amount or evidence_amount
    description = draft.description or evidence_description
    changed = (
        transaction_type != draft.transaction_type
        or amount != draft.amount
        or description != draft.description
    )
    if not changed:
        return _unresolved("ยังไม่มีข้อมูลใหม่ที่ใช้เติมรายการ")

    category = draft.category or evidence_category
    inference_rule = draft.inference_rule or evidence_rule
    if transaction_type is not None and description:
        classification = classify_item(description, explicit_type=transaction_type)
        if classification is not None:
            category = classification.category
            inference_rule = inference_rule or classification.rule_id
    if transaction_type is not None and category is None:
        category = fallback_category(transaction_type)

    if transaction_type is None or amount is None:
        return IncompleteCommand(
            transaction_type,
            amount,
            category,
            draft.transaction_date,
            description,
            inference_rule,
        )

    kind = (
        CommandKind.EXPENSE if transaction_type == "expense" else CommandKind.INCOME
    )
    return TransactionCommand(
        kind,
        amount,
        category or fallback_category(transaction_type),
        draft.transaction_date,
        description or "ไม่ระบุรายการ",
        inference_rule or "followup.explicit_direction",
    )


__all__ = [
    "CommandKind",
    "EDIT_INCOMPLETE_REASON",
    "EDIT_MISSING_DETAILS_REASON",
    "EditLatestCommand",
    "FOLLOWUP_CONFLICT_REASON",
    "IncompleteCommand",
    "ParsedCommand",
    "SavingsGoalCommand",
    "SavingsProgressCommand",
    "SimpleCommand",
    "TransactionCommand",
    "UnresolvedCommand",
    "UnresolvedEditCommand",
    "parse_command",
    "parse_followup",
]
