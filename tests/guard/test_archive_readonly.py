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

    def test_an_o_after_a_double_dash_is_a_pathspec(self):
        # git reads every word after `--` as a pathspec (git 2.55: "'<f>' is outside repository")
        for tail in ('-- -o', '-- -o README.md', '-- --output=README.md'):
            text = 'git -C %s archive HEAD %s' % (self.main, tail)
            with self.subTest(text):
                self.assertIsNone(self.pm(bash(text, self.w.plain)))


class ArchiveFilters(ArchiveWorld):
    """git archive runs tar.<format>.command as a filter in the checkout it reads (git 2.55, cwd
    observed), so a filter set on the line makes the archive a program run, as --exec does: -c,
    --config-env, an include.path that may set one, a computed -c, and the GIT_CONFIG_* variables."""

    def refused(self, shell, text):
        d = self.pm(shell(text, self.w.plain))
        self.assertIsNotNone(d)
        self.assertEqual('write-jail', d.rule, d.reason)
        self.assertIsNotNone(self.sub(shell(text, self.w.wt, sub=True)))

    def test_a_tar_filter_set_with_c_is_no_read(self):
        for shell in (bash, ps):
            for cfg in ('-c tar.x.command="sh -c \'echo x > README.md\'"', '-c tar.x.command=x.sh',
                        '-ctar.x.command=x.sh', '-c TAR.X.Command=x.sh', '-c tar.tar.gz.command=x.sh',
                        '-c tar.x.command', '--config-env=tar.x.command=CMD', '--config-env tar.x.command=CMD',
                        '-c include.path=%s/x.cfg' % self.scratch, '-c includeIf.onbranch:main.path=x.cfg'):
                text = 'git -C %s %s archive --format=x HEAD' % (self.main, cfg)
                with self.subTest(shell=shell.__name__, text=text):
                    self.refused(shell, text)

    def test_a_computed_c_is_no_read(self):
        self.refused(bash, 'git -C %s -c $C archive HEAD' % self.main)

    def test_git_config_variables_are_no_read(self):
        for text in ('GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=tar.x.command GIT_CONFIG_VALUE_0=x.sh '
                     'git -C %s archive --format=x HEAD',
                     'GIT_CONFIG_PARAMETERS="\'tar.x.command\'=\'x.sh\'" git -C %s archive --format=x HEAD',
                     'export GIT_CONFIG_PARAMETERS="\'tar.x.command\'=\'x.sh\'"; git -C %s archive --format=x HEAD'):
            text = text % self.main
            with self.subTest(text):
                self.refused(bash, text)
        self.refused(ps, '$env:GIT_CONFIG_PARAMETERS = "\'tar.x.command\'=\'x.sh\'"; git -C %s archive --format=x HEAD'
                     % self.main)

    def test_other_settings_on_the_line_keep_it_a_read(self):
        for shell in (bash, ps):
            for cfg in ('-c core.quotepath=false', '-c tar.umask=0022', '-c tar.x.remote=true'):
                text = 'git -C %s %s archive HEAD' % (self.main, cfg)
                with self.subTest(shell=shell.__name__, text=text):
                    self.assertIsNone(self.pm(shell(text, self.w.plain)))


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

    def test_an_o_after_an_option_git_s_first_pass_takes_no_value_for_is_an_output(self):
        # builtin/archive.c cmd_archive opens -o before --prefix takes its value: README.md is truncated
        for opt in ('--prefix -o README.md', '--format -o README.md', '--mtime --output README.md',
                    '--prefix --output=README.md', '--add-file -oREADME.md'):
            text = 'git -C %s archive %s HEAD HEAD' % (self.main, opt)
            with self.subTest(text):
                d = self.pm(bash(text, self.w.plain))
                self.assertIsNotNone(d)
                self.assertEqual('write-jail', d.rule, d.reason)
        text = 'git -C %s archive --prefix -o %s/README.md HEAD HEAD' % (self.wt, self.main)
        self.refused_by(self.sub(bash(text, self.w.wt, sub=True)), 'write-jail', 'git')

    def test_an_unplaced_archive_with_o_after_prefix_is_no_read(self):
        text = 'git --git-dir="%s/.git" archive --prefix -o %s/f HEAD HEAD' % (self.main, self.main)
        d = self.sub(bash(text, self.w.wt, sub=True))
        self.assertIsNotNone(d)
        self.assertEqual('write-jail', d.rule, d.reason)

    def test_an_unplaced_archive_reads_only_without_o(self):
        gd = '--git-dir="%s/.git"' % self.main
        self.assertIsNone(self.sub(bash('git %s archive HEAD' % gd, self.w.wt, sub=True)))
        for text in ('git %s archive -o %s/a.tar HEAD' % (gd, self.scratch),
                     'git %s archive --o=%s/a.tar HEAD' % (gd, self.scratch)):
            with self.subTest(text):
                d = self.sub(bash(text, self.w.wt, sub=True))
                self.assertIsNotNone(d)
                self.assertEqual('write-jail', d.rule, d.reason)


class TarFolders(ArchiveWorld):
    """rules._tar reads -C only where tar does: a C after a letter that takes a value, or in a word
    that is another option's value, may be no -C, so the working directory is judged as well (fail
    closed); an old-style first word (tar xCf <dir> a.tar) takes its values in turn."""

    def in_main(self, tail):
        return 'cd %s && tar %s' % (self.main, tail)

    def test_a_c_that_may_be_a_value_judges_the_working_directory(self):
        for tail in ('-xf %s/a.tar -NC%s', '-xf a.tar --exclude -C%s', '-xfC %s', '-xf -C%s',
                     '-xkfC %s', '-x -f a.tar -T -C%s', '-x --newer -C %s'):
            text = self.in_main(tail % ((self.scratch, self.scratch) if tail.count('%s') == 2 else self.scratch))
            with self.subTest(text):
                self.refused_by(self.pm(bash(text, self.w.plain)), 'write-jail', 'tar')
                self.refused_by(self.sub(bash(text, self.w.wt, sub=True)), 'main-checkout', 'tar')

    def test_a_c_after_letters_that_take_no_value_is_read(self):
        for tail in ('-xvC %s -f a.tar', '-xzvC%s -f a.tar', '-x -f a.tar -C %s', '-xf a.tar -C%s',
                     '-xvf a.tar --directory %s', '--extract --verbose -C %s', '-x --exclude=-Cx -C %s'):
            text = self.in_main(tail % self.scratch)
            with self.subTest(text):
                self.assertIsNone(self.pm(bash(text, self.w.plain)))

    def test_an_old_style_first_word_takes_its_values_in_turn(self):
        self.refused_by(self.pm(bash('tar xCf %s a.tar' % self.main, self.w.plain)), 'write-jail', 'tar')
        self.refused_by(self.pm(bash('tar xfC a.tar %s' % self.main, self.w.plain)), 'write-jail', 'tar')
        self.assertIsNone(self.pm(bash(self.in_main('xCf %s a.tar' % self.scratch), self.w.plain)))
        self.assertIsNone(self.pm(bash(self.in_main('xfC a.tar %s' % self.scratch), self.w.plain)))
        # a letter the guard does not know may take a value: the folder may be either word, or none
        self.refused_by(self.pm(bash(self.in_main('xqCf %s a.tar' % self.scratch), self.w.plain)),
                        'write-jail', 'tar')

    def test_an_old_style_folder_in_scratch_from_powershell_passes(self):
        self.assertIsNone(self.pm(ps('tar xCf %s a.tar' % self.scratch, self.w.plain)))

    def test_bsdtar_cd_is_a_folder(self):
        # bsdtar (Windows' tar.exe): libarchive's tar long options map --cd to -C
        for shell in (bash, ps):
            for opt in ('--cd %s', '--cd=%s'):
                with self.subTest(shell=shell.__name__, opt=opt):
                    text = 'tar -xf a.tar ' + opt
                    self.refused_by(self.pm(shell(text % self.main, self.w.temp)), 'write-jail', 'tar')
                    self.assertIsNone(self.pm(shell(text % self.scratch, self.w.main)))


if __name__ == '__main__':
    unittest.main()
