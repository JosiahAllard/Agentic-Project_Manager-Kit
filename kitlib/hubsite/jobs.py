"""kitlib.hubsite.jobs: the runner of the hub server's timed jobs, one thread each. The jobs
themselves are modules of kitlib.hubjobs (its docstring: adding a job); the first is the plugin
refresh (kitlib.hubsite.plugin_job, docs/plugin-source.md "Hub refresh").

  Job(name, every_s, run, first_s=FIRST_S)   one job: `run(ctx)` every `every_s` seconds, the
                                             first time `first_s` after start
  Skip(note)                                 what a run returns when it did nothing
  Jobs(jobs, notify=None, log=None)          the jobs of one hub
      .start()                               start each job's thread (once)
      .stop(timeout)                         ask every thread to end and join them: True when all
                                             ended in `timeout` (None: wait)
      .status()                              {name: health block} (below); any thread
      .run_now(name)                         one run on the caller's thread (tests): False when
                                             that job is running already

A job's `run(ctx)` gets a Ctx: `ctx.stop`, the event the close sets (a long job looks at it
between steps), and `ctx.event(word, detail=None)`, which pushes a job event to the PM
(`notify(name, word)`, or `notify(name, word, detail)` when a detail is given:
kitlib.hubsite.push PushWorker.job_event, where the detail is one more header field cut to its
alphabet, a short token such as a sha or a count, never prose; nothing when the hub has no push
worker). What run
returns, a short text or None, is a success and the health block's `note`; a Skip(note) says the
run did nothing, and only sets `note`. What it raises is a failure: its class and text are
`error`, and `failures` counts the failures since the last success (skips neither count nor
reset it). At the FAIL_PUSH-th failure in a row the job pushes `failing`, once; the first
success after that pushes `recovered`. A job never runs twice at once: its own thread runs
it, and run_now refuses while it is running.

Health block (GET health `jobs`, kitlib.hubsite.api docstring "Jobs"): {every_s, runs, failures, running,
last_at, ok_at, note, error}: times are epoch seconds of the last run's end and of the last
success (None before one), `error` the last failure's text while `failures` is not 0
(else None).

Nothing here knows any job: each is a module of kitlib.hubjobs that gives a Job, found by
kitlib.hubjobs.makers in serve.machine_sources (Sources.jobs). The existing timers (the git
sync, the backlog views, the push worker) are not jobs.
"""
import threading
import time

FIRST_S = 30            # a job's first run, after start: the hub serves before any job runs
FAIL_PUSH = 3           # failures in a row that push `failing` (pm_watch's ERROR rule)
ERROR_MAX = 300         # characters of a failure's text kept for health


class Job(object):
    def __init__(self, name, every_s, run, first_s=FIRST_S):
        self.name, self.every_s, self.run, self.first_s = name, every_s, run, first_s


class Skip(object):
    """What a run returns when it did nothing (nothing to do yet, a switch off, another process
    busy, a retry not due): `note` goes to health; the failure count, `error`, `ok_at` and the
    failing episode stay as they were."""

    def __init__(self, note):
        self.note = note


class Ctx(object):
    """What a job's run gets: `stop` (threading.Event) and `event(word, detail=None)`."""

    def __init__(self, stop, event):
        self.stop = stop
        self.event = event


class _State(object):
    def __init__(self, job):
        self.job = job
        self.runs = 0
        self.failures = 0
        self.running = False
        self.last_at = None
        self.ok_at = None
        self.note = None
        self.error = None
        self.pushed = False             # `failing` pushed and not yet `recovered`
        self.thread = None


class Jobs(object):
    def __init__(self, jobs, notify=None, log=None, clock=None):
        self._states = []
        for j in jobs:
            if any(s.job.name == j.name for s in self._states):
                raise ValueError('two jobs named %r' % j.name)
            self._states.append(_State(j))
        self.notify = notify
        self.log = log or (lambda text: None)
        self.clock = clock or time.time
        self._stop = threading.Event()
        self._guard = threading.Lock()  # running flags
        self._started = False

    def names(self):
        return [s.job.name for s in self._states]

    def start(self):
        if self._started:
            return
        self._started = True
        for s in self._states:
            s.thread = threading.Thread(target=self._loop, args=(s,), name='hub-job-' + s.job.name,
                                        daemon=True)
            s.thread.start()

    def stop(self, timeout=None):
        self._stop.set()
        end = None if timeout is None else time.monotonic() + timeout
        done = True
        for s in self._states:
            t = s.thread
            if t is None:
                continue
            t.join(None if end is None else max(0.0, end - time.monotonic()))
            done = done and not t.is_alive()
        return done

    def status(self):
        return {s.job.name: {'every_s': s.job.every_s, 'runs': s.runs, 'failures': s.failures,
                             'running': s.running, 'last_at': s.last_at, 'ok_at': s.ok_at,
                             'note': s.note, 'error': s.error if s.failures else None}
                for s in self._states}

    def run_now(self, name):
        for s in self._states:
            if s.job.name == name:
                return self._once(s)
        raise KeyError(name)

    # ------------------------------------------------------------------ inside
    def _loop(self, s):
        wait = s.job.first_s
        while not self._stop.wait(wait):
            self._once(s)
            wait = s.job.every_s

    def _once(self, s):
        with self._guard:
            if s.running:
                return False
            s.running = True
        try:
            try:
                note = s.job.run(Ctx(self._stop,
                                     lambda word, detail=None: self._event(s, word, detail)))
            except Exception as e:      # noqa: BLE001 - a job's failure is counted, never fatal
                s.failures += 1
                s.error = ('%s: %s' % (type(e).__name__, e))[:ERROR_MAX]
                self.log('job %s failed (%d in a row): %s' % (s.job.name, s.failures, s.error))
                if s.failures == FAIL_PUSH and not s.pushed:
                    s.pushed = True
                    self._event(s, 'failing')
            else:
                if isinstance(note, Skip):  # did nothing: the count and the episode stay
                    s.note = note.note
                    s.runs += 1
                    s.last_at = self.clock()
                    return True
                s.failures, s.error = 0, None
                s.note = note if isinstance(note, str) else None
                s.ok_at = self.clock()
                if s.pushed:
                    s.pushed = False
                    self._event(s, 'recovered')
            s.runs += 1
            s.last_at = self.clock()
        finally:
            with self._guard:
                s.running = False
        return True

    def _event(self, s, word, detail=None):
        if self.notify is None:
            return
        try:
            if detail is None:          # a notify(name, word) of before `detail` still works
                self.notify(s.job.name, word)
            else:
                self.notify(s.job.name, word, detail)
        except Exception as e:          # noqa: BLE001 - a push that fails never stops the job
            self.log('job %s: event %s not pushed: %s' % (s.job.name, word, type(e).__name__))
