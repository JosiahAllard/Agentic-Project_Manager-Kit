"""Containment on Linux (kitlib.platform.linux.contain; docs/linux-support-plan.md section 2.3,
slice 5): kitlib.proc.job.Job on a cgroup scope (S1, S2), a user and PID namespace (W1) or on the
weak tier (W2).

On every platform: which cgroups a job may kill (only one it made, checked before any write), the
tier's report line, the scope prefix, a nested owner's tier, the probe's order (a scope, else W1
when its trial works, else W2 with why not), systemd-run's --expand-environment asked of its
--help, the weak tier's walk and kill on a faked /proc where a pid is reused between reads (never
signalled), and W1's members, kill and ready-line checks on a faked /proc.

On Linux, on real processes: a setsid grandchild ends with its job; memory.peak is read; the pin
is the cgroup's; an owner killed by SIGKILL takes its job's tree and its watchdog with it; a job
that ended cleanly leaves no watchdog; a released job's daemon runs on and leftovers() takes it out
of the job; a job made inside a job is nested in it; a systemd-run that runs the command outside
any scope is caught; an owner SIGKILLed before go, a start aborted before go and a forged ready
line each run no command and leave no stub, watchdog or unit; the forced weak tier kills a setsid
tree; a W1 job runs its command in a namespace of its own with a fresh /proc and its own ids,
kills a setsid tree through its pid 1, ends with its owner or its starter, spares leftovers, and
refuses a forged ready line. Every scope a test makes is named
`pmkit-containment-...-<this pid>-...` and is checked gone after each test."""
import ast
import importlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from unittest import mock

from tests.platforms import posix_only

PREFIX = 'pmkit-containment'


def contain():
    return importlib.import_module('kitlib.platform.linux.contain')


def jobmod():
    return importlib.import_module('kitlib.proc.job')


def bare(cls, **attrs):
    """A job object with `attrs` and no start: what the kill path checks, without a process."""
    j = object.__new__(cls)
    j.__dict__.update(dict(closed=False, kill_on_close=True, _k=None, _g=None, _child=None,
                           wd=None, p=None), **attrs)
    return j


TOP = '/user.slice/user-1000.slice/user@1000.service/app.slice/pmkit-job-41-2-0a1b2c.scope'


class KillPath(unittest.TestCase):
    def test_a_job_cgroup_is_a_kit_job_scope_or_a_job_nested_in_one(self):
        c = contain()
        for cg in (TOP, TOP + '/j-50-1-ff00aa', TOP + '/j-50-1-ff00aa/j-60-3-0b0b0b',
                   '/pmkit-containment-job-1-1-ab.scope'):
            with self.subTest(cg=cg):
                self.assertTrue(c.is_job_cgroup(cg))
        for cg in ('', '/', 'pmkit-job-41-2-0a1b2c.scope', TOP + '/', TOP + '/main',
                   TOP + '/w', TOP + '/j-50-1-ff00aa/main', TOP + '/../x.scope',
                   TOP + '/j-50-1-ff00aa/..', TOP + '//j-50-1-ff00aa',
                   '/user.slice/user-1000.slice/user@1000.service/app.slice/other.scope',
                   '/user.slice/pmkit-leftovers-41-2-0a1b2c.scope',
                   '/user.slice/pmkit-job-41-2-0a1b2c.scope.d', '/x/j-50-1-ff00aa', None, 7,
                   TOP + '\0'):
            with self.subTest(cg=cg):
                self.assertFalse(c.is_job_cgroup(cg))

    def test_a_job_owns_only_the_cgroup_named_as_it_named_it(self):
        c = contain()
        self.assertTrue(c.owned(TOP, 'pmkit-job-41-2-0a1b2c.scope'))
        self.assertFalse(c.owned(TOP, 'pmkit-job-41-2-0a1b2d.scope'))
        self.assertFalse(c.owned(TOP + '/main', 'main'))

    def test_the_kill_is_written_only_to_the_job_s_own_cgroup(self):
        c = contain()
        name = 'pmkit-job-41-2-0a1b2c.scope'
        with mock.patch.object(c, '_write') as write:
            self.assertTrue(bare(c.ScopeJob, cg=TOP, name=name, parent=None)._kill_all())
            write.assert_called_once_with(c.CGROOT + TOP + '/cgroup.kill', '1')
        refused = (
            dict(cg='/user.slice/user-1000.slice/user@1000.service/app.slice', name=name,
                 parent=None),                                      # its parent
            dict(cg=TOP + '/main', name='main', parent=None),       # its own leaf
            dict(cg=TOP, name='pmkit-job-41-2-ffffff.scope', parent=None),   # another job's
            dict(cg=TOP + '/j-50-1-ff00aa', name='j-50-1-ff00aa',
                 parent=TOP + '/j-1-1-aaaaaa'),                     # not under its parent
            dict(cg='/user.slice/../' + name, name=name, parent=None))
        for attrs in refused:
            with self.subTest(**attrs), mock.patch.object(c, '_write') as write:
                with self.assertRaisesRegex(jobmod().JobError, 'refused to kill'):
                    bare(c.ScopeJob, **attrs)._kill_all()
                write.assert_not_called()

    def test_a_job_with_no_cgroup_kills_nothing(self):
        c = contain()
        with mock.patch.object(c, '_write') as write:
            self.assertFalse(bare(c.ScopeJob, cg=None, name=None, parent=None)._kill_all())
            write.assert_not_called()


class Tiers(unittest.TestCase):
    def test_each_tier_s_line_names_it_and_what_it_lacks(self):
        c = contain()
        self.assertEqual(c.Tier('S1').text, 'containment: strong (S1)')
        s2 = c.Tier('S2', [c.S2_LACK]).text
        self.assertTrue(s2.startswith('containment: partial (S2; lacks cpu pinning by the cgroup'),
                        s2)
        w2 = c.Tier('W2', c.W2_LACKS, why='no systemd-run', w1='W1 available, not built').text
        self.assertTrue(w2.startswith('containment: weak (W2; no cgroup scope: no systemd-run; '
                                      'lacks a guaranteed kill'), w2)
        for lack in c.W2_LACKS:
            self.assertIn(lack, w2)
        self.assertTrue(w2.endswith('; W1 available, not built)'), w2)
        self.assertTrue(c.Tier('S2').scoped and c.Tier('S1').scoped)
        self.assertFalse(c.Tier('W2').scoped)

    def test_the_scope_prefix_is_a_plain_name_or_pmkit(self):
        c = contain()
        for value, want in (('pmkit-containment', 'pmkit-containment'), ('a/b', 'pmkit'),
                            ('', 'pmkit'), ('x' * 41, 'pmkit'), ('a b', 'pmkit')):
            with self.subTest(value=value), mock.patch.dict(os.environ, {c.PREFIX_ENV: value}):
                self.assertEqual(c.prefix(), want)

    def test_the_w1_line_names_it_why_there_is_no_scope_and_what_it_lacks(self):
        c = contain()
        w1 = c.Tier('W1', c.W1_LACKS, why='no systemd-run').text
        self.assertTrue(w1.startswith('containment: partial (W1; no cgroup scope: no systemd-run; '
                                      'lacks cpu pinning by the cgroup'), w1)
        self.assertIn(c.PEAK_LACK, w1)
        self.assertFalse(c.Tier('W1').scoped)

    def test_a_process_in_a_kit_job_reads_its_tier_from_that_job_and_runs_no_trial(self):
        c = contain()
        with mock.patch.object(c, 'parent_job', return_value=TOP), \
                mock.patch.object(c, 'trial', side_effect=AssertionError('a trial ran')), \
                mock.patch.object(c, 'w1_trial', side_effect=AssertionError('a W1 trial ran')), \
                mock.patch.object(c, 'nested_tier', return_value=c.Tier('S1', parent=TOP)):
            self.assertEqual((c._probe().name, c._probe().parent), ('S1', TOP))
        for no, want in (('x', 'W2'), (None, 'W1')):
            with self.subTest(w1=no), mock.patch.object(c, 'parent_job', return_value=TOP), \
                    mock.patch.object(c, 'trial', side_effect=AssertionError('a trial ran')), \
                    mock.patch.object(c, 'w1_trial', return_value=no), \
                    mock.patch.object(c, 'nested_tier', return_value=None):
                tier = c._probe()
            # never a scope of its own, which would take its jobs out of the job it runs in
            self.assertEqual(tier.name, want)
            self.assertIn('it runs in kit job %s' % TOP, tier.text)

    def test_the_probe_takes_a_scope_then_w1_then_w2(self):
        c = contain()
        with mock.patch.object(c, 'parent_job', return_value=None), \
                mock.patch.object(c, 'trial', return_value=('S2', [c.S2_LACK], None)), \
                mock.patch.object(c, 'w1_trial', side_effect=AssertionError('a W1 trial ran')):
            self.assertEqual(c._probe().name, 'S2')
        with mock.patch.object(c, 'parent_job', return_value=None), \
                mock.patch.object(c, 'trial', return_value=(None, (), 'no systemd-run')), \
                mock.patch.object(c, 'w1_trial', return_value=None):
            tier = c._probe()
            self.assertEqual((tier.name, tier.lacks, tier.why),
                             ('W1', c.W1_LACKS, 'no systemd-run'))
        with mock.patch.object(c, 'parent_job', return_value=None), \
                mock.patch.object(c, 'trial', return_value=(None, (), 'no systemd-run')), \
                mock.patch.object(c, 'w1_trial', return_value='user namespaces denied'):
            tier = c._probe()
            self.assertEqual(tier.name, 'W2')
            self.assertTrue(tier.text.endswith('; W1 unavailable (user namespaces denied))'),
                            tier.text)

    def test_a_probe_inside_a_w1_job_is_w2_with_no_scope_trial_and_no_nested_w1(self):
        c = contain()
        with mock.patch.dict(os.environ, {c.W1_ENV: '77'}), \
                mock.patch.object(c.lproc, 'pidns', return_value=77), \
                mock.patch.object(c, 'parent_job', side_effect=AssertionError('it looked')), \
                mock.patch.object(c, 'trial', side_effect=AssertionError('a scope trial ran')), \
                mock.patch.object(c, 'w1_trial', side_effect=AssertionError('a nested W1')):
            tier = c._probe()
            self.assertEqual((tier.name, tier.lacks), ('W2', c.W2_LACKS))
            self.assertEqual(tier.why, 'it runs in a W1 job, whose pids the user systemd '
                                       'cannot name')
            self.assertEqual(tier.w1, 'W1 not nested: its jobs stay in the namespace of the W1 '
                                      'job it runs in')
            self.assertIs(type(c.Job.__new__(c.Job, tier=tier)), c.WeakJob)
        for mark in ('78', ''):
            with mock.patch.dict(os.environ, {c.W1_ENV: mark}), \
                    mock.patch.object(c.lproc, 'pidns', return_value=77), \
                    mock.patch.object(c, 'parent_job', return_value=None), \
                    mock.patch.object(c, 'trial', return_value=('S2', [c.S2_LACK], None)):
                self.assertEqual(c._probe().name, 'S2', mark)

    def test_the_w1_starter_marks_its_job_with_the_env_name(self):
        c = contain()
        self.assertIn("os.environ['%s'] = str(os.stat('/proc/self/ns/pid').st_ino)" % c.W1_ENV,
                      c.NSTART)

    def test_a_w1_tier_makes_an_ns_job(self):
        c = contain()
        self.assertIs(type(c.Job.__new__(c.Job, tier=c.Tier('W1'))), c.NsJob)
        self.assertIs(type(c.Job.__new__(c.Job, tier=c.Tier('W2'))), c.WeakJob)
        self.assertIs(type(c.Job.__new__(c.Job, tier=c.Tier('S2'))), c.ScopeJob)

    def test_the_w1_starter_reads_its_ids_before_it_unshares(self):
        # after the unshare they read as the overflow id until the maps are written, and a map of
        # the overflow id is refused (EPERM): only the writer's own id may be mapped. W1 would
        # then never be used and the live classes would only skip, so this is the guard
        text = contain().NSTART
        compile(text, 'NSTART', 'exec')
        unshare = text.index('os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWPID)')
        self.assertLess(text.index('uid, gid = os.getuid(), os.getgid()'), unshare)
        self.assertEqual(text.count('os.getuid()'), 1)
        self.assertEqual(text.count('os.getgid()'), 1)

    def test_root_is_w2_with_no_w1_trial_and_a_user_still_tries_w1(self):
        # under sudo root has no user systemd, so it is unscoped; a W1 namespace maps root's own
        # id only, and git there could not stat the kit user's 0750 home (VM kit-linux-ref, T-324)
        c = contain()
        with mock.patch.object(c.os, 'geteuid', create=True, return_value=0),                 mock.patch.object(c, 'parent_job', return_value=None),                 mock.patch.object(c, 'trial', return_value=(None, (), 'no systemd-run')),                 mock.patch.object(c, 'w1_trial', side_effect=AssertionError('a W1 trial ran')):
            tier = c._probe()
        self.assertEqual((tier.name, tier.lacks, tier.why, tier.w1),
                         ('W2', c.W2_LACKS, 'no systemd-run', c.W1_ROOT))
        self.assertTrue(tier.text.endswith('; %s)' % c.W1_ROOT), tier.text)
        self.assertIs(type(c.Job.__new__(c.Job, tier=tier)), c.WeakJob)
        with mock.patch.object(c.os, 'geteuid', create=True, return_value=1000),                 mock.patch.object(c, 'parent_job', return_value=None),                 mock.patch.object(c, 'trial', return_value=(None, (), 'no systemd-run')),                 mock.patch.object(c, 'w1_trial', return_value=None) as w1:
            self.assertEqual(c._probe().name, 'W1')
        w1.assert_called_once_with()


BASE_JOB = 'pmkit-job-41-2-0a1b2c.scope'


class Delegated(unittest.TestCase):
    """The boot hook's route off systemd (delegated_group, the probe's order, the job in /pm-kit,
    sweep), on a cgroup tree faked in a test folder: CGROOT points there and the reads of this
    process's own ids and cgroup are patched, so each case runs on any host."""

    def setUp(self):
        self.c = c = contain()
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.group = self.root + '/pm-kit'
        os.makedirs(os.path.join(self.group, 'anchor'))
        self.ctl('cpuset cpu memory pids')
        open(os.path.join(self.group, 'cgroup.procs'), 'w').close()
        for patch in (mock.patch.object(c, 'CGROOT', self.root),
                      mock.patch.object(c, 'cgroup2', return_value=True),
                      mock.patch.object(c, 'SYSTEMD_DIR', os.path.join(self.root, 'no-systemd')),
                      mock.patch.object(c.os, 'getuid', create=True,
                                        return_value=os.lstat(self.group).st_uid)):
            patch.start()
            self.addCleanup(patch.stop)

    def ctl(self, text):
        with open(os.path.join(self.group, 'cgroup.subtree_control'), 'w') as f:
            f.write(text + '\n')

    def group_of(self, own='/pm-kit/anchor'):
        return self.c.delegated_group(own=own)

    def test_the_group_of_the_boot_hook_is_the_route(self):
        self.assertEqual(self.group_of(), ('/pm-kit', None))
        self.assertEqual(self.group_of('/pm-kit/%s/main' % BASE_JOB), ('/pm-kit', None))

    def test_no_cgroup2_or_no_group_on_a_systemd_box_is_no_route_and_no_message(self):
        with mock.patch.object(self.c, 'cgroup2', return_value=False):
            self.assertEqual(self.group_of(), (None, None))
        shutil.rmtree(self.group)
        os.makedirs(self.c.SYSTEMD_DIR)
        self.assertEqual(self.group_of(), (None, None))

    def test_no_group_says_the_boot_hook_did_not_run(self):
        shutil.rmtree(self.group)
        base, why = self.group_of()
        self.assertIsNone(base)
        self.assertIn('the boot hook did not run', why)
        self.assertIn('/etc/local.d/pm-kit-cgroup.start', why)
        self.assertIn('/etc/cron.d/pm-kit', why)

    def events(self, text):
        with open(os.path.join(self.group, 'anchor', 'cgroup.events'), 'w') as f:
            f.write(text)

    def test_a_process_outside_the_group_is_told_to_start_from_the_kit_s_tmux_server(self):
        self.events('populated 1\nfrozen 0\n')
        for own in ('/', '/user.slice', '/pm-kit', '/pm-kitx/anchor'):
            with self.subTest(own=own):
                base, why = self.group_of(own)
                self.assertIsNone(base)
                self.assertIn('start work from the kit\'s tmux server: tmux -L pm-kit attach -t '
                              'pm-kit-anchor \\; new-window', why)
                self.assertIn(own, why)

    def test_outside_the_group_with_the_anchor_empty_names_the_root_step_or_a_reboot(self):
        for state in ('populated 0\nfrozen 0\n', None):
            with self.subTest(state=state):
                if state is None:
                    os.remove(os.path.join(self.group, 'anchor', 'cgroup.events'))
                else:
                    self.events(state)
                base, why = self.group_of('/user.slice')
                self.assertIsNone(base)
                self.assertIn('the kit\'s tmux server has ended (/pm-kit/anchor is empty)', why)
                self.assertIn('run sudo python3 ', why)
                self.assertIn('machine_setup.py --delegation --apply again, or reboot', why)
                # never the advice to start the server by hand: that one would not be contained
                self.assertNotIn('tmux -L pm-kit', why)
                self.assertNotIn('start work from', why)

    def test_the_way_in_never_starts_a_server_nor_works_in_the_anchor_s_own_window(self):
        from kitlib.machinesetup import delegation
        self.assertEqual(self.c.ATTACH, delegation.ATTACH)
        self.assertTrue(self.c.ATTACH.startswith('tmux -L pm-kit attach -t pm-kit-anchor '))
        self.assertTrue(self.c.ATTACH.endswith(' \\; new-window'))
        self.assertNotIn('new-session', self.c.ATTACH)

    def test_a_group_that_is_not_this_user_s_is_refused(self):
        with mock.patch.object(self.c.os, 'getuid', create=True,
                               return_value=os.lstat(self.group).st_uid + 1):
            base, why = self.group_of()
        self.assertIsNone(base)
        self.assertIn('is owned by uid', why)
        real = os.lstat
        link = types.SimpleNamespace(st_mode=0o120777, st_uid=os.lstat(self.group).st_uid)
        with mock.patch.object(self.c.os, 'lstat',
                               side_effect=lambda p: link if p == self.group else real(p)):
            base, why = self.group_of()
        self.assertIsNone(base)
        self.assertIn('not a folder', why)
        shutil.rmtree(self.group)
        open(self.group, 'w').close()
        base, why = self.group_of()
        self.assertIsNone(base)
        self.assertIn('not a folder', why)

    def test_a_group_this_user_cannot_move_processes_into_is_refused(self):
        with mock.patch.object(self.c.os, 'access', return_value=False):
            base, why = self.group_of()
        self.assertIsNone(base)
        self.assertIn('cgroup.procs is not writable', why)

    def test_a_group_without_its_controllers_is_refused(self):
        self.ctl('cpu pids')
        base, why = self.group_of()
        self.assertIsNone(base)
        self.assertIn('cpuset, memory', why)
        os.remove(os.path.join(self.group, 'cgroup.subtree_control'))
        self.assertIsNone(self.group_of()[0])

    def test_the_probe_takes_the_group_before_a_systemd_scope(self):
        c = self.c
        seen = []

        def trial(base=None):
            seen.append(base)
            return ('S1', [], None) if base else (None, (), 'no systemd-run')

        with mock.patch.object(c, 'parent_job', return_value=None), \
                mock.patch.object(c, 'delegated_group', return_value=('/pm-kit', None)), \
                mock.patch.object(c, 'sweep') as sweep, \
                mock.patch.object(c, 'trial', side_effect=trial), \
                mock.patch.object(c, 'w1_trial', side_effect=AssertionError('a W1 trial ran')):
            tier = c._probe()
        self.assertEqual((tier.name, tier.base, tier.parent), ('S1', '/pm-kit', None))
        self.assertEqual(seen, ['/pm-kit'])
        sweep.assert_called_once_with('/pm-kit')
        self.assertIn('/pm-kit', tier.text)
        self.assertIs(type(c.Job.__new__(c.Job, tier=tier)), c.ScopeJob)

    def test_without_the_group_the_probe_says_why_at_the_lower_tier(self):
        c = self.c
        why = 'this process runs outside /pm-kit (in /): start work from ...'
        with mock.patch.object(c, 'parent_job', return_value=None), \
                mock.patch.object(c, 'delegated_group', return_value=(None, why)), \
                mock.patch.object(c, 'sweep', side_effect=AssertionError('a sweep ran')), \
                mock.patch.object(c, 'trial', return_value=(None, (), 'no systemd-run')), \
                mock.patch.object(c, 'w1_trial', return_value=None):
            tier = c._probe()
        self.assertEqual((tier.name, tier.why, tier.base), ('W1', why, None))
        self.assertIn(why, tier.text)
        # the group's trial fails: a systemd scope is still tried, and the group's why is shown
        with mock.patch.object(c, 'parent_job', return_value=None), \
                mock.patch.object(c, 'delegated_group', return_value=('/pm-kit', None)), \
                mock.patch.object(c, 'sweep'), \
                mock.patch.object(c, 'trial', side_effect=lambda base=None: (
                    None, (), 'the group trial failed' if base else 'no systemd-run')), \
                mock.patch.object(c, 'w1_trial', return_value='denied'):
            tier = c._probe()
        self.assertEqual((tier.name, tier.why), ('W2', 'the group trial failed'))
        # a systemd box: no group, no message, the scope as before
        with mock.patch.object(c, 'parent_job', return_value=None), \
                mock.patch.object(c, 'delegated_group', return_value=(None, None)), \
                mock.patch.object(c, 'trial', return_value=('S2', [c.S2_LACK], None)):
            tier = c._probe()
        self.assertEqual((tier.name, tier.base), ('S2', None))
        # a process in a kit job reads its tier there and never looks for the group
        with mock.patch.object(c, 'parent_job', return_value='/pm-kit/' + BASE_JOB), \
                mock.patch.object(c, 'delegated_group', side_effect=AssertionError('it looked')), \
                mock.patch.object(c, 'nested_tier', return_value=c.Tier('S1', parent='/pm-kit/'
                                                                         + BASE_JOB)):
            self.assertEqual(c._probe().parent, '/pm-kit/' + BASE_JOB)

    def scope(self, **attrs):
        base = dict(cg=None, name=None, unit=None, parent=None, base='/pm-kit', carried=None,
                    cpuset=True, mask=None, short=False, tier=self.c.Tier('S1', base='/pm-kit'),
                    note=None, pin=None, effective=None, _peak=0)
        base.update(attrs)
        return bare(self.c.ScopeJob, **base)

    def test_a_job_in_the_group_is_made_there_with_no_systemd_run(self):
        c = self.c
        job = self.scope()
        made = []
        p = types.SimpleNamespace(pid=4242)

        def ready(this, proc, fd, want=('s',)):
            return {'s': [str(p.pid), made[0]]}

        with mock.patch.object(c, '_tool', side_effect=AssertionError('systemd-run looked up')), \
                mock.patch.object(c, 'bus_env', side_effect=AssertionError('the user bus')), \
                mock.patch.object(c, 'carrier', side_effect=AssertionError('a carrier')), \
                mock.patch.object(c.ScopeJob, '_make_nested', lambda this, cg: made.append(cg)), \
                mock.patch.object(c.ScopeJob, '_ready', ready), \
                mock.patch.object(c, 'cgroup_of', side_effect=lambda pid: made[0] + '/main'), \
                mock.patch.object(c, '_popen', return_value=p) as popen:
            self.assertIs(job._begin(['sleep', '1'], {}, {}), p)
        self.addCleanup(c._close, job._g)
        self.addCleanup(c._close, job._k)
        self.assertEqual(len(made), 1)
        self.assertTrue(c.TOP.match(job.name), job.name)
        self.assertEqual(made[0], '/pm-kit/' + job.name)
        self.assertEqual(job.cg, made[0])
        argv = popen.call_args[0][0]
        self.assertEqual(argv[:6], [c.SH, '-c', c.STUB, 'pmkit-job', job.name, made[0]])
        self.assertEqual(argv[-2:], ['sleep', '1'])

    def test_a_group_job_s_kill_is_written_only_to_its_own_cgroup(self):
        c = self.c
        with mock.patch.object(c, '_write') as write:
            self.assertTrue(self.scope(cg='/pm-kit/' + BASE_JOB, name=BASE_JOB)._kill_all())
            write.assert_called_once_with(self.root + '/pm-kit/' + BASE_JOB + '/cgroup.kill', '1')
        for cg in ('/elsewhere/' + BASE_JOB, '/pm-kit/x/' + BASE_JOB, '/pm-kit'):
            with self.subTest(cg=cg), mock.patch.object(c, '_write') as write:
                with self.assertRaisesRegex(jobmod().JobError, 'refused to kill'):
                    self.scope(cg=cg, name=BASE_JOB)._kill_all()
                write.assert_not_called()

    def test_a_group_job_is_removed_at_close_and_pins_through_its_own_cpuset(self):
        c = self.c
        cg = '/pm-kit/' + BASE_JOB
        with mock.patch.object(c, 'wait_empty', return_value=True), \
                mock.patch.object(c, 'rmtree_cgroup') as rm:
            self.scope(cg=cg, name=BASE_JOB, kill_on_close=False).close()
        rm.assert_called_once_with(cg)
        wrote = {}
        with mock.patch.object(c, '_call', side_effect=AssertionError('systemctl')), \
                mock.patch.object(c, '_write', side_effect=lambda p, t: wrote.update({p: t})), \
                mock.patch.object(c, '_read', return_value='0\n'):
            self.assertTrue(self.scope(cg=cg, name=BASE_JOB)._set_pin(1))
        self.assertEqual(wrote, {self.root + cg + '/cpuset.cpus': '0'})

    def test_a_group_job_s_leftovers_go_to_a_sibling_in_the_group_not_to_systemd(self):
        c = self.c
        cg = '/pm-kit/' + BASE_JOB
        os.makedirs(self.root + cg)
        moved = []
        job = self.scope(cg=cg, name=BASE_JOB)
        with mock.patch.object(c, '_call', side_effect=AssertionError('busctl')), \
                mock.patch.object(c.ScopeJob, 'pids', return_value=[77]), \
                mock.patch.object(c.ScopeJob, 'contains',
                                  side_effect=lambda pid: 'out' if moved else 'in'), \
                mock.patch.object(c, '_write', side_effect=lambda p, t: moved.append((p, t))):
            self.assertEqual(job.leftovers(), [77])
        self.assertEqual(len(moved), 1)
        dest, pid = moved[0]
        self.assertEqual(pid, '77')
        name = os.path.basename(os.path.dirname(dest))
        self.assertTrue(c.LEFT.match(name), name)
        self.assertEqual(os.path.dirname(os.path.dirname(dest)), self.group)

    def test_the_group_s_trial_kills_and_empties_with_no_systemd(self):
        c = self.c
        cg = '/pm-kit/' + BASE_JOB
        os.makedirs(self.root + cg)
        p = mock.Mock(pid=4242, returncode=None)
        p.poll.return_value = 0

        def start(this, cmd, **kw):
            this.cg, this.name = cg, BASE_JOB
            return p

        def wait(seconds=None):
            p.returncode = -9

        p.wait.side_effect = wait
        with mock.patch.object(c, '_tool', side_effect=AssertionError('systemd-run looked up')), \
                mock.patch.object(c, 'bus_env', side_effect=AssertionError('the user bus')), \
                mock.patch.object(c, '_call', side_effect=AssertionError('systemctl')), \
                mock.patch.object(c.signal, 'SIGKILL', 9, create=True), \
                mock.patch.object(c.os, 'sched_getaffinity', create=True, return_value={0}), \
                mock.patch.object(c.hardware, 'all_mask', return_value=1), \
                mock.patch.object(c.ScopeJob, 'start', start), \
                mock.patch.object(c.ScopeJob, 'terminate', return_value=True), \
                mock.patch.object(c.ScopeJob, 'close') as close, \
                mock.patch.object(c, 'wait_empty', return_value=True), \
                mock.patch.object(c, '_read', return_value='0\n'), \
                mock.patch.object(c, '_read_int', return_value=5):
            self.assertEqual(c.trial('/pm-kit'), ('S1', [], None))
            close.assert_called_once_with()
            with mock.patch.object(c, 'wait_empty', return_value=False):
                name, _, why = c.trial('/pm-kit')
            self.assertIsNone(name)
            self.assertIn('still there', why)

            def refused(this, cmd, **kw):
                raise OSError('no room')

            with mock.patch.object(c.ScopeJob, 'start', refused):
                self.assertEqual(c.trial('/pm-kit'),
                                 (None, (), 'a trial job in /pm-kit could not be made (no room)'))

    def test_sweep_takes_only_empty_jobs_and_leftovers_whose_maker_has_gone(self):
        c = self.c
        proc = os.path.join(self.root, 'proc')
        os.makedirs(os.path.join(proc, '500'))
        names = {'pmkit-job-499-1-0a0a0a.scope': True,            # maker gone, empty: swept
                 'other-job-499-2-0b0b0b.scope': True,            # another prefix's: swept too
                 'pmkit-leftovers-499-3-0c0c0c': True,            # maker gone, empty: swept
                 'pmkit-job-499-4-0d0d0d.scope': False,           # maker gone, populated
                 'pmkit-job-500-5-0e0e0e.scope': False,           # its maker runs
                 'anchor': False, 'pmkit-job-x.scope': False, 'stray': False}
        for n, _swept in names.items():
            os.makedirs(os.path.join(self.group, n), exist_ok=True)
        with open(os.path.join(self.group, 'pmkit-job-499-4-0d0d0d.scope', 'cgroup.events'),
                  'w') as f:
            f.write('populated 1\nfrozen 0\n')
        with mock.patch.object(c, 'PROC', proc), \
                mock.patch.object(c, 'rmtree_cgroup') as rm:
            c.sweep('/pm-kit')
        self.assertEqual(sorted(a[0][0] for a in rm.call_args_list),
                         sorted('/pm-kit/' + n for n, swept in names.items() if swept))

    def test_the_tier_line_names_the_group(self):
        c = self.c
        self.assertEqual(c.Tier('S1', base='/pm-kit').text,
                         'containment: strong (S1; jobs in the kit\'s cgroup /pm-kit)')
        self.assertEqual(c.Tier('S1').text, 'containment: strong (S1)')


class KitTmux(unittest.TestCase):
    """kitlib.launcher.linuxwin: a window opens in the kit's tmux server (tmux -L pm-kit) when
    that server runs in the boot hook's group and is tmux 3.2 or later (new-session -e), else in
    the default server, the reason said; and when the kit's server refuses the window, in the
    default server too."""

    def setUp(self):
        self.w = importlib.import_module('kitlib.launcher.linuxwin')
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        self.sock = os.path.join(folder, 'pm-kit')
        self.addCleanup(self.w._opened.clear)

    def anchored(self, cg='/pm-kit/anchor', out='4242 3.4\n', code=0, sock=True, said='tmux 3.4\n'):
        if sock:
            open(self.sock, 'w').close()
        elif os.path.exists(self.sock):
            os.remove(self.sock)
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if argv[1:] == ['-V']:
                return types.SimpleNamespace(returncode=0, stdout=said, stderr='')
            return types.SimpleNamespace(returncode=code, stdout=out, stderr='')

        with mock.patch.object(self.w, 'anchor_socket', return_value=self.sock), \
                mock.patch.object(contain(), 'cgroup_of', return_value=cg) as of:
            got = self.w.anchor_check(run)
        return got, calls, of

    def test_the_kit_s_server_counts_only_when_it_runs_in_the_group(self):
        got, calls, of = self.anchored()
        self.assertEqual(got, (True, None))
        self.assertEqual(calls, [['tmux', '-L', 'pm-kit', 'display-message', '-p',
                                  '#{pid} #{version}']])
        of.assert_called_once_with(4242)
        for kw in (dict(cg='/user.slice/x.scope'), dict(cg='/pm-kit'), dict(cg=None),
                   dict(code=1), dict(out='no server\n')):
            with self.subTest(**kw):
                self.assertEqual(self.anchored(**kw)[0], (False, None))
        got, calls, _ = self.anchored(sock=False)
        self.assertEqual((got, calls), ((False, None), []))

    def test_a_kit_s_server_older_than_3_2_is_not_used_and_says_why(self):
        for out in ('4242 3.1c\n', '4242 3.0a\n', '4242 2.9\n'):
            with self.subTest(out=out):
                (ok, why), _, _ = self.anchored(out=out)
                self.assertFalse(ok)
                self.assertIn('needs tmux 3.2 or later (new-session -e)', why)
                self.assertIn('tmux %s' % out.split()[1][:3], why)
        for out in ('4242 3.2\n', '4242 next-3.5\n', '4242 3.3a\n'):
            with self.subTest(out=out):
                self.assertEqual(self.anchored(out=out)[0], (True, None))
        # a server that names no version: the binary is asked
        got, calls, _ = self.anchored(out='4242\n', said='tmux 3.1c\n')
        self.assertFalse(got[0])
        self.assertEqual(calls[-1], ['tmux', '-V'])
        self.assertEqual(self.anchored(out='4242 \n', said='tmux 3.4\n')[0], (True, None))
        self.assertFalse(self.anchored(out='4242\n', said='')[0][0])
        self.assertEqual([self.w.tmux_version(t) for t in ('tmux 3.1c', 'tmux next-3.4', 'master',
                                                            None)],
                         [(3, 1), (3, 4), None, None])

    def test_an_anchored_window_opens_in_the_kit_s_server_with_the_caller_s_path(self):
        w = self.w
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(w, 'anchor_check', return_value=(True, None)), \
                mock.patch.dict(os.environ, {'PATH': '/srv/kit/.local/bin:/usr/bin',
                                             'LANG': 'C.UTF-8'}):
            w.open_window('echo hi', 'pm-a1', run=run)
            said = w.opened_in('pm-a1')
        argv = calls[0]
        self.assertEqual(len(calls), 1)
        self.assertEqual(argv[:5], ['tmux', '-L', 'pm-kit', 'new-session', '-d'])
        self.assertIn('PATH=/srv/kit/.local/bin:/usr/bin', argv)
        self.assertEqual(argv[argv.index('PATH=/srv/kit/.local/bin:/usr/bin') - 1], '-e')
        self.assertEqual(argv[argv.index('-s') + 1:], ['pm-a1', '/bin/bash', '-c', 'echo hi'])
        self.assertIn('tmux -L pm-kit attach -t', said)
        with mock.patch.object(w, 'anchor_check', return_value=(False, None)):
            self.assertEqual(w.tmux_argv('pm-a1', 'x')[:2], ['tmux', 'new-session'])
            self.assertNotIn('-L', w.opened_in('pm-a1'))

    def test_an_old_kit_s_server_opens_the_window_in_the_default_server_and_says_why(self):
        w = self.w
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        why = "the kit's tmux server is tmux 3.1, and a window there needs tmux 3.2 or later"
        with mock.patch.object(w, 'anchor_check', return_value=(False, why)):
            w.open_window('echo hi', 'pm-a1', run=run)
        self.assertEqual(calls, [['tmux', 'new-session', '-d', '-s', 'pm-a1', '/bin/bash', '-c',
                                  'echo hi']])
        with mock.patch.object(w, 'anchor_check', side_effect=AssertionError('asked again')):
            said = w.opened_in('pm-a1')
        self.assertIn("tmux session 'pm-a1' (tmux attach -t 'pm-a1')", said)
        self.assertIn('not contained there: ' + why, said)

    def test_a_window_the_kit_s_server_refuses_opens_in_the_default_server(self):
        w = self.w
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if '-L' in argv:
                return types.SimpleNamespace(returncode=1, stdout='', stderr='unknown flag -e\n')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch.object(w, 'anchor_check', return_value=(True, None)):
            w.open_window('echo hi', 'pm-a1', run=run)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:3], ['tmux', '-L', 'pm-kit'])
        self.assertEqual(calls[1], ['tmux', 'new-session', '-d', '-s', 'pm-a1', '/bin/bash', '-c',
                                    'echo hi'])
        said = w.opened_in('pm-a1')
        self.assertNotIn('-L', said)
        self.assertIn("the kit's tmux server refused the window (tmux exit 1: unknown flag -e)",
                      said)
        # a name the kit's server already has is refused, never opened elsewhere
        calls[:] = []

        def dup(argv, **kw):
            calls.append(argv)
            return types.SimpleNamespace(returncode=1, stdout='', stderr='duplicate session: pm-a1')

        with mock.patch.object(w, 'anchor_check', return_value=(True, None)), \
                self.assertRaises(importlib.import_module('kitlib.launcher.settings').LaunchError):
            w.open_window('echo hi', 'pm-a1', run=dup)
        self.assertEqual(len(calls), 1)


class Expand(unittest.TestCase):
    def test_the_no_expand_switch_is_passed_only_when_systemd_run_names_it(self):
        c = contain()
        for out, want in ((b'  --expand-environment=BOOL  Control expansion\n',
                           ['--expand-environment=no']),         # systemd 254 on
                          (b'  --scope  Run this as scope rather than service\n', []),
                          (b'', [])):
            with self.subTest(out=out), mock.patch.dict(c._no_expand, clear=True), \
                    mock.patch.object(c.subprocess, 'run', return_value=subprocess.
                                      CompletedProcess([], 0, out, b'')) as run:
                self.assertEqual(c.no_expand('/usr/bin/systemd-run'), want)
                self.assertEqual(c.no_expand('/usr/bin/systemd-run'), want)
                run.assert_called_once()                         # asked once for each path
        with mock.patch.dict(c._no_expand, clear=True), \
                mock.patch.object(c.subprocess, 'run', side_effect=OSError('no such file')):
            self.assertEqual(c.no_expand('/x/systemd-run'), [])


class FakeProc:
    """pstat's, tree's and marked's /proc: {pid: (ppid, starttime, state)}. then[(pid, n)] runs
    right after pid's nth stat read (that read sees the old process): a pid that ends and is
    reused between two reads."""
    BOOT = 0x5eed

    def __init__(self, procs):
        self.procs, self.reads, self.then = dict(procs), {}, {}

    def ct(self, start):
        return contain().lproc._ctime(start, self.BOOT)

    def stat(self, pid):
        if pid not in self.procs:
            raise FileNotFoundError(pid)
        ppid, start, state = self.procs[pid]
        data = ('%d (x y) %s %d %s %d %s' % (pid, state, ppid, ' '.join(['0'] * 17), start,
                                             ' '.join(['0'] * 5))).encode()
        n = self.reads[pid] = self.reads.get(pid, 0) + 1
        hook = self.then.pop((pid, n), None)
        if hook is not None:
            hook()
        return data

    def patch(self, test, c):
        nowhere = os.path.join(tempfile.gettempdir(), 'kit-no-proc-%d' % os.getpid())
        for p in (mock.patch.object(c, '_stat_bytes', side_effect=self.stat),
                  mock.patch.object(c, '_pids', side_effect=lambda: sorted(self.procs)),
                  mock.patch.object(c, '_boot', return_value=self.BOOT),
                  mock.patch.object(c, 'PROC', nowhere)):        # no environ: nothing marked
            p.start()
            test.addCleanup(p.stop)


class Walk(unittest.TestCase):
    """The weak tier's tree walk and kill on a faked /proc, where a pid is reused between reads."""
    R, C, G = 100, 200, 300

    def setUp(self):
        self.c = contain()
        self.f = FakeProc({self.R: (1, 50, 'S'), self.C: (self.R, 60, 'S'),
                           self.G: (self.C, 70, 'S')})
        self.f.patch(self, self.c)

    def reuse(self, pid, ppid, start):
        def go():
            self.f.procs[pid] = (ppid, start, 'S')
        return go

    def test_the_tree_is_the_command_s_descendants_with_their_ctimes(self):
        f = self.f
        self.assertEqual(self.c.tree(self.R, f.ct(50), f.BOOT),
                         {self.C: f.ct(60), self.G: f.ct(70)})
        self.assertEqual(self.c.tree(self.R, f.ct(51), f.BOOT), {})   # not the command
        self.assertEqual(self.c.pstat(self.C, f.BOOT), (f.ct(60), self.R, False))
        self.assertIsNone(self.c.pstat(999, f.BOOT))

    def test_a_child_s_pid_reused_between_the_reads_never_joins_the_tree(self):
        f = self.f
        # the scan saw the command's child; it ended and its pid went to another process (another
        # parent) before the child's own read: the parent and the ctime are that one read's
        f.then[(self.C, 1)] = self.reuse(self.C, 1, 90)
        self.assertEqual(self.c.tree(self.R, f.ct(50), f.BOOT), {})

    def test_a_parent_s_pid_reused_during_the_walk_takes_no_children(self):
        f = self.f
        for n in (2, 3):            # after its read as the root's child, or between its 2 reads
            with self.subTest(after_read=n):
                f.procs[self.C] = (self.R, 60, 'S')
                f.reads.clear()
                f.then.clear()
                f.then[(self.C, n)] = self.reuse(self.C, 1, 95)   # G now reads as its child
                self.assertEqual(self.c.tree(self.R, f.ct(50), f.BOOT), {self.C: f.ct(60)})

    def job(self):
        return bare(self.c.WeakJob, token='wfake', _child=types.SimpleNamespace(pid=self.R),
                    _ctime=self.f.ct(50), spared=set())

    def kill(self, job, on_open=None):
        """The pids _kill_pass signals (each pid's fake pidfd is pid + 10000)."""
        sent = []

        def pidfd_open(pid):
            if on_open is not None:
                on_open(pid)
            return pid + 10000
        sig = types.SimpleNamespace(SIGSTOP=19, SIGKILL=9,
                                    pidfd_send_signal=lambda fd, s: sent.append((fd, s)))
        with mock.patch.object(self.c.os, 'pidfd_open', side_effect=pidfd_open, create=True), \
                mock.patch.object(self.c, 'signal', sig), mock.patch.object(self.c, '_close'):
            job._kill_pass()
        return sorted({fd - 10000 for fd, _ in sent})

    def test_the_kill_pass_signals_the_tree(self):
        self.assertEqual(self.kill(self.job()), [self.R, self.C, self.G])

    def test_a_pid_reused_during_the_walk_is_not_signalled(self):
        self.f.then[(self.C, 1)] = self.reuse(self.C, 1, 90)
        self.assertEqual(self.kill(self.job()), [self.R])

    def test_a_pid_reused_before_its_pidfd_is_opened_is_not_signalled(self):
        def on_open(pid):
            if pid == self.C:
                self.reuse(self.C, 1, 90)()
        self.assertEqual(self.kill(self.job(), on_open), [self.R, self.G])

    def test_an_ended_or_reused_command_is_no_member(self):
        f = self.f
        f.procs[self.R] = (1, 50, 'Z')
        self.assertNotIn(self.R, self.job().members())
        f.procs[self.R] = (1, 77, 'S')
        self.assertEqual(self.job().members(), {})


class NsWalk(unittest.TestCase):
    """W1's members, kill and ready-line checks on a faked /proc: the starter S, pid 1 R, the
    command C (pid 2 in the namespace) and its child G; namespace inodes and NSpid lines faked."""
    S, R, C, G, OWN, NS = 90, 100, 200, 300, 7, 4026531999

    def setUp(self):
        self.c = contain()
        self.f = FakeProc({self.S: (1, 40, 'S'), self.R: (self.S, 50, 'S'),
                           self.C: (self.R, 60, 'S'), self.G: (self.C, 70, 'S')})
        self.f.patch(self, self.c)
        self.ns = {'self': self.OWN, self.S: self.OWN, self.R: self.NS, self.C: self.NS,
                   self.G: self.NS}
        self.nspid = {self.S: [self.S], self.R: [self.R, 1], self.C: [self.C, 2],
                      self.G: [self.G, 3]}
        for name, table in (('_pidns', self.ns), ('_nspid', self.nspid)):
            patch = mock.patch.object(self.c, name, side_effect=lambda pid, t=table: t.get(pid))
            patch.start()
            self.addCleanup(patch.stop)

    def job(self, **attrs):
        attrs = dict(dict(reaper=self.R, _rct=self.f.ct(50), ns=self.NS, spared=set(),
                          _rfd=55, _child=types.SimpleNamespace(pid=self.S)), **attrs)
        return bare(self.c.NsJob, **attrs)

    def test_the_members_are_pid_1_s_descendants_not_pid_1_or_the_starter(self):
        f = self.f
        self.assertEqual(self.job().members(), {self.C: f.ct(60), self.G: f.ct(70)})
        self.assertEqual(self.job(spared={self.G}).members(), {self.C: f.ct(60)})
        f.procs[self.G] = (self.C, 70, 'Z')
        self.assertEqual(self.job().members(), {self.C: f.ct(60)})        # an ended one is not

    def test_a_pid_1_that_ended_or_was_reused_has_no_members(self):
        self.f.procs[self.R] = (self.S, 51, 'S')            # the pid now another process's
        self.assertEqual(self.job().members(), {})
        self.assertEqual(self.job(reaper=None).members(), {})

    def kill(self, job):
        sent = []
        sig = types.SimpleNamespace(SIGSTOP=19, SIGKILL=9,
                                    pidfd_send_signal=lambda fd, s: sent.append((fd, s)))
        with mock.patch.object(self.c, 'signal', sig), \
                mock.patch.object(self.c.os, 'pidfd_open', side_effect=lambda pid: pid + 10000,
                                  create=True), \
                mock.patch.object(self.c, '_close'), \
                mock.patch.object(self.c, 'GONE_SECONDS', 0.05), \
                mock.patch.object(self.c.time, 'sleep'):
            done = job._kill_all()
        return done, sent

    def test_the_kill_is_pid_1_s_through_its_pidfd(self):
        def die(fd, s):
            for pid in (self.R, self.C, self.G):
                self.f.procs.pop(pid)
        job = self.job()
        sig = types.SimpleNamespace(SIGSTOP=19, SIGKILL=9, pidfd_send_signal=mock.Mock(
            side_effect=die))
        with mock.patch.object(self.c, 'signal', sig):
            self.assertTrue(job._kill_all())
        sig.pidfd_send_signal.assert_called_once_with(55, 9)

    def test_a_kill_that_leaves_members_is_not_done(self):
        done, sent = self.kill(self.job())
        self.assertFalse(done)
        self.assertEqual(sent[0], (55, 9))

    def test_with_spared_pids_the_kill_goes_member_by_member_and_spares_pid_1(self):
        done, sent = self.kill(self.job(spared={self.G}))
        self.assertEqual(sorted({fd - 10000 for fd, _ in sent}), [self.C])
        self.assertNotIn((55, 9), sent)

    def test_with_no_pidfd_on_pid_1_the_kill_reaches_nothing(self):
        done, sent = self.kill(self.job(_rfd=None))
        self.assertEqual((done, sent), (False, []))

    def test_leftovers_name_the_spared_by_their_pids_in_the_namespace(self):
        job = self.job()
        with mock.patch.object(job, '_watch') as watch:
            self.assertEqual(job.leftovers([self.G, 4242]), [self.G])
        watch.assert_called_once_with('spare 3')
        self.assertEqual(job.spared, {self.G})

    def test_a_member_in_a_namespace_nested_in_the_job_s_is_named_by_its_pid_in_the_job_s(self):
        self.nspid[self.G] = [self.G, 3, 1]         # pid 1 of a namespace its command made
        job = self.job()
        with mock.patch.object(job, '_watch') as watch:
            self.assertEqual(job.leftovers([self.G]), [self.G])
        watch.assert_called_once_with('spare 3')

    def place(self, lines, own=None):
        job = self.job(reaper=None, _rct=None, ns=None, _rfd=None, command=None, _ctime=None)
        if own is not None:
            self.ns['self'] = own
        with mock.patch.object(self.c.os, 'pidfd_open', return_value=55, create=True), \
                mock.patch.object(self.c, '_close') as close:
            try:
                job._place(types.SimpleNamespace(pid=self.S), lines)
            finally:
                self.closed = [a[0][0] for a in close.call_args_list]
        return job

    def test_the_ready_lines_name_pid_1_and_the_command_in_its_namespace(self):
        job = self.place({'r': [str(self.R)], 's': ['2']})
        self.assertEqual((job.reaper, job.command, job.ns, job._rfd, job.wd),
                         (self.R, self.C, self.NS, 55, self.R))

    def test_a_forged_ready_line_is_refused(self):
        err = jobmod().JobError
        for lines, want in (({'r': [str(self.C)], 's': ['2']}, "not the starter's pid 1"),
                            ({'r': [str(self.S)], 's': ['2']}, "not the starter's pid 1"),
                            ({'r': [str(self.R)], 's': ['3']}, 'not a process in the job'),
                            ({'r': [str(self.R)], 's': ['1']}, 'not a process in the job'),
                            ({'r': ['x'], 's': ['2']}, 'not a pid')):
            with self.subTest(lines=lines), self.assertRaisesRegex(err, want):
                self.place(lines)
        with self.assertRaisesRegex(err, 'no PID namespace of its own'):
            self.place({'r': [str(self.R)], 's': ['2']}, own=self.NS)

    def test_a_pid_1_reused_before_its_pidfd_opens_is_let_go(self):
        self.f.then[(self.R, 1)] = lambda: self.f.procs.__setitem__(self.R, (self.S, 99, "S"))
        with self.assertRaisesRegex(jobmod().JobError, 'ended before its pidfd'):
            self.place({'r': [str(self.R)], 's': ['2']})
        self.assertEqual(self.closed, [55])


def unit_names(pid=None):
    """The pmkit-containment units process pid (default: this one) made that systemd still
    lists, any state."""
    r = subprocess.run(['systemctl', '--user', 'list-units', '--all', '--plain', '--no-legend',
                        PREFIX + '-*'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=30, check=False)
    mine = '-%d-' % (os.getpid() if pid is None else pid)
    return [line.split()[0] for line in r.stdout.decode().splitlines()
            if line.split() and mine in line.split()[0]]


def gone(pid):
    """True when pid is no process, or an ended one (a zombie)."""
    try:
        with open('/proc/%d/stat' % pid) as f:
            text = f.read()
    except OSError:
        return True
    return text[text.rindex(')') + 2] in 'ZX'


def wait_until(test, what, timeout=10.0):
    end = time.monotonic() + timeout
    while not test():
        if time.monotonic() > end:
            raise AssertionError('not within %g s: %s' % (timeout, what))
        time.sleep(0.02)


def kill_own(pid):
    """Kills pid this test started (it reads the pid from its own child), through a pidfd."""
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        signal.pidfd_send_signal(fd, signal.SIGKILL)
    except OSError:
        pass
    finally:
        os.close(fd)


def busy_starts(test, make_job, n=4):
    """Start n jobs at once from a process with descriptors past 9 open (a pool runner's, a
    busy owner's), each command listing its own descriptors: every one starts, and holds only
    stdin, stdout and stderr (the stub's pipes are not left to it)."""
    spare = [fd for _ in range(8) for fd in os.pipe()]
    test.addCleanup(lambda: [os.close(fd) for fd in spare])
    out, errors = {}, []

    def one(i):
        job = make_job()
        try:
            p = job.start([sys.executable, '-c', 'import os; print(sorted(int(f) for f in '
                           'os.listdir("/proc/self/fd")))'], stdout=subprocess.PIPE)
            out[i] = p.communicate(timeout=30)[0].decode().strip()
        except Exception as e:      # reported below, with the others
            errors.append(repr(e))
        finally:
            job.close()
    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    test.assertEqual(errors, [])
    test.assertEqual(sorted(out), list(range(n)))
    for text in out.values():
        fds = ast.literal_eval(text)
        test.assertEqual(fds[:3], [0, 1, 2])
        test.assertLessEqual(len(fds), 4, text)     # 0-2 and listdir's own


def lines(p, n, timeout=20.0):
    """The first n lines the child prints (stdout a pipe), as words."""
    out = []
    end = time.monotonic() + timeout
    while len(out) < n:
        if time.monotonic() > end:
            raise AssertionError('the child printed %r only' % out)
        line = p.stdout.readline()
        if not line:
            raise AssertionError('the child ended after %r' % out)
        out.append(line.decode().split())
    return out


class Case(unittest.TestCase):
    """Real jobs. Each test's scopes carry the pmkit-containment prefix and this pid; tearDown
    checks every one is gone."""

    def setUp(self):
        self.c = contain()
        patch = mock.patch.dict(os.environ, {self.c.PREFIX_ENV: PREFIX})
        patch.start()
        self.addCleanup(patch.stop)
        self.strays = []
        self.addCleanup(self.no_units_left)
        self.addCleanup(self.kill_strays)
        self.addCleanup(self.c.drop_carrier)

    def kill_strays(self):
        for pid in self.strays:
            kill_own(pid)

    def no_units_left(self):
        wait_until(lambda: not unit_names(), 'no %s-* unit of pid %d left (%s)'
                   % (PREFIX, os.getpid(), unit_names()))

    def owner_argv(self, body):
        return [sys.executable, '-c', 'import os, subprocess, sys, time\n'
                'from kitlib.proc.job import Job\n' + textwrap.dedent(body)]

    def owner_env(self):
        return dict(os.environ, PYTHONPATH=os.getcwd())

    def owner(self, body):
        """A python that runs `body` (it prints what the test reads), stdout a pipe."""
        p = subprocess.Popen(self.owner_argv(body), stdout=subprocess.PIPE, cwd=os.getcwd(),
                             env=self.owner_env())
        self.addCleanup(p.wait)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.addCleanup(p.stdout.close)
        return p

    def marker(self):
        """A path the command touches: it ran when the file is there."""
        folder = tempfile.mkdtemp(prefix='kit-contain-')
        self.addCleanup(shutil.rmtree, folder, True)
        return os.path.join(folder, 'ran')

    def owner_dies_before_go(self, make, wd):
        """An owner SIGKILLed in before_resume, its job made and before go: the command never
        runs, its stub and watchdog end, and no unit of the owner's is left."""
        for kill_on_close in (True, False):
            with self.subTest(kill_on_close=kill_on_close):
                ran = self.marker()
                p = self.owner('''
                    import signal
                    from kitlib.platform.linux import contain
                    def die(stub):
                        print(stub.pid, %s, flush=True)
                        os.kill(os.getpid(), signal.SIGKILL)
                    job = %s
                    job.start(['sh', '-c', 'touch %s'], before_resume=die)
                    time.sleep(300)
                ''' % (wd, make % kill_on_close, ran))
                stub, dog = (int(x) for x in lines(p, 1)[0])
                self.strays.append(stub)
                self.assertEqual(p.wait(20), -signal.SIGKILL)
                for pid, what in ((stub, 'the stub'), (dog, 'the watchdog')):
                    wait_until(lambda: gone(pid), '%s ended with its owner' % what)
                wait_until(lambda: not unit_names(p.pid), 'no unit of the owner %d left (%s)'
                           % (p.pid, unit_names(p.pid)))
                time.sleep(0.2)
                self.assertFalse(os.path.exists(ran))          # the command never ran

    def start_fails_before_go(self, job, wd):
        """before_resume raises: the start is aborted, its stub and watchdog end, the command
        never runs. Returns what before_resume saw (stub, wd, cg)."""
        ran, seen = self.marker(), {}

        def fail(stub):
            seen.update(stub=stub.pid, wd=wd(job), cg=getattr(job, 'cg', None))
            self.strays.append(stub.pid)
            raise RuntimeError('refused by the test')
        with self.assertRaisesRegex(RuntimeError, 'refused by the test'):
            job.start(['sh', '-c', 'touch ' + ran], before_resume=fail)
        wait_until(lambda: gone(seen['stub']), 'the stub ended')
        wait_until(lambda: gone(seen['wd']), 'the watchdog ended')
        job.close()
        time.sleep(0.2)
        self.assertFalse(os.path.exists(ran))
        return seen

    def forged(self, name, line, forge, want):
        """The stub's ready line forged (another pid, another cgroup): JobError, nothing runs."""
        stub = getattr(self.c, name)
        self.assertIn(line, stub)
        ran = self.marker()
        job = self.make()
        with mock.patch.object(self.c, name, stub.replace(line, forge)):
            with self.assertRaisesRegex(jobmod().JobError, want):
                job.start(['sh', '-c', 'touch ' + ran])
        dog = job.wd.pid if hasattr(job.wd, 'pid') else job.wd
        if dog is not None:
            wait_until(lambda: gone(dog), 'the watchdog ended')
        job.close()
        time.sleep(0.2)
        self.assertFalse(os.path.exists(ran))
        self.assertIsNone(job.p)


@posix_only('cgroup scopes of the user systemd are Linux')
class Scope(Case):
    def setUp(self):
        super().setUp()
        tier = self.c.probe()
        if not tier.scoped:
            self.skipTest('no cgroup scope here: %s' % tier.text)

    def test_a_setsid_grandchild_ends_with_its_job(self):
        job = self.c.Job()
        self.assertIsInstance(job, self.c.ScopeJob)
        self.addCleanup(job.close)
        p = job.start(['sh', '-c', 'setsid sleep 300 & echo $!; exec sleep 300'],
                      stdout=subprocess.PIPE)
        self.addCleanup(p.stdout.close)
        grand = int(lines(p, 1)[0][0])
        self.strays.append(grand)
        self.assertEqual(job.contains(grand), 'in')
        self.assertEqual(job.contains(os.getpid()), 'out')
        self.assertTrue(job.terminate())
        self.assertEqual(p.wait(10), -signal.SIGKILL)
        wait_until(lambda: gone(grand), 'the setsid grandchild ended')
        job.close()
        wait_until(lambda: not os.path.isdir(self.c.cg_dir(job.cg)), 'the scope is gone')

    def test_the_command_runs_in_the_job_s_leaf_with_the_caller_s_environment(self):
        job = self.c.Job()
        self.addCleanup(job.close)
        env = {k: v for k, v in os.environ.items()
               if k not in ('INVOCATION_ID', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS')}
        env['KIT_PROBE'] = '$HOME'
        p = job.start(['sh', '-c', 'echo $$; cat /proc/self/cgroup; '
                       'echo "${INVOCATION_ID-none} ${XDG_RUNTIME_DIR-none} $KIT_PROBE"'],
                      stdout=subprocess.PIPE, env=env)
        out = p.communicate(timeout=20)[0].decode().splitlines()
        self.assertEqual(p.returncode, 0)
        self.assertEqual(int(out[0]), p.pid)                # systemd-run execs in place
        self.assertEqual(out[1], '0::%s/main' % job.cg)
        self.assertEqual(out[2], 'none none $HOME')         # nothing added, nothing expanded
        self.assertTrue(job.name.startswith('%s-job-%d-' % (PREFIX, os.getpid())), job.name)
        self.assertTrue(job.cg.endswith('/' + job.name), job.cg)

    def test_the_exit_code_and_the_pipes_are_the_command_s(self):
        job = self.c.Job(kill_on_close=False)
        p = job.start([sys.executable, '-c', 'import sys; sys.stdout.write(sys.stdin.read()); '
                       'sys.exit(7)'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.assertEqual(p.communicate(b'through', timeout=20), (b'through', None))
        self.assertEqual(p.returncode, 7)
        job.close()

    def test_jobs_started_at_once_by_a_busy_owner_each_start_and_hold_no_stub_pipe(self):
        busy_starts(self, lambda: self.c.Job(kill_on_close=True))

    def test_a_command_that_is_not_there_raises_and_makes_no_scope(self):
        with self.assertRaises(FileNotFoundError):
            self.c.Job().start(['kit-no-such-command-xyz'])
        with self.assertRaises(FileNotFoundError):
            self.c.Job().start(['/nonexistent/kit-cmd'])

    def test_a_job_starts_one_command(self):
        job = self.c.Job()
        self.addCleanup(job.close)
        job.start(['true']).wait(20)
        with self.assertRaisesRegex(jobmod().JobError, 'starts one command'):
            job.start(['true'])

    def test_peak_memory_and_cpu_time_are_the_cgroup_s(self):
        job = self.c.Job()
        self.addCleanup(job.close)
        p = job.start([sys.executable, '-c', 'b = bytearray(64 << 20); print(len(b), flush=True);'
                       ' import time; time.sleep(60)'], stdout=subprocess.PIPE)
        self.addCleanup(p.stdout.close)
        lines(p, 1)
        cpu_s, peak, now = job.usage()
        self.assertGreater(peak, 64 << 20)
        self.assertGreater(now, 32 << 20)
        self.assertGreater(cpu_s, 0)
        job.close()
        p.wait(10)
        self.assertIsNone(job.usage())
        self.assertIsNone(job.processes())

    def test_the_pin_is_the_cgroup_s_and_narrow_and_repin_move_it(self):
        if self.c.probe().name != 'S1':
            self.skipTest('no cpuset here: %s' % self.c.probe().text)
        cpus = sorted(os.sched_getaffinity(0))
        if len(cpus) < 2:
            self.skipTest('one cpu only')
        a, b = cpus[-2], cpus[-1]
        job = self.c.Job(mask=(1 << a) | (1 << b))
        self.addCleanup(job.close)
        p = job.start(['sleep', '60'])
        self.assertEqual(job.pin, 'pinned')
        self.assertEqual(os.sched_getaffinity(p.pid), {a, b})
        eff = os.path.join(self.c.cg_dir(job.cg), 'cpuset.cpus.effective')
        self.assertTrue(job.narrow(1 << b))
        self.assertEqual(os.sched_getaffinity(p.pid), {b})
        if cpus[0] not in (a, b):
            self.assertFalse(job.narrow(1 << cpus[0]))     # not part of its mask
            self.assertEqual(os.sched_getaffinity(p.pid), {b})
        self.assertTrue(job.repin((1 << a) | (1 << b)))
        self.assertEqual(os.sched_getaffinity(p.pid), {a, b})
        with open(eff) as f:
            self.assertEqual(f.read().strip(), '%d-%d' % (a, b) if b == a + 1 else '%d,%d' % (a, b))
        job.end(kill=False)                               # unpinned, released
        self.assertEqual(os.sched_getaffinity(p.pid), set(cpus))
        p.kill()
        p.wait()

    def test_a_job_that_ended_cleanly_leaves_no_watchdog(self):
        for kill_on_close, how in ((True, 'close'), (False, 'close'), (True, 'end-keep'),
                                   (False, 'end-kill')):
            with self.subTest(kill_on_close=kill_on_close, how=how):
                job = self.c.Job(kill_on_close=kill_on_close)
                p = job.start(['true'])
                p.wait(20)
                wd = job.wd
                self.assertIsNotNone(wd)
                self.assertFalse(gone(wd))
                self.assertNotIn(wd, job.pids())           # never one of the job's processes
                if how == 'close':
                    job.close()
                else:
                    job.end(kill=how == 'end-kill')
                wait_until(lambda: gone(wd), 'the watchdog ended with its job')
                wait_until(lambda: not os.path.isdir(self.c.cg_dir(job.cg)), 'the scope went')

    def test_an_owner_killed_by_sigkill_takes_its_job_s_tree_and_watchdog_with_it(self):
        p = self.owner('''
            job = Job()
            c = job.start(['sh', '-c', 'setsid sleep 300 & echo $!; exec sleep 300'],
                          stdout=subprocess.PIPE)
            g = c.stdout.readline().decode().strip()
            print(c.pid, g, job.wd, job.cg, flush=True)
            time.sleep(300)
        ''')
        child, grand, wd, cg = lines(p, 1)[0]
        child, grand, wd = int(child), int(grand), int(wd)
        self.strays += [child, grand]
        self.assertFalse(gone(child) or gone(grand) or gone(wd))
        p.kill()
        p.wait()
        for pid, what in ((child, 'the command'), (grand, 'its setsid child'),
                          (wd, 'the watchdog')):
            wait_until(lambda: gone(pid), '%s ended with its owner' % what)
        wait_until(lambda: not os.path.isdir(self.c.cg_dir(cg)), 'the scope went')

    def test_an_owner_killed_by_sigkill_leaves_a_released_job_running_without_its_watchdog(self):
        p = self.owner('''
            job = Job(kill_on_close=False)
            c = job.start(['sleep', '300'])
            print(c.pid, job.wd, job.cg, flush=True)
            time.sleep(300)
        ''')
        child, wd, cg = lines(p, 1)[0]
        child, wd = int(child), int(wd)
        self.strays.append(child)
        p.kill()
        p.wait()
        wait_until(lambda: gone(wd), 'the watchdog ended with its owner')
        time.sleep(0.2)
        self.assertFalse(gone(child))                     # kill_on_close off: it runs on
        kill_own(child)
        wait_until(lambda: not os.path.isdir(self.c.cg_dir(cg)), 'the scope went')

    def test_a_released_job_s_daemon_runs_on_and_leftovers_takes_it_out_of_the_job(self):
        job = self.c.Job()
        self.addCleanup(job.close)
        p = job.start(['sh', '-c', 'setsid sleep 300 >/dev/null 2>&1 & echo $!'],
                      stdout=subprocess.PIPE)
        daemon = int(p.communicate(timeout=20)[0])
        self.strays.append(daemon)
        self.assertEqual(job.pids(), [daemon])
        self.assertEqual(job.leftovers(), [daemon])
        self.assertEqual(job.contains(daemon), 'out')
        job.close()                                        # kill-on-close: the job only
        time.sleep(0.2)
        self.assertFalse(gone(daemon))
        with open('/proc/%d/cgroup' % daemon) as f:
            self.assertIn('-leftovers-%d-' % os.getpid(), f.read())
        kill_own(daemon)

    def test_a_job_made_inside_a_job_is_nested_in_it_and_dies_with_it(self):
        outer = self.c.Job()
        self.addCleanup(outer.close)
        q = outer.start(self.owner_argv('''
            from kitlib.platform.linux import contain
            job = Job()
            print(contain.probe().text, job.tier.parent, flush=True)
            c = job.start(['sh', '-c', 'echo $$; exec sleep 300'], stdout=subprocess.PIPE)
            print(c.stdout.readline().decode().strip(), job.cg, flush=True)
            time.sleep(300)
        '''), stdout=subprocess.PIPE, cwd=os.getcwd(), env=self.owner_env())
        self.addCleanup(q.stdout.close)
        (first, second) = lines(q, 2)
        self.assertEqual(first[-1], outer.cg)              # the parent the inner owner read
        sleeper, inner = int(second[0]), second[1]
        self.strays.append(sleeper)
        self.assertTrue(inner.startswith(outer.cg + '/j-'), inner)
        self.assertEqual(outer.contains(sleeper), 'in')
        self.assertIn(sleeper, outer.pids())
        self.assertTrue(outer.terminate())
        wait_until(lambda: gone(sleeper), 'the nested job ended with the outer one')
        q.wait(10)
        outer.close()

    def test_a_systemd_run_that_runs_the_command_outside_any_scope_is_caught(self):
        folder = tempfile.mkdtemp(prefix='kit-contain-')
        self.addCleanup(shutil.rmtree, folder, True)
        fake = os.path.join(folder, 'systemd-run')
        out = os.path.join(folder, 'ran')
        with open(fake, 'w') as f:
            f.write('#!/bin/sh\n[ "$1" = --help ] && exit 0\n'
                    'while [ "$1" != -- ]; do shift; done; shift; exec "$@"\n')
        os.chmod(fake, 0o755)
        with mock.patch.object(self.c, 'SYSTEMD_RUN', fake):
            name, lacks, why = self.c.trial()
            self.assertIsNone(name)
            self.assertIn('not in its scope', why)
            job = self.c.Job(tier=self.c.Tier('S1'))
            with self.assertRaisesRegex(jobmod().JobError, 'not in its scope'):
                job.start(['sh', '-c', 'touch %s' % out])
            job.close()
        self.assertFalse(os.path.exists(out))               # the command never ran
        self.assertIsNone(job.cg)

    def make(self):
        return self.c.Job()

    def test_an_owner_killed_before_go_runs_no_command_and_leaves_no_scope(self):
        self.owner_dies_before_go('Job(kill_on_close=%s)', 'job.wd')

    def test_a_start_that_fails_before_go_kills_the_scope_it_made_and_runs_nothing(self):
        job = self.c.Job()
        with mock.patch.object(self.c, '_write', wraps=self.c._write) as write:
            seen = self.start_fails_before_go(job, lambda j: j.wd)
        self.assertIn(mock.call(self.c.CGROOT + seen['cg'] + '/cgroup.kill', '1'),
                      write.call_args_list)
        wait_until(lambda: not os.path.isdir(self.c.cg_dir(seen['cg'])), 'the scope went')

    def test_a_forged_ready_line_is_refused(self):
        for forge, want in (('echo "s 1 $j"', 'the ready line named pid'),
                            ('echo "s $$ /user.slice/other.scope"',
                             'not a cgroup this job made')):
            with self.subTest(forge=forge):
                self.forged('STUB', 'echo "s $$ $j"', forge, want)

    def test_the_probe_finds_a_scope_that_is_killed_and_goes(self):
        tier = self.c.probe(fresh=True)
        self.assertTrue(tier.scoped, tier.text)
        self.assertIn(tier.name, ('S1', 'S2'))


def fake_busctl(folder, body):
    """A busctl that runs `body` (sh) for AttachProcessesToUnit and the real one otherwise."""
    path = os.path.join(folder, 'busctl')
    real = shutil.which('busctl')
    with open(path, 'w') as f:
        f.write('#!/bin/sh\nfor a; do [ "$a" = AttachProcessesToUnit ] && { %s; }; done\n'
                'exec %s "$@"\n' % (body, real))
    os.chmod(path, 0o755)
    return path


@posix_only('cgroup scopes of the user systemd are Linux')
class Carrier(Case):
    """Short jobs nested in this process's carrier scope, moved there by systemd."""

    def setUp(self):
        super().setUp()
        tier = self.c.probe()
        if not tier.scoped or tier.parent is not None:
            self.skipTest('no top-level cgroup scope here: %s' % tier.text)

    def short(self, **kw):
        return self.c.Job(short=True, **kw)

    def run_short(self, argv, **kw):
        job = self.short(**kw)
        p = job.start(argv, stdout=subprocess.PIPE)
        out = p.communicate(timeout=20)[0].decode()
        job.close()
        return job, p.returncode, out

    def carriers(self):
        return [u for u in unit_names() if '-job-' in u]

    def test_short_jobs_share_one_carrier_each_in_a_cgroup_of_its_own(self):
        a, rc, out = self.run_short(['sh', '-c', 'cat /proc/self/cgroup; exit 3'])
        self.assertEqual(rc, 3)
        b = self.short()
        p = b.start(['sh', '-c', 'echo $$; cat /proc/self/cgroup'], stdout=subprocess.PIPE)
        pid, cg = p.communicate(timeout=20)[0].decode().split()
        self.assertEqual(int(pid), p.pid)
        b.close()
        self.assertIs(a.carried, b.carried)
        top = a.carried.cg
        self.assertTrue(self.c.TOP.match(top.rsplit('/', 1)[1]), top)
        self.assertEqual(out.strip(), '0::%s/main' % a.cg)
        self.assertEqual(cg, '0::%s/main' % b.cg)
        self.assertNotEqual(a.cg, b.cg)
        for job in (a, b):
            self.assertEqual(job.cg.rsplit('/', 1)[0], top)
            self.assertTrue(self.c.NESTED.match(job.name), job.name)
            wait_until(lambda: not os.path.isdir(self.c.cg_dir(job.cg)), 'its cgroup went')
        self.assertEqual(len(self.carriers()), 1, unit_names())

    def test_terminating_one_short_job_leaves_another_running(self):
        jobs = [self.short() for _ in range(2)]
        procs = [j.start(['sleep', '300']) for j in jobs]
        for j in jobs:
            self.addCleanup(j.close)
        self.assertTrue(jobs[0].terminate())
        self.assertEqual(procs[0].wait(10), -signal.SIGKILL)
        time.sleep(0.2)
        self.assertIsNone(procs[1].poll())
        self.assertEqual(jobs[1].contains(procs[1].pid), 'in')
        jobs[1].close()                                    # kill_on_close
        self.assertEqual(procs[1].wait(10), -signal.SIGKILL)

    def test_an_owner_killed_by_sigkill_kills_its_short_job_and_spares_a_released_one(self):
        p = self.owner('''
            kill = Job(short=True)
            c = kill.start(['sh', '-c', 'setsid sleep 300 & echo $!; exec sleep 300'],
                           stdout=subprocess.PIPE)
            g = c.stdout.readline().decode().strip()
            keep = Job(kill_on_close=False, short=True)
            d = keep.start(['sleep', '300'])
            print(c.pid, g, kill.wd, d.pid, kill.carried.cg, flush=True)
            time.sleep(300)
        ''')
        child, grand, wd, daemon, top = lines(p, 1)[0]
        child, grand, wd, daemon = int(child), int(grand), int(wd), int(daemon)
        self.strays += [child, grand, daemon]
        p.kill()
        p.wait()
        for pid, what in ((child, 'the command'), (grand, 'its setsid child'),
                          (wd, 'the watchdog')):
            wait_until(lambda: gone(pid), '%s ended with its owner' % what)
        time.sleep(0.2)
        self.assertFalse(gone(daemon))                      # released: it runs on
        self.assertTrue(unit_names(p.pid))                 # its carrier holds it
        kill_own(daemon)
        wait_until(lambda: not unit_names(p.pid), 'the carrier went with its last job')
        self.assertFalse(os.path.isdir(self.c.cg_dir(top)))

    def test_an_owner_that_exits_leaves_no_carrier(self):
        p = self.owner('''
            for _ in range(3):
                job = Job(kill_on_close=False, short=True)
                job.start(['true']).wait(20)
                job.close()
            print(job.carried.cg, flush=True)
        ''')
        top = lines(p, 1)[0][0]
        self.assertEqual(p.wait(20), 0)
        wait_until(lambda: not unit_names(p.pid), 'no unit of the owner left')
        self.assertFalse(os.path.isdir(self.c.cg_dir(top)))

    def test_a_carrier_that_was_killed_or_stopped_is_made_again_and_each_command_runs_once(self):
        ran = self.marker()
        argv = ['sh', '-c', 'echo x >> ' + ran]
        first = self.run_short(argv)[0].carried
        first.holder.kill()                                 # the holder killed
        first.holder.wait()
        second = self.run_short(argv)[0].carried
        self.assertIsNot(second, first)
        subprocess.run(['systemctl', '--user', 'stop', second.name], timeout=30, check=False)
        third = self.run_short(argv)[0].carried
        self.assertIsNot(third, second)
        with open(ran) as f:
            self.assertEqual(f.read().split(), ['x', 'x', 'x'])

    def fell_back(self, body, refused):
        folder = tempfile.mkdtemp(prefix='kit-contain-')
        self.addCleanup(shutil.rmtree, folder, True)
        ran = self.marker()
        with mock.patch.object(self.c, 'BUSCTL', fake_busctl(folder, body)):
            job, rc, out = self.run_short(['sh', '-c', 'echo x >> %s; cat /proc/self/cgroup'
                                           % ran])
            self.assertEqual(rc, 0)
            self.assertIsNone(job.carried)                  # a scope of its own
            self.assertIsNone(job.parent)
            self.assertTrue(self.c.TOP.match(job.name), job.name)
            self.assertEqual(out.strip(), '0::%s/main' % job.cg)
            with open(ran) as f:
                self.assertEqual(f.read().split(), ['x'])  # once
            self.assertEqual(self.c._carrier.off is not None, refused)
        job2 = self.run_short(['true'])[0]                  # the real busctl
        self.assertEqual(job2.carried is None, refused)

    def test_a_refused_move_gives_a_scope_of_its_own_and_turns_the_carrier_off(self):
        self.fell_back('echo refused by the test >&2; exit 1', refused=True)

    def test_a_move_not_seen_gives_a_scope_of_its_own(self):
        self.fell_back('exit 0', refused=False)

    def test_a_long_job_has_its_own_scope_and_a_short_job_in_a_kit_job_nests_there(self):
        job = self.c.Job()
        job.start(['true']).wait(20)
        job.close()
        self.assertIsNone(job.carried)
        self.assertTrue(self.c.TOP.match(job.name), job.name)
        outer = self.c.Job()
        self.addCleanup(outer.close)
        q = outer.start(self.owner_argv('''
            job = Job(short=True)
            job.start(['true']).wait(20)
            job.close()
            print(job.cg, job.carried, flush=True)
        '''), stdout=subprocess.PIPE, cwd=os.getcwd(), env=self.owner_env())
        out = q.communicate(timeout=30)[0].decode().split()
        self.assertTrue(out[0].startswith(outer.cg + '/j-'), out)
        self.assertEqual(out[1], 'None')
        self.assertEqual(self.carriers(), [outer.name])

    def test_a_forked_child_makes_its_own_carrier(self):
        p = self.owner('''
            def top():
                job = Job(short=True)
                job.start(['true']).wait(20)
                job.close()
                return job.carried.cg
            mine = top()
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.write(w, top().encode())
                os._exit(0)
            os.close(w)
            os.waitpid(pid, 0)
            print(mine, os.read(r, 4096).decode(), top(), pid, flush=True)
        ''')
        mine, child, again, pid = lines(p, 1)[0]
        self.assertNotEqual(mine, child)
        self.assertIn('-job-%s-' % pid, child)              # the child's own
        self.assertEqual(mine, again)
        self.assertEqual(p.wait(20), 0)
        for owner in (p.pid, int(pid)):
            wait_until(lambda: not unit_names(owner), 'no unit of %d left' % owner)

    def test_short_jobs_started_at_once_make_one_carrier(self):
        busy_starts(self, lambda: self.short(kill_on_close=True), n=8)
        self.assertEqual(len(self.carriers()), 1, unit_names())

    def test_an_owner_killed_before_go_runs_no_short_command(self):
        self.owner_dies_before_go('Job(kill_on_close=%s, short=True)', 'job.wd')

    def test_a_weak_short_job_is_a_weak_job(self):
        job = self.c.Job(tier=self.c.Tier('W2', self.c.W2_LACKS, why='forced'), short=True)
        self.assertIsInstance(job, self.c.WeakJob)
        job.start(['true']).wait(20)
        job.close()
        self.assertIsNone(self.c._carrier)


@posix_only('the weak tier walks /proc')
class Weak(Case):
    def job(self, **kw):
        c = self.c
        return c.Job(tier=c.Tier('W2', c.W2_LACKS, why='forced by the test'), **kw)

    def test_the_weak_tier_kills_a_setsid_tree_and_its_orphans(self):
        job = self.job()
        self.assertIsInstance(job, self.c.WeakJob)
        self.addCleanup(job.close)
        p = job.start(['sh', '-c', 'setsid sh -c "sleep 300 & echo \\$!; exec sleep 300" & '
                       'wait'], stdout=subprocess.PIPE)
        self.addCleanup(p.stdout.close)
        orphan = int(lines(p, 1)[0][0])
        self.strays.append(orphan)
        self.assertEqual(job.contains(orphan), 'in')
        self.assertEqual(job.contains(os.getpid()), 'out')
        self.assertTrue(job.terminate())
        p.wait(10)
        wait_until(lambda: gone(orphan), 'the setsid orphan ended')
        self.assertEqual(job.pids(), [])
        self.assertIsNone(job.processes())

    def test_weak_jobs_started_at_once_by_a_busy_owner_each_start_and_hold_no_stub_pipe(self):
        busy_starts(self, self.job)

    def test_a_weak_job_that_ended_cleanly_leaves_no_watchdog(self):
        for kill_on_close in (True, False):
            with self.subTest(kill_on_close=kill_on_close):
                job = self.job(kill_on_close=kill_on_close)
                job.start(['true']).wait(20)
                wd = job.wd.pid
                self.assertNotIn(wd, job.pids())
                job.close()
                wait_until(lambda: gone(wd), 'the watchdog ended with its job')

    def test_an_owner_killed_by_sigkill_takes_its_weak_job_s_tree_and_watchdog_with_it(self):
        p = self.owner('''
            from kitlib.platform.linux import contain
            job = Job(tier=contain.Tier('W2', contain.W2_LACKS, why='forced'))
            c = job.start(['sh', '-c', 'setsid sleep 300 & echo $!; exec sleep 300'],
                          stdout=subprocess.PIPE)
            g = c.stdout.readline().decode().strip()
            print(c.pid, g, job.wd.pid, flush=True)
            time.sleep(300)
        ''')
        child, grand, wd = (int(x) for x in lines(p, 1)[0])
        self.strays += [child, grand]
        p.kill()
        p.wait()
        for pid, what in ((child, 'the command'), (grand, 'its setsid child'),
                          (wd, 'the watchdog')):
            wait_until(lambda: gone(pid), '%s ended with its owner' % what)

    def make(self):
        return self.job()

    def test_an_owner_killed_before_go_runs_no_weak_command(self):
        self.owner_dies_before_go("Job(kill_on_close=%s, tier=contain.Tier('W2', "
                                  "contain.W2_LACKS, why='forced'))", 'job.wd.pid')

    def test_a_weak_start_that_fails_before_go_ends_its_stub_and_runs_nothing(self):
        self.start_fails_before_go(self.job(), lambda j: j.wd.pid)

    def test_a_forged_weak_ready_line_is_refused(self):
        self.forged('WSTUB', 'echo "s $$"', 'echo "s 1"', 'the ready line named pid')

    def test_leftovers_are_spared_by_the_weak_job_s_kill(self):
        job = self.job()
        self.addCleanup(job.close)
        p = job.start(['sh', '-c', 'setsid sleep 300 >/dev/null 2>&1 & echo $!'],
                      stdout=subprocess.PIPE)
        daemon = int(p.communicate(timeout=20)[0])
        self.strays.append(daemon)
        self.assertEqual(job.leftovers(), [daemon])
        job.close()
        time.sleep(0.3)
        self.assertFalse(gone(daemon))
        kill_own(daemon)



W1 = "contain.Tier('W1', contain.W1_LACKS)"


@posix_only('user and PID namespaces are Linux')
class Ns(Case):
    """W1 on real processes, where its trial works (unprivileged user namespaces allowed)."""
    _no = []

    def setUp(self):
        super().setUp()
        if not Ns._no:
            Ns._no.append(self.c.w1_trial())
        if Ns._no[0] is not None:
            self.skipTest('no W1 here: %s' % Ns._no[0])

    def job(self, **kw):
        return self.c.Job(tier=self.c.Tier('W1', self.c.W1_LACKS), **kw)

    def outer(self, job, inner):
        """The owner's pid of the job's process whose pid in the namespace is `inner`."""
        found = []

        def look():
            found[:] = [p for p in job.pids() if self.c._nspid(p)[-1:] == [int(inner)]]
            return found
        wait_until(look, 'pid %s of the namespace is a member' % inner)
        return found[0]

    def test_w1_kills_a_setsid_tree_and_its_orphans_through_its_pid_1(self):
        job = self.job()
        self.assertIsInstance(job, self.c.NsJob)
        self.addCleanup(job.close)
        p = job.start(['sh', '-c', 'setsid sh -c "sleep 300 & echo \\$!; exec sleep 300" & '
                       'wait'], stdout=subprocess.PIPE)
        self.addCleanup(p.stdout.close)
        orphan = self.outer(job, lines(p, 1)[0][0])
        self.strays.append(orphan)
        reaper = job.reaper
        self.assertEqual(job.contains(orphan), 'in')
        self.assertEqual(job.contains(job.command), 'in')
        self.assertEqual(job.contains(os.getpid()), 'out')
        self.assertNotIn(reaper, job.pids())
        self.assertNotIn(p.pid, job.pids())
        self.assertTrue(job.terminate())
        self.assertEqual(p.wait(10), -signal.SIGKILL)
        for pid, what in ((orphan, 'the setsid orphan'), (reaper, 'pid 1')):
            wait_until(lambda: gone(pid), '%s ended' % what)
        self.assertEqual(job.pids(), [])
        self.assertIsNone(job.processes())

    def test_the_command_sees_its_own_namespace_a_fresh_proc_and_its_own_ids(self):
        job = self.job()
        self.addCleanup(job.close)
        p = job.start(['sh', '-c', 'echo $$; id -u; id -g; ls /proc | grep -c "^[0-9]"; '
                       'readlink /proc/self/ns/pid'], stdout=subprocess.PIPE)
        out = p.communicate(timeout=20)[0].decode().split()
        self.assertEqual(p.returncode, 0)
        self.assertEqual(out[0], '2')                   # pid 1 is the reaper, the stub execs
        self.assertEqual(out[1:3], [str(os.getuid()), str(os.getgid())])
        self.assertLessEqual(int(out[3]), 4, out)       # the reaper, sh, ls and grep at most
        self.assertNotEqual(out[4], os.readlink('/proc/self/ns/pid'))
        self.assertEqual(int(out[4].strip('pid:[]')), job.ns)
        self.assertNotEqual(job.ns, self.c.lproc.pidns())
        self.assertEqual(self.c.lproc.pidns(), self.c.lproc.pidns(os.getpid()))

    def test_the_exit_code_the_signal_and_the_pipes_are_the_command_s(self):
        job = self.job(kill_on_close=False)
        p = job.start([sys.executable, '-c', 'import sys; sys.stdout.write(sys.stdin.read()); '
                       'sys.exit(7)'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.assertEqual(p.communicate(b'through', timeout=20), (b'through', None))
        self.assertEqual(p.returncode, 7)
        job.close()
        job = self.job()
        p = job.start(['sh', '-c', 'kill -TERM $$'])
        self.assertEqual(p.wait(20), -signal.SIGTERM)
        job.close()

    def test_w1_jobs_started_at_once_by_a_busy_owner_each_start_and_hold_no_stub_pipe(self):
        busy_starts(self, self.job)

    def test_a_w1_job_that_ended_cleanly_leaves_no_pid_1(self):
        for kill_on_close in (True, False):
            with self.subTest(kill_on_close=kill_on_close):
                job = self.job(kill_on_close=kill_on_close)
                job.start(['true']).wait(20)
                reaper = job.reaper
                self.assertNotIn(reaper, job.pids())
                job.close()
                wait_until(lambda: gone(reaper), 'pid 1 ended with its job')

    def test_a_killed_starter_ends_its_job(self):
        job = self.job(kill_on_close=False)
        self.addCleanup(job.close)
        p = job.start(['sh', '-c', 'setsid sleep 300 & echo $!; exec sleep 300'],
                      stdout=subprocess.PIPE)
        self.addCleanup(p.stdout.close)
        orphan = self.outer(job, lines(p, 1)[0][0])
        self.strays.append(orphan)
        command, reaper = job.command, job.reaper
        p.kill()
        p.wait(10)
        for pid, what in ((command, 'the command'), (orphan, 'its setsid child'),
                          (reaper, 'pid 1')):
            wait_until(lambda: gone(pid), '%s ended with the starter' % what)

    def test_the_starter_and_pid_1_ignore_the_terminal_s_signals(self):
        job = self.job()
        self.addCleanup(job.close)
        p = job.start(['sleep', '300'])
        self.addCleanup(lambda: (job.close(), p.wait(10)))
        for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGHUP):
            os.kill(p.pid, sig)
        os.kill(job.reaper, signal.SIGINT)
        os.kill(job.reaper, signal.SIGTERM)
        time.sleep(0.3)
        self.assertIsNone(p.poll())
        self.assertEqual(job.contains(job.command), 'in')
        self.assertFalse(gone(job.reaper))

    def test_an_owner_killed_by_sigkill_takes_its_w1_job_s_tree_and_pid_1_with_it(self):
        p = self.owner('''
            from kitlib.platform.linux import contain
            job = Job(tier=%s)
            c = job.start(['sh', '-c', 'setsid sleep 300 & echo $!; exec sleep 300'],
                          stdout=subprocess.PIPE)
            g = int(c.stdout.readline())
            while not [p for p in job.pids() if contain._nspid(p)[-1:] == [g]]:
                time.sleep(0.01)
            print(c.pid, job.reaper, *job.pids(), flush=True)
            time.sleep(300)
        ''' % W1)
        pids = [int(x) for x in lines(p, 1)[0]]
        self.assertGreaterEqual(len(pids), 4, pids)     # the starter, pid 1, sleep, setsid sleep
        self.strays += pids
        p.kill()
        p.wait()
        for pid in pids:
            wait_until(lambda: gone(pid), 'pid %d of the job ended with its owner' % pid)

    def make(self):
        return self.job()

    def test_an_owner_killed_before_go_runs_no_w1_command(self):
        self.owner_dies_before_go('Job(kill_on_close=%%s, tier=%s)' % W1, 'job.reaper')

    def test_a_w1_start_that_fails_before_go_ends_its_stub_and_runs_nothing(self):
        seen = self.start_fails_before_go(self.job(), lambda j: j.reaper)
        self.assertIsNotNone(seen['wd'])

    def test_a_forged_w1_ready_line_is_refused(self):
        self.forged('WSTUB', 'echo "s $$"', 'echo "s 1"', 'the ready line named pid')
        self.forged('NSTART', "say('r %d' % pid)", "say('r %d' % os.getpid())",
                    'the ready line named pid')

    def test_leftovers_are_spared_by_the_w1_job_s_kill(self):
        job = self.job()
        self.addCleanup(job.close)
        p = job.start(['sh', '-c', 'setsid sleep 300 >/dev/null 2>&1 & echo $!'],
                      stdout=subprocess.PIPE)
        daemon = self.outer(job, p.communicate(timeout=20)[0].decode().strip())
        self.strays.append(daemon)
        reaper = job.reaper
        self.assertEqual(job.leftovers(), [daemon])
        job.close()
        time.sleep(0.3)
        self.assertFalse(gone(daemon))
        self.assertFalse(gone(reaper))                  # pid 1 stays while they run
        kill_own(daemon)
        wait_until(lambda: gone(reaper), 'pid 1 ended after its last spared process')

    def test_a_probe_inside_the_job_knows_it_and_runs_no_scope_trial(self):
        job = self.job()
        self.addCleanup(job.close)
        p = job.start([sys.executable, '-c', 'import os; from kitlib.platform.linux import '
                       'contain as c; print(os.environ[c.W1_ENV], c._in_w1(), c.probe().name)'],
                      cwd=os.getcwd(), env=self.owner_env(), stdout=subprocess.PIPE)
        out = p.communicate(timeout=60)[0].decode().split()
        self.assertEqual(p.returncode, 0)
        self.assertEqual(out, [str(job.ns), 'True', 'W2'])

    def test_peak_memory_is_the_command_s_own(self):
        job = self.job()
        self.addCleanup(job.close)
        p = job.start([sys.executable, '-c', 'b = bytearray(64 << 20); print(len(b), flush=True);'
                       ' import time; time.sleep(60)'], stdout=subprocess.PIPE)
        self.addCleanup(p.stdout.close)
        self.addCleanup(lambda: (job.close(), p.wait(10)))
        lines(p, 1)
        cpu_s, peak, now = job.usage()
        self.assertGreater(now, 64 << 20)
        self.assertGreater(peak, 32 << 20)
        self.assertGreater(cpu_s, 0)
        job.close()

if __name__ == '__main__':
    unittest.main()
