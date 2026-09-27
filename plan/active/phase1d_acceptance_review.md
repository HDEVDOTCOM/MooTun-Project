# Phase 1D Architecture - Critical Review Finding and Correction

The critical review identified a routing blocker: `get_pending_action()` returning `None` collapsed two distinct states ('no action' and 'expired action cleaned up'), preventing `ยกเลิก` and `ยืนยัน` from deterministically blocking fall-through to Phase 1B pending states.

## 1. Revised Read Contract

Instead of returning `Optional[PendingAction]`, the repository read function will return a typed result:

```python
from dataclasses import dataclass
from enum import Enum
from models import PendingAction

class ActionStateKind(Enum):
    ACTIVE = "active"
    EXPIRED_CLEANED = "expired_cleaned"
    ABSENT = "absent"

@dataclass(frozen=True)
class PendingActionResult:
    kind: ActionStateKind
    action: PendingAction | None = None
```

## 2. Routing Table

### For `ยกเลิก` (Cancel)
- **ACTIVE(confirm_delete)**: Cancel confirm_delete only. Preserve PendingTransaction. Reply: `"ยกเลิกการลบแล้ว"`
- **EXPIRED_CLEANED**: Stop routing for this event. Reply: `"คำสั่งลบหมดอายุแล้ว กรุณาส่ง 'ลบล่าสุด' อีกครั้งหากต้องการลบ"`. Preserve PendingTransaction.
- **ABSENT** (with active PendingTransaction): Existing Phase 1B cancellation behavior (cancels draft).
- **ABSENT** (with no PendingTransaction): Preserve pre-Phase-1D fallback behavior (parser ambiguity rejection).

### For `ยืนยัน` (Confirm)
- **ACTIVE(confirm_delete)**: Confirm exact target and delete.
- **EXPIRED_CLEANED**: Stop routing. Reply: `"คำสั่งลบหมดอายุแล้ว กรุณาส่ง 'ลบล่าสุด' อีกครั้งหากต้องการลบ"`. Preserve PendingTransaction.
- **ABSENT**: Existing unresolved behavior (Unknown command).

### Other Commands
- **EXPIRED_CLEANED**: The expired cleanup signal does not block normal transaction entry or stateless commands. Normal commands treat `EXPIRED_CLEANED` identically to `ABSENT` (continue routing), ensuring the user isn't forced to acknowledge an expired state before adding a new transaction.

## 3. Repository API Implications

- `get_pending_action()` will return `PendingActionResult`.
- **Lazy-expiry OCC race rule is strictly preserved:** If Event A observes expired action A1, but its conditional cleanup DELETE (`rowcount == 1`) loses OCC (e.g. Event B concurrently replaced A1 with active A2), `get_pending_action()` raises `PendingActionConflictError`.
- The router catches `PendingActionConflictError` and returns the standard deterministic retry message (`"รายการที่ค้างอยู่มีการเปลี่ยนแปลงแล้วครับ กรุณาลองส่งอีกครั้ง"`), completely aborting the event. It does not reinterpret against A2.

## 4. Updated Test Matrix

The test suite must assert the following explicit cases:
1. **expired confirm_delete + active draft + `ยกเลิก`** => expiry reply, draft unchanged.
2. **expired confirm_delete + active draft + `ยืนยัน`** => expiry reply, target unchanged, draft unchanged.
3. **absent PendingAction + active draft + `ยกเลิก`** => Phase 1B draft cancellation still works.
4. **absent PendingAction + `ยืนยัน`** => existing unknown/unresolved behavior.
5. **expired cleanup OCC loser** => deterministic retry, no reinterpretation, draft unchanged.

## 5. Product Decisions Remaining

None. The tri-state contract cleanly isolates the Phase 1D confirmation lifecycle from the Phase 1B entry lifecycle. The architecture is mathematically sound and ready for implementation.

---

# Rebuild Status (Superseding the Pre-Implementation Section Above)

Status: **READY TO COMMIT** — final re-review PASSED.

The sections above are preserved as historical evidence of the original BLOCKED
finding and the correction it required. This section records the state after the
Phase 1D rebuild; it does not mark the feature approved or ready to commit.

## Prior Blocker

`get_pending_action()` returned `None` for both "no action" and "expired action
that was just cleaned up". The router therefore could not distinguish an expired
`confirm_delete` from an absent one, so an expired `ยกเลิก` fell through and
cancelled an unrelated `PendingTransaction`, destroying an active Phase 1B draft.

## Required Correction

An explicit tri-state read result instead of `Optional[PendingAction]`, with the
router stopping on the expired branch for `ยกเลิก` and `ยืนยัน`.

## Rebuild Status: Implemented

- Repository exposes the tri-state read contract. Implemented names:
  - `PendingActionState` with members `ACTIVE`, `EXPIRED_CLEANED`, `ABSENT`.
  - `PendingActionResult` with fields `state` and `action`.
- `get_pending_action()` returns `ACTIVE(action)`, `EXPIRED_CLEANED`, or `ABSENT`,
  and raises `PendingActionConflictError` when lazy-expiry cleanup loses its
  conditional DELETE.
- TTL is the fixed `PENDING_ACTION_TTL = timedelta(minutes=10)`.
- `app.py::handle_text_message` evaluates the expired state before reading
  `PendingTransaction`:
  - `ยกเลิก` / `ยืนยัน` while `EXPIRED_CLEANED` returns
    `"คำสั่งลบหมดอายุแล้ว กรุณาส่ง 'ลบล่าสุด' อีกครั้งหากต้องการลบ"` and stops;
    the draft and any transaction are untouched.
  - `ACTIVE(confirm_delete)` + `ยืนยัน` consumes the exact stored action and
    deletes the bound, user-scoped target atomically.
  - `ACTIVE(confirm_delete)` + `ยกเลิก` cancels only the `PendingAction`.
  - `ABSENT` falls through to the unchanged Phase 1B draft cancellation and
    pre-Phase-1D unresolved behavior.
  - Non-control commands treat `EXPIRED_CLEANED` as absence and continue normally.
- Transaction creation, successful Phase 1B completion, edit-latest, and repeated
  `ลบล่าสุด` invalidate an observed `confirm_delete` via OCC before their primary
  mutation; an OCC loser performs no mutation and returns the deterministic retry
  reply cached through `ProcessedWebhookEvent`.
- Lazy-expiry replacement race: cleanup is conditional on the exact
  `action_id` + `version` + expiry; the loser raises, returns the retry reply, does
  not reinterpret against the replacement, and does not mutate the draft.

## Documented Design/Implementation Contradiction

The pre-implementation section above proposes the type name `ActionStateKind`
with a `kind` attribute on `PendingActionResult`. The rebuilt implementation uses
`PendingActionState` and a `state` attribute. Semantics are identical; only the
names differ. The design text was intentionally left as-is rather than edited to
match the code.

## Tests

- Full suite: **143 passed**, 4 dependency deprecation warnings.
- Coverage includes: expired cancel/confirm with an active draft, Phase 1B
  cancellation fallback, absent-action confirm behavior, exact bound-target
  deletion, user isolation, stale-confirmation OCC, `created_at DESC, id DESC`
  selection, repeated delete replacement and missing-only-target handling,
  transaction/edit/draft-completion invalidation, stateless preservation, fixed
  TTL, lazy-expiry replacement race, OCC loser response caching, duplicate
  webhook delete/cancel/confirm delivery, and PostgreSQL DELETE-rowcount plus
  INSERT-claim safety.

## Final Re-Review Result (Appended)

Verdict: **PASS — READY TO COMMIT**.

The rebuild described above was independently re-reviewed in a read-only session
against the restored Phase 1D contract. The prior BLOCKED finding and its
correction remain preserved in the historical sections above; they are not
overwritten.

- Tri-state read, routing table, atomic bound-target confirmation, OCC /
  lazy-expiry behavior, and Phase 1E guardrails all match the implementation.
- Independent full-suite run (`python -m pytest -q`): **143 passed, 203 subtests
  passed, 3 warnings** (dependency deprecations only). An earlier note claiming
  "143 passed, 4 warnings" and no subtests was inaccurate and is superseded.
- Non-blocking observations were reported, not fixed, per the read-only review
  scope:
  - `repository.delete_latest_transaction` remains an unused direct-delete
    primitive still exercised by `test_repository.py`; it would bypass the Phase
    1D confirmation if called directly. Decision (remove vs. retain as internal)
    is deferred.
  - The `ActionStateKind` / `.kind` vs `PendingActionState` / `.state` naming
    difference remains intentionally documented.
  - The Coexistence wording ("an entry draft does not cancel a confirmation")
    versus the Other Commands rule (Phase 1B completion invalidates) can be
    clarified; the implementation already follows the specific rule, so this is a
    documentation clarity note, not a code defect.

## Current Status

**READY TO COMMIT** — final acceptance re-review PASSED. Not yet committed; commit
requires explicit user authorization.
