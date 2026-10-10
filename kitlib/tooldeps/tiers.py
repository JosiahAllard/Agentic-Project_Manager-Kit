"""kit_tests --both-tiers: the same tests on the fake git, then on real git (docs/test-tools.md
"Fake git"). The daily full run uses it through a gate step's `daily_cmd` (docs/gate.md "Steps").

`run_tiers(run_one, out)` calls `run_one(fake, tier_out, last)` once per tier in TIERS order:
`fake` is the KIT_FAKE_GIT value the units get, `tier_out` the stream that pass writes to, and
`last` is true only for the last pass (only it may give cpu slots back to the lock: an earlier
pass that gave slots back would leave the next one running on slots the run no longer holds).

Each pass's own `kit_tests: ` lines are written as `kit_tests [<tier>]: ` (TierOut), so a pass
verdict such as `kit_tests [fake git]: OK in ...` never reads as the run's `kit_tests: OK`. After
each pass a `kit_tests: tier <tier>: OK|FAILED, exit N` line; then one verdict: `kit_tests: OK,
both tiers green (fake git, real git)` only when every pass exited 0, else `kit_tests: FAILED,
exit 1: red tier(s): ...`. A pass that is interrupted (exit 130) stops the run with 130.

Nothing here starts a process or reads the environment.
"""
PREFIX = 'kit_tests: '
TIERS = (('fake git', '1'), ('real git', '0'))
INTERRUPTED = 130


class TierOut(object):
    """A text stream over `out` that writes each line starting `kit_tests: ` as
    `kit_tests [<tier>]: `; other lines pass unchanged. Lines are rewritten whole: a partial line
    waits for its end (or flush)."""

    def __init__(self, out, tier):
        self.out = out
        self.tier = tier
        self._pending = ''

    def _line(self, line):
        if line.startswith(PREFIX):
            line = 'kit_tests [%s]: %s' % (self.tier, line[len(PREFIX):])
        self.out.write(line)

    def write(self, text):
        self._pending += text
        while True:
            i = self._pending.find('\n')
            if i < 0:
                break
            self._line(self._pending[:i + 1])
            self._pending = self._pending[i + 1:]
        return len(text)

    def flush(self):
        if self._pending:
            self._line(self._pending)
            self._pending = ''
        self.out.flush()


def run_tiers(run_one, out):
    """Run every tier (module docstring); the run's exit status: 0 every pass green, 1 any red,
    INTERRUPTED when a pass was."""
    red = []
    for i, (tier, fake) in enumerate(TIERS):
        out.write('%s--both-tiers: tier %s (KIT_FAKE_GIT=%s)\n' % (PREFIX, tier, fake))
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
