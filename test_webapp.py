"""Read-only MINI App contracts; no live LINE calls or real database access."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import app as app_module
import database
import repository
import webapp_auth as auth
import webapp_routes as routes
from models import (
    Base,
    ExportToken,
    PendingAction,
    PendingTransaction,
    ProcessedWebhookEvent,
    SavingsGoal,
    Transaction,
    UserFeedback,
)


A = "U" + "a" * 32
B = "U" + "b" * 32
EMPTY = "U" + "c" * 32
D = "U" + "d" * 32
CHANNEL = "1234567890"
LIFF_ID = CHANNEL + "-Developing"
NOW = datetime(2026, 10, 10, 10, 30, tzinfo=timezone.utc)
PATHS = ("/api/me/summary", "/api/me/transactions/recent", "/api/me/savings")


def raw_token(subject):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'HS256'})}.{encode({'sub': subject})}.c2lnbmF0dXJl"


TOKEN_A = raw_token(A)
TOKEN_B = raw_token(B)
TOKEN_EMPTY = raw_token(EMPTY)
TOKEN_D = raw_token(D)


def headers(token=TOKEN_A):
    return {"Authorization": f"Bearer {token}"}


def claims(subject=A):
    return {
        "iss": "https://access.line.me",
        "aud": CHANNEL,
        "exp": int(NOW.timestamp()) + 3600,
        "sub": subject,
    }


@pytest.fixture
def web(tmp_path, monkeypatch):
    engine = database.configure_database(f"sqlite:///{tmp_path / 'webapp-test.db'}")
    monkeypatch.setenv("WEBAPP_ENABLED", "true")
    monkeypatch.setenv("MINIAPP_DEVELOPING_LIFF_ID", LIFF_ID)
    monkeypatch.setenv("MINIAPP_DEVELOPING_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.setattr(app_module, "LINE_CHANNEL_SECRET", "test-secret")
    monkeypatch.setattr(app_module, "LINE_CHANNEL_ACCESS_TOKEN", "test-access")
    monkeypatch.setattr(routes, "utc_now", lambda: NOW)
    monkeypatch.setattr(auth.time, "time", lambda: NOW.timestamp())
    state = {"calls": [], "response": None, "exception": None, "sql": []}

    def verify(request):
        state["calls"].append(request)
        if state["exception"]:
            raise state["exception"]
        if state["response"] is not None:
            return state["response"]
        form = parse_qs(request.content.decode())
        subject = {TOKEN_A: A, TOKEN_B: B, TOKEN_EMPTY: EMPTY, TOKEN_D: D}.get(form["id_token"][0], A)
        return httpx.Response(200, json=claims(subject))

    with TestClient(app_module.app) as client:
        pooled = app_module.app.state.webapp_verify_client
        assert pooled.timeout.read == 5.0
        # A real AsyncClient still performs encoding/timeout handling through a mock transport.
        mocked = httpx.AsyncClient(transport=httpx.MockTransport(verify), timeout=5.0)
        monkeypatch.setattr(app_module.app.state, "webapp_verify_client", mocked)

        def record_sql(connection, cursor, statement, parameters, context, executemany):
            state["sql"].append(statement)

        event.listen(engine, "before_cursor_execute", record_sql)
        try:
            yield client, engine, state
        finally:
            event.remove(engine, "before_cursor_execute", record_sql)
            asyncio.run(mocked.aclose())
    assert pooled.is_closed
    engine.dispose()


def seed(engine):
    with Session(engine) as session:
        session.add_all([
            Transaction(line_user_id=A, transaction_type="income", amount_satang=10001,
                        category="รายรับ A", description="<img src=x onerror=alert(1)>",
                        occurred_on=date(2026, 10, 1), created_at=NOW),
            Transaction(line_user_id=A, transaction_type="expense", amount_satang=2050,
                        category="อาหาร A", description=None,
                        occurred_on=date(2026, 10, 2), created_at=NOW),
            Transaction(line_user_id=B, transaction_type="expense", amount_satang=9012,
                        category="อาหาร B", description="B only",
                        occurred_on=date(2026, 10, 3), created_at=NOW),
            SavingsGoal(line_user_id=A, title="หนังสือ A", target_satang=30000,
                        saved_satang=10001, deadline=date(2026, 12, 31)),
            SavingsGoal(line_user_id=B, title="หนังสือ B", target_satang=10000,
                        saved_satang=15001),
        ])
        session.commit()


def assert_secure(response, api=True):
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    if api:
        assert response.headers["Vary"] == "Authorization"


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("authorization", [
    None, "", "Bearer", "Bearer ", "Basic abc", "Bearer abc", "Bearer a.b.c",
    "Bearer abc.def.ghi", "Bearer e30.e30.", "Bearer e30.e30.bad=",
    "Bearer e30.e30.c2ln token", "Bearer " + "a" * 16385,
])
def test_invalid_local_credentials_never_call_line_or_database(web, path, authorization):
    client, _, state = web
    supplied = {} if authorization is None else {"Authorization": authorization}
    response = client.get(path, headers=supplied)
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    assert state["calls"] == []
    assert state["sql"] == []
    assert_secure(response)


def test_duplicate_bearer_rejected(web):
    client, _, state = web
    response = client.get(PATHS[0], headers=[("Authorization", f"Bearer {TOKEN_A}"),
                                            ("Authorization", f"Bearer {TOKEN_B}")])
    assert response.status_code == 401
    assert not state["calls"] and not state["sql"]


def test_deeply_nested_token_json_rejected_locally(web):
    client, _, state = web
    nested = base64.urlsafe_b64encode(("[" * 2000 + "0" + "]" * 2000).encode()).decode().rstrip("=")
    response = client.get(PATHS[0], headers=headers(f"e30.{nested}.c2ln"))
    assert response.status_code == 401
    assert not state["calls"] and not state["sql"]


def test_deeply_nested_upstream_json_fails_closed(web):
    client, _, state = web
    state["response"] = httpx.Response(200, content=("[" * 2000 + "0" + "]" * 2000).encode())
    assert client.get(PATHS[0], headers=headers()).status_code == 503
    assert not state["sql"]


def test_verify_contract_encoding_timeout_and_per_request_verification(web):
    client, _, state = web
    for path in PATHS:
        assert client.get(path, headers=headers()).status_code == 200
    assert len(state["calls"]) == 3
    for request in state["calls"]:
        assert request.method == "POST"
        assert str(request.url) == auth.VERIFY_URL
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert parse_qs(request.content.decode()) == {"id_token": [TOKEN_A], "client_id": [CHANNEL]}
        assert request.content == f"id_token={TOKEN_A}&client_id={CHANNEL}".encode()
        assert "authorization" not in request.headers
        assert all(value == 5.0 for value in request.extensions["timeout"].values())


@pytest.mark.parametrize("field,value,status", [
    ("iss", "https://attacker.example", 401), ("iss", "https://access.line.me/", 401),
    ("aud", "review-channel", 401), ("aud", "published-channel", 401),
    ("aud", "messaging-channel", 401), ("aud", "unrelated-channel", 401),
    ("exp", int(NOW.timestamp()), 401), ("exp", int(NOW.timestamp()) - 1, 401),
    ("sub", None, 401), ("sub", "", 401), ("sub", 1, 401),
    ("sub", " U" + "a" * 32, 401), ("sub", "U-alice", 401), ("sub", "U" + "A" * 32, 401),
    ("iss", None, 503), ("aud", [CHANNEL], 503), ("aud", int(CHANNEL), 503),
    ("exp", str(int(NOW.timestamp()) + 100), 503), ("exp", True, 503), ("exp", None, 503),
])
def test_claim_validation_fails_closed_before_database(web, field, value, status):
    client, _, state = web
    response_claims = claims()
    response_claims[field] = value
    state["response"] = httpx.Response(200, json=response_claims)
    response = client.get(PATHS[0], headers=headers())
    assert response.status_code == status
    assert response.json()["code"] == ("UNAUTHORIZED" if status == 401 else "AUTH_UNAVAILABLE")
    assert len(state["calls"]) == 1
    assert state["sql"] == []
    assert_secure(response)


@pytest.mark.parametrize("field,status", [("iss", 503), ("aud", 503), ("exp", 503), ("sub", 401)])
def test_missing_claim(web, field, status):
    client, _, state = web
    result = claims()
    result.pop(field)
    state["response"] = httpx.Response(200, json=result)
    assert client.get(PATHS[0], headers=headers()).status_code == status
    assert not state["sql"]


@pytest.mark.parametrize("body", [[], None, "claims", {"ok": True}])
def test_malformed_line_structure(web, body):
    client, _, state = web
    state["response"] = httpx.Response(200, json=body)
    assert client.get(PATHS[0], headers=headers()).status_code == 503
    assert not state["sql"]


@pytest.mark.parametrize("status", [200, 204, 400, 401, 403, 429, 500, 502, 503, 302])
def test_upstream_status_and_bad_json(web, status):
    client, _, state = web
    state["response"] = httpx.Response(status, content=b"not-json-sensitive-response")
    response = client.get(PATHS[0], headers=headers())
    assert response.status_code == (401 if status in (400, 401) else 503)
    assert len(state["calls"]) == 1  # No automatic retry.
    assert not state["sql"]
    assert_secure(response)


@pytest.mark.parametrize("exception", [httpx.ReadTimeout("sensitive"), httpx.ConnectError("sensitive")])
def test_timeout_and_network_fail_closed_no_fallback(web, exception):
    client, engine, state = web
    seed(engine)
    assert client.get(PATHS[0], headers=headers()).status_code == 200
    state["sql"].clear()
    state["exception"] = exception
    response = client.get(PATHS[0], headers=headers())
    assert response.status_code == 503
    assert response.json()["code"] == "AUTH_UNAVAILABLE"
    assert "sensitive" not in response.text
    assert len(state["calls"]) == 2
    assert not state["sql"]


def test_no_query_or_session_before_authentication_success(web, monkeypatch):
    client, _, state = web
    observed = []
    original = routes.monthly_summary

    def read(owner, year, month, *, session):
        assert len(state["calls"]) == 1
        assert not session.autoflush
        assert isinstance(session, routes.ReadOnlySession)
        observed.append(session)
        return original(owner, year, month, session=session)

    monkeypatch.setattr(routes, "monthly_summary", read)
    assert client.get(PATHS[0], headers=headers()).status_code == 200
    assert not observed[0].in_transaction()


def test_two_user_isolation_and_exact_summary(web):
    client, engine, _ = web
    seed(engine)
    for token, income, expense, balance, count, category, title in [
        (TOKEN_A, "100.01", "20.50", "79.51", 2, "อาหาร A", "หนังสือ A"),
        (TOKEN_B, "0.00", "90.12", "-90.12", 1, "อาหาร B", "หนังสือ B"),
    ]:
        summary = client.get(PATHS[0], headers=headers(token)).json()
        assert summary == {
            "currency": "THB", "timezone": "Asia/Bangkok", "year_ce": 2026,
            "year_be": 2569, "month": 10, "income_baht": income, "expense_baht": expense,
            "balance_baht": balance, "transaction_count": count, "as_of": NOW.isoformat(),
        }
        recent = client.get(PATHS[1], headers=headers(token)).json()
        assert recent["items"][0]["category"] == category
        assert len(recent["items"]) == count
        assert all(item["category"].endswith("A" if token == TOKEN_A else "B") for item in recent["items"])
        assert all("id" not in item and "line_user_id" not in item for item in recent["items"])
        assert client.get(PATHS[2], headers=headers(token)).json()["goal"]["title"] == title


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("query", ["line_user_id=" + B, "user_id=" + B, "limit=100", "year=2020", "client_id=review"])
def test_owner_queries_and_all_unexpected_queries_rejected(web, path, query):
    client, _, state = web
    response = client.get(f"{path}?{query}", headers=headers())
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    assert len(state["calls"]) == 1
    assert not state["sql"]
    assert_secure(response)


def test_body_headers_and_unverified_claims_cannot_change_verified_owner(web):
    client, engine, state = web
    seed(engine)
    # Token locally claims B; the mock verifier's authoritative subject says A.
    state["response"] = httpx.Response(200, json=claims(A))
    response = client.request("GET", PATHS[0], headers={**headers(TOKEN_B), "X-Line-User-ID": B,
                              "X-Expected-Audience": "review-channel"}, json={"line_user_id": B})
    assert response.status_code == 200
    assert response.json()["income_baht"] == "100.01"


def test_empty_user_returns_empty_not_auth_error(web):
    client, _, _ = web
    summary = client.get(PATHS[0], headers=headers(TOKEN_EMPTY)).json()
    assert summary["transaction_count"] == 0
    assert [summary[key] for key in ("income_baht", "expense_baht", "balance_baht")] == ["0.00"] * 3
    assert client.get(PATHS[1], headers=headers(TOKEN_EMPTY)).json()["items"] == []
    assert client.get(PATHS[2], headers=headers(TOKEN_EMPTY)).json()["goal"] is None


@pytest.mark.parametrize("moment,year,month", [
    (datetime(2026, 12, 31, 16, 59, 59, tzinfo=timezone.utc), 2026, 12),
    (datetime(2026, 12, 31, 17, 0, tzinfo=timezone.utc), 2027, 1),
])
def test_bangkok_month_year_rollover(web, monkeypatch, moment, year, month):
    client, engine, _ = web
    monkeypatch.setattr(routes, "utc_now", lambda: moment)
    with Session(engine) as session:
        for effective_date, amount in [(date(2026, 12, 31), 101), (date(2027, 1, 1), 202)]:
            session.add(Transaction(line_user_id=A, transaction_type="income", amount_satang=amount,
                                    category="rollover", occurred_on=effective_date, created_at=NOW))
        session.commit()
    data = client.get(PATHS[0], headers=headers()).json()
    assert (data["year_ce"], data["year_be"], data["month"]) == (year, year + 543, month)
    assert data["income_baht"] == ("1.01" if month == 12 else "2.02")
    assert datetime.fromisoformat(data["as_of"]).tzinfo is not None


def test_recent_fixed_five_full_order_and_dates(web):
    client, engine, _ = web
    specifications = [
        (date(2026, 9, 1), NOW + timedelta(days=50), "backdated"),
        (date(2026, 10, 10), NOW, "tie older ID"),
        (date(2026, 10, 10), NOW, "tie newer ID"),
        (date(2026, 10, 10), NOW + timedelta(seconds=1), "newer created"),
        (date(2026, 10, 11), NOW - timedelta(days=1), "next date"),
        (date(2026, 10, 12), NOW, "later date"),
        (date(2027, 1, 1), NOW - timedelta(days=5), "future date"),
    ]
    with Session(engine) as session:
        for effective_date, created, description in specifications:
            session.add(Transaction(line_user_id=A, transaction_type="expense", amount_satang=12345,
                                    category="food", description=description,
                                    occurred_on=effective_date, created_at=created))
        session.commit()
    response = client.get(PATHS[1], headers=headers())
    data = response.json()
    assert data["limit"] == 5
    assert [item["description"] for item in data["items"]] == [
        "future date", "later date", "next date", "newer created", "tie newer ID",
    ]
    for item in data["items"]:
        assert set(item) == {"transaction_type", "amount_baht", "category", "description", "occurred_on", "created_at"}
        assert item["amount_baht"] == "123.45"
        assert item["created_at"].endswith("+07:00")
    assert data["items"][1]["created_at"] == "2026-10-10T17:30:00+07:00"
    assert data["items"][0]["occurred_on"] == "2027-01-01"
    assert A not in response.text


@pytest.mark.parametrize("target,saved,remaining,progress", [
    (30000, 10001, "199.99", "33.34"), (10000, 0, "100.00", "0.00"),
    (10000, 10000, "0.00", "100.00"), (10000, 15001, "0.00", "100.00"),
    (20000, 1, "199.99", "0.01"),
])
def test_savings_exact_partial_complete_overfunded_half_up(web, target, saved, remaining, progress):
    client, engine, _ = web
    with Session(engine) as session:
        session.add(SavingsGoal(line_user_id=A, title="<script>goal</script>", target_satang=target,
                               saved_satang=saved, deadline=date(2026, 12, 31)))
        session.commit()
    goal = client.get(PATHS[2], headers=headers()).json()["goal"]
    assert goal == {
        "title": "<script>goal</script>", "target_baht": f"{target // 100}.{target % 100:02d}",
        "saved_baht": f"{saved // 100}.{saved % 100:02d}", "remaining_baht": remaining,
        "progress_percent": progress, "deadline": "2026-12-31",
    }


def snapshot(engine):
    with engine.connect() as connection:
        return {table.name: [tuple(row) for row in connection.execute(select(table).order_by(*table.primary_key.columns))]
                for table in Base.metadata.sorted_tables}


def seed_state(engine):
    seed(engine)
    with Session(engine) as session:
        session.add(PendingTransaction(line_user_id=A, draft_id="d" * 32, transaction_type="expense",
                                       amount_satang=123, category="draft", description="keep",
                                       occurred_on=NOW.date(), created_at=NOW - timedelta(hours=3),
                                       expires_at=NOW - timedelta(hours=2), version=7))
        for index, action_type in enumerate(["confirm_delete", "undo_delete", "confirm_delete_all", "confirm_delete"]):
            owner = [A, B, EMPTY, D][index]
            session.add(PendingAction(line_user_id=owner, action_id=str(index) * 32, version=9,
                                     action_type=action_type, target_transaction_id=-1 if index == 2 else 1,
                                     created_at=NOW - timedelta(hours=1),
                                     expires_at=NOW - timedelta(seconds=1) if index == 3 else NOW + timedelta(hours=1),
                                     snapshot_transaction_id=1 if index == 1 else None,
                                     snapshot_transaction_type="expense" if index == 1 else None,
                                     snapshot_amount_satang=123 if index == 1 else None,
                                     snapshot_description="snapshot keep" if index == 1 else None,
                                     snapshot_category="food" if index == 1 else None,
                                     snapshot_occurred_on=NOW.date() if index == 1 else None,
                                     snapshot_created_at=NOW if index == 1 else None))
        session.add(ExportToken(token_hash="e" * 64, line_user_id=A, created_at=NOW - timedelta(days=1),
                                expires_at=NOW - timedelta(hours=1)))
        session.add(UserFeedback(line_user_id=A, rating=4, comment="feedback keep", created_at=NOW))
        session.add(ProcessedWebhookEvent(webhook_event_id="keep-event", processed_at=NOW,
                                          response_text="keep-response", reply_sent=True))
        session.commit()


def test_all_tables_byte_identical_and_no_mutating_sql_for_repeated_web_reads(web, monkeypatch):
    client, engine, state = web
    seed_state(engine)
    before = snapshot(engine)

    def forbidden(*args, **kwargs):
        pytest.fail("Web requests must not call mutation, cleanup, export, or webhook helpers")

    permitted = {"monthly_summary", "list_recent_transactions", "get_savings_goal"}
    # Make accidentally importing/calling any other public repository helper fail.
    for name in dir(repository):
        value = getattr(repository, name)
        if not name.startswith("_") and name not in permitted and callable(value) and getattr(value, "__module__", None) == "repository" and not isinstance(value, type):
            monkeypatch.setattr(repository, name, forbidden)
            if hasattr(app_module, name):
                monkeypatch.setattr(app_module, name, forbidden)
    monkeypatch.setattr(app_module, "handle_text_message", forbidden)
    state["sql"].clear()
    for _ in range(3):
        for token in (TOKEN_A, TOKEN_B, TOKEN_EMPTY, TOKEN_D):
            for path in PATHS:
                assert client.get(path, headers=headers(token)).status_code == 200
        for path in ("/app/", "/app/config.json", "/app/assets/app.js", "/app/assets/styles.css"):
            assert client.get(path).status_code == 200
        assert client.get(PATHS[0]).status_code == 401
        state["response"] = httpx.Response(503)
        assert client.get(PATHS[0], headers=headers()).status_code == 503
        state["response"] = None
    assert state["sql"]
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in state["sql"])
    assert snapshot(engine) == before
    assert set(before) == {"transactions", "savings_goals", "pending_transactions", "pending_actions",
                           "export_tokens", "user_feedback", "processed_webhook_events"}


@pytest.mark.parametrize("operation", ["flush", "commit", "update", "insert", "delete", "text"])
def test_read_session_defensively_blocks_accidental_writes(web, operation):
    _, engine, _ = web
    with routes.ReadOnlySession(bind=engine, autoflush=False) as session:
        event.listen(session, "do_orm_execute", routes._only_select)
        with pytest.raises(routes.ReadOnlyViolation):
            if operation in ("flush", "commit"):
                getattr(session, operation)()
            elif operation == "update":
                session.execute(update(Transaction).values(category="mutated"))
            elif operation == "insert":
                session.execute(insert(Transaction))
            elif operation == "delete":
                session.execute(Transaction.__table__.delete())
            else:
                session.execute(text("DELETE FROM transactions"))
        session.rollback()


@pytest.mark.parametrize("path,query", list(zip(PATHS, ["monthly_summary", "list_recent_transactions", "get_savings_goal"])))
def test_database_errors_are_sanitized_and_not_empty(web, monkeypatch, path, query):
    client, _, _ = web

    def failed(*args, **kwargs):
        raise OperationalError("sensitive-sql", {}, Exception("sensitive-details"))

    monkeypatch.setattr(routes, query, failed)
    response = client.get(path, headers=headers())
    assert response.status_code == 503
    assert response.json()["code"] == "DATA_UNAVAILABLE"
    assert "sensitive" not in response.text
    assert_secure(response)


def test_unexpected_web_errors_are_sanitized_and_no_store(web, monkeypatch, caplog):
    client, _, _ = web

    def failed(*args, **kwargs):
        raise RuntimeError("sensitive-details " + TOKEN_A)

    monkeypatch.setattr(routes, "monthly_summary", failed)
    response = client.get(PATHS[0], headers=headers())
    assert response.status_code == 503
    assert response.json()["code"] == "DATA_UNAVAILABLE"
    assert "sensitive-details" not in response.text and TOKEN_A not in response.text
    assert "sensitive-details" not in caplog.text and TOKEN_A not in caplog.text
    assert_secure(response)


def test_static_config_security_headers_and_existing_routes_not_shadowed(web):
    client, _, _ = web
    for path, content_type in [("/app/", "text/html"), ("/app/assets/app.js", "javascript"),
                               ("/app/assets/styles.css", "text/css")]:
        response = client.get(path)
        assert response.status_code == 200
        assert content_type in response.headers["Content-Type"]
        assert_secure(response, api=False)
    config = client.get("/app/config.json")
    assert config.json() == {"liff_id": LIFF_ID, "environment": "Developing"}
    assert_secure(config, api=False)
    assert client.get("/").json()["service"] == "mootoon-line-bot"
    assert client.get("/ready").json() == {"status": "ready"}
    assert client.post("/webhook", json={"events": []}).status_code == 400
    body = b'{"events": []}'
    signature = base64.b64encode(hmac.new(b"test-secret", body, hashlib.sha256).digest()).decode()
    assert client.post("/webhook", content=body, headers={"x-line-signature": signature}).status_code == 200
    assert client.get("/export/bad").status_code == 404
    csp = config.headers["Content-Security-Policy"]
    assert "*" not in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "uts-front" not in csp


@pytest.mark.parametrize("enabled", [True, False])
def test_existing_valid_csv_export_works_with_web_enabled_or_disabled(web, monkeypatch, enabled):
    client, engine, state = web
    seed(engine)
    token = "x" * 43
    with Session(engine) as session:
        session.add(ExportToken(token_hash=hashlib.sha256(token.encode()).hexdigest(), line_user_id=A,
                                created_at=NOW, expires_at=NOW + timedelta(minutes=5)))
        session.commit()
    monkeypatch.setattr(app_module, "_utc_now", lambda: NOW)
    monkeypatch.setattr(app_module.app.state, "webapp_config", auth.WebAppConfig(enabled, LIFF_ID, CHANNEL))
    response = client.get("/export/" + token)
    assert response.status_code == 200
    assert "text/csv" in response.headers["Content-Type"]
    assert "อาหาร A" in response.content.decode("utf-8-sig")
    assert "อาหาร B" not in response.content.decode("utf-8-sig")
    assert state["calls"] == []


@pytest.mark.parametrize("path", [
    "/app/assets/%2e%2e/%2e%2e/app.py", "/app/assets/%2e%2e/index.html",
    "/app/assets/%2e%2e%5c%2e%2e%5c.env", "/app/assets/app.py", "/app/assets/.env",
    "/app/assets/missing.js", "/app/not-a-route", "/api/me/not-a-route",
])
def test_traversal_and_unintended_files_never_served(web, path):
    client, _, _ = web
    response = client.get(path)
    assert response.status_code == 404
    assert_secure(response, api=path.startswith("/api/"))


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", PATHS)
def test_financial_surface_is_get_only(web, method, path):
    client, _, state = web
    response = client.request(method, path, headers=headers())
    assert response.status_code == 405
    assert not state["sql"] and not state["calls"]
    assert_secure(response)


@pytest.mark.parametrize("config", [
    auth.WebAppConfig(False, LIFF_ID, CHANNEL), auth.WebAppConfig(True, "", CHANNEL),
    auth.WebAppConfig(True, LIFF_ID, ""), auth.WebAppConfig(True, "wrong", CHANNEL),
    auth.WebAppConfig(True, "9999999999-Review", CHANNEL),
])
def test_disabled_or_incomplete_config_is_controlled_and_bot_remains_ready(web, monkeypatch, config):
    client, _, state = web
    monkeypatch.setattr(app_module.app.state, "webapp_config", config)
    for path in (*PATHS, "/app/", "/app/config.json", "/app/assets/app.js"):
        response = client.get(path, headers=headers())
        assert response.status_code == 503
        assert response.json()["code"] == "WEBAPP_UNAVAILABLE"
        assert_secure(response, api=path.startswith("/api/"))
    assert state["calls"] == [] and state["sql"] == []
    assert client.get("/").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.post("/webhook", json={}).status_code == 400
    assert client.get("/export/bad").status_code == 404


def test_environment_config_defaults_to_disabled(monkeypatch):
    for key in ("WEBAPP_ENABLED", "MINIAPP_DEVELOPING_LIFF_ID", "MINIAPP_DEVELOPING_CHANNEL_ID"):
        monkeypatch.delenv(key, raising=False)
    assert not auth.WebAppConfig.from_environment().available


def test_tokens_financial_bodies_and_upstream_details_not_logged(web, caplog):
    client, engine, state = web
    seed(engine)
    with caplog.at_level(logging.DEBUG):
        assert client.get(PATHS[1], headers=headers()).status_code == 200
        state["response"] = httpx.Response(200, content=b"upstream-sensitive-body")
        assert client.get(PATHS[0], headers=headers()).status_code == 503
        access_logger = logging.getLogger("uvicorn.access")
        access_logger.info('%s - "%s %s HTTP/%s" %d', "client", "GET",
                           "/app/?id_token=" + TOKEN_A, "1.1", 200)
    assert TOKEN_A not in caplog.text
    assert "upstream-sensitive-body" not in caplog.text
    assert "onerror=alert" not in caplog.text
    assert "id_token=" not in caplog.text


def test_frontend_source_authentication_rendering_and_lifecycle_contract():
    root = Path(__file__).resolve().parent / "webapp"
    script = (root / "assets" / "app.js").read_text(encoding="utf-8")
    html = (root / "index.html").read_text(encoding="utf-8")
    for forbidden in ("localStorage", "sessionStorage", "IndexedDB", "indexedDB", "innerHTML",
                      "outerHTML", "insertAdjacentHTML", "getProfile", "getDecodedIDToken", "console."):
        assert forbidden not in script
    assert "element.textContent = text" in script
    assert 'const token = liff.getIDToken();' in script
    null_branch = script.split("if (!token) {", 1)[1].split("}", 1)[0]
    assert "authRequired();" in null_branch and "return;" in null_branch
    assert script.index("if (!token)") < script.index('await financial("/api/me/summary"')
    assert script.index('await financial("/api/me/summary"') < script.index('"/api/me/transactions/recent", panels')
    assert "new AbortController()" in script and "controller.abort()" in script
    assert "current === generation && !active.signal.aborted" in script
    assert "if (isCurrent()) render(data)" in script
    assert 'error.status === 401' in script and "invalidate(\"กรุณาเข้าสู่ระบบ LINE" in script
    assert 'pagehide' in script and 'pageshow' in script and 'visibilitychange' in script
    assert 'new URL("/app/", window.location.origin).href' in script
    assert "Number(year) + 543" in script and "Number(goal.saved" not in script
    assert "withLoginOnExternalBrowser: false" in script
    assert "versions/2.31.2/sdk.js" in html
    assert "openid" in script
