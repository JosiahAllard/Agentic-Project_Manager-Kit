"""A launched session's peer pipe: the registry read, the liveness check, pipes.json written on
start and dropped on stop (docs/launcher.md "Pipe addresses")."""
import json
import os
import unittest

from kitlib.launcher import pipes, start, stop
from kitlib.launcher import settings as S
from tests.launcher.fakes import NOW, FakeHost, Home, session
from tests.launcher.test_lookup_stop import windowed

NOW_MS = pipes.launch_ms(NOW)


def rec(pid, name, started=NOW_MS + 500, start_key='procStart', start_val=None, **kw):
    out = {'pid': pid, 'sessionId': 'sid-%d' % pid, 'startedAt': started, 'entrypoint': 'cli',
           'messagingSocketPath': r'\\.\pipe\cc-msg-%d' % pid, 'name': name}
    out[start_key] = str(5000 + pid) if start_val is None else start_val
    out.update(kw)
    return out


class Ctime(object):
    """A fake process table: pid -> creation time; a pid listed in `fails` raises OSError."""

    def __init__(self, table=None, fails=()):
        self.table, self.fails, self.asked = dict(table or {}), set(fails), []

    def __call__(self, pid):
        self.asked.append(pid)
        if pid in self.fails:
            raise OSError('access denied')
        return self.table.get(pid)


class Registry(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)
        self.reg = os.path.join(self.h.root, '.claude', pipes.REGISTRY)
        os.makedirs(self.reg)

    def put(self, r, folder=None, fname=None):
        with open(os.path.join(folder or self.reg, fname or '%d.json' % r['pid']), 'w', encoding='utf-8') as f:
            json.dump(r, f)

    def test_proc_start_takes_either_key_and_only_a_whole_number(self):
        self.assertEqual(pipes.proc_start({'procStart': '123'}), '123')
        self.assertEqual(pipes.proc_start({'procStartFt': 456}), '456')
        self.assertEqual(pipes.proc_start({'procStart': 'x', 'procStartFt': '789'}), '789')
        self.assertIsNone(pipes.proc_start({'procStart': True}))
        self.assertIsNone(pipes.proc_start({'procStart': '12a'}))
        self.assertIsNone(pipes.proc_start({'procStart': 1.5}))
        self.assertIsNone(pipes.proc_start({}))

    def test_a_record_is_live_only_while_its_pid_runs_with_the_same_creation_time(self):
        r = rec(7, 'x')
        self.assertTrue(pipes.is_live(r, Ctime({7: 5007})))
        self.assertTrue(pipes.is_live(rec(7, 'x', start_key='procStartFt'), Ctime({7: 5007})))
        self.assertFalse(pipes.is_live(r, Ctime({})), 'a dead pid')
        self.assertFalse(pipes.is_live(r, Ctime({7: 9999})), 'a reused pid')
        self.assertFalse(pipes.is_live(r, Ctime({7: 5007}, fails={7})), 'an unreadable process')
        self.assertFalse(pipes.is_live(dict(r, pid=True), Ctime({7: 5007})))
        self.assertFalse(pipes.is_live(dict(r, pid=0), Ctime({0: 5007})))
        self.assertFalse(pipes.is_live(dict(r, procStart=None), Ctime({7: 5007})))

    def test_find_takes_the_newest_live_record_of_the_name_started_after_the_launch(self):
        self.put(rec(1, 'x-a1', started=NOW_MS + 100))
        self.put(rec(2, 'x-a1', started=NOW_MS + 900))          # newest, but dead
        self.put(rec(3, 'x-a1', started=NOW_MS + 400))
        self.put(rec(4, 'x-a1', started=NOW_MS - 1))            # started before the launch
        self.put(rec(5, 'other', started=NOW_MS + 950))
        self.put({'pid': 6}, fname='notes.json')                # not <digits>.json
        with open(os.path.join(self.reg, '8.json'), 'w') as f:
            f.write('not json')
        ct = Ctime({1: 5001, 3: 5003, 4: 5004, 5: 5005})
        got = pipes.find(pipes.registry_dirs(self.h.settings), 'x-a1', NOW_MS, ct)
        self.assertEqual(got['pid'], 3)
        self.assertIsNone(pipes.find([self.reg], 'x-a1', NOW_MS + 1000, ct))
        self.assertIsNone(pipes.find([self.reg], 'nobody', NOW_MS, ct))

    def test_registry_dirs_lists_each_existing_folder_once(self):
        a1 = os.path.join(self.h.accounts['1'][0], pipes.REGISTRY)
        os.makedirs(a1)                                         # account 2 has no registry folder
        org = os.path.join(self.h.root, 'org9')
        os.makedirs(os.path.join(org, pipes.REGISTRY))
        obj = dict(self.h.obj, orgs={'9': org},
                   accounts=dict(self.h.accounts, **{'3': [self.h.accounts['1'][0]]}))
        got = pipes.registry_dirs(S.Settings(obj, self.h.root))
        self.assertEqual(got, [self.reg, a1, os.path.join(org, pipes.REGISTRY)])

    def test_record_writes_the_entry_without_any_token(self):
        self.put(rec(3, 'x-a1', peerToken='s' * 32))
        out = pipes.record(self.h.settings, 'x-a1', NOW_MS, Ctime({3: 5003}), self.fail, lambda: NOW)
        self.assertIsNone(out)
        data = self.h.read_state(pipes.PIPES)
        self.assertEqual(data, {'x-a1': {'pid': 3, 'session_id': 'sid-3', 'pipe': r'\\.\pipe\cc-msg-3',
                                         'proc_start': '5003', 'entrypoint': 'cli',
                                         'recorded_at': '2026-10-10T12:00:00+00:00'}})
        with open(os.path.join(self.h.state, pipes.PIPES), encoding='utf-8') as f:
            self.assertNotIn('s' * 32, f.read())
        self.assertEqual(pipes.read(self.h.settings), data)

    def test_record_keeps_other_entries_and_polls_until_the_record_appears(self):
        self.h.write_state(pipes.PIPES, {'y-a2': {'pid': 9}})
        slept = []

        def sleep(s):
            slept.append(s)
            if len(slept) == 2:
                self.put(rec(3, 'x-a1'))
        out = pipes.record(self.h.settings, 'x-a1', NOW_MS, Ctime({3: 5003}), sleep, lambda: NOW, tries=5)
        self.assertIsNone(out)
        self.assertEqual(slept, [pipes.POLL_S, pipes.POLL_S])
        self.assertEqual(sorted(self.h.read_state(pipes.PIPES)), ['x-a1', 'y-a2'])

    def test_record_gives_up_after_its_tries_with_one_line(self):
        slept = []
        out = pipes.record(self.h.settings, 'x-a1', NOW_MS, Ctime(), slept.append, lambda: NOW, tries=3)
        self.assertEqual(out, pipes.NOT_YET)
        self.assertEqual(slept, [pipes.POLL_S, pipes.POLL_S])
        self.assertFalse(os.path.exists(os.path.join(self.h.state, pipes.PIPES)))

    def test_without_a_state_folder_nothing_is_polled_or_written(self):
        s = S.Settings(dict(self.h.obj, state_dir=None), self.h.root)
        self.assertIsNone(pipes.record(s, 'x-a1', NOW_MS, self.fail, self.fail, self.fail))
        self.assertFalse(pipes.remove(s, 'x-a1'))
        self.assertEqual(pipes.read(s), {})

    def test_remove_drops_only_that_entry(self):
        self.assertFalse(pipes.remove(self.h.settings, 'x-a1'), 'no file')
        self.h.write_state(pipes.PIPES, {'x-a1': {'pid': 3}, 'y-a2': {'pid': 9}})
        self.assertTrue(pipes.remove(self.h.settings, 'x-a1'))
        self.assertFalse(pipes.remove(self.h.settings, 'x-a1'), 'already gone')
        self.assertEqual(self.h.read_state(pipes.PIPES), {'y-a2': {'pid': 9}})


class Launcher(unittest.TestCase):
    def setUp(self):
        self.h = Home(self, repos=('alpha',))

    def test_start_records_the_pipe_of_a_session_started_after_the_window_opens(self):
        host = FakeHost(self.h.root)
        out = start.start(self.h.settings, host, repo='alpha', account='1')
        self.assertEqual(len(host.opened), 1)
        self.assertEqual(len(host.piped), 1)
        name, after_ms = host.piped[0]
        self.assertEqual(after_ms, NOW_MS)
        self.assertIn('Remote Control name: %s' % name, out[-1])

    def test_a_failing_record_still_reads_as_a_started_session(self):
        host = FakeHost(self.h.root)

        def broken(settings, name, after_ms):
            raise OSError('registry unreadable')
        host.record_pipe = broken
        out = start.start(self.h.settings, host, repo='alpha', account='1')
        self.assertEqual(len(host.opened), 1)
        self.assertIn('Started ', out[-2])
        self.assertEqual(out[-1], 'pipe not recorded (registry unreadable): the hub resolves it at send time')

    def test_record_says_its_line_after_the_started_line(self):
        host = FakeHost(self.h.root)
        host.record_pipe = lambda settings, name, after_ms: pipes.NOT_YET
        out = start.start(self.h.settings, host, repo='alpha', account='1')
        self.assertEqual(out[-1], pipes.NOT_YET)

    def test_a_dry_run_records_nothing(self):
        host = FakeHost(self.h.root)
        start.start(self.h.settings, host, repo='alpha', account='1', whatif=True)
        self.assertEqual(host.piped, [])

    def test_stop_drops_the_entry_of_the_stopped_session(self):
        self.h.write_state(pipes.PIPES, {'x-a1': {'pid': 10}, 'x-a2': {'pid': 40}})
        host = FakeHost(self.h.root, windowed('x-a1') + [session(40, 'x-a2')])
        stop.stop(self.h.settings, host, 'x-a1')
        self.assertEqual(self.h.read_state(pipes.PIPES), {'x-a2': {'pid': 40}})

    def test_an_unreadable_pipes_file_is_said_and_the_stop_stands(self):
        with open(os.path.join(self.h.state, pipes.PIPES), 'w') as f:
            f.write('{broken')
        host = FakeHost(self.h.root, windowed('x-a1'))
        out = stop.stop(self.h.settings, host, 'x-a1')
        self.assertEqual(host.killed[0], (10, 1010))
        self.assertTrue(out[-1].startswith('could not update pipes.json: '), out)


if __name__ == '__main__':
    unittest.main()
