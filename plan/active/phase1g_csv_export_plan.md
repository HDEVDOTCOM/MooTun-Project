# Phase 1G — CSV Export Architecture Plan

**Status:** `COMPLETE / PUSHED / DEPLOYED / PRODUCTION VERIFIED`

## 1. Exact Phase 1G Scope
- **Exported Entity:** `Transaction` only. `SavingsGoal` and internal database IDs (e.g., `id`) are excluded to maintain a flat ledger structure.
- **Columns Included:**
  1. `วันที่` (occurred_on)
  2. `ประเภท` (transaction_type: "รายรับ" / "รายจ่าย")
  3. `หมวดหมู่` (category)
  4. `รายการ` (description)
  5. `จำนวนเงิน` (amount_satang -> decimal string)
  6. `บันทึกเมื่อ` (created_at)

## 2. Delivery Mechanism
- **Mechanism:** The LINE bot sends a standard text reply containing an HTTPS URL. The CSV download is served entirely by MooTun's own FastAPI application.
- **Wording Correction:** LINE outbound messaging has no general arbitrary-file message type. We do NOT claim that LINE officially supports arbitrary file download links.

## 3. Token Architecture & Storage Claim (B1)
- **Design:** Opaque DB-backed capability token.
- **Generation:** Raw token generated via `secrets.token_urlsafe(32)` (256 bits of CSPRNG entropy).
- **Persistence Details:**
  - The raw token is NEVER stored in the `ExportToken` table. The table stores ONLY `sha256(raw_token).hexdigest()`.
  - The raw token necessarily appears in the reply URL.
  - The full reply URL is cached in `ProcessedWebhookEvent.response_text` for webhook idempotency (a mechanism intentionally retained by Phase 1F).
  - Therefore, the raw token *does* exist in server-side persistence within the webhook cache. However, once the corresponding `ExportToken` row expires or is revoked, the cached URL is no longer a valid authorization credential.
- **Restrictions:** No LINE user ID is encoded in the URL. No signing secret (like `LINE_CHANNEL_SECRET`) or HMAC is used.

## 4. ExportToken Database Model
- **New Table (`ExportToken`):**
  - `id`: Integer primary key
  - `token_hash`: String(64), unique, not null (SHA-256 hex digest)
  - `line_user_id`: String(128) matching existing LINE-user field length, not null, indexed
  - `created_at`: timezone-aware datetime, not null
  - `expires_at`: timezone-aware datetime, not null
- **Impact:** Add `ExportToken` as a NEW table only. No existing table/column alteration is required.

## 5. Token Lifetime & Optional Cleanup
- **TTL:** Use a distinct constant: `EXPORT_TOKEN_TTL = timedelta(minutes=10)`.
- **Lookup:** Hash the presented raw token, find the matching `ExportToken` row, and require `expires_at > now`.
- **Optional Cleanup:** Expired token cleanup is non-security-critical and may be treated as lazy optional hygiene rather than required authorization correctness.

## 6. Delete All Integration & No-Data Semantics
- **Rule:** A confirmed `ลบข้อมูลทั้งหมด` must atomically revoke all export capabilities belonging to that user.
- **Live Token State:** A LIVE, UNEXPIRED `ExportToken` counts as deletable user state for Phase 1F Delete All. This ensures Delete All can revoke an outstanding export capability even if the user currently has zero transactions.
  - The Delete All no-data gate (`UserDataSummary`) must consider: `Transaction`, `SavingsGoal`, `PendingTransaction`, `PendingAction`, and live/unexpired `ExportToken` rows.
  - Expired `ExportToken` rows alone do NOT count as deletable state (they are non-authorizing residue).
  - If a user has a live export token but no other financial state, `ลบข้อมูลทั้งหมด` still allows confirmation, and successful confirmation revokes the token.
- **Inside the Phase 1F delete-all transaction:** Atomically OCC-consume `confirm_delete_all`, delete user's `PendingTransaction`, `SavingsGoal`, `Transaction` rows, `ExportToken` rows, and consume `PendingAction`.
- **Isolation:** Triggering or cancelling Delete All must NOT revoke tokens. User scoped deletions must filter by exact `line_user_id`. Other users' tokens remain intact.

## 7. URL Construction (B3)
- **Source:** Use `RENDER_EXTERNAL_URL` as the export base URL source.
- **Production Environment:**
  - `RENDER_EXTERNAL_URL` is a required runtime environment variable. Add it to the application's existing production startup/lifespan validation.
  - The value MUST begin with `https://`.
  - Normalize by removing any trailing `/`.
- **Non-Production Environment:**
  - If `RENDER_EXTERNAL_URL` is supplied, it may be used after validation.
  - If missing, do NOT silently fall back to localhost.
  - The export command must return the fixed message: `ยังไม่สามารถส่งออกข้อมูลได้ในขณะนี้ครับ กรุณาลองใหม่ภายหลัง`
  - Do NOT mint an `ExportToken` row in this unavailable state.

## 8. Export Semantics & Single-Statement Read (B2)
- **Behavior:** LIVE DATA AT DOWNLOAD TIME. The CSV represents live `Transaction` data. It is NOT a command-time immutable snapshot. Two downloads using the same token may differ.
- **Single SQL Statement Requirement:** Download authorization and transaction fetch MUST be performed in ONE SQL statement.
  - **Conceptual Query:**
    ```sql
    SELECT t.* FROM transactions t
    JOIN export_tokens e ON e.line_user_id = t.line_user_id
    WHERE e.token_hash = :hash AND e.expires_at > :now
    ORDER BY t.occurred_on ASC, t.created_at ASC, t.id ASC
    ```
  - **Why?** Under PostgreSQL Read Committed, this guarantees the authorization row and the transaction rows are observed by a single statement-level snapshot.
  - **T0 -> T3 Guarantee:** (T0) URL L issued -> (T1) Confirmed Delete All removes data & token -> (T2) New transaction created -> (T3) Old URL L opened. The joined SELECT will return zero rows because the token was deleted in T1. The endpoint returns a 404, guaranteeing post-delete data is never exposed.
  - **Compatibility:** This query must work on both PostgreSQL and SQLite. Do not use PostgreSQL-only locking like `FOR SHARE`.

## 9. Empty-Data Gate (Issuance)
- **Condition:** Generating a token requires `transaction_count > 0`.
- **Required Behavior:** If a user has 0 transactions (even if they have a SavingsGoal, PendingTransaction, undo_delete, or confirm_delete_all), the bot replies: `ไม่มีข้อมูลสำหรับส่งออกครับ` and creates ZERO `ExportToken` rows.

## 10. Webhook Idempotency & Issuance
- **Issuance:** When `ส่งออกข้อมูล` is requested and transactions > 0 (and URL base exists), generate token, store hash, and return URL.
- **Idempotency:** Same LINE webhook event ID creates exactly one `ExportToken`. Cached `response_text` replays the URL. If the cached URL has expired, the replay still returns the old URL (which will 404), and MUST NOT regenerate a token.

## 11. Download Endpoint (`GET /export/{token}`)
- **Mechanism:** Token is the bearer capability.
- **Success (HTTP 200):** Only if the single joined authorization/data query returns at least one transaction.
  - Headers: `Content-Type: text/csv; charset=utf-8`, `Content-Disposition: attachment; filename="mootoon_export.csv"`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`.
- **Failure Conditions:** Return identical HTTP 404 for malformed, unknown, expired, revoked, or valid tokens with zero transactions.
  - Headers: `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`.
  - Byte-identical response body, no user-existence oracle.

## 12. Help Message & Command Parsing
- **Help Update:** Add the exact command `ส่งออกข้อมูล` to the user-facing help message. Do not add aliases.
- **Pending State Preservation:** `ส่งออกข้อมูล` is read-only. Must preserve unchanged `PendingTransaction`, `confirm_delete`, `undo_delete`, and `confirm_delete_all` (no invalidation, version change, expiry refresh, or snapshot mutation). Ensure exact command is routed before Phase 1B follow-up parsing.

## 13. CSV Generation Details
- **Formatting:** In-memory generation using stdlib `csv`. UTF-8 with BOM (`utf-8-sig`).
- **Money:** Exact decimal baht string (never float).
- **Dates:** `occurred_on` -> Gregorian ISO `YYYY-MM-DD`. `created_at` -> Asia/Bangkok +07:00 ISO timestamp.
- **Ordering:** `occurred_on ASC, created_at ASC, id ASC`.
- **Injection Safety:** Sanitize `category` and `description`. If a cell starts with `=`, `+`, `-`, `@`, `\t`, or `\r`, prefix a single quote (`'`). Do not mutate stored DB values or numeric columns.

## 14. Required Test Matrix
- **B1 Storage Semantics:** Raw token absent from `ExportToken`, `token_hash == sha256(raw)`, `ProcessedWebhookEvent` may contain full URL, expired/revoked cached URL returns 404.
- **B2 Atomic Read:** T0->T3 revocation scenario prevents post-delete exposure, single authorization+data SQL statement, Postgres/SQLite compatibility.
- **B3 URL Construction:** Production missing `RENDER_EXTERNAL_URL` fails startup, invalid non-https rejected, trailing slash normalized. Non-production missing base returns fixed unavailable reply, no token created.
- **Delete All Semantics:** Live `ExportToken` counts toward deletable state, zero transactions + live token permits Delete All confirmation, confirmed Delete All revokes it, expired token alone does not require confirmation. Other users' tokens untouched.
- **Help:** Help text lists exact `ส่งออกข้อมูล`.
- **Token/Security:** CSPRNG generation, LINE UID absent from URL, valid/malformed/unknown/expired/revoked states, cross-user isolation.
- **Empty Data:** Zero transactions -> no link, no token.
- **CSV Output:** Columns, BOM, Thai, emoji, comma, quote, newline, injection mitigation, exact money, dates, ordering.
- **Pending State:** Draft, confirm/undo states preserved (identity/version/expiry unchanged).
- **Idempotency:** Same event -> one token, distinct -> distinct.
- **Endpoint Matrix:** Complete 200/404 matrix, byte-identical failures, headers.
- **Regression:** Phase 1D, 1E, 1F, webhook RETURNING regression.

## 15. Scope Exclusions
- XLSX, PDF, scheduled exports, email delivery, analytics, date-range filters, cloud/object storage, immutable export snapshots, permanent public links.
