"""kitlib.hubsite.storecheck: the findings of the hub store folder as it is on disk
(hub.py validate).

  validate(root)                         -> [Finding] the store's findings, read only, no lock
  check(docs)                            -> [Finding] the same over {collection: {id: fields}}
  store_docs(root)                       -> {collection: {id: document}} read from the folder
  validate_lines(findings)               -> [str]     the findings, one line each, and a summary

Read only: no lock is taken and nothing is made, so it may run beside a server.
Nothing here prints, parses arguments or exits.
"""
import json
import os

from kitlib import hubvalidate
from kitlib.config.types import ConfigError

from . import schema


class StoreReadError(Exception):
    """The store folder could not be read, said in one message."""


def _doc_files(folder):
    """{id: path} of the *.json files directly in folder."""
    out = {}
    for n in sorted(os.listdir(folder)):
        path = os.path.join(folder, n)
        if n.lower().endswith('.json') and len(n) > 5 and os.path.isfile(path):
            out[n[:-5]] = path
    return out


def _read_json(path):
    with open(path, encoding='utf-8-sig') as f:
        return json.load(f)


def store_docs(root):
    """{collection: {id: document}} of the store folder `root`, every schema.COLLECTIONS key
    present, read from its files with no lock ({} each for a missing folder). StoreReadError for
    a document that cannot be read or is not an object."""
    out = {}
    for coll in schema.COLLECTIONS:
        folder = os.path.join(root, coll)
        docs = {}
        try:
            files = _doc_files(folder) if os.path.isdir(folder) else {}
            for doc_id, path in files.items():
                docs[doc_id] = _read_json(path)
                if not isinstance(docs[doc_id], dict):
                    raise ValueError('not a JSON object')
        except (OSError, ValueError) as e:
            raise StoreReadError('cannot read the hub store %s: %s/: %s' % (root, coll, e))
        out[coll] = docs
    return out


def check(docs, known_repos=()):
    """The findings for docs ({collection: {id: document}}): schema.check's errors for each
    document, then kitlib.hubvalidate.validate's (given `known_repos`, the configured repo names
    or a callable giving them), sorted by document, code and field. With no repos/ document and
    no known repos there is nothing to judge a `repo` tag against: `repos` is then left out, so
    validate gives its one note that the tags were not checked, not a warning per tagged
    document. A question whose `recommended` is text that stands for a number is judged as the
    store reads it (schema.whole_recommended), with that number, and has one warning that its file
    still holds the text: the api accepts its updates, and the first one stores the number."""
    out = []
    read = {coll: {} for coll in schema.COLLECTIONS if coll in docs}
    for coll in schema.COLLECTIONS:
        for doc_id, d in sorted(docs.get(coll, {}).items()):
            d, old, _ = (schema.whole_recommended(coll, d) if isinstance(d, dict)
                         else (d, None, False))
            if old is not None:
                out.append(hubvalidate.Finding(
                    hubvalidate.WARN, '%s/%s' % (coll, doc_id), 'recommended', schema.CODE,
                    'recommended is the text %r in its file; the store reads it as the number %d '
                    'and its next write stores the number' % (old, d['recommended'])))
            read[coll][doc_id] = d
            out.extend(schema.check(coll, d, doc_id))
    docs = dict(docs, **read)
    if not docs.get('repos'):
        docs = {k: v for k, v in docs.items() if k != 'repos'}
    out.extend(hubvalidate.validate(docs, known_repos=known_repos))
    return sorted(out, key=lambda f: (f.rel, f.code, f.field, f.message))


def machine_known_repos(home=None):
    """The callable the live server hands its store (serve.machine_sources(...).known_repos): the
    PM's configured repo names, asked only when a document has a repo tag. () when the machine
    config cannot be used or names no sessions file, as the server then has none either."""
    from . import serve                     # here: serve is not needed by the other verbs
    try:
        found = serve.machine_sources(home, sync=False).known_repos
    except ConfigError:
        return ()
    return found if found is not None else ()


def validate(root, known_repos=None, home=None):
    """check() over the store folder `root` as it is on disk. Read only: no lock is taken and
    nothing is made, so it may run beside a server. `known_repos` (names, or a callable giving
    them, as hubvalidate.validate takes) default to the machine's own, as the live server uses
    (machine_known_repos of `home`, the user's by default). StoreReadError for a folder that
    does not exist or a document that cannot be read."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise StoreReadError('the hub store %s is not a folder' % root)
    if known_repos is None:
        known_repos = machine_known_repos(home)
    return check(store_docs(root), known_repos)


def validate_lines(findings):
    """The findings as lines, kitlib.hubvalidate.format_line each, then one summary line; one
    line saying the store is clean when there is none. After the findings, one line per document
    that has a schema.check error: the store refuses a write that leaves such an error in the
    document (store.Store, "check"), so the api cannot change it until its file is fixed."""
    if not findings:
        return ['VALIDATE clean: no finding']
    lines = ['VALIDATE %s' % hubvalidate.format_line(f) for f in findings]
    for rel in sorted({f.rel for f in findings
                       if f.code == schema.CODE and f.level == hubvalidate.ERROR}):
        lines.append('VALIDATE %s: the api will refuse every update of this document until it is '
                     'fixed; fix its file by hand with the server stopped (no api route repairs '
                     'it)' % rel)
    return lines + ['VALIDATE %s' % hubvalidate.summary(findings)]
