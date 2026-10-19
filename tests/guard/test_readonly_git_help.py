"""Read-only git the write jail lets run in another checkout (patch-id, apply --check: rules.git_reads,
rules.apply_checks), and a piped `machine_lock.py run --help` the wrapped-gate rule (P16) lets pass
(rules.lock_run_help), as does a piped gate.py --help, --plan or --measure (rules.gate_runs_nothing).
git symbolic-ref with one ref, grep -O and archive --remote/--exec read (rules.git_reads,
SYMREF_READ_OPTS)."""
import unittest

from . import world as W
from .claudehost import decide
from .test_jail import Base, COORDINATOR
from .world import bash, ps


class ReadOnlyGitElsewhere(Base):
    """Both forms were refused by write-jail in a folder outside the session's roots (observed)."""

    def test_patch_id_reads(self):
        d = W.fwd(self.w.plain)
        self.allowed(self.run_bash('git -C "%s" patch-id --stable < a.diff' % d))
        self.allowed(self.run_bash('git -C "%s" diff main | git -C "%s" patch-id' % (d, d)))

    def test_apply_check_reads(self):
        d = W.fwd(self.w.plain)
        for text in ('git -C "%s" apply --check a.patch' % d, 'git -C "%s" apply --check --stat a.patch' % d,
                     'git -C "%s" apply --stat --check -p1 a.patch' % d):
            with self.subTest(text):
                self.allowed(self.run_bash(text))
        self.allowed(self.run_ps('git -C "%s" apply --check --stat a.patch' % d))

    def test_apply_that_applies_is_still_refused(self):
        d = W.fwd(self.w.plain)
        for text in ('git -C "%s" apply a.patch' % d, 'git -C "%s" apply --stat a.patch' % d,
                     'git -C "%s" apply --check --apply a.patch' % d,     # --apply applies after all
                     'git -C "%s" apply --check --app a.patch' % d,       # git expands the prefix to --apply
                     'git -C "%s" apply --check --index a.patch' % d):    # an option it does not list
            with self.subTest(text):
                self.jailed(self.run_bash(text))
        # a word the guard cannot read may hold --apply: still refused
        self.assertEqual(self.run_bash('git -C "%s" apply --check $X a.patch' % d).verdict, 'deny')
        self.assertEqual(self.run_ps('git -C "%s" apply --check @a a.patch' % d).verdict, 'deny')

    def test_apply_check_after_double_dash_is_refused(self):
        # git reads no option after --: `-- --check` applies a.patch, and a file named --check
        d = W.fwd(self.w.plain)
        for text in ('git -C "%s" apply a.patch -- --check' % d, 'git -C "%s" apply -- --check' % d):
            with self.subTest(text):
                self.jailed(self.run_bash(text))
        self.allowed(self.run_bash('git -C "%s" apply --check -- a.patch' % d))


class SymbolicRefAndProgramReads(Base):
    """`git -C <root>/$r symbolic-ref --short refs/remotes/origin/HEAD` (a coordinator's loop over
    its repositories) was refused as unplaced: symbolic-ref with one ref only prints where it
    points. grep -O and archive --remote/--exec were refused as running a program; only an
    attacker hides a write in one (R-2271), so they read like any grep or archive."""

    def setUp(self):
        Base.setUp(self)
        W.deny_softened()
        self.addCleanup(W.allow_unplaced)

    def each_cwd_and_role(self, text, run):
        for cwd in (self.w.wt, self.w.main):
            for env, sub in (({}, True), (COORDINATOR, False)):
                with self.subTest(text, cwd=cwd, env=env):
                    yield run(text, cwd=cwd, sub=sub, env=env)

    def test_symbolic_ref_reads_with_a_computed_checkout(self):
        root = W.fwd(self.w.temp)
        for text in ('git -C %s/$r symbolic-ref --short refs/remotes/origin/HEAD' % root,
                     'git -C "%s/$r" symbolic-ref --short refs/remotes/origin/HEAD' % root,
                     'for r in a b; do git -C %s/$r symbolic-ref --short refs/remotes/origin/HEAD; done' % root,
                     'git -C $p symbolic-ref -q HEAD', 'git -C $p symbolic-ref --quiet --short HEAD'):
            for d in self.each_cwd_and_role(text, self.run_bash):
                self.allowed(d)
        text = 'foreach ($r in "a", "b") { git -C "%s/$r" symbolic-ref --short refs/remotes/origin/HEAD }' % root
        for d in self.each_cwd_and_role(text, self.run_ps):
            self.allowed(d)

    def test_symbolic_ref_writes_stay_unplaced(self):
        for text in ('git -C $p symbolic-ref HEAD refs/heads/x', 'git -C $p symbolic-ref -d HEAD',
                     'git -C $p symbolic-ref --delete HEAD', 'git -C $p symbolic-ref -m why HEAD refs/heads/x',
                     'git -C $p symbolic-ref --d HEAD', 'git -C $p symbolic-ref'):
            with self.subTest(text):
                self.jailed(self.run_bash(text), 'unplaced')

    def test_grep_o_and_archive_remote_read(self):
        self.allowed(self.run_bash('git -C $p grep -O foo'))
        self.allowed(self.run_bash('git -C $p grep --open-files-in-pager=less foo'))
        d = W.fwd(self.w.plain)
        for text in ('git -C "%s" archive --remote=x HEAD' % d, 'git -C "%s" archive --exec=x --remote=y HEAD' % d,
                     'git -C "%s" grep -nO foo' % d):
            with self.subTest(text):
                self.allowed(self.run_bash(text))


class LockRunHelpPiped(unittest.TestCase):
    """A piped run --help was refused as a wrapped gate (observed); it prints usage and runs nothing."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def judge(self, text, shell=bash):
        return decide(shell(text, self.w.wt, sub=True), self.w.ctx())

    def test_bare_help_piped_passes(self):
        for text in ('python tools/machine_lock.py run --help | head -40',
                     'python tools/machine_lock.py run -h 2>&1 | head'):
            with self.subTest(text):
                d = self.judge(text)
                self.assertIsNone(d, d and d.reason)
        d = self.judge('python tools/machine_lock.py run --help | Select-Object -First 20', ps)
        self.assertIsNone(d, d and d.reason)

    def test_help_with_more_words_or_a_real_run_is_refused(self):
        W.deny_softened()                       # wrapped-gate is off by default (test_cheap.py)
        self.addCleanup(W.allow_unplaced)
        for text in ('python tools/machine_lock.py run --help -- python x.py | head',
                     'python tools/machine_lock.py run --kind build --help | head',
                     'python tools/machine_lock.py run --kind build -- python x.py --help | head',
                     'python tools/machine_lock.py run $H | head',
                     'python tools/machine_lock.py run --kind build -- python x.py | tail'):
            with self.subTest(text):
                d = self.judge(text)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', 'wrapped-gate'), d.reason)


class GatePipedRunsNothing(unittest.TestCase):
    """gate.py --help, --plan or --measure runs no gate step and takes no lock: it may be piped
    (docs/guard-v2.md B9, rules.gate_runs_nothing). The default steps, --full, --only, land and
    wait-result stay refused piped."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        W.deny_softened()                       # wrapped-gate is off by default (test_cheap.py)
        self.addCleanup(W.allow_unplaced)

    def judge(self, text, shell=bash):
        return decide(shell(text, self.w.wt, sub=True), self.w.ctx())

    def test_runs_nothing_piped_passes(self):
        for text in ('python tools/gate.py --help | head', 'python tools/gate.py -h 2>&1 | head',
                     'python tools/gate.py --plan | head', 'python tools/gate.py --full --plan | tail',
                     'python tools/gate.py -C $WT --plan | head',
                     'python tools/gate.py --measure --out m.json | tail',
                     'python tools/backlog.py list | head'):       # not a wrapper at all
            with self.subTest(text):
                d = self.judge(text)
                self.assertIsNone(d, d and d.reason)
        d = self.judge('python tools/gate.py --plan | Select-Object -First 5', ps)
        self.assertIsNone(d, d and d.reason)

    def test_a_run_piped_is_still_refused(self):
        for text in ('python tools/gate.py --full | tail', 'python tools/gate.py | tail',
                     'python tools/gate.py --only unit | tail',
                     'python tools/gate.py land b --expect-tip abc1234 | tail',
                     'python tools/gate.py land --help | tail',
                     'python tools/gate.py wait-result b --tip abc1234 | tail',
                     'python tools/gate.py $X --plan | head',
                     'python tools/gate.py --pla | head'):
            with self.subTest(text):
                d = self.judge(text)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', 'wrapped-gate'), d.reason)


if __name__ == '__main__':
    unittest.main()
