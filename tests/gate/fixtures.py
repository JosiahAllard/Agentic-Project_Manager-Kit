"""Shared fixtures for the gate tests: a scratch git repository (tests.gitfixture, with a bare
remote in the same temp folder) holding a `.kit.json` gate, and the scratch machine-lock store of
tests/lock/helpers.StoreCase. Nothing here touches the machine's lock store, a real repository or
a real remote."""
import json
import os

from tests import gitfixture
from tests.home import child_home
from tests.lock.helpers import StoreCase

# A step that prints ok and exits 1 when the file RED is in the tree it runs in.
RED_AWARE = ['{python}', '-c',
             'import os, sys; print("ok"); sys.exit(1 if os.path.exists("RED") else 0)']
GATE = {'steps': [{'name': 'unit', 'always': True, 'cmd': RED_AWARE, 'pass': '^ok$'}]}


def test_env():
    """The fixture's git environment without the Claude Code and machine lock variables: what the
    gate's steps and git writes get in a test. KIT_PINNED is `live`: a tools/gate.py or land.py a
    test starts in the kit's main checkout runs this tree's code, never an export of pushed main
    under the real LOCALAPPDATA (kitlib.gate.pin). USERPROFILE and HOME are a temp folder
    (tests.home.child_home): a home lookup in such a process reads no real machine config, and the
    home fence has nothing to refuse there (kitlib.gate.runner._run_check reports any exception
    of a check as a failed check, a refusal included)."""
    home = child_home()
    env = {k: v for k, v in gitfixture.fixture_env({'USERPROFILE': home, 'HOME': home}).items()
           if k not in ('CLAUDE_CODE_SESSION_ID', 'CLAUDE_PID') and not k.startswith('MACHINE_LOCK_')}
    env['KIT_PINNED'] = 'live'
    return env


class GateRepoCase(StoreCase):
    """self.repo: main with `.kit.json` (self.GATE) committed and pushed to a bare remote;
    self.book_kw books one fake cpu slot on the scratch store."""
    GATE = GATE
    KIT = None

    def setUp(self):
        StoreCase.setUp(self)
        self.confine(1)
        text = json.dumps(dict(self.KIT or {}, gate=self.GATE), indent=1) + '\n'

        def prepare(repo):          # built once per process per .kit.json; each test gets a copy
            repo.write('.kit.json', text)
            repo.write('.gitignore', '.claude/\n')      # the linked worktrees live under it
            main = repo.commit('kit config', '.kit.json', '.gitignore')
            repo.git('push', '-q', 'origin', 'main')
            return main
        self.repo = gitfixture.TempRepo(bare_remote=True, prepare=prepare,
                                        prepare_key=('gate.fixtures.GateRepoCase', text))
        self.addCleanup(self.repo.cleanup)
        self.main = self.repo.prepared
        self.env = test_env()
        self.book_kw = {'cores': 1, 'store': self.store, 'env': self.env}
        self.logs = os.path.join(self.tmp, 'logs')

    def branch(self, name, files):
        """A worktree on branch kit/<name> with `files` ({path: text}) committed: (path, tip)."""
        wt = self.repo.worktree(name)
        for rel, text in files.items():
            self.repo.write(rel, text, root=wt)
        tip = self.repo.commit('work on %s' % name, *files, cwd=wt)
        return wt, tip

    def remote_main(self):
        return self.repo.git('--git-dir', self.repo.remote, 'rev-parse', 'main', cwd=self.repo.base)
