"""What `hub.py stop` and `hub.py restart` do: ask the running hub server to stop, wait for it to
close, and for restart start the scheduled task (kitlib.hubsite.autostart) that runs it.

stop() posts httpd.STOP_ROUTE with the owner's token. The server answers 200 with its pid and
creation time and closes as it does on Ctrl+C (serve.run's clean close: its listeners, the
store's last write, its lock). stop() then waits until nothing listens on the port, and then
until that process has ended. A server that is already closing (it answers 503 with
httpd.CLOSING) is waited for on its port alone. Nothing here ever ends a process or a task: a
server that does not close within the time given is TimedOut, and the owner decides.

Nothing answering on the port does not prove that no server is left. A closing server frees its
port first (its listeners' doors) and only then makes its last git pass and closes the store, so
for a while after its port is free it still runs and holds the store lock: the pass waits for
git, and each fetch and push may take gitsync.NET_TIMEOUT (120 s). There is no pid file, so a
stop that finds nothing on the port says only that nothing answers there.

restart() reads the task's state first, so an unreadable one stops nothing; then it stops the
server. It starts the task only when the task ran that server (the task is Running, or Queued,
while its server runs, because the carrier waits for it) or when nothing ran and the task is
ready. A server started by hand is stopped and never started again (HAND_STARTED): hub.py has no
way to start it as its owner did. No task (NO_TASK) or a disabled one (DISABLED) starts nothing.
Once started, it waits until the new server answers GET health on `new_port`. One deadline
covers every phase. A task that cannot be read or started once a server was stopped is
StartFailed, which carries what was stopped.

The deadline is approximate: it is looked at between polls, and one poll may run past it by up
to serve.ASK_TIMEOUT_S (a request to the server) or autostart.TASK_TIMEOUT_S (a read of the
task's state).

ask, alive, task_state and start_task are looked up when called (serve.ask, process.alive and
autostart's own), so a test's patch of the module attribute reaches them.
"""
import collections
import time

from kitlib.hubsite import api as _api
from kitlib.hubsite import autostart, gitsync, httpd, serve
from kitlib.proc import process

POLL_S = 0.25
DEFAULT_TIMEOUT_S = 300
HEALTH = _api.BASE + '/health'

CLOSING_PHASE = 'closing'       # waiting for the port to stop answering
PROCESS_PHASE = 'process'       # waiting for the server's process to end
TASK_PHASE = 'task'             # waiting for the scheduled task to leave Running
ANSWER_PHASE = 'answer'         # waiting for the new server to answer GET health

RESTARTED = 'restarted'         # the task was started and its server answers
NO_TASK = 'no-task'             # there is no task: nothing was started
DISABLED = 'disabled'           # the task is disabled: nothing was started
HAND_STARTED = 'hand-started'   # the server stopped was not the task's: nothing was started

RUNNING = 'Running'
QUEUED = 'Queued'
ACTIVE = (RUNNING, QUEUED)      # the task's states while it runs its server, or is about to
TASK_DISABLED = 'Disabled'
NOTHING_KILLED = 'nothing was killed'
# what a port that answers nothing may still hide: a server whose close has freed its port
STILL_CLOSING = ('a hub server stopped a moment ago may still be closing: it frees its port '
                 'first and can hold the store for some minutes more (its last git pass: each '
                 'fetch and push may take up to %d s)' % gitsync.NET_TIMEOUT)

# ran: a server was asked to stop and has closed; pid: its pid when it answered one; already:
# it was already closing when asked
Stopped = collections.namedtuple('Stopped', 'ran pid already')
# stopped: the Stopped; outcome: one of the outcomes above; port: where the new server answers
Restarted = collections.namedtuple('Restarted', 'stopped outcome port')


class Refused(serve.ServeError):
    """The server answered the stop request with something other than "stopping": `status` and
    `answer` are what it said (a 403 for a token that is not the owner's)."""

    def __init__(self, port, status, answer):
        serve.ServeError.__init__(self, 'the hub server on port %d answered %d to the stop request'
                                  % (port, status))
        self.port, self.status, self.answer = port, status, answer


class TimedOut(serve.ServeError):
    """A wait ran past the deadline. `phase` is one of the phases above; nothing was ended."""

    def __init__(self, phase, message):
        serve.ServeError.__init__(self, message)
        self.phase = phase


class StartFailed(autostart.AutostartError):
    """restart() stopped (or found nothing to stop) and then could not read the task's state or
    start it: `stopped` is the Stopped, `reason` the AutostartError. Nothing was started."""

    def __init__(self, stopped, reason):
        autostart.AutostartError.__init__(self, str(reason))
        self.stopped, self.reason = stopped, reason


def _closing(status, obj):
    """True when (status, obj) is the answer of a server that is already closing."""
    error = obj.get('error') if status == 503 else None
    return (isinstance(error, dict) and error.get('code') == 'store'
            and error.get('message') == httpd.CLOSING)


def _whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _wait(done, deadline, clock, pause):
    """Ask `done()` every POLL_S until it is true (True) or the deadline has passed (False).
    The deadline is looked at between calls, never during one, so a wait can run past it by one
    call of `done()`: up to serve.ASK_TIMEOUT_S for a request to the server, or
    autostart.TASK_TIMEOUT_S for a read of the task's state."""
    while True:
        if done():
            return True
        if clock() >= deadline:
            return False
        pause(POLL_S)


def _port_closed(port, token, ask):
    def done():
        try:
            ask(port, 'GET', HEALTH, token=token)
        except serve.NotRunning:
            return True
        except serve.ServeError:
            return False        # a connection cut while it closes: still closing
        return False
    return done


def stop(port, token, timeout, ask=None, alive=None, clock=time.monotonic, pause=time.sleep,
         deadline=None):
    """Ask the hub server on `port` to stop with the owner's `token`, and wait up to `timeout`
    seconds (or until `deadline` on `clock`; approximate, _wait) for it to close and its process
    to end. Stopped; Stopped(False, None, False) when nothing answers on the port, which a
    server still closing after its port is free also gives (module docstring). Refused when it
    answers
    anything but "stopping", ServeError when what answers is not the hub, TimedOut when it
    does not close in time (nothing is ended)."""
    ask = ask or serve.ask
    alive = alive or process.alive
    if deadline is None:
        deadline = clock() + timeout
    try:
        status, obj = ask(port, 'POST', httpd.STOP_ROUTE, token=token)
    except serve.NotRunning:
        return Stopped(False, None, False)
    already = _closing(status, obj)
    if not already and not (status == 200 and obj.get('ok') is True
                            and obj.get('stopping') is True):
        raise Refused(port, status, obj)
    if not _wait(_port_closed(port, token, ask), deadline, clock, pause):
        raise TimedOut(CLOSING_PHASE, 'the hub server on port %d still answers: it did not close '
                       'within %s s; %s' % (port, _secs(timeout), NOTHING_KILLED))
    pid, ctime = (None, None) if already else (obj.get('pid'), obj.get('ctime'))
    if not (_whole(pid) and _whole(ctime)):
        return Stopped(True, None, already)
    if not _wait(lambda: not alive(pid, ctime), deadline, clock, pause):
        raise TimedOut(PROCESS_PHASE, 'the hub server on port %d has closed its port, but its '
                       'process (pid %d) did not end within %s s; %s'
                       % (port, pid, _secs(timeout), NOTHING_KILLED))
    return Stopped(True, pid, False)


def _secs(timeout):
    return ('%d' % timeout) if float(timeout).is_integer() else ('%g' % timeout)


def _answers(port, token, ask):
    def done():
        try:
            status, obj = ask(port, 'GET', HEALTH, token=token)
        except serve.ServeError:
            return False        # not up yet (NotRunning), or cut while it starts
        return status == 200 and obj.get('ok') is True
    return done


def restart(port, token, timeout, new_port=None, ask=None, alive=None, task_state=None,
            start_task=None, clock=time.monotonic, pause=time.sleep):
    """Stop the hub server on `port` and start the scheduled task again, waiting up to `timeout`
    seconds in all (approximate, _wait) until the new server answers on `new_port` (default
    `port`). Restarted; its outcome says whether the task was started (module docstring).
    AutostartError when the task's state cannot be read (nothing is stopped); StartFailed, with
    what was stopped, when a read of the state after the stop or the start fails; ServeError
    when the task is Running but nothing answers on `port` (its server is on another port, or
    is still closing after its port is free: nothing is stopped or started); stop's errors;
    TimedOut (nothing is ended)."""
    ask = ask or serve.ask
    task_state = task_state or autostart.task_state
    start_task = start_task or autostart.start_task
    new_port = port if new_port is None else new_port
    deadline = clock() + timeout
    state = task_state()
    stopped = stop(port, token, timeout, ask=ask, alive=alive, clock=clock, pause=pause,
                   deadline=deadline)
    if state is None:
        return Restarted(stopped, NO_TASK, new_port)
    if state == TASK_DISABLED:
        return Restarted(stopped, DISABLED, new_port)
    if state not in ACTIVE and stopped.ran:
        return Restarted(stopped, HAND_STARTED, new_port)
    if state == RUNNING and not stopped.ran:
        raise serve.ServeError(
            "nothing answers on port %d, yet the task '%s' is running: its server may be on "
            "another port (name it with --port), or %s. Nothing was stopped or started"
            % (port, autostart.TASK_NAME, STILL_CLOSING))
    try:
        if state in ACTIVE and stopped.ran:
            seen = [state]

            def ended():
                seen[0] = task_state()
                return seen[0] not in ACTIVE
            if not _wait(ended, deadline, clock, pause):
                raise TimedOut(TASK_PHASE, "the task '%s' was still running %s s after its server "
                               "was asked to stop, so it was not started again; %s"
                               % (autostart.TASK_NAME, _secs(timeout), NOTHING_KILLED))
            if seen[0] is None:
                return Restarted(stopped, NO_TASK, new_port)
            if seen[0] == TASK_DISABLED:
                return Restarted(stopped, DISABLED, new_port)
        start_task()
    except autostart.AutostartError as e:
        raise StartFailed(stopped, e)
    if not _wait(_answers(new_port, token, ask), deadline, clock, pause):
        raise TimedOut(ANSWER_PHASE, "the task '%s' was started, but no hub server answered on "
                       "port %d within %s s" % (autostart.TASK_NAME, new_port, _secs(timeout)))
    return Restarted(stopped, RESTARTED, new_port)
