"""A running hub server that restarts itself through its scheduled task once newer hub code has
landed (docs/action-hub.md "Stale code"). serve.Hub._watch_code takes serve.code_stamp at most
every serve.CODE_LOOK_S and hands each stamp to its Watcher's look(); the Watcher says when the
stale warning (codestale) shows, and starts the restart.

What look() does, in turns, all on the caller's thread except the work marked (thread):
  1. A stamp other than the one this server started on, or other than the last one seen, starts
     the wait again: the code must stay the same for SETTLE_S (a landing that is still writing
     the tree keeps changing it). The first difference logs CHANGED.
  2. Whether this server may restart itself (thread, `eligible`): the machine config's hub port
     and store are this server's, and the scheduled task autostart.TASK_NAME is Running (the
     task is Running while the server it started runs; a server started by hand, a scratch
     store or another port is not the task's). No task, a task in another state, or one that
     cannot be read: NOT_HERE once, and the stale warning shows at once, as before this module.
  3. Once settled, the self-check (thread): eligible again, then a child python (CHECK_SNIPPET,
     self_check) that imports every module of serve.code_folders() but the timed jobs' own (a job
     that fails is a start-up note of the new server, not a failed start), runs tools/hub.py's top level
     and builds, starts and asks GET health of a hub on a temp store and temp token folder, on
     port 0 with no tunnel, sync, push, backlog or jobs. Exit 0 within CHECK_TIMEOUT_S passes.
     A failure logs CHECK_FAILED and the stale warning shows; that stamp is never tried again.
  4. With the check passed for the stamp still on disk, and no request in flight (`quiet`: every
     listener drained but its counted waits, no timed job running), the server logs RESTARTING
     (the old and new stamps) to the hub log, writes the first line of LOG_NAME in its token
     folder (overwritten each attempt), starts the restarter detached (kitlib.lock.detach: WMI
     and the carrier, so it is in neither this process's tree nor the task's job) and logs
     LAUNCHED with the stamps and the restarter's pid.
     The restarter (RESTARTER_SNIPPET, `restarter`) runs `hub.py restart --port <port>` with its
     output appended to that file: kitlib.hubsite.control stops this server with its clean
     close, waits for its port and process, waits for the task to leave Running, starts the task
     and waits for the new server's GET health. A restarter that cannot be started logs
     LAUNCH_FAILED and the stale warning shows.
A server still running LAUNCH_WAIT_S after its restarter started logs NOT_RESTARTED and the stale
warning shows. Once a restarter was started, look() does nothing more in that process, for any
stamp, newer ones too: the owner's restart is the way on. Before that, one attempt per stamp: a
stamp whose check failed or whose restarter failed to start is not tried again; only a newer
stamp, settled again, is. The new server starts on the stamp now on disk, so it has nothing to
restart for. Until a
restart is given up the stale warning stays hidden, so no restart to-do is filed for a restart
that is about to happen; past GIVE_UP_S from the first difference with no restarter started it
shows (STILL_TRYING), and the restart is still made when the turns allow.

Known limits: a restarter that stops this server and then cannot start the task (or whose new
server never answers) leaves no hub; LOG_NAME says why. The check proves the new code builds and
answers on a scratch store, not that the machine's store and settings serve.
Nothing here prints or exits: the two snippets are what a child python runs.
"""
import datetime
import hashlib
import os
import subprocess
import sys
import threading
import time

from kitlib import platform
from kitlib.proc import spawn

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HUB_TOOL = os.path.join(KIT_ROOT, 'tools', 'hub.py')
SETTLE_S = 90               # the code must be unchanged this long before the check
GIVE_UP_S = 900             # past this from the first difference, the stale warning shows
CHECK_TIMEOUT_S = 120       # the self-check's child python
RESTART_TIMEOUT_S = 300     # hub.py restart --timeout
LAUNCH_WAIT_S = RESTART_TIMEOUT_S + 60  # a server still here this long after: the warning shows
LOG_NAME = 'self-restart.log'   # in the hub's token folder (auth.folder), one attempt each
RUNNING = 'Running'
DETAIL_MAX = 300            # characters of a child's last line in a log line

CHECK_SNIPPET = ('import sys;sys.path.insert(0,sys.argv[1]);'
                 'from kitlib.hubsite import selfrestart;selfrestart.self_check()')
RESTARTER_SNIPPET = ('import sys;sys.path.insert(0,sys.argv[1]);'
                     'from kitlib.hubsite import selfrestart;'
                     'selfrestart.restarter(sys.argv[2],int(sys.argv[3]))')

CHANGED = ('the hub code on disk changed: this server restarts itself through its scheduled task '
           'once the code has stayed the same for %d s, the new code passes its self-check and no '
           'request is in flight' % SETTLE_S)
NOT_HERE = 'this server does not restart itself: %s'
CHECK_FAILED = ('the new hub code failed its self-check, so this server keeps the old code and '
                'does not restart itself for this code: %s')
LAUNCH_FAILED = ('the restarter could not be started, so this server keeps the old code and does '
                 'not restart itself for this code: %s')
RESTARTING = 'restarting itself: code %s -> %s'
LAUNCHED = ('restarting itself: code %s -> %s; restarter pid %d runs python hub.py restart --port '
            '%d, its output in %s')
STILL_TRYING = ('the hub code changed %d s ago and this server has not restarted itself yet (the '
                'code is still changing, a request is in flight or the check has not ended); it '
                'still will when it can')
NOT_RESTARTED = ('the restarter started %d s ago has not restarted this server, which keeps the '
                 'old code; its log: %s')
NO_TASK = "there is no scheduled task '%s' (hub.py setup --autostart makes it)"
NOT_RUNNING = ("the scheduled task '%s' is %s, not Running, so this server is not the task's "
               "(a server started by hand)")
UNREADABLE = "the state of the scheduled task '%s' could not be read: %s"
OTHER_PORT = "this server listens on port %d and the machine config's hub.port is %d"
OTHER_STORE = "this server serves %s and the machine config's store is %s"
NO_CONFIG = 'the machine config could not be read: %s'


class SelfCheckError(Exception):
    """The scratch hub of the self-check did not start or did not answer."""


def digest(stamp):
    """A short name for a serve.code_stamp: 12 hex digits of its hash and its file count."""
    try:
        n = len(stamp)
    except TypeError:
        n = 0
    return '%s (%d files)' % (hashlib.sha1(repr(stamp).encode('utf-8')).hexdigest()[:12], n)


def _last_line(text):
    lines = [x.strip() for x in (text or '').splitlines() if x.strip()]
    said = lines[-1] if lines else 'no output'
    return said if len(said) <= DETAIL_MAX else said[:DETAIL_MAX - 3] + '...'


def _same_folder(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.normpath(a)) == \
        os.path.normcase(os.path.normpath(b))


def task_state():
    """autostart.task_state, looked up when called (autostart imports serve, which imports this)."""
    from kitlib.hubsite import autostart
    return autostart.task_state()


def load_settings():
    from kitlib.hubsite import settings
    return settings.load()


def eligible(port, store, task_state=None, load_settings=None):
    """None when the server on `port` serving `store` is the scheduled task's and may restart
    itself, else why not (one of the texts above). Reads the machine config, then the task's
    state (one PowerShell call, up to autostart.TASK_TIMEOUT_S). Never raises."""
    from kitlib.hubsite import autostart
    task_state = task_state or globals()['task_state']
    load_settings = load_settings or globals()['load_settings']
    try:
        s = load_settings()
    except Exception as e:          # noqa: BLE001 - a config that cannot be read: no restart
        return NO_CONFIG % ('%s: %s' % (type(e).__name__, e))
    if s.port != port:
        return OTHER_PORT % (port, s.port)
    if not _same_folder(s.store, store):
        return OTHER_STORE % (store, s.store)
    try:
        state = task_state()
    except Exception as e:          # noqa: BLE001 - AutostartError, or a stand-in's
        return UNREADABLE % (autostart.TASK_NAME, e)
    if state is None:
        return NO_TASK % autostart.TASK_NAME
    if state != RUNNING:
        return NOT_RUNNING % (autostart.TASK_NAME, state)
    return None


def check(timeout=None):
    """Run the self-check in a child python: (True, None) when it exited 0 within `timeout`
    (default CHECK_TIMEOUT_S), else (False, why: its last output line, or the timeout)."""
    timeout = CHECK_TIMEOUT_S if timeout is None else timeout
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
    try:
        r = spawn.run([sys.executable, '-B', '-c', CHECK_SNIPPET, KIT_ROOT], cwd=KIT_ROOT,
                      env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                      encoding='utf-8', errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, 'the check did not end within %d s' % timeout
    except OSError as e:
        return False, 'the check could not be started: %s' % e
    if r.returncode != 0:
        return False, 'exit %d: %s' % (r.returncode, _last_line(r.stderr or r.stdout))
    return True, None


def self_check(kit_root=None, make_settings=None, make_sources=None):
    """What the self-check's child does; raises when the code does not start (the child's
    traceback is its last output line's source). Imports every module of serve.code_folders()
    under `kit_root` (default KIT_ROOT) but the timed jobs' own modules (the server serves without
    a job whose module fails, with a start-up note, so a broken job is no reason to keep old
    code; their package is imported and listed), runs tools/hub.py's top level, then builds a hub from
    make_settings(tmp) and make_sources(tmp) (default: the machine's settings and sources with a
    temp store and temp token folder, port 0, no tunnel, sync, push, backlog, lock or jobs),
    starts it, asks its GET health and closes it. `make_*`: the tests' stand-ins."""
    import importlib
    import runpy
    import shutil
    import tempfile
    from kitlib import hubjobs
    from kitlib.hubsite import api, auth, serve
    from kitlib.hubsite import settings as hub_settings
    kit_root = kit_root or KIT_ROOT
    jobs_folder = os.path.dirname(os.path.abspath(hubjobs.__file__))
    for folder in serve.code_folders():
        package = os.path.relpath(folder, kit_root).replace(os.sep, '.')
        for name in sorted(os.listdir(folder)):
            if not name.endswith('.py'):
                continue
            if name == '__init__.py':
                importlib.import_module(package)
            elif not _same_folder(folder, jobs_folder):     # a job: serve.build notes its failure
                importlib.import_module('%s.%s' % (package, name[:-3]))
    hubjobs.makers()
    runpy.run_path(os.path.join(kit_root, 'tools', 'hub.py'), run_name='hub_self_check')

    def machine_settings(tmp):
        return hub_settings.load()._replace(store=os.path.join(tmp, 'store'), port=0,
                                             push=False, access=hub_settings.Access())

    def machine_sources(tmp):
        src = serve.machine_sources(sync=False)
        src.tokens = os.path.join(tmp, 'tokens')
        src.backlog = src.lock = src.push = src.push_dir = None
        src.jobs = []
        return src

    tmp = tempfile.mkdtemp(prefix='hub-self-check-')
    try:
        src = (make_sources or machine_sources)(tmp)
        hub = serve.build((make_settings or machine_settings)(tmp), src)
        try:
            if not hub.start():
                raise SelfCheckError('the scratch hub did not start')
            status, obj = serve.ask(hub.port, 'GET', api.BASE + '/health',
                                    token=auth.read_token(src.tokens, auth.AGENT))
            if status != 200 or obj.get('ok') is not True:
                raise SelfCheckError('the scratch hub answered GET health with %d' % status)
        finally:
            hub.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def restarter(log_path, port, hub_tool=None, call=None):
    """What the detached restarter does: append its pid to `log_path`, run `hub.py restart --port
    port --timeout RESTART_TIMEOUT_S` with its output appended there too, then its exit code. A
    log that cannot be written is skipped; the restart is still run. call: the tests' stand-in
    for spawn.call."""
    hub_tool = hub_tool or HUB_TOOL
    call = call or spawn.call

    def note(text):
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write('%s %s\n' % (_now(), text))
        except OSError:
            pass

    note('restarter pid %d (started by pid %d): python hub.py restart --port %d'
         % (os.getpid(), os.getppid(), port))
    argv = [sys.executable, '-B', hub_tool, 'restart', '--port', str(port),
            '--timeout', str(RESTART_TIMEOUT_S)]
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
    try:
        try:
            out = open(log_path, 'a', encoding='utf-8')
        except OSError:
            out = None
        try:
            code = call(argv, stdin=subprocess.DEVNULL, stdout=out or subprocess.DEVNULL,
                        stderr=subprocess.STDOUT, cwd=os.path.dirname(os.path.dirname(hub_tool)),
                        env=env)
        finally:
            if out is not None:
                out.close()
    except Exception as e:          # noqa: BLE001 - said in the log, the only place anyone looks
        note('hub.py restart could not be run: %s: %s' % (type(e).__name__, e))
        return
    note('hub.py restart exit %s' % code)


def restarter_argv(log_path, port, python=None):
    """The detached restarter's argv: on Windows the pythonw.exe beside `python` (default this
    interpreter) running the carrier tools/no_window.py, which runs the python.exe beside it with
    RESTARTER_SNIPPET (kitlib.lock.detach.tool_command's shape, with -c for the tool); on Linux
    `python -B -c ...`."""
    from kitlib.lock import detach
    python = python or sys.executable
    tail = ['-B', '-c', RESTARTER_SNIPPET, KIT_ROOT, log_path, str(port)]
    if platform.NAME == 'linux':
        return [python] + tail
    pythonw, console = detach.pythons(python)
    return [pythonw, os.path.join(KIT_ROOT, 'tools', spawn.CARRIER), console] + tail


def launch(argv):
    """Start `argv` detached (kitlib.lock.detach.create, from the kit folder): (pid, None) or
    (None, why), nothing started when its pythonw.exe, carrier or python.exe is not a file."""
    from kitlib.lock import detach
    if platform.NAME != 'linux':
        why = detach.missing(argv)
        if why:
            return None, why
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
    return detach.create(argv, KIT_ROOT, env)


def in_thread(fn):
    """Run fn() on a daemon thread of its own (the Watcher's default `run`)."""
    threading.Thread(target=fn, name='hub-self-restart', daemon=True).start()


def run_now(fn):
    """Run fn() here: the tests' `run`, so a turn's work has ended when look() returns."""
    fn()


class Watcher(object):
    """One server's restart on new code (module docstring). `started` is the stamp the server
    started on, `port` and `store` its listener's port and store folder, `tokens` its token
    folder (LOG_NAME goes there), say(text, level) its log, quiet() True when no request is in
    flight. The rest are looked up when used unless given: the tests' stand-ins."""

    def __init__(self, started, port, store, tokens, say, quiet, eligible=None, check=None,
                 launch=None, run=None, clock=time.monotonic):
        self.started = started
        self.port, self.store, self.tokens = port, store, tokens
        self._say = say
        self._quiet = quiet
        self._eligible = eligible
        self._check = check
        self._launch = launch
        self._run = run or in_thread
        self._clock = clock
        self.stale = False          # the stale warning shows
        self.launched = False       # a restarter was started: nothing more is done
        self._launched_at = None
        self._seen = started        # the last stamp look() was given
        self._since = None          # when _seen was first given
        self._first = None          # when the first difference was seen
        self._off = None            # why this server may not restart itself, for _seen
        self._told = None           # the last NOT_HERE reason logged (a newer stamp asks again)
        self._probed = None         # the stamp `eligible` was asked for
        self._passed = None         # the stamp whose check passed
        self._done = set()          # stamps never tried again (check failed, launch failed)
        self._work = None           # (kind, stamp, box) of the work on its thread

    @property
    def log_path(self):
        return os.path.join(self.tokens, LOG_NAME) if self.tokens else None

    def _show(self):
        self.stale = True

    def _start(self, kind, stamp, fn):
        box = []

        def work():
            try:
                box.append(fn())
            except Exception as e:  # noqa: BLE001 - a failure of the work is its answer
                why = '%s: %s' % (type(e).__name__, e)
                box.append(why if kind == 'probe' else ('failed', why))
        self._work = (kind, stamp, box)
        self._run(work)
        self._collect()

    def _collect(self):
        if self._work is None or not self._work[2]:
            return
        kind, stamp, box = self._work
        self._work = None
        got = box[0]
        if kind == 'probe':
            if got is not None and stamp == self._seen:
                self._not_here(got)
            return
        verdict, why = got
        if verdict == 'off':
            self._not_here(why)
        elif verdict == 'ok':
            self._passed = stamp
        else:
            self._done.add(stamp)
            self._say(CHECK_FAILED % why)
            self._show()

    def _not_here(self, why):
        self._off = why
        if why != self._told:
            self._told = why
            self._say(NOT_HERE % why, 'note')
        self._show()

    def _probe(self):
        return (self._eligible or eligible)(self.port, self.store)

    def _checked(self):
        why = self._probe()
        if why is not None:
            return ('off', why)
        ok, why = (self._check or check)()
        return ('ok', None) if ok else ('failed', why)

    def look(self, stamp):
        """One turn with the stamp now on disk (module docstring). Never raises out of a log."""
        now = self._clock()
        if self.launched:
            if not self.stale and now - self._launched_at >= LAUNCH_WAIT_S:
                self._say(NOT_RESTARTED % (now - self._launched_at, self.log_path))
                self._show()
            return
        self._collect()
        if stamp != self._seen:
            self._seen, self._since, self._passed = stamp, now, None
            if self._off is not None:
                self._off, self._probed = None, None    # ask again for newer code
            if self._first is None:
                self._first = now
                self._say(CHANGED, 'note')
        if stamp == self.started or self._work is not None:
            return
        if not self.stale and now - self._first >= GIVE_UP_S:
            self._say(STILL_TRYING % (now - self._first), 'note')
            self._show()
        if self._off is not None or stamp in self._done:
            return
        if self._probed is None:
            self._probed = stamp
            self._start('probe', stamp, self._probe)
            return
        if now - self._since < SETTLE_S:
            return
        if self._passed != stamp:
            self._start('check', stamp, self._checked)
            return
        if not self._quiet():
            return
        self._restart(stamp)

    def _restart(self, stamp):
        path = self.log_path
        old, new = digest(self.started), digest(stamp)
        self._say(RESTARTING % (old, new), 'note')
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write('%s hub server on port %d restarting itself: code %s -> %s\n'
                        % (_now(), self.port, old, new))
        except (OSError, TypeError) as e:
            self._done.add(stamp)
            self._say(LAUNCH_FAILED % ('its log %s cannot be written: %s' % (path, e)))
            self._show()
            return
        try:
            pid, why = (self._launch or launch)(restarter_argv(path, self.port))
        except Exception as e:      # noqa: BLE001 - said, and the old server goes on
            pid, why = None, '%s: %s' % (type(e).__name__, e)
        if pid is None:
            self._done.add(stamp)
            self._say(LAUNCH_FAILED % why)
            self._show()
            return
        self.launched, self._launched_at = True, self._clock()
        self._say(LAUNCHED % (old, new, pid, self.port, path), 'note')
