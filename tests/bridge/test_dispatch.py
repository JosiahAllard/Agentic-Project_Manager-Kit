"""Shared dispatch contract with explicit test-only host and runtime adapters."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import os
import subprocess
import sys
import unittest

from optional.bridge.core.contracts import AdapterDescription, Capability
from optional.bridge.core.dispatch import CoreBroker
from optional.bridge.core.messages import BeforeWriteError, DeliveryError, NativeResult, SenderAuthority
from optional.bridge.core.outbox import Outbox
from optional.bridge.core.pairing import PairingStore
from tests.bridge.test_pairing import chat


class TestAdapter:
    def __init__(self, adapter_id):
        self.adapter_id, self.calls, self.fail, self.response = adapter_id, [], None, None

    def describe(self):
        return AdapterDescription(self.adapter_id, (Capability.SEND,))

    def send(self, message):
        self.calls.append(message)
        if self.fail is not None:
            raise self.fail
        return self.response or NativeResult(message.operation_id, message.recipient.reference,
                                             'reply-received', 'test-native-op', 'test reply')


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-dispatch-')
        self.a, self.b, self.c = chat('a'), chat('b', adapter='test-recipient'), chat('c')
        self.live = {peer.identity.session_id: peer for peer in (self.a, self.b, self.c)}
        self.proofs = {}
        self.root, self.now, self.leader = 'test-task-root', 1000.0, True
        self.pairings = PairingStore(Path(self.temp.name) / 'pair.sqlite',
            live_check=lambda peer: self.live.get(peer.identity.session_id) == peer,
            owner_check=lambda selection, proof: proof == b'test-owner-proof')
        for peer in (self.a, self.b, self.c):
            self.pairings.observe(peer)
        self.select(True)
        self.path = Path(self.temp.name) / 'outbox.sqlite'
        self.store = self.open_store()
        self.store.claim()
        self.adapter = TestAdapter('test-recipient')
        self.allowed = True
        self.core = self.open_core()

    def tearDown(self):
        self.store.close()
        self.pairings.close()
        self.temp.cleanup()

    def open_store(self):
        return Outbox(self.path, leadership_check=lambda: self.leader, clock=lambda: self.now)

    def open_core(self):
        return CoreBroker(self.pairings, self.store, {'test-recipient': self.adapter},
                          authorize=self.authorize, dispatch_check=lambda message: self.allowed)

    def authorize(self, intent, proof):
        return self.proofs.get(proof)

    def select(self, enabled):
        self.pairings.owner_select(self.pairings.propose('a', 'b', enabled=enabled), b'test-owner-proof')

    def admit(self, operation='op', text='private test message', **kwargs):
        self.proofs[b'test-caller-proof'] = SenderAuthority(self.a, self.root)
        return self.core.admit(operation, self.b, text, b'test-caller-proof', **kwargs)

    def test_full_message_survives_restart_before_dispatch(self):
        self.admit(text='test message with unicode: \u03bb')
        self.store.close()
        self.store = self.open_store()
        self.store.claim()
        self.core = self.open_core()
        self.assertEqual(self.store.message('op').text, 'test message with unicode: \u03bb')
        receipt = self.core.dispatch_once('op')
        self.assertEqual(receipt.state, 'reply-received')
        self.assertEqual(self.core.reply('op', b'test-caller-proof'), 'test reply')

    def test_duplicate_id_returns_original_receipt_and_never_resends(self):
        self.admit()
        self.core.dispatch_once('op')
        self.assertEqual(self.admit().state, 'reply-received')
        self.assertEqual(self.core.dispatch_once('op').state, 'reply-received')
        self.assertEqual(len(self.adapter.calls), 1)
        with self.assertRaisesRegex(DeliveryError, 'operation-conflict'):
            self.admit(text='different body')

    def test_revoke_queued_route_prevents_native_write(self):
        self.admit()
        self.select(False)
        self.assertEqual(self.core.dispatch_once('op').state, 'refused')
        self.assertEqual(self.adapter.calls, [])

    def test_identity_drift_prevents_native_write(self):
        self.admit()
        self.live['b'] = replace(self.b, permission_revision=2)
        self.assertEqual(self.core.dispatch_once('op').state, 'refused')
        self.assertEqual(self.adapter.calls, [])

    def test_unverified_caller_and_dispatch_context_fail_closed(self):
        with self.assertRaisesRegex(DeliveryError, 'caller-context-unverified'):
            self.core.admit('op', self.b, 'text', b'unknown-proof')
        self.assertIsNone(self.store.receipt('op'))
        self.admit()
        self.allowed = False
        with self.assertRaisesRegex(DeliveryError, 'dispatch-context-unverified'):
            self.core.dispatch_once('op')
        self.assertEqual(self.store.receipt('op').state, 'queued')
        self.assertEqual(self.adapter.calls, [])

    def test_lost_native_ack_is_unknown_and_never_automatically_retried(self):
        self.admit()
        self.adapter.fail = TimeoutError('private transport detail must not escape')
        result = self.core.dispatch_once('op')
        self.assertEqual(result.state, 'unknown')
        self.assertNotIn('private transport', str(result))
        self.store.close()
        self.store = self.open_store()
        self.store.claim()
        self.core = self.open_core()
        self.assertEqual(self.core.dispatch_once('op').state, 'unknown')
        self.assertEqual(len(self.adapter.calls), 1)

    def test_native_refusal_persists_for_root_across_new_ids_and_restart(self):
        self.admit()
        self.admit('queued-sibling')
        self.adapter.response = NativeResult('op', self.b.reference, 'refused', 'test-native-op')
        self.assertEqual(self.core.dispatch_once('op').state, 'refused')
        self.assertEqual(self.store.receipt('queued-sibling').state, 'refused')
        self.store.close()
        self.store = self.open_store()
        self.store.claim()
        self.core = self.open_core()
        with self.assertRaisesRegex(DeliveryError, 'task-root-refused'):
            self.admit('renamed-op')

    def test_held_receipt_remains_held_without_timeout_resend(self):
        self.admit(expires_at=1001)
        self.adapter.response = NativeResult('op', self.b.reference, 'held', 'test-native-op')
        self.assertEqual(self.core.dispatch_once('op').state, 'held')
        self.now = 2000
        self.assertEqual(self.core.dispatch_once('op').state, 'held')
        self.assertEqual(len(self.adapter.calls), 1)

    def test_queued_expiry_has_no_native_side_effect(self):
        self.admit(expires_at=1001)
        self.now = 1001
        self.assertEqual(self.core.dispatch_once('op').state, 'expired')
        self.assertEqual(self.adapter.calls, [])

    def test_wrong_operation_or_recipient_receipt_is_unknown(self):
        self.admit()
        self.adapter.response = NativeResult('other-op', self.b.reference, 'reply-received', 'native', 'x')
        self.assertEqual(self.core.dispatch_once('op').state, 'unknown')
        self.admit('op-2')
        self.adapter.response = NativeResult('op-2', self.c.reference, 'reply-received', 'native', 'x')
        self.assertEqual(self.core.dispatch_once('op-2').state, 'unknown')

    def test_receipt_and_reply_cannot_be_read_by_another_chat(self):
        self.admit()
        self.core.dispatch_once('op')
        self.proofs[b'other-proof'] = SenderAuthority(self.c, self.root)
        for read in (self.core.receipt, self.core.reply):
            with self.assertRaisesRegex(DeliveryError, 'operation-caller-mismatch'):
                read('op', b'other-proof')

    def test_stale_broker_epoch_cannot_admit_or_dispatch(self):
        self.admit()
        other = self.open_store()
        try:
            other.claim()
            with self.assertRaisesRegex(DeliveryError, 'stale-broker-fence'):
                self.admit('new-op')
            with self.assertRaisesRegex(DeliveryError, 'stale-broker-fence'):
                self.core.dispatch_once('op')
            self.assertEqual(self.adapter.calls, [])
        finally:
            other.close()

    def test_missing_or_lost_leadership_disables_writes(self):
        self.admit()
        self.leader = False
        with self.assertRaisesRegex(DeliveryError, 'broker-ownership-unverified'):
            self.core.dispatch_once('op')
        self.assertEqual(self.adapter.calls, [])

    def test_message_and_reply_bounds_and_foreign_database(self):
        for text in ('x' * 65537, '\ud800'):
            with self.assertRaises(DeliveryError):
                self.admit(text=text)
        other = Path(self.temp.name) / 'foreign.sqlite'
        with closing(sqlite3.connect(other)) as db, db:
            db.execute('CREATE TABLE foreign_data(value TEXT)')
        with self.assertRaisesRegex(DeliveryError, 'outbox-open-failed'):
            Outbox(other)

    def test_event_cursor_is_durable_and_scoped_to_operation(self):
        self.admit()
        first = self.core.events('op', b'test-caller-proof')
        self.assertEqual([event.state for event in first], ['queued'])
        self.core.dispatch_once('op')
        later = self.core.events('op', b'test-caller-proof', after=first[-1].cursor)
        self.assertEqual([event.state for event in later], ['dispatching', 'reply-received'])
        self.store.close()
        self.store = self.open_store()
        self.store.claim()
        self.core = self.open_core()
        self.assertEqual(self.core.events('op', b'test-caller-proof', after=later[-1].cursor), ())

    def test_two_concurrent_dispatchers_send_an_operation_once(self):
        self.admit()
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = tuple(pool.map(self.core.dispatch_once, ('op', 'op')))
        self.assertEqual([receipt.state for receipt in receipts], ['reply-received', 'reply-received'])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_actual_child_crash_after_external_side_effect_recovers_as_unknown(self):
        self.admit()
        self.store.close()
        marker = Path(self.temp.name) / 'owned-native-effect.txt'
        code = '''import os,pathlib,sys
sys.path.insert(0,sys.argv[1])
from optional.bridge.core.outbox import Outbox
box=Outbox(pathlib.Path(sys.argv[2]),leadership_check=lambda:True,clock=lambda:1000)
box.claim()
def send(message):
    pathlib.Path(sys.argv[3]).write_text('owned test effect',encoding='utf-8')
    os._exit(23)
box.run_dispatch('op',send)
'''
        result = subprocess.run([sys.executable, '-I', '-S', '-c', code,
            str(Path(__file__).resolve().parents[2]), str(self.path), str(marker)],
            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 23, result.stderr.decode(errors='replace'))
        self.assertEqual(marker.read_text(encoding='utf-8'), 'owned test effect')
        self.store = self.open_store()
        self.store.claim()
        self.core = self.open_core()
        self.assertEqual(self.store.receipt('op').state, 'unknown')
        self.assertEqual(self.core.dispatch_once('op').state, 'unknown')
        self.assertEqual(self.adapter.calls, [])

    def test_leadership_loss_between_durable_intent_and_native_write_blocks_io(self):
        self.admit()
        checks = []
        def leader():
            checks.append(None)
            return len(checks) <= 2
        self.store._leader = leader
        with self.assertRaisesRegex(DeliveryError, 'broker-ownership-unverified'):
            self.core.dispatch_once('op')
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.store.receipt('op').state, 'dispatching')
        self.store._leader = lambda: True
        self.store.claim()
        self.assertEqual(self.store.receipt('op').state, 'unknown')

    def test_verified_before_write_failure_is_distinct_from_unknown(self):
        self.admit()
        self.adapter.fail = BeforeWriteError('test-native-not-ready')
        self.assertEqual(self.core.dispatch_once('op').state, 'failed')
        self.assertEqual(self.core.dispatch_once('op').state, 'failed')
        self.assertEqual(len(self.adapter.calls), 1)

    def test_before_write_failure_keeps_the_adapters_fixed_code_as_its_reason(self):
        self.admit()
        self.adapter.fail = BeforeWriteError('test-native-not-ready')
        receipt = self.core.dispatch_once('op')
        self.assertEqual((receipt.state, receipt.reason), ('failed', 'test-native-not-ready'))
        self.assertEqual(self.store.receipt('op').reason, 'test-native-not-ready')

    def test_before_write_failure_with_a_code_that_is_not_an_id_gets_the_generic_reason(self):
        for operation, code in (('op', 'private detail, not an id'), ('op2', 'x' * 200)):
            self.admit(operation)
            self.adapter.fail = BeforeWriteError(code)
            receipt = self.core.dispatch_once(operation)
            self.assertEqual((receipt.state, receipt.reason), ('failed', 'native-before-write'))

    def test_clock_regression_and_invalid_expiry_fail_closed(self):
        self.admit()
        self.now = 999
        with self.assertRaisesRegex(DeliveryError, 'outbox-clock-unverified'):
            self.core.dispatch_once('op')
        self.now = 1000
        for value in (float('nan'), float('inf'), True, 10**500):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(DeliveryError, 'invalid-message-expiry'):
                    self.admit('new-op', expires_at=value)

    def test_authenticated_late_receipt_resolves_unknown_without_resend(self):
        self.admit()
        self.adapter.fail = TimeoutError('test timeout')
        self.core.dispatch_once('op')
        native = NativeResult('op', self.b.reference, 'reply-received', 'test-native-op', 'late reply')
        self.store.append_native('op', native)
        self.assertEqual(self.core.reply('op', b'test-caller-proof'), 'late reply')
        self.assertEqual(len(self.adapter.calls), 1)

    def test_event_capacity_is_checked_before_any_native_write(self):
        from unittest.mock import patch
        self.admit()
        self.admit('sibling', text='different queued message')
        with patch('optional.bridge.core.outbox.MAX_EVENTS', 3):
            with self.assertRaisesRegex(DeliveryError, 'event-capacity'):
                self.core.dispatch_once('op')
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.store.receipt('op').state, 'queued')
        self.assertEqual(self.store.receipt('sibling').state, 'queued')

    def test_malformed_or_wrong_version_adapter_is_rejected_before_native_io(self):
        from unittest.mock import patch
        self.admit()
        descriptions = (AdapterDescription('wrong-adapter', (Capability.SEND,)),
                        AdapterDescription('test-recipient', (Capability.SEND,), contract_version=2),
                        AdapterDescription('test-recipient', (Capability.SEND,), contract_version=True),
                        AdapterDescription('test-recipient', ('send',)),
                        AdapterDescription('test-recipient', (Capability.READ_STATUS,)))
        for description in descriptions:
            with patch.object(self.adapter, 'describe', return_value=description):
                with self.assertRaisesRegex(DeliveryError, 'runtime-adapter-incompatible'):
                    self.core.dispatch_once('op')
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.store.receipt('op').state, 'queued')
