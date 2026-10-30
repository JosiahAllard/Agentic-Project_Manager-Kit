"""kitlib.host.headerfollow: a session whose header named an older plugin export is told the
current kit folder at a later tool event: at once when that folder is gone, else at most once an
hour (EVERY_S)."""
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from hosts.claude.adapter.host import ADAPTER
from kitlib import agentstart, pluginsource
from kitlib.config import user
from kitlib.host import headerfollow, hookmain
from tests import gitfixture
from tests.home import pm_repo, temp_home

OLD, NEW, NEWER = '766c4424cb37', '085253eeaa90', '4214494f1661'


class Follow(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='headerfollow-')
        self.addCleanup(shutil.rmtree, self.root, True)
        for name in (OLD, NEW, NEWER):
            os.makedirs(os.path.join(self.root, name))
            with open(os.path.join(self.root, name, pluginsource.MARKER), 'w') as fh:
                fh.write('{}')
        self.payload = {'session_id': 'abc-123'}

    def kit(self, name):
        return os.path.join(self.root, name)

    def record(self, k='abc-123-main'):
        with open(os.path.join(self.root, headerfollow.HEADERS, k), encoding='utf-8') as fh:
            return fh.read()

    def later(self):
        """A time when the record just written is EVERY_S old."""
        return time.time() + headerfollow.EVERY_S + 1

    def test_a_session_started_before_a_refresh_is_told_the_new_folder_once(self):
        self.assertTrue(headerfollow.note(self.kit(OLD), self.payload, event='SessionStart'))
        line = headerfollow.line('C:/kit', self.payload, current=self.kit(NEW), now=self.later())
        self.assertIn(self.kit(NEW).replace('\\', '/'), line)
        self.assertIsNone(headerfollow.line('C:/kit', self.payload, current=self.kit(NEW), now=self.later()))
        self.assertEqual(self.record(), os.path.normpath(self.kit(NEW)))
        self.assertIn(NEWER, headerfollow.line('C:/kit', self.payload, current=self.kit(NEWER), now=self.later()))

    def age(self, minutes, k='abc-123-main'):
        """Date the record `minutes` back, as if its folder was named to the session then."""
        then = time.time() - minutes * 60
        os.utime(os.path.join(self.root, headerfollow.HEADERS, k), (then, then))

    def test_while_the_old_folder_is_there_the_line_waits_an_hour_from_the_last_one(self):
        headerfollow.note(self.kit(OLD), self.payload, event='SessionStart')
        self.age(59)
        self.assertIsNone(headerfollow.line('C:/kit', self.payload, current=self.kit(NEW)))
        self.assertIsNone(headerfollow.line('C:/kit', self.payload, current=self.kit(NEWER)))
        self.assertEqual(self.record(), os.path.normpath(self.kit(OLD)))     # not spent while held back
        self.age(61)
        self.assertIn(NEWER, headerfollow.line('C:/kit', self.payload, current=self.kit(NEWER)))
        # the line just given starts the next hour: a refresh right after it is held back again
        self.assertIsNone(headerfollow.line('C:/kit', self.payload, current=self.kit(NEW)))
        self.age(61)
        self.assertIn(NEW, headerfollow.line('C:/kit', self.payload, current=self.kit(NEW)))

    def test_a_session_whose_old_folder_is_gone_is_told_at_once(self):
        headerfollow.note(self.kit(OLD), self.payload, event='SessionStart')
        os.rename(self.kit(OLD), self.kit(OLD) + pluginsource.CLAIM + '42')     # prune's claim
        line = headerfollow.line('C:/kit', self.payload, current=self.kit(NEW))
        self.assertIn(NEW, line)
        self.assertEqual(self.record(), os.path.normpath(self.kit(NEW)))

    def test_write_false_gives_the_line_and_leaves_the_record_for_the_next_call(self):
        headerfollow.note(self.kit(OLD), self.payload, event='SessionStart')
        first = headerfollow.line('C:/kit', self.payload, current=self.kit(NEW), write=False, now=self.later())
        self.assertEqual(self.record(), os.path.normpath(self.kit(OLD)))
        self.assertEqual(headerfollow.line('C:/kit', self.payload, current=self.kit(NEW), now=self.later()), first)
        self.assertEqual(self.record(), os.path.normpath(self.kit(NEW)))

    def test_a_session_whose_header_named_the_current_folder_is_told_nothing(self):
        headerfollow.note(self.kit(NEW), self.payload, event='SessionStart')
        self.assertIsNone(headerfollow.line('C:/kit', self.payload, current=self.kit(NEW)))

    def test_each_agent_has_its_own_record(self):
        headerfollow.note(self.kit(OLD), self.payload, event='SessionStart')
        headerfollow.note(self.kit(NEW), self.payload, agent='a1', event='SubagentStart')
        self.assertIsNone(headerfollow.line('C:/kit', self.payload, agent='a1', current=self.kit(NEW)))
        self.assertIsNotNone(headerfollow.line('C:/kit', self.payload, current=self.kit(NEW), now=self.later()))

    def test_no_record_no_line(self):
        self.assertIsNone(headerfollow.line('C:/kit', self.payload, current=self.kit(NEW)))
        self.assertIsNone(headerfollow.line('C:/kit', {}, current=self.kit(NEW)))

    def test_a_header_from_a_kit_checkout_or_without_a_plain_session_is_not_noted(self):
        checkout = tempfile.mkdtemp(prefix='headerfollow-kit-')
        self.addCleanup(shutil.rmtree, checkout, True)
        self.assertFalse(headerfollow.note(checkout, self.payload))
        for bad in ({}, {'session_id': '../x'}, {'session_id': 7}, 'not a dict'):
            self.assertFalse(headerfollow.note(self.kit(OLD), bad))
        self.assertFalse(headerfollow.note(self.kit(OLD), self.payload, agent='a/b'))
        self.assertFalse(os.path.isdir(os.path.join(self.root, headerfollow.HEADERS)))

    def test_a_session_start_drops_old_records_and_a_subagent_start_does_not(self):
        headerfollow.note(self.kit(OLD), {'session_id': 'stale'})
        stale = os.path.join(self.root, headerfollow.HEADERS, 'stale-main')
        old = time.time() - (headerfollow.MAX_AGE_DAYS + 1) * 86400
        os.utime(stale, (old, old))
        headerfollow.note(self.kit(NEW), self.payload, agent='a1', event='SubagentStart')
        self.assertTrue(os.path.isfile(stale))
        headerfollow.note(self.kit(NEW), self.payload, event='SessionStart')
        self.assertFalse(os.path.isfile(stale))
        self.assertTrue(os.path.isfile(os.path.join(self.root, headerfollow.HEADERS, 'abc-123-a1')))

    def test_the_folder_comes_from_the_guards_advice_and_failures_are_silent(self):
        headerfollow.note(self.kit(OLD), self.payload, event='SessionStart')
        with mock.patch('kitlib.guard.rules.advice_kit', return_value='C:/kit'):
            self.assertIsNone(headerfollow.line('C:/kit', self.payload, environ={}))   # not an export
        with mock.patch('kitlib.guard.rules.advice_kit', side_effect=RuntimeError('boom')):
            self.assertIsNone(headerfollow.line('C:/kit', self.payload, environ={}))
        with mock.patch('kitlib.guard.rules.advice_kit', return_value=self.kit(NEW)) as advice:
            self.assertIn(NEW, headerfollow.line('C:/kit', self.payload, environ={'X': '1'}, now=self.later()))
        ctx = advice.call_args[0][0]
        self.assertEqual((ctx.kit, ctx.environ), ('C:/kit', {'X': '1'}))

    def test_the_machine_config_comes_from_the_hook_environments_home_only(self):
        self.assertIn('error', headerfollow._Ctx('C:/kit', {}).user())
        with mock.patch('kitlib.config.user.user_config', return_value={'plugin': None}) as cfg:
            self.assertEqual(headerfollow._Ctx('C:/kit', {'USERPROFILE': self.root}).user(), {'plugin': None})
        cfg.assert_called_once_with(self.root)


class SubagentStart(unittest.TestCase):
    """A session on an old export starts a subagent: the real hook path, with the real advice_kit
    over a marketplace on disk that names a newer export."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='headerfollow-start-')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.checkout = os.path.join(self.base, 'kit')
        os.makedirs(os.path.join(self.checkout, '.git'))
        self.plugin = os.path.join(self.base, 'plugin')
        self.old, self.new = (os.path.join(self.plugin, n) for n in (OLD, NEW))
        for folder in (self.old, self.new):
            os.makedirs(folder)
            self.write(os.path.join(folder, pluginsource.MARKER), {'checkout': self.checkout})
            self.write(os.path.join(folder, 'kit.json'), {'version': '1.0.0'})
        os.makedirs(os.path.join(self.plugin, '.claude-plugin'))
        self.write(os.path.join(self.plugin, '.claude-plugin', 'marketplace.json'),
                   {'name': 'pm-kit', 'plugins': [{'name': 'pm-kit', 'source': './%s' % NEW}]})
        home, pm = temp_home(self), pm_repo(self, record=False)
        os.makedirs(os.path.join(pm, user.DIR_NAME))
        self.write(os.path.join(pm, user.DIR_NAME, user.FILE_NAME), {'plugin': {'export_dir': self.plugin}})
        self.env = {'USERPROFILE': home}
        self.repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(self.repo.cleanup)
        # a key a later kit knows and the code the hook runs does not
        self.write(os.path.join(self.repo.root, '.kit.json'), {'landing': {'key_of_a_later_kit': 1}})
        self.payload = {'hook_event_name': 'SubagentStart', 'cwd': self.repo.root, 'session_id': 's1',
                        'agent_id': 'a1', 'agent_type': 'pm-kit:worker'}

    @staticmethod
    def write(path, data):
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh)

    def start(self, event='SubagentStart', kit=None):
        out = io.StringIO()
        # a fake export holds no roles: the block's own test is tests/host/test_header.py
        with mock.patch.object(agentstart, 'is_dispatched', return_value=True):
            code = hookmain.dispatch(ADAPTER, event, kit or self.old, json.dumps(self.payload), out,
                                     environ=self.env)
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())['hookSpecificOutput']['additionalContext']

    @staticmethod
    def fwd(path):
        return path.replace(os.sep, '/')

    def test_it_names_the_latest_export_when_the_session_s_folder_cannot_read_the_config(self):
        text = self.start()
        self.assertIn('unknown key(s) key_of_a_later_kit', text)      # the running code cannot read it
        self.assertTrue(text.startswith('PM kit header. The kit folder is %s.' % self.fwd(self.new)), text)
        self.assertIn('%s/tools/new_worktree.py' % self.fwd(self.new), text)
        self.assertNotIn(OLD, text)

    def test_the_record_names_the_folder_the_header_named(self):
        self.start()
        with open(os.path.join(self.plugin, headerfollow.HEADERS, 's1-a1'), encoding='utf-8') as fh:
            self.assertEqual(os.path.normcase(fh.read()), os.path.normcase(os.path.normpath(self.new)))
        later = time.time() + headerfollow.EVERY_S + 1
        self.assertIsNone(headerfollow.line(self.old, self.payload, 'a1', self.env, now=later))

    def test_the_session_s_own_header_keeps_its_folder(self):
        self.assertIn(self.fwd(self.old), self.start('SessionStart'))

    def test_a_kit_checkout_stays_on_itself(self):
        self.assertEqual(headerfollow.start_kit(self.checkout, self.env), self.checkout)

    def test_a_failure_keeps_the_session_s_folder_and_the_start(self):
        with mock.patch('kitlib.guard.rules.advice_kit', side_effect=RuntimeError('config')):
            self.assertEqual(headerfollow.start_kit(self.old, self.env), self.old)
            self.assertIn(self.fwd(self.old), self.start())
        with mock.patch.object(headerfollow, 'start_kit', side_effect=RuntimeError('boom')):
            self.assertIn(self.fwd(self.old), self.start())

    def test_a_current_folder_that_is_no_export_keeps_the_session_s_folder(self):
        with mock.patch('kitlib.guard.rules.advice_kit', return_value=self.checkout):
            self.assertEqual(headerfollow.start_kit(self.old, self.env), self.old)


if __name__ == '__main__':
    unittest.main()
