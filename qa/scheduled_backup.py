"""Encrypted S3 backups with a restore drill in a private, ephemeral local database."""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode, urlsplit
import uuid

from qa import ops

MAGIC = b'CSQA-GCM-1\n'
CHUNK = 1024 * 1024
MAX_ARCHIVE_BYTES = 4 * 1024 ** 3  # Single-object upload; fail rather than truncate.


def encryption_key():
    try:
        key = base64.b64decode(os.environ['BACKUP_ENCRYPTION_KEY'], altchars=b'-_', validate=True)
        if len(key) != 32:
            raise ValueError()
        return key
    except (KeyError, ValueError):
        raise ops.OpsError('backup_encryption_key_invalid') from None


def _digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _private_output(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')


def encrypt_file(source, destination, key):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    nonce = secrets.token_bytes(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    with Path(source).open('rb') as input_file, _private_output(destination) as output:
        output.write(MAGIC + nonce)
        while chunk := input_file.read(CHUNK):
            output.write(encryptor.update(chunk))
        output.write(encryptor.finalize())
        output.write(encryptor.tag)
    return _digest(destination)


def decrypt_file(source, destination, key):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    created = False
    try:
        with Path(source).open('rb') as input_file:
            if input_file.read(len(MAGIC)) != MAGIC:
                raise ops.OpsError('encrypted_archive_format_invalid')
            nonce = input_file.read(12)
            if len(nonce) != 12 or Path(source).stat().st_size < len(MAGIC) + 28:
                raise ops.OpsError('encrypted_archive_format_invalid')
            input_file.seek(-16, os.SEEK_END)
            tag = input_file.read(16)
            remaining = input_file.tell() - len(MAGIC) - 12 - 16
            input_file.seek(len(MAGIC) + 12)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(MAGIC)
            with _private_output(destination) as output:
                created = True
                while remaining:
                    chunk = input_file.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ops.OpsError('encrypted_archive_format_invalid')
                    output.write(decryptor.update(chunk))
                    remaining -= len(chunk)
                output.write(decryptor.finalize())
        return _digest(destination)
    except InvalidTag:
        if created:
            Path(destination).unlink(missing_ok=True)
        raise ops.OpsError('encrypted_archive_authentication_failed') from None
    except Exception:
        if created:
            Path(destination).unlink(missing_ok=True)
        raise


def _storage():
    import boto3
    from botocore.config import Config
    required = ('ENDPOINT_URL', 'BUCKET', 'ACCESS_KEY_ID', 'SECRET_ACCESS_KEY')
    if any(not os.environ.get('BACKUP_S3_' + key) for key in required):
        raise ops.OpsError('backup_storage_configuration_missing')
    endpoint = os.environ['BACKUP_S3_ENDPOINT_URL']
    parts = urlsplit(endpoint)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ops.OpsError('backup_storage_https_required')
    style = os.environ.get('BACKUP_S3_URL_STYLE', 'virtual')
    if style not in ('virtual', 'path'):
        raise ops.OpsError('backup_storage_url_style_invalid')
    client = boto3.client('s3', endpoint_url=endpoint,
        aws_access_key_id=os.environ['BACKUP_S3_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['BACKUP_S3_SECRET_ACCESS_KEY'],
        region_name=os.environ.get('BACKUP_S3_REGION', 'auto'),
        config=Config(signature_version='s3v4', connect_timeout=10, read_timeout=60,
                      retries={'max_attempts': 3, 'mode': 'standard'},
                      request_checksum_calculation='when_required',
                      response_checksum_validation='when_required', s3={'addressing_style': style}))
    return client, os.environ['BACKUP_S3_BUCKET']


def _upload(client, bucket, key, path):
    if Path(path).stat().st_size > MAX_ARCHIVE_BYTES:
        raise ops.OpsError('backup_archive_too_large')
    with Path(path).open('rb') as source:
        client.put_object(Bucket=bucket, Key=key, Body=source, IfNoneMatch='*',
                          ContentType='application/octet-stream')


def _download(client, bucket, key, path, expected_digest):
    response = client.get_object(Bucket=bucket, Key=key)
    body = response['Body']
    try:
        total = 0
        with _private_output(path) as output:
            while chunk := body.read(CHUNK):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise ops.OpsError('backup_archive_too_large')
                output.write(chunk)
        if _digest(path) != expected_digest:
            raise ops.OpsError('uploaded_backup_checksum_mismatch')
    finally:
        body.close()


@contextmanager
def local_restore_database(root):
    """No TCP listener, no reused path, and no production database creation/drop."""
    import psycopg
    from psycopg import sql
    root = Path(root)
    data, socket = root / 'postgres', root / 'socket'
    socket.mkdir(mode=0o700)
    env = ops._client_env()
    ops._run(['initdb', '-D', str(data), '--username=backup_restore', '--auth-local=trust',
              '--auth-host=reject', '--encoding=UTF8', '--no-locale'], env=env)
    process = subprocess.Popen(['postgres', '-D', str(data), '-c', 'listen_addresses=',
                                '-c', 'unix_socket_directories=' + str(socket), '-c', 'port=55432'],
                               env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    old_target = os.environ.get('QA_RESTORE_TEST_DATABASE_URL')
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise ops.OpsError('local_restore_database_start_failed')
            try:
                with psycopg.connect(host=str(socket), port=55432, user='backup_restore',
                                      dbname='postgres', connect_timeout=2, autocommit=True) as conn:
                    conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier('qa_restore_drill')))
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise ops.OpsError('local_restore_database_start_timeout') from None
                time.sleep(0.2)
        os.environ['QA_RESTORE_TEST_DATABASE_URL'] = ('postgresql://backup_restore@/qa_restore_drill?' +
                                                     urlencode({'host': str(socket), 'port': '55432'}))
        yield 'qa_restore_drill'
    finally:
        if old_target is None:
            os.environ.pop('QA_RESTORE_TEST_DATABASE_URL', None)
        else:
            os.environ['QA_RESTORE_TEST_DATABASE_URL'] = old_target
        try:
            ops._run(['pg_ctl', '-D', str(data), '-m', 'immediate', '-w', 'stop'], env=env, timeout=30)
        except ops.OpsError:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _record_status(conn, values):
    with conn.transaction():
        for key, value in values.items():
            conn.execute('INSERT INTO settings(key,value) VALUES (%s,%s) '
                         'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


def run_backup():
    import psycopg
    key = encryption_key()
    client, bucket = _storage()
    prefix = os.environ.get('BACKUP_S3_PREFIX', 'cs-quality').strip('/')
    if not re.fullmatch(r'[A-Za-z0-9_/-]{1,100}', prefix):
        raise ops.OpsError('backup_prefix_invalid')
    database_url, _ = ops._connection('DATABASE_URL')
    # Session lock prevents a manual run and the cron job from overlapping.
    with psycopg.connect(database_url, connect_timeout=10, autocommit=True,
                          options='-c statement_timeout=15000') as conn:
        if not conn.execute('SELECT pg_try_advisory_lock(4391332)').fetchone()[0]:
            raise ops.OpsError('backup_already_running')
        started = time.time()
        _record_status(conn, {'backup_last_attempt': started})
        try:
            with tempfile.TemporaryDirectory(prefix='csqa-backup-') as folder:
                root = Path(folder)
                archive = root / 'database.dump'
                result = ops.backup(archive)
                encrypted = root / 'database.dump.gcm'
                ciphertext_digest = encrypt_file(archive, encrypted, key)
                manifest = ops._manifest_path(archive)
                stamp = datetime.now(timezone.utc).strftime('%Y/%m/%d/%H%M%SZ') + '-' + uuid.uuid4().hex
                object_prefix = prefix + '/' + stamp
                _upload(client, bucket, object_prefix + '/database.dump.gcm', encrypted)
                _upload(client, bucket, object_prefix + '/database.dump.manifest.json', manifest)
                downloaded_encrypted = root / 'downloaded.dump.gcm'
                recovered_archive = root / 'downloaded.dump'
                _download(client, bucket, object_prefix + '/database.dump.gcm', downloaded_encrypted, ciphertext_digest)
                _download(client, bucket, object_prefix + '/database.dump.manifest.json',
                          ops._manifest_path(recovered_archive), _digest(manifest))
                if decrypt_file(downloaded_encrypted, recovered_archive, key) != result['sha256']:
                    raise ops.OpsError('decrypted_backup_checksum_mismatch')
                with local_restore_database(root) as target:
                    restored = ops.restore_check(recovered_archive, target)
                completed = time.time()
                receipt = {'ok': True, 'operation': 'scheduled-backup', 'completed_at': completed,
                           'object_prefix': object_prefix, 'encrypted_sha256': ciphertext_digest,
                           'archive_sha256': result['sha256'], 'bytes': result['bytes'],
                           'restore_verified': True, 'row_counts': restored['row_counts']}
                receipt_path = root / 'verified.json'
                with _private_output(receipt_path) as output:
                    output.write(json.dumps(receipt, sort_keys=True).encode())
                _upload(client, bucket, object_prefix + '/verified.json', receipt_path)
                _download(client, bucket, object_prefix + '/verified.json', root / 'verified-copy.json',
                          _digest(receipt_path))
                _record_status(conn, {'backup_completed_at': completed, 'restore_verified_at': completed,
                                      'backup_error': ''})
                return receipt
        except Exception as error:
            code = str(error) if isinstance(error, ops.OpsError) else 'scheduled_backup_failed'
            try:
                _record_status(conn, {'backup_error': code})
            except Exception:
                pass
            if isinstance(error, ops.OpsError):
                raise
            raise ops.OpsError(code) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('run')
    decrypt = commands.add_parser('decrypt')
    decrypt.add_argument('--source', required=True)
    decrypt.add_argument('--destination', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'run':
            result = run_backup()
        else:
            digest = decrypt_file(args.source, args.destination, encryption_key())
            result = {'ok': True, 'operation': 'decrypt', 'sha256': digest}
    except ops.OpsError as error:
        result = {'ok': False, 'operation': args.command, 'error': str(error)}
    except Exception:
        result = {'ok': False, 'operation': args.command, 'error': 'scheduled_backup_failed'}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
