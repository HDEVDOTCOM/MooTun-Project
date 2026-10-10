# MooTun Web App in LINE — Read-only Foundation

**Status:** `APPROVED FOR BUILD`
**Repository baseline:** `main` at `c484f986defa48f9f57c364bbb42265cb2d1da13`

## 1. Product Outcome

Provide a small web application inside LINE where an authenticated user can view:

1. Current-month income, expense, net amount, and transaction count.
2. Five recent transactions.
3. Current savings goal and progress.
4. Static privacy/help information.

Chat remains the interface for recording and changing financial data.

The foundation must prove:

- LINE identity maps correctly to existing MooTun ownership.
- The web interface displays the same underlying data as chat.
- Opening, refreshing, or navigating the web application performs no application-database writes.
- Users need no separate MooTun account or password.

This is a read-only product slice, not a general dashboard platform.

## 2. Current Platform State

### Owner-confirmed LINE configuration

| Item | Current state |
|---|---|
| Provider | `MooTun Project` |
| Messaging API channel | `น้องหมูตุ๋น` |
| MINI App channel | `MooTun` |
| Provider relationship | Both channels under the same Provider |
| MINI App region/status | Thailand / Unverified |
| Internal environments | Developing, Review, Published |
| Display size | Full |
| Intended scope | `openid` only |
| Add friend / Scan QR | Off / Off |
| LIFF URLs | Exist for each internal environment |
| Application endpoint URLs | Not yet configured |

These platform settings are supplied by the owner; this planning session did not inspect or change the console.

### Repository-confirmed backend

- FastAPI, SQLAlchemy, PostgreSQL production, SQLite development/tests.
- Existing application routes: `/`, `/ready`, `/webhook`, `/export/{token}`.
- No authenticated web API or frontend.
- Repository queries already support monthly totals, recent transactions, and savings retrieval.
- Pending-state helpers can perform lazy expiry cleanup.
- `ProcessedWebhookEvent` is specific to webhook processing.
- No existing browser session or web-account model.

### MINI App environment distinction

Each internal environment has its own LIFF ID and channel identity.

The frontend must initialize the **Developing LIFF ID**. The backend must accept only the corresponding **Developing internal channel ID** as the token audience.

Neither the Messaging API channel ID nor a browser-selected channel ID is acceptable.

LINE documents that settings changes for unverified MINI Apps can propagate to Published. Before eventual configuration, inspect the current console behavior and record any automatic propagation. Do not assume all edits are Developing-isolated.

References:

- [MINI App console guide](https://developers.line.biz/en/docs/line-mini-app/discover/console-guide/)
- [MINI App development overview](https://developers.line.biz/en/docs/line-mini-app/develop/develop-overview/)

## 3. Authentication / Identity

### Chosen architecture

Use the raw LIFF ID token as a bearer credential, verified server-side **on every financial API request**.

Do not introduce:

- MooTun accounts or passwords.
- Browser authentication cookies.
- A session table.
- Refresh-token storage.
- Profile persistence.
- A separate OAuth callback implementation.
- A separate authentication-bootstrap write.

The first authenticated summary request acts as identity bootstrap. There is no need for an additional `/login` or `/session` endpoint.

### Frontend flow

1. Load public frontend configuration.
2. Run `liff.init()` using the configured Developing LIFF ID.
3. Complete the appropriate LIFF login/consent flow.
4. Obtain the raw token using `liff.getIDToken()`.
5. Request the summary with:

```http
Authorization: Bearer <raw-ID-token>
```

6. After successful bootstrap, load recent transactions and savings.

Do not request financial data before a token is available. SDK initialization or `liff.isLoggedIn()` alone does not authorize backend access.

The intended scope is `openid` only. This slice does not use `liff.getProfile()`, display-name personalization, profile picture, friendship status, email, or storage of profile claims. `profile` is therefore unnecessary privilege/consent surface in this slice.

### Backend verification

Use the existing `httpx` dependency to call:

```text
POST https://api.line.me/oauth2/v2.1/verify
Content-Type: application/x-www-form-urlencoded
```

Send form fields:

- `id_token`: the received raw token.
- `client_id`: the server-configured Developing internal channel ID.

Require successful verification and a valid response establishing:

- Issuer exactly `https://access.line.me`.
- Audience is a string and exactly equals the configured Developing internal Channel ID.
- Unexpired `exp`.
- A valid, nonempty LINE subject identifier.

Derive the owner exclusively from verified `sub`. Do not strip, case-fold, or otherwise transform a verified identity into another identity.

The implementation must validate response structure and field types rather than treating any HTTP 200 as sufficient.

### Rejected identity sources

Never authorize using:

- Request-body or query-string `line_user_id`.
- `liff.getProfile().userId`.
- Browser-decoded token claims.
- Display name or email.
- A transaction ID alone.
- A client-supplied expected audience.
- The Messaging API channel access token.

A wrong-channel token is rejected even if it belongs to the same Provider.

### Failure behavior

- Missing, malformed, invalid, expired, or wrong-audience credential: `401`.
- Verification timeout, LINE service failure, or invalid upstream response: fail closed with `503`.
- No financial database query before authentication succeeds.
- No stale verification result used during a LINE outage.

Before any LINE verification request, reject an empty or malformed token locally without calling LINE.

Use a pooled `httpx.AsyncClient`, initialized and closed with the FastAPI application lifespan. Use a bounded five-second verification timeout and no automatic verification retry in the initial slice. User-initiated retry remains available. No new runtime dependency is introduced.

No server-side token-verification cache initially. This keeps expiration behavior explicit; its latency/call-volume cost must be measured during Developing testing.

No custom OAuth nonce is invented for the SDK-managed flow. If a custom authorization flow is introduced later, its state, nonce, PKCE, and callback requirements need a separate design.

Reference: [Using LIFF identity securely](https://developers.line.biz/en/docs/liff/using-user-profile/).

## 4. Frontend Hosting

### Chosen topology

Serve the frontend and APIs from the existing Render/FastAPI origin.

```text
Existing HTTPS origin
├── /                       Existing health endpoint
├── /ready                  Existing readiness endpoint
├── /webhook                Existing webhook
├── /export/{token}         Existing CSV download
├── /app/                   Public application shell
├── /app/config.json        Public frontend configuration
├── /app/assets/...         Static JS/CSS
└── /api/me/...              Authenticated read-only APIs
```

### Frontend implementation approach

Use a small static HTML/CSS/JavaScript application:

- No frontend framework or Node build pipeline required.
- Same-origin `fetch()` calls.
- Official LIFF SDK, pinned to an explicitly reviewed fixed version.
- No third-party charting, analytics, advertising, or profile service.
- One application document with summary, transactions, savings, and help sections.

Suggested source layout:

```text
webapp/
  index.html
  assets/
    app.js
    styles.css
```

Serve the HTML through an explicit FastAPI route and mount only the assets directory for static files. Resolve files relative to the application source, not the shell's working directory.

Do not mount the repository root or add a catch-all route that could shadow existing endpoints.

### CORS

No CORS middleware is necessary for the chosen same-origin frontend/API topology.

Do not enable wildcard CORS. CORS is not the ownership or authentication mechanism.

### Configuration and enablement

Proposed deployment configuration names:

- `WEBAPP_ENABLED` — disabled by default.
- `MINIAPP_DEVELOPING_LIFF_ID`.
- `MINIAPP_DEVELOPING_CHANNEL_ID`.

These are proposed names, not values read from or written to the environment.

`/app/config.json` exposes only:

- Developing LIFF ID.
- Environment label `Developing`.

It must not expose secrets, tokens, database settings, or arbitrary environment variables.

When disabled or incompletely configured, the new web surface returns a controlled unavailable response. The existing webhook and health/readiness behavior must remain operational.

No new runtime package is required for this topology.

## 5. Endpoint URL Contract

### Recommended Developing endpoint

```text
<existing Render HTTPS origin>/app/
```

The exact path is **`/app/`**, including its trailing slash.

The real origin must be confirmed from the deployed service; this plan does not invent a hostname.

Requirements:

- HTTPS.
- No token, user ID, or fragment in the configured endpoint.
- LIFF initializes at `/app/`.
- Any future LIFF page paths must remain at or below that endpoint.
- Do not redirect upward to `/` after initialization.
- Preserve LIFF SDK query parameters until `liff.init()` resolves.

For this slice, use the same document rather than introducing nested application routes. Optional navigation intent may use:

```text
/app/?view=summary
/app/?view=transactions
/app/?view=savings
/app/?view=help
```

`view` selects a display section only. It has no ownership or authorization meaning. Unknown values select the default summary section.

Public entry uses the **Developing MINI App URL supplied by the console**. Use its exact value rather than constructing a URL from guessed identifiers.

Review/Published endpoint mapping is outside this slice.

## 6. API Surface

### Three authenticated data endpoints

All financial routes use the same mandatory authentication dependency.

| Endpoint | Contract |
|---|---|
| `GET /api/me/summary` | Current Bangkok month only |
| `GET /api/me/transactions/recent` | Latest five by existing recent-list ordering |
| `GET /api/me/savings` | Current savings goal or `null` |

No ownership parameters, date-range filters, pagination parameters, or configurable limits are accepted.

Reject unexpected query parameters with `400`; authenticate before business-data access. An arbitrary identity header must never influence the verified owner.

No new POST/PATCH/PUT/DELETE application routes.

### Summary response fields

- `currency`: `"THB"`.
- `timezone`: `"Asia/Bangkok"`.
- `year_ce`: integer.
- `year_be`: integer.
- `month`: integer.
- `income_baht`: exact two-decimal string.
- `expense_baht`: exact two-decimal string.
- `balance_baht`: exact signed two-decimal string.
- `transaction_count`: integer.
- `as_of`: server timestamp.

The balance is **income minus expense for the selected month**, not a verified bank balance. The UI should label it as monthly net, such as `สุทธิเดือนนี้`.

### Recent-transactions response

- `limit`: `5`.
- `items`: array.
- `as_of`: server timestamp.

Each item includes:

- `transaction_type`: `income` or `expense`.
- `amount_baht`: positive two-decimal string.
- `category`.
- `description`: string or `null`.
- `occurred_on`: ISO date.
- `created_at`: timestamp with Bangkok offset.

Do not expose LINE user IDs or transaction database IDs; neither is needed for this display-only slice.

The section is labeled "five recent transactions," not complete history or necessarily the five most recently entered records.

### Savings response

- `goal`: object or `null`.
- `as_of`: server timestamp.

When present, the goal contains:

- `title`.
- `target_baht`.
- `saved_baht`.
- `remaining_baht`.
- `progress_percent`.
- `deadline`: ISO date or `null`.

All money fields are exact two-decimal strings.

`progress_percent` is a backend-calculated two-decimal string, rounded with `Decimal` using `ROUND_HALF_UP`, clamped to 0–100 for the progress indicator. Actual saved amounts are not clamped when savings exceed the target.

### Empty results

Return HTTP 200:

- Summary: `"0.00"` totals and count `0`.
- Recent transactions: `items: []`.
- Savings: `goal: null`.

Authentication and database failures must not masquerade as empty data.

### Consistency boundary

Each response reflects its own database read. The three endpoints do not promise a single shared snapshot across simultaneous chat mutations.

Refresh reloads all panels. Display freshness information and discard superseded requests.

## 7. Repository / Read Model Reuse

Reuse existing functions with a supplied request-scoped session:

| Web data | Existing query |
|---|---|
| Current-month summary | `monthly_summary(line_user_id, year, month, session=...)` |
| Five recent transactions | `list_recent_transactions(line_user_id, limit=5, session=...)` |
| Savings | `get_savings_goal(line_user_id, session=...)` |

Recent ordering remains:

```text
occurred_on DESC, created_at DESC, id DESC
```

Do not change it to latest-entry ordering as a side effect of the web feature.

### Request session

Use a fresh session with autoflush disabled. Close/roll it back after the read rather than using a helper whose purpose is committing mutations.

Always supply this session to reused repository queries.

No call to:

- `handle_text_message()`.
- The transaction parser.
- `get_pending_action()`.
- `get_pending_transaction()`.
- `get_user_data_summary()`.
- Export-token or webhook-event operations.

No new table, migration, or repository query is needed for the proposed scope.

Existing application startup initialization is distinct from request handling. Automated no-write assertions begin after test database/schema setup.

## 8. Read-only State Invariants

Every successful or failed web request must leave these unchanged:

- `Transaction`.
- `SavingsGoal`.
- `PendingTransaction`.
- `PendingAction`.
- `ExportToken`.
- `UserFeedback`.
- `ProcessedWebhookEvent`.

Specifically:

- No draft creation or completion.
- No pending-state reads through cleanup helpers.
- No expiry cleanup, including already-expired rows.
- No TTL refresh.
- No action-version increment.
- No action invalidation or snapshot changes.
- No export-token issuance.
- No feedback insertion.
- No onboarding flag or profile record.
- No webhook claim/cache record.
- No last-seen, login-history, or web-session record.

Opening the web app while `confirm_delete_all` is active must preserve that action exactly.

Repeated GET requests are ordinary authenticated reads. They neither need nor use webhook idempotency.

Future web writes require a separate architecture review.

## 9. Money / Date Serialization

### Money

- Keep authoritative calculations in integer satang/`Decimal`.
- Serialize money explicitly as two-decimal strings.
- Do not rely on default Decimal-to-JSON conversion.
- Never calculate financial totals using JavaScript floating point.
- Frontend formatting may group digits in the string without converting to a floating-point amount.

Examples:

```text
"0.00"
"50.00"
"120.50"
"-79.50"
```

Transaction amounts remain positive; direction determines their presentation.

Savings progress is independent of the monthly transaction balance. Do not subtract manually recorded savings from income/expense totals.

### Month boundaries

Capture the server clock at the start of the summary operation and convert it to `Asia/Bangkok`.

Use that year/month for `monthly_summary()`. Do not use browser timezone or client-selected dates.

### Date meanings

- `occurred_on`: transaction's effective date, serialized as `YYYY-MM-DD`.
- `created_at`: recording timestamp, presented with an explicit `+07:00` offset.
- `as_of`: response's server-clock timestamp, serialized as timezone-aware ISO 8601.

Display B.E. years in Thai UI. Treat date-only strings as calendar dates, not as JavaScript UTC instants that might shift days.

For SQLite-returned naive timestamps, apply the repository's UTC-storage convention before Bangkok conversion.

### Listing bounds

Exactly five recent transactions. No pagination or "load more" in this foundation.

Full history will require a later bounded pagination contract.

## 10. Privacy / Caching / Logging

### Authentication and data handling

- Tokens exist only in the current request and application-managed frontend memory.
- Do not add localStorage/sessionStorage/IndexedDB copies of tokens or financial responses.
- Do not claim to control storage used internally by LINE/LIFF.
- Do not persist name, picture, email, or decoded token claims.
- Do not include personal financial data in HTML metadata or share previews.
- Render descriptions and titles using text-safe DOM operations, never untrusted HTML.

### Cache controls

For the initial Developing release:

- `/app/`, frontend configuration including `/app/config.json`, and locally served assets: `Cache-Control: no-store`.
- `/app/config.json` also uses `X-Content-Type-Options: nosniff`.
- All `/api/me/*` responses, including errors, and all disabled/misconfigured Web App responses: `Cache-Control: no-store`.
- Use `Vary: Authorization` as an additional separation signal, not as a substitute for `no-store`.
- No service worker or offline cache.
- Clear sensitive panels and cancel in-flight reads when leaving the page; revalidate on return.
- Prevent late responses from an earlier load from repopulating cleared or reauthenticated UI.

The external, version-pinned SDK has its own delivery/cache policy.

### Headers and scripts

- Use `X-Content-Type-Options: nosniff`.
- Use a restrictive referrer policy.
- Define a Content Security Policy limited to this origin and the LINE origins actually needed by the pinned SDK.
- Do not introduce broad wildcard script permissions or inline execution merely to bypass SDK setup errors.
- Validate CSP against actual Developing initialization before release.

### Logging

Never log:

- Authorization headers.
- Raw ID/access tokens.
- Verification request bodies.
- Full upstream verification responses.
- Financial response bodies.
- LIFF initialization URLs containing credential parameters.

Use endpoint name, status, timing, and non-identifying error codes. Avoid exception formatting that embeds request bodies or credentials.

### Privacy/help surface

Include static information explaining:

- This page only displays existing MooTun data.
- Financial operations remain in chat.
- LINE identity is used to retrieve the user's own records.
- Delete All remains available through chat.
- Existing operational-record retention remains unchanged.
- No fixed data-retention period is newly promised.

This help text is not automatically a substitute for the MINI App's required privacy-policy presentation; its exact public policy URL remains an operational question.

## 11. User Isolation

Every endpoint derives one immutable request owner from verified `sub`.

The request owner is passed to every query through exact equality scoping.

There is no:

- `/users/{line_user_id}` data route.
- Owner-switching parameter.
- Cross-user aggregate.
- Teacher dashboard.
- Identity matching by display name.
- Automatic migration or merging when identities do not match.

If same-Provider continuity unexpectedly fails during testing, stop and diagnose the channel/environment configuration. Do not add an ad hoc linking workaround.

Two-user continuity must establish that each tester sees the records created through their own Messaging API identity.

## 12. Error / Consent / Re-entry UX

### Initial states

1. **Loading application:** shell/configuration/SDK.
2. **Connecting to LINE:** `กำลังเชื่อมต่อ LINE…`
3. **Loading financial data:** `กำลังโหลดข้อมูล…`
4. **Loaded:** independent summary, recent, and savings panels.

No previous user's data is displayed during initialization.

### Login and consent

- LIFF browser: let `liff.init()` perform its supported authentication behavior.
- If `liff.getIDToken()` returns `null`, do not call financial APIs; display an explicit LINE login action and a recoverable state, and proceed only after authentication succeeds.
- External browser/general LINE in-app browser: when not logged in, offer an explicit LINE login action using `liff.login()`.
- Use a fixed same-origin return destination under `/app/`, not an arbitrary `next` URL.
- Consent cancellation produces a recoverable explanation; no financial API data is exposed.
- Add friend remains Off. The web app must not silently prompt friendship changes.

No MooTun password or basic-auth gate is introduced.

### API errors

Use stable JSON error codes and sanitized Thai messages.

| Condition | Status/code | UX |
|---|---|---|
| Missing/invalid/expired/wrong-channel token | `401 UNAUTHORIZED` | Clear financial data; explain that LINE authentication is required again |
| Verification unavailable | `503 AUTH_UNAVAILABLE` | Retry action; no stale-auth fallback |
| Database unavailable | `503 DATA_UNAVAILABLE` | Panel error, not zero totals |
| Web feature disabled/misconfigured | `503 WEBAPP_UNAVAILABLE` | Web temporarily unavailable; chat remains available |
| Unsupported query parameters | `400 INVALID_REQUEST` | Generic invalid-request message |

Do not implement automatic login/reload loops. A failed reauthentication attempt remains a visible recoverable error.

### Empty and partial states

- Summary: zero values with "ยังไม่มีรายการในเดือนนี้".
- Transactions: "ยังไม่มีรายการ".
- Savings: "ยังไม่มีเป้าหมายการออม", with static chat-command guidance.
- A non-authentication error in one panel does not turn another panel into an error or erase valid current results.
- Any authentication failure clears all financial panels.

### Re-entry

On `pageshow`/return to visibility, recheck token availability and refetch rather than trusting an old rendered page.

Use an abort controller/load generation so results from an earlier refresh cannot overwrite a newer one.

Closing LINE/MINI App must not be described as guaranteed logout; platform behavior varies.

### Accessibility

- Thai labels, readable text, semantic headings, and keyboard-accessible controls.
- Adequate touch targets and contrast.
- Direction expressed through words/signs, not color alone.
- Savings progress includes textual amounts and percentage.
- Usable at increased text size and in landscape.
- No charts, animation-dependent meaning, or inaccessible custom controls.

## 13. Developing Environment Setup

These are future authorized operational steps, not actions taken during this task.

1. Confirm the exact Developing LIFF ID and **Developing internal channel ID** from console metadata.
2. Confirm the actual existing Render HTTPS origin.
3. Confirm two testers have accepted their channel testing permissions.
4. Implement and verify the web slice locally with mocked LINE verification and temporary databases.
5. Deploy the code only after separate deployment authorization.
6. Supply Developing configuration and enable the web surface.
7. Configure the Developing endpoint to:

   ```text
   <confirmed HTTPS origin>/app/
   ```

8. Retain Full size, `openid` only, Add friend Off, and Scan QR Off.
9. Open the console-provided Developing MINI App URL with authorized testers.
10. Verify backend rejection of other audiences.

### Unverified-channel propagation

Before saving settings, inspect the console's reflection behavior for this unverified MINI App.

- Do not intentionally configure Review/Published endpoints.
- Do not request verification or publication.
- Record any automatic settings propagation.
- Never broaden the backend audience allowlist to make another environment work.
- If the owner's strict requirement to leave other environments untouched cannot be guaranteed by the console, obtain clarification before making that setting change.

The backend remains Developing-only even if some settings are copied by LINE.

### Deployment versus platform environment

Using the existing Render service means Developing testers may be reading the production database.

That is intentional only if approved: read-only application access does not create an isolated database environment.

Use synthetic pilot data for verification. A separate staging service/database is a future option, not a prerequisite invented by this plan.

No MINI App channel access token or service-message credentials are required for this read-only ID-token verification flow.

## 14. Automated Test Matrix

Use temporary SQLite databases and mocked outbound LINE verification. No real tokens, production database, or live LINE calls in automated tests.

| Area | Required coverage |
|---|---|
| Authentication input | Missing bearer header, malformed header, empty token |
| Verification call | Exact LINE endpoint; raw token in form body; server-configured Developing `client_id` |
| Claims | Invalid response shape; wrong issuer/audience; expired token; missing/malformed subject; correct verified subject |
| Environment separation | Messaging API, Review, Published, and unrelated-channel audiences rejected |
| Upstream failure | Timeout, network exception, 429/5xx, malformed response fail closed |
| Query ordering | Financial repository queries never run before successful verification |
| Identity spoofing | Browser UID/query/body/header cannot override verified owner |
| Two users | A sees only A; B sees only B across all three endpoints |
| Summary | Positive/negative net, empty month, exact decimal serialization, Bangkok month/year rollover |
| Recent list | Fixed five-item bound; documented ordering and ties; backdated/future-dated entries; no IDs/UID exposure |
| Savings | No goal, partial progress, completed/overfunded goal, exact remaining amount and percentage |
| State invariants | All existing tables unchanged after repeated successful and failed requests |
| Expired states | Expired draft and action rows remain byte-identical |
| Forbidden helpers | Pending cleanup, mutation, export, and webhook helpers fail the test if called |
| SQL behavior | Request-time SQL contains no INSERT/UPDATE/DELETE or schema modification after fixture setup |
| No hidden bootstrap writes | New/empty user does not acquire account/session/profile/last-seen rows |
| Webhook separation | API calls create no `ProcessedWebhookEvent`; existing webhook deduplication remains unchanged |
| Serving | `/app/`, config and assets work; traversal rejected; no repository files served; existing routes unaffected |
| Configuration | Disabled/missing Developing config exposes no data and does not break the bot |
| Cache/logging | `no-store` on success/errors; no token, claims, financial text, or credential URL in logs |
| Frontend safety | User strings inserted as text; no persistent financial/token storage |
| Frontend state | Consent denial, null token, 401 clear-all, partial 503, refresh races, navigation/re-entry |
| Methods/surface | Financial routes accept GET only; no web mutations or CSV issuance added |
| Regression | Existing parser, webhook, export, feedback, onboarding, Delete All and pending-state tests pass |

Backend coverage can use the existing pytest/FastAPI testing stack.

Client lifecycle tests require browser automation. A Playwright-based development test harness is recommended; its installation and development-dependency changes require separate implementation approval. Runtime deployment does not need browser tooling.

Expected later backend checks:

```text
pytest -q test_webapp.py
pytest -q
git diff --check
```

Browser automation must supplement, not replace, real LINE Developing smoke testing.

## 15. Production/Developing Smoke Plan

**All future cases begin as NOT PERFORMED.**

"Production" below refers to the deployed Render backend; the MINI App entry remains **Developing**, not Published.

### One authorized tester

1. Open the Developing URL in LINE and confirm the correct environment.
2. Verify first-use consent behavior and that no MooTun password is requested.
3. Confirm summary, recent transactions, and savings match known synthetic chat data.
4. Check empty-user/empty-month/no-goal states using suitable test data.
5. Reopen and refresh after a chat-created transaction; updated data appears.
6. Start an incomplete chat draft, open/refresh the web app, and complete the draft afterward.
7. Repeat while each active action exists:
   - `confirm_delete`
   - `undo_delete`
   - `confirm_delete_all`
8. Confirm action identity, expiry, versions, and snapshots remain unchanged using controlled inspection where available.
9. Exercise consent cancellation, interrupted connectivity, and re-entry.
10. Test iOS and Android LINE where devices are available.
11. Test external-browser login/fallback and large text/landscape.
12. Confirm `/`, `/ready`, webhook responses, and existing CSV behavior still operate.

### Two authorized testers — release gate

1. A and B each create distinguishable synthetic records through chat.
2. Each opens the Developing MINI App.
3. A sees only A’s summary/list/goal; B sees only B’s.
4. Verify same-Provider identity continuity through protected test inspection.
5. Record the outcome without exposing raw IDs or tokens in the report.
6. Verify a non-enrolled account cannot gain Developing access through the supported flow.

If a second tester is unavailable, record that limitation and keep the two-user live identity gate incomplete. Automated isolation tests do not prove actual console/channel mapping.

### Automated/controlled cases

Do not claim ordinary manual smoke establishes:

- Forged or expired token handling.
- Wrong-audience rejection across every environment.
- Database write absence.
- Verification-service timeouts.
- Late-response race handling.
- Precise midnight boundary behavior.

Those require automated tests or an explicitly authorized controlled environment.

No Review/Published launch or production publication is necessary for this verification.

## 16. Files Expected to Change

**Later implementation only:**

| File/path | Purpose |
|---|---|
| `app.py` | Mount frontend/read-only router and manage verification-client lifecycle |
| `webapp_auth.py` | Developing configuration, ID-token verification, verified principal |
| `webapp_routes.py` | Static/config routes, authenticated reads, explicit serialization |
| `webapp/index.html` | Public application shell |
| `webapp/assets/app.js` | LIFF initialization, requests, safe rendering, lifecycle handling |
| `webapp/assets/styles.css` | Mobile and accessible presentation |
| `test_webapp.py` | API/auth/state-preservation/serving coverage |
| Browser test files | Frontend lifecycle and rendering coverage |

No model or schema migration is expected.

Existing repository query implementations should be reused without modification.

No required changes to:

- Transaction parser.
- Chat message formatters or LINE reply transport.
- `database.py` migrations.
- Runtime requirements.
- Phase 1J plan.

Any development browser-test dependency/configuration must be separately approved. Future Render configuration is an operational step, not part of this planning task.

`PRODUCT_SPEC.md` and `PILOT_TEST.md` synchronization requires later authorization.

## 17. Risks / Open Questions

### Architecture risks and selected mitigations

- **Wrong internal audience:** accept only the configured Developing channel.
- **Same-Provider mismatch in practice:** require two-user identity-continuity smoke; no automatic linking workaround.
- **Lazy cleanup during reads:** whitelist the three existing read queries and prohibit pending-state helpers.
- **Money precision:** explicit decimal strings and backend calculations.
- **Stale browser data:** no-store responses, clear/revalidate lifecycle, late-response protection.
- **Verification latency:** pooled client, bounded timeout, no initial caching; measure on devices.
- **Shared production service:** feature-disabled fallback and regression verification for the webhook.
- **Settings propagation:** review unverified MINI App behavior before future console edits.
- **Three-response freshness:** disclose independent reads; do not claim a cross-panel atomic snapshot.

### Outstanding operational inputs

1. Exact Developing LIFF ID and internal channel ID.
2. Confirmed Render HTTPS origin.
3. Two accepted tester accounts and available iOS/Android devices.
4. Approval to read the existing production database through the Developing slice.
5. Current Render latency/cold-start characteristics.
6. Existing MINI App privacy-policy URL and approved help/privacy wording.
7. Observed console propagation behavior before setting the endpoint.
8. Fixed LIFF SDK version to pin and its tested CSP requirements.
9. Approval for browser automation tooling.

These inputs do not change the selected architecture. They must be supplied or verified before implementation/release as appropriate.

## 18. Non-goals

Exclude:

- Transaction creation, editing, deletion.
- Delete All from the web.
- Undo, confirm, and cancel actions.
- Category management and budgets.
- Analytics charts or new aggregate queries.
- CSV issuance/download UI.
- Persistent settings.
- Profile persistence or account management.
- Problem reporting and Phase 1J implementation.
- Flex messages.
- Rich Menu implementation or publication.
- Review/Published environment rollout.
- MINI App verification submission.
- Service messages, push notifications, or external integrations.
- New background jobs, token caches, sessions, or database tables.
- Historical-plan or engineering cleanup.

### Future Rich Menu mapping — documentation only

| Label | Future intended destination |
|---|---|
| สรุป | MINI App summary |
| รายการ | MINI App recent transactions; full history later |
| วิเคราะห์ | Deferred until analytics exists |
| หมวด/งบ | Deferred until product scope exists |
| ตั้งค่า | Deferred |
| เพิ่มเติม | Future second menu or other future surface |

No menu is published as part of this foundation.

Phase 1J remains paused and its separate draft remains untouched.

## 19. Acceptance Criteria

The foundation is acceptable when:

1. The Developing MINI App opens the same-origin `/app/` frontend.
2. The frontend uses the correct Developing LIFF ID.
3. Every financial request verifies its ID token against the configured Developing internal channel.
4. Owner identity comes only from verified `sub`.
5. Wrong-channel, expired, invalid, or missing credentials expose no financial data.
6. A and B can each see only their own existing chat-owned data.
7. The UI displays current Bangkok-month summary, five recent transactions, and current savings accurately.
8. Money is serialized as exact decimal strings; dates retain their defined meaning.
9. API requests perform no database mutations, including expired-state cleanup or webhook-event creation.
10. No browser account/session/profile record is created.
11. Loading, consent, errors, empty states, and re-entry are handled without stale-user data exposure.
12. Same-origin serving, cache controls, logging redaction, and safe text rendering are verified.
13. Existing bot routes and business behavior pass regression testing.
14. Two-user Developing identity-continuity smoke is completed and documented honestly.
15. Review/Published audiences remain unauthorized by the backend.
16. No excluded feature, Rich Menu publication, or Phase 1J implementation is introduced.
