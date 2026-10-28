"""PM routes (README "PM routes"): only the PM-marked chat connects, only kit-launched chats,
only its own routes, in memory, logged, and the owner sees and stops them."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from optional.bridge.core.messages import SenderAuthority
from optional.bridge.win.controller import Controller
from optional.bridge.win.leader import ServiceLeader
from tests.bridge.test_dispatch import TestAdapter
from tests.bridge.test_pairing import chat

PM_LOG = 'pm-routes.log'  # the documented name (README, PM routes)


class PmRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-pm-routes-')
        self.root = Path(self.temp.name)
        self.leader = ServiceLeader(uuid.uuid4().hex)
        self.controller = Controller(self.root, self.leader)
        self.p, self.a, self.b, self.c = chat('p'), chat('a'), chat('b', adapter='test-recipient'), chat('c')
        self.live = {}
        for peer in (self.p, self.a, self.b, self.c): self.enroll(peer)
        self.adapter = TestAdapter('test-recipient')
        self.controller.adapter(self.adapter)
        self.pm = self.frontend(self.p, 'pm')
        self.servers = {'a': self.frontend(self.a, 'worker'), 'b': self.frontend(self.b, 'worker'),
                        'c': self.frontend(self.c, None)}
        self.operation = 0

    def tearDown(self):
        self.controller.close()
        self.leader.close()
        self.temp.cleanup()

    def enroll(self, peer):
        self.live[peer.identity.session_id] = peer
        self.controller.enroll(peer, lambda item: self.live.get(item.identity.session_id) == item,
                               {'title': peer.identity.session_id})

    def frontend(self, binding, mark):
        """A frontend launched with `mark` whose chat has attested once (its first call)."""
        server = self.controller.frontend(
            lambda metadata: SenderAuthority(binding, 'root-' + binding.identity.session_id), mark=mark)
        server.handle({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {}})
        server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        server.calls.observe({})
        return server

    def call(self, server, name, **arguments):
        reply = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                               'params': {'name': name, 'arguments': arguments}})
        return reply['result']['structuredContent']

    def connect(self, sender, recipient, server=None, **options):
        return self.call(server or self.pm, 'bridge_pm_connect', sender=sender.reference,
                         recipient=recipient.reference, **options)

    def send(self, server, recipient):
        self.operation += 1
        return self.call(server, 'bridge_send', operation_id='op-%d' % self.operation,
                         recipient=recipient.reference, text='hello')

    def code(self, result):
        return result.get('error', {}).get('code')

    def stop(self, sender, recipient):
        """The owner's Stop on one PM direction, as (removed, why a reconnect is not blocked)."""
        stopped = self.controller.owner.stop_pm(sender.reference, recipient.reference)
        return stopped.removed, stopped.unblocked

    def log(self):
        path = self.root / PM_LOG
        if not path.exists(): return []
        return [json.loads(line) for line in path.read_text(encoding='ascii').splitlines()]

    # Refusals

    def test_only_a_chat_carrying_the_pm_mark_may_use_the_pm_tools(self):
        for server in (self.servers['a'], self.servers['c']):  # a worker mark, then no mark
            self.assertEqual(self.code(self.connect(self.a, self.b, server)), 'pm-launch-mark-required')
            self.assertEqual(self.code(self.call(server, 'bridge_pm_chats')), 'pm-launch-mark-required')
            self.assertEqual(self.code(self.call(server, 'bridge_pm_disconnect', sender=self.a.reference,
                                                 recipient=self.b.reference)), 'pm-launch-mark-required')
        self.assertEqual(self.controller.pm.current(), ())
        self.assertEqual(self.code(self.send(self.servers['a'], self.b)), 'chat-route-not-selected')
        self.assertEqual([(entry['event'], entry['code']) for entry in self.log()],
                         [('refused', 'pm-launch-mark-required')] * 4)  # connect and disconnect

    def test_no_tool_argument_can_claim_a_mark(self):
        self.assertEqual(self.code(self.connect(self.a, self.b, mark='pm')), 'invalid-tool-arguments')
        self.assertEqual(self.code(self.connect(self.a, self.b, self.servers['a'], mark='pm')),
                         'invalid-tool-arguments')
        self.assertEqual(self.code(self.connect(self.a, self.b, bidirectional='yes')), 'invalid-tool-arguments')
        self.assertEqual(self.controller.pm.current(), ())

    def test_both_chats_must_be_kit_launched_now(self):
        self.assertEqual(self.code(self.connect(self.a, self.c)), 'recipient-not-kit-launched')
        self.assertEqual(self.code(self.connect(self.c, self.a)), 'sender-not-kit-launched')
        d = chat('d')
        self.enroll(d)  # listed, but no frontend of it has attested
        self.assertEqual(self.code(self.connect(self.a, d)), 'recipient-not-kit-launched')
        self.assertEqual(self.code(self.connect(self.a, self.a)), 'same-chat-selection')
        self.assertEqual(self.code(self.call(self.pm, 'bridge_pm_connect', sender=self.a.reference,
                                             recipient=chat('z').reference)), 'chat-not-observed')
        self.assertEqual(self.controller.pm.current(), ())

    def test_codex_to_codex_is_refused(self):
        x, y = chat('x', adapter='codex-desktop'), chat('y', adapter='codex-desktop')
        for peer in (x, y):
            self.enroll(peer)
            self.frontend(peer, 'worker')
        self.assertEqual(self.code(self.connect(x, y, bidirectional=False)), 'host-pair-refused')
        self.assertEqual(self.controller.pm.current(), ())

    def test_the_pm_never_removes_an_owner_route(self):
        self.controller.owner.change(self.a.reference, self.b.reference, enabled=True)
        self.assertEqual(self.code(self.call(self.pm, 'bridge_pm_disconnect', sender=self.a.reference,
                                             recipient=self.b.reference)), 'route-not-made-by-pm')
        self.controller.pairs.require_selected(self.a, self.b)
        # Connecting an owner route makes nothing: the owner's route holds it.
        self.assertEqual(self.connect(self.a, self.b)['routes'],
                         [{'sender': self.a.reference, 'recipient': self.b.reference, 'by': 'owner'},
                          {'sender': self.b.reference, 'recipient': self.a.reference, 'by': 'owner'}])
        self.assertEqual(self.controller.pm.current(), ())

    def test_an_owner_disconnect_refuses_a_pm_connect(self):
        self.controller.owner.change(self.a.reference, self.b.reference, enabled=False, bidirectional=False)
        self.assertEqual(self.code(self.connect(self.a, self.b)), 'owner-disconnected')
        self.assertEqual(self.code(self.connect(self.b, self.a)), 'owner-disconnected')  # both directions asked
        self.assertEqual(self.controller.pm.current(), ())

    def test_a_connect_that_cannot_be_logged_is_refused(self):
        (self.root / PM_LOG).mkdir()
        self.assertEqual(self.code(self.connect(self.a, self.b)), 'pm-route-log-failed')
        self.assertEqual(self.controller.pm.current(), ())
        self.assertEqual(self.code(self.send(self.servers['a'], self.b)), 'chat-route-not-selected')

    def test_a_route_stops_working_once_a_chat_is_no_longer_kit_launched(self):
        self.connect(self.a, self.b)
        self.assertEqual(self.send(self.servers['a'], self.b)['state'], 'queued')
        queued = 'op-%d' % self.operation
        # The same chat attests through an unmarked frontend: its mark is gone.
        unmarked = self.frontend(self.a, None)
        self.assertEqual(self.code(self.send(unmarked, self.b)), 'chat-route-not-selected')
        self.assertEqual(self.controller.dispatch_pending()[0].state, 'refused')
        self.assertEqual(self.controller.outbox.receipt(queued).state, 'refused')
        self.assertEqual(self.adapter.calls, [])
        # Marked again, then the recipient's frontend exits.
        self.assertEqual(self.send(self.servers['a'], self.b)['state'], 'queued')
        self.controller.detach_frontend(self.servers['b'])
        self.assertEqual(self.code(self.send(self.servers['a'], self.b)), 'chat-route-not-selected')
        self.assertEqual([item.state for item in self.controller.dispatch_pending()], ['refused'])
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(len(self.controller.pm.current()), 2)  # still listed for the owner to see and stop

    def test_a_replaced_binding_drops_its_routes(self):
        self.connect(self.a, self.b)
        self.enroll(replace(self.a, permission_revision=2))
        self.assertEqual(self.controller.pm.current(), ())
        self.assertEqual([(entry['event'], entry['by']) for entry in self.log()][-2:],
                         [('replaced', 'bridge')] * 2)

    def test_a_restart_has_no_pm_routes_and_wrote_no_owner_row(self):
        self.connect(self.a, self.b)
        self.assertIsNone(self.controller.pairs.owner_choice(self.a, self.b))
        self.assertEqual(self.controller.pairs.remembered_routes(), ())
        self.assertEqual(self.controller.owner.connections(), ())
        self.controller.close()
        self.controller = Controller(self.root, self.leader)
        for peer in (self.p, self.a, self.b): self.enroll(peer)
        self.pm = self.frontend(self.p, 'pm')
        for peer in (self.a, self.b): self.frontend(peer, 'worker')
        self.assertEqual(self.controller.pm.current(), ())
        with self.assertRaises(ValueError): self.controller.pairs.require_selected(self.a, self.b)

    def test_the_agent_server_and_pm_routes_hold_no_owner_decision(self):
        for item in (self.pm, self.controller.pm):
            self.assertEqual(set(vars(item)) & {'owner', 'leases', 'decisions'}, set())
        names = [tool['name'] for tool in self.pm.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
                 ['result']['tools']]
        self.assertTrue({'bridge_pm_chats', 'bridge_pm_connect', 'bridge_pm_disconnect'} <= set(names))
        self.assertFalse([name for name in names if 'lease' in name or 'writer' in name or 'owner' in name])

    def test_an_owner_stop_ends_the_route_and_blocks_a_pm_reconnect(self):
        self.connect(self.a, self.b)
        self.assertEqual({row[:2] + row[3:] for row in self.controller.owner.pm_routes()},
                         {(self.a.reference, self.b.reference, True), (self.b.reference, self.a.reference, True)})
        self.assertEqual(self.stop(self.a, self.b), (True, None))
        self.assertIs(self.controller.pairs.owner_choice(self.a, self.b), False)
        self.assertIsNone(self.controller.pairs.owner_choice(self.b, self.a))  # one direction stopped
        self.assertEqual(self.code(self.send(self.servers['a'], self.b)), 'chat-route-not-selected')
        self.assertEqual(self.code(self.connect(self.a, self.b, bidirectional=False)), 'owner-disconnected')
        # A chat that is not listed now: its route can still be stopped.
        self.controller.pairs.withdraw('b')
        self.assertEqual(self.stop(self.b, self.a), (True, None))
        self.assertEqual(self.controller.pm.current(), ())
        self.assertEqual(self.stop(self.b, self.a), (False, None))
        self.assertIs(self.controller.pairs.owner_choice(self.b, self.a), False)
        self.assertEqual([(entry['event'], entry['by'], entry.get('code')) for entry in self.log()],
                         [('connect', 'pm', None)] * 2 + [('stop', 'owner', None), ('refused', 'pm', 'owner-disconnected'),
                          ('stop', 'owner', None)])

    def test_an_owner_stop_whose_block_cannot_be_written_still_stops_and_says_a_reconnect_is_open(self):
        self.connect(self.a, self.b, bidirectional=False)
        # The routes table is full: the block row is refused and rolled back.
        with patch('optional.bridge.core.pairing.MAX_ROUTES', 0):
            stopped = self.stop(self.a, self.b)
        self.assertEqual(stopped, (True, 'route-capacity'))
        self.assertEqual(self.controller.pm.current(), ())
        self.assertIsNone(self.controller.pairs.owner_choice(self.a, self.b))
        self.assertEqual(self.code(self.send(self.servers['a'], self.b)), 'chat-route-not-selected')
        self.assertEqual([(entry['event'], entry['by'], entry.get('unblocked')) for entry in self.log()],
                         [('connect', 'pm', None), ('stop', 'owner', 'route-capacity')])
        # Not blocked: the PM may make it again, as the owner was told.
        self.assertEqual(self.connect(self.a, self.b, bidirectional=False)['routes'][0]['by'], 'pm')

    def test_an_owner_disconnect_ends_a_pm_route_and_only_connect_undoes_it(self):
        self.connect(self.a, self.b)
        self.controller.owner.change(self.a.reference, self.b.reference, enabled=False)
        self.assertEqual(self.controller.pm.current(), ())
        self.assertEqual(self.code(self.connect(self.a, self.b)), 'owner-disconnected')
        self.controller.owner.change(self.a.reference, self.b.reference, enabled=True)
        self.assertEqual(self.send(self.servers['a'], self.b)['state'], 'queued')

    # Working

    def test_the_pm_connects_two_kit_launched_chats_and_disconnects_its_own_route(self):
        self.controller.pm.clock = lambda: 100.0
        chats = self.call(self.pm, 'bridge_pm_chats')
        self.assertEqual({(item['reference'], item['mark']) for item in chats['chats']},
                         {(self.p.reference, 'pm'), (self.a.reference, 'worker'), (self.b.reference, 'worker')})
        self.assertEqual(self.connect(self.a, self.b)['routes'],
                         [{'sender': self.a.reference, 'recipient': self.b.reference, 'by': 'pm'},
                          {'sender': self.b.reference, 'recipient': self.a.reference, 'by': 'pm'}])
        self.assertEqual({(item['sender'], item['by']) for item in self.call(self.pm, 'bridge_pm_chats')['routes']},
                         {(self.a.reference, 'pm'), (self.b.reference, 'pm')})
        self.assertEqual(self.call(self.servers['a'], 'bridge_connections')['chats'][0]['reference'], self.b.reference)
        self.assertEqual(self.send(self.servers['a'], self.b)['state'], 'queued')
        self.assertEqual([item.state for item in self.controller.dispatch_pending()], ['reply-received'])
        self.assertEqual(len(self.adapter.calls), 1)
        removed = self.call(self.pm, 'bridge_pm_disconnect', sender=self.a.reference, recipient=self.b.reference)
        self.assertEqual(len(removed['removed']), 2)
        self.assertEqual(self.controller.pm.current(), ())
        self.assertEqual(self.code(self.send(self.servers['a'], self.b)), 'chat-route-not-selected')
        p, a, b = self.p.reference, self.a.reference, self.b.reference
        at = '1970-01-01T00:01:40Z'
        self.assertEqual(self.log(), [
            {'at': at, 'event': 'connect', 'by': 'pm', 'caller': p, 'sender': a, 'recipient': b,
             'sender_mark': 'worker', 'recipient_mark': 'worker'},
            {'at': at, 'event': 'connect', 'by': 'pm', 'caller': p, 'sender': b, 'recipient': a,
             'sender_mark': 'worker', 'recipient_mark': 'worker'},
            {'at': at, 'event': 'disconnect', 'by': 'pm', 'caller': p, 'sender': a, 'recipient': b},
            {'at': at, 'event': 'disconnect', 'by': 'pm', 'caller': p, 'sender': b, 'recipient': a}])

    def test_the_pm_mark_is_the_launchers_pm_mark(self):
        from kitlib.launcher.window import PM
        from optional.bridge.core.pmroutes import PM_MARK
        self.assertEqual(PM_MARK, PM)


if __name__ == '__main__':
    unittest.main()
