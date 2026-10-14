"""Distinct owned Windows executable images share one trusted Claude registry."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
import uuid

import tests.bridge.test_claude_runtime as fixture
from optional.bridge.hosts.claude.runtime import ClaudeSources
from optional.bridge.hosts.claude.sessions import ClaudeDirectory,NativeSessionError
from optional.bridge.core.messages import DeliveryError
from optional.bridge.win.frontend import FrontendInspector,NativeFrontend
from optional.bridge.win.service import BrokerService
from kitlib.proc.process import own_ctime,proc_ctime

DIRECTORY_SOURCE='optional/bridge/hosts/claude/sessions.py'
RUNTIME_SOURCE='optional/bridge/hosts/claude/runtime.py'
TERMINAL_SOURCE='optional/bridge/hosts/claude/terminal.py'


class ClaudeImagesTests(unittest.TestCase):
    setUp=fixture.ClaudeRuntimeTests.setUp
    tearDown=fixture.ClaudeRuntimeTests.tearDown
    write=fixture.ClaudeRuntimeTests.write

    def replace_provider(self):
        self.sources.close()
        self.python=Path(sys.executable).resolve()
        self.pythonw=self.python.with_name('pythonw.exe').resolve(strict=True)
        # Reverse order proves the actual native host is never the first default.
        self.sources=ClaudeSources(self.controller,self.registry,(self.pythonw,self.python),'owned-host',
            self.profile,self.terminal_account,self.projects,self.marker,'kit')

    def host(self):
        child_argv=[sys.executable,'-I','-c','import sys; sys.stdin.read()']
        code=("import sys,subprocess,json; p=subprocess.Popen("+repr(child_argv)+",stdin=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW); "
            "print(json.dumps({'frontend':p.pid}),flush=True); "
            "event=json.loads(sys.stdin.readline()); "
            "hook=subprocess.Popen("+repr(self.sources.hook_argv)+",stdin=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW); "
            "hook.communicate(json.dumps(event).encode(),timeout=8); print('hook-done',flush=True); "
            "sys.stdin.readline(); p.stdin.close(); p.wait(timeout=3)")
        child=self.job.start([str(self.pythonw),'-I','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
        self.children.append(child)
        frontend_pid=json.loads(child.stdout.readline())['frontend']
        session=str(uuid.uuid4())
        record={'pid':child.pid,'procStartFt':str(proc_ctime(child.pid)),'sessionId':session,
            'cwd':str(self.root),'peerProtocol':1,'entrypoint':'cli',
            'messagingSocketPath':r'\\.\pipe\LOCAL\cc-msg-owned-second-image'}
        (self.registry/('%d.json'%child.pid)).write_text(json.dumps(record),encoding='utf-8')
        transcript=self.transcripts/(session+'.jsonl');transcript.write_text('{}\n',encoding='utf-8')
        native=NativeFrontend(frontend_pid,proc_ctime(frontend_pid),child.pid,proc_ctime(child.pid),'claude',self.pythonw)
        return child,native,session,transcript

    def test_two_real_images_are_observed_and_retained_under_one_registry(self):
        self.replace_provider()
        child,native,session,_=self.host()
        sessions={item.pid:item for item in self.sources.directory.discover()}
        self.assertEqual(set(sessions),{os.getpid(),child.pid})
        self.assertEqual(sessions[os.getpid()].executable,self.python)
        self.assertEqual(sessions[child.pid].executable,self.pythonw)
        self.assertEqual(sessions[os.getpid()].kind,'desktop')
        self.assertEqual(sessions[child.pid].kind,'terminal')
        self.assertNotEqual(sessions[os.getpid()].account_ref,sessions[child.pid].account_ref)
        self.assertEqual(sessions[child.pid].native_session_id,session)
        self.assertEqual(self.sources.directory.executables,(self.pythonw,self.python))
        with self.assertRaises(AttributeError):self.sources.directory.executables=(self.python,)
        with self.assertRaises(NativeSessionError):_=self.sources.directory.executable

    def test_singleton_interface_remains_compatible_without_admitting_second_image(self):
        self.replace_provider();child,_,_,_=self.host()
        directory=ClaudeDirectory(self.registry,self.python,account_probe=self.sources._account)
        try:
            self.assertEqual(directory.executables,(self.python,))
            self.assertEqual(directory.executable,self.python)
            self.assertEqual([item.pid for item in directory.discover()],[os.getpid()])
        finally:directory.close()

    def test_actual_image_is_part_of_snapshot_liveness_even_if_both_are_allowed(self):
        self.replace_provider();child,_,_,_=self.host()
        snapshot=next(item for item in self.sources.directory.discover() if item.pid==child.pid)
        with self.assertRaises(NativeSessionError):
            self.sources.directory.require_live(replace(snapshot,executable=self.python))
        # A changed observed image cannot borrow membership of another allowed one.
        with patch('optional.bridge.hosts.claude.sessions.process_image',return_value=self.python):
            with self.assertRaises(NativeSessionError):self.sources.directory.require_live(snapshot)

    def test_sources_accept_each_exact_host_image_and_reject_wrong_member_claim(self):
        self.replace_provider();child,native,_,_=self.host()
        self.assertEqual(self.sources.refresh()[0].identity.session_id,self.task)
        source=self.sources(native)
        self.assertTrue(callable(source.prepare))
        with self.assertRaises(DeliveryError):self.sources(replace(native,host_executable=self.python))
        self.assertFalse(self.controller.core.adapters)

    def test_real_direct_hook_under_second_native_image_enrolls_correct_cli(self):
        self.replace_provider();child,native,session,transcript=self.host()
        self.sources(native)
        inspector=FrontendInspector(self.kit,self.marker,sys.executable,{'claude':sys.executable})
        self.service=BrokerService(self.controller,inspector,hook_sink=self.sources.hook_sink)
        self.service_thread=threading.Thread(target=self.service.run,args=(self.stop,),daemon=True)
        self.service_thread.start()
        event={'session_id':session,'prompt_id':str(uuid.uuid4()),'cwd':str(self.root),
            'transcript_path':str(transcript),'permission_mode':'auto','hook_event_name':'PreToolUse',
            'tool_name':'mcp__kit__bridge_connections','tool_input':{},'tool_use_id':'toolu_second_image',
            'mcp_server':{'name':'kit','source':'plugin'}}
        child.stdin.write(json.dumps(event).encode()+b'\n');child.stdin.flush()
        self.assertEqual(child.stdout.readline(),b'hook-done\r\n')
        binding=next(item for item in self.controller.pairs.registered() if item.identity.native_session_id==session)
        self.assertEqual(self.sources.resolve(binding).executable,self.pythonw)
        self.assertEqual(self.sources.sender_mode(binding),'prompting')
        self.assertEqual(self.controller.pairs.owner_routes(),())

    def test_duplicate_relative_empty_or_oversized_allowed_image_tuple_is_rejected(self):
        self.replace_provider()
        for images in ((),(self.python,self.python),('python.exe',),(self.python,)*9):
            with self.subTest(images=images):
                with self.assertRaises(NativeSessionError):
                    ClaudeDirectory(self.registry,images,account_probe=self.sources._account)
