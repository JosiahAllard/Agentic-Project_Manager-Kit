"""Make (or re-link) the PM-data branch of a kit repository: `tools/pm_data.py init` and
`init --adopt` (docs/pm-data-branch.md "Making the branch").

`init(root, path, branch, remote, url, adopt, home)` acts on the MAIN checkout `root`:

- Every check runs before the first write, so a refusal changes nothing: the main checkout on a
  branch; a data branch name git takes (`git check-ref-format --branch`) that is one path
  component, not `pm` (work branches are `pm/<slug>`), not the main branch, not the prefix of
  any branch and no case twin of one (names compared case-folded: Windows refs are files); a
  private remote name that is not the public remote's (`.kit.json` "repo".remote); a public
  remote that has a url; private urls that reach, in no form (compare_url, with insteadOf and
  pushInsteadOf applied), the url or pushurl of any other remote; a worktree path outside the
  main checkout and every work tree, absent or empty; no data branch, no kit-pm-data.json, no
  machine-config `pm_data` and no data branch on the private remote yet (else --adopt; a remote
  that cannot be reached is not refused).
- --adopt takes an existing data branch with one root commit, which no main-branch history holds
  and which equals the recorded root when a record exists; its `.kit.json`, if it has one, must
  set "repo".main_branch to the data branch, "repo".remote to the private remote and a "backlog"
  store other than `ref` (the default is `ref`, so an unset store refuses too). A data worktree
  already at `path` is kept (and locked); one whose folder is gone is unlocked and removed from
  git's list, then added again; one at another existing folder refuses.
- The writes, in order: the root commit on the empty tree (`git commit-tree`; the tree is written
  by `git write-tree` of an empty index, the same id as the spec's literal) and `git branch`;
  kit-pm-data.json (before anything can be pushed, so the pre-push guard knows the root from the
  start); `git worktree add` and `git worktree lock`; `branch.<b>.remote`, `.pushRemote`, `.merge`
  and `push.default=simple` when unset; `git remote add` when a url was given; the machine
  config's `pm_data`; last the push to the private remote, with an explicit refspec.
- Nothing here edits `remote.<name>.fetch` or `remote.<name>.push` (docs/pm-data-branch.md
  "Never configure a refspec"); `git remote add` writes git's own default fetch line for the new
  remote only.

Exit codes: OK 0; REFUSED 1 (nothing changed); CONFIG 2 (a kit or machine configuration that
cannot be read); PUSH_FAILED 3 (all made locally, the push was tried and failed; the lines say
how to recover); PARTIAL 4 (stopped part-way: the lines name what was made, and `--adopt`
finishes it; a failure before anything was changed is REFUSED); INTERRUPTED 130 (Ctrl+C, with
the lines made so far).
"""
import collections
import json
import os
import re
import shutil
import tempfile

from kitlib import pmdata
from kitlib.config import ConfigError, load, load_at
from kitlib.config import user
from kitlib.config.types import parse_json
from kitlib.git import cmd
from kitlib.git import repo as gitrepo

OK, REFUSED, CONFIG, PUSH_FAILED, PARTIAL = 0, 1, 2, 3, 4

# The empty tree of each object format: git knows both without a stored object.
EMPTY_TREE = {'sha1': '4b825dc642cb6eb9a060e54bf8d69288fbee4904',
              'sha256': '6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321'}
ROOT_MESSAGE = 'PM data root'
LOCK_REASON = 'PM data'
RESERVED = ('pm',)          # work-branch areas a data branch name would shadow
PUSH_TIMEOUT = 300          # seconds
LS_REMOTE_TIMEOUT = 60      # seconds
INTERRUPTED = 130

Outcome = collections.namedtuple('Outcome', 'code lines')


class _Refused(Exception):
    pass


class _Failed(Exception):
    pass


def _git(cwd, *args, stdin=None, timeout=cmd.GIT_TIMEOUT):
    r = cmd.call(list(args), cwd, stdin=stdin, timeout=timeout)
    return r.rc, cmd.text(r.out or b'').strip(), (r.err or '').strip()


def _must(cwd, *args, timeout=cmd.GIT_TIMEOUT):
    """Stdout of a git write that must succeed, else _Failed naming it."""
    rc, out, err = _git(cwd, *args, timeout=timeout)
    if rc != 0:
        raise _Failed('git %s failed: %s' % (' '.join(args), err or 'exit %s' % rc))
    return out


def _config_regexp(cwd, pattern):
    """[(key, value)] of the repository's config keys matching `pattern` (git lower-cases the
    section and variable name, not a subsection); _Refused when git fails."""
    rc, out, err = _git(cwd, 'config', '-z', '--get-regexp', pattern)
    if rc == 1:
        return []
    if rc != 0:
        raise _Refused('cannot read git config %s: %s' % (pattern, err or 'exit %s' % rc))
    pairs = []
    for item in out.split('\0'):
        if item:
            key, _, value = item.partition('\n')
            pairs.append((key, value))
    return pairs


_SCHEME = re.compile(r'^([a-z][a-z0-9+.-]*)://(?:[^@/]*@)?([^/]*)(.*)$', re.I)
# `[user@]host:path`; a host of two or more characters, so `C:/x` stays a path.
_SCP = re.compile(r'^(?:[^@/]+@)?([^:/@]{2,}):(.*)$')


def compare_url(url, base=None):
    """The form two remote urls are compared in: slashes forward, a trailing slash and `.git`
    trimmed (repeatedly), case folded. A `scheme://[user@]host[:port]/path` or `[user@]host:path`
    url becomes `host/path`, so the https and ssh spellings of one repository compare equal; a
    `file://` url becomes its path; a relative path is taken from `base` (the checkout)."""
    u = (url or '').strip().replace('\\', '/')
    m = _SCHEME.match(u)
    if m and m.group(1).lower() == 'file':
        u = u[len(m.group(1)) + 3:]
        if re.match(r'^/[A-Za-z]:', u):
            u = u[1:]
        u = _as_path(u, base)
    elif m:
        host = m.group(2).rsplit(':', 1)[0] if ':' in m.group(2) else m.group(2)
        u = '%s/%s' % (host, m.group(3).lstrip('/'))
    else:
        m = _SCP.match(u)
        if m:
            u = '%s/%s' % (m.group(1), m.group(2).lstrip('/'))
        else:
            u = _as_path(u, base)
    while True:
        old = u
        u = u.rstrip('/')
        if u.lower().endswith('.git'):
            u = u[:-4]
        if u == old:
            break
    return u.casefold()


def _as_path(u, base):
    if base is not None and u and not os.path.isabs(u) and not u.startswith('/'):
        u = os.path.join(base, u)
    if u and (os.path.isabs(u) or u.startswith('/')):
        u = os.path.normpath(u)
    return u.replace('\\', '/')


def _rewrites(top):
    """{'insteadof': [(prefix, base)], 'pushinsteadof': [...]}: git's url.<base>.insteadOf and
    url.<base>.pushInsteadOf, longest prefix first (the one git uses)."""
    out = {'insteadof': [], 'pushinsteadof': []}
    for key, value in _config_regexp(top, r'^url\..*\.(push)?insteadof$'):
        head, _, var = key.rpartition('.')
        if head.startswith('url.') and var.lower() in out and value:
            out[var.lower()].append((value, head[len('url.'):]))
    for v in out.values():
        v.sort(key=lambda pair: -len(pair[0]))
    return out


def _forms(url, rewrites, top):
    """Every compare_url form `url` can reach: as written, after insteadOf and after
    pushInsteadOf. Comparing all of them fails closed."""
    forms = {compare_url(url, top)}
    for kind in ('insteadof', 'pushinsteadof'):
        for prefix, base in rewrites[kind]:
            if url.startswith(prefix):
                forms.add(compare_url(base + url[len(prefix):], top))
                break
    return forms


def _remote_table(top):
    """{remote name: [url and pushurl values]} for every configured remote with either."""
    table = {}
    for key, value in _config_regexp(top, r'^remote\..*\.(url|pushurl)$'):
        head, _, var = key.rpartition('.')
        if head.startswith('remote.') and var.lower() in ('url', 'pushurl'):
            table.setdefault(head[len('remote.'):], []).append(value)
    return table


class _Plan(object):
    """What the checks found and the writes will do."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _check_names(top, cfg, branch, remote):
    errs = pmdata.check_fields(branch, remote, '0' * 40)
    if errs:
        raise _Refused('; '.join(errs))
    rc, out, err = _git(top, 'check-ref-format', '--branch', branch)
    if rc != 0 or out != branch:
        raise _Refused('git does not take %r as a branch name: %s' % (branch, err or out))
    if '/' in branch:
        raise _Refused('the data branch name %r must be one path component (no "/")' % branch)
    # Windows keeps loose refs as files: names that differ only in case collide.
    fold = branch.casefold()
    if fold in RESERVED:
        raise _Refused('the data branch cannot be called %r: git refuses refs/heads/%s/<slug> '
                       'work branches while refs/heads/%s exists' % (branch, branch, branch))
    if fold == cfg.main_branch.casefold():
        raise _Refused('the data branch cannot be the main branch %r' % cfg.main_branch)
    rc, out, err = _git(top, 'for-each-ref', '--format=%(refname)', 'refs/heads/', 'refs/remotes/')
    if rc != 0:
        raise _Refused('cannot list branches: %s' % (err or 'exit %s' % rc))
    for ref in out.splitlines():
        parts = ref.split('/')
        heads = ref.startswith('refs/heads/')
        under = parts[2:] if heads else parts[3:]
        if len(under) > 1 and under[0].casefold() == fold:
            raise _Refused('%s exists: a data branch called %r would collide with it'
                           % (ref, branch))
        if heads and len(under) == 1 and under[0] != branch and under[0].casefold() == fold:
            raise _Refused('the branch %s differs from %r only in case; on Windows they collide'
                           % (under[0], branch))
    if '/' in remote:
        raise _Refused('the private remote name %r must have no "/"' % remote)
    if remote.casefold() == cfg.remote.casefold():
        raise _Refused('the private remote %r is the public remote ("repo".remote in %s)'
                       % (remote, cfg.where))


def _check_remote(top, cfg, remote, url):
    """(remote exists, its urls). Refuses, failing closed: a public remote with no url (nothing to
    compare against); a url that disagrees with an existing private remote's; a private url or
    pushurl that equals, in any form it can reach (_forms: insteadOf and pushInsteadOf applied),
    any form of the url or pushurl of ANY other configured remote."""
    table = _remote_table(top)
    if not table.get(cfg.remote):
        raise _Refused('the public remote %r has no url, so the private url cannot be checked '
                       'against it' % cfg.remote)
    rewrites = _rewrites(top)
    have = table.get(remote, [])
    if url is not None:
        if not url.strip() or url.strip().startswith('-'):
            raise _Refused('the private remote url %r is not a url' % url)
        if have and {compare_url(u, top) for u in have} != {compare_url(url, top)}:
            raise _Refused('the remote %r exists with url %s, not %s'
                           % (remote, ', '.join(have), url))
    others = {}
    for name, urls in table.items():
        if name != remote:
            for u in urls:
                for f in _forms(u, rewrites, top):
                    others.setdefault(f, name)
    for u in have + ([url] if url is not None else []):
        for f in _forms(u, rewrites, top):
            if f in others:
                which = others[f]
                raise _Refused('the private remote url %s reaches the url of the remote %r%s'
                               % (u, which, ' (the public remote): the data would be pushed to '
                                  'the public repository' if which == cfg.remote else ''))
    return bool(have), have


def _check_remote_branch(top, remote, branch, target):
    """Refuse a new data branch when the private remote already holds one: a remote-tracking
    ref here, or `git ls-remote <target>` (the remote, or the url about to be added; None: no
    remote at all) finding it. A remote that cannot be reached is not refused (the push will fail
    and say so); the line returned says it was not checked."""
    ref = 'refs/remotes/%s/%s' % (remote, branch)
    rc, _out, err = _git(top, 'rev-parse', '--verify', '-q', ref)
    if rc == 0:
        raise _Refused('%s exists: the private remote already has a %s branch; fetch it '
                       '(git fetch %s refs/heads/%s:refs/heads/%s) and run init --adopt'
                       % (ref, branch, remote, branch, branch))
    if rc != 1:
        raise _Refused('cannot read %s: %s' % (ref, err or 'exit %s' % rc))
    if target is None:
        return None
    rc, out, err = _git(top, 'ls-remote', target, 'refs/heads/%s' % branch,
                        timeout=LS_REMOTE_TIMEOUT)
    if rc != 0:
        return ('remote    %s could not be asked whether it has %s (%s); the push will tell'
                % (remote, branch, err or 'exit %s' % rc))
    if out.strip():
        raise _Refused('the private remote %s already has refs/heads/%s; fetch it (git fetch %s '
                       'refs/heads/%s:refs/heads/%s) and run init --adopt, which checks its root'
                       % (remote, branch, remote, branch, branch))
    return None


def _check_path(top, path):
    if gitrepo.inside(path, top):
        raise _Refused('%s is inside the kit checkout %s; put the data worktree outside it'
                       % (path, top))
    if os.path.lexists(path):
        if not os.path.isdir(path):
            raise _Refused('%s exists and is not a folder' % path)
        if os.listdir(path):
            raise _Refused('%s exists and is not empty' % path)
    place = gitrepo.classify(path)
    if place.kind in (gitrepo.MAIN, gitrepo.LINKED):
        raise _Refused('%s is inside the git work tree %s' % (path, place.top))
    if place.kind != gitrepo.NONE:
        raise _Refused('cannot tell whether %s is inside a git work tree: %s' % (path, place.detail))


def _branch_root(top, branch):
    rc, out, err = _git(top, 'rev-list', '--max-parents=0', 'refs/heads/%s' % branch)
    if rc != 0:
        raise _Refused('cannot read the root of %s: %s' % (branch, err or 'exit %s' % rc))
    roots = out.split()
    if len(roots) != 1:
        raise _Refused('%s has %d root commits; a data branch has exactly one' % (branch, len(roots)))
    return roots[0]


def _check_unshared(top, cfg, branch, root):
    """Refuse a data branch whose root the main branch (local or the public remote's) holds."""
    for ref in ('refs/heads/%s' % cfg.main_branch,
                'refs/remotes/%s/%s' % (cfg.remote, cfg.main_branch)):
        rc, _out, err = _git(top, 'rev-parse', '--verify', '-q', ref + '^{commit}')
        if rc == 1:                     # -q: exit 1 and no message when the ref does not exist
            continue
        if rc != 0:
            raise _Refused('cannot read %s: %s' % (ref, err or 'exit %s' % rc))
        rc, _out, err = _git(top, 'merge-base', '--is-ancestor', root, ref)
        if rc == 0:
            raise _Refused('%s shares history with %s (its root %s is there): it is not a '
                           'separate data branch' % (branch, ref, root))
        if rc != 1:
            raise _Refused('cannot compare %s with %s: %s' % (branch, ref, err or 'exit %s' % rc))


def kit_json_line(branch, remote):
    """The `.kit.json` a data worktree needs (docs/pm-data-branch.md "Layout")."""
    return json.dumps({'repo': {'main_branch': branch, 'remote': remote},
                       'backlog': {'store': 'main'}})


def _check_data_config(top, branch, remote):
    """True when the data branch has a `.kit.json`, which must suit a data worktree; False when it
    has none."""
    rc, _out, err = _git(top, 'cat-file', '-e', 'refs/heads/%s:%s' % (branch, '.kit.json'))
    if rc != 0:
        rc2, _o, err2 = _git(top, 'cat-file', '-e', 'refs/heads/%s' % branch)
        if rc2 != 0:
            raise _Refused('cannot read %s: %s' % (branch, err2 or err))
        return False
    try:
        dcfg = load_at(top, 'refs/heads/%s' % branch)
    except ConfigError as e:
        raise _Refused('the data branch\'s .kit.json cannot be read: %s' % e)
    problems = []
    if dcfg.main_branch != branch:
        problems.append('"repo".main_branch is %r, not %r' % (dcfg.main_branch, branch))
    if dcfg.remote != remote:
        problems.append('"repo".remote is %r, not %r' % (dcfg.remote, remote))
    if dcfg.section('backlog')['store'] == 'ref':
        problems.append('"backlog".store is ref (refs/backlog/main belongs to the kit): use a '
                        'folder, main or branch')
    if problems:
        raise _Refused('%s:.kit.json does not suit a data worktree: %s; it needs %s'
                       % (branch, '; '.join(problems), kit_json_line(branch, remote)))
    return True


def _check_worktree(top, wts, branch, path):
    """('add' | 'keep' | 'readd', the registered worktree or None)."""
    for i, w in enumerate(wts):
        if w.branch != branch:
            continue
        if i == 0:
            raise _Refused('%s is checked out in the main checkout %s' % (branch, w.path))
        if os.path.isdir(w.path):
            if gitrepo.same_path(w.path, path):
                return 'keep', w
            raise _Refused('%s is checked out at %s, not %s; pass that path or move it with '
                           'git worktree move' % (branch, w.path, path))
        return 'readd', w
    return 'add', None


def plan(root, path, branch=pmdata.DEFAULT_BRANCH, remote=pmdata.DEFAULT_REMOTE, url=None,
         adopt=False, home=None):
    """The checks, with no write. Raises _Refused, ConfigError or PmDataError."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise _Refused('the worktree path %r must be absolute' % (path,))
    path = os.path.normpath(path)
    place = gitrepo.classify(root)
    if place.kind != gitrepo.MAIN:
        raise _Refused('run this in the main checkout (%s is %s%s)' % (
            root, place.kind, ': %s' % place.detail if place.detail else ''))
    top = place.top
    rc, head, _err = _git(top, 'symbolic-ref', '-q', '--short', 'HEAD')
    if rc != 0 or not head:
        raise _Refused('the main checkout %s is not on a branch' % top)
    cfg = load(top)
    _check_names(top, cfg, branch, remote)
    common, why = gitrepo.common_dir(top)
    if common is None:
        raise _Refused('cannot find the git common dir: %s' % why)
    record = pmdata.read(common)
    machine = pmdata.machine_settings(home)
    remote_exists, _urls = _check_remote(top, cfg, remote, url)
    rc, out, err = _git(top, 'for-each-ref', '--format=%(refname)', 'refs/heads/%s' % branch)
    if rc != 0:
        raise _Refused('cannot list branches: %s' % (err or 'exit %s' % rc))
    exists = out == 'refs/heads/%s' % branch
    wts, why = gitrepo.worktrees(top)
    if wts is None:
        raise _Refused('cannot list worktrees: %s' % why)
    p = _Plan(top=top, cfg=cfg, common=common, branch=branch, remote=remote, url=url,
              path=path, adopt=adopt,
              home=home, record=record, machine=machine, remote_exists=remote_exists,
              root=None, has_kit_json=False, action='add', registered=None, wrote=False,
              notes=[])
    if not adopt:
        if exists:
            raise _Refused('the branch %s exists; pass --adopt to link it' % branch)
        if record is not None:
            raise _Refused('%s already records a data branch (%s, root %s); pass --adopt to '
                           're-link it' % (pmdata.record_path(common), record.branch, record.root))
        if machine is not None:
            raise _Refused('%s already sets %s (path %s); pass --adopt, or remove the key'
                           % (user.config_file(home), pmdata.KEY, machine['path']))
        _check_path(top, p.path)
        note = _check_remote_branch(top, remote, branch,
                                    remote if remote_exists else url)
        if note:
            p.notes.append(note)
        return p
    if not exists:
        raise _Refused('--adopt needs an existing branch %s' % branch)
    p.root = _branch_root(top, branch)
    if record is not None and record.root != p.root:
        raise _Refused('%s has root %s, but %s records root %s' % (
            branch, p.root, pmdata.record_path(common), record.root))
    _check_unshared(top, cfg, branch, p.root)
    p.has_kit_json = _check_data_config(top, branch, remote)
    p.action, p.registered = _check_worktree(top, wts, branch, p.path)
    if p.action != 'keep':
        _check_path(top, p.path)
    return p


def write_machine(home, settings):
    """Set the machine config's `pm_data` to `settings`, keeping every other key; the file is
    replaced whole. ConfigError (from reading) or OSError."""
    user.user_config(home)                       # a file that does not check is never rewritten
    path = user.config_file(home)
    try:
        with open(path, encoding='utf-8-sig') as f:
            data = parse_json(f.read(), path)
    except FileNotFoundError:
        data = {}
    data[pmdata.KEY] = settings
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
    return path


def _empty_tree(top):
    """The empty tree's id, its object written: `git write-tree` of an index file that does not
    exist (an empty index) in a folder of its own, so the repository's own index is never read or
    written. The same id as EMPTY_TREE (the literal docs/pm-data-branch.md names), and stored, so
    nothing depends on git knowing it without an object."""
    folder = tempfile.mkdtemp(prefix='kit-pmdata-')
    try:
        env = cmd.scrubbed_env()
        env['GIT_INDEX_FILE'] = os.path.join(folder, 'index')
        r = cmd.call(['write-tree'], top, env=env)
        tree = cmd.text(r.out or b'').strip()
        if r.rc != 0:
            raise _Failed('git write-tree of an empty index failed: %s' % (r.err or 'exit %s' % r.rc))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if tree not in EMPTY_TREE.values():
        raise _Failed('git write-tree of an empty index gave %r, not the empty tree' % tree)
    return tree


def _write(p, *args):
    """A git write that may change something even when it fails: counted as written first."""
    p.wrote = True
    return _must(p.top, *args)


def _apply(p, lines):
    """The writes. Raises _Failed after the first failed one; `lines` says what was done and
    `p.wrote` whether anything was changed (a git object nothing names is not a change). The
    branch and kit-pm-data.json are each made whole or not at all, so they count once made."""
    top, b, r = p.top, p.branch, p.remote
    lines.extend(p.notes)
    if p.root is None:
        tree = _empty_tree(top)
        root = _must(top, 'commit-tree', tree, '-m', ROOT_MESSAGE)
        _must(top, 'branch', b, root)
        p.wrote = True
        p.root = root
        lines.append('root      %s (no parent, empty tree), branch %s made' % (root, b))
    else:
        lines.append('root      %s (branch %s, adopted)' % (p.root, b))
    want = pmdata.Record(b, r, p.root)
    if p.record != want:
        try:
            pmdata.write(p.common, b, r, p.root)
        except pmdata.PmDataError as e:
            raise _Failed(str(e))
        p.wrote = True
        lines.append('record    %s written' % pmdata.record_path(p.common))
    else:
        lines.append('record    %s kept' % pmdata.record_path(p.common))
    if p.action == 'readd':
        w = p.registered
        if w.locked is not None:
            _write(p, 'worktree', 'unlock', w.path)
        _write(p, 'worktree', 'remove', w.path)
        lines.append('worktree  %s was gone; removed from git\'s list' % w.path)
    if p.action in ('add', 'readd'):
        _write(p, 'worktree', 'add', p.path, b)
        _write(p, 'worktree', 'lock', '--reason', LOCK_REASON, p.path)
        lines.append('worktree  %s on %s, locked (%s)' % (p.path, b, LOCK_REASON))
    else:
        if p.registered.locked is None:
            _write(p, 'worktree', 'lock', '--reason', LOCK_REASON, p.path)
            lines.append('worktree  %s kept, locked now (%s)' % (p.path, LOCK_REASON))
        else:
            lines.append('worktree  %s kept (already locked)' % p.path)
    _write(p, 'config', '--local', 'branch.%s.remote' % b, r)
    _write(p, 'config', '--local', 'branch.%s.pushRemote' % b, r)
    _write(p, 'config', '--local', 'branch.%s.merge' % b, 'refs/heads/%s' % b)
    rc, cur, err = _git(top, 'config', '--local', '--get', 'push.default')
    if rc == 1:
        _write(p, 'config', '--local', 'push.default', 'simple')
        pd = 'push.default=simple set'
    elif rc == 0:
        pd = 'push.default=%s kept' % cur
    else:
        raise _Failed('cannot read push.default: %s' % (err or 'exit %s' % rc))
    lines.append('config    branch.%s.remote and .pushRemote=%s, .merge=refs/heads/%s; %s'
                 % (b, r, b, pd))
    if p.url is not None and not p.remote_exists:
        _write(p, 'remote', 'add', r, p.url)
        p.remote_exists = True
        lines.append('remote    %s added (%s)' % (r, p.url))
    try:
        cfgpath = write_machine(p.home, {'path': p.path, 'branch': b, 'remote': r})
    except (ConfigError, OSError) as e:
        raise _Failed('cannot write the machine config: %s' % e)
    p.wrote = True
    lines.append('machine   %s: %s set' % (cfgpath, pmdata.KEY))
    if not p.has_kit_json:
        lines.append('kit.json  the data worktree has no .kit.json; add %s with: %s'
                     % (os.path.join(p.path, '.kit.json'), kit_json_line(b, r)))


def init(root, path, branch=pmdata.DEFAULT_BRANCH, remote=pmdata.DEFAULT_REMOTE, url=None,
         adopt=False, home=None):
    """Outcome(code, lines); see the module docstring. Ctrl+C is INTERRUPTED (130) with the lines
    made so far, never a bare KeyboardInterrupt."""
    try:
        p = plan(root, path, branch, remote, url, adopt, home)
    except _Refused as e:
        return Outcome(REFUSED, ['REFUSED: %s' % e, 'nothing was changed'])
    except (ConfigError, pmdata.PmDataError) as e:
        return Outcome(CONFIG, ['CONFIG: %s' % e, 'nothing was changed'])
    except KeyboardInterrupt:
        return Outcome(INTERRUPTED, ['INTERRUPTED during the checks', 'nothing was changed'])
    lines = []
    finish = ('run init again with --adopt (and the same --path) to finish; it re-checks the '
              'branch against the recorded root')
    try:
        _apply(p, lines)
    except _Failed as e:
        lines.append('FAILED: %s' % e)
        if not p.wrote:
            lines.append('nothing was changed')
            return Outcome(REFUSED, lines)
        lines.append('stopped part-way; ' + finish)
        return Outcome(PARTIAL, lines)
    except KeyboardInterrupt:
        lines.append('INTERRUPTED')
        lines.append('stopped part-way; ' + finish if p.wrote else 'nothing was changed')
        return Outcome(INTERRUPTED, lines)
    spec = 'refs/heads/%s:refs/heads/%s' % (p.branch, p.branch)
    if not p.remote_exists:
        lines.append('push      not done: there is no remote %s; add it (git remote add %s <url>), '
                     'then git push %s %s' % (p.remote, p.remote, p.remote, spec))
        return Outcome(OK, lines)
    try:
        rc, _out, err = _git(p.top, 'push', p.remote, spec, timeout=PUSH_TIMEOUT)
    except KeyboardInterrupt:
        lines.append('INTERRUPTED during git push %s %s; everything else is made' % (p.remote, spec))
        return Outcome(INTERRUPTED, lines)
    if rc != 0:
        lines.append('push      NOT DONE: git push %s %s failed: %s; the local branch is kept'
                     % (p.remote, spec, err or 'exit %s' % rc))
        lines.append('recover   make the remote reachable, then git push %s %s. If it refuses '
                     'because %s already has a %s with other history: git fetch %s '
                     'refs/heads/%s:refs/remotes/%s/%s and compare; nothing here overwrites it. '
                     'pm_data.py init --adopt re-checks the branch against the recorded root'
                     % (p.remote, spec, p.remote, p.branch, p.remote, p.branch, p.remote,
                        p.branch))
        return Outcome(PUSH_FAILED, lines)
    lines.append('push      %s to %s' % (spec, p.remote))
    return Outcome(OK, lines)
