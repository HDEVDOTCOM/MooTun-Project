"""Database models for MooToon.

Money is persisted as integer satang.  This avoids the rounding errors that can
occur when a database or Python represents money as a floating-point number.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount_satang > 0", name="ck_transactions_positive_amount"),
        CheckConstraint(
            "transaction_type IN ('income', 'expense')",
            name="ck_transactions_valid_type",
        ),
        Index(
            "ix_transactions_user_date_created",
            "line_user_id",
            "occurred_on",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    line_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    transaction_type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_satang: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    @property
    def amount(self) -> Decimal:
        """Amount expressed in baht as an exact Decimal."""

        return Decimal(self.amount_satang) / Decimal(100)


class SavingsGoal(Base):
    __tablename__ = "savings_goals"
    __table_args__ = (
        UniqueConstraint("line_user_id", name="uq_savings_goals_line_user_id"),
        CheckConstraint("target_satang > 0", name="ck_savings_goals_positive_target"),
        CheckConstraint("saved_satang >= 0", name="ck_savings_goals_nonnegative_saved"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    line_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    target_satang: Mapped[int] = mapped_column(BigInteger, nullable=False)
    saved_satang: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    @property
    def target_amount(self) -> Decimal:
        return Decimal(self.target_satang) / Decimal(100)

    @property
    def saved_amount(self) -> Decimal:
        return Decimal(self.saved_satang) / Decimal(100)

    @property
    def remaining_amount(self) -> Decimal:
        return max(self.target_amount - self.saved_amount, Decimal("0.00"))


class ProcessedWebhookEvent(Base):
    """Durable idempotency record for a LINE webhook event."""

    __tablename__ = "processed_webhook_events"

    webhook_event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    response_text: Mapped[str | None] = mapped_column(String(5000), nullable=True)
    reply_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
