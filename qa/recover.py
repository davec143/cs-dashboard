"""Plan missing-call recovery without transcripts/model calls; enqueue only explicitly."""
import argparse
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from .db import Store, database_path
from .providers import Aircall, ProviderError
from .worker import ROOT, load_config, minimal_call


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Use an explicit UTC offset or Z for both timestamps.')
    return parsed.timestamp()


def existing_ids(path):
    if str(path).startswith(('postgresql://', 'postgres://')):
        import psycopg
        with psycopg.connect(str(path), options='-c default_transaction_read_only=on', connect_timeout=10) as conn:
            return {r[0] for r in conn.execute('SELECT id FROM calls')}
    path = Path(path).resolve()
    if not path.exists():
        return set()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        return {r[0] for r in conn.execute('SELECT id FROM calls')}


def plan_recovery(aircall, config, known, start, end, pause=time.sleep):
    if not 0 <= start < end or end - start > 31 * 86400:
        raise ValueError('Choose an increasing range of at most 31 days.')
    enabled = {str(k) for k, v in config.get('agents', {}).items() if v.get('enabled')}
    if not enabled:
        raise ValueError('Configure verified enabled agents first.')
    missing, seen = {}, set()
    counts = {'existing': 0, 'out_of_scope': 0, 'not_finished': 0}
    for page in range(1, 201):
        response = aircall.calls_page(start, end, page)
        calls = response.get('calls')
        meta = response.get('meta')
        if not isinstance(calls, list) or not isinstance(meta, dict) or 'next_page_link' not in meta:
            raise ProviderError('recovery_invalid_page')
        for call in calls:
            item = minimal_call(call)
            cid = item['id']
            if cid in seen:
                continue
            seen.add(cid)
            started = item.get('started_at')
            if not isinstance(started, (int, float)) or not start <= started < end:
                raise ProviderError('recovery_call_outside_requested_range')
            if item['agent_id'] not in enabled:
                counts['out_of_scope'] += 1
            elif call.get('status') != 'done' or not call.get('ended_at'):
                counts['not_finished'] += 1
            elif cid in known:
                counts['existing'] += 1
            else:
                missing[cid] = item
        if not meta['next_page_link']:
            return {'start': start, 'end_exclusive': end, 'seen': len(seen),
                    **counts, 'missing': list(missing.values())}
        pause(1.1)
    raise ProviderError('recovery_window_exceeds_10000_calls')


def apply_recovery(store, plan):
    # Atomic absence check also protects calls arriving between planning and apply.
    return sum(store.enqueue(m, 'recovery:' + m['id'], 'recovery', only_missing=True)
               for m in plan['missing'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True, help='Inclusive ISO timestamp, with timezone')
    parser.add_argument('--end', required=True, help='Exclusive ISO timestamp, with timezone')
    parser.add_argument('--db', default=database_path())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Default: report only')
    mode.add_argument('--enqueue', action='store_true', help='Queue missing calls after full successful scan')
    args = parser.parse_args()
    try:
        start, end = timestamp(args.start), timestamp(args.end)
        # Aircall's to bound can be inclusive; subtract one second for our half-open range.
        if start != int(start) or end != int(end):
            raise ValueError('Use whole-second timestamps.')
        class WindowClient:
            def __init__(self):
                self.client = Aircall()
            def calls_page(self, start, end, page):
                return self.client.calls_page(start, end - 1, page)
        plan = plan_recovery(WindowClient(), load_config(), existing_ids(args.db), start, end)
        count = apply_recovery(Store(args.db), plan) if args.enqueue else 0
        report = {k: v for k, v in plan.items() if k != 'missing'}
        report.update(mode='enqueue' if args.enqueue else 'dry-run', queued=count,
                      missing_count=len(plan['missing']), missing_call_ids=[m['id'] for m in plan['missing']])
        print(json.dumps(report, indent=2))
    except (ProviderError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f'Recovery stopped: {exc}\n')


if __name__ == '__main__':
    main()
