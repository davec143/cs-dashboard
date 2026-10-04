# cs-dashboard

Customer-service call quality dashboard with evidence-based evaluations, human coaching follow-through, durable processing, and missing-call recovery.

## Deployment

Railway runs three services: Flask/Gunicorn dashboard/API, a separate Python worker, and PostgreSQL. The Dockerfile is shared; override the worker start command with `python -m qa.worker`. Configure `/healthz` as the dashboard deployment probe. `/readyz` separately verifies recent worker/reconciliation activity; it intentionally reports 503 while processing is paused.

Set `DATABASE_URL` on both app services to the private PostgreSQL reference. Set `QA_PROCESSING_ENABLED=false` initially. Configure verified agent IDs with `QA_AGENTS_JSON` (an object mapping Aircall user IDs to `{ "name": "...", "enabled": true }`). Never commit real agent IDs, transcripts, assessment history, account passwords, provider keys, or database backups.

Named management accounts use scrypt password hashes, revocable database sessions, secure cookies, CSRF checks, login rate limits, and admin/viewer permissions. `QA_BOOTSTRAP_EMAIL` and `QA_BOOTSTRAP_PASSWORD_HASH` create the first admin only if absent; remove these variables after successful provisioning. Account owners can change their passwords inside the dashboard. Company SSO and MFA are not implemented yet.

Before enabling processing, configure Aircall API credentials, the webhook token, OpenAI API key, and a calibrated pinned model. Register the `/webhooks/aircall` endpoint for supported events only after a controlled integration test. A paused receiver returns 503 and should not yet be registered as an active delivery destination. Existing integrations remain available during shadow operation.

## Required configuration

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection, private within Railway |
| `QA_PROCESSING_ENABLED` | Explicit `true` to enable live ingestion/worker |
| `QA_AGENTS_JSON` | Verified agent scope |
| `AIRCALL_API_ID`, `AIRCALL_API_TOKEN` | Aircall API credentials (or `AIRCALL_ACCESS_TOKEN`) |
| `AIRCALL_WEBHOOK_TOKEN` | Validate Aircall event payloads |
| `OPENAI_API_KEY`, `EVALUATOR_MODEL` | Evaluation provider and pinned model |
| `QA_BOOTSTRAP_EMAIL`, `QA_BOOTSTRAP_PASSWORD_HASH` | Initial account provisioning; remove afterwards |

Historical audit files are optional private inputs via `QA_AUDIT_DIR`. They are excluded from this repository and deployment image. The production dashboard starts empty until real calls or separately authorized historical data are ingested.

## Local development and verification

Python 3.11+:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m qa.server --demo
.venv/bin/python -m unittest discover -s tests -v
node --check web/app.js
```

The local demo uses synthetic calls and SQLite. Set `QA_TEST_DATABASE_URL` to run PostgreSQL integration tests; they create and remove only isolated schemas with unique names. Never point tests at a database without permission to create temporary schemas.

Recovery defaults to a read-only plan and requests no transcripts or model evaluations:

```sh
python -m qa.recover --start 2026-10-01T00:00:00Z --end 2026-10-02T00:00:00Z --dry-run
```

Use `--enqueue` only after reviewing the plan. Existing call IDs are preserved. An enabled worker processes queued calls and incurs provider usage.

## Release requirements

Prove one real call from Aircall through durable storage to dashboard, including retries and reconciliation. Calibrate a balanced sample against independent human reviews before relying on agent quality estimates. Unknown speakers and unsupported evidence route to review. Product accuracy requires an authoritative knowledge source, which is not yet integrated.

Configure and verify PostgreSQL backup/restore, ongoing monitoring, named management access, and retention before production cutover. Railway database templates require operator-managed backups and maintenance. Keep credentials in host variables and keep the old pipeline available until the replacement passes acceptance checks.
