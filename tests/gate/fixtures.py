"""Shared fixtures for the gate tests: a scratch git repository (tests.gitfixture, with a bare
remote in the same temp folder) holding a `.kit.json` gate, and the scratch machine-lock store of
tests/lock/helpers.StoreCase. Nothing here touches the machine's lock store, a real repository or
a real remote."""
import inspect
import json
import os
from unittest import mock

from kitlib.lock import run as lockrun
from tests import gitfixture
from tests.home import child_home
from tests.lock.helpers import StoreCase

# A step that prints ok and exits 1 when the file RED is in the tree it runs in.
RED_AWARE = ['{python}', '-c',
             'import os, sys; print("ok"); sys.exit(1 if os.path.exists("RED") else 0)']
GATE = {'steps': [{'name': 'unit', 'always': True, 'cmd': RED_AWARE, 'pass': '^ok$'}]}


def gate_full(root, *tips):
    """Each of `tips` given the gate-OK record a passing `gate.py --full` writes (a record whose
    `full` is true): a landing takes only such a tip (docs/gate.md "Landing"). A tip that is not
    a commit of the repository is skipped (the landing refuses it for that); a record already
    there is kept and only marked full. The record file names this wrote that were not there
    before."""
    from kitlib.git import refs
    from kitlib.records import gate_ok
    made = []
    for tip in tips:
        if not isinstance(tip, str) or not refs.is_sha(tip) or refs.resolve(tip, root)[0] != tip:
            continue
        rec, _ = gate_ok.read(root, tip)
        if rec is None:
            rec = gate_ok.build(root, tip, {}, {'version': 'test'}, 'test', gate_ok.utc(),
                                [{'name': 'unit'}])
            made.append(tip + '.json')
        rec['full'] = True
        gate_ok.write(root, rec)
    return made


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


REAL_BOOK = lockrun.book
# The machine lock's book takes resource= and priority= (its landing queue): only then does the
# lock itself keep a second landing of one repository from its turn (docs/lock-api.md).
HAS_RESOURCE = 'resource' in inspect.signature(REAL_BOOK).parameters


def compat_book(calls, real=None):
    """kitlib.lock.run.book that records each call in `calls` ({kind, repo, slug} and the
    keywords), then books for real with the keywords the real book takes. The landing books with
    resource= and priority= (the machine lock's landing queue); until a checkout's book takes them
    they are recorded and dropped, so the tests run on either side of that change."""
    real = real or REAL_BOOK
    params = inspect.signature(real).parameters

    def book(kind, repo, slug, **kw):
        calls.append(dict(kw, kind=kind, repo=repo, slug=slug))
        return real(kind, repo, slug, **{k: v for k, v in kw.items() if k in params})
    return book


def patch_book(case):
    """Replace kitlib.lock.run.book for the test `case` with compat_book; the list of calls."""
    calls = []
    patcher = mock.patch.object(lockrun, 'book', compat_book(calls))
    patcher.start()
    case.addCleanup(patcher.stop)
    return calls


class GateRepoCase(StoreCase):
    """self.repo: main with `.kit.json` (self.GATE; None: no "gate" section, so the gate is
    auto-detected) committed and pushed to a bare remote; self.book_kw books one fake cpu slot
    on the scratch store; self.book_calls: every kitlib.lock.run.book call (compat_book)."""
    GATE = GATE
    KIT = None

    def setUp(self):
        StoreCase.setUp(self)
        self.book_calls = patch_book(self)
        self.confine(1)
        kit = dict(self.KIT or {})
        if self.GATE is not None:
            kit['gate'] = self.GATE
        text = json.dumps(kit, indent=1) + '\n'

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
