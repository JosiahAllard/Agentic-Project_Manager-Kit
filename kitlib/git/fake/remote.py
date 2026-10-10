"""remote, push, fetch, ls-remote and clone, between repositories on this machine only.

A remote is a local path (a drive path, '/', '.', or a file:// url); anything else is Unsupported.
The other repository is opened in process: objects are copied (the closure of what is sent, down to
what the receiver already has) and refs written as git writes them. Hooks on either side, receive.*
config, tags that would be auto-followed, pruning, shallow clones and every option not named here are
Unsupported. Output follows git 2.55 as probed (push/fetch status tables, porcelain, FETCH_HEAD).
"""
import os
import shutil

from . import command
from . import config as _config
from . import index as _index
from . import plumbing as _pl
from . import refs as _refs
from . import revs as _revs
from . import util as _u
from . import worktrees as _wts
from .errors import GitExit, Unsupported, die
from .repo import Repo, is_git_directory, slash

SUMMARY_WIDTH = 17          # transport_summary_width: 2 * 7 + 3


# -- where a remote is ------------------------------------------------------------------------

def _no_such_repo(name):
    return GitExit(128, "fatal: '%s' does not appear to be a git repository\n"
                        "fatal: Could not read from remote repository.\n\n"
                        "Please make sure you have the correct access rights\n"
                        "and the repository exists.\n" % name)


def configured(repo, name):
    """True when config names remote `name` (any remote.<name>.* key)."""
    if repo is None:
        return False
    pre = 'remote.%s.' % name.lower()
    for k, _v in repo.config.items():
        if k.lower().startswith(pre) and k.count('.') >= 2 and k[len('remote.'):k.rfind('.')] == name:
            return True
    return False


def _looks_local(url):
    u = url.replace('\\', '/')
    return (u.startswith('/') or u.startswith('./') or u.startswith('../') or u in ('.', '..')
            or (len(u) >= 2 and u[1] == ':' and u[0].isalpha()) or u.startswith('file://'))


def _local_path(url, base):
    """The folder a local url names, resolved against `base`; Unsupported for anything else."""
    u = url
    if u.startswith('file://'):
        u = u[len('file://'):]
        if u.startswith('/') and len(u) > 2 and u[2] == ':':
            u = u[1:]
    if '://' in u or (':' in u and not (len(u) >= 2 and u[1] == ':')):
        raise Unsupported('a remote that is not a local path: %s' % url)
    if not os.path.isabs(u):
        if not _looks_local(url):
            raise Unsupported('a remote url git may read as a host: %s' % url)
        u = os.path.join(base, u)
    return os.path.normpath(u)


def open_dir(path):
    """(Repo of the repository at `path`, as git's enter_repo finds it) or None."""
    for cand in (os.path.join(path, '.git'), path, path + '.git', os.path.join(path + '.git', '.git')):
        if os.path.isfile(cand) and os.path.basename(cand) == '.git':
            raise Unsupported('a remote whose .git is a gitfile: %s' % path)
        if os.path.isdir(cand) and is_git_directory(cand):
            return cand
    return None


class Remote:
    """The other end: `name` (None for a bare url), `url` (as configured or given), `repo`."""

    def __init__(self, ctx, repo, arg):
        self.arg = arg
        if configured(repo, arg):
            urls = repo.config.get_all('remote.%s.url' % arg)
            if not urls:
                raise Unsupported('remote %s without a url' % arg)
            if len(urls) > 1:
                raise Unsupported('remote %s with several urls' % arg)
            if repo.config.get_all('remote.%s.pushurl' % arg):
                raise Unsupported('remote.%s.pushurl' % arg)
            self.name = arg
            self.url = urls[0]
        elif _looks_local(arg):
            self.name = None
            self.url = arg
        else:
            raise _no_such_repo(arg)
        for k, _v in (repo.config.items() if repo is not None else ()):
            if k.lower().startswith('url.'):
                raise Unsupported('url.<base>.insteadOf config')
        base = (repo.worktree or repo.gitdir) if repo is not None else ctx.cwd
        path = _local_path(self.url, base)
        gd = open_dir(path)
        if gd is None:
            raise _no_such_repo(self.url)
        worktree = None
        if os.path.basename(os.path.normpath(gd)) == '.git' and os.path.normcase(gd) != os.path.normcase(path):
            worktree = os.path.dirname(gd)
        env = ctx.env
        self.repo = Repo(os.path.realpath(gd), worktree and os.path.realpath(worktree), env,
                         ctx.cli_config)
        if self.repo.config.bool('core.bare', False):
            self.repo.worktree = None
        if repo is not None and self.repo.algo != repo.algo:
            raise Unsupported('a remote with another object format')

    def display(self):
        """The url as fetch prints it: trailing slashes, then '.git', stripped."""
        u = self.url
        i = len(u) - 1
        while i >= 0 and u[i] == '/':
            i -= 1
        u = u[:i + 1]
        if len(u) > 4 and u.endswith('.git'):
            u = u[:-4]
        return u

    def refs(self):
        """[(name, oid)] of the remote's refs/ (sorted), as ls-remote lists them."""
        return _refs.list_refs(self.repo, 'refs/')

    def head(self):
        """(symref target or None, oid or None) of the remote's HEAD."""
        raw = _refs.read_raw(self.repo, 'HEAD')
        oid, _ = _refs.resolve(self.repo, 'HEAD')
        target = raw[1] if raw and raw[0] == 'sym' else None
        return target, oid


def copy_closure(src, dst, tips):
    """Copy every object reachable from `tips` that `dst` lacks, stopping at objects it has."""
    todo = [t for t in tips if t]
    seen = set()
    while todo:
        oid = todo.pop()
        if oid in seen:
            continue
        seen.add(oid)
        if dst.objects.has(oid):
            continue
        got = src.objects.read(oid)
        if got is None:
            raise Unsupported('object %s missing on the sending side' % oid)
        kind, data = got
        if kind == 'commit':
            c = _revs.commit(src, oid)
            todo.append(c.tree)
            todo.extend(c.parents)
        elif kind == 'tree':
            from .objects import parse_tree
            for mode, _name, child in parse_tree(data, src.hexlen):
                if mode != '160000':
                    todo.append(child)
        elif kind == 'tag':
            todo.append(_revs.tag(src, oid).object)
        dst.objects.write(kind, data)


def _no_hooks(repo, names, what):
    for n in names:
        if repo.hook_exists(n):
            raise Unsupported('%s with a %s hook' % (what, n))


def _no_receive_config(r):
    for k, _v in r.config.items():
        kl = k.lower()
        if kl == 'receive.denydeletecurrent':
            continue                    # push's _judge reads it
        if kl.startswith('receive.') or kl.startswith('transfer.') or kl.startswith('uploadpack.'):
            raise Unsupported('%s on the remote' % k)


def _checked_out(r, ref):
    """The worktree path (git's '/' form) where `ref` is checked out in repository `r`, or None."""
    for w in _wts.all_worktrees(r):
        if not w.bare and w.head_ref == ref:
            return w.path
    return None


def prettify(name):
    for p in ('refs/heads/', 'refs/tags/', 'refs/remotes/'):
        if name.startswith(p):
            return name[len(p):]
    return name


def _match_remote(names, short):
    """Remote ref names `short` matches by git's refname rules (refname_match)."""
    rules = ('%s', 'refs/%s', 'refs/tags/%s', 'refs/heads/%s', 'refs/remotes/%s', 'refs/remotes/%s/HEAD')
    return [n for n in names if any(n == r % short for r in rules)]


# -- remote -----------------------------------------------------------------------------------

def _no_such_remote(name, colon=False):
    return GitExit(2, "error: No such remote%s '%s'\n" % (':' if colon else '', name))


@command('remote')
def remote_cmd(ctx, args):
    repo = ctx.need_repo()
    if not args or args[0] in ('-v', '--verbose'):
        verbose = bool(args)
        if len(args) > 1:
            raise Unsupported('git remote %s' % ' '.join(args))
        for name in sorted(_pl._remote_names(repo)):
            if not verbose:
                ctx.write('%s\n' % name)
                continue
            url = repo.config.get('remote.%s.url' % name)
            if url is None or len(repo.config.get_all('remote.%s.url' % name)) > 1 \
                    or repo.config.get_all('remote.%s.pushurl' % name):
                raise Unsupported('git remote -v of a remote without one plain url')
            ctx.write('%s\t%s (fetch)\n%s\t%s (push)\n' % (name, url, name, url))
        return 0
    sub, rest = args[0], args[1:]
    path = os.path.join(repo.commondir, 'config')
    if sub == 'add':
        if len(rest) != 2 or any(a.startswith('-') for a in rest):
            raise Unsupported('git remote add %s' % ' '.join(rest))
        name, url = rest
        if not _refs.check_refname('refs/remotes/%s/test' % name):
            raise die("'%s' is not a valid remote name" % name)
        if configured(repo, name):
            raise GitExit(3, 'error: remote %s already exists.\n' % name)
        _config.set_value(path, 'remote.%s.url' % name, url)
        _config.set_value(path, 'remote.%s.fetch' % name, '+refs/heads/*:refs/remotes/%s/*' % name)
        repo.reload_config()
        return 0
    if sub == 'get-url':
        if len(rest) != 1 or rest[0].startswith('-'):
            raise Unsupported('git remote get-url %s' % ' '.join(rest))
        name = rest[0]
        if not configured(repo, name):
            raise _no_such_remote(name)
        urls = repo.config.get_all('remote.%s.url' % name)
        if len(urls) != 1:
            raise Unsupported('git remote get-url of a remote with %d urls' % len(urls))
        ctx.write(urls[0] + '\n')
        return 0
    if sub == 'set-url':
        if len(rest) != 2 or any(a.startswith('-') for a in rest):
            raise Unsupported('git remote set-url %s' % ' '.join(rest))
        name, url = rest
        if not configured(repo, name):
            raise _no_such_remote(name)
        if len(repo.config.get_all('remote.%s.url' % name)) > 1:
            raise Unsupported('git remote set-url of a remote with several urls')
        _config.set_value(path, 'remote.%s.url' % name, url)
        repo.reload_config()
        return 0
    if sub in ('remove', 'rm'):
        if len(rest) != 1:
            raise Unsupported('git remote %s %s' % (sub, ' '.join(rest)))
        return _remove(ctx, repo, path, rest[0])
    if sub == 'rename':
        if len(rest) != 2 or any(a.startswith('-') for a in rest):
            raise Unsupported('git remote rename %s' % ' '.join(rest))
        return _rename(ctx, repo, path, rest[0], rest[1])
    raise Unsupported('git remote %s' % sub)


def _branches_on(repo, name):
    """Branch names whose branch.<b>.remote is `name`."""
    out = []
    for k, v in repo.config.items():
        kl = k.lower()
        if kl.startswith('branch.') and kl.endswith('.remote') and v == name:
            out.append(k[len('branch.'):-len('.remote')])
    return out


def _remove(ctx, repo, path, name):
    if not configured(repo, name):
        raise _no_such_remote(name, colon=True)
    for k, _v in repo.config.items():
        if k.lower() in ('remote.pushdefault',) or k.lower().endswith('.pushremote'):
            raise Unsupported('git remote remove with %s set' % k)
    for b in _branches_on(repo, name):
        for key in ('branch.%s.remote' % b, 'branch.%s.merge' % b):
            try:
                _config.unset(path, key, all_=True)
            except GitExit:
                pass
    _config.rename_section(path, 'remote', name, None)
    repo.reload_config()
    for ref, _oid in _refs.list_refs(repo, 'refs/remotes/%s/' % name):
        _refs.delete(repo, ref)
    raw = _refs.read_raw(repo, 'refs/remotes/%s/HEAD' % name)
    if raw is not None:
        _refs.delete(repo, 'refs/remotes/%s/HEAD' % name)
    d = os.path.join(repo.commondir, 'refs', 'remotes', name)
    _rmdirs(d)
    return 0


def _rmdirs(d):
    if not os.path.isdir(d):
        return
    for dirpath, dirs, files in os.walk(d, topdown=False):
        if files:
            return
        try:
            os.rmdir(dirpath)
        except OSError:
            return


def _rename(ctx, repo, path, old, new):
    if not configured(repo, old):
        raise _no_such_remote(old, colon=True)
    if not _refs.check_refname('refs/remotes/%s/test' % new):
        raise die("'%s' is not a valid remote name" % new)
    if configured(repo, new):
        raise die('remote %s already exists.' % new)
    for k, _v in repo.config.items():
        if k.lower() in ('remote.pushdefault',) or k.lower().endswith('.pushremote'):
            raise Unsupported('git remote rename with %s set' % k)
    specs = repo.config.get_all('remote.%s.fetch' % old)
    _config.rename_section(path, 'remote', old, new)
    old_default = '+refs/heads/*:refs/remotes/%s/*' % old
    if specs:
        if any(s != old_default for s in specs):
            raise Unsupported('git remote rename with a non-default fetch refspec')
        _config.set_value(path, 'remote.%s.fetch' % new, '+refs/heads/*:refs/remotes/%s/*' % new,
                          replace_all=True)
    repo.reload_config()
    for b in _branches_on(repo, old):
        _config.set_value(path, 'branch.%s.remote' % b, new)
    repo.reload_config()
    sym = _refs.read_raw(repo, 'refs/remotes/%s/HEAD' % old)
    for ref, oid in _refs.list_refs(repo, 'refs/remotes/%s/' % old):
        if ref == 'refs/remotes/%s/HEAD' % old and sym and sym[0] == 'sym':
            continue
        _refs.delete(repo, ref)
        _refs.update(repo, 'refs/remotes/%s/' % new + ref[len('refs/remotes/%s/' % old):], oid)
    if sym and sym[0] == 'sym':
        _refs.delete(repo, 'refs/remotes/%s/HEAD' % old)
        target = sym[1]
        pre = 'refs/remotes/%s/' % old
        if target.startswith(pre):
            target = 'refs/remotes/%s/' % new + target[len(pre):]
        _refs.write_symref(repo, 'refs/remotes/%s/HEAD' % new, target)
    _rmdirs(os.path.join(repo.commondir, 'refs', 'remotes', old))
    return 0


# -- ls-remote --------------------------------------------------------------------------------

_LS = _u.Spec('ls-remote', flags='heads|branches t|tags refs q|quiet exit-code')


@command('ls-remote')
def ls_remote(ctx, args):
    opts, pos, dd = _u.parse(_LS, args)
    if dd:
        pos += dd
    if not pos:
        raise Unsupported('git ls-remote without a remote')
    repo = ctx.repo
    rem = Remote(ctx, repo, pos[0])
    patterns = pos[1:]
    for p in patterns:
        if any(c in p for c in '*?['):
            raise Unsupported('git ls-remote glob pattern %s' % p)
    rows = []
    target, head_oid = rem.head()
    heads, tags = opts.get('heads'), opts.get('tags')
    if head_oid and not (heads or tags or opts.get('refs')):
        rows.append(('HEAD', head_oid))
    for name, oid in rem.refs():
        if heads or tags:
            if not ((heads and name.startswith('refs/heads/')) or (tags and name.startswith('refs/tags/'))):
                continue
        rows.append((name, oid))
        if name.startswith('refs/tags/') and not opts.get('refs'):
            if _revs.obj_type(rem.repo, oid) == 'tag':
                rows.append((name + '^{}', _revs.peel(rem.repo, oid)))
    if patterns:
        rows = [(n, o) for n, o in rows if any(n == p or n.endswith('/' + p) for p in patterns)]
    for name, oid in rows:
        ctx.write('%s\t%s\n' % (oid, name))
    if opts.get('exit-code') and not rows:
        return 2
    return 0


# -- push -------------------------------------------------------------------------------------

_PUSH = _u.Spec('push', flags='q|quiet f|force porcelain d|delete no-verify',
                optional='force-with-lease', multi='')


class _Update:
    def __init__(self, src_given, src_full, new, dst, force):
        self.src_given = src_given      # what the refspec said ('' for a delete)
        self.src_full = src_full        # the local ref it matched, or None
        self.new = new                  # oid, or None for a delete
        self.dst = dst                  # the remote ref, full name
        self.force = force
        self.old = None
        self.lease = None               # None: no lease; '' must not exist; else the oid expected
        self.status = None              # ok / uptodate / rejected / stale / fetch-first
        self.flag = ' '
        self.summary = ''
        self.msg = None

    def porcelain_src(self):
        if self.new is None:
            return ''
        return self.src_full or self.src_given

    def display_from(self):
        if self.src_full:
            return prettify(self.src_full)
        return self.src_given


def _parse_leases(values):
    out = {}
    for v in values:
        if v is True or ':' not in v:
            raise Unsupported('--force-with-lease without an explicit expected value')
        ref, _, expect = v.partition(':')
        if not ref.startswith('refs/'):
            raise Unsupported('--force-with-lease on a short ref name %s' % ref)
        out[ref] = expect
    return out


def _parse_push_args(args):
    """(opts, positionals, leases): --force-with-lease may repeat."""
    leases, rest = [], []
    for a in args:
        if a == '--force-with-lease' or a.startswith('--force-with-lease='):
            leases.append(a.partition('=')[2] if '=' in a else True)
        else:
            rest.append(a)
    opts, pos, dd = _u.parse(_PUSH, rest)
    if dd:
        pos += dd
    return opts, pos, _parse_leases(leases)


@command('push')
def push(ctx, args):
    opts, pos, leases = _parse_push_args(args)
    repo = ctx.need_repo()
    if not pos:
        raise Unsupported('git push without a remote')
    if len(pos) < 2:
        raise Unsupported('git push without a refspec (push.default)')
    rem = Remote(ctx, repo, pos[0])
    if not opts.get('no-verify'):
        _no_hooks(repo, ('pre-push',), 'push')
    _no_hooks(repo, ('reference-transaction',), 'push')
    _no_hooks(rem.repo, ('pre-receive', 'update', 'post-receive', 'post-update', 'proc-receive',
                         'reference-transaction', 'push-to-checkout'), 'push')
    _no_receive_config(rem.repo)
    quiet, porcelain = opts.get('quiet'), opts.get('porcelain')
    remote_names = [n for n, _ in rem.refs()]
    updates, errors = [], []
    for spec in pos[1:]:
        u = _push_spec(ctx, repo, rem, spec, opts, remote_names, errors, leases)
        if u is not None:
            updates.append(u)
    if errors:
        if porcelain:
            raise Unsupported('git push --porcelain with a refspec error')
        raise GitExit(1, ''.join('error: %s\n' % e for e in errors)
                      + "error: failed to push some refs to '%s'\n" % rem.url)
    head_ref = _refs.head_target(repo)
    for ref, expect in list(leases.items()):
        if expect and not _is_oid(expect, repo.hexlen):
            got = _pl.resolve(ctx, expect)
            if got is None:
                raise Unsupported('--force-with-lease expected value %s' % expect)
            leases[ref] = got
    reasons = set()
    for u in updates:
        _judge(repo, rem, u, leases, reasons, head_ref)
    accepted = [u for u in updates if u.status == 'ok']
    copy_closure(repo, rem.repo, [u.new for u in accepted if u.new])
    remote_said = []
    for u in accepted:
        if u.new is not None and _checked_out(rem.repo, u.dst):
            raise Unsupported('push to a branch checked out in the remote')
        # receive-pack updates each ref as a compare-and-swap against the value it judged, so a
        # write that lands in between loses here, as a remote rejection
        try:
            if u.new is None:
                _refs.delete(rem.repo, u.dst, old=u.old)
            else:
                _refs.update(rem.repo, u.dst, u.new, old=u.old or '')
        except GitExit as e:
            _remote_rejected(rem, u, e, remote_said)
    if rem.name is not None:
        for u in updates:
            if u.status not in ('ok', 'uptodate'):
                continue
            track = _revs.map_ref(repo, rem.name, u.dst)
            if not track:
                continue
            if u.new is None:
                if _refs.exists(repo, track):
                    _refs.delete(repo, track)
            else:
                _refs.update(repo, track, u.new)
    failed = any(u.status not in ('ok', 'uptodate') for u in updates)
    pushed = any(u.status == 'ok' for u in updates)
    for line in remote_said:                    # receive-pack's stderr comes before the report
        ctx.warn(line)
    if porcelain:
        if not quiet or failed:
            ctx.write('To %s\n' % rem.url)
            for u in updates:
                line = '%s\t%s:%s\t%s' % (u.flag, u.porcelain_src(), u.dst, u.summary)
                if u.msg:
                    line += ' (%s)' % u.msg
                ctx.write(line + '\n')
        ctx.write('Done\n')
    else:
        if not quiet or failed:
            shown = [u for u in updates if u.status != 'uptodate']
            if shown:
                ctx.warn('To %s\n' % rem.url)
            for u in shown:
                if u.new is None and u.status in ('ok', 'remote-rejected'):
                    body = prettify(u.dst)
                elif u.new is None:
                    body = '(delete) -> %s' % prettify(u.dst)
                else:
                    body = '%s -> %s' % (u.display_from(), prettify(u.dst))
                line = ' %s %-*s %s' % (u.flag, SUMMARY_WIDTH, u.summary, body)
                if u.msg:
                    line += ' (%s)' % u.msg
                ctx.warn(line + '\n')
        if not quiet and not failed and not pushed:
            ctx.warn('Everything up-to-date\n')
    if failed:
        ctx.warn("error: failed to push some refs to '%s'\n" % rem.url)
        ctx.warn(_hint(reasons))
        return 1
    return 0


_HINT_HEAD = ("hint: Updates were rejected because the tip of your current branch is behind\n"
              "hint: its remote counterpart. If you want to integrate the remote changes,\n"
              "hint: use 'git pull' before pushing again.\n"
              "hint: See the 'Note about fast-forwards' in 'git push --help' for details.\n")
_HINT_FETCH_FIRST = ("hint: Updates were rejected because the remote contains work that you do not\n"
                     "hint: have locally. This is usually caused by another repository pushing to\n"
                     "hint: the same ref. If you want to integrate the remote changes, use\n"
                     "hint: 'git pull' before pushing again.\n"
                     "hint: See the 'Note about fast-forwards' in 'git push --help' for details.\n")


def _hint(reasons):
    if 'non-ff-head' in reasons:
        return _HINT_HEAD
    if 'non-ff-other' in reasons:
        raise Unsupported('the push hint for a non-fast-forward of a branch not checked out')
    if 'fetch-first' in reasons:
        return _HINT_FETCH_FIRST
    return ''


# receive-pack's stderr is relayed over the sideband; off a terminal each non-empty line ends in
# eight spaces (sideband.c's DUMB_SUFFIX).
_SIDEBAND_PAD = ' ' * 8


def _remote_rejected(rem, u, exc, said):
    """Mark `u` as receive-pack marks a ref update that failed under its lock, and queue the
    remote's error lines. Wording checked against git 2.55 (2026-10-10): a held lock or a ref
    that appeared reads "reference already exists"; a ref that moved, "incorrect old value
    provided"."""
    first = exc.err.split('\n', 1)[0]
    why = first[len('fatal: '):] if first.startswith('fatal: ') else first
    if 'Unable to create' in why and why.endswith('File exists.'):
        lock = '%s/./%s.lock' % (slash(rem.repo.commondir).rstrip('/'), u.dst)
        said.append("remote: error: cannot lock ref '%s': Unable to create '%s': File exists.%s\n"
                    "remote: \n"
                    "remote: Another git process seems to be running in this repository, or the "
                    "lock file may be stale%s\n" % (u.dst, lock, _SIDEBAND_PAD, _SIDEBAND_PAD))
        msg = 'reference already exists'
    elif why.endswith(': reference already exists'):
        said.append('remote: error: %s%s\n' % (why, _SIDEBAND_PAD))
        msg = 'reference already exists'
    elif ' but expected ' in why and ': is at ' in why:
        said.append('remote: error: %s%s\n' % (why, _SIDEBAND_PAD))
        msg = 'incorrect old value provided'
    else:
        raise Unsupported('a push whose ref update failed on the remote: %s' % first)
    u.status, u.flag, u.summary, u.msg = 'remote-rejected', '!', '[remote rejected]', msg


def _push_spec(ctx, repo, rem, spec, opts, remote_names, errors, leases):
    force = bool(opts.get('force'))
    if spec.startswith('+'):
        force, spec = True, spec[1:]
    if opts.get('delete'):
        if ':' in spec:
            raise Unsupported('git push --delete with a colon refspec')
        src, dst = '', spec
        colon = True
    else:
        src, colon, dst = spec.partition(':')
    if any(c in spec for c in '*^'):
        raise Unsupported('git push refspec %s' % spec)
    if colon and src == '':
        # a delete
        full = dst if dst.startswith('refs/') else None
        if full is None:
            hits = _match_remote(remote_names, dst)
            if len(hits) > 1:
                raise Unsupported('git push delete of an ambiguous %s' % dst)
            full = hits[0] if hits else None
        if full is not None and full not in remote_names and leases.get(full):
            # the lease is checked first: a stale-info rejection, not a missing-ref error
            return _Update('', None, None, full, force)
        if full is None or full not in remote_names:
            errors.append("unable to delete '%s': remote ref does not exist" % dst)
            return None
        return _Update('', None, None, full, force)
    src_full = None
    if src == 'HEAD':
        target = _refs.head_target(repo)
        if target is None:
            raise Unsupported('git push of a detached HEAD')
        src_full, new = 'HEAD', _refs.resolve(repo, 'HEAD')[0]
        if new is None:
            errors.append('src refspec %s does not match any' % src)
            return None
        implied = target
    else:
        hits = [h for h in _refs.dwim(repo, src) if h.startswith('refs/')] if src else []
        if len(hits) > 1:
            raise Unsupported('git push of an ambiguous src %s' % src)
        if hits:
            src_full = hits[0]
            new = _refs.resolve(repo, src_full)[0]
            implied = src_full
        else:
            if not colon:
                errors.append('src refspec %s does not match any' % src)
                return None
            try:
                new = _pl.resolve(ctx, src)
            except GitExit:
                new = None
            if new is None:
                errors.append('src refspec %s does not match any' % src)
                return None
            implied = None
    if not colon:
        if implied is None:
            raise Unsupported('git push %s without a destination' % src)
        dst_full = implied
    elif dst.startswith('refs/'):
        dst_full = dst
    else:
        hits = _match_remote(remote_names, dst)
        if len(hits) > 1:
            raise Unsupported('git push to an ambiguous destination %s' % dst)
        if hits:
            dst_full = hits[0]
        elif implied and implied.startswith('refs/heads/'):
            dst_full = 'refs/heads/' + dst
        elif implied and implied.startswith('refs/tags/'):
            dst_full = 'refs/tags/' + dst
        elif src_full == 'HEAD':
            dst_full = 'refs/heads/' + dst
        else:
            raise Unsupported('git push of %s to a short destination %s' % (src, dst))
    if not _refs.check_refname(dst_full):
        raise Unsupported('git push to an invalid ref %s' % dst_full)
    if _revs.obj_type(repo, new) != 'commit' or not dst_full.startswith(('refs/heads/', 'refs/backlog/')):
        if dst_full.startswith('refs/tags/') or _revs.obj_type(repo, new) != 'commit':
            raise Unsupported('git push of a tag or non-commit to %s' % dst_full)
    return _Update(src, src_full, new, dst_full, force)


def _judge(repo, rem, u, leases, reasons, head_ref):
    u.old = _refs.resolve(rem.repo, u.dst)[0]
    lease = leases.get(u.dst)
    # git marks an unchanged ref up to date before it checks a lease
    if u.new is not None and u.old == u.new:
        u.status, u.flag, u.summary = 'uptodate', '=', '[up to date]'
        return
    if lease is not None:
        expect = lease
        if (expect == '' and u.old is not None) or (expect != '' and u.old != expect):
            u.status, u.flag, u.summary, u.msg = 'stale', '!', '[rejected]', 'stale info'
            return
    if u.new is None:
        if u.dst == _refs.head_target(rem.repo):
            # receive-pack's denyDeleteCurrent: only 'ignore' (or false) is ported
            v = rem.repo.config.get('receive.denydeletecurrent')
            if v is None or v.lower() not in ('ignore', 'false', 'no', 'off', '0'):
                raise Unsupported('push deleting the remote\'s current branch '
                                  '(receive.denyDeleteCurrent=%s)' % v)
        u.status, u.flag, u.summary = 'ok', '-', '[deleted]'
        return
    if u.old == u.new:
        u.status, u.flag, u.summary = 'uptodate', '=', '[up to date]'
        return
    if u.old is None:
        kind = ('[new branch]' if u.dst.startswith('refs/heads/') else
                '[new tag]' if u.dst.startswith('refs/tags/') else '[new reference]')
        u.status, u.flag, u.summary = 'ok', '*', kind
        return
    old_short = _pl.abbrev(repo, u.old) if repo.objects.has(u.old) else u.old[:7]
    new_short = _pl.abbrev(repo, u.new)
    forced = u.force or lease is not None
    if not repo.objects.has(u.old):
        if forced:
            u.status, u.flag, u.summary, u.msg = 'ok', '+', '%s...%s' % (old_short, new_short), 'forced update'
            return
        u.status, u.flag, u.summary, u.msg = 'rejected', '!', '[rejected]', 'fetch first'
        reasons.add('fetch-first')
        return
    if _revs.obj_type(repo, u.old) != 'commit':
        raise Unsupported('git push over a non-commit %s' % u.dst)
    if _revs.is_ancestor(repo, u.old, u.new):
        u.status, u.summary = 'ok', '%s..%s' % (old_short, new_short)
        return
    if forced:
        u.status, u.flag, u.summary, u.msg = 'ok', '+', '%s...%s' % (old_short, new_short), 'forced update'
        return
    u.status, u.flag, u.summary, u.msg = 'rejected', '!', '[rejected]', 'non-fast-forward'
    reasons.add('non-ff-head' if u.dst == head_ref else 'non-ff-other')


def _is_oid(s, hexlen):
    return len(s) == hexlen and all(c in '0123456789abcdef' for c in s)


# -- fetch ------------------------------------------------------------------------------------

_FETCH = _u.Spec('fetch', flags='q|quiet n|no-tags no-write-fetch-head f|force',
                 values='refmap')


class _Fetched:
    def __init__(self, name, oid, dst, force, merge, cmdline):
        self.name = name        # remote ref
        self.oid = oid
        self.dst = dst          # local ref or None (FETCH_HEAD only)
        self.force = force
        self.merge = merge      # for-merge in FETCH_HEAD
        self.cmdline = cmdline  # part of FETCH_HEAD (not an opportunistic tracking update)
        self.line = None        # (flag, summary, msg) when displayed
        self.failed = False


def _refspec(spec):
    force = spec.startswith('+')
    if force:
        spec = spec[1:]
    if spec.startswith('^'):
        raise Unsupported('negative refspec %s' % spec)
    src, _, dst = spec.partition(':')
    return force, src, dst


def _expand(remote_refs, force, src, dst, merge, cmdline):
    """[_Fetched] one refspec gives over the remote's refs ([(name, oid)])."""
    out = []
    if '*' in src:
        if dst and '*' not in dst:
            raise Unsupported('refspec %s:%s' % (src, dst))
        pa, sa = src.split('*', 1)
        for name, oid in remote_refs:
            if name.startswith(pa) and name.endswith(sa) and len(name) >= len(pa) + len(sa):
                mid = name[len(pa):len(name) - len(sa)] if sa else name[len(pa):]
                local = dst.replace('*', mid) if dst else None
                out.append(_Fetched(name, oid, local, force, merge, cmdline))
        return out
    if dst and not dst.startswith('refs/'):
        raise Unsupported('fetch refspec destination %s' % dst)
    names = [n for n, _ in remote_refs]
    if src.startswith('refs/'):
        hits = [src] if src in names else []
    elif src == 'HEAD':
        raise Unsupported('fetch of the remote HEAD')
    else:
        hits = _match_remote(names, src)
    if not hits:
        raise die("couldn't find remote ref %s" % src)
    name = hits[0]
    oid = dict(remote_refs)[name]
    out.append(_Fetched(name, oid, dst or None, force, merge, cmdline))
    return out


@command('fetch')
def fetch(ctx, args):
    opts, pos, dd = _u.parse(_FETCH, args)
    if dd:
        pos += dd
    repo = ctx.need_repo()
    if not pos:
        raise Unsupported('git fetch without a remote')
    rem = Remote(ctx, repo, pos[0])
    _no_hooks(repo, ('reference-transaction',), 'fetch')
    if rem.repo.config.get('uploadpack.hiderefs') or rem.repo.config.get('transfer.hiderefs'):
        raise Unsupported('hidden refs on the remote')
    quiet = opts.get('quiet')
    if not opts.get('no-write-fetch-head'):
        # git truncates FETCH_HEAD before it matches any refspec (probed: a missing remote ref
        # leaves it empty)
        _fetch_head(repo, rem, [])
    remote_refs = rem.refs()
    target, head_oid = rem.head()
    refmap = opts.get('refmap')
    if refmap not in (None, ''):
        raise Unsupported('fetch --refmap=%s' % refmap)
    configured_specs = repo.config.get_all('remote.%s.fetch' % rem.name) if rem.name else []
    fetched = []
    autotags = False
    cmd_specs = pos[1:]
    if cmd_specs:
        for s in cmd_specs:
            force, src, dst = _refspec(s)
            if dst:
                autotags = True
            fetched += _expand(remote_refs, force or opts.get('force'), src, dst, True, True)
        if refmap is None and rem.name:
            extra = []
            for f in fetched:
                for cs in configured_specs:
                    cforce, csrc, cdst = _refspec(cs)
                    for g in _expand_quiet(remote_refs, cforce, csrc, cdst):
                        if g.name == f.name and g.dst and g.dst != f.dst:
                            g.cmdline = False
                            extra.append(g)
            fetched += extra
    else:
        if not rem.name:
            raise Unsupported('git fetch <url> without a refspec')
        if not configured_specs:
            raise Unsupported('git fetch of a remote without fetch refspecs')
        branch = _revs.current_branch(repo)
        merge_src = None
        if branch and repo.config.get('branch.%s.remote' % branch) == rem.name:
            merge_src = repo.config.get('branch.%s.merge' % branch)
        for i, cs in enumerate(configured_specs):
            force, src, dst = _refspec(cs)
            if dst:
                autotags = True
            got = _expand(remote_refs, force or opts.get('force'), src, dst, False, True) \
                if '*' in src else _expand_quiet(remote_refs, force or opts.get('force'), src, dst)
            for g in got:
                g.cmdline = True
                if merge_src is not None:
                    g.merge = g.name == merge_src
                elif i == 0 and '*' not in src:
                    g.merge = True
            fetched += got
    # refuse to update a checked-out branch
    for f in fetched:
        if f.dst and f.dst.startswith('refs/heads/'):
            where = _checked_out(repo, f.dst)
            if where is not None:
                raise die("refusing to fetch into branch '%s' checked out at '%s'" % (f.dst, where))
    copy_closure(rem.repo, repo, [f.oid for f in fetched])
    if autotags and not opts.get('no-tags'):
        fetched += _follow_tags(rem, repo, remote_refs, fetched)
    failed = False
    for f in fetched:
        if not f.dst:
            continue
        failed |= _store(repo, f)
    if not cmd_specs:
        _follow_remote_head(repo, rem, target, head_oid, fetched)
    if not opts.get('no-write-fetch-head'):
        _fetch_head(repo, rem, [f for f in fetched if f.cmdline])
    if not quiet:
        _show_fetch(ctx, rem, fetched)
    return 1 if failed else 0


def _follow_tags(rem, repo, remote_refs, fetched):
    """Tag auto-following: a remote tag missing here whose object (peeled) is now present
    locally is fetched to the same name, listed last, not-for-merge."""
    names = {f.dst for f in fetched}
    out = []
    for name, oid in remote_refs:
        if not name.startswith('refs/tags/') or name in names or _refs.read_raw(repo, name) is not None:
            continue
        peeled = _revs.peel(rem.repo, oid)
        if peeled is None or not repo.objects.has(peeled):
            continue
        out.append(_Fetched(name, oid, name, False, False, True))
    copy_closure(rem.repo, repo, [f.oid for f in out])
    return out


def _follow_remote_head(repo, rem, target, head_oid, fetched):
    """remote.<name>.followRemoteHEAD's default ('create'), probed on git 2.5x: a fetch with
    the configured refspecs creates a missing refs/remotes/<name>/HEAD pointing at
    refs/remotes/<name>/<the remote HEAD's branch>, when that branch was fetched and its
    tracking ref exists; silent, and an existing HEAD is left alone."""
    mode = repo.config.get('remote.%s.followremotehead' % rem.name)
    if mode is not None and mode.lower() != 'create':
        raise Unsupported('fetch with remote.%s.followRemoteHEAD=%s' % (rem.name, mode))
    if repo.worktree is None:
        raise Unsupported('fetch into a bare repository with configured refspecs')
    if head_oid is None or target is None or not target.startswith('refs/heads/'):
        return
    if not any(f.name == target and f.dst for f in fetched):
        return
    name = 'refs/remotes/%s/HEAD' % rem.name
    tracking = 'refs/remotes/%s/%s' % (rem.name, target[len('refs/heads/'):])
    if _refs.read_raw(repo, name) is not None or not _refs.exists(repo, tracking):
        return
    _refs.write_symref(repo, name, tracking)


def _expand_quiet(remote_refs, force, src, dst):
    try:
        return _expand(remote_refs, force, src, dst, False, False)
    except GitExit:
        return []


def _store(repo, f):
    """Update local ref f.dst to f.oid as fetch does; True when it was rejected."""
    old = _refs.resolve(repo, f.dst)[0]
    if old == f.oid:
        return False
    if f.dst.startswith('refs/tags/') and old is not None:
        raise Unsupported('fetch that would move tag %s' % f.dst)
    if _revs.obj_type(repo, f.oid) != 'commit':
        raise Unsupported('fetch of a non-commit into %s' % f.dst)
    if old is None:
        kind = ('[new tag]' if f.name.startswith('refs/tags/') else
                '[new branch]' if f.name.startswith('refs/heads/') else '[new ref]')
        _refs.update(repo, f.dst, f.oid)
        f.line = ('*', kind, None)
        return False
    a, b = _pl.abbrev(repo, old), _pl.abbrev(repo, f.oid)
    if _revs.is_ancestor(repo, old, f.oid):
        _refs.update(repo, f.dst, f.oid)
        f.line = (' ', '%s..%s' % (a, b), None)
        return False
    if f.force:
        _refs.update(repo, f.dst, f.oid)
        f.line = ('+', '%s...%s' % (a, b), 'forced update')
        return False
    f.failed = True
    f.line = ('!', '[rejected]', 'non-fast-forward')
    return True


def _note(name, url):
    if name == 'HEAD':
        return url
    for pre, kind in (('refs/heads/', 'branch'), ('refs/tags/', 'tag'),
                      ('refs/remotes/', 'remote-tracking branch')):
        if name.startswith(pre):
            return "%s '%s' of %s" % (kind, name[len(pre):], url)
    return "'%s' of %s" % (name, url)


def _fetch_head(repo, rem, entries):
    url = rem.display()
    lines = []
    for merge in (True, False):
        for f in entries:
            if f.merge == merge:
                lines.append('%s\t%s\t%s\n' % (f.oid, '' if merge else 'not-for-merge', _note(f.name, url)))
    path = os.path.join(repo.gitdir, 'FETCH_HEAD')
    with open(path, 'wb') as fh:
        fh.write(''.join(lines).encode('utf-8'))


def _show_fetch(ctx, rem, fetched):
    width = 10
    for f in fetched:
        if f.dst and f.line is not None:
            width = max(width, len(prettify(f.name)))
    lines = []
    for f in fetched:
        if f.dst is None:
            kind = 'branch'
            what = f.name
            for pre, k in (('refs/heads/', 'branch'), ('refs/tags/', 'tag'),
                           ('refs/remotes/', 'remote-tracking branch')):
                if f.name.startswith(pre):
                    kind, what = k, f.name[len(pre):]
                    break
            lines.append(' * %-*s %-*s -> FETCH_HEAD' % (SUMMARY_WIDTH, kind, width, what))
        elif f.line is not None:
            flag, summary, msg = f.line
            if f.failed:
                raise Unsupported('the fetch status line of a rejected update')
            line = ' %s %-*s %-*s -> %s' % (flag, SUMMARY_WIDTH, summary, width, prettify(f.name),
                                           prettify(f.dst))
            if msg:
                line += '  (%s)' % msg
            lines.append(line)
    if lines:
        ctx.warn('From %s\n' % rem.display())
        ctx.warn(''.join(l + '\n' for l in lines))


# -- clone ------------------------------------------------------------------------------------

_CLONE = _u.Spec('clone', flags='q|quiet', values='b|branch')


@command('clone')
def clone(ctx, args):
    opts, pos, dd = _u.parse(_CLONE, args)
    if dd:
        pos += dd
    if not opts.get('quiet'):
        raise Unsupported('git clone without -q (progress output)')
    if len(pos) != 2:
        raise Unsupported('git clone with %d positionals' % len(pos))
    src, dst = pos
    if not os.path.isabs(src):
        raise Unsupported('git clone of a relative path')
    gd = open_dir(os.path.normpath(_local_path(src, ctx.cwd)))
    if gd is None:
        raise _no_such_repo(src)
    dest = os.path.normpath(os.path.join(ctx.cwd, dst))
    if os.path.exists(dest) and (not os.path.isdir(dest) or os.listdir(dest)):
        raise die("destination path '%s' already exists and is not an empty directory." % dst)
    srepo = Repo(os.path.realpath(gd), None, ctx.env, ctx.cli_config)
    _no_receive_config(srepo)
    if srepo.objects.alternates():
        raise Unsupported('git clone of a repository with alternates')
    head_raw = _refs.read_raw(srepo, 'HEAD')
    if not head_raw or head_raw[0] != 'sym':
        raise Unsupported('git clone of a detached HEAD')
    head_branch = head_raw[1]
    branch_ref = 'refs/heads/' + opts['branch'] if opts.get('branch') else head_branch
    branches = _refs.list_refs(srepo, 'refs/heads/')
    tags = _refs.list_refs(srepo, 'refs/tags/')
    oid = dict(branches).get(branch_ref)
    if oid is None and opts.get('branch'):
        raise Unsupported('git clone -b of a branch the remote does not have')
    for _n, t in tags:
        if _revs.obj_type(srepo, t) == 'tag':
            raise Unsupported('git clone of annotated tags')
    from . import Ctx
    sub = Ctx(ctx.cwd, ctx.env, None)
    sub.cli_config = list(ctx.cli_config)
    name = branch_ref[len('refs/heads/'):]
    _pl.init(sub, ['-q', '-b', name, dest])
    if sub.err:
        raise Unsupported('git clone: init said %s' % ''.join(sub.err))
    gitdir = os.path.join(dest, '.git')
    repo = Repo(gitdir, dest, ctx.env, ctx.cli_config)
    if repo.algo != srepo.algo:
        raise Unsupported('git clone across object formats')
    sobj = os.path.join(srepo.commondir, 'objects')
    dobj = os.path.join(gitdir, 'objects')
    for dirpath, _dirs, files in os.walk(sobj):
        rel = os.path.relpath(dirpath, sobj)
        if rel.split(os.sep)[0] == 'info':
            continue
        for f in files:
            if f.endswith('.lock') or f.startswith('tmp'):
                continue
            os.makedirs(os.path.join(dobj, rel), exist_ok=True)
            shutil.copyfile(os.path.join(dirpath, f), os.path.join(dobj, rel, f))
    cfg = os.path.join(gitdir, 'config')
    _config.set_value(cfg, 'remote.origin.url', src)
    _config.set_value(cfg, 'remote.origin.fetch', '+refs/heads/*:refs/remotes/origin/*')
    _config.set_value(cfg, 'branch.%s.remote' % name, 'origin')
    _config.set_value(cfg, 'branch.%s.merge' % name, branch_ref)
    repo.reload_config()
    _write_packed(gitdir, branches, tags)
    if oid is None:
        # the remote HEAD names a branch it does not have: git fetches, configures that branch,
        # leaves HEAD unborn on it and checks nothing out (checked against real git, 2026-10-10)
        ctx.warn('warning: remote HEAD refers to nonexistent ref, unable to checkout\n')
        return 0
    _refs.write_symref(repo, 'refs/remotes/origin/HEAD',
                       'refs/remotes/origin/' + head_branch[len('refs/heads/'):])
    _refs.update(repo, branch_ref, oid)
    from . import wt as _wt
    from . import wtcmds as _wc
    if repo.hook_exists('post-checkout'):
        raise Unsupported('git clone with a post-checkout hook')
    ix = _index.Index(repo)
    lock = _index.IndexLock(ix.path).acquire()
    try:
        tree = _wt.Tree(repo, ix)
        _wt.twoway(tree, {}, _wc.commit_side(repo, oid), reset=True)
        ix.write(lock)
    except BaseException:
        lock.rollback()
        raise
    return 0


def _write_packed(gitdir, branches, tags):
    """A fresh clone's packed-refs: the remote's branches as origin/*, and its tags."""
    packed = ['# pack-refs with: peeled fully-peeled sorted \n']
    entries = [('refs/remotes/origin/' + n[len('refs/heads/'):], o) for n, o in branches] + list(tags)
    for n, o in sorted(entries):
        packed.append('%s %s\n' % (o, n))
    with open(os.path.join(gitdir, 'packed-refs'), 'wb') as fh:
        fh.write(''.join(packed).encode('utf-8'))


__all__ = ['Remote', 'copy_closure', 'open_dir', 'configured', 'prettify', 'slash']
