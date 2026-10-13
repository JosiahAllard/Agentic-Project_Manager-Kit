"""hosts.codex.adapter.session_registry (ported from the Codex branch's tests/codex/test_session_registry.py; its DeliveryTests went with session_delivery.py, the owner-grant
transport scaffolding the bridge (plan step 10) replaces, and its StartupTests with the runtime
registry hook-up of session_adapter.startup, which no hook entry could reach)."""
from dataclasses import replace
from pathlib import Path
import unittest
from unittest import mock
from types import SimpleNamespace

from hosts.codex.adapter.session_registry import (
    RuntimeInventory, RuntimeLease, SessionIdentity, SessionRegistry, path_key)
from kitlib.git import repo


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.cwd = path_key(str(Path('fixture-worktree').resolve()))
        self.repository = path_key(str(Path('fixture-main/.git').resolve()))
        self.classify = mock.Mock(return_value=SimpleNamespace(kind=repo.LINKED, common=self.repository))
        self.first = SessionIdentity('one', 'root-one', self.cwd, 'worker', None)
        self.second = SessionIdentity('two', 'root-two', self.cwd, 'reviewer', None)
        self.bindings = {value.thread: value for value in (self.first, self.second)}
        self.leases = tuple(RuntimeLease(value, 'generation-one', 1050, 'live')
                            for value in self.bindings.values())
        self.complete = True
        self.observed = 1000
        self.reader = mock.Mock(side_effect=lambda: RuntimeInventory(
            'native-provider', self.observed, self.complete, self.leases))
        self.registry = SessionRegistry(self.bindings.__getitem__, lease_reader=self.reader,
                                       authority='native-provider', clock=lambda: self.now, classify=self.classify)

    def register_pair(self):
        for binding in (self.first, self.second):
            self.registry.register(binding)

    def test_registration_matches_bound_native_identity_and_live_lease(self):
        status = self.registry.register(self.first)
        self.assertEqual((status['liveness'], status['generation']), ('live', 'generation-one'))
        self.assertEqual(self.registry.register(self.first), status)

    def test_unmarked_owner_is_preserved_without_claiming_liveness(self):
        binding = replace(self.first, role=None)
        registry = SessionRegistry(lambda _: binding)
        self.assertEqual(registry.register(binding)['liveness'], 'unknown')
        self.assertIsNone(registry.snapshot()[0]['role'])

    def test_loaded_metadata_cannot_substitute_for_a_lease(self):
        self.reader.side_effect = None
        self.reader.return_value = {'data': ['one'], 'status': 'loaded', 'alive': True}
        self.assertEqual(self.registry.register(self.first)['liveness'], 'unknown')

    def test_missing_partial_failed_or_stale_inventory_does_not_clear_coordinators(self):
        self.complete = False
        self.assertEqual(self.registry.coordinator_check(self.repository)['status'], 'unknown')
        self.complete = True
        self.observed = 980
        self.assertEqual(self.registry.coordinator_check(self.repository)['status'], 'unknown')
        self.reader.side_effect = OSError('connection unavailable')
        self.assertEqual(self.registry.register(self.first)['liveness'], 'unknown')

    def test_expired_and_dead_leases_are_stale(self):
        for lease in (replace(self.leases[0], expires_at=1000), replace(self.leases[0], state='dead')):
            with self.subTest(lease=lease):
                self.leases = (lease,)
                self.assertEqual(self.registry.register(self.first)['liveness'], 'stale')

    def test_changed_native_identity_fails_before_registration(self):
        self.bindings['one'] = replace(self.first, session='replacement')
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.registry.register(self.first)
        self.assertFalse(self.registry.identities)

    def test_changes_after_registration_become_unknown(self):
        self.registry.register(self.first)
        for changed in (replace(self.first, cwd=path_key(str(Path('other').resolve()))),
                        replace(self.first, role='reviewer'), replace(self.first, session='other')):
            with self.subTest(changed=changed):
                self.bindings['one'] = changed
                self.assertEqual(self.registry.status('one')['liveness'], 'unknown')

    def test_ambiguous_wrong_authority_or_unbounded_lease_is_unknown(self):
        bad = (RuntimeInventory('other', 1000, True, self.leases),
               RuntimeInventory('native-provider', 1000, True, (self.leases[0], self.leases[0])),
               RuntimeInventory('native-provider', 1000, True,
                                (replace(self.leases[0], expires_at=float('nan')),)),
               RuntimeInventory('native-provider', 1000, True,
                                (replace(self.leases[0], expires_at=1200),)),
               RuntimeInventory('native-provider', 1001, True, self.leases))
        self.reader.side_effect = None
        for inventory in bad:
            with self.subTest(inventory=inventory):
                self.reader.return_value = inventory
                self.assertEqual(self.registry.register(self.first)['liveness'], 'unknown')

    def test_authoritative_identity_mismatch_is_unknown(self):
        self.leases = (replace(self.leases[0], identity=replace(self.first, session='other')),)
        self.assertEqual(self.registry.register(self.first)['liveness'], 'unknown')

    def test_registration_writer_is_existing_service_and_failure_is_not_local_success(self):
        writer = mock.Mock(side_effect=ValueError('duplicate coordinator'))
        registry = SessionRegistry(self.bindings.__getitem__, lease_reader=self.reader,
                registration_writer=writer, authority='native-provider', clock=lambda: self.now, classify=self.classify)
        with self.assertRaisesRegex(ValueError, 'duplicate coordinator'):
            registry.register(self.first)
        self.assertFalse(registry.identities)
        writer.assert_called_once_with(self.first)

    def test_duplicate_coordinator_scan_scopes_repository_and_ignores_expired_peers(self):
        coordinator = replace(self.first, role='coordinator', repository=self.repository)
        peer = replace(coordinator, thread='peer', session='root-peer',
                       cwd=path_key(str(Path('fixture-second-worktree').resolve())))
        stale = replace(peer, thread='stale', session='root-stale')
        other = replace(peer, thread='elsewhere', role='worker', repository=path_key(str(Path('other').resolve())))
        self.leases = tuple(RuntimeLease(value, 'generation', expiry, 'live') for value, expiry in
                            ((coordinator, 1050), (peer, 1050), (stale, 999), (other, 1050)))
        self.bindings['one'] = coordinator
        self.registry.register(coordinator)
        result = self.registry.coordinator_check(self.repository, exclude='one')
        self.assertEqual(result, {'status': 'duplicate', 'live': ['peer'], 'stale': ['stale']})
        self.leases = (self.leases[2], self.leases[3])
        self.assertEqual(self.registry.coordinator_check(self.repository)['status'], 'stale')
        self.leases = (self.leases[1],)
        self.assertEqual(self.registry.coordinator_check(self.repository)['status'], 'clear')

    def test_unknown_or_unrelated_repository_query_cannot_return_clear(self):
        self.register_pair()
        self.leases = ()
        unknown = path_key(str(Path('unrelated-checkout').resolve()))
        self.classify.side_effect = lambda cwd: SimpleNamespace(kind=repo.NONE, common=None) if path_key(cwd) == unknown else SimpleNamespace(kind=repo.LINKED, common=self.repository)
        self.assertEqual(self.registry.coordinator_check(unknown)['status'], 'unknown')
        self.assertEqual(self.registry.coordinator_check(self.repository)['status'], 'clear')

    def test_two_linked_checkout_queries_share_derived_common_git_scope(self):
        coordinator = replace(self.first, role='coordinator', repository=self.repository)
        self.bindings['one'] = coordinator
        self.leases = (RuntimeLease(coordinator, 'generation', 1050, 'live'),)
        self.registry.register(coordinator)
        second_checkout = path_key(str(Path('fixture-second-worktree').resolve()))
        first = self.registry.coordinator_check(self.cwd)
        second = self.registry.coordinator_check(second_checkout)
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'duplicate')

    def test_coordinator_scope_is_derived_and_arbitrary_scope_or_child_is_refused(self):
        coordinator = replace(self.first, role='coordinator')
        self.bindings['one'] = coordinator
        self.registry.register(coordinator)
        self.assertEqual(self.registry.identities['one'].repository, self.repository)
        with self.assertRaisesRegex(ValueError, 'Git identity'):
            self.registry.register(coordinator, repository=str(Path('arbitrary').resolve()))
        with self.assertRaises(ValueError):
            self.registry.register(replace(coordinator, parent='parent'))

    def test_unknown_git_identity_refuses_coordinator_registration(self):
        self.classify.return_value = SimpleNamespace(kind=repo.UNKNOWN, common=None)
        with self.assertRaisesRegex(ValueError, 'verified Git'):
            self.registry.register(replace(self.first, role='coordinator'))

