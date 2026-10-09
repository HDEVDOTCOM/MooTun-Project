# Phase 1H: In-Bot Feedback & Ratings Plan

**Status:** `APPROVED FOR BUILD`

## 1. Exact Phase 1H Command Syntax & Parsing Contract

**Trigger:** `เสนอแนะ` (Exact Thai prefix only. No aliases).

**Parsing Contract:**
- The parser must **not** reconstruct comments using `split()` and `join()`. 
- The exact regex logic (e.g., `^เสนอแนะ(?:\s+(\S+))?(?:\s+([\s\S]*))?$`) must be used to:
  1. Recognize the exact prefix `เสนอแนะ`.
  2. Capture the exact next non-whitespace token as the rating.
  3. Capture the *entire remainder* of the original input string as the comment.
- **Rating Validation:** The captured rating token must strictly match `^[1-5]$`. 
  - Rejects: `0`, `6`, `-1`, `5.5`, `05`, or malformed text. 
  - An invalid rating (like `5.5`) must be rejected as an invalid feedback command. It must **not** fall through to Phase 1B financial processing.
- **Comment Capture:** The captured remainder is the comment. Only surrounding whitespace is stripped. Internal spaces, tabs, newlines, Thai characters, emojis, and punctuation are preserved exactly as submitted.
- **Bare Command:** Input matching only `เสนอแนะ` creates no feedback, inserts nothing, and returns an instructional message.

## 2. Comment Policy

- **Optionality:** The comment is optional.
- **Length Constraint:** Exactly **1000 characters** maximum (checked *after* surrounding whitespace is stripped).
- **Enforcement:** If over 1000 characters, it is **rejected deterministically**. It will NOT be truncated, and no `UserFeedback` row will be created. An exact over-limit reply is returned.

## 3. UserFeedback Schema Model

```python
class UserFeedback(Base):
    __tablename__ = "user_feedback"
    __table_args__ = (
        CheckConstraint(
            "rating >= 1 AND rating <= 5", name="ck_user_feedback_valid_rating"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    line_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
```

**Timestamp Convention:** Enforces the existing repository standard (`datetime.now(timezone.utc)` for storage). Datetimes are timezone-aware UTC.

## 4. Delete All Privacy Integration

- **`UserDataSummary` Extension:** Adds `has_feedback: bool`.
- **No-Data Gate:** `has_any_data` returns `True` if `has_feedback` is True. A user with zero transactions/goals/drafts but ONE feedback row MUST still receive the Delete All confirmation prompt.
- **Atomic Deletion:** Confirmed Delete All executes `DELETE FROM user_feedback WHERE line_user_id = :uid` to erase the exact user's feedback.

## 5. Correct Concurrency Semantics

Feedback operations do **not** claim global per-user serialization.

- **No active `confirm_delete_all`:** Distinct feedback events are independent, append-only inserts.
- **Multiple feedback events vs. SAME active `confirm_delete_all`:** 
  - Multiple requests may carry the same expected `action_id` and `version`.
  - Exactly ONE wins the OCC invalidation, removes the pending action, and inserts feedback.
  - The loser receives a `PendingActionConflictError` (deterministic retry), inserts NO feedback, and is NOT reinterpreted against the replacement state.
- **Feedback vs. Delete All Cancellation:**
  - If feedback OCC wins first: `confirm_delete_all` is removed, feedback inserts. Competing cancellation observes the loss and handles it according to existing `PendingAction` behavior.
  - If cancellation wins first: feedback OCC invalidation loses, inserts nothing, and returns a deterministic conflict/retry response with no reinterpretation.
- **Feedback vs. Confirmed Delete All:**
  - If feedback invalidation wins: old delete-all confirmation is gone, feedback inserts.
  - If confirmed Delete All wins: existing user data is wiped. A stale feedback event expecting the old action loses OCC and inserts nothing. (Only a genuinely *later* feedback event observing no active confirmation may create post-delete feedback).

## 6. Interaction with Other Pending State

Feedback explicitly **preserves**:
- `PendingTransaction`
- `confirm_delete`
- `undo_delete`

Feedback parsing does not refresh TTL, increment versions, modify snapshots, or cancel these drafts.

## 7. Webhook Idempotency (Actual Semantics)

Relies on the exact existing `ProcessedWebhookEvent` architecture without modification:
- **Same LINE `webhook_event_id`:** 
  - `UserFeedback` is inserted at most once. Duplicate delivery never creates another row.
  - If the event is already processed and `reply_sent` is True (or no response was needed): the retry is swallowed completely.
  - If the event is processed but `reply_sent` is False (and a cached `response_text` exists): the system uses the cached response to retry sending the reply, preserving exact original state.
- **Distinct Webhook IDs:** Evaluated anew and may create distinct feedback rows.

## 8. CSV Export Interaction

- **Excluded:** `UserFeedback` does NOT appear in the CSV. CSV remains exclusively for `Transaction` data.

## 9. Messages (Matching MooTun Persona)

Messages must exactly match the existing bot persona, which uses `ครับ` as the polite particle.

- **Success:** `"บันทึกข้อเสนอแนะระดับ {rating} ดาวเรียบร้อย ขอบคุณที่ช่วยพัฒนาหมูตุ๋นครับ 🐷"`
- **Bare command:** `"หากต้องการส่งข้อเสนอแนะ กรุณาพิมพ์ 'เสนอแนะ [คะแนน 1-5]' หรือ 'เสนอแนะ [คะแนน] [ข้อความ]' เช่น:\n- เสนอแนะ 5\n- เสนอแนะ 4 ใช้ง่ายดีครับ"`
- **Invalid rating:** `"คะแนนข้อเสนอแนะต้องเป็นตัวเลข 1 ถึง 5 เท่านั้นครับ เช่น 'เสนอแนะ 5'"`
- **Over 1000 chars:** `"ข้อความเสนอแนะยาวเกินไป (สูงสุด 1000 ตัวอักษร) กรุณาสรุปข้อความแล้วส่งใหม่อีกครั้งครับ"`

## 10. Updated Test Matrix

- **Parser Correctness:**
  - `เสนอแนะ 1`, `เสนอแนะ 5` (Valid boundaries).
  - Optional comment with repeated internal spaces preserved exactly (not reconstructed via split/join).
  - `เสนอแนะ 0`, `เสนอแนะ 6`, `เสนอแนะ -1`, `เสนอแนะ 5.5`, `เสนอแนะ 05`, malformed token (Rejected).
  - Bare `เสนอแนะ` (Instructional).
  - Exactly 1000 chars accepted, 1001 chars rejected.
  - Unsupported aliases safely fall through.
- **Concurrency & Races:**
  - Two distinct feedback events racing on same `confirm_delete_all`: one wins, one is deterministic OCC loser, exactly ONE feedback inserted.
  - Cancellation wins before feedback invalidation: feedback inserts nothing.
  - Feedback invalidation wins before cancellation: feedback inserts successfully.
  - Confirmed Delete All wins the stale-action race: stale feedback inserts nothing.
- **Privacy & State:**
  - UTC timestamp usage and `CHECK` constraint tests.
  - Feedback-only user receives Delete All prompt.
  - Confirmed Delete All purges exact user feedback.
  - Drafts (`PendingTransaction`, `confirm_delete`) safely preserved.
- **Idempotency:**
  - Same webhook ID correctly honors `reply_sent`/cached-response behavior.
- **Persona:**
  - Phase 1H message tone strictly matches the existing `ครับ` paradigm.

## 11. Implementation Order & Scope Exclusions

- **Order:** `models.py` -> `repository.py` (Insert + Delete All Integration) -> `parser.py` (Regex Capture) -> `messages.py` -> `app.py` -> Tests. Docs sync is deferred.
- **Exclusions:** Admin dashboards, aliases, multi-step flows, LLMs, editing feedback, exporting feedback to CSV.
