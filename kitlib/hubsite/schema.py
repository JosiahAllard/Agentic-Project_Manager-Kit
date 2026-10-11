"""kitlib.hubsite.schema: the fields each hub collection's documents must carry (docs/action-hub.md
field tables), checked by the store before every write.

  COLLECTIONS            the collections the hub store holds
  ID_PREFIX              {collection: prefix} of the collections whose ids the store assigns
  STORE_KEYS             the keys the store itself sets on every document (version, updated_at)
  check(coll, fields)    [kitlib.hubvalidate.Finding]: an `error` per missing required field, per
                         field of the wrong type, and per to_pm kind that is not known (an
                         unknown status is kitlib.hubvalidate's status-unknown error)
  check_id(doc_id)       an error message for a document id that cannot be a file name, else None

A field that is absent or null passes its type check unless it is required: the page writes many
fields as null until they are set (sent_at, q, reply). Unknown fields pass. The status values and
the ref prefixes stay in kitlib.hubvalidate (STATUSES, REF_COLLECTIONS), the single source.
Nothing here prints, parses arguments or exits.
"""
import re

from kitlib import hubvalidate

COLLECTIONS = ('questions', 'inbox', 'todos', 'to_pm', 'stories', 'pages', 'page_html', 'repos')
ID_PREFIX = {coll: prefix for prefix, coll in hubvalidate.REF_COLLECTIONS.items()}
STORE_KEYS = ('version', 'updated_at')
CODE = 'schema-field'

_STR, _LIST, _DICT, _BOOL, _INT = (str,), (list,), (dict,), (bool,), (int,)
# {collection: (required fields, {field: allowed types})}; a required field is also typed here
FIELDS = {
    'questions': (('status', 'title'), {
        'status': _STR, 'title': _STR, 'repo': _STR, 'from': _STR, 'asked_at': _STR,
        'context': _STR, 'options': _LIST, 'recommended': _INT, 'refs': _LIST, 'urgent': _BOOL,
        'answer': _DICT, 'ruling': _STR, 'story': _STR}),
    'inbox': (('text',), {
        'text': _STR, 'ref': _STR, 'asked': _STR, 'title': _STR, 'repo': _STR, 'from': _STR,
        'at': _STR, 'refs': _LIST, 'read': _BOOL, 'read_at': _STR}),
    'todos': (('status', 'title'), {
        'status': _STR, 'title': _STR, 'detail': _STR, 'commands': _LIST, 'repo': _STR,
        'from': _STR, 'added_at': _STR, 'refs': _LIST, 'done_at': _STR, 'urgent': _BOOL,
        'pm_ack': (bool, str)}),
    'to_pm': (('status', 'kind', 'text'), {
        'status': _STR, 'kind': _STR, 'text': _STR, 'repo': _STR, 'urgent': _BOOL,
        'created_at': _STR, 'sent_at': _STR, 'reply': _STR, 'ruling': _STR, 'done_at': _STR}),
    'stories': (('status', 'repo', 'title', 'story'), {
        'status': _STR, 'repo': _STR, 'title': _STR, 'story': _STR, 'urgent': _BOOL,
        'from': _STR, 'submitted_at': _STR, 'reviewer': _STR, 'review_note': _STR,
        'backlog_id': _STR, 'added_at': _STR, 'closed_note': _STR, 'closed_at': _STR}),
    'pages': (('title',), {'title': _STR}),
    'page_html': (('html',), {'html': _STR}),
    'repos': ((), {'name': _STR, 'added_at': _STR, 'aliases': _LIST}),
}
KINDS = {'to_pm': ('question', 'fyi')}

_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
# Windows device names: a file named so (any extension) opens the device, not a file
_DEVICES = frozenset(['con', 'prn', 'aux', 'nul'] + ['com%d' % i for i in range(1, 10)]
                     + ['lpt%d' % i for i in range(1, 10)])


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
