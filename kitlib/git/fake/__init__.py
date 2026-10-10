"""A fake git for the kit's unit tests: `run(args, cwd, stdin, env)` answers `git <args>` in
process, reading and writing the real on-disk repository (loose objects, packs, refs, the index,
config), so a repository it touched stays a valid git repository and real git can carry on.

kitlib.git.cmd routes every git child here when the environment says KIT_FAKE_GIT=1 (tests set
it; production never does). What the fake cannot do exactly as git does it raises Unsupported -
"unsupported in fake git: <what>" - which ends the test; it never falls back to real git.
docs/test-tools.md "Fake git" lists what is covered and what stays on the integration tier.
"""
import os

from . import config as _config
from . import repo as _repo
from .errors import GitExit, Unsupported, die

COMMANDS = {}


def command(name):
    def deco(fn):
        COMMANDS[name] = fn
        return fn
    return deco


class Ctx:
    """One command: where it runs, what it reads and what it writes."""

    def __init__(self, cwd, env, stdin):
        self.cwd = cwd
        self.env = env
        self.stdin = stdin
        self.cli_config = []
        self.git_dir = None
        self.no_optional_locks = False
        self.out = bytearray()
        self.err = []
        self._repo = None
        self._found = False
        self.prefix = None

    def write(self, data):
        self.out += data.encode('utf-8', 'surrogateescape') if isinstance(data, str) else data

    def warn(self, text):
        self.err.append(text)

    def _discover(self):
        if not self._found:
            self._found = True
            self._repo, self.prefix = _repo.discover(self.cwd, self.env, self.cli_config, self.git_dir)
        return self._repo

    @property
    def repo(self):
        return self._discover()

    def need_repo(self):
        r = self._discover()
        if r is None:
            raise die('not a git repository (or any of the parent directories): .git')
        return r

    def need_worktree(self):
        r = self.need_repo()
        if r.worktree is None or getattr(r, 'inside_gitdir', False):
            raise die('this operation must be run in a work tree')
        return r

    def stdin_bytes(self):
        return self.stdin or b''


def run(args, cwd=None, stdin=None, env=None):
    """(rc, stdout bytes, stderr text) for `git <args>`. Raises Unsupported."""
    _load()
    args = [os.fspath(a) if not isinstance(a, str) else a for a in args]
    env = dict(os.environ if env is None else env)
    cwd = os.path.abspath(cwd or os.getcwd())
    if isinstance(stdin, str):
        stdin = stdin.encode('utf-8')
    ctx = Ctx(cwd, env, stdin)
    try:
        i = 0
        while i < len(args):
            a = args[i]
            if a == '-C':
                p = args[i + 1]
                if p:
                    ctx.cwd = os.path.normpath(os.path.join(ctx.cwd, p))
                i += 2
            elif a == '-c':
                ctx.cli_config.append(_config.parse_cli(args[i + 1]))
                i += 2
            elif a == '--git-dir':
                ctx.git_dir = args[i + 1]
                i += 2
            elif a.startswith('--git-dir='):
                ctx.git_dir = a[len('--git-dir='):]
                i += 1
            elif a == '--no-optional-locks':
                ctx.no_optional_locks = True
                i += 1
            elif a in ('--no-replace-objects', '--no-pager', '-P', '--literal-pathspecs'):
                if a == '--literal-pathspecs':
                    raise Unsupported('--literal-pathspecs')
                i += 1
            elif a.startswith('-'):
                raise Unsupported('git global option %s' % a)
            else:
                break
        if i >= len(args):
            raise Unsupported('git with no subcommand')
        name = args[i]
        fn = COMMANDS.get(name)
        if fn is None:
            raise Unsupported('git %s' % name)
        if ctx.git_dir is None and not os.path.isdir(ctx.cwd):
            raise die("cannot change to '%s': No such file or directory" % ctx.cwd.replace('\\', '/'))
        rc = fn(ctx, args[i + 1:])
        rc = 0 if rc is None else rc
    except GitExit as e:
        rc = e.rc
        ctx.err.append(e.err)
    return rc, bytes(ctx.out), ''.join(ctx.err)


_LOADED = []


def _load():
    if not _LOADED:
        from . import plumbing, diffs, history, wtcmds, worktreecmd, remote, mergecmd  # noqa: F401
        _LOADED.append(True)


def missing_cwd_error():
    """What starting a process in a missing folder raises on Windows, as cmd.call words it."""
    return NotADirectoryError(None, 'The directory name is invalid', None, 267)


# subprocess.run keywords completed() answers; any other one is Unsupported.
_RUN_KW = ('input', 'cwd', 'env', 'capture_output', 'stdout', 'stderr', 'stdin', 'text',
           'universal_newlines', 'encoding', 'errors', 'check', 'timeout', 'creationflags',
           'start_new_session', 'close_fds')


def completed(argv, **kw):
    """`subprocess.run(argv, **kw)` for an argv starting git, answered by run() as the
    subprocess.CompletedProcess real git would give: same capture, text and check handling."""
    import locale
    import subprocess
    bad = sorted(set(kw) - set(_RUN_KW))
    if bad:
        raise Unsupported('subprocess.run keyword(s) %s for %s' % (', '.join(bad), list(argv)))
    capture = kw.get('capture_output', False)
    out_to = subprocess.PIPE if capture else kw.get('stdout')
    err_to = subprocess.PIPE if capture else kw.get('stderr')
    # None (inherited) is answered only while git writes nothing to it: a console write is not
    # reproduced, so a non-empty inherited stream is Unsupported after the run.
    for what, to in (('stdout', out_to), ('stderr', err_to)):
        if to not in (None, subprocess.PIPE, subprocess.DEVNULL) and not (what == 'stderr' and to == subprocess.STDOUT):
            raise Unsupported('%s=%r for %s (only captured, discarded or silent output)' % (what, to, list(argv)))
    if kw.get('stdin') not in (None, subprocess.DEVNULL, subprocess.PIPE):
        raise Unsupported('stdin=%r for %s' % (kw.get('stdin'), list(argv)))
    textmode = bool(kw.get('text') or kw.get('universal_newlines') or kw.get('encoding')
                    or kw.get('errors'))
    encoding = kw.get('encoding') or (locale.getpreferredencoding(False) if textmode else None)
    errors = kw.get('errors') or 'strict'
    data = kw.get('input')
    if textmode and data is not None:
        data = data.encode(encoding, errors)
    cwd = kw.get('cwd')
    if cwd is not None and not os.path.isdir(cwd):
        raise missing_cwd_error()
    rc, out, err = run([os.fspath(a) for a in argv[1:]], cwd, data, kw.get('env'))
    err = err.encode('utf-8', 'surrogateescape')
    if err_to == subprocess.STDOUT:
        out, err = err + out, None
    for what, to, data in (('stdout', out_to, out), ('stderr', err_to, err)):
        if to is None and data:
            raise Unsupported('git writing to an inherited %s: %s wrote %r' % (what, list(argv), data[:200]))
    if out_to is None:
        out = None
    if err_to is None:
        err = None
    if out_to == subprocess.DEVNULL:
        out = None
    if err_to == subprocess.DEVNULL:
        err = None

    def dec(b):
        if b is None or not textmode:
            return b
        return b.decode(encoding, errors).replace('\r\n', '\n').replace('\r', '\n')
    res = subprocess.CompletedProcess(argv, rc, dec(out), dec(err))
    if kw.get('check') and rc:
        raise subprocess.CalledProcessError(rc, argv, res.stdout, res.stderr)
    return res


class _Pipe:
    """One end of a BatchProcess pipe."""

    def __init__(self, owner):
        self._owner = owner
        self.closed = False

    def write(self, data):
        if self.closed:
            raise ValueError('write to closed file')
        self._owner._pending += data
        return len(data)

    def flush(self):
        if self.closed:
            raise ValueError('flush of closed file')
        self._owner._answer()

    def readline(self):
        return self._owner._read(line=True)

    def read(self, n=-1):
        return self._owner._read(n=n)

    def close(self):
        self.closed = True


class BatchProcess:
    """A stand-in for a long-lived `git <args>` that answers one flushed request at a time
    (cat-file --batch / --batch-check, kitlib.git.batch.CatFile), each answered by run()."""

    def __init__(self, args, cwd=None, env=None):
        self.args = list(args)
        self.cwd = cwd
        self.env = env
        self._pending = b''
        self._buf = b''
        self.returncode = None
        self.stdin = _Pipe(self)
        self.stdout = _Pipe(self)

    def _answer(self):
        if not self._pending:
            return
        data, self._pending = self._pending, b''
        rc, out, err = run(self.args, self.cwd, data, self.env)
        if rc:
            raise Unsupported('git %s failing (rc %d: %s) inside a batch process'
                              % (' '.join(self.args), rc, err.strip()))
        self._buf += out

    def _read(self, line=False, n=-1):
        if line:
            i = self._buf.find(b'\n')
            end = len(self._buf) if i < 0 else i + 1
        else:
            end = len(self._buf) if n is None or n < 0 else min(n, len(self._buf))
        got, self._buf = self._buf[:end], self._buf[end:]
        return got

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = 1

    terminate = kill
