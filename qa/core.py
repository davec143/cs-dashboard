"""Pure normalization and evidence validation. No network or persistence."""
import hashlib
import json
import math
import re

RUBRIC_VERSION = 'hl-service-2.1'
DIMENSIONS = ('rapport', 'needs_discovery', 'technical_escalation',
              'retention_value', 'call_flow', 'appreciative_closing')
CALL_TYPES = ('service', 'sales', 'outbound_followup', 'voicemail',
              'wrong_number', 'insufficient', 'unknown')
THEMES = ('discovery', 'accuracy_review', 'ownership', 'next_steps',
          'empathy', 'value', 'efficiency', 'handoff', 'other')

class InvalidEvaluation(ValueError):
    pass

def object_schema(properties):
    return {'type': 'object', 'properties': properties,
            'required': list(properties), 'additionalProperties': False}

def array_schema(item):
    return {'type': 'array', 'items': item}

def enum_schema(values):
    return {'type': 'string', 'enum': list(values)}

EVIDENCE_SCHEMA = object_schema({'turn_id': {'type': 'integer'}, 'quote': {'type': 'string'}})
DIMENSION_SCHEMA = object_schema({
    'applicability': enum_schema(('applicable', 'not_applicable', 'unobservable')),
    'anchor': {'type': ['integer', 'null'], 'enum': [0, 1, 2, 3, 4, None]},
    'reason': {'type': 'string'}, 'evidence': array_schema(EVIDENCE_SCHEMA)})
FEEDBACK_SCHEMA = object_schema({'theme': enum_schema(THEMES), 'behavior': {'type': 'string'},
    'evidence': array_schema(EVIDENCE_SCHEMA), 'next_action': {'type': 'string'}})
EVALUATION_SCHEMA = object_schema({
    'call_type': enum_schema(CALL_TYPES), 'context': {'type': 'string'},
    'dimensions': object_schema({d: DIMENSION_SCHEMA for d in DIMENSIONS}),
    'strengths': array_schema(FEEDBACK_SCHEMA), 'improvements': array_schema(FEEDBACK_SCHEMA),
    'review_reasons': array_schema({'type': 'string'})})

def validate_schema(value, schema, path='$'):
    """Reject drift even if a provider's structured-output enforcement changes."""
    types = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
    actual = ('null' if value is None else 'boolean' if isinstance(value, bool) else
              'integer' if isinstance(value, int) else 'number' if isinstance(value, float) else
              'string' if isinstance(value, str) else 'array' if isinstance(value, list) else
              'object' if isinstance(value, dict) else 'unsupported')
    if actual not in types or isinstance(value, float) and not math.isfinite(value):
        raise InvalidEvaluation(path + ': wrong type')
    if 'enum' in schema and value not in schema['enum']:
        raise InvalidEvaluation(path + ': invalid enum')
    if actual == 'object':
        if set(value) != set(schema['properties']):
            raise InvalidEvaluation(path + ': missing or unexpected fields')
        for key in value:
            validate_schema(value[key], schema['properties'][key], path + '.' + key)
    elif actual == 'array':
        for i, item in enumerate(value):
            validate_schema(item, schema['items'], path + '[' + str(i) + ']')

def redact(text):
    """Best-effort contact/number redaction; never claim complete anonymization."""
    text = re.sub(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[EMAIL]', str(text), flags=re.I)
    text = re.sub(r'(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)', '[NUMBER]', text)
    return text

def canonicalize(payload, speaker_map=None):
    """Keep source speaker IDs; map roles only using trusted configuration."""
    speaker_map = speaker_map or {}
    transcript = payload.get('transcription', payload) if isinstance(payload, dict) else payload
    content = transcript.get('content', transcript) if isinstance(transcript, dict) else transcript
    utterances = content.get('utterances') if isinstance(content, dict) else None
    if not isinstance(utterances, list) or not utterances:
        raise InvalidEvaluation('structured_transcript_required')
    turns = []
    for u in utterances:
        if not isinstance(u, dict) or not isinstance(u.get('text'), str):
            raise InvalidEvaluation('invalid_utterance')
        text = redact(u['text']).strip()
        if not text:
            continue
        participant = u.get('participant_type')
        if participant == 'internal' and u.get('user_id') is not None:
            speaker = 'user:' + str(u['user_id'])
            provider_role = {'role': 'agent', 'agent_id': str(u['user_id'])}
        elif participant == 'external':
            speaker = 'external'
            provider_role = {'role': 'customer'}
        elif participant == 'ai_voice_agent':
            speaker = 'ai:' + str(u.get('ai_voice_agent_id', 'unknown'))
            provider_role = {'role': 'ai'}
        else:
            speaker = u.get('speaker_id', u.get('speaker', 'unknown'))
            provider_role = {}
        if isinstance(speaker, dict):
            speaker = speaker.get('id', 'unknown')
        sid = str(speaker)
        mapped = provider_role or speaker_map.get(sid, {})
        role = mapped.get('role', 'unknown')
        if role not in ('agent', 'customer', 'ai', 'unknown'):
            raise InvalidEvaluation('invalid_speaker_configuration')
        turns.append({'id': len(turns) + 1, 'speaker_id': sid, 'role': role,
                      'agent_id': str(mapped.get('agent_id', '')),
                      'text': text, 'start': u.get('start', u.get('start_time')),
                      'end': u.get('end', u.get('end_time'))})
    if not turns:
        raise InvalidEvaluation('empty_transcript')
    return turns

def fingerprint(turns):
    return hashlib.sha256(json.dumps(turns, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def early_disposition(call):
    # Duration is intentionally not a scoring/exclusion rule.
    if call.get('voicemail'):
        return 'voicemail'
    if call.get('missed_call_reason'):
        return 'unanswered'
    if call.get('answered_at') is None:
        return 'unanswered'
    return None

def evaluate_gate(result, turns, target_agent_id):
    validate_schema(result, EVALUATION_SCHEMA)
    if len(result['improvements']) > 3:
        raise InvalidEvaluation('at_most_three_improvements_required')
    by_id = {t['id']: t for t in turns}
    target = [t for t in turns if t['role'] == 'agent' and t['agent_id'] == str(target_agent_id)]
    reasons = list(result['review_reasons'])
    if not target:
        reasons.append('Human agent speaker attribution not verified')
    if not any(t['role'] == 'customer' for t in turns):
        reasons.append('Customer speaker attribution not verified')
    if any(t['role'] == 'unknown' for t in turns):
        reasons.append('Unresolved speaker attribution')

    def check_evidence(evidence):
        for e in evidence:
            turn = by_id.get(e['turn_id'])
            if not e['quote'].strip() or turn is None or e['quote'] not in turn['text']:
                raise InvalidEvaluation('evidence_quote_not_in_source')
        return any(by_id[e['turn_id']]['role'] == 'agent' and
                   by_id[e['turn_id']]['agent_id'] == str(target_agent_id) for e in evidence)

    applicable = []
    for name, d in result['dimensions'].items():
        if not d['reason'].strip():
            raise InvalidEvaluation('dimension_reason_required')
        has_agent = check_evidence(d['evidence'])
        if d['applicability'] == 'applicable':
            if d['anchor'] is None or not has_agent:
                raise InvalidEvaluation('scored_dimension_requires_agent_evidence')
            applicable.append(d['anchor'] * 25)
        elif d['anchor'] is not None:
            raise InvalidEvaluation('unscored_dimension_must_have_null_anchor')
        elif d['applicability'] == 'unobservable':
            reasons.append(name + ': unobservable')
    for feedback in result['strengths'] + result['improvements']:
        if not feedback['behavior'].strip() or not feedback['next_action'].strip():
            raise InvalidEvaluation('feedback_behavior_and_action_required')
        if not check_evidence(feedback['evidence']):
            raise InvalidEvaluation('feedback_requires_target_agent_evidence')
        if feedback['theme'] == 'accuracy_review':
            reasons.append('Technical accuracy requires subject-matter review')
    excluded = result['call_type'] in ('voicemail', 'wrong_number', 'insufficient')
    if excluded:
        if applicable or result['strengths'] or result['improvements']:
            raise InvalidEvaluation('excluded_calls_must_not_have_scores_or_coaching')
        return {'status': 'excluded', 'total': None, 'review_reasons': reasons}
    if result['call_type'] == 'unknown':
        reasons.append('Call type unresolved')
    if len(applicable) < 3:
        reasons.append('Fewer than three observable dimensions')
    if not result['strengths']:
        reasons.append('No evidenced strength; human review required')
    if not result['context'].strip():
        raise InvalidEvaluation('context_required')
    # Python round() uses bankers rounding; scores use conventional half-up.
    total = math.floor(sum(applicable) / len(applicable) + .5) if applicable else None
    return {'status': 'review' if reasons else 'validated',
            'total': total, 'review_reasons': list(dict.fromkeys(reasons))}
