"""SQLAlchemy engine and session configuration."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from models import Base


DEFAULT_DATABASE_URL = "sqlite:///./mootoon.db"

load_dotenv()


def normalize_database_url(database_url: str) -> str:
    """Accept the legacy ``postgres://`` URL commonly supplied by hosts."""

    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    return database_url


def create_db_engine(database_url: str | None = None) -> Engine:
    url = normalize_database_url(
        database_url or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def configure_database(database_url: str) -> Engine:
    """Replace the process-wide engine, primarily for tests and app startup."""

    global engine, SessionLocal
    engine.dispose()
    engine = create_db_engine(database_url)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    return engine


def init_db(db_engine: Engine | None = None) -> None:
    """Create all application tables if they do not exist."""

    active_engine = db_engine or engine
    Base.metadata.create_all(bind=active_engine)
    _upgrade_webhook_event_table(active_engine)
    _upgrade_pending_transaction_table(active_engine)


def _upgrade_webhook_event_table(db_engine: Engine) -> None:
    """Apply the small pre-Alembic v0.1 webhook-state schema upgrade."""

    table_name = "processed_webhook_events"
    columns = {column["name"] for column in inspect(db_engine).get_columns(table_name)}
    statements: list[str] = []
    if "response_text" not in columns:
        statements.append(
            f"ALTER TABLE {table_name} ADD COLUMN response_text VARCHAR(5000)"
        )
    if "reply_sent" not in columns:
        statements.append(
            f"ALTER TABLE {table_name} ADD COLUMN reply_sent BOOLEAN NOT NULL DEFAULT FALSE"
        )
    if statements:
        with db_engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))


def _upgrade_pending_transaction_table(db_engine: Engine) -> None:
    """Add pending-state fields introduced before schema migrations exist."""

    table_name = "pending_transactions"
    columns = {column["name"] for column in inspect(db_engine).get_columns(table_name)}
    if "inference_rule" not in columns:
        with db_engine.begin() as connection:
            connection.execute(
                text(
                    f"ALTER TABLE {table_name} "
                    "ADD COLUMN inference_rule VARCHAR(100)"
                )
            )


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a committing session and roll it back on failure."""

    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
