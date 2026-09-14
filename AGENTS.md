# AGENTS.md

## Commands

- Setup: `pip install -r requirements-dev.txt` (includes pytest)
- Dev server: `uvicorn app:app --reload --port 8000`
- Tests: `pytest -q`; single file: `pytest test_parser.py`; single test: `pytest test_parser.py::test_name`
- No lint/typecheck/formatter config exists in this repo

## Architecture

Single flat package (no subdirectories): `app.py` (FastAPI webhook + command dispatch) →
`parser.py` (rule-based Thai parser, no LLM; returns frozen dataclasses; only emits a
transaction when both direction and amount are unambiguous) → `repository.py` (all DB
access, scoped per LINE user ID) → `messages.py` (Thai reply formatting).
`line_api.py` handles signature verification and replies; `database.py`/`models.py`
hold the SQLAlchemy engine and tables.

- Tests and docs live at repo root (`test_*.py`, `PRODUCT_SPEC.md`, `PILOT_TEST.md`) —
  the directory layout in README.md is outdated.

## Critical invariants

- Money is stored as integer satang (`amount_satang`); parse with `Decimal`, never float.
- Webhook: HMAC signature is verified against the raw request body before any DB write;
  every event is deduplicated via `processed_webhook_events`.
- The parser must never guess: a missing amount or direction returns an
  UnresolvedCommand, not a transaction.
- Month boundaries and summaries use Asia/Bangkok; display years are B.E. (+543).

## Database quirks

- No Alembic. `init_db()` runs `create_all` plus a hand-rolled column migration
  (`_upgrade_webhook_event_table` in database.py). Schema changes must extend that path.
- `postgres://` and `postgresql://` URLs are rewritten to `postgresql+psycopg://`
  in `normalize_database_url`.
- Tests never touch the real DB: they build tmp_path SQLite engines, and webhook tests
  call `configure_database()` + monkeypatch `app_module` attributes (see test_webhook.py).
- `ENVIRONMENT=production` makes the app fail at startup if LINE/DB env vars are missing.

## Secrets

- Never commit `.env`, LINE tokens, channel secrets, or `*.db` files (all gitignored).
- `.env.example` contains placeholders only — keep it that way.
- Deploy via Render blueprint (`render.yaml`); env vars are set in Render, not in the repo.
