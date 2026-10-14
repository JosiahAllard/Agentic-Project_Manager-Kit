"""Remembered logical choice never retargets old durable native operations."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from optional.bridge.core.dispatch import CoreBroker
from optional.bridge.core.messages import SenderAuthority
from optional.bridge.core.outbox import Outbox
from optional.bridge.core.pairing import PairingStore
from tests.bridge.test_dispatch import TestAdapter
from tests.bridge.test_pairing import chat


class RememberedDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='remembered-route-')
        self.a, self.b = chat('a'), chat('b', adapter='test-recipient')
        self.live = {'a': self.a, 'b': self.b}
        self.pairings = PairingStore(Path(self.temp.name)/'pair.sqlite',
            live_check=lambda peer: self.live.get(peer.identity.session_id) == peer,
            owner_check=lambda selection, proof: proof == b'fixture-owner-proof')
        self.pairings.observe(self.a)
        self.pairings.observe(self.b)
        self.pairings.owner_select(self.pairings.propose('a', 'b'), b'fixture-owner-proof')
        self.outbox = Outbox(Path(self.temp.name)/'outbox.sqlite', leadership_check=lambda: True)
        self.outbox.claim()
        self.adapter = TestAdapter('test-recipient')
        self.authority = SenderAuthority(self.a, 'same-task-root')
        self.core = CoreBroker(self.pairings, self.outbox, {'test-recipient': self.adapter},
            authorize=lambda intent, proof: self.authority if proof == b'fixture-caller-proof' else None,
            dispatch_check=lambda message: True)

    def tearDown(self):
        self.outbox.close()
        self.pairings.close()
        self.temp.cleanup()

    def admit(self, operation):
        return self.core.admit(operation, self.b, 'fixture body', b'fixture-caller-proof')

    def reopen(self):
        self.pairings.withdraw('a')
        self.pairings.withdraw('b')
        self.a = replace(self.a, permission_revision=2,
            identity=replace(self.a.identity, incarnation_id='reopened-a'))
        self.b = replace(self.b, permission_revision=2,
            identity=replace(self.b.identity, incarnation_id='reopened-b'))
        self.live = {'a': self.a, 'b': self.b}
        self.pairings.observe(self.a)
        self.pairings.observe(self.b)
        self.authority = SenderAuthority(self.a, 'same-task-root')

    def test_old_queued_message_is_not_retargeted_but_new_message_uses_remembered_route(self):
        self.admit('old-operation')
        original = self.outbox.message('old-operation')
        self.reopen()
        self.assertEqual(self.core.dispatch_once('old-operation').state, 'refused')
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.outbox.message('old-operation'), original)
        self.admit('fresh-operation')
        self.assertEqual(self.core.dispatch_once('fresh-operation').state, 'reply-received')
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.adapter.calls[0].recipient, self.b)
        self.assertEqual(self.adapter.calls[0].sender, self.a)

    def test_uncertain_operation_is_not_replayed_on_reopen_with_remembered_route(self):
        self.admit('uncertain')
        self.adapter.fail = TimeoutError('fixture lost response')
        self.assertEqual(self.core.dispatch_once('uncertain').state, 'unknown')
        self.reopen()
        self.adapter.fail = None
        self.pairings.require_selected(self.a, self.b)
        self.assertEqual(self.core.dispatch_once('uncertain').state, 'unknown')
        self.assertEqual(len(self.adapter.calls), 1)
