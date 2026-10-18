"""kit_tests' two passes: the tests on the fake git, then a pass on real git (docs/test-tools.md
"Fake git"). --both-tiers runs them (the daily full run, through a gate step's `daily_cmd`,
docs/gate.md "Steps"), and so does a --select or --full run whose change adds or changes a test
module (changed_test_modules): its real-git pass runs those modules.

`run_tiers(run_one, out, label)` calls `run_one(fake, tier_out, last)` once per tier in TIERS order:
`fake` is the KIT_FAKE_GIT value the units get, `tier_out` the stream that pass writes to, and
`last` is true only for the last pass (only it may give cpu slots back to the lock: an earlier
pass that gave slots back would leave the next one running on slots the run no longer holds).

Each pass's own `kit_tests: ` lines are written as `kit_tests [<tier>]: ` (TierOut), so a pass
verdict such as `kit_tests [fake git]: OK in ...` never reads as the run's `kit_tests: OK`. After
each pass a `kit_tests: tier <tier>: OK|FAILED, exit N` line; then one verdict: `kit_tests: OK,
both tiers green (fake git, real git)` only when every pass exited 0, else `kit_tests: FAILED,
exit 1: red tier(s): ...`. A pass that is interrupted (exit 130) stops the run with 130.

Before each pass a `kit_tests: <label>: tier <tier> (KIT_FAKE_GIT=<0|1>)` header; `label` names
what runs the two passes (default `--both-tiers`).

The real-git pass runs only some modules, not the whole selection: under --both-tiers
REAL_GIT_MODULES and the added or changed test modules (real_git_modules), otherwise only the
added or changed ones.

Nothing here starts a process or reads the environment.
"""
import threading

PREFIX = 'kit_tests: '
# The real-git pass runs only these modules (prefixes): the git, worktree and landing tests, and
# the daily-only tests/stubs/test_new_repo.py, which skips unless the pass is on real git. The
# fake pass runs every module.
REAL_GIT_MODULES = ('tests.git.', 'tests.worktree.', 'tests.gate.test_land',
                    'tests.stubs.test_new_repo')
TIERS = (('fake git', '1'), ('real git', '0'))
INTERRUPTED = 130


class TierOut(object):
    """A text stream over `out` that writes each line starting `kit_tests: ` as
    `kit_tests [<tier>]: `; other lines pass unchanged. Lines are rewritten whole: a partial line
    waits for its end (or flush). Thread-safe: the pool's threads and the main thread (its
    give-back lines) write to one pass's stream at once, so the buffer is held under a lock."""

    def __init__(self, out, tier):
        self.out = out
        self.tier = tier
        self._pending = ''
        self._lock = threading.Lock()

    def _line(self, line):
        if line.startswith(PREFIX):
            line = 'kit_tests [%s]: %s' % (self.tier, line[len(PREFIX):])
        self.out.write(line)

    def write(self, text):
        with self._lock:
            self._pending += text
            while True:
                i = self._pending.find('\n')
                if i < 0:
                    break
                self._line(self._pending[:i + 1])
                self._pending = self._pending[i + 1:]
        return len(text)

    def flush(self):
        with self._lock:
            if self._pending:
                self._line(self._pending)
                self._pending = ''
            self.out.flush()


def real_git_modules(modules, also=()):
    """The modules of `modules` --both-tiers' real-git pass runs: REAL_GIT_MODULES and those in
    `also` (the added or changed test modules), in `modules`' order, then any of `also` that
    `modules` does not hold; each once."""
    picked = [m for m in modules if m.startswith(REAL_GIT_MODULES) or m in also]
    return picked + [m for m in dict.fromkeys(also) if m not in picked]


def changed_test_modules(changed, modules):
    """The test modules of `modules` (dotted names, the tree's) that the changed paths `changed`
    (repo-relative, either slash) add or change, in `modules`' order. A path that is not a test
    module of the tree - a deleted module, a helper, data, code - gives none."""
    wanted = set()
    for path in changed:
        path = path.replace('\\', '/')
        if path.endswith('.py'):
            wanted.add(path[:-len('.py')].replace('/', '.'))
    return [m for m in modules if m in wanted]


def run_tiers(run_one, out, label='--both-tiers'):
    """Run every tier (module docstring); the run's exit status: 0 every pass green, 1 any red,
    INTERRUPTED when a pass was."""
    red = []
    for i, (tier, fake) in enumerate(TIERS):
        out.write('%s%s: tier %s (KIT_FAKE_GIT=%s)\n' % (PREFIX, label, tier, fake))
        out.flush()
        stream = TierOut(out, tier)
        rc = run_one(fake, stream, i == len(TIERS) - 1)
        stream.flush()
        out.write('%stier %s: %s\n' % (PREFIX, tier, 'OK' if rc == 0 else 'FAILED, exit %d' % rc))
        out.flush()
        if rc == INTERRUPTED:
            out.write('%sINTERRUPTED, exit %d in tier %s\n' % (PREFIX, INTERRUPTED, tier))
            out.flush()
            return INTERRUPTED
        if rc != 0:
            red.append(tier)
    if red:
        out.write('%sFAILED, exit 1: red tier(s): %s\n' % (PREFIX, ', '.join(red)))
        out.flush()
        return 1
    out.write('%sOK, both tiers green (%s)\n' % (PREFIX, ', '.join(t for t, _ in TIERS)))
    out.flush()
    return 0
