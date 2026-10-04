"""Run one durable worker and an hourly reconciliation in a separate process."""
import argparse
import json
import math
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
    if not isinstance(call, dict):
        raise ValueError('invalid_call')
    cid = str(call.get('id', ''))
    if not cid.isascii() or not cid.isdigit():
        raise ValueError('invalid_call_id')
    user = call.get('user')
    user = {} if user is None else user
    if not isinstance(user, dict):
        raise ValueError('invalid_call_user')
    agent_id = str(user.get('id') or '')
    if agent_id and (not agent_id.isascii() or not agent_id.isdigit()):
        raise ValueError('invalid_agent_id')
    if not isinstance(user.get('name', 'Unassigned'), str):
        raise ValueError('invalid_agent_name')
    for key in ('started_at', 'answered_at', 'ended_at', 'duration'):
        value = call.get(key)
        if value is not None and (type(value) not in (int, float)
                                  or not 0 <= value <= 253402300799):
            raise ValueError('invalid_call_' + key)
    source = call.get('asset', '')
    parsed = urlparse(source) if isinstance(source, str) else None
    source = source if parsed and parsed.scheme == 'https' and parsed.hostname and \
        (parsed.hostname == 'aircall.io' or parsed.hostname.endswith('.aircall.io')) else ''
    return {'id': cid, 'agent_id': agent_id, 'agent': user.get('name', 'Unassigned'),
            'direction': call.get('direction', 'unknown'), 'started_at': call.get('started_at'),
            'answered_at': call.get('answered_at'), 'ended_at': call.get('ended_at'),
            'duration': call.get('duration'), 'voicemail': bool(call.get('voicemail')),
            'missed_call_reason': call.get('missed_call_reason'), 'source_url': source}

def load_config():
    config = json.loads(Path(os.environ.get('QA_CONFIG', ROOT / 'config.json')).read_text())
    if not isinstance(config, dict):
        raise ValueError('invalid_worker_configuration')
    if os.environ.get('QA_AGENTS_JSON'):
        config['agents'] = json.loads(os.environ['QA_AGENTS_JSON'])
    agents = config.get('agents', {})
    if not isinstance(agents, dict) or any(
        not isinstance(key, str) or not key.isascii() or not key.isdigit()
        or not isinstance(agent, dict) or type(agent.get('enabled', False)) is not bool
        for key, agent in agents.items()
    ):
        raise ValueError('invalid_agent_configuration')
    maps = config.get('speaker_maps', {})
    if not isinstance(maps, dict) or any(
        not isinstance(mapping, dict) or any(not isinstance(item, dict) for item in mapping.values())
        for mapping in maps.values()
    ):
        raise ValueError('invalid_speaker_configuration')
    return config

def configuration_status(config):
    """Publish readiness flags only; never copy provider credentials into storage."""
    present = lambda key: bool(os.environ.get(key, '').strip())
    return {
        'aircall_configured': present('AIRCALL_ACCESS_TOKEN') or (
            present('AIRCALL_API_ID') and present('AIRCALL_API_TOKEN')),
        'openai_configured': present('OPENAI_API_KEY'),
        'model_configured': present('EVALUATOR_MODEL'),
        'agents_configured': config is not None and any(
            agent.get('enabled') is True for agent in config.get('agents', {}).values()),
        'config_valid': config is not None,
    }

def process_one(store, aircall, evaluator, config):
    job = store.claim()
    if not job:
        return False
    try:
        store.setting('worker_heartbeat', time.time())
        raw = aircall.call(job['call_id'])
        meta = minimal_call(raw)
        if meta['id'] != job['call_id']:
            raise ProviderError('provider_call_id_mismatch')
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
        store.setting('worker_heartbeat', time.time())
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
            store.finish(job, existing['status'], evaluation_id=existing['id'])
            return True
        result = evaluator.evaluate(meta, turns)
        gate = evaluate_gate(result, turns, meta['agent_id'])
        result['gate'] = gate
        record = {'agent_id': meta['agent_id'], 'fingerprint': digest,
                  'rubric': RUBRIC_VERSION, 'model': evaluator.model,
                  'status': gate['status'], 'total': gate['total'], 'result': result, 'turns': turns}
        store.finish(job, gate['status'], evaluation=record)
    except ProviderError as exc:
        retry = (exc.retry_after if exc.retry_after is not None else
                 min(3600, 30 * 2 ** job['attempts'] + random.randint(0, 10))) \
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
    if type(end) not in (int, float) or not 0 <= end <= 253402300799:
        raise ProviderError('reconcile_invalid_window')
    # Fixed 48h overlap picks up late metadata. First run covers seven days.
    try:
        cursor = float(store.setting('reconcile_cursor') or max(0, end - 7 * 86400))
    except (ValueError, TypeError):
        raise ProviderError('reconcile_invalid_cursor') from None
    if not math.isfinite(cursor) or not 0 <= cursor <= end:
        raise ProviderError('reconcile_invalid_cursor')
    start = max(0, cursor - 2 * 86400)
    added = 0
    for page in range(1, 201):
        store.setting('worker_heartbeat', time.time())
        response = aircall.calls_page(start, end, page)
        if not isinstance(response, dict) or not isinstance(response.get('calls'), list) or not isinstance(response.get('meta'), dict) or 'next_page_link' not in response['meta']:
            raise ProviderError('reconcile_invalid_page')
        for call in response['calls']:
            if not isinstance(call, dict):
                raise ProviderError('reconcile_invalid_call')
            if call.get('status') != 'done' or not call.get('ended_at'):
                continue
            try:
                meta = minimal_call(call)
            except ValueError:
                raise ProviderError('reconcile_invalid_call') from None
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
    store = Store(database_path())
    # Match receiver semantics: only the explicit value "true" enables provider use.
    while True:
        try:
            config = load_config()
        except (ValueError, OSError, TypeError):
            config = None
        store.setting('worker_configuration', json.dumps(configuration_status(config)))
        store.setting('worker_heartbeat', time.time())
        if os.environ.get('QA_PROCESSING_ENABLED') == 'true':
            break
        if args.once or args.reconcile_only:
            raise SystemExit('Processing is paused. Enable QA_PROCESSING_ENABLED explicitly.')
        print('Worker paused; no Aircall or model requests.', flush=True)
        time.sleep(60)
    if config is None:
        raise SystemExit('Worker configuration is invalid; processing cannot start.')
    if not any(a.get('enabled') for a in config.get('agents', {}).values()):
        raise SystemExit('Configure verified enabled Aircall agent IDs before starting the real worker.')
    aircall = Aircall()
    evaluator = None if args.reconcile_only else OpenAI()
    next_reconcile = 0
    while True:
        store.setting('worker_heartbeat', time.time())
        if time.time() >= next_reconcile:
            reconcile_delay = 3600
            try:
                reconcile(store, aircall)
            except ProviderError as exc:
                store.setting('reconcile_error', exc.code)
                if args.reconcile_only:
                    raise SystemExit('Reconciliation stopped: ' + exc.code) from None
                if exc.retryable:
                    reconcile_delay = exc.retry_after if exc.retry_after is not None else 60
            next_reconcile = time.time() + reconcile_delay
        if args.reconcile_only:
            break
        worked = process_one(store, aircall, evaluator, config)
        if args.once:
            break
        if not worked:
            time.sleep(2)

if __name__ == '__main__':
    main()
