# Phase 1F: Delete All User Data Architecture

**Status**: `IMPLEMENTED — READY TO COMMIT`

This document defines the architecture for the Phase 1F feature: safe, destructive deletion of all user data with explicit confirmation, adhering to the invariants of the MooToon codebase.

## 1. Exact Phase 1F Scope
- **In Scope**: A feature allowing a user to permanently erase their financial footprint (transactions, goals, drafts, and pending actions) via the command `ลบข้อมูลทั้งหมด`.
- **Out of Scope**: 
  - Soft-delete or multi-level undo.
  - Deletion of global operational deduplication metadata.
  - Refactoring or cleanup of unused legacy code (e.g., `repository.delete_latest_transaction`). **No unrelated cleanup is permitted in Phase 1F.**
  - Updates to `PRODUCT_SPEC.md` (deferred to a post-Phase-1F sync).
  - Aliases for the command (only `ลบข้อมูลทั้งหมด` is supported).

## 2. User-Data Deletion Matrix
| Table | Deleted? | User-Scope Key | Justification & Safety |
|---|---|---|---|
| `Transaction` | **YES** | `line_user_id` | Core financial data. `delete().where(line_user_id == ...)` is perfectly safe and isolated. |
| `SavingsGoal` | **YES** | `line_user_id` | Core financial data. `delete().where(line_user_id == ...)` is perfectly safe and isolated. |
| `PendingTransaction` | **YES** | `line_user_id` | User's active draft. `delete().where(line_user_id == ...)` is perfectly safe. |
| `PendingAction` | **YES** | `line_user_id` | Consumes the `confirm_delete_all` state and deletes any active `undo_delete` state. |
| `ProcessedWebhookEvent` | **NO** | *None* | This table tracks `webhook_event_id` for HTTP idempotency. It lacks a `line_user_id` column, meaning records cannot be safely mapped to a specific user. Furthermore, deleting them would break webhook deduplication, causing redelivered webhooks to re-trigger OCC races and fail to return cached success responses. This operational table is explicitly excluded from the deletion scope. |

## 3. Product Wording Decision
Because `ProcessedWebhookEvent` retains `response_text` (which may include transaction descriptions in bot replies) to ensure webhook idempotency, we must align the feature promise. We will retain the user command as `ลบข้อมูลทั้งหมด` but clarify the scope in the confirmation prompt:

**Confirmation Prompt:**
"คุณกำลังจะลบข้อมูลทั้งหมด (รายการ {x} รายการ, เป้าหมายการออม และสถานะที่ค้างอยู่)\nการกระทำนี้ไม่สามารถย้อนกลับได้ (บันทึกของระบบบางส่วนจะถูกคงไว้เพื่อป้องกันข้อผิดพลาด)\n\nพิมพ์ 'ยืนยัน' เพื่อลบ หรือ 'ยกเลิก' เพื่อกลับไปหน้าปกติ"

Note: `ProcessedWebhookEvent` records are retained for webhook idempotency and currently have no TTL or purge mechanism, so the prompt must not describe that retention as temporary.

## 4. PendingAction Representation
We will reuse the existing `PendingAction` model with a new action type and a sentinel value to avoid schema migrations.

- `action_type`: `"confirm_delete_all"`
- `target_transaction_id`: `DELETE_ALL_TARGET_SENTINEL = 0`. (A magic value, but justified because `target_transaction_id` is a non-nullable `INTEGER`, `0` is a valid integer, SQLite/Postgres auto-increments start at `1`, and existing rows use valid IDs `>0`. This is the smallest safe option and completely avoids the complexity of an `ALTER COLUMN DROP NOT NULL` rebuild in SQLite).
- `version`: Starts at `1`.
- `created_at`: Current UTC.
- `expires_at`: `created_at + CONFIRM_DELETE_ALL_TTL` (TTL = 10 minutes, implemented as a distinct constant).
- `snapshot_*`: `NULL` (No undo snapshot is created).

## 5. Invalidation Rules (OCC)
While `confirm_delete_all` is active, any unrelated state-mutating command (creating a transaction, editing, setting a goal, completing a draft) must invalidate the pending confirmation before applying the mutation. 

We will update `invalidate_pending_action` in `repository.py` to match:
`PendingAction.action_type.in_(["confirm_delete", "confirm_delete_all"])`

This perfectly preserves the Phase 1E invariant: `undo_delete` is intentionally ignored by `invalidate_pending_action` and survives unrelated financial work, while both forms of deletion confirmation are safely discarded.

## 6. Atomic Delete-All Mechanics (`repository.execute_delete_all`)
The deletion must be atomic and OCC-guarded.
1. `session.execute(delete(PendingAction).where(... action_id, version, action_type="confirm_delete_all" ...))`
2. If rowcount is 0, raise `PendingActionConflictError`.
3. `session.execute(delete(PendingTransaction).where(line_user_id == ...))`
4. `session.execute(delete(SavingsGoal).where(line_user_id == ...))`
5. `session.execute(delete(Transaction).where(line_user_id == ...))`
6. Commit.
All rows are guaranteed to be filtered by `line_user_id` inside the same transaction block.

## 7. Concurrency & OCC Model
By relying on `PendingAction`'s versioned OCC logic:
- **`ยืนยัน` vs `ยืนยัน` (concurrent same intent):** The first transaction consumes the `PendingAction` and deletes the data. The second fails the `PendingAction` deletion (rowcount == 0), raises `PendingActionConflictError`, and the application safely replies with the cached conflict/retry response. No data is partially deleted.
- **`ยืนยัน` vs Edit/New Transaction:** If the edit commits first, it invalidates the `confirm_delete_all` action. The `ยืนยัน` will then fail OCC. If `ยืนยัน` commits first, the edit fails to find the target and reports "not found".

## 8. Webhook Idempotency Implications
Because `ProcessedWebhookEvent` is completely excluded from the deletion scope, webhook idempotency is entirely safe. If the LINE platform redelivers the exact same event that successfully triggered the delete-all, the idempotency middleware will instantly return the cached `format_delete_all_success` text without ever reaching the business logic or triggering a false OCC failure.

## 9. Full Routing / State Machine
**Trigger: `ลบข้อมูลทั้งหมด`**
- If no user data exists (0 transactions, no savings goal, no draft, no `undo_delete`), return "ไม่มีข้อมูลให้ลบครับ" (do not create an action).
- If `ABSENT`, expired, or ACTIVE (`confirm_delete` or `undo_delete`), override with a fresh `confirm_delete_all` (using `replace_pending_action_with_confirm` equivalent). Note: Overwriting `undo_delete` is an explicit design choice, as requesting a total wipe logically abandons a single-item undo.

**Trigger: `ยืนยัน`**
- ACTIVE `confirm_delete_all`: execute the atomic wipe.
- ACTIVE `confirm_delete`: process single deletion (Phase 1D unchanged).
- ACTIVE `undo_delete`: "unknown command" (Phase 1E unchanged).

**Trigger: `ยกเลิก`**
- ACTIVE `confirm_delete_all`: delete the action. Preserve active `PendingTransaction` drafts.
- ACTIVE `confirm_delete`: cancel single deletion. Preserve draft.
- ACTIVE `undo_delete`: falls through to cancel draft (Phase 1E unchanged).

**Trigger: `เลิกทำ`**
- ACTIVE `confirm_delete_all`: Returns `format_undo_unavailable()` (since delete-all cannot be undone and it is not a confirmed deletion).

**Trigger: Expired `confirm_delete_all`**
- Stop routing, preserve drafts, reply with `PENDING_ACTION_EXPIRED_REPLY` (or a delete-all specific expired reply).

## 10. PendingTransaction Semantics
An active `PendingTransaction` (draft) is explicitly part of the user's data. 
- Triggering `ลบข้อมูลทั้งหมด` leaves the draft active (waiting for confirmation).
- Sending `ยกเลิก` preserves the draft. 
- Sending `ยืนยัน` deletes the draft alongside the transactions.

## 11. Migration Requirements
No schema migration is required. The design specifically avoids altering tables or columns by utilizing a sentinel value (`DELETE_ALL_TARGET_SENTINEL = 0`) for the integer `target_transaction_id` column, fully compatible with the existing SQLite and Postgres schemas.

## 12. Test Matrix Requirements
**Trigger:**
- Exact command `ลบข้อมูลทั้งหมด` creates action.
- "No-data" case rejects creation.
- Overrides active draft safely.
- Overrides active `undo_delete` safely.
- Repeated `ลบข้อมูลทั้งหมด` resets action correctly.

**Confirmation:**
- Deletes `Transaction`, `SavingsGoal`, `PendingTransaction`, `PendingAction` strictly for the requesting user.
- Preserves `ProcessedWebhookEvent` and ALL data belonging to a second user.
- Consumes confirmation.
- No `snapshot` fields accidentally populated.
- Fails safely on missing/expired action (OCC loser).

**Cancellation:**
- Cancels `confirm_delete_all` only. Drafts and financial data are preserved.

**Mutation Interaction:**
- New transaction invalidates `confirm_delete_all`.
- Edit invalidates `confirm_delete_all`.
- Existing `undo_delete` remains preserved by unrelated mutations (regression test).

**Concurrency:**
- Distinct competing events for `ยืนยัน` vs `ยืนยัน` gracefully fall back to OCC rejection without partial deletion.
- Duplicate same webhook event uses `ProcessedWebhookEvent` idempotency correctly.

**Regression:**
- Phase 1D (delete latest) and Phase 1E (undo) remain fully operational.

## 13. PRODUCT_SPEC.md Sync Recommendations
After Phase 1F implementation is complete, `PRODUCT_SPEC.md` should be synchronized in a separate commit to reflect that:
- "แก้ไขรายการล่าสุด" (Phase 1C) is Shipped.
- "ขอคำยืนยันก่อนลบรายการ และเพิ่มทางเลือกย้อนกลับ" (Phase 1D, 1E) is Shipped.
- "ให้ผู้ใช้ลบข้อมูลทั้งหมดของตนเอง" (Phase 1F) is Shipped.
