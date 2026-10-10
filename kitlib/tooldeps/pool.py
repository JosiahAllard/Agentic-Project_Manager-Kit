"""The parent side of kit_tests' worker pool (docs/test-tools.md "kit_tests"): long-lived
interpreters (kitlib/tooldeps/pool_worker.py) that each run test units one after another, so a
run starts about one interpreter per job instead of one per unit.

A worker is started like a unit process was (kitlib.pytests.runner.run_unit): created suspended,
put in its own kill-on-close job (kitlib.proc.job), shielded from kills by pid (runner.shield_unit),
then resumed; cwd the checkout root. Everything a unit starts is in that job too. `run_unit` has
runner.run_unit's signature and result, so kitlib.tooldeps.selfrun calls either:
(unit, status OK|FAILED|TIMEOUT, tests run, seconds, output).

A unit's output is what its fds 1 and 2 received (a file in the pool's folder, so the processes it
starts write there too), with the runner's notes: a unit that ran no test is a FAIL, a FAILED unit
with no unittest summary says how it ended.

A worker is ended and the next unit gets a new one (started on demand) when:
  - the unit is still running after `timeout` seconds: the job is terminated - the worker and
    everything the unit started - and the unit is reported TIMEOUT;
  - the worker ended before it replied (a crash, os._exit): the unit FAILS with the exit status;
  - the worker says so (pool_worker: Ctrl+C, threads still running, an audit hook, a failed reset);
    a unit that left a non-daemon thread running also FAILS, as its own process would never
    have exited (and would have been reported TIMEOUT);
  - a process the unit started is still in the job LEFTOVER_GRACE seconds after it ended (a
    unit process's leftovers were ended by its job's close; here they end with the worker), or
    the job cannot be asked which processes it holds.
An idle worker found ended when it is next taken is replaced before the unit is sent, so no unit
is blamed for it. A worker not ready READY_TIMEOUT seconds after it started is ended and its
unit FAILS, not run.
`stats` counts the workers started and why each was ended. `close` ends them all: stdin closed so
each exits, and its job closed (KILL_ON_JOB_CLOSE) after CLOSE_WAIT seconds at the latest.
Ending a worker returns once every process in its job has ended, GONE_WAIT seconds at most:
terminating a job only starts each process's end, and the worker's can come first.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

from kitlib.proc import process
from kitlib.proc.job import Job, JobError
from kitlib.pytests import runner as unit_runner

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pool_worker.py')
MARK = 'kit-pool: '                 # pool_worker.MARK
READY_TIMEOUT = 120                 # seconds a new worker has to say it is ready
LEFTOVER_GRACE = 2.0
CLOSE_WAIT = 5.0
GONE_WAIT = 5.0                     # seconds an ended worker's job has to empty
POLL = 0.25


class WorkerError(Exception):
    """A worker that could not be started or made ready; the message says why."""


class Worker:
    def __init__(self, root, env, preload=()):
        self.notes = []
        try:
            self.job = Job(kill_on_close=True)
        except OSError as e:
            raise WorkerError('no job object for a pool worker (%s)' % e)

        def before_resume(p):
            try:
                unit_runner.shield_unit(p)
            except OSError as e:
                self.notes.append('kit_tests: could not shield pool worker pid %d from kills by '
                                  'pid (%s); it ran unshielded' % (p.pid, e))
        try:
            self.proc = self.job.start([sys.executable, WORKER] + list(preload),
                                       before_resume=before_resume,
                                       cwd=root, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except JobError as e:
            self.job.close()
            raise WorkerError('could not place a pool worker in its job (%s); it was killed' % e)
        except OSError as e:
            self.job.close()
            raise WorkerError('could not start a pool worker: %s' % e)
        self.replies = queue.Queue()
        self.noise = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.last = None                    # the unit it ran last
        try:
            msg = self._next(READY_TIMEOUT)
        except queue.Empty:
            self.end()
            raise WorkerError('a pool worker was not ready after %ds; ended it: %s' % (
                READY_TIMEOUT, self.how_it_ended()))
        except BaseException:
            self.end()
            raise
        if not msg or 'ready' not in msg:
            self.end()
            raise WorkerError('a pool worker did not start: %s' % self.how_it_ended())
        self.pid = self.proc.pid
        try:
            self.own = set(self.job.pids())     # the worker (and a launcher that started it)
        except OSError as e:
            self.end()
            raise WorkerError('could not list the processes in a pool worker\'s job (%s); '
                              'ended it' % e)

    def _read(self):
        try:
            for raw in self.proc.stdout:
                line = raw.decode('utf-8', 'replace')
                if line.startswith(MARK):
                    try:
                        self.replies.put(json.loads(line[len(MARK):]))
                        continue
                    except ValueError:
                        pass
                self.noise.append(line.rstrip('\r\n'))
        finally:
            self.replies.put(None)

    def _next(self, timeout):
        """The next reply; None at EOF; raises queue.Empty after `timeout` seconds. Waits in
        POLL slices, so Ctrl+C reaches a main thread that waits here."""
        end = time.monotonic() + timeout
        while True:
            try:
                return self.replies.get(timeout=max(0.0, min(POLL, end - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= end:
                    raise

    def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + '\n').encode('ascii'))
        self.proc.stdin.flush()

    def leftovers(self):
        """The pids in the worker's job it did not start with; None when the job cannot be
        asked (then nothing says the unit left nothing running)."""
        try:
            return set(self.job.pids()) - self.own
        except OSError:
            return None

    def how_it_ended(self):
        rc = self.proc.poll()
        text = 'still running' if rc is None else 'exit %d (0x%08X)' % (rc, rc & 0xFFFFFFFF)
        if self.noise:
            text += '; it wrote:\n' + '\n'.join(self.noise[-40:])
        return text

    def end(self, wait=0.0):
        """End the worker and everything in its job; `wait` > 0 first lets it exit on its own.
        Returns once each process the job held has ended, GONE_WAIT seconds at most."""
        held = []
        try:
            held = self._hold()
            if wait:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
                try:
                    self.proc.wait(wait)
                except subprocess.TimeoutExpired:
                    pass
            try:
                self.job.terminate()
            except OSError:
                pass
            self.proc.wait()
            _wait_all(held, GONE_WAIT)
            self.reader.join(5)
            for f in (self.proc.stdin, self.proc.stdout):
                try:
                    f.close()
                except OSError:
                    pass
        finally:
            for h in held:
                process.close(h)
            self.job.close()

    def _hold(self):
        """A SYNCHRONIZE handle to each process in the job now, taken before it is terminated:
        terminating a job empties its pid list at once, while each process's end is only started
        (one with 1.5 GB to tear down was signalled ~45 ms later, 2026-10-10). None when the job
        cannot be asked; a process that ended meanwhile is skipped."""
        try:
            pids = self.job.pids()
        except OSError:
            return []
        return [h for h in (process.open_process(pid, process.SYNCHRONIZE) for pid in pids) if h]


def _wait_all(handles, seconds):
    """Wait, `seconds` at most in all, until every process handle is signalled. Waits in POLL
    slices, so Ctrl+C reaches a main thread that waits here."""
    end = time.monotonic() + seconds
    for h in handles:
        while True:
            left = end - time.monotonic()
            if left <= 0 or process.K32.WaitForSingleObject(
                    h, int(min(POLL, left) * 1000)) != process.WAIT_TIMEOUT:
                break


class WorkerPool:
    def __init__(self, root, folder, env, preload=()):
        """Workers for checkout `root`, started with environment `env` (each unit gets its own);
        unit output files go in `folder`. Each worker imports the packages `preload` names once,
        before its first unit (pool_worker.py)."""
        self.root, self.folder, self.env = root, folder, dict(env)
        self.preload = list(preload)
        self.lock = threading.Lock()
        self.idle = []
        self.all = []
        self.count = 0
        self.started = 0
        self.ended = {}             # why -> workers ended before the pool closed
        self.replaced = []          # (unit, why) for each of them, in order

    def stats(self):
        """(workers started, {why: workers ended early})."""
        with self.lock:
            return self.started, dict(self.ended)

    def why_replaced(self):
        """[(unit, why)]: the unit each early-ended worker was running, and why it ended."""
        with self.lock:
            return list(self.replaced)

    def _retire(self, worker, why, unit):
        with self.lock:
            self.ended[why] = self.ended.get(why, 0) + 1
            self.replaced.append((unit, why))
            if worker in self.all:
                self.all.remove(worker)
        worker.end()

    def _take(self):
        while True:
            with self.lock:
                if not self.idle:
                    self.started += 1
                    break
                w = self.idle.pop()
            if w.proc.poll() is None:
                return w
            # It ended while idle: replaced, and no unit is blamed for it.
            self._retire(w, 'ended while idle', w.last)
        w = Worker(self.root, self.env, self.preload)
        with self.lock:
            self.all.append(w)
        return w

    def run_unit(self, unit, audit, timeout, root=None, cmd=None, env=None):
        """runner.run_unit's result for `unit` run in a pool worker (`root` and `cmd` are the
        pool's; `audit` is not supported and ignored, as selfrun never sets it)."""
        t = time.time()
        name = unit_runner.unit_name(unit)
        with self.lock:
            self.count += 1
            out = os.path.join(self.folder, 'unit-%d.out' % self.count)
        try:
            w = self._take()
        except WorkerError as e:
            return unit, 'FAILED', 0, time.time() - t, 'kit_tests: %s; not run\n' % e
        notes, w.notes, w.last = w.notes, [], name
        status, rc, retire, stuck = None, None, None, []
        try:
            w.send({'unit': name, 'env': dict(env if env is not None else self.env), 'out': out})
            try:
                msg = w._next(timeout)
            except queue.Empty:
                status = 'TIMEOUT'
                notes.append('pytests: TIMEOUT - %s still running after %ds; killed it and every '
                             'process it started' % (name, timeout))
                self._retire(w, 'timeout', name)
                w = None
            else:
                if msg is None or 'rc' not in msg:
                    lived = time.time() - t
                    self._retire(w, 'ended during a unit', name)
                    notes.append('kit_tests: the pool worker (pid %d) running %s ended %.2fs '
                                 'into it, before it reported: %s' % (
                                     w.pid, name, lived, w.how_it_ended()))
                    status, w = 'FAILED', None
                else:
                    rc, retire = int(msg['rc']), list(msg.get('retire') or [])
                    stuck = list(msg.get('stuck') or [])
        except BaseException:
            # Ctrl+C in this thread, or a broken pipe: never reuse the worker.
            if w is not None:
                self._retire(w, 'interrupted', name)
            raise
        if w is not None:
            deadline = time.time() + LEFTOVER_GRACE
            left = w.leftovers()
            while left and time.time() < deadline:
                time.sleep(0.05)
                left = w.leftovers()
            if left is None:
                retire.append('its job could not be queried')
            elif left:
                retire.append('processes it started still running')
            if retire:
                for why in retire:
                    with self.lock:
                        self.ended[why] = self.ended.get(why, 0) + 1
                        self.replaced.append((name, why))
                with self.lock:
                    if w in self.all:
                        self.all.remove(w)
                w.end()
            else:
                with self.lock:
                    self.idle.append(w)
        text = _read(out)
        ran = sum(int(x) for x in unit_runner.RAN.findall(text))
        if status is None:
            status = 'OK' if rc == 0 else 'FAILED'
            if status == 'FAILED' and not unit_runner.RAN.search(text):
                notes.append('kit_tests: %s ran in a pool worker and ended with exit %d (0x%08X) '
                             'after %.2fs, with no unittest summary' % (
                                 name, rc, rc & 0xFFFFFFFF, time.time() - t))
            elif status == 'OK' and not ran:
                status = 'FAILED'
                notes.append('pytests: %s ran no test; an empty unit is a FAIL' % name)
            if stuck:
                status = 'FAILED'
                notes.append('kit_tests: %s left a non-daemon thread running (%s); as its own '
                             'process it would never have exited, so it FAILS' % (
                                 name, ', '.join(stuck)))
        if notes:
            text += '\n' + '\n'.join(notes) + '\n'
        return unit, status, ran, time.time() - t, text

    def close(self):
        """End every worker: each may exit on its own for CLOSE_WAIT seconds, then is killed."""
        with self.lock:
            workers, self.all, self.idle = list(self.all), [], []
        for w in workers:
            try:
                w.proc.stdin.close()
            except OSError:
                pass
        deadline = time.time() + CLOSE_WAIT
        for w in workers:
            w.end(wait=max(0.01, deadline - time.time()))


def _read(path):
    try:
        with open(path, 'rb') as fh:
            raw = fh.read()
    except OSError:
        return ''
    try:
        os.remove(path)
    except OSError:
        pass
    return raw.decode('utf-8', 'replace').replace('\r\n', '\n')
