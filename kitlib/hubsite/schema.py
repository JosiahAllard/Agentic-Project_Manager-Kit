"""kitlib.hubsite.schema: the fields each hub collection's documents must carry (docs/action-hub.md
field tables), checked by the store before every write.

  COLLECTIONS            the collections the hub store holds
  ID_PREFIX              {collection: prefix} of the collections whose ids the store assigns
  STORE_KEYS             the keys the store itself sets on every document (version, updated_at)
  check(coll, fields)    [kitlib.hubvalidate.Finding]: an `error` per missing required field, per
                         field of the wrong type, and per to_pm, handoffs or approvals kind that is not
                         known (an unknown status is kitlib.hubvalidate's status-unknown error);
                         an approval's required fields depend on its kind (BY_KIND)
  check_id(doc_id)       an error message for a document id that cannot be a file name, else None
  whole_recommended(coll, fields)
                         (fields, old, label): a question whose `recommended` is text that
                         stands for a number, given back with the number; read so by the
                         store

A field that is absent or null passes its type check unless it is required: the page writes many
fields as null until they are set (sent_at, q, reply). Unknown fields pass. The status values and
the ref prefixes stay in kitlib.hubvalidate (STATUSES, REF_COLLECTIONS), the single source.
Nothing here prints, parses arguments or exits.
"""
import re

from kitlib import hubvalidate

COLLECTIONS = ('questions', 'inbox', 'todos', 'to_pm', 'stories', 'pages', 'page_html', 'repos',
               'approvals', 'schedules', 'finding_marks', 'skill_edits', 'landings', 'handoffs')
ID_PREFIX = {coll: prefix for prefix, coll in hubvalidate.REF_COLLECTIONS.items()}
STORE_KEYS = ('version', 'updated_at')
CODE = 'schema-field'

_STR, _LIST, _DICT, _BOOL, _INT = (str,), (list,), (dict,), (bool,), (int,)
# {collection: (required fields, {field: allowed types})}; a required field is also typed here
FIELDS = {
    'questions': (('status', 'title'), {
        'status': _STR, 'title': _STR, 'repo': _STR, 'from': _STR, 'asked_at': _STR,
        'context': _STR, 'options': _LIST, 'recommended': _INT, 'refs': _LIST, 'urgent': _BOOL,
        'answer': _DICT, 'story': _STR, 'done_why': _STR, 'done_at': _STR, 'done_by': _STR,
        'record': _DICT, 'class': _STR, 'filing': _DICT, 'filed_at': _STR,
        'unfiled_why': _STR}),
    'inbox': (('text',), {
        'text': _STR, 'ref': _STR, 'asked': _STR, 'title': _STR, 'repo': _STR, 'from': _STR,
        'at': _STR, 'refs': _LIST, 'read': _BOOL, 'read_at': _STR}),
    'todos': (('status', 'title'), {
        'status': _STR, 'title': _STR, 'detail': _STR, 'commands': _LIST, 'repo': _STR,
        'from': _STR, 'added_at': _STR, 'refs': _LIST, 'done_at': _STR, 'urgent': _BOOL,
        'pm_ack': (bool, str)}),
    'to_pm': (('status', 'kind', 'text'), {
        'status': _STR, 'kind': _STR, 'text': _STR, 'repo': _STR, 'urgent': _BOOL,
        'created_at': _STR, 'sent_at': _STR, 'reply': _STR, 'done_at': _STR,
        'refs': _LIST, 'finding': _STR}),
    'stories': (('status', 'repo', 'title', 'story'), {
        'status': _STR, 'repo': _STR, 'title': _STR, 'story': _STR, 'urgent': _BOOL,
        'from': _STR, 'submitted_at': _STR, 'reviewer': _STR, 'review_note': _STR,
        'backlog_id': _STR, 'added_at': _STR, 'closed_note': _STR, 'closed_at': _STR}),
    'pages': (('title',), {'title': _STR}),
    'page_html': (('html',), {'html': _STR}),
    'repos': ((), {'name': _STR, 'added_at': _STR, 'aliases': _LIST}),
    # kitlib.hubsite.approvals, "The document"; BY_KIND adds the fields of each kind
    'approvals': (('status', 'repo', 'title', 'created_at'), {
        'status': _STR, 'repo': _STR, 'title': _STR, 'head': _STR, 'files': _LIST,
        'created_at': _STR, 'reason': _STR, 'decision': _STR, 'decided_at': _STR, 'sig': _STR,
        'commit': _STR, 'consumed_at': _STR, 'diff': _LIST, 'kind': _STR, 'op': _STR,
        'line': _STR, 'args_sha256': _STR, 'what': _STR}),
    # kitlib.hubsite.api, "Schedules": the owner's times for the kit's automatic tasks
    'schedules': (('task', 'repo', 'cadence', 'at'), {
        'task': _STR, 'repo': _STR, 'cadence': _STR, 'days': _LIST, 'at': _STR, 'set_at': _STR}),
    # kitlib.hubsite.api, "Findings": the owner's mark on one maintenance-lint finding, by its
    # fingerprint, with what the finding said when it was marked
    'finding_marks': (('status',), {
        'status': _STR, 'repo': _STR, 'check': _STR, 'target': _STR, 'text': _STR, 'note': _STR,
        'marked_at': _STR}),
    # kitlib.hubsite.api, "Skill edits": the owner's new text for one kit skill's SKILL.md, which an
    # agent commits; base_blob is the git blob of the source the owner edited
    'skill_edits': (('status', 'skill', 'text', 'base_blob'), {
        'status': _STR, 'skill': _STR, 'text': _STR, 'base_blob': _STR, 'created_at': _STR,
        'submitted_at': _STR, 'done_at': _STR, 'branch': _STR, 'blob': _STR,
        'source_blob': _STR, 'why': _STR, 'closed_at': _STR}),
    # kitlib.hubsite.landings, the event tools/land.py posts for each branch it landed: the land
    # ledger line's fields (kitlib.gate.result.LEDGER_FIELDS) and landings.EXTRA_FIELDS; the api
    # takes these fields only and sets posted_at
    'landings': (('repo', 'branch', 'tip', 'main'), {
        'branch': _STR, 'tip': _STR, 'run_id': _STR, 'started_at': _STR, 'finished_at': _STR,
        'verdict': _STR, 'code': _INT, 'summary': _STR, 'gate_once': _BOOL, 'escalated': _BOOL,
        'repo': _STR, 'main_branch': _STR, 'main': _STR, 'records': _LIST, 'mode': _STR,
        'deploy': _STR, 'posted_at': _STR}),
    # kitlib.hubsite.api, "Hand-offs": a session's record that it checkpointed and stands down
    # (from: its session key; kind: KINDS['handoffs']; state_path: its state note); the api
    # takes from, kind and state_path only and sets at
    'handoffs': (('from', 'kind', 'state_path', 'at'), {
        'from': _STR, 'kind': _STR, 'state_path': _STR, 'at': _STR}),
}
KINDS = {'to_pm': ('question', 'fyi', 'act'), 'handoffs': ('handoff', 'restart', 'stop')}
# {collection: {kind: more required fields}}: a document of such a collection needs its kind's
# fields too, None standing for a document with no kind; any other kind is an error
BY_KIND = {'approvals': {None: ('head', 'files'), 'op': ('op', 'line', 'args_sha256')}}

_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
# Windows device names: a file named so (any extension) opens the device, not a file
_DEVICES = frozenset(['con', 'prn', 'aux', 'nul'] + ['com%d' % i for i in range(1, 10)]
                     + ['lpt%d' % i for i in range(1, 10)])
_WHOLE = re.compile(r'[0-9]{1,9}')  # a whole number small enough to be a position; more digits
_DIGITS = re.compile(r'[0-9]+')     # than that are kept as text


def _err(coll, doc_id, field, message):
    return hubvalidate.Finding(hubvalidate.ERROR, '%s/%s' % (coll, doc_id), field, CODE, message)


def _type_ok(value, types):
    if bool not in types and isinstance(value, bool):
        return False                    # True is an int to Python, never to the hub
    return isinstance(value, types)


def check(coll, fields, doc_id='?'):
    """The schema errors for a document of `coll` (see the module docstring)."""
    if coll not in FIELDS:
        return [_err(coll, doc_id, '', 'there is no %s/ collection in the hub store' % coll)]
    if not isinstance(fields, dict):
        return [_err(coll, doc_id, '', 'the document is not an object')]
    required, types = FIELDS[coll]
    out = []
    by_kind = BY_KIND.get(coll)
    if by_kind is not None:
        kind = fields.get('kind')
        if kind is None or (isinstance(kind, str) and kind in by_kind):
            required = required + by_kind[kind]
        elif isinstance(kind, str):     # another type is the type check's error below
            out.append(_err(coll, doc_id, 'kind', 'kind %r is not one of %s' % (
                kind, ', '.join(k for k in by_kind if k is not None))))
    for field in required:
        v = fields.get(field)
        if v is None or (isinstance(v, str) and not v.strip()):
            out.append(_err(coll, doc_id, field, '%s is required' % field))
    for field, allowed in sorted(types.items()):
        v = fields.get(field)
        if v is not None and not _type_ok(v, allowed):
            out.append(_err(coll, doc_id, field, '%s must be %s, not %s' % (
                field, ' or '.join(t.__name__ for t in allowed), type(v).__name__)))
    kinds = KINDS.get(coll)
    kind = fields.get('kind')
    if kinds and isinstance(kind, str) and kind.strip() and kind not in kinds:
        out.append(_err(coll, doc_id, 'kind', 'kind %r is not one of %s' % (kind, ', '.join(kinds))))
    return out


def check_id(doc_id):
    """Why `doc_id` cannot name a document file, or None when it can."""
    if not isinstance(doc_id, str) or not _ID.match(doc_id):
        return 'a document id is letters, digits, _ . and -, starting with a letter or digit, ' \
               'not %r' % (doc_id,)
    if doc_id.split('.')[0].lower() in _DEVICES or doc_id.endswith('.'):
        return 'a document id cannot be %r: Windows would not open it as a file' % doc_id
    return None


def whole_recommended(coll, fields):
    """(fields, old, label): a question whose `recommended` is text that stands for a number is
    given back as a copy with the number; old is that text and label is True when it was an
    option's label. Any other document is given back as it is, with old None.

    The rule. Text that is exactly the `label` of one of the question's options (the same
    characters, case and spaces included; an option that is an object) stands for that option's
    position, counted from 0 as hubvalidate's options check and the page count it. Text that is
    a whole number (one to nine of the digits 0-9 and nothing else) stands for that number, in
    range of the options or not. These stand for nothing, and the document is given back as it
    is: text that is not a whole number and is no option's label or the label of two; a whole
    number that is also an option's label, unless it is the label of one option only and that
    option is at the position the number says; more than nine digits; and the empty text. The
    store refuses every update of a question whose `recommended` is text that stands for nothing
    (check), so such a question cannot be answered or logged until its file is fixed. The store
    never rewrites a file for this: one that holds text that stands for a number keeps it, the
    store reads it as the number and its next write stores the number."""
    old = fields.get('recommended') if coll == 'questions' else None
    if not isinstance(old, str) or not old:
        return fields, None, False              # '' names no option, whatever an option's label is
    options = fields.get('options')
    at = [i for i, o in enumerate(options if isinstance(options, list) else [])
          if isinstance(o, dict) and o.get('label') == old]
    if _WHOLE.fullmatch(old):
        if at and at != [int(old)]:
            return fields, None, False          # the number, or one of those options: it does not say
        return dict(fields, recommended=int(old)), old, bool(at)
    if len(at) != 1 or _DIGITS.fullmatch(old):  # too long to be a number here: left as it is
        return fields, None, False
    return dict(fields, recommended=at[0]), old, True
