"""git archive reads the repository it runs in; only its -o/--output file and the folder a tar
fed by it extracts into are written (KIT-GUARD-ARCHIVE-READONLY; docs/guard.md "Write jail").

The repro from the PM's session: `git -C <kit> archive <sha> | tar -x -C <scratch>` was refused as
a write to the kit checkout. These rows pin both sides: the archive passes, the tar side is judged
by its -C target, and an archive whose -o writes a main checkout stays refused."""
import os
import unittest

from kitlib.guard import context

from . import world as W
from .claudehost import decide
from .world import bash, ps

PM = {context.ROLE_ENV: 'pm'}


class ArchiveReadOnly(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.main = W.fwd(self.w.main)
        self.wt = W.fwd(self.w.wt)
        self.scratch = W.fwd(os.path.join(self.w.temp, 'x'))

    def pm(self, payload):
        """The decision for the PM's marked session (held by the write jail), run in its own folder."""
        return decide(payload, self.w.ctx(environ=PM))

    def sub(self, payload):
        return decide(payload, self.w.ctx())

    def refused_by(self, d, rule, program):
        """d is a deny of `rule` whose reason names `program` as the writer (not the archive)."""
        self.assertIsNotNone(d)
        self.assertEqual(rule, d.rule, d.reason)
        self.assertTrue(d.reason.startswith('`%s' % program), d.reason)

    # ---------------------------------------------------------------- the repro passes
    def test_the_pm_extracts_an_archive_of_a_checkout_into_scratch(self):
        for shell in (bash, ps):
            with self.subTest(shell.__name__):
                text = 'git -C %s archive HEAD | tar -x -C %s' % (self.main, self.scratch)
                self.assertIsNone(self.pm(shell(text, self.w.plain)))

    def test_a_bare_archive_to_standard_output_is_a_read(self):
        for text in ('git -C %s archive HEAD' % self.main,
                     'git -C %s archive --format=tar --prefix=kit/ HEAD' % self.main,
                     'git -C %s archive --format tar HEAD -- docs' % self.main):
            with self.subTest(text):
                self.assertIsNone(self.pm(bash(text, self.w.plain)))

    def test_a_subagent_extracts_an_archive_of_its_main_checkout_into_scratch(self):
        text = 'git -C %s archive HEAD | tar -x -C %s' % (self.main, self.scratch)
        self.assertIsNone(self.sub(bash(text, self.w.wt, sub=True)))

    def test_an_archive_written_with_o_into_scratch_passes(self):
        for text in ('git -C %s archive -o %s/a.tar HEAD' % (self.main, self.scratch),
                     'git -C %s archive --output=%s/a.tar HEAD' % (self.main, self.scratch)):
            with self.subTest(text):
                self.assertIsNone(self.pm(bash(text, self.w.plain)))

    # ---------------------------------------------------------------- the tar side still writes
    def test_tar_into_a_main_checkout_is_still_refused_as_the_tar(self):
        text = 'git -C %s archive HEAD | tar -x -C %s' % (self.main, self.main)
        self.refused_by(self.pm(bash(text, self.w.plain)), 'write-jail', 'tar')
        self.refused_by(self.sub(bash(text, self.w.wt, sub=True)), 'main-checkout', 'tar')

    def test_tar_into_a_main_checkout_from_powershell_is_still_refused_as_the_tar(self):
        text = 'git -C %s archive HEAD | tar -x -C %s' % (self.main, self.main)
        self.refused_by(self.pm(ps(text, self.w.plain)), 'write-jail', 'tar')
        self.refused_by(self.sub(ps(text, self.w.wt, sub=True)), 'main-checkout', 'tar')

    # ---------------------------------------------------------------- archive -o still writes
    def test_archive_o_into_a_main_checkout_is_refused(self):
        for opt in ('-o %s/a.tar', '--output %s/a.tar', '--output=%s/a.tar', '--outp=%s/a.tar', '-o%s/a.tar'):
            out = opt % self.main
            with self.subTest(opt):
                d = self.pm(bash('git -C %s archive %s HEAD' % (self.main, out), self.w.plain))
                self.assertIsNotNone(d)
                self.assertEqual('write-jail', d.rule, d.reason)
                # from the session's own worktree too: the -o file is the write, not the checkout
                d = self.sub(bash('git -C %s archive %s HEAD' % (self.wt, out), self.w.wt, sub=True))
                self.assertIsNotNone(d, out)

    def test_archive_exec_runs_a_program_and_is_no_read(self):
        text = 'git -C %s archive --remote=. --exec=/tmp/x.sh HEAD' % self.main
        self.assertIsNotNone(self.pm(bash(text, self.w.plain)))


if __name__ == '__main__':
    unittest.main()
