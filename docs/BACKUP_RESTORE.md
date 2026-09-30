# Backup and restore

Run these commands from the release checkout on the Ubuntu VPS. Python 3, Docker,
and the Compose plugin are sufficient on the host; PostgreSQL tools run inside
the pinned PostgreSQL container. No database port needs to be published. The production command wrapper removes
inherited application variables so the protected environment file is authoritative.

The examples use project `omni-task-prod`, the protected environment file
`/etc/omni-task/production.env`, and both production Compose files. Keep those
arguments consistent for every operation. If using a different project name,
change it everywhere. The scripts require an explicit project name and environment
file and default to `docker-compose.yml` plus `compose.production.yml`.

## Create a backup

```sh
sudo install -d -m 0700 /var/backups/omni-task
sudo python3 scripts/backup.py \
  --env-file /etc/omni-task/production.env \
  --project-name omni-task-prod \
  --output-dir /var/backups/omni-task
```

The command prints the completed backup directory, not credentials or task data.
Each bundle contains `database.dump` and `manifest.json` with its SHA-256 checksum,
size, source database name, and timestamp. Files are mode 0600; directories are
0700. A temporary directory is published by an atomic rename only after export
and checksum completion. Files and directory entries are flushed before success.
A failed export removes its temporary bundle. Existing backups are not overwritten.
The default command deadline is ten minutes; `--timeout-seconds` can increase it
up to one hour for larger databases.

`pg_dump --format=custom` takes a consistent online database snapshot. It includes
tasks and complete content, users, owner revisions, deleted-source receipts,
processing/transcript caches, notification state, live outbox records, hashed
authentication credentials, CSRF secrets, and migration history. It does not copy
PostgreSQL roles, the production environment file, TLS state, or Redis files.
The deployment creates the destination database role; restore assigns objects to
that role with `--no-owner --no-acl`. Use the same pinned PostgreSQL major version
for this tested workflow. Back up before changing images or migrating schema.

Store a separate encrypted copy of the environment file and release configuration
in an administrator-controlled location. Preserve `BOT_IDENTITY` so Telegram
source receipts continue to deduplicate correctly. Keep provider and Telegram
credentials private; none belong in the submission archive or Git. Caddy's
certificate/account volume can be backed up separately or certificates can be
reissued; the PostgreSQL dump does not preserve it.

Copy completed bundles to encrypted storage outside the VPS after checking the
command exit status. A SHA-256 manifest detects corruption; it is not a signature
or encryption. Restore only trusted backups you created. Keep multiple generations
and choose retention/frequency for the acceptable data-loss window. This simple
logical-backup workflow restores the last snapshot; it does not implement continuous
WAL archiving or point-in-time recovery. Changes after that snapshot are outside it.
No backup transfer or recurring schedule is installed by these scripts.

## Restore without overwriting the current database

First ensure PostgreSQL is running in the intended destination project. For a new
VPS, provision the protected environment/configuration and matching release images
as described in [DEPLOYMENT.md](DEPLOYMENT.md), then start only the data services.
Do not start polling or workers against restored data before completing validation.

```sh
sudo python3 scripts/production.py --env-file /etc/omni-task/production.env \
  --project-name omni-task-prod -- up -d --wait postgres redis

sudo python3 scripts/restore.py \
  --env-file /etc/omni-task/production.env \
  --project-name omni-task-prod \
  --backup /var/backups/omni-task/backup-YYYYMMDDTHHMMSSZ-XXXXXXXX \
  --database omni_task_restore_20260930
```

Replace the example bundle path and choose a **new** database name. The script
verifies the complete checksum/size before database creation. It refuses the
configured application database, PostgreSQL system databases, and every existing
database, including empty ones. It creates a database from `template0` and runs
`pg_restore --single-transaction --exit-on-error --no-owner --no-acl` without
`--clean` or `--create`. It never drops a database, starts an application, changes
the environment file, or chooses a cutover automatically. Failure can leave the
new target empty; inspect it before any deliberate cleanup. Existing databases
remain unchanged. Never restore an untrusted dump: PostgreSQL restore executes SQL.

Inspect the restored migration version and row counts without printing content:

```sh
sudo python3 scripts/production.py --env-file /etc/omni-task/production.env \
  --project-name omni-task-prod -- exec -T postgres sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" \
    -d omni_task_restore_20260930 -c "SELECT version_num FROM alembic_version;" \
    -c "SELECT count(*) AS tasks FROM tasks;" \
    -c "SELECT state, count(*) FROM processing_requests GROUP BY state;"'
```

Compare counts and migration version with the backup's time and intended release.
Use the release compatible with the restored schema. Do not point older code at an
incompatible newer schema or run a downgrade as a substitute for recovery.

## Deliberate cutover with fresh Redis

For an existing deployment, this is an administrator-controlled maintenance
operation. Stop writers before changing the application database. Keep the old
database and Redis volume for investigation; do not run `down --volumes`,
`FLUSHALL`, or delete the old queue volume.

```sh
sudo python3 scripts/production.py --env-file /etc/omni-task/production.env \
  --project-name omni-task-prod -- stop edge bot
sudo python3 scripts/production.py --env-file /etc/omni-task/production.env \
  --project-name omni-task-prod -- stop worker api
sudoedit /etc/omni-task/production.env
```

Change only `POSTGRES_DB` to the verified target, e.g.
`omni_task_restore_20260930`; retain mode 0600. Preserve the original credentials
and bot identity, and deliberately choose the matching `RELEASE_TAG`.

Create `/etc/omni-task/restore-redis.yml` as a root-owned mode-0600 file with a
fresh volume name. This requires the Compose version supporting `!override` used
by the production deployment:

```yaml
services:
  redis:
    volumes: !override
      - redis_restore_20260930:/data
volumes:
  redis_restore_20260930:
```

Use **all three** `--file` arguments consistently for **all future** deployment,
backup, and restore commands after cutover: `--file docker-compose.yml`,
`--file compose.production.yml`, and `--file /etc/omni-task/restore-redis.yml`.
Supplying any `--file` replaces the default pair; passing only the extra file
is insufficient.
Otherwise a later command could reattach the old Redis volume. The previous
project-prefixed `redis_data` volume is retained. Start the new Redis and refresh
PostgreSQL's healthcheck environment:

```sh
sudo python3 scripts/production.py --env-file /etc/omni-task/production.env \
  --project-name omni-task-prod --file docker-compose.yml --file compose.production.yml \
  --file /etc/omni-task/restore-redis.yml -- up -d --wait postgres redis
```

A backup also rolls authentication state back to its snapshot: a later logout or
link revocation may no longer be present. Before exposing a disaster restore,
revoke the restored browser sessions and links so users obtain fresh `/profile`
links. This changes authentication records only in the explicitly named restored
database; task/source/job content remains intact:

```sh
sudo python3 scripts/production.py --env-file /etc/omni-task/production.env \
  --project-name omni-task-prod --file docker-compose.yml --file compose.production.yml \
  --file /etc/omni-task/restore-redis.yml -- exec -T postgres sh -c \
  'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d omni_task_restore_20260930 \
   -c "BEGIN; UPDATE sessions SET revoked_at = now(); \
       UPDATE login_links SET revoked_at = now(); COMMIT;"'

sudo python3 scripts/production.py --env-file /etc/omni-task/production.env \
  --project-name omni-task-prod --file docker-compose.yml --file compose.production.yml \
  --file /etc/omni-task/restore-redis.yml -- rm -f migrate
sudo python3 scripts/production.py --env-file /etc/omni-task/production.env \
  --project-name omni-task-prod --file docker-compose.yml --file compose.production.yml \
  --file /etc/omni-task/restore-redis.yml -- up -d --wait api worker bot frontend edge
```

Removing only the stopped migration initializer before `up` makes Compose run
one controlled initializer before API and worker readiness. It does not delete
a database or a data volume.

The API recovers queued processing and pending notification work from PostgreSQL
even when Redis is empty. An in-progress snapshot waits for its stored execution
lease to expire (normally 240 seconds) and the next dispatch tick. Saved transcripts
resume task creation; completed tasks are not transcribed again for notification
failure. Incomplete external calls can be repeated after restore, so provider
billing and fallback Telegram sends cannot be exactly once. Redis AOF helps with
ordinary restarts but is not the recovery authority. Browser reconnection reloads
the complete database snapshot; obtain a fresh session after cutover.

Check HTTPS readiness, both worker consumers, `/profile` login, full task content,
and two-tab updates. Create only disposable acceptance tasks. Keep the previous
database/volume until the restored deployment is accepted and retention policy
permits their deliberate removal. To abandon the restored deployment, stop its
writers before changing configuration; do not run two polling processes for the
same Telegram bot.

## Rehearse safely

After building the local images, run:

```sh
python3 scripts/check_restore.py
```

This creates a random `omni-restore-test-*` project with its own data volumes,
generated credentials, blank external keys, and no host ports. It migrates and
seeds synthetic records, runs the actual backup and new-database restore, refuses
a second restore over the target, and verifies complete Unicode content, status,
source/deletion receipts, session/CSRF preservation, consumed-link rejection, and
owner isolation. With initially empty Redis it runs a separate prefork Celery
worker consuming transcription and notification queues and verifies queued fake
voice plus previously saved notification recovery. It then removes only its own
temporary project and fixture artifacts. It never touches active user data or
makes a paid/Telegram API call. `tests/test_backup_restore.py` separately exercises
checksum, private-file, failed-export and refusal guardrails.

This rehearsal is a logical restore and service-process test, not evidence of an
off-site disaster recovery, physical disk failure, backup retention automation,
real Telegram file retention, or public production deployment. Record the latest
executed result in TASKS.md rather than treating `pg_restore --list` as proof of a
successful restore.

Official references: [PostgreSQL pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html),
[pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html), and
[SQL dump recovery](https://www.postgresql.org/docs/17/backup-dump.html).
