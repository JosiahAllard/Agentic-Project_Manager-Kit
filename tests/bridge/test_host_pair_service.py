"""The service's controller applies the host-pair rule to every pair of the peers it serves.

A real Controller (optional/bridge/win/controller.py), with one test adapter per served peer whose
id the peer's name starts, walks every ordered sender x recipient pair of registry.names(). The
owner settled one refusal, Codex to Codex; every other pair is admitted and delivered. Nothing here
lists the pairs, so a newly served peer is walked too.
"""
import hashlib
import itertools
from pathlib import Path
import tempfile
import unittest
import uuid

from optional.bridge import registry
from optional.bridge.core.messages import DeliveryError, MessageIntent, SenderAuthority
from optional.bridge.win.controller import Controller
from optional.bridge.win.leader import ServiceLeader
from tests.bridge.test_dispatch import TestAdapter
from tests.bridge.test_pairing import chat

OWNER_REFUSED = {('codex', 'codex')}


class ServedPairs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-host-pairs-')
        self.leader = ServiceLeader(uuid.uuid4().hex)
        self.controller = Controller(Path(self.temp.name), self.leader)
        self.peers = registry.names()
        self.adapters = {peer: TestAdapter(peer + '-test') for peer in self.peers}
        for adapter in self.adapters.values():
            self.controller.adapter(adapter)
        self.live, self.caller = {}, None
        self.server = self.controller.frontend(lambda metadata: self.caller)

    def tearDown(self):
        self.controller.close()
        self.leader.close()
        self.temp.cleanup()

    def enroll(self, name, peer):
        binding = chat(name, adapter=peer + '-test')
        self.live[name] = binding
        self.controller.enroll(binding, lambda item: self.live.get(item.identity.session_id) == item)
        return binding

    def deliver(self, sender, recipient):
        name = '%s-to-%s' % (sender, recipient)
        a, b = self.enroll(name + '-sender', sender), self.enroll(name + '-recipient', recipient)
        self.controller.owner.change(a.reference, b.reference, enabled=True)
        self.caller = SenderAuthority(a, 'root-' + name)
        operation = 'op-' + name
        proof = self.server.calls.issue(MessageIntent(operation, b, hashlib.sha256(b'text').hexdigest()), {})
        try:
            self.controller.core.admit(operation, b, 'text', proof)
        except DeliveryError as error:
            return error.code
        self.controller.dispatch_pending()
        sent = [m for m in self.adapters[recipient].calls if m.operation_id == operation]
        return self.controller.outbox.receipt(operation).state, len(sent)

    def test_every_served_pair_is_delivered_except_codex_to_codex(self):
        self.assertEqual(set(self.peers), {'claude', 'codex'})
        found = {(s, r): self.deliver(s, r) for s, r in itertools.product(self.peers, repeat=2)}
        self.assertEqual(found, {pair: 'host-pair-refused' if pair in OWNER_REFUSED else ('reply-received', 1)
                                 for pair in itertools.product(self.peers, repeat=2)})


if __name__ == '__main__':
    unittest.main()
