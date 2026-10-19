"""The routes past the pre-push leak stage that the remote-rewrite and pm-data-bypass guards
name (docs/guard.md "Rules", docs/pm-data-branch.md "Pre-push guard"). Each is denied only when the
repository has a PM data record, and never in the owner's
own session:

  - git-remote-rewrite: git remote set-url/add/rename/remove and git config writes to remote.*,
    url.*, include.* and includeIf.*, and git config --edit;
  - GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM / GIT_CONFIG before a push: denied today (the place is
    blind), pinned here;
  - a computed program name on an unreadable line, and `& (Get-Command git)`;
  - hand-driven remote helpers (git remote-https, git http-push, git-remote-*).

git aliases from config and a push from a copy of the data without a record (its root commit, a
shallow clone) are no longer judged: the guard catches honest mistakes, not evasion (R-2271).
Their allowed forms stay pinned."""
import os
import unittest

from kitlib import pmdata
from kitlib.git import repo
from kitlib.guard import context

from .claudehost import decide

from . import world as W
from .world import bash, ps

PM = {context.ROLE_ENV: 'pm'}
COORD = {context.ROLE_ENV: 'coordinator'}
PLAIN = {context.ROLE_ENV: 'plain'}             # a marked session the write jail does not hold
ROOT = 'a' * 40
PUBLIC = 'https://github.com/o/kit'


class Base(unittest.TestCase):
    def setUp(self):
        self.w = W.World(kit={'repo': {'name': 'demo'}})
        self.addCleanup(self.w.cleanup)
        common = os.path.join(self.w.main, '.git')
        os.makedirs(common, exist_ok=True)
        pmdata.write(common, 'pmdata', 'private', ROOT)
        self.record = pmdata.record_path(common)
        wt_git = os.path.join(common, 'worktrees', 'wt')
        os.makedirs(wt_git, exist_ok=True)
        with open(os.path.join(wt_git, 'HEAD'), 'w', encoding='utf-8') as f:
            f.write('ref: refs/heads/kit/wt\n')
        # a repository of its own under the temp folder (inside every session's write jail)
        self.clone = os.path.join(self.w.temp, 'scratch')
        os.makedirs(os.path.join(self.clone, '.git'))

    def classify(self, path):
        p = os.path.normcase(os.path.abspath(path))
        if p.startswith(os.path.normcase(self.clone)):
            git = os.path.join(self.clone, '.git')
            return repo.Place(repo.MAIN, self.clone, '', git, git)
        return self.w.classify(path)

    def ctx(self, environ=None):
        return self.w.ctx(environ=environ or {}, classify=self.classify)

    def decide(self, text, sub=False, environ=None, cwd=None, shell=bash):
        return decide(shell(text, cwd or self.w.wt, sub=sub), self.ctx(environ))

    def sessions(self):
        """(label, sub, environ) of every session but the owner's own."""
        return (('subagent', True, None), ('pm', False, PM), ('coordinator', False, COORD))

    def assert_denied(self, d, rule=None):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual(d.verdict, 'deny', d.reason)
        if rule:
            self.assertEqual(d.rule, rule, d.reason)

    def machine_pm_data(self):
        self.w.user_config({'pm_data': {'path': self.w.data}})
        os.makedirs(self.w.data_git_dir, exist_ok=True)
        with open(os.path.join(self.w.data_git_dir, 'HEAD'), 'w', encoding='utf-8') as f:
            f.write('ref: refs/heads/pmdata\n')


class RemoteRewrite(Base):
    """git-remote-rewrite: repointing a remote would make the public remote look private to the
    pre-push leak stage, which judges the private remote by its configured URLs."""

    DENIED = (
        'git remote set-url private %s' % PUBLIC,
        'git remote add evil %s' % PUBLIC,
        'git remote rename origin private2',
        'git remote remove origin',
        'git remote rm origin',
        'git remote -v set-url --push private %s' % PUBLIC,
        'git config remote.private.url %s' % PUBLIC,
        'git config remote.private.pushurl %s' % PUBLIC,
        'git config --add remote.private.pushurl %s' % PUBLIC,
        'git config --replace-all remote.private.url %s' % PUBLIC,
        'git config --unset remote.origin.url',
        'git config --unset-all remote.private.pushurl',
        'git config --remove-section remote.origin',
        'git config --rename-section remote.origin remote.x',
        'git config --rename-section old remote.private',
        'git config --remove-section url.%s' % PUBLIC,
        'git config url.%s.pushInsteadOf https://p/x' % PUBLIC,
        'git config --global url.%s.insteadOf https://p/x' % PUBLIC,
        'git config URL.%s.InsteadOf https://p/x' % PUBLIC,
        'git config set remote.private.url %s' % PUBLIC,
        'git config unset remote.private.url',
        'git config rename-section remote.a remote.b',
        'git config remove-section remote.a',
        'git config include.path /tmp/x.cfg',
        'git config includeIf.gitdir:~/.path /tmp/x.cfg',
        'git config --edit',
        'git config -e',
        'git config --global --edit',
        'git config edit',
        'git config $K %s' % PUBLIC,
        'git remote $V private %s' % PUBLIC,
    )

    def test_each_rewrite_is_denied_in_every_session_but_the_owners(self):
        for text in self.DENIED:
            for label, sub, env in self.sessions():
                with self.subTest(text=text, session=label):
                    d = self.decide(text, sub=sub, environ=env)
                    self.assert_denied(d, None if label == 'subagent' and '$' in text else 'git-remote-rewrite')
            with self.subTest(text=text, session='owner'):
                self.assertIsNone(self.decide(text))

    def test_powershell_and_a_named_checkout_too(self):
        d = self.decide('git -C "%s" remote set-url private %s' % (W.fwd(self.w.main), PUBLIC),
                        environ=PM, shell=ps)
        self.assert_denied(d, 'git-remote-rewrite')
        self.assertIn('git -C "%s" remote -v' % W.fwd(self.w.main), d.reason)

    def test_a_file_git_reads_counts_and_one_it_does_not_is_left_alone(self):
        cfg = W.fwd(os.path.join(self.w.main, '.git', 'config'))
        self.assert_denied(self.decide('git config --file %s remote.private.url %s' % (cfg, PUBLIC), environ=PM),
                           'git-remote-rewrite')
        self.assert_denied(self.decide('git config -f %s remote.private.url %s' % (cfg, PUBLIC), environ=PM,
                                       cwd=self.w.plain), 'git-remote-rewrite')
        other = W.fwd(os.path.join(self.w.temp, 'other.cfg'))
        self.assertIsNone(self.decide('git config --file %s remote.private.url %s' % (other, PUBLIC), environ=PM))

    READS = (
        'git remote',
        'git remote -v',
        'git remote get-url origin',
        'git remote show origin',
        'git config --get remote.origin.url',
        'git config remote.origin.url',
        'git config --get-regexp ^remote\\.',
        'git config --list',
        'git config get remote.origin.url',
        'git config user.name x',
        'git config --unset user.name',
        'git config --remove-section alias',
    )

    def test_reads_and_other_keys_pass(self):
        for text in self.READS:
            with self.subTest(text):
                self.assertIsNone(self.decide(text, environ=PM))

    def test_without_a_record_it_passes(self):
        os.remove(self.record)
        for text in ('git remote set-url private %s' % PUBLIC, 'git config remote.x.url y', 'git config --edit',
                     'git config --global url.a.insteadOf b'):
            with self.subTest(text):
                self.assertIsNone(self.decide(text, environ=PM))

    def test_global_scope_without_a_record_is_judged_by_the_machine_config(self):
        os.remove(self.record)
        self.machine_pm_data()
        for text in ('git config --global url.%s.pushInsteadOf https://p/x' % PUBLIC,
                     'git config --system remote.private.url x', 'git config --global --edit',
                     'git config --global include.path /tmp/x.cfg'):
            with self.subTest(text):
                self.assert_denied(self.decide(text, environ=PM, cwd=self.w.plain), 'git-remote-rewrite')
        self.assertIsNone(self.decide('git config remote.x.url y', environ=PM, cwd=self.w.plain))
        self.assertIsNone(self.decide('git config --global user.name x', environ=PM, cwd=self.w.plain))


class ConfigFileEnv(Base):
    """GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM and GIT_CONFIG before a push or send-pack name a config
    file on purpose: the guard catches honest mistakes, not evasion (R-2271), so the push is judged
    as the same push with no variable."""

    def same(self, a, b):
        if b is None:
            self.assertIsNone(a)
        else:
            self.assertIsNotNone(a)
            self.assertEqual((b.verdict, b.rule), (a.verdict, a.rule), a.reason)

    def test_a_config_file_variable_before_a_push_changes_nothing(self):
        for name in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG'):
            for label, sub, env in self.sessions():
                with self.subTest(name=name, session=label):
                    for push in ('git push private pmdata', 'git send-pack %s pmdata' % PUBLIC):
                        self.same(self.decide('%s=/tmp/x.cfg %s' % (name, push), sub=sub, environ=env),
                                  self.decide(push, sub=sub, environ=env))

    def test_powershell_too(self):
        self.same(self.decide('$env:GIT_CONFIG_GLOBAL = "x"; git push private pmdata', environ=PM, shell=ps),
                  self.decide('git push private pmdata', environ=PM, shell=ps))


class HomeConfig(Base):
    """A line that sets HOME, XDG_CONFIG_HOME, USERPROFILE, HOMEDRIVE or
    HOMEPATH (git for Windows finds the user config through each) with a git push makes git read
    another user config, whose url.*.pushInsteadOf the pre-push hook would read too."""

    BASH = ('HOME=/tmp/h git push private pmdata',
            'XDG_CONFIG_HOME=/tmp/x git push private pmdata',
            'USERPROFILE=C:/x git push private pmdata',
            'HOMEDRIVE=C: HOMEPATH=/x git push private pmdata',
            'env HOME=/tmp/h git push private pmdata',
            'export HOME=/tmp/h; git push private pmdata',
            'HOME=/tmp/h git -C "{main}" push private pmdata')
    PS = ('$env:HOME = "C:/h"; git push private pmdata',
          '$env:USERPROFILE = "C:/h"; git push private pmdata',
          'Set-Item env:XDG_CONFIG_HOME C:/x; git push private pmdata')

    def texts(self):
        main = W.fwd(self.w.main)
        return [(t.replace('{main}', main), bash) for t in self.BASH] + [(t, ps) for t in self.PS]

    def test_a_push_with_a_home_variable_is_denied(self):
        for text, shell in self.texts():
            for label, sub, env in self.sessions():
                with self.subTest(text=text, session=label):
                    self.assert_denied(self.decide(text, sub=sub, environ=env, shell=shell), 'git-push-pmdata')

    def test_owner_other_commands_and_no_record_pass(self):
        for text, shell in self.texts():
            with self.subTest(text=text):
                self.assertIsNone(self.decide(text, shell=shell))
        self.assertIsNone(self.decide('HOME=/tmp/h git status', environ=PM))
        self.assertIsNone(self.decide('git push private pmdata', environ=PM))
        os.remove(self.record)
        for text, shell in self.texts():
            with self.subTest(text=text, record=False):
                # the jailed pm session may not run git in a main checkout (write-jail): an unjailed mark
                self.assertIsNone(self.decide(text, environ=PLAIN, shell=shell))


class ComputedProgram(Base):
    """A program the text does not name, on a line in a repository with a PM data record: it may
    be git push --no-verify. Denied for sessions the write jail does not hold, from a linked
    worktree too (before, only a main checkout or an unplaceable folder)."""

    BASH = ('${G}t push --no-verify origin pmdata', 'G=git; $G push --no-verify origin pmdata')
    PS = ('& $env:G push --no-verify origin pmdata',)

    def test_pm_session_from_a_worktree_is_denied(self):
        # a line naming git may meet the write jail first, where the session's mark is jailed
        for text in self.BASH:
            with self.subTest(text):
                self.assert_denied(self.decide(text, environ=PM), None if 'git' in text else 'git-push-pmdata')
        for text in self.PS:
            with self.subTest(text):
                self.assert_denied(self.decide(text, environ=PM, shell=ps), 'git-push-pmdata')
        self.assert_denied(self.decide(self.BASH[0], environ={context.ROLE_ENV: 'deep-worker'}), 'git-push-pmdata')

    def test_coordinator_too(self):
        self.assert_denied(self.decide(self.BASH[0], environ=COORD))
        self.assert_denied(self.decide(self.PS[0], environ=COORD, shell=ps))

    def test_owner_and_no_record_pass(self):
        self.assertIsNone(self.decide(self.BASH[0]))
        self.assertIsNone(self.decide(self.PS[0], shell=ps))
        os.remove(self.record)
        self.assertIsNone(self.decide(self.BASH[0], environ=PM))

    def test_call_operator_on_a_subexpression_is_a_computed_program(self):
        text = '& (Get-Command git) push --no-verify origin pmdata'
        self.assert_denied(self.decide(text, sub=True, shell=ps), 'git-push-pmdata')
        self.assert_denied(self.decide(text, environ=PM, shell=ps), 'git-push-pmdata')
        self.assert_denied(self.decide('& (gcm git) push --no-verify origin pmdata', environ=PM, shell=ps),
                           'git-push-pmdata')
        self.assertIsNone(self.decide(text, shell=ps))


class AliasAndCopyCut(Base):
    """A git subcommand that is not a builtin is left to git, and a push from a repository
    without a record of its own is not judged as a copy of the data (R-2271)."""

    def test_builtins_and_unknown_names_pass(self):
        self.assertIsNone(self.decide('git status', environ=PM))
        self.assertIsNone(self.decide('git lfs ls-files', environ=PM))   # git's own error or git-lfs
        self.assertIsNone(self.decide('git pu origin pmdata', environ=PM))

    def test_a_push_from_a_repository_without_a_record_passes(self):
        self.machine_pm_data()
        text = 'git -C "%s" push %s origin/pmdata:refs/heads/x' % (W.fwd(self.clone), PUBLIC)
        for label, sub, env in self.sessions():
            with self.subTest(session=label):
                self.assertIsNone(self.decide(text, sub=sub, environ=env))


class RemoteHelper(Base):
    """git remote-<helper>, git http-push and their dashed programs talk to a remote without the
    pre-push hook, as git send-pack does."""

    DENIED = ('git remote-https origin %s' % PUBLIC, 'git http-push %s pmdata' % PUBLIC,
              'git remote-ext origin "ext::x"', 'git remote-fd 3', 'git-remote-https origin %s' % PUBLIC,
              'git-http-push %s pmdata' % PUBLIC, 'git-remote-http.exe origin %s' % PUBLIC)

    def test_each_is_denied_in_every_session_but_the_owners(self):
        for text in self.DENIED:
            for label, sub, env in self.sessions():
                with self.subTest(text=text, session=label):
                    self.assert_denied(self.decide(text, sub=sub, environ=env), 'git-push-pmdata')
            with self.subTest(text=text, session='owner'):
                self.assertIsNone(self.decide(text))

    def test_without_a_record_they_pass(self):
        os.remove(self.record)
        for text in self.DENIED:
            with self.subTest(text):
                self.assertIsNone(self.decide(text, environ=PM))

    def test_on_a_line_the_parser_cannot_read(self):
        # the unclosed quote hides the helper from the parser: only fallback's patterns see it
        for text in ("echo 'x ; git-remote-https origin %s" % PUBLIC, "echo 'x ; git http-push %s pmdata" % PUBLIC,
                     "echo 'x ; git remote-ext origin x"):
            with self.subTest(text):
                self.assert_denied(self.decide(text, environ=PM), 'git-push-pmdata')


if __name__ == '__main__':
    unittest.main()
