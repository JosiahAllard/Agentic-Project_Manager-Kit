"""Exact reply-job framing over real owned native-shaped pipes, with no live model."""
from dataclasses import replace
import unittest

from optional.bridge.hosts.codex.delivery import CodexDesktopAdapter,native_id
from optional.bridge.core.messages import reply_operation_id,OutboundMessage,NativeResult
from tests.bridge.test_codex_delivery import CodexDeliveryTests
from tests.bridge.test_claude_peer import ClaudePeerTests
from tests.bridge.test_codex_runtime_delivery import DirectRuntimeDeliveryTests


class CodexReplyPacketTests(CodexDeliveryTests):
    def test_reply_packet_keeps_original_target_permissions_and_exact_parent_body(self):
        self.message=replace(self.message,operation_id=reply_operation_id('parent-operation'),
            reply_of='parent-operation')
        self.serve()
        receipt=CodexDesktopAdapter(lease=self.lease,timeout=2).send(self.message)
        self.threads[0].join(3)
        request=self.requests[0]
        params=request['params'];args=params['arguments']
        header,body=args['prompt'].split('\n\n',1)
        self.assertIn('Reply to bridge operation: parent-operation',header)
        self.assertNotIn('bridge_reply',header)
        self.assertEqual(body,self.message.text)
        self.assertEqual(args['threadId'],self.target_context.thread_id)
        self.assertEqual(set(args),{'threadId','prompt'})
        self.assertEqual((receipt.native_id,params['callId']),(native_id(self.message),native_id(self.message)))
        self.assertEqual(set(params),{'arguments','callerSource','callId','namespace','threadId','turnId','tool'})


class ClaudeReplyPacketTests(ClaudePeerTests):
    def test_reply_packet_retains_native_sender_mode_and_omits_reply_invitation(self):
        self.message=replace(self.message,operation_id=reply_operation_id('parent-operation'),
            reply_of='parent-operation')
        self.dispatch()
        frame=self.received[1]
        self.assertEqual(frame['type'],'user')
        self.assertIn('from-mode="prompting"',frame['message']['content'])
        self.assertIn('Reply to bridge operation: parent-operation',frame['message']['content'])
        self.assertNotIn('Use bridge_reply',frame['message']['content'])
        self.assertIn(self.message.text,frame['message']['content'])
        self.assertEqual(self.received[0],{'type':'auth','token':'b'*32})


class CompletedCodexReplyTests(DirectRuntimeDeliveryTests):
    def test_completed_reply_author_leaves_parent_known_and_sends_zero_native_bytes(self):
        adapter=self.populate()
        parent=OutboundMessage('parent-operation',self.recipient.identity.session_id,
            self.recipient,self.sender,'original parent body')
        self.controller.outbox.admit(parent)
        self.controller.outbox.run_dispatch(parent.operation_id,lambda message:NativeResult(
            message.operation_id,message.recipient.reference,'recipient-accepted','parent-native'))
        calls=self.controller.frontend(self.direct_source.observe).calls
        proof=calls.issue(parent.intent,self.metadata)
        self.controller.core.reply_to(parent.operation_id,'exact returned body',proof)
        child=self.controller.outbox.reply_delivery(parent.operation_id)['child']
        self.controller.adapter(adapter)
        self.complete_source()
        self.assertTrue(self.controller._live(self.sender))
        self.assertEqual(self.controller.core.dispatch_once(child).state,'failed')
        self.assertEqual(self.rpc.requests,[])
        self.assertEqual(self.controller.outbox.reply(parent.operation_id),'exact returned body')
        self.assertEqual(self.controller.outbox.receipt(parent.operation_id).state,'reply-received')

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(kind(name) for kind in (CodexReplyPacketTests,ClaudeReplyPacketTests,CompletedCodexReplyTests)
        for name in sorted(kind.__dict__) if name.startswith('test_'))
