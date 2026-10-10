"""Static web shell and the three bounded, rollback-only financial reads."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.staticfiles import StaticFiles

import database
from repository import get_savings_goal, list_recent_transactions, monthly_summary
from webapp_auth import (
    ERROR_MESSAGES,
    WebAppError,
    is_web_path,
    require_webapp,
    verified_owner,
)


ROOT = Path(__file__).resolve().parent / "webapp"
BANGKOK = ZoneInfo("Asia/Bangkok")
router = APIRouter()
# SDK 2.31.2's init/login use the LINE API/Access origins and its translation CDN.
# The SDK loads platform bridge extensions from static.line-scdn.net.
# No UTS/analytics origin, wildcard, inline script, or eval permission is granted.
CSP = (
    "default-src 'none'; base-uri 'none'; object-src 'none'; "
    "script-src 'self' https://static.line-scdn.net; style-src 'self'; "
    "connect-src 'self' https://api.line.me https://access.line.me "
    "https://liffsdk.line-scdn.net; "
    "frame-src https://access.line.me https://liff.line.me; "
    "frame-ancestors 'none'; form-action 'none'"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def bangkok_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BANGKOK).isoformat()


class ReadOnlyViolation(SQLAlchemyError):
    pass


class ReadOnlySession(Session):
    """Prevent an accidental explicit ORM flush/commit as well as autoflush."""

    def flush(self, objects=None) -> None:
        raise ReadOnlyViolation("Web sessions cannot flush")

    def commit(self) -> None:
        raise ReadOnlyViolation("Web sessions cannot commit")


def _only_select(state) -> None:
    if not state.is_select or state.is_insert or state.is_update or state.is_delete:
        raise ReadOnlyViolation("Web sessions only allow SELECT")


def read_session(owner: Annotated[str, Depends(verified_owner)]) -> Iterator[Session]:
    # Authentication (including query validation) completes before session creation.
    session = ReadOnlySession(bind=database.engine, autoflush=False, expire_on_commit=False)
    event.listen(session, "do_orm_execute", _only_select)
    try:
        yield session
    except SQLAlchemyError:
        raise WebAppError(503, "DATA_UNAVAILABLE") from None
    finally:
        try:
            session.rollback()
        finally:
            session.close()


Owner = Annotated[str, Depends(verified_owner)]
ReadSession = Annotated[Session, Depends(read_session)]


@router.get("/app/", include_in_schema=False)
def shell(request: Request):
    require_webapp(request)
    return FileResponse(ROOT / "index.html", media_type="text/html")


@router.get("/app/config.json", include_in_schema=False)
def frontend_config(request: Request):
    config = require_webapp(request)
    return {"liff_id": config.liff_id, "environment": "Developing"}


class AppAssets(StaticFiles):
    async def __call__(self, scope, receive, send):
        require_webapp(Request(scope))
        await super().__call__(scope, receive, send)


@router.get("/api/me/summary")
def summary(owner: Owner, session: ReadSession):
    now = utc_now()
    local = now.astimezone(BANGKOK)
    totals = monthly_summary(owner, local.year, local.month, session=session)
    return {
        "currency": "THB",
        "timezone": "Asia/Bangkok",
        "year_ce": local.year,
        "year_be": local.year + 543,
        "month": local.month,
        "income_baht": money(totals["income"]),
        "expense_baht": money(totals["expense"]),
        "balance_baht": money(totals["balance"]),
        "transaction_count": totals["transaction_count"],
        "as_of": now.isoformat(),
    }


@router.get("/api/me/transactions/recent")
def recent(owner: Owner, session: ReadSession):
    now = utc_now()
    items = list_recent_transactions(owner, limit=5, session=session)
    return {
        "limit": 5,
        "items": [
            {
                "transaction_type": item.transaction_type,
                "amount_baht": money(item.amount),
                "category": item.category,
                "description": item.description,
                "occurred_on": item.occurred_on.isoformat(),
                "created_at": bangkok_timestamp(item.created_at),
            }
            for item in items
        ],
        "as_of": now.isoformat(),
    }


@router.get("/api/me/savings")
def savings(owner: Owner, session: ReadSession):
    now = utc_now()
    goal = get_savings_goal(owner, session=session)
    result = None
    if goal is not None:
        progress = (Decimal(goal.saved_satang) * 100 / Decimal(goal.target_satang))
        result = {
            "title": goal.title,
            "target_baht": money(goal.target_amount),
            "saved_baht": money(goal.saved_amount),
            "remaining_baht": money(goal.remaining_amount),
            "progress_percent": money(min(Decimal(100), max(Decimal(0), progress))),
            "deadline": goal.deadline.isoformat() if goal.deadline else None,
        }
    return {"goal": result, "as_of": now.isoformat()}


def register_webapp(app: FastAPI) -> None:
    def error_response(status_code: int, code: str) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={"code": code, "message": ERROR_MESSAGES[code]},
        )

    @app.exception_handler(WebAppError)
    async def web_error(request: Request, exc: WebAppError):
        return error_response(exc.status_code, exc.code)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        web_path = is_web_path(request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            if not web_path:
                raise
            # Unexpected web failures must also be sanitized and non-cacheable.
            # Do not format/log exceptions that could embed credentials or data.
            code = "DATA_UNAVAILABLE" if request.url.path.startswith("/api/me/") else "WEBAPP_UNAVAILABLE"
            response = error_response(503, code)
        if web_path:
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = CSP
            if request.url.path.startswith("/api/me/"):
                response.headers["Vary"] = "Authorization"
        return response

    app.include_router(router)
    app.mount("/app/assets", AppAssets(directory=ROOT / "assets"), name="webapp-assets")
