"""Repository operations for transactions and savings goals.

Every public query requires ``line_user_id`` so one LINE user's data cannot be
returned or modified through another user's request.
"""

from __future__ import annotations

from calendar import monthrange
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import init_db, session_scope
from models import PendingTransaction, ProcessedWebhookEvent, SavingsGoal, Transaction


MoneyInput = Decimal | int | str
PENDING_TRANSACTION_TTL = timedelta(hours=1)


class PendingTransactionConflictError(RuntimeError):
    """A pending draft changed while the current operation was in progress."""


def baht_to_satang(amount: MoneyInput) -> int:
    """Convert baht to satang without any floating-point arithmetic."""

    if isinstance(amount, bool) or isinstance(amount, float):
        raise TypeError("amount must be Decimal, int, or a decimal string")
    try:
        value = Decimal(amount)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("amount must be a valid number") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be greater than zero")
    satang = value * 100
    if satang != satang.to_integral_value():
        raise ValueError("amount cannot have more than two decimal places")
    return int(satang)


def _validate_user_id(line_user_id: str) -> str:
    value = line_user_id.strip()
    if not value:
        raise ValueError("line_user_id is required")
    return value


def _session_context(session: Session | None):
    return nullcontext(session) if session is not None else session_scope()


def add_transaction(
    line_user_id: str,
    transaction_type: str,
    amount: MoneyInput,
    category: str,
    *,
    description: str | None = None,
    occurred_on: date | datetime | None = None,
    session: Session | None = None,
) -> Transaction:
    user_id = _validate_user_id(line_user_id)
    kind = transaction_type.strip().lower()
    if kind not in {"income", "expense"}:
        raise ValueError("transaction_type must be 'income' or 'expense'")
    category_value = category.strip()
    if not category_value:
        raise ValueError("category is required")
    if isinstance(occurred_on, datetime):
        occurred_on = occurred_on.date()

    item = Transaction(
        line_user_id=user_id,
        transaction_type=kind,
        amount_satang=baht_to_satang(amount),
        category=category_value,
        description=description.strip() if description and description.strip() else None,
        occurred_on=occurred_on or date.today(),
    )
    with _session_context(session) as db:
        db.add(item)
        db.flush()
    return item


def list_recent_transactions(
    line_user_id: str,
    limit: int = 10,
    *,
    session: Session | None = None,
) -> list[Transaction]:
    user_id = _validate_user_id(line_user_id)
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    statement = (
        select(Transaction)
        .where(Transaction.line_user_id == user_id)
        .order_by(
            Transaction.occurred_on.desc(),
            Transaction.created_at.desc(),
            Transaction.id.desc(),
        )
        .limit(limit)
    )
    with _session_context(session) as db:
        return list(db.scalars(statement).all())


def delete_latest_transaction(
    line_user_id: str, *, session: Session | None = None
) -> Transaction | None:
    """Delete the user's most recently entered transaction.

    Entry order is used rather than ``occurred_on`` so a user can add a
    backdated item and immediately undo it with "delete latest".
    """

    user_id = _validate_user_id(line_user_id)
    statement = (
        select(Transaction)
        .where(Transaction.line_user_id == user_id)
        .order_by(
            Transaction.created_at.desc(),
            Transaction.id.desc(),
        )
        .limit(1)
    )
    with _session_context(session) as db:
        item = db.scalar(statement)
        if item is not None:
            db.delete(item)
            db.flush()
        return item


def monthly_summary(
    line_user_id: str,
    year: int,
    month: int,
    *,
    session: Session | None = None,
) -> dict[str, Any]:
    user_id = _validate_user_id(line_user_id)
    try:
        start = date(year, month, 1)
    except ValueError as exc:
        raise ValueError("year and month must form a valid calendar month") from exc
    end = date(year, month, monthrange(year, month)[1])

    statement = (
        select(
            Transaction.transaction_type,
            func.coalesce(func.sum(Transaction.amount_satang), 0),
            func.count(Transaction.id),
        )
        .where(
            Transaction.line_user_id == user_id,
            Transaction.occurred_on >= start,
            Transaction.occurred_on <= end,
        )
        .group_by(Transaction.transaction_type)
    )
    with _session_context(session) as db:
        rows = db.execute(statement).all()

    totals = {"income": 0, "expense": 0}
    count = 0
    for transaction_type, total_satang, row_count in rows:
        totals[transaction_type] = int(total_satang)
        count += int(row_count)
    income = Decimal(totals["income"]) / Decimal(100)
    expense = Decimal(totals["expense"]) / Decimal(100)
    return {
        "year": year,
        "month": month,
        "income": income,
        "expense": expense,
        "balance": income - expense,
        "transaction_count": count,
    }


def set_savings_goal(
    line_user_id: str,
    title: str,
    target_amount: MoneyInput,
    *,
    deadline: date | None = None,
    session: Session | None = None,
) -> SavingsGoal:
    """Create or update the user's single active goal, retaining progress."""

    user_id = _validate_user_id(line_user_id)
    title_value = title.strip()
    if not title_value:
        raise ValueError("title is required")
    target_satang = baht_to_satang(target_amount)

    with _session_context(session) as db:
        goal = db.scalar(
            select(SavingsGoal).where(SavingsGoal.line_user_id == user_id)
        )
        if goal is None:
            goal = SavingsGoal(
                line_user_id=user_id,
                title=title_value,
                target_satang=target_satang,
                deadline=deadline,
            )
            db.add(goal)
        else:
            goal.title = title_value
            goal.target_satang = target_satang
            goal.deadline = deadline
        db.flush()
        return goal


def get_savings_goal(
    line_user_id: str, *, session: Session | None = None
) -> SavingsGoal | None:
    user_id = _validate_user_id(line_user_id)
    with _session_context(session) as db:
        return db.scalar(
            select(SavingsGoal).where(SavingsGoal.line_user_id == user_id)
        )


def add_savings_progress(
    line_user_id: str,
    amount: MoneyInput,
    *,
    session: Session | None = None,
) -> SavingsGoal:
    user_id = _validate_user_id(line_user_id)
    amount_satang = baht_to_satang(amount)
    with _session_context(session) as db:
        goal = db.scalar(
            select(SavingsGoal).where(SavingsGoal.line_user_id == user_id)
        )
        if goal is None:
            raise LookupError("savings goal not found")
        goal.saved_satang += amount_satang
        db.flush()
        return goal


def get_pending_transaction(
    line_user_id: str,
    *,
    now: datetime | None = None,
    session: Session | None = None,
) -> PendingTransaction | None:
    user_id = _validate_user_id(line_user_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    with _session_context(session) as db:
        pending = db.get(PendingTransaction, user_id)
        if pending is None:
            return None
        expires_at = pending.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > current_time:
            return pending
        result = db.execute(
            delete(PendingTransaction).where(
                PendingTransaction.line_user_id == user_id,
                PendingTransaction.draft_id == pending.draft_id,
                PendingTransaction.version == pending.version,
                PendingTransaction.expires_at <= pending.expires_at,
            )
        )
        db.flush()
        if result.rowcount == 0:
            raise PendingTransactionConflictError(
                "pending transaction changed during expiry cleanup"
            )
        return None


def create_pending_transaction(
    line_user_id: str,
    *,
    transaction_type: str | None,
    amount: MoneyInput | None,
    category: str | None,
    description: str | None,
    occurred_on: date,
    inference_rule: str | None = None,
    now: datetime | None = None,
    session: Session | None = None,
) -> bool:
    user_id = _validate_user_id(line_user_id)
    if transaction_type not in {None, "income", "expense"}:
        raise ValueError("transaction_type must be 'income', 'expense', or None")
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    values = {
        "line_user_id": user_id,
        "draft_id": uuid4().hex,
        "transaction_type": transaction_type,
        "amount_satang": baht_to_satang(amount) if amount is not None else None,
        "category": category,
        "description": description,
        "inference_rule": inference_rule,
        "occurred_on": occurred_on,
        "created_at": current_time,
        "expires_at": current_time + PENDING_TRANSACTION_TTL,
        "version": 1,
    }
    with _session_context(session) as db:
        try:
            with db.begin_nested():
                db.execute(insert(PendingTransaction).values(**values))
                db.flush()
        except IntegrityError:
            return False
        return True


def update_pending_transaction(
    line_user_id: str,
    expected_version: int,
    *,
    expected_draft_id: str,
    transaction_type: str | None,
    amount: MoneyInput | None,
    category: str | None,
    description: str | None,
    inference_rule: str | None = None,
    now: datetime | None = None,
    session: Session | None = None,
) -> bool:
    user_id = _validate_user_id(line_user_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    with _session_context(session) as db:
        result = db.execute(
            update(PendingTransaction)
            .where(
                PendingTransaction.line_user_id == user_id,
                PendingTransaction.draft_id == expected_draft_id,
                PendingTransaction.version == expected_version,
                PendingTransaction.expires_at > current_time,
            )
            .execution_options(synchronize_session=False)
            .values(
                transaction_type=transaction_type,
                amount_satang=baht_to_satang(amount) if amount is not None else None,
                category=category,
                description=description,
                inference_rule=inference_rule,
                expires_at=current_time + PENDING_TRANSACTION_TTL,
                version=expected_version + 1,
            )
        )
        db.flush()
        return result.rowcount == 1


def delete_pending_transaction(
    line_user_id: str,
    expected_version: int,
    *,
    expected_draft_id: str,
    now: datetime | None = None,
    session: Session | None = None,
) -> bool:
    user_id = _validate_user_id(line_user_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    with _session_context(session) as db:
        result = db.execute(
            delete(PendingTransaction).where(
                PendingTransaction.line_user_id == user_id,
                PendingTransaction.draft_id == expected_draft_id,
                PendingTransaction.version == expected_version,
                PendingTransaction.expires_at > current_time,
            ).execution_options(synchronize_session=False)
        )
        db.flush()
        return result.rowcount == 1


def mark_webhook_processed(
    webhook_event_id: str, *, session: Session | None = None
) -> bool:
    """Atomically claim an event, returning False when it was claimed before.

    The unique primary key makes this safe when LINE retries the same webhook or
    two workers receive it at nearly the same time.
    """

    event_id = webhook_event_id.strip()
    if not event_id:
        raise ValueError("webhook_event_id is required")

    with _session_context(session) as db:
        dialect_name = db.get_bind().dialect.name
        if dialect_name == "sqlite":
            statement = sqlite_insert(ProcessedWebhookEvent).values(
                webhook_event_id=event_id
            ).on_conflict_do_nothing(index_elements=["webhook_event_id"])
            result = db.execute(statement)
            db.flush()
            return result.rowcount == 1
        if dialect_name == "postgresql":
            statement = postgresql_insert(ProcessedWebhookEvent).values(
                webhook_event_id=event_id
            ).on_conflict_do_nothing(index_elements=["webhook_event_id"])
            result = db.execute(statement)
            db.flush()
            return result.rowcount == 1
        try:
            # A SAVEPOINT contains the uniqueness error when the caller supplied
            # a session that is doing other work in the same transaction.
            with db.begin_nested():
                db.add(ProcessedWebhookEvent(webhook_event_id=event_id))
                db.flush()
        except IntegrityError:
            return False
        return True


def get_webhook_event(
    webhook_event_id: str, *, session: Session | None = None
) -> ProcessedWebhookEvent | None:
    event_id = webhook_event_id.strip()
    if not event_id:
        raise ValueError("webhook_event_id is required")
    with _session_context(session) as db:
        return db.get(ProcessedWebhookEvent, event_id)


def set_webhook_response(
    webhook_event_id: str,
    response_text: str,
    *,
    session: Session | None = None,
) -> ProcessedWebhookEvent:
    text = response_text.strip()
    if not text:
        raise ValueError("response_text is required")
    with _session_context(session) as db:
        event = db.get(ProcessedWebhookEvent, webhook_event_id.strip())
        if event is None:
            raise LookupError("webhook event not found")
        event.response_text = text
        db.flush()
        return event


def mark_webhook_replied(
    webhook_event_id: str, *, session: Session | None = None
) -> ProcessedWebhookEvent:
    with _session_context(session) as db:
        event = db.get(ProcessedWebhookEvent, webhook_event_id.strip())
        if event is None:
            raise LookupError("webhook event not found")
        event.reply_sent = True
        db.flush()
        return event
