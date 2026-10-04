"""Synthetic fixture only. Never substitute it for real model validation."""
from .core import DIMENSIONS, RUBRIC_VERSION, canonicalize, evaluate_gate, fingerprint

def seed(store):
    if store.snapshot()['calls']:
        return
    mapping = {'0': {'role': 'agent', 'agent_id': 'demo-agent'}, '1': {'role': 'customer'}}
    source = {'content': {'utterances': [
        {'speaker': '0', 'text': 'Thank you for calling HitLights. How can I help with your lighting project?'},
        {'speaker': '1', 'text': 'I need help choosing a connector for my LED strip.'},
        {'speaker': '0', 'text': 'What is the strip width, and where will you install it?'},
        {'speaker': '1', 'text': 'Eight millimeters, under a cabinet.'},
        {'speaker': '0', 'text': 'I will check the exact connector dimensions with our technical team and email you tomorrow.'},
        {'speaker': '1', 'text': 'That works, thank you.'},
        {'speaker': '0', 'text': 'Thank you for choosing HitLights. I will follow up tomorrow with the confirmed options.'}]}}
    turns = canonicalize(source, mapping)
    evidence = lambda n: [{'turn_id': n, 'quote': turns[n - 1]['text']}]
    dimensions = {d: {'applicability': 'applicable', 'anchor': 3,
                     'reason': 'Synthetic example of appropriate observable behavior.', 'evidence': evidence(5)} for d in DIMENSIONS}
    dimensions['rapport']['evidence'] = evidence(1)
    dimensions['needs_discovery']['evidence'] = evidence(3)
    dimensions['needs_discovery']['anchor'] = 2
    dimensions['appreciative_closing']['evidence'] = evidence(7)
    dimensions['appreciative_closing']['anchor'] = 4
    dimensions['retention_value'] = {'applicability': 'not_applicable', 'anchor': None,
                                    'reason': 'No relevant retention or expansion opportunity.', 'evidence': []}
    result = {'call_type': 'service', 'context': 'Synthetic connector-selection conversation for testing the coaching workflow.',
              'dimensions': dimensions, 'strengths': [{'theme': 'ownership',
              'behavior': 'Promises to verify dimensions before giving compatibility advice.',
              'evidence': evidence(5), 'next_action': 'Continue verifying product constraints.'}],
              'improvements': [{'theme': 'discovery', 'behavior': 'Clarify channel clearance before selecting a connector.',
              'evidence': evidence(3), 'next_action': 'Practice asking about width, height, and installation clearance.'}],
              'review_reasons': []}
    gate = evaluate_gate(result, turns, 'demo-agent')
    result['gate'] = gate
    meta = {'id': '900000001', 'agent_id': 'demo-agent', 'agent': 'Sample Agent',
            'direction': 'inbound', 'started_at': 1790942400, 'answered_at': 1790942405}
    store.enqueue(meta, 'demo:1')
    job = store.claim()
    store.finish(job, gate['status'], evaluation={'agent_id': 'demo-agent', 'fingerprint': fingerprint(turns),
        'rubric': RUBRIC_VERSION, 'model': 'synthetic-fixture', 'status': gate['status'], 'total': gate['total'],
        'result': result, 'turns': turns})
    store.enqueue({**meta, 'id': '900000002'}, 'demo:2')
    job = store.claim()
    store.finish(job, 'review', error='speaker_mapping_required')
    store.enqueue({**meta, 'id': '900000003'}, 'demo:3')
    job = store.claim()
    store.finish(job, 'excluded', error='voicemail')
