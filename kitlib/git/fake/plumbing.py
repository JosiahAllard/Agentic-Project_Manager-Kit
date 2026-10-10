"""Plumbing and ref commands: rev-parse, config, symbolic-ref, update-ref, for-each-ref,
check-ref-format, var, hash-object, cat-file, ls-tree, write-tree, read-tree, commit-tree,
merge-base, branch, tag, init."""
import os
import re
import shutil

from . import command
from . import config as _config
from . import index as _index
from . import refs as _refs
from . import revs as _revs
from . import util as _u
from .errors import GitExit, Unsupported, die
from .objects import EMPTY_TREE, Commit, build_tree, parse_tree
from .repo import slash

ZERO = '0' * 40


def is_zero(s):
    return bool(s) and set(s) == {'0'}


def index_reader(repo):
    """For `:path` revisions: the oid of an index entry."""
    state = {}

    def read(path, stage):
        if 'ix' not in state:
            state['ix'] = _index.Index(repo).load()
        e = state['ix'].get(path, stage)
        return e.oid if e else None
    return read


def resolve(ctx, spec):
    """oid for a revision, None when it does not resolve; ambiguity is Unsupported."""
    try:
        return _revs.get_oid(ctx.repo, spec, index_reader(ctx.repo))
    except _revs.Ambiguous:
        raise Unsupported('ambiguous short object name %s' % spec)


def abbrev(repo, oid, n=None):
    """git's find_unique_abbrev with the default length (7, longer when needed)."""
    if n is None:
        v = repo.config.get('core.abbrev')
        n = 7
        if v is not None and v.strip().lower() not in ('auto',):
            if v.strip().lower() in ('no', 'false', 'off'):
                return oid
            n = max(4, _config.parse_int(v, 'core.abbrev'))
        else:
            count = len(repo.objects.all_oids())
            if count:
                bits = count.bit_length()
                n = max(7, (bits + 1) // 2)
    while n < len(oid) and len(repo.objects.prefix_matches(oid[:n])) > 1:
        n += 1
    return oid[:n]


# -- rev-parse ------------------------------------------------------------------------------


def _path_out(ctx, path, absolute, rel_default=True):
    path = os.path.normpath(path)
    if absolute or not rel_default:
        return slash(os.path.realpath(path) if os.path.exists(path) else path)
    rel = os.path.relpath(path, ctx.cwd)
    if rel.startswith('..') or os.path.isabs(rel):
        return slash(path)
    return slash(rel)


def _gitdir_out(ctx, repo, absolute):
    """--git-dir: '.git' at the top, else absolute (setup's own rule)."""
    if absolute:
        return slash(os.path.realpath(repo.gitdir))
    if getattr(repo, 'inside_gitdir', False):
        if os.path.normcase(os.path.realpath(ctx.cwd)) == os.path.normcase(repo.gitdir):
            return '.'
        return slash(repo.gitdir)
    if repo.worktree and not repo.linked and not repo.gitdir_from_env:
        if os.path.normcase(os.path.realpath(ctx.cwd)) == os.path.normcase(repo.worktree):
            return '.git'
    if getattr(repo, 'git_dir_arg', None) is not None:
        return repo.git_dir_arg.replace('\\', '/')
    return slash(repo.gitdir)


def _common_out(ctx, repo, absolute):
    if absolute:
        return slash(os.path.realpath(repo.commondir))
    if not repo.linked:
        return _gitdir_out(ctx, repo, False)
    rel = os.path.relpath(repo.commondir, ctx.cwd)
    return slash(rel)


@command('rev-parse')
def rev_parse(ctx, args):
    out = []
    verify = quiet = False
    short = None
    abbrev_ref = False
    symbolic_full = False
    absolute = False
    revs = []
    i = 0
    seen_dashdash = False
    while i < len(args):
        a = args[i]
        if seen_dashdash:
            out.append(a)
            i += 1
            continue
        if a == '--':
            seen_dashdash = True
            i += 1
            continue
        if a == '--verify':
            verify = True
        elif a in ('-q', '--quiet'):
            quiet = True
        elif a == '--short' or a.startswith('--short='):
            short = int(a.split('=', 1)[1]) if '=' in a else 0
        elif a == '--abbrev-ref' or a == '--abbrev-ref=strict' or a == '--abbrev-ref=loose':
            abbrev_ref = True
        elif a == '--symbolic-full-name':
            symbolic_full = True
        elif a.startswith('--path-format='):
            v = a.split('=', 1)[1]
            if v not in ('absolute', 'relative'):
                raise die("unknown argument to --path-format: %s" % v)
            absolute = v == 'absolute'
        elif a == '--git-dir':
            repo = ctx.need_repo()
            out.append(_gitdir_out(ctx, repo, absolute))
        elif a == '--absolute-git-dir':
            out.append(slash(os.path.realpath(ctx.need_repo().gitdir)))
        elif a == '--git-common-dir':
            repo = ctx.need_repo()
            out.append(_common_out(ctx, repo, absolute))
        elif a == '--show-toplevel':
            repo = ctx.need_repo()
            if repo.worktree is None or getattr(repo, 'inside_gitdir', False):
                raise die('this operation must be run in a work tree')
            out.append(slash(repo.worktree))
        elif a == '--show-prefix':
            ctx.need_repo()
            out.append(ctx.prefix or '')
        elif a == '--show-cdup':
            ctx.need_repo()
            p = ctx.prefix or ''
            out.append('../' * p.count('/'))
        elif a == '--is-inside-work-tree':
            repo = ctx.need_repo()
            inside = repo.worktree is not None and not getattr(repo, 'inside_gitdir', False) and ctx.prefix is not None
            out.append('true' if inside else 'false')
        elif a == '--is-inside-git-dir':
            repo = ctx.need_repo()
            out.append('true' if getattr(repo, 'inside_gitdir', False) else 'false')
        elif a == '--is-bare-repository':
            repo = ctx.need_repo()
            out.append('true' if repo.bare else 'false')
        elif a == '--is-shallow-repository':
            repo = ctx.need_repo()
            out.append('true' if os.path.exists(os.path.join(repo.commondir, 'shallow')) else 'false')
        elif a == '--show-ref-format':
            ctx.need_repo()
            out.append('files')
        elif a == '--show-object-format' or a == '--show-object-format=storage':
            out.append(ctx.need_repo().algo)
        elif a == '--git-path':
            repo = ctx.need_repo()
            i += 1
            p = repo.git_path(args[i])
            if absolute:
                out.append(slash(os.path.normpath(p)))
            else:
                out.append(_path_out(ctx, p, False))
        elif a == '--local-env-vars':
            raise Unsupported('rev-parse --local-env-vars')
        elif a.startswith('-') and a not in ('-',):
            raise Unsupported('git rev-parse %s' % a)
        else:
            revs.append(a)
        i += 1
    if verify:
        repo = ctx.need_repo()
        if len(revs) != 1:
            if quiet:
                return 1
            raise die('Needed a single revision')
        oid = resolve(ctx, revs[0])
        if oid is None:
            if quiet:
                return 1
            raise die('Needed a single revision')
        out.append(_show_rev(ctx, repo, revs[0], oid, short, abbrev_ref, symbolic_full))
    else:
        for r in revs:
            repo = ctx.need_repo()
            neg = r.startswith('^')
            spec = r[1:] if neg else r
            if '..' in spec and not neg:
                a, _, b = spec.partition('...') if '...' in spec else spec.partition('..')
                raise Unsupported('rev-parse range %s' % r)
            oid = resolve(ctx, spec)
            if oid is None:
                if repo.worktree and os.path.exists(os.path.join(ctx.cwd, r)):
                    out.append(r)
                    continue
                raise GitExit(128, "fatal: ambiguous argument '%s': unknown revision or path not in "
                                   "the working tree.\nUse '--' to separate paths from revisions, like "
                                   "this:\n'git <command> [<revision>...] -- [<file>...]'\n" % r)
            out.append(('^' if neg else '') + _show_rev(ctx, repo, spec, oid, short, abbrev_ref, symbolic_full))
    for line in out:
        ctx.write(line + '\n')
    return 0


def _show_rev(ctx, repo, spec, oid, short, abbrev_ref, symbolic_full):
    if abbrev_ref or symbolic_full:
        full = _full_name(repo, spec)
        if full is None:
            if abbrev_ref and spec in ('HEAD', '@'):
                return 'HEAD'
            return '' if not abbrev_ref else spec
        if symbolic_full:
            return full
        return shorten_ref(repo, full)
    if short is not None:
        return abbrev(repo, oid, short if short else None)
    return oid


def _full_name(repo, spec):
    """The full ref a name means (HEAD followed to its branch), or None."""
    if re.search(r'[~^:]', spec) or '@{' in spec:
        return None
    if spec in ('HEAD', '@'):
        t = _refs.head_target(repo)
        if t is None:
            return None
        return t
    found = _refs.dwim(repo, spec)
    if not found:
        return None
    if len(found) > 1:
        raise Unsupported('ambiguous ref name %s' % spec)
    return found[0]


def shorten_ref(repo, full):
    """git's shorten_unambiguous_ref (strict off)."""
    for pre in ('refs/heads/', 'refs/tags/', 'refs/remotes/'):
        if full.startswith(pre):
            short = full[len(pre):]
            if pre == 'refs/remotes/' and short.endswith('/HEAD'):
                short = short[:-len('/HEAD')]
            others = [r for r in _refs.dwim(repo, short) if r != full]
            if others and pre != 'refs/heads/':
                return full[len('refs/'):] if not others else full
            return short
    if full.startswith('refs/'):
        return full[len('refs/'):]
    return full


# -- config ---------------------------------------------------------------------------------

_CFG = _u.Spec('config', flags='global local system get get-all get-regexp add unset unset-all '
                              'replace-all bool int list|l null|z show-origin show-scope '
                              'name-only remove-section rename-section path',
               values='file|f type default')


@command('config')
def config_cmd(ctx, args):
    if args and args[0] in ('get', 'set', 'unset', 'list', 'rename-section', 'remove-section', 'edit'):
        raise Unsupported('git config %s' % args[0])
    opts, pos, dd = _u.parse(_CFG, args)
    if dd:
        pos += dd
    if opts.get('system'):
        raise Unsupported('git config --system')
    t = opts.get('type')
    if opts.get('bool'):
        t = 'bool'
    if opts.get('int'):
        t = 'int'
    if t not in (None, 'bool', 'int', 'path'):
        raise Unsupported('git config --type=%s' % t)
    if opts.get('path'):
        t = 'path'
    z = opts.get('null')
    eol = '\0' if z else '\n'
    repo = ctx.repo
    target = None
    if opts.get('file'):
        target = os.path.join(ctx.cwd, opts['file'])
    elif opts.get('global'):
        g = _config.global_files(ctx.env)
        target = g[-1] if not ctx.env.get('GIT_CONFIG_GLOBAL') else g[0]
        if ctx.env.get('GIT_CONFIG_GLOBAL') is None:
            # git writes ~/.gitconfig unless only the XDG file exists
            if not os.path.exists(g[1]) and os.path.exists(g[0]):
                target = g[0]
    elif opts.get('local') or True:
        if repo is not None:
            target = os.path.join(repo.commondir, 'config')
    writing = (opts.get('add') or opts.get('unset') or opts.get('unset-all') or opts.get('replace-all')
               or opts.get('remove-section') or opts.get('rename-section')
               or (len(pos) >= 2 and not any(opts.get(k) for k in ('get', 'get-all', 'get-regexp'))))
    reading_file_only = opts.get('file') or opts.get('global') or opts.get('local')
    if writing:
        if target is None:
            raise GitExit(128, 'fatal: not in a git directory\n')
        if os.path.normcase(os.path.abspath(target)) == os.path.normcase(os.path.abspath(os.devnull)) or \
                target.lower() in ('nul', '/dev/null'):
            raise Unsupported('git config writing to the null device')
        if opts.get('remove-section'):
            raise Unsupported('git config --remove-section')
        if opts.get('rename-section'):
            raise Unsupported('git config --rename-section')
        if opts.get('unset') or opts.get('unset-all'):
            if not pos:
                raise GitExit(129, 'error: wrong number of arguments, should be from 1 to 2\n')
            _config.unset(target, pos[0], all_=bool(opts.get('unset-all')),
                          value_re=pos[1] if len(pos) > 1 else None)
            _after_write(ctx)
            return 0
        if len(pos) < 2:
            raise GitExit(129, 'error: wrong number of arguments, should be 2\n')
        value = pos[1]
        if t == 'bool':
            value = 'true' if _config.parse_bool(value, pos[0]) else 'false'
        _config.set_value(target, pos[0], value, add=bool(opts.get('add')),
                          replace_all=bool(opts.get('replace-all')),
                          value_re=pos[2] if len(pos) > 2 else None)
        _after_write(ctx)
        return 0
    # reading
    if opts.get('list'):
        pairs = _read_pairs(ctx, target if reading_file_only else None)
        for k, v in pairs:
            ctx.write('%s=%s%s' % (k, v, eol) if v is not None else '%s%s' % (k, eol))
        return 0
    if not pos:
        raise GitExit(129, 'error: wrong number of arguments\n')
    if opts.get('get-regexp'):
        try:
            rx = re.compile(pos[0], re.I)
        except re.error:
            raise GitExit(6, 'error: invalid key pattern: %s\n' % pos[0])
        vrx = re.compile(pos[1]) if len(pos) > 1 else None
        found = False
        for k, v in _read_pairs(ctx, target if reading_file_only else None):
            if rx.search(k) and (vrx is None or (v is not None and vrx.search(v))):
                found = True
                if opts.get('name-only'):
                    ctx.write(k + eol)
                else:
                    ctx.write('%s%s%s%s' % (k, '\n' if z else ' ', _typed(v, t, k), eol) if v is not None
                              else k + eol)
        return 0 if found else 1
    try:
        key = _config.canon_key(pos[0])
    except GitExit:
        raise GitExit(1, 'error: key does not contain a section: %s\n' % pos[0]) if '.' not in pos[0] \
            else GitExit(1, 'error: invalid key: %s\n' % pos[0])
    vrx = re.compile(pos[1]) if len(pos) > 1 else None
    vals = [v for k, v in _read_pairs(ctx, target if reading_file_only else None)
            if k == key and (vrx is None or (v is not None and vrx.search(v)))]
    if not vals:
        return 1
    if opts.get('get-all'):
        for v in vals:
            ctx.write(_typed(v, t, key) + eol)
    else:
        ctx.write(_typed(vals[-1], t, key) + eol)
    return 0


def _typed(v, t, key):
    if t == 'bool':
        return 'true' if _config.parse_bool(v, key) else 'false'
    if t == 'int':
        return str(_config.parse_int(v, key))
    if v is None:
        return 'true' if t == 'bool' else ''
    if t == 'path' and v.startswith('~'):
        return os.path.expanduser(v).replace('\\', '/')
    return v


def _read_pairs(ctx, only_file):
    if only_file is not None:
        return _config.read_file(only_file)
    repo = ctx.repo
    if repo is not None:
        return list(repo.config.items())
    layers = []
    for p in _config.system_files(ctx.env):
        layers += _config.read_file(p)
    for p in _config.global_files(ctx.env):
        layers += _config.read_file(p)
    layers += _config.env_pairs(ctx.env)
    layers += list(ctx.cli_config)
    return layers


def _after_write(ctx):
    if ctx.repo is not None:
        ctx.repo.reload_config()


# -- refs -----------------------------------------------------------------------------------

_SYM = _u.Spec('symbolic-ref', flags='q|quiet short d|delete no-recurse recurse', values='m')


@command('symbolic-ref')
def symbolic_ref(ctx, args):
    opts, pos, dd = _u.parse(_SYM, args)
    repo = ctx.need_repo()
    if opts.get('delete'):
        raise Unsupported('symbolic-ref -d')
    if len(pos) == 2:
        name, target = pos
        if not target.startswith('refs/'):
            raise die('Refusing to point %s outside of refs/' % name)
        if not _refs.check_refname(target):
            raise die('Refusing to set %s to invalid ref %s' % (name, target))
        _refs.write_symref(repo, name, target)
        return 0
    if len(pos) != 1:
        raise GitExit(129, 'usage: git symbolic-ref [-m <reason>] <name> <ref>\n')
    raw = _refs.read_raw(repo, pos[0])
    if raw is None or raw[0] != 'sym':
        if opts.get('quiet'):
            return 1
        if raw is None:
            raise die('No such ref: %s' % pos[0])
        raise die('ref %s is not a symbolic ref' % pos[0])
    target = raw[1]
    if not opts.get('no-recurse'):
        depth = 0
        while depth < 5:
            nxt = _refs.read_raw(repo, target)
            if nxt is None or nxt[0] != 'sym':
                break
            target = nxt[1]
            depth += 1
    ctx.write((shorten_ref(repo, target) if opts.get('short') else target) + '\n')
    return 0


_UREF = _u.Spec('update-ref', flags='d no-deref create-reflog stdin z', values='m')


@command('update-ref')
def update_ref(ctx, args):
    opts, pos, dd = _u.parse(_UREF, args)
    if dd:
        pos += dd
    if opts.get('stdin'):
        raise Unsupported('update-ref --stdin')
    repo = ctx.need_repo()
    deref = not opts.get('no-deref')
    if opts.get('d'):
        if not pos or len(pos) > 2:
            raise GitExit(129, 'usage: git update-ref -d <refname> [<old-oid>]\n')
        name = pos[0]
        old = None
        if len(pos) == 2:
            old = _oid_arg(ctx, pos[1], allow_zero=True)
            if old is not None and is_zero(old):
                old = ''
        try:
            _refs.delete(repo, name, old, deref=deref)
        except GitExit as e:
            raise GitExit(e.rc, _wrap_update(e.err, name, 'delete_ref'))
        return 0
    if len(pos) not in (2, 3):
        raise GitExit(129, 'usage: git update-ref [<options>] <refname> <new-oid> [<old-oid>]\n')
    name = pos[0]
    new = _oid_arg(ctx, pos[1], allow_zero=True)
    old = None
    if len(pos) == 3:
        old = _oid_arg(ctx, pos[2], allow_zero=True) if pos[2] != '' else ''
        if old is not None and is_zero(old):
            old = ''
    if is_zero(new):
        try:
            _refs.delete(repo, name, old, deref=deref)
        except GitExit as e:
            raise GitExit(e.rc, _wrap_update(e.err, name, 'delete_ref'))
        return 0
    try:
        _refs.update(repo, name, new, old=old, deref=deref)
    except GitExit as e:
        raise GitExit(e.rc, _wrap_update(e.err, name, 'update_ref'))
    return 0


def _wrap_update(err, name, what):
    if err.startswith('fatal: ') and 'update_ref failed' not in err:
        return 'fatal: %s failed for ref \'%s\': %s' % (what, name, err[len('fatal: '):])
    return err


def _oid_arg(ctx, spec, allow_zero=False):
    if allow_zero and is_zero(spec) and len(spec) >= 4:
        return spec
    oid = resolve(ctx, spec)
    if oid is None:
        raise die('%s: not a valid SHA1' % spec)
    return oid


_FER = _u.Spec('for-each-ref', flags='python shell perl tcl ignore-case', values='format sort count '
               'points-at merged no-merged contains no-contains', multi='exclude')


@command('for-each-ref')
def for_each_ref(ctx, args):
    opts, pos, dd = _u.parse(_FER, args)
    if dd:
        pos += dd
    for k in ('sort', 'count', 'points-at', 'merged', 'no-merged', 'contains', 'no-contains', 'exclude',
              'python', 'shell', 'perl', 'tcl', 'ignore-case'):
        if opts.get(k):
            raise Unsupported('for-each-ref --%s' % k)
    repo = ctx.need_repo()
    fmt = opts.get('format', '%(objectname) %(objecttype)\t%(refname)')
    for name, oid in _matching_refs(repo, pos):
        ctx.write(format_ref(repo, fmt, name, oid) + '\n')
    return 0


def _matching_refs(repo, patterns):
    allrefs = _refs.list_refs(repo, 'refs/')
    if not patterns:
        return allrefs
    out = []
    from .ignore import wildmatch
    for name, oid in allrefs:
        for p in patterns:
            if name == p or (name.startswith(p) and (p.endswith('/') or name[len(p)] == '/')):
                out.append((name, oid))
                break
            if any(c in p for c in '*?[') and wildmatch(p, name, pathname=True):
                out.append((name, oid))
                break
    return out


_ATOM = re.compile(r'%\(([^)]*)\)|%([0-9a-fA-F]{2})|%%')


def format_ref(repo, fmt, name, oid):
    def sub(m):
        if m.group(0) == '%%':
            return '%'
        if m.group(2):
            return chr(int(m.group(2), 16))
        atom = m.group(1)
        if atom == 'refname':
            return name
        if atom == 'refname:short':
            return shorten_ref(repo, name)
        if atom == 'objectname':
            return oid
        if atom == 'objectname:short':
            return abbrev(repo, oid)
        if atom == 'objecttype':
            return _revs.obj_type(repo, oid) or ''
        if atom == 'HEAD':
            return '*' if _refs.head_target(repo) == name else ' '
        if atom in ('upstream', 'upstream:short'):
            if not name.startswith('refs/heads/'):
                return ''
            try:
                up = _revs.upstream_of(repo, name[len('refs/heads/'):])
            except GitExit:
                return ''
            return shorten_ref(repo, up) if atom.endswith(':short') else up
        if atom == 'symref':
            raw = _refs.read_raw(repo, name)
            return raw[1] if raw and raw[0] == 'sym' else ''
        if atom in ('subject', 'contents:subject'):
            c = _revs.commit(repo, _revs.peel(repo, oid, 'commit') or oid)
            if c is None:
                return ''
            return c.message.decode('utf-8', 'replace').split('\n\n', 1)[0].replace('\n', ' ').strip()
        if atom in ('committerdate:unix', 'creatordate:unix'):
            c = _revs.commit(repo, _revs.peel(repo, oid, 'commit') or oid)
            return str(c.commit_time) if c else ''
        raise Unsupported('for-each-ref atom %%(%s)' % atom)
    return _ATOM.sub(sub, fmt)


@command('check-ref-format')
def check_ref_format(ctx, args):
    opts, pos, _ = _u.parse(_u.Spec('check-ref-format', flags='branch allow-onelevel normalize '
                                    'refspec-pattern no-allow-onelevel'), args)
    if opts.get('branch'):
        if len(pos) != 1:
            raise GitExit(129, 'usage: git check-ref-format --branch <branchname-shorthand>\n')
        name = pos[0]
        if name.startswith('@{-') or name == '-':
            raise Unsupported('check-ref-format --branch %s' % name)
        if name.startswith('-') or name == 'HEAD' or not _refs.check_refname('refs/heads/' + name):
            raise die("'%s' is not a valid branch name" % name)
        ctx.write(name + '\n')
        return 0
    if opts.get('normalize') or opts.get('refspec-pattern'):
        raise Unsupported('check-ref-format options')
    if len(pos) != 1:
        raise GitExit(129, 'usage: git check-ref-format [<options>] <refname>\n')
    return 0 if _refs.check_refname(pos[0], allow_onelevel=bool(opts.get('allow-onelevel'))) else 1


@command('var')
def var(ctx, args):
    if len(args) != 1:
        raise Unsupported('git var %s' % ' '.join(args))
    if args[0] == 'GIT_AUTHOR_IDENT':
        ctx.write(_u.ident(_IdentCtx(ctx), 'author') + '\n')
    elif args[0] == 'GIT_COMMITTER_IDENT':
        ctx.write(_u.ident(_IdentCtx(ctx), 'committer') + '\n')
    else:
        raise Unsupported('git var %s' % args[0])
    return 0


class _IdentCtx:
    """An ident context that works outside a repository too."""

    def __init__(self, ctx):
        self.env = ctx.env
        r = ctx.repo
        if r is None:
            class R:
                pass
            r = R()
            layers = []
            for p in _config.system_files(ctx.env):
                layers.append(('system', _config.read_file(p)))
            for p in _config.global_files(ctx.env):
                layers.append(('global', _config.read_file(p)))
            layers.append(('command', _config.env_pairs(ctx.env)))
            layers.append(('command', list(ctx.cli_config)))
            r.config = _config.Config(layers)
        self.repo = r


# -- objects --------------------------------------------------------------------------------

_HASHOBJ = _u.Spec('hash-object', flags='w stdin no-filters literally stdin-paths', values='t path')


@command('hash-object')
def hash_object(ctx, args):
    opts, pos, dd = _u.parse(_HASHOBJ, args)
    if dd:
        pos += dd
    if opts.get('literally') or opts.get('stdin-paths') or opts.get('path'):
        raise Unsupported('hash-object options')
    kind = opts.get('t', 'blob')
    if kind not in ('blob', 'commit', 'tree', 'tag'):
        raise die('invalid object type "%s"' % kind)
    repo = ctx.repo
    if opts.get('w'):
        repo = ctx.need_repo()
    datas = []
    if opts.get('stdin'):
        datas.append(ctx.stdin_bytes())
    for p in pos:
        with open(os.path.join(ctx.cwd, p), 'rb') as f:
            data = f.read()
        if not opts.get('no-filters') and kind == 'blob' and repo is not None and repo.worktree:
            from . import wt as _wt
            conv = _wt.Convert(repo)
            conv.guard()
            data = conv.to_git(p, data)
        datas.append(data)
    from .objects import hash_object as _ho
    for data in datas:
        if kind != 'blob':
            raise Unsupported('hash-object -t %s' % kind)
        if opts.get('w'):
            oid = repo.objects.write(kind, data)
        else:
            oid = _ho(repo.algo if repo else 'sha1', kind, data)
        ctx.write(oid + '\n')
    return 0


_CATFILE = _u.Spec('cat-file', flags='e t s p batch batch-check z Z buffer allow-unknown-type '
                   'follow-symlinks batch-all-objects textconv filters', optional='')


@command('cat-file')
def cat_file(ctx, args):
    batch = None
    rest = []
    z = False
    for a in args:
        if a in ('--batch', '--batch-check'):
            batch = a[2:]
        elif a.startswith('--batch=') or a.startswith('--batch-check='):
            raise Unsupported('cat-file %s' % a)
        elif a == '-z':
            z = True
        elif a == '--buffer':
            pass
        else:
            rest.append(a)
    repo = ctx.need_repo()
    if batch:
        if rest:
            raise Unsupported('cat-file %s' % ' '.join(rest))
        data = ctx.stdin_bytes()
        if z:
            items = data.split(b'\0')
            if items and items[-1] == b'':
                items.pop()
        else:
            items = data.split(b'\n')
            if items and items[-1] == b'':
                items.pop()
        rd = index_reader(repo)
        for raw in items:
            spec = raw.decode('utf-8', 'surrogateescape')
            if not z:
                spec = spec.rstrip('\r') if False else spec
            try:
                oid = _revs.get_oid(repo, spec, rd) if spec else None
            except _revs.Ambiguous:
                ctx.write(raw + b' ambiguous\n')
                continue
            got = repo.objects.read(oid) if oid else None
            if got is None:
                ctx.write(raw + b' missing\n')
                continue
            kind, body = got
            ctx.write(('%s %s %d\n' % (oid, kind, len(body))).encode('ascii'))
            if batch == 'batch':
                ctx.write(body)
                ctx.write(b'\n')
        return 0
    if len(rest) == 2 and rest[0] in ('-e', '-t', '-s', '-p'):
        flag, spec = rest
        oid = resolve(ctx, spec)
        if oid is None:
            raise die('Not a valid object name %s' % spec)
        got = repo.objects.read(oid)
        if flag == '-e':
            return 0 if got is not None else 1
        if got is None:
            raise die('git cat-file %s: bad file' % spec)
        kind, body = got
        if flag == '-t':
            ctx.write(kind + '\n')
        elif flag == '-s':
            ctx.write('%d\n' % len(body))
        else:
            if kind == 'tree':
                for mode, name, o in parse_tree(body, repo.hexlen):
                    t = 'tree' if mode == '40000' else ('commit' if mode == '160000' else 'blob')
                    ctx.write(('%06d %s %s\t' % (int(mode), t, o)).encode('ascii') + name + b'\n')
            else:
                ctx.write(body)
        return 0
    if len(rest) == 2 and not rest[0].startswith('-'):
        want, spec = rest
        oid = resolve(ctx, spec)
        if oid is None:
            raise die('Not a valid object name %s' % spec)
        target = _revs.peel(repo, oid, want) if want != 'tag' else oid
        got = repo.objects.read(target) if target else None
        if got is None or got[0] != want:
            raise die('git cat-file %s: bad file' % spec)
        ctx.write(got[1])
        return 0
    raise Unsupported('cat-file %s' % ' '.join(args))


_LSTREE = _u.Spec('ls-tree', flags='r t d l|long name-only name-status z full-name full-tree '
                  'object-only', optional='abbrev', values='format')


@command('ls-tree')
def ls_tree(ctx, args):
    opts, pos, dd = _u.parse(_LSTREE, args)
    if opts.get('format') or opts.get('abbrev') or opts.get('object-only') or opts.get('d'):
        raise Unsupported('ls-tree options')
    repo = ctx.need_repo()
    if not pos:
        raise GitExit(129, 'usage: git ls-tree [<options>] <tree-ish> [<path>...]\n')
    spec = pos[0]
    paths = pos[1:] + (dd or [])
    oid = resolve(ctx, spec)
    if oid is None:
        raise die('Not a valid object name %s' % spec)
    tree = _revs.peel(repo, oid, 'tree')
    if tree is None:
        raise die('not a tree object')
    full_tree = opts.get('full-tree')
    prefix = '' if full_tree else (ctx.prefix or '')
    full_name = opts.get('full-name') or full_tree
    specs = []
    for p in paths:
        p = p.replace('\\', '/')
        specs.append(_u._normalize(prefix + p, p) + ('/' if p.endswith('/') and p.strip('/') else ''))
    if not specs and prefix:
        specs = [prefix]
    recurse = opts.get('r')
    show_trees = opts.get('t') or not recurse
    z = opts.get('z')
    name_only = opts.get('name-only') or opts.get('name-status')
    long_ = opts.get('long')
    qp = _u.quotepath(repo)

    def wants_inside(path):
        for s in specs:
            if s.startswith(path + '/') and len(s) > len(path) + 1:
                return True
        return False

    def matches(path):
        if not specs:
            return True
        for s in specs:
            st = s.rstrip('/')
            if s == '' or path == st or path.startswith(st + '/') if st else True:
                if s.endswith('/') and path == st:
                    continue
                return True
        return False

    def emit(mode, kind, o, path):
        shown = path if full_name else _u.rel_to_prefix(path, ctx.prefix or '')
        if z:
            name = shown.encode('utf-8', 'surrogateescape')
        else:
            name = _u.cquote(shown, qp).encode('utf-8', 'surrogateescape')
        if name_only:
            ctx.write(name + (b'\0' if z else b'\n'))
            return
        m = '%06o' % int(mode, 8)
        if long_:
            if kind == 'blob':
                got = repo.objects.read(o)
                size = '%7d' % (len(got[1]) if got else 0)
            else:
                size = '%7s' % '-'
            head = '%s %s %s %s\t' % (m, kind, o, size)
        else:
            head = '%s %s %s\t' % (m, kind, o)
        ctx.write(head.encode('ascii') + name + (b'\0' if z else b'\n'))

    def walk(t, base):
        ents = _revs.tree_entries(repo, t)
        for mode, name, o in sorted(ents, key=lambda e: _tree_order(e)):
            path = base + name.decode('utf-8', 'surrogateescape')
            if mode == '40000':
                inside = recurse or wants_inside(path)
                if inside and (recurse and (matches(path) or wants_inside(path)) or wants_inside(path)):
                    if opts.get('t') and matches(path):
                        emit(mode, 'tree', o, path)
                    walk(o, path + '/')
                    continue
                if matches(path) and show_trees:
                    emit(mode, 'tree', o, path)
                continue
            if mode == '160000':
                if matches(path):
                    emit(mode, 'commit', o, path)
                continue
            if matches(path):
                emit(mode, 'blob', o, path)
    walk(tree, '')
    return 0


def _tree_order(e):
    mode, name, _o = e
    return name + b'/' if mode == '40000' else name


# -- index to tree and back -----------------------------------------------------------------


def write_tree_from_index(repo, ix, missing_ok=False):
    """Write the trees for a stage-0 index; returns the root tree oid. Raises GitExit when an
    entry is unmerged."""
    unmerged = ix.unmerged()
    if unmerged:
        lines = ''.join('error: %s: unmerged (%s)\n' % (p, ix.stages(p)[min(ix.stages(p))].oid)
                        for p in unmerged)
        raise GitExit(128, lines + 'fatal: git-write-tree: error building trees\n')
    root = {}
    for e in ix.sorted_entries():
        if e.intent_to_add:
            continue
        parts = e.path.split('/')
        d = root
        for p in parts[:-1]:
            d = d.setdefault(p, {})
        d[parts[-1]] = e
    return _write_dir(repo, root)


def _write_dir(repo, d):
    ents = []
    for name, v in d.items():
        bname = name.encode('utf-8', 'surrogateescape')
        if isinstance(v, dict):
            ents.append(('40000', bname, _write_dir(repo, v)))
        else:
            ents.append(('%o' % v.mode, bname, v.oid))
    return repo.objects.write('tree', build_tree(ents))


@command('write-tree')
def write_tree(ctx, args):
    if args:
        raise Unsupported('write-tree %s' % ' '.join(args))
    repo = ctx.need_repo()
    ix = _index.Index(repo).load()
    ctx.write(write_tree_from_index(repo, ix) + '\n')
    return 0


def index_from_tree(repo, tree, ix=None):
    """An Index (stat data empty) holding every blob of `tree`."""
    ix = ix or _index.Index(repo)
    ix.clear()
    for path, (mode, oid) in _revs.flatten_tree(repo, tree).items():
        m = int(mode, 8)
        if m == 0o160000:
            raise Unsupported('gitlink in a tree')
        ix.entries[(path, 0)] = _index.Entry(path, m, oid)
    return ix


@command('read-tree')
def read_tree(ctx, args):
    repo = ctx.need_repo()
    if args == ['--empty']:
        ix = _index.Index(repo)
        lock = _index.IndexLock(ix.path).acquire()
        try:
            ix.write(lock)
        except BaseException:
            lock.rollback()
            raise
        return 0
    if len(args) != 1 or args[0].startswith('-'):
        raise Unsupported('read-tree %s' % ' '.join(args))
    oid = resolve(ctx, args[0])
    tree = _revs.peel(repo, oid, 'tree') if oid else None
    if tree is None:
        raise die('failed to unpack tree object %s' % args[0])
    ix = _index.Index(repo)
    lock = _index.IndexLock(ix.path).acquire()
    try:
        index_from_tree(repo, tree, ix)
        ix.write(lock)
    except BaseException:
        lock.rollback()
        raise
    return 0


_CTREE = _u.Spec('commit-tree', flags='no-gpg-sign', multi='p m F', optional='S|gpg-sign')


def message_from(ctx, ms, fs):
    """The message -m (paragraphs) and -F (files, '-' stdin) give, as commit-tree builds it."""
    buf = ''
    for m in ms or ():
        if buf:
            buf += '\n'
        buf += m + '\n'
    for f in fs or ():
        if buf:
            buf += '\n'
        if f == '-':
            buf += ctx.stdin_bytes().decode('utf-8', 'surrogateescape')
        else:
            with open(os.path.join(ctx.cwd, f), 'rb') as fh:
                buf += fh.read().decode('utf-8', 'surrogateescape')
    return buf


def make_commit(ctx, tree, parents, message, author=None):
    repo = ctx.repo
    if repo.config.bool('commit.gpgsign', False):
        raise Unsupported('commit.gpgSign')
    a = _u.ident(ctx, 'author', author)
    c = _u.ident(ctx, 'committer')
    lines = ['tree %s' % tree] + ['parent %s' % p for p in parents] + ['author %s' % a, 'committer %s' % c]
    body = ('\n'.join(lines) + '\n\n').encode('utf-8') + message.encode('utf-8', 'surrogateescape')
    return repo.objects.write('commit', body)


@command('commit-tree')
def commit_tree(ctx, args):
    opts, pos, dd = _u.parse(_CTREE, args)
    if opts.get('S'):
        raise Unsupported('commit-tree -S')
    repo = ctx.need_repo()
    if len(pos) != 1:
        raise GitExit(129, 'usage: git commit-tree <tree> [(-p <parent>)...]\n')
    tree = resolve(ctx, pos[0])
    if tree is None:
        raise die('not a valid object name %s' % pos[0])
    tree = _revs.peel(repo, tree, 'tree')
    if tree is None:
        raise die('%s is not a valid \'tree\' object' % pos[0])
    parents = []
    for p in opts.get('p', []):
        o = resolve(ctx, p)
        c = _revs.peel(repo, o, 'commit') if o else None
        if c is None:
            raise die('not a valid object name %s' % p)
        if c not in parents:
            parents.append(c)
    if not opts.get('m') and not opts.get('F'):
        msg = ctx.stdin_bytes().decode('utf-8', 'surrogateescape')
    else:
        msg = message_from(ctx, opts.get('m'), opts.get('F'))
    if opts.get('no-gpg-sign') is None and repo.config.bool('commit.gpgsign', False):
        raise Unsupported('commit.gpgSign')
    oid = make_commit_raw(ctx, tree, parents, msg)
    ctx.write(oid + '\n')
    return 0


def make_commit_raw(ctx, tree, parents, message, author=None):
    a = _u.ident(ctx, 'author', author)
    c = _u.ident(ctx, 'committer')
    lines = ['tree %s' % tree] + ['parent %s' % p for p in parents] + ['author %s' % a, 'committer %s' % c]
    body = ('\n'.join(lines) + '\n\n').encode('utf-8') + message.encode('utf-8', 'surrogateescape')
    return ctx.repo.objects.write('commit', body)


# -- merge-base -----------------------------------------------------------------------------

_MB = _u.Spec('merge-base', flags='a|all is-ancestor octopus independent fork-point')


@command('merge-base')
def merge_base(ctx, args):
    opts, pos, dd = _u.parse(_MB, args)
    if opts.get('octopus') or opts.get('independent') or opts.get('fork-point'):
        raise Unsupported('merge-base options')
    repo = ctx.need_repo()
    if opts.get('is-ancestor'):
        if len(pos) != 2:
            raise GitExit(129, 'usage: git merge-base --is-ancestor <commit> <commit>\n')
        a, b = (_commit_arg(ctx, x) for x in pos)
        return 0 if _revs.is_ancestor(repo, a, b) else 1
    if len(pos) < 2:
        raise GitExit(129, 'usage: git merge-base [-a | --all] <commit> <commit>...\n')
    oids = [_commit_arg(ctx, x) for x in pos]
    bases = _revs.merge_bases(repo, oids[0], oids[1:])
    if not bases:
        return 1
    for b in (bases if opts.get('all') else bases[:1]):
        ctx.write(b + '\n')
    return 0


def _commit_arg(ctx, spec):
    oid = resolve(ctx, spec)
    if oid is None:
        raise die('Not a valid object name %s' % spec)
    c = _revs.peel(ctx.repo, oid, 'commit')
    if c is None:
        raise die('Not a valid commit name %s' % spec)
    return c


# -- branch ---------------------------------------------------------------------------------

_BRANCH = _u.Spec('branch', flags='d D m M f|force q|quiet r a l|list show-current no-track '
                  'c C v vv', values='format', optional='t|track u|set-upstream-to',
                  negatable='')


def worktrees_using(repo, branch_ref):
    """[(worktree path, kind)] where `branch_ref` is checked out ('HEAD') in any worktree."""
    from . import worktrees as _wts
    return [w for w in _wts.all_worktrees(repo) if w.head_ref == branch_ref]


@command('branch')
def branch(ctx, args):
    opts, pos, dd = _u.parse(_BRANCH, args)
    if dd:
        pos += dd
    repo = ctx.need_repo()
    for k in ('r', 'a', 'c', 'C', 'v', 'vv', 'track', 'set-upstream-to'):
        if opts.get(k):
            raise Unsupported('branch -%s' % k)
    quiet = opts.get('quiet')
    if opts.get('show-current'):
        b = _revs.current_branch(repo)
        if b:
            ctx.write(b + '\n')
        return 0
    if opts.get('d') or opts.get('D'):
        force = opts.get('D') or opts.get('force')
        if not pos:
            raise die('branch name required')
        rc = 0
        for name in pos:
            ref = 'refs/heads/' + name
            oid, _ = _refs.resolve(repo, ref)
            if oid is None:
                ctx.warn("error: branch '%s' not found\n" % name)
                rc = 1
                continue
            using = worktrees_using(repo, ref)
            if using:
                ctx.warn("error: cannot delete branch '%s' used by worktree at '%s'\n"
                         % (name, using[0].path_out))
                rc = 1
                continue
            if not force and not _merged(repo, name, oid):
                ctx.warn("error: the branch '%s' is not fully merged\n"
                         "hint: If you are sure you want to delete it, run 'git branch -D %s'\n"
                         "hint: Disable this message with \"git config set advice.forceDeleteBranch false\"\n"
                         % (name, name))
                rc = 1
                continue
            _refs.delete(repo, ref)
            _remove_branch_config(repo, name)
            if not quiet:
                ctx.write('Deleted branch %s (was %s).\n' % (name, abbrev(repo, oid)))
        return rc
    if opts.get('m') or opts.get('M'):
        if len(pos) == 1:
            old = _revs.current_branch(repo)
            if old is None:
                raise die('cannot rename the current branch while not on any')
            new = pos[0]
        elif len(pos) == 2:
            old, new = pos
        else:
            raise die('too many arguments for a rename operation')
        if not _refs.check_refname('refs/heads/' + new):
            raise die("'%s' is not a valid branch name" % new)
        oldref, newref = 'refs/heads/' + old, 'refs/heads/' + new
        oid, _ = _refs.resolve(repo, oldref)
        if oid is None:
            raise die("no branch named '%s'" % old)
        if _refs.exists(repo, newref) and not opts.get('M') and oldref != newref:
            raise die("a branch named '%s' already exists" % new)
        here = os.path.normcase(os.path.normpath(repo.worktree or ''))
        if any(os.path.normcase(os.path.normpath(w.os_path())) != here
               for w in worktrees_using(repo, oldref)):
            raise Unsupported('branch -m of a branch checked out in another worktree')
        if oldref != newref:
            if _refs.exists(repo, newref):
                _refs.delete(repo, newref)
            _refs.delete(repo, oldref)
            _refs.update(repo, newref, oid, old='')
            if _refs.head_target(repo) == oldref:
                _refs.write_symref(repo, 'HEAD', newref)
            _rename_branch_config(repo, old, new)
        return 0
    if opts.get('list') or not pos:
        fmt = opts.get('format')
        names = _refs.list_refs(repo, 'refs/heads/')
        if pos:
            from .ignore import wildmatch
            names = [(n, o) for n, o in names if any(wildmatch(p, n[len('refs/heads/'):], True) for p in pos)]
        cur = _refs.head_target(repo)
        if fmt is None and cur is None and not pos:
            head, _ = _refs.resolve(repo, 'HEAD')
            if head is not None:
                ctx.write('* (HEAD detached at %s)\n' % abbrev(repo, head))
        for n, o in names:
            if fmt is not None:
                ctx.write(format_ref(repo, fmt, n, o) + '\n')
            else:
                using = worktrees_using(repo, n)
                mark = '* ' if n == cur else ('+ ' if using else '  ')
                ctx.write(mark + n[len('refs/heads/'):] + '\n')
        return 0
    # create
    if len(pos) > 2:
        raise die('too many arguments')
    name = pos[0]
    start = pos[1] if len(pos) == 2 else 'HEAD'
    if not _refs.check_refname('refs/heads/' + name) or name.startswith('-') or name == 'HEAD':
        raise GitExit(128, "fatal: '%s' is not a valid branch name\n"
                           "hint: See 'git help check-ref-format'\n"
                           "hint: Disable this message with \"git config set advice.refSyntax false\"\n" % name)
    ref = 'refs/heads/' + name
    if _refs.exists(repo, ref) and not opts.get('force'):
        raise die("a branch named '%s' already exists" % name)
    if _refs.exists(repo, ref) and worktrees_using(repo, ref):
        raise die("cannot force update the branch '%s' used by worktree at '%s'"
                  % (name, worktrees_using(repo, ref)[0].path_out))
    oid = resolve(ctx, start)
    c = _revs.peel(repo, oid, 'commit') if oid else None
    if c is None:
        if start == 'HEAD':
            raise die("not a valid object name: 'HEAD'")
        raise die("not a valid object name: '%s'" % start)
    setup_tracking(ctx, name, start, opts.get('no-track') is True)
    _refs.update(repo, ref, c)
    return 0


def setup_tracking(ctx, name, start, no_track=False, quiet=False):
    """branch.autoSetupMerge (default true): a branch started from a remote-tracking branch gets
    its upstream config, and git says so."""
    repo = ctx.repo
    if no_track:
        return
    mode = repo.config.get('branch.autosetupmerge')
    if mode is not None and mode.strip().lower() not in ('true', 'always', 'false'):
        raise Unsupported('branch.autoSetupMerge=%s' % mode)
    if mode is not None and mode.strip().lower() == 'false':
        return
    full = _full_name_quiet(repo, start)
    if full is None:
        if mode is not None and mode.strip().lower() == 'always':
            raise Unsupported('branch.autoSetupMerge=always from a non-ref')
        return
    if full.startswith('refs/heads/'):
        if mode is not None and mode.strip().lower() == 'always':
            raise Unsupported('branch.autoSetupMerge=always from a local branch')
        return
    if not full.startswith('refs/remotes/'):
        return
    for rname in _remote_names(repo):
        for spec in repo.config.get_all('remote.%s.fetch' % rname):
            s = spec.lstrip('+')
            src, _, dst = s.partition(':')
            if '*' in dst:
                pa, sa = dst.split('*', 1)
                if full.startswith(pa) and full.endswith(sa):
                    mid = full[len(pa):len(full) - len(sa)] if sa else full[len(pa):]
                    merge = src.replace('*', mid)
                    _write_tracking(ctx, name, rname, merge, quiet)
                    return
            elif dst == full:
                _write_tracking(ctx, name, rname, src, quiet)
                return


def _write_tracking(ctx, name, remote, merge, quiet):
    repo = ctx.repo
    path = os.path.join(repo.commondir, 'config')
    _config.set_value(path, 'branch.%s.remote' % name, remote)
    _config.set_value(path, 'branch.%s.merge' % name, merge)
    repo.reload_config()
    if not quiet:
        short = merge[len('refs/heads/'):] if merge.startswith('refs/heads/') else merge
        ctx.write("branch '%s' set up to track '%s/%s'.\n" % (name, remote, short))


def _full_name_quiet(repo, spec):
    try:
        return _full_name(repo, spec)
    except Unsupported:
        return None


def _remote_names(repo):
    seen = []
    for k, _v in repo.config.items():
        if k.startswith('remote.') and k.count('.') >= 2:
            n = k[len('remote.'):k.rfind('.')]
            if n not in seen:
                seen.append(n)
    return seen


def _merged(repo, name, oid):
    head, _ = _refs.resolve(repo, 'HEAD')
    ref = None
    try:
        up = _revs.upstream_of(repo, name)
        ref, _ = _refs.resolve(repo, up)
    except GitExit:
        ref = None
    target = ref if ref is not None else head
    if target is None:
        return True
    return _revs.is_ancestor(repo, oid, target)


def _remove_branch_config(repo, name):
    # git branch -d removes the whole [branch "<name>"] section (git_config_rename_section to NULL).
    path = os.path.join(repo.commondir, 'config')
    if _config.rename_section(path, 'branch', name, None):
        repo.reload_config()


def _rename_branch_config(repo, old, new):
    path = os.path.join(repo.commondir, 'config')
    pre = 'branch.%s.' % old
    if any(e.key.startswith(pre) for e in _config.file_entries(path)):
        _config.rename_section(path, 'branch', old, new)
        repo.reload_config()


# -- tag ------------------------------------------------------------------------------------

_TAG = _u.Spec('tag', flags='a|annotate l|list d|delete f|force s|sign no-sign', values='cleanup',
               multi='m F')


@command('tag')
def tag_cmd(ctx, args):
    opts, pos, dd = _u.parse(_TAG, args)
    repo = ctx.need_repo()
    if opts.get('sign') or repo.config.bool('tag.gpgsign', False) and not opts.get('no-sign'):
        if opts.get('a') or opts.get('m') or opts.get('F') or opts.get('sign'):
            raise Unsupported('signed tags')
    if opts.get('delete'):
        rc = 0
        for n in pos:
            oid, _ = _refs.resolve(repo, 'refs/tags/' + n)
            if oid is None:
                ctx.warn("error: tag '%s' not found.\n" % n)
                rc = 1
                continue
            _refs.delete(repo, 'refs/tags/' + n)
            ctx.write("Deleted tag '%s' (was %s)\n" % (n, abbrev(repo, oid)))
        return rc
    if opts.get('list') or not pos:
        from .ignore import wildmatch
        for n, _o in _refs.list_refs(repo, 'refs/tags/'):
            short = n[len('refs/tags/'):]
            if not pos or any(wildmatch(p, short, False) for p in pos):
                ctx.write(short + '\n')
        return 0
    name = pos[0]
    target_spec = pos[1] if len(pos) > 1 else 'HEAD'
    if len(pos) > 2:
        raise GitExit(129, 'usage: git tag ...\n')
    ref = 'refs/tags/' + name
    if not _refs.check_refname(ref):
        raise die("'%s' is not a valid tag name." % name)
    if _refs.exists(repo, ref) and not opts.get('force'):
        raise die("tag '%s' already exists" % name)
    oid = resolve(ctx, target_spec)
    if oid is None:
        raise die('Failed to resolve \'%s\' as a valid ref.' % target_spec)
    annotated = opts.get('a') or opts.get('m') or opts.get('F')
    if annotated:
        if not opts.get('m') and not opts.get('F'):
            raise Unsupported('tag -a without a message (editor)')
        msg = message_from(ctx, opts.get('m'), opts.get('F'))
        mode = opts.get('cleanup', 'strip')
        if mode not in ('verbatim', 'whitespace', 'strip'):
            raise Unsupported('tag --cleanup=%s' % mode)
        msg = _u.cleanup_message(msg, mode)
        kind = _revs.obj_type(repo, oid)
        tagger = _u.ident(ctx, 'committer')
        body = ('object %s\ntype %s\ntag %s\ntagger %s\n\n' % (oid, kind, name, tagger)).encode('utf-8') + \
            msg.encode('utf-8', 'surrogateescape')
        oid = repo.objects.write('tag', body)
    _refs.update(repo, ref, oid)
    return 0


# -- init -----------------------------------------------------------------------------------

_INIT = _u.Spec('init', flags='q|quiet bare', values='b|initial-branch object-format template '
                'separate-git-dir ref-format', optional='shared')

_TEMPLATE_CACHE = {}


def _template_dir(ctx, cfg_get):
    d = ctx.env.get('GIT_TEMPLATE_DIR') or cfg_get('init.templatedir')
    if d:
        return os.path.expanduser(d)
    prefix = _config._git_prefix()
    for cand in (os.path.join(prefix, 'mingw64', 'share', 'git-core', 'templates'),
                 os.path.join(prefix, 'share', 'git-core', 'templates')):
        if prefix and os.path.isdir(cand):
            return cand
    return None


def _template_files(d):
    got = _TEMPLATE_CACHE.get(d)
    if got is None:
        got = []
        if d and os.path.isdir(d):
            for dirpath, dirs, files in os.walk(d):
                rel = os.path.relpath(dirpath, d)
                for f in sorted(files):
                    with open(os.path.join(dirpath, f), 'rb') as fh:
                        got.append((os.path.normpath(os.path.join(rel, f)), fh.read()))
                for dd in dirs:
                    got.append((os.path.normpath(os.path.join(rel, dd)), None))
        _TEMPLATE_CACHE[d] = got
    return got


@command('init')
def init(ctx, args):
    opts, pos, dd = _u.parse(_INIT, args)
    if dd:
        pos += dd
    for k in ('template', 'separate-git-dir', 'shared'):
        if opts.get(k):
            raise Unsupported('init --%s' % k)
    if opts.get('ref-format') not in (None, 'files'):
        raise Unsupported('init --ref-format=%s' % opts['ref-format'])
    if len(pos) > 1:
        raise GitExit(129, 'usage: git init [<options>] [<directory>]\n')
    bare = bool(opts.get('bare'))
    top = os.path.normpath(os.path.join(ctx.cwd, pos[0])) if pos else ctx.cwd
    gitdir = top if bare else os.path.join(top, '.git')
    algo = opts.get('object-format', 'sha1')
    if algo not in ('sha1', 'sha256'):
        raise die('unknown hash algorithm \'%s\'' % algo)
    for v in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_DEFAULT_HASH', 'GIT_DEFAULT_REF_FORMAT'):
        if ctx.env.get(v):
            raise Unsupported('%s with init' % v)
    pre = _IdentCtx(ctx).repo.config if ctx.repo is None or True else None

    def cfg_get(k):
        return pre.get(k)
    if pre.get('init.defaultobjectformat') or pre.get('init.defaultrefformat'):
        raise Unsupported('init.defaultObjectFormat / init.defaultRefFormat')
    from .repo import is_git_directory
    if os.path.isdir(gitdir) and is_git_directory(gitdir):
        if opts.get('b'):
            ctx.warn('warning: re-init: ignored --initial-branch=%s\n' % opts['b'])
        if not opts.get('quiet'):
            ctx.write('Reinitialized existing Git repository in %s/\n' % slash(os.path.realpath(gitdir)))
        return 0
    os.makedirs(gitdir, exist_ok=True)
    tdir = _template_dir(ctx, cfg_get)
    for rel, data in _template_files(tdir):
        dst = os.path.join(gitdir, rel)
        if data is None:
            os.makedirs(dst, exist_ok=True)
            continue
        if not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, 'wb') as f:
                f.write(data)
    for d in ('objects/info', 'objects/pack', 'refs/heads', 'refs/tags'):
        os.makedirs(os.path.join(gitdir, d), exist_ok=True)
    branch_name = opts.get('b') or cfg_get('init.defaultbranch') or 'master'
    if not _refs.check_refname('refs/heads/' + branch_name):
        raise die("invalid initial branch name: '%s'" % branch_name)
    with open(os.path.join(gitdir, 'HEAD'), 'wb') as f:
        f.write(('ref: refs/heads/%s\n' % branch_name).encode('utf-8'))
    lines = []
    if algo == 'sha256':
        lines += ['[extensions]', '\tobjectformat = sha256']
    lines += ['[core]', '\trepositoryformatversion = %d' % (1 if algo == 'sha256' else 0),
              '\tfilemode = false', '\tbare = %s' % ('true' if bare else 'false')]
    if not bare:
        lines.append('\tlogallrefupdates = true')
    lines += ['\tsymlinks = false', '\tignorecase = true']
    with open(os.path.join(gitdir, 'config'), 'wb') as f:
        f.write(('\n'.join(lines) + '\n').encode('utf-8'))
    if not opts.get('quiet'):
        ctx.write('Initialized empty Git repository in %s/\n' % slash(os.path.realpath(gitdir)))
    return 0


__all__ = ['abbrev', 'resolve', 'write_tree_from_index', 'index_from_tree', 'make_commit_raw',
           'shorten_ref', 'Commit', 'EMPTY_TREE', 'ZERO', 'shutil']
