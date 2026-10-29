"""Shared fixtures for the gate tests: a scratch git repository (tests.gitfixture, with a bare
remote in the same temp folder) holding a `.kit.json` gate, and the scratch machine-lock store of
tests/lock/helpers.StoreCase. Nothing here touches the machine's lock store, a real repository or
a real remote."""
import faulthandler
import inspect
import json
import os
from unittest import mock

from kitlib.lock import run as lockrun
from tests import gitfixture
from tests.home import child_home, result_drive
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


REVIEW = {'verdict': 'LAND', 'risk': 'Low', 'reviewer': 'pm-kit:reviewer',
          'reviewer_model': 'sonnet', 'reviewer_id': 'fixture-reviewer',
          'author': 'Kit Test / worker', 'session': 'fixture', 'records': []}


def queue_review(root, branches, tips):
    """A LAND verdict (REVIEW, by a reviewer who is not the author) queued for each branch of
    `branches` at its tip of `tips` (one each, or lists, as a batch's --expect-tip): every landing
    needs one (docs/gate.md "Landing" step 4). Only for a branch with no entry at all and whose head
    is that tip: an entry a test wrote is never replaced, and a tip that is not the branch's head
    (a landing the test means to refuse earlier) gets none. The branches queued."""
    from kitlib.gate import verdicts
    from kitlib.git import repo as repomod
    branches = branches if isinstance(branches, (list, tuple)) else [branches]
    tips = tips if isinstance(tips, (list, tuple)) else [tips]
    common, _ = repomod.common_dir(root)
    queued = []
    if not common:
        return queued
    for b, t in zip(branches, tips):
        try:
            if (not isinstance(t, str) or os.path.exists(verdicts.entry_path(common, b))
                    or verdicts.head_of(common, b) != t):
                continue
            verdicts.add(common, dict(REVIEW, branch=b, tip=t, range='main..%s' % t))
        except verdicts.VerdictError:
            continue            # not a branch the queue takes: the landing refuses it as before
        queued.append(b)
    return queued


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


# A gate test still running DUMP_S seconds after its setUp ends writes every thread's stack to
# DUMP_FD, and again every DUMP_S after that; the test's cleanup cancels it (registered last, so it
# runs first). The setUp's own repository build does not count. The landing tests once hung
# with no trace for the hour the suite's pool waits on a unit. Fd 2: under
# tools/kit_tests.py's pool it is the unit's unit-<n>.out (kitlib.tooldeps.pool_worker), and it is
# so even when unittest's buffer has replaced sys.stderr. The slowest gate test takes well under
# DUMP_S, and TakeTurns (tests/gate/test_land.py) waits HANG_S = 600 before it fails a hang.
# DUMP_S is well under the pool's whole-unit kitlib.tooldeps.selfrun.UNIT_TIMEOUT (300), since a
# test starts some way into its unit: a dump at 300 never fired before the unit was killed.
DUMP_S = 120
DUMP_FD = 2


def arm_dump(case):
    """Arms the stack dump (DUMP_S, DUMP_FD, read now) for the test `case`, cancelled in its
    cleanup. faulthandler keeps one such timer per process, so arming replaces the last one. A
    dump that cannot be armed (no usable fd 2) is skipped: it is a diagnostic, never a failure."""
    try:
        faulthandler.dump_traceback_later(DUMP_S, repeat=True, file=DUMP_FD)
    except (ValueError, OSError, RuntimeError):
        return False
    case.addCleanup(faulthandler.cancel_dump_traceback_later)
    return True


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
    on the scratch store; self.book_calls: every kitlib.lock.run.book call (compat_book);
    self.result_drive: the temp dev_drive the result records go under (tests.home.result_drive),
    in the machine config of the PM repository the test's environment names. A test
    still running DUMP_S after its setUp ends dumps every thread's stack (arm_dump, the last step
    of setUp)."""
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
        self.result_drive = result_drive(self)     # before test_env: its tools write there too
        self.env = test_env()
        self.book_kw = {'cores': 1, 'store': self.store, 'env': self.env}
        self.logs = os.path.join(self.tmp, 'logs')
        arm_dump(self)

    def branch(self, name, files):
        """A worktree on branch kit/<name> with `files` ({path: text}) committed: (path, tip)."""
        wt = self.repo.worktree(name)
        for rel, text in files.items():
            self.repo.write(rel, text, root=wt)
        tip = self.repo.commit('work on %s' % name, *files, cwd=wt)
        return wt, tip

    def remote_main(self):
        return self.repo.git('--git-dir', self.repo.remote, 'rev-parse', 'main', cwd=self.repo.base)
