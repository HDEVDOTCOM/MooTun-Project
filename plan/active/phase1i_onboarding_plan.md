# Phase 1I: New-user Welcome / Onboarding Plan

**Status:** `COMPLETE / PUSHED / DEPLOYED / PRODUCTION VERIFIED`

## 1. Exact Phase 1I Scope

In scope:

- Welcome on an authenticated direct-user LINE `follow` event (add and unblock).
- Manual replay via the exact command `เริ่มใช้งาน` — repeatable at any time, surrounding
  whitespace handled per existing parser conventions, no aliases, no arguments, no
  business-state mutation.
- The same deterministic guide for both triggers.
- Help discoverability: `ช่วยเหลือ` lists `เริ่มใช้งาน`; the already-shipped `เสนอแนะ`
  command is included in help as part of command discovery.
- One static Thai text reply; no wizard, no acknowledgment requirement, no blocking or
  replacing of the user's real commands.

Out of scope: see §24.

## 2. LINE Follow / Unblock Lifecycle

- The trigger is LINE's authenticated `follow` event, not inferred newness.
- LINE sends `follow` both when a user adds the account and when the user unblocks it.
  The contract is therefore **welcome on follow/unblock**, NOT "welcome exactly once in a
  user's lifetime". Repeated legitimate follow events may produce repeated welcomes.
- No `welcomed` flag, no per-user lifetime tracking.
- The first real command is never blocked or replaced. If the first received event is a
  text message and no `follow` event was observed, process the text normally; do not
  guess that the user is new and do not prepend/append a welcome automatically.
- A `follow` event and a first text command are separate webhook events and may receive
  separate replies. No cross-request reply ordering is promised.
- After a confirmed Delete All, no automatic welcome is produced; `เริ่มใช้งาน` remains
  available.

## 3. No Lifetime-Welcome Persistence

- No new table, no new column, no onboarding flag, no schema migration.
- First contact is detected ONLY from the authenticated LINE `follow` lifecycle event.
- Never infer first contact from: zero transactions, no savings goal, no draft/action,
  feedback absence, Delete All state, `ProcessedWebhookEvent` history, or
  `get_user_data_summary()`.
- The existing `ProcessedWebhookEvent` claim/response cache is still used for webhook
  idempotency (this is not "onboarding persistence").

## 4. Repeatable Manual `เริ่มใช้งาน`

- Exact command `เริ่มใช้งาน`; no aliases; no arguments.
- Surrounding whitespace may follow existing parser conventions (`text.strip()`).
- Repeatable at any time; each distinct message intentionally produces the guide.
- Informational only: no business-state mutation.

## 5. Malformed Onboarding Behavior

Known onboarding-intent text with trailing arguments must NOT fall through to Phase 1B
follow-up, semantic transaction parsing, or draft merge.

Detection is boundary-aware (no substring interception). Conceptual contract:

```python
t = text.strip()  # Thai has no case; strip surrounding whitespace
keyword = "เริ่มใช้งาน"
remainder = t[len(keyword):]

if t == keyword:
    # valid welcome
elif remainder and remainder[0].isspace():
    # malformed onboarding usage (keyword + whitespace + anything)
else:
    # not onboarding; falls through to normal routing
```

Required semantics:

| Input | Result |
|---|---|
| `เริ่มใช้งาน` | valid welcome |
| ` เริ่มใช้งาน \n` (surrounding whitespace) | valid welcome after `strip()` |
| `เริ่มใช้งาน    ` (trailing spaces only) | valid welcome after `strip()` |
| `เริ่มใช้งาน 100` | malformed onboarding usage |
| `เริ่มใช้งาน abc` | malformed onboarding usage |
| `เริ่มใช้งาน เพิ่มเติม` | malformed onboarding usage |
| `เริ่มใช้งานใหม่` (no whitespace boundary) | NOT onboarding; falls through normally |

Malformed onboarding must:

- return the deterministic onboarding usage reply (see §6);
- create no `Transaction`;
- create no `PendingTransaction`;
- mutate no `PendingAction`;
- not refresh TTL, change version, or alter snapshots.

No substring-based interception (e.g. a bare `startswith("เริ่มใช้งาน")` guard is
insufficient and would wrongly capture `เริ่มใช้งานใหม่`).

## 6. Concise Deterministic Welcome Copy

Welcome message (concise first-contact copy; `ครับ` persona; deterministic; under the
LINE text limit):

```
🐷 สวัสดีครับ หมูตุ๋นช่วยจดรายรับ รายจ่าย และเงินออมผ่านแชตครับ

เริ่มได้เลย เช่น
• ข้าว 50
• เงินเดือน 20000
• สรุปเดือนนี้

ดูคำสั่งเพิ่มเติม: ช่วยเหลือ
ดูข้อความเริ่มต้นนี้อีกครั้ง: เริ่มใช้งาน
ส่งข้อเสนอแนะ: เสนอแนะ 5 ใช้ง่ายดีครับ

ข้อมูลของคุณแยกตาม LINE user ID และลบข้อมูลของคุณได้ด้วย `ลบข้อมูลทั้งหมด` ครับ
```

Malformed onboarding usage reply (deterministic; `ครับ` persona; does not echo user
input):

```
คำสั่งเริ่มใช้งานไม่ต้องใส่ข้อมูลเพิ่มเติมครับ พิมพ์ 'เริ่มใช้งาน' ได้เลย
```

Detailed retention / `ProcessedWebhookEvent` wording (records are intentionally retained
for webhook idempotency, have no TTL/purge, and must not be described as temporary) stays
in `PRODUCT_SPEC.md` and help documentation, NOT in the welcome message, unless
`PRODUCT_SPEC.md` explicitly requires it in the onboarding bubble. No false claims: the
welcome does not state that Delete All removes `ProcessedWebhookEvent` records, and does
not claim that reading the welcome is recorded consent.

## 7. Follow-Event Webhook Routing

Current `app.py` accepts only direct-user text-message events, so `follow` events are
ignored today. Smallest safe extension:

1. Keep raw-body `verify_webhook_signature()` BEFORE any DB write.
2. Validate common fields: `webhookEventId` (str), `source.type == "user"`,
   `source.userId` (str), and `type` in `{"message", "follow"}`.
3. For `message`: require `message.type == "text"` as today.
4. For `follow`: no `message` object is required; do NOT invoke text parsing.
5. Reuse the existing claim/cache/commit/reply flow
   (`get_webhook_event` → `mark_webhook_processed` → welcome/`handle_text_message` →
   `set_webhook_response` → `mark_webhook_replied`).
6. A `follow` event generates the deterministic welcome directly, bypassing
   `handle_text_message()`.

Unsupported event/source types remain ignored according to current behavior.

## 8. Early Informational Routing Before Pending-State Reads

- Route `เริ่มใช้งาน` (valid or malformed) BEFORE `get_pending_action()` and
  `get_pending_transaction()` — the same early position as the `ส่งออกข้อมูล` shortcut.
  This avoids lazy-expiry side effects and guarantees no pending-state mutation.
- `parse_followup()` already rejects `SimpleCommand` as financial evidence; onboarding
  must never enter draft merge.
- Follow welcome bypasses `handle_text_message()` entirely.

Routing order: `ส่งออกข้อมูล` shortcut → `เริ่มใช้งาน` early return → pending-action reads
→ pending-transaction reads → normal `parse_command()` flow.

## 9. No Schema / Model / Repository Persistence Changes

- No changes to `models.py`, `repository.py`, `database.py`, or dependencies.
- Onboarding is stateless apart from the existing webhook claim/response cache.

## 10-13. Pending-State Preservation Contract

Both triggers (follow welcome and `เริ่มใช้งาน`, including malformed) must leave the
following byte-identical:

- **PendingTransaction** — draft id, type, amount, category, description, inference_rule,
  occurred_on, `expires_at`, version.
- **confirm_delete** — action_id, version, target, snapshots = NULL, `expires_at`.
- **undo_delete** — including all snapshot fields.
- **confirm_delete_all** — sentinel target 0, `expires_at`.

## 14. No TTL / Version / Snapshot Mutation

Onboarding must not:

- refresh `expires_at`;
- increment/decrement `version`;
- alter snapshots;
- invalidate Delete All;
- trigger lazy pending-state cleanup when routing can safely avoid it (early return).

The onboarding command is informational only.

## 15. Existing `ProcessedWebhookEvent` Idempotency Semantics

Reuse exactly; no new outbound-delivery subsystem:

- Same `webhookEventId`: process at most once.
- `reply_sent=True`: current skip behavior.
- Cached unsent `response_text`: current resend behavior; do NOT regenerate onboarding or
  rerun command handling.
- DB failure before commit: claim and cached response roll back together.
- LINE reply failure after commit (502): cached response retained for redelivery.
- Distinct follow events: each may produce a welcome.
- Distinct `เริ่มใช้งาน` messages: each intentionally produces a guide.

Concurrency:

- Same-event concurrent delivery: unique event claim yields one processing winner.
- Two distinct follow events: each may welcome (no lifetime suppression).
- Follow vs. first transaction: independent; welcome performs no business-state mutation.
- Two simultaneous first text commands: existing command semantics; onboarding adds no
  first-user claim or serialization.

## 16. No Cross-Request Ordering Promise

A `follow` welcome and a separate first text command are distinct webhook events; their
reply ordering is not guaranteed. The implementation does not promise ordering.

## 17. Delete All Interaction

- There is no onboarding-owned persistent user data.
- A welcome-only user still has no deletable user data; `ลบข้อมูลทั้งหมด` must still
  report no data (`ไม่มีข้อมูลให้ลบครับ`) when nothing else exists. The retained
  operational webhook cache is not user data.
- Delete All does not create or reset onboarding state.
- No automatic welcome after Delete All.
- Manual `เริ่มใช้งาน` remains available.

## 18. Feedback Interaction

- Onboarding must not create, edit, or delete `UserFeedback` rows.
- Existing feedback behavior remains unchanged.

## 19. CSV Interaction

- Onboarding must not create/revoke `ExportToken` or alter its expiry.
- Onboarding never appears in CSV output.
- Existing CSV behavior remains unchanged.

## 20. User Isolation

- Onboarding does not query or mutate another user's business data.
- Replies correspond to the event/reply token being handled.
- No personalization or profile lookup in Phase 1I.

## 21. LINE OA Built-in Greeting Check (Production Prerequisite)

Record as a deployment prerequisite: before production smoke, verify the LINE Official
Account built-in greeting configuration. If a platform-configured automatic greeting is
enabled, the application-level `follow` welcome may create duplicate greetings. Do not
solve console configuration in application code, and do not block local implementation on
it — but require the check before production verification.

## 22. Complete Test Matrix

Welcome copy:

- deterministic text; `ครับ` persona; supported examples; under the LINE text limit;
  contains no personalization.

Follow webhook:

- valid signed direct-user `follow` returns the welcome;
- `follow` does not require a `message` object;
- invalid signature performs no processing/reply and creates no event record;
- unsupported source/event types remain ignored;
- same `follow` webhook ID deduplicates;
- cached unsent `follow` response is reused without regeneration;
- distinct `follow` IDs each welcome.

Manual command:

- exact `เริ่มใช้งาน`;
- surrounding whitespace;
- `เริ่มใช้งาน 100` / `เริ่มใช้งาน abc` / `เริ่มใช้งาน เพิ่มเติม` → malformed usage reply;
- `เริ่มใช้งานใหม่` → explicitly NOT onboarding (falls through normally);
- repeatability;
- no alias;
- cannot become a transaction;
- cannot become Phase 1B follow-up.

Malformed isolation (each with an ACTIVE row and `_row_state` byte comparison):

- `เริ่มใช้งาน 100` creates no `Transaction` (even though `100` is a valid amount);
- `เริ่มใช้งาน abc` creates no `PendingTransaction`;
- preserves ACTIVE `confirm_delete` byte-identical;
- preserves ACTIVE `undo_delete` including snapshots byte-identical;
- preserves ACTIVE `confirm_delete_all` byte-identical;
- expired-row behavior follows the intended early informational-command contract.

State preservation:

- PendingTransaction unchanged;
- confirm_delete unchanged;
- undo_delete including snapshots unchanged;
- confirm_delete_all unchanged;
- ACTIVE rows compare identity/version/expiry/snapshots.

Regression:

- first transaction works normally;
- incomplete transaction still creates/continues a draft normally;
- feedback still works;
- Delete All unchanged;
- CSV unchanged;
- webhook PostgreSQL claim/RETURNING behavior unchanged.

Isolation:

- A onboarding does not mutate B transactions/feedback/pending state.

Failure / idempotency:

- transaction/cache rollback behavior;
- reply failure / cached response behavior.

## 23. Production Smoke Plan

1. Verify the LINE OA built-in greeting setting first (see §21).
2. Fresh add with a suitable test account → welcome.
3. Block/unblock → welcome again.
4. `เริ่มใช้งาน` twice → repeatable.
5. Normal transaction after/beside onboarding.
6. Replay command while a draft is active.
7. Replay command while `confirm_delete` is active.
8. Replay command while `undo_delete` is active.
9. Replay command while `confirm_delete_all` is active.
10. Delete All for a welcome-only user still reports no user data.
11. Feedback still works.
12. CSV still works.

If no fresh/second LINE account is available, do NOT falsely report fresh-add or
cross-user production smoke; record those as automated-test/review coverage only.

## 24. Explicit Out of Scope

- lifetime onboarding tracking / onboarding-complete flag;
- onboarding analytics;
- profile retrieval / personalization;
- consent record;
- multi-step tutorial / wizard state;
- automatic broadcast/backfill;
- reminders;
- rich menu changes;
- Flex Messages;
- LIFF;
- problem-reporting workflow;
- cleanup of historical delete/undo plans;
- `ExportToken` cleanup;
- `repository.delete_latest_transaction` cleanup;
- unrelated docs cleanup;
- runtime LLM.

## 25. Safest Implementation Order

1. Add the welcome and malformed-usage formatters in `messages.py` plus focused content
   tests.
2. Add the `เริ่มใช้งาน` boundary check and `ช่วยเหลือ` entry in `parser.py`.
3. Add the early informational route in `app.py` (before pending reads) and extend
   webhook eligibility/dispatch for direct-user `follow` through the existing
   claim/cache/commit/reply flow.
4. Add state-preservation, idempotency, concurrency, malformed-isolation, and isolation
   tests.
5. Run focused tests, then the full suite (`pytest -q`), and `git diff --check`; review the
   diff.

## 26. Expected Implementation Files

Expected changes later:

- `app.py`
- `parser.py`
- `messages.py`
- tests (e.g. `test_webhook.py` / `test_parser.py` / `test_messages.py` or a dedicated
  `test_onboarding.py`)

No expected changes later:

- `models.py`
- `repository.py`
- `database.py`
- dependencies

## 27. Remaining Blockers / Product Decisions

- Approve the whitespace-boundary contract in §5 (exact keyword OR keyword followed by
  whitespace; `เริ่มใช้งานใหม่` intentionally not onboarding).
- Approve the malformed usage reply string in §6.
- Approve the concise welcome copy in §6.
- Confirm whether `PRODUCT_SPEC.md` requires retention-duration disclosure inside the
  onboarding bubble itself; if so, add only the minimum accurate wording. Otherwise keep
  detailed retention wording in `PRODUCT_SPEC.md`/help.
- Confirm LINE OA built-in greeting ownership before production smoke (§21).

No code blocker was found for the proposed stateless design.

## 28. Historical Plan Note

`plan/active/v0.2_delete_undo_plan.md` is actually a Phase 1D Delete Confirmation
contract whose functionality has shipped. It is classified as historical/obsolete as a
future product phase and must be handled in a separate documentation-maintenance task.
Do NOT edit, move, or archive it during Phase 1I planning.
