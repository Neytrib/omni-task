# Railway configuration

This directory describes only the three Railway application services. The frontend
is deployed to GitHub Pages from the same repository; PostgreSQL and Redis remain
on Neon and Upstash. Local Docker Compose is independent and unchanged.

Install the locked authoring SDK with `npm ci --prefix .railway`, then run
`npm --prefix .railway run check` and `npm --prefix .railway test`.

Use Railway CLI 5.49.1 or newer. Before applying anything, verify the linked project
and production environment, populate the protected service variables using the
templates in `deploy/railway`, and run `railway config plan`. Review the exact plan
before `railway config apply`; applying infrastructure can deploy services and incur
hosting costs. Never use `--include-variables`, `--show-values` or commit plan files.

Secrets use `preserve()` and references, so applying this file does not replace
filled credentials with placeholders. Public API domains are created separately.
Keep bot and worker private. Changes to this IaC require a reviewed plan/apply;
ordinary application pushes use API/worker GitHub autodeploys and the serialized
bot deployment workflow. Do not connect the bot to native GitHub autodeploys: that
would race the stop-before-start workflow and can start two polling processes.

After the first apply, explicitly connect the API and worker GitHub sources with
`railway service source connect` as documented in `docs/HOSTING_PLAN.md`, then
verify their deployment triggers. A repository/branch in the IaC or service
configuration alone is not evidence that a push will deploy. The Railway account
must be linked to the owning GitHub identity as well as have the Railway GitHub
app installed for this repository. Leave the bot disconnected.

Railway's old `railway.json`/`railway.toml` format is not used: new services cannot
opt into it and legacy support ends on 2026-12-01. The current TypeScript SDK is
pinned separately from the frontend and never enters the browser bundle.

References: [IaC](https://docs.railway.com/infrastructure-as-code),
[authoring API](https://docs.railway.com/infrastructure-as-code/reference),
[deployment teardown](https://docs.railway.com/deployments/deployment-teardown).
