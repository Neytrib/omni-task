# One-repository managed hosting

Status on 2026-09-30: reviewed source is public at [Neytrib/omni-task](https://github.com/Neytrib/omni-task). **Pages and all Railway services are live; full manual product acceptance remains incomplete.** [Pages run 36767552673](https://github.com/Neytrib/omni-task/actions/runs/36767552673) completed successfully for security release `b7600e0a7e7f4287a485707dfe60682a420a45bd`; the Pages root and compiled assets return HTTP 200 with the repository base, configured CSP and no-referrer policy. [API readiness](https://api-production-08eb2.up.railway.app/api/health/ready) returns HTTP 200 and `status: ready`.

The owner-approved Railway Hobby [project](https://railway.com/project/06507807-7826-4081-beef-a493a2fd73e4) contains `api`, `bot`, `worker` services in `production`. Neon direct TLS and schema revision `0004_voice_admission_index` were verified after a private PostgreSQL 18 backup; Upstash native TLS, Pub/Sub, transactions and Lua checks passed, with the synthetic key removed. The owner supplied Telegram/OpenAI credentials privately and explicitly enabled `ALLOW_PAID_TRANSCRIPTION=true`. API and worker native deployments reached `SUCCESS` on `b7600e0`. The quota-aware bot was deployed first through [serialized compatibility run 36767078482](https://github.com/Neytrib/omni-task/actions/runs/36767078482), before the quota API release. Actual hosted Telegram/voice acceptance remains for the owner; no independent paid transcription test was performed. Local bot and worker were stopped with no pending voice/notification work before activation; local credentials and database data are preserved and were not migrated. The owner linked GitHub, and both API/worker source triggers are now configured for `Neytrib/omni-task` on `main`. Their matching-push deployments are verified at security release `b7600e0`. A harmless job passed through real Upstash to the separate Railway worker; both transcription (pool 2) and notifications/foundation (pool 1) consumers were inspected, and the synthetic result key was removed. The bot Actions secret is configured and it has no native source trigger. Arc login, token removal, session reload, live status synchronization, a browser status mutation and logout passed with a disposable account. Managed restore remains unverified. [TASKS.md](../TASKS.md) records detailed evidence.

## Where each part runs

| Destination | Deployment |
| --- | --- |
| One public GitHub repository | All reviewed application source, Dockerfiles, Compose, tests, documentation and Actions |
| GitHub Pages | Only `frontend/dist`; deployed URL `https://neytrib.github.io/omni-task/` |
| Railway `api` | FastAPI HTTPS/WSS, private bot API and durable dispatch |
| Railway `bot` | One polling process and its private voice/notification HTTP adapter |
| Railway `worker` | Supervised transcription and separate notification/foundation consumers |
| Neon | PostgreSQL: tasks, ownership, sessions, deduplication and durable work records |
| Upstash | Native TLS Redis: Celery broker, short-lived results and separate live Pub/Sub |

No second frontend repository or Cloudflare Pages is used. Once accepted, the managed system runs without the developer's computer. Local Compose and the previously verified [Ubuntu runbook](DEPLOYMENT.md) remain independent and unchanged. Existing local data has not been uploaded.

## Fill private configuration

```sh
python3 scripts/configure_hosting.py
```

From the project root, this creates ignored `private/railway/api.env`, `bot.env` and `worker.env` with mode 0600 in mode-0700 directories. It generates one matching `BOT_API_KEY`, preserves an existing complete set, refuses a partial set rather than overwriting it, and never reads local `.env`. Fill the private copies; public [templates](../deploy/railway) stay placeholders.

| Setting | Service | Fill with |
| --- | --- | --- |
| `DATABASE_URL` | API and worker | Neon **direct** PostgreSQL URL with `postgresql+psycopg://` and TLS |
| `REDIS_URL` | API and worker | Native Upstash `rediss://` URL, database `/0`, verified TLS |
| `BOT_API_KEY` | All three | Already generated; keep matching and server-only |
| `TELEGRAM_BOT_TOKEN` | Bot only | BotFather token; leave blank until the previous local poller is stopped |
| `OPENAI_API_KEY` | Worker only | Owner's OpenAI API project key |
| `TRANSCRIPTION_PROVIDER`, `TRANSCRIPTION_MODEL` | API and worker | Matching provider/model; templates use `openai` and the configurable model |
| `ALLOW_PAID_TRANSCRIPTION` | Worker | Templates keep `false`; enable only for approved owner voice use |
| `BOT_IDENTITY` | API and worker | Stable production namespace; preserve across token rotation |

Templates include the Pages URL/origin, secure partitioned cookies, queue prefix and live channel. Railway references supply private API/bot URLs. The bot receives no PostgreSQL/Redis credentials. Import each private copy into its service's protected **Variables → Raw Editor**; never paste credentials into chat, command arguments or public GitHub variables.

For Neon, disable **Connection pooling** in its connection dialog to obtain the direct hostname, without `-pooler`. Preserve URL escaping and TLS options; change only the scheme from `postgresql://` to `postgresql+psycopg://`. Direct connections support migration advisory locks and avoid transaction-pooler session restrictions. Monitor the selected compute's connection limit; application pools are per process. The application's continuous dispatch/recovery queries keep PostgreSQL active, so do not budget as though the free compute will normally sleep while services run. [Neon connection strings](https://neon.com/docs/connect/connect-from-any-app), [direct versus pooled connections](https://neon.com/docs/connect/connection-pooling).

For Upstash, use native TCP credentials, **not** its HTTPS REST endpoint/token pair. Keep `ssl_cert_reqs=required&ssl_check_hostname=true`; the application rejects disabled TLS verification. The database must support native blocking queue operations, transactions/Lua and Pub/Sub, with enough connection and command capacity for the API and both worker consumers. Use database 0 and keep eviction disabled. Broker/results use `CELERY_BROKER_KEY_PREFIX`; `LIVE_CHANNEL` is separate. PostgreSQL retains accepted work during broker failures. [Native compatibility](https://upstash.com/docs/redis/overall/compatibility), [eviction](https://upstash.com/docs/redis/features/eviction), [durability](https://upstash.com/docs/redis/features/durability).

Celery polls even while idle; a free/trial quota does not promise indefinitely free operation. Review usage limits before enabling services. No paid upgrade is implicit. The managed queue probe and live Pub/Sub checks passed; provider-specific crash/redelivery still requires acceptance. [Upstash Celery support and polling costs](https://upstash.com/docs/redis/integrations/celery).

## First start, in order

The database/service bootstrap below is retained for reproducibility; its completed operations do not need repeating in the current environment. The private integration credentials are now configured; finish the remaining manual acceptance steps instead.

1. For a fresh environment, fill the private Neon direct connection string and owner-controlled Upstash connection. These and the owner-supplied Telegram/OpenAI credentials are configured in the current environment. The owner explicitly enabled cloud voice transcription; this does not authorize further purchases or independent paid transcription tests.
2. Use the **existing** `api`, `bot`, `worker` services in `production`; do not create duplicates. Keep the Telegram token blank until the old poller is stopped. On a fresh setup, import private variables and complete the first migration before attaching deployment sources. Service shells let the CLI retrieve API variables before an API container exists.
3. Review migrations and back up any nonempty database. From the reviewed checkout, execute one controlled migration against the explicit API environment:

   ```sh
   uv sync --locked
   railway run --project 06507807-7826-4081-beef-a493a2fd73e4 --environment production --service api --no-local -- uv run --locked alembic current
   railway run --project 06507807-7826-4081-beef-a493a2fd73e4 --environment production --service api --no-local -- uv run --locked alembic upgrade head
   railway run --project 06507807-7826-4081-beef-a493a2fd73e4 --environment production --service api --no-local -- uv run --locked alembic current
   ```

   `railway run` executes locally with protected service variables. `--no-local` prevents Compose development overrides; no database URL enters command history. The current database's migration completed at `0004_voice_admission_index`; check the current revision before any future schema operation.
4. Validate the locked Railway authoring SDK and review/apply the configuration:

   ```sh
   npm ci --prefix .railway
   npm --prefix .railway run check
   npm --prefix .railway test
   railway link --project 06507807-7826-4081-beef-a493a2fd73e4 --environment production
   railway config plan
   railway config apply
   ```

   Verify the selected project/environment and exact redacted plan before applying. Never use variable-decryption/show-value options or commit plans. [.railway/railway.ts](../.railway/railway.ts) uses SDK 3.12.0 and CLI 5.49.1+, preserves filled variables, specifies production Dockerfiles, and has no migration startup hook. New services use the current TypeScript IaC, not legacy `railway.json`/`railway.toml`. [Railway IaC](https://docs.railway.com/infrastructure-as-code).

   After IaC, explicitly connect the API and worker GitHub sources. First link the owner's GitHub identity under Railway account settings, and grant the Railway GitHub App access to `Neytrib/omni-task`; these are distinct requirements. These prerequisites and source connections are complete in the current environment. For a fresh environment, run:

   ```sh
   railway service source connect --project 06507807-7826-4081-beef-a493a2fd73e4 --environment production --service api --repo Neytrib/omni-task --branch main
   railway service source connect --project 06507807-7826-4081-beef-a493a2fd73e4 --environment production --service worker --repo Neytrib/omni-task --branch main
   ```

   Connecting can start a deployment. Verify each service's source is this repository on `main`, its production Dockerfile/start settings remain correct, and its native trigger exists. Verify that a subsequent matching source push creates a deployment. Do not infer working autodeploy from the `source` declaration in the IaC file alone. **Never connect the bot this way**; it uses its serialized workflow.
5. The API is live at `https://api-production-08eb2.up.railway.app`, targeting port 8000; its readiness endpoint passed HTTP 200. Keep bot and worker private. Both worker consumers and the real managed queue probe passed; full browser/Telegram acceptance remains separate. The server binds both public IPv4 and private IPv6 traffic.
6. For a fresh activation, run `docker compose stop bot worker` before setting that same Telegram token on Railway. In the current environment, local bot and worker were stopped with no pending voice/notification work before import; both remain stopped and their credentials/data are preserved. The cloud bot activation used the serialized workflow below. Use a different development token before restarting a local bot, and inspect any local queued work before restarting the local worker.
7. Pages and the intended integrations are configured and deployed. Complete the remaining live acceptance below using `/profile`, a text task and the owner's short voice recording.

## Automatic deployments

API/worker native GitHub triggers are configured for `Neytrib/omni-task` on `main`, with relevant source watch patterns. The deployments initiated by source connection succeeded. The docs-only `a44d74d` push produced `SKIPPED` API/worker deployments under their watch patterns; a subsequent source-matching pushed build has not yet been observed. These triggers do not wait for GitHub check suites, so complete applicable tests before pushing. [The bot workflow](../.github/workflows/railway-bot.yml) uses [a guarded deployment script](../scripts/deploy_railway_bot.py) to stop the previous deployment and wait for termination before uploading its replacement. **Do not also enable native GitHub autodeploys for bot:** one replica and zero rolling overlap alone do not guarantee one polling process during replacement. This intentionally introduces a brief bot outage. Never start a manual Railway bot deployment while this workflow runs.

The bot workflow uses repository variables `RAILWAY_PROJECT_ID=06507807-7826-4081-beef-a493a2fd73e4`, `RAILWAY_ENVIRONMENT_ID=1117bb4b-3bcc-4b93-bcf1-e87718dd55b0`, and `RAILWAY_BOT_SERVICE_ID=e7182f5b-ae55-456b-a267-82d677c46f69`. Its production-environment token is already stored as the encrypted **Actions secret** `RAILWAY_TOKEN`. Missing configuration skips deployment with a clear summary; use **Actions → Deploy Telegram bot → Run workflow → main** for controlled replacement. A failed/interrupted stop-before-start run can leave the bot stopped; inspect Railway before retrying.

[The Pages workflow](../.github/workflows/pages.yml) runs `npm ci`, tests, type checking and `npm run build` in `frontend/`; it uploads **only** `frontend/dist` using pinned official Pages actions. No backend secret enters its build. It never commits generated files or triggers another deployment workflow.

The same repository's **Settings → Pages → GitHub Actions** source is already enabled. Under **Settings → Secrets and variables → Actions → Variables**, verify:

| Public variable | Value |
| --- | --- |
| `PUBLIC_API_ORIGIN` | Already set to `https://api-production-08eb2.up.railway.app`; update only if the API domain changes |
| `PUBLIC_WS_ORIGIN` | Optional `wss://` origin on the same API hostname; blank derives it from API |

The workflow passes these as `VITE_API_ORIGIN`/`VITE_WS_ORIGIN` and sets `VITE_BASE_PATH=/omni-task/`. Missing API configuration produces an explicit summary and skips deployment while local frontend checks/build still run. Invalid origins fail the build. Once configured, use **Actions → Frontend · GitHub Pages → Run workflow → main**; subsequent `main` pushes update Pages automatically.

There is no client router or path-based task screen. The dashboard and login open at `https://neytrib.github.io/omni-task/`; the login fragment is removed before requests while retaining that pathname. Root refresh/direct navigation works on static Pages without a 404 redirect or login subdirectory. Local `/` and `/login#token=...` remain supported.

## Authentication and live acceptance

Hosted sessions use host-only HttpOnly `__Host-omni_session` cookies with `Secure; SameSite=None; Partitioned`. Exact credentialed CORS, CSRF/Origin checks, POST-only single-use exchange, WebSocket authentication and expiry/revocation remain enforced. No permanent credential enters browser localStorage or WSS URLs. After exchange, a session GET verifies the cookie; failure explains possible blocked/unsupported cookies or expiry instead of displaying false success.

**Real Pages → Railway authentication and WSS behavior passed in Arc on 2026-09-30:** token removal, reload with the session retained, live remote status movement, full details, a keyboard status change visible to the internal API, and logout. The disposable fixture was isolated from real users. Partitioned cookies require browser support and compatible privacy settings; universal support is not claimed. All Pages projects under `neytrib.github.io` share a browser origin: treat other published projects on that origin as trusted code, because CORS cannot isolate `/omni-task/` from another path. Pages serves a narrow CSP and no-referrer meta policy, but cannot provide our custom response headers: `frame-ancestors` cannot be enforced through a CSP meta tag. Compose nginx retains its existing frame protection. [Partitioned cookies](https://developer.mozilla.org/en-US/docs/Web/Privacy/Guides/Third-party_cookies/Partitioned_cookies), [CSP delivery limits](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy).

1. Send `/profile`, open its fresh Pages-root link in Arc, verify token removal, the correct private board and Live state. Reload the clean URL and confirm the session survives.
2. Open a second tab. Create one disposable Telegram text task, change status from Telegram and dashboard, inspect complete content and confirm deletion. Both tabs must synchronize without refreshing.
3. Disconnect one tab, change state elsewhere and reconnect. Verify authoritative recovery. Test logout across tabs, reused/expired links and a second user's isolated session.
4. The owner has explicitly enabled cloud voice transcription. For the owner's manual test, send one short recording and verify the quick acknowledgement, continued command responsiveness, exactly one task with the complete transcript, final notification and live dashboard update. Deterministic/fake-provider tests remain the default for development; no independent paid test was run during deployment.
5. Exercise worker termination/recovery with synthetic work, and managed Redis reconnect in an isolated fixture or approved maintenance window. Local process tests do not prove provider limits or availability.

## Update, rollback and troubleshooting

For code-only updates, run local checks, inspect exact staged bytes for secrets/confidential artifacts, then push reviewed `main`. API/worker are configured to autodeploy relevant source changes; bot restarts serially and Pages builds/deploys independently. Verify every resulting service revision/status: a green Pages workflow alone does not prove the backend received that revision. IaC changes require a separate reviewed `railway config plan`/`apply`; application pushes do not apply `.railway/` themselves.

For schema changes, review compatibility and back up first. Use maintenance downtime and pause automatic deployments when needed; apply one reviewed migration through the explicit API environment before incompatible code runs. Never run destructive migrations on every process start or push.

Before reverting code on `main`, verify that the current schema supports the older release. Do not automatically downgrade or restore over the live database. Restore necessary data into a **separate** Neon database/branch, verify it, then deliberately switch API/worker connection variables. Keep the original database until recovery is proven. The [backup/restore runbook](BACKUP_RESTORE.md) and S9 rehearsal cover Compose; a managed Neon restore has not yet been rehearsed.

Use scoped Railway deployment status and sanitized logs. Polling conflicts indicate another process using the token; login failures require checking exact origin/cookie flags/browser support; reconnecting state requires checking WSS, Redis subscription and provider quotas. Never print `railway run env`, secret URL values, cookies, login links, recordings or transcripts into logs/support reports.

Recorded local hosting checks report **452 backend tests**, **44 bot-deployment guard tests**, **4 Railway configuration checks**, and **172 frontend tests** passing, with frontend type checking and local/Pages builds. Latest verified Pages run `36736415868` reports successful dependency installation, frontend tests, type checking, build, upload and deployment; HTTP checks independently confirm the live page/assets/CSP and API readiness. The real managed Redis/separate-worker probe passed, and API/worker source-connect deployments succeeded. Exact commands and final evidence belong in TASKS.md. The owner-supplied private Telegram/OpenAI credentials are configured, cloud transcription is enabled and the updated worker/bot deployments succeeded. Remaining work: observing a future matching-push API/worker deployment, the owner's real hosted `/profile`/text/voice checks and managed restore checks. Deployment readiness is not full product acceptance.

## Public-source boundary

Only reviewed source, placeholder templates and normal documentation belong in Git. `.env`, `private/`, credentials, confidential PDFs/source documents, recordings, logs, backups and real database dumps remain excluded from Git, Docker contexts and submission archives. The bot workflow's Railway token belongs in a GitHub **secret**, never a public variable or frontend build. Check exact staged bytes before every push; ignore rules cannot protect already tracked or force-added files. The S9 archive remains an older immutable snapshot. The owner-approved Hobby subscription does not authorize further purchases or independent paid transcription tests.

If CLI authentication cannot create a project token, create it in the existing Railway project’s Settings → Tokens for production. Place it only in the ignored `private/railway/deploy.env` field `RAILWAY_TOKEN=` for secure CLI transfer into the repository’s encrypted Actions secret. Never use an account-wide token in this workflow. Bot variable changes must use `--skip-deploys`, followed by the serialized workflow, so updating a variable cannot bypass stop-before-start.
