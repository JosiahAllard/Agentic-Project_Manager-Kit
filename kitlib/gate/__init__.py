"""The universal gate (ARCHITECTURE.md "Contracts": Gate-OK record, Landing; "Details").

  steps    the configured steps: built, selected by what changed, judged by their logs
  detect   with no "gate" section, the steps detected from the checkout (source auto)
  runner   the steps run in a machine-lock slot; the branch gate and the merged-tree gate
  land     one branch landed: landing lock, expected refs, merge, merged gate, push, verify
  stop     one gate run ended by its lock run id: its own tree and lock; a landing's is refused
  stash    the stash guard: no gate and no landing while the repository's stash stack has an entry
  measure  a proposed kinds table from the lock's run history (never written in place)

tools/gate.py parses, prints and exits; everything else is here. Nothing here prints: progress
goes to a `say` callable the caller passes. docs/gate.md holds the exit codes and messages.
"""

# Exit codes (docs/gate.md "Exit codes"). The machine lock's refusal codes pass through unchanged
# (kitlib.lock.store: 6 busy, 8 timeout, 10 require-lock, 11 reserved, 12 hold).
OK = 0          # the gate passed / the branch landed / measure wrote its proposal
FAILED = 1      # the gate failed / the landing did not land (red gate, merge conflict, push
                # refused: nothing pushed, the merge dropped)
USAGE = 2       # the command line or a named step is wrong; nothing ran
STUCK = 3       # a landing's push may have landed and git cannot tell: the landing lock is
                # left held at phase "stuck" for a person to look at
REFUSED = 4     # a precondition does not hold (config, checkout, expected refs); nothing changed
BUSY = 5        # another landing holds the repository's landing lock
LANDED_UNGATED = 9  # retired: no landing produces it (it was a landing finished on commits made
                    # on main while its merged gate ran); kept so old result records still read
STOPPED = 16    # a stop (gate.py stop, machine_lock.py stop) ended the gate before its verdict: no
                # pass and no gate-OK record. `gate.py stop` itself exits OK, FAILED or REFUSED.
                # Unused by the load node's codes too (kitlib.loadnode.job): node.py gate passes
                # a gate's code on, and there 13 is a job that succeeded

NAMES = {OK: 'OK', FAILED: 'FAILED', USAGE: 'USAGE', STUCK: 'STUCK', REFUSED: 'REFUSED',
         BUSY: 'BUSY', LANDED_UNGATED: 'LANDED_UNGATED', STOPPED: 'STOPPED'}


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
