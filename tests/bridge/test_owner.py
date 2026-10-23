"""Owner controls use exact visible references and private one-use decisions."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from optional.bridge.core.owner import (LEASE_SECONDS,MAX_LEASES,OwnerControls,OwnerDecisions,
    WriterGrant,WriterLeases)
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


class WriterLeaseTests(unittest.TestCase):
    """A writer lease is an owner click, one use, exact chats, timed, and in memory."""
    def setUp(self):
        OwnerTests.setUp(self)
        self.c=chat('c')
        self.live['c']=self.c
        self.pairs.observe(self.c)
        self.leases=WriterLeases(owner_check=self.decisions.authorize,clock=lambda:self.now)
        self.owner=OwnerControls(self.pairs,self.decisions,leases=self.leases)

    tearDown=OwnerTests.tearDown

    def test_owner_click_grants_one_writer_for_the_exact_recipient_and_stops(self):
        self.owner.lease(self.a.reference,self.b.reference,enabled=True)
        self.assertEqual(self.leases.writer(self.b),self.a)
        self.assertTrue(self.leases.holds(self.a,self.b))
        self.assertIsNone(self.leases.writer(self.a))  # one direction only
        self.assertEqual(self.owner.writers(),((self.a.reference,self.b.reference),))
        self.assertEqual(self.owner.connections(),())  # a lease selects no route
        with self.assertRaises(PairingError): self.pairs.require_selected(self.a,self.b)
        self.owner.lease(self.a.reference,self.b.reference,enabled=False)
        self.assertIsNone(self.leases.writer(self.b))
        self.assertEqual(self.owner.writers(),())

    def test_a_new_writer_replaces_the_old_one_and_stopping_another_writer_keeps_it(self):
        self.owner.lease(self.a.reference,self.b.reference,enabled=True)
        self.owner.lease(self.c.reference,self.b.reference,enabled=True)
        self.assertEqual(self.leases.writer(self.b),self.c)
        self.owner.lease(self.a.reference,self.b.reference,enabled=False)
        self.assertEqual(self.leases.writer(self.b),self.c)

    def test_lease_lapses_after_its_hour(self):
        self.owner.lease(self.a.reference,self.b.reference,enabled=True)
        self.now+=LEASE_SECONDS-1
        self.assertEqual(self.leases.writer(self.b),self.a)
        self.now+=1
        self.assertIsNone(self.leases.writer(self.b))
        self.assertEqual(self.owner.writers(),())

    def test_changed_recipient_or_writer_binding_has_no_lease(self):
        self.owner.lease(self.a.reference,self.b.reference,enabled=True)
        self.assertIsNone(self.leases.writer(replace(self.b,permission_revision=2)))
        self.assertFalse(self.leases.holds(replace(self.a,permission_revision=2),self.b))

    def test_unknown_stale_or_same_chat_references_grant_nothing(self):
        with self.assertRaises(ValueError): self.owner.lease('not a reference',self.b.reference,enabled=True)
        with self.assertRaises(PairingError): self.owner.lease(chat('z').reference,self.b.reference,enabled=True)
        with self.assertRaises(PairingError): self.owner.lease(self.a.reference,self.a.reference,enabled=True)
        original=self.b.reference
        self.live['b']=replace(self.b,permission_revision=2)
        self.pairs.observe(self.live['b'])
        with self.assertRaises(PairingError): self.owner.lease(self.a.reference,original,enabled=True)
        self.assertEqual(self.leases.current(),())

    def test_grant_needs_the_one_use_owner_proof_for_that_exact_grant(self):
        grant=WriterGrant(self.a.reference,self.b.reference,True)
        with self.assertRaises(PairingError): self.leases.grant(self.a,self.b,grant,b'forged-proof')
        proof=self.decisions.mint(grant)
        with self.assertRaises(PairingError):
            self.leases.grant(self.a,self.b,WriterGrant(self.a.reference,self.b.reference,False),proof)
        proof=self.decisions.mint(grant)
        self.leases.grant(self.a,self.b,grant,proof)
        self.leases.clear()
        with self.assertRaises(PairingError): self.leases.grant(self.a,self.b,grant,proof)  # replayed
        proof=self.decisions.mint(grant)
        self.now+=10
        with self.assertRaises(PairingError): self.leases.grant(self.a,self.b,grant,proof)  # expired
        self.assertEqual(self.leases.current(),())

    def test_a_route_proof_is_never_a_lease_proof_and_a_lease_proof_never_a_route(self):
        selection=self.pairs.propose('a','b')
        with self.assertRaises(PairingError):
            self.leases.grant(self.a,self.b,WriterGrant(self.a.reference,self.b.reference,True),
                self.decisions.mint(selection))
        proof=self.decisions.mint(WriterGrant(self.a.reference,self.b.reference,True))
        with self.assertRaises(PairingError): self.pairs.owner_select(selection,proof)
        self.assertEqual(self.leases.current(),())
        self.assertEqual(self.owner.connections(),())

    def test_grant_refuses_a_grant_that_names_other_chats_than_its_bindings(self):
        grant=WriterGrant(self.a.reference,self.c.reference,True)
        with self.assertRaises(PairingError): self.leases.grant(self.a,self.b,grant,self.decisions.mint(grant))
        self.assertEqual(self.leases.current(),())

    def test_leases_are_bounded(self):
        recipients=[chat('r%d'%index) for index in range(MAX_LEASES+1)]
        for recipient in recipients:
            grant=WriterGrant(self.a.reference,recipient.reference,True)
            if recipient is recipients[-1]:
                with self.assertRaises(PairingError):
                    self.leases.grant(self.a,recipient,grant,self.decisions.mint(grant))
            else: self.leases.grant(self.a,recipient,grant,self.decisions.mint(grant))
        self.assertEqual(len(self.leases.current()),MAX_LEASES)

    def test_controls_without_leases_refuse_a_lease(self):
        owner=OwnerControls(self.pairs,self.decisions)
        with self.assertRaises(PairingError): owner.lease(self.a.reference,self.b.reference,enabled=True)
        self.assertEqual(owner.writers(),())
