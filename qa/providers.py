"""Bounded official API clients. Credentials stay in environment variables."""
import base64
import http.client
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from email.utils import parsedate_to_datetime
from .core import EVALUATION_SCHEMA, InvalidEvaluation

class ProviderError(Exception):
    def __init__(self, code, retryable=False, retry_after=None):
        super().__init__(code)
        self.code, self.retryable, self.retry_after = code, retryable, retry_after

def retry_delay(value, now=None):
    """Honor either HTTP Retry-After format with a bounded, nonnegative delay."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        value = value.strip()
        delay = int(value) if value.isdigit() else parsedate_to_datetime(value).timestamp() - (
            time.time() if now is None else now)
        if math.isfinite(delay):
            return min(3600, max(1, math.ceil(delay)))
    except (ValueError, TypeError, OverflowError):
        pass
    return None

def request_json(url, headers, body=None, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**headers, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ProviderError('provider_response_too_large')
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise ProviderError('provider_invalid_shape')
            return response
    except urllib.error.HTTPError as exc:
        # Never log response bodies, recording URLs, credentials, or customer details.
        retry_after = (exc.headers or {}).get('Retry-After', '')
        exc.close()
        raise ProviderError('http_' + str(exc.code), exc.code in (408, 429) or exc.code >= 500,
                            retry_delay(retry_after)) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
        raise ProviderError('network_timeout', True) from None
    except (ValueError, UnicodeError):
        raise ProviderError('provider_invalid_json') from None

class Aircall:
    def __init__(self):
        token = os.environ.get('AIRCALL_ACCESS_TOKEN')
        if token:
            self.headers = {'Authorization': 'Bearer ' + token}
        else:
            key, secret = os.environ.get('AIRCALL_API_ID'), os.environ.get('AIRCALL_API_TOKEN')
            if not key or not secret:
                raise ProviderError('aircall_credentials_missing')
            encoded = base64.b64encode((key + ':' + secret).encode()).decode()
            self.headers = {'Authorization': 'Basic ' + encoded}

    def get(self, path):
        return request_json('https://api.aircall.io/v1/' + path, self.headers, timeout=30)

    def call(self, call_id):
        response = self.get('calls/' + urllib.parse.quote(str(call_id), safe=''))
        if not isinstance(response, dict) or not isinstance(response.get('call'), dict):
            raise ProviderError('aircall_invalid_call')
        return response['call']

    def transcript(self, call_id):
        try:
            return self.get('calls/' + urllib.parse.quote(str(call_id), safe='') + '/transcription')
        except ProviderError as exc:
            if exc.code == 'http_404':
                raise ProviderError('transcript_not_ready', True) from None
            raise

    def calls_page(self, start, end, page):
        query = urllib.parse.urlencode({'from': int(start), 'to': int(end), 'per_page': 50,
                                       'page': page, 'order': 'asc'})
        return self.get('calls?' + query)

class OpenAI:
    def __init__(self, model=None):
        self.model = model or os.environ.get('EVALUATOR_MODEL', '')
        self.key = os.environ.get('OPENAI_API_KEY', '')
        if not self.model or not self.key:
            raise ProviderError('evaluator_configuration_missing')
        self.prompt = (Path(__file__).resolve().parents[1] / 'evaluator.md').read_text()

    def evaluate(self, metadata, turns):
        body = {'model': self.model, 'store': False,
                'instructions': self.prompt,
                'input': json.dumps({'metadata': metadata, 'turns': turns}, ensure_ascii=False),
                'max_output_tokens': 6000,
                'text': {'format': {'type': 'json_schema', 'name': 'hitlights_call_evaluation',
                                    'strict': True, 'schema': EVALUATION_SCHEMA}}}
        response = request_json('https://api.openai.com/v1/responses',
                                {'Authorization': 'Bearer ' + self.key}, body)
        if not isinstance(response, dict) or response.get('status') != 'completed':
            raise InvalidEvaluation('evaluator_incomplete_response')
        if not isinstance(response.get('output'), list):
            raise InvalidEvaluation('evaluator_invalid_output')
        pieces = []
        for output in response.get('output', []):
            if not isinstance(output, dict) or not isinstance(output.get('content', []), list):
                raise InvalidEvaluation('evaluator_invalid_output')
            for content in output.get('content', []):
                if not isinstance(content, dict):
                    raise InvalidEvaluation('evaluator_invalid_content')
                if content.get('type') == 'refusal':
                    raise InvalidEvaluation('evaluator_refusal')
                if content.get('type') == 'output_text':
                    if not isinstance(content.get('text'), str):
                        raise InvalidEvaluation('evaluator_invalid_text')
                    pieces.append(content['text'])
        try:
            result = json.loads(''.join(pieces))
        except ValueError:
            raise InvalidEvaluation('evaluator_invalid_json') from None
        return result
