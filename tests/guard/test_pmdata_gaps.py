"""The routes past the pre-push leak stage that KIT-GUARD-REMOTE-REWRITE and KIT-GUARD-PMDATA-BYPASS
name (docs/guard.md "Rules", docs/pm-data-branch.md "Pre-push guard"). Each is denied only when the
repository has a PM data record (or, for a copy, holds the recorded root), and never in the owner's
own session:

  - git-remote-rewrite: git remote set-url/add/rename/remove and git config writes to remote.*,
    url.*, include.* and includeIf.*, and git config --edit;
  - GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM / GIT_CONFIG before a push: denied today (the place is
    blind), pinned here;
  - a computed program name on an unreadable line, and `& (Get-Command git)`;
  - a git alias, expanded from config and judged as what it runs;
  - a push from a repository without a record that holds the recorded root, or is shallow;
  - hand-driven remote helpers (git remote-https, git http-push, git-remote-*)."""
import os
import unittest

from kitlib import pmdata
from kitlib.git import repo
from kitlib.guard import context, decide

from . import world as W
from .world import bash, ps

PM = {context.ROLE_ENV: 'pm'}
COORD = {context.ROLE_ENV: 'coordinator'}
ROOT = 'a' * 40
PUBLIC = 'https://github.com/o/kit'


class FakeRead(object):
    """The guard's git config and object reads (kitlib.guard.gitread), answered from tables."""

    def __init__(self):
        self.aliases = {}                     # name -> value
        self.alias_error = None
        self.commits = set()                  # (normcase(dir), sha)
        self.shallow = set()                  # normcase(dir)
        self.error = None

    def alias(self, cwd, name):
        if self.alias_error:
            return None, self.alias_error
        return self.aliases.get(name.lower()), None

    def has_commit(self, cwd, sha):
        if self.error:
            return None, self.error
        return (os.path.normcase(os.path.abspath(cwd)), sha) in self.commits, None

    def is_shallow(self, cwd):
        if self.error:
            return None, self.error
        return os.path.normcase(os.path.abspath(cwd)) in self.shallow, None


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
        self.read = FakeRead()
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
        c = self.w.ctx(environ=environ or {}, classify=self.classify)
        c.gitread = self.read
        return c

    def decide(self, text, sub=False, environ=None, cwd=None, shell=bash):
        return decide(shell(text, cwd or self.w.wt, sub=sub), self.ctx(environ))

    def sessions(self):
        """(label, sub, environ) of every session but the owner's own."""
        return (('subagent', True, None), ('pm', False, PM), ('coordinator', False, COORD))

    def assert_denied(self, d, rule=None):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual(d.kind, 'deny', d.reason)
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
    """GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM and GIT_CONFIG before a push or send-pack: already
    denied (commands.git_env_unsafe blinds the place, and git_push refuses a place it cannot tell).
    Pinned so the record's ask stays met."""

    def test_a_config_file_variable_before_a_push_is_denied(self):
        for name in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG'):
            for label, sub, env in self.sessions():
                with self.subTest(name=name, session=label):
                    d = self.decide('%s=/tmp/x.cfg git push private pmdata' % name, sub=sub, environ=env)
                    self.assert_denied(d, 'git-push-main')
                    d = self.decide('%s=/tmp/x.cfg git send-pack %s pmdata' % (name, PUBLIC), sub=sub, environ=env)
                    self.assert_denied(d)

    def test_powershell_too(self):
        d = self.decide('$env:GIT_CONFIG_GLOBAL = "x"; git push private pmdata', environ=PM, shell=ps)
        self.assert_denied(d, 'git-push-main')


class ComputedProgram(Base):
    """A program the text does not name, on a line in a repository with a PM data record: it may
    be git push --no-verify. Denied for sessions the write jail does not hold, from a linked
    worktree too (before, only a main checkout or an unplaceable folder)."""

    BASH = ('${G}t push --no-verify origin pmdata', 'G=git; $G push --no-verify origin pmdata')
    PS = ('& $env:G push --no-verify origin pmdata',)

    def test_pm_session_from_a_worktree_is_denied(self):
        for text in self.BASH:
            with self.subTest(text):
                self.assert_denied(self.decide(text, environ=PM), 'git-push-pmdata')
        for text in self.PS:
            with self.subTest(text):
                self.assert_denied(self.decide(text, environ=PM, shell=ps), 'git-push-pmdata')

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
        self.assert_denied(self.decide(text, sub=True, shell=ps), 'unreadable')
        self.assert_denied(self.decide(text, environ=PM, shell=ps), 'git-push-pmdata')
        self.assert_denied(self.decide('& (gcm git) push --no-verify origin pmdata', environ=PM, shell=ps),
                           'git-push-pmdata')
        self.assertIsNone(self.decide(text, shell=ps))


class Alias(Base):
    """A git alias in config is expanded and judged as the command it runs."""

    def test_an_alias_for_push_no_verify_is_denied(self):
        self.read.aliases = {'pu': 'push'}
        for label, sub, env in self.sessions():
            with self.subTest(label):
                self.assert_denied(self.decide('git pu --no-verify origin pmdata', sub=sub, environ=env),
                                   'git-push-pmdata')
        self.assertIsNone(self.decide('git pu --no-verify origin pmdata'))
        self.assertIsNone(self.decide('git pu origin kit/wt', environ=PM))

    def test_an_alias_holding_the_flag_and_a_chain(self):
        self.read.aliases = {'pnv': 'push --no-verify', 'p2': 'pnv'}
        self.assert_denied(self.decide('git pnv origin pmdata', environ=PM), 'git-push-pmdata')
        self.assert_denied(self.decide('git p2 origin pmdata', environ=PM), 'git-push-pmdata')

    def test_aliases_for_other_hookless_or_rewriting_commands(self):
        self.read.aliases = {'sp': 'send-pack', 'rs': "remote set-url", 'rh': 'remote-https'}
        self.assert_denied(self.decide('git sp %s pmdata' % PUBLIC, environ=PM), 'git-push-pmdata')
        self.assert_denied(self.decide('git rs private %s' % PUBLIC, environ=PM), 'git-remote-rewrite')
        self.assert_denied(self.decide('git rh origin %s' % PUBLIC, environ=PM), 'git-push-pmdata')

    def test_a_shell_alias_or_one_that_cannot_be_read_is_denied(self):
        self.read.aliases = {'x': '!git push --no-verify origin pmdata', 'o': '-c a.b=c push', 'loop': 'loop'}
        self.assert_denied(self.decide('git x', environ=PM), 'git-push-pmdata')
        self.assert_denied(self.decide('git o origin pmdata', environ=PM), 'git-push-pmdata')
        self.assert_denied(self.decide('git loop', environ=PM), 'git-push-pmdata')
        self.read.alias_error = 'git config timed out'
        self.assert_denied(self.decide('git pu origin pmdata', environ=PM), 'git-push-pmdata')

    def test_builtins_unknown_names_and_no_record_pass(self):
        self.read.aliases = {'pu': 'push --no-verify', 'status': 'push --no-verify'}
        self.assertIsNone(self.decide('git status', environ=PM))         # git ignores an alias that hides a builtin
        self.assertIsNone(self.decide('git lfs ls-files', environ=PM))   # no alias: git's own error or git-lfs
        os.remove(self.record)
        self.assertIsNone(self.decide('git pu origin pmdata', environ=PM))


class DataCopy(Base):
    """A repository without a record of its own that holds the recorded root commit (a clone or a
    fetch of the data branch) or is shallow: it runs no kit pre-push leak stage, so every push and
    send-pack from it is denied when the machine config names pm_data."""

    def push(self, sub=False, environ=PM, text=None):
        return self.decide(text or 'git -C "%s" push %s origin/pmdata:refs/heads/x' % (W.fwd(self.clone), PUBLIC),
                           sub=sub, environ=environ)

    def test_a_clone_holding_the_root_is_denied(self):
        self.machine_pm_data()
        self.read.commits.add((os.path.normcase(self.clone), ROOT))
        for label, sub, env in self.sessions():
            with self.subTest(label):
                self.assert_denied(self.push(sub=sub, environ=env), 'git-push-pmdata')
        self.assert_denied(self.push(text='git -C "%s" push origin main' % W.fwd(self.clone)), 'git-push-pmdata')
        self.assert_denied(self.push(text='git -C "%s" send-pack %s pmdata' % (W.fwd(self.clone), PUBLIC)),
                           'git-push-pmdata')
        self.assertIsNone(self.push(environ={}))

    def test_a_shallow_repository_is_denied(self):
        self.machine_pm_data()
        self.read.shallow.add(os.path.normcase(self.clone))
        self.assert_denied(self.push(), 'git-push-pmdata')

    def test_a_check_that_fails_is_denied(self):
        self.machine_pm_data()
        self.read.error = 'git cat-file timed out'
        self.assert_denied(self.push(), 'git-push-pmdata')

    def test_an_unrelated_repository_and_no_machine_pm_data_pass(self):
        self.machine_pm_data()
        self.assertIsNone(self.push())
        self.w.user_config({})
        self.read.commits.add((os.path.normcase(self.clone), ROOT))
        self.assertIsNone(self.push())


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
        self.assert_denied(self.decide('git-remote-https origin %s; echo "unclosed' % PUBLIC, environ=PM),
                           'git-push-pmdata')
        self.assert_denied(self.decide('git http-push %s pmdata; echo "unclosed' % PUBLIC, environ=PM),
                           'git-push-pmdata')


if __name__ == '__main__':
    unittest.main()
