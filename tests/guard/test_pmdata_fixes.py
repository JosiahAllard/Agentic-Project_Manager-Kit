"""Review fixes to the PM data routes of tests/guard/test_pmdata_gaps.py (docs/guard.md "Rules",
git-push-pmdata and git-remote-rewrite; docs/pm-data-branch.md "Failure modes"). Each is denied
only where PM data is (a record, or a copy when the machine config names pm_data), never in the
owner's own session:

  - git config options in any abbreviation git accepts (--glo, --fil, --ed) and -f<path>, for
    git-remote-rewrite and git-hooks-path;
  - cmd's `set HOME=...` before a push;
  - a computed git config word (git config $K, PowerShell @a) counts as a write;
  - a machine record the guard cannot read names the owner's git re-add of the data worktree;
  - alias words split at CR (and VT, FF) too;
  - `& (<expr>)` makes no line unreadable;
  - an unknown directory is no ground to deny when the machine config names no pm_data.

The checks git aliases from config, a copy of the data without a record and HOME set by a
builtin (read, printf -v, for, ...) once had were cut: the guard catches honest mistakes, not
evasion (honest mistakes only). Their allowed forms stay pinned."""
import os
import unittest
from unittest import mock

from kitlib.config import pointer as kpointer
from kitlib.guard import rules

from . import world as W
from .test_pmdata_gaps import COORD, PLAIN, PM, PUBLIC, Base
from .world import bash, ps


class AliasConfigCut(Base):
    """git aliases from config are no longer read (honest mistakes only): a name that is not a git builtin, on
    a line that moves the user config or not, is left to git."""

    def test_builtins_and_unknown_names_pass(self):
        self.assertIsNone(self.decide('HOME=C:/tmp/h git status', environ=PM))
        self.assertIsNone(self.decide('git -c color.ui=never log -1', environ=PM))
        for text in ('git pu origin pmdata', 'HOME=C:/tmp/h git pu origin pmdata',
                     'git -c include.path=C:/tmp/x.cfg pu origin pmdata'):
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
    """commands.line_env_names counts HOME only when the line assigns it literally (HOME=,
    export, $env:); a bash builtin that sets a variable from its arguments is not read (honest mistakes only)."""

    def test_other_names_pass(self):
        for text in ('read X; git push private pmdata', 'printf -v Y %s z; git push private pmdata',
                     'for i in 1 2; do git push private pmdata; done', 'getopts ab opt; git push private pmdata',
                     'for ((i=$n; i<2; i++)); do git push private pmdata; done',
                     'printf %s HOME; git push private pmdata'):
            with self.subTest(text):
                self.assertIsNone(self.decide(text, environ=PM))


class HomeByCmdSet(Base):
    """cmd's `set HOME=...` before a push, pinned."""

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


class MachineRecordUnreadable(Base):
    """Finding: Ctx.pm_root fails closed when pm_data.path cannot be placed (the data worktree
    folder deleted), so every push from a repository without a record is denied; the refusal
    names the owner's git re-add of the data worktree, since pm_data.py init refuses while the
    PM-repo pointer is set (docs/pm-data-branch.md "Failure modes")."""

    def test_a_push_while_the_data_worktree_is_gone_names_the_git_re_add(self):
        os.remove(self.record)
        gone = os.path.join(self.w.base, 'gone')
        self.w.user_config({'pm_data': {'path': gone}})
        d = self.decide('git push origin kit/wt', environ=PM)
        self.assert_denied(d, 'git-push-pmdata')
        self.assertNotIn('init --adopt', d.reason)
        g = '"%s"' % W.fwd(gone)
        self.assertIn('worktree unlock %s, then worktree remove %s, worktree add %s <data branch>, '
                      'worktree lock --reason "PM data" %s' % (g, g, g, g), d.instead)
        self.assertIsNone(self.decide('git push origin kit/wt'))

    def test_a_pointer_naming_a_deleted_data_worktree_names_its_folder(self):
        os.remove(self.record)
        gone = os.path.join(self.w.base, 'gone')
        # the real locate, under the test fence's pointer: one naming no folder is BAD, so the
        # machine config cannot be read and pm_data names no path; the refusal takes the pointer's
        with mock.patch.dict(os.environ, {kpointer.FENCE_ENV: gone}):
            d = self.decide('git push origin kit/wt', environ=PM)
            self.assert_denied(d, 'git-push-pmdata')
            self.assertIn('no folder at', d.reason)
            self.assertIn('worktree unlock "%s"' % W.fwd(gone), d.instead)
            self.assertIsNone(self.decide('git push origin kit/wt'))


class AliasSplit(unittest.TestCase):
    """Finding: alias words were split at space, tab and LF only; git splits at CR too (the
    split an alias the line defines is expanded by, rules.inline_alias)."""

    def test_split_alias(self):
        self.assertEqual(rules.split_alias('push\r--no-verify'), ['push', '--no-verify'])
        self.assertEqual(rules.split_alias('push\v--no-verify\fx'), ['push', '--no-verify', 'x'])
        self.assertEqual(rules.split_alias('"a\rb" c'), ['a\rb', 'c'])


class CallSubexpression(Base):
    """Regression: `& (<expr>) ...` made every such line unclean, so it read as unreadable in a
    main checkout for every session. The PM data hidden-program check judges it, and an agent
    session is denied it anywhere (tests/guard/test_pmdata_review.py)."""

    TEXT = '& (Join-Path $r x.exe) a'

    def test_the_owners_line_in_a_main_checkout_is_not_refused(self):
        self.assertIsNone(self.decide(self.TEXT, cwd=self.w.main, shell=ps))
        self.assertIsNone(self.decide('& (Get-Command git) status', cwd=self.w.main, shell=ps))

    def test_an_ordinary_line_without_pm_data_is_not_unreadable(self):
        # a session the write jail does not hold; an agent session is denied (CallSubexpressionAgents)
        os.remove(self.record)
        self.assertIsNone(self.decide(self.TEXT, environ=PLAIN, shell=ps))
        self.assertIsNone(self.decide(self.TEXT, environ=PLAIN, cwd=self.w.main, shell=ps))

    def test_in_a_pm_data_repository_it_is_denied(self):
        for label, sub, env in self.sessions():
            with self.subTest(label):
                self.assert_denied(self.decide(self.TEXT, sub=sub, environ=env, shell=ps), 'git-push-pmdata')
        # an unclean line too (the unclosed quote): the fallback judges it the same way
        self.assert_denied(self.decide(self.TEXT + "; echo 'x", environ=PM, shell=ps), 'git-push-pmdata')


class UnknownDirectory(Base):
    """Finding: a non-builtin subcommand in a directory the text cannot tell was denied in every
    repository, on machines whose config names no pm_data too. Since aliases from config are
    no longer read (honest mistakes only), it passes with a record or machine pm_data too."""

    TEXT = 'git -C $D lfs pull'

    def test_no_machine_pm_data_and_no_record_passes(self):
        os.remove(self.record)
        # a session the write jail does not hold: for a jailed one write-jail denies git -C $D first
        self.assertIsNone(self.decide(self.TEXT, environ=PLAIN, cwd=self.w.plain))

    def test_a_record_where_it_runs_or_machine_pm_data_passes(self):
        self.assertIsNone(self.decide(self.TEXT, environ=PLAIN))
        os.remove(self.record)
        self.machine_pm_data()
        self.assertIsNone(self.decide(self.TEXT, environ=PLAIN, cwd=self.w.plain))


if __name__ == '__main__':
    unittest.main()
