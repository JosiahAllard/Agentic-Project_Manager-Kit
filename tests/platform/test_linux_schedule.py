"""kitlib.platform.linux.schedule (docs/linux-support-plan.md slice 10): the systemd user units,
crontab lines and XDG autostart entries a pm-kit job is written as, their read-back, the backend
choice, the foreign-file refusals and linger's read-only note.

Every test but the Live ones writes into a temporary home and runs a fake `systemctl`,
`crontab` and `loginctl` (FakeSystem), so they run on Windows too and never touch a real user's
units or crontab. The Live tests run only on Linux with KIT_LINUX_LIVE_SCHEDULE=1: each installs
a throwaway job pm-kit-test-<pid> to the real user systemd, or to the real crontab, waits for it
to run once, then removes it.
"""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from kitlib.platform.linux import schedule as S
from tests.platforms import posix_only

LIVE = os.environ.get('KIT_LINUX_LIVE_SCHEDULE') == '1'


def cron_sees(command):
    """The command as cron hands it to the shell (Vixie cron and cronie, do_command): a bare %
    ends it (the rest is stdin), \\% is a literal %, any other backslash stays as it is."""
    out, escaped = [], False
    for ch in command:
        if escaped:
            if ch == '%':
                out[-1] = '%'
            else:
                out.append(ch)
            escaped = False
            continue
        if ch == '\\':
            escaped = True
        elif ch == '%':
            break
        out.append(ch)
    return ''.join(out)


def which_cron(name):
    return '/usr/bin/crontab' if name == 'crontab' else None


def which_none(name):
    return None


class FakeSystem(object):
    """systemctl --user, crontab and loginctl as a test's `run`: answers and records each call."""

    def __init__(self, systemd=True, crontab='', crontab_error=None, linger='no', show=None):
        self.systemd, self.crontab, self.crontab_error = systemd, crontab, crontab_error
        self.linger, self.show, self.calls = linger, show or {}, []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw.get('input')))
        ok = SimpleNamespace(returncode=0, stdout='', stderr='')
        if argv[0] == 'systemctl':
            if not self.systemd:
                return SimpleNamespace(returncode=1, stdout='',
                                       stderr='Failed to connect to bus: No medium found')
            if argv[2] == 'show':
                ok.stdout = ''.join('%s=%s\n' % kv for kv in sorted(self.show.items()))
            return ok
        if argv[:2] == ['crontab', '-l']:
            if self.crontab_error:
                return SimpleNamespace(returncode=1, stdout='', stderr=self.crontab_error)
            if self.crontab is None:
                return SimpleNamespace(returncode=1, stdout='', stderr='no crontab for kitvm')
            ok.stdout = self.crontab
            return ok
        if argv[:2] == ['crontab', '-']:
            self.crontab = kw.get('input')
            return ok
        if argv[0] == 'loginctl':
            ok.stdout = (self.linger or '') + '\n'
            return ok
        raise AssertionError('unexpected command %r' % (argv,))

    def ran(self, *head):
        return [a for a, _ in self.calls if a[:len(head)] == list(head)]


class Case(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='schedule-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.kit = os.path.join(self.home, 'kit dir')
        self.py = os.path.abspath(sys.executable)

    def job(self, when=('daily', '03:00'), name='demo-worktree-sweep', limit=None, argv=None):
        return S.Job(name, argv or [self.py, os.path.join(self.kit, 'tools', 'x.py'), '--apply'],
                     self.kit, when, 'a demo job', limit)

    def files(self):
        out = []
        for top, _, names in os.walk(self.home):
            out += [os.path.relpath(os.path.join(top, n), self.home).replace(os.sep, '/') for n in names]
        return sorted(out)


class Systemd(Case):
    def test_a_daily_job_is_a_oneshot_service_and_a_persistent_timer_started_now(self):
        run = FakeSystem()
        lines = S.install(self.job(), S.SYSTEMD, self.home, run)
        self.assertEqual(self.files(), ['.config/systemd/user/pm-kit-demo-worktree-sweep.service',
                                        '.config/systemd/user/pm-kit-demo-worktree-sweep.timer'])
        unit = os.path.join(S.unit_dir(self.home), 'pm-kit-demo-worktree-sweep')
        service, timer = S._read(unit + '.service'), S._read(unit + '.timer')
        self.assertTrue(service.startswith(S.MARK + 'demo-worktree-sweep\n'))
        for part in ('Type=oneshot', 'TimeoutStartSec=infinity', 'KillMode=process',
                     'WorkingDirectory=%s' % self.kit):
            self.assertIn(part, service)
        self.assertNotIn('[Install]', service)
        for part in ('OnCalendar=*-*-* 03:00:00', 'Persistent=true', 'WantedBy=timers.target'):
            self.assertIn(part, timer)
        self.assertEqual(run.ran('systemctl', '--user', 'daemon-reload'),
                         [['systemctl', '--user', 'daemon-reload']])
        self.assertEqual(run.ran('systemctl', '--user', 'enable'),
                         [['systemctl', '--user', 'enable', '--now', 'pm-kit-demo-worktree-sweep.timer']])
        self.assertIn('started', lines[0])

    def test_every_n_minutes_with_a_limit(self):
        job = self.job(('every', 15), 'kit-automatic-tasks', 10)
        S.install(job, S.SYSTEMD, self.home, FakeSystem())
        unit = os.path.join(S.unit_dir(self.home), 'pm-kit-kit-automatic-tasks')
        self.assertIn('OnCalendar=*:0/15', S._read(unit + '.timer'))
        self.assertIn('TimeoutStartSec=10min', S._read(unit + '.service'))

    def test_a_boot_job_is_a_service_wanted_by_the_session_enabled_not_started(self):
        run = FakeSystem()
        job = self.job(('boot',), 'coordinators-after-reboot')
        lines = S.install(job, S.SYSTEMD, self.home, run)
        self.assertEqual(self.files(), ['.config/systemd/user/pm-kit-coordinators-after-reboot.service'])
        service = S._read(os.path.join(S.unit_dir(self.home), 'pm-kit-coordinators-after-reboot.service'))
        for part in ('Type=simple', 'KillMode=process', 'WantedBy=default.target'):
            self.assertIn(part, service)
        self.assertNotIn('OnStartupSec', service)
        self.assertEqual(run.ran('systemctl', '--user', 'enable'),
                         [['systemctl', '--user', 'enable', 'pm-kit-coordinators-after-reboot.service']])
        self.assertIn('enabled', lines[0])

    def test_a_dollar_in_the_folder_is_written_as_it_is_and_reads_back(self):
        """WorkingDirectory= expands specifiers (%) but no $ variables: only % is doubled."""
        folder = os.path.join(self.home, 'a $HOME b 5%')
        job = S.Job('odd-folder', [self.py, os.path.join(folder, 'x.py')], folder, ('daily', '03:00'),
                    'costs $5 at 5%', None)
        S.install(job, S.SYSTEMD, self.home, FakeSystem())
        unit = os.path.join(S.unit_dir(self.home), 'pm-kit-odd-folder')
        service, timer = S._read(unit + '.service'), S._read(unit + '.timer')
        self.assertIn('\nWorkingDirectory=%s\n' % folder.replace('%', '%%'), service)
        self.assertIn('\nDescription=costs $5 at 5%%\n', service)
        self.assertIn('\nDescription=costs $5 at 5%%\n', timer)
        self.assertNotIn('$$', service.split('ExecStart=')[0])
        task = S.find(job.name, S.written(self.home))
        self.assertEqual(task['workdir'], folder)
        self.assertTrue(S.same(task, job), (task, job))

    def test_a_logon_server_keeps_the_default_kill_mode(self):
        S.install(self.job(('logon',), 'hub'), S.SYSTEMD, self.home, FakeSystem())
        service = S._read(os.path.join(S.unit_dir(self.home), 'pm-kit-hub.service'))
        self.assertIn('Type=simple', service)
        self.assertNotIn('KillMode', service)
        self.assertIn('WantedBy=default.target', service)

    def test_every_form_reads_back_as_written(self):
        odd = [self.py, os.path.join(self.kit, 'a "b" $HOME 100%.py'), 'back\\slash', "it's"]
        for when, limit in ((('daily', '04:00'), None), (('every', 5), 10), (('boot',), None),
                            (('logon',), None)):
            name = 'j-%s' % when[0]
            job = self.job(when, name, limit, odd)
            S.install(job, S.SYSTEMD, self.home, FakeSystem())
            task = S.find(name, S.read_back(self.home, FakeSystem(), which_none))
            self.assertTrue(S.same(task, job), (task, job))
            self.assertEqual(task['backend'], S.SYSTEMD)
            self.assertEqual(task['path'], name)
            self.assertEqual(task['actions'][0]['exe'], self.py)

    def test_a_changed_job_is_not_the_same(self):
        job = self.job()
        S.install(job, S.SYSTEMD, self.home, FakeSystem())
        task = S.find(job.name, S.written(self.home))
        self.assertFalse(S.same(task, job._replace(when=('daily', '05:00'))))
        self.assertFalse(S.same(task, job._replace(argv=job.argv + ['--more'])))
        self.assertFalse(S.same(None, job))

    def test_a_foreign_unit_of_that_name_is_refused_and_left(self):
        folder = S.unit_dir(self.home)
        os.makedirs(folder)
        mine = os.path.join(folder, 'pm-kit-demo-worktree-sweep.timer')
        with open(mine, 'w') as f:
            f.write('[Timer]\nOnCalendar=daily\n')
        run = FakeSystem()
        with self.assertRaises(S.ScheduleError) as e:
            S.install(self.job(), S.SYSTEMD, self.home, run)
        self.assertIn('not the pm-kit job', str(e.exception))
        self.assertEqual(self.files(), ['.config/systemd/user/pm-kit-demo-worktree-sweep.timer'])
        self.assertEqual(run.calls, [])

    def test_remove_disables_and_deletes_only_marked_units(self):
        run = FakeSystem()
        S.install(self.job(), S.SYSTEMD, self.home, run)
        other = os.path.join(S.unit_dir(self.home), 'pm-kit-other.service')
        with open(other, 'w') as f:
            f.write('[Service]\nExecStart=/bin/true\n')
        lines = S.remove('demo-worktree-sweep', self.home, run, which_none)
        self.assertEqual(self.files(), ['.config/systemd/user/pm-kit-other.service'])
        self.assertEqual(run.ran('systemctl', '--user', 'disable'),
                         [['systemctl', '--user', 'disable', '--now', 'pm-kit-demo-worktree-sweep.timer',
                           'pm-kit-demo-worktree-sweep.service']])
        self.assertTrue(lines)
        self.assertEqual(S.read_back(self.home, run, which_none), [])

    def test_a_systemctl_failure_after_the_write_is_an_error(self):
        class Fails(FakeSystem):
            def __call__(self, argv, **kw):
                if argv[2:3] == ['enable']:
                    self.calls.append((list(argv), None))
                    return SimpleNamespace(returncode=1, stdout='', stderr='Unit file is masked.')
                return FakeSystem.__call__(self, argv, **kw)
        with self.assertRaises(S.ScheduleError) as e:
            S.install(self.job(), S.SYSTEMD, self.home, Fails())
        self.assertIn('masked', str(e.exception))

    def test_state_reads_the_service(self):
        run = FakeSystem(show={'LoadState': 'loaded', 'UnitFileState': 'enabled', 'ActiveState': 'active'})
        self.assertEqual(S.state('hub', run), {'LoadState': 'loaded', 'UnitFileState': 'enabled',
                                                'ActiveState': 'active'})
        self.assertIn('pm-kit-hub.service', run.calls[0][0])
        S.start('hub', run)
        self.assertEqual(run.ran('systemctl', '--user', 'start'),
                         [['systemctl', '--user', 'start', 'pm-kit-hub.service']])


class Cron(Case):
    def test_without_systemd_a_timed_job_is_a_marked_crontab_line_beside_the_others(self):
        run = FakeSystem(systemd=False, crontab='MAILTO=""\n0 1 * * * /bin/mine\n')
        job = self.job(argv=[self.py, os.path.join(self.kit, '100%.py')])
        self.assertEqual(S.choose(job, run, which_cron), S.CRON)
        S.install(job, None, self.home, run, which_cron)
        lines = run.crontab.splitlines()
        self.assertEqual(lines[:2], ['MAILTO=""', '0 1 * * * /bin/mine'])
        self.assertTrue(lines[2].startswith('0 3 * * * cd '), lines[2])
        self.assertTrue(lines[2].endswith(' # pm-kit job: demo-worktree-sweep'), lines[2])
        self.assertIn('100\\%.py', lines[2])
        self.assertIn('>/dev/null 2>&1', lines[2])
        self.assertEqual(self.files(), [])

    def test_a_second_install_replaces_its_line_only(self):
        run = FakeSystem(systemd=False, crontab='0 1 * * * /bin/mine\n')
        S.install(self.job(), S.CRON, self.home, run)
        S.install(self.job(('daily', '05:30')), S.CRON, self.home, run)
        lines = run.crontab.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith('30 5 * * * '), lines[1])

    def test_every_form_reads_back_as_written(self):
        odd = [self.py, os.path.join(self.kit, 'a b %d.py'), "it's", '$x']
        for when, limit in ((('daily', '04:00'), None), (('every', 15), 10), (('boot',), None)):
            run = FakeSystem(systemd=False, crontab=None)
            job = self.job(when, 'j-%s' % when[0], limit, odd)
            S.install(job, S.CRON, self.home, run)
            task = S.find(job.name, S.read_back(self.home, run, which_cron))
            self.assertTrue(S.same(task, job), (task, job))
            self.assertEqual(task['backend'], S.CRON)

    def test_a_limit_runs_under_timeout(self):
        line = S.cron_line(self.job(('every', 15), 'tick', 10))
        self.assertTrue(line.startswith('*/15 * * * * cd '), line)
        self.assertIn(' && timeout --kill-after=30s 600 ', line)

    def test_a_crontab_that_cannot_be_read_is_never_written(self):
        run = FakeSystem(systemd=False, crontab_error='crontab: cannot open /var/spool: Permission denied')
        with self.assertRaises(S.ScheduleError) as e:
            S.install(self.job(), S.CRON, self.home, run)
        self.assertIn('nothing was written', str(e.exception))
        self.assertEqual(run.ran('crontab', '-'), [])

    def test_a_logon_job_has_no_crontab_form(self):
        with self.assertRaises(S.ScheduleError):
            S.cron_line(self.job(('logon',)))

    def test_remove_takes_only_its_line(self):
        run = FakeSystem(systemd=False, crontab='0 1 * * * /bin/mine # pm-kit job: lookalike-not\n')
        S.install(self.job(), S.CRON, self.home, run)
        S.remove('demo-worktree-sweep', self.home, run, which_cron)
        self.assertEqual(run.crontab, '0 1 * * * /bin/mine # pm-kit job: lookalike-not\n')

    def test_a_backslash_before_a_percent_stays_literal(self):
        """cron's own % handling (cron_sees), then the shell's words: every argument arrives as it
        was, a backslash right before a % included."""
        folder = os.path.join(self.home, 'w\\%d')
        argv = [self.py, os.path.join(folder, 'a\\%b.py'), 'c\\\\%d', '%', '\\', '50%\\']
        job = S.Job('slash-pct', argv, folder, ('every', 15), 'x', None)
        line = S.cron_line(job)
        words = shlex.split(cron_sees(line.split(None, 5)[5]), comments=True)
        self.assertEqual(words, ['cd', folder, '&&'] + argv + ['>/dev/null', '2>&1'])
        task = S.parse_cron(line)
        self.assertTrue(S.same(task, job), (task, job))

    def test_a_bad_timeout_number_is_an_unreadable_line_not_an_error(self):
        line = ('*/15 * * * * cd /k && timeout --kill-after=30s soon /usr/bin/python3 /k/x.py '
                '>/dev/null 2>&1 # pm-kit job: tick')
        task = S.parse_cron(line)
        self.assertEqual((task['path'], task['argv'], task['when'], task['line']),
                         ('tick', [], None, line))
        run = FakeSystem(systemd=False, crontab=line + '\n')
        found = S.find('tick', S.read_back(self.home, run, which_cron))
        self.assertFalse(S.same(found, self.job(('every', 15), 'tick', 10)))

    def test_unmarked_lines_are_not_tasks(self):
        self.assertIsNone(S.parse_cron('0 1 * * * /bin/mine'))
        self.assertIsNone(S.parse_cron('# a comment'))


class Remove(Case):
    def test_a_unit_that_cannot_be_removed_is_a_refusal(self):
        S.install(self.job(), S.SYSTEMD, self.home, FakeSystem())
        with mock.patch('os.remove', side_effect=PermissionError(13, 'Permission denied')):
            with self.assertRaises(S.ScheduleError) as e:
                S.remove('demo-worktree-sweep', self.home, FakeSystem(), which_none)
        self.assertIn('job demo-worktree-sweep: cannot remove ', str(e.exception))
        self.assertIn('pm-kit-demo-worktree-sweep.timer', str(e.exception))
        self.assertIn('Permission denied', str(e.exception))

    def test_an_entry_that_cannot_be_removed_says_what_already_went(self):
        run = FakeSystem(systemd=False, crontab='')
        S.install(self.job(('every', 15), 'tick'), S.CRON, self.home, run)
        S.install(S.Job('tick', [self.py, os.path.join(self.kit, 'x.py')], self.kit, ('logon',), 'x'),
                  S.AUTOSTART, self.home, run, which_cron, environ={'XDG_CURRENT_DESKTOP': 'GNOME'})
        with mock.patch('os.remove', side_effect=PermissionError(13, 'Permission denied')):
            with self.assertRaises(S.ScheduleError) as e:
                S.remove('tick', self.home, run, which_cron)
        self.assertIn('cannot remove %s' % S.desktop_path('tick', self.home), str(e.exception))
        self.assertIn('already: removed the crontab line of tick', str(e.exception))


class Autostart(Case):
    DESKTOP = {'XDG_CURRENT_DESKTOP': 'GNOME'}

    def test_a_logon_job_with_no_systemd_in_a_desktop_is_an_autostart_entry(self):
        run = FakeSystem(systemd=False)
        job = self.job(('logon',), 'hub', argv=[self.py, os.path.join(self.kit, 'say "hi" $x 5%.py'),
                                                'back\\slash'])
        self.assertEqual(S.choose(job, run, which_cron, self.DESKTOP), S.AUTOSTART)
        S.install(job, None, self.home, run, which_cron, self.DESKTOP)
        self.assertEqual(self.files(), ['.config/autostart/pm-kit-hub.desktop'])
        text = S._read(S.desktop_path('hub', self.home))
        self.assertIn('X-PmKit-Job=hub', text)
        self.assertIn('Terminal=false', text)
        task = S.find('hub', S.written(self.home))
        self.assertTrue(S.same(task, job), (task, job))
        self.assertFalse(task['hidden'])

    def test_a_hidden_entry_reads_back_hidden(self):
        S.install(self.job(('logon',), 'hub'), S.AUTOSTART, self.home, FakeSystem(systemd=False))
        path = S.desktop_path('hub', self.home)
        with open(path, 'a') as f:
            f.write('Hidden=true\n')
        self.assertTrue(S.find('hub', S.written(self.home))['hidden'])

    def test_only_a_logon_job_is_an_autostart_entry(self):
        with self.assertRaises(S.ScheduleError):
            S.install(self.job(('boot',)), S.AUTOSTART, self.home, FakeSystem(systemd=False))

    def test_no_shell_startup_file_is_ever_written(self):
        for kind in (S.SYSTEMD, S.AUTOSTART):
            S.install(self.job(('logon',), 'hub'), kind, self.home, FakeSystem())
        for f in self.files():
            self.assertTrue(f.startswith('.config/'), f)


class Choice(Case):
    def test_systemd_first(self):
        self.assertEqual(S.choose(self.job(), FakeSystem(), which_cron), S.SYSTEMD)
        self.assertEqual(S.choose(self.job(('logon',)), FakeSystem(), which_none, {}), S.SYSTEMD)

    def test_neither_backend_names_both(self):
        with self.assertRaises(S.ScheduleError) as e:
            S.choose(self.job(), FakeSystem(systemd=False), which_none)
        self.assertIn('no user systemd', str(e.exception))
        self.assertIn('no crontab', str(e.exception))
        with self.assertRaises(S.ScheduleError) as e:
            S.choose(self.job(('logon',)), FakeSystem(systemd=False), which_cron, {})
        self.assertIn('no user systemd', str(e.exception))
        self.assertIn('no desktop session', str(e.exception))

    def test_a_systemctl_that_cannot_run_is_no_systemd(self):
        def gone(argv, **kw):
            raise FileNotFoundError(2, 'No such file', argv[0])
        self.assertFalse(S.has_systemd(gone))

    def test_bad_jobs_are_refused(self):
        bad = [self.job(name='a b'), self.job(name=''), self.job(argv=['python3', 'x.py']),
               self.job(argv=[self.py, 'a\nb']), self.job(('daily', '3:00')),
               self.job(('every', 0)), self.job(('every', 60)), self.job(('weekly',)),
               self.job(limit=0)]
        for job in bad:
            with self.assertRaises(S.ScheduleError, msg=repr(job)):
                S.check(job)
        S.check(self.job())


class Linger(unittest.TestCase):
    def test_linger_is_read_never_enabled(self):
        for said, want in (('yes', True), ('no', False), ('', None)):
            run = FakeSystem(linger=said)
            self.assertIs(S.linger(run, 1000), want)
            self.assertEqual(run.ran('loginctl'),
                             [['loginctl', 'show-user', '1000', '-p', 'Linger', '--value']])
        run = FakeSystem(linger='no')
        note = S.linger_note(run, 1000)
        self.assertIn(S.LINGER_COMMAND, note)
        self.assertIsNone(S.linger_note(FakeSystem(linger='yes'), 1000))
        self.assertFalse([a for a, _ in run.calls if 'enable-linger' in a])


class DefaultRunner(unittest.TestCase):
    """With no `run`, commands go through subprocess_run: subprocess.run with creation flags 0,
    never kitlib.proc.spawn.run, whose no-window check asks Windows for its console and so
    fails on Linux (it did, on the reference machine, before this)."""

    def test_subprocess_run_drops_any_creation_flags(self):
        done = subprocess.CompletedProcess(['x'], 0, '', '')
        with mock.patch('subprocess.run', return_value=done) as got:
            self.assertIs(S.subprocess_run(['x'], creationflags=0x08000000, text=True), done)
        got.assert_called_once_with(['x'], creationflags=0, text=True)

    def test_a_command_with_no_run_given_never_reaches_spawn(self):
        done = subprocess.CompletedProcess(['crontab', '-l'], 0, '0 * * * * x\n', '')
        with mock.patch('kitlib.proc.spawn.run', side_effect=AssertionError('spawn.run')), \
                mock.patch('subprocess.run', return_value=done) as got:
            self.assertEqual(S.read_crontab(), '0 * * * * x\n')
        self.assertEqual(got.call_args[0][0], ['crontab', '-l'])
        self.assertEqual(got.call_args[1]['creationflags'], 0)


@posix_only('installs a real systemd user timer and crontab line')
@unittest.skipUnless(LIVE, 'set KIT_LINUX_LIVE_SCHEDULE=1 to install throwaway real jobs')
class Live(unittest.TestCase):
    """A throwaway job pm-kit-test-<pid> that touches a file each minute: installed to the real
    user systemd and to the real crontab, waited on, then removed."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='pm-kit-live-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.mark = os.path.join(self.dir, 'ran')
        self.name = 'test-%d' % os.getpid()
        self.addCleanup(S.remove, self.name)
        self.job = S.Job(self.name, [shutil.which('touch') or '/usr/bin/touch', self.mark], self.dir,
                         ('every', 1), 'pm-kit live test')

    def wait(self, seconds=150):
        end = time.time() + seconds
        while time.time() < end:
            if os.path.exists(self.mark):
                return True
            time.sleep(2)
        return False

    def test_systemd(self):
        if not S.has_systemd():
            self.skipTest('no user systemd here')
        S.install(self.job, S.SYSTEMD)
        task = S.find(self.name)
        self.assertTrue(S.same(task, self.job), task)
        self.assertTrue(self.wait(), 'the timer did not run the job')
        S.remove(self.name)
        self.assertIsNone(S.find(self.name))
        self.assertNotIn(S.unit(self.name), subprocess.run(
            ['systemctl', '--user', 'list-timers', '--all'], capture_output=True, text=True).stdout)

    def test_cron(self):
        if not S.has_cron():
            self.skipTest('no crontab here')
        before = S.read_crontab()
        S.install(self.job, S.CRON)
        task = S.find(self.name)
        self.assertEqual(task['backend'], S.CRON)
        self.assertTrue(S.same(task, self.job), task)
        self.assertTrue(self.wait(), 'cron did not run the job')
        S.remove(self.name)
        self.assertEqual(S.read_crontab().strip(), before.strip())


if __name__ == '__main__':
    unittest.main()
