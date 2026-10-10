"""Review fixes to the PM data routes of tests/guard/test_pmdata_gaps.py (docs/guard.md "Rules",
git-push-pmdata and git-remote-rewrite; docs/pm-data-branch.md "Failure modes"). Each is denied
only where PM data is (a record, or a copy when the machine config names pm_data), never in the
owner's own session:

  - a git alias in a copy of PM data with no record of its own is expanded too;
  - a git alias on a line that sets HOME or the like, or with -c/--config-env, is not judged by
    the guard's own read of the alias;
  - git config options in any abbreviation git accepts (--glo, --fil, --ed) and -f<path>, for
    git-remote-rewrite and git-hooks-path;
  - HOME set by read, printf -v, for, mapfile/readarray and getopts;
  - a computed git config word (git config $K, PowerShell @a) counts as a write;
  - an alias for `config core.hooksPath` is judged by git-hooks-path;
  - a machine record the guard cannot read names pm_data.py init --adopt;
  - gitread.has_commit fails closed on any answer but 0 or 1;
  - alias words split at CR (and VT, FF) too;
  - `& (<expr>)` makes no line unreadable;
  - an unknown directory is no ground to deny when the machine config names no pm_data."""
import os
import unittest
from unittest import mock

from kitlib.git import cmd as gitcmd
from kitlib.guard import gitread, rules

from . import world as W
from .test_pmdata_gaps import COORD, PM, PUBLIC, ROOT, Base
from .world import bash, ps


def nc(p):
    return os.path.normcase(os.path.abspath(p))


class AliasInCopy(Base):
    """Finding: git_alias_config skipped a repository without a record, so an alias push from a
    copy holding the root never reached data_copy."""

    def test_an_alias_push_from_a_copy_holding_the_root_is_denied(self):
        self.machine_pm_data()
        self.read.commits.add((nc(self.clone), ROOT))
        self.read.aliases = {'pu': 'push', 'sp': 'send-pack'}
        for label, env in (('pm', PM), ('coordinator', COORD)):
            with self.subTest(label):
                d = self.decide('git -C "%s" pu %s main' % (W.fwd(self.clone), PUBLIC), environ=env)
                self.assert_denied(d, 'git-push-pmdata')
                self.assertIn('alias.pu=push', d.reason)
        self.assert_denied(self.decide('git -C "%s" sp %s main' % (W.fwd(self.clone), PUBLIC), environ=PM),
                           'git-push-pmdata')
        self.assertIsNone(self.decide('git -C "%s" pu %s main' % (W.fwd(self.clone), PUBLIC)))

    def test_an_unjudgeable_alias_counts_only_in_a_copy(self):
        self.machine_pm_data()
        self.read.aliases = {'x': '!git push origin main', 'pu': 'push'}
        clone = W.fwd(self.clone)
        self.assertIsNone(self.decide('git -C "%s" x' % clone, environ=PM))       # an unrelated repository
        self.assertIsNone(self.decide('git -C "%s" pu origin main' % clone, environ=PM))
        self.read.commits.add((nc(self.clone), ROOT))
        self.assert_denied(self.decide('git -C "%s" x' % clone, environ=PM), 'git-push-pmdata')

    def test_no_machine_pm_data_leaves_a_repository_without_a_record_alone(self):
        self.read.aliases = {'pu': 'push'}
        self.read.commits.add((nc(self.clone), ROOT))
        self.assertIsNone(self.decide('git -C "%s" pu origin main' % W.fwd(self.clone), environ=PM))


class AliasConfigMoved(Base):
    """Finding: the guard read alias.<name> with its own HOME, not the line's, and without the
    call's -c settings, so an alias defined only there passed."""

    DENIED_BASH = ('HOME=C:/tmp/h git pu origin pmdata',
                   'XDG_CONFIG_HOME=C:/tmp/x git pu origin pmdata',
                   'USERPROFILE=C:/tmp/u git pu origin pmdata',
                   'git -c include.path=C:/tmp/x.cfg pu origin pmdata',
                   'git -c includeIf.onbranch:main.path=C:/tmp/x.cfg pu origin pmdata')

    def test_an_alias_the_guard_cannot_read_from_the_lines_config_is_denied(self):
        # no alias in the guard's own view: only the line's HOME or -c holds it
        for text in self.DENIED_BASH:
            for label, env in (('pm', PM), ('coordinator', COORD)):
                with self.subTest(text=text, session=label):
                    self.assert_denied(self.decide(text, environ=env), 'git-push-pmdata')
            with self.subTest(text=text, session='owner'):
                self.assertIsNone(self.decide(text))

    def test_powershell_home_too(self):
        self.assert_denied(self.decide('$env:HOME = "C:/h"; git pu origin pmdata', environ=PM, shell=ps),
                           'git-push-pmdata')

    def test_builtins_and_no_record_pass(self):
        self.assertIsNone(self.decide('HOME=C:/tmp/h git status', environ=PM))
        self.assertIsNone(self.decide('git -c color.ui=never log -1', environ=PM))
        os.remove(self.record)
        for text in self.DENIED_BASH:
            with self.subTest(text):
                self.assertIsNone(self.decide(text, environ=PM))


class ConfigPrefixes(Base):
    """Finding: git config options were matched exactly, so an abbreviation git accepts (--glo,
    --sys, --fil <path>, --ed) and -f<path> passed git-remote-rewrite and git-hooks-path."""

    def cfg(self):
        return W.fwd(os.path.join(self.w.main, '.git', 'config'))

    def test_global_and_system_abbreviated_are_judged_by_the_machine_config(self):
        os.remove(self.record)
        self.machine_pm_data()
        for text in ('git config --glo url.%s.pushInsteadOf https://p/x' % PUBLIC,
                     'git config --gl url.%s.insteadOf https://p/x' % PUBLIC,
                     'git config --sys remote.private.url %s' % PUBLIC,
                     'git config --glob --ed'):
            with self.subTest(text):
                self.assert_denied(self.decide(text, environ=PM, cwd=self.w.plain), 'git-remote-rewrite')

    def test_a_file_named_by_an_abbreviation_or_attached_is_judged(self):
        for text in ('git config --fil %s remote.private.url %s' % (self.cfg(), PUBLIC),
                     'git config --fil=%s remote.private.url %s' % (self.cfg(), PUBLIC),
                     'git config -f%s remote.private.url %s' % (self.cfg(), PUBLIC)):
            with self.subTest(text):
                self.assert_denied(self.decide(text, environ=PM, cwd=self.w.plain), 'git-remote-rewrite')

    def test_edit_abbreviated(self):
        self.assert_denied(self.decide('git config --ed', environ=PM), 'git-remote-rewrite')
        self.assert_denied(self.decide('git config --edi', environ=COORD), 'git-remote-rewrite')

    def test_hooks_path_with_an_abbreviated_option(self):
        temp_repo = W.fwd(self.clone)            # a repository under the temp folder
        for text, cwd in (('git config --fil %s core.hooksPath C:/x' % self.cfg(), self.w.plain),
                          ('git config --glo core.hooksPath C:/x', temp_repo),
                          ('git config --sy core.hooksPath C:/x', temp_repo)):
            with self.subTest(text):
                self.assert_denied(self.decide(text, environ=PM, cwd=cwd), 'git-hooks-path')

    def test_reads_and_other_files_pass(self):
        other = W.fwd(os.path.join(self.w.temp, 'other.cfg'))
        for text in ('git config --get-a remote.origin.url', 'git config --li', 'git config -l',
                     'git config --fil %s remote.private.url %s' % (other, PUBLIC),
                     'git config -f%s remote.private.url %s' % (other, PUBLIC),
                     'git config --glo user.name x'):
            with self.subTest(text):
                self.assertIsNone(self.decide(text, environ=PM))
        self.assertIsNone(self.decide('git config -f%s core.hooksPath C:/x' % other, environ=PM))


class HomeByBuiltin(Base):
    """Finding: commands.line_env_names counted HOME only when assigned (HOME=, export, $env:),
    not when a bash builtin sets it from its arguments."""

    DENIED = ('read HOME; git push private pmdata',
              'read -r -p "where? " HOME; git push private pmdata',
              'read -a XDG_CONFIG_HOME; git push private pmdata',
              'printf -v HOME %s C:/h; git push private pmdata',
              'printf -vUSERPROFILE %s C:/h; git push private pmdata',
              'for HOME in C:/h; do git push private pmdata; done',
              'mapfile -t HOME < f; git push private pmdata',
              'readarray -n 1 HOME < f; git push private pmdata',
              'getopts ab HOME; git push private pmdata',
              'read $n; git push private pmdata')

    def test_a_push_on_a_line_whose_builtin_sets_home_is_denied(self):
        for text in self.DENIED:
            for label, env in (('pm', PM), ('coordinator', COORD)):
                with self.subTest(text=text, session=label):
                    self.assert_denied(self.decide(text, environ=env), 'git-push-pmdata')
            with self.subTest(text=text, session='owner'):
                self.assertIsNone(self.decide(text))

    def test_other_names_pass(self):
        for text in ('read X; git push private pmdata', 'printf -v Y %s z; git push private pmdata',
                     'for i in 1 2; do git push private pmdata; done', 'getopts ab opt; git push private pmdata',
                     'printf %s HOME; git push private pmdata'):
            with self.subTest(text):
                self.assertIsNone(self.decide(text, environ=PM))


class HomeByCmdSet(Base):
    """cmd's `set HOME=...` before a push (KIT-GUARD-HOME-GITCONFIG), pinned."""

    def test_cmd_set_home_is_denied(self):
        for text in ('cmd /c "set HOME=C:/h&& git push private pmdata"',
                     'cmd.exe /c "set USERPROFILE=C:\\h& git push private pmdata"'):
            with self.subTest(text):
                self.assert_denied(self.decide(text, environ=PM), 'git-push-pmdata')
                self.assertIsNone(self.decide(text))


class ConfigComputed(Base):
    """Finding: a lone computed git config word (git config $K, PowerShell git config @a) was read
    as a read; it may be any write."""

    def test_a_computed_config_word_counts_as_a_write(self):
        for text, shell in (('git config $K', bash), ('git config "$K"', bash), ('git config --add $K', bash),
                            ('git config @a', ps), ('git config $k', ps), ('git remote @a', ps)):
            for label, env in (('pm', PM), ('coordinator', COORD)):
                with self.subTest(text=text, session=label):
                    self.assert_denied(self.decide(text, environ=env, shell=shell), 'git-remote-rewrite')
            with self.subTest(text=text, session='owner'):
                self.assertIsNone(self.decide(text, shell=shell))

    def test_a_quoted_at_sign_and_no_record_pass(self):
        self.assertIsNone(self.decide("git config '@a'", environ=PM, shell=ps))
        os.remove(self.record)
        self.assertIsNone(self.decide('git config $K', environ=PM))


class AliasHooksPath(Base):
    """Finding: an alias for `config core.hooksPath X` passed, as git-hooks-path judged the alias
    name as the subcommand."""

    def test_an_alias_that_sets_hooks_path_is_denied(self):
        self.read.aliases = {'hp': 'config core.hooksPath C:/x', 'hp2': 'hp', 'hpu': 'config --unset core.hooksPath'}
        for text in ('git hp', 'git hp2', 'git hpu'):
            for label, env in (('pm', PM), ('coordinator', COORD)):
                with self.subTest(text=text, session=label):
                    self.assert_denied(self.decide(text, environ=env), 'git-hooks-path')

    def test_an_alias_that_reads_it_passes(self):
        self.read.aliases = {'hr': 'config --get core.hooksPath'}
        self.assertIsNone(self.decide('git hr', environ=PM))


class MachineRecordUnreadable(Base):
    """Finding: Ctx.pm_root fails closed when pm_data.path cannot be placed (the data worktree
    folder deleted), so every push from a repository without a record is denied; the refusal
    names pm_data.py init --adopt (docs/pm-data-branch.md "Failure modes")."""

    def test_a_push_while_the_data_worktree_is_gone_names_adopt(self):
        os.remove(self.record)
        gone = os.path.join(self.w.base, 'gone')
        self.w.user_config({'pm_data': {'path': gone}})
        d = self.decide('git push origin kit/wt', environ=PM)
        self.assert_denied(d, 'git-push-pmdata')
        self.assertIn('pm_data.py init --adopt', d.reason)
        self.assertIn('--path "%s"' % W.fwd(gone), d.reason)
        self.assertIsNone(self.decide('git push origin kit/wt'))


class HasCommit(unittest.TestCase):
    """Finding: gitread.has_commit took any non-zero exit of cat-file as "not held"; a git that
    fails (128, a timeout) must be an error, and a partial clone must not fetch to answer."""

    def run_with(self, rc, err=''):
        calls = []

        def fake(args, cwd, stdin=None, timeout=None, env=None):
            calls.append((list(args), dict(env or {})))
            return gitcmd.Result(rc, b'', err)
        with mock.patch.object(gitread.cmd, 'call', fake):
            out = gitread.has_commit('C:/r', ROOT)
        return out, calls

    def test_the_question_asked(self):
        _out, calls = self.run_with(0)
        args, env = calls[0]
        self.assertEqual(args, ['rev-parse', '--verify', '--quiet', '%s^{commit}' % ROOT])
        self.assertEqual(env.get('GIT_NO_LAZY_FETCH'), '1')

    def test_answers(self):
        self.assertEqual(self.run_with(0)[0], (True, None))
        self.assertEqual(self.run_with(1)[0], (False, None))
        for rc, err in ((128, 'fatal: not a git repository'), (128, ''), (None, 'git timed out'), (2, '')):
            with self.subTest(rc=rc):
                held, why = self.run_with(rc, err)[0]
                self.assertIsNone(held)
                self.assertTrue(why)


class AliasSplit(Base):
    """Finding: alias words were split at space, tab and LF only; git splits at CR too."""

    def test_split_alias(self):
        self.assertEqual(rules.split_alias('push\r--no-verify'), ['push', '--no-verify'])
        self.assertEqual(rules.split_alias('push\v--no-verify\fx'), ['push', '--no-verify', 'x'])
        self.assertEqual(rules.split_alias('"a\rb" c'), ['a\rb', 'c'])

    def test_an_alias_split_at_cr_is_judged(self):
        self.read.aliases = {'pu': 'push\r--no-verify'}
        self.assert_denied(self.decide('git pu origin pmdata', environ=PM), 'git-push-pmdata')


class CallSubexpression(Base):
    """Regression: `& (<expr>) ...` made every such line unclean, so it read as unreadable in a
    main checkout for every session. Only the PM data hidden-program check judges it."""

    TEXT = '& (Join-Path $r x.exe) a'

    def test_the_owners_line_in_a_main_checkout_is_not_refused(self):
        self.assertIsNone(self.decide(self.TEXT, cwd=self.w.main, shell=ps))
        self.assertIsNone(self.decide('& (Get-Command git) status', cwd=self.w.main, shell=ps))

    def test_an_ordinary_line_without_pm_data_is_not_unreadable(self):
        os.remove(self.record)
        self.assertIsNone(self.decide(self.TEXT, sub=True, shell=ps))
        self.assertIsNone(self.decide(self.TEXT, environ=PM, cwd=self.w.main, shell=ps))

    def test_in_a_pm_data_repository_it_is_denied(self):
        for label, sub, env in self.sessions():
            with self.subTest(label):
                self.assert_denied(self.decide(self.TEXT, sub=sub, environ=env, shell=ps), 'git-push-pmdata')
        # an unclean line too (the unclosed quote): the fallback judges it the same way
        self.assert_denied(self.decide(self.TEXT + "; echo 'x", environ=PM, shell=ps), 'git-push-pmdata')


class UnknownDirectory(Base):
    """Finding: a non-builtin subcommand in a directory the text cannot tell was denied in every
    repository, on machines whose config names no pm_data too."""

    TEXT = 'git -C $D lfs pull'

    def test_no_machine_pm_data_and_no_record_passes(self):
        os.remove(self.record)
        self.assertIsNone(self.decide(self.TEXT, environ=PM, cwd=self.w.plain))

    def test_a_record_where_it_runs_or_machine_pm_data_denies(self):
        self.assert_denied(self.decide(self.TEXT, environ=PM), 'git-push-pmdata')
        os.remove(self.record)
        self.machine_pm_data()
        self.assert_denied(self.decide(self.TEXT, environ=PM, cwd=self.w.plain), 'git-push-pmdata')


if __name__ == '__main__':
    unittest.main()
