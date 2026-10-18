"""Read-only git the write jail lets run in another checkout (patch-id, apply --check: rules.git_reads,
rules.apply_checks), and a piped `machine_lock.py run --help` the wrapped-gate rule (P16) lets pass
(rules.lock_run_help)."""
import unittest

from . import world as W
from .claudehost import decide
from .test_jail import Base
from .world import bash, ps


class ReadOnlyGitElsewhere(Base):
    """KIT-GUARD-READONLY-GIT-WRITEJAIL: both forms were refused by write-jail in a folder outside
    the session's roots."""

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


class LockRunHelpPiped(unittest.TestCase):
    """KIT-GUARD-WRAPPED-GATE-HELP-FALSE-POSITIVE: run --help prints usage and runs nothing."""

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
        for text in ('python tools/machine_lock.py run --help -- python x.py | head',
                     'python tools/machine_lock.py run --kind build --help | head',
                     'python tools/machine_lock.py run --kind build -- python x.py --help | head',
                     'python tools/machine_lock.py run $H | head',
                     'python tools/machine_lock.py run --kind build -- python x.py | tail'):
            with self.subTest(text):
                d = self.judge(text)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', 'wrapped-gate'), d.reason)


if __name__ == '__main__':
    unittest.main()
