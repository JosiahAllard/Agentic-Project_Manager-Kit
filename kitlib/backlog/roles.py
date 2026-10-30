"""Who may take each backlog action (owner-authored; in OWNER_PATHS, so agents cannot widen the rows).

The readers are kitlib.backlog.policy, which tools/backlog.py runs before every write (and the
guard's backlog-role rule runs for a main session), and kitlib.agentview, which shows each role's
rights. For each command: role = role_of(session mark or bare agent_type, subagent); sort the command
(a backlog.py verb, a python import of kitlib.backlog, or a raw write found by its target) into
ACTIONS rows, keep the narrowest (a row's `over` names the broader rows it replaces), and refuse
unless allows() holds for every row left; audit AUDITED risks and require --why for NEEDS_WHY ones.
Fail closed: a write command no row names is refused for every role, so a new backlog.py write verb
needs a row here before anyone can run it.

A Row: verbs, risk, allowed (roles always allowed), when ((role, fact) pairs: allowed once the
guard has established the fact), over. SESSION_ONLY roles come only from a launcher session mark
(owner: a session nothing marks); a subagent naming one is unlisted. An unlisted role (role_of
gives None: a built-in agent, a plain session) may take only the risks in UNLISTED_MAY.

The groups are not names written here: they are read from this kit's role files
(kitlib.content.roster; docs/multi-host-plan.md section 7, decision 17), so a role is added with
one file. A role's group is its `mode:`'s (controller: lead, write: builder, read and test:
read-only, lander: lander) unless its file names `group:` (docs-writer: docs, though its mode is write); a file
that says `run: session` is SESSION_ONLY. An old name (kitlib.content.ALIASES) is in its new
name's group for one release. Only OWNER, which no file defines, and PM, the one lead TOP adds to
the owner, are named here; both are SESSION_ONLY whatever a role file says, so a TOP right never
reaches a subagent. The role files are not owner paths: a change to a file's `mode:`, `group:` or
`run:` moves that role between groups once it lands in the installed kit. A role file that cannot
be read is in no group (its role is unlisted), and with none readable only the owner is listed.
"""
import collections
import os

from kitlib import content as _content

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROSTER = _content.roster(KIT)


def _names(keep):
    """The names of the roster's roles `keep(entry)` holds for, by name, each followed by its old
    names (kitlib.content.ALIASES)."""
    return tuple(n for r in _ROSTER if keep(r) for n in _content.names_of(r.name))


OWNER, PM = 'owner', 'pm'
LEADS = (OWNER,) + _names(lambda r: r.group == 'lead')
BUILDERS = _names(lambda r: r.group == 'builder')
DOCS = _names(lambda r: r.group == 'docs')
READ_ONLY = _names(lambda r: r.group == 'read-only')
LANDERS = _names(lambda r: r.group == 'lander')
ROLES = LEADS + BUILDERS + LANDERS + DOCS + READ_ONLY
TOP = (OWNER, PM)
SESSION_ONLY = TOP + tuple(n for n in _names(lambda r: r.run == 'session' and r.group is not None)
                           if n not in TOP)

NONE, LOW, MEDIUM, HIGH, REFUSED = 'none', 'low', 'medium', 'high', 'refused'
RISKS = (NONE, LOW, MEDIUM, HIGH, REFUSED)
AUDITED = (MEDIUM, HIGH)
NEEDS_WHY = (HIGH,)
UNLISTED_MAY = (NONE,)

# The facts the guard establishes before a `when` grant counts.
OWN_PULL = 'own-pull'      # the record's pull is this session's own
VIA_LAND = 'via-land'      # the close is the landing tool's, not a backlog.py command
FACTS = (OWN_PULL, VIA_LAND)
OWN_GRANTS = tuple((role, OWN_PULL) for role in BUILDERS)

# Sorting a `set`: TEXT_FIELDS set-text, other fields but priority set-other. Priority: PRICE_TOP
# set-price-owner; PRICE_LOW, or any other value (the retired -1, which the plan refuses),
# set-price on an unpriced or agent-priced record; on a PRICED_BY_OWNER record, or one blocked on
# the owner, set-price-owner. `priority_by` set to a PRICED_BY_OWNER value, or changed on a
# PRICED_BY_OWNER record, set-price-owner. `blocked_on` changed on a record blocked on the owner,
# unblock-owner. A record blocked on the owner (owner-held) makes pull-held and close-held.
TEXT_FIELDS = ('summary', 'scope', 'effort', 'body')
PRICE_LOW = ('2', '3', '4', '5')
PRICE_TOP = ('1',)
PRICED_BY_OWNER = ('owner', 'pm')

Row = collections.namedtuple('Row', 'verbs risk allowed when over')

ACTIONS = collections.OrderedDict([
    ('read', Row(('next', 'show', 'list', 'check', 'stale', 'landable', 'find'), NONE, ROLES, (), ())),
    ('add', Row(('add',), LOW, tuple(r for r in ROLES if r not in READ_ONLY), (), ())),
    ('set-text', Row(('set',), LOW, LEADS, OWN_GRANTS, ())),
    ('set-price', Row(('set',), LOW, LEADS, (), ())),
    ('set-price-owner', Row(('set',), MEDIUM, TOP, (), ('set-price',))),
    ('set-other', Row(('set',), MEDIUM, LEADS, (), ())),
    # A pulled record's scope widened into another branch's live pull (or a path its branch
    # changed): computed by the plan (kitlib/backlog/plan.py plan_set), not read from a flag.
    ('set-widen-collision', Row(('set',), MEDIUM, LEADS, (), ())),
    ('pull', Row(('pull',), LOW, LEADS + BUILDERS, (), ())),
    # A pull whose scope collides with a live pull on another branch, or folds into its work:
    # computed by the plan (plan_pull), not read from a flag.
    ('pull-collision', Row(('pull',), MEDIUM, LEADS, (), ('pull',))),
    ('pull-held', Row(('pull',), REFUSED, (), (), ('pull', 'pull-collision'))),
    ('release', Row(('release',), LOW, LEADS, OWN_GRANTS, ())),
    ('block', Row(('block', 'unblock'), LOW, LEADS, OWN_GRANTS, ())),
    ('block-owner', Row(('block',), MEDIUM, LEADS, (), ('block',))),
    # Clearing the owner from a record's blockers (`unblock`, or a `set` of `blocked_on`) is TOP's.
    ('unblock-owner', Row(('unblock', 'set'), MEDIUM, TOP, (), ('block',))),
    ('set-status', Row(('set-status', 'unpark'), MEDIUM, LEADS, (), ())),
    ('close', Row(('close',), MEDIUM, LEADS, tuple((r, VIA_LAND) for r in LANDERS), ())),
    # A held record: closing, retiring or deleting it is TOP's (the owner added delete).
    ('close-held', Row(('close', 'retire', 'delete'), HIGH, TOP, (), ('close', 'retire', 'delete'))),
    # A record the owner or the PM priced (PRICED_BY_OWNER), not held: retiring or deleting it is
    # TOP's.
    ('retire-priced', Row(('retire', 'delete'), MEDIUM, TOP, (), ('retire', 'delete'))),
    ('retire', Row(('retire',), MEDIUM, LEADS, (), ())),
    ('delete', Row(('delete',), MEDIUM, LEADS, (), ())),
    ('clear-review', Row(('clear-review',), MEDIUM, LEADS, (), ())),
    ('retarget', Row(('retarget', 'migrate', 'uncite'), HIGH, TOP, (), ())),
    # `catch-up --apply PATH...` commits the PM data worktree's uncommitted record paths; the plain
    # `catch-up` lists them (a read). Coordinators, the PM and the owner use it.
    ('catch-up', Row(('catch-up',), MEDIUM, LEADS, (), ())),
    # A raw write: a direct edit of a record file or a ref write (refs/backlog, git update-ref),
    # judged at the write; a commit of what the backlog tool wrote (land.py's deletions) is not.
    ('raw-write', Row((), HIGH, (), (), ())),
])


def role_of(name, subagent):
    """The role for a session mark, or a subagent's bare agent_type; None: unlisted."""
    if name not in ROLES or (subagent and name in SESSION_ONLY):
        return None
    return name


def allows(action, role, facts=()):
    """True when `role` (role_of's answer) may take `action`, given the established facts."""
    row = ACTIONS[action]
    if role is None:
        return row.risk in UNLISTED_MAY
    return role in row.allowed or any(r == role and f in facts for r, f in row.when)
