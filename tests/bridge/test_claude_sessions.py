"""Real owned-process observations plus adversarial native registry records."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from optional.bridge.hosts.claude.sessions import ClaudeDirectory, NativeSessionError
from kitlib.proc.process import own_ctime

NATIVE_SOURCE = 'optional/bridge/hosts/claude/sessions.py'  # the selector's host-file dependency


class ClaudeDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-native-directory-')
        self.root = Path(self.temp.name)
        self.registry = self.root / 'sessions'
        self.registry.mkdir()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.record = {'pid':os.getpid(),'procStart':str(own_ctime()),'procStartFt':str(own_ctime()),
            'sessionId':'test-native-session','cwd':str(self.workspace),'peerProtocol':1,
            'entrypoint':'cli','messagingSocketPath':r'\\.\pipe\LOCAL\cc-msg-test','name':'test native chat'}
        self.write()
        self.directory = ClaudeDirectory(self.registry, Path(sys.executable),
            account_probe=lambda record, root: 'a' * 64)

    def tearDown(self):
        self.directory.close()
        self.temp.cleanup()

    def write(self):
        (self.registry / ('%d.json' % os.getpid())).write_text(json.dumps(self.record),encoding='utf-8')

    def test_owned_live_process_is_observed_without_launch_or_configuration_write(self):
        before = set(self.root.rglob('*'))
        sessions = self.directory.discover()
        self.assertEqual(len(sessions),1)
        self.assertEqual(sessions[0].kind,'terminal')
        self.assertEqual(sessions[0].native_session_id,'test-native-session')
        self.assertEqual(set(self.root.rglob('*')),before)
        self.assertTrue(self.directory.require_live(sessions[0]))

    def test_desktop_and_terminal_use_same_exact_identity_checks(self):
        self.record['entrypoint']='claude-desktop'
        self.write()
        self.assertEqual(self.directory.discover()[0].kind,'desktop')

    def test_process_replacement_or_account_drift_rejects_use(self):
        snapshot = self.directory.discover()[0]
        self.record['procStartFt']=str(own_ctime()+1)
        self.write()
        with self.assertRaises(NativeSessionError):
            self.directory.require_live(snapshot)
        self.record['procStartFt']=str(own_ctime())
        self.write()
        self.directory.account_probe=lambda record, root:'b'*64
        with self.assertRaises(NativeSessionError):
            self.directory.require_live(snapshot)

    def test_changed_workspace_and_native_id_reject_use(self):
        snapshot = self.directory.discover()[0]
        self.record['sessionId']='another-session'
        self.write()
        with self.assertRaises(NativeSessionError):
            self.directory.require_live(snapshot)

    def test_missing_account_or_unknown_protocol_never_looks_verified(self):
        self.directory.account_probe=None
        self.assertEqual(self.directory.discover(),())
        self.directory.account_probe=lambda record,root:'a'*64
        self.record['peerProtocol']=True
        self.write()
        self.assertEqual(self.directory.discover(),())

    def test_peer_key_is_process_bound_and_not_in_snapshot_repr(self):
        snapshot = self.directory.discover()[0]
        canonical = r'\\.\pipe\local\cc-msg-test'
        key = self.registry / ('%d.%s.key' % (os.getpid(),hashlib.sha256(canonical.encode()).hexdigest()))
        token='c'*32
        key.write_text(json.dumps({'peerToken':token,'procStartFt':str(own_ctime())}),encoding='utf-8')
        self.assertEqual(self.directory.peer_token(snapshot),token)
        self.assertNotIn(token,repr(snapshot))
        key.write_text(json.dumps({'token':token,'procStartFt':str(own_ctime())}),encoding='utf-8')
        with self.assertRaisesRegex(NativeSessionError,'external-peer-key-unavailable'):
            self.directory.peer_token(snapshot)

    def test_nonlocal_pipe_and_registry_pid_mismatch_are_not_candidates(self):
        self.record['messagingSocketPath']=r'\\other-host\pipe\cc-msg-test'
        self.write()
        self.assertEqual(self.directory.discover(),())
        self.record['messagingSocketPath']=r'\\.\pipe\LOCAL\cc-msg-test'
        self.record['pid']=True
        self.write()
        self.assertEqual(self.directory.discover(),())

    def test_unrelated_files_count_toward_bounded_directory_scan(self):
        from unittest.mock import patch
        for index in range(3):
            (self.registry / ('unrelated-%d.txt' % index)).write_text('fixture',encoding='utf-8')
        with patch('optional.bridge.hosts.claude.sessions.MAX_RECORDS',2):
            with self.assertRaisesRegex(NativeSessionError,'native-registry-limit'):
                self.directory.discover()
