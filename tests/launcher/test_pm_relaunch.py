"""launch.py pm-relaunch (kitlib/launcher/relaunch.py, docs/launcher.md "PM relaunch"): the fork's
exact command line and environment, coordinators.json's update, the stand-down sent to the old PM,
and every refusal. No window opens and no claude starts: FakeHost records the window's script."""
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
    """record_pipe writes the pipes.json entry the registry poll would (session_id `sid`), or says
    `said` and writes nothing."""

    def __init__(self, home, sid=FORK, said=None, **kw):
        FakeHost.__init__(self, home, **kw)
        self.sid, self.said = sid, said

    def record_pipe(self, settings, name, after_ms):
        FakeHost.record_pipe(self, settings, name, after_ms)
        if self.said:
            return self.said
        p = os.path.join(settings.state_dir(), pipes.PIPES)
        data = {}
        if os.path.exists(p):
            with open(p, encoding='utf-8') as f:
                data = json.load(f)
        data[name] = {'pid': 4242, 'session_id': self.sid, 'pipe': r'\\.\pipe\cc-msg-4242', 'proc_start': '5242'}
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        return None


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

    def pm_process(self):
        return {'Name': 'claude.exe', 'ProcessId': 70, 'ParentProcessId': 1, 'ExecutablePath': r'C:\bin\claude.exe',
                'CommandLine': "claude --resume '%s' --agent pm-kit:pm --remote-control 'pm-session' -n "
                               "'pm-session' 'x'" % OLD}


class Relaunch(Base):
    def run_it(self, host=None, delivery=None):
        self.host = host or ForkHost(self.h.root, procs=[self.pm_process()], env={'AGENT_TEMP': r'C:\agent-temp'})
        self.delivery = delivery if delivery is not None else Delivery()
        return relaunch.relaunch(self.h.settings, self.host, NEW, self.delivery)

    def test_the_window_runs_the_fork_with_the_pm_environment_on_the_pms_account(self):
        out = self.run_it()
        self.assertEqual(len(self.host.opened), 1, out)
        text = self.host.opened[0]
        cmd = ("claude --resume '%s' --fork-session --agent pm-kit:pm --remote-control '%s' -n '%s' %s"
               % (OLD, NEW, NEW, window.q(relaunch.FORK_PROMPT % (NEW, NEW, NEW))))
        self.assertTrue(text.endswith('; ' + cmd), text)
        self.assertEqual(role_of(text), 'pm')
        self.assertIn("$env:PM_KIT_SESSION_NAME = '%s'" % NEW, text)
        self.assertIn("$env:CLAUDE_CONFIG_DIR = '%s'" % self.h.accounts['2'][0], text)
        self.assertIn("$env:AGENT_TEMP = 'C:\\agent-temp'", text)
        self.assertIn("Set-Location '%s'" % self.folder, text)
        self.assertIn("$Host.UI.RawUI.WindowTitle = '%s'" % NEW, text)
        self.assertEqual([n for n, _ in self.host.piped], [NEW])           # the fork's id: the registry poll

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
        self.assertTrue(any('The fork was not confirmed (%s)' % pipes.NOT_YET in l for l in out), out)

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
        self.refused("--name 'pm-session' is already taken", name='pm-session')

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


class Cli(unittest.TestCase):
    def test_the_parser_takes_pm_relaunch_name(self):
        from tools import launch
        a = launch.parser().parse_args(['pm-relaunch', '--name', NEW])
        self.assertEqual((a.cmd, a.name), ('pm-relaunch', NEW))


if __name__ == '__main__':
    unittest.main()
