"""Owner controls use exact visible references and private one-use decisions."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from optional.bridge.core.owner import MAX_LEASES,OwnerControls,OwnerDecisions,PmStop,WriterGrant,WriterLeases
from optional.bridge.core.pairing import PairingError,PairingStore
from tests.bridge.test_pairing import chat

LEASE_LOG='writer-leases.log'  # the documented name (README, Writer leases)


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


    def test_pm_routes_must_share_the_owner_store_and_controls_without_them_list_none(self):
        from optional.bridge.core.pmroutes import PmRoutes
        with PairingStore(Path(self.temp.name)/'other.sqlite') as other:
            with self.assertRaisesRegex(PairingError,'invalid-owner-components'):
                OwnerControls(self.pairs,self.decisions,pm=PmRoutes(other,launched=lambda binding:'pm'))
        self.assertEqual(self.owner.pm_routes(),())
        with self.assertRaisesRegex(PairingError,'pm-routes-unavailable'):
            self.owner.stop_pm(self.a.reference,self.b.reference)

    def test_a_pm_route_the_owner_connected_since_is_not_listed_and_stop_never_disconnects_the_owner(self):
        from optional.bridge.core.messages import SenderAuthority
        from optional.bridge.core.pmroutes import PmRoutes
        p=chat('p')
        self.live['p']=p
        self.pairs.observe(p)
        pm=PmRoutes(self.pairs,launched=lambda binding:'pm' if binding in self.live.values() else None,
            clock=lambda:self.now)
        owner=OwnerControls(self.pairs,self.decisions,pm=pm)
        pm.connect(SenderAuthority(p,'root-p',launched='pm'),self.a.reference,self.b.reference,bidirectional=False)
        self.assertEqual(owner.pm_routes(),((self.a.reference,self.b.reference,100.0,True),))
        owner.change(self.a.reference,self.b.reference,enabled=True,bidirectional=False)
        self.assertEqual(owner.pm_routes(),())  # the owner's own row decides it now
        self.assertEqual(owner.stop_pm(self.a.reference,self.b.reference),PmStop(True,None))
        self.assertIs(self.pairs.owner_choice(self.a,self.b),True)
        self.pairs.require_selected(self.a,self.b)


class WriterLeaseTests(unittest.TestCase):
    """A writer lease is an owner click, one use, exact chats, until revoked, logged, and in memory."""
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

    def log(self):
        path=Path(self.temp.name)/LEASE_LOG
        if not path.exists(): return []
        return [json.loads(line) for line in path.read_text(encoding='ascii').splitlines()]

    def test_lease_has_no_expiry_and_keeps_its_grant_time(self):
        granted=self.owner.lease(self.a.reference,self.b.reference,enabled=True)
        self.assertEqual(granted,100.0)
        self.now+=10*365*24*3600
        self.assertEqual(self.leases.writer(self.b),self.a)
        self.assertEqual(self.owner.writer_grants(),((self.a.reference,self.b.reference,100.0,True),))

    def test_pm_can_revoke_a_lease_and_only_owner_or_pm_may(self):
        self.owner.lease(self.a.reference,self.b.reference,enabled=True)
        with self.assertRaises(PairingError): self.owner.revoke(self.a.reference,self.b.reference,by='agent')
        self.assertFalse(self.owner.revoke(self.c.reference,self.b.reference,by='pm'))  # not the writer
        self.assertEqual(self.leases.writer(self.b),self.a)
        self.assertTrue(self.owner.revoke(self.a.reference,self.b.reference,by='pm'))
        self.assertIsNone(self.leases.writer(self.b))
        self.assertFalse(self.owner.revoke(self.a.reference,self.b.reference,by='pm'))

    def test_a_lease_survives_a_failed_live_check_and_the_same_binding_returning(self):
        # A Codex chat fails observation while it starts a new turn; that is not a close.
        for gone in ('a','b'):
            self.owner.lease(self.a.reference,self.b.reference,enabled=True)
            del self.live[gone]
            with self.assertRaises(PairingError): self.pairs.propose('a','b')  # the live check fails
            self.assertEqual(self.leases.writer(self.b),self.a)
            self.live[gone]=getattr(self,gone)
            self.pairs.observe(getattr(self,gone))
            self.assertEqual(self.owner.writers(),((self.a.reference,self.b.reference),))

    def test_a_lease_survives_a_withdraw_and_the_chat_returning(self):
        # One discovery pass that misses a chat withdraws it; that is not a close either.
        self.owner.lease(self.a.reference,self.b.reference,enabled=True)
        self.pairs.withdraw('a')
        self.pairs.withdraw('b')
        self.assertEqual(self.leases.writer(self.b),self.a)
        self.pairs.observe(self.a)
        self.pairs.observe(self.b)
        self.assertTrue(self.leases.holds(self.a,self.b))

    def test_a_different_binding_for_a_session_drops_only_its_leases(self):
        d=chat('d')
        self.live['d']=d
        self.pairs.observe(d)
        self.owner.lease(self.a.reference,self.b.reference,enabled=True)
        self.owner.lease(self.c.reference,d.reference,enabled=True)
        self.pairs.withdraw('c')  # c away, not replaced: c -> d stays
        self.live['b']=replace(self.b,permission_revision=2)
        self.pairs.observe(self.live['b'])  # recipient b replaced
        self.assertEqual(self.leases.current(),((self.c.reference,d.reference),))
        self.owner.lease(self.a.reference,self.live['b'].reference,enabled=True)
        self.pairs.withdraw('a')
        self.live['a']=replace(self.a,permission_revision=2)
        self.pairs.observe(self.live['a'])  # writer a replaced, after an absence
        self.assertEqual(self.leases.current(),((self.c.reference,d.reference),))

    def test_a_lease_whose_chat_is_unlisted_is_shown_and_stopping_it_frees_its_slot(self):
        recipients=[chat('r%d'%index) for index in range(MAX_LEASES)]
        for recipient in recipients:
            self.live[recipient.identity.session_id]=recipient
            self.pairs.observe(recipient)
            self.owner.lease(self.a.reference,recipient.reference,enabled=True)
            self.pairs.withdraw(recipient.identity.session_id)  # closed or missed: not listed now
        grants=self.owner.writer_grants()
        self.assertEqual(len(grants),MAX_LEASES)
        self.assertEqual({grant[3] for grant in grants},{False})
        self.assertEqual(self.owner.writers(),())  # only fully listed leases
        with self.assertRaises(PairingError): self.owner.lease(self.a.reference,self.b.reference,enabled=True)
        self.assertTrue(self.owner.lease(self.a.reference,recipients[0].reference,enabled=False))
        self.owner.lease(self.a.reference,self.b.reference,enabled=True)  # the slot is free again
        self.assertEqual(self.leases.writer(self.b),self.a)

    def test_replaced_bindings_never_fill_lease_capacity(self):
        for index in range(MAX_LEASES+1):
            self.owner.lease(self.a.reference,self.live['b'].reference,enabled=True)
            self.live['b']=replace(self.live['b'],permission_revision=index+2)
            self.pairs.observe(self.live['b'])
        self.assertEqual(self.leases.current(),())

    def test_every_grant_revoke_and_replacement_is_logged_beside_the_pairings_store(self):
        self.owner.lease(self.a.reference,self.b.reference,enabled=True)
        self.owner.lease(self.a.reference,self.b.reference,enabled=False)
        self.owner.lease(self.c.reference,self.b.reference,enabled=True)
        self.owner.revoke(self.a.reference,self.b.reference,by='pm')  # no such lease: no line
        self.pairs.withdraw('c')  # not a close: no line
        self.live['c']=replace(self.c,permission_revision=2)
        self.pairs.observe(self.live['c'])
        self.assertEqual([(entry['event'],entry['by'],entry['writer'],entry['recipient']) for entry in self.log()],
            [('grant','owner',self.a.reference,self.b.reference),('revoke','owner',self.a.reference,self.b.reference),
             ('grant','owner',self.c.reference,self.b.reference),('replaced','bridge',self.c.reference,self.b.reference)])
        self.assertEqual(self.log()[0]['at'],'1970-01-01T00:01:40Z')

    def test_a_grant_that_cannot_be_logged_is_refused(self):
        (Path(self.temp.name)/LEASE_LOG).mkdir()
        with self.assertRaises(PairingError): self.owner.lease(self.a.reference,self.b.reference,enabled=True)
        self.assertEqual(self.leases.current(),())

    def test_an_unlogged_replacing_grant_leaves_the_old_writer_its_lease(self):
        self.owner.lease(self.a.reference,self.b.reference,enabled=True)
        with patch.object(self.owner,'_log',side_effect=OSError('disk full')):
            with self.assertRaises(PairingError): self.owner.lease(self.c.reference,self.b.reference,enabled=True)
        self.assertEqual(self.leases.writer(self.b),self.a)
        self.assertEqual(self.owner.writer_grants(),((self.a.reference,self.b.reference,100.0,True),))

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
