"""hosts.codex.adapter.lock: the lock MCP server Codex runs lists and answers its read tools only
(ported from the Codex branch's tests/codex/test_lock.py)."""
import io
import json
import unittest
from unittest import mock

from hosts.codex.adapter.lock import INSTRUCTIONS, READ_TOOLS, ReadOnlyServer
from kitlib.lockmcp.server import OWNER_ONLY, TOOLS, serve


class Lock(unittest.TestCase):
    def test_discovery_lists_the_read_tools(self):
        tools = ReadOnlyServer().tool_list()
        self.assertEqual({t['name'] for t in tools}, READ_TOOLS)
        self.assertEqual(READ_TOOLS, set(TOOLS) - set(OWNER_ONLY))
        self.assertTrue(READ_TOOLS)

    def test_a_write_call_never_reaches_the_lock(self):
        with mock.patch('kitlib.lockmcp.server.Server.call') as original:
            for name in OWNER_ONLY:
                payload, error = ReadOnlyServer().call(name, {})
                self.assertTrue(error)
                self.assertEqual(payload['error']['code'], 'forbidden')
            original.assert_not_called()

    def test_a_write_over_jsonrpc_or_in_a_batch_is_refused(self):
        calls = [{'jsonrpc': '2.0', 'id': i, 'method': 'tools/call', 'params': {'name': name, 'arguments': {}}}
                 for i, name in enumerate(OWNER_ONLY)]
        with mock.patch('kitlib.lockmcp.server.Server.call') as original:
            single = ReadOnlyServer().handle(calls[0])
            self.assertTrue(single['result']['isError'])
            responses = ReadOnlyServer().handle(calls)
            self.assertEqual(len(responses), len(OWNER_ONLY))
            self.assertTrue(all(r['result']['isError'] for r in responses))
            original.assert_not_called()

    def test_a_read_goes_to_the_lock_api(self):
        with mock.patch('kitlib.lockmcp.server.api.snapshot', return_value={'revision': 7}) as snapshot:
            payload, error = ReadOnlyServer().call('lock_snapshot', {})
        self.assertFalse(error)
        self.assertEqual(payload, {'revision': 7})
        snapshot.assert_called_once()

    def test_the_stdio_handshake_and_discovery(self):
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
        incoming = io.BytesIO(b'\n'.join(json.dumps(r).encode() for r in requests) + b'\n')
        outgoing = io.BytesIO()
        serve(ReadOnlyServer(), incoming, outgoing)
        first, second = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(first['result']['instructions'], INSTRUCTIONS)
        self.assertEqual({t['name'] for t in second['result']['tools']}, READ_TOOLS)


if __name__ == '__main__':
    unittest.main()
