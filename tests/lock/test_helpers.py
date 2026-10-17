"""The lock fixtures isolate inherited host identity while permitting explicit test inputs."""
import os
import unittest
from unittest import mock

from kitlib.host import registry
from tests.lock import helpers


class CleanEnvironment(unittest.TestCase):
    def test_every_registered_host_session_and_lock_marker_is_removed(self):
        sessions = registry.union('session_env')
        self.assertTrue(sessions)
        source = {name: 'inherited-session' for name in sessions}
        source.update(CLAUDE_PID='123', MACHINE_LOCK_RUN='inherited-run',
                      MACHINE_LOCK_OTHER='inherited-marker', UNRELATED_FIXTURE='keep')
        before = dict(source)
        with mock.patch.object(os, 'environ', source):
            cleaned = helpers.clean_env()
        self.assertEqual(cleaned, {'UNRELATED_FIXTURE': 'keep'})
        self.assertEqual(source, before)

    def test_windows_environment_names_are_matched_without_case(self):
        source = {name.lower(): 'inherited-session' for name in registry.union('session_env')}
        source.update(claude_pid='123', machine_lock_run='inherited-run', Mixed_Unrelated='keep')
        with mock.patch.object(os, 'environ', source):
            self.assertEqual(helpers.clean_env(), {'Mixed_Unrelated': 'keep'})

    def test_explicit_overrides_can_restore_session_and_lock_values(self):
        session = registry.union('session_env')[0]
        source = {session: 'inherited-session', 'MACHINE_LOCK_RUN': 'inherited-run',
                  'UNRELATED_FIXTURE': 'before'}
        overrides = {session: 'explicit-session', 'MACHINE_LOCK_RUN': 'explicit-run',
                     'UNRELATED_FIXTURE': 'after'}
        with mock.patch.object(os, 'environ', source):
            cleaned = helpers.clean_env(**overrides)
        self.assertEqual(cleaned, overrides)
        self.assertEqual(source[session], 'inherited-session')
