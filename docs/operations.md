# CS Call Quality operations

The dashboard and worker use the same PostgreSQL database. The operator tools below
prepare backup and monitoring jobs; installing this repository does **not** schedule
backups, send alerts, or complete a restore drill. Verify actual deployed schedules and
successful receipts before enabling live processing.

## Railway daily encrypted backup job

Create a **separate** Railway service using `Dockerfile.backup`, with start command
`python -m qa.scheduled_backup run`. Configure it as a daily cron job, disable automatic
restart on normal exit, and assign no public domain or persistent volume. The job needs
outbound access to the production PostgreSQL private address and its private bucket.
Do not change the dashboard or worker to use this Dockerfile. The image includes
PostgreSQL 18 tools and works with a source server no newer than PostgreSQL 18.

Required environment variables:

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | Railway reference to production PostgreSQL; reads all backup data and writes settings receipts |
| `BACKUP_ENCRYPTION_KEY` | Secret URL-safe base64 encoding of 32 random bytes |
| `BACKUP_S3_ENDPOINT_URL` | Bucket credential's HTTPS endpoint |
| `BACKUP_S3_BUCKET` | Actual S3 bucket name (`BUCKET`), not its display name |
| `BACKUP_S3_ACCESS_KEY_ID` | Reference to the bucket's key ID |
| `BACKUP_S3_SECRET_ACCESS_KEY` | Reference to the bucket's secret |
| `BACKUP_S3_REGION` | Bucket region credential, usually `auto` |
| `BACKUP_S3_URL_STYLE` | `virtual` by default; use `path` only if bucket credentials specify it |
| `BACKUP_S3_PREFIX` | Optional, defaults to `cs-quality` |

Generate the encryption key with a cryptographic random generator and store a separate
recovery copy in the organization's password manager or protected operator file. Do not
print it in build logs or commit it. Losing this key makes these backups unrecoverable.
Keep older keys when rotating; existing archives require their original key.

Each job creates a consistent dump, encrypts it using AES-256-GCM, uploads it under a
unique UTC timestamp/UUID, downloads the encrypted object and manifest to verify their
checksums, authenticates/decrypts the download, and restores that downloaded archive
into a new local PostgreSQL database. This scratch server runs as the unprivileged
`postgres` OS user, listens only on a socket in a private temporary directory, and is
stopped and deleted at the end of the job. No production restore is attempted. The
job exits with failure if upload, authentication, restore, or verification fails.

Only after success does it upload `verified.json`, read that receipt back, and update
production settings `backup_completed_at` and `restore_verified_at` with a Unix timestamp.
`backup_last_attempt` and the safe `backup_error` code support troubleshooting. The
job's database advisory lock prevents overlapping manual/scheduled runs. Objects are
never automatically expired; agree a retention policy and configure cleanup separately.
A failed run may leave uploaded objects without a `verified.json`; those objects must
not be treated as successful backups.
Archives above 4 GiB fail explicitly; upgrade the upload strategy before reaching that
size. The job's log contains only safe status, checksums, and aggregate row counts.

[Railway bucket documentation](https://docs.railway.com/storage-buckets) currently lists
server-side encryption as unsupported. This job therefore encrypts the archive before
uploading; no server-side encryption header or public object permission is used. The
manifest and receipt contain only checksums, timestamps, sizes, and table counts.

For manual recovery, download an archive, its `database.dump.manifest.json`, and its
`verified.json` from the same object prefix using authenticated bucket access. Verify
the ciphertext SHA-256 against the receipt, securely load the corresponding
`BACKUP_ENCRYPTION_KEY`, and decrypt to a **new** file:

```sh
python -m qa.scheduled_backup decrypt \
  --source /secure/recovery/database.dump.gcm \
  --destination /secure/recovery/database.dump
```

Keep the manifest next to the dump with the exact `.manifest.json` suffix. The
`restore-check` command below validates the recovered dump again. A failed AES-GCM
authentication removes partial decrypted output. Never restore an unauthenticated or
unverified archive. The initial live job and ongoing scheduled runs still need to be
verified by an operator; passing mocked tests alone is not a production restore drill.

## Backups and restoration

Use a trusted operator or backup host with Python dependencies from `requirements.txt`
and PostgreSQL `pg_dump` / `pg_restore` installed. Use the server's PostgreSQL major
version for both client tools. The application Docker image does not include those
binaries. Never put database URLs on the command line, in shell history, GitHub issues,
or in this public repository. Load credentials from the host's protected environment
or secret manager; do not copy live Railway secrets into examples.

For a one-time backup with `DATABASE_URL` already supplied securely:

```sh
umask 077
python -m qa.ops backup --destination /secure/cs-quality-backups/2026-10-04T180000Z.dump
```

Use a new UTC timestamp for each run. The command creates a consistent PostgreSQL
custom-format archive plus a `.manifest.json` containing a SHA-256 checksum, timestamp,
size, and application table names. Both files are owner-only (`0600`), existing files
are never replaced, partial failed outputs are removed, and subprocess diagnostics
are reduced to safe error codes. An archive catalog and checksum check is **not** a
successful restore drill. Archive files contain sensitive transcripts, password hashes,
session data, and assessment history; file permissions are not encryption.

Provision a new, empty, isolated PostgreSQL database specifically for the restore drill,
with a name such as `qa_restore_20261004`. Use a separate test PostgreSQL instance where
possible. Do not attach a dashboard, worker, webhook, public URL, or provider credentials
to it. Supply its connection URL as `QA_RESTORE_TEST_DATABASE_URL` and run:

```sh
python -m qa.ops restore-check \
  --archive /secure/cs-quality-backups/2026-10-04T180000Z.dump \
  --confirm-isolated-database qa_restore_20261004
```

The tool accepts only explicitly confirmed `qa_restore_...` database names, rejects
the production connection, refuses a target containing user objects, checks the
archive checksum, and restores in one transaction without cleaning existing objects.
It verifies application tables and validated constraints, then emits aggregate row
counts without customer content. It leaves the isolated database intact for inspection;
the operator must securely remove the test database after the drill. The confirmation
is not a substitute for choosing a genuinely isolated target. Restore only an archive
produced by a trusted operator: checksums detect change, not a malicious archive's origin.

On a recovered test instance, also inspect the application with external integrations
disabled and verify a sample call, evaluation, coaching task, account, and audit entry.
Compare aggregate counts with the expected backup date. Do not re-use restored live
sessions or turn on polling while inspecting a drill. Document the measured restore
time and any missing data. A failed drill blocks production cutover until resolved.

Before a real disaster recovery: pause processing and inbound ingestion, preserve the
damaged database, restore to a **new** database, inspect it, invalidate restored sessions,
then deliberately switch the app connection. Reconcile Aircall from the backup timestamp
after validation to recover missed calls; duplicate event protection remains essential.
Never run a destructive restore over the only production copy.

## Schedule and retention

A practical starting policy is daily backups, 14 daily copies, and 8 weekly copies,
with a monthly restore drill and a drill after schema changes. Agree the acceptable
recovery point with management: one daily backup can lose up to one day of changes;
this is not point-in-time recovery. Schedule more frequently if required.

Use the organization's existing secure scheduled runner if available. The job should
run the command above with a unique name, copy both files to an approved encrypted
storage location outside the application/database host, verify the copied SHA-256
checksum, and record success. Never use an ephemeral Railway deployment filesystem as
the only backup location. Restrict backup readers, require encrypted transport and
storage, and make storage credentials separate from the application credentials.
Delete expired copies only after a new verified copy is present. Preserve the last
known successful backup if a job fails. Do not silently delete backups inside this CLI.

Add an alert if the daily job fails or no successful backup is younger than 26 hours.
Also alert on a failed restore drill. Keep secrets out of scheduled job output and
notifications. No new paid service or plan upgrade is assumed by this policy. A specific
backup destination, scheduler, alert recipient, and retention owner still need to be
configured and verified on the hosting account.

## Health and readiness monitoring

```sh
python -m qa.ops check --url https://dashboard-production-5be47.up.railway.app
```

The command uses HTTPS, refuses credential-bearing URLs and redirects, emits only
safe JSON status, and exits nonzero on failure. `/healthz` proves the web service can
query its database. `/readyz` verifies processing is enabled and worker/reconciliation
activity is recent. Neither endpoint proves a particular call has been scored correctly.

While intentionally setting up a paused deployment, `--allow-paused` accepts a healthy
web service only when readiness is false (503) **and** `processing_enabled` is explicitly
false, while still reporting `processing_ready: false`. An enabled but broken pipeline
always fails this check. Omit the flag when all processing pauses must alert.

Use a five-minute check in an existing monitoring system; notify the designated
operator after two consecutive failures and once on recovery. During operating hours,
also review queue age, errors requiring replay, and the latest successful reconciliation.
Investigate unexpected absence of calls against Aircall before changing scores or
replaying a large historical range. No calls overnight is not automatically an outage.

## Cutover checklist

- Verify a real backup and a completed isolated restore drill, not just the tooling.
- Configure and test monitoring delivery to a named operator.
- Confirm every manager's own login and a password change; never share one account.
- Verify one representative real call end to end, duplicate delivery, delayed transcript,
  transient failure retry, and missed-call reconciliation.
- Have the CS lead review scoring examples and accept the rubric; keep uncertain
  evaluations out of staff performance decisions until reviewed.
- Agree transcript/assessment retention and backup expiry with the data owner.
- Keep Make available during the pilot; retire it only after a documented acceptance
  window with no missing calls and understood scoring differences.

Maintenance records should contain timestamps, version/commit, checksums, status, and
aggregate counts only. Store transcripts, credentials, and backup archives outside this
public source repository.
