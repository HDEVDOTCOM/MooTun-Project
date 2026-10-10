"""Developing-only MINI App configuration and per-request LINE verification."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import time
from dataclasses import dataclass

import httpx
from fastapi import Request


VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
VERIFY_TIMEOUT_SECONDS = 5.0
LINE_SUBJECT = re.compile(r"U[0-9a-f]{32}")
JWT_SHAPE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
ERROR_MESSAGES = {
    "UNAUTHORIZED": "กรุณาเข้าสู่ระบบ LINE อีกครั้ง",
    "AUTH_UNAVAILABLE": "การยืนยัน LINE ไม่พร้อมใช้งานชั่วคราว กรุณาลองอีกครั้ง",
    "DATA_UNAVAILABLE": "ข้อมูลไม่พร้อมใช้งานชั่วคราว กรุณาลองอีกครั้ง",
    "WEBAPP_UNAVAILABLE": "หน้าเว็บไม่พร้อมใช้งานชั่วคราว ยังใช้งานผ่านแชตได้",
    "INVALID_REQUEST": "คำขอไม่ถูกต้อง",
}


class WebAppError(Exception):
    def __init__(self, status_code: int, code: str):
        self.status_code = status_code
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WebAppConfig:
    enabled: bool
    liff_id: str
    channel_id: str

    @classmethod
    def from_environment(cls) -> WebAppConfig:
        return cls(
            enabled=os.getenv("WEBAPP_ENABLED", "false").lower() == "true",
            liff_id=os.getenv("MINIAPP_DEVELOPING_LIFF_ID", ""),
            channel_id=os.getenv("MINIAPP_DEVELOPING_CHANNEL_ID", ""),
        )

    @property
    def available(self) -> bool:
        return bool(
            self.enabled
            and re.fullmatch(r"[0-9]+-[A-Za-z0-9]+", self.liff_id)
            and re.fullmatch(r"[0-9]+", self.channel_id)
            and self.liff_id.split("-", 1)[0] == self.channel_id
        )


def is_web_path(path: str) -> bool:
    return path == "/app" or path.startswith(("/app/", "/api/me/"))


class WebAccessLogFilter(logging.Filter):
    """Do not let Uvicorn access logs record LIFF credential-bearing entry URLs."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's access record: client, method, full path, HTTP version, status.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            path = record.args[2]
            if isinstance(path, str) and is_web_path(path.split("?", 1)[0]):
                return False
        return True


def require_webapp(request: Request) -> WebAppConfig:
    config = request.app.state.webapp_config
    if not config.available:
        raise WebAppError(503, "WEBAPP_UNAVAILABLE")
    return config


def token_has_valid_shape(token: str) -> bool:
    if len(token) > 16384 or JWT_SHAPE.fullmatch(token) is None:
        return False
    parts = token.split(".")
    try:
        decoded = []
        for part in parts:
            if len(part) % 4 == 1:
                return False
            decoded.append(base64.b64decode(part + "=" * (-len(part) % 4), altchars=b"-_", validate=True))
        # Syntax only. No local claim, algorithm, or identity authorizes a request.
        return all(isinstance(json.loads(part.decode("utf-8")), dict) for part in decoded[:2])
    except (ValueError, UnicodeDecodeError, binascii.Error, RecursionError):
        return False


async def verified_owner(request: Request) -> str:
    config = require_webapp(request)
    headers = request.headers.getlist("authorization")
    if len(headers) != 1:
        raise WebAppError(401, "UNAUTHORIZED")
    match = re.fullmatch(r"Bearer ([^\s]+)", headers[0], flags=re.IGNORECASE)
    token = match.group(1) if match else ""
    if not token_has_valid_shape(token):
        raise WebAppError(401, "UNAUTHORIZED")
    try:
        response = await request.app.state.webapp_verify_client.post(
            VERIFY_URL,
            data={"id_token": token, "client_id": config.channel_id},
            timeout=VERIFY_TIMEOUT_SECONDS,
        )
    except httpx.RequestError:
        raise WebAppError(503, "AUTH_UNAVAILABLE") from None
    if response.status_code in (400, 401):
        raise WebAppError(401, "UNAUTHORIZED")
    if not response.is_success:
        raise WebAppError(503, "AUTH_UNAVAILABLE")
    try:
        claims = response.json()
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise WebAppError(503, "AUTH_UNAVAILABLE") from None
    if (
        not isinstance(claims, dict)
        or not isinstance(claims.get("iss"), str)
        or not isinstance(claims.get("aud"), str)
        or type(claims.get("exp")) is not int
    ):
        raise WebAppError(503, "AUTH_UNAVAILABLE")
    if (
        claims["iss"] != "https://access.line.me"
        or claims["aud"] != config.channel_id
        or claims["exp"] <= time.time()
    ):
        raise WebAppError(401, "UNAUTHORIZED")
    subject = claims.get("sub")
    if not isinstance(subject, str) or LINE_SUBJECT.fullmatch(subject) is None:
        raise WebAppError(401, "UNAUTHORIZED")
    # Preserve the exact verified identity, including case. No owner override exists.
    if request.query_params:
        raise WebAppError(400, "INVALID_REQUEST")
    return subject
