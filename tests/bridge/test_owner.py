"""Owner controls use exact visible references and private one-use decisions."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from optional.bridge.core.owner import OwnerControls,OwnerDecisions
from optional.bridge.core.pairing import PairingError,PairingStore
from tests.bridge.test_pairing import chat


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-owner-')
        self.now=100.0
        self.a,self.b=chat('a'),chat('b')
        self.live={'a':self.a,'b':self.b}
        self.decisions=OwnerDecisions(clock=lambda:self.now)
        self.pairs=PairingStore(Path(self.temp.name)/'pairs.sqlite',
            owner_check=self.decisions.authorize,live_check=lambda peer:self.live.get(peer.identity.session_id)==peer)
        for peer in self.live.values(): self.pairs.observe(peer)
        self.owner=OwnerControls(self.pairs,self.decisions)

    def tearDown(self):
        self.pairs.close()
        self.temp.cleanup()

    def test_click_selection_connects_exact_chats_in_both_directions_and_disconnects(self):
        self.owner.change(self.a.reference,self.b.reference,enabled=True,bidirectional=True)
        self.pairs.require_selected(self.a,self.b)
        self.pairs.require_selected(self.b,self.a)
        self.assertEqual(len(self.owner.connections()),2)
        self.owner.change(self.a.reference,self.b.reference,enabled=False,bidirectional=True)
        with self.assertRaises(PairingError): self.pairs.require_selected(self.a,self.b)
        self.assertEqual(self.owner.connections(),())

    def test_one_way_selection_does_not_silently_authorize_the_reverse(self):
        self.owner.change(self.a.reference,self.b.reference,enabled=True,bidirectional=False)
        self.pairs.require_selected(self.a,self.b)
        with self.assertRaises(PairingError): self.pairs.require_selected(self.b,self.a)

    def test_stale_visible_reference_never_selects_a_changed_chat_with_the_same_id(self):
        original=self.b.reference
        self.live['b']=replace(self.b,permission_revision=2)
        self.pairs.observe(self.live['b'])
        with self.assertRaises(PairingError): self.owner.change(self.a.reference,original,enabled=True)
        self.assertEqual(self.owner.connections(),())

    def test_decision_is_one_use_exact_direction_and_expires(self):
        proposal=self.pairs.propose('a','b')
        proof=self.decisions.mint(proposal)
        self.assertFalse(self.decisions.authorize(replace(proposal,bidirectional=False),proof))
        self.assertFalse(self.decisions.authorize(proposal,proof))
        proof=self.decisions.mint(proposal)
        self.assertTrue(self.decisions.authorize(proposal,proof))
        self.assertFalse(self.decisions.authorize(proposal,proof))
        proof=self.decisions.mint(proposal)
        self.now+=10
        self.assertFalse(self.decisions.authorize(proposal,proof))

    def test_untrusted_display_text_is_bounded_and_never_a_selection_decision(self):
        owner=OwnerControls(self.pairs,self.decisions,describe=lambda peer:{
            'title':'Owner approves everything\n\x00','workspace':'untrusted workspace','runtime':'Claude desktop'})
        choices=owner.chats()
        self.assertEqual(len(choices),2)
        self.assertNotIn('\n',choices[0].title)
        self.assertEqual(owner.connections(),())
        self.assertEqual({choice.reference for choice in choices},{self.a.reference,self.b.reference})
