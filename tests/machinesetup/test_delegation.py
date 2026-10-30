"""The --delegation mode (kitlib.machinesetup.delegation, machinesetup.run_delegation): the systemd
drop-in, the daemon reload and the kit-user marker, written under a test folder standing for /; and
its refusals (off Linux, WSL, root's own login, not root with --apply, a marker naming another
user). Off systemd (BootHook): the boot hook, its script and its entry (OpenRC local.d, else cron
@reboot), the script run once, the undo; and its refusals (no cgroup v2, runit or s6, no route, a
bad id or home, a link, a folder or binary that is not root's alone). The box and the root side's
file checks are fakes and the platform is patched, so each case runs on any host."""
import os
import re
import shutil
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from kitlib import machinesetup
from kitlib.machinesetup import delegation, linuxbox

from tests.machinesetup.test_linuxbox import CRON, OPENRC, WSL, Box

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Host(delegation.Host):
    """The live writes, under the test folder; the reload, the runs and each write's mode recorded.
    The ownership checks read fakes: every path is root's and 0755 (folders) or 0644 (files), and
    every binary root's 0755, but those in `odd` ({path: stat})."""

    def __init__(self, fail_reload=False):
        self.reloads, self.fail_reload = 0, fail_reload
        self.runs, self.modes, self.odd = [], {}, {}

    def reload(self):
        if self.fail_reload:
            raise RuntimeError('systemctl daemon-reload exit 1: boom')
        self.reloads += 1

    def write(self, path, text, mode=0o644):
        self.modes[path] = mode
        delegation.Host.write(self, path, text, mode)

    def lstat(self, path):
        if path in self.odd:
            return self.odd[path]
        if not os.path.lexists(path):
            return None
        return fake_stat(stat.S_IFDIR | 0o755 if os.path.isdir(path) else stat.S_IFREG | 0o644)

    def stat(self, path):
        return self.odd.get(path) or fake_stat(
            stat.S_IFDIR | 0o755 if path in ('/', '/bin', '/sbin', '/usr', '/usr/bin')
            else stat.S_IFREG | 0o755)

    def realpath(self, path):
        return path

    def run(self, argv):
        self.runs.append(list(argv))


def fake_stat(mode, uid=0):
    return SimpleNamespace(st_mode=mode, st_uid=uid)


TOOLS = {'git': '/usr/bin/git', 'tmux': '/usr/bin/tmux', 'mkdir': '/bin/mkdir',
         'chown': '/bin/chown', 'python3': '/usr/bin/python3', 'rc-update': '/sbin/rc-update'}
BINS = {'mkdir': '/bin/mkdir', 'chown': '/bin/chown', 'python3': '/usr/bin/python3',
        'tmux': '/usr/bin/tmux'}


class Delegation(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.host = Host()

    def ctx(self, **kw):
        kw.setdefault('me', 0)
        kw.setdefault('sudo', (1000, 'kit'))
        return delegation.DelegationContext(KIT, box=Box(**kw), host=self.host, root=self.root)

    def run_mode(self, ctx, apply, name='linux'):
        lines = []
        with mock.patch('kitlib.platform.NAME', name):
            code, _ = machinesetup.run_delegation(ctx, apply=apply, out=lines.append)
        return code, '\n'.join(lines)

    def read(self, path):
        p = os.path.join(self.root, *path.lstrip('/').split('/'))
        if not os.path.exists(p):
            return None
        with open(p, newline='') as f:
            return f.read()

    def test_apply_writes_the_dropin_reloads_and_names_the_kit_user(self):
        code, out = self.run_mode(self.ctx(), apply=True)
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertEqual(self.read(delegation.DROPIN), delegation.DROPIN_TEXT)
        self.assertIn('Delegate=cpuset cpu io memory pids\n', self.read(delegation.DROPIN))
        self.assertEqual(self.read(linuxbox.MARKER), '1000 kit\n')
        self.assertEqual(self.host.reloads, 1)
        self.assertIn('sign out and in again', out)

    def test_the_folders_it_makes_are_0755_whatever_the_umask(self):
        real = os.chmod
        with mock.patch.object(delegation.os, 'chmod', side_effect=real) as chmod:
            self.run_mode(self.ctx(), apply=True)
        folders = {c[0][0] for c in chmod.call_args_list if c[0][1] == 0o755}
        here = lambda *p: os.path.join(self.root, *p)                     # noqa: E731
        self.assertEqual(folders, {here('etc'), here('etc', 'pm-kit'), here('etc', 'systemd'),
                                   here('etc', 'systemd', 'system'),
                                   here('etc', 'systemd', 'system', 'user@.service.d')})

    def test_a_second_run_has_nothing_to_do(self):
        self.run_mode(self.ctx(), apply=True)
        code, out = self.run_mode(self.ctx(), apply=True)
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertIn('delegation: nothing to do', out)
        self.assertEqual(self.host.reloads, 1)

    def test_a_dry_run_changes_nothing(self):
        code, out = self.run_mode(self.ctx(), apply=False)
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertIn('write %s' % delegation.DROPIN, out)
        self.assertIn('write %s: kit (uid 1000)' % linuxbox.MARKER, out)
        self.assertIsNone(self.read(delegation.DROPIN))
        self.assertIsNone(self.read(linuxbox.MARKER))
        self.assertEqual(self.host.reloads, 0)

    def test_a_dry_run_as_the_kit_user_names_the_sudo_command(self):
        code, out = self.run_mode(self.ctx(me=1000, sudo=None), apply=False)
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertIn('not root: --apply is refused here; run: sudo python3 ', out)
        self.assertIn('write %s: kit (uid 1000)' % linuxbox.MARKER, out)

    def refused(self, ctx, apply=True, name='linux'):
        code, out = self.run_mode(ctx, apply=apply, name=name)
        self.assertEqual(code, machinesetup.EXIT_REFUSED, out)
        self.assertIsNone(self.read(delegation.DROPIN))
        self.assertIsNone(self.read(linuxbox.MARKER))
        self.assertEqual(self.host.reloads, 0)
        return out

    def test_refusals(self):
        self.assertIn('--delegation is a Linux step (this is windows)',
                      self.refused(self.ctx(), name='windows'))
        self.assertIn('this is WSL', self.refused(self.ctx(release=WSL)))
        self.assertIn('no OpenRC local.d and no /etc/cron.d',
                      self.refused(self.ctx(has_systemd=False)))
        self.assertIn('run it through sudo from the kit user', self.refused(self.ctx(sudo=None)))
        self.assertIn('run it through sudo from the kit user', self.refused(self.ctx(sudo=(0, 'root'))))
        self.assertIn('not root: %s and %s are root-owned' % (delegation.DROPIN, linuxbox.MARKER),
                      self.refused(self.ctx(me=1000, sudo=None)))

    def test_a_marker_naming_another_user_is_refused(self):
        p = os.path.join(self.root, 'etc', 'pm-kit')
        os.makedirs(p)
        with open(os.path.join(p, 'kit-user'), 'w') as f:
            f.write('1001 ann\n')
        code, out = self.run_mode(self.ctx(), apply=True)
        self.assertEqual(code, machinesetup.EXIT_REFUSED, out)
        self.assertIn("this box's kit user is ann (uid 1001", out)
        self.assertIsNone(self.read(delegation.DROPIN))

    def test_a_failed_reload_stops_before_the_marker(self):
        self.host = Host(fail_reload=True)
        code, out = self.run_mode(self.ctx(), apply=True)
        self.assertEqual(code, machinesetup.EXIT_REFUSED, out)
        self.assertIn('STOPPED at: systemctl daemon-reload', out)
        self.assertIn('boom', out)
        self.assertIn('already done by this run (1): write %s' % delegation.DROPIN, out)
        self.assertIsNone(self.read(linuxbox.MARKER))


class BootHook(unittest.TestCase):
    """No systemd: the boot hook (delegation.boot_route, the boot-hook changes of survey)."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.host = Host()

    def ctx(self, paths=OPENRC, **kw):
        kw.setdefault('me', 0)
        kw.setdefault('sudo', (1000, 'kit'))
        kw.setdefault('tools', dict(TOOLS))
        box = Box(has_systemd=False, paths=set(paths), **kw)
        return delegation.DelegationContext(KIT, box=box, host=self.host, root=self.root)

    def at(self, path):
        return os.path.join(self.root, *path.lstrip('/').split('/'))

    def read(self, path):
        p = self.at(path)
        if not os.path.exists(p):
            return None
        with open(p, newline='') as f:
            return f.read()

    def run_mode(self, ctx, apply=True):
        lines = []
        with mock.patch('kitlib.platform.NAME', 'linux'):
            code, _ = machinesetup.run_delegation(ctx, apply=apply, out=lines.append)
        return code, '\n'.join(lines)

    def refused(self, ctx, apply=True):
        code, out = self.run_mode(ctx, apply)
        self.assertEqual(code, machinesetup.EXIT_REFUSED, out)
        for p in (delegation.SCRIPT, delegation.LOCALD, delegation.CROND, linuxbox.MARKER):
            self.assertIsNone(self.read(p), p)
        self.assertEqual(self.host.runs, [])
        return out

    def test_openrc_writes_the_script_and_its_local_d_entry_enables_local_and_runs_it(self):
        code, out = self.run_mode(self.ctx())
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertEqual(self.read(delegation.SCRIPT),
                         delegation.boot_script(1000, 1000, '/srv/kit', BINS))
        self.assertEqual(self.read(delegation.LOCALD), delegation.LOCALD_TEXT)
        self.assertTrue(delegation.LOCALD_TEXT.startswith('#!/bin/sh\n'))
        self.assertTrue(delegation.LOCALD_TEXT.endswith('\nexec /etc/pm-kit/cgroup-boot\n'))
        self.assertIsNone(self.read(delegation.CROND))
        self.assertEqual(self.host.modes[self.at(delegation.SCRIPT)], 0o755)
        self.assertEqual(self.host.modes[self.at(delegation.LOCALD)], 0o755)
        self.assertEqual(self.read(linuxbox.MARKER), '1000 kit\n')
        # local in no runlevel: enabled, then the script runs once, so no reboot is needed
        self.assertEqual(self.host.runs, [['/sbin/rc-update', 'add', 'local', 'default'],
                                          [self.at(delegation.SCRIPT)]])
        self.assertIn('undo: %s' % delegation.UNDO_HOOK, out)
        self.assertEqual(delegation.UNDO_HOOK, 'sudo rm -f /etc/local.d/pm-kit-cgroup.start '
                                               '/etc/cron.d/pm-kit /etc/pm-kit/cgroup-boot')
        self.assertIn('sudo rc-update del local default', out)
        self.assertIn('tmux -L pm-kit', out)
        self.assertNotIn('sign out', out)

    def test_local_already_in_a_runlevel_is_left_alone(self):
        for level in ('sysinit', 'boot', 'default'):
            with self.subTest(level):
                self.host.runs = []
                code, out = self.run_mode(self.ctx(OPENRC | {'/etc/runlevels/%s/local' % level}))
                self.assertEqual(code, machinesetup.EXIT_OK, out)
                self.assertEqual(self.host.runs, [[self.at(delegation.SCRIPT)]])
                self.assertNotIn('rc-update', out)

    def test_without_openrc_it_is_a_cron_d_reboot_line(self):
        code, out = self.run_mode(self.ctx(CRON))
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertEqual(self.read(delegation.CROND), delegation.CROND_TEXT)
        self.assertTrue(delegation.CROND_TEXT.endswith('\n@reboot root /etc/pm-kit/cgroup-boot\n'))
        self.assertEqual(self.host.modes[self.at(delegation.CROND)], 0o644)
        self.assertEqual(self.host.modes[self.at(delegation.SCRIPT)], 0o755)
        self.assertIsNone(self.read(delegation.LOCALD))
        self.assertEqual(self.host.runs, [[self.at(delegation.SCRIPT)]])
        self.assertIn('cron daemon', out)
        self.assertNotIn('rc-update', out)
        self.assertEqual(sorted(os.listdir(self.at('/etc'))), ['cron.d', 'pm-kit'])  # no crontab

    def up(self, events='populated 1\nfrozen 0\n', procs=('400', '401', '402'), exes=None,
           hold=True):
        """The anchor as the box shows it to root: cgroup.events, cgroup.procs, and per pid its
        binary and command line (400 the tmux server, 401 the HOLD window, 402 a shell)."""
        a = delegation.ANCHOR_CG
        files = {a + '/cgroup.events': events, a + '/cgroup.procs': '\n'.join(procs) + '\n'}
        if events is None:
            files = {}
        hold_cmd = '\0'.join(['/usr/bin/python3'] + list(delegation.HOLD_ARGS)) + '\0'
        files.update({'/proc/400/cmdline': 'tmux: server\0', '/proc/402/cmdline': '-bash\0',
                      '/proc/401/cmdline': hold_cmd if hold else '/bin/sh\0'})
        exes = {'400': '/usr/bin/tmux', '401': '/usr/bin/python3', '402': '/bin/bash'} \
            if exes is None else exes
        return dict(files=files, exes=exes)

    def test_a_second_run_has_nothing_to_do(self):
        self.run_mode(self.ctx())
        runs = len(self.host.runs)
        code, out = self.run_mode(self.ctx(OPENRC | {'/etc/runlevels/default/local',
                                                     delegation.CGROUP}, **self.up()))
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertIn('delegation: nothing to do', out)
        self.assertEqual(len(self.host.runs), runs)
        # a tmux upgraded under the running server still counts as it
        code, out = self.run_mode(self.ctx(OPENRC | {'/etc/runlevels/default/local'}, **self.up(
            exes={'400': '/usr/bin/tmux (deleted)', '401': '/usr/bin/python3'})))
        self.assertIn('delegation: nothing to do', out)

    def test_the_hook_there_but_no_group_runs_the_script_again(self):
        self.run_mode(self.ctx())
        self.host.runs = []
        code, out = self.run_mode(self.ctx(OPENRC | {'/etc/runlevels/default/local'}))
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertEqual(self.host.runs, [[self.at(delegation.SCRIPT)]])
        self.assertIn('run %s again: the kit\'s cgroup /sys/fs/cgroup/pm-kit/anchor is not there'
                      % delegation.SCRIPT, out)

    def test_the_kit_s_server_or_its_anchor_session_gone_runs_the_script_again(self):
        self.run_mode(self.ctx())
        cases = ((dict(events='populated 0\nfrozen 0\n', procs=()), 'populated 0'),
                 (dict(exes={'401': '/usr/bin/python3', '402': '/bin/bash'}),
                  'no tmux server runs in /sys/fs/cgroup/pm-kit/anchor'),
                 (dict(exes={'401': '/usr/bin/python3', '402': '/usr/bin/tmux-other'}),
                  'no tmux server runs in'),
                 (dict(hold=False), 'no pm-kit-anchor session holding it'),
                 (dict(procs=('400', '402')), 'no pm-kit-anchor session holding it'))
        for kw, want in cases:
            with self.subTest(want=want, kw=kw):
                self.host.runs = []
                code, out = self.run_mode(self.ctx(OPENRC | {'/etc/runlevels/default/local',
                                                             delegation.CGROUP}, **self.up(**kw)))
                self.assertEqual(code, machinesetup.EXIT_OK, out)
                self.assertEqual(self.host.runs, [[self.at(delegation.SCRIPT)]])
                self.assertIn('run %s again: ' % delegation.SCRIPT, out)
                self.assertIn(want, out)
                self.assertNotIn('delegation: nothing to do', out)

    def test_a_tmux_older_than_3_2_is_refused(self):
        for said in ('tmux 3.1c', 'tmux 3.0a', 'tmux 2.9', 'tmux master', None):
            with self.subTest(said=said):
                out = self.refused(self.ctx(tmux_said=said))
                self.assertIn('/usr/bin/tmux is ', out)
                self.assertIn('the boot hook needs tmux 3.2 or later', out)
                self.assertIn('new-session -e', out)
        for said in ('tmux 3.2', 'tmux 3.3a', 'tmux next-3.5', 'tmux 10.0'):
            with self.subTest(said=said):
                self.host.runs = []
                code, out = self.run_mode(self.ctx(tmux_said=said))
                self.assertEqual(code, machinesetup.EXIT_OK, out)

    def test_a_dry_run_changes_nothing_and_as_the_kit_user_names_the_sudo_command(self):
        code, out = self.run_mode(self.ctx(), apply=False)
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertIn('write %s' % delegation.SCRIPT, out)
        self.assertIn('write %s' % delegation.LOCALD, out)
        self.assertIsNone(self.read(delegation.SCRIPT))
        self.assertEqual(self.host.runs, [])
        code, out = self.run_mode(self.ctx(me=1000, sudo=None), apply=False)
        self.assertEqual(code, machinesetup.EXIT_OK, out)
        self.assertIn('not root: --apply is refused here; run: sudo python3 ', out)
        self.assertIn('not root: %s, %s and %s are root-owned'
                      % (delegation.SCRIPT, delegation.LOCALD, linuxbox.MARKER),
                      self.refused(self.ctx(me=1000, sudo=None)))

    def test_the_script_reads_no_environment_and_bakes_in_the_kit_user(self):
        text = delegation.boot_script(1000, 1001, '/srv/kit', BINS)
        self.assertTrue(text.startswith('#!/bin/sh\n'))
        # every $ is one of its own variables: nothing from the environment
        used = set(re.findall(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)', text))
        self.assertTrue(used, text)
        self.assertEqual(used - set(delegation.SCRIPT_VARS), set())
        self.assertIn('/bin/chown 1000:1001 "$d"', text)
        self.assertIn('/bin/mkdir -p "$g/anchor"', text)
        self.assertIn('/sys/kernel/cgroup/delegate', text)
        self.assertIn('echo 0 >"$g/anchor/cgroup.procs"', text)
        self.assertIn("exec /usr/bin/python3 -I -S -c '%s' 1000 1001 '/srv/kit' /usr/bin/tmux "
                      "/usr/bin/python3 </dev/null" % delegation.DROP, text)
        for c in ('cpuset', 'cpu', 'memory', 'pids'):
            self.assertIn('+$c', text)
            self.assertIn(c, text)
        self.assertNotIn('memory.max', text)       # never the group's own limit files
        self.assertNotIn("'", delegation.DROP)
        compile(delegation.DROP, 'DROP', 'exec')
        for step in ('os.setgroups([])', 'os.setresgid(g,g,g)', 'os.setresuid(u,u,u)',
                     'os.getresuid()', 'os.setuid(0)', 'os.execve(t,'):
            self.assertIn(step, delegation.DROP)
        self.assertLess(delegation.DROP.index('os.setgroups'), delegation.DROP.index('os.setresgid'))
        self.assertLess(delegation.DROP.index('os.setresgid'), delegation.DROP.index('os.setresuid'))
        self.assertLess(delegation.DROP.index('os.setuid(0)'), delegation.DROP.index('os.execve'))
        self.assertIn('"PATH":"/usr/bin:/bin"', delegation.DROP)
        self.assertIn('"new-session","-d","-s","pm-kit-anchor"', delegation.DROP)

    def test_the_server_outlives_its_sessions_and_one_from_a_shell_is_never_used(self):
        drop = delegation.DROP
        compile(drop, 'DROP', 'exec')
        self.assertNotIn('$', drop)
        # the anchor session holds HOLD, not a shell, and the server outlives its last session
        self.assertIn('"pm-kit-anchor",p,"-I","-S","-c","import signal\\n', drop)
        self.assertIn('";","set-option","-s","exit-empty","off"', drop)
        hold = delegation.HOLD
        compile(hold, 'HOLD', 'exec')
        self.assertNotIn('"', hold)
        self.assertIn('signal.signal(signal.SIGINT,signal.SIG_IGN)', hold)
        self.assertEqual(delegation.HOLD_ARGS, ('-I', '-S', '-c', hold))
        # a server already on the kit's socket but outside the anchor is refused, not reused
        self.assertIn('k.connect("/tmp/tmux-%d/pm-kit"%u)', drop)
        self.assertIn('"/sys/fs/cgroup/pm-kit/anchor/cgroup.procs"', drop)
        self.assertIn('if up and not inside():\n    sys.exit(', drop)
        self.assertIn('tmux -L pm-kit kill-server', drop)
        self.assertLess(drop.index('if up and not inside()'), drop.index('"has-session"'))
        self.assertLess(drop.index('"has-session"'), drop.index('os.execve'))
        # the way in never starts a server (attach) and never works in HOLD's window
        self.assertEqual(delegation.ATTACH, 'tmux -L pm-kit attach -t pm-kit-anchor \\; new-window')

    def test_the_notes_name_the_way_back_when_the_server_has_ended(self):
        code, out = self.run_mode(self.ctx())
        self.assertIn(delegation.ATTACH, out)
        self.assertIn('if it is ended (kill-server), run this step again with sudo, or reboot', out)
        self.assertIn('a `tmux -L pm-kit` started from a shell is not contained', out)

    def test_a_bad_id_or_home_is_refused(self):
        for acct, want in (((1000, "/srv/o'neil"), 'home'), ((1000, 'srv/kit'), 'home'),
                           ((1000, '/srv/a\nb'), 'home'), ((1000, '/srv/"x"'), 'home'),
                           (None, 'no account'), ((-1, '/srv/kit'), 'gid')):
            with self.subTest(acct=acct):
                self.assertIn(want, self.refused(self.ctx(accounts={1000: acct})))
        for args in (('1000; rm', 1000, '/srv/kit'), (1000, True, '/srv/kit'),
                     (0, 0, '/root'), (1000, 1000, "/x'y")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                delegation.boot_script(*args, tools=BINS)

    def test_routes_that_are_refused(self):
        self.assertIn('no cgroup v2', self.refused(self.ctx(cg2=False)))
        for init in ('runit', 'runsvdir', 's6-svscan'):
            with self.subTest(init):
                self.assertIn(init, self.refused(self.ctx(OPENRC | CRON, init_name=init)))
        for paths in (set(), {'/run/openrc'}, {'/etc/local.d'}):
            with self.subTest(paths=paths):
                out = self.refused(self.ctx(paths))
                self.assertIn('no OpenRC local.d and no /etc/cron.d', out)
                self.assertIn('stays at the lower tier', out)

    def test_a_link_or_a_path_not_root_s_alone_is_refused(self):
        link = fake_stat(stat.S_IFLNK | 0o777)
        cases = (('/etc/pm-kit', link, 'a link'),
                 ('/etc/local.d', fake_stat(stat.S_IFDIR | 0o755, uid=1000), 'not owned by root'),
                 ('/etc/local.d', fake_stat(stat.S_IFDIR | 0o775), 'writable'),
                 ('/etc', fake_stat(stat.S_IFDIR | 0o757), 'writable'),
                 (delegation.SCRIPT, link, 'a link'),
                 (delegation.LOCALD, fake_stat(stat.S_IFREG | 0o755, uid=1000), 'not owned by root'),
                 (linuxbox.MARKER, link, 'a link'))
        for path, st, want in cases:
            with self.subTest(path=path, want=want):
                self.host.odd = {self.at(path): st}
                out = self.refused(self.ctx())
                self.assertIn(want, out)
                self.assertIn(path, out)

    def test_a_binary_not_root_s_alone_is_refused(self):
        cases = (({'/usr/bin/tmux': fake_stat(stat.S_IFREG | 0o755, uid=1000)}, {}, 'tmux'),
                 ({'/usr/bin/python3': fake_stat(stat.S_IFREG | 0o775)}, {}, 'python3'),
                 ({'/usr/bin': fake_stat(stat.S_IFDIR | 0o777)}, {}, '/usr/bin'),
                 ({'/bin/sh': fake_stat(stat.S_IFREG | 0o757)}, {}, '/bin/sh'),
                 ({}, {'tmux': None}, 'no tmux'),
                 ({}, {'chown': 'bin/chown'}, 'chown'),
                 ({}, {'mkdir': '/bin/mk dir'}, 'mkdir'),
                 ({}, {'rc-update': None}, 'rc-update'))
        for odd, tools, want in cases:
            with self.subTest(want=want):
                self.host.odd = odd
                t = dict(TOOLS)
                t.update(tools)
                self.assertIn(want, self.refused(self.ctx(tools={k: v for k, v in t.items() if v})))


if __name__ == '__main__':
    unittest.main()
