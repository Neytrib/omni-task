# One-repository hosting plan

Status: the owner approved `Neytrib/omni-task` as the one public source repository on 2026-09-30. Source publication is authorized after the staged-content safety check. Cloud configuration and deployment remain future work. The existing local Compose application and tested Ubuntu preparation remain intact.

## Source and hosting

Use one public GitHub repository containing the complete backend, bot, worker, frontend, Dockerfiles, Compose configuration, tests and normal documentation. No separate frontend repository and no Cloudflare Pages.

| Destination | Content or process |
| --- | --- |
| Public GitHub repository, `main` | Complete reviewed application source and eventual GitHub Actions workflow |
| GitHub Pages | Only compiled React/Vite frontend assets |
| Railway | Separate API, polling bot and supervised Celery worker services |
| Neon | Authoritative PostgreSQL data and authentication state |
| Upstash | Native Redis over TLS for Celery queues/results and distinct live Pub/Sub |

The final server-side processes run on Railway independently of the developer's computer. Telegram reaches the polling bot; the bot calls the API over private authenticated HTTP. API and worker use Neon and Upstash. Browser HTTPS/WSS reaches the public API; the API owns browser sockets. The worker uses OpenAI only under the existing explicit paid-transcription setting.

## Verified current frontend layout

| Item | Inspected value |
| --- | --- |
| Directory | `frontend/` |
| Dependency lock | `frontend/package-lock.json` |
| Install | `npm ci` with `frontend` as working directory |
| Production build | `npm run build`, which runs TypeScript checking followed by Vite |
| Output | `frontend/dist/` |
| Routing | No React router dependency; one dashboard screen and fragment-token login |
| Existing Vite base | Default `/`; requires repository-specific production base |

The eventual production base is `/omni-task/` for `https://neytrib.github.io/omni-task/`. This is the intended Pages address, not a live deployment. Use repository-root fragment login instead of introducing a router solely for Pages. A direct refresh must serve that root index. Preserve local `/login#token=...` compatibility and immediate removal of the visible token. Browser origin validation must stay separate from the full dashboard URL, which includes the repository path.

## Required changes before deployment

1. **Authentication and transport:** existing requests use relative `/api/` paths and same-origin credentials; sockets derive their address from the page origin. Configure explicit public HTTPS/WSS endpoints for the split deployment. Current HttpOnly `SameSite=Lax` cookies will not work across default Pages/Railway sites. Choose and verify a browser-compatible session approach before deployment: strict credentialed CORS and `SameSite=None; Secure` alone do not bypass third-party-cookie blocking. Retain HttpOnly sessions, CSRF, expiry/revocation, exact origin validation and single-use links; do not put credentials in localStorage or socket URLs. No domain purchase or authentication redesign is implied by this plan.
2. **Railway builds and private transport:** the existing root Dockerfile ends at the test stage. Give each Railway service an explicit production build/start configuration using the complete root context and correct backend/bot image contents. Configure private API-to-bot and bot-to-API URLs. Expose only the API publicly; keep bot adapter endpoints protected. Keep exactly one poller per Telegram token and stop the local poller before reusing its token remotely.
3. **Managed data connections:** configure Neon TLS connection URLs and a native `rediss://` Upstash connection with certificate verification. An Upstash REST URL is not a Celery broker. Verify the selected service's queues, Pub/Sub, limits and recovery against real managed services before claiming compatibility is proven for this application. Celery polling consumes Upstash requests even when idle; review the chosen budget before provisioning.
4. **Migrations:** run reviewed migrations as one controlled operation with a backup and compatibility check. Do not run independent, potentially racing migration commands in API, bot and worker startup, or automatically execute destructive schema changes on every push. Keep local Compose's controlled migration initializer.

## Automatic deployment after setup

On push to `main`, Railway's GitHub integration rebuilds its three services from the same repository. GitHub Actions separately installs and tests/builds `frontend`, uploads only `frontend/dist`, and deploys the Pages artifact. Use GitHub's current official Pages workflow with limited permissions, Pages environment and deployment concurrency. The workflow must not commit generated assets back to `main`; this avoids deployment loops. Railway services deploy independently, so application startup must not assume deployment ordering.

Public build variables may contain only the Pages base and API/WSS addresses. Telegram/OpenAI credentials, database/Redis URLs, service-authentication keys and Railway secrets stay in protected server configuration. No production secret is needed by the Pages build. Recheck the official workflow action versions at implementation rather than treating this plan as a pinned workflow.

## Publication sequence and safety gate

The owner resolved the preflight pause by approving `Neytrib/omni-task`. The source-publication sequence is:

1. Reinspect the current tree and credential/confidential-file scan; initialize the project on `main`.
2. Stage only the reviewed source list. Inspect the complete staged file list and content, including placeholders and lockfiles. Re-run the known-secret/pattern checks against the exact staged bytes; force-adding private files is prohibited. Configure an appropriate Git author identity without exposing a private email inadvertently.
3. Commit the reviewed source; create one **public** repository under the approved account/name, add `origin`, verify its URL and push `main`.
4. Implement/test the hosting-specific changes and workflow, then provision/configure providers under the owner's next instructions and any required cost approval. Do not claim a public application exists merely because the source has been pushed.

The source audit is not approval to upload the whole workspace. `.env`, confidential PDFs/source documents, personal audio, runtime logs, data/dumps, backups and generated archives stay excluded. Root `.env.example` and `production.env.example` are the only allowed environment examples and contain placeholders/empty external keys. Git ignore rules do not protect already tracked or force-added files, so the staged-tree check is mandatory before the first push. The prior S9 archive remains an older immutable snapshot, not the source for this new repository.

## Official references

- [Git ignore semantics](https://git-scm.com/docs/gitignore)
- [GitHub Pages custom workflow](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
- [Vite static deployment](https://vite.dev/guide/static-deploy)
- [Railway GitHub autodeploys](https://docs.railway.com/deployments/github-autodeploys), [Dockerfiles](https://docs.railway.com/builds/dockerfiles), [private networking](https://docs.railway.com/networking/private-networking)
- [Credentialed CORS and third-party cookies](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CORS)
- [Upstash Celery integration and polling costs](https://upstash.com/docs/redis/integrations/celery)
