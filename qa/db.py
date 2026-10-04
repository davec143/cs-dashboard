"""Durable single-host pilot storage. Every operation uses its own connection."""
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

DDL = '''
CREATE TABLE IF NOT EXISTS calls (
 id TEXT PRIMARY KEY, metadata TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 generation INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 event_key TEXT PRIMARY KEY, call_id TEXT NOT NULL, kind TEXT NOT NULL, received REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 call_id TEXT PRIMARY KEY REFERENCES calls(id), state TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL, lease_until REAL,
 lease_token TEXT, generation INTEGER, last_error TEXT);
CREATE TABLE IF NOT EXISTS evaluations (
 id INTEGER PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id), agent_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, rubric TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
 total INTEGER, result TEXT NOT NULL, turns TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(call_id,agent_id,fingerprint,rubric,model));
CREATE TABLE IF NOT EXISTS coaching (
 id INTEGER PRIMARY KEY, evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
 owner TEXT NOT NULL, due TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL,
 created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL,
 detail TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
 email TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, email TEXT NOT NULL REFERENCES users(email), csrf TEXT NOT NULL,
 expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts (bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(state,due);
CREATE INDEX IF NOT EXISTS evaluations_latest ON evaluations(call_id,agent_id,id);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires);

'''

class Store:
    def __init__(self, path):
        self.path = str(path)
        self.postgres = self.path.startswith(('postgresql://', 'postgres://'))
        if not self.postgres:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            ddl = DDL.replace('INTEGER PRIMARY KEY', 'BIGSERIAL PRIMARY KEY').replace(' REAL', ' DOUBLE PRECISION') if self.postgres else DDL
            if self.postgres:
                conn.execute('BEGIN IMMEDIATE')
                for statement in ddl.split(';'):
                    if statement.strip():
                        conn.execute(statement)
            else:
                conn.executescript(ddl)

    @contextmanager
    def connect(self):
        if self.postgres:
            import psycopg
            with psycopg.connect(self.path, row_factory=pg_row, connect_timeout=10) as conn:
                conn.execute('SET statement_timeout=15000')
                conn.execute('SET lock_timeout=10000')
                yield PostgresConnection(conn)
            return
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=FULL')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def enqueue(self, metadata, event_key, kind='call.ended', now=None, only_missing=False):
        now = time.time() if now is None else now
        cid = str(metadata['id'])
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if only_missing and conn.execute('SELECT 1 FROM calls WHERE id=?', (cid,)).fetchone():
                return False
            if conn.execute('SELECT 1 FROM events WHERE event_key=?', (event_key,)).fetchone():
                return False
            conn.execute('INSERT INTO events VALUES (?,?,?,?)', (event_key, cid, kind, now))
            existing = conn.execute('SELECT * FROM calls WHERE id=?', (cid,)).fetchone()
            conn.execute('INSERT INTO calls(id,metadata,updated) VALUES (?,?,?) '
                         'ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,updated=excluded.updated',
                         (cid, json.dumps(metadata), now))
            conn.execute('INSERT INTO jobs(call_id,due) VALUES (?,?) ON CONFLICT DO NOTHING', (cid, now))
            # A later asset event can repair missing transcripts; ordinary repeats do not rescore.
            if existing and kind in ('transcription.created', 'call.comm_assets_generated', 'manual.replay'):
                conn.execute('UPDATE calls SET generation=generation+1,state=? WHERE id=?', ('queued', cid))
                conn.execute("UPDATE jobs SET state=CASE WHEN state='processing' THEN state ELSE 'queued' END, "
                             'due=?,attempts=0,last_error=NULL WHERE call_id=?', (now, cid))
            return True

    def claim(self, now=None, lease_seconds=300):
        now = time.time() if now is None else now
        token = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            exhausted = [r[0] for r in conn.execute("SELECT call_id FROM jobs WHERE attempts>=6 AND "
                "(state='queued' OR (state='processing' AND lease_until<=?))", (now,))]
            for cid in exhausted:
                conn.execute("UPDATE jobs SET state='error',lease_until=NULL,lease_token=NULL,"
                             "last_error='retry_budget_exhausted' WHERE call_id=?", (cid,))
                conn.execute("UPDATE calls SET state='error',updated=? WHERE id=?", (now, cid))
            row = conn.execute("SELECT j.*,c.metadata,c.generation AS current_generation FROM jobs j "
                               "JOIN calls c ON c.id=j.call_id WHERE ((j.state='queued' AND j.due<=?) OR "
                               "(j.state='processing' AND j.lease_until<=?)) AND j.attempts<6 ORDER BY due LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE jobs SET state='processing',attempts=attempts+1,lease_until=?,"
                         'lease_token=?,generation=? WHERE call_id=?',
                         (now + lease_seconds, token, row['current_generation'], row['call_id']))
            conn.execute("UPDATE calls SET state='processing' WHERE id=?", (row['call_id'],))
            result = dict(row)
            result.update(lease_token=token, generation=row['current_generation'], attempts=row['attempts'] + 1)
            result['metadata'] = json.loads(result['metadata'])
            return result

    def has_evaluation(self, cid, agent, digest, rubric, model):
        with self.connect() as conn:
            return conn.execute('SELECT * FROM evaluations WHERE call_id=? AND agent_id=? AND fingerprint=? '
                                'AND rubric=? AND model=?', (cid, agent, digest, rubric, model)).fetchone()

    def finish(self, job, state, evaluation=None, error=None, retry_after=None):
        now = time.time()
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            active = conn.execute('SELECT * FROM jobs WHERE call_id=?', (job['call_id'],)).fetchone()
            if not active or active['lease_token'] != job['lease_token'] or active['state'] != 'processing':
                return False
            current = conn.execute('SELECT generation FROM calls WHERE id=?', (job['call_id'],)).fetchone()[0]
            if evaluation:
                e = evaluation
                conn.execute('INSERT INTO evaluations(call_id,agent_id,fingerprint,rubric,model,status,'
                             'total,result,turns,created) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                             (job['call_id'], e['agent_id'], e['fingerprint'], e['rubric'], e['model'],
                              e['status'], e['total'], json.dumps(e['result']), json.dumps(e['turns']), now))
            next_state = 'queued' if current != job['generation'] or retry_after is not None else state
            due = now + (retry_after or 0)
            conn.execute('UPDATE jobs SET state=?,due=?,lease_until=NULL,lease_token=NULL,last_error=? WHERE call_id=?',
                         (next_state, due, error, job['call_id']))
            if current == job['generation']:
                conn.execute('UPDATE calls SET state=?,updated=?,metadata=? WHERE id=?',
                             (next_state, now, json.dumps(job['metadata']), job['call_id']))
            else:
                conn.execute('UPDATE calls SET state=?,updated=? WHERE id=?',
                             (next_state, now, job['call_id']))
            return True

    def setting(self, key, value=None):
        with self.connect() as conn:
            if value is not None:
                conn.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                             (key, str(value)))
                return value
            row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row[0] if row else None

    def snapshot(self):
        with self.connect() as conn:
            states = dict(conn.execute('SELECT state,COUNT(*) FROM calls GROUP BY state').fetchall())
            calls = [dict(r) for r in conn.execute('SELECT c.id,c.metadata,c.state,j.last_error,j.attempts,j.due '
                                                 'FROM calls c JOIN jobs j ON j.call_id=c.id ORDER BY c.updated DESC LIMIT 500')]
            evaluations = [dict(r) for r in conn.execute('SELECT e.*,c.state AS current_state FROM evaluations e '
                'JOIN calls c ON c.id=e.call_id WHERE e.id=(' 
                'SELECT MAX(e2.id) FROM evaluations e2 WHERE e2.call_id=e.call_id AND e2.agent_id=e.agent_id) '
                'ORDER BY e.created DESC LIMIT 500')]
            tasks = [dict(r) for r in conn.execute('SELECT * FROM coaching ORDER BY created DESC LIMIT 500')]
            last_event = conn.execute('SELECT MAX(received) FROM events').fetchone()[0]
            oldest_queued = conn.execute("SELECT MIN(due) FROM jobs WHERE state='queued'").fetchone()[0]
            validated_count = conn.execute("SELECT COUNT(*) FROM evaluations e JOIN calls c ON c.id=e.call_id "
                "WHERE c.state='validated' AND e.status='validated' AND e.id=(SELECT MAX(e2.id) "
                "FROM evaluations e2 WHERE e2.call_id=e.call_id AND e2.agent_id=e.agent_id)").fetchone()[0]
            scored = conn.execute("SELECT e.total,e.result,e.created,c.metadata FROM evaluations e JOIN calls c "
                "ON c.id=e.call_id WHERE c.state='validated' AND e.status='validated' AND e.id=(SELECT MAX(e2.id) "
                "FROM evaluations e2 WHERE e2.call_id=e.call_id AND e2.agent_id=e.agent_id)").fetchall()
            task_states = dict(conn.execute('SELECT status,COUNT(*) FROM coaching GROUP BY status').fetchall())
        summary = {'agents': {}, 'dimensions': {}, 'themes': {}, 'call_types': {}, 'daily': {},
                   'mean': None, 'latest_evaluation': None, 'task_states': task_states}
        totals = []
        for row in scored:
            result, meta = json.loads(row['result']), json.loads(row['metadata'])
            totals.append(row['total'])
            name = meta.get('agent', 'Unassigned')
            summary['agents'].setdefault(name, []).append(row['total'])
            summary['latest_evaluation'] = max(row['created'], summary['latest_evaluation'] or 0)
            kind = result.get('call_type', 'unknown')
            summary['call_types'][kind] = summary['call_types'].get(kind, 0) + 1
            started = meta.get('started_at') or row['created']
            day = datetime.fromtimestamp(float(started), timezone.utc).date().isoformat()
            summary['daily'].setdefault(day, []).append(row['total'])
            for dim, item in result['dimensions'].items():
                if item['applicability'] == 'applicable':
                    summary['dimensions'].setdefault(dim, []).append(item['anchor'] * 25)
            for item in result['improvements']:
                theme = item['theme']
                summary['themes'][theme] = summary['themes'].get(theme, 0) + 1
        def aggregate(values):
            return {'count': len(values), 'mean': round(sum(values) / len(values), 1)}
        for key in ('agents', 'dimensions', 'daily'):
            summary[key] = {name: aggregate(values) for name, values in summary[key].items()}
        if totals:
            summary['mean'] = aggregate(totals)['mean']
        for c in calls:
            c['metadata'] = json.loads(c['metadata'])
        for e in evaluations:
            e['result'] = json.loads(e['result'])
            e['turns'] = json.loads(e['turns'])
        for e in evaluations:
            e['eligible'] = e['status'] == 'validated' and e.pop('current_state') == 'validated'
        return {'states': states, 'calls': calls, 'evaluations': evaluations, 'tasks': tasks,
                'validated_count': validated_count, 'recent_limit': 500, 'summary': summary,
                'last_event': last_event, 'oldest_queued': oldest_queued,
                'last_reconcile': self.setting('last_reconcile'),
                'reconcile_error': self.setting('reconcile_error'), 'worker_heartbeat': self.setting('worker_heartbeat')}

    def create_task(self, evaluation_id, owner, due, action, actor):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute("SELECT 1 FROM evaluations e JOIN calls c ON c.id=e.call_id "
                "WHERE e.id=? AND e.status='validated' AND c.state='validated' AND e.id=(SELECT MAX(e2.id) "
                "FROM evaluations e2 WHERE e2.call_id=e.call_id AND e2.agent_id=e.agent_id)", (evaluation_id,)).fetchone():
                raise ValueError('validated evaluation required')
            cur = conn.execute('INSERT INTO coaching(evaluation_id,owner,due,action,status,created,updated) '
                               "VALUES (?,?,?,?,'open',?,?) RETURNING id", (evaluation_id, owner, due, action, time.time(), time.time()))
            task_id = cur.fetchone()[0]
            conn.execute('INSERT INTO audit_log(actor,action,subject,detail,created) VALUES (?,?,?,?,?)',
                         (actor, 'coaching.created', str(task_id), '{}', time.time()))
            return task_id

    def task_status(self, task_id, status, actor):
        if status not in ('open', 'practicing', 'verified', 'dismissed'):
            raise ValueError('invalid task status')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            cur = conn.execute('UPDATE coaching SET status=?,updated=? WHERE id=?', (status, time.time(), task_id))
            if not cur.rowcount:
                raise ValueError('unknown task')
            conn.execute('INSERT INTO audit_log(actor,action,subject,detail,created) VALUES (?,?,?,?,?)',
                         (actor, 'coaching.status', str(task_id), json.dumps({'status': status}), time.time()))


def database_path():
    return os.environ.get('DATABASE_URL') or os.environ.get('QA_DB', Path(__file__).resolve().parents[1] / 'runtime/pilot.db')

class PostgresRow(dict):
    """Match SQLite Row's mapping plus positional access used by Store."""
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)
    def __iter__(self):
        return iter(self.values())

def pg_row(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: PostgresRow(zip(names, values))

class PostgresConnection:
    def __init__(self, conn):
        self.conn = conn
    def execute(self, sql, parameters=()):
        if sql == 'BEGIN IMMEDIATE':
            # Serialize short write transactions across receiver/worker replicas.
            # External API requests never hold this lock. Correctness first at CS volume.
            return self.conn.execute('SELECT pg_advisory_xact_lock(4391331)')
        return self.conn.execute(sql.replace('?', '%s'), parameters)
