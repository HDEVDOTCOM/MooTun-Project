"""Phase 1G capability storage, live CSV downloads, and revocation guarantees."""

import csv
import io
import secrets
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, create_mock_engine, delete, event, func, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app as app_module
import repository
from database import configure_database, init_db
from line_api import LineTransportError
from messages import format_help_message
from models import (
    Base,
    ExportToken,
    PendingAction,
    PendingTransaction,
    ProcessedWebhookEvent,
    SavingsGoal,
    Transaction,
)
from parser import CommandKind, SimpleCommand, parse_command
from repository import (
    EXPORT_TOKEN_TTL,
    PendingActionConflictError,
    add_transaction,
    create_export_token,
    execute_delete_all,
    fetch_export_transactions,
    get_user_data_summary,
)
from test_webhook import SECRET, _post_text, _row_state


NOW = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)
USER = "U-alice"
BASE_URL = "https://mootoon.example"
CSV_HEADER = ["วันที่", "ประเภท", "หมวดหมู่", "รายการ", "จำนวนเงิน", "บันทึกเมื่อ"]


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'export-unit.db'}")
    init_db(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.rollback()
    engine.dispose()


@pytest.fixture()
def export_client(tmp_path, monkeypatch):
    engine = configure_database(f"sqlite:///{tmp_path / 'export-webhook.db'}")
    replies = []

    async def fake_reply(reply_token, text, token):
        replies.append((reply_token, text, token))

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", BASE_URL + "///")
    monkeypatch.setattr(app_module, "LINE_CHANNEL_SECRET", SECRET)
    monkeypatch.setattr(app_module, "LINE_CHANNEL_ACCESS_TOKEN", "test-access-token")
    monkeypatch.setattr(app_module, "reply_text", fake_reply)
    monkeypatch.setattr(app_module, "_utc_now", lambda: NOW)
    with TestClient(app_module.app) as client:
        yield client, engine, replies
    engine.dispose()


def _export_url(replies):
    return replies[-1][1].splitlines()[-1]


def _issue(client, replies, event_id="export", user=USER):
    assert _post_text(client, event_id, user, "ส่งออกข้อมูล").status_code == 200
    return _export_url(replies)


def _csv_rows(response):
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"), newline="")))


@pytest.mark.parametrize("existing", [False, True])
def test_init_db_creates_export_table_without_alter_and_is_repeatable(tmp_path, existing):
    engine = create_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    if existing:
        Base.metadata.create_all(
            engine, tables=[table for table in Base.metadata.sorted_tables if table.name != "export_tokens"]
        )
        with Session(engine) as session:
            add_transaction(USER, "expense", "50", "อาหาร", session=session)
            session.commit()
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, p, ctx, many: statements.append(sql))
    init_db(engine)
    with Session(engine) as session:
        session.add(ExportToken(
            line_user_id=USER, token_hash="a" * 64, created_at=NOW, expires_at=NOW + EXPORT_TOKEN_TTL,
        ))
        session.commit()
    init_db(engine)
    inspector = inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("export_tokens")}
    assert set(columns) == {"id", "token_hash", "line_user_id", "created_at", "expires_at"}
    assert all(not column["nullable"] for column in columns.values())
    assert columns["token_hash"]["type"].length == 64
    assert columns["line_user_id"]["type"].length == 128
    assert any(index["column_names"] == ["line_user_id"] for index in inspector.get_indexes("export_tokens"))
    assert any(c["column_names"] == ["token_hash"] for c in inspector.get_unique_constraints("export_tokens"))
    assert sum(sql.startswith("\nCREATE TABLE export_tokens") for sql in statements) == 1
    assert not any("ALTER TABLE" in sql.upper() for sql in statements)
    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == int(existing)
        assert session.scalar(select(ExportToken.token_hash)) == "a" * 64
    engine.dispose()


def test_postgresql_create_all_emits_portable_export_schema():
    statements = []
    engine = create_mock_engine(
        "postgresql+psycopg://",
        lambda sql, *args, **kwargs: statements.append(str(sql.compile(dialect=engine.dialect))),
    )
    Base.metadata.create_all(engine)
    ddl = next(sql for sql in statements if "CREATE TABLE export_tokens" in sql)
    assert "id SERIAL NOT NULL" in ddl
    assert "token_hash VARCHAR(64) NOT NULL" in ddl
    assert "line_user_id VARCHAR(128) NOT NULL" in ddl
    assert "UNIQUE (token_hash)" in ddl
    assert ddl.count("TIMESTAMP WITH TIME ZONE NOT NULL") == 2
    assert any("CREATE INDEX ix_export_tokens_line_user_id" in sql for sql in statements)
    assert not any("ALTER TABLE" in sql for sql in statements)


def test_issuance_uses_csprng_hash_only_and_ttl(db_session, monkeypatch):
    add_transaction(USER, "expense", "50", "อาหาร", session=db_session)
    raw = secrets.token_urlsafe(32)
    calls = []

    def generate(size):
        calls.append(size)
        return raw

    monkeypatch.setattr(repository.secrets, "token_urlsafe", generate)
    token = create_export_token(USER, now=NOW, session=db_session)
    assert token == raw
    assert calls == [32]
    stored = db_session.scalar(select(ExportToken))
    assert stored.token_hash == sha256(raw.encode("utf-8")).hexdigest()
    assert raw not in _row_state(stored)
    assert stored.line_user_id == USER
    assert stored.created_at.replace(tzinfo=timezone.utc) == NOW
    assert stored.expires_at.replace(tzinfo=timezone.utc) == NOW + EXPORT_TOKEN_TTL
    assert EXPORT_TOKEN_TTL == timedelta(minutes=10)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(ExportToken(
                token_hash=stored.token_hash, line_user_id="U-bob",
                created_at=NOW, expires_at=NOW + EXPORT_TOKEN_TTL,
            ))
            db_session.flush()


def test_repository_empty_export_does_not_call_csprng(db_session, monkeypatch):
    def unexpected_generation(size):
        pytest.fail("Empty export must not generate a capability")

    monkeypatch.setattr(repository.secrets, "token_urlsafe", unexpected_generation)
    assert create_export_token(USER, now=NOW, session=db_session) is None
    assert db_session.scalar(select(func.count(ExportToken.id))) == 0


@pytest.mark.parametrize("moment", [NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=7)))])
def test_export_clock_normalization_is_utc(db_session, moment):
    add_transaction(USER, "expense", "50", "อาหาร", session=db_session)
    raw = create_export_token(USER, now=moment, session=db_session)
    token_hash = sha256(raw.encode()).hexdigest()
    db_session.expire_all()
    stored = db_session.scalar(select(ExportToken))
    assert stored.created_at.replace(tzinfo=timezone.utc) == NOW
    assert stored.expires_at.replace(tzinfo=timezone.utc) == NOW + EXPORT_TOKEN_TTL
    assert len(fetch_export_transactions(token_hash, now=moment, session=db_session)) == 1
    assert fetch_export_transactions(token_hash, now=moment + EXPORT_TOKEN_TTL, session=db_session) == []
    db_session.execute(delete(Transaction))
    assert get_user_data_summary(USER, now=moment, session=db_session).has_live_export_token
    assert not get_user_data_summary(USER, now=moment + EXPORT_TOKEN_TTL, session=db_session).has_any_data


def test_download_repository_authorizes_and_reads_in_one_select(db_session):
    own = add_transaction(USER, "expense", "50", "อาหาร", session=db_session)
    add_transaction("U-bob", "income", "500", "เงินเดือน", session=db_session)
    raw = create_export_token(USER, now=NOW, session=db_session)
    statements = []
    compiled = []

    def capture(connection, cursor, sql, parameters, context, executemany):
        statements.append(sql)
        compiled.append(context.compiled.statement)

    event.listen(db_session.get_bind(), "before_cursor_execute", capture)
    try:
        items = fetch_export_transactions(sha256(raw.encode()).hexdigest(), now=NOW, session=db_session)
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", capture)
    assert [item.id for item in items] == [own.id]
    assert len(statements) == 1
    sql = statements[0]
    assert sql.startswith("SELECT")
    assert "JOIN export_tokens ON export_tokens.line_user_id = transactions.line_user_id" in sql
    assert "export_tokens.token_hash =" in sql and "export_tokens.expires_at >" in sql
    assert "ORDER BY transactions.occurred_on ASC, transactions.created_at ASC, transactions.id ASC" in sql
    pg_sql = str(compiled[0].compile(dialect=postgresql.dialect()))
    assert "JOIN export_tokens" in pg_sql
    assert "FOR SHARE" not in pg_sql and "FOR UPDATE" not in pg_sql


@pytest.mark.parametrize("production", [False, True])
@pytest.mark.parametrize("value", ["http://mootoon.example", "https://", "not-a-url", "https://host:bad", "https://host/?token=x", "https://host/#fragment", "https://host?", "https://host#", "https://user:password@host", "https://host/\n"])
def test_invalid_base_url_fails_startup(monkeypatch, production, value):
    monkeypatch.setenv("ENVIRONMENT", "production" if production else "development")
    for name in ("LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "DATABASE_URL"):
        monkeypatch.setenv(name, "test-placeholder")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", value)
    monkeypatch.setattr(app_module, "init_db", lambda: pytest.fail("Invalid configuration must fail before DB init"))
    with pytest.raises(RuntimeError, match="RENDER_EXTERNAL_URL"):
        with TestClient(app_module.app):
            pass


def test_production_missing_base_url_fails_startup(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    for name in ("LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "DATABASE_URL"):
        monkeypatch.setenv(name, "test-placeholder")
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.setattr(app_module, "init_db", lambda: pytest.fail("Missing configuration must fail before DB init"))
    with pytest.raises(RuntimeError, match="RENDER_EXTERNAL_URL"):
        with TestClient(app_module.app):
            pass


def test_production_https_base_is_normalized(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    for name in ("LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "DATABASE_URL"):
        monkeypatch.setenv(name, "test-placeholder")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", BASE_URL + "///")
    monkeypatch.setattr(app_module, "init_db", lambda: None)
    with TestClient(app_module.app):
        assert app_module._export_base_url() == BASE_URL


def test_nonproduction_missing_base_is_unavailable_and_mints_nothing(tmp_path, monkeypatch):
    engine = configure_database(f"sqlite:///{tmp_path / 'unavailable.db'}")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    with TestClient(app_module.app):
        with Session(engine) as session:
            add_transaction(USER, "expense", "50", "อาหาร", session=session)
            reply = app_module.handle_text_message(USER, "ส่งออกข้อมูล", NOW, session)
            assert reply == "ยังไม่สามารถส่งออกข้อมูลได้ในขณะนี้ครับ กรุณาลองใหม่ภายหลัง"
            assert session.scalar(select(func.count(ExportToken.id))) == 0
    engine.dispose()


def test_exact_parser_command_and_help():
    assert parse_command("ส่งออกข้อมูล") == SimpleCommand(CommandKind.EXPORT)
    assert "ส่งออกข้อมูล" in format_help_message()
    for alias in ("export", "CSV", "ส่งออก", "ส่งออกข้อมูล CSV"):
        assert parse_command(alias) != SimpleCommand(CommandKind.EXPORT)


@pytest.mark.parametrize("state", ["empty", "goal", "draft", "undo", "confirm_delete_all"])
def test_empty_transaction_export_mints_nothing(export_client, state):
    client, engine, replies = export_client
    if state == "goal":
        assert _post_text(client, "goal", USER, "ตั้งเป้า 1500 ซื้อหนังสือ").status_code == 200
    elif state == "draft":
        assert _post_text(client, "draft", USER, "กาแฟ").status_code == 200
    elif state == "undo":
        assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
        assert _post_text(client, "delete", USER, "ลบล่าสุด").status_code == 200
        assert _post_text(client, "confirm", USER, "ยืนยัน").status_code == 200
    elif state == "confirm_delete_all":
        assert _post_text(client, "goal", USER, "ตั้งเป้า 1500 ซื้อหนังสือ").status_code == 200
        assert _post_text(client, "delete-all", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    assert _post_text(client, "export", USER, "ส่งออกข้อมูล").status_code == 200
    assert replies[-1][1] == "ไม่มีข้อมูลสำหรับส่งออกครับ"
    with Session(engine) as session:
        assert session.scalar(select(func.count(Transaction.id))) == 0
        assert session.scalar(select(func.count(ExportToken.id))) == 0


def test_endpoint_success_headers_and_all_failures_are_identical(export_client, monkeypatch):
    client, engine, replies = export_client
    assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
    url = _issue(client, replies)
    assert url.startswith(BASE_URL + "/export/")
    assert USER not in url
    valid = client.get(url)
    assert valid.status_code == 200
    assert valid.headers["content-type"] == "text/csv; charset=utf-8"
    assert valid.headers["content-disposition"] == 'attachment; filename="mootoon_export.csv"'
    assert valid.headers["cache-control"] == "no-store"
    assert valid.headers["x-content-type-options"] == "nosniff"
    assert valid.content.startswith(b"\xef\xbb\xbf")
    failures = [client.get("/export/" + token) for token in ("", "short", "!" * 43, "a" * 42, "a" * 44, "a" * 43, "a/b", "a%2Fb")]
    monkeypatch.setattr(app_module, "_utc_now", lambda: NOW + EXPORT_TOKEN_TTL)
    failures.append(client.get(url))  # Equality at expiry must deny.
    monkeypatch.setattr(app_module, "_utc_now", lambda: NOW)
    with Session(engine) as session:
        session.execute(delete(Transaction))
        session.commit()
    failures.append(client.get(url))  # Live capability with no current transactions.
    with Session(engine) as session:
        session.execute(delete(ExportToken))
        session.commit()
    failures.append(client.get(url))  # Revoked.
    assert all(response.status_code == 404 for response in failures)
    assert len({response.content for response in failures}) == 1
    assert len({tuple(response.headers.items()) for response in failures}) == 1
    for response in failures:
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"


def test_download_endpoint_is_one_select_and_never_writes(export_client):
    client, engine, replies = export_client
    assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
    url = _issue(client, replies)
    statements = []

    def capture(c, cur, sql, p, ctx, many):
        statements.append(sql)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert client.get(url).status_code == 200
        assert len(statements) == 1 and statements[0].startswith("SELECT")
        statements.clear()
        assert client.get("/export/short").status_code == 404
        assert statements == []
        assert client.get("/export/" + "a" * 43).status_code == 404
        assert len(statements) == 1 and statements[0].startswith("SELECT")
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_csv_round_trip_exact_values_and_ordering(export_client):
    client, engine, replies = export_client
    # Deliberately insert out of chronological order, including ties by id.
    fixtures = [
        (date(2026, 10, 5), NOW, "expense", 5000, "อาหาร", "ท้าย"),
        (date(2026, 10, 4), NOW + timedelta(seconds=1), "expense", 1999, "อาหาร", "ถัดไป"),
        (date(2026, 10, 4), NOW, "income", 1, "เงินเดือน", 'ไทย 🐷, "คำพูด"\nบรรทัดใหม่'),
        (date(2026, 10, 4), NOW, "expense", 9223372036854775807, "อื่นๆ", None),
    ]
    with Session(engine) as session:
        for occurred_on, created_at, kind, amount, category, description in fixtures:
            session.add(Transaction(
                line_user_id=USER, transaction_type=kind, amount_satang=amount,
                category=category, description=description, occurred_on=occurred_on, created_at=created_at,
            ))
        add_transaction("U-bob", "expense", "999", "ความลับ", session=session)
        session.add(SavingsGoal(line_user_id=USER, title="ไม่ส่งออก", target_satang=10000))
        session.commit()
    url = _issue(client, replies)
    response = client.get(url)
    rows = _csv_rows(response)
    assert rows[0] == CSV_HEADER
    assert all(len(row) == 6 for row in rows)
    assert rows[1] == ["2026-10-04", "รายรับ", "เงินเดือน", 'ไทย 🐷, "คำพูด"\nบรรทัดใหม่', "0.01", "2026-10-04T10:00:00+07:00"]
    assert rows[2] == ["2026-10-04", "รายจ่าย", "อื่นๆ", "", "92233720368547758.07", "2026-10-04T10:00:00+07:00"]
    assert rows[3][3:5] == ["ถัดไป", "19.99"]
    assert rows[3][5] == "2026-10-04T10:00:01+07:00"
    assert rows[4][3:5] == ["ท้าย", "50.00"]
    assert len(rows) == 5
    assert USER not in response.text and "ความลับ" not in response.text and "ไม่ส่งออก" not in response.text
    assert response.content.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_sanitizes_every_dangerous_prefix_without_mutating_storage(export_client, prefix):
    client, engine, replies = export_client
    value = prefix + 'ไทย, "🐷"\nต่อ'
    with Session(engine) as session:
        session.add(Transaction(
            line_user_id=USER, transaction_type="expense", amount_satang=5000,
            category=value, description=value, occurred_on=NOW.date(), created_at=NOW,
        ))
        session.commit()
    response = client.get(_issue(client, replies))
    assert _csv_rows(response)[1][2:5] == ["'" + value, "'" + value, "50.00"]
    with Session(engine) as session:
        item = session.scalar(select(Transaction))
        assert item.category == value and item.description == value


def test_same_capability_downloads_live_data(export_client):
    client, engine, replies = export_client
    assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
    url = _issue(client, replies)
    assert len(_csv_rows(client.get(url))) == 2
    assert _post_text(client, "new", USER, "BTS 47").status_code == 200
    assert len(_csv_rows(client.get(url))) == 3
    assert _post_text(client, "edit", USER, "แก้ไข BTS 49").status_code == 200
    assert "49.00" in client.get(url).text


def test_t0_t3_delete_all_revocation_and_exact_user_isolation(export_client):
    client, engine, replies = export_client
    assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
    assert _post_text(client, "bob-seed", USER + "-other", "เงินเดือน 20000").status_code == 200
    old_url = _issue(client, replies)  # T0
    second_url = _issue(client, replies, "second-export")
    bob_url = _issue(client, replies, "bob-export", USER + "-other")
    with Session(engine) as session:
        session.add(ExportToken(
            line_user_id=USER, token_hash="c" * 64,
            created_at=NOW - EXPORT_TOKEN_TTL, expires_at=NOW,
        ))
        session.commit()
    assert _post_text(client, "goal", USER, "ตั้งเป้า 1500 ซื้อหนังสือ").status_code == 200
    assert _post_text(client, "draft", USER, "กาแฟ").status_code == 200
    assert _post_text(client, "trigger", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    assert client.get(old_url).status_code == 200  # Trigger preserves capability.
    assert _post_text(client, "cancel", USER, "ยกเลิก").status_code == 200
    assert client.get(old_url).status_code == 200  # Cancellation preserves it too.
    assert _post_text(client, "trigger-again", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    assert _post_text(client, "confirm", USER, "ยืนยัน").status_code == 200  # T1
    with Session(engine) as session:
        for model in (Transaction, SavingsGoal, PendingTransaction, PendingAction, ExportToken):
            assert session.scalar(select(func.count()).select_from(model).where(model.line_user_id == USER)) == 0
        assert session.scalar(select(func.count(ExportToken.id))) == 1
        assert session.get(ProcessedWebhookEvent, "export").response_text.endswith(old_url)
    assert _post_text(client, "after-delete", USER, "ข้าว 500").status_code == 200  # T2
    old_hash = sha256(old_url.rsplit("/", 1)[1].encode()).hexdigest()
    with Session(engine) as session:
        assert fetch_export_transactions(old_hash, now=NOW, session=session) == []
        assert session.scalar(select(Transaction).where(Transaction.line_user_id == USER)).amount_satang == 50000
    for url in (old_url, second_url):
        response = client.get(url)  # T3
        assert response.status_code == 404
        assert "500.00" not in response.text
    assert client.get(bob_url).status_code == 200
    assert _post_text(client, "export", USER, "ส่งออกข้อมูล").status_code == 200
    with Session(engine) as session:
        assert session.scalar(select(func.count(ExportToken.id))) == 1  # Redelivery cannot re-mint revoked URL.


@pytest.mark.parametrize("expires_delta", [timedelta(seconds=1), timedelta(0), timedelta(seconds=-1)])
def test_delete_all_gate_counts_only_live_tokens(export_client, expires_delta):
    client, engine, replies = export_client
    with Session(engine) as session:
        session.add(ExportToken(
            line_user_id=USER, token_hash="b" * 64, created_at=NOW - timedelta(minutes=5),
            expires_at=NOW + expires_delta,
        ))
        session.commit()
        summary = get_user_data_summary(USER, now=NOW, session=session)
        assert summary.transaction_count == 0
        assert summary.has_live_export_token == (expires_delta > timedelta(0))
        assert summary.has_any_data == (expires_delta > timedelta(0))
        assert not get_user_data_summary("U-bob", now=NOW, session=session).has_any_data
    assert _post_text(client, "trigger", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    if expires_delta > timedelta(0):
        assert "รายการ 0 รายการ" in replies[-1][1]
        assert _post_text(client, "confirm", USER, "ยืนยัน").status_code == 200
        with Session(engine) as session:
            assert session.scalar(select(func.count(ExportToken.id))) == 0
    else:
        assert replies[-1][1] == "ไม่มีข้อมูลให้ลบครับ"
        with Session(engine) as session:
            assert session.get(PendingAction, USER) is None
            assert session.scalar(select(func.count(ExportToken.id))) == 1


def test_failed_delete_all_occ_preserves_capabilities(export_client):
    client, engine, replies = export_client
    assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
    url = _issue(client, replies)
    assert _post_text(client, "trigger", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    with Session(engine) as session:
        action = session.get(PendingAction, USER)
        with pytest.raises(PendingActionConflictError):
            execute_delete_all(USER, action.version + 1, expected_action_id=action.action_id, now=NOW, session=session)
        session.commit()
    assert client.get(url).status_code == 200


@pytest.mark.parametrize("action_type", [None, "confirm_delete", "undo_delete", "confirm_delete_all"])
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("outcome", ["success", "empty", "unavailable"])
def test_export_preserves_every_pending_column(export_client, monkeypatch, action_type, expired, outcome):
    client, engine, replies = export_client
    assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
    if action_type == "undo_delete":
        assert _post_text(client, "undo-seed", USER, "BTS 47").status_code == 200
        assert _post_text(client, "delete", USER, "ลบล่าสุด").status_code == 200
        assert _post_text(client, "confirm", USER, "ยืนยัน").status_code == 200
    elif action_type == "confirm_delete":
        assert _post_text(client, "delete", USER, "ลบล่าสุด").status_code == 200
    assert _post_text(client, "draft", USER, "กาแฟ").status_code == 200
    if action_type == "confirm_delete_all":
        assert _post_text(client, "delete-all", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    with Session(engine) as session:
        if outcome == "empty":
            session.execute(delete(Transaction).where(Transaction.line_user_id == USER))
            session.commit()
        draft = session.get(PendingTransaction, USER)
        action = session.get(PendingAction, USER)
        if expired:
            draft.expires_at = NOW - timedelta(seconds=1)
            if action is not None:
                action.expires_at = NOW - timedelta(seconds=1)
            session.commit()
        before_draft = _row_state(draft)
        before_action = _row_state(action) if action is not None else None
    if outcome == "unavailable":
        monkeypatch.delenv("RENDER_EXTERNAL_URL")
    assert _post_text(client, "export", USER, "ส่งออกข้อมูล").status_code == 200
    if outcome == "success":
        assert client.get(_export_url(replies)).status_code == 200
    elif outcome == "empty":
        assert replies[-1][1] == "ไม่มีข้อมูลสำหรับส่งออกครับ"
    else:
        assert replies[-1][1] == "ยังไม่สามารถส่งออกข้อมูลได้ในขณะนี้ครับ กรุณาลองใหม่ภายหลัง"
    with Session(engine) as session:
        assert _row_state(session.get(PendingTransaction, USER)) == before_draft
        action = session.get(PendingAction, USER)
        assert (_row_state(action) if action is not None else None) == before_action
        assert session.scalar(select(func.count(ExportToken.id))) == int(outcome == "success")


def test_delete_all_data_and_capability_revocation_roll_back_together(export_client):
    client, engine, replies = export_client
    assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
    url = _issue(client, replies)
    assert _post_text(client, "trigger", USER, "ลบข้อมูลทั้งหมด").status_code == 200
    with pytest.raises(RuntimeError, match="outer transaction failure"):
        with Session(engine) as session, session.begin():
            action = session.get(PendingAction, USER)
            execute_delete_all(
                USER, action.version, expected_action_id=action.action_id, now=NOW, session=session,
            )
            raise RuntimeError("outer transaction failure")
    assert client.get(url).status_code == 200
    with Session(engine) as session:
        assert session.get(PendingAction, USER).action_type == "confirm_delete_all"
        assert session.scalar(select(func.count(Transaction.id))) == 1
        assert session.scalar(select(func.count(ExportToken.id))) == 1


def test_duplicate_events_skip_sent_reply_and_distinct_events_issue_distinct_tokens(export_client):
    client, engine, replies = export_client
    assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
    first = _issue(client, replies)
    assert _post_text(client, "export", USER, "ส่งออกข้อมูล").status_code == 200
    assert len([reply for reply in replies if reply[0] == "reply-export"]) == 1
    second = _issue(client, replies, "distinct-export")
    assert first != second
    assert client.get(first).status_code == client.get(second).status_code == 200
    with Session(engine) as session:
        assert session.scalar(select(func.count(ExportToken.id))) == 2
        assert session.get(ProcessedWebhookEvent, "export").response_text.endswith(first)


@pytest.mark.parametrize("invalidate", ["expire", "revoke"])
def test_unsent_cached_url_replays_without_remint_after_invalidation(export_client, monkeypatch, invalidate):
    client, engine, replies = export_client
    assert _post_text(client, "seed", USER, "ข้าว 50").status_code == 200
    attempts = []

    async def fail_once(reply_token, text, token):
        attempts.append(text)
        if len(attempts) == 1:
            raise LineTransportError("temporary failure")

    monkeypatch.setattr(app_module, "reply_text", fail_once)
    assert _post_text(client, "export", USER, "ส่งออกข้อมูล").status_code == 502
    url = attempts[0].splitlines()[-1]
    with Session(engine) as session:
        assert session.get(ProcessedWebhookEvent, "export").reply_sent is False
        assert session.scalar(select(func.count(ExportToken.id))) == 1
        if invalidate == "expire":
            session.scalar(select(ExportToken)).expires_at = NOW
        else:
            session.execute(delete(ExportToken))
        session.commit()
    assert client.get(url).status_code == 404
    assert _post_text(client, "export", USER, "ส่งออกข้อมูล").status_code == 200
    assert attempts == [attempts[0], attempts[0]]
    with Session(engine) as session:
        assert session.scalar(select(func.count(ExportToken.id))) == int(invalidate == "expire")
        assert session.get(ProcessedWebhookEvent, "export").reply_sent is True
    assert client.get(url).status_code == 404
