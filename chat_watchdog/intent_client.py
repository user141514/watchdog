"""Managed Watchdog publishes intents; Sidecar alone executes browser writes."""
from __future__ import annotations
import hashlib
import json
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID
from urllib.request import Request, urlopen

from .contracts import parse_conversation_state, parse_intent_envelope
from .state_client import _conversation_id

DEFAULT_INTENT_URL = 'http://127.0.0.1:7337/internal/conversation-intents'

def _post(endpoint, payload):
    request = Request(endpoint, data=json.dumps(payload).encode('utf-8'),
                      headers={'content-type': 'application/json'}, method='POST')
    with urlopen(request, timeout=5.0) as response:
        if response.status != 200:
            raise RuntimeError(f'intent owner returned HTTP {response.status}')
        return json.loads(response.read().decode('utf-8'))

class SidecarIntentClient:
    def __init__(self, endpoint=DEFAULT_INTENT_URL, *, request_json=_post):
        url = urlsplit(endpoint)
        if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or url.username or url.password or url.query or url.fragment
                or url.path != '/internal/conversation-intents'):
            raise ValueError('intent owner must be the localhost Sidecar intent endpoint')
        self.endpoint = endpoint
        self._request = request_json

    def _sibling(self, path):
        url = urlsplit(self.endpoint)
        return urlunsplit((url.scheme, url.netloc, path, '', ''))

    @staticmethod
    def _registration_id(value):
        if not isinstance(value, str):
            raise ValueError('Watchdog registration identity is required')
        try:
            parsed = UUID(value)
        except ValueError as error:
            raise ValueError('Watchdog registration identity must be a UUID') from error
        if str(parsed) != value.lower():
            raise ValueError('Watchdog registration identity must be a canonical UUID')
        return value

    def bind_watch(self, registration_id, target):
        self._registration_id(registration_id)
        expected_id = _conversation_id(target)
        if expected_id is None:
            raise ValueError('exact conversation target is required')
        result = self._request(self._sibling('/internal/watchdog-bind'),
                               {'registrationId': registration_id, 'target': target})
        if (not isinstance(result, Mapping) or result.get('accepted') is not True
                or result.get('registrationId') != registration_id
                or not isinstance(result.get('target'), str)
                or _conversation_id(result['target']) != expected_id):
            reason = result.get('reason') if isinstance(result, Mapping) else None
            raise RuntimeError(reason or 'invalid Watchdog binding receipt')
        return result

    def withdraw_watch(self, registration_id, target):
        self._registration_id(registration_id)
        result = self._request(self._sibling('/internal/watchdog-withdraw'),
                               {'registrationId': registration_id, 'target': target})
        if (not isinstance(result, Mapping) or result.get('accepted') is not True
                or result.get('quiescent') is not True
                or result.get('registrationId') != registration_id):
            reason = result.get('reason') if isinstance(result, Mapping) else None
            raise RuntimeError(reason or 'Watchdog withdrawal is not confirmed quiescent')
        return result

    def refresh_v1(self, state, *, registration_id):
        self._registration_id(registration_id)
        raw_state = state.to_dict() if hasattr(state, 'to_dict') else state
        authoritative = parse_conversation_state(raw_state).to_dict()
        turn = authoritative['turn']
        material = ['watchdog-refresh/v1', registration_id, authoritative]
        request_id = hashlib.sha256(json.dumps(
            material, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
        payload = {
            'registrationId': registration_id, 'requestId': request_id, 'kind': 'refresh',
            'conversationId': authoritative['conversationId'], 'target': authoritative['target'],
            'expectedStateVersion': authoritative['stateVersion'],
            'expectedWriterEpoch': authoritative['writer']['epoch'],
            'expected': {'userMessageId': turn['userMessageId'],
                         'assistantMessageId': turn['assistantMessageId']},
        }
        result = self._request(self._sibling('/internal/conversation-recovery'), payload)
        if not isinstance(result, Mapping) or not isinstance(result.get('accepted'), bool):
            raise RuntimeError('invalid recovery owner receipt')
        return result

    def submit(self, target, snapshot, text, *, kind='continue'):
        if not snapshot.user_turn_id or not snapshot.assistant_turn_id:
            raise ValueError('both expected message identities are required')
        result = self._request(self.endpoint, {
            'kind': kind, 'target': target, 'text': text,
            'expected': {'userMessageId': snapshot.user_turn_id,
                         'assistantMessageId': snapshot.assistant_turn_id},
        })
        if not isinstance(result, Mapping) or not isinstance(result.get('accepted'), bool):
            raise RuntimeError('invalid intent owner receipt')
        return result

    def submit_v1(self, state, text=None, *, source='watchdog', action='continue', registration_id=None):
        if source == 'watchdog':
            self._registration_id(registration_id)
        if action not in {'continue', 'stop'}:
            raise ValueError('Watchdog v1 intent client only supports continue or stop')
        raw_state = state.to_dict() if hasattr(state, 'to_dict') else state
        authoritative = parse_conversation_state(raw_state).to_dict()
        turn = authoritative['turn']
        material = [
            'conversation-runtime/v1',
            registration_id,
            source,
            action,
            authoritative['target'],
            authoritative['conversationId'],
            authoritative['stateVersion'],
            authoritative['writer']['epoch'],
            turn['userMessageId'],
            turn['assistantMessageId'],
            text,
        ]
        encoded = json.dumps(material, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        intent_id = hashlib.sha256(encoded).hexdigest()
        payload = parse_intent_envelope({
            'contractVersion': 1,
            'intentId': intent_id,
            'source': source,
            'conversationId': authoritative['conversationId'],
            'target': authoritative['target'],
            'expectedStateVersion': authoritative['stateVersion'],
            'expectedWriterEpoch': authoritative['writer']['epoch'],
            'action': action,
            'allocation': None,
            'text': text,
            'expected': {
                'userMessageId': turn['userMessageId'],
                'assistantMessageId': turn['assistantMessageId'],
            },
        }).to_dict()
        endpoint = self.endpoint
        if source == 'watchdog':
            endpoint = self._sibling('/internal/watchdog-intents')
            payload = {'registrationId': registration_id, 'intent': payload}
        result = self._request(endpoint, payload)
        if not isinstance(result, Mapping) or not isinstance(result.get('accepted'), bool):
            raise RuntimeError('invalid intent owner receipt')
        if 'contractVersion' in result and result['contractVersion'] != 1:
            raise RuntimeError('unsupported intent owner response contract')
        return result
