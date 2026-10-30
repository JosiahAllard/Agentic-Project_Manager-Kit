"""Rule modes (docs/guard.md "Rule modes"): what a guard rule's refusal does, by the rule's name.

  deny   the refusal stands (its verdict, deny or ask, is the rule's)
  warn   the call goes ahead, and the refusal's reason is handed to the agent as a warning
  off    the call goes ahead, nothing said

DEFAULTS holds the kit's modes; a rule it does not name is deny. The owner overrides them in the
machine config's `guard_rules` ({rule: deny|warn|off}), and only there: never .kit.json or any
other file a branch carries, so a branch can never loosen the rules it is judged by. A value the
config does not hold as one of the three is ignored (the default stands).

kitlib.guard.rules.refuse asks mode() for every refusal and kitlib.guard.rules.decide holds the
modes for one call (judging()); with none held (a rule called on its own, as a test does) the
defaults apply and a warning goes nowhere.

  mode(rule)          the mode of `rule` for the call being judged
  note(text)          keep a warning for the call being judged
  judging(ctx)        a context manager: the call's modes (resolve(ctx)) and warnings, yielding
                      the warnings list
  resolve(ctx)        DEFAULTS overlaid with the machine config's guard_rules
"""
import contextlib
import contextvars

DENY, WARN, OFF = 'deny', 'warn', 'off'
MODES = (DENY, WARN, OFF)
CONFIG_KEY = 'guard_rules'

# The kit's own modes. A rule not named here is deny. owner-only, the hub's owner verbs on a parsed
# hub tool run (hub-owner-verb), the hub's secrets, machine config, ledger, PM data,
# lock state, .kit.json, generated files, gate results, destructive git in a main checkout, branch -D,
# forcing or deleting main, core.hooksPath, owner identity, the owner's delegation list
# (owner-delegations), kill-by-name, the backlog roles, raw
# writes and audit log, file-tool edits of a main checkout and the write jail's outer bound
# (dev-drive: no agent write off the Dev Drive) stay deny (docs/guard.md "Rule modes").
DEFAULTS = {
    'unplaced': WARN,           # a write the guard cannot place (kitlib.guard.rules.UNPLACED)
    # dropped as blocks: the owner can turn each back on
    'wrapped-gate': OFF,
    'busy-wait': OFF,
    # dropped as blocks, kept as warnings: what the guard cannot read it cannot place, and
    # it catches honest mistakes, not evasion
    'unreadable': WARN,         # a line the parser cannot read: the kept rules still judge its text
    'git-alias': WARN,          # an alias on the command line: the command it runs is judged
    # turned into warnings
    'heavy': WARN,              # lock etiquette: a heavy program run with no machine lock held
}

_STATE = contextvars.ContextVar('kit_guard_modes', default=None)


def resolve(ctx):
    """DEFAULTS overlaid with the valid entries of the machine config's guard_rules (ctx.user());
    any failure to read it leaves the defaults."""
    out = dict(DEFAULTS)
    try:
        user = ctx.user() if ctx is not None else None
        given = user.get(CONFIG_KEY) if isinstance(user, dict) else None
    except Exception:                           # the defaults stand; the rules report a broken config
        given = None
    if isinstance(given, dict):
        for rule, m in given.items():
            if isinstance(rule, str) and m in MODES:
                out[rule] = m
    return out


def mode(rule):
    state = _STATE.get()
    modes = DEFAULTS if state is None else state[0]
    return modes.get(rule, DENY)


def note(text):
    state = _STATE.get()
    if state is not None and text not in state[1]:
        state[1].append(text)


@contextlib.contextmanager
def judging(ctx):
    warnings = []
    token = _STATE.set((resolve(ctx), warnings))
    try:
        yield warnings
    finally:
        _STATE.reset(token)
