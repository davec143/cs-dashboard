"""Bounded official API clients. Credentials stay in environment variables."""
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from .core import EVALUATION_SCHEMA, InvalidEvaluation

class ProviderError(Exception):
    def __init__(self, code, retryable=False, retry_after=None):
        super().__init__(code)
        self.code, self.retryable, self.retry_after = code, retryable, retry_after

def request_json(url, headers, body=None, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**headers, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ProviderError('provider_response_too_large')
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        # Never log response bodies, recording URLs, credentials, or customer details.
        retry_after = exc.headers.get('Retry-After', '')
        raise ProviderError('http_' + str(exc.code), exc.code in (408, 429) or exc.code >= 500,
                            min(int(retry_after), 3600) if retry_after.isdigit() else None) from None
    except (urllib.error.URLError, TimeoutError):
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
        return self.get('calls/' + urllib.parse.quote(str(call_id), safe=''))['call']

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
        if response.get('status') != 'completed':
            raise InvalidEvaluation('evaluator_incomplete_response')
        pieces = []
        for output in response.get('output', []):
            for content in output.get('content', []):
                if content.get('type') == 'refusal':
                    raise InvalidEvaluation('evaluator_refusal')
                if content.get('type') == 'output_text':
                    pieces.append(content.get('text', ''))
        try:
            result = json.loads(''.join(pieces))
        except ValueError:
            raise InvalidEvaluation('evaluator_invalid_json') from None
        return result
