import json
import unittest
from unittest import mock

from optional.bridge.hosts.codex.client import BridgeClient, BridgeRefused, BridgeUnavailable, TOOLS


class BridgeConsumer(unittest.TestCase):
    def request(self, payload, *, failed=False):
        return mock.Mock(side_effect=lambda method, params:
            {'tools': [{'name': name} for name in sorted(TOOLS)]} if method == 'tools/list'
            else {'isError': failed, 'structuredContent': payload})

    def test_send_uses_shared_tool_without_sender_owner_or_metadata_arguments(self):
        request = self.request({'state': 'queued', 'operation_id': 'operation'})
        result = BridgeClient(request).send('operation', 'selected-reference', 'Literal $(text)')
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(request.call_args.args, ('tools/call', {'name': 'bridge_send', 'arguments': {
            'operation_id': 'operation', 'recipient': 'selected-reference', 'text': 'Literal $(text)'}}))
        with self.assertRaises(TypeError):
            BridgeClient(request).send('operation', 'selected-reference', 'text', sender='forged')

    def test_offline_bridge_has_no_transport_or_authority_fallback(self):
        with self.assertRaisesRegex(BridgeUnavailable, 'not connected'):
            BridgeClient().connections()

    def test_missing_capability_never_submits(self):
        request = mock.Mock(return_value={'tools': [{'name': 'bridge_connections'}]})
        with self.assertRaises(BridgeUnavailable):
            BridgeClient(request).send('operation', 'peer', 'text')
        self.assertEqual([call.args[0] for call in request.call_args_list], ['tools/list'])

    def test_owner_tools_are_not_exposed_by_discovery(self):
        request = mock.Mock(return_value={'tools': [{'name': 'bridge_owner_select'}, {'name': 'bridge_connections'}]})
        client = BridgeClient(request)
        self.assertEqual(client.capabilities(), {'bridge_connections'})
        with self.assertRaises(ValueError):
            client._call('bridge_owner_select', {})

    def test_shared_route_refusal_propagates_without_fresh_approval_or_retry(self):
        request = self.request({'error': {'code': 'route-not-selected'}}, failed=True)
        with self.assertRaises(BridgeRefused) as error:
            BridgeClient(request).send('operation', 'peer', 'text')
        self.assertEqual(error.exception.code, 'route-not-selected')
        self.assertEqual(request.call_count, 2)

    def test_uncertain_transport_failure_never_retries(self):
        request = mock.Mock(side_effect=[{'tools': [{'name': 'bridge_send'}]}, TimeoutError('uncertain')])
        with self.assertRaises(TimeoutError):
            BridgeClient(request).send('operation', 'peer', 'text')
        self.assertEqual(request.call_count, 2)

    def test_canonical_non_delivery_states_are_preserved(self):
        for state in ('queued', 'held', 'unknown'):
            with self.subTest(state=state):
                payload = {'state': state, 'operation_id': 'operation'}
                self.assertEqual(BridgeClient(self.request(payload)).receipt('operation'), payload)

    def test_reply_and_receipt_read_use_recorded_operation_only(self):
        request = self.request({'state': 'accepted'})
        client = BridgeClient(request)
        client.reply('operation', 'reply')
        self.assertEqual(request.call_args.args[1], {'name': 'bridge_reply', 'arguments': {'operation_id': 'operation', 'text': 'reply'}})
        client.read_reply('operation')
        self.assertEqual(request.call_args.args[1]['name'], 'bridge_read_reply')
        client.poll_native('operation')
        self.assertEqual(request.call_args.args[1]['name'], 'bridge_poll_native')

    def test_text_only_mcp_result_parses_without_losing_receipt_state(self):
        request = mock.Mock(side_effect=[{'tools': [{'name': 'bridge_receipt'}]},
            {'content': [{'type': 'text', 'text': json.dumps({'state': 'unknown'})}]}])
        self.assertEqual(BridgeClient(request).receipt('operation'), {'state': 'unknown'})

    def test_malformed_or_oversized_frontend_response_refuses(self):
        for result in ({'tools': [{'name': 'bridge_send'}, {'name': 'bridge_send'}]},
                       {'tools': None}, {'tools': [{'name': 1}]}, {'body': 'x' * (1024 * 1024 + 1)}):
            with self.subTest(result_type=list(result)), self.assertRaises(BridgeUnavailable):
                BridgeClient(mock.Mock(return_value=result)).capabilities()

    def test_refusal_does_not_expose_backend_error_text(self):
        request = self.request({'error': {'code': 'bad\nprivate path', 'message': 'private detail'}}, failed=True)
        with self.assertRaises(BridgeRefused) as error:
            BridgeClient(request).connections()
        self.assertEqual(str(error.exception), 'bridge-call-refused')
