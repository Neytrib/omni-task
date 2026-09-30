# Implementation stages and verification record

The latest user request authorizes S9: submission and single-Ubuntu-VPS deployment preparation, protected configuration, HTTPS routing, backup/restore procedures, and isolated verification. No new product features, purchases, public deployment, or independent paid calls are authorized. Stop after S9 preparation. Read `AGENTS.md`, `SPEC.md`, this file, and applicable nested instructions before every stage. Implement the requested scope only, record evidence, then stop.

Acceptance checkboxes below are planned work unless explicitly checked with evidence. Suggested commands for future stages are not claims that those files, dependencies, or services exist today. Each implementation stage must record its final exact runnable commands and manual steps.

## Stage index

| Stage | Deliverable | Prerequisite | Status | Coverage |
| --- | --- | --- | --- | --- |
| S0 | Workspace/assessment review and planning documents | None | Complete | A1-A7, U1-U10 planning |
| F1 | Infrastructure, data/API, and backend authentication requested together | S0 | Complete; stopped | Current user foundation request |
| S1 | Reproducible service foundation and test harness | S0 | Complete through F1 | A6, A7, U7, U8, U10 |
| S2 | Persistence, ownership, task API, durable intent schema | S1 | Complete through F1 | A2, A7, U1, U4, U8, U9 |
| S3 | Private Telegram text/task management workflow | S2 | Complete; stopped | A1, A7, U1, U2, U4 |
| S4 | `/profile` and private browser sessions | S3 | Backend in F1; bot login entry point in S3 | U1, U2, U9 |
| S5 | Voice transcription, Celery dispatch, queued notifications | S4 | Complete; user reported live voice success | A1, A3, A7, U3, U4, U7-U9 |
| S6 | Responsive, accessible private dashboard | S5 | Implemented; S7 verified desktop live flows; broad visual audit remains | A4, U5, U6 |
| S7 | Live event delivery and complete reconnect recovery | S6 | Complete; stopped | A5, U6, U8, U9 |
| S8 | Integrated failure verification and local handoff | S7 | Complete; limitations recorded | A1-A7, U1-U10 verification |
| S9 | Submission and single-VPS deployment preparation | S8 | Complete; public/manual limitations recorded | User's deployment/submission request |

## F1 - Combined application foundation

Authorized scope: six services, persistent PostgreSQL/Redis, controlled migration, environment/setup instructions, independent Celery probe, Telegram-linked users, complete-content owner-scoped task APIs, source deduplication, separate processing records, and server-side login/session/CSRF authentication. A minimal authentication shell verifies login/logout; it is not the dashboard. The user also required two-user security tests and proof that database/broker ports are not public.

- [x] Implement task data and APIs with ownership, validation, versions, paginated reads, and preserved content.
- [x] Implement durable source identities and processing-request state independent of task status.
- [x] Authenticate the bot before Telegram identity resolution; reject browser access to bot-only routes.
- [x] Implement hashed, expiring, single-use login links and opaque revocable sessions with cookie/Origin/CSRF protections.
- [x] Run real PostgreSQL tests covering requested two-user, forgery, status, duplicate, expiry/reuse, logout, and unauthenticated cases.
- [x] Complete live Compose/worker/persistence checks, authentication-shell checks, and Arc inspection.
- [x] Record final commands/outcomes and limitations, update original stage coverage, and stop after F1.

Completed implementation evidence (2026-09-30):

- Initial workspace inspection found only the four S0 planning files; no working application was replaced. Docker Engine was installed but stopped; Docker Desktop was started locally for actual PostgreSQL/Redis verification.
- `.venv/bin/python scripts/test_backend.py` passed: **102 tests**, using a fresh disposable PostgreSQL database, the reviewed Alembic migration, real constraints/concurrency, and cleanup of only the temporary database. Coverage includes source/login races, task isolation, forged fields, status validation, exact long Unicode content, transaction rollback, migration drift, session expiry/revocation, CSRF, and credential-safe errors.
- Final `uv run ruff check .` and `uv run ruff format --check backend bot tests scripts` passed, including all 27 Python files. `docker compose config --quiet` passed.
- A separate security review found three issues before completion: partially null source identities, non-ASCII CSRF header comparison, and mismatched quoted versions. They were fixed and regression-tested. The follow-up review found no outstanding actionable issue.
- One test-tool dependency warning is visible: Starlette deprecates its current httpx-based TestClient integration. It did not fail tests and has not been suppressed. Application correctness is not inferred from that warning.
- `docker compose up --build -d --wait` passed with six healthy services and migration exit 0. `docker compose run --rm migrate alembic check` reported no new upgrade operations. PostgreSQL 17.11 and Redis 7.4.11 were observed in the actual containers; lockfiles and image digests record resolved dependencies.
- `docker compose exec -T api python scripts/queue_smoke.py` passed: a UUID probe traveled over Redis and returned from a different worker container/process. No eager/local Celery execution substituted for the worker.
- `python3 scripts/check_infrastructure.py --runtime` passed: PostgreSQL/Redis have no host port bindings; their data network is internal; only frontend `127.0.0.1:8080` is published; bot has no data network, credentials, or SQLAlchemy/psycopg/Redis/Celery clients.
- `docker compose exec -T api python scripts/api_smoke.py` passed against live HTTP, using two synthetic users. Its first run exposed a smoke-script expectation error (409 instead of the documented 410 after source deletion); the expectation was corrected and rerun. The first run's single leftover synthetic task was removed via the authenticated API.
- `python3 scripts/persistence_smoke.py` passed: PostgreSQL/Redis containers restarted without removing volumes; original task fields/content, source deduplication, and a Redis marker survived; API/worker/bot recovered. The synthetic task and marker were removed afterward, with only minimal user/source receipt metadata retained.
- `npm --prefix frontend test` passed **6 component tests** for link handling, one exchange, fragment removal, session status, and CSRF logout/failure behavior. `npm --prefix frontend run build` passed TypeScript and Vite production build. These use jsdom, with no downloaded browser binaries.
- Native Arc verification observed successful synthetic login, a clean `/login` URL without the fragment, signed-in state after reload, logout followed by an unauthenticated reload, and visible rejection when reopening the consumed link. Arc accessibility snapshots lagged after logout and coordinate input returned `noWindowsAvailable`; the live page state and reload confirmed the result. No alternate browser, profile reset, or extension change was used.
- A temporary mode-0600 local redirect fixture avoided printing the synthetic login token during Arc navigation. It was removed afterward. The actual token was absent from API/bot/frontend logs; generated database/service credentials were absent from nonignored source files, `.env` remained mode 0600, and no assessment PDF was copied into the project.

Exact manual verification from `/Users/neytrib/Desktop/omni_task`:

1. Run `python3 scripts/configure_env.py` (preserves an existing `.env`), then `docker compose up --build -d --wait` and `docker compose ps`. Expect six healthy services and only frontend port `127.0.0.1:8080` published.
2. Run `python3 scripts/open_dev_login.py` on this Mac. Arc should show Signed in at `/login` with no token in the URL. Reload to confirm the session, then activate Log out and reload to confirm the private-link prompt. The helper is development/loopback-only and creates a synthetic user, not a real Telegram account.
3. Run `docker compose exec -T api python scripts/api_smoke.py` and `docker compose exec -T api python scripts/queue_smoke.py`. Both must report `"status": "passed"`; the queue result identifies a separate worker. The API walkthrough removes its synthetic tasks but retains receipt/user metadata.
4. Run `python3 scripts/check_infrastructure.py --runtime`, then `python3 scripts/test_backend.py`. Expect boundary checks and 102 passing backend tests; the database test helper creates/drops only its own disposable database.
5. Optionally run `python3 scripts/persistence_smoke.py` to repeat the explicit synthetic restart check; it restarts local PostgreSQL/Redis and preserves volumes. Stop the stack with `docker compose down` when desired, without `-v`.

Full Telegram commands (including `/profile`), task-board UI, transcription/notification execution, event publication, and WebSockets remain unimplemented by design. Processing requests are records only; their `queued` state does not mean a transcription worker is running. Local smoke checks leave synthetic user/receipt/session metadata but no retained synthetic task content. No paid API calls, real Telegram messages, public deployment, volume resets, or Git initialization were performed. F1 is complete and work stops here; services remain running locally for review.

## S0 - Planning only (historical evidence)

Deliverables: `SPEC.md`, `TASKS.md`, `AGENTS.md`, and minimal `.gitignore` protection. No application code or dependency installation.

- [x] Inspect regular/hidden workspace entries and applicable instruction locations before planning.
- [x] Establish whether code, dependencies, tests, and Git history exist; document preservation decisions.
- [x] Read and visually inspect the assessment; distinguish A-series assessment requirements from U-series user choices.
- [x] Plan product flows, service boundaries, ownership/authentication, voice failure handling, idempotency, notification delivery, and reconnect recovery.
- [x] Define stages with observable checks, Arc-only browser verification, and explicit paid-call/deployment boundaries.
- [x] Review all planning documents for consistency and validate local links, stage coverage, and confidentiality exclusions.
- [x] Record checks actually run and stop before S1.

Completed planning evidence (2026-09-30):

- `pwd`, `ls -la`, and `rg --files` inspection found an empty `/Users/neytrib/Desktop/omni_task`; no code or tests were present. Ancestor/project `AGENTS.md` checks found no on-disk instructions; the user's supplied browser rules are preserved in the new file.
- `git rev-parse --show-toplevel` returned exit 128 because this directory is not a Git repository; no Git history/status can be reported. No repository was initialized.
- Bundled Python `pypdf.PdfReader` extracted the single-page PDF. `pdftoppm -scale-to 1600 -png -singlefile` rendered it outside the repository, and the complete page was visually reviewed.
- Official documentation was checked for OpenAI transcription, Telegram, Redis/Celery, FastAPI/WebSockets, SQLAlchemy/Alembic, Vite, and Compose. References and unresolved version-specific checks are recorded in `SPEC.md`.
- Independent planning reviews covered requirement traceability, reliability/security edge cases, and official stack constraints. No paid calls, browser launches, application builds, or runtime tests were performed.
- Bundled Python inline document validation passed (exit 0): exactly the four expected files, UTF-8 readability, final newlines, no trailing whitespace/NUL bytes, balanced Markdown fences, valid local Markdown link targets, one A1-A7/U1-U10 requirement row each, one S0-S8 stage row/section each, and no future acceptance boxes checked.
- A temporary synthetic fixture check passed (exit 0): `rg --files --hidden --no-ignore-parent --ignore-file /Users/neytrib/Desktop/omni_task/.gitignore .` excluded 18 secret/private/generated paths and preserved 8 source/example/lockfile paths. This verifies ripgrep's ignore-file behavior; Git staging checks remain unavailable until a repository exists. No actual secret or assessment content was used in fixtures.
- Final independent reviews found and resolved four planning details: duplicate voice receipts, best-effort notification delivery, the intended HttpOnly cookie exception, and cookie sharing between Arc tabs. The assessment render was removed after review; the original PDF was untouched.
- Remaining limitation: this is a documented design, not a running system. No application tests, builds, browser journeys, provider calls, or performance measurements exist yet. S1-S8 remain unstarted and require an explicit stage request. S0 is complete; stop here.

Manual review for this stage:

1. Open `SPEC.md` and compare its A1-A7 table with the local assessment; compare U1-U10 and product flows with the user's request.
2. Review the service diagram and confirm the bot has no database/broker access, the API owns WebSockets, and snapshots recover missed changes.
3. Review S1-S8 below and confirm each has a bounded deliverable and observable checks.
4. Read `AGENTS.md` for stage stopping, Arc, confidentiality, truthful testing, and approval rules. Confirm only the four planning/protection files exist in the workspace.

## S1 - Service foundation and test harness

Scope: initialize the new project only when S1 is requested. Create minimal API/bot/worker/frontend entry points, dependency manifests/lockfiles, container definitions, `docker-compose.yml`, placeholder `.env.example`, `.dockerignore`, healthchecks, test harnesses, and local run instructions. Select a supported Python/Node toolchain using official compatibility documentation. Product handlers, task CRUD, authentication, and provider calls remain later stages.

- [x] Lock compatible Python and frontend dependencies; clean installs and frontend typecheck/build succeed without downloaded browser binaries.
- [x] `docker compose config --quiet` passes with placeholder local configuration; six distinct services exist, with only loopback public application ports.
- [x] PostgreSQL/Redis healthchecks, frontend/API readiness, service startup/shutdown, and worker entry-point checks work; migration ordering is explicit once S2 adds migrations.
- [x] Bot configuration/image/import checks show no PostgreSQL/Redis credentials or client dependencies and no data-network membership. It can call the internal API over HTTP.
- [x] Unit/integration test commands are documented and run at least a meaningful configuration/health smoke check. Fake provider/Telegram transports require no credentials and make no paid calls.
- [x] `.gitignore`, `.dockerignore`, and staged-file review exclude secrets, confidential PDFs/extracts, audio, and runtime data. `.env.example` contains placeholders only.

Manual: follow the new README from a clean local environment, validate Compose, build and start services in fake mode, inspect health output, open the placeholder frontend in Arc, then shut down without deleting volumes. Record exact commands and expected health output in this stage's evidence. Stop after S1.

## S2 - Persistence, ownership, and task API

Scope: migrations and shared domain/repository services; internal authenticated task CRUD; user creation, deterministic title utility, versions, source receipts, revision/outbox/job schema. Public browser routes stay disabled or require authentication introduced in S4; never expose a development owner-header shortcut. Define durable intents here; background execution starts in S5 and live delivery in S7.

- [x] Reviewed Alembic migrations upgrade an empty PostgreSQL database to head; `alembic check` reports no unintended schema drift. Test teardown uses disposable test databases, not user volumes.
- [x] Create/list/details/status/delete persist across API restart and PostgreSQL volume-preserving restart. New tasks default Pending; invalid statuses and empty/oversized inputs are rejected.
- [x] Round-trip multiline text, surrounding whitespace, emoji, non-Latin text, markup-like strings, and long content without modification. Title length/derivation is deterministic and provider-free.
- [x] Two-user tests reject cross-user list/detail/status/delete and source-key reuse attempts; no task bodies leak through errors.
- [x] Concurrent duplicate Telegram source submissions and dashboard idempotency keys create exactly one task. Reusing a key with different content conflicts. Two different messages with identical text create two tasks.
- [x] Delete a task, replay the original source, and observe no resurrection. Deleted content is absent from task/job data; minimal deduplication receipts remain.
- [x] Concurrent mutations enforce expected versions; task change, owner revision, and outbox intent commit or roll back together. Snapshot rows and revision are transactionally consistent.

Manual: use the documented authenticated local API fixture/client to create tasks for two test users, change each status, delete one, repeat its original create request, and restart services without deleting data. Verify user isolation and exact persisted content through the API. Record requests and responses with all credentials redacted. Stop after S2.

## S3 - Telegram text and task management

Scope: aiogram private-chat long polling, `/start`, `/help`, `/profile`, text capture, confirmations/status buttons, `/list` pagination, task details/Back, and confirmed deletion. The latest request includes the S4 bot login-link entry point. Voice processing remains S5; help reports configured voice limits and its current unavailable state.

- [x] Fake Telegram transport verifies commands and private-chat filtering, rejects unsupported media clearly, and prevents commands/edited/group messages from becoming tasks.
- [x] One text message produces one API request with stable source metadata and one task confirmation with working status controls. Replayed updates never duplicate tasks.
- [x] Twelve seeded tasks appear across three 5-item pages with correct Next/Previous behavior, stable ordering, and owner-scoped navigation. Back returns to the prior page when possible.
- [x] Details show full short content or a bounded long preview with dashboard access, preserving all stored content (latest user clarification). Callback payload/message lengths meet current API limits; callbacks are acknowledged promptly.
- [x] Status buttons refresh current state; stale/forged/cross-user callbacks fail safely. API errors produce actionable messages instead of success claims.
- [x] Delete -> Cancel leaves the task; Delete -> Confirm removes it. Double confirmation and already-deleted tasks are safe. Plain Delete alone never deletes.
- [x] Bot integration/import/config tests prove it uses HTTP and never connects to PostgreSQL/Redis. Document verified polling/offset and restart behavior.

Manual: with an explicitly configured development bot, send `/start`, `/help`, one multiline text, and `/list`; open the task, traverse all three statuses, use Back, cancel deletion, then confirm deletion. Use seeded fixtures for pagination and a second private test account for isolation. Fake transport checks can run without sending real Telegram messages; report which mode was actually used. Stop after S3.

Completed implementation evidence (2026-09-30):

- Preserved the existing PostgreSQL/API domain layer, authentication, task schema, service isolation, frontend shell, and pinned aiogram 3.31.0 dependency. No schema migration, provider call, dashboard feature, or new queue job was needed. Workspace inspection reconfirmed that this directory has no Git repository; no commits or repository initialization were performed.
- Added `bot/app/api.py`, `config.py`, `polling.py`, `handlers.py`, `interface.py`, `navigation.py`, and `views.py`; extended the existing bot lifecycle. Updated Compose bot-only configuration, `.env.example`, the test image's aiogram dependency group, and runtime isolation checks. `/profile` is now the sole dashboard-link command after the follow-up correction; command menus are private-chat scoped.
- Read the installed/versioned aiogram dispatcher implementation and official Telegram contracts. A controlled sequential poller uses `feed_update` and retains API failures before offset acknowledgement. It does not delete webhooks or discard pending updates. Logs omit raw exception strings and request payloads. Terminal output errors cannot cause an unaccepted API message to be acknowledged.
- Independent review found and fixed whitespace-only command parsing, permanent-output queue starvation, a failed retry acknowledgement masking an API failure, and stale delete-confirm controls after Cancel (including edit fallback). Follow-up review reported no outstanding actionable findings, with regression tests for each.
- `python3 scripts/test_backend.py -q` passed: **151 tests** in the final full run, exit 0. It includes **49 new bot tests**: 33 PostgreSQL-backed API/aiogram-dispatch scenarios and 16 polling/HTTP/configuration cases. The helper creates and removes only its own disposable PostgreSQL database. One existing Starlette/httpx TestClient deprecation warning remains visible.
- New tests exercised actual aiogram updates with a fake Telegram `BaseSession`, actual authenticated HTTP requests through the ASGI adapter, and real PostgreSQL transactions. Verified complete Unicode/markup-like text, stable message identity and redelivery after deletion, same text in separate messages, two-user/forged controls, 12 tasks over three pages, stale revisions/versions, current-status reselection, Delete/Cancel/Confirm/repeated callbacks, API errors, expired/restarted keyboards, edit fallback, unsupported/voice input, 4,096 UTF-16 output/64-byte callback limits, preview suppression, and fresh dashboard links. The fake transport prevents external Telegram calls.
- `uv sync --locked --group bot`, `ruff check .`, `ruff format --check backend bot tests scripts`, and `docker compose config --quiet` passed. The final Ruff check covered 36 Python files. A targeted document check verified UTF-8/final newlines/no trailing whitespace, balanced fences, and local links; a private scan found no configured secrets in changed bot/config/docs and no assessment PDF in the project. `.env` remains private and unmodified.
- `docker compose up -d --build --wait bot` passed. Compose also rebuilt/recreated API and the migration initializer as dependencies. Bot readiness returned `{status: ready, telegram_polling: disabled}`. The local `TELEGRAM_BOT_TOKEN` is absent; no fabricated token was substituted and no real Telegram update/send was claimed. A container/source hash check confirmed that the running bot image contains the final implementation.
- `python3 scripts/check_infrastructure.py --runtime` passed after rebuilding: PostgreSQL/Redis are healthy with no host port bindings; the frontend alone is published to loopback; the bot has no data-network membership, DB/Redis credentials, or persistence/queue client libraries. The existing worker and frontend services remain running.

Exact manual walkthrough from `/Users/neytrib/Desktop/omni_task`:

1. Add the real `TELEGRAM_BOT_TOKEN` to the ignored `.env`, and choose a distinct stable `BOT_IDENTITY` for this Telegram bot (keep it unchanged on token rotation). Run `docker compose up -d --build --wait`. Only one application may poll this bot; an existing webhook must be removed through your bot administration setup first. These credential-dependent steps were **not run** locally because no token is configured.
2. In a private Telegram chat send `/start`, `/help`, then one multiline text message. Expect one Pending task and a confirmation with all three status buttons and Open task. Send `/list`, tap the task, and verify the body, status, and creation time.
3. Tap In Progress, tap it again, then Completed. Expect current state without an error; navigation edits the current bot message. Use Back to tasks. With 12 tasks, verify five/five/two items with Next and Previous. A concurrent mutation may reset a stale page with an explanation.
4. Open the task, tap Delete then Cancel, and confirm it still exists. Tap Delete again, then Confirm delete, and confirm it disappears. Repeated/expired controls must not create or resurrect tasks. A second private user sees only their own list.
5. Send `/profile` and use Open Dashboard on this Mac in **Arc**. Expect the existing signed-in web shell, not a Kanban board. The link is short-lived/single-use, previews are disabled, and another `/profile` request replaces unused older links. Localhost is reachable only on this computer, not a separate phone.
6. Send a photo, unknown command, and voice recording; expect accurate unsupported/not-enabled feedback. Run `python3 scripts/test_backend.py -q tests/test_bot.py tests/test_bot_runtime.py` to repeat the fake-transport bot checks without credentials, or `python3 scripts/test_backend.py -q` for the full 151-test suite. Run `python3 scripts/check_infrastructure.py --runtime` for deployment boundaries.

Remaining limitations: real Telegram long polling, real client button rendering, and real Telegram-to-Arc login have not been manually exercised without a token. All bot walkthrough assertions above were verified through the fake transport and real API/database. Native Arc was not needed again in this stage because the frontend is unchanged. Dashboard task details/board, actual voice transcription, queued background notifications, and live updates remain deferred. Long-message previews preserve full stored content, but the current web shell does not yet display that content. Telegram retains pending updates for at most 24 hours; a broad outage delays the sequential inbox, and ambiguous sends can repeat a confirmation without duplicating its task. S3 and the requested bot login-link entry point are complete; work stops at this stage, with local services running.

## S3 follow-up - Profile command and local login fix

User correction: keep `/profile` only; the command sent no reply while other commands worked. Removed `/dashboard` from command routing, help and the live private-chat command menu. The old command now returns help without creating a task or issuing a login link.

Diagnosis: recent bot logs contained three sanitized `delivery_rejected` events and no API/handler/network failures. The active URL used `http://localhost:8080`. Official TDLib source and its link tests confirm that ordinary inline URL buttons reject this dotless hostname. Two validation probes against nonexistent chat/message zero did not reach meaningful button validation and were inconclusive; they delivered no message. No existing user task or private message content was read for diagnosis.

Fix: changed the default/example/Compose and active local `DASHBOARD_ORIGIN` to `http://127.0.0.1:8080`, preserving service credentials and exact browser Origin/CSRF checks. If Telegram rejects a URL button, the bot now sends a safe explanation instead of silently dropping the reply; logs contain only a fixed error code. No public tunnel, port exposure, deployment, or schema change was introduced.

Actual checks:

- `python3 scripts/test_backend.py -q tests/test_bot.py tests/test_bot_runtime.py`: 49 passed after removing the command.
- `python3 scripts/test_backend.py -q`: **154 passed**, exit 0, after URL-failure regression tests and default-origin/login-exchange coverage. The existing Starlette/httpx deprecation warning remains.
- `ruff check .`, `ruff format --check backend bot tests scripts` (36 files), and `docker compose config --quiet`: passed.
- `docker compose up -d --build --wait`: passed; `python3 scripts/check_infrastructure.py --runtime`: passed, preserving private PostgreSQL/Redis and bot isolation.
- Live Telegram `getMyCommands` confirmed exactly `start`, `help`, `list`, `profile` for private chats; bot readiness reported `ready` and `telegram_polling: polling`. No test message was sent to a real chat.
- A synthetic development link opened in native **Arc** at `http://127.0.0.1:8080/login`; the token fragment disappeared and the page visibly reported Signed in. The temporary mode-0600 redirect fixture was deleted. No real Telegram user's token was exposed, and no real task was changed.

Manual verification: in the existing private bot chat send `/profile` again, then click **Open Dashboard** on this Mac and open it in Arc. Expect the signed-in shell. `/help` and the command menu should show `/profile`, with no `/dashboard`; sending the removed command should return help. A separate phone still cannot reach this Mac through a loopback link. Full task-board UI remains deferred; this repair stops at the existing Telegram/login scope.

## S4 - Private dashboard login

F1 completed server-side authentication and the minimal login/logout shell. S3 supplies the requested `/profile` button. WebSocket-specific checks remain deferred.

Scope: `/profile`, login-link issuance/exchange, session/CSRF handling, logout, owner-authenticated browser API, and a minimal login/session landing page. The Kanban UI is S6. Build authentication helpers for later sockets without claiming live updates exist.

- [x] `/profile` produces an Open Dashboard button; the landing page exchanges the fragment token with no registration or typed code and removes it from the URL/history.
- [x] Link GET/HEAD do not consume credentials. Expired, revoked, reused, and malformed links fail; a newly requested link revokes older unused ones.
- [x] Concurrent exchange of one token yields exactly one session. Raw login/session tokens are absent from database fields, access logs, JavaScript-accessible storage (localStorage/IndexedDB), and fixtures committed to Git. The intended HttpOnly session cookie is allowed.
- [x] Cookie expiry/flags and host scope match `SPEC.md`; logout revokes the session. Secure-cookie exceptions exist only in explicit loopback development mode.
- [x] All browser task endpoints enforce session owner; cross-user IDs fail. CSRF/mutation Origin failures are tested, including logout and the unauthenticated exchange route's Origin policy.
- [ ] Expiry/reuse errors direct users to `/profile` without revealing tasks. Auth helper tests cover exact WebSocket Origin and session checks for S7.

Manual in Arc: open a fresh `/profile` link, confirm successful session establishment, reload, log out, and reopen the consumed link. Repeat using an expired link. Verify a second user sequentially after logout, or in an already available isolated Arc profile without modifying existing profile data; ordinary tabs in the same profile share cookies. Use API tests for concurrent token exchange and cookie assertions. Stop after S4.

## S5 - Voice pipeline and notifications

Scope: immediate voice receipt, durable API acceptance/dispatch reconciliation, Celery queues, replaceable OpenAI adapter, bounded audio download/conversion, independent retries/notifications, and cleanup. User explicitly prohibited paid calls and will provide an API key personally; the implementation must leave those calls disabled.

- [x] Bot acknowledgement precedes every API call in the deterministic transport test and completes within the 2-second test bound. API failures retain source identity and reuse the preliminary receipt during in-process retries.
- [x] Valid synthetic OGG/Opus input crosses real Redis to a separate Celery process, creates one Pending task with the complete fake transcript, and produces an independently delivered notification. Bot confirmation tests verify edits and task-status controls.
- [x] Duplicate updates during processing and after success/failure/deletion resolve to canonical state; repeated concurrent claims/jobs cannot duplicate a task. Identical recordings sent as distinct messages remain separate sources.
- [x] Transcription results survive a simulated task-commit interruption. The next job resumes the cached transcript without invoking the provider again, even at the provider-attempt cap. A transient notification HTTP failure retries delivery only.
- [x] Validate metadata and actual streamed bytes, full decoded duration/output, missing/corrupt/multi-stream audio, empty/oversized transcript, download/conversion/provider timeouts, provider 429/5xx/authentication errors, cancellation, and temporary-file cleanup.
- [x] Durable dispatch recovers lost publication/redelivery and expired leases in PostgreSQL tests. The Redis integration stops and restarts a distinct worker process while accepted input remains queued and other API operations remain responsive. A full production Redis/API restart during an active voice job is not claimed as performed in this stage.
- [x] Delete and redeliver a voice task: no resurrection; cached text is purged and pending notifications suppress deleted content. Current task status is loaded for notification delivery. Telegram calls already in flight cannot be recalled atomically.
- [x] Notification retry limits and terminal failure are durable; fallback message IDs are checkpointed and reused after adapter reconstruction. “Message is not modified” is success. Telegram Retry-After is propagated, and configurable leases cannot be shorter than the notification job deadline.
- [x] Tokens, audio, transcripts, and file identifiers are excluded from queue bodies; only job UUIDs travel through Redis. Internal transport credentials stay in headers or JSON bodies, never URLs. Full transcripts are accessible through the bot’s new text pagination.
- [x] OpenAI uses a configurable model/key behind a replaceable interface and an explicit paid-call guard. Fake/mocked test transports cannot become a paid fallback. Official transcription/Telegram/FFmpeg documentation is recorded in SPEC.md.
- [ ] A successful real Telegram-to-OpenAI transcription and the complete paid pause/send/help/resume walkthrough remain user-run: paid calls were explicitly prohibited. No speech-recognition result is claimed from fake text.

Verification evidence (2026-09-30):

- Final `python3 scripts/test_backend.py -q`: **308 passed in 14.15 seconds**, with the existing Starlette/httpx deprecation warning. It uses a disposable PostgreSQL database and isolated Redis key prefixes. It includes real FFmpeg conversion and a separate Celery worker process, not eager execution. Existing text-task, ownership, authentication and bot tests remain part of the suite.
- Test development exposed and fixed a stopped-worker fixture assumption: Redis can remove a queued message into a paused consumer’s outstanding BRPOP socket. The test now stops the first consumer, inspects the UUID-only envelope, and starts a fresh process to prove recovery. No failed run is counted as passing.
- A real Telegram recording supplied by the user was downloaded (42,896 bytes, OGG/Opus). The initial strict validator rejected its missing end-of-stream flag. Telegram byte-count verification and complete OGG page/checksum checks showed the file was complete; worker compatibility now allows that client variant, with a synthetic full-sample regression. The first failed job created no task and its failure notification succeeded. After correction, the same actual recording passed validation: all 42,896 input bytes/105 OGG pages were read, producing a 65,756-byte WAV with 2.0535 seconds of decoded samples. The probe duration was 2.060 seconds (encoder timing differs from decoded sample time); no truncation limit was used. Audio was deleted from private diagnostic temporary directories; none was sent to OpenAI.
- `.venv/bin/ruff check backend bot scripts tests` and `.venv/bin/ruff format --check backend bot scripts tests` passed (50 files). `docker compose config --quiet` passed.
- API/bot/worker were rebuilt, a private ignored mode-0600 database backup was saved, and controlled migration `0002_voice_jobs` was applied without resetting data. `alembic current` reported the new head.
- `python3 scripts/check_infrastructure.py --runtime` passed: only loopback frontend is published, PostgreSQL/Redis remain private, bot has no data credentials/client packages/network membership, Telegram token is bot-only, and OpenAI key is worker-only.
- Recreating the API exposed stale nginx DNS in the existing frontend. Updated the upstream to resolve Docker DNS; `docker compose exec -T frontend nginx -t` passed. The frontend Docker build passed TypeScript/Vite checks. After another API container recreation, proxy readiness returned 200, blocked internal routes returned 404, and unauthenticated sessions returned 401. This preserves login through provider configuration changes.
- `.env` remains mode 0600; current service logs contain none of the configured service/Telegram/provider secrets. Private diagnostic audio was cleaned and no raw voice fixture was added to the project.
- `docker compose exec -T api python scripts/queue_smoke.py` passed through real Redis with a separate worker process. Local provider selection is OpenAI with a blank key field and `ALLOW_PAID_TRANSCRIPTION=false`; no provider request was made.

Exact manual verification once the user deliberately enables their own paid provider:

1. In ignored `.env`, set `OPENAI_API_KEY` to a project key (a different OpenAI account is fine), keep `TRANSCRIPTION_PROVIDER=openai`, and set `ALLOW_PAID_TRANSCRIPTION=true` only when personally ready to incur API charges. Run `docker compose up -d --wait api worker`.
2. Run `docker compose pause worker`. Send one short voice recording in the private bot chat and confirm immediate acknowledgement. Send `/help` and `/list` while processing remains queued.
3. Run `docker compose unpause worker`. Confirm the original receipt becomes **Task saved** with status controls, and `/list` contains exactly one new Pending task.
4. Open the task and read the complete transcript, including its ending; use **Read full text** pages for long content. Change status and reselect it; delete only the test task with confirmation if desired.
5. Always unpause the worker. Set `ALLOW_PAID_TRANSCRIPTION=false` and recreate the worker when finishing a limited paid test. For a free equivalent use explicit `TRANSCRIPTION_PROVIDER=fake` in both API/worker, understanding its clearly labeled text is synthetic.

Remaining limits: real paid recognition is unperformed by instruction; the frontend is still the login shell and live updates remain deferred. Exactly-once external billing/send delivery cannot be guaranteed after ambiguous remote timeouts, but durable task creation is deduplicated. Stop after S5.

S5 activation follow-up: the user added their own OpenAI API key, set `ALLOW_PAID_TRANSCRIPTION=true`, and requested the remaining startup steps for their own Telegram test. Ran `docker compose up -d --wait api worker`; worker recreation completed and all six services are healthy. Verified the running worker loaded the configured key (without printing it), OpenAI mode and the enabled flag, HTTP readiness, and `python3 scripts/check_infrastructure.py --runtime`. No independent transcription request was initiated. The user then reported their real recording worked. This is user-reported recognition evidence; the assistant did not initiate an independent paid request. The complete paid pause/resume walkthrough is not claimed.

## S6 - Dashboard product interface

Implemented scope: private three-column board, create, complete details, status selector/drag handling, confirmed deletion, themes, responsive CSS and explicit UI states. Continuous live transport remains S7. See DESIGN.md for the original visual system.

- [x] Real owner-scoped task loading assembles every page at one revision; authentication, logout, expiry, and shared-cookie account switches clear private state correctly.
- [x] Create preserves full input, validates without silent truncation, prevents repeated submits, and retains exact content/idempotency identity for uncertain retries. Details render markup-like strings as safe text.
- [x] Status selector and drag handler use the same real API mutation; pending/rollback/conflict/error paths are covered. Actual pointer dragging in Arc is still unverified.
- [x] Delete requires confirmation, supports Cancel and already-deleted outcomes, and has tested initial/return focus behavior.
- [x] Tests, type checking, and the production build pass. Light/dark persistence, loading/empty/offline/error/session states and created/updated/deleted event reducers are covered.
- [ ] Arc inspection at 390×844, 1440×900 and 200% zoom; actual light/dark rendering, overflow, pointer drag, native focus containment and reduced-motion appearance. **Blocked:** Arc native control calls returned state-change and timeout errors before successful dashboard navigation. No other browser was used.
- [x] Ready is honestly labeled Manual updates. Refresh/focus/online snapshot recovery works without claiming a live transport or a heartbeat.

Verification evidence (2026-09-30):

- `npm --prefix frontend test`: **77 passed across 5 files**. Tests cover auth/one exchange/token removal, safe complete content, create retries/validation, dialog controls, selector writes, rollback/conflicts, session deadline and cross-tab changes, pagination races, stale responses, offline recovery, event gaps, and pinned CSP style hashes. Initial integration runs exposed missing test-only ResizeObserver/Node types and stale mocked session responses; those fixtures were corrected and the final complete run passed.
- `python3 scripts/test_backend.py -q`: **308 passed in 16.43 seconds**, including real PostgreSQL, isolated Redis, and the separate worker integration. The existing Starlette/httpx deprecation warning remains. The test suite never calls OpenAI or sends real Telegram messages. `.venv/bin/ruff check bot/app/views.py` and `.venv/bin/ruff format --check bot/app/views.py` passed for the profile copy correction.
- `npm --prefix frontend run build`: **passed**, including `tsc --noEmit` and Vite production build. React 19/TypeScript/Vite retained. New drag/font packages and Node test types are pinned in package-lock.json. Fonts and licenses are bundled locally.
- Production frontend was rebuilt with `docker compose build frontend` and recreated with `docker compose up -d --no-deps --wait frontend`. `docker compose exec -T frontend nginx -t` passed. HTTP returned 200, no-store, no-referrer, and the strict CSP. The bot's `/profile` description was updated to reflect the available dashboard. Final assets/licenses and that copy were deployed with `docker compose build frontend bot` and `docker compose up -d --no-deps --wait frontend bot`; API and worker were not restarted.
- `docker compose exec -T api python scripts/api_smoke.py`: **passed** with two synthetic users. The same smoke workflow was also run with browser requests routed through `http://frontend:8080`, proving production proxy login, full content, source deduplication, owner isolation, forged/invalid input rejection, mutation, deletion, and logout. Synthetic task contents were removed afterward; minimal user/source/session receipts remain.
- `python3 scripts/check_infrastructure.py --runtime`: **passed**. PostgreSQL/Redis are healthy without host port bindings; only the frontend is loopback-published and the bot remains HTTP-only.
- Independent review found light muted-label/drag-handle contrast and a cross-tab shared-cookie account switch issue. The light muted color/handles were darkened to #66635c (counts 4.54:1, canvas 5.54:1), and pre/post-snapshot session checks with regressions prevent cross-account cached boards. This calculation is not a rendered visual audit.
- Arc was detected and read through native computer use. Repeated attempts to open a dedicated local tab failed with changed-state/timeout errors; available browser-extension controls only exposed the prohibited alternative in-app browser. No fallback browser, profile reset, or extension change was used. The six synthetic visual fixtures and temporary loopback redirect server were cleaned up; no credential link was printed or saved.
- No paid transcription request, public deployment, database migration, or Git initialization was performed in S6. Existing user tasks and provider configuration are preserved. Browser evidence is explicitly incomplete rather than inferred from DOM tests.

Exact manual verification (services left running locally):

1. Send `/profile` in Telegram Desktop on this Mac; open **Open Dashboard** in Arc. Verify `/login` contains no token, the board loads your actual tasks, and reload keeps the session.
2. **New task** → enter multiline text including `<b>literal text</b>` → **Create task** once. Expect one Pending card. Open its title and compare the complete content and timestamps.
3. Move the card from its title, preview, or background to In Progress, then use Tab and the labeled status selector to choose Completed. Refresh; the saved status must remain.
4. Open details → **Delete task** → **Cancel**; verify it remains. Repeat and confirm only this disposable test task; verify it disappears. Check Escape and focus return.
5. Toggle light/dark, reload, and inspect at 390×844, 1440×900 and 200% zoom in Arc. Check long content, visible focus, all controls, and no page-width overflow.
6. Temporarily set Arc DevTools Network to Offline. Expect cached content, Offline, and disabled writes. Restore No throttling; expect reconnect then Manual updates. Send a Telegram text task and use **Refresh tasks** to load it.
7. **Log out**, reload, and verify Telegram access instructions. Reopening the consumed login link must explain that a fresh `/profile` link is needed.

Implementation stops at S6. The pending visual checks are an explicit environment limitation; S7 WebSocket/Redis transport has not been implemented.

## S6 follow-up - Whole-card dragging

User correction: dragging must work across the task card, not only its six-dot button. The existing API/status behavior is preserved; no S7 work is included.

- PointerSensor now listens on the card. Background, source, title, and preview start a drag after 8px mouse/pen movement. The status label/select is excluded so ordinary selection works. The six-dot button remains the keyboard drag activator.
- Touch uses a 250ms hold with a 5px scrolling tolerance and retains native card scrolling. Post-drag pointer clicks are suppressed; the next ordinary title click and keyboard activation still open details.
- Official dnd-kit sensor documentation and installed 0.5.0 types/source were checked. SPEC.md, DESIGN.md and the README walkthrough reflect the whole-card interaction.
- `npm --prefix frontend test`: **96 passed across 7 files**. Seventeen new tests exercise the installed PointerSensor's actual event binding and activation thresholds, including title/preview/card, excluded controls, touch hold/scroll/release, and disabled cards. Two new App tests verify persisted drop, canceled drop, suppressed release-click, subsequent normal click, and keyboard opening. The sensor tests stop before layout/collision work; they do not claim browser geometry verification.
- `npm --prefix frontend run typecheck` and `npm --prefix frontend run build`: **passed**. `docker compose build frontend` and `docker compose up -d --no-deps --wait frontend`: **passed**, frontend healthy. Only the frontend was recreated; no provider calls, data changes, or backend changes were needed.
- Arc native control was retried. Opening a local tab still returned `timeoutReached`; visual drag verification remains unavailable. No alternate browser was used.

Manual check: refresh the dashboard in Arc, drag a test card from its preview to In Progress, then from its title to Completed. Each drop should move it without opening a dialog. Click the title normally to open details; use the status dropdown and refresh to confirm persistence. On a phone-sized viewport, quick swipes should scroll and a brief hold should start dragging. Stop after this S6 correction.

## S6 follow-up - Dismiss transient notifications

User-reported issue: the bottom success notification remained visible indefinitely. All board notices now share a replaceable four-second dismissal timer. Every new action restarts it, including identical text; unchanged refreshes do not extend it. Session expiry, reset and unmount cancel the timer. Persistent error/offline states are preserved.

- `npm --prefix frontend test`: **100 passed across 7 files**, including four new regressions for dismissal, identical-message restart, refresh behavior, and unmount cleanup.
- `npm --prefix frontend run build`: **passed**, including TypeScript checking. `docker compose build frontend` and `docker compose up -d --no-deps --wait frontend`: **passed**, frontend healthy. Verified the served index matches the new production build.
- SPEC.md and DESIGN.md document the notification lifetime. No API/backend, provider settings, task data, or live-transport changes. Browser timing was not visually reverified; Arc native control has repeatedly timed out in the immediately preceding dashboard checks.

Manual check: refresh the dashboard in Arc, move a task, and wait four seconds; the bottom notification should disappear. Move another task to the same status before the first timer expires; the notification should remain for four seconds after the latest action, then disappear. Stop after this S6 fix.

## S7 - Live updates and recovery

Completed 2026-09-30. Scope: committed outbox dispatch, Redis Pub/Sub, authenticated owner-scoped API WebSockets, revision/snapshot reconciliation, connection states and heartbeats. S8 is not authorized.

- [x] Text/API and fake voice-worker creation, bot/dashboard status changes, and deletion fan out across two independent API processes. Two Arc tabs show synchronized state; healthy synthetic HTTP-to-two-socket delivery measured 0.164 seconds (not a rendered-browser latency measurement).
- [x] User B receives none of A's hints/IDs/content. Missing/revoked/expired sessions and unapproved/missing/duplicate Origins are rejected; established sockets close on revocation/expiry checks. Cookies are the only socket credentials; query identifiers are rejected.
- [x] Disconnected clients recover complete snapshots, including create/status/delete. Arc's offline tab stayed stale with disabled writes, then automatically displayed the intervening task on reconnect.
- [x] Initial registration/snapshot races, stale/delayed snapshots, duplicate/out-of-order hints, hints during optimistic writes, and bounded queue overflow are tested. Old snapshots cannot replace newer applied revisions.
- [x] A deliberately lost final Pub/Sub hint is repaired by PostgreSQL revision heartbeat with no subsequent mutation.
- [x] Real Redis subscriber disconnection/reconnection and API process restart recover; subscribe acknowledgement and bounded PING/PONG watchdog protect readiness. Database-check failure closes connections instead of asserting Live. An entire Redis container restart was not repeated in this stage.
- [x] Browser backoff is bounded at 30 seconds, watchdog detects missed messages, focus/online recovery resyncs, and offline mutations are disabled. Deterministic DOM tests cover retained drafts and session cleanup.
- [x] Outbox publication sees only committed rows; rollback publishes nothing, failed/ambiguous publication retries the same revision, and competing PostgreSQL dispatchers skip locked rows. Celery queues remain separate from the live channel.

Changed implementation:

- `backend/app/live_outbox.py`, `realtime.py`, and API lifecycle implement dispatch, per-process subscriber/hub, safe wire messages and session/revision checks. Shared API/worker task transactions already wrote outbox rows and were preserved.
- `0003_live_outbox` adds a partial pending-row index. Config/Compose/example expose a deployment-specific `LIVE_CHANNEL`; redis-py Pub/Sub is global across Redis database numbers.
- `websockets==17.1` is resolved in the backend dependency group and lockfile. Uvicorn uses the Sans-I/O protocol and warning-level connection logs; nginx upgrades only `/api/ws/tasks` while preserving private-route blocking and CSP.
- `frontend/src/liveTasks.ts`, `useTasks.ts`, and App details handling add transport/reconnect/caught-up state and graceful remote deletion. Existing full-card dragging and four-second notices remain.
- SPEC.md, DESIGN.md, README.md and AGENTS.md reflect S7 and its stopping boundary. Official references are recorded in SPEC.md. No Git repository was initialized.

Executed checks and results:

- `python3 scripts/test_backend.py -q`: final **339 passed, 1 existing Starlette/httpx deprecation warning, 47.27s**, exit 0. This includes actual disposable PostgreSQL, real Redis, two Uvicorn processes and an independent fake-provider Celery worker; no eager execution or SQLite substitutes. The initial full run had 338 passed/1 failed because a test rejected legitimate older hints after reconnect; the test was corrected to reject only the deliberately lost newer hint, and the full suite was rerun successfully.
- `python3 scripts/test_backend.py -q -s tests/test_realtime_integration.py`: final **3 passed, 22.97s**. Healthy synthetic bot HTTP request through committed publication to both API sockets measured **0.164s**. Voice uses synthetic OGG/Opus, real conversion, a fake provider, a separate worker, and duplicate-job fencing. No OpenAI call or real Telegram send occurred.
- Focused PostgreSQL WebSocket security/recovery tests: **23 passed**. Focused outbox/transaction checks: **7 passed**. `.venv/bin/ruff check backend bot scripts tests` and `.venv/bin/ruff format --check backend bot scripts tests`: passed, **56 files**.
- `cd frontend && npm run typecheck && npm test -- --reporter=dot && npm run build`: **125 passed across 9 files**, TypeScript and production build passed. Production asset is `index-BCyL9J0z.js`. Browser-independent component tests use a DOM environment and mocked sockets.
- Docker's saved-credential helper stalled on public image metadata. Builds/tests completed with a temporary anonymous configuration, leaving saved credentials untouched: prefix affected commands with `DOCKER_CONFIG=/Users/neytrib/Desktop/omni_task/tmp/s7-docker-config DOCKER_HOST=unix:///Users/neytrib/.docker/run/docker.sock`. Canceled pre-test builds are not counted as successes. One interrupted focused runner's confirmed idle disposable database was cleaned; other databases were preserved.
- `docker compose build api frontend`: passed. Saved a private ignored mode-0600 backup, ran `docker compose stop api worker`, `docker compose run --rm migrate`, and `docker compose up -d --wait api worker frontend`: passed. `docker compose exec -T api alembic current`: **0003_live_outbox (head)**. No volumes were reset; existing task data/provider settings were preserved.
- `docker compose exec -T api python scripts/queue_smoke.py`: passed against Redis and the restarted separate worker.
- `docker compose config --quiet`, `docker compose exec -T frontend nginx -t`, and `python3 scripts/check_infrastructure.py --runtime`: passed. All six services are healthy; PostgreSQL/Redis have no host port bindings, only frontend loopback is published, and bot remains HTTP-only. Running API live files/nginx configuration match the workspace. Recent service logs contain none of the configured credentials or login fragments.

Arc checks actually performed:

- Native Arc controls worked in S7. Opened a synthetic private login through a temporary loopback redirect without printing/saving the bearer URL; the visible URL became clean `/login` and the board showed Live. Desktop dark rendering was inspected.
- A synthetic text task submitted to the authenticated bot API appeared while the Arc page stayed open, without refreshing. Full task details were readable and changed to In Progress after an external bot API update.
- Opened a second authenticated Arc tab; selected Completed through the dashboard control and observed the same status in the first tab. A subsequent bot API read also returned Completed.
- With task details open, a remote API deletion removed the card, closed the dialog and returned focus to New task. Both tabs showed zero tasks. The deletion notice subsequently disappeared.
- In Arc DevTools, set one tab to Offline; observed cached state and disabled writes. Created a synthetic bot task while disconnected; the offline tab remained unchanged. Restored No throttling and observed automatic task recovery and Live without manual refresh.
- Logged out from one tab and observed both signed out. Restored network settings, closed DevTools and only the two newly created test tabs. Both synthetic task bodies were removed through the API; minimal deduplication/user records remain. No browser/profile/extension was replaced or installed.

Exact manual verification for the user's real account:

1. Send `/profile` in Telegram and open the fresh link in Arc. Open a second tab at `http://127.0.0.1:8080/login` with no fragment; both must reach Live. Reload old app tabs once to load the new build.
2. Send a disposable text task in Telegram. Verify one Pending card appears in both tabs without refresh. Press its bot In Progress button; both cards must move. Select Completed in the dashboard; the next Telegram list/open must report Completed (old bot messages do not refresh automatically).
3. Open its details in one tab; delete with confirmation in the other. Expect removal everywhere and graceful detail closure.
4. Set one Arc tab's Network throttling to Offline. Create/change/delete disposable tasks through the other interface. Restore No throttling; compare the complete recovered board and Live state. Log out and verify both tabs lose private content.
5. A real voice recording uses the already enabled paid OpenAI configuration only when you personally choose to send it. The assistant did not initiate a paid recording. Automated tests already verified fake voice-worker live delivery; real Telegram voice recognition was previously user-reported in S5.

Remaining limits: this stage did not conduct a new real Telegram/OpenAI voice call, repeat a full Redis container restart, use a second actual Telegram account in Arc, or finish S6's phone/light-theme/zoom/pointer-drag visual audit. Isolation, fake voice, lost-hint recovery, subscriber reconnection and process restart have explicit automated evidence. Stop after S7; S8 remains pending.

## S8 - Integrated verification and local handoff

Completed 2026-09-30. The latest user request authorized review of the complete existing application against the assessment and SPEC.md, verified fixes, durable delivery/failure testing, structured logs, and local handoff. No new product features, schema changes, paid calls, public deployment, or Git initialization were performed. The confidential one-page assessment was re-read locally in memory; it was not copied into the repository or uploaded. Its A1–A7 requirements remain distinct from the user's additional choices in SPEC.md.

### Findings identified before their fixes

| Finding and location | Reproduction / evidence | Focused fix and regression |
| --- | --- | --- |
| Expired worker leases were accepted after PostgreSQL lock waits: `backend/app/voice.py:123`, `:184`, `:339` and related fenced operations. | Hold the owner's row lock in one connection; block a claim/completion/notification authorization in a second; advance the test clock beyond expiry only after PostgreSQL reports the lock wait; release the lock. Three new tests failed before the fix: expired completion/authorization succeeded and a new lease used the old time. | Compute time after acquiring locks in claims, renewals, transcript/result commits, failures, delivery contexts and checkpoints. Three real-PostgreSQL regressions in `tests/test_voice_domain.py:150`, `:177`, `:199`. |
| Login exchange could consume a link that expired while waiting on its row lock: `backend/app/auth.py:46`. | Hold the LoginLink row lock, start exchange in another connection, observe its PostgreSQL lock wait, advance the clock past expiry, then release. The pre-fix regression failed with DID NOT RAISE. | Lock and refresh the link, then check current time, consumption, and revocation before creating the session in the same transaction. `tests/test_auth_lock_wait.py:13`; existing concurrent exchange tests retain exactly one winner. |
| Fallback Telegram notification reused stale text/buttons: `bot/app/voice.py:162`. | First context returns Pending/version 1; Telegram rejects editing the missing/uneditable acknowledgement; the refreshed context returns Completed/version 2. The old fallback still used Pending/version 1. | Rebuild the view from the refreshed authenticated context immediately before fallback send. `tests/test_bot_voice.py:585`. No claim of atomic external delivery. |
| Celery producer subscribed to unused results: `backend/app/jobs/dispatcher.py:23`. | Real Redis proxy outage exposed about 21 seconds of result-backend retry delay despite `retry=False`. Official `send_task` source defaults `ignore_result` to false independently of the worker task decorator; successful publications accumulated subscriptions for results never stored. | Pass `ignore_result=True` for both voice queues. Exact-options regression in `tests/test_voice_jobs.py:136`; real outage test requires dispatch under five seconds and no residual result subscriptions. |
| Operational warnings lacked correlation and structure: dispatcher/polling/worker warning paths. | Synthetic failed publication and failed bot API update produced plain messages without the request/job identity needed to trace recovery. | Shared `omni_logging.py`, API/bot middleware and propagation, worker/outbox/live events, safe JSON formatting via the Celery logging signal. Validated UUIDs and allowlisted metadata only; new observability and bot retry tests. |
| README described implemented dashboard/live behavior as pending. | Its introduction said live transport was a future stage; its login walkthrough said the board did not exist. | Corrected current behavior, recovery/logging guidance, isolated restart command, and stage boundary. |

The existing PostgreSQL processing/notification dispatch intents and committed live outbox already close the database-commit/queue-publication gap. They were preserved and exercised with actual failures rather than replaced. No additional verified ownership, duplicate-source, full-content, callback, provider-classification, or WebSocket consistency defect was reproduced.

### Acceptance evidence

- [x] All six current images start in a fresh private Compose project with fake/blank external credentials, controlled migrations, independent data volumes, and no host ports. PostgreSQL and Redis containers were actually recreated; tasks, opaque sessions, source deduplication, Redis AOF marker, and a queued Celery probe survived. Both worker consumers recovered. Only that temporary project's resources were removed; cleanup was checked by its exact project label.
- [x] Complete backend/frontend suites, lint/format, type checking, production build, migration consistency and live runtime health passed. Exact results follow.
- [x] Real PostgreSQL constraints, source/job concurrency, deletion receipts, two-user API/bot/WebSocket isolation, login races/reuse/expiry, CSRF/logout, provider failures, notification independence and missed/repeated/out-of-order live recovery are covered by the passing suites. Relevant files: `test_tasks.py`, `test_transactions.py`, `test_auth*.py`, `test_bot*.py`, `test_voice*.py`, `test_live_outbox.py`, `test_realtime*.py`, plus new fault tests.
- [x] Separate prefork worker child SIGKILL produced an actual Celery `redelivered=True` execution. It resumed a durably cached transcript into one task without another download/provider attempt. A separate production supervisor SIGKILL/restart recovered only notification work after an ambiguous external edit. Both production queues and independent worker PIDs were verified. These are neither mocks of Celery nor eager-mode tests.
- [x] A per-test TCP proxy disconnected actual Redis while the API committed a request. Publication failed promptly, PostgreSQL retained the request, and restored connectivity produced one task and notification. A real Pub/Sub publish followed by a simulated lost acknowledgement replayed the identical revision without another task.
- [x] Configured-secret/source/log scans, private-artifact exclusions, `.env` mode 0600, bot package/network/credential separation, and absence of PostgreSQL/Redis host ports passed. The workspace has no `.git`; no staged diff or commit could be reviewed and Git was not initialized.
- [x] Arc desktop inspection exercised one clearly marked audit-created task: creation, multiline/literal HTML rendered as safe text, full details, keyboard status change to In Progress, delete Cancel, confirmed deletion, focus restored to New task, notice dismissal, and the board returning to Live after the API restart. The audit-created task was removed; the three pre-existing tasks and existing session/theme/tabs were preserved.
- [ ] Full final external Telegram voice -> dashboard journey and phone/light-theme/200%-zoom/whole-card pointer-drag visual checks were not completed in S8. Native Arc clicks/dragging intermittently timed out or had no observed effect; no alternate browser was used. S7's two-tab/live/offline/logout Arc evidence remains historical, not a newly repeated result. Deterministic real-worker tests cover fake voice and Telegram transports; no paid transcription was initiated.
- [x] SPEC.md and README document current architecture, official references, setup, bounded retries/timeouts/concurrency, durable recovery, privacy-preserving logs, external-delivery ambiguity and exact manual steps. Stop after S8.

### Commands actually run and results

Commands ran from `/Users/neytrib/Desktop/omni_task`. Public-image builds used the existing ignored anonymous Docker configuration because the user's credential helper had previously stalled. The prefix below changes only these processes, not the user's Docker settings:

```sh
DOCKER_CONFIG=/Users/neytrib/Desktop/omni_task/tmp/s7-docker-config DOCKER_HOST=unix:///Users/neytrib/.docker/run/docker.sock
```

Apply that prefix on the same line before each build/test command when reproducing this environment's workaround.

| Executed command / check | Observed result |
| --- | --- |
| `python3 scripts/test_backend.py -q tests/test_auth_lock_wait.py` before auth fix | 1 failed as expected, demonstrating expired link accepted after lock wait. |
| Three new lock-wait voice tests before the lease fix | 3 failed as expected; then passed after the fix. |
| `python3 scripts/test_backend.py -q tests/test_auth_lock_wait.py tests/test_auth.py tests/test_live_outbox.py tests/test_realtime.py` | 82 passed, 7.23s. |
| Worker/media/provider/domain focused suite after logging changes | 126 passed, 10.78s. |
| `PYTHONPATH=backend:. .venv/bin/pytest -q tests/test_bot_runtime.py tests/test_bot_voice.py -k 'not real_'` | 51 passed, 2 deliberately deselected PostgreSQL cases; both included in the final full run. An earlier local attempt without a database produced 2 fixture errors, not an application result. |
| `python3 scripts/test_backend.py -q tests/test_voice_jobs.py tests/test_voice_domain.py` | 32 passed, 1.64s. |
| `python3 scripts/test_backend.py -q tests/test_delivery_recovery_integration.py` | Final 4 passed, 24.34s. First harness run had 3 pass/1 failure because one consumer was probed before the other was ready; readiness now probes both. A subsequent pre-`ignore_result` run passed 4 in 44.20s and exposed the needless outage retry delay. |
| `python3 scripts/test_backend.py -q` | **356 passed**, 69.95s, exit 0, no skips. Uses a random disposable PostgreSQL database, real Redis prefixes/channels, and separate workers/API processes. |
| `npm --prefix frontend test -- --reporter=dot` | **125 tests passed in 9 files**, exit 0. |
| `npm --prefix frontend run typecheck` and `npm --prefix frontend run build` | Both exit 0; production Vite build succeeded. No browser downloaded. |
| `.venv/bin/ruff check omni_logging.py backend bot scripts tests` and `.venv/bin/ruff format --check omni_logging.py backend bot scripts tests` | All checks passed; 62 files already formatted. |
| `docker compose build api bot frontend` | All three images built successfully. |
| `python3 scripts/check_isolated_recovery.py` | `status=passed`; tasks/sessions/deduplication/AOF marker/queued job preserved; second-user isolation passed; both worker queues healthy; no host ports; data containers recreated; private project removed. |
| `docker compose up -d --wait api worker bot frontend` | Six services healthy; migrate initializer exited successfully; existing PostgreSQL/Redis containers and volumes preserved. |
| `python3 scripts/check_infrastructure.py --runtime` | Loopback frontend, private data network, HTTP-only bot, package/credential isolation, healthy and unexposed PostgreSQL/Redis all passed. |
| `docker compose exec -T api python scripts/queue_smoke.py` | Redis probe passed in separate worker container `7eded14ade91`, PID 10; synthetic result removed. |
| `docker compose exec -T frontend nginx -t` and `curl --fail --silent http://127.0.0.1:8080/api/health/ready` | nginx configuration valid; readiness returned `ready`. |
| `docker compose exec -T api alembic current` and `docker compose exec -T api alembic check` | `0003_live_outbox (head)`; no new upgrade operations. |
| Private inline Python source and runtime-log checks (values read only into memory, never printed) | 105 source/config/document files contained none of the configured credentials; PDF/key/audio exclusions passed; `.env` 0600. API/worker/bot logs contained no configured credentials, login-token markers, credential-bearing Telegram URLs, or audit task text; 20 API JSON events had valid correlation UUIDs. Six runtime services healthy. |
| Independent logging formatter/context probes | API/bot/worker each emitted valid content-free JSON with preserved correlation; 3 local observability tests passed, HTTP case included in full PostgreSQL run. |
| Final inline documentation/source-image checks | Markdown final newline, whitespace and fence checks passed. SHA-256 comparison confirmed all 23 API and 12 bot Python source files, including the shared logger, match the running containers. |

Backend runs report one existing Starlette TestClient/httpx deprecation warning. No dependency replacement was made solely to remove it. Full-run results supersede the earlier scoped checks.

### Remaining limitations and exact manual acceptance

External providers cannot participate in a PostgreSQL transaction. A crash after an external transcription or fallback send but before its durable checkpoint can incur another provider call/send; exactly-once external notification delivery is not claimed. Crash tests explicitly advanced only synthetic lease deadlines after killing processes; they do not claim to have waited or measured the default 240-second recovery interval. Redis AOF is `everysec`; the test verifies container recreation, not physical disk loss or arbitrary host power failure. Framework exception messages are intentionally suppressed for privacy; sanitized event codes/UUIDs are the supported diagnostics.

No new real Telegram/OpenAI call, second actual Telegram account, public deployment, physical-phone test, or complete final visual audit was performed. The user's enabled paid provider setting was preserved for their own testing. Minimal source/session metadata from the single Arc-created-and-deleted task remains by design; existing user data was not deleted. There is no Git metadata in this workspace.

For a local acceptance walkthrough, use only newly created disposable tasks:

1. Run `docker compose ps`, `python3 scripts/check_infrastructure.py --runtime`, and `docker compose exec -T api python scripts/queue_smoke.py`. Expect six healthy services, only frontend on `127.0.0.1:8080`, and a passed probe. Run `python3 scripts/check_isolated_recovery.py` for the destructive-container fault exercise in its **own** temporary project; it leaves the active project intact.
2. In the bot's private chat send `/start`, `/help`, then `S8 manual test` with a second line. Expect one Pending task and status buttons. Send the same words as a second Telegram message: expect a second distinct task. `/list` -> open -> In Progress -> In Progress again should show current state without error. Old stale buttons must refresh/reject safely.
3. Send `/profile` and open the link in Arc. Confirm the token vanishes from the URL, then open another tab at the same origin. Expect Live and the same tasks in both. Bot status changes move cards; a dashboard-created task appears on the next Telegram list/open. Old Telegram messages do not automatically refresh.
4. Create a fresh multiline task, inspect full content, drag from its title/preview, and repeat with the labeled keyboard status selector. Verify temporary success notices disappear. Test light/dark persistence, a narrow phone viewport, and 200% zoom. Delete Cancel must preserve the task; confirmed deletion must remove it and close any open details in the second tab.
5. Disconnect one Arc tab with DevTools Network -> Offline. Make changes in the other interface; restore networking. Expect full reconciliation, including deletions, and Live after recovery. Log out: reload remains signed out and the other tab clears private content within the session-check interval. Reusing the link, or waiting over its five-minute lifetime before exchange, must request a fresh `/profile` link. A separately authenticated second Telegram user must see none of the first user's tasks.
6. For **your own explicitly chosen voice test**, run `docker compose pause worker`, send a short voice recording, then `/help`. Expect an immediate receipt and a responsive command. Always restore with `docker compose unpause worker`. Expect one task and a final acknowledgement edit; no fake Pending task on failure. Your current configuration uses OpenAI, so this user-initiated step can be billable. For no-charge automated coverage run `python3 scripts/test_backend.py -q tests/test_delivery_recovery_integration.py tests/test_voice_worker_integration.py` instead; it uses synthetic audio/fake transports/providers and never changes `.env`.

S8 is complete with these explicit limitations. Work stopped at this stage.

## S9 - Submission and VPS deployment preparation

Completed 2026-09-30. The user authorized preparation of the existing application for submission and one Ubuntu VPS, without new features, resource purchases, public deployment, or paid/provider tests. Read AGENTS.md, SPEC.md, TASKS.md and workspace state first; the directory still has no Git metadata. The existing local Compose configuration, active `.env`, running services and user data were preserved. Verification used fresh source extraction/private fixture projects, synthetic records and blank external credentials.

### Changes and focused review corrections

- `compose.production.yml`, `production.env.example`, and `deploy/Caddyfile` add a pinned HTTPS edge, same-origin API/WebSocket forwarding, release image tags, forced production cookie/origin settings, private PostgreSQL/Redis networking, persistent certificate storage, controlled migration dependencies and per-container log rotation. Local `docker-compose.yml` is unchanged.
- `scripts/configure_production.py` creates a mode-0600 server environment with generated independent secrets, placeholder identity and disabled/blank external integration settings. `scripts/production.py` shares protected configuration loading with backup/restore. Review reproduced that ordinary `docker compose --env-file` can be overridden by inherited shell variables; the wrapper now removes inherited application/Compose values and rejects full configuration output. Tests and a read-only real Compose render verify conflicting inherited values cannot override the protected file.
- `scripts/backup.py` and `scripts/restore.py` provide private, atomic, checksummed logical backups and transactional restore into a new database only. They never drop a database or choose cutover. The runbook preserves the old database/Redis volume and revokes restored authentication before public cutover. An independent review corrected the extra-file examples: any explicit `--file` requires listing the base, production and recovery files together.
- `scripts/check_clean_setup.py`, `scripts/check_production.py`, `deploy/check_https.py`, and `scripts/check_restore.py` run isolated verification using real services. `scripts/package_submission.py` creates an allowlisted source archive with per-file hashes and secret/private-artifact exclusions. `.gitignore`/`.dockerignore` include production env and backup patterns; the Docker test stage includes the nonsecret production fixtures needed by new tests.
- README, SPEC, AGENTS and the documents under `docs/` describe setup/configuration, requirements/test mapping, interview architecture, an allocated three-minute demo, deployment/update/rollback, trusted backup/restore, known limitations and substantial AI assistance. No application behavior or schema changed.
- The initial full S9 backend run passed but exposed a test-only Celery lifecycle warning. The outage fixture's readiness result could be garbage-collected after its Redis proxy disconnected, and `AsyncResult.__del__` then attempted to unsubscribe. Forcing garbage collection during the outage with unraisable warnings treated as errors reproduced the failure. The test now releases readiness results while Redis is reachable, closes the result consumer before the proxy, and retains the forced-collection regression. This does not alter application jobs or suppress the warning.

### Acceptance and commands actually run

Private Docker verification used the existing ignored anonymous Docker configuration to avoid this Mac's stalled credential helper. It changes only the invoked process environment:

```sh
DOCKER_CONFIG=/Users/neytrib/Desktop/omni_task/tmp/s7-docker-config DOCKER_HOST=unix:///Users/neytrib/.docker/run/docker.sock
```

Apply that prefix on the same line before the Docker/build verification commands when reproducing this machine's workaround. It is not an Ubuntu requirement and does not belong in server configuration.

| Executed command / check | Observed result |
| --- | --- |
| `python3 scripts/check_clean_setup.py` | Initial run passed with 118 then-current files. Final-source rerun passed with **125 source files**: fresh extraction/configuration, all app images built, six healthy services, two-user HTTP checks, separate-worker queue probe, migration consistency and nginx validation. No host ports or external credentials; only its temporary project/volumes removed. Unique rehearsal image tags cleaned up. |
| `python3 scripts/check_production.py` | Passed twice after final privacy assertions: actual TLS with a private CA, HTTP redirect, built frontend, Secure/HttpOnly cookie, CSRF, two-user ownership, WSS create/update/delete, bad origin/unauthenticated rejection and logout. Seven healthy services, no fixture host ports. Actual upstream 502 log scan checked both stdout/stderr and found no configured secret or credential-bearing request marker. Only fixture resources removed; no public ACME or browser trust changes. |
| `python3 scripts/check_restore.py` | Passed actual custom-format dump/new-database restore, complete Unicode content/status/authentication/source/deletion receipt/ownership checks, existing-target refusal, and queued fake voice plus saved-notification recovery with empty real Redis and a separate prefork worker. No live user database touched. |
| `.venv/bin/pytest -q tests/test_submission.py tests/test_backup_restore.py` | 26 passed. Packager manifest/exclusions/secret/symlink guards, configuration permission/injection/override guards, checksum/export/restore refusal behavior covered. |
| `python3 scripts/test_backend.py -q` (initial full S9 run) | 382 passed in 98.20s, exit 0. Two warnings: existing Starlette deprecation and the newly investigated Celery teardown warning. Real PostgreSQL/Redis and separate workers/API processes; no skips or paid transports. |
| `python3 scripts/test_backend.py -q tests/test_delivery_recovery_integration.py -k isolated_redis_outage -W error::pytest.PytestUnraisableExceptionWarning` with forced collection before fixture fix | Failed as expected in 27.48s with the same Redis result-destructor retry, establishing the test lifecycle defect. |
| `python3 scripts/test_backend.py -q -W error::pytest.PytestUnraisableExceptionWarning tests/test_delivery_recovery_integration.py` after fixture fix | Four real-process recovery tests passed in 28.44s. Unraisable warnings are errors; only the existing Starlette warning remained. |
| `python3 scripts/test_backend.py -q -W error::pytest.PytestUnraisableExceptionWarning` (final full S9 run) | **382 passed in 97.87s**, exit 0, no skips. Only the existing Starlette TestClient/httpx deprecation warning remains. This final run supersedes the initial full run and proves the lifecycle correction without suppressing unraisable warnings. |
| `npm --prefix frontend test -- --reporter=dot` | 125 passed in 9 files, 9.44s, exit 0. DOM tests; no browser downloaded. |
| `npm --prefix frontend run typecheck` and `npm --prefix frontend run build` | Both exit 0. Vite production asset build succeeded. |
| `.venv/bin/ruff check omni_logging.py backend bot scripts tests deploy` and `.venv/bin/ruff format --check omni_logging.py backend bot scripts tests deploy` | Final rerun passed after fixture cleanup; 73 Python files already formatted. |
| Independent synthetic production-wrapper verification | Real read-only Compose rendering with conflicting inherited release, database, domain, token, paid flag and Compose controls used protected-file values; `config --quiet` exited 0. No services started or credentials printed. |
| `python3 scripts/check_infrastructure.py --runtime` and `docker compose ps --format json` | Passed HTTP-only bot dependency/credential boundaries and private healthy PostgreSQL/Redis. All six existing services healthy; only frontend published on host loopback. No existing container restarted for S9 verification. |
| `git status --short` | Exit 128: no Git repository, as at stage start. No repository initialized or Git checkout claimed. |
| Inline Python Markdown/local-link and `bash -n` checks | Nine documents passed final-newline, whitespace/fence validation; 146 local links exist and 36 shell blocks have valid syntax. Independent review also verified the demo's eight contiguous slots total 180 seconds. Shell syntax checks are not claims that public-start commands ran. |
| Final Docker resource inventory | No `omni-clean-test-*`, `omni-production-test-*`, or `omni-restore-test-*` fixture containers, networks or volumes remained. Existing application resources were retained. |

Early rehearsal failures were test-fixture issues, not silently counted as passes: the clean-source API probe emits a JSON line followed by plain text, so its parser was corrected; restore assertions were corrected for invalid-login cookie clearing, required mutation versions and a deleted-source replay returning 410. Production privacy validation was broadened to both output streams. Every success above refers to an observed rerun after its correction.

### Remaining limitations and exact manual handoff

- A real Ubuntu host, public DNS/ACME renewal, external firewall reachability, off-site restore, sustained load and actual release rollback have not been exercised. The HTTPS rehearsal used a private CA and private networks. No VPS/domain was bought and nothing was publicly deployed. No new independent OpenAI or Telegram call was initiated.
- S7/S8 Arc desktop evidence remains historical. Phone/light-theme/200%-zoom/whole-card pointer-drag checks and a final complete real-provider demo remain outstanding. The source submission has no Git history; no clean Git checkout, staged diff or repository URL is claimed. The candidate must disclose AI assistance and confirm the recipient's format/rules.
- Snapshots omit later writes and are not continuous PITR. Off-host encryption, scheduling/retention and alerting require operator setup. A restored authentication snapshot must be revoked before exposure. Telegram/provider ambiguity can repeat an external operation; exactly-once external delivery/billing is not promised. Redis AOF every-second persistence is not a guarantee against physical disk loss.
- `whisper-1` remains configurable and has a published February 26, 2027 retirement date; recheck the official provider schedule before deployment. This stage did not change the configured model or existing paid setting.

Manual submission and approved deployment steps:

1. Use the generated `dist/omni-task-submission.tar.gz`, not the workspace directory. In a new empty directory, extract it, `cd omni-task`, and run `shasum -a 256 -c MANIFEST.sha256` on macOS or `sha256sum -c MANIFEST.sha256` on Ubuntu. Inspect its file list/documents; it must contain no `.env`, secret, PDF, recording, database dump or runtime logs.
2. Follow README's clean setup: `python3 scripts/configure_env.py`, `docker compose up --build -d --wait`, `python3 scripts/check_infrastructure.py --runtime`, and `docker compose exec -T api python scripts/queue_smoke.py`. Configure your own private Telegram/provider values only if deliberately testing those integrations. Never start another poller with an already-running token.
3. In Telegram Desktop and Arc, run `docs/DEMO.md` with disposable content: `/profile`, live text, acknowledged queued voice, both status directions, full content, Delete Cancel/Confirm, and pause/resume recovery. Always unpause the worker after interruption. Complete the remaining responsive/theme/zoom/drag visual checks and two-user/logout verification.
4. Read `docs/ARCHITECTURE_WALKTHROUGH.md` and explain the database/queue/notification boundaries, owner scope, source receipts, recovery and external-delivery limits in your own words. Review the final checklist in `docs/REQUIREMENTS.md` before sending the archive.
5. Only after public deployment approval, follow `docs/DEPLOYMENT.md` on the chosen Ubuntu host. Generate protected configuration, keep one polling owner, validate actual TLS/ports and run the listed health/migration/probe checks. Establish encrypted off-host backups, and rehearse `docs/BACKUP_RESTORE.md` against a new target database before relying on recovery. Retain the old release, database and volume through acceptance.

Generate the final handoff archive with `python3 scripts/package_submission.py`. It writes `dist/omni-task-submission.tar.gz` with the 125 allowlisted sources and a per-file manifest, refuses overwrite, and prints the archive SHA-256. The clean-source rehearsal above already exercised packaging/extraction/build; review and verify the final archive again before sharing as specified in the submission checklist.

S9 is complete with the recorded public/manual limitations. Work stopped at submission/deployment preparation; the future public/manual operations are not implicitly authorized.

## Local testing startup follow-up (2026-09-30)

The user requested starting everything locally for their own tests. Docker Desktop was stopped (its daemon socket was absent); `open -a Docker` started it. `docker compose up -d --no-build --wait --wait-timeout 120` exited 0 with all six long-running services healthy and the controlled migration initializer successfully exited. Existing images, credentials, data and volumes were preserved; no new product stage or public deployment was performed.

Actual checks: `python3 scripts/check_infrastructure.py --runtime` passed private database/broker networking and bot credential/dependency isolation; `docker compose exec -T api python scripts/queue_smoke.py` passed via real Redis and a separate worker; `docker compose exec -T worker python -m app.jobs.worker --health` exited 0 for both consumers. The bot's internal `/health/ready` returned `telegram_polling=polling`, and `curl --fail --silent --show-error http://127.0.0.1:8080/api/health/ready` returned `ready`. A boolean-only worker configuration check confirmed OpenAI/key-present/paid-enabled without printing the key or invoking transcription. Git inspection still reports no repository.

Manual entry: in Telegram Desktop send `/profile` to the configured bot, open the fresh dashboard link in Arc on this Mac, then send text or a personally chosen voice recording and check live task delivery/status/deletion. The assistant sent no test message or recording and made no independent paid provider call. The services remain running for the user; work stops at local startup. The previously prepared submission archive remains the immutable S9 handoff snapshot.

## Post-S9 interface refresh — 2026-09-30

Status: **Implemented, tested and running locally. Stopped at the requested interface refresh.** This user-authorized correction changes presentation and bot copy/buttons; it does not authorize new application features or public deployment.

### Changes

- `bot/app/interface.py`: `/start` is the exact supplied welcome; `/help` explains text and voice capture, `/start`, `/help`, `/list`, `/profile`, navigation, statuses, deletion and privacy without the configured voice-limit sentence. Limits remain enforced by existing validation.
- `bot/app/views.py`: text and voice creation receipts show **Delete task** instead of Open task. Delete and Confirm delete use native `style="danger"`; confirmation, ownership, stale-version handling and `/list` detail/full-text navigation remain intact. Voice notifications already reuse these views through the bot's internal transport, so the updated bot image covers both inputs.
- `frontend/src/App.tsx` and `style.css`: compact graphite/ivory workspace, citron actions, clearer three-column lanes, stronger card hierarchy and contrast, visible drop targets and card lift, restrained dialog/toast/button motion, responsive layout and reduced-motion treatment. No sample tasks added.
- New `boardMotion.ts` and regression tests: 320 ms position animation across status-column remounts, pointer-position landing, surrounding-card movement and rollback. Cancellation/replacement, current-animation marker cleanup, resizing, reduced motion and unmount are covered. Existing API/authentication/live-state logic and accessible status selectors remain unchanged.
- Updated `DESIGN.md`, `.impeccable.md`, `SPEC.md`, `README.md`, this log and scope instructions. The existing S9 submission archive is an unchanged historical snapshot; it does not contain this later interface refresh.

### Checks actually run

The already-recorded machine-specific `DOCKER_CONFIG`/`DOCKER_HOST` prefix was used for image builds and the backend test runner; no credential values were printed.

| Command/check | Observed result |
| --- | --- |
| `python3 scripts/test_backend.py -q tests/test_bot.py tests/test_bot_runtime.py tests/test_bot_voice.py` | **88 passed**, 3.25s, exit 0, using the existing real-PostgreSQL runner. Exact welcome/help distinction, red receipt controls, confirmed/canceled/stale-version deletion and voice notification rendering covered. One existing Starlette/httpx deprecation warning. Fake Telegram/provider transports only. |
| `npm --prefix frontend test -- --reporter=dot` | Final run **140 passed in 10 files**, 3.29s, exit 0. Includes 15 new motion tests and existing whole-card drag, task-state, dialog, authentication and realtime regressions. An earlier run passed 138 before the final two marker-lifecycle cases. |
| `npm --prefix frontend run typecheck` | Passed, exit 0, after final motion changes. |
| `npm --prefix frontend run build` | Passed, exit 0; final Vite build processed 35 modules, JavaScript 359.71 kB / 112.72 kB gzip. |
| `.venv/bin/ruff check bot/app/interface.py bot/app/views.py tests/test_bot.py tests/test_bot_voice.py` | Passed, exit 0. |
| `.venv/bin/ruff format --check bot/app/interface.py bot/app/views.py tests/test_bot.py tests/test_bot_voice.py` | Passed; four files already formatted. |
| sRGB contrast calculations against current CSS | Corrected an initial faint muted-text color and reviewer-found count/grip contrast. Final count ratios at least 10.34:1 light / 10.61:1 dark; tinted drop targets at least 9.31/8.76; idle grip 3.62/4.99. These calculations are targeted checks, not a complete WCAG audit. |
| `docker compose build bot frontend`, followed by final `docker compose build frontend` | Passed. Built local production assets and the current bot; no provider request made. |
| `docker compose up -d --no-deps --no-build --wait --wait-timeout 120 bot frontend`, then the same command for final `frontend` only | Passed; refreshed services healthy. API, worker, PostgreSQL, Redis and their data volumes retained. |
| `python3 scripts/check_infrastructure.py --runtime` | Passed HTTP-only bot isolation and private healthy PostgreSQL/Redis; frontend is loopback-only. |
| `docker compose ps --format json` | All six running services healthy. Only frontend publishes host loopback port 8080. |
| Local HTTP asset comparison and Markdown sanity script | Both served JS/CSS assets match the final `frontend/dist` bytes. Six updated Markdown documents passed newline, trailing-whitespace and balanced-code-fence checks. |
| `docker compose exec -T api python -c 'import httpx; r=httpx.get("http://bot:8001/health/ready", timeout=10); r.raise_for_status(); print(r.json())'` | Exit 0, `status: ready`, `telegram_polling: polling`. |
| Arc native visual/accessibility inspection | Observed the redesigned desktop dark board with live connection and existing task counts. Theme toggle and New task dialog responded; accessibility focus moved into the content field. No task was submitted or existing task modified. Arc's active tab changed repeatedly during native automation, including a tool refusal on stale UI state; stopped further input to avoid interfering with other activity. No alternative browser used. |
| Workspace inspection | No Git repository (`git status` exit 128). No commit or clean Git diff claimed. Existing changes retained. |

No real Telegram test message, voice recording, independent paid request, purchase or public deployment occurred. Existing user provider settings were preserved. Both refreshed services remain running for the user's testing.

### Remaining limitations and exact manual acceptance

Native Telegram button rendering, actual pointer drop/landing in Arc, phone/zoom layout, full light-theme visual inspection and OS reduced-motion appearance remain manual checks. DOM tests exercise the relevant state/motion lifecycle but do not prove frame-level browser rendering. Previous Telegram messages are not automatically restyled; after the bot restart use new receipts or `/list` for fresh controls. This stage did not rerun the complete historical S8/S9 recovery/deployment suites because persistence, queues and authentication were unchanged.

1. In the bot's private chat, send `/start`: verify the exact short welcome ending `/help — Inputs and commands`. Send `/help`: verify distinct capability/command help and no configured voice-limit sentence.
2. Send a disposable text task. Check three status buttons and a red **Delete task** button. Tap Delete task → **Cancel**: the task remains. Use `/list` to open it and verify full content. Tap Delete → **Confirm delete** only when ready to remove that disposable task. Old or repeated controls must remain safe.
3. Send `/profile`, open the fresh link in Arc, and reload the dashboard with Cmd+R if an older bundle is open. Expect the compact workspace, three status lanes and Live indicator. Toggle light/dark; reload to verify preference persistence.
4. Create a disposable task. Drag from its title, preview or empty card area into In Progress and Completed: the card lifts, target highlights, and the card settles into place without opening details. Move from Completed back to Pending to check paint order across lanes. Change the same status through the native selector and verify animation, correct counts and saved state.
5. Open details with a normal title click, verify complete content, then close with Escape and verify focus returns. Check new-task input focus, canceled deletion and successful toast dismissal after roughly four seconds. Change status in Telegram and confirm the dashboard updates without reload.
6. Narrow Arc to phone width and test selectors, dialogs and long content; verify no horizontal overflow at 200% zoom. With OS reduced motion enabled, repeat a status move: position/lift effects should be disabled. If desired, test rollback by disconnecting while an update is pending; restore connectivity afterward and verify authoritative state.

No subsequent stage or deployment was started.

## Public-source safety preflight and hosting-plan update — 2026-09-30

Status: **Preflight complete; waiting for the owner to approve the repository name.** The owner permits one public source repository, with future Railway API/bot/worker, GitHub Pages frontend, Neon PostgreSQL and Upstash Redis. This step explicitly stops before project Git initialization or publication. Local Compose, the current UI and running services were not changed.

Changed files: `.gitignore`, `.dockerignore`, new `tests/test_publication_boundaries.py`, `docs/HOSTING_PLAN.md`, and corresponding README/SPEC/AGENTS/task-log updates. No application code, environment credential or database state changed. The hosted workflow is documented as future work, not claimed implemented.

### Inspection and corrections

- The existing rules excluded the current `.env`, PDF, media and runtime directories, but missed other private document/media/key/backup formats and uppercase variants. Both ignore files now cover these, environment shell files, credential stores, database exports, compressed archives and editor backups. Only the two root placeholder environment examples are excepted; nested `.env.example` files are not automatically public.
- The complete path inventory includes dependency/cache files and symlinks. Git's actual matching engine identified **130 prospective source/config/test/documentation files** after these changes. They contain no private binary documents, recordings, logs, dumps, runtime directories or symlinks. Vendor symlinks remain under ignored dependencies. Both root environment examples contain only placeholders or empty external credential fields. `.env` retains mode 0600.
- Active local credential values were compared in memory without printing them. Pattern checks covered OpenAI/Telegram/GitHub/AWS keys, private keys, JWT shapes and credential-bearing URLs. No real credential was found in the prospective public tree. Reviewed URL matches belong to placeholder examples and deliberate synthetic validation/redaction tests. Independent Python-literal/high-entropy/document review found no hardcoded production credential, confidential document reproduction or real transcript in the publishable source. This is a scoped source audit, not a claim that the whole workspace can be uploaded.
- GitHub CLI is authenticated for `Neytrib` over HTTPS. A read-only check for the proposed `Neytrib/omni-task` repository returned 404 at inspection time; no repository was created. No Git history exists in the project, so there are no committed or staged files to inspect yet. The eventual exact index/commit must be scanned again before the first push.
- The actual frontend is `frontend/`, with `npm ci`, `npm run build` and output `frontend/dist`. The new hosting plan records the required base-path, login-root, API/WSS, cross-site session and Railway build changes. Current production cookies are Lax and the frontend uses same-origin transport; this must be addressed and browser-tested before Pages/Railway deployment. Upstash's official Celery integration uses native TLS Redis and warns about idle polling costs. No managed resources were provisioned.

### Commands and checks actually run

| Check | Observed result |
| --- | --- |
| `git status --short` | Exit 128: project is not a Git repository. No root `.git` created. |
| `gh auth status --active --json hosts` and read-only `gh api repos/Neytrib/omni-task --jq .full_name` | Authenticated account confirmed; proposed repository not found. Outputs were filtered to omit credentials. |
| Local `python3 tmp/public_preflight.py` | Final scan passed for 130 prospective public files, zero active-local-credential matches and zero binary candidates. Git matching used a disposable temporary repository with this directory as a read-only work tree; no application Git index/history exists. Report and path-only manifest remain in ignored `tmp/`. Initial scanner falsely included commented example values; corrected to read active assignments before the successful rerun. |
| Independent source/package audit | Initial 128 first-party files reviewed (before new test/plan documents); four active local credentials matched zero source files. Existing `collect_sources()` guard passed. Final root scan includes the additions. |
| `.venv/bin/pytest -q tests/test_publication_boundaries.py tests/test_submission.py` | **12 passed**, 0.06s, exit 0. Real Git ignore matching against synthetic private/public paths plus existing package exclusion/credential tests. One existing Starlette/httpx deprecation warning. No database, Telegram or provider call. |
| `.venv/bin/ruff check --fix tests/test_publication_boundaries.py` and `.venv/bin/ruff format --check tests/test_publication_boundaries.py` | Initial import-order finding corrected; final checks pass. |
| Local Docker `FROM scratch` / `COPY . /context/` export with `--network=none` | Actual Docker build context excludes `.env`, data, temporary files, dependencies and generated outputs; both placeholder examples and all service source remain present. Temporary export removed; no app image/container replaced or public upload. |

The ignore semantics were checked against official Git documentation; hosting references are linked in the plan. The full application suite was not rerun because this stage changes exclusion rules, a regression test and documentation only. No live security scanner SaaS or external file upload was used; known-secret/pattern/manual review is not an absolute guarantee against arbitrary future secrets. Do not force-add ignored files.

### First manual action and stop boundary

Ask the owner to approve **`Neytrib/omni-task` as the one public repository name** (or provide another name), then wait. Authenticated tooling is available, so no manual Git command or GitHub UI setup is needed now. After the owner continues, inspect the current files again, initialize `main`, stage the reviewed set, rescan exact staged bytes, commit, create the approved public repository, verify `origin`, and push. Do not infer that this preflight also approves paid resource creation or independently paid transcription tests.

## Public GitHub repository publication — 2026-09-30

Status: **Complete.** The owner approved the name and continuation. The one public source repository is [Neytrib/omni-task](https://github.com/Neytrib/omni-task), with `main` as the default branch and local `origin` set to its HTTPS Git URL. The initial source commit is `6770a24da0e5b09ffcec4dd64c79d08294100f7e`. Cloud application deployment remains a separate next step.

### Changes and verified safety gate

- Re-read project instructions and inspected current source/authentication state. The account was still authenticated, the approved repository did not yet exist, and the project had no Git history at entry.
- Repeated the 130-file pre-initialization scan, initialized `main`, and staged only the explicit reviewed manifest using literal NUL-separated paths. No wildcard staging, force-add, private artifact, generated archive or source document was included.
- Configured this repository's author/committer with the public account name and GitHub ID-based no-reply email. Global Git identity remained unchanged. No custom or active local Git hooks were present.
- Inspected exact index blobs, file modes and content rather than only working-tree files. Root and independent reviews found zero real credential matches and no private paths, binaries or symlinks. Four active local credential values were compared in memory. Five URL matches in the root scanner (six detections with the independent scanner's patterns) were reviewed placeholders/synthetic security-test fixtures. Both root environment examples contain placeholders or empty credentials. All 562 lockfile URLs passed the independent embedded-credential/query check.
- The reviewed 130-file index contained 1,297,989 bytes before the publication-evidence update. Its `git ls-files --stage -z` SHA-256 was `b62e2eddbf9a1d782bfc757866b718d614f188d5f0472811cb1756af4538e861`; it was unchanged immediately before the initial commit. No credential value was printed or sent to an external scanner.
- Created the approved public repository, verified the exact `origin` push destination and visibility, scanned again immediately before the first push, and pushed only `main`. GitHub's first remote tree exactly matched the 130 reviewed local source paths and initial commit. `.env`, data, temporary logs and generated archives remain local and ignored.
- README now provides an actual clone-based setup. Scope/hosting documents record source publication separately from an eventual public application. No application logic, running service, local credential, database or user data changed.

### Commands and results

| Executed check/action | Observed result |
| --- | --- |
| `gh auth status --active --json hosts`, `gh api user`, target repository read | Authenticated owner confirmed using filtered metadata; target was absent before creation. No tokens printed. |
| `python3 tmp/public_preflight.py` | Passed for 130 candidate files before initialization, with no active-secret match. Local output remains ignored. |
| `.venv/bin/pytest -q tests/test_publication_boundaries.py tests/test_submission.py` | **12 passed**, 0.06s, exit 0; one existing Starlette/httpx deprecation warning. No external integration call. |
| `git init -b main`, local identity configuration, explicit-manifest `git add --pathspec-from-file=- --pathspec-file-nul` | Passed. Only reviewed source staged; private no-reply commit identity verified. |
| `git diff --cached --check`, `git check-ignore` on `.env`/dump/audit/archive paths, `python3 tmp/staged_publication_audit.py` | Passed; exact index checked both before commit and before first push. Independent exact-index audit also passed. |
| `git commit --quiet -m "feat: initialize Omni Task application"` | Created initial commit `6770a24`; clean working tree afterward. |
| `gh repo create Neytrib/omni-task --public --source=. --remote=origin --description "Private Telegram task manager with voice transcription and a live Kanban dashboard."` | Created exactly the approved public repository; no automatic README/license/second repository or deployment. |
| `GIT_TERMINAL_PROMPT=0 git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push -u origin main` | Succeeded; local `main` tracks `origin/main`. Existing GitHub authentication was passed through the credential protocol, not embedded in the URL. |
| `git ls-remote origin refs/heads/main` and GitHub repository/tree API reads | Verified identical initial local/remote commit, public visibility, default branch `main`, and all 130 reviewed source files with no private artifact. |

The full application suite was not rerun: application code is unchanged and prior stage results remain historical evidence. The root safety helper/manifest are ignored local audit tools; publication-boundary regression tests are included in source. The final documentation-only evidence update is separately reviewed, scanned, committed and pushed through the same safety gate.

### Remaining work and manual verification

Open `https://github.com/Neytrib/omni-task` and verify the public badge, `main`, README, all service source directories and placeholder environment examples. Local `git status --short --branch` should show a clean tracked branch. Actual `.env`, confidential PDFs, recordings, logs and dumps must never be added later, including with force-add.

GitHub Actions/Pages configuration, Railway production builds/private routing, cross-site authentication, Neon/Upstash connections and a managed-hosting acceptance test are not yet implemented or deployed. No second repository, Cloudflare Pages, paid resource, independent OpenAI call or new polling process was created. Work stops after source publication.

## Evidence template for each implementation stage

- Requested stage and date:
- Changed files and observable behavior:
- Official dependency/API references verified:
- Automated checks actually run (exact commands, result/exit code, relevant counts):
- Manual checks actually run (Arc where applicable, expected and observed behavior):
- Skipped/blocked checks and reason; paid/public actions approved or unperformed:
- Remaining limitations and next eligible stage:
- Stage status and confirmation that work stopped at its boundary:

## Managed hosting setup — 2026-09-30

Status: **Cloud infrastructure deployed and verified; Telegram/OpenAI activation remains intentionally disabled pending the owner's keys.** Pages, API, worker and the private bot adapter run remotely. The owner authorized completing the one-repository Railway/Pages/Neon/Upstash setup, approved Railway CLI authentication, and personally upgraded Railway to Hobby. Source inspection and existing behavior were preserved; no application feature or schema migration was added. The entries below retain chronological checkpoints; the latest handoff evidence supersedes earlier setup blockers.

### Changes and checks actually executed

| Check | Observed result |
| --- | --- |
| `npm --prefix frontend test -- --reporter=dot` | 172 passed, including hosted URLs/base, blocked-cookie handling and safe public build settings. |
| `npm --prefix frontend run typecheck`; local and synthetic Pages `npm run build` | Passed. `/omni-task/` assets, exact HTTPS/WSS CSP and no-referrer inspected; unapproved VITE sentinel excluded; missing API Pages build correctly rejected. Local output restored. |
| `DOCKER_CONFIG=.../tmp/s7-docker-config DOCKER_HOST=unix:///Users/neytrib/.docker/run/docker.sock python3 scripts/test_backend.py -q` | 452 passed in 64.75s with isolated PostgreSQL, real Redis and separate worker processes. Initial test-context failure (missing Dockerfile) fixed and complete suite rerun. Existing user data untouched. |
| Rebuilt Docker test image, focused `tests/test_railway_bot_deploy.py` | 40 passed, after those tests were added. Fake CLI only: no real bot restarted. |
| `.venv/bin/pytest -q tests/test_hosting_setup.py tests/test_publication_boundaries.py tests/test_submission.py tests/test_railway_bot_deploy.py` | 56 passed, exit 0. Existing Starlette/httpx deprecation warning remains. |
| `npm ci --prefix .railway --ignore-scripts`; `npm --prefix .railway run check`; `npm --prefix .railway test` | Locked SDK install/type checking passed; 4 configuration contract tests passed. |
| Build `deploy/railway/backend.Dockerfile` and `deploy/railway/bot.Dockerfile` | Both passed. Actual Linux container checks confirmed UID 10001, IPv4/IPv6 listening, and no database/Redis/Celery/backend modules in bot image. |
| Ruff checks/format checks and `git diff --check` | Passed for changed Python/source. |
| `python3 scripts/configure_hosting.py` | Created three ignored files, directory mode 700/files 600, shared generated internal credential; no local `.env` copied. A Python 3.9 incompatibility in the new helper was corrected before successful execution and regression tests. |
| Railway CLI `whoami`, project/service creation, status, sanitized plan and account checks | Authentication succeeded. Created empty `omni-task` project and API/bot/worker shells. Account verified Hobby. IaC plan succeeded; not applied while database/provider setup is incomplete. |
| GitHub Pages API and repository variable setup | Enabled workflow-based Pages on the existing repository and set its public API origin. Workflow has not yet run at this checkpoint. |
| Arc account/setup inspection | Confirmed Railway Hobby and existing new Neon `omni-task` Free project in Ohio. Native input was intermittent; user-assisted account/credential entry required. No hosted UI acceptance claimed. |

Cloud project: `06507807-7826-4081-beef-a493a2fd73e4`; production environment `1117bb4b-3bcc-4b93-bcf1-e87718dd55b0`. API domain reserved: `https://api-production-08eb2.up.railway.app`; it is not a healthy deployed API yet. API internal credential was sent through CLI stdin with deployment disabled, never printed. Bot and worker have no public domain. No polling token or OpenAI key was transferred; local services/settings remain unchanged. No independent paid transcription was run.

Neon follow-up: the owner supplied the connection string only in the ignored private API file. Its direct endpoint was verified with a real TLS connection: PostgreSQL 18.6, zero public tables. The verified URL was saved to the private API/worker files without printing it. No migration or user-data copy has run yet.

### Remaining acceptance / continuation

Complete private Upstash credentials and bot/worker external key placeholders, verify TLS connectivity and resource quotas, run one explicit migration, review/apply IaC and source connections, stop local polling before starting the same token in Railway, configure the scoped Actions deployment secret and verify workflows. Then use the exact Arc walkthrough in `docs/HOSTING_PLAN.md`: fresh `/profile` login/token removal/reload, two live tabs, Telegram text/status, complete content/deletion, disconnect/reconnect, logout/expiry/reuse and second-user isolation. Voice testing requires the owner's deliberate paid setting. Managed restore and provider-specific worker redelivery remain unverified; passing local tests is not evidence that those cloud checks passed. Do not claim exactly-once notification delivery or indefinitely free hosting.

Cloud continuation: source commit `83fdfd4` was scanned as 155 exact staged UTF-8 files (zero active-secret matches; 13 reviewed synthetic/placeholder URL findings), committed and pushed. Pages workflow `36730700776` completed SUCCESS; the deployed page and repository-base JS/CSS returned HTTP 200. Bot workflow `36730700619` correctly skipped deployment because its Railway Actions secret was absent (overall workflow success is not bot deployment evidence). The owner connected the Railway GitHub app and chose to leave Telegram/OpenAI keys blank. Neon migration was explicitly run against the verified empty database and reached `0003_live_outbox` with 9 public tables. Upstash certificate/hostname-verified PING, Pub/Sub, transaction and Lua checks passed using a unique synthetic key, removed afterward. Protected API/bot/worker variables were imported with `skipDeploys`; the reviewed IaC plan had 12 non-destructive changes and was applied. API and worker builds then started; bot deployment still needs the project-scoped Actions token. Initial IaC planning proposed deleting API’s disabled paid flag; the definition was corrected to preserve it and its four contract tests passed again. Publication scanner now also checks the private deployment-token file; 15 targeted submission/boundary tests passed. No Telegram token/OpenAI key was transferred, no local process was stopped, and no paid provider test ran.

Deployment follow-up: Pages also succeeded for `6fd5070`. API deployment `d9f856a4-4c3e-4a23-bf4e-be14a5ea04b4` reached SUCCESS; real HTTPS readiness returned 200, unauthenticated session/tasks returned 401, trusted Pages CORS/preflight passed, untrusted origins received no allow-origin header, and unauthenticated WSS was rejected with 403. Initial bot deployment `0d7ddbad-e125-4f60-ba39-aedeba9f1464` reached SUCCESS with polling disabled, but workflow `36732646147` failed during a CLI call; its old generic error cannot establish the exact failing phase. The guard now logs safe phase/exit metadata and the validated deployment ID, retries only read-only status failures at most three times, and never retries upload/stop mutations. All 44 deployment-guard tests passed. The worker's initial queue reason was “Waiting for dependencies to deploy,” not a failed build. IaC's apparent restart/sleep drift was checked against actual service configuration: all three have ON_FAILURE, five restart retries and sleep disabled; the planner omits default values in its current-state representation. No repeated corrective apply was made.

### Hosted deployment and handoff evidence

- The owner supplied a production-scoped Railway project token in the ignored mode-0600 deployment file. Its project/environment scope was verified before storing it as the encrypted `RAILWAY_TOKEN` GitHub Actions secret. No credential value was printed or committed.
- The initial IaC apply stored API/worker source metadata but created no native deployment triggers. Explicit source connections first failed because Railway's authenticated account had no GitHub identity, despite GitHub App installation. The owner linked `Neytrib` in Account Integrations; both `railway service source connect` commands then succeeded. Documentation now includes this separate prerequisite and verification step.
- API trigger `6dcba173-79d7-45b0-92f4-4b79672161f5` and worker trigger `cfc73f29-4d37-4892-ad06-231f38846be7` were verified for `Neytrib/omni-task`, `main`, with check-suite waiting disabled. Bot has zero native triggers. Source connection produced successful API deployment `2218becf-36d0-464a-b1d3-261ef2e255d4` and worker deployment `9b081850-666e-455b-a39a-d28c31118fb0`, both at `919adb7502d0cf690e4f1009df4b130c4cd22ae0`. This replaced the stalled initial dependency batch. It is source-connection deployment evidence; a later matching push remains a separate autodeploy check.
- One harmless `foundation.probe` job traversed native Upstash TLS to the separate Railway worker and returned the expected nonce from container `5833cc36f0d5`, PID 5. Its synthetic result key was removed. Celery inspection confirmed the transcription consumer on `transcription` with pool size 2 and the notification consumer on `notifications,foundation` with pool size 1. No recording, Telegram send or OpenAI call was involved.
- Bot workflow `36733448939` attempt 1 failed at the stop command (exit 1). Read-back showed the prior bot still SUCCESS and no replacement, so the guard had prevented upload after an uncertain stop. Railway subsequently displayed an incident notice about slow dashboard/auth responses; independent control-plane requests also returned intermittent 503/non-JSON responses. These observations do not establish the precise cause of that stop failure. After read-back, attempt 2 succeeded: old deployment `0d7ddbad-e125-4f60-ba39-aedeba9f1464` moved through REMOVING to REMOVED before replacement `fde9a569-9623-4f9e-8d3b-148927452f51` was built and verified SUCCESS. Polling stayed disabled.
- Pages run `36733448756` at `919adb7` completed dependency installation, 172 frontend tests, type checking, build, artifact upload and deployment. Independent HTTP checks returned 200 for the root and JS/CSS, with correct `/omni-task/` asset paths, no-referrer and the exact configured HTTPS/WSS CSP. Native Arc inspection subsequently verified the actual signed-out screen and `/profile` instructions; existing tabs and profiles were preserved.
- The current API passed live readiness 200, unauthenticated session/tasks 401, unauthenticated WSS 403, trusted credentialed CORS and preflight, and rejection of an untrusted origin. A further `.venv/bin/pytest -q tests/test_publication_boundaries.py tests/test_submission.py` run passed all 15 tests; `git diff --check` passed. Existing Starlette/httpx deprecation warning remains.
- `.venv/bin/python tmp/hosted_wire_acceptance.py` passed 11 live HTTPS/WSS check groups using two newly created synthetic users against Railway/Neon/Upstash. Checks covered login GET/HEAD nonconsumption, reused/expired links, secure cookie headers, CSRF and origin rejection, bot-only routes, duplicate source messages, complete long content, two owner sockets, second-user API/socket isolation, created/updated/deleted events, logout revocation and session expiry (existing sockets closed with 4401). The script verified both IDs were absent before starting and removed only these users and their cascading fixture records afterward. No processing request, external notification or provider call was created. This protocol test does not itself prove browser cookie behavior.
- A separate disposable browser fixture verified the actual Pages/Railway flow in Arc: the single-use link exchanged successfully, the token disappeared, the board showed Live, an internal API status change moved its card from Pending to In Progress without reload, and clean-URL reload retained the session and correct task. Full content was inspected in details. The keyboard status selector saved Completed; the authenticated internal API independently confirmed version 3. Logout returned to the signed-out `/profile` instructions. This proves the current Arc configuration's hosted session and live transport, not universal browser support or real Telegram polling. No personal content or external provider credential was used.
- Browser fixture preparation/cleanup used `.venv/bin/python tmp/hosted_browser_fixture.py prepare` and `cleanup`, plus scoped authenticated internal-API checks. Cleanup verified the synthetic session's persisted revocation, removed exactly that new fixture user and cascades, and removed its private HTML/state files. All three acceptance-test users were removed; existing user data was untouched.

External Telegram/OpenAI credentials remain blank in cloud configuration and `ALLOW_PAID_TRANSCRIPTION=false`. Local `.env`, polling/worker processes and user data remain unchanged. Initial managed migration is complete; do not repeat provisioning or copy local data. The remaining activation starts with filling the protected bot/worker placeholders, then coordinating a stop of the local poller/worker before enabling the same bot token remotely. Bot variable updates must skip deployment and then use the serialized workflow. Managed restore, real hosted Telegram/voice flows and provider-specific crash/redelivery remain unverified; no exactly-once notification claim is made.

## Managed integration activation — 2026-09-30

The owner subsequently filled the protected bot/worker files, selected `ALLOW_PAID_TRANSCRIPTION=true`, and replied “filled” to activate Railway. This supersedes the blank-key handoff checkpoint above. No new application feature, migration, data transfer or independent paid test is authorized by this action.

- Private files passed mode-0600, ignored-path, credential-format and matching internal-key checks without printing values. A read-only Telegram `getMe` request verified the supplied token belongs to `omni_task_manager_bot`; it sent no message. The OpenAI key was checked locally for presence/format only; no provider API request was made.
- Aggregate database checks found zero queued/processing voice requests and zero pending/delivering notifications both before and after `DOCKER_HOST=unix:///Users/neytrib/.docker/run/docker.sock docker compose stop -t 40 bot worker`. Compose confirmed both exited. API, frontend, PostgreSQL and Redis stayed healthy; existing local records and `.env` were preserved.
- `python3 tmp/activate_railway_keys.py` asserted the local processes were stopped, then sent only the worker's OpenAI key/paid flag and bot's Telegram token to their exact production services using the GraphQL variable collection API over stdin. Both updates succeeded with `replace=false, skipDeploys=true`. Credentials never entered shell arguments, command output, source or frontend artifacts.
- Scoped `railway redeploy --from-source --yes --json` for worker produced deployment `913976e8-00f1-45ef-b1c1-4db18c789544`, SUCCESS at source `a44d74d083949915c246ab035b63483e5744a947`; the previous worker was removed. Bot activation was dispatched through `gh workflow run railway-bot.yml --repo Neytrib/omni-task --ref main`, run `36743007749`, retaining the stop-before-start guard.

Runtime verification passed: both consumers in new worker container `91ea50411b32` answered Redis control pings, with the expected `transcription` and `notifications,foundation` queues. Protected Railway configuration matched the private OpenAI key, enabled paid flag, provider and model; only equality/presence booleans were emitted. Bot workflow `36743007749` passed and exact replacement `7cbbd119-c26c-4b70-89c2-0a16db600004` reached SUCCESS after the previous deployment was removed. Its nonempty configured Telegram token matched the private file. The successful deployment readiness check supports active polling at startup: with a nonblank token, the code cannot enter `disabled`, and starting/retrying/error states return 503. Direct container HTTP inspection was unavailable because no Railway SSH key is registered; no new SSH access was added. Captured, bounded deployment logs contained no recognized polling/configuration error events. No Celery task or OpenAI request was initiated for these activation checks.

`git diff --check` and `.venv/bin/pytest -q tests/test_publication_boundaries.py tests/test_submission.py` passed (15 tests, one existing Starlette/httpx deprecation warning). Application code is unchanged; existing integration results remain historical evidence, and real provider billing/quota/transcription is deliberately left to the owner's voice test.

Manual acceptance: send `/start` and `/profile` to `@omni_task_manager_bot`; open the fresh dashboard link in Arc and verify the `neytrib.github.io/omni-task/` address. Send a text task, observe it live, change status from both interfaces, inspect full details and confirm deletion of that test task. If choosing to exercise the enabled paid voice setting, send a new short recording and verify quick acknowledgement, one task and a final result. The assistant submitted no voice recording. Existing local tasks remain in local PostgreSQL; the hosted board uses the separate Neon dataset. Do not start another local poller with the production token. Stop after activation and runtime verification; managed restore and real hosted Telegram/voice acceptance are not claimed complete.
