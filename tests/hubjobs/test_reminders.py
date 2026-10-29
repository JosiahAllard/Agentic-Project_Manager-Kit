import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

from kitlib.hubjobs import reminders
from kitlib.hubsite import jobs

TODAY = datetime.date(2026, 10, 29)


class Ctx(object):
    def __init__(self, fail=False):
        self.events, self.fail, self.stop = [], fail, None

    def event(self, word, detail=None):
        if self.fail:
            raise RuntimeError('push down')
        self.events.append((word, detail))


class RemindersJobTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, 'reminders.json')
        self.state = os.path.join(self.dir.name, reminders.STATE_FILE)

    def put(self, items):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(items, f)

    def job(self):
        return reminders.RemindersJob(self.path, today=lambda: TODAY)

    def test_due_pushed_and_noted_future_not(self):
        self.put([{'due': '2026-10-29', 'text': 'pay'}, {'due': '2026-11-01', 'text': 'later'}])
        c = Ctx()
        note = self.job()(c)
        self.assertEqual(c.events, [('due', '1')])
        self.assertEqual(note, '1 due, 1 new: pay')

    def test_none_due_pushes_nothing(self):
        self.put([{'due': '2026-11-01', 'text': 'later'}])
        c = Ctx()
        self.assertEqual(self.job()(c), 'none due')
        self.assertEqual(c.events, [])
        self.assertFalse(os.path.exists(self.state))

    def test_pushed_once_across_runs_and_restart(self):
        self.put([{'due': '2026-10-01', 'text': 'a'}, {'due': '2026-10-02', 'text': 'b'}])
        c = Ctx()
        self.job()(c)
        self.job()(c)           # a new instance: the restart
        self.assertEqual(c.events, [('due', '2')])
        self.put([{'due': '2026-10-01', 'text': 'a'}, {'due': '2026-10-02', 'text': 'b'},
                  {'due': '2026-10-03', 'text': 'c'}])
        self.assertIn('3 due, 1 new', self.job()(c))
        self.assertEqual(c.events, [('due', '2'), ('due', '1')])

    def test_state_written_after_push(self):
        self.put([{'due': '2026-10-01', 'text': 'a'}])
        with self.assertRaises(RuntimeError):
            self.job()(Ctx(fail=True))
        self.assertFalse(os.path.exists(self.state))    # not marked: the next run pushes again
        c = Ctx()
        self.job()(c)
        self.assertEqual(c.events, [('due', '1')])

    def test_state_write_failure_repeats_push(self):
        self.put([{'due': '2026-10-01', 'text': 'a'}])
        c = Ctx()
        with mock.patch.object(reminders.fsreplace, 'replace', side_effect=PermissionError('x')):
            with self.assertRaises(PermissionError):
                self.job()(c)
        self.assertEqual(os.listdir(self.dir.name), ['reminders.json'])     # no tmp left
        self.job()(c)
        self.assertEqual(c.events, [('due', '1'), ('due', '1')])

    def test_malformed_does_not_stop_others_then_fails(self):
        self.put([5, {'due': 'soon', 'text': 'x'}, {'due': '2026-10-01'},
                  {'due': '2026-10-01', 'text': 'good'}])
        c = Ctx()
        with self.assertRaisesRegex(ValueError, '3 malformed reminders'):
            self.job()(c)
        self.assertEqual(c.events, [('due', '1')])
        self.assertFalse(os.path.exists(self.state))    # not marked while an entry is malformed
        with self.assertRaises(ValueError):
            self.job()(c)
        self.assertEqual(c.events, [('due', '1'), ('due', '1')])    # handed over again
        self.put([{'due': '2026-10-01', 'text': 'good'}])           # the file is fixed
        self.job()(c)
        self.job()(c)
        self.assertEqual(len(c.events), 3)

    def test_removed_reminder_key_dropped(self):
        self.put([{'due': '2026-10-01', 'text': 'a'}, {'due': '2026-10-02', 'text': 'b'}])
        self.job()(Ctx())
        self.put([{'due': '2026-10-02', 'text': 'b'}])
        self.job()(Ctx())
        with open(self.state) as f:
            self.assertEqual(json.load(f), [reminders.key({'due': '2026-10-02', 'text': 'b'})])

    def test_unreadable_state_is_empty(self):
        self.put([{'due': '2026-10-01', 'text': 'a'}])
        with open(self.state, 'w') as f:
            f.write('{not json')
        c = Ctx()
        self.job()(c)
        self.assertEqual(c.events, [('due', '1')])

    def test_missing_and_bad_files_fail(self):
        with self.assertRaises(OSError):
            self.job()(Ctx())
        with open(self.path, 'w') as f:
            f.write('{}')
        with self.assertRaisesRegex(ValueError, 'not a JSON list'):
            self.job()(Ctx())
        with open(self.path, 'w') as f:
            f.write('nope')
        with self.assertRaises(ValueError):
            self.job()(Ctx())


class ThroughJobsTest(unittest.TestCase):
    """The real runner: ctx.event hands off and never reports delivery."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, 'reminders.json')
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump([{'due': '2026-10-01', 'text': 'a'}], f)

    def run_with(self, notify):
        job = reminders.RemindersJob(self.path, today=lambda: TODAY)
        runner = jobs.Jobs([jobs.Job('reminders', 300, job)], notify=notify)
        runner.run_now('reminders')
        return runner.status()['reminders']

    def marked(self):
        with open(os.path.join(self.dir.name, reminders.STATE_FILE)) as f:
            return json.load(f)

    def test_marked_after_handoff_with_no_push_worker(self):
        st = self.run_with(None)
        self.assertEqual((st['failures'], st['note']), (0, '1 due, 1 new: a'))
        self.assertEqual(len(self.marked()), 1)

    def test_marked_when_notify_raises(self):
        def boom(*a):
            raise OSError('down')
        st = self.run_with(boom)
        self.assertEqual(st['failures'], 0)
        self.assertEqual(len(self.marked()), 1)

    def test_notify_gets_the_due_event(self):
        seen = []
        self.run_with(lambda *a: seen.append(a))
        self.assertEqual(seen, [('reminders', 'due', '1')])


class MakeTest(unittest.TestCase):
    def make(self, cfg):
        with mock.patch('kitlib.config.user.readable_file', return_value='cfg'), \
                mock.patch('kitlib.pmstart.load_config', return_value=cfg):
            return reminders.make(mock.Mock(home=None))

    def test_none_without_reminders_file(self):
        self.assertIsNone(self.make(None))
        self.assertIsNone(self.make({'memory_dir': 'x'}))

    def test_job_with_file(self):
        j = self.make({'reminders_file': 'r.json'})
        self.assertIsInstance(j, jobs.Job)
        self.assertEqual((j.name, j.every_s), ('reminders', reminders.EVERY_S))


if __name__ == '__main__':
    unittest.main()
