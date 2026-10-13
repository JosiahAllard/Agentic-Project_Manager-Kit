"""Who may take each backlog action (owner-authored; in OWNER_PATHS, so agents cannot widen it).

The guard reads this map from the installed kit, never from a branch. For each command it takes
role = role_of(session mark or bare agent_type, subagent), sorts the command (a backlog.py verb, a
python import of kitlib.backlog, or a raw write found by its target) into ACTIONS rows, keeps the
narrowest (a row's `over` names the broader rows it replaces), and refuses unless allows() holds
for every row left. It audits AUDITED risks and requires --why for NEEDS_WHY ones.

A Row: verbs, risk, allowed (roles always allowed), when ((role, fact) pairs: allowed once the
guard has established the fact), over. SESSION_ONLY roles come only from a launcher session mark
(owner: a session nothing marks); a subagent naming one is unlisted. An unlisted role (role_of
gives None: a built-in agent, a plain session) may take only the risks in UNLISTED_MAY.

The groups are not names written here: they are read from this kit's role files
(kitlib.content.roster; docs/multi-host-plan.md section 7, decision 17), so a role is added with
one file. A role's group is its `mode:`'s (controller: lead, write: builder, read and test:
read-only, lander: lander) unless its file names `group:` (records and spec-writer: docs); a file
that says `run: session` is SESSION_ONLY. An old name (kitlib.content.ALIASES) is in its new
name's group for one release. Only OWNER, which no file defines, and PM, the one lead TOP adds to
the owner, are named here.
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
SESSION_ONLY = (OWNER,) + _names(lambda r: r.run == 'session' and r.group is not None)
TOP = (OWNER, PM)

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

# Sorting a `set`: TEXT_FIELDS set-text, other fields but priority set-other. Priority: HOLD
# set-hold (a held record makes pull-held and close-held), PRICE_TOP set-price-owner, PRICE_LOW
# set-price on an unpriced or agent-priced record; on a PRICED_BY_OWNER record, set-price-owner.
TEXT_FIELDS = ('summary', 'scope', 'effort', 'body')
HOLD = '-1'
PRICE_LOW = ('2', '3', '4', '5')
PRICE_TOP = ('1',)
PRICED_BY_OWNER = ('owner', 'pm')

Row = collections.namedtuple('Row', 'verbs risk allowed when over')

ACTIONS = collections.OrderedDict([
    ('read', Row(('next', 'show', 'list', 'check', 'stale', 'landable'), NONE, ROLES, (), ())),
    ('add', Row(('add',), LOW, tuple(r for r in ROLES if r not in READ_ONLY), (), ())),
    ('set-text', Row(('set',), LOW, LEADS, OWN_GRANTS, ())),
    ('set-price', Row(('set',), LOW, LEADS, (), ())),
    ('set-price-owner', Row(('set',), MEDIUM, TOP, (), ('set-price',))),
    ('set-hold', Row(('set',), HIGH, TOP, (), ('set-price', 'set-price-owner'))),
    ('set-other', Row(('set',), MEDIUM, LEADS, (), ())),
    ('pull', Row(('pull',), LOW, LEADS + BUILDERS, (), ())),
    ('pull-collision', Row(('pull',), MEDIUM, LEADS, (), ('pull',))),
    ('pull-held', Row(('pull',), REFUSED, (), (), ('pull', 'pull-collision'))),
    ('release', Row(('release',), LOW, LEADS, OWN_GRANTS, ())),
    ('block', Row(('block', 'unblock'), LOW, LEADS, OWN_GRANTS, ())),
    ('block-owner', Row(('block', 'unblock'), MEDIUM, LEADS, (), ('block',))),
    ('set-status', Row(('set-status', 'unpark'), MEDIUM, LEADS, (), ())),
    ('close', Row(('close',), MEDIUM, LEADS, tuple((r, VIA_LAND) for r in LANDERS), ())),
    ('close-held', Row(('close', 'retire'), HIGH, TOP, (), ('close', 'retire'))),
    ('retire', Row(('retire',), MEDIUM, LEADS, (), ())),
    ('retarget', Row(('retarget', 'migrate'), HIGH, TOP, (), ())),
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
