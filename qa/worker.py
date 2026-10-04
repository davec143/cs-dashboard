"""Run one durable worker and an hourly reconciliation in a separate process."""
import argparse
import hashlib
import json
import os
import random
import time
from pathlib import Path
from urllib.parse import urlparse
from .core import InvalidEvaluation, RUBRIC_VERSION, canonicalize, early_disposition, evaluate_gate, fingerprint
from .db import Store, database_path
from .providers import Aircall, OpenAI, ProviderError

ROOT = Path(__file__).resolve().parents[1]

def minimal_call(call):
    cid = str(call.get('id', ''))
    if not cid.isdigit():
        raise ValueError('invalid_call_id')
    user = call.get('user') or {}
    source = call.get('asset', '')
    parsed = urlparse(source) if isinstance(source, str) else None
    source = source if parsed and parsed.scheme == 'https' and parsed.hostname and \
        (parsed.hostname == 'aircall.io' or parsed.hostname.endswith('.aircall.io')) else ''
    return {'id': cid, 'agent_id': str(user.get('id', '')), 'agent': user.get('name', 'Unassigned'),
            'direction': call.get('direction', 'unknown'), 'started_at': call.get('started_at'),
            'answered_at': call.get('answered_at'), 'ended_at': call.get('ended_at'),
            'duration': call.get('duration'), 'voicemail': bool(call.get('voicemail')),
            'missed_call_reason': call.get('missed_call_reason'), 'source_url': source}

def load_config():
    config = json.loads(Path(os.environ.get('QA_CONFIG', ROOT / 'config.json')).read_text())
    if os.environ.get('QA_AGENTS_JSON'):
        config['agents'] = json.loads(os.environ['QA_AGENTS_JSON'])
    return config

def process_one(store, aircall, evaluator, config):
    job = store.claim()
    if not job:
        return False
    try:
        raw = aircall.call(job['call_id'])
        meta = minimal_call(raw)
        job['metadata'] = meta
        agent = config.get('agents', {}).get(meta['agent_id'])
        if not agent or not agent.get('enabled'):
            store.finish(job, 'excluded', error='unconfigured_agent')
            return True
        disposition = early_disposition(raw)
        if disposition:
            store.finish(job, 'excluded', error=disposition)
            return True
        transcript = aircall.transcript(job['call_id'])
        # Official Aircall participant_type/user_id fields identify humans and AI.
        # Explicit mappings are a fallback only for older speaker-only transcripts.
        mapping = config.get('speaker_maps', {}).get(job['call_id'], {})
        turns = canonicalize(transcript, mapping)
        agent_ids = {t['agent_id'] for t in turns if t['role'] == 'agent'}
        if any(t['role'] == 'unknown' for t in turns) or not agent_ids or agent_ids != {meta['agent_id']}:
            store.finish(job, 'review', error='speaker_mapping_required')
            return True
        digest = fingerprint(turns)
        existing = store.has_evaluation(job['call_id'], meta['agent_id'], digest, RUBRIC_VERSION, evaluator.model)
        if existing:
            store.finish(job, existing['status'])
            return True
        result = evaluator.evaluate(meta, turns)
        gate = evaluate_gate(result, turns, meta['agent_id'])
        result['gate'] = gate
        record = {'agent_id': meta['agent_id'], 'fingerprint': digest,
                  'rubric': RUBRIC_VERSION, 'model': evaluator.model,
                  'status': gate['status'], 'total': gate['total'], 'result': result, 'turns': turns}
        store.finish(job, gate['status'], evaluation=record)
    except ProviderError as exc:
        retry = (exc.retry_after or min(3600, 30 * 2 ** job['attempts']) + random.randint(0, 10)) \
            if exc.retryable and job['attempts'] < 6 else None
        store.finish(job, 'error', error=exc.code, retry_after=retry)
    except InvalidEvaluation as exc:
        store.finish(job, 'review', error=str(exc))
    except (ValueError, KeyError, TypeError):
        store.finish(job, 'review', error='unexpected_provider_shape')
    except Exception:
        # A coding error is visible and bounded, never an endless hot retry.
        store.finish(job, 'error', error='worker_internal_error')
        raise
    return True

def reconcile(store, aircall, end=None):
    end = time.time() if end is None else end
    # Fixed 48h overlap picks up late metadata. First run covers seven days.
    cursor = float(store.setting('reconcile_cursor') or (end - 7 * 86400))
    start = max(0, cursor - 2 * 86400)
    added = 0
    for page in range(1, 201):
        response = aircall.calls_page(start, end, page)
        if not isinstance(response.get('calls'), list) or not isinstance(response.get('meta'), dict) or 'next_page_link' not in response['meta']:
            raise ProviderError('reconcile_invalid_page')
        for call in response['calls']:
            if call.get('status') != 'done' or not call.get('ended_at'):
                continue
            meta = minimal_call(call)
            event_key = 'reconcile:' + meta['id']
            added += int(store.enqueue(meta, event_key, 'reconcile'))
        if not (response.get('meta') or {}).get('next_page_link'):
            store.setting('reconcile_cursor', end)
            store.setting('last_reconcile', end)
            store.setting('reconcile_error', '')
            return added
        time.sleep(1.1)
    raise ProviderError('reconcile_window_exceeds_10000_calls')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--reconcile-only', action='store_true')
    args = parser.parse_args()
    while os.environ.get('QA_PROCESSING_ENABLED', 'true').lower() != 'true':
        if args.once or args.reconcile_only:
            raise SystemExit('Processing is paused. Enable QA_PROCESSING_ENABLED explicitly.')
        print('Worker paused; no Aircall or model requests.', flush=True)
        time.sleep(60)
    config = load_config()
    if not any(a.get('enabled') for a in config.get('agents', {}).values()):
        raise SystemExit('Configure verified enabled Aircall agent IDs before starting the real worker.')
    store = Store(database_path())
    aircall = Aircall()
    evaluator = None if args.reconcile_only else OpenAI()
    next_reconcile = 0
    while True:
        store.setting('worker_heartbeat', time.time())
        if time.time() >= next_reconcile:
            try:
                reconcile(store, aircall)
            except ProviderError as exc:
                store.setting('reconcile_error', exc.code)
            next_reconcile = time.time() + 3600
        if args.reconcile_only:
            break
        worked = process_one(store, aircall, evaluator, config)
        if args.once:
            break
        if not worked:
            time.sleep(2)

if __name__ == '__main__':
    main()
