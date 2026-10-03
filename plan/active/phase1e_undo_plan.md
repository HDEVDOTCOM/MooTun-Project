# Phase 1E — Undo Delete Planning

Status: **ACCEPTED** — approved for implementation (2026-09-27); production deployment not yet verified.

## 1. Final Phase 1E Scope
Strictly limited to an immediate, time-bound "Undo" (`เลิกทำ`) for a confirmed transaction deletion. The scope focuses on safely restoring the exact original transaction identity, chronological position, and data. Long-term deletion history, audit logging, and multi-level undo are explicitly deferred to Phase 1F.

## 2. Typed PendingAction Read Contract
To preserve provenance after an expired row is lazily cleaned, the read operation will return explicitly typed states:
- `ACTIVE(action: PendingAction)`
- `EXPIRED_CLEANED(action_type: str)`
- `ABSENT`

**Cleanup Semantics:** The expired row must be cleaned using an OCC `DELETE` matching identity, version, and expiry. If the `DELETE` loses the OCC race, the operation returns a deterministic conflict/retry response, performs no draft mutation, and caches the webhook response without reinterpreting the new action state.

## 3. Complete Routing Matrix

### `ยกเลิก` (Cancel)
- **ACTIVE confirm_delete**: Cancel `confirm_delete` only. Preserve `PendingTransaction`.
- **EXPIRED_CLEANED(confirm_delete)**: Reply that confirmation expired. Preserve `PendingTransaction`. Stop routing.
- **ACTIVE undo_delete**: Preserve `undo_delete`. If `PendingTransaction` is active, execute Phase 1B draft cancellation. Otherwise, execute fallback.
- **EXPIRED_CLEANED(undo_delete)**: Treat undo state as gone. Do NOT emit a confirmation-expired reply. Execute Phase 1B cancellation if draft exists; otherwise fallback.
- **ABSENT**: Phase 1B / pre-Phase-1D fallback behavior.

### `ยืนยัน` (Confirm)
- **ACTIVE confirm_delete**: Execute atomic delete and transition to undo snapshot.
- **EXPIRED_CLEANED(confirm_delete)**: Reply that confirmation expired. Delete nothing.
- **ACTIVE undo_delete**: Preserve `undo_delete`. Reply with a no-active-confirmation response.
- **EXPIRED_CLEANED(undo_delete)**: Treat as no active confirmation. Do NOT emit a confirmation-expired reply.
- **ABSENT**: Unresolved behavior.

### `เลิกทำ` (Undo)
- **ACTIVE undo_delete**: Execute restore using exact snapshot.
- **EXPIRED_CLEANED(undo_delete)**: Reply that undo expired. Preserve `PendingTransaction`. Stop routing.
- **ACTIVE confirm_delete**: Preserve `confirm_delete` and `PendingTransaction`. Reply that no deletion has occurred yet.
- **EXPIRED_CLEANED(confirm_delete)**: Treat as no undo available. Do not emit an undo-expired reply.
- **ABSENT**: Reply `"ไม่มีรายการให้ย้อนกลับครับ"`. Preserve `PendingTransaction`. Stop routing.

## 4. Action-Type Invariants
- `CONFIRM_DELETE_TTL = timedelta(minutes=10)`
- `UNDO_DELETE_TTL = timedelta(minutes=10)`

**`confirm_delete` Invariants:**
- `target_transaction_id`: Required.
- `snapshot_*` fields: All strictly NULL.
- `created_at` / `expires_at`: Fixed at creation (`CONFIRM_DELETE_TTL`).

**`undo_delete` Invariants:**
- `snapshot_transaction_id`: Required.
- `snapshot_transaction_type`: Required.
- `snapshot_amount_satang`: Required.
- `snapshot_category`: Required.
- `snapshot_description`: Follows original `Transaction` nullability.
- `snapshot_occurred_on`: Required.
- `snapshot_created_at`: Required.
- `target_transaction_id`: Retains the deleted original ID.
- `expires_at`: Reset strictly to `successful_deletion_time + UNDO_DELETE_TTL`.

## 5. confirm_delete -> undo_delete Atomic Transition
The exact transition executed entirely within one outer transaction:
1. Conditionally validate the exact active `confirm_delete` action.
2. Read the exact user-scoped target `Transaction` row.
3. Populate snapshot fields from the target row.
4. Transition `action_type` to `"undo_delete"`.
5. Increment `version`.
6. Reset `expires_at` using `deletion time + UNDO_DELETE_TTL`.
7. `DELETE` the target transaction.

If any part fails (including OCC), the entire outer transaction rolls back (no partial delete, no partial undo action).

## 6. Exact-Original-ID Restore Policy
Restores the exact original ID, `created_at`, `occurred_on`, and all other fields. There is no silent fallback to a new ID.

## 7. PostgreSQL Sequence Reasoning
Reinserting an already-issued lower ID is safe because sequence allocation does not decrement. Subsequent sequence-generated IDs remain above the sequence high-water mark; no `setval()` is required.

## 8. SQLite Identity/Ordering Assumptions
**[RESOLVED]**
A plain `INTEGER PRIMARY KEY` lets SQLite reuse the highest rowid after it is deleted,
so the original assumption did **not** hold by default and a re-created "latest"
transaction could reuse a just-deleted id. This is fixed by enabling
`sqlite_autoincrement=True` on the `transactions` table, which emits
`INTEGER PRIMARY KEY AUTOINCREMENT` so `sqlite_sequence` tracks the highest id ever
issued and deleted ids are never reused. Explicit reinsertion of the original id is
therefore safe and cannot collide with later auto-generated ids.

**Development/pilot migration caveat:** `sqlite_autoincrement=True` affects only
freshly created SQLite `transactions` tables. Phase 1D SQLite files have a plain
`INTEGER PRIMARY KEY`; `create_all` and the existing `ALTER TABLE` snapshot-column
migration cannot retrofit `AUTOINCREMENT`. On those files, deleting the highest id
and creating a new transaction before `เลิกทำ` can reuse the deleted id and make
exact-id restore collide. Recreate pre-Phase-1E local/dev SQLite database files
before exercising Undo Delete (including local pilots). PostgreSQL is the supported
production path and uses a sequence that does not reuse issued ids; this SQLite
caveat does not apply there. No SQLite table-rebuild migration is included in
Phase 1E. This does not claim a production deployment has been verified.

## 9. Structured Snapshot Schema
Add structured nullable columns to `PendingAction` whose SQL types exactly match the `Transaction` model (without duplicating `line_user_id`):
- `snapshot_transaction_id` (Integer)
- `snapshot_transaction_type` (String(16))
- `snapshot_amount_satang` (BigInteger)
- `snapshot_category` (String(100))
- `snapshot_description` (String(500))
- `snapshot_occurred_on` (Date)
- `snapshot_created_at` (DateTime(timezone=True))

## 10. Migration Plan
Hand-written migrations in `database.py` conditionally append the nullable
`snapshot_*` columns (`ALTER TABLE ... ADD COLUMN ...`) to existing Phase 1D
SQLite/PostgreSQL schemas and fresh DB setups. This upgrades the action snapshot
schema only; it does **not** change the primary-key allocation behavior of an
existing SQLite `transactions` table. Follow the recreation policy in §8 for
pre-Phase-1E local/dev SQLite files.

## 11. Undo TTL
The window to undo a deletion is exactly `10 minutes`, beginning at the moment the deletion is successfully executed (not when the original `ลบล่าสุด` was triggered).

## 12. Replacement Behavior (`ลบล่าสุด` vs Active Undo)
If a user sends `ลบล่าสุด` while an `undo_delete` is active:
- Check if a current latest transaction exists.
- If **none exists**: Preserve `undo_delete` unchanged. Reply no transaction to delete.
- If **exists**: OCC-safely replace the `undo_delete` with a new `confirm_delete` targeting the exact current latest transaction. The old undo opportunity is intentionally discarded.

## 13. Preservation of Undo Across Unrelated Financial Work
New standalone transactions, Phase 1B completions, Phase 1C edits, and stateless commands must **preserve** an active `undo_delete`. Unrelated operations must not increment the undo version, refresh its expiry, or delete it. The existing invalidation helper must be updated to target `confirm_delete` only.

## 14. OCC / Concurrency Rules
All state transitions (`ยืนยัน`, `เลิกทำ`, replacement) predicate on `line_user_id`, `action_id`, `version`, `action_type`, and `expires_at > NOW()`. If a transition loses the OCC race, the operation safely returns a deterministic conflict/retry response and performs no mutation.

## 15. Webhook Idempotency Rules
- **Same Webhook Redelivery**: `ProcessedWebhookEvent` prevents re-execution. Redeliveries immediately return the cached response.
- **Distinct Concurrent Events**: `PendingAction` OCC controls the state transition. For two distinct concurrent `เลิกทำ` events, one wins; the loser fails OCC and safely returns a deterministic conflict/absent response. Never restore twice, never reinterpret.

## 16. ID-Collision Policy
If the original ID is unexpectedly occupied during restoration (e.g., manual DB tampering):
- Conditionally consume the exact `undo_delete` action in the outer transaction.
- Attempt the explicit-ID `INSERT` inside a nested savepoint (`db.begin_nested()`).
- If `INSERT` fails due to PK collision: Roll back *only* the nested restore savepoint. Leave the action consumption intact (committed) in the outer transaction. Commit a deterministic restore-conflict response (`"ไม่สามารถย้อนกลับได้เนื่องจากมีข้อมูลอื่นทับซ้อน"`).

## 17. Ordering Semantics
Restored rows return to their exact `created_at DESC, id DESC` position. The `created_at` timestamp ensures the restored transaction slots back properly, and exact ID restoration preserves tie-breaking semantics. Newer transactions created after the deletion remain correctly ordered.

## 18. Full Test Matrix
Includes standard Phase 1D tests plus:
- `EXPIRED_CLEANED(confirm_delete)` + `ยกเลิก` + draft => confirmation expiry reply, draft preserved.
- `EXPIRED_CLEANED(undo_delete)` + `ยกเลิก` + draft => draft cancellation, NO confirmation-expiry reply.
- `EXPIRED_CLEANED(confirm_delete)` + `ยืนยัน` => confirmation expiry reply.
- `EXPIRED_CLEANED(undo_delete)` + `ยืนยัน` => no active confirmation behavior.
- `EXPIRED_CLEANED(undo_delete)` + `เลิกทำ` => undo expiry reply.
- `EXPIRED_CLEANED(confirm_delete)` + `เลิกทำ` => no undo available/not yet deleted response.
- ACTIVE `undo_delete` + `ยกเลิก` + draft => undo preserved, draft cancelled.
- ACTIVE `undo_delete` + `ยืนยัน` => undo preserved, no delete confirmation executed.
- ACTIVE `undo_delete` + new transaction/edit/Phase 1B completion => undo row unchanged logically/byte-for-byte.
- ACTIVE `undo_delete` + `ลบล่าสุด` (empty DB) => undo preserved.
- ACTIVE `undo_delete` + `ลบล่าสุด` (transactions exist) => undo safely replaced by new `confirm_delete`.
- Original-ID restore collision successfully consumes action while rolling back insert, yielding deterministic collision response.
- Fresh SQLite schema includes `AUTOINCREMENT`/`sqlite_sequence`: after creating ids 1 and 2, deleting id 2 and adding a normal transaction must generate id 3, leaving exact-id restore of id 2 possible.
- Schema upgrades support new nullable snapshot columns correctly.

## 19. Phase 1F/History Deferred Scope
Explicitly deferred:
- Soft deletes on the `transactions` table.
- A "History" or "Trash" view for users to browse past deletions.
- Restoring transactions deleted beyond the 10-minute active `undo_delete` window.
- Multiple/nested levels of undo.

## 20. Remaining Product Decisions
None.
