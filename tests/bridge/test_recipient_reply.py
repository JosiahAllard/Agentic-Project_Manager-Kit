"""Explicit recipient replies, without guessing which pending message text belongs to."""
import unittest

from optional.bridge.core.messages import DeliveryError, NativeResult, SenderAuthority
from tests.bridge.test_dispatch import DispatchTests


class RecipientReplyTests(DispatchTests):
    def test_authenticated_recipient_can_reply_to_its_exact_operation(self):
        self.admit()
        self.adapter.response = NativeResult('op', self.b.reference, 'recipient-accepted', 'native-op')
        self.core.dispatch_once('op')
        self.proofs[b'recipient-proof'] = SenderAuthority(self.b, 'recipient-native-turn')
        receipt = self.core.reply_to('op', 'explicit reply', b'recipient-proof')
        self.assertEqual(receipt.state, 'reply-received')
        self.assertEqual(self.core.reply('op', b'test-caller-proof'), 'explicit reply')

    def test_another_chat_cannot_reply_to_a_guessed_operation_id(self):
        self.admit()
        self.adapter.response = NativeResult('op', self.b.reference, 'recipient-accepted', 'native-op')
        self.core.dispatch_once('op')
        self.proofs[b'other-proof'] = SenderAuthority(self.c, 'other-turn')
        with self.assertRaisesRegex(DeliveryError, 'operation-recipient-mismatch'):
            self.core.reply_to('op', 'wrong reply', b'other-proof')
        self.assertEqual(self.store.receipt('op').state, 'recipient-accepted')

    def test_one_way_connection_does_not_authorize_reply_content(self):
        self.select(False)
        self.pairings.owner_select(self.pairings.propose('a', 'b', bidirectional=False), b'test-owner-proof')
        self.admit()
        self.adapter.response = NativeResult('op', self.b.reference, 'recipient-accepted', 'native-op')
        self.core.dispatch_once('op')
        self.proofs[b'recipient-proof'] = SenderAuthority(self.b, 'recipient-turn')
        with self.assertRaises(DeliveryError):
            self.core.reply_to('op', 'not allowed', b'recipient-proof')
        self.adapter.response = None
        self.store.append_native('op', NativeResult('op', self.b.reference, 'reply-received', 'native-op', 'stored reply'))
        with self.assertRaisesRegex(DeliveryError, 'reply-route-not-selected'):
            self.core.reply('op', b'test-caller-proof')

    def test_a_recipient_no_longer_live_is_not_reported_as_an_unselected_route(self):
        # Both directions stay selected: a dead chat is a different refusal from a route off.
        self.admit()
        self.adapter.response = NativeResult('op', self.b.reference, 'recipient-accepted', 'native-op')
        self.core.dispatch_once('op')
        self.proofs[b'recipient-proof'] = SenderAuthority(self.b, 'recipient-turn')
        self.core.reply_to('op', 'explicit reply', b'recipient-proof')
        del self.live['b']
        with self.assertRaisesRegex(DeliveryError, 'reply-chat-not-observed'):
            self.core.reply('op', b'test-caller-proof')
        with self.assertRaisesRegex(DeliveryError, 'reply-chat-not-observed'):
            self.core.reply('op', b'test-caller-proof')  # now dropped from the observed list too

    def test_reply_to_from_a_sender_no_longer_live_names_that_refusal(self):
        self.admit()
        self.adapter.response = NativeResult('op', self.b.reference, 'recipient-accepted', 'native-op')
        self.core.dispatch_once('op')
        self.proofs[b'recipient-proof'] = SenderAuthority(self.b, 'recipient-turn')
        del self.live['a']
        with self.assertRaisesRegex(DeliveryError, 'reply-chat-not-observed'):
            self.core.reply_to('op', 'explicit reply', b'recipient-proof')

    def test_reply_before_native_dispatch_is_rejected(self):
        self.admit()
        self.proofs[b'recipient-proof'] = SenderAuthority(self.b, 'recipient-turn')
        with self.assertRaisesRegex(DeliveryError, 'reply-before-dispatch'):
            self.core.reply_to('op', 'too early', b'recipient-proof')


# Only the reply tests are loaded; imported base tests are covered in their own module.
def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in sorted(RecipientReplyTests.__dict__):
        if name.startswith('test_'):
            suite.addTest(RecipientReplyTests(name))
    return suite
