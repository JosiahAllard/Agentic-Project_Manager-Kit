"""A hold's end condition that a program can check: the `until` key (docs/backlog.md "Fields",
"block --until").

An `until` line is `<member> <condition>`: the `blocked_on` member it ends and, after one space,
the condition. `block ID --on M --until "<condition>"` writes it; the Action Hub's
unblock-conditions job (kitlib.hubjobs.unblock_conditions) reads it, and when the condition is met
clears that member (plan.plan_condition_met). The conditions it reads:

  todo:T-n done                the hub to-do T-n is done
  question:Q-n answered        the hub question Q-n is answered (filed, logged or done count too)
  record:<repo>:<ID> closed    record ID is gone from repo's store: a commit removed it (close,
                               retire or delete)
  path:<path> exists|absent    the file or folder is there, or not; <path> is absolute, or starts
                               with a %VAR% (machine paths stay out of records), and may hold spaces
  plugin:<folder> absent       the plugin export folder <folder> is gone from the export root

Any other text is free text: it is kept, and nothing acts on it (it is cleared by hand, with
`unblock --on M`). An `owner` member ends only on an owner event, a to-do or a question
(OWNER_KINDS): any other condition on `owner` is refused at block time, and the job skips one.

  split(value)              (member, condition) of an until line; condition '' when it has none
  parse(text)               a Condition, or None when the text is not one of the forms
  kind_of(text)             the form a text names by its prefix (`path:` ...), readable or not
  owner_error(member, text) None, or why `member` may not end on `text`
  evaluate(cond, readers)   True (met), False (not met) or None (cannot tell); only True acts
  expand(path, environ)     a path's %VAR%s filled from `environ`; None when one is not set

Nothing here reads a file, a store or the hub: evaluate asks the `readers` it is given.
"""
import collections
import os
import re

KINDS = ('todo', 'question', 'record', 'path', 'plugin')
OWNER_KINDS = ('todo', 'question')      # the owner's own acts: the only end of an `owner` hold
# The hub question statuses that hold the owner's answer (kitlib.hubvalidate STATUSES; the same
# set as audit_check.ANSWERED).
ANSWERED = ('answered', 'filed', 'logged', 'done')

Condition = collections.namedtuple('Condition', 'kind target state')

_VAR = re.compile(r'%([A-Za-z_][A-Za-z0-9_]*)%')
_FORMS = {
    'todo': re.compile(r'^todo:(T-[0-9]+) (done)$'),
    'question': re.compile(r'^question:(Q-[0-9]+) (answered)$'),
    'record': re.compile(r'^record:([A-Za-z0-9][A-Za-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9.-]*) '
                         r'(closed)$'),
    'path': re.compile(r'^path:(\S(?:.*\S)?) (exists|absent)$'),
    'plugin': re.compile(r'^plugin:([A-Za-z0-9][A-Za-z0-9._-]*) (absent)$'),
}


def split(value):
    """(member, condition) of an `until` value: the member is its first word."""
    member, _, cond = (value or '').partition(' ')
    return member, cond


def kind_of(text):
    """The form `text` names by its prefix (`todo`, `question`, `record`, `path`, `plugin`), or
    None: read before the text is parsed, so a malformed `path:...` is still a path condition."""
    prefix, sep, _ = (text or '').partition(':')
    return prefix if sep and prefix in KINDS else None


def parse(text):
    """The Condition `text` states, or None when it is not one of the forms (free text)."""
    kind = kind_of(text)
    if kind is None:
        return None
    m = _FORMS[kind].match(text)
    if m is None:
        return None
    target, state = m.group(1), m.group(2)
    if kind == 'path' and not (os.path.isabs(target) or _VAR.match(target)):
        return None
    return Condition(kind, target, state)


def owner_error(member, text):
    """None, or why the hold `member` may not end on `text`: an `owner` hold ends only on an
    owner event, a to-do or a question (OWNER_KINDS)."""
    kind = kind_of(text)
    if member != 'owner' or kind is None or kind in OWNER_KINDS:
        return None
    return ('`owner` ends only on an owner event (%s), not on a `%s:` condition: the tool never '
            'clears the owner\'s hold on its own reading' % (
                ', '.join('`%s:`' % k for k in OWNER_KINDS), kind))


def expand(path, environ=None):
    """`path` with each %VAR% filled from `environ` (default os.environ), or None when one is not
    set or is empty."""
    env = os.environ if environ is None else environ
    missing = []

    def fill(m):
        v = env.get(m.group(1))
        if not v:
            missing.append(m.group(1))
            return ''
        return v
    out = _VAR.sub(fill, path)
    return None if missing else out


def evaluate(cond, readers):
    """Whether `cond` (a Condition) is met: True, False, or None when it cannot be told (a store,
    a hub document or a folder that cannot be read). `readers` gives

      todo_status(tid)            the to-do's status, or None (no such to-do, unreadable)
      question_status(qid)        the question's status, or None
      record_closed(repo, rid)    True (a commit removed it), False (still there), None
      path_exists(path)           True or False for an absolute path, None when unreadable
      plugin_present(folder)      True or False, None when the export root is not known
    """
    if cond.kind == 'todo':
        status = readers.todo_status(cond.target)
        return None if status is None else status == 'done'
    if cond.kind == 'question':
        status = readers.question_status(cond.target)
        return None if status is None else status in ANSWERED
    if cond.kind == 'record':
        repo, _, rid = cond.target.partition(':')
        return readers.record_closed(repo, rid)
    if cond.kind == 'path':
        path = expand(cond.target)
        if path is None or not os.path.isabs(path):
            return None
        there = readers.path_exists(path)
        return None if there is None else there == (cond.state == 'exists')
    if cond.kind == 'plugin':
        there = readers.plugin_present(cond.target)
        return None if there is None else not there
    return None
