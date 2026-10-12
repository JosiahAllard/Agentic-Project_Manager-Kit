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

The JSON ArtifactData writes under `out_dir` has not been seen: both a wrapped document (a `data`
object plus only WRAPPER_KEYS) and the bare fields are read, a `data` object beside any other key
is refused, and `id`/`version` never become fields (fields_of). Nothing here prints, parses
arguments or exits.
"""
import collections
import json
import os
import posixpath
import re
import tempfile

from kitlib import commit as kcommit
from kitlib import config as kconfig
from kitlib import hubvalidate
from kitlib import pmdata
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
# A wrapped out_dir document: a `data` object plus only these keys (fields_of).
WRAPPER_KEYS = frozenset(('data', 'id', 'version', 'doc_id', 'collection'))
BATCH_ENTRIES = 50                  # ArtifactData batch: at most 50 entries ...
BATCH_BYTES = 1024 * 1024           # ... and 1 MiB
BATCH_BUDGET = BATCH_BYTES - 64 * 1024   # margin: how the 1 MiB is counted has not been seen
PUSH_TIMEOUT = 120                  # seconds
_LABEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')

Change = collections.namedtuple('Change', 'op rel')          # op: added, changed, deleted
CommitPlan = collections.namedtuple('CommitPlan', 'root rel branch model remote env')


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
    """The document's own fields from one ArtifactData out_dir file or one mirror file. An object
    whose `data` is an object and whose other keys are all in WRAPPER_KEYS (none at all included)
    is a wrapped document and gives its `data`. An object with a `data` object beside any other
    key is refused: it is either a wrapper with keys this module does not know or a document
    with a `data` field, and mirroring the wrong one would restore nested garbage (no hub
    collection has a `data` field, docs/action-hub.md). Otherwise the object is the fields.
    `id` and `version` are never kept: a document's id is its file name (the page's to_pm writes
    store no `id` field either)."""
    if not isinstance(obj, dict):
        raise Refused('a %s document is not a JSON object' % collection)
    if isinstance(obj.get('data'), dict):
        extra = sorted(set(obj) - WRAPPER_KEYS)
        if extra:
            raise Refused(
                'a %s document has a `data` object beside %s: cannot tell a wrapped ArtifactData '
                'document from one with a `data` field, so nothing was saved. If these are '
                "ArtifactData's own keys, add them to kitlib.hubmirror.WRAPPER_KEYS"
                % (collection, ', '.join(extra)))
        out = dict(obj['data'])
    else:
        out = dict(obj)
    out.pop('version', None)
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
    name the other folders, which are not mirrored, then any VALIDATE lines (validation_notes;
    they never stop a save). Refused for a missing src or a file that is
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
    notes.extend(validation_notes(found, read_repos(src)))
    return found, notes


def read_repos(src):
    """{doc_id: fields} of the repos/ folder in an out_dir (read only to judge repo tags, never
    mirrored); None when src has no repos/ folder or one of its files cannot be read."""
    folder = os.path.join(src, 'repos')
    if not os.path.isdir(folder):
        return None
    try:
        return {k: fields_of('repos', _read_json(p)) for k, p in sorted(_doc_files(folder).items())}
    except Refused:
        return None


def validation_notes(found, repos=None):
    """Lines for kitlib.hubvalidate's findings on `found` (plus repos/ when given): a warning
    never stops a save. [] when the documents are clean."""
    docs = dict(found)
    if repos is not None:
        docs['repos'] = repos
    try:
        findings = hubvalidate.validate(docs)
    except Exception as e:      # a bug in a check must never abort a save
        return ['VALIDATE skipped: %s' % e]
    if not findings:
        return []
    lines = ['VALIDATE %s' % hubvalidate.format_line(f) for f in findings]
    lines.append('VALIDATE %s (saved anyway)' % hubvalidate.summary(findings))
    return lines


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


def _is_batch_file(path):
    """True when `path` is a file restore wrote: a JSON list of `set` entries with a file_path."""
    try:
        with open(path, encoding='utf-8') as f:
            entries = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(entries, list) and bool(entries) and all(
        isinstance(e, dict) and e.get('op') == 'set' and 'file_path' in e for e in entries)


def restore(mirror, out):
    """Write ArtifactData batch files that `set` every mirrored document in a fresh copy (no
    if_version: a new copy holds no documents). Each document's fields go to their own file in
    <out without extension>.docs/<collection>/<doc_id>.json, named by the entry's file_path.
    Batches hold at most BATCH_ENTRIES entries and BATCH_BUDGET bytes (entries plus document
    files). Returns [(batch path, entry count)]; an empty mirror writes nothing and returns [].
    Every document is sized before anything is written; numbered batch files an earlier, longer
    run left beyond the last one written are removed, so no stale batch is sent. An OSError
    writing is Refused."""
    docs = mirror_docs(mirror)
    if not docs:
        return []
    docs_dir = os.path.splitext(os.path.abspath(out))[0] + '.docs'
    batches, cur, size, bodies = [], [], 2, []
    for coll, doc_id, fields in docs:
        body = json.dumps(fields, ensure_ascii=False)
        path = os.path.join(docs_dir, coll, doc_id + '.json')
        entry = {'op': 'set', 'collection': coll, 'doc_id': doc_id,
                 'file_path': path.replace(os.sep, '/')}
        cost = len(json.dumps(entry).encode('utf-8')) + 2 + len(body.encode('utf-8'))
        if cost > BATCH_BUDGET:
            raise Refused('%s/%s is %d bytes, over one batch (%d); nothing was written'
                          % (coll, doc_id, cost, BATCH_BUDGET))
        if cur and (len(cur) >= BATCH_ENTRIES or size + cost > BATCH_BUDGET):
            batches.append(cur)
            cur, size = [], 2
        cur.append(entry)
        bodies.append((path, body))
        size += cost
    batches.append(cur)
    written = []
    try:
        for path, body in bodies:
            _write_text(path, body)
        for path, entries in zip(batch_paths(out, len(batches)), batches):
            _write_text(path, json.dumps(entries, indent=1) + '\n')
            written.append((path, len(entries)))
        n = len(batches) + 1
        while True:
            stale = batch_paths(out, n)[-1]
            if not _is_batch_file(stale):
                break
            os.remove(stale)
            n += 1
    except OSError as e:
        raise Refused('cannot write the restore files: %s' % e)
    return written


# ---- committing and pushing the mirror -------------------------------------------------------

def prepare_commit(mirror, model=None, environ=None, remote=None, env=None):
    """A CommitPlan for the mirror, or Refused: run before save writes anything. Needs the model
    for the Co-Authored-By trailer (model, else kitlib.commit.MODEL_ENV), the mirror inside a git
    work tree that is not a main checkout (kitlib.commit refuses one) and is on its `.kit.json`
    main branch (the data worktree's is the data branch), and refuses a work tree of the kit
    itself (_kit_code_error, _kit_checkout_error): the hub holds the owner's private to-dos and answers, and the
    kit's main is public. `remote` is where push goes (pm_data.remote); None: the data branch's
    recorded remote in the kit's repository, else the work tree's repo.remote. `env` is the git
    environment for status and push (tests); the commit itself runs in this process's."""
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
    _kit_code_error(root)       # first, whatever HEAD is: the reason that matters most
    branch, why = refs.current_branch(root)
    if branch is None:
        raise Refused('cannot read the branch of %s: %s' % (root, why))
    if not branch:
        raise Refused('HEAD is detached in %s' % root)
    data_remote = _kit_checkout_error(root, branch, remote)
    if branch != sets['main_branch']:
        raise Refused('%s is on %r, not its main branch %s' % (root, branch, sets['main_branch']))
    rel = os.path.relpath(os.path.abspath(mirror), root).replace('\\', '/')
    if rel == '..' or rel.startswith('../'):
        raise Refused('the hub mirror %s is outside %s' % (mirror, root))
    err = kcommit.main_checkout_error(False, root, sets['main_branch'], sets['rule_pointer'])
    if err:
        raise Refused('the hub mirror cannot be committed where it is: %s' % err)
    try:
        kcommit.coauthor(name, sets['coauthor_families'])
    except ValueError as e:
        raise Refused(str(e))
    return CommitPlan(root, rel, branch, name, remote or data_remote or cfg.remote, env)


def _kit_code_error(root):
    """Refused when the work tree `root` holds the kit's code (any clone), on any HEAD state."""
    if os.path.isfile(os.path.join(root, 'kitlib', 'hubmirror', '__init__.py')):
        raise Refused('the hub mirror is in %s, a checkout of the kit itself: the hub holds the '
                      "owner's private to-dos and answers and the kit is public; mirror into the "
                      'PM data worktree (pm_data) or a private repository' % root)


def _kit_checkout_error(root, branch, remote):
    """Refused when the work tree `root` is a work tree of the repository this kit runs from that
    is not on the data branch recorded in its kit-pm-data.json (docs/pm-data-branch.md "Layout"),
    or one pushing to another remote than the recorded one (a checkout holding the kit's code is
    _kit_code_error's). Returns the recorded remote for the data branch, else None."""
    same, why = repo.same_repository(root, kcommit.KIT_ROOT)
    if same is None:
        kit_common, _ = repo.common_dir(kcommit.KIT_ROOT)
        if kit_common is not None:      # the kit is in git; the mirror's repository could not be told
            raise Refused('cannot tell whether %s is the kit\'s repository: %s' % (root, why))
        return None
    if not same:
        return None
    try:
        rec = pmdata.locate(root)
    except pmdata.PmDataError as e:
        raise Refused(str(e))
    if rec is None or branch != rec.branch:
        raise Refused('the hub mirror is in %s, a work tree of the kit\'s repository on %r, not its '
                      'data branch%s: committing would put the owner\'s private hub data in the '
                      "kit's history" % (root, branch, ' %s' % rec.branch if rec else
                                         ' (no kit-pm-data.json: run pm_data.py init)'))
    if remote is not None and remote != rec.remote:
        raise Refused('the data branch %s pushes to %s (kit-pm-data.json), not %s'
                      % (rec.branch, rec.remote, remote))
    return rec.remote


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
    """Commit this save's changed mirror paths that differ from HEAD through kitlib.commit (one
    path each, named explicitly; its main-checkout refusal, staged machine-path scan, refusal of
    anything else already staged, and trailer). Mirror paths an earlier save left uncommitted are
    not committed; a line counts them. Returns lines; [] when nothing of this save differs."""
    pending = {p.lower(): p for p in _status_paths(plan)}
    mine = [posixpath.normpath(posixpath.join(plan.rel, c.rel)) for c in changes]
    paths = sorted(p for p in mine if p.lower() in pending)
    left = sorted(set(pending) - {p.lower() for p in paths})
    lines = (['%d mirror path(s) an earlier save left uncommitted are not in this commit'
              % len(left)] if left else [])
    if not paths:
        return lines
    subject = 'hub mirror: save %d document change(s)' % len(paths)
    out = kcommit.commit(kcommit.request(plan.root, subject, paths, model=plan.model,
                                         cwd=plan.root))
    if out.code != kcommit.OK:
        raise Refused('%s\nthe mirror is written but NOT committed.' % '\n'.join(out.lines))
    return out.lines + lines


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
    is replaced whole. Returns (config path, the URL it replaced or None, notes): a note when this
    is hub_urls' first entry while pm_start.hub_url holds another URL, which hub() stops reading
    then (it is not moved: which account published it is not known here). It writes what
    kitlib.config.user.writable_file names and refuses what that refuses (ConfigError), and never
    rewrites a file that does not check (user_config). Refused for a bad label or URL, a pm_start
    or hub_urls that is not an object, or a write that fails (the file is left as it was)."""
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
    notes = []
    old = pm.get('hub_url')
    if not urls and isinstance(old, str) and old.strip() and old != url:
        notes.append('pm_start.hub_url %s is no longer read now that hub_urls has an entry: the '
                     'other accounts that used it get the publish line at session start; if that '
                     'copy is theirs, record it: set-url --account <its label> %s' % (old, old))
    was = urls.get(label)
    urls[label] = url
    folder = os.path.dirname(path)
    tmp = None
    try:
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=user.FILE_NAME + '.', suffix='.tmp', dir=folder)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(data, indent=2) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    except OSError as e:
        raise Refused('cannot write %s: %s; it is unchanged' % (path, e))
    finally:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path, was, notes
