from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from dotenv import load_dotenv
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from database import init_db, session_scope
from line_api import LineAPIError, reply_text, verify_webhook_signature
from messages import (
    format_buddhist_month,
    format_delete_all_cancelled,
    format_delete_all_confirmation_prompt,
    format_delete_all_expired,
    format_delete_all_no_data,
    format_delete_all_success,
    format_delete_confirmation_prompt,
    format_delete_not_confirmed_yet,
    format_delete_success,
    format_edit_confirmation,
    format_export_link,
    format_export_no_data,
    format_export_unavailable,
    format_feedback_comment_too_long,
    format_feedback_instruction,
    format_feedback_invalid_rating,
    format_feedback_success,
    format_help_message,
    format_monthly_summary,
    format_onboarding_usage,
    format_onboarding_welcome,
    format_invalid_followup,
    format_pending_conflict,
    format_pending_transaction_prompt,
    format_recent_transactions,
    format_savings_goal,
    format_transaction_confirmation,
    format_undo_expired,
    format_undo_id_collision,
    format_undo_restored,
    format_undo_unavailable,
    format_unknown_message,
)
from parser import (
    CommandKind,
    EditLatestCommand,
    FOLLOWUP_CONFLICT_REASON,
    FeedbackCommand,
    FeedbackError,
    IncompleteCommand,
    InvalidFeedbackCommand,
    InvalidOnboardingCommand,
    SavingsGoalCommand,
    SavingsProgressCommand,
    SimpleCommand,
    TransactionCommand,
    UnresolvedCommand,
    UnresolvedEditCommand,
    parse_command,
    parse_followup,
    parse_onboarding,
)
from repository import (
    DELETE_ALL_TARGET_SENTINEL,
    PendingActionConflictError,
    PendingActionState,
    PendingTransactionConflictError,
    RestoreOutcome,
    add_savings_progress,
    add_transaction,
    add_user_feedback,
    confirm_delete_transaction,
    create_export_token,
    create_pending_action,
    create_pending_transaction,
    delete_pending_action,
    delete_pending_transaction,
    execute_delete_all,
    fetch_export_transactions,
    get_latest_transaction,
    get_pending_action,
    get_savings_goal,
    get_pending_transaction,
    get_user_data_summary,
    get_webhook_event,
    list_recent_transactions,
    mark_webhook_processed,
    mark_webhook_replied,
    monthly_summary,
    replace_pending_action_with_confirm,
    replace_pending_action_with_delete_all,
    restore_deleted_transaction,
    set_savings_goal,
    set_webhook_response,
    invalidate_pending_action,
    update_latest_transaction,
    update_pending_transaction,
)
from webapp_auth import VERIFY_TIMEOUT_SECONDS, WebAccessLogFilter, WebAppConfig
from webapp_routes import register_webapp


logger = logging.getLogger("mootoon")
BANGKOK = ZoneInfo("Asia/Bangkok")
PENDING_ACTION_RETRY_REPLY = "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"
PENDING_ACTION_EXPIRED_REPLY = "คำสั่งลบหมดอายุแล้ว กรุณาส่ง 'ลบล่าสุด' อีกครั้งหากต้องการลบ"

load_dotenv()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")


def _export_base_url() -> str | None:
    """Validate the configured HTTPS base; there is no development fallback."""

    value = os.getenv("RENDER_EXTERNAL_URL", "")
    if not value:
        return None
    try:
        parts = urlsplit(value)
        valid = (
            value.startswith("https://")
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and "?" not in value
            and "#" not in value
            and not any(character.isspace() or ord(character) < 32 for character in value)
            and "\\" not in value
        )
        # Accessing port also validates malformed/out-of-range ports.
        parts.port
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("RENDER_EXTERNAL_URL must be a valid https:// URL")
    return value.rstrip("/")


@asynccontextmanager
async def lifespan(application: FastAPI):
    if os.getenv("ENVIRONMENT") == "production":
        missing = [
            name
            for name in (
                "LINE_CHANNEL_SECRET",
                "LINE_CHANNEL_ACCESS_TOKEN",
                "DATABASE_URL",
                "RENDER_EXTERNAL_URL",
            )
            if not os.getenv(name)
        ]
        if missing:
            raise RuntimeError(f"Missing production environment variables: {', '.join(missing)}")
    _export_base_url()
    init_db()
    application.state.webapp_config = WebAppConfig.from_environment()
    access_logger = logging.getLogger("uvicorn.access")
    access_filter = WebAccessLogFilter()
    access_logger.addFilter(access_filter)
    try:
        # HTTPX's default transport has no automatic retries. No credential cache.
        async with httpx.AsyncClient(timeout=VERIFY_TIMEOUT_SECONDS) as client:
            application.state.webapp_verify_client = client
            yield
    finally:
        access_logger.removeFilter(access_filter)


app = FastAPI(title="MooToon LINE Bot", lifespan=lifespan)
register_webapp(app)


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
    normalized_text = text.strip().lower()

    # Export must not touch draft/action reads that can perform expiry cleanup.
    if normalized_text == "ส่งออกข้อมูล":
        command = parse_command(text, now=event_time)
        if isinstance(command, SimpleCommand) and command.kind == CommandKind.EXPORT:
            base_url = _export_base_url()
            if base_url is None:
                return format_export_unavailable()
            token = create_export_token(line_user_id, now=processing_time, session=session)
            if token is None:
                return format_export_no_data()
            return format_export_link(f"{base_url}/export/{token}")

    # Onboarding must avoid financial parsing and even lazy pending expiry reads.
    onboarding = parse_onboarding(text)
    if isinstance(onboarding, InvalidOnboardingCommand):
        return format_onboarding_usage()
    if onboarding is not None:
        return format_onboarding_welcome()

    try:
        action_result = get_pending_action(
            line_user_id,
            now=processing_time,
            session=session,
        )
    except PendingActionConflictError:
        return PENDING_ACTION_RETRY_REPLY

    action = action_result.action
    action_type = (
        action.action_type if action is not None else action_result.action_type
    )
    pending_delete_confirmation = (
        action
        if action is not None
        and action.action_type in {"confirm_delete", "confirm_delete_all"}
        else None
    )
    pending_delete_all = (
        action
        if action is not None and action.action_type == "confirm_delete_all"
        else None
    )

    if action_result.state == PendingActionState.EXPIRED_CLEANED:
        if normalized_text in {"ยืนยัน", "ยกเลิก"} and action_type == "confirm_delete":
            return PENDING_ACTION_EXPIRED_REPLY
        if (
            normalized_text in {"ยืนยัน", "ยกเลิก"}
            and action_type == "confirm_delete_all"
        ):
            return format_delete_all_expired()
        if normalized_text == "เลิกทำ":
            if action_type == "undo_delete":
                return format_undo_expired()
            return format_undo_unavailable()

    if action is not None and action.action_type == "confirm_delete":
        if normalized_text == "ยืนยัน":
            try:
                item = confirm_delete_transaction(
                    line_user_id,
                    action.version,
                    expected_action_id=action.action_id,
                    target_transaction_id=action.target_transaction_id,
                    now=_utc_now(),
                    session=session,
                )
            except PendingActionConflictError:
                return PENDING_ACTION_RETRY_REPLY
            if item is None:
                return "ไม่พบรายการที่ต้องการลบ (อาจถูกลบไปแล้ว)"
            return format_delete_success(_transaction_data(item))
        if normalized_text == "ยกเลิก":
            if not delete_pending_action(
                line_user_id,
                action.version,
                expected_action_id=action.action_id,
                now=_utc_now(),
                session=session,
            ):
                return PENDING_ACTION_RETRY_REPLY
            return "ยกเลิกการลบแล้ว"
        if normalized_text == "เลิกทำ":
            return format_delete_not_confirmed_yet()

    if action is not None and action.action_type == "confirm_delete_all":
        if normalized_text == "ยืนยัน":
            try:
                execute_delete_all(
                    line_user_id,
                    action.version,
                    expected_action_id=action.action_id,
                    now=_utc_now(),
                    session=session,
                )
            except PendingActionConflictError:
                return PENDING_ACTION_RETRY_REPLY
            return format_delete_all_success()
        if normalized_text == "ยกเลิก":
            if not delete_pending_action(
                line_user_id,
                action.version,
                expected_action_id=action.action_id,
                now=_utc_now(),
                session=session,
            ):
                return PENDING_ACTION_RETRY_REPLY
            return format_delete_all_cancelled()
        if normalized_text == "เลิกทำ":
            return format_undo_unavailable()

    if action is not None and action.action_type == "undo_delete":
        if normalized_text == "เลิกทำ":
            try:
                outcome = restore_deleted_transaction(
                    line_user_id,
                    action.version,
                    expected_action_id=action.action_id,
                    now=_utc_now(),
                    session=session,
                )
            except PendingActionConflictError:
                return PENDING_ACTION_RETRY_REPLY
            if outcome.outcome == RestoreOutcome.ID_COLLISION:
                return format_undo_id_collision()
            assert outcome.transaction is not None
            return format_undo_restored(_transaction_data(outcome.transaction))
        if normalized_text == "ยืนยัน":
            return format_unknown_message()

    if normalized_text == "เลิกทำ":
        return format_undo_unavailable()

    try:
        pending = get_pending_transaction(
            line_user_id,
            now=processing_time,
            session=session,
        )
    except PendingTransactionConflictError:
        return "รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"
    if pending is not None and normalized_text == "ยกเลิก":
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

    # Recognized feedback, including errors, never enters draft follow-up logic.
    if isinstance(command, InvalidFeedbackCommand):
        if command.error == FeedbackError.INSTRUCTION:
            return format_feedback_instruction()
        if command.error == FeedbackError.COMMENT_TOO_LONG:
            return format_feedback_comment_too_long()
        return format_feedback_invalid_rating()

    if isinstance(command, FeedbackCommand):
        try:
            add_user_feedback(
                line_user_id,
                command.rating,
                comment=command.comment,
                expected_action_id=(
                    pending_delete_all.action_id if pending_delete_all is not None else None
                ),
                expected_version=(
                    pending_delete_all.version if pending_delete_all is not None else None
                ),
                now=_utc_now(),
                session=session,
            )
        except PendingActionConflictError:
            return PENDING_ACTION_RETRY_REPLY
        return format_feedback_success(command.rating)

    if isinstance(command, UnresolvedEditCommand):
        return format_unknown_message(command.reason)

    if isinstance(command, EditLatestCommand):
        if pending_delete_confirmation is not None and not invalidate_pending_action(
            line_user_id,
            pending_delete_confirmation.version,
            expected_action_id=pending_delete_confirmation.action_id,
            now=_utc_now(),
            session=session,
        ):
            return PENDING_ACTION_RETRY_REPLY
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
        if pending_delete_confirmation is not None:
            if not invalidate_pending_action(
                line_user_id,
                pending_delete_confirmation.version,
                expected_action_id=pending_delete_confirmation.action_id,
                now=_utc_now(),
                session=session,
            ):
                return PENDING_ACTION_RETRY_REPLY
            pending_delete_confirmation = None
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
            if pending_delete_confirmation is not None:
                if not invalidate_pending_action(
                    line_user_id,
                    pending_delete_confirmation.version,
                    expected_action_id=pending_delete_confirmation.action_id,
                    now=_utc_now(),
                    session=session,
                ):
                    return PENDING_ACTION_RETRY_REPLY
                pending_delete_confirmation = None
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
        if pending_delete_confirmation is not None and not invalidate_pending_action(
            line_user_id,
            pending_delete_confirmation.version,
            expected_action_id=pending_delete_confirmation.action_id,
            now=_utc_now(),
            session=session,
        ):
            return PENDING_ACTION_RETRY_REPLY
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
        if pending_delete_all is not None and not invalidate_pending_action(
            line_user_id,
            pending_delete_all.version,
            expected_action_id=pending_delete_all.action_id,
            now=_utc_now(),
            session=session,
        ):
            return PENDING_ACTION_RETRY_REPLY
        goal = set_savings_goal(
            line_user_id,
            command.description or "เป้าหมายการออม",
            command.amount,
            session=session,
        )
        return "ตั้งเป้าหมายเรียบร้อยแล้ว\n" + format_savings_goal(_goal_data(goal))

    if isinstance(command, SavingsProgressCommand):
        if pending_delete_all is not None and not invalidate_pending_action(
            line_user_id,
            pending_delete_all.version,
            expected_action_id=pending_delete_all.action_id,
            now=_utc_now(),
            session=session,
        ):
            return PENDING_ACTION_RETRY_REPLY
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

    if command.kind == CommandKind.DELETE_ALL:
        summary = get_user_data_summary(line_user_id, now=processing_time, session=session)
        if not summary.has_any_data:
            return format_delete_all_no_data()
        if action is not None:
            if not replace_pending_action_with_delete_all(
                line_user_id,
                action.version,
                expected_action_id=action.action_id,
                expected_action_type=action.action_type,
                now=_utc_now(),
                session=session,
            ):
                return PENDING_ACTION_RETRY_REPLY
        elif not create_pending_action(
            line_user_id,
            action_type="confirm_delete_all",
            target_transaction_id=DELETE_ALL_TARGET_SENTINEL,
            now=_utc_now(),
            session=session,
        ):
            return PENDING_ACTION_RETRY_REPLY
        return format_delete_all_confirmation_prompt(summary.transaction_count)

    if command.kind == CommandKind.DELETE_LATEST:
        item = get_latest_transaction(line_user_id, session=session)
        if action is not None and action.action_type == "undo_delete":
            if item is None:
                return "ยังไม่มีรายการให้ลบ"
            if not replace_pending_action_with_confirm(
                line_user_id,
                action.version,
                expected_action_id=action.action_id,
                expected_action_type="undo_delete",
                target_transaction_id=item.id,
                now=_utc_now(),
                session=session,
            ):
                return PENDING_ACTION_RETRY_REPLY
            return format_delete_confirmation_prompt(_transaction_data(item))
        if pending_delete_confirmation is not None and not invalidate_pending_action(
            line_user_id,
            pending_delete_confirmation.version,
            expected_action_id=pending_delete_confirmation.action_id,
            now=_utc_now(),
            session=session,
        ):
            return PENDING_ACTION_RETRY_REPLY
        if item is None:
            return "ยังไม่มีรายการให้ลบ"
        if not create_pending_action(
            line_user_id,
            action_type="confirm_delete",
            target_transaction_id=item.id,
            now=_utc_now(),
            session=session,
        ):
            return PENDING_ACTION_RETRY_REPLY
        return format_delete_confirmation_prompt(_transaction_data(item))

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


def _safe_csv_text(value: str | None) -> str:
    text = value or ""
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


# The fallback also sends slash-containing malformed tokens through the same
# fixed 404 response, including its no-store/nosniff headers.
@app.get("/export/{token:path}", include_in_schema=False)
@app.get("/export/{token}")
def download_export(token: str) -> Response:
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

    def not_found() -> Response:
        return Response(
            content=b'{"detail":"Not Found"}',
            status_code=404,
            media_type="application/json",
            headers=headers,
        )

    # token_urlsafe(32) produces 43 unpadded URL-safe base64 characters.
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        return not_found()

    token_hash = sha256(token.encode("utf-8")).hexdigest()
    with session_scope() as session:
        items = fetch_export_transactions(token_hash, now=_utc_now(), session=session)
        if not items:
            return not_found()
        output = io.StringIO(newline="")
        writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["วันที่", "ประเภท", "หมวดหมู่", "รายการ", "จำนวนเงิน", "บันทึกเมื่อ"])
        for item in items:
            created_at = item.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            writer.writerow([
                item.occurred_on.isoformat(),
                "รายรับ" if item.transaction_type == "income" else "รายจ่าย",
                _safe_csv_text(item.category),
                _safe_csv_text(item.description),
                f"{item.amount:.2f}",
                created_at.astimezone(BANGKOK).isoformat(),
            ])
        content = output.getvalue().encode("utf-8-sig")

    headers["Content-Disposition"] = 'attachment; filename="mootoon_export.csv"'
    return Response(content=content, media_type="text/csv; charset=utf-8", headers=headers)


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
        event_type = event.get("type")

        if (
            not isinstance(event_id, str)
            or event_type not in ("message", "follow")
            or not isinstance(source, dict)
            or source.get("type") != "user"
            or not isinstance(source.get("userId"), str)
        ):
            continue
        if event_type == "message" and (
            not isinstance(message, dict) or message.get("type") != "text"
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
                if event_type == "follow":
                    response_text = format_onboarding_welcome()
                else:
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
