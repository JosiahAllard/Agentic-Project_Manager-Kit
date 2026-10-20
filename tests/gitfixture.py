"""Throwaway git repositories for kitlib tests (a temp folder, removed after).

The fixture's own git calls ignore the user's global and system git config (no hooks, no
signing, no default-branch surprises) and turn off auto gc and maintenance. The identity is a
fake one: tests never carry a real person's name or address, so an inherited GIT_AUTHOR_* or
GIT_COMMITTER_* name or email (an agent's identity) is dropped and the repo's user.* config wins.

The fixture's own steps (building a repository, refreshing a copy's index, commit(), worktree())
run in this process on the fake git (kitlib.git.fake) whatever git the test runs on: the fake
writes the real on-disk repository, so a @real_git class's real git carries on from it, and the
class pays a process only for the git it tests. The fake writes no reflog entry and runs no hook
for those steps. A class that checks either for a fixture step, or checks the fixture's real-git
build itself, is marked real_fixture(reason): its fixture runs real git too. TempRepo.git() is
the test's own git and runs what the test runs on (kitlib.git.cmd: the fake under KIT_FAKE_GIT=1).
"""
import atexit
import os
import re
import shutil
import tempfile

from kitlib import sessionmark
from kitlib.config import user as kuser
from kitlib.git import batch, cmd as gitcmd
from kitlib.git import cache as gitcache
from kitlib.git import fake as gitfake
from kitlib.tooldeps import selfrun

# A test process keeps kitlib.git.cache's answers; this module forgets them whenever its own git
# runs or a repository is copied in. Nothing outside the tests turns the memo on.
gitcache.enable()

NAME = 'Kit Test'
EMAIL = 'kit-test@example.invalid'

# A built repository's own config. core.trustctime=false lets a copy keep the template's index
# (see TempRepo): a copied file's mtime and size are kept, only its creation time is new.
BUILD_CONFIG = (('user', 'name', NAME), ('user', 'email', EMAIL), ('commit', 'gpgsign', 'false'),
                ('core', 'autocrlf', 'false'), ('core', 'trustctime', 'false'))


def fixture_env(extra=None):
    """The environment for the fixture's tools: this process's, without git's repository and
    identity variables and without the caller's session mark (kitlib.sessionmark: a test run from a
    marked session must not take its role, which tools/backlog.py reads); then `extra`, which may
    set a mark of its own."""
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_CEILING_DIRECTORIES',
                                'GIT_INDEX_FILE', 'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL',
                                'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL',
                                sessionmark.ENV, sessionmark.SESSION_NAME_ENV)}
    env.update(batch.fixture_env(env))
    env['GIT_CONFIG_GLOBAL'] = os.devnull
    env['GIT_CONFIG_NOSYSTEM'] = '1'
    env.update(extra or {})
    if 'USERPROFILE' in (extra or {}):
        env.update(ps_cache_env(env['USERPROFILE']))
    return env


def ps_cache_env(home):
    """{PSModuleAnalysisCachePath: a file under `home`}. Windows PowerShell 5.1 looks the cache
    up under LOCALAPPDATA, and with a temp home that has none it can write
    Microsoft/Windows/PowerShell/ModuleAnalysisCache relative to the cwd: a worktree's root, an
    untracked file that costs the gate its record. Any env that gives a real powershell a temp
    USERPROFILE carries this."""
    return {'PSModuleAnalysisCachePath': os.path.join(home, 'PSModuleAnalysisCache')}


def rmtree(path):
    """Remove a fixture folder, git's read-only object files included (selfrun.rmtree_force,
    which the test runner also removes its kit-tests-* folders with)."""
    if os.path.isdir(path):
        selfrun.rmtree_force(path)


KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIRECT = []        # this process's kit-direct-* folder, once made


def fixture_parent():
    """The folder fixture repositories and copies are made in. Under tools/kit_tests.py a unit's
    TEMP is its own `<run folder>/<n>` in the kit's temp folder, and that is kept. A direct
    `python -m unittest` run's TEMP is the session's, where a fixture's churn slows every session
    watching it, so this process gets one selfrun.DIRECT_PREFIX folder in the kit's default temp
    folder (kitlib.config.user.default_work_temp, kept out of this TEMP and AGENT_TEMP too),
    removed at exit and otherwise by the next run's selfrun.prune_stale when that is its run
    folder's parent; this TEMP when that folder cannot be made."""
    t = tempfile.gettempdir()
    if os.path.basename(os.path.dirname(os.path.abspath(t))).startswith(selfrun.RUN_PREFIX):
        return t
    if _DIRECT and os.path.isdir(_DIRECT[0]):
        return _DIRECT[0]
    try:
        parent = kuser.default_work_temp(KIT, sessions=(t, os.environ.get('AGENT_TEMP')))
        os.makedirs(parent, exist_ok=True)
        folder = tempfile.mkdtemp(prefix=selfrun.DIRECT_PREFIX, dir=parent)
    except (OSError, RuntimeError):
        return t
    _DIRECT[:] = [folder]
    return folder


def _drop_direct():
    for folder in _DIRECT:
        selfrun.rmtree_force(folder, ignore_errors=True)
    del _DIRECT[:]


# Registered before _drop_templates, so it runs after it (atexit runs last-registered first).
atexit.register(_drop_direct)


# Set (to 1) while a real_fixture class runs: the fixture's own steps then start real git.
REAL_FIXTURE_ENV = 'KIT_FIXTURE_REAL_GIT'
REASONS = {}        # 'module.Class' -> why its fixture needs real git


def real_fixture(reason):
    """Class decorator: the fixture's own steps start real git while the class runs (setUpClass
    to its last class cleanup), for a class that checks what only real git leaves after a fixture
    step (a reflog entry, a hook run) or checks the real-git build itself. `reason` says which.
    It does not put the class on real git: that is tests.realgit.real_git."""
    def mark(cls):
        REASONS['%s.%s' % (cls.__module__, cls.__qualname__)] = reason
        own = cls.__dict__.get('setUpClass')

        def setUpClass(klass):
            old = os.environ.get(REAL_FIXTURE_ENV)
            os.environ[REAL_FIXTURE_ENV] = '1'
            klass.addClassCleanup(_restore_env, REAL_FIXTURE_ENV, old)
            if own is not None:
                own.__func__(klass)
            else:
                super(cls, klass).setUpClass()
        cls.setUpClass = classmethod(setUpClass)
        return cls
    return mark


def _restore_env(name, old):
    if old is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = old


def in_process():
    """True when the fixture's own steps run on the fake git in this process: always, but in a
    real_fixture class."""
    return os.environ.get(REAL_FIXTURE_ENV) != '1'


def own_git(args, cwd):
    """The CompletedProcess (text) of a fixture step's `git <args>` in `cwd`, under fixture_env():
    the fake git in this process (in_process()), else git as kitlib.git.cmd starts it. What
    kitlib.git.cache kept is forgotten either way (a repository may be replaced)."""
    gitcache.clear()
    kw = dict(cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace',
              env=fixture_env())
    argv = ('git',) + tuple(args)
    if in_process():
        return gitfake.completed(argv, **kw)
    return gitcmd.subprocess_run(argv, **kw)


# Built repositories this process copies instead of building again: {key: TempRepo}. A fresh
# TempRepo's own steps run in process (see the top); in a real_fixture class it costs 4 git
# processes (init, add, commit, its one index refresh; its config and its HEAD are written and
# read as files), 6 with a bare remote. A copy costs none on Windows.
_TEMPLATES = {}
_REMOTE_URL = re.compile(r'^(\s*url\s*=)(.*)$', re.M)


def _template_key(branch, bare_remote, object_format, prepare_key=None):
    """What a built repository depends on: its arguments, which git built it (in_process()) and
    the environment its git ran in."""
    return (branch, bool(bare_remote), object_format, prepare_key, in_process(),
            tuple(sorted(fixture_env().items())))


# Folders built_copy() copies instead of building again: {key: folder}.
_BUILT = {}


def _drop_templates():
    for t in _TEMPLATES.values():
        t.cleanup()
    _TEMPLATES.clear()
    for folder in _BUILT.values():
        rmtree(os.path.dirname(folder))
    _BUILT.clear()


def built_copy(key, dest, build, repos=('',)):
    """Fill folder `dest` (made if missing) with a copy of what `build(folder)` makes in an empty
    folder. The build runs once per process per hashable `key`, which must name everything the
    result depends on; later calls copy it. Then each git repository at `repos` (paths relative to
    `dest`, '' for dest itself) has its index refreshed, as TempRepo does: a copied file has a new
    ctime. `build` must leave no absolute path in what it makes (no linked worktree, no remote by
    path) and must run its git under fixture_env(), so the copy is the repository it built."""
    src = _BUILT.get(key)
    if src is None or not os.path.isdir(src):
        src = os.path.join(tempfile.mkdtemp(prefix='kit-tpl-', dir=fixture_parent()), 'built')
        os.makedirs(src)
        build(src)
        _BUILT[key] = src
    shutil.copytree(src, dest, dirs_exist_ok=True)
    gitcache.clear()
    for rel in repos:
        r = own_git(('update-index', '-q', '--refresh'), os.path.join(dest, rel))
        if r.returncode != 0:
            raise AssertionError('gitfixture: index refresh failed in %s: %s'
                                 % (dest, r.stderr.strip()))


atexit.register(_drop_templates)


def _config_value(text):
    return '"%s"' % text.replace('\\', '\\\\').replace('"', '\\"')


def _config_unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return re.sub(r'\\(.)', r'\1', value)


def _same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


_SHA = re.compile(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')


def _read(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return fh.read().strip()
    except OSError:
        return None


def head_from_files(top):
    """HEAD's commit in the work tree whose root is `top`, read from git's files with no process,
    or None when that is not plain: `top` holds no .git, HEAD names a ref outside refs/heads/,
    the branch has no loose ref file (packed, unborn, or another ref store). A loose ref file
    always wins over packed-refs, and git replaces it whole (lock file, then rename), so what it
    holds is what `git rev-parse HEAD` prints. None means: ask git."""
    dotgit = os.path.join(top, '.git')
    if os.path.isdir(dotgit):
        gitdir = dotgit
    else:
        line = _read(dotgit)
        if not line or not line.startswith('gitdir:'):
            return None
        gitdir = os.path.normpath(os.path.join(top, line[len('gitdir:'):].strip()))
    common = _read(os.path.join(gitdir, 'commondir'))
    common = os.path.normpath(os.path.join(gitdir, common)) if common else gitdir
    head = _read(os.path.join(gitdir, 'HEAD'))
    if not head:
        return None
    if head.startswith('ref:'):
        ref = head[len('ref:'):].strip()
        if not ref.startswith('refs/heads/'):
            return None
        head = _read(os.path.join(common, *ref.split('/')))
    return head if head and _SHA.match(head) else None


def _checked(args, r, check):
    if check and r.returncode != 0:
        raise AssertionError('git %s failed: %s' % (' '.join(args), r.stderr.strip()))
    return r.stdout.strip()


class TempRepo:
    """A git repository in a fresh temp folder: `main` with one commit. `object_format` (sha1 or
    sha256; None: git's default) applies to the repository and its bare remote.

    The first TempRepo of a kind in a process is built with git; later ones copy it (folders and
    files, the remote's url rewritten to the copy's remote, the template's refreshed index kept),
    so they are the same repository: same commit, same refs, same config, a fresh index.

    `prepare(repo)`, with a hashable `prepare_key` naming what it does, runs once on the built
    template before it is copied, so a test case's own fixed setup (files written, commits, a
    branch switched to) is copied too instead of run again for every test. It must leave no
    absolute path inside the repository (no linked worktree, no remote url but the bare remote)
    and no state outside root and remote, and leave its tracked files committed or staged
    (`update-index --refresh` refuses a modified one). `initial` stays the first commit;
    `prepared` is what prepare returned (None without one), the same for every copy."""

    def __init__(self, branch='main', bare_remote=False, object_format=None, prepare=None,
                 prepare_key=None, _build=False):
        if (prepare is None) != (prepare_key is None):
            raise ValueError('gitfixture: prepare and prepare_key go together')
        self.base = tempfile.mkdtemp(prefix='kit-git-', dir=fixture_parent())
        self.root = os.path.join(self.base, 'repo')
        self.remote = os.path.join(self.base, 'remote.git') if bare_remote else None
        self.prepared = None
        if _build:
            self._build(branch, bare_remote, object_format)
            if prepare is not None:
                self.prepared = prepare(self)
            return
        key = _template_key(branch, bare_remote, object_format, prepare_key)
        tpl = _TEMPLATES.get(key)
        if tpl is None or not os.path.isdir(tpl.base):     # a test may have removed its temp
            _TEMPLATES.pop(key, None)
            try:
                tpl = TempRepo(branch, bare_remote, object_format, prepare, prepare_key,
                               _build=True)
            except BaseException:
                self.cleanup()
                raise
            # Once per template, not per copy: settles its index and refuses a prepare that left
            # a tracked file modified. Only a template that passed is kept: a refused one is
            # removed, so the next TempRepo of its kind builds (and is refused) again instead of
            # copying it.
            try:
                tpl._own('update-index', '--refresh')
            except BaseException:
                tpl.cleanup()
                self.cleanup()
                raise
            _TEMPLATES[key] = tpl
        # copytree keeps every file's mtime and size; the template sets core.trustctime=false, so
        # the copied index's stat data still matches the copied files (a copied file's new
        # creation time, which Git for Windows keeps as ctime, is not compared) and the index is
        # as fresh as the template's: no `update-index --refresh` process per copy. Elsewhere git
        # also compares a file's inode, which a copy changes, so the copy's index is refreshed.
        shutil.copytree(tpl.root, self.root)
        if os.name != 'nt':
            self._own('update-index', '-q', '--refresh')
        if bare_remote:
            shutil.copytree(tpl.remote, self.remote)
            cfg = os.path.join(self.root, '.git', 'config')
            with open(cfg, encoding='utf-8') as fh:
                text = fh.read()
            # Every url line names the template's bare remote (a prepare may add a second remote
            # name for it); each is pointed at this copy's remote.
            urls = [_config_unquote(m.group(2)) for m in _REMOTE_URL.finditer(text)]
            if not urls or any(not _same_path(u, tpl.remote) for u in urls):
                raise AssertionError('gitfixture: the template config urls %r are not its bare '
                                     'remote %s' % (urls, tpl.remote))
            text = _REMOTE_URL.sub(lambda m: '%s %s' % (m.group(1), _config_value(self.remote)),
                                   text)
            with open(cfg, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(text)
        self.initial = tpl.initial
        self.prepared = tpl.prepared

    def _build(self, branch, bare_remote, object_format):
        os.makedirs(self.root)
        fmt = ['--object-format=%s' % object_format] if object_format else []
        self._own('init', '-q', '-b', branch, *fmt)
        # The repository config `git config` would set one process per key, written in one go.
        self._add_config(BUILD_CONFIG)
        self.write('README.md', 'hello\n')
        self.initial = self.commit('initial', 'README.md')
        if bare_remote:
            self._own('init', '-q', '--bare', *fmt, self.remote, cwd=self.base)
            # What `git remote add origin <remote>` writes.
            self._add_config((('remote "origin"', 'url', self.remote),
                              ('remote "origin"', 'fetch', '+refs/heads/*:refs/remotes/origin/*')))
            self._own('push', '-q', 'origin', branch)

    def _add_config(self, entries):
        """Append `(section, key, value)` entries to the repository's .git/config, one section
        header per run of entries in the same section (git reads a repeated section as one)."""
        out, last = [], None
        for section, key, value in entries:
            if section != last:
                out.append('[%s]\n' % section)
                last = section
            out.append('\t%s = %s\n' % (key, _config_value(value)))
        with open(os.path.join(self.root, '.git', 'config'), 'a', encoding='utf-8',
                  newline='\n') as fh:
            fh.write(''.join(out))

    def git(self, *args, cwd=None, check=True):
        """The test's own `git <args>` in `cwd` (default root), stdout stripped: the git the test
        runs on. AssertionError when it fails and `check`."""
        if args == ('rev-parse', 'HEAD'):
            sha = head_from_files(cwd or self.root)
            if sha:
                return sha
        # kitlib.git.cache cannot see this git: forget what it kept (a repository may be replaced).
        gitcache.clear()
        r = gitcmd.subprocess_run(('git',) + args, cwd=cwd or self.root, capture_output=True,
                                  text=True, encoding='utf-8', errors='replace', env=fixture_env())
        return _checked(args, r, check)

    def _own(self, *args, cwd=None):
        """A fixture step's `git <args>` (own_git) in `cwd` (default root), stdout stripped;
        AssertionError when it fails."""
        return _checked(args, own_git(args, cwd or self.root), True)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def write(self, rel, text, root=None):
        full = os.path.join(root or self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        return full

    def commit(self, message, *paths, cwd=None):
        """Stage `paths` and commit them with `message` in the work tree at `cwd` (default root),
        a fixture step; returns the new HEAD."""
        self._own('add', '--', *paths, cwd=cwd)
        self._own('commit', '-q', '-m', message, cwd=cwd)
        return self.head(cwd)

    def head(self, cwd=None):
        """HEAD's commit in the work tree at `cwd` (default root): what `git rev-parse HEAD`
        prints, read from the ref files where that is plain (a loose ref or a detached HEAD),
        else asked of git."""
        sha = head_from_files(cwd or self.root)
        return sha or self.git('rev-parse', 'HEAD', cwd=cwd)

    def worktree(self, name, branch=None):
        """A linked worktree at <root>/.claude/worktrees/<name> on a new branch (default kit/<name>)."""
        path = os.path.join(self.root, '.claude', 'worktrees', name)
        self._own('worktree', 'add', '-q', path, '-b', branch or 'kit/%s' % name)
        return path

    def common_dir(self):
        return os.path.join(self.root, '.git')

    def cleanup(self):
        rmtree(self.base)


def stale_local_main(repo, name='later.txt'):
    """Make the local `main` of a TempRepo(bare_remote=True) one commit behind origin/main (the
    state of a main checkout nobody pulled into). Returns (stale hash, fresh hash); the checkout
    stays on `main`, whose tree is the fresh one's."""
    stale = repo.head()
    repo.write(name, 'later\n')
    fresh = repo.commit('later', name)
    repo.git('push', '-q', 'origin', 'main')
    repo.git('update-ref', 'refs/heads/main', stale)
    return stale, fresh
