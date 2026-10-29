"""After a reboot (kitlib/launcher/reboot.py): plain entries start and agent entries are left to the
hub, a failed start never stops the run, briefs reach every entry that has one, and each path of
the PM resume."""
import os
import subprocess
import unittest

from kitlib.launcher import reboot, start, window
from kitlib.launcher import settings as S
from tests.launcher.fakes import NOW, FakeHost, Home, role_of, session

# plain sessions (agent false): the ones after-reboot starts
ALPHA = {'repo': 'alpha', 'agent': False, 'stem': 'alpha-coord', 'state': 's.md', 'account': 1, 'name': 'alpha-coord-a1'}
BETA = {'repo': 'alpha', 'agent': False, 'stem': 'beta-coord', 'state': 's.md', 'account': 1, 'name': 'beta-coord-a1'}
COORD = {'repo': 'alpha', 'agent': True, 'stem': 'lead-coord', 'state': 's.md', 'account': 1, 'name': 'lead-coord-a1'}
HELPER = {'repo': 'alpha', 'agent': False, 'stem': 'alpha-help', 'state': 's.md', 'brief': 'b.md', 'account': 1,
          'name': 'alpha-help-a1'}
PM = {'pm_session_id': 'pm-sid-1', 'pm_folder': None, 'pm_account': 2}
STORE = {'Name': 'claude.exe', 'ProcessId': 5, 'ExecutablePath': 'C:' + chr(92) + 'Program Files' + chr(92)
         + 'WindowsApps' + chr(92) + 'claude.exe', 'CommandLine': 'claude'}
UNNAMED = {'Name': 'claude.exe', 'ProcessId': 6, 'ExecutablePath': r'C:\bin\claude.exe', 'CommandLine': 'claude'}


class FailingHost(FakeHost):
    """Opening a window whose script names `bad` raises `exc`."""

    def __init__(self, home, bad, exc, **kw):
        FakeHost.__init__(self, home, **kw)
        self.bad, self.exc = bad, exc

    def open_window(self, text):
        if "-n '%s" % self.bad in text:
            raise self.exc
        FakeHost.open_window(self, text)


class FailedStarts(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)
        self.h.write_state('sessions.json', {'alpha': dict(ALPHA), 'beta': dict(BETA)})
        self.h.write_state('coordinators.json', dict(PM, pm_folder=self.h.root))

    def check(self, exc, said):
        host = FailingHost(self.h.root, 'alpha-coord', exc)
        out = reboot.run(self.h.settings, host)
        self.assertTrue(any(line.startswith('FAILED to start alpha : ') and said in line for line in out), out)
        names = sorted(role_of(t) for t in host.opened)
        self.assertEqual(names, ['plain', 'pm'])                        # beta and the PM still start
        written = self.h.read_state('sessions.json')
        self.assertEqual(written['alpha'], ALPHA)                        # the failed entry is unchanged
        self.assertEqual(written['beta']['name'], 'beta-coord-a1')
        self.assertIn('started pm-session', out)
        with open(os.path.join(self.h.state, reboot.LOG), encoding='utf-8') as f:
            self.assertIn('FAILED to start alpha', f.read())

    def test_a_runtime_error_from_the_window_is_logged_and_the_run_goes_on(self):
        self.check(RuntimeError('opening the window failed (powershell exit 1): x'), 'RuntimeError: opening')

    def test_a_timeout_is_logged_and_the_run_goes_on(self):
        self.check(subprocess.TimeoutExpired(['powershell'], 60), 'TimeoutExpired')

    def test_a_refusal_is_logged_as_before(self):
        self.check(S.LaunchError('No repo at x'), 'No repo at x')


class Briefs(unittest.TestCase):
    def test_after_reboot_passes_the_brief_of_every_plain_entry(self):
        h = Home(self)
        h.write_state('sessions.json', {'helper': dict(HELPER), 'alpha': dict(ALPHA, brief='c.md')})
        host = FakeHost(h.root)
        reboot.run(h.settings, host)
        briefs = sorted(t for t in host.opened if role_of(t) == 'plain')
        self.assertEqual(len(briefs), 2)
        self.assertTrue(any('Read your standing brief b.md first. ' in t for t in briefs))
        self.assertTrue(any('Read your standing brief c.md first. ' in t for t in briefs))

    def test_the_prompts_name_the_pm_by_its_live_session_name(self):
        h = Home(self)
        h.write_state('sessions.json', {'alpha': dict(ALPHA)})
        host = FakeHost(h.root)
        reboot.run(h.settings, host)
        text, = host.opened
        self.assertIn("''Project Manager'' in the desktop app, or ''pm-session'' when the launcher resumed it", text)
        self.assertIn('this pm-session must be stopped', reboot.PM_PROMPT)

    def test_entry_start_args_keeps_the_hand_off_rule_unless_any_brief(self):
        args = start.entry_start_args(None, 'helper', HELPER, '2', 'go')
        self.assertNotIn('brief', args)
        self.assertTrue(args['no_agent'])
        args = start.entry_start_args(None, 'helper', HELPER, '2', 'go', any_brief=True)
        self.assertEqual(args['brief'], 'b.md')
        self.assertEqual(start.entry_start_args(None, 'lead', dict(COORD, brief='c.md'), '2', 'go')['brief'], 'c.md')
        self.assertNotIn('brief', start.entry_start_args(None, 'lead', COORD, '2', 'go', any_brief=True))

    def test_dry_run_says_the_pm_decision_and_opens_and_logs_nothing(self):
        for key, said in ((False, 'desktop app running, pm-session skipped'),
                          (True, 'would resume pm-session (session pm-sid-1, account 2) in ')):
            h = Home(self, extra={'pm_resume_beside_desktop': key})
            h.write_state('sessions.json', {'helper': dict(HELPER)})
            h.write_state('coordinators.json', dict(PM, pm_folder=h.root))
            host = FakeHost(h.root, [STORE])
            out = reboot.run(h.settings, host, dry_run=True)
            self.assertTrue(any(line.startswith(said) for line in out), out)
            self.assertEqual(host.opened, [])
            self.assertFalse(os.path.exists(os.path.join(h.state, reboot.LOG)))
            self.assertEqual(h.read_state('sessions.json'), {'helper': HELPER})

    def test_dry_run_says_the_brief(self):
        h = Home(self)
        h.write_state('sessions.json', {'helper': dict(HELPER)})
        out = reboot.run(h.settings, FakeHost(h.root), dry_run=True)
        self.assertTrue(any(line.startswith('would start alpha-help-a') and 'brief=b.md' in line for line in out), out)


class ResumePm(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)
        self.lines = []

    def resume(self, cfg, procs=(), uptime=5.0, settings=None):
        host = FakeHost(self.h.root, procs, uptime=uptime)
        reboot.resume_pm(settings or self.h.settings, host, cfg, self.lines.append)
        return host

    def cfg(self, **kw):
        cfg = dict(PM, pm_folder=self.h.root)
        cfg.update(kw)
        return cfg

    def transcript(self, minutes_ago):
        d = os.path.join(self.h.accounts['1'][0], 'projects', 'p')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, 'pm-sid-1.jsonl')
        open(p, 'w').close()
        t = NOW.timestamp() - minutes_ago * 60
        os.utime(p, (t, t))

    def test_a_missing_or_placeholder_pm_entry_is_skipped(self):
        for cfg in ({}, self.cfg(pm_session_id='<pm session id>'), self.cfg(pm_folder=None),
                    self.cfg(pm_account=None)):
            host = self.resume(cfg)
            self.assertEqual(host.opened, [])
        self.assertEqual(len(self.lines), 4)
        self.assertTrue(all('names no PM session' in line for line in self.lines))

    def test_a_running_pm_session_or_session_id_is_left_alone(self):
        for procs in ([session(7, 'pm-session')],
                      [dict(session(8, 'x'), CommandLine="claude --resume 'pm-sid-1' --remote-control 'x' -n 'x'")]):
            self.assertEqual(self.resume(self.cfg(), procs).opened, [])
        self.assertEqual(self.lines, ['pm-session already running, skipped'] * 2)

    def test_the_desktop_app_or_an_unnamed_session_skips_it(self):
        self.assertFalse(self.h.settings.pm_resume_beside_desktop)          # the default
        for p in (STORE, UNNAMED):
            self.assertEqual(self.resume(self.cfg(), [p]).opened, [])
        self.assertEqual(self.lines, ['desktop app running, pm-session skipped'] * 2)

    def beside(self):
        return S.Settings(dict(self.h.obj, pm_resume_beside_desktop=True), self.h.root)

    def test_the_key_resumes_the_pm_beside_the_desktop_app_or_an_unnamed_session(self):
        for p in (STORE, UNNAMED):
            text, = self.resume(self.cfg(), [p], settings=self.beside()).opened
            self.assertEqual(role_of(text), 'pm')
            self.assertIn("claude --resume 'pm-sid-1' --agent pm-kit:pm --remote-control 'pm-session' -n 'pm-session'", text)
        self.assertEqual(self.lines, ['desktop app running, pm-session not skipped '
                                      '(launcher.pm_resume_beside_desktop)', 'started pm-session'] * 2)

    def test_the_key_still_refuses_a_running_pm_session(self):
        for procs in ([session(7, 'pm-session'), STORE],
                      [dict(session(8, 'x'), CommandLine="claude --resume 'pm-sid-1' --remote-control 'x' -n 'x'"),
                       UNNAMED]):
            self.assertEqual(self.resume(self.cfg(), procs, settings=self.beside()).opened, [])
        self.assertEqual(self.lines, ['pm-session already running, skipped'] * 2)

    def test_the_key_keeps_the_fresh_transcript_rule(self):
        self.transcript(minutes_ago=3)
        self.assertEqual(self.resume(self.cfg(), [STORE], uptime=60, settings=self.beside()).opened, [])
        self.assertEqual(self.lines[-1], 'pm transcript active, pm-session skipped')

    def test_a_fresh_transcript_skips_it_only_after_the_boot_window(self):
        self.transcript(minutes_ago=3)
        self.assertEqual(self.resume(self.cfg(), uptime=60).opened, [])
        self.assertEqual(self.lines, ['pm transcript active, pm-session skipped'])
        self.assertEqual(len(self.resume(self.cfg(), uptime=5).opened), 1)          # just booted: resume

    def test_a_stale_transcript_resumes(self):
        self.transcript(minutes_ago=40)
        host = self.resume(self.cfg(), uptime=60)
        text, = host.opened
        self.assertEqual(role_of(text), 'pm')
        self.assertIn("claude --resume 'pm-sid-1' --agent pm-kit:pm --remote-control 'pm-session' -n 'pm-session'", text)
        self.assertNotIn('--permission-mode', text)
        self.assertIn("$env:CLAUDE_CONFIG_DIR = %s" % window.q(self.h.accounts['2'][0]), text)
        self.assertIn("Set-Location %s" % window.q(self.h.root), text)
        self.assertEqual(self.lines, ['started pm-session'])

    def test_the_pm_window_sets_agent_temp_and_points_temp_and_tmp_at_it(self):
        temp = os.path.join(self.h.root, 'agent-temp')
        host = FakeHost(self.h.root, env={'AGENT_TEMP': temp})
        reboot.resume_pm(self.h.settings, host, self.cfg(), self.lines.append)
        text, = host.opened
        self.assertEqual(role_of(text), 'pm')
        for var in ('AGENT_TEMP', 'TEMP', 'TMP'):
            self.assertIn('$env:%s = %s' % (var, window.q(temp)), text)
        self.assertLess(text.index('$env:TMP'), text.index('claude --resume'))

    def test_the_default_account_folder_leaves_config_dir_unset(self):
        default = os.path.join(self.h.root, '.claude')
        os.makedirs(default)
        open(os.path.join(default, '.credentials.json'), 'w').close()
        s = S.Settings(dict(self.h.obj, accounts={'2': ['~/.claude']}), self.h.root)
        text, = self.resume(self.cfg(), settings=s).opened
        self.assertIn('Remove-Item Env:CLAUDE_CONFIG_DIR', text)
        self.assertNotIn('$env:CLAUDE_CONFIG_DIR =', text)

    def test_a_signed_out_pm_account_is_logged_failed_not_raised(self):
        h = Home(self, signed_out=('2',))
        host = FakeHost(h.root)
        self.assertEqual(reboot.resume_pm(h.settings, host, dict(PM, pm_folder=h.root), self.lines.append), [])
        self.assertEqual(host.opened, [])
        self.assertEqual(len(self.lines), 1)
        self.assertTrue(self.lines[0].startswith('FAILED to resume pm-session : Account 2 is not signed in'),
                        self.lines)


class HubOwnsCoordinators(unittest.TestCase):
    """The hub is the only restarter: its coord-restart job starts the coordinators, and runs
    after-reboot once after a boot for the plain sessions and the PM."""

    def test_the_plain_entry_starts_the_agent_entry_is_skipped_and_the_pm_resumes(self):
        h = Home(self)
        h.write_state('sessions.json', {'lead': dict(COORD), 'helper': dict(HELPER)})
        h.write_state('coordinators.json', dict(PM, pm_folder=h.root))
        host = FakeHost(h.root)
        out = reboot.run(h.settings, host)
        self.assertEqual(sorted(role_of(t) for t in host.opened), ['plain', 'pm'])
        self.assertFalse(any("-n 'lead-coord" in t for t in host.opened))
        self.assertIn('lead a coordinator: the hub restarts it (coord-restart), skipped', out)
        self.assertTrue(any(line.startswith('started alpha-help-a') for line in out), out)
        self.assertIn('started pm-session', out)
        self.assertEqual(h.read_state('sessions.json')['lead'], COORD)     # left as it was

    def test_a_dry_run_says_the_skip_too(self):
        h = Home(self)
        h.write_state('sessions.json', {'lead': dict(COORD)})
        out = reboot.run(h.settings, FakeHost(h.root), dry_run=True)
        self.assertIn('lead a coordinator: the hub restarts it (coord-restart), skipped', out)
        self.assertFalse(any(line.startswith('would start lead-coord') for line in out), out)


class TopLevelFailures(unittest.TestCase):
    """The hidden task has no console: a failure before any entry is a FAILED log line, not a traceback."""

    def logged(self, h):
        with open(os.path.join(h.state, reboot.LOG), encoding='utf-8') as f:
            return f.read()

    def test_no_signed_in_account_logs_failed_for_the_sessions_and_the_pm(self):
        h = Home(self, signed_out=('1', '2'))
        h.write_state('sessions.json', {'alpha': dict(ALPHA)})
        h.write_state('coordinators.json', dict(PM, pm_folder=h.root))
        host = FakeHost(h.root)
        out = reboot.run(h.settings, host)
        self.assertTrue(any(line.startswith('FAILED, no session started : ') for line in out), out)
        self.assertTrue(any(line.startswith('FAILED to resume pm-session : ') for line in out), out)
        self.assertEqual(host.opened, [])
        self.assertIn('FAILED, no session started', self.logged(h))

    def test_a_missing_or_bad_sessions_json_logs_failed_and_resumes_the_pm(self):
        for bad in (None, 'not json'):
            h = Home(self)
            h.write_state('coordinators.json', dict(PM, pm_folder=h.root))
            if bad is not None:
                with open(os.path.join(h.state, 'sessions.json'), 'w', encoding='utf-8') as f:
                    f.write(bad)
            host = FakeHost(h.root)
            out = reboot.run(h.settings, host)
            self.assertEqual([line for line in out if line.startswith('FAILED')][:1],
                             [line for line in out if line.startswith('FAILED, no session started : ')], out)
            self.assertIn('sessions.json', self.logged(h))
            self.assertEqual([role_of(t) for t in host.opened], ['pm'])
            self.assertIn('started pm-session', out)

    def test_a_bad_coordinators_json_logs_failed_and_the_coordinators_still_start(self):
        h = Home(self)
        h.write_state('sessions.json', {'alpha': dict(ALPHA)})
        with open(os.path.join(h.state, 'coordinators.json'), 'w', encoding='utf-8') as f:
            f.write('{broken')
        host = FakeHost(h.root)
        out = reboot.run(h.settings, host)
        self.assertTrue(any(line.startswith('FAILED to read coordinators.json : ') for line in out), out)
        self.assertEqual([role_of(t) for t in host.opened], ['plain'])


class SaidAsItHappens(unittest.TestCase):
    """`after-reboot --test` waited WAIT_SECONDS with nothing said, then opened its window
    minimized: the owner took the silence for a failure."""

    class Watching(FakeHost):
        """Records what had been said when the run slept and when it opened a window."""

        def __init__(self, home, said):
            FakeHost.__init__(self, home)
            self.said, self.at_sleep, self.at_open = said, None, None

        def sleep(self, seconds):
            self.at_sleep = list(self.said)
            FakeHost.sleep(self, seconds)

        def open_window(self, text):
            self.at_open = list(self.said)
            FakeHost.open_window(self, text)

    def test_the_wait_is_said_before_it_starts_and_say_takes_every_line(self):
        h = Home(self)
        said = []
        host = self.Watching(h.root, said)
        out = reboot.run(h.settings, host, test=True, say=said.append)
        self.assertEqual(out, [])                                    # said, so not returned twice
        self.assertEqual(host.slept, [reboot.WAIT_SECONDS])
        self.assertEqual(host.at_sleep, ['start (test=True, dry_run=False)',
                                         'waiting %d s for the network before starting anything'
                                         % reboot.WAIT_SECONDS])
        self.assertEqual(host.at_open, host.at_sleep)
        self.assertEqual(len(said), 3, said)
        with open(os.path.join(h.state, reboot.LOG), encoding='utf-8') as f:
            self.assertEqual(f.read().count('waiting %d s' % reboot.WAIT_SECONDS), 1)

    def test_the_test_session_line_says_its_window_opens_minimized(self):
        h = Home(self)
        host = FakeHost(h.root)
        out = reboot.run(h.settings, host, test=True)
        self.assertEqual(len(host.opened), 1)
        self.assertTrue(out[-1].startswith('started %s' % reboot.TEST_NAME), out)
        self.assertIn('minimized', out[-1])
        self.assertIn('taskbar', out[-1])

    def test_a_dry_run_says_its_lines_too(self):
        h = Home(self)
        h.write_state('sessions.json', {'alpha': dict(ALPHA)})
        said = []
        self.assertEqual(reboot.run(h.settings, FakeHost(h.root), dry_run=True, say=said.append), [])
        self.assertTrue(any(line.startswith('would start alpha-coord-a1 (alpha)') for line in said), said)


if __name__ == '__main__':
    unittest.main()
