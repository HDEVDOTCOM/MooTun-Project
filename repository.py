"""Repository operations for transactions and savings goals.

User operations are scoped by ``line_user_id``. Export downloads instead use a
hashed capability joined to its owner's transactions in one SQL statement.
"""

from __future__ import annotations

import secrets
from calendar import monthrange
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import init_db, session_scope
from models import (
    ExportToken,
    PendingAction,
    PendingTransaction,
    ProcessedWebhookEvent,
    SavingsGoal,
    Transaction,
    UserFeedback,
)


MoneyInput = Decimal | int | str
PENDING_TRANSACTION_TTL = timedelta(hours=1)
PENDING_ACTION_TTL = timedelta(minutes=10)
CONFIRM_DELETE_TTL = timedelta(minutes=10)
CONFIRM_DELETE_ALL_TTL = timedelta(minutes=10)
UNDO_DELETE_TTL = timedelta(minutes=10)
EXPORT_TOKEN_TTL = timedelta(minutes=10)

# ``PendingAction.target_transaction_id`` is a non-nullable INTEGER with no
# positive-only constraint.  Delete-all has no single target, so it uses this
# reserved sentinel.  SQLite/Postgres auto-increment IDs start at 1, leaving 0
# permanently safe and avoiding an ``ALTER COLUMN`` rebuild.
DELETE_ALL_TARGET_SENTINEL = 0


class PendingTransactionConflictError(RuntimeError):
    """A pending draft changed while the current operation was in progress."""


class PendingActionConflictError(RuntimeError):
    """A pending action changed while the current operation was in progress."""


class PendingActionState(str, Enum):
    ACTIVE = "active"
    EXPIRED_CLEANED = "expired_cleaned"
    ABSENT = "absent"


class RestoreOutcome(str, Enum):
    RESTORED = "restored"
    ID_COLLISION = "id_collision"


@dataclass(frozen=True)
class PendingActionResult:
    state: PendingActionState
    action: PendingAction | None = None
    action_type: str | None = None


@dataclass(frozen=True)
class RestoreResult:
    outcome: RestoreOutcome
    transaction: Transaction | None = None


@dataclass(frozen=True)
class UserDataSummary:
    """Everything the delete-all feature must account for, per user."""

    transaction_count: int
    has_savings_goal: bool
    has_pending_transaction: bool
    has_pending_action: bool
    has_live_export_token: bool
    has_feedback: bool

    @property
    def has_any_data(self) -> bool:
        return (
            self.transaction_count > 0
            or self.has_savings_goal
            or self.has_pending_transaction
            or self.has_pending_action
            or self.has_live_export_token
            or self.has_feedback
        )


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


def add_user_feedback(
    line_user_id: str,
    rating: int,
    *,
    comment: str | None = None,
    expected_action_id: str | None = None,
    expected_version: int | None = None,
    now: datetime | None = None,
    session: Session | None = None,
) -> UserFeedback:
    """Append feedback, atomically invalidating an observed delete-all action.

    An observation of ACTIVE confirm_delete_all must carry its exact identity
    and version. An OCC loser inserts nothing and must never retry against a
    replacement action. Other actions and drafts are deliberately untouched.
    The caller's outer transaction owns both invalidation and insertion.
    """

    user_id = _validate_user_id(line_user_id)
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        raise ValueError("rating must be an integer from 1 to 5")
    comment_value = comment.strip() if comment is not None else None
    if comment_value is not None and len(comment_value) > 1000:
        raise ValueError("comment cannot exceed 1000 characters")
    if (expected_action_id is None) != (expected_version is None):
        raise ValueError("expected_action_id and expected_version must be supplied together")
    current_time = now or datetime.now(timezone.utc)
    current_time = (
        current_time.replace(tzinfo=timezone.utc)
        if current_time.tzinfo is None
        else current_time.astimezone(timezone.utc)
    )
    with _session_context(session) as db:
        if expected_action_id is not None:
            consumed = db.execute(
                delete(PendingAction)
                .where(
                    PendingAction.line_user_id == user_id,
                    PendingAction.action_id == expected_action_id,
                    PendingAction.version == expected_version,
                    PendingAction.action_type == "confirm_delete_all",
                    PendingAction.expires_at > current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if consumed.rowcount == 0:
                raise PendingActionConflictError(
                    "pending action changed during feedback insertion"
                )
        feedback = UserFeedback(
            line_user_id=user_id,
            rating=rating,
            comment=comment_value or None,
            created_at=current_time,
        )
        db.add(feedback)
        db.flush()
        return feedback


def create_export_token(
    line_user_id: str,
    *,
    now: datetime,
    session: Session | None = None,
) -> str | None:
    """Issue a ten-minute capability only when the user has transactions.

    The raw token is returned for the reply URL, which may be persisted in the
    webhook response cache. The ExportToken table stores only its hash.
    """

    user_id = _validate_user_id(line_user_id)
    current_time = (
        now.replace(tzinfo=timezone.utc)
        if now.tzinfo is None
        else now.astimezone(timezone.utc)
    )
    with _session_context(session) as db:
        count = db.scalar(
            select(func.count(Transaction.id)).where(Transaction.line_user_id == user_id)
        )
        if not count:
            return None
        raw_token = secrets.token_urlsafe(32)
        db.add(
            ExportToken(
                token_hash=sha256(raw_token.encode("utf-8")).hexdigest(),
                line_user_id=user_id,
                created_at=current_time,
                expires_at=current_time + EXPORT_TOKEN_TTL,
            )
        )
        db.flush()
        return raw_token


def fetch_export_transactions(
    token_hash: str,
    *,
    now: datetime,
    session: Session | None = None,
) -> list[Transaction]:
    """Authorize and read live data in ONE statement-level snapshot.

    Never split this join into token lookup followed by a transaction query:
    Delete All revocation must also protect subsequently created transactions.
    Empty data and unauthorized capabilities both return an empty list.
    """

    current_time = (
        now.replace(tzinfo=timezone.utc)
        if now.tzinfo is None
        else now.astimezone(timezone.utc)
    )
    statement = (
        select(Transaction)
        .join(ExportToken, ExportToken.line_user_id == Transaction.line_user_id)
        .where(ExportToken.token_hash == token_hash, ExportToken.expires_at > current_time)
        .order_by(
            Transaction.occurred_on.asc(),
            Transaction.created_at.asc(),
            Transaction.id.asc(),
        )
    )
    with _session_context(session) as db:
        with db.no_autoflush:
            return list(db.scalars(statement).all())


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


def update_latest_transaction(
    line_user_id: str,
    transaction_type: str,
    amount: MoneyInput,
    category: str,
    *,
    description: str | None = None,
    occurred_on: date | datetime | None = None,
    session: Session | None = None,
) -> Transaction | None:
    """Replace every field of the user's most recently entered transaction.

    The row is selected by entry order (``created_at DESC, id DESC``) so the
    transaction the user just recorded is the one that changes; ``occurred_on``
    is only overwritten when the caller supplies an explicit date.  Returns
    ``None`` when the user has no transactions to edit.
    """

    user_id = _validate_user_id(line_user_id)
    kind = transaction_type.strip().lower()
    if kind not in {"income", "expense"}:
        raise ValueError("transaction_type must be 'income' or 'expense'")
    category_value = category.strip()
    if not category_value:
        raise ValueError("category is required")
    if isinstance(occurred_on, datetime):
        occurred_on = occurred_on.date()

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
        if item is None:
            return None
        item.transaction_type = kind
        item.amount_satang = baht_to_satang(amount)
        item.category = category_value
        item.description = (
            description.strip() if description and description.strip() else None
        )
        if occurred_on is not None:
            item.occurred_on = occurred_on
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


def get_pending_action(
    line_user_id: str,
    *,
    now: datetime | None = None,
    session: Session | None = None,
) -> PendingActionResult:
    """Read active action state without collapsing cleaned expiry into absence."""

    user_id = _validate_user_id(line_user_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    with _session_context(session) as db:
        action = db.get(PendingAction, user_id)
        if action is None:
            return PendingActionResult(PendingActionState.ABSENT)
        expires_at = action.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > current_time:
            return PendingActionResult(PendingActionState.ACTIVE, action)

        result = db.execute(
            delete(PendingAction)
            .where(
                PendingAction.line_user_id == user_id,
                PendingAction.action_id == action.action_id,
                PendingAction.version == action.version,
                PendingAction.expires_at <= action.expires_at,
            )
            .execution_options(synchronize_session=False)
        )
        db.flush()
        if result.rowcount == 0:
            raise PendingActionConflictError(
                "pending action changed during expiry cleanup"
            )
        return PendingActionResult(
            PendingActionState.EXPIRED_CLEANED,
            action_type=action.action_type,
        )


def create_pending_action(
    line_user_id: str,
    *,
    action_type: str,
    target_transaction_id: int,
    now: datetime | None = None,
    session: Session | None = None,
) -> bool:
    """Create one pending action; first successful concurrent create wins."""

    user_id = _validate_user_id(line_user_id)
    if action_type == "confirm_delete":
        if target_transaction_id < 1:
            raise ValueError("target_transaction_id must be positive")
        ttl = PENDING_ACTION_TTL
    elif action_type == "confirm_delete_all":
        if target_transaction_id != DELETE_ALL_TARGET_SENTINEL:
            raise ValueError("confirm_delete_all requires the delete-all sentinel")
        ttl = CONFIRM_DELETE_ALL_TTL
    else:
        raise ValueError(
            "action_type must be 'confirm_delete' or 'confirm_delete_all'"
        )
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    values = {
        "line_user_id": user_id,
        "action_id": uuid4().hex,
        "version": 1,
        "action_type": action_type,
        "target_transaction_id": target_transaction_id,
        "created_at": current_time,
        "expires_at": current_time + ttl,
    }
    with _session_context(session) as db:
        try:
            with db.begin_nested():
                db.execute(insert(PendingAction).values(**values))
                db.flush()
        except IntegrityError:
            return False
        return True


def delete_pending_action(
    line_user_id: str,
    expected_version: int,
    *,
    expected_action_id: str,
    now: datetime | None = None,
    session: Session | None = None,
) -> bool:
    """Delete the exact active action using identity and version OCC guards."""

    user_id = _validate_user_id(line_user_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    with _session_context(session) as db:
        result = db.execute(
            delete(PendingAction)
            .where(
                PendingAction.line_user_id == user_id,
                PendingAction.action_id == expected_action_id,
                PendingAction.version == expected_version,
                PendingAction.expires_at > current_time,
            )
            .execution_options(synchronize_session=False)
        )
        db.flush()
        return result.rowcount == 1


def invalidate_pending_action(
    line_user_id: str,
    expected_version: int,
    *,
    expected_action_id: str,
    now: datetime | None = None,
    session: Session | None = None,
) -> bool:
    """Discard an unrelated deletion confirmation action.

    Both ``confirm_delete`` and ``confirm_delete_all`` are dropped by unrelated
    state mutations.  An active ``undo_delete`` snapshot is intentionally
    preserved: unrelated transactions, edits, draft completions, and stateless
    commands must not consume the user's pending undo opportunity.
    """

    user_id = _validate_user_id(line_user_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    with _session_context(session) as db:
        result = db.execute(
            delete(PendingAction)
            .where(
                PendingAction.line_user_id == user_id,
                PendingAction.action_id == expected_action_id,
                PendingAction.version == expected_version,
                PendingAction.action_type.in_(
                    ["confirm_delete", "confirm_delete_all"]
                ),
                PendingAction.expires_at > current_time,
            )
            .execution_options(synchronize_session=False)
        )
        db.flush()
        return result.rowcount == 1


def confirm_delete_transaction(
    line_user_id: str,
    expected_version: int,
    *,
    expected_action_id: str,
    target_transaction_id: int,
    now: datetime | None = None,
    session: Session | None = None,
) -> Transaction | None:
    """Consume an exact confirmation, delete its target, and open an undo window.

    The whole confirm -> undo transition runs in one outer transaction: the
    ``confirm_delete`` row is replaced by an ``undo_delete`` snapshot and the
    target ``Transaction`` is deleted.  A missing target consumes the
    confirmation without creating an undo snapshot and returns ``None``.
    """

    user_id = _validate_user_id(line_user_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    with _session_context(session) as db:
        action = db.scalar(
            select(PendingAction).where(
                PendingAction.line_user_id == user_id,
                PendingAction.action_id == expected_action_id,
                PendingAction.version == expected_version,
                PendingAction.action_type == "confirm_delete",
                PendingAction.target_transaction_id == target_transaction_id,
                PendingAction.expires_at > current_time,
            )
        )
        if action is None:
            raise PendingActionConflictError(
                "pending action changed during confirmation"
            )

        item = db.scalar(
            select(Transaction).where(
                Transaction.id == target_transaction_id,
                Transaction.line_user_id == user_id,
            )
        )
        if item is None:
            result = db.execute(
                delete(PendingAction)
                .where(
                    PendingAction.line_user_id == user_id,
                    PendingAction.action_id == expected_action_id,
                    PendingAction.version == expected_version,
                    PendingAction.action_type == "confirm_delete",
                    PendingAction.target_transaction_id == target_transaction_id,
                    PendingAction.expires_at > current_time,
                )
                .execution_options(synchronize_session=False)
            )
            db.flush()
            if result.rowcount == 0:
                raise PendingActionConflictError(
                    "pending action changed during confirmation"
                )
            db.expunge(action)
            return None

        result = db.execute(
            update(PendingAction)
            .where(
                PendingAction.line_user_id == user_id,
                PendingAction.action_id == expected_action_id,
                PendingAction.version == expected_version,
                PendingAction.action_type == "confirm_delete",
                PendingAction.target_transaction_id == target_transaction_id,
                PendingAction.expires_at > current_time,
            )
            .execution_options(synchronize_session=False)
            .values(
                action_type="undo_delete",
                version=expected_version + 1,
                snapshot_transaction_id=item.id,
                snapshot_transaction_type=item.transaction_type,
                snapshot_amount_satang=item.amount_satang,
                snapshot_category=item.category,
                snapshot_description=item.description,
                snapshot_occurred_on=item.occurred_on,
                snapshot_created_at=item.created_at,
                expires_at=current_time + UNDO_DELETE_TTL,
            )
        )
        if result.rowcount == 0:
            raise PendingActionConflictError(
                "pending action changed during confirmation"
            )

        db.expire(action)
        db.delete(item)
        db.flush()
        return item


def replace_pending_action_with_confirm(
    line_user_id: str,
    expected_version: int,
    *,
    expected_action_id: str,
    expected_action_type: str,
    target_transaction_id: int,
    now: datetime | None = None,
    session: Session | None = None,
) -> bool:
    """OCC-safely replace the current action with a fresh ``confirm_delete``.

    Used when ``ลบล่าสุด`` arrives while an ``undo_delete`` snapshot is active:
    the stale undo opportunity is intentionally discarded and re-pointed at the
    current latest transaction.
    """

    user_id = _validate_user_id(line_user_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    with _session_context(session) as db:
        result = db.execute(
            update(PendingAction)
            .where(
                PendingAction.line_user_id == user_id,
                PendingAction.action_id == expected_action_id,
                PendingAction.version == expected_version,
                PendingAction.action_type == expected_action_type,
                PendingAction.expires_at > current_time,
            )
            .execution_options(synchronize_session=False)
            .values(
                action_id=uuid4().hex,
                version=expected_version + 1,
                action_type="confirm_delete",
                target_transaction_id=target_transaction_id,
                snapshot_transaction_id=None,
                snapshot_transaction_type=None,
                snapshot_amount_satang=None,
                snapshot_category=None,
                snapshot_description=None,
                snapshot_occurred_on=None,
                snapshot_created_at=None,
                created_at=current_time,
                expires_at=current_time + CONFIRM_DELETE_TTL,
            )
        )
        db.flush()
        return result.rowcount == 1


def replace_pending_action_with_delete_all(
    line_user_id: str,
    expected_version: int,
    *,
    expected_action_id: str,
    expected_action_type: str,
    now: datetime | None = None,
    session: Session | None = None,
) -> bool:
    """OCC-safely replace the current action with a fresh ``confirm_delete_all``.

    Used when ``ลบข้อมูลทั้งหมด`` arrives while any other action (a single-delete
    confirmation, an undo snapshot, or a previous delete-all) is active: the
    stale action is intentionally discarded because a total wipe abandons it.
    """

    user_id = _validate_user_id(line_user_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    with _session_context(session) as db:
        result = db.execute(
            update(PendingAction)
            .where(
                PendingAction.line_user_id == user_id,
                PendingAction.action_id == expected_action_id,
                PendingAction.version == expected_version,
                PendingAction.action_type == expected_action_type,
                PendingAction.expires_at > current_time,
            )
            .execution_options(synchronize_session=False)
            .values(
                action_id=uuid4().hex,
                version=expected_version + 1,
                action_type="confirm_delete_all",
                target_transaction_id=DELETE_ALL_TARGET_SENTINEL,
                snapshot_transaction_id=None,
                snapshot_transaction_type=None,
                snapshot_amount_satang=None,
                snapshot_category=None,
                snapshot_description=None,
                snapshot_occurred_on=None,
                snapshot_created_at=None,
                created_at=current_time,
                expires_at=current_time + CONFIRM_DELETE_ALL_TTL,
            )
        )
        db.flush()
        return result.rowcount == 1


def execute_delete_all(
    line_user_id: str,
    expected_version: int,
    *,
    expected_action_id: str,
    now: datetime | None = None,
    session: Session | None = None,
) -> None:
    """Atomically consume an exact ``confirm_delete_all`` and wipe user data.

    The confirmation row is deleted with identity/version/type/expiry OCC
    guards.  Only when that succeeds are the user's transactions, savings goal,
    pending draft, export capabilities, and feedback removed, all in the same
    transaction and every statement scoped by ``line_user_id``.  Global
    webhook-idempotency records are deliberately never touched.
    """

    user_id = _validate_user_id(line_user_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    with _session_context(session) as db:
        consumed = db.execute(
            delete(PendingAction)
            .where(
                PendingAction.line_user_id == user_id,
                PendingAction.action_id == expected_action_id,
                PendingAction.version == expected_version,
                PendingAction.action_type == "confirm_delete_all",
                PendingAction.expires_at > current_time,
            )
            .execution_options(synchronize_session=False)
        )
        db.flush()
        if consumed.rowcount == 0:
            raise PendingActionConflictError(
                "pending action changed during delete-all"
            )
        for model in (PendingTransaction, SavingsGoal, Transaction, ExportToken, UserFeedback):
            db.execute(
                delete(model)
                .where(model.line_user_id == user_id)
                .execution_options(synchronize_session=False)
            )
        db.flush()


def get_user_data_summary(
    line_user_id: str,
    *,
    now: datetime | None = None,
    session: Session | None = None,
) -> UserDataSummary:
    """Report whether a user still has anything delete-all would remove."""

    user_id = _validate_user_id(line_user_id)
    current_time = now or datetime.now(timezone.utc)
    current_time = (
        current_time.replace(tzinfo=timezone.utc)
        if current_time.tzinfo is None
        else current_time.astimezone(timezone.utc)
    )
    with _session_context(session) as db:
        transaction_count = int(
            db.scalar(
                select(func.count(Transaction.id)).where(
                    Transaction.line_user_id == user_id
                )
            )
            or 0
        )
        has_goal = (
            db.scalar(
                select(SavingsGoal.id).where(SavingsGoal.line_user_id == user_id)
            )
            is not None
        )
        has_draft = db.get(PendingTransaction, user_id) is not None
        has_action = db.get(PendingAction, user_id) is not None
        has_live_export_token = db.scalar(
            select(ExportToken.id).where(
                ExportToken.line_user_id == user_id,
                ExportToken.expires_at > current_time,
            ).limit(1)
        ) is not None
        has_feedback = db.scalar(
            select(UserFeedback.id).where(UserFeedback.line_user_id == user_id).limit(1)
        ) is not None
    return UserDataSummary(
        transaction_count=transaction_count,
        has_savings_goal=has_goal,
        has_pending_transaction=has_draft,
        has_pending_action=has_action,
        has_live_export_token=has_live_export_token,
        has_feedback=has_feedback,
    )


def restore_deleted_transaction(
    line_user_id: str,
    expected_version: int,
    *,
    expected_action_id: str,
    now: datetime | None = None,
    session: Session | None = None,
) -> RestoreResult:
    """Consume an exact ``undo_delete`` and restore its snapshot verbatim.

    The action is consumed conditionally; the row is reinserted under its exact
    original id inside a nested savepoint.  An id collision rolls back only the
    insert while leaving the action consumed, yielding a deterministic
    ``ID_COLLISION`` outcome.
    """

    user_id = _validate_user_id(line_user_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    with _session_context(session) as db:
        action = db.scalar(
            select(PendingAction).where(
                PendingAction.line_user_id == user_id,
                PendingAction.action_id == expected_action_id,
                PendingAction.version == expected_version,
                PendingAction.action_type == "undo_delete",
                PendingAction.expires_at > current_time,
            )
        )
        if action is None:
            raise PendingActionConflictError(
                "pending action changed during undo"
            )
        if (
            action.snapshot_transaction_id is None
            or action.snapshot_transaction_type is None
            or action.snapshot_amount_satang is None
            or action.snapshot_category is None
            or action.snapshot_occurred_on is None
            or action.snapshot_created_at is None
        ):
            raise PendingActionConflictError("undo snapshot is incomplete")

        consumed = db.execute(
            delete(PendingAction)
            .where(
                PendingAction.line_user_id == user_id,
                PendingAction.action_id == expected_action_id,
                PendingAction.version == expected_version,
                PendingAction.action_type == "undo_delete",
                PendingAction.expires_at > current_time,
            )
            .execution_options(synchronize_session=False)
        )
        db.flush()
        if consumed.rowcount == 0:
            raise PendingActionConflictError(
                "pending action changed during undo"
            )
        db.expunge(action)

        values = {
            "id": action.snapshot_transaction_id,
            "line_user_id": user_id,
            "transaction_type": action.snapshot_transaction_type,
            "amount_satang": action.snapshot_amount_satang,
            "category": action.snapshot_category,
            "description": action.snapshot_description,
            "occurred_on": action.snapshot_occurred_on,
            "created_at": action.snapshot_created_at,
        }
        restored = Transaction(**values)
        try:
            with db.begin_nested():
                db.add(restored)
                db.flush()
        except IntegrityError:
            return RestoreResult(RestoreOutcome.ID_COLLISION)
        return RestoreResult(RestoreOutcome.RESTORED, restored)


def get_latest_transaction(
    line_user_id: str, *, session: Session | None = None
) -> Transaction | None:
    user_id = _validate_user_id(line_user_id)
    statement = (
        select(Transaction)
        .where(Transaction.line_user_id == user_id)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .limit(1)
    )
    with _session_context(session) as db:
        return db.scalar(statement)


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
            statement = (
                postgresql_insert(ProcessedWebhookEvent)
                .values(webhook_event_id=event_id)
                .on_conflict_do_nothing(index_elements=["webhook_event_id"])
                .returning(ProcessedWebhookEvent.webhook_event_id)
            )
            result = db.execute(statement)
            db.flush()
            return result.scalar_one_or_none() is not None
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
