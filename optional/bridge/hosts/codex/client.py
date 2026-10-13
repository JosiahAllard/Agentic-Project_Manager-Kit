"""Thin consumer of the shared bridge's initialized native MCP frontend.

The host supplies a session-bound request callback, with caller metadata outside
ordinary tool arguments. This module creates no transport, registry, owner grant,
route, retry queue or message store. The shared bridge owns those decisions.
"""
import copy
import json
import re


TOOLS = frozenset({'bridge_connections', 'bridge_send', 'bridge_receipt',
                  'bridge_read_reply', 'bridge_reply', 'bridge_events', 'bridge_poll_native'})
MAX_BYTES = 1024 * 1024
_CODE = re.compile(r'[a-z0-9][a-z0-9-]{0,95}\Z')


class BridgeUnavailable(RuntimeError):
    """The existing native frontend or its required capability is unavailable."""


class BridgeRefused(RuntimeError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and _CODE.fullmatch(code) else 'bridge-call-refused'
        super().__init__(self.code)


def _bounded(value):
    try:
        if len(json.dumps(value, allow_nan=False).encode('utf-8')) > MAX_BYTES:
            raise ValueError('size')
    except (TypeError, ValueError, RecursionError):
        raise BridgeUnavailable('invalid or oversized bridge frontend response') from None
    return value


class BridgeClient:
    """`request(method, params)` returns decoded MCP results on an initialized channel.

Only a trusted host may supply the callback for its actual native caller. A Python
callable or an installed manifest does not prove that channel's identity. The
caller should use the native MCP tools directly when they are already available.
"""
    def __init__(self, request=None):
        if request is not None and not callable(request):
            raise ValueError('a native session-bound MCP request callback is required')
        self.request = request

    def _ask(self, method, params):
        if self.request is None:
            raise BridgeUnavailable('shared native bridge frontend is not connected')
        # Exceptions, including uncertain delivery, propagate. Never retry a send.
        return _bounded(self.request(method, params))

    def capabilities(self):
        result = self._ask('tools/list', {})
        if not isinstance(result, dict) or not isinstance(result.get('tools'), list):
            raise BridgeUnavailable('invalid bridge capability response')
        names = []
        for tool in result['tools']:
            if not isinstance(tool, dict) or not isinstance(tool.get('name'), str):
                raise BridgeUnavailable('invalid bridge tool declaration')
            names.append(tool['name'])
        if len(names) != len(set(names)):
            raise BridgeUnavailable('ambiguous bridge capability response')
        return frozenset(names) & TOOLS

    def _call(self, name, arguments):
        if name not in TOOLS:
            raise ValueError('unsupported shared bridge operation')
        if name not in self.capabilities():
            raise BridgeUnavailable('shared bridge capability is unavailable: ' + name)
        result = self._ask('tools/call', {'name': name, 'arguments': arguments})
        if not isinstance(result, dict) or type(result.get('isError', False)) is not bool:
            raise BridgeUnavailable('invalid bridge tool result')
        payload = result.get('structuredContent')
        if payload is None:
            content = result.get('content')
            if (not isinstance(content, list) or len(content) != 1
                    or not isinstance(content[0], dict) or content[0].get('type') != 'text'
                    or not isinstance(content[0].get('text'), str)):
                raise BridgeUnavailable('bridge tool result has no structured payload')
            try:
                payload = json.loads(content[0]['text'])
            except (ValueError, RecursionError):
                raise BridgeUnavailable('invalid bridge JSON payload') from None
        if not isinstance(payload, dict):
            raise BridgeUnavailable('invalid bridge payload')
        _bounded(payload)
        if result.get('isError', False):
            error = payload.get('error')
            raise BridgeRefused(error.get('code') if isinstance(error, dict) else None)
        # Preserve canonical receipt states. Queued/held/unknown are not delivery.
        return copy.deepcopy(payload)

    def connections(self):
        return self._call('bridge_connections', {})

    def send(self, operation_id, recipient, text, *, expires_at=None):
        args = {'operation_id': operation_id, 'recipient': recipient, 'text': text}
        if expires_at is not None:
            args['expires_at'] = expires_at
        return self._call('bridge_send', args)

    def receipt(self, operation_id):
        return self._call('bridge_receipt', {'operation_id': operation_id})

    def read_reply(self, operation_id):
        return self._call('bridge_read_reply', {'operation_id': operation_id})

    def reply(self, operation_id, text):
        return self._call('bridge_reply', {'operation_id': operation_id, 'text': text})

    def events(self, operation_id, *, after=0, limit=32):
        return self._call('bridge_events', {'operation_id': operation_id, 'after': after, 'limit': limit})

    def poll_native(self, operation_id):
        return self._call('bridge_poll_native', {'operation_id': operation_id})
