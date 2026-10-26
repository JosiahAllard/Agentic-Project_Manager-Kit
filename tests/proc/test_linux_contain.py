"""Containment on Linux (kitlib.platform.linux.contain; docs/linux-support-plan.md section 2.3,
slice 5): kitlib.proc.job.Job on a cgroup scope (S1, S2) or on the weak tier (W2).

On every platform: which cgroups a job may kill (only one it made, checked before any write), the
tier's report line, the scope prefix, a nested owner's tier, and the W1 trial read from what it
prints, never its exit code.

On Linux, on real processes: a setsid grandchild ends with its job; memory.peak is read; the pin
is the cgroup's; an owner killed by SIGKILL takes its job's tree and its watchdog with it; a job
that ended cleanly leaves no watchdog; a released job's daemon runs on and leftovers() takes it out
of the job; a job made inside a job is nested in it; a systemd-run that runs the command outside
any scope is caught; the forced weak tier kills a setsid tree. Every scope a test makes is named
`pmkit-containment-...-<this pid>-...` and is checked gone after each test."""
import importlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
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

    def test_a_process_in_a_kit_job_reads_its_tier_from_that_job_and_runs_no_trial(self):
        c = contain()
        with mock.patch.object(c, 'parent_job', return_value=TOP), \
                mock.patch.object(c, 'trial', side_effect=AssertionError('a trial ran')), \
                mock.patch.object(c, 'w1_status', return_value='W1 unavailable (x)'), \
                mock.patch.object(c, 'nested_tier', return_value=c.Tier('S1', parent=TOP)):
            self.assertEqual((c._probe().name, c._probe().parent), ('S1', TOP))
        with mock.patch.object(c, 'parent_job', return_value=TOP), \
                mock.patch.object(c, 'trial', side_effect=AssertionError('a trial ran')), \
                mock.patch.object(c, 'w1_status', return_value='W1 unavailable (x)'), \
                mock.patch.object(c, 'nested_tier', return_value=None):
            tier = c._probe()
        # never a scope of its own, which would take its jobs out of the job it runs in
        self.assertEqual(tier.name, 'W2')
        self.assertIn('it runs in kit job %s' % TOP, tier.text)

    def test_the_w1_trial_is_read_from_what_it_prints_never_its_exit_code(self):
        c = contain()
        for out, rc, want in ((b'ok\n', 1, 'W1 available, not built'),
                              (b'user namespaces denied: [Errno 1] x\n', 0,
                               'W1 unavailable (user namespaces denied: [Errno 1] x)'),
                              (b'', 0, 'W1 unavailable (its trial said nothing)')):
            with self.subTest(out=out, rc=rc), mock.patch.object(
                    c.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], rc, out, b'')):
                self.assertEqual(c.w1_status(), want)


def unit_names():
    """The pmkit-containment units this process made that systemd still lists, any state."""
    r = subprocess.run(['systemctl', '--user', 'list-units', '--all', '--plain', '--no-legend',
                        PREFIX + '-*'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=30, check=False)
    mine = '-%d-' % os.getpid()
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
            f.write('#!/bin/sh\nwhile [ "$1" != -- ]; do shift; done; shift; exec "$@"\n')
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

    def test_the_probe_finds_a_scope_that_is_killed_and_goes(self):
        tier = self.c.probe(fresh=True)
        self.assertTrue(tier.scoped, tier.text)
        self.assertIn(tier.name, ('S1', 'S2'))


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


if __name__ == '__main__':
    unittest.main()
