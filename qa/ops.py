"""Operator backup, isolated restore drill, and HTTP health checks.

Database credentials come from environment variables, never CLI arguments or logs.
PostgreSQL client tools must be installed separately on the trusted operator host.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


CORE_TABLES = frozenset(('calls', 'events', 'jobs', 'evaluations', 'coaching',
                         'audit_log', 'settings', 'users', 'sessions', 'login_attempts'))
PG_ENV = {'host': 'PGHOST', 'port': 'PGPORT', 'user': 'PGUSER', 'password': 'PGPASSWORD',
          'dbname': 'PGDATABASE', 'sslmode': 'PGSSLMODE', 'sslrootcert': 'PGSSLROOTCERT',
          'sslcert': 'PGSSLCERT', 'sslkey': 'PGSSLKEY', 'channel_binding': 'PGCHANNELBINDING',
          'connect_timeout': 'PGCONNECT_TIMEOUT', 'application_name': 'PGAPPNAME',
          'target_session_attrs': 'PGTARGETSESSIONATTRS'}


class OpsError(Exception):
    """Errors intentionally contain stable codes instead of provider/connection text."""


def _connection(env_name):
    import psycopg
    value = os.environ.get(env_name, '')
    if not value.startswith(('postgres://', 'postgresql://')):
        raise OpsError('postgres_url_required')
    try:
        info = psycopg.conninfo.conninfo_to_dict(value)
    except Exception:
        raise OpsError('invalid_postgres_url') from None
    if not info.get('dbname') or set(info) - set(PG_ENV):
        raise OpsError('unsupported_postgres_connection_options')
    return value, info


def _client_env(info=None):
    # Do not inherit a service file, alternate host, or a competing DB URL.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('PG') and k not in ('DATABASE_URL', 'QA_RESTORE_TEST_DATABASE_URL')}
    env['PGCONNECT_TIMEOUT'] = '10'
    for key, value in (info or {}).items():
        env[PG_ENV[key]] = value
    return env


def _run(command, *, env=None, stdout=subprocess.PIPE, timeout=600):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stdout,
                                stderr=subprocess.PIPE, env=env or _client_env(),
                                timeout=timeout, check=False)
    except FileNotFoundError:
        raise OpsError('postgres_client_tool_missing') from None
    except subprocess.TimeoutExpired:
        raise OpsError('postgres_client_timeout') from None
    except OSError:
        raise OpsError('postgres_client_failed') from None
    if result.returncode:
        # PostgreSQL stderr can contain passwords, connection strings, or customer rows.
        raise OpsError('postgres_client_failed')
    return result.stdout


def _manifest_path(path):
    return path.with_name(path.name + '.manifest.json')


def _archive_info(path):
    try:
        with path.open('rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode) or source.read(5) != b'PGDMP':
                raise OpsError('invalid_backup_format')
            source.seek(0)
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        toc = _run(['pg_restore', '--list', str(path)]).decode('utf-8', errors='replace')
        # Standard production tables live in public. Do not claim a different schema is covered.
        tables = {m.group(1) for m in re.finditer(r'\bTABLE public ([a-z_]+)\s', toc)}
        if not CORE_TABLES <= tables:
            raise OpsError('backup_missing_application_tables')
        return {'bytes': path.stat().st_size, 'sha256': digest, 'tables': sorted(tables)}
    except OpsError:
        raise
    except OSError:
        raise OpsError('backup_unreadable') from None


def backup(destination, source_env='DATABASE_URL'):
    """Produce a consistent custom-format dump and manifest, refusing any overwrite."""
    _, info = _connection(source_env)
    path = Path(destination).expanduser().absolute()
    manifest_path = _manifest_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink() or manifest_path.exists() or manifest_path.is_symlink():
        raise OpsError('backup_destination_exists')
    created = []
    try:
        # O_EXCL protects against path races and symlinks; fd remains 0600 during pg_dump.
        dump_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created.append(path)
        with os.fdopen(dump_fd, 'wb') as output:
            _run(['pg_dump', '--format=custom', '--no-owner', '--no-acl'],
                 env=_client_env(info), stdout=output)
            output.flush()
            os.fsync(output.fileno())
        details = _archive_info(path)
        manifest = {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(), **details}
        manifest_fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created.append(manifest_path)
        with os.fdopen(manifest_fd, 'w') as output:
            json.dump(manifest, output, sort_keys=True)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        return {'ok': True, 'operation': 'backup', 'archive_verified': True,
                'restore_verified': False, 'bytes': details['bytes'], 'sha256': details['sha256']}
    except Exception:
        for incomplete in reversed(created):
            incomplete.unlink(missing_ok=True)
        raise


def verify_archive(archive):
    path = Path(archive).expanduser().absolute()
    try:
        manifest = json.loads(_manifest_path(path).read_text())
    except (OSError, ValueError):
        raise OpsError('backup_manifest_missing_or_invalid') from None
    details = _archive_info(path)
    if manifest.get('version') != 1 or any(manifest.get(k) != details[k] for k in details):
        raise OpsError('backup_checksum_or_manifest_mismatch')
    return path, details


def restore_check(archive, confirm_database, target_env='QA_RESTORE_TEST_DATABASE_URL'):
    """Restore only to an explicitly named, empty, separate test database.

    It never creates/drops databases and never cleans/replaces an existing schema.
    The drill leaves restored data in the isolated target for operator inspection.
    """
    import psycopg
    from psycopg import sql
    if target_env == 'DATABASE_URL':
        raise OpsError('production_target_forbidden')
    target_url, info = _connection(target_env)
    target_name = info['dbname']
    if (confirm_database != target_name or
            not re.fullmatch(r'qa_restore_[a-z0-9_]{1,48}', target_name)):
        raise OpsError('explicit_isolated_database_confirmation_required')
    # Even a production database with a test-looking name must not be used.
    if os.environ.get('DATABASE_URL'):
        _, source = _connection('DATABASE_URL')
        if (source.get('host'), source.get('port', '5432'), source['dbname']) == (
                info.get('host'), info.get('port', '5432'), target_name):
            raise OpsError('production_target_forbidden')
    path, details = verify_archive(archive)
    try:
        with psycopg.connect(target_url, connect_timeout=10, options='-c statement_timeout=15000') as conn:
            if conn.execute('SELECT current_database()').fetchone()[0] != target_name:
                raise OpsError('restore_target_identity_mismatch')
            # Reject user relations in ANY schema, plus stored routines. Never overwrite a used target.
            objects = conn.execute("SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                                   "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                                   "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'").fetchone()[0]
            routines = conn.execute("SELECT COUNT(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                                    "WHERE n.nspname NOT IN ('pg_catalog','information_schema')").fetchone()[0]
            if objects or routines:
                raise OpsError('restore_target_not_empty')
        _run(['pg_restore', '--exit-on-error', '--single-transaction', '--no-owner', '--no-acl',
              '--dbname', target_name, str(path)], env=_client_env(info))
        with psycopg.connect(target_url, connect_timeout=10, options='-c statement_timeout=15000') as conn:
            present = {row[0] for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()}
            if not CORE_TABLES <= present:
                raise OpsError('restored_application_tables_missing')
            invalid = conn.execute("SELECT COUNT(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace "
                                   "WHERE n.nspname='public' AND NOT c.convalidated").fetchone()[0]
            if invalid:
                raise OpsError('restored_constraints_invalid')
            counts = {name: conn.execute(sql.SQL('SELECT COUNT(*) FROM public.{}').format(
                sql.Identifier(name))).fetchone()[0] for name in sorted(CORE_TABLES)}
        return {'ok': True, 'operation': 'restore-check', 'restore_verified': True,
                'sha256': details['sha256'], 'row_counts': counts, 'target_retained': True}
    except OpsError:
        raise
    except Exception:
        raise OpsError('restore_verification_failed') from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _probe(url):
    try:
        with build_opener(_NoRedirect()).open(Request(url, headers={'Accept': 'application/json'}),
                                             timeout=10) as response:
            return response.status, json.loads(response.read(8192))
    except HTTPError as error:
        try:
            return error.code, json.loads(error.read(8192))
        except (ValueError, OSError):
            return error.code, {}
    except (URLError, ValueError, OSError):
        return 0, {}


def check(base_url, allow_paused=False):
    parts = urlsplit(base_url)
    local_http = parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1', '::1')
    if (not parts.hostname or (parts.scheme != 'https' and not local_http) or parts.username or
            parts.password or parts.query or parts.fragment or parts.path not in ('', '/')):
        raise OpsError('https_origin_required')
    health_code, health = _probe(base_url.rstrip('/') + '/healthz')
    ready_code, ready = _probe(base_url.rstrip('/') + '/readyz')
    live = health_code == 200 and isinstance(health, dict) and health.get('status') == 'up'
    processing_ready = ready_code == 200 and isinstance(ready, dict) and ready.get('ready') is True
    expected_pause = (allow_paused and ready_code == 503 and isinstance(ready, dict)
                      and ready.get('ready') is False and ready.get('processing_enabled') is False)
    operations_ok = ready.get('operations_ok') if isinstance(ready, dict) else None
    # Emit only status, never remote response bodies, URLs with credentials, or customer data.
    return {'ok': bool(live and operations_ok is not False and (processing_ready or expected_pause)), 'operation': 'check',
            'live': live, 'processing_ready': processing_ready, 'pause_allowed': allow_paused,
            'operations_ok': operations_ok,
            'health_http': health_code, 'readiness_http': ready_code}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    dump = commands.add_parser('backup')
    dump.add_argument('--destination', required=True)
    dump.add_argument('--source-env', default='DATABASE_URL', help='Environment variable name, not a URL')
    restore = commands.add_parser('restore-check')
    restore.add_argument('--archive', required=True)
    restore.add_argument('--target-env', default='QA_RESTORE_TEST_DATABASE_URL')
    restore.add_argument('--confirm-isolated-database', required=True)
    monitor = commands.add_parser('check')
    monitor.add_argument('--url', required=True)
    monitor.add_argument('--allow-paused', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'backup':
            result = backup(args.destination, args.source_env)
        elif args.command == 'restore-check':
            result = restore_check(args.archive, args.confirm_isolated_database, args.target_env)
        else:
            result = check(args.url, args.allow_paused)
    except OpsError as error:
        result = {'ok': False, 'operation': args.command, 'error': str(error)}
    except Exception:
        result = {'ok': False, 'operation': args.command, 'error': 'operation_failed'}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
