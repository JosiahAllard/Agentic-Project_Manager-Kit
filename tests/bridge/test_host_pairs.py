"""The host-pair rule (optional/bridge/core/hostpairs.py) and its pairwise walk.

The walk goes over every sender x recipient pair of the served peers, read from a peers.json, so a
newly listed peer is covered without anyone writing its pairs down. Each pair runs through a real
PairingStore, Outbox and CoreBroker with a test adapter per peer: an allowed pair is admitted and
sent once; a refused pair is refused at admission with nothing queued, and a message queued by a
broker without the rule is refused before any adapter call.
"""
import itertools
import json
import os
from pathlib import Path
import tempfile
import unittest

from optional.bridge import registry
from optional.bridge.core.dispatch import CoreBroker
from optional.bridge.core.hostpairs import HostPairs
from optional.bridge.core.messages import DeliveryError, SenderAuthority
from optional.bridge.core.outbox import Outbox
from optional.bridge.core.pairing import PairingStore
from tests.bridge.registered import write_peers
from tests.bridge.test_dispatch import TestAdapter
from tests.bridge.test_pairing import chat

OWNER = b'test-owner-proof'
CALLER = b'test-caller-proof'


class Rule(unittest.TestCase):
    def test_a_binding_belongs_to_the_peer_its_adapter_id_names(self):
        rule = HostPairs(['claude', 'codex'], [['codex', 'codex']])
        for adapter, host in (('claude-peer', 'claude'), ('codex-desktop', 'codex'),
                              ('codex-terminal', 'codex'), ('codex', 'codex'),
                              ('codexy-desktop', None), ('test-adapter', None), ('peer-claude', None)):
            with self.subTest(adapter=adapter):
                self.assertEqual(rule.host_of(chat('x', adapter=adapter)), host)

    def test_the_longest_listed_name_wins(self):
        rule = HostPairs(['app', 'app-pro'])
        self.assertEqual(rule.host_of(chat('x', adapter='app-pro-desktop')), 'app-pro')
        self.assertEqual(rule.host_of(chat('x', adapter='app-desktop')), 'app')

    def test_only_a_listed_refused_pair_is_refused(self):
        rule = HostPairs(['claude', 'codex'], [['codex', 'codex']])
        codex, other = chat('a', adapter='codex-desktop'), chat('b', adapter='codex-terminal')
        with self.assertRaisesRegex(DeliveryError, 'host-pair-refused'):
            rule.check(codex, other)
        rule.check(codex, chat('c', adapter='claude-peer'))
        rule.check(chat('c', adapter='claude-peer'), codex)
        rule.check(chat('c', adapter='claude-peer'), chat('d', adapter='claude-peer'))
        rule.check(codex, chat('e'))                  # an id no peer names is outside the rule

    def test_a_malformed_rule_is_refused(self):
        for peers, refused in (([], ()), (['a', 'a'], ()), (['A'], ()), (['a b'], ()), ([1], ()),
                               (['a'], [['a', 'b']]), (['a'], [['a']]), (['a'], [['a', 'a'], ['a', 'a']]),
                               (['p%d' % n for n in range(33)], ()), ([['a']], ()), (['a'], [[['a'], 'a']]),
                               (['a'], [1]), (1, ())):
            with self.subTest(peers=peers, refused=refused), self.assertRaisesRegex(DeliveryError, 'invalid-host-pairs'):
                HostPairs(peers, refused)
        with self.assertRaisesRegex(DeliveryError, 'invalid-host-pair-binding'):
            HostPairs(['a']).host_of('a-desktop')

    def test_a_broker_takes_only_a_host_pairs_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            pairings = PairingStore(Path(folder) / 'pair.sqlite', live_check=lambda peer: True,
                                    owner_check=lambda selection, proof: False)
            store = Outbox(Path(folder) / 'outbox.sqlite', leadership_check=lambda: True)
            try:
                with self.assertRaisesRegex(DeliveryError, 'invalid-broker-ports'):
                    CoreBroker(pairings, store, {}, host_pairs=[('a', 'a')])
            finally:
                store.close()
                pairings.close()


class OwnerPins(unittest.TestCase):
    """What the owner settled, read from the kit's real peers.json."""

    def test_claude_and_codex_are_served_and_codex_to_codex_is_refused(self):
        self.assertEqual(registry.names(), ('claude', 'codex'))
        rule = registry.host_pairs()
        claude, codex = chat('a', adapter='claude-peer'), chat('b', adapter='codex-desktop')
        self.assertFalse(rule.allowed(codex, chat('c', adapter='codex-terminal')))
        self.assertTrue(rule.allowed(claude, codex))
        self.assertTrue(rule.allowed(codex, claude))
        self.assertTrue(rule.allowed(claude, chat('d', adapter='claude-peer')))


class Walk:
    """Every sender x recipient pair of `peers` through a real store, outbox and broker."""

    def __init__(self, test, folder, peers, rule):
        self.test, self.peers, self.rule = test, tuple(peers), rule
        self.pairings = PairingStore(Path(folder) / 'pair.sqlite', live_check=lambda peer: True,
                                     owner_check=lambda selection, proof: proof == OWNER)
        self.store = Outbox(Path(folder) / 'outbox.sqlite', leadership_check=lambda: True)
        self.store.claim()
        self.adapters = {peer: TestAdapter(peer + '-test') for peer in self.peers}
        self.proofs = {}
        ports = {adapter.adapter_id: adapter for adapter in self.adapters.values()}
        self.core = CoreBroker(self.pairings, self.store, ports, authorize=self.authorize,
                               dispatch_check=lambda message: True, host_pairs=rule)
        self.plain = CoreBroker(self.pairings, self.store, ports, authorize=self.authorize,
                                dispatch_check=lambda message: True)
        self.visited = []

    def close(self):
        self.store.close()
        self.pairings.close()

    def authorize(self, intent, proof):
        return self.proofs.get(proof)

    def sent(self, recipient, operation):
        return [m for m in self.adapters[recipient].calls if m.operation_id == operation]

    def pair(self, sender, recipient):
        """Run one pair; True when it was delivered, False when it was refused."""
        name = '%s-to-%s' % (sender, recipient)
        a = chat(name + '-sender', adapter=sender + '-test')
        b = chat(name + '-recipient', adapter=recipient + '-test')
        for peer in (a, b):
            self.pairings.observe(peer)
        self.pairings.owner_select(self.pairings.propose(a.identity.session_id, b.identity.session_id,
                                                         enabled=True), OWNER)
        self.proofs[CALLER] = SenderAuthority(a, 'root-' + name)
        self.visited.append((sender, recipient))
        operation = 'op-' + name
        try:
            receipt = self.core.admit(operation, b, 'walk message', CALLER)
        except DeliveryError as error:
            self.test.assertEqual(error.code, 'host-pair-refused')
            self.test.assertIsNone(self.store.receipt(operation), 'a refused pair queues nothing')
            # Queued by a broker without the rule, it is still refused before any adapter call.
            queued = 'held-' + name
            self.test.assertEqual(self.plain.admit(queued, b, 'walk message', CALLER).state, 'queued')
            receipt = self.core.dispatch_once(queued)
            self.test.assertEqual((receipt.state, receipt.reason), ('refused', 'host-pair-refused'))
            self.test.assertEqual(self.sent(recipient, queued), [])
            return False
        self.test.assertEqual(receipt.state, 'queued')
        self.test.assertEqual(self.core.dispatch_once(operation).state, 'reply-received')
        self.test.assertEqual(len(self.sent(recipient, operation)), 1)
        return True

    def run(self):
        """{(sender, recipient): delivered} over every ordered pair, self pairs included."""
        return {(s, r): self.pair(s, r) for s, r in itertools.product(self.peers, repeat=2)}


def expected(peers_json, peers):
    """Each pair's outcome straight from the file: delivered unless listed as refused."""
    with open(peers_json, encoding='utf-8') as f:
        refused = {tuple(pair) for pair in json.load(f)['refused']}
    return {(s, r): (s, r) not in refused for s, r in itertools.product(peers, repeat=2)}


class PairwiseWalk(unittest.TestCase):
    def walk(self, peers_json, peers):
        with tempfile.TemporaryDirectory(prefix='bridge-pairs-') as folder:
            walk = Walk(self, folder, peers, registry.host_pairs(peers_json))
            try:
                return walk.run(), walk.visited
            finally:
                walk.close()

    def test_every_pair_of_the_served_peers(self):
        peers = registry.names()
        found, visited = self.walk(registry.PEERS, peers)
        self.assertEqual(found, expected(registry.PEERS, peers))
        self.assertEqual(len(visited), len(peers) ** 2)
        self.assertEqual(found[('codex', 'codex')], False)       # the owner's settled refusal

    def test_a_newly_listed_peer_is_walked_without_a_pair_list(self):
        with tempfile.TemporaryDirectory(prefix='bridge-newpeer-') as folder:
            kit = Path(folder)
            listed, refused = registry.read_peers()
            peers_json = write_peers(str(kit / 'peers.json'), listed + ('newpeer',),
                                     refused + (('newpeer', 'codex'),))
            for name in listed + ('newpeer',):
                half = kit / 'optional' / 'bridge' / 'hosts' / registry.module_name(name)
                half.mkdir(parents=True)
                (half / '__init__.py').write_text('HOST = None\n', encoding='utf-8')
            peers = registry.names(peers_json, kit)
            self.assertEqual(peers, listed + ('newpeer',))
            found, visited = self.walk(peers_json, peers)
            want = expected(peers_json, peers)
        self.assertEqual(len(visited), 9)
        self.assertEqual(found, want)
        self.assertEqual({pair for pair in visited if 'newpeer' in pair},
                         {(s, r) for s, r in itertools.product(peers, repeat=2) if 'newpeer' in (s, r)})
        self.assertEqual((found[('newpeer', 'codex')], found[('codex', 'newpeer')],
                          found[('newpeer', 'newpeer')]), (False, True, True))


if __name__ == '__main__':
    unittest.main()
