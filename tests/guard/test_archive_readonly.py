"""git archive reads the repository it runs in; only its -o/--output file and the folder a tar
fed by it extracts into are written (docs/guard.md "Write jail", the git bullet, and "Shell writes").

The repro from the PM's session: `git -C <kit> archive <sha> | tar -x -C <scratch>` was refused as
a write to the kit checkout. These rows pin both sides: the archive passes, the tar side is judged
by its -C target, and an archive whose -o writes a main checkout stays refused."""
import os
import unittest

from kitlib.guard import context, rules
from kitlib.guard import shell as sh

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

    def refused(self, shell, text):
        """The PM's session refuses `text` in the write jail; a subagent's refuses it too."""
        d = self.pm(shell(text, self.w.plain))
        self.assertIsNotNone(d)
        self.assertEqual('write-jail', d.rule, d.reason)
        self.assertIsNotNone(self.sub(shell(text, self.w.wt, sub=True)))

    def archive_refused(self, shell, text):
        """refused, and the PM's refusal names the git archive (not another command of the line)."""
        self.refused(shell, text)
        d = self.pm(shell(text, self.w.plain))
        self.assertTrue(d.reason.startswith('`git'), d.reason)

    def kept_a_read(self, shell, text):
        """The PM's session runs `text` as a read: the line changes git's environment in a way the
        guard does not read, and it catches honest mistakes, not evasion (R-2271)."""
        self.assertIsNone(self.pm(shell(text, self.w.plain)))


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


class ArchiveConfigFailsClosed(ArchiveWorld):
    """An archive is a read only when the line leaves git's config alone: every -c or --config-env
    but a key on rules.ARCHIVE_SAFE_KEY_RE, each word read as the shell hands it to git, a word the
    guard cannot read, a moved user config (HOME and the like) and an unplaced git (--git-dir) make it
    no read, as on the main branch, where no archive in a main checkout was one."""

    def test_a_key_bash_unescapes_is_read_as_git_gets_it(self):
        for cfg in ('-c tar.x.comman\\d=x.sh', '-\\c tar.x.command=x.sh', '-c \\tar.x.command=x.sh',
                    '-c\\ tar.x.command=x.sh', '--config-en\\v=tar.x.command=V'):
            text = 'V=x.sh git -C %s %s archive --format=x HEAD' % (self.main, cfg)
            with self.subTest(text):
                self.refused(bash, text)
        for cfg in ('-`c tar.x.command=x.sh', '-c tar.x.comman`d=x.sh', '--`config-env=tar.x.command=V'):
            text = 'git -C %s %s archive --format=x HEAD' % (self.main, cfg)
            with self.subTest(text):
                self.refused(ps, text)

    def test_any_key_off_the_safe_list_is_no_read(self):
        attrs = '-c core.attributesFile=%s/attrs' % self.scratch
        for shell in (bash, ps):
            for cfg in (attrs + ' -c filter.x.smudge=x.sh', '-c filter.lfs.smudge=x.sh', '-c filter.x.process=x.sh',
                        '-c filter.x.clean=x.sh', attrs, '-p -c core.pager=x.sh', '-c core.fsmonitor=x.sh',
                        '-c core.quotepath=false -c filter.x.smudge=x.sh', '-c', '-c ""'):
                text = 'git -C %s %s archive HEAD' % (self.main, cfg)
                with self.subTest(shell=shell.__name__, text=text):
                    self.refused(shell, text)

    def test_a_word_the_guard_cannot_read_is_no_read(self):
        for text in ('git -C %s -$O archive HEAD', 'git -C %s --namespace $N archive HEAD',
                     'git -C %s -c "$K" archive HEAD'):
            text = text % self.main
            with self.subTest(text):
                self.refused(bash, text)
        # Windows PowerShell 5.1 passes '\"a -c b' to a program as three words and drops ''
        for text in ('git -C %s --namespace \'\\"a -c tar.x.command=x.sh\' archive HEAD',
                     'git -C %s --namespace \'p "q -c\' archive HEAD', 'git -C %s --namespace \'\' archive HEAD',
                     'git -C %s archive HEAD \'\\"a -o README.md\'', 'git -C %s archive \'x y\\\' HEAD'):
            text = text % self.main
            with self.subTest(text):
                self.refused(ps, text)

    def test_an_o_bash_unescapes_is_an_output(self):
        # bash drops every backslash here: git gets -o README.md and truncates it in the checkout
        for opt in ('\\-o README.md', '\\--output=README.md', '-\\o README.md', '\\--outp README.md'):
            text = 'git -C %s archive %s HEAD' % (self.main, opt)
            with self.subTest(text):
                self.refused(bash, text)

    def test_a_user_config_moved_on_the_line_is_no_read(self):
        for name in ('HOME', 'XDG_CONFIG_HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH'):
            for text in ('%s=%s git -C %s archive --format=x HEAD', 'export %s=%s; git -C %s archive --format=x HEAD'):
                text = text % (name, self.scratch, self.main)
                with self.subTest(text):
                    self.refused(bash, text)
            text = '$env:%s = "%s"; git -C %s archive --format=x HEAD' % (name, self.scratch, self.main)
            with self.subTest(text):
                self.refused(ps, text)
        # a config file named by GIT_CONFIG_GLOBAL or GIT_CONFIG_SYSTEM is set on purpose: not read
        for name in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM'):
            for text in ('%s=%s git -C %s archive --format=x HEAD', 'export %s=%s; git -C %s archive --format=x HEAD'):
                text = text % (name, self.scratch, self.main)
                with self.subTest(text):
                    self.kept_a_read(bash, text)
            text = '$env:%s = "%s"; git -C %s archive --format=x HEAD' % (name, self.scratch, self.main)
            with self.subTest(text):
                self.kept_a_read(ps, text)

    def test_an_unplaced_git_is_no_read(self):
        other = '%s/r/.git' % self.scratch
        for text in ('cd %s && git --git-dir=%s archive --format=x HEAD', 'cd %s && git --git-dir %s archive HEAD',
                     'cd %s && git --work-tree=. --git-dir=%s archive HEAD'):
            text = text % (self.main, other)
            with self.subTest(text):
                self.refused(bash, text)
        self.refused(ps, 'Set-Location %s; git --git-dir=%s archive --format=x HEAD' % (self.main, other))

    def test_safe_keys_bash_unescapes_keep_it_a_read(self):
        for text in ('git -C %s -c core.quote\\path=false archive HEAD', 'git -C %s -c "tar.umask=0022" archive HEAD',
                     'git -C %s -c core.autocrlf=false -c core.eol=lf archive HEAD'):
            text = text % self.main
            with self.subTest(text):
                self.assertIsNone(self.pm(bash(text, self.w.plain)))


class ArchiveProgramSearch(ArchiveWorld):
    """git looks a filter's bare program name up on the exec path, then PATH (Git for Windows sets
    filter.lfs.process = git-lfs filter-process), and archive reads the attributes of the tree it
    archives: --exec-path or a PATH change on the line makes the archive no read. So does a word
    cmd.exe passes on that the C runtime may split other than cmd reads it (a quote after a
    backslash, a quote inside a word)."""

    def test_an_exec_path_is_no_read(self):
        # a bare --exec-path prints the path and exits before the subcommand (git 2.55, observed)
        for shell in (bash, ps):
            for opt in ('--exec-path=%s' % self.scratch, '--exec-path="%s"' % self.scratch):
                text = 'git %s -C %s archive HEAD' % (opt, self.main)
                with self.subTest(shell=shell.__name__, text=text):
                    self.refused(shell, text)
        self.refused(bash, 'git -C %s --exec-pat\\h=%s archive HEAD' % (self.main, self.scratch))

    def test_a_path_change_on_the_line_is_no_read(self):
        archive = 'git -C %s archive HEAD' % self.main
        for text in ('PATH=%s:$PATH %s', 'export PATH=%s:$PATH; %s', 'env PATH=%s %s', 'PATH+=:%s %s',
                     'set -a; PATH=%s; %s', 'declare -x PATH=%s; %s'):
            text = text % (self.scratch, archive)
            with self.subTest(text):
                self.refused(bash, text)
        # PATH set by read is not read (R-2271)
        self.kept_a_read(bash, 'read PATH <<< %s; %s' % (self.scratch, archive))
        for text in ('$env:Path = "%s;" + $env:Path; %s', '$env:PATH += ";%s"; %s',
                     "[Environment]::SetEnvironmentVariable('Path', '%s'); %s", 'Set-Item env:Path %s; %s',
                     'Set-Item -Path env:PATH -Value %s; %s'):
            text = text % (self.scratch, archive)
            with self.subTest(text):
                self.refused(ps, text)
        self.refused(bash, "cmd //c 'set PATH=%s& %s'" % (self.scratch, archive))

    def test_a_word_cmd_and_the_c_runtime_read_apart_is_no_read(self):
        # cmd reads a\" -o README.md \"b as one word; git.exe gets a", -o, README.md, "b
        for text in ('git -C %s archive a\\" -o README.md \\"b HEAD',
                     'git -C %s --namespace a\\" -c tar.x.command=x.sh \\"b archive HEAD',
                     'git -C %s archive "a"" -o README.md" HEAD', 'git -C %s archive HEAD "x\\"'):
            text = text % self.main
            for shell, call in ((bash, "cmd //c '%s'"), (ps, "cmd /c '%s'")):
                with self.subTest(shell=shell.__name__, text=text):
                    self.refused(shell, call % text)

    def test_cmd_s_path_command_is_not_read(self):
        # cmd's own path command sets PATH (path X, path=X, path;X, and path ; which clears it), but
        # only on purpose: the guard does not read it (R-2271)
        archive = 'git -C %s archive HEAD' % self.main
        for spell in ('path {x};%PATH%', 'PATH {x}', 'Path={x}', 'path ;', 'path;{x}', 'path,{x}',
                      '@path {x}', '(path {x})'):
            line = spell.replace('{x}', self.scratch) + '& ' + archive
            for shell, call in ((bash, "cmd //c '%s'"), (ps, "cmd /c '%s'")):
                text = call % line
                with self.subTest(text):
                    self.kept_a_read(shell, text)

    def test_cmd_s_leading_at_and_delimiters_still_name_the_command(self):
        # cmd drops a leading @ and its delimiters (, ; =) before the command name, as a prefix of
        # the word or as words of their own: @set PATH=x is set (cmd's path command and a batch
        # file are not read: test_cmd_s_path_command_is_not_read)
        archive = 'git -C %s archive HEAD' % self.main
        for spell in ('@set PATH={x};%PATH%', '@set PATH={x}', '@set HOME={x}', '@SET GIT_DIR={x}',
                      '@ set PATH={x}', ',set PATH={x}', ';set PATH={x}', '=set HOME={x}', '@@set HOME={x}'):
            line = spell.replace('{x}', self.scratch) + '& ' + archive
            for shell, call in ((bash, "cmd //c '%s'"), (ps, "cmd /c '%s'")):
                text = call % line
                with self.subTest(text):
                    self.archive_refused(shell, text)
        for spell in ('@ path {x}', ', path {x}', '@;path {x}', '@call x.bat'):
            line = spell.replace('{x}', self.scratch) + '& ' + archive
            for shell, call in ((bash, "cmd //c '%s'"), (ps, "cmd /c '%s'")):
                text = call % line
                with self.subTest(text):
                    self.kept_a_read(shell, text)
        # what changes nothing still reads: a bare @path prints PATH, @echo prints its words
        for spell in ('@path', '@ path', '@echo hi', '@set'):
            for shell, call in ((bash, "cmd //c '%s& %s'"), (ps, "cmd /c '%s& %s'")):
                text = call % (spell, archive)
                with self.subTest(text):
                    self.assertIsNone(self.pm(shell(text, self.w.plain)))

    def test_cmd_s_leading_at_does_not_hide_a_git_env_move(self):
        # not an archive: a subagent's @set GIT_DIR or GIT_WORK_TREE moves its git into the main
        # checkout, as set does
        for spell in ('@set GIT_DIR={m}/.git& git commit -m x', ', set GIT_DIR={m}/.git& git commit -m x',
                      '@set GIT_WORK_TREE={m}& git checkout -- .'):
            line = spell.replace('{m}', self.main)
            for shell, call in ((bash, "cmd //c '%s'"), (ps, "cmd /c '%s'")):
                text = call % line
                with self.subTest(text):
                    self.assertIsNotNone(self.sub(shell(text, self.w.wt, sub=True)))

    def test_powershell_content_on_the_environment_sets_path(self):
        # the environment provider's content cmdlets and New-Item set the variable a path names, in
        # every spelling of the env: drive
        archive = 'git -C %s archive HEAD' % self.main
        drives = ('Environment::Path', 'ENVIRONMENT::path', 'Microsoft.PowerShell.Core\\Environment::Path',
                  'microsoft.powershell.core\\ENVIRONMENT::\\PATH', 'Env:Path', 'env:\\PATH')
        for verb, value in (('Set-Content', " -Value ';%s'" % self.scratch),
                            ('Add-Content', " -Value ';%s'" % self.scratch)):
            for drive in drives:
                for form in ('%s', '-Path %s', '-LiteralPath %s', '-Path:%s'):
                    text = '%s %s%s; %s' % (verb, form % drive, value, archive)
                    with self.subTest(text):
                        self.archive_refused(ps, text)
        for item in ("sc Environment::Path ';%s'" % self.scratch, "ac Environment::Path ';%s'" % self.scratch,
                     'New-Item -Path Environment:: -Name PATH -Value %s' % self.scratch,
                     'Set-Content -Path Environment::Path -Value %s' % self.scratch,
                     "Add-Content Microsoft.PowerShell.Core\\Environment::Path ';%s'" % self.scratch):
            text = '%s; %s' % (item, archive)
            with self.subTest(text):
                self.archive_refused(ps, text)
        # refused already, with the provider cmdlet named as the writer
        self.refused(ps, 'Set-Content -Path Environment::Path -Value "%s;$env:Path"; %s' % (self.scratch, archive))
        # clearing or removing PATH, or setting a property on it, is not read (R-2271)
        for item in ('Clear-Content Environment::Path', 'Clear-Content -Path:env:\\PATH', 'clc Environment::Path',
                     'Clear-Item Environment::Path', 'cli env:Path', 'Remove-Item Environment::Path',
                     'ri Microsoft.PowerShell.Core\\Environment::Path', 'del -Path Environment::Path',
                     'Set-ItemProperty -Path Environment::Path -Name x -Value y', 'sp env:Path x y',
                     'Clear-Item Environment::PAT*', 'Remove-Item Environment::\\ -Include PATH', 'rm env:Path'):
            text = '%s; %s' % (item, archive)
            with self.subTest(text):
                self.kept_a_read(ps, text)

    def test_powershell_content_on_the_environment_moves_the_user_config(self):
        # HOMEDRIVE is left out: changing it is refused by itself, as a move of the drive a path names
        archive = 'git -C %s archive --format=x HEAD' % self.main
        for name in ('HOME', 'XDG_CONFIG_HOME', 'USERPROFILE', 'HOMEPATH'):
            for item in ('Set-Content Environment::%s %s' % (name, self.scratch),
                         'Add-Content -LiteralPath Microsoft.PowerShell.Core\\Environment::%s x' % name):
                text = '%s; %s' % (item, archive)
                with self.subTest(text):
                    self.archive_refused(ps, text)
            # clearing or removing one is not read (R-2271)
            for item in ('Clear-Content env:%s' % name, 'Clear-Item -Path Environment::%s' % name,
                         'Remove-Item env:%s' % name):
                text = '%s; %s' % (item, archive)
                with self.subTest(text):
                    self.kept_a_read(ps, text)

    def test_reading_path_and_a_plain_cmd_keep_it_a_read(self):
        archive = 'git -C %s archive HEAD' % self.main
        for text in ('echo "$PATH" && %s' % archive, "cmd //c 'git -C \"%s\" archive HEAD'" % self.main,
                     "cmd //c '%s'" % archive, "cmd //c 'path& %s'" % archive,
                     "cmd //c '@path>nul& %s'" % archive, "cmd //c 'pathping -n x& %s'" % archive):
            with self.subTest(text):
                self.assertIsNone(self.pm(bash(text, self.w.plain)))
        for text in ('Write-Output $env:Path; %s' % archive, "cmd /c 'path& %s'" % archive,
                     'Get-Content Environment::Path; %s' % archive, 'Get-Content env:HOME; %s' % archive):
            with self.subTest(text):
                self.assertIsNone(self.pm(ps(text, self.w.plain)))


class CmdWordPosition(unittest.TestCase):
    """rules.cmd_native_unsure reads a word's raw source span; a word with no valid span is unsure
    (the guard cannot see the quotes cmd removed)."""

    def test_a_word_with_no_source_position_is_unsure(self):
        _parsed, cmds = sh.parse('git archive HEAD', sh.CMD, 'C:/w')
        cmd = cmds[0]
        for start, end in ((None, None), (5, 999), (12, 3), (-1, 4)):
            with self.subTest(start=start, end=end):
                self.assertTrue(rules.cmd_native_unsure(cmd, sh.Word('HEAD', False, start, end)))
        self.assertFalse(rules.cmd_native_unsure(cmd, cmd.words[2]))


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

    def test_an_unplaced_archive_is_no_read(self):
        # --git-dir may name a repository whose own config sets a filter (ArchiveConfigFailsClosed)
        gd = '--git-dir="%s/.git"' % self.main
        for text in ('git %s archive HEAD' % gd, 'git %s archive -o %s/a.tar HEAD' % (gd, self.scratch),
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
