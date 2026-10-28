"""Owner-selected exact chat routes, with explicit test-only control/live verifiers."""
from dataclasses import replace
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from optional.bridge.core.contracts import SessionIdentity, WorkspaceIdentity
from optional.bridge.core.pairing import ChatBinding, PairingError, PairingStore


def chat(name, adapter='test-adapter', project='test-project'):
    identity = SessionIdentity(name, 'native-' + name, adapter, 'incarnation-' + name,
                               WorkspaceIdentity('test-host', project, 'worktree-' + name))
    return ChatBinding(identity, 'test-account', 1)


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-pairing-')
        self.path = Path(self.temp.name) / 'pairing.sqlite'
        self.live = {}
        self.approvals = {}
        self.store = self.open_store()
        self.a, self.b, self.c = chat('a'), chat('b', adapter='other-adapter'), chat('c')
        for peer in (self.a, self.b, self.c):
            self.observe(peer)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def live_check(self, peer):
        return self.live.get(peer.identity.session_id) == peer

    def owner_check(self, selection, proof):
        expected = self.approvals.pop(proof, None)
        return expected == selection

    def open_store(self):
        return PairingStore(self.path, live_check=self.live_check, owner_check=self.owner_check)

    def observe(self, peer):
        self.live[peer.identity.session_id] = peer
        self.store.observe(peer)

    def select(self, enabled=True, bidirectional=True):
        proposal = self.store.propose('a', 'b', enabled=enabled, bidirectional=bidirectional)
        proof = b'test-only-owner-proof'
        self.approvals[proof] = proposal
        return self.store.owner_select(proposal, proof)

    def test_a_route_extension_opens_only_directions_the_owner_never_chose(self):
        asked = []
        self.store.extend_routes(lambda sender, recipient: asked.append((sender, recipient)) or True)
        self.store.require_selected(self.a, self.b)  # no owner row: the extension decides
        with self.store.selected_guard(self.a, self.b): pass
        self.select(enabled=False, bidirectional=False)  # the owner disconnects a -> b
        asked.clear()
        with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
            self.store.require_selected(self.a, self.b)
        with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
            with self.store.selected_guard(self.a, self.b): pass
        self.assertEqual(asked, [])  # an owner row decides alone
        self.assertIs(self.store.owner_choice(self.a, self.b), False)
        self.assertIsNone(self.store.owner_choice(self.b, self.a))
        self.store.require_selected(self.b, self.a)
        # The extension's chats must still be the observed, live bindings.
        del self.live['c']
        with self.assertRaisesRegex(PairingError, 'chat-live-binding-unverified'):
            self.store.require_selected(self.a, self.c)

    def test_a_route_extension_is_set_once_and_a_failing_one_opens_nothing(self):
        def broken(sender, recipient): raise RuntimeError('broken')
        self.store.extend_routes(broken)
        with self.assertRaisesRegex(PairingError, 'route-extension-set'):
            self.store.extend_routes(lambda sender, recipient: True)
        with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
            self.store.require_selected(self.a, self.b)

    def test_an_owner_block_needs_the_owner_proof_and_writes_only_a_disabled_row(self):
        with self.assertRaisesRegex(PairingError, 'owner-control-unverified'):
            self.store.owner_block(self.a, self.b, b'forged')
        self.assertIsNone(self.store.owner_choice(self.a, self.b))
        self.select()
        self.store.withdraw('b')  # works for a chat that is not listed now
        proof = b'test-only-block-proof'
        from optional.bridge.core.pairing import ChatSelection
        self.approvals[proof] = ChatSelection(self.a.reference, self.b.reference, False, False)
        self.store.owner_block(self.a, self.b, proof)
        self.assertIs(self.store.owner_choice(self.a, self.b), False)
        self.assertIs(self.store.owner_choice(self.b, self.a), True)

    def test_discovery_does_not_connect_other_chats_even_in_same_project(self):
        self.assertEqual(len(self.store.registered()), 3)
        for target in (self.b, self.c):
            with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
                self.store.require_selected(self.a, target)

    def test_exact_bidirectional_selection_leaves_third_chat_disconnected(self):
        self.select()
        self.store.require_selected(self.a, self.b)
        self.store.require_selected(self.b, self.a)
        with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
            self.store.require_selected(self.a, self.c)

    def test_one_way_selection_does_not_authorize_a_reply(self):
        self.select(bidirectional=False)
        self.store.require_selected(self.a, self.b)
        with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
            self.store.require_selected(self.b, self.a)

    def test_revoke_and_reopen_preserve_disconnection(self):
        self.select()
        self.select(enabled=False)
        self.store.close()
        self.store = self.open_store()
        for peer in (self.a, self.b):
            self.store.observe(peer)
        with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
            self.store.require_selected(self.a, self.b)

    def test_selection_survives_restart_only_after_live_reobservation(self):
        self.select()
        self.store.close()
        self.store = self.open_store()
        with self.assertRaisesRegex(PairingError, 'chat-not-observed'):
            self.store.require_selected(self.a, self.b)
        self.store.observe(self.a)
        self.store.observe(self.b)
        self.store.require_selected(self.a, self.b)

    def test_account_workspace_native_id_or_adapter_change_cannot_inherit_remembered_selection(self):
        changes = (replace(self.a, account_ref='another-account'),
                   replace(self.a, identity=replace(self.a.identity, native_session_id='different-native-chat')),
                   replace(self.a, identity=replace(self.a.identity, adapter_id='different-adapter')),
                   replace(self.a, identity=replace(self.a.identity,
                       workspace=replace(self.a.identity.workspace, workspace_id='new-worktree'))))
        self.select()
        for changed in changes:
            with self.subTest(changed=changed):
                self.observe(changed)
                with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
                    self.store.require_selected(changed, self.b)
                self.observe(self.a)
                # The verified original logical chat returns; the changed one
                # never acquired the original owner's connection.
                self.store.require_selected(self.a, self.b)

    def test_fresh_permission_or_incarnation_observation_restores_choice_but_not_old_binding(self):
        self.select()
        for changed in (replace(self.a, permission_revision=2),
                replace(self.a, identity=replace(self.a.identity, incarnation_id='new-incarnation'))):
            with self.subTest(changed=changed):
                self.observe(changed)
                self.store.require_selected(changed, self.b)
                with self.assertRaisesRegex(PairingError, 'chat-not-observed'):
                    self.store.require_selected(self.a, self.b)
                self.observe(self.a)
                self.store.require_selected(self.a, self.b)

    def test_stale_owner_proposal_cannot_select_a_replacement_chat(self):
        proposal = self.store.propose('a', 'b')
        proof = b'test-only-proof'
        self.approvals[proof] = proposal
        changed = replace(self.b, identity=replace(self.b.identity, incarnation_id='replacement'))
        self.observe(changed)
        with self.assertRaisesRegex(PairingError, 'selection-chat-changed'):
            self.store.owner_select(proposal, proof)

    def test_owner_proof_is_bound_to_exact_proposal_and_cannot_be_replayed(self):
        proposal = self.store.propose('a', 'b')
        other = self.store.propose('a', 'c')
        self.approvals[b'test-proof'] = other
        with self.assertRaisesRegex(PairingError, 'owner-control-unverified'):
            self.store.owner_select(proposal, b'test-proof')
        self.select()
        with self.assertRaisesRegex(PairingError, 'owner-control-unverified'):
            self.store.owner_select(proposal, b'test-only-owner-proof')

    def test_missing_or_nonboolean_control_verifier_fails_closed(self):
        self.store.close()
        self.store = PairingStore(self.path, live_check=self.live_check)
        self.store.observe(self.a)
        self.store.observe(self.b)
        with self.assertRaisesRegex(PairingError, 'owner-control-unverified'):
            self.store.owner_select(self.store.propose('a', 'b'), b'test-proof')
        self.store.close()
        self.store = PairingStore(self.path, live_check=self.live_check,
                                  owner_check=lambda proposal, proof: 1)
        self.store.observe(self.a)
        self.store.observe(self.b)
        with self.assertRaisesRegex(PairingError, 'owner-control-unverified'):
            self.store.owner_select(self.store.propose('a', 'b'), b'test-proof')

    def test_dead_or_unverifiable_chat_suspends_choice_until_fresh_reobservation(self):
        self.select()
        self.live.pop('b')
        with self.assertRaisesRegex(PairingError, 'chat-live-binding-unverified'):
            self.store.require_selected(self.a, self.b)
        self.observe(self.b)
        self.store.require_selected(self.a, self.b)

    def test_missing_live_verifier_cannot_enroll_a_self_claimed_chat(self):
        self.store.close()
        self.store = PairingStore(self.path)
        with self.assertRaisesRegex(PairingError, 'chat-live-binding-unverified'):
            self.store.observe(self.a)

    def test_closed_store_refuses_use(self):
        self.store.close()
        with self.assertRaisesRegex(PairingError, 'pairing-store-closed'):
            self.store.observe(self.a)

    def test_owner_selection_rechecks_chats_after_control_verification(self):
        proposal = self.store.propose('a', 'b')
        def approve_and_replace(selection, proof):
            self.live['b'] = replace(self.b, permission_revision=2)
            return True
        self.store.close()
        self.store = PairingStore(self.path, live_check=self.live_check, owner_check=approve_and_replace)
        self.store.observe(self.a)
        self.store.observe(self.b)
        with self.assertRaisesRegex(PairingError, 'chat-live-binding-unverified'):
            self.store.owner_select(proposal, b'test-proof')
        self.observe(self.b)
        with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
            self.store.require_selected(self.a, self.b)

    def test_capacity_failure_rolls_back_both_directions(self):
        from unittest.mock import patch
        proposal = self.store.propose('a', 'b')
        self.approvals[b'test-proof'] = proposal
        with patch('optional.bridge.core.pairing.MAX_ROUTES', 1):
            with self.assertRaisesRegex(PairingError, 'route-capacity'):
                self.store.owner_select(proposal, b'test-proof')
        for left, right in ((self.a, self.b), (self.b, self.a)):
            with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
                self.store.require_selected(left, right)

    def test_foreign_database_is_refused_without_adopting_its_contents(self):
        import sqlite3
        other = Path(self.temp.name) / 'foreign.sqlite'
        with closing(sqlite3.connect(other)) as db, db:
            db.execute('CREATE TABLE foreign_content(value TEXT)')
        with self.assertRaisesRegex(PairingError, 'pairing-state-open-failed'):
            PairingStore(other, live_check=self.live_check, owner_check=self.owner_check)
        with closing(sqlite3.connect(other)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                             [('foreign_content',)])

    def test_withdraw_then_reopen_same_chats_restores_remembered_bidirectional_route(self):
        self.select()
        self.store.withdraw('a')
        self.store.withdraw('b')
        with self.assertRaisesRegex(PairingError, 'chat-not-observed'):
            self.store.require_selected(self.a, self.b)
        reopened_a = replace(self.a, identity=replace(self.a.identity, incarnation_id='reopened-a'))
        reopened_b = replace(self.b, permission_revision=3,
            identity=replace(self.b.identity, incarnation_id='reopened-b'))
        self.observe(reopened_a)
        self.observe(reopened_b)
        self.store.require_selected(reopened_a, reopened_b)
        self.store.require_selected(reopened_b, reopened_a)
        for old_a, old_b in ((self.a, reopened_b), (reopened_a, self.b), (self.a, self.b)):
            with self.assertRaisesRegex(PairingError, 'chat-not-observed'):
                self.store.require_selected(old_a, old_b)

    def test_store_restart_and_native_process_reopen_require_fresh_exact_observations(self):
        self.select(bidirectional=False)
        self.store.close()
        self.store = self.open_store()
        reopened_a = replace(self.a, permission_revision=2,
            identity=replace(self.a.identity, incarnation_id='reopened-a'))
        reopened_b = replace(self.b, identity=replace(self.b.identity, incarnation_id='reopened-b'))
        with self.assertRaisesRegex(PairingError, 'chat-not-observed'):
            self.store.require_selected(reopened_a, reopened_b)
        self.observe(reopened_a)
        with self.assertRaisesRegex(PairingError, 'chat-not-observed'):
            self.store.require_selected(reopened_a, reopened_b)
        self.observe(reopened_b)
        self.store.require_selected(reopened_a, reopened_b)
        with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
            self.store.require_selected(reopened_b, reopened_a)

    def test_explicit_owner_disconnect_stays_disconnected_after_new_native_incarnation(self):
        self.select()
        self.select(enabled=False)
        self.store.withdraw('a')
        self.store.withdraw('b')
        self.store.close()
        self.store = self.open_store()
        reopened_a = replace(self.a, identity=replace(self.a.identity, incarnation_id='reopened-a'))
        reopened_b = replace(self.b, permission_revision=2,
            identity=replace(self.b.identity, incarnation_id='reopened-b'))
        self.observe(reopened_a)
        self.observe(reopened_b)
        for left, right in ((reopened_a, reopened_b), (reopened_b, reopened_a)):
            with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
                self.store.require_selected(left, right)

    def test_owner_active_routes_project_current_exact_bindings_only(self):
        self.select()
        self.store.withdraw('b')
        self.assertEqual(self.store.owner_routes(), ())
        changed = replace(self.b, permission_revision=2,
            identity=replace(self.b.identity, incarnation_id='reopened-b'))
        self.observe(changed)
        expected = {(self.a.reference, changed.reference), (changed.reference, self.a.reference)}
        self.assertEqual(set(self.store.owner_routes()), expected)
        self.assertNotIn(self.b.reference, {ref for route in self.store.owner_routes() for ref in route})

    def legacy_store(self):
        import sqlite3
        from optional.bridge.core.pairing import APPLICATION_ID
        self.store.close()
        self.path = Path(self.temp.name)/'legacy-pairing.sqlite'
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('CREATE TABLE meta(id INTEGER PRIMARY KEY,revision INTEGER NOT NULL);'
                'INSERT INTO meta VALUES(1,7);'
                'CREATE TABLE routes(sender TEXT NOT NULL,recipient TEXT NOT NULL,PRIMARY KEY(sender,recipient));')
            db.execute('INSERT INTO routes VALUES(?,?)', (self.a.reference, self.b.reference))
            db.execute('PRAGMA application_id=%d' % APPLICATION_ID)
            db.execute('PRAGMA user_version=1')
        self.store = self.open_store()

    def test_legacy_exact_hash_choice_promotes_only_after_original_bindings_are_verified(self):
        self.legacy_store()
        reopened_a = replace(self.a, identity=replace(self.a.identity, incarnation_id='reopened-a'))
        reopened_b = replace(self.b, permission_revision=2)
        self.observe(reopened_a)
        self.observe(reopened_b)
        with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
            self.store.require_selected(reopened_a, reopened_b)
        self.observe(self.a)
        self.observe(self.b)
        self.store.require_selected(self.a, self.b)
        self.store.close()
        self.store = self.open_store()
        self.observe(reopened_a)
        self.observe(reopened_b)
        self.store.require_selected(reopened_a, reopened_b)
        with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
            self.store.require_selected(reopened_b, reopened_a)

    def test_legacy_promotion_requires_both_original_endpoints_freshly_live(self):
        self.legacy_store()
        self.observe(self.a)
        self.live.pop('a')
        self.observe(self.b)
        with self.assertRaises(PairingError):
            self.store.require_selected(self.a, self.b)
        self.observe(self.a)
        self.store.require_selected(self.a, self.b)

    def test_explicit_disconnect_cannot_be_resurrected_by_unresolved_legacy_selection(self):
        self.legacy_store()
        reopened_a = replace(self.a, permission_revision=2)
        reopened_b = replace(self.b, permission_revision=2)
        self.observe(reopened_a)
        self.observe(reopened_b)
        self.select()
        self.select(enabled=False)
        self.observe(self.a)
        self.observe(self.b)
        with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
            self.store.require_selected(self.a, self.b)
        self.store.close()
        self.store = self.open_store()
        self.observe(reopened_a)
        self.observe(reopened_b)
        with self.assertRaisesRegex(PairingError, 'chat-route-not-selected'):
            self.store.require_selected(reopened_a, reopened_b)

    def test_remembered_snapshot_survives_suspension_and_owner_disable_removes_grant(self):
        self.select()
        expected = {(self.a.logical_reference, self.b.logical_reference),
            (self.b.logical_reference, self.a.logical_reference)}
        self.assertEqual(set(self.store.remembered_routes()), expected)
        self.store.withdraw('b')
        self.assertEqual(self.store.owner_routes(), ())
        self.assertEqual(set(self.store.remembered_routes()), expected)
        self.observe(self.b)
        self.select(enabled=False)
        self.assertEqual(self.store.remembered_routes(), ())
