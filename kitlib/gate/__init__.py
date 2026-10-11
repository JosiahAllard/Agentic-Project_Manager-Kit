"""The universal gate (ARCHITECTURE.md "Contracts": Gate-OK record, Landing; "Details").

  steps    the configured steps: built, selected by what changed, judged by their logs
  runner   the steps run in a machine-lock slot; the branch gate and the merged-tree gate
  land     one branch landed: landing lock, expected refs, merge, merged gate, push, verify
  measure  a proposed kinds table from the lock's run history (never written in place)

tools/gate.py parses, prints and exits; everything else is here. Nothing here prints: progress
goes to a `say` callable the caller passes. docs/gate.md holds the exit codes and messages.
"""

# Exit codes (docs/gate.md "Exit codes"). The machine lock's refusal codes pass through unchanged
# (kitlib.lock.store: 6 busy, 8 timeout, 10 require-lock, 11 reserved, 12 hold).
OK = 0          # the gate passed / the branch landed / measure wrote its proposal
FAILED = 1      # the gate failed / the landing did not land (red gate undone, merge conflict)
USAGE = 2       # the command line or a named step is wrong; nothing ran
STUCK = 3       # a landing stopped after its merge and could not undo it: the landing lock is
                # left held at phase "stuck" for a person to look at
REFUSED = 4     # a precondition does not hold (config, checkout, expected refs); nothing changed
BUSY = 5        # another landing holds the repository's landing lock
LANDED_UNGATED = 9  # a landing landed, but commits made on main while its merged gate ran change
                    # paths a gate step selects: main was not gated whole (kitlib.gate.land.adopt)

NAMES = {OK: 'OK', FAILED: 'FAILED', USAGE: 'USAGE', STUCK: 'STUCK', REFUSED: 'REFUSED',
         BUSY: 'BUSY', LANDED_UNGATED: 'LANDED_UNGATED'}


class Outcome(object):
    """What a gate, a landing or a measure ended with: `code` (an exit code above or a machine
    lock code), `lines` (what the entry prints, in order), and the facts tests read."""

    def __init__(self, code, lines=(), **facts):
        self.code = code
        self.lines = list(lines)
        self.facts = facts

    def __getattr__(self, name):
        try:
            return self.__dict__['facts'][name]
        except KeyError:
            raise AttributeError(name)

    def __repr__(self):
        return 'Outcome(%s, %r)' % (self.code, self.lines)
