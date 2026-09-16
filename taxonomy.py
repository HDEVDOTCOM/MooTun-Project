"""Deterministic transaction taxonomy for short Thai finance messages.

The rules intentionally stay small and explainable.  They are ordered by
strength so action phrases such as ``ขาย`` can override an object phrase such
as ``ข้าว`` in ``ขายข้าว 50``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


TransactionType = Literal["expense", "income"]


@dataclass(frozen=True)
class CategoryRule:
    rule_id: str
    transaction_type: TransactionType
    category: str
    keywords: tuple[str, ...]
    priority: int = 50
    prefix_only: bool = False
    exact_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Classification:
    transaction_type: TransactionType
    category: str
    rule_id: str


CATEGORY_RULES: tuple[CategoryRule, ...] = (
    CategoryRule(
        "expense.rent_context",
        "expense",
        "ที่พัก",
        ("ค่าเช่า",),
        priority=110,
        prefix_only=True,
    ),
    CategoryRule(
        "income.salary",
        "income",
        "เงินเดือน",
        ("เงินเดือน",),
        priority=100,
        prefix_only=True,
    ),
    CategoryRule(
        "income.sale",
        "income",
        "ขายของ",
        ("ขายของ", "ขายสินค้า", "ขายข้าว", "ขาย"),
        priority=100,
        prefix_only=True,
    ),
    CategoryRule(
        "income.side_job",
        "income",
        "งานพิเศษ",
        ("งานพิเศษ", "สอนพิเศษ", "ฟรีแลนซ์"),
        90,
    ),
    CategoryRule(
        "income.received",
        "income",
        "เงินได้รับ",
        ("แม่ให้", "พ่อให้", "ผู้ปกครองให้", "ได้เงิน", "ได้ค่าขนม", "โบนัส"),
        90,
    ),
    CategoryRule(
        "expense.food",
        "expense",
        "อาหาร",
        (
            "ข้าว",
            "อาหาร",
            "กาแฟ",
            "ชา",
            "ขนม",
            "ก๋วยเตี๋ยว",
            "น้ำดื่ม",
            "หมูกระทะ",
        ),
        exact_keywords=("ชา",),
    ),
    CategoryRule(
        "expense.transport",
        "expense",
        "เดินทาง",
        (
            "bts",
            "mrt",
            "เดินทาง",
            "รถเมล์",
            "รถไฟ",
            "แท็กซี่",
            "วินมอเตอร์ไซค์",
            "เติมน้ำมัน",
            "ค่าน้ำมัน",
            "ทางด่วน",
        ),
    ),
    CategoryRule(
        "expense.housing",
        "expense",
        "ที่พัก",
        ("ที่พัก", "ค่าหอ", "ค่าเช่า", "ค่าเช่าห้อง", "หอพัก"),
    ),
    CategoryRule(
        "expense.bills",
        "expense",
        "บิลและบริการ",
        (
            "ค่าไฟ",
            "ค่าน้ำ",
            "บิลและบริการ",
            "ค่าเน็ต",
            "ค่าอินเทอร์เน็ต",
            "ค่าโทรศัพท์",
            "subscription",
            "ค่าสมาชิก",
        ),
    ),
    CategoryRule(
        "expense.education",
        "expense",
        "การศึกษา",
        ("การศึกษา", "ค่าเทอม", "หนังสือเรียน", "หนังสือ", "อุปกรณ์การเรียน", "เรียนพิเศษ"),
    ),
    CategoryRule(
        "expense.health",
        "expense",
        "สุขภาพ",
        ("สุขภาพ", "ค่ายา", "ค่าหมอ", "ค่ารักษา", "โรงพยาบาล", "คลินิก"),
        exact_keywords=("ค่ายา",),
    ),
    CategoryRule(
        "expense.shopping",
        "expense",
        "ชอปปิง",
        ("ชอปปิง", "shopee", "lazada", "เสื้อผ้า", "รองเท้า", "ซื้อของ"),
    ),
    CategoryRule(
        "expense.entertainment",
        "expense",
        "บันเทิง",
        ("บันเทิง", "ดูหนัง", "เกม", "คอนเสิร์ต", "netflix", "spotify"),
    ),
)


def _contains_keyword(
    text: str,
    keyword: str,
    *,
    prefix_only: bool,
    exact_only: bool,
) -> bool:
    """Match Thai phrases by substring and Latin words on token boundaries."""

    if exact_only:
        return bool(re.search(rf"(?:^|\s){re.escape(keyword)}(?=\s|$)", text))
    if prefix_only:
        return text.startswith(keyword)
    if keyword.isascii() and keyword.isalnum():
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text))
    return keyword in text


def classify_item(
    text: str,
    *,
    explicit_type: TransactionType | None = None,
) -> Classification | None:
    """Return the strongest compatible category rule, or ``None``.

    An explicit transaction type always wins.  Without one, a higher-priority
    intent phrase (for example ``ขาย``) wins over ordinary category nouns.
    Ties across different categories are left unresolved rather than guessed.
    """

    normalized = re.sub(r"\s+", " ", text.strip().lower())
    if not normalized:
        return None

    candidates: list[tuple[int, int, CategoryRule]] = []
    for rule in CATEGORY_RULES:
        if explicit_type is not None and rule.transaction_type != explicit_type:
            continue
        matched = [
            word
            for word in rule.keywords
            if _contains_keyword(
                normalized,
                word,
                prefix_only=rule.prefix_only,
                exact_only=word in rule.exact_keywords,
            )
        ]
        if matched:
            candidates.append((rule.priority, max(map(len, matched)), rule))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_priority, best_length, best_rule = candidates[0]
    tied_categories = {
        rule.category
        for priority, length, rule in candidates
        if priority == best_priority and length == best_length
    }
    if len(tied_categories) > 1:
        return None

    return Classification(
        best_rule.transaction_type,
        best_rule.category,
        best_rule.rule_id,
    )


def fallback_category(transaction_type: TransactionType) -> str:
    return "รายรับอื่นๆ" if transaction_type == "income" else "อื่นๆ"


__all__ = [
    "CATEGORY_RULES",
    "CategoryRule",
    "Classification",
    "TransactionType",
    "classify_item",
    "fallback_category",
]
