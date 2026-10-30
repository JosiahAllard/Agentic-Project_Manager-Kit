"""Codex's session reads through the local app-server (kitlib.host.SessionHost; from the Codex branch's
session_host.py and executable.py).

  native_codex(explicit, environ)   the absolute codex.exe to run: `explicit`, else PM_KIT_CODEX_EXE,
                                    else codex.exe on PATH, else the desktop app's own
                                    %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\codex.exe, the newest
                                    by mtime (desktop_codex); a relative path, a missing file or a
                                    .cmd/.bat shim is refused (no shell ever runs it)
  ReadOnlyAppServer(argv, timeout, popen)
        a context manager over `codex app-server proxy`: it attaches to the running app-server,
        never starts a daemon, creates a thread or sends a turn. request() takes only READ_METHODS.
        The proxy's environment is the launching one scrubbed (kitlib.sessionmark.child_environment).
  SessionHost(request)   the SessionHost over any read-only request callable:
        sessions(cwd, max_pages)  [record, ...] from thread/list, each {id, sessionId, name, cwd,
                                  status, updatedAt, source, agentRole, parentThreadId, liveness:
                                  'unknown'}; ValueError when the inventory runs past max_pages
        inventory(cwd, max_pages) {"sessions": [...], "complete": bool}: the same, a partial one
                                  marked incomplete (kitlib.hoststart reads this shape)
        loaded(), usage(), models()   thread/loaded/list, account/rateLimits/read, model/list
  AppServerSessions(argv)   the SessionHost ADAPTER.sessions() hands out: each read opens its own
                            ReadOnlyAppServer. An unavailable proxy is an error, never an empty list.

Loaded metadata is not process liveness: every record says liveness 'unknown'.
"""
import os
from pathlib import Path
import shutil
import subprocess

from kitlib import sessionmark

HOST = 'codex'
EXE_ENV = 'PM_KIT_CODEX_EXE'
READ_METHODS = frozenset({'thread/list', 'thread/loaded/list', 'account/rateLimits/read', 'model/list'})
RECORD = ('id', 'sessionId', 'name', 'cwd', 'status', 'updatedAt', 'source', 'agentRole', 'parentThreadId')


def desktop_codex(environ=None):
    """The Codex desktop app's own codex.exe: the newest by mtime of
    %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\codex.exe (an update adds a <hash> folder; a folder
    without codex.exe is skipped). None when LOCALAPPDATA is unset or no folder holds one."""
    env = os.environ if environ is None else environ
    local = env.get('LOCALAPPDATA')
    if not local:
        return None
    found = []
    for exe in Path(local, 'OpenAI', 'Codex', 'bin').glob('*/codex.exe'):
        try:
            found.append((exe.stat().st_mtime, str(exe)))
        except OSError:
            continue
    return max(found)[1] if found else None


def native_codex(explicit=None, environ=None):
    env = os.environ if environ is None else environ
    selected = (explicit or env.get(EXE_ENV) or shutil.which('codex.exe', path=env.get('PATH', ''))
                or desktop_codex(env))
    if not selected:
        raise ValueError('codex.exe is unavailable; provide an absolute %s or --codex-exe path' % EXE_ENV)
    path = Path(selected)
    if not path.is_absolute() or path.suffix.lower() != '.exe' or not path.is_file():
        raise ValueError('Codex requires an existing absolute .exe path; .cmd/.bat shims are unsupported')
    return str(path.resolve())


class ReadOnlyAppServer:
    """Attach a stdio proxy; never start a daemon, create a thread or send a turn/message."""

    def __init__(self, argv=None, timeout=10, popen=None):
        if not 0 < timeout <= 60:
            raise ValueError('read timeout must be between 0 and 60 seconds')
        self.argv = tuple(argv) if argv is not None else (str(native_codex()), 'app-server', 'proxy')
        if popen is None:
            from kitlib.proc import spawn
            popen = spawn.popen
        self.timeout, self.popen = timeout, popen
        self.process, self.rpc = None, None
        self.cleanup_errors = ()

    def __enter__(self):
        from kitlib.hostrpc import Client
        self.process = self.popen(list(self.argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1,
                                  shell=False, env=sessionmark.child_environment(os.environ, HOST))
        self.rpc = Client(self.process.stdout, self.process.stdin, timeout=self.timeout)
        self.reader = self.rpc.reader
        try:
            self._request('initialize', {'clientInfo': {'name': 'pm_kit', 'version': _version()}})
            self._send({'method': 'initialized'})
        except BaseException:
            self.close()
            raise
        return self

    def _send(self, message):
        self.rpc.send(message)

    def _request(self, method, params):
        return self.rpc.request(method, params)

    def request(self, method, params=None):
        if method not in READ_METHODS:
            raise ValueError('session adapter permits metadata reads only')
        if self.process is None:
            raise RuntimeError('open the read-only app-server context first')
        return self._request(method, params or {})

    def close(self):
        if self.process is None:
            return self.cleanup_errors
        process = self.process
        errors = []
        try:
            process.stdin.close()
        except Exception as error:
            errors.append('stdin close: ' + str(error))
        for operation in (None, process.terminate, process.kill):
            try:
                if operation is not None:
                    operation()            # this proxy only; never the app-server/peer daemon
                process.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                if operation == process.kill:
                    errors.append('proxy did not exit after kill')
            except Exception as error:
                errors.append('proxy cleanup: ' + str(error))
        self.reader.join(timeout=0.2)
        if self.reader.is_alive():
            errors.append('proxy reader still open after bounded cleanup')
        self.process = None
        self.cleanup_errors = tuple(errors)
        return self.cleanup_errors

    def __exit__(self, exc_type, *_):
        errors = self.close()
        if errors and exc_type is None:
            raise RuntimeError('app-server cleanup failed: ' + '; '.join(errors))


def _version():
    from kitlib import content, kitroot
    return content.load_kit(kitroot.root())['version']


class SessionHost:
    """kitlib.host.SessionHost over any authorized read-only request transport."""

    def __init__(self, request):
        self.request = request

    def inventory(self, cwd=None, max_pages=10):
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
            raise ValueError('invalid pagination bound')
        sessions, cursor, seen = [], None, set()
        for _ in range(max_pages):
            params = {'limit': 100}
            if cursor:
                params['cursor'] = cursor
            if cwd is not None:
                params['cwd'] = cwd
            page = self.request('thread/list', params)
            if not isinstance(page, dict) or not isinstance(page.get('data'), list):
                raise ValueError('invalid session inventory')
            for item in page['data']:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']:
                    raise ValueError('invalid session record')
                record = {k: item.get(k) for k in RECORD}
                record['liveness'] = 'unknown'
                sessions.append(record)
            cursor = page.get('nextCursor')
            if not cursor:
                return {'sessions': sessions, 'complete': True}
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError('invalid or repeated session cursor')
            seen.add(cursor)
        return {'sessions': sessions, 'complete': False}

    def sessions(self, cwd=None, max_pages=10):
        found = self.inventory(cwd, max_pages)
        if not found['complete']:
            raise ValueError('the session inventory runs past %d pages; read inventory() for a partial one'
                             % max_pages)
        return found['sessions']

    def loaded(self):
        return self.request('thread/loaded/list', {})

    def usage(self):
        return self.request('account/rateLimits/read', {})

    def models(self):
        return self.request('model/list', {})


class AppServerSessions:
    """The SessionHost of the running app-server: each call opens and closes its own proxy."""

    def __init__(self, argv=None, server=ReadOnlyAppServer):
        self.argv, self.server = argv, server

    def _call(self, name, *args, **kwargs):
        with self.server(self.argv) as app:
            return getattr(SessionHost(app.request), name)(*args, **kwargs)

    def sessions(self, cwd=None, max_pages=10):
        return self._call('sessions', cwd, max_pages)

    def inventory(self, cwd=None, max_pages=10):
        return self._call('inventory', cwd, max_pages)

    def loaded(self):
        return self._call('loaded')

    def usage(self):
        return self._call('usage')

    def models(self):
        return self._call('models')
