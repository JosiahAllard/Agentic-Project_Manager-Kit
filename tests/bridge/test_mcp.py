"""Shared MCP tools with native source facts and explicit owned test routes."""
import base64
import io
import json
from pathlib import Path
import unittest

from optional.bridge.hosts.codex.account import CodexAccount
from optional.bridge.hosts.codex.calls import CodexCalls
from optional.bridge.core.callers import CallProofs
from optional.bridge.core.dispatch import CoreBroker
from optional.bridge.core.mcp import Server, serve
from optional.bridge.core.outbox import Outbox
from optional.bridge.core.pairing import PairingStore
from tests.bridge.test_pairing import chat
from tests.bridge import test_codex_context as nativefixtures

NATIVE_SOURCE = 'optional/bridge/hosts/codex/calls.py'


class McpTests(unittest.TestCase):
    def setUp(self):
        self.native=nativefixtures.CodexContextTests()
        self.native.setUp()
        self.root=self.native.root
        claims={'https://api.openai.com/auth':{'chatgpt_account_id':'test-account','chatgpt_user_id':'test-user'}}
        body=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        (self.root/'auth.json').write_text(json.dumps({'auth_mode':'chatgpt','tokens':{
            'account_id':'test-account','id_token':'e30.'+body+'.c2ln'}}),encoding='utf-8')
        self.source=CodexCalls(self.native.observer,CodexAccount(self.root),'test-host')
        self.authority=self.source.observe(self.native.meta)
        self.target=chat('recipient')
        self.other=chat('not-connected')
        self.live={peer.identity.session_id:peer for peer in (self.authority.sender,self.target,self.other)}
        self.pairs=PairingStore(self.root/'pairs.sqlite',
            live_check=lambda binding:self.live.get(binding.identity.session_id)==binding,
            owner_check=lambda selection,proof:proof==b'test-owner')
        for peer in self.live.values(): self.pairs.observe(peer)
        self.pairs.owner_select(self.pairs.propose(self.native.thread,'recipient'),b'test-owner')
        self.outbox=Outbox(self.root/'outbox.sqlite',leadership_check=lambda:True)
        self.outbox.claim()
        self.calls=CallProofs(observe=self.source.observe)
        self.core=CoreBroker(self.pairs,self.outbox,{},authorize=self.calls.authorize)
        self.server=Server(self.core,self.calls)
        self.server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}})
        self.server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})

    def tearDown(self):
        self.outbox.close()
        self.pairs.close()
        self.native.tearDown()

    def call(self,name,args=None,meta=None):
        return self.server.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
            'name':name,'arguments':args or {},'_meta':self.native.meta if meta is None else meta}})

    def payload(self,response): return response['result']['structuredContent']

    def send(self):
        return self.call('bridge_send',{'operation_id':'test-op','recipient':self.target.reference,'text':'private message'})

    def test_send_and_reads_use_native_source_identity_and_admit_full_message_once(self):
        self.assertEqual(self.payload(self.send())['state'],'queued')
        self.assertEqual(self.outbox.message('test-op').sender,self.authority.sender)
        self.assertEqual(self.outbox.message('test-op').task_root,self.native.thread)
        self.assertEqual(self.outbox.message('test-op').text,'private message')
        self.assertEqual(self.payload(self.send())['state'],'queued')
        self.assertEqual(self.payload(self.call('bridge_receipt',{'operation_id':'test-op'}))['state'],'queued')

    def test_connections_reveal_only_owner_selected_outbound_peers(self):
        result=self.payload(self.call('bridge_connections'))
        self.assertEqual([item['reference'] for item in result['chats']],[self.target.reference])
        refused=self.call('bridge_send',{'operation_id':'wrong','recipient':self.other.reference,'text':'secret'})
        self.assertTrue(refused['result']['isError'])
        self.assertIsNone(self.outbox.receipt('wrong'))

    def test_model_cannot_choose_sender_root_owner_proof_or_host_metadata_in_arguments(self):
        for key in ('sender','task_root','owner_approved','proof','_meta'):
            response=self.call('bridge_send',{'operation_id':'test-op','recipient':self.target.reference,
                'text':'private message',key:'pretend'})
            self.assertTrue(response['result']['isError'])
            self.assertIsNone(self.outbox.receipt('test-op'))
        self.assertTrue(self.call('bridge_owner_select')['error'])

    def test_missing_host_metadata_and_changed_native_login_cannot_submit(self):
        self.assertTrue(self.call('bridge_send',{'operation_id':'test-op','recipient':self.target.reference,
            'text':'private message'},meta={})['result']['isError'])
        (self.root/'auth.json').write_text('{"auth_mode":"apikey"}',encoding='utf-8')
        self.assertTrue(self.send()['result']['isError'])
        self.assertIsNone(self.outbox.receipt('test-op'))

    def test_caller_proof_is_one_use_exact_intent_and_never_an_argument_or_result(self):
        from optional.bridge.core.messages import MessageIntent
        import hashlib
        intent=MessageIntent('scope',self.target,hashlib.sha256(b'text').hexdigest())
        proof=self.calls.issue(intent,self.native.meta)
        self.assertEqual(self.calls.authorize(intent,proof),self.authority)
        self.assertIsNone(self.calls.authorize(intent,proof))
        proof=self.calls.issue(intent,self.native.meta)
        different=MessageIntent('different',self.target,intent.body_sha256)
        self.assertIsNone(self.calls.authorize(different,proof))

    def test_stdio_handles_native_framing_and_returns_content_free_protocol_errors(self):
        wire=io.BytesIO(b'{broken}\n'+json.dumps({'jsonrpc':'2.0','id':3,'method':'tools/list'}).encode()+b'\n')
        output=io.BytesIO()
        serve(self.server,wire,output)
        frames=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(frames[0]['error']['code'],-32700)
        self.assertTrue(frames[1]['result']['tools'])
        names=[tool['name'] for tool in frames[1]['result']['tools']]
        self.assertIn('bridge_reply',names)
        self.assertNotIn('bridge_owner_select',names)

    def test_uninitialized_server_and_oversize_rpc_do_not_dispatch(self):
        server=Server(self.core,self.calls)
        response=server.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':'bridge_send','arguments':{'operation_id':'x','recipient':self.target.reference,'text':'secret'}}})
        self.assertIn('error',response)
        out=io.BytesIO()
        serve(self.server,io.BytesIO(b'x'*(1024*1024+1)+b'\n'),out)
        self.assertEqual(json.loads(out.getvalue())['error']['code'],-32700)
        self.assertIsNone(self.outbox.receipt('x'))

    def test_proof_expiry_and_unverified_operation_reads_fail_closed(self):
        from optional.bridge.core.messages import MessageIntent
        import hashlib
        now=[100.0]
        calls=CallProofs(observe=self.source.observe,clock=lambda:now[0])
        intent=MessageIntent('scope',self.target,hashlib.sha256(b'text').hexdigest())
        proof=calls.issue(intent,self.native.meta)
        now[0]=105.0
        self.assertIsNone(calls.authorize(intent,proof))
        self.send()
        known=self.call('bridge_receipt',{'operation_id':'test-op'},meta={})
        missing=self.call('bridge_receipt',{'operation_id':'does-not-exist'},meta={})
        self.assertEqual(self.payload(known),self.payload(missing))
