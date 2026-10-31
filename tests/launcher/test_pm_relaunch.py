"""launch.py pm-relaunch and pm-crossover (kitlib/launcher/relaunch.py, docs/launcher.md "PM relaunch",
"PM crossover"): the new session's exact command line and environment, coordinators.json's update,
the stand-down sent to the old PM, and every refusal. No window opens and no claude starts: FakeHost records the window's script."""
import json
import os
import unittest

from kitlib.hubsite import push
from kitlib.launcher import pipes, reboot, relaunch, stop, window
from kitlib.launcher.settings import LaunchError
from tests.launcher.fakes import FakeHost, Home, role_of, session

OLD = 'pm-sid-1'
NEW = 'pm-b'
FORK = 'fork-sid-2'


class ForkHost(FakeHost):
    """Opening the window makes the fork's live registry record (pid 4242, the window's session
    name) with session id sids[0]; each sleep moves it to the next id in sids (a long transcript
    resumes slowly, so the fork's own id comes late). said: no record is ever made (a fork that
    never starts)."""

    PID = 4242

    def __init__(self, home, sid=FORK, said=None, sids=None, **kw):
        FakeHost.__init__(self, home, **kw)
        self.sids, self.said, self.at, self.name = list(sids or [sid]), said, 0, None
        self.reg = os.path.join(home, '.claude', pipes.REGISTRY)

    def open_window(self, text, **kw):
        FakeHost.open_window(self, text, **kw)
        if not self.said:
            self.name = NEW
            self._write()

    def _write(self):
        with open(os.path.join(self.reg, '%d.json' % self.PID), 'w', encoding='utf-8') as f:
            json.dump({'pid': self.PID, 'sessionId': self.sids[self.at], 'procStart': str(1000 + self.PID),
                       'startedAt': pipes.launch_ms(self.now()) + 5, 'name': self.name,
                       'messagingSocketPath': '\\\\.\\pipe\\cc-msg-%d' % self.PID}, f)

    def sleep(self, seconds):
        FakeHost.sleep(self, seconds)
        if self.name and self.at < len(self.sids) - 1:
            self.at += 1
            self._write()

    def record_pipe(self, settings, name, after_ms):
        raise AssertionError('pm-relaunch polls the registry itself (pipes.await_record)')


class Delivery(object):
    """The hub push delivery: notes each deliver and answers `sent`."""

    def __init__(self, state=push.WRITTEN, session=OLD, raises=None):
        self.calls, self.state, self.session, self.raises = [], state, session, raises

    def deliver(self, recipient, text):
        self.calls.append((recipient, text))
        if self.raises:
            raise self.raises
        return push.Sent(self.state, 'pm-session', self.session, False)


class Base(unittest.TestCase):
    def setUp(self):
        self.h = Home(self, repos=('alpha', 'pm'))
        self.folder = self.h.repos['pm']
        self.coords = {'alpha': 1, 'pm_session_id': OLD, 'pm_folder': self.folder, 'pm_account': 2}
        self.h.write_state('coordinators.json', dict(self.coords))
        self.h.write_state('sessions.json', {'alpha': {'repo': 'alpha', 'agent': True, 'stem': 'alpha-coord',
                                                        'account': 1, 'name': 'alpha-coord-a1'}})
        self.reg = os.path.join(self.h.root, '.claude', pipes.REGISTRY)
        os.makedirs(self.reg)

    def record(self, pid, name, sid):
        """A live registry record (FakeHost.ctime is 1000 + pid)."""
        with open(os.path.join(self.reg, '%d.json' % pid), 'w', encoding='utf-8') as f:
            json.dump({'pid': pid, 'sessionId': sid, 'startedAt': 1, 'procStart': str(1000 + pid),
                       'name': name, 'messagingSocketPath': r'\\.\pipe\cc-msg-%d' % pid}, f)

    def coords_path(self):
        return os.path.join(self.h.state, 'coordinators.json')

    def pm_process(self):
        return {'Name': 'claude.exe', 'ProcessId': 70, 'ParentProcessId': 1, 'ExecutablePath': r'C:\bin\claude.exe',
                'CommandLine': "claude --resume '%s' --agent pm-kit:pm --remote-control 'pm-session' -n "
                               "'pm-session' 'x'" % OLD}


class Relaunch(Base):
    def run_it(self, host=None, delivery=None, **kw):
        self.host = host or ForkHost(self.h.root, procs=[self.pm_process()], env={'AGENT_TEMP': r'C:\agent-temp'})
        self.delivery = delivery if delivery is not None else Delivery()
        return relaunch.relaunch(self.h.settings, self.host, NEW, self.delivery, **kw)

    def test_the_fork_acts_as_pm_only_once_coordinators_json_names_its_own_session(self):
        prompt = relaunch.fork_prompt(NEW, self.coords_path())
        self.assertNotIn('You are the PM now', prompt)
        self.assertIn('only once %s has pm_session_id equal to your own session id' % self.coords_path(), prompt)
        self.assertIn('ask the owner', prompt)

    def test_a_fork_whose_id_changes_slowly_is_waited_for(self):
        host = ForkHost(self.h.root, sids=[OLD, OLD, OLD, FORK], procs=[self.pm_process()])
        out = self.run_it(host=host)
        self.assertEqual(self.h.read_state('coordinators.json')['pm_session_id'], FORK, out)
        self.assertEqual(len(host.slept), 3)
        self.assertEqual(self.h.read_state('pipes.json')[NEW]['session_id'], FORK)

    def test_the_wait_for_the_forks_id_is_about_two_minutes_and_can_be_set(self):
        self.assertGreaterEqual(relaunch.WAIT_S, 120)
        host = ForkHost(self.h.root, sid=OLD, procs=[self.pm_process()])
        self.run_it(host=host, wait_s=7)
        self.assertEqual(sum(host.slept), 7 - pipes.POLL_S)
        os.remove(os.path.join(self.reg, '%d.json' % ForkHost.PID))
        host = ForkHost(self.h.root, said=pipes.NOT_YET, procs=[self.pm_process()])
        self.run_it(host=host)
        self.assertEqual(sum(host.slept), relaunch.WAIT_S - pipes.POLL_S)

    def test_a_failing_coordinators_write_keeps_every_line_and_says_the_manual_step(self):
        from unittest import mock
        with mock.patch.object(relaunch, 'WRITE_WAIT', 0, create=True), \
                mock.patch('os.replace', side_effect=PermissionError(13, 'in use')):
            out = self.run_it()
        self.assertEqual(self.h.read_state('coordinators.json'), self.coords)
        self.assertTrue(out[0].startswith('Started %s' % NEW), out)
        self.assertIn('Told the old PM (pm-session, session %s) to stand down.' % OLD, out)
        self.assertTrue(any(('set pm_session_id to %s and pm_name to %s in %s' % (FORK, NEW, self.coords_path()))
                            in l for l in out), out)
        self.assertEqual(out[-1], relaunch.OWNER_STEP % NEW)

    def test_the_window_runs_the_fork_with_the_pm_environment_on_the_pms_account(self):
        out = self.run_it()
        self.assertEqual(len(self.host.opened), 1, out)
        text = self.host.opened[0]
        cmd = ("claude --resume '%s' --fork-session --agent pm-kit:pm --remote-control '%s' -n '%s' %s"
               % (OLD, NEW, NEW, window.q(relaunch.fork_prompt(NEW, self.coords_path()))))
        self.assertTrue(text.endswith('; ' + cmd), text)
        self.assertEqual(role_of(text), 'pm')
        self.assertIn("$env:PM_KIT_SESSION_NAME = '%s'" % NEW, text)
        self.assertIn("$env:CLAUDE_CONFIG_DIR = '%s'" % self.h.accounts['2'][0], text)
        self.assertIn("$env:AGENT_TEMP = 'C:\\agent-temp'", text)
        self.assertIn("Set-Location '%s'" % self.folder, text)
        self.assertIn("$Host.UI.RawUI.WindowTitle = '%s'" % NEW, text)
        self.assertEqual(self.h.read_state('pipes.json')[NEW]['session_id'], FORK)   # the registry poll's entry

    def test_coordinators_json_names_the_fork_and_keeps_every_other_key(self):
        out = self.run_it()
        got = self.h.read_state('coordinators.json')
        self.assertEqual(got, dict(self.coords, pm_session_id=FORK, pm_name=NEW))
        self.assertIn('coordinators.json: pm_session_id %s -> %s, pm_name %s' % (OLD, FORK, NEW), out)

    def test_the_old_pm_is_told_to_stand_down_before_coordinators_json_moves(self):
        seen = []

        class Watch(Delivery):
            def deliver(inner, recipient, text):
                seen.append(self.h.read_state('coordinators.json')['pm_session_id'])
                return Delivery.deliver(inner, recipient, text)

        out = self.run_it(delivery=Watch())
        self.assertEqual(self.delivery.calls, [(push.PM_KEY, relaunch.STAND_DOWN % (NEW, FORK, NEW))])
        self.assertEqual(seen, [OLD])                     # the hub's PM key still resolves to the old session
        self.assertIn('Told the old PM (pm-session, session %s) to stand down.' % OLD, out)
        self.assertEqual(out[-1], relaunch.OWNER_STEP % NEW)
        self.assertEqual(self.host.killed, [])            # nothing is stopped

    def test_no_push_delivery_says_the_stand_down_is_the_owners_to_send(self):
        self.host = ForkHost(self.h.root, procs=[self.pm_process()])
        out = relaunch.relaunch(self.h.settings, self.host, NEW, None)
        self.assertTrue(any(l.startswith('Stand-down not sent: this host has no push delivery') for l in out), out)
        self.assertEqual(self.h.read_state('coordinators.json')['pm_session_id'], FORK)

    def test_a_stand_down_not_written_is_said(self):
        out = self.run_it(delivery=Delivery(state=push.NO_SESSION))
        self.assertTrue(any(l.startswith('Stand-down not sent (%s)' % push.NO_SESSION) for l in out), out)

    def test_a_stand_down_that_reached_another_session_is_a_warning(self):
        out = self.run_it(delivery=Delivery(session='other-sid'))
        self.assertTrue(any(l.startswith('WARNING: the stand-down reached') for l in out), out)

    def test_an_unconfirmed_fork_changes_nothing_and_sends_nothing(self):
        host = ForkHost(self.h.root, said=pipes.NOT_YET, procs=[self.pm_process()])
        out = self.run_it(host=host)
        self.assertEqual(len(host.opened), 1)
        self.assertEqual(self.delivery.calls, [])
        self.assertEqual(self.h.read_state('coordinators.json'), self.coords)
        self.assertTrue(any('The fork was not confirmed (no session of that name came up in %s s)' % relaunch.WAIT_S
                            in l for l in out), out)

    def test_a_fork_that_kept_the_old_id_changes_nothing(self):
        out = self.run_it(host=ForkHost(self.h.root, sid=OLD, procs=[self.pm_process()]))
        self.assertEqual(self.delivery.calls, [])
        self.assertEqual(self.h.read_state('coordinators.json'), self.coords)
        self.assertTrue(any('the fork kept the old session id' in l for l in out), out)

    def test_a_desktop_app_pm_counts_as_running_by_its_registry_record(self):
        self.record(80, 'Project Manager', OLD)
        out = self.run_it(host=ForkHost(self.h.root))
        self.assertEqual(len(self.host.opened), 1, out)


class Refusals(Base):
    def refused(self, said, procs=None, name=NEW):
        host = ForkHost(self.h.root, procs=[self.pm_process()] if procs is None else procs)
        delivery = Delivery()
        with self.assertRaises(LaunchError) as c:
            relaunch.relaunch(self.h.settings, host, name, delivery)
        self.assertIn(said, str(c.exception))
        self.assertEqual(host.opened, [])
        self.assertEqual(delivery.calls, [])
        return host

    def test_no_coordinators_json(self):
        os.remove(os.path.join(self.h.state, 'coordinators.json'))
        self.refused('names no running PM')

    def test_a_placeholder_pm_session_id(self):
        self.h.write_state('coordinators.json', dict(self.coords, pm_session_id='<PM session id>'))
        self.refused('names no running PM')

    def test_no_pm_folder_or_account(self):
        self.h.write_state('coordinators.json', dict(self.coords, pm_account=None))
        self.refused('names no running PM')

    def test_the_recorded_pm_is_not_running(self):
        self.refused('is not running', procs=[session(71, 'alpha-coord-a1')])
        self.assertEqual(self.h.read_state('coordinators.json'), self.coords)

    def test_a_name_a_running_session_has(self):
        self.refused("--name '%s' is already taken: a session of that name is running (PID 72)" % NEW,
                     procs=[self.pm_process(), session(72, NEW)])

    def test_a_name_a_live_registry_record_has(self):
        self.record(81, NEW, 'someone-sid')
        self.refused("--name '%s' is already taken: a live session has that name" % NEW)

    def test_a_name_a_sessions_json_entry_has(self):
        self.refused("sessions.json entry 'alpha' has that name", name='alpha-coord-a1')

    def test_the_old_pm_name_itself(self):
        self.refused("--name 'pm-session' is a PM name", name='pm-session')

    def test_any_pm_name(self):
        for bad in ('pm-session-2', 'my-project manager', 'Project Manager'):
            self.refused("--name '%s' is a PM name" % bad, name=bad)

    def test_an_empty_or_spaced_name(self):
        for bad in ('', ' pm-b', 'pm-b '):
            self.refused('--name must be a session name on one line', name=bad)


class AfterTheRelaunch(Base):
    """The fork's command line carries the old id, so stop and after-reboot know it by pm_name."""

    def setUp(self):
        Base.setUp(self)
        self.h.write_state('coordinators.json', dict(self.coords, pm_session_id=FORK, pm_name=NEW))
        self.fork = {'Name': 'claude.exe', 'ProcessId': 90, 'ParentProcessId': 1, 'ExecutablePath': r'C:\bin\claude.exe',
                     'CommandLine': "claude --resume '%s' --fork-session --agent pm-kit:pm --remote-control '%s' "
                                    "-n '%s' 'x'" % (OLD, NEW, NEW)}

    def test_stop_refuses_the_relaunched_pm(self):
        with self.assertRaises(stop.Refused) as c:
            stop.stop(self.h.settings, FakeHost(self.h.root, procs=[self.fork]), NEW)
        self.assertIn('names the PM session', str(c.exception))

    def test_after_reboot_does_not_resume_beside_the_relaunched_pm(self):
        host = FakeHost(self.h.root, procs=[self.fork])
        said = []
        reboot.resume_pm(self.h.settings, host, self.h.read_state('coordinators.json'), said.append)
        self.assertEqual(host.opened, [])
        self.assertEqual(said, ['pm-session already running, skipped'])

    def test_after_reboot_resuming_pm_session_clears_pm_name(self):
        host = FakeHost(self.h.root, procs=[])
        said = []
        reboot.resume_pm(self.h.settings, host, self.h.read_state('coordinators.json'), said.append)
        self.assertEqual(len(host.opened), 1, said)
        got = self.h.read_state('coordinators.json')
        self.assertNotIn('pm_name', got)
        self.assertEqual(got['pm_session_id'], FORK)


class Crossover(Base):
    """launch.py pm-crossover (relaunch.crossover): a fresh PM on another account."""

    def setUp(self):
        self.h = Home(self, accounts=('1', '2', '3'), signed_out=('3',), repos=('alpha', 'pm'))
        self.folder = self.h.repos['pm']
        self.coords = {'alpha': 1, 'pm_session_id': OLD, 'pm_folder': self.folder, 'pm_account': 2}
        self.h.write_state('coordinators.json', dict(self.coords))
        self.h.write_state('sessions.json', {'alpha': {'repo': 'alpha', 'agent': True, 'stem': 'alpha-coord',
                                                        'account': 1, 'name': 'alpha-coord-a1'}})
        self.reg = os.path.join(self.h.root, '.claude', pipes.REGISTRY)
        os.makedirs(self.reg)
        self.handoff = os.path.join(self.folder, 'HANDOFF.md')
        with open(self.handoff, 'w') as f:
            f.write('hand-off')

    def run_it(self, host=None, delivery=None, account='1', name=NEW, **kw):
        self.host = host or ForkHost(self.h.root, procs=[self.pm_process()], env={'AGENT_TEMP': r'C:\agent-temp'})
        self.delivery = delivery if delivery is not None else Delivery()
        return relaunch.crossover(self.h.settings, self.host, account, name, delivery=self.delivery, **kw)

    def test_the_window_runs_a_fresh_pm_with_no_permission_mode_on_the_target_account(self):
        out = self.run_it(state_path=self.handoff)
        self.assertEqual(len(self.host.opened), 1, out)
        text = self.host.opened[0]
        prompt = relaunch.crossover_prompt(NEW, '1', self.coords_path(), self.handoff)
        cmd = "claude --agent pm-kit:pm --remote-control '%s' -n '%s' %s" % (NEW, NEW, window.q(prompt))
        self.assertTrue(text.endswith('; ' + cmd), text)
        self.assertNotIn('--resume', text)
        self.assertNotIn('--permission-mode', text)
        self.assertEqual(role_of(text), 'pm')
        self.assertIn("$env:CLAUDE_CONFIG_DIR = '%s'" % self.h.accounts['1'][0], text)
        self.assertIn("Set-Location '%s'" % self.folder, text)
        self.assertIn(self.handoff, prompt)
        self.assertIn('only once %s has pm_session_id equal to your own session id' % self.coords_path(), prompt)
        self.assertIn('ask the owner', prompt)

    def test_coordinators_json_names_the_new_pm_on_its_account_and_keeps_every_other_key(self):
        out = self.run_it()
        self.assertEqual(self.h.read_state('coordinators.json'),
                         dict(self.coords, pm_session_id=FORK, pm_name=NEW, pm_account=1))
        self.assertIn('coordinators.json: pm_session_id %s -> %s, pm_name %s, pm_account 2 -> 1' % (OLD, FORK, NEW),
                      out)
        self.assertEqual(out[-1], relaunch.OWNER_STEP % NEW)

    def test_the_old_pm_is_told_to_stand_down_before_coordinators_json_moves(self):
        seen = []

        class Watch(Delivery):
            def deliver(inner, recipient, text):
                seen.append(self.h.read_state('coordinators.json')['pm_session_id'])
                return Delivery.deliver(inner, recipient, text)

        out = self.run_it(delivery=Watch())
        self.assertEqual(self.delivery.calls, [(push.PM_KEY, relaunch.CROSS_STAND_DOWN % (NEW, FORK, '1'))])
        self.assertEqual(seen, [OLD])
        self.assertIn('Told the old PM (pm-session, session %s) to stand down.' % OLD, out)
        self.assertEqual(self.host.killed, [])

    def test_an_unconfirmed_start_exits_1_and_changes_nothing(self):
        host = ForkHost(self.h.root, said=pipes.NOT_YET, procs=[self.pm_process()])
        with self.assertRaises(LaunchError) as c:
            self.run_it(host=host, wait_s=4)
        self.assertIn('the new PM was not confirmed', str(c.exception))
        self.assertTrue(c.exception.lines[0].startswith('Started %s' % NEW), c.exception.lines)
        self.assertEqual(len(host.opened), 1)
        self.assertEqual(self.delivery.calls, [])
        self.assertEqual(self.h.read_state('coordinators.json'), self.coords)

    def test_a_failing_coordinators_write_exits_1_with_the_manual_step(self):
        from unittest import mock
        with mock.patch.object(relaunch, 'WRITE_WAIT', 0), \
                mock.patch('os.replace', side_effect=PermissionError(13, 'in use')):
            with self.assertRaises(LaunchError) as c:
                self.run_it()
        self.assertIn('set pm_session_id to %s, pm_name to %s and pm_account to 1' % (FORK, NEW), str(c.exception))
        self.assertEqual(self.h.read_state('coordinators.json'), self.coords)

    def test_what_if_opens_nothing_and_writes_nothing(self):
        out = self.run_it(name=None, what_if=True)
        self.assertEqual((self.host.opened, self.delivery.calls), ([], []))
        self.assertIn('Would start pm-a1, a fresh PM on account 1', out[0])
        self.assertNotIn('--permission-mode', out[0])
        self.assertEqual(self.h.read_state('coordinators.json'), self.coords)

    def test_the_default_name_is_pm_a_n_and_steps_past_a_taken_one(self):
        self.assertEqual(relaunch.crossover_name('1', set()), 'pm-a1')
        self.assertEqual(relaunch.crossover_name('1', {'pm-a1', 'pm-a1-2'}), 'pm-a1-3')
        host = ForkHost(self.h.root, procs=[self.pm_process(), session(72, 'pm-a1')])
        out = self.run_it(host=host, name=None, what_if=True)
        self.assertIn('Would start pm-a1-2', out[0])

    def refused(self, said, account='1', name=NEW, **kw):
        host = ForkHost(self.h.root, procs=[self.pm_process()])
        delivery = Delivery()
        with self.assertRaises(LaunchError) as c:
            relaunch.crossover(self.h.settings, host, account, name, delivery=delivery, **kw)
        self.assertIn(said, str(c.exception))
        self.assertEqual((host.opened, delivery.calls), ([], []))
        self.assertEqual(self.h.read_state('coordinators.json'), self.coords)

    def test_refusals(self):
        self.refused('the PM is already on account 2', account='2')
        self.refused('Account 3 is not signed in', account='3')
        self.refused('--state', state_path=os.path.join(self.folder, 'none.md'))
        self.refused("--name 'pm-session' is a PM name", name='pm-session')
        self.refused("sessions.json entry 'alpha' has that name", name='alpha-coord-a1')
        self.h.write_state('coordinators.json', dict(self.coords, pm_session_id='<PM session id>'))
        self.coords = self.h.read_state('coordinators.json')
        self.refused('names no running PM')

    def test_the_recorded_pm_not_running(self):
        host = ForkHost(self.h.root, procs=[session(71, 'alpha-coord-a1')])
        with self.assertRaises(LaunchError) as c:
            relaunch.crossover(self.h.settings, host, '1', NEW)
        self.assertIn('is not running', str(c.exception))
        self.assertEqual(host.opened, [])

    def test_relaunch_still_writes_only_its_two_keys(self):
        self.assertIsNone(relaunch.record_pm(self.h.settings, FakeHost(self.h.root), FORK, NEW))
        self.assertEqual(self.h.read_state('coordinators.json'), dict(self.coords, pm_session_id=FORK, pm_name=NEW))


class Cli(unittest.TestCase):
    def test_the_parser_takes_pm_relaunch_name(self):
        from tools import launch
        a = launch.parser().parse_args(['pm-relaunch', '--name', NEW])
        self.assertEqual((a.cmd, a.name, a.wait), ('pm-relaunch', NEW, relaunch.WAIT_S))
        self.assertEqual(launch.parser().parse_args(['pm-relaunch', '--name', NEW, '--wait', '30']).wait, 30)

    def test_push_delivery_is_none_only_when_the_host_has_none(self):
        import sys
        import types
        from unittest import mock
        from tools import launch

        def fake(err):
            def adapter_module():
                raise err
            return types.SimpleNamespace(adapter_module=adapter_module)

        with mock.patch.dict(sys.modules, {'_host': fake(ImportError('no adapter'))}):
            self.assertIsNone(launch.push_delivery(None))
        with mock.patch.dict(sys.modules, {'_host': fake(RuntimeError('broken'))}):
            with self.assertRaises(RuntimeError):
                launch.push_delivery(None)


    def test_the_parser_takes_pm_crossover(self):
        from tools import launch
        a = launch.parser().parse_args(['pm-crossover', '--account', '1'])
        self.assertEqual((a.cmd, a.account, a.name, a.state, a.wait, a.what_if),
                         ('pm-crossover', '1', None, None, relaunch.WAIT_S, False))
        import io as _io
        from unittest import mock
        for argv in (['pm-crossover'], ['pm-crossover', '--account', '1', '--state', r'Z:\no\such\file.md']):
            with mock.patch('sys.stderr', _io.StringIO()), self.assertRaises(SystemExit) as c:
                launch.parser().parse_args(argv)
            self.assertEqual(c.exception.code, launch.USAGE, argv)

    def test_pm_crossover_exits_1_on_a_refusal(self):
        import io as _io
        from unittest import mock
        from tools import launch
        h = Home(self, repos=('alpha', 'pm'))
        h.write_state('coordinators.json', {'pm_session_id': OLD, 'pm_folder': h.repos['pm'], 'pm_account': 2})
        with mock.patch('sys.stderr', _io.StringIO()) as err, mock.patch.object(launch.S, 'load',
                                                                               lambda home=None: h.settings):
            code = launch.main(['pm-crossover', '--account', '2'], host=FakeHost(h.root))
        self.assertEqual(code, 1)
        self.assertIn('the PM is already on account 2', err.getvalue())


if __name__ == '__main__':
    unittest.main()
