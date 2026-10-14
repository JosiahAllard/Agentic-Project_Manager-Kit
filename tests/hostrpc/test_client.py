import io
import json
import unittest

from kitlib.hostrpc import Client


class Rpc(unittest.TestCase):
    def client(self, messages, **kwargs):
        outgoing = io.StringIO()
        client = Client(io.StringIO(''.join(json.dumps(m) + '\n' for m in messages)), outgoing,
                        timeout=1, **kwargs)
        client.reader.join(timeout=1)
        return client, outgoing

    def test_server_requests_never_masquerade_as_matching_responses(self):
        client, outgoing = self.client([{'id': 1, 'method': 'execute', 'params': {}},
                                       {'id': 1, 'result': {'ok': True}}])
        self.assertEqual(client.request('read'), {'ok': True})
        sent = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(sent[1]['error']['code'], -32601)

    def test_nested_requests_preserve_the_outer_response(self):
        client, outgoing = self.client([{'id': 90, 'method': 'kit', 'params': {}},
                                       {'id': 1, 'result': 'outer'}, {'id': 2, 'result': 'inner'}])
        client.on_request = lambda method, params: client.request('nested')
        self.assertEqual(client.request('outer'), 'outer')
        sent = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(sent[-1], {'id': 90, 'result': 'inner'})

    def test_events_are_observed_but_never_accepted_as_results(self):
        seen = []
        client, _ = self.client([{'method': 'event', 'params': {'value': 1}}, {'id': 1, 'result': 2}],
                               on_event=lambda method, params: seen.append((method, params)))
        self.assertEqual(client.request('read'), 2)
        self.assertEqual(seen, [('event', {'value': 1})])

    def test_pending_request_replay_is_deferred_and_never_reexecuted(self):
        replay = {'id': 90, 'method': 'kit', 'params': {'call': 'same'}}
        client, outgoing = self.client([replay, replay, {'id': 1, 'result': 'outer'}, {'id': 2, 'result': 'inner'}])
        called = []
        def handle(method, params):
            called.append(params)
            return client.request('nested')
        client.on_request = handle
        self.assertEqual(client.request('outer'), 'outer')
        self.assertEqual(len(called), 1)
        replies = [json.loads(line) for line in outgoing.getvalue().splitlines() if '"id": 90' in line]
        self.assertEqual(replies, [{'id': 90, 'result': 'inner'}, {'id': 90, 'result': 'inner'}])

    def test_end_of_stream_and_native_errors_are_explicit(self):
        client, _ = self.client([{'id': 1, 'error': {'message': 'denied'}}])
        with self.assertRaisesRegex(RuntimeError, 'denied'):
            client.request('write')
        client, _ = self.client([])
        with self.assertRaises(EOFError):
            client.request('read')

    def test_expired_server_results_are_never_executed_again(self):
        client, _ = self.client([])
        called = []
        client.on_request = lambda method, params: called.append(params)
        original = {'id': 90, 'method': 'kit', 'params': {'call': 'original'}}
        client._handle(original)
        for key in range(100, 164):
            client._handle({'id': key, 'method': 'kit'})
        with self.assertRaisesRegex(RuntimeError, 'expired'):
            client._handle(original)
        self.assertEqual(len(called), 65)

    def test_callback_mutation_does_not_change_cached_signature(self):
        client, outgoing = self.client([])
        def mutate(method, params):
            params['value'] = 'changed'
            return 'done'
        client.on_request = mutate
        client._handle({'id': 90, 'method': 'kit', 'params': {'value': 'original'}})
        client._handle({'id': 90, 'method': 'kit', 'params': {'value': 'original'}})
        self.assertEqual(len(outgoing.getvalue().splitlines()), 2)

    def test_read_ahead_is_bounded_and_overflow_is_explicit(self):
        client, _ = self.client([{'method': 'event'} for _ in range(257)])
        self.assertEqual(client.messages.qsize(), 256)
        for _ in range(256):
            client.pump()
        with self.assertRaisesRegex(RuntimeError, 'RPC read failed'):
            client.pump()

    def test_write_failure_removes_pending_identity(self):
        client, outgoing = self.client([])
        outgoing.close()
        with self.assertRaises(ValueError):
            client.request('read')
        self.assertEqual(client.pending, set())
