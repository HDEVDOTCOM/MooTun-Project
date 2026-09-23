from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Request
from dotenv import load_dotenv
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from database import init_db, session_scope
from line_api import LineAPIError, reply_text, verify_webhook_signature
from messages import (
    format_buddhist_date,
    format_buddhist_month,
    format_edit_confirmation,
    format_help_message,
    format_monthly_summary,
    format_invalid_followup,
    format_pending_conflict,
    format_pending_transaction_prompt,
    format_recent_transactions,
    format_savings_goal,
    format_transaction_confirmation,
    format_unknown_message,
)
from parser import (
    CommandKind,
    EditLatestCommand,
    FOLLOWUP_CONFLICT_REASON,
    IncompleteCommand,
    SavingsGoalCommand,
    SavingsProgressCommand,
    SimpleCommand,
    TransactionCommand,
    UnresolvedCommand,
    UnresolvedEditCommand,
    parse_command,
    parse_followup,
)
from repository import (
    PendingTransactionConflictError,
    add_savings_progress,
    add_transaction,
    create_pending_transaction,
    delete_pending_transaction,
    delete_latest_transaction,
    get_savings_goal,
    get_pending_transaction,
    get_webhook_event,
    list_recent_transactions,
    mark_webhook_processed,
    mark_webhook_replied,
    monthly_summary,
    set_savings_goal,
    set_webhook_response,
    update_latest_transaction,
    update_pending_transaction,
)


logger = logging.getLogger("mootoon")
BANGKOK = ZoneInfo("Asia/Bangkok")

load_dotenv()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.getenv("ENVIRONMENT") == "production":
        missing = [
            name
            for name in (
                "LINE_CHANNEL_SECRET",
                "LINE_CHANNEL_ACCESS_TOKEN",
                "DATABASE_URL",
            )
            if not os.getenv(name)
        ]
        if missing:
            raise RuntimeError(f"Missing production environment variables: {', '.join(missing)}")
    init_db()
    yield


app = FastAPI(title="MooToon LINE Bot", lifespan=lifespan)


def _transaction_data(item: Any) -> dict[str, Any]:
    return {
        "type": item.transaction_type,
        "amount": item.amount,
        "category": item.category,
        "date": item.occurred_on,
        "note": item.description,
    }


def _goal_data(goal: Any) -> dict[str, Any]:
    return {
        "name": goal.title,
        "target": goal.target_amount,
        "saved": goal.saved_amount,
        "due_date": goal.deadline,
    }


def _event_datetime(timestamp_ms: object) -> datetime:
    if isinstance(timestamp_ms, (int, float)):
        try:
            return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _thai_month_label(moment: datetime) -> str:
    local = moment.astimezone(BANGKOK)
    return format_buddhist_month(local)


def handle_text_message(
    line_user_id: str,
    text: str,
    event_time: datetime,
    session: Session,
) -> str:
    """Run one verified user's command and return a Thai LINE reply."""

    processing_time = _utc_now()
    try:
        pending = get_pending_transaction(
            line_user_id,
            now=processing_time,
            session=session,
        )
    except PendingTransactionConflictError:
        return "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"
    if pending is not None and text.strip().lower() == "ยกเลิก":
        if not delete_pending_transaction(
            line_user_id,
            pending.version,
            expected_draft_id=pending.draft_id,
            now=_utc_now(),
            session=session,
        ):
            return "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"
        return "ยกเลิกรายการที่ค้างไว้แล้วครับ"

    command = parse_command(text, now=event_time)

    if isinstance(command, UnresolvedEditCommand):
        return format_unknown_message(command.reason)

    if isinstance(command, EditLatestCommand):
        item = update_latest_transaction(
            line_user_id,
            command.transaction_type,
            command.amount,
            command.category,
            description=(
                None
                if command.description == "ไม่ระบุรายการ"
                else command.description
            ),
            occurred_on=(
                command.transaction_date if command.explicit_occurred_on else None
            ),
            session=session,
        )
        if item is None:
            return "ยังไม่มีรายการให้แก้ไข"
        return format_edit_confirmation(_transaction_data(item))

    if pending is not None and isinstance(command, TransactionCommand):
        if not delete_pending_transaction(
            line_user_id,
            pending.version,
            expected_draft_id=pending.draft_id,
            now=_utc_now(),
            session=session,
        ):
            return "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"

    if pending is not None and isinstance(
        command,
        (IncompleteCommand, UnresolvedCommand),
    ):
        draft = IncompleteCommand(
            pending.transaction_type,
            pending.amount,
            pending.category,
            pending.occurred_on,
            pending.description,
            pending.inference_rule,
        )
        followup = parse_followup(draft, text, now=event_time)
        if isinstance(followup, TransactionCommand):
            if not delete_pending_transaction(
                line_user_id,
                pending.version,
                expected_draft_id=pending.draft_id,
                now=_utc_now(),
                session=session,
            ):
                return "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"
            command = followup
        elif isinstance(followup, IncompleteCommand):
            if not update_pending_transaction(
                line_user_id,
                pending.version,
                expected_draft_id=pending.draft_id,
                transaction_type=followup.transaction_type,
                amount=followup.amount,
                category=followup.category,
                description=followup.description,
                inference_rule=followup.inference_rule,
                now=_utc_now(),
                session=session,
            ):
                return "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"
            return format_pending_transaction_prompt(
                amount=followup.amount,
                description=followup.description,
            )
        elif followup.reason == FOLLOWUP_CONFLICT_REASON:
            return format_pending_conflict()
        else:
            return format_invalid_followup()

    if isinstance(command, IncompleteCommand):
        if not create_pending_transaction(
            line_user_id,
            transaction_type=command.transaction_type,
            amount=command.amount,
            category=command.category,
            description=command.description,
            occurred_on=command.transaction_date,
            inference_rule=command.inference_rule,
            now=processing_time,
            session=session,
        ):
            return "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"
        return format_pending_transaction_prompt(
            amount=command.amount,
            description=command.description,
        )

    if isinstance(command, TransactionCommand):
        item = add_transaction(
            line_user_id,
            command.kind.value,
            command.amount,
            command.category,
            description=command.description,
            occurred_on=command.transaction_date,
            session=session,
        )
        return format_transaction_confirmation(_transaction_data(item))

    if isinstance(command, SavingsGoalCommand):
        goal = set_savings_goal(
            line_user_id,
            command.description or "เป้าหมายการออม",
            command.amount,
            session=session,
        )
        return "ตั้งเป้าหมายเรียบร้อยแล้ว\n" + format_savings_goal(_goal_data(goal))

    if isinstance(command, SavingsProgressCommand):
        try:
            goal = add_savings_progress(
                line_user_id,
                command.amount,
                session=session,
            )
        except LookupError:
            return "ยังไม่มีเป้าหมายการออม ลองพิมพ์ “ตั้งเป้า 1500 ซื้อหนังสือ”"
        return "บันทึกเงินออมเรียบร้อยแล้ว\n" + format_savings_goal(_goal_data(goal))

    if isinstance(command, UnresolvedCommand):
        if command.kind == CommandKind.AMBIGUOUS:
            return format_unknown_message(command.reason)
        return format_unknown_message()

    if not isinstance(command, SimpleCommand):
        return format_unknown_message()

    if command.kind == CommandKind.HELP:
        return format_help_message()

    if command.kind == CommandKind.RECENT:
        items = list_recent_transactions(line_user_id, limit=5, session=session)
        return format_recent_transactions([_transaction_data(item) for item in items])

    if command.kind == CommandKind.DELETE_LATEST:
        item = delete_latest_transaction(line_user_id, session=session)
        if item is None:
            return "ยังไม่มีรายการให้ลบ"
        return (
            "🗑️ ลบรายการล่าสุดแล้ว\n"
            f"{item.category} · {item.amount:,.2f} บาท · "
            f"{format_buddhist_date(item.occurred_on)}"
        ).replace(".00 บาท", " บาท")

    if command.kind == CommandKind.MONTHLY_SUMMARY:
        local = event_time.astimezone(BANGKOK)
        summary = monthly_summary(
            line_user_id,
            local.year,
            local.month,
            session=session,
        )
        summary["month"] = _thai_month_label(event_time)
        return format_monthly_summary(summary)

    if command.kind == CommandKind.SAVINGS_STATUS:
        goal = get_savings_goal(line_user_id, session=session)
        if goal is None:
            return "ยังไม่มีเป้าหมายการออม ลองพิมพ์ “ตั้งเป้า 1500 ซื้อหนังสือ”"
        return format_savings_goal(_goal_data(goal))

    return format_unknown_message()


@app.get("/")
async def health_check() -> dict[str, object]:
    return {
        "status": "ok",
        "service": "mootoon-line-bot",
        "line_configured": bool(LINE_CHANNEL_SECRET and LINE_CHANNEL_ACCESS_TOKEN),
    }


@app.get("/ready")
async def readiness_check() -> dict[str, str]:
    if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
        raise HTTPException(status_code=503, detail="LINE credentials are not configured")
    try:
        with session_scope() as session:
            session.execute(sql_text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database is not ready") from exc
    return {"status": "ready"}


@app.post("/webhook")
async def line_webhook(
    request: Request,
    x_line_signature: str = Header(default=""),
) -> dict[str, str]:
    body = await request.body()

    if not LINE_CHANNEL_SECRET:
        raise HTTPException(status_code=503, detail="LINE channel secret is not configured")

    if not verify_webhook_signature(body, x_line_signature, LINE_CHANNEL_SECRET):
        raise HTTPException(status_code=400, detail="Invalid LINE signature")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    for event in payload.get("events", []):
        if not isinstance(event, dict):
            continue

        event_id = event.get("webhookEventId")
        reply_token = event.get("replyToken")
        source = event.get("source", {})
        message = event.get("message", {})

        if (
            not isinstance(event_id, str)
            or event.get("type") != "message"
            or not isinstance(source, dict)
            or source.get("type") != "user"
            or not isinstance(source.get("userId"), str)
            or not isinstance(message, dict)
            or message.get("type") != "text"
        ):
            continue

        with session_scope() as session:
            processed = get_webhook_event(event_id, session=session)
            if processed is not None:
                if processed.reply_sent or not processed.response_text:
                    continue
                response_text = processed.response_text
            else:
                if not mark_webhook_processed(event_id, session=session):
                    continue
                response_text = handle_text_message(
                    source["userId"],
                    str(message.get("text", "")),
                    _event_datetime(event.get("timestamp")),
                    session,
                )
                set_webhook_response(event_id, response_text, session=session)

        if isinstance(reply_token, str) and reply_token:
            try:
                await reply_text(
                    reply_token,
                    response_text,
                    LINE_CHANNEL_ACCESS_TOKEN,
                )
            except LineAPIError as exc:
                logger.error("LINE reply failed for event %s: %s", event_id, type(exc).__name__)
                raise HTTPException(status_code=502, detail="LINE reply failed") from exc
            with session_scope() as session:
                mark_webhook_replied(event_id, session=session)

    return {"status": "ok"}
