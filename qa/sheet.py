"""Mirror of the "Call Evaluation" Google Sheet (Data tab) and the Sheet dashboard's math.

Make reads the Data tab with the Sheets API and posts the whole range here. Every sync
replaces the mirror in one transaction, so the dashboard always equals the sheet: no
partial states, no drift, rows removed from the sheet disappear here too.
"""
import math
import re
import time
from datetime import date, datetime, timedelta

HEADER = ('Call ID', 'Agent', 'Date', 'Customer Number', 'Total Score', 'Rapport',
          'Needs Discovery', 'Technical / Escalation', 'Retention & Value', 'Call Flow',
          'Appreciative Closing', 'Evaluated', 'Call Type', 'Strengths',
          'Areas for Improvement', 'Call Context', 'Improvement Plan')
SCORES = ('total', 'rapport', 'needs_discovery', 'technical_escalation',
          'retention_value', 'call_flow', 'appreciative_closing')
SCORE_LABELS = dict(zip(SCORES, HEADER[4:11]))
TEXT = ('strengths', 'improvements', 'context', 'plan')
COLUMNS = ('row_number', 'call_id', 'agent', 'call_date', 'customer_number') + SCORES + (
    'evaluated', 'call_type') + TEXT
MAX_ROWS = 200_000
MAX_CELL = 50_000
# A sync that would drop more than this share of existing rows is refused: a truncated
# or wrong-range read must never wipe the dashboard.
MAX_SHRINK = 0.2
STALE_AFTER = 8 * 3600

DDL = '''
CREATE TABLE IF NOT EXISTS sheet_rows (
 row_number INTEGER PRIMARY KEY, call_id TEXT NOT NULL, agent TEXT NOT NULL, call_date TEXT,
 customer_number TEXT NOT NULL, total REAL, rapport REAL, needs_discovery REAL,
 technical_escalation REAL, retention_value REAL, call_flow REAL, appreciative_closing REAL,
 evaluated TEXT NOT NULL, call_type TEXT NOT NULL, strengths TEXT NOT NULL, improvements TEXT NOT NULL,
 context TEXT NOT NULL, plan TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sheet_rows_call ON sheet_rows(call_id);
'''


class SheetError(ValueError):
    pass


def _text(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()[:MAX_CELL]


def _call_id(value):
    text = _text(value)
    if re.fullmatch(r'\d+\.0+', text):
        text = text.split('.')[0]
    if re.fullmatch(r'\d+(\.\d+)?[eE]\+?\d+', text):
        text = str(int(float(text)))
    return text


def _score(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().rstrip('%').strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


SHEETS_EPOCH = date(1899, 12, 30)


def _date(value):
    """Return YYYY-MM-DD for ISO strings, US M/D/YYYY strings, or Sheets serial numbers."""
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 20000 < value < 80000:
            return (SHEETS_EPOCH + timedelta(days=int(value))).isoformat()
        return None
    text = str(value).strip()
    match = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', text)
    try:
        if match:
            return date(*map(int, match.groups())).isoformat()
        match = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', text)
        if match:
            month, day, year = map(int, match.groups())
            return date(year, month, day).isoformat()
        return datetime.strptime(text[:16], '%a, %b %d %Y').date().isoformat()
    except ValueError:
        return None


def parse(payload):
    """Accept the Sheets API values response (or {"values": [...]}) for Data!A:Q."""
    values = payload.get('values') if isinstance(payload, dict) else None
    if not isinstance(values, list) or not values or not isinstance(values[0], list):
        raise SheetError('sheet_values_required')
    header = tuple(_text(c) for c in values[0][:len(HEADER)])
    if header != HEADER:
        raise SheetError('sheet_header_mismatch')
    if len(values) - 1 > MAX_ROWS:
        raise SheetError('sheet_too_many_rows')
    rows = []
    for index, raw in enumerate(values[1:], start=2):
        if not isinstance(raw, list):
            raise SheetError('sheet_row_invalid')
        cells = list(raw[:len(HEADER)]) + [None] * (len(HEADER) - len(raw[:len(HEADER)]))
        call_id = _call_id(cells[0])
        if not call_id:
            continue  # Blank rows below the data are normal in a sheet.
        row = {'row_number': index, 'call_id': call_id, 'agent': _text(cells[1]) or 'Unassigned',
               'call_date': _date(cells[2]), 'customer_number': _text(cells[3])}
        for name, cell in zip(SCORES, cells[4:11]):
            row[name] = _score(cell)
        row.update(evaluated=_text(cells[11]), call_type=_text(cells[12]) or 'Unknown',
                   strengths=_text(cells[13]), improvements=_text(cells[14]),
                   context=_text(cells[15]), plan=_text(cells[16]))
        rows.append(row)
    return rows


def replace(store, rows, force=False, now=None):
    """Atomically make the mirror equal to this snapshot."""
    now = time.time() if now is None else now
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT COUNT(*) FROM sheet_rows').fetchone()[0]
        if not force and existing and len(rows) < existing * (1 - MAX_SHRINK):
            raise SheetError('sheet_snapshot_shrank_unexpectedly')
        conn.execute('DELETE FROM sheet_rows')
        placeholders = ','.join('?' * len(COLUMNS))
        sql = 'INSERT INTO sheet_rows(' + ','.join(COLUMNS) + ') VALUES (' + placeholders + ')'
        for row in rows:
            conn.execute(sql, tuple(row[c] for c in COLUMNS))
        for key, value in (('sheet_last_sync', now), ('sheet_row_count', len(rows)), ('sheet_sync_error', '')):
            conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE '
                         'SET value=excluded.value', (key, str(value)))
    return {'rows': len(rows), 'previous_rows': existing}


def record_error(store, code):
    try:
        store.setting('sheet_sync_error', code)
        store.setting('sheet_last_attempt', time.time())
    except Exception:
        pass


def half_up(value):
    # Same as the sheet's Math.round for these non-negative scores.
    return math.floor(value + 0.5)


def _mean(values):
    values = [v for v in values if v is not None]
    return half_up(sum(values) / len(values)) if values else None


def load_rows(store, full=False):
    columns = COLUMNS if full else tuple(c for c in COLUMNS if c not in TEXT and c != 'customer_number')
    with store.connect() as conn:
        return [dict(r) for r in conn.execute('SELECT ' + ','.join(columns) +
                                              ' FROM sheet_rows ORDER BY row_number')]


def row_detail(store, row_number):
    with store.connect() as conn:
        row = conn.execute('SELECT * FROM sheet_rows WHERE row_number=?', (row_number,)).fetchone()
    return dict(row) if row else None


def summary(store):
    """Reproduce the sheet's Dashboard tab numbers exactly, plus data-health checks."""
    rows = load_rows(store)
    evaluated = [r for r in rows if r['call_type'] == 'Evaluated']
    types = {}
    for r in rows:
        types[r['call_type']] = types.get(r['call_type'], 0) + 1
    agents = {}
    for r in evaluated:
        agents.setdefault(r['agent'], []).append(r)
    scorecard = []
    for name, items in agents.items():
        entry = {'agent': name, 'calls': len(items)}
        entry.update({s: _mean(i[s] for i in items) for s in SCORES})
        scorecard.append(entry)
    scorecard.sort(key=lambda e: (-(e['total'] if e['total'] is not None else -1), e['agent']))
    daily = {}
    for r in evaluated:
        if r['call_date'] and r['total'] is not None:
            daily.setdefault(r['call_date'], []).append(r['total'])
    monthly = {}
    for r in rows:
        if r['call_date']:
            monthly[r['call_date'][:7]] = monthly.get(r['call_date'][:7], 0) + 1
    counts = {}
    for r in rows:
        counts[r['call_id']] = counts.get(r['call_id'], 0) + 1
    duplicates = sorted(cid for cid, n in counts.items() if n > 1)
    missing_scores = [r['call_id'] for r in evaluated if r['total'] is None]
    undated = sum(1 for r in rows if not r['call_date'])
    dates = [r['call_date'] for r in rows if r['call_date']]
    try:
        last_sync = float(store.setting('sheet_last_sync') or 0) or None
    except ValueError:
        last_sync = None
    return {
        'connected': last_sync is not None,
        'last_sync': last_sync,
        'sync_error': store.setting('sheet_sync_error') or '',
        'stale': bool(last_sync) and time.time() - last_sync > STALE_AFTER,
        'total_calls': len(rows), 'unique_calls': len(counts),
        'evaluated': len(evaluated), 'voicemails': types.get('Voicemail', 0),
        'call_types': types,
        'dimensions': [{'key': s, 'label': SCORE_LABELS[s], 'mean': _mean(r[s] for r in evaluated)} for s in SCORES],
        'scorecard': scorecard,
        'daily': [{'date': d, 'calls': len(v), 'mean': half_up(sum(v) / len(v))} for d, v in sorted(daily.items())],
        'monthly': dict(sorted(monthly.items())),
        'latest_call_date': max(dates) if dates else None,
        'health': {'duplicate_call_ids': duplicates, 'duplicate_rows': len(rows) - len(counts),
                   'evaluated_missing_scores': missing_scores, 'undated_rows': undated},
    }


def sync_ok(store):
    """True when the feed is not configured, or the last sync is fresh and clean."""
    import os
    if not os.environ.get('SHEET_INGEST_TOKEN'):
        return True
    try:
        last = float(store.setting('sheet_last_sync') or 0)
    except ValueError:
        return False
    return time.time() - last < STALE_AFTER and not store.setting('sheet_sync_error')
