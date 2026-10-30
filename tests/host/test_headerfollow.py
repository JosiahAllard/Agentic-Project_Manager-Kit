"""kitlib.host.headerfollow: a session whose header named an older plugin export is told the
current kit folder at a later tool event: at once when that folder is gone, else at most once an
hour (EVERY_S)."""
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from kitlib import pluginsource
from kitlib.host import headerfollow

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


if __name__ == '__main__':
    unittest.main()
