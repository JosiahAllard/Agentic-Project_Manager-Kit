"""git archive reads the repository it runs in; only its -o/--output file and the folder a tar
fed by it extracts into are written (docs/guard.md "Write jail", the git bullet, and "Shell writes").

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


class ArchiveWorld(unittest.TestCase):
    """A fake machine and the decisions for the PM's session and a subagent's (no tests of its own)."""

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


class ArchiveReadOnly(ArchiveWorld):
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


class ArchiveHoles(ArchiveWorld):
    """The two writes into a main checkout the guard let through before this fix (rules.write_targets
    returned before _tar under PowerShell; rules.git_targets did not know archive -o), the tar -C
    spellings _tar did not read, and the archive forms that stay no read (rules.archive_outputs)."""

    def test_powershell_tar_into_a_main_checkout_is_refused(self):
        for text in ('tar -x -C %s', 'tar.exe -x -f a.tar -C %s', 'tar -xf a.tar --directory=%s'):
            text = text % self.main
            with self.subTest(text):
                self.refused_by(self.pm(ps(text, self.w.plain)), 'write-jail', 'tar')
                self.refused_by(self.sub(ps(text, self.w.wt, sub=True)), 'main-checkout', 'tar')

    def test_powershell_tar_into_scratch_passes(self):
        self.assertIsNone(self.pm(ps('tar -x -f a.tar -C %s' % self.scratch, self.w.plain)))

    def test_a_subagent_archive_o_into_a_main_checkout_from_its_worktree_is_refused(self):
        for shell in (bash, ps):
            for opt in ('-o %s/a.tar', '--output=%s/a.tar', '-vo %s/a.tar', '--o=%s/a.tar'):
                text = 'git -C %s archive %s HEAD' % (self.wt, opt % self.main)
                with self.subTest(shell=shell.__name__, text=text):
                    d = self.sub(shell(text, self.w.wt, sub=True))
                    self.refused_by(d, 'write-jail', 'git')
                    self.assertIn('makes %s/a.tar' % self.main, d.reason)

    def test_the_instead_keeps_an_attached_output_option(self):
        d = self.pm(bash('git -C %s archive --output=%s/a.tar HEAD' % (self.main, self.main), self.w.plain))
        self.assertIsNotNone(d)
        self.assertIn('--output=', d.instead)
        self.assertNotIn('%s/a.tar' % self.main, d.instead)

    def test_clustered_and_abbreviated_tar_folders_are_read(self):
        for text in ('tar -xC %s', 'tar -xvC%s', 'tar -xf a.tar --dir=%s', 'tar -xf a.tar --directory %s'):
            text = text % self.main
            with self.subTest(text):
                self.refused_by(self.pm(bash(text, self.w.plain)), 'write-jail', 'tar')

    def test_a_relative_tar_folder_is_read_from_the_one_before_it(self):
        # -C <base> -C repo extracts into <base>/repo, the main checkout; read against the cwd it was plain/repo
        text = 'tar -x -C %s -C repo' % W.fwd(self.w.base)
        self.refused_by(self.pm(bash(text, self.w.plain)), 'write-jail', 'tar')
        self.assertIsNone(self.pm(bash('tar -x -C %s -C sub' % self.scratch, self.w.plain)))

    def test_archive_forms_the_guard_cannot_read_are_no_read(self):
        for opt in ('$X', '--bogus', '--add=x', '--no-worktree-attributes', '--remote=origin', '--rem=origin',
                    '--e=/tmp/x.sh', '-q', '-o $X', '-- $X'):
            text = 'git -C %s archive %s HEAD' % (self.main, opt)
            with self.subTest(text):
                d = self.pm(bash(text, self.w.plain))
                self.assertIsNotNone(d)
                self.assertEqual('write-jail', d.rule, d.reason)

    def test_archive_flags_it_knows_are_a_read(self):
        for opt in ('-v', '-l', '--list', '-9', '-v9', '--worktree-attributes', '--format tar --prefix p/',
                    '--add-file a.txt', '--mtime=2020-01-01', '--verb'):
            text = 'git -C %s archive %s HEAD' % (self.main, opt)
            with self.subTest(text):
                self.assertIsNone(self.pm(bash(text, self.w.plain)))

    def test_an_unplaced_archive_reads_only_without_o(self):
        gd = '--git-dir="%s/.git"' % self.main
        self.assertIsNone(self.sub(bash('git %s archive HEAD' % gd, self.w.wt, sub=True)))
        for text in ('git %s archive -o %s/a.tar HEAD' % (gd, self.scratch),
                     'git %s archive --o=%s/a.tar HEAD' % (gd, self.scratch)):
            with self.subTest(text):
                d = self.sub(bash(text, self.w.wt, sub=True))
                self.assertIsNotNone(d)
                self.assertEqual('write-jail', d.rule, d.reason)


if __name__ == '__main__':
    unittest.main()
