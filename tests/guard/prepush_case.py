"""The shared base of the pre-push tests (test_prepush*.py): the scratch repository with a bare
remote, its gate-OK record and push helpers, and the PM data branch fixture."""
import json
import os
import subprocess
import unittest

from kitlib import pmdata
from kitlib.gate import runner
from kitlib.records import gate_ok
from tests import gitfixture
from tests.home import child_home, result_drive

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOKS = os.path.join(KIT_ROOT, 'githooks')
GATE = {'steps': [{'name': 'unit', 'run': ['python', '-m', 'unittest']}]}
KIT = {'version': '0.1', 'commit': 'a' * 40}
AGENT = 'agent@example.invalid'


def _with_kit(repo):
    """The committed .kit.json, built once per process (each test's repository is a copy): the
    commit holding it."""
    # The fixture identity is the owner, so only OwnerTest's agent commits meet the owner check.
    repo.write('.kit.json', json.dumps({'repo': {'name': 'kit-test', 'push_block': True},
                                        'owner': {'email': gitfixture.EMAIL}}))
    return repo.commit('kit config', '.kit.json')


def _with_kit_pushed(repo):
    """_with_kit, its commit pushed to the remote's main (no hook: a template's own git)."""
    with_kit = _with_kit(repo)
    repo.git('push', '-q', 'origin', 'main')
    return with_kit


class PrePushCase(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo(bare_remote=True, prepare=_with_kit,
                                        prepare_key='test_prepush.PrePushCase')
        self.addCleanup(self.repo.cleanup)
        self.with_kit = self.repo.prepared
        result_drive(self)          # the gate-OK record folder; the hook's child reads it too

    def passed(self, rev, gate_config=None, config_rev='same', git_copy_only=False):
        """A gate-OK record for `rev` as gate.py writes it: the gate config `rev` holds and
        config_rev `rev`. `gate_config` / `config_rev` given: a record made otherwise.
        `git_copy_only`: only its .git copy (gate_ok.legacy_path), as a gate with no dev_drive
        writes it."""
        if gate_config is None:
            gate_config = runner.gate_config_at(self.repo.root, rev)
        rec = gate_ok.build(self.repo.root, rev, gate_config, KIT, 'kit-test',
                            '2026-10-06T10:00:00Z', [{'name': 'unit', 'result': 'pass', 'seconds': 1.0}],
                            config_rev=rev if config_rev == 'same' else config_rev)
        if not git_copy_only:
            return gate_ok.write(self.repo.root, rec)
        path = gate_ok.legacy_path(self.repo.root, rec['commit'])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(rec, sort_keys=True, indent=1) + '\n')
        return path

    def push(self, *args):
        home = child_home()         # the hook is a kit process: it gets a temp home, never the real one
        r = subprocess.run(('git', '-c', 'core.hooksPath=%s' % HOOKS.replace('\\', '/'), 'push') + args,
                           cwd=self.repo.root, capture_output=True, text=True, encoding='utf-8',
                           errors='replace',
                           env=gitfixture.fixture_env({'USERPROFILE': home, 'HOME': home}))
        return r.returncode, r.stderr

    def remote_main(self):
        return self.repo.git('rev-parse', 'main', cwd=self.repo.remote)


EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
SECRET = 'a PM ruling that is never public\n'


def _with_pmdata(repo):
    """_with_kit, then the PM data branch pmdata: a root commit of the empty tree, and on it a
    commit of ledger.md and .gitkeep, made in a linked worktree that is removed again (a template
    keeps no absolute path). Built once per process: (with_kit, the data root, the data commit)."""
    with_kit = _with_kit(repo)
    root = repo.git('commit-tree', EMPTY_TREE, '-m', 'PM data root')
    repo.git('branch', 'pmdata', root)
    wt = os.path.join(repo.base, 'data')
    repo.git('worktree', 'add', '-q', wt, 'pmdata')
    repo.write('ledger.md', SECRET, root=wt)
    repo.write('.gitkeep', '', root=wt)
    data = repo.commit('a ruling', 'ledger.md', '.gitkeep', cwd=wt)
    repo.git('worktree', 'remove', wt)
    return with_kit, root, data


class PmDataCase(PrePushCase):
    """The leak stage (docs/guard.md "Pre-push", "PM data branch"): with kit-pm-data.json in the
    git common dir, the data branch, its history and its objects reach only the recorded private
    remote, and that remote gets nothing else. origin is the public remote."""

    def setUp(self):
        self.repo = gitfixture.TempRepo(bare_remote=True, prepare=_with_pmdata,
                                        prepare_key='test_prepush.PmDataTest')
        self.addCleanup(self.repo.cleanup)
        self.with_kit, self.root, self.data = self.repo.prepared
        result_drive(self)
        # The private remote and the data worktree are paths outside the copy: made per test.
        self.private = os.path.join(self.repo.base, 'private.git')
        gitfixture.built_copy(('test_prepush.PmDataTest.private', os.environ.get('KIT_FAKE_GIT')),
                              self.private, self.bare, repos=())
        self.repo.git('remote', 'add', 'private', self.private)
        self.data_wt = os.path.join(self.repo.base, 'data')
        self.repo.git('worktree', 'add', '-q', self.data_wt, 'pmdata')
        self.record()

    def bare(self, folder):
        """An empty bare repository in `folder`: the private remote, copied for each test."""
        self.repo.git('init', '-q', '--bare', folder, cwd=self.repo.base)

    def record(self, text=None, root=None):
        """kit-pm-data.json as pm_data.py init writes it, or `text` written as is."""
        if text is None:
            pmdata.write(self.repo.common_dir(), 'pmdata', 'private', root or self.root)
            return
        with open(pmdata.record_path(self.repo.common_dir()), 'w', encoding='utf-8') as f:
            f.write(text)

    def ls(self, remote, ref):
        out = self.repo.git('ls-remote', remote, ref)
        return out.split('\t')[0] if out else ''

    def refused(self, *args, why=None):
        code, err = self.push(*args)
        self.assertEqual(code, 1, (args, err))
        if why:
            self.assertIn(why, err, args)
        return err

    def new_branch(self, name):
        self.repo.git('checkout', '-q', '-b', name, 'main')
