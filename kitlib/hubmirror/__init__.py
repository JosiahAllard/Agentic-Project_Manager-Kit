"""kitlib.hubmirror: the Action Hub's data of record, kept in git (docs/action-hub.md "One copy
per account").

A published artifact belongs to one Claude account, so each account the PM runs on has its own
copy of the hub page (pm_start.hub_urls). What the owner and the PM write on the hub is mirrored as
files, one per document, so a new copy on another account is filled from the mirror and nothing is
lost by switching accounts:

    <mirror>/<collection>/<doc_id>.json    the document's fields, sorted keys, 2-space indent

The mirror is the folder MIRROR_FOLDER in the PM data worktree (pm_data.path), else
pm_start.hub_mirror (an absolute folder) for an install without a data branch.

  save     ArtifactData `out_dir` files -> the mirror (added, changed, deleted per collection)
  restore  the mirror -> ArtifactData batch files of `set` entries for a fresh copy
  set_url  pm_start.hub_urls[<account>] in the machine config

The JSON ArtifactData writes under `out_dir` has not been seen: both a wrapped document (an
object with `data` plus `id` and/or `version`) and the bare fields are read, and `id`/`version`
never become fields (fields_of). Nothing here prints, parses arguments or exits.
"""
import collections
import json
import os
import re
import tempfile

from kitlib import commit as kcommit
from kitlib import config as kconfig
from kitlib.config import user
from kitlib.config.types import parse_json
from kitlib.git import cmd, refs, repo

MIRROR_FOLDER = 'hub'
# The collections the owner, the page or the PM write and no tool regenerates: mirrored.
MIRRORED = ('questions', 'inbox', 'to_pm', 'todos', 'parked', 'stories', 'pages', 'page_html')
# The collections a tool or a repost fills: never mirrored; refilled on a new copy by these.
REGENERATED = (
    ('backlog', 'claude/hub/hub_backlog.py --force'),
    ('completed', 'claude/hub/hub_backlog.py --force'),
    ('repos', 'claude/hub/hub_repos.py --have <names> --out <file>, then any aliases again'),
    ('usage', 'launch.py usage, posted as at session start'),
    ('agents', 'repost each agent definition'),
)
# A wrapped out_dir document: `data` plus only these keys, at least one of id and version.
WRAPPER_KEYS = frozenset(('data', 'id', 'version', 'doc_id', 'collection'))
# Collections whose documents carry a real `id` field (the page writes it): kept in fields.
KEEPS_ID = frozenset(('to_pm',))
BATCH_ENTRIES = 50                  # ArtifactData batch: at most 50 entries ...
BATCH_BYTES = 1024 * 1024           # ... and 1 MiB
BATCH_BUDGET = BATCH_BYTES - 64 * 1024   # margin: how the 1 MiB is counted has not been seen
PUSH_TIMEOUT = 120                  # seconds
_LABEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')

Change = collections.namedtuple('Change', 'op rel')          # op: added, changed, deleted
CommitPlan = collections.namedtuple('CommitPlan', 'root rel branch trailer remote env')


class Refused(Exception):
    """A refusal, said in one message; nothing was written unless the message says so."""


def regenerate_text():
    """The regenerated collections and what refills each, as one phrase."""
    return '; '.join('%s/: %s' % (c, how) for c, how in REGENERATED)


def mirror_dir(pm_data, pm_start):
    """The mirror folder: MIRROR_FOLDER in pm_data["path"] when the machine config sets pm_data,
    else pm_start["hub_mirror"] (absolute). Refused when neither is set or hub_mirror is not an
    absolute path."""
    if pm_data and pm_data.get('path'):
        return os.path.join(pm_data['path'], MIRROR_FOLDER)
    folder = (pm_start or {}).get('hub_mirror')
    if folder is None:
        raise Refused('no hub mirror: set pm_data (docs/pm-data-branch.md) or pm_start.hub_mirror '
                      '(an absolute folder) in the machine config')
    if not isinstance(folder, str) or not os.path.isabs(folder):
        raise Refused('pm_start.hub_mirror must be an absolute folder, not %r' % (folder,))
    return folder


def fields_of(collection, obj):
    """The document's own fields from one ArtifactData out_dir file or one mirror file. A wrapped
    document (`data` an object, every other key in WRAPPER_KEYS, `id` or `version` among them)
    gives its `data`. Otherwise the object is the fields; `version` is dropped, and `id` too
    except in KEEPS_ID collections, whose documents hold a real `id`."""
    if not isinstance(obj, dict):
        raise Refused('a %s document is not a JSON object' % collection)
    keys = set(obj)
    if isinstance(obj.get('data'), dict) and keys <= WRAPPER_KEYS and keys & {'id', 'version'}:
        out = dict(obj['data'])
    else:
        out = dict(obj)
    out.pop('version', None)
    if collection not in KEEPS_ID:
        out.pop('id', None)
    return out


def normal_text(fields):
    """A document's mirror file text: sorted keys, 2-space indent, UTF-8 kept, one final newline."""
    return json.dumps(fields, indent=2, sort_keys=True, ensure_ascii=False) + '\n'


def _read_json(path):
    try:
        with open(path, encoding='utf-8-sig') as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise Refused('cannot read %s: %s' % (path, e))


def _doc_files(folder):
    """{doc_id: path} of the *.json files directly in folder ({} when it does not exist)."""
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise Refused('cannot list %s: %s' % (folder, e))
    return {n[:-5]: os.path.join(folder, n) for n in names
            if n.lower().endswith('.json') and len(n) > 5 and os.path.isfile(os.path.join(folder, n))}


def read_source(src):
    """({collection: {doc_id: fields}}, notes) from an ArtifactData out_dir folder: one entry per
    MIRRORED collection that has a folder in src (an empty folder is an empty collection); notes
    name the other folders, which are not mirrored. Refused for a missing src or a file that is
    not a JSON object; nothing is read into the mirror then."""
    if not os.path.isdir(src):
        raise Refused('--from %s is not a folder' % src)
    found, notes = {}, []
    for name in sorted(os.listdir(src)):
        if not os.path.isdir(os.path.join(src, name)):
            continue
        if name not in MIRRORED:
            notes.append('%s/: not mirrored, left alone' % name)
            continue
        docs = {}
        for doc_id, path in sorted(_doc_files(os.path.join(src, name)).items()):
            docs[doc_id] = fields_of(name, _read_json(path))
        found[name] = docs
    return found, notes


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


def _read_text(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _remove(path):
    try:
        os.remove(path)
    except OSError as e:
        raise Refused('cannot delete %s: %s' % (path, e))


def save(found, mirror):
    """Bring each collection in `found` (read_source) into the mirror: a new document is added, a
    different one rewritten, and a mirror document missing from its collection in `found` deleted.
    A collection absent from `found` is left alone. Returns the Changes, sorted by path."""
    changes = []
    for coll in sorted(found):
        folder = os.path.join(mirror, coll)
        # file names compare without case (Windows): a doc id whose case changed is one file
        have = {k.lower(): (k, p) for k, p in _doc_files(folder).items()}
        keep = set()
        for doc_id, fields in sorted(found[coll].items()):
            path = os.path.join(folder, doc_id + '.json')
            text = normal_text(fields)
            old = have.get(doc_id.lower())
            keep.add(doc_id.lower())
            if old is None:
                _write_text(path, text)
                changes.append(Change('added', '%s/%s.json' % (coll, doc_id)))
            elif old[0] != doc_id or _read_text(old[1]) != text:
                if old[0] != doc_id:
                    _remove(old[1])
                _write_text(path, text)
                changes.append(Change('changed', '%s/%s.json' % (coll, doc_id)))
        for key in sorted(set(have) - keep):
            doc_id, path = have[key]
            _remove(path)
            changes.append(Change('deleted', '%s/%s.json' % (coll, doc_id)))
    return sorted(changes, key=lambda c: c.rel)


def mirror_docs(mirror):
    """[(collection, doc_id, fields)] of every mirrored document, sorted; [] for a missing mirror."""
    out = []
    for coll in MIRRORED:
        for doc_id, path in sorted(_doc_files(os.path.join(mirror, coll)).items()):
            out.append((coll, doc_id, fields_of(coll, _read_json(path))))
    return out


def batch_paths(out, n):
    """The n batch files restore writes: `out` first, then <stem>-2<ext>, <stem>-3<ext>, ..."""
    stem, ext = os.path.splitext(os.path.abspath(out))
    return [os.path.abspath(out)] + ['%s-%d%s' % (stem, i, ext or '.json') for i in range(2, n + 1)]


def restore(mirror, out):
    """Write ArtifactData batch files that `set` every mirrored document in a fresh copy (no
    if_version: a new copy holds no documents). Each document's fields go to their own file in
    <out without extension>.docs/<collection>/<doc_id>.json, named by the entry's file_path.
    Batches hold at most BATCH_ENTRIES entries and BATCH_BUDGET bytes (entries plus document
    files). Returns [(batch path, entry count)]; an empty mirror writes nothing and returns []."""
    docs = mirror_docs(mirror)
    if not docs:
        return []
    docs_dir = os.path.splitext(os.path.abspath(out))[0] + '.docs'
    batches, cur, size = [], [], 2
    for coll, doc_id, fields in docs:
        body = json.dumps(fields, ensure_ascii=False)
        path = os.path.join(docs_dir, coll, doc_id + '.json')
        _write_text(path, body)
        entry = {'op': 'set', 'collection': coll, 'doc_id': doc_id,
                 'file_path': path.replace(os.sep, '/')}
        cost = len(json.dumps(entry).encode('utf-8')) + 2 + len(body.encode('utf-8'))
        if cost > BATCH_BUDGET:
            raise Refused('%s/%s is %d bytes, over one batch (%d)' % (coll, doc_id, cost, BATCH_BUDGET))
        if cur and (len(cur) >= BATCH_ENTRIES or size + cost > BATCH_BUDGET):
            batches.append(cur)
            cur, size = [], 2
        cur.append(entry)
        size += cost
    batches.append(cur)
    written = []
    for path, entries in zip(batch_paths(out, len(batches)), batches):
        _write_text(path, json.dumps(entries, indent=1) + '\n')
        written.append((path, len(entries)))
    return written


# ---- committing and pushing the mirror -------------------------------------------------------

def prepare_commit(mirror, model=None, environ=None, remote=None, env=None):
    """A CommitPlan for the mirror, or Refused: run before save writes anything. Needs the model
    for the Co-Authored-By trailer (model, else kitlib.commit.MODEL_ENV), the mirror inside a git
    work tree, and that work tree on its `.kit.json` main branch (the data worktree's is the data
    branch). `remote` is where push goes (pm_data.remote); None: the work tree's repo.remote."""
    name = model if model is not None else (os.environ if environ is None else environ).get(
        kcommit.MODEL_ENV, '')
    if not (name or '').strip():
        raise Refused('--commit needs the model for the Co-Authored-By trailer: --model, else %s'
                      % kcommit.MODEL_ENV)
    folder = os.path.abspath(mirror)
    while not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            raise Refused('no folder above %s exists' % mirror)
        folder = parent
    root, why = repo.toplevel(folder)
    if root is None:
        raise Refused('the hub mirror %s is in no git work tree: %s' % (mirror, why))
    try:
        cfg = kconfig.load(root)
        sets = kcommit.settings(cfg)
    except kconfig.ConfigError as e:
        raise Refused("the mirror repository's .kit.json: %s" % e)
    branch, why = refs.current_branch(root)
    if branch is None:
        raise Refused('cannot read the branch of %s: %s' % (root, why))
    if not branch:
        raise Refused('HEAD is detached in %s' % root)
    if branch != sets['main_branch']:
        raise Refused('%s is on %r, not its main branch %s' % (root, branch, sets['main_branch']))
    rel = os.path.relpath(os.path.abspath(mirror), root).replace('\\', '/')
    if rel == '..' or rel.startswith('../'):
        raise Refused('the hub mirror %s is outside %s' % (mirror, root))
    try:
        trailer = kcommit.coauthor(name, sets['coauthor_families'])
    except ValueError as e:
        raise Refused(str(e))
    return CommitPlan(root, rel, branch, trailer, remote or cfg.remote, env)


def _status_paths(plan):
    """Every path under the mirror that differs from HEAD (untracked files one by one)."""
    out, why = cmd.run(['status', '--porcelain', '-z', '--untracked-files=all', '--', plan.rel],
                       plan.root, env=plan.env)
    if out is None:
        raise Refused('git status failed: %s' % why)
    fields = cmd.text(out).split('\0')
    paths, i = [], 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if len(f) < 4:
            continue
        paths.append(f[3:])
        if f[0] in 'RC':        # a staged rename or copy: its source follows
            paths.append(fields[i])
            i += 1
    return sorted(set(paths))


def commit(plan, changes):
    """Commit every mirror path that differs from HEAD (this save's changes and any an earlier
    save left uncommitted), by explicit path: `git add -A -- <paths>` then `git commit -- <paths>`,
    so nothing else staged is swept in. Returns lines; [] when there is nothing to commit."""
    paths = _status_paths(plan)
    if not paths:
        return []
    _, why = cmd.run(['add', '-A', '--'] + paths, plan.root, env=plan.env)
    if why is None:
        subject = 'hub mirror: save %d document change(s)' % (len(changes) or len(paths))
        _, why = cmd.run(['commit', '-q', '-m', '%s\n\n%s\n' % (subject, plan.trailer), '--'] + paths,
                         plan.root, env=plan.env)
        if why is not None:
            cmd.run(['reset', '-q', '--'] + paths, plan.root, env=plan.env)
    if why is not None:
        raise Refused('git failed: %s\nthe mirror is written but NOT committed (left unstaged).' % why)
    sha, _ = cmd.run_text(['rev-parse', '--short', 'HEAD'], plan.root, env=plan.env)
    return ['commit %s (%d path(s))' % (sha, len(paths))]


def push(plan):
    """Push the mirror's branch to plan.remote with an explicit refspec
    (refs/heads/<b>:refs/heads/<b>); no git config is read for it or changed."""
    ref = 'refs/heads/%s' % plan.branch
    _, why = cmd.run(['push', '-q', plan.remote, '%s:%s' % (ref, ref)], plan.root,
                     timeout=PUSH_TIMEOUT, env=plan.env)
    if why is not None:
        raise Refused('git push failed: %s\nthe mirror is committed but NOT pushed.' % why)
    return ['pushed %s to %s' % (ref, plan.remote)]


# ---- the machine config's hub_urls ------------------------------------------------------------

def set_url(label, url, home=None):
    """Set pm_start.hub_urls[label] = url in the machine config, keeping every other key; the file
    is replaced whole. Returns (config path, the URL it replaced or None). It writes what
    kitlib.config.user.writable_file names and refuses what that refuses (ConfigError), and never
    rewrites a file that does not check (user_config). Refused for a bad label or URL, or a
    pm_start or hub_urls that is not an object."""
    if not isinstance(label, str) or not _LABEL.match(label):
        raise Refused('the account label %r must be letters, digits, - or _ (an account number or '
                      '"default")' % (label,))
    if not isinstance(url, str) or not url.startswith('https://') or len(url) <= len('https://'):
        raise Refused('the hub URL %r must be an https:// artifact link' % (url,))
    user.user_config(home)
    path = user.writable_file(home)
    try:
        with open(path, encoding='utf-8-sig') as f:
            data = parse_json(f.read(), path)
    except FileNotFoundError:
        data = {}
    pm = data.setdefault('pm_start', {})
    if not isinstance(pm, dict):
        raise Refused('pm_start in %s is not an object' % path)
    urls = pm.setdefault('hub_urls', {})
    if not isinstance(urls, dict):
        raise Refused('pm_start.hub_urls in %s is not an object' % path)
    was = urls.get(label)
    urls[label] = url
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=user.FILE_NAME + '.', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(data, indent=2) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path, was
