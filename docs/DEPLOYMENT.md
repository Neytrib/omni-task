# One Ubuntu VPS: deployment and operations

This is a prepared runbook, not evidence of a public deployment. No VPS, domain, or other resource has been purchased. Obtain the owner's approval before provisioning or running the public-start commands. The local verification uses private Docker networks and an internal test certificate; public DNS, ACME issuance/renewal, and the VPS firewall still require verification on the approved server.

## Prerequisites and layout

Use Ubuntu 24.04 LTS (amd64 or arm64), Python 3, Docker Engine, Buildx, and the Docker Compose plugin **2.24.4 or newer**. Install Docker using its [official Ubuntu apt-repository instructions](https://docs.docker.com/engine/install/ubuntu/), then check:

```sh
sudo docker version
sudo docker compose version
python3 --version
sudo systemctl enable --now docker
```

The daemon/socket and membership in the Docker group grant powerful host access. These commands use a root administrative shell instead of widening group membership. No systemd unit for the application is required: Docker's restart policy resumes existing service containers after reboot. Health checks report failures; they are not an automatic repair service for a running unhealthy container.

Use these paths:

| Path | Contents |
| --- | --- |
| `/opt/omni-task/releases/RELEASE_ID/` | Immutable extracted source for one release; retain the previous release. |
| `/etc/omni-task/production.env` | Root-owned 0600 configuration; parent directory 0700. |
| `/var/backups/omni-task/` | Root-only database backup bundles; copy encrypted backups off the VPS. |
| Docker named volumes | PostgreSQL, Redis AOF, Caddy certificates and configuration. Keep project name `omni-task-prod` stable. |

A domain's A record must point to the approved VPS. Set AAAA only if IPv6 reaches that server. Allow TCP 80 and 443 to Caddy, and SSH only from your administration network where practical. Never publish 5432, 6379, 8000, 8001, 8080, or the Docker socket. Docker-published ports can bypass uncomplicated UFW rules; inspect the actual provider/host firewall and Docker rules rather than relying on UFW alone. [Docker firewall guidance](https://docs.docker.com/engine/network/packet-filtering-firewalls/)

## Prepare the release and protected configuration

The submission has no Git history or repository URL. Upload the **source-only submission archive**, not the entire development directory, through your approved transfer method. Do not transfer the assessment, recordings, development `.env`, local databases, or logs.

After approval, on the server (replace the release ID and hostname/email):

```sh
sudo -i
install -d -m 0755 /opt/omni-task/releases/release-001
install -d -m 0700 /etc/omni-task /var/backups/omni-task
tar -xzf /root/omni-task-submission.tar.gz -C /opt/omni-task/releases/release-001 --strip-components=1
cd /opt/omni-task/releases/release-001
sha256sum -c MANIFEST.sha256
python3 scripts/configure_production.py --domain tasks.example.com --email admin@example.com --release release-001 --output /etc/omni-task/production.env
sudoedit /etc/omni-task/production.env
chmod 0600 /etc/omni-task/production.env
```

The generator refuses to overwrite an existing file, writes independent random URL-safe PostgreSQL/service secrets, and starts nothing. Replace `tasks.example.com` and the contact email with your actual values. Set `BOT_IDENTITY` to a stable namespace for your real Telegram bot and enter `TELEGRAM_BOT_TOKEN` only in the protected file. Keep that identity unchanged across rotation of the same bot's token. Leave transcription disabled unless deliberately enabling the paid integration; configure its key/model as described in README. A separate OpenAI account/project key is supported.

Do not `source` the file, put secrets in shell arguments, or paste `docker compose config` output into a report. Use `config --quiet`; rendering the full configuration exposes environment values. Containers receive only their needed secrets; no frontend build argument contains credentials. Environment-based secrets are visible to host root/Docker administrators. This is a single-server protected-file arrangement, not a secrets vault. Production files and backups must never enter the submission or Git.

Define this shell helper in **each** administrative shell from the chosen release directory:

```sh
dc() {
  python3 scripts/production.py --project-name omni-task-prod --env-file /etc/omni-task/production.env -- "$@"
}
dc config --quiet
dc build api bot frontend
```

The wrapper validates env-file permissions, clears inherited application/Compose variables so the protected file wins, and rejects full configuration output. It defaults to the base and production Compose files.

Use a new `RELEASE_TAG` every time the source changes. It tags the three application images; API, migration initializer, and worker share the backend tag. Do not rebuild an old tag or prune the previous release's images before the rollback window ends. Image base digests and application dependencies are pinned; these still require deliberate security updates.

## Transfer Telegram polling ownership before public startup

Run exactly one polling bot process per token. Never scale `bot` or start a second Compose project with that token. Use a separate BotFather token for development, or on the development machine run:

```sh
cd /path/to/your/development/omni-task
docker compose stop bot worker
docker compose ps
```

Verify any other programs using the token are stopped too. Stopping the local worker prevents it retrying delivery through a now-stopped bot. A separate server database does **not** contain local tasks unless you deliberately migrate a protected database backup; do not mistake switching tokens or changing `BOT_IDENTITY` for a data migration. If migrating the data, stop local writes, take a final backup, restore according to the recovery guide, and then hand over polling. Never run both sides during migration. Telegram only retains undelivered updates for a limited period, so plan the handover.

## Start publicly — only after approval

From the release directory, with `dc` defined:

```sh
dc up -d --no-build --wait --wait-timeout 180
dc ps -a
dc exec -T api alembic current
dc exec -T api alembic check
dc exec -T api python scripts/queue_smoke.py
curl --fail --silent --show-error https://tasks.example.com/api/health/ready
```

Expect seven healthy long-running services (the original six plus `edge`) and one successfully exited `migrate` initializer. The initializer applies Alembic migrations once before API/worker startup; it is not a worker replica. A failed migration must block startup. Do not run schema changes concurrently from another shell.

Caddy terminates TLS, obtains/renews the public certificate, and redirects HTTP to HTTPS. It forwards to the built nginx frontend, which serves Vite assets, proxies only public API/WebSocket paths, and blocks `/internal`. Browser cookies and Origin remain on `https://YOUR_DOMAIN`; no separate API hostname/CORS configuration is needed. Caddy's certificate/account storage is persistent. Port 8081 is a container-loopback health endpoint and is not published. Production forces Secure cookies and `APP_ENV=production` even if the local defaults differ.

Check an HTTP redirect and certificate verification **without** `curl -k`:

```sh
curl --head http://tasks.example.com/
curl --head https://tasks.example.com/
dc port edge 443
dc port postgres 5432
dc port redis 6379
```

The last two commands must report no host mapping. Review actual listeners/firewall exposure on the server. From Telegram Desktop, request a new `/profile` link and open it in Arc; confirm the token disappears, the board reaches Live, and a text task appears. Follow the three-minute demo and two-user/logout checks. The container-local edge health check alone does not prove public DNS, certificate validity, or WebSocket routing.

## Backups and restore

Run a backup from the current release:

```sh
python3 scripts/backup.py --env-file /etc/omni-task/production.env --project-name omni-task-prod --output-dir /var/backups/omni-task
```

Follow [BACKUP_RESTORE.md](BACKUP_RESTORE.md) for checksum verification, backup retention/off-host handling, and the tested **new-database** restore procedure. A backup on the same VPS is not disaster recovery. Preserve the protected configuration separately in an encrypted store; it is intentionally absent from database backups and source archives. Keep Redis AOF enabled with `noeviction`. PostgreSQL records recover accepted voice work even if a fresh Redis instance has no queued messages. Do not use `FLUSHALL` or delete current volumes as a repair step.

## Update with a maintenance window

1. Extract a new submission to a new release directory and verify its manifest. Keep the previous source/images. Set only `RELEASE_TAG` to the new unique ID in the protected env file; preserve credentials, bot identity, domain, and project name. Define `dc` from the new directory and build the three application images before the outage.
2. Stop incoming work and consumers, then take a fresh backup. A stopped worker's unfinished jobs recover from PostgreSQL leases after restart; wait for active work beforehand when possible.

```sh
dc stop edge bot
dc stop worker api frontend
python3 scripts/backup.py --env-file /etc/omni-task/production.env --project-name omni-task-prod --output-dir /var/backups/omni-task
dc rm -f migrate
dc up -d --no-build --wait --wait-timeout 180
dc exec -T api alembic current
dc exec -T api alembic check
dc exec -T api python scripts/queue_smoke.py
curl --fail --silent --show-error https://tasks.example.com/api/health/ready
```

`rm -f migrate` removes only the stopped initializer container so the next startup runs controlled migrations again; it does not remove database/Redis volumes. If migration or health checks fail, leave ingress and polling stopped while diagnosing. Stop `edge bot` again if startup partially succeeded. Repeat the login, text/live, status, and user-isolation acceptance checks before announcing the release.

## Rollback

A code/image rollback is safe only when the previous code supports the current database schema. This preparation stage introduces no schema migration: the current head is `0003_live_outbox`. Future migrations may be incompatible; **do not blindly run `alembic downgrade` or restore over an existing database**.

For a verified schema-compatible rollback, stop ingress/consumers, change to the retained previous release directory, set `RELEASE_TAG` back to its original value using `sudoedit`, redefine `dc`, and run:

```sh
dc stop edge bot worker api frontend
dc config --quiet
dc up -d --no-build --wait --wait-timeout 180
dc exec -T api alembic current
dc exec -T api python scripts/queue_smoke.py
```

Do not rebuild the old tag. If the database schema is incompatible, keep services stopped, restore the pre-update backup into a **new database**, validate it with the matching release, then deliberately change `POSTGRES_DB` to that database and use a fresh Redis recovery volume as documented in BACKUP_RESTORE.md. Preserve the old database/volume for inspection. This snapshot recovery loses changes made after the backup and can repeat ambiguous external notifications; discuss that consequence before a real cutover.

## Troubleshooting

| Symptom | Safe checks and next action |
| --- | --- |
| HTTPS fails | Check domain A/AAAA, ports 80/443, `dc ps`, and `dc logs --since=10m edge`. A loopback health check can pass while public issuance fails. Do not disable certificate validation. |
| API/migration unhealthy | `dc ps -a`, `dc logs --since=10m migrate api postgres`; keep polling/ingress stopped until schema and database connectivity are correct. Do not delete data volumes. |
| Telegram 409 / no commands | Look for another polling process using the token, including the laptop. Stop the duplicate; do not repeatedly rotate the namespace. Check the token and whether a webhook was configured elsewhere. |
| Voice stays queued | `dc exec -T worker python -m app.jobs.worker --health`; inspect structured `voice_dispatch_delayed`, `transcription_failed`, and `notification_failed` events by correlation UUID. Verify Redis free memory/disk and both consumers. |
| Task exists but no Telegram result | Notification retries are independent. Inspect their state/error code; do not retranscribe a successful task. `/list` and the dashboard are authoritative. |
| Dashboard stays reconnecting | Check the exact HTTPS origin, public `/api/ws/tasks` routing, session expiry, and API/Redis health. Reconnect reloads PostgreSQL state; Pub/Sub is not durable replay history. |
| Login loops / CSRF error | Request a fresh `/profile` link. Ensure it uses the configured domain, not an IP/alternate hostname. Verify the cookie is Secure, and do not use localStorage credentials or disable CSRF. |
| Disk usage grows | `docker system df`, `df -h`, `dc logs --tail=50`; inspect backup retention and persistent volumes. Production Docker logs rotate at 10 MB × 3 files per container. Never prune active volumes. |
| Service remains unhealthy | Health checks report status; they do not automatically restart a running unhealthy container. Investigate, then restart only the affected service when its failure is understood. |

Application logs contain fixed event/error codes and correlation IDs, not message bodies, transcripts, tokens, cookies, or URLs. Caddy access logging is disabled and runtime request objects are removed. Do not enable debug/access/body/SQL-parameter logging or publish rendered Compose configuration. Avoid sharing even sanitized logs without reviewing their contents.

This is a single-VPS design, with planned downtime for updates and no high-availability/failover guarantee. Host sizing, public network behavior, sustained load, off-site backup automation, and alerting have not been validated on a real VPS. The tested restore and local TLS evidence belong in TASKS.md; deployment approval remains separate from preparation.

Official references: [Compose production configuration](https://docs.docker.com/compose/how-tos/production/), [Compose merge/reset rules](https://docs.docker.com/reference/compose-file/merge/), [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https), [Caddy reverse proxy/WebSockets](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy), and [Docker restart policy](https://docs.docker.com/engine/containers/start-containers-automatically/).
