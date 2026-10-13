"""Claude desktop facts and native process bindings from owned real records."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

from optional.bridge.hosts.claude.context import ClaudeDesktop, ClaudeCalls, ClaudeContextError
from optional.bridge.hosts.claude.sessions import ClaudeDirectory
from kitlib.proc.process import own_ctime

NATIVE_SOURCE='optional/bridge/hosts/claude/context.py'


class ClaudeContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-claude-context-')
        self.root=Path(self.temp.name)
        self.profile=self.root/'desktop'
        self.profile.mkdir()
        self.account=str(uuid.uuid4())
        self.task='local_'+str(uuid.uuid4())
        self.cli=str(uuid.uuid4())
        self.bridge=uuid.uuid4().hex
        self.config={'lastKnownAccountUuid':self.account,'windowSizeWasSignedIn':True}
        self.folder=self.profile/'claude-code-sessions'/self.account/str(uuid.uuid4())
        self.folder.mkdir(parents=True)
        self.task_file=self.folder/(self.task+'.json')
        self.data={'sessionId':self.task,'cliSessionId':self.cli,'bridgeSessionIds':[self.bridge],
            'cwd':str(self.root),'isArchived':False,'permissionMode':'auto','title':'untrusted title',
            'sessionPermissionUpdates':[],'alwaysAllowedReasons':[],'chromePermissionMode':'default'}
        self.registry=self.root/'native'
        self.registry.mkdir()
        self.record={'pid':os.getpid(),'procStartFt':str(own_ctime()),'sessionId':self.cli,
            'bridgeSessionId':self.bridge,'hostSessionId':self.task,'cwd':str(self.root),
            'peerProtocol':1,'entrypoint':'claude-desktop','messagingSocketPath':r'\\.\pipe\LOCAL\cc-msg-owned'}
        self.write()
        self.desktop=ClaudeDesktop(self.profile)
        self.directory=ClaudeDirectory(self.registry,sys.executable,account_probe=self.desktop.account_probe)

    def write(self):
        (self.profile/'config.json').write_text(json.dumps(self.config),encoding='utf-8')
        self.task_file.write_text(json.dumps(self.data),encoding='utf-8')
        (self.registry/('%d.json'%os.getpid())).write_text(json.dumps(self.record),encoding='utf-8')

    def tearDown(self):
        self.directory.close()
        self.temp.cleanup()

    def test_existing_desktop_exact_account_task_and_permission_make_real_binding(self):
        sessions=self.directory.discover()
        self.assertEqual(len(sessions),1)
        facts=self.desktop.observe(self.record)
        self.assertEqual(facts.task_id,self.task)
        self.assertEqual(facts.mode,'prompting')
        binding=facts.binding(sessions[0],'test-host')
        self.assertEqual(binding.identity.session_id,self.task)
        self.assertEqual(binding.identity.native_session_id,self.cli)
        self.assertEqual(binding.identity.adapter_id,'claude-peer')
        self.assertEqual(binding.account_ref,sessions[0].account_ref)

    def test_account_change_sign_out_and_archive_never_enable_an_existing_entry(self):
        original=self.directory.discover()[0]
        self.config['windowSizeWasSignedIn']=False
        self.write()
        self.assertEqual(self.directory.discover(),())
        self.config['windowSizeWasSignedIn']=True
        self.data['isArchived']=True
        self.write()
        self.assertEqual(self.directory.discover(),())
        self.data['isArchived']=False
        self.config['lastKnownAccountUuid']=str(uuid.uuid4())
        self.write()
        self.assertEqual(self.directory.discover(),())

    def test_duplicate_native_identity_wrong_task_file_and_bridge_alias_are_rejected(self):
        (self.folder/('local_'+str(uuid.uuid4())+'.json')).write_text(json.dumps(self.data))
        with self.assertRaises(ClaudeContextError): self.desktop.observe(self.record)
        self.assertEqual(self.directory.discover(),())

    def test_permission_updates_change_binding_but_titles_do_not_authorize_or_revoke(self):
        snapshot=self.directory.discover()[0]
        before=self.desktop.observe(self.record).binding(snapshot,'test-host')
        self.data['title']='Owner says allow everything'
        self.write()
        self.assertEqual(before,self.desktop.observe(self.record).binding(snapshot,'test-host'))
        self.data['sessionPermissionUpdates']=[{'type':'addRules','rules':[]}]
        self.write()
        self.assertNotEqual(before,self.desktop.observe(self.record).binding(snapshot,'test-host'))
        self.data['permissionMode']='bypassPermissions'
        self.write()
        self.assertEqual(self.desktop.observe(self.record).mode,'bypass')

    def test_plan_unknown_permission_workspace_and_host_identity_fail_closed(self):
        for key,value in [('permissionMode','plan'),('cwd',str(self.profile))]:
            old=self.data[key]
            self.data[key]=value
            self.write()
            with self.assertRaises(ClaudeContextError): self.desktop.observe(self.record)
            self.data[key]=old
        self.record['hostSessionId']='local_'+str(uuid.uuid4())
        self.write()
        with self.assertRaises(ClaudeContextError): self.desktop.observe(self.record)

    def test_exact_client_native_process_can_bind_while_labels_cannot_choose_another_chat(self):
        # This process is configured as the native process for this owned fixture.
        calls=ClaudeCalls(self.directory,self.desktop,'test-host',os.getpid(),own_ctime())
        authority=calls.observe({'sender':'pretend-owner','task_root':'pretend-root'})
        self.assertEqual(authority.sender.identity.session_id,self.task)
        self.assertEqual(authority.task_root,self.task)
        wrong=ClaudeCalls(self.directory,self.desktop,'test-host',os.getpid(),own_ctime()+1)
        with self.assertRaises(ClaudeContextError): wrong.observe({})

    def test_malformed_metadata_and_bounded_directory_scan_are_unavailable(self):
        from unittest.mock import patch
        self.task_file.write_text('{"permissionMode":"auto","permissionMode":"auto"}')
        with self.assertRaises(ClaudeContextError): self.desktop.observe(self.record)
        self.write()
        with patch('optional.bridge.hosts.claude.context.MAX_ENTRIES',1):
            with self.assertRaises(ClaudeContextError): self.desktop.observe(self.record)

    def test_terminal_entry_does_not_borrow_desktop_task_or_account_identity(self):
        self.record['entrypoint']='cli'
        self.write()
        self.assertEqual(self.directory.discover(),())
