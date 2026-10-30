"""kitlib.hubvalidate: checks on the Action Hub's data (docs/action-hub.md field tables).

Pure logic over the hub's JSON shape, `docs = {collection: {doc_id: fields}}` (what
kitlib.hubsite.storecheck.check takes); it knows nothing of the page, ArtifactData or any
transport.
Nothing here prints, parses arguments or exits.

  validate(docs, only=None, known_repos=())
                              [Finding] for every document; `only` (a set of "collection/doc_id")
                              limits the findings to those documents while refs and repos are
                              still judged against all of docs; `known_repos` are extra repo
                              names (the configured ones) a repo tag may use, or a callable
                              giving them, called only when a checked document has a repo tag
  batch_docs(entries, base)   (docs, findings) from an ArtifactData batch (a list of entries with
                              op, collection, doc_id and a data object or a file_path)
  summary(findings)           "N error(s), M warning(s)"

A Finding is (level, rel, field, code, message). level is `error` (the page would drop or hide
the data), `warn` (the data is intact but wrong or unreachable) or `note` (a check was skipped).
Codes: commands-type, status-unknown, status-missing, ref-dangling, options-shape, repo-alias,
handstep-commands, plugin-path, pages-schema, not-object, unreadable.

plugin-path warns of a to-do (not done) whose command names a kit plugin export folder
(PLUGIN_EXPORT); the store hands that warning back to whoever adds or edits the to-do.
"""
import collections
import json
import os
import re

Finding = collections.namedtuple('Finding', 'level rel field code message')

ERROR, WARN, NOTE = 'error', 'warn', 'note'

# status values per collection (docs/action-hub.md: questions, stories, todos, to_pm, finding_marks,
# skill_edits; approvals: kitlib.hubsite.approvals)
STATUSES = {
    'questions': ('open', 'answered', 'logged', 'done', 'filed'),
    'stories': ('submitted', 'review', 'questions', 'added', 'declined', 'withdrawn'),
    'todos': ('open', 'done'),
    'to_pm': ('queued', 'sent', 'done'),
    'approvals': ('pending', 'approved', 'denied', 'consumed', 'expired'),
    'finding_marks': ('dismissed', 'passed_on', 'done'),
    'skill_edits': ('draft', 'submitted', 'committed', 'superseded', 'failed', 'withdrawn'),
}
# the collection a Q-/M-/T-/P-/S-/A-/E-/L-/C- id names (the store's id prefixes), and the fields
# that carry such ids (an A-, E-, L- or C- id is never a ref; E is a skill edit, L a landing feed
# event, C a session's hand-off record, docs/action-hub.md)
REF_COLLECTIONS = {'Q': 'questions', 'M': 'inbox', 'T': 'todos', 'P': 'to_pm', 'S': 'stories',
                   'A': 'approvals', 'E': 'skill_edits', 'L': 'landings', 'C': 'handoffs'}
REF_FIELDS = {'questions': ('refs',), 'inbox': ('ref', 'refs'), 'parked': ('q',),
              'todos': ('refs',), 'to_pm': ('refs',)}
REPO_COLLECTIONS = ('questions', 'inbox', 'todos', 'to_pm', 'stories', 'parked', 'landings')
ANY_REPO = ('all', 'pm', '')        # a repo tag that needs no repos/ entry
_REF = re.compile(r'(?<![A-Za-z0-9])([QMTPS])-\d+(?![A-Za-z0-9])', re.I)
# a kit plugin export folder (kitlib.pluginsource: <export_dir>/<sha12>, the default export_dir
# %LOCALAPPDATA%\pm-kit\plugin): a plugin refresh replaces it, so a to-do command naming it goes
# stale (plugin-path)
PLUGIN_EXPORT = re.compile(r'(?i)[^\s"\']*?[\\/]plugin[\\/]+[0-9a-f]{12}(?![0-9a-z])')


def _f(level, coll, doc_id, field, code, message):
    return Finding(level, '%s/%s' % (coll, doc_id), field, code, message)


def _text(v):
    return isinstance(v, str) and bool(v.strip())


def _check_commands(coll, doc_id, d, out):
    if 'commands' not in d:
        return
    c = d['commands']
    if not (isinstance(c, list) and all(_text(x) for x in c)):
        out.append(_f(ERROR, coll, doc_id, 'commands', 'commands-type',
                      'commands must be a list of non-empty strings, not %s' % type(c).__name__))
        return
    if d.get('status') == 'done':
        return
    for i, cmd in enumerate(c):
        m = PLUGIN_EXPORT.search(cmd)
        if m:
            out.append(_f(WARN, coll, doc_id, 'commands', 'plugin-path',
                          'commands[%d] names the kit plugin export folder %s, which a plugin refresh '
                          'replaces, so the command goes stale. Instead: name the tool in the kit main '
                          'checkout (python <kit main checkout>/tools/<tool>.py)' % (i, m.group(0))))
            return


def _check_status(coll, doc_id, d, out):
    allowed = STATUSES.get(coll)
    if allowed is None:
        return
    s = d.get('status')
    if s is None:
        out.append(_f(WARN, coll, doc_id, 'status', 'status-missing',
                      'no status: the page falls back to a default'))
    elif s not in allowed:
        out.append(_f(ERROR, coll, doc_id, 'status', 'status-unknown',
                      'status %r is not one of %s' % (s, ', '.join(allowed))))


def _check_options(coll, doc_id, d, out):
    if coll != 'questions':
        return
    opts = d.get('options')
    if opts is not None:
        if not (isinstance(opts, list) and all(
                isinstance(o, dict) and _text(o.get('label'))
                and isinstance(o.get('description', ''), str) for o in opts)):
            out.append(_f(ERROR, coll, doc_id, 'options', 'options-shape',
                          'options must be a list of {label, description} objects'))
            opts = None
    if 'recommended' in d and d['recommended'] is not None:
        r = d['recommended']
        n = len(opts) if isinstance(opts, list) else 0
        if isinstance(r, bool) or not isinstance(r, int) or not 0 <= r < n:
            out.append(_f(ERROR, coll, doc_id, 'recommended', 'options-shape',
                          'recommended must be an index into options (%d option(s)), not %r'
                          % (n, r)))
    if 'refs' in d and not (isinstance(d['refs'], list) and all(_text(x) for x in d['refs'])):
        out.append(_f(WARN, coll, doc_id, 'refs', 'options-shape',
                      'refs must be a list of ids'))


def _ref_tokens(v):
    if isinstance(v, str):
        return [m.group(0).upper() for m in _REF.finditer(v)]
    if isinstance(v, list):
        return [t for x in v for t in _ref_tokens(x)]
    return []


def _check_refs(coll, doc_id, d, docs, out):
    for field in REF_FIELDS.get(coll, ()):
        for tok in _ref_tokens(d.get(field)):
            target = REF_COLLECTIONS[tok[0]]
            if target not in docs:
                continue                    # that collection was not read: cannot judge
            if tok not in docs[target]:
                out.append(_f(WARN, coll, doc_id, field, 'ref-dangling',
                              '%s names %s, which is not in %s/' % (field, tok, target)))


def _check_handstep(coll, doc_id, d, out):
    if coll != 'todos' or d.get('status') != 'open' or d.get('from') == 'you':
        return
    if 'commands' in d and not isinstance(d['commands'], list):
        return                              # commands-type already says it
    if not d.get('commands'):
        out.append(_f(WARN, coll, doc_id, 'commands', 'handstep-commands',
                      'an open to-do from the PM has no commands'))


def _known_repos(repos):
    known = set()
    for doc_id, d in repos.items():
        known.add(str(doc_id).lower())
        if _text(d.get('name')):
            known.add(d['name'].lower())
        a = d.get('aliases')
        if isinstance(a, list):
            known.update(x.lower() for x in a if isinstance(x, str))
    return known


def _check_pages(docs, out):
    pages, html = docs.get('pages'), docs.get('page_html')
    for doc_id, d in sorted((pages or {}).items()):
        if not isinstance(d, dict):
            continue                        # validate reports it as not-object
        if not _text(d.get('title')):
            out.append(_f(WARN, 'pages', doc_id, 'title', 'pages-schema',
                          'a page needs a non-empty string title'))
    for doc_id, d in sorted((html or {}).items()):
        if not isinstance(d, dict):
            continue
        if not isinstance(d.get('html'), str):
            out.append(_f(WARN, 'page_html', doc_id, 'html', 'pages-schema',
                          'page_html needs a string html field'))
        if pages is not None and doc_id not in pages:
            out.append(_f(WARN, 'page_html', doc_id, 'id', 'pages-schema',
                          'no pages/%s for this page_html' % doc_id))


def validate(docs, only=None, known_repos=()):
    """The Findings for docs, sorted by document then code. See the module docstring.
    `known_repos`: further repo names (the PM's configured repositories, which the hub's repo
    menu lists beside the stored repos/ docs) that a repo tag may use without an alias, or a
    callable() giving them, called only when a document checked carries a repo tag. With `only`,
    repo tags are checked on those documents alone, so the repo-alias note is about them too."""
    out = []
    for coll in sorted(docs):
        for doc_id, d in sorted(docs[coll].items()):
            if not isinstance(d, dict):
                out.append(_f(ERROR, coll, doc_id, '', 'not-object', 'the document is not an object'))
                continue
            if coll == 'todos':
                _check_commands(coll, doc_id, d, out)
                _check_handstep(coll, doc_id, d, out)
            _check_status(coll, doc_id, d, out)
            _check_options(coll, doc_id, d, out)
            _check_refs(coll, doc_id, d, docs, out)
    _check_pages(docs, out)
    out.extend(_check_repos(docs, known_repos, only))
    if only is not None:
        out = [f for f in out if f.rel in only or f.level == NOTE]
    return sorted(out, key=lambda f: (f.rel, f.code, f.field))


def _check_repos(docs, known_repos=(), only=None):
    tags = []
    for coll in REPO_COLLECTIONS:
        for doc_id, d in sorted(docs.get(coll, {}).items()):
            if only is not None and '%s/%s' % (coll, doc_id) not in only:
                continue                    # its finding would be dropped: never look it up
            if isinstance(d, dict) and isinstance(d.get('repo'), str) \
                    and d['repo'].strip().lower() not in ANY_REPO:
                tags.append((coll, doc_id, d['repo']))
    if not tags:
        return []
    if callable(known_repos):               # asked only now that a repo tag is checked
        known_repos = known_repos()
    if 'repos' not in docs and not known_repos:
        return [Finding(NOTE, '', 'repo', 'repo-alias',
                        'repo aliases not checked: there is no repos/ collection to check against')]
    known = _known_repos({k: v for k, v in docs.get('repos', {}).items() if isinstance(v, dict)})
    known.update(str(x).lower() for x in known_repos)
    return [_f(WARN, coll, doc_id, 'repo', 'repo-alias',
               'repo %r is neither a repos/ name nor one of its aliases: it shows only under All'
               % tag) for coll, doc_id, tag in tags if tag.strip().lower() not in known]


def summary(findings):
    e = sum(1 for f in findings if f.level == ERROR)
    w = sum(1 for f in findings if f.level == WARN)
    return '%d error(s), %d warning(s)' % (e, w)


def format_line(f):
    where = f.rel + (' ' + f.field if f.field else '') if f.rel else f.field
    return '%s %s: %s [%s]' % (f.level.upper(), where, f.message, f.code)


def batch_docs(entries, base):
    """(docs, findings) from an ArtifactData batch: the `set` entries with a collection and a
    doc_id, their fields from `data` or from `file_path` (relative to `base`). Other ops are
    skipped: an `update` holds part of a document, which cannot be judged alone. `docs` holds
    only the batch's own documents; an unreadable file is an `unreadable` error."""
    docs, out = {}, []
    if not isinstance(entries, list):
        return docs, [Finding(ERROR, '', '', 'not-object', 'a batch is a JSON list of entries')]
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or e.get('op') != 'set' or not e.get('collection') \
                or not e.get('doc_id'):
            continue
        coll, doc_id = e['collection'], str(e['doc_id'])
        fields = e.get('data')
        if fields is None and e.get('file_path'):
            path = e['file_path']
            if not os.path.isabs(path):
                path = os.path.join(base, path)
            try:
                with open(path, encoding='utf-8-sig') as f:
                    fields = json.load(f)
            except (OSError, ValueError) as err:
                out.append(_f(ERROR, coll, doc_id, 'file_path', 'unreadable',
                              'cannot read %s: %s' % (e['file_path'], err)))
                continue
        if isinstance(fields, dict) and isinstance(fields.get('data'), dict) \
                and set(fields) <= {'data', 'id', 'version'}:
            fields = fields['data']
        docs.setdefault(coll, {})[doc_id] = fields
    return docs, out
