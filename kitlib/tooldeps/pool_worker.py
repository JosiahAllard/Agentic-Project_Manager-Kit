"""The interpreter kitlib.tooldeps.pool keeps: it runs test units one after another, each in this
process as `python -m unittest <unit>` would run it in a new one, and puts the interpreter back
as it was before the next. Started by path (`python <this file> [package ...]`) with cwd the
checkout root. Standard library only, and it imports nothing of the kit but the packages named
on its command line, so every other kit and test module a unit loads is unloaded after it.

Preloaded packages (the packages the units are in: kitlib.tooldeps.selfrun names them) are
imported once, before the worker is ready, as unittest imports a unit's package before the unit:
what they set up is every unit's starting state, so an audit hook one adds (tests/__init__.py's
guard) does not retire each worker. Their globals are put back after each unit like any loaded
module's, but their import code does not run again: what it does to os.environ is not in a
unit's environment, which the pool sets. One that fails to import is left out, and each unit
that needs it fails as its own process would.

Protocol. A command is one JSON line on stdin: {"unit": dotted name, "env": {environment},
"out": file}. A reply is one line on the stdout pipe the worker started with: MARK, then JSON -
first {"ready": pid}, then one per command: {"rc": exit status, "retire": [reasons], "stuck":
[names of the non-daemon threads the unit left running]}. A non-empty `retire` means this
interpreter must not run another unit; the pool ends it. A non-empty `stuck` means the unit's
own process would never have exited; the pool FAILS the unit. Nothing
else is written to that pipe once the worker is ready: the reply pipe is a private, non-inherited
copy, and fds 1 and 2 point at the unit's output file while it runs, else at the null device.

A unit. Its stdout and stderr (fds 1 and 2, so also the standard handles every process it starts
inherits) go to its `out` file; stdin is the null device, as today's units have it. cwd is the
checkout root, os.environ is exactly `env` (TEMP and TMP its own folder; tempfile's cached
temp dir is dropped), sys.argv is ['python -m unittest', unit] and sys.path[0] the root, as `-m`
sets them. unittest.main runs it; the exit status is what the process would have exited with.

After a unit, in the order an interpreter ends: the threading atexit hooks it added run, the
threads it started get THREAD_GRACE seconds to end, then the atexit callbacks it registered run
(atexit.register is wrapped while it runs; weakref.finalize's own hook is the one kept). Then the
reset, back to the state captured when the worker was ready:
  - sys.modules: every module the unit imported is removed, every one it replaced put back;
  - every module that was loaded before: attributes it added are removed and attributes it
    rebound restored (sys.stdout and friends, sys.argv, sys.path, the hooks, tempfile.tempdir,
    a leaked mock.patch on a stdlib function, a package attribute naming an unloaded submodule);
  - sys.path, sys.meta_path, sys.path_hooks hold their old entries again; the importer caches
    are cleared;
  - os.environ and cwd (set again for the next unit), the warnings filters (the unit ran
    inside warnings.catch_warnings), signal handlers, the recursion limit, the switch interval,
    the trace and profile functions, the int string-digit limit, gc's thresholds and state;
  - random is reseeded and linecache cleared; gc collects what the unit left.
Retired instead of reused: a unit that ended with KeyboardInterrupt (Ctrl+C) or another
BaseException out of unittest, one whose threads outlive the grace (a non-daemon one is also
reported `stuck`), one that added an audit hook (sys.addaudithook; hooks cannot be removed), and a
reset that raised.

Compiled code is kept (CodeCache). The kit's runs write no bytecode files
(PYTHONDONTWRITEBYTECODE), so every unit would compile again each kit and test module it imports
from source. Instead the worker keeps each module's code object, keyed by its path and a hash of
the source it was compiled from: a later unit that imports the same source reuses it, and a
changed source (whatever its time stamp and size) is compiled again. A code object is immutable;
importing runs it in a new module, as a compile would. Only the compile is skipped, and with it
the compiler's own warnings for that source after its first unit.
"""
import os
import sys

ROOT = os.getcwd()
sys.path[0] = ROOT          # as `python -m unittest` has it: the cwd, not this file's folder

import atexit               # noqa: E402
import gc                   # noqa: E402
import hashlib              # noqa: E402
import importlib            # noqa: E402
from importlib import _bootstrap_external  # noqa: E402
import json                 # noqa: E402
import linecache            # noqa: E402
import random               # noqa: E402
import signal               # noqa: E402
import tempfile             # noqa: E402
import threading            # noqa: E402
import time                 # noqa: E402
import traceback            # noqa: E402
import unittest             # noqa: E402
import warnings             # noqa: E402
import weakref              # noqa: E402

MARK = 'kit-pool: '
ARGV0 = 'python -m unittest'        # what unittest's __main__ sets sys.argv[0] to
CTRL_C_EXIT = 0xC000013A            # STATUS_CONTROL_C_EXIT: how Ctrl+C ends a Python process
THREAD_GRACE = 1.0
BINARY = getattr(os, 'O_BINARY', 0)
SIGNALS = [getattr(signal, n) for n in ('SIGINT', 'SIGTERM', 'SIGBREAK', 'SIGABRT')
           if hasattr(signal, n)]
_MISSING = object()


def exit_status(code):
    """The exit status of a process ending with SystemExit(code), and the text it prints."""
    if code is None:
        return 0, None
    if isinstance(code, int):
        return int(code), None
    return 1, str(code)


class Exits:
    """atexit.register while a unit runs: its callbacks are kept here and run when it ends."""

    def __init__(self):
        self.real_register, self.real_unregister = atexit.register, atexit.unregister
        self.calls = []

    def register(self, func, *args, **kwargs):
        if func == weakref.finalize._exitfunc:
            # Kept: running it sets finalize._shutdown and stops every later finalizer.
            return self.real_register(func, *args, **kwargs)
        self.calls.append((func, args, kwargs))
        return func

    def unregister(self, func):
        self.calls[:] = [c for c in self.calls if c[0] != func]
        self.real_unregister(func)

    def run(self):
        while self.calls:
            func, args, kwargs = self.calls.pop()
            try:
                func(*args, **kwargs)
            except BaseException:       # noqa: BLE001 - as the interpreter reports them
                sys.stderr.write('Exception ignored in atexit callback %r:\n' % (func,))
                traceback.print_exc()


class CodeCache:
    """The code objects the source loader compiled in this worker, reused by later units:
    SourceLoader.source_to_code (what imports a module that has no bytecode file) is wrapped to
    look the source up first. The key is (path, BLAKE2b of the source bytes, the compile
    options): the same file with other contents is another key. Kept for at most LIMIT bytes of
    source, the oldest dropped first. `compiled` and `reused` count each path's compiles and
    reuses (a unit reads them from __main__.CODE)."""

    LIMIT = 64 * 1024 * 1024

    def __init__(self):
        self.codes = {}         # key -> (code object, source bytes)
        self.size = 0
        self.compiled = {}
        self.reused = {}

    def install(self, loader=_bootstrap_external.SourceLoader):
        real = loader.source_to_code
        cache = self

        def source_to_code(self, data, path, **kw):
            if not isinstance(data, bytes) or not isinstance(path, str):
                return real(self, data, path, **kw)
            return cache.code(lambda: real(self, data, path, **kw), data, path, kw)
        source_to_code.__doc__ = real.__doc__
        loader.source_to_code = source_to_code

    def code(self, compile_it, data, path, kw):
        key = (path, hashlib.blake2b(data, digest_size=20).digest(), tuple(sorted(kw.items())))
        hit = self.codes.get(key)
        if hit is not None:
            self.reused[path] = self.reused.get(path, 0) + 1
            return hit[0]
        code = compile_it()
        self.compiled[path] = self.compiled.get(path, 0) + 1
        self.codes[key] = (code, len(data))
        self.size += len(data)
        while self.size > self.LIMIT and len(self.codes) > 1:
            self.size -= self.codes.pop(next(iter(self.codes)))[1]
        return code


CODE = CodeCache()


class Worker:
    def __init__(self):
        CODE.install()
        self.exits = Exits()
        atexit.register, atexit.unregister = self.exits.register, self.exits.unregister
        self.audit_hooks = 0
        self.real_addaudithook = sys.addaudithook
        sys.addaudithook = self.addaudithook
        self.null = os.open(os.devnull, os.O_RDWR | BINARY)
        self.commands = os.fdopen(os.dup(0), 'rb')          # os.dup: not inherited
        self.replies = os.dup(1)
        os.dup2(self.null, 0)
        for name in sys.argv[1:]:
            try:
                importlib.import_module(name)
            except Exception:               # noqa: BLE001 - each unit then fails importing it
                pass
        self.capture()

    def addaudithook(self, hook):
        self.audit_hooks += 1
        return self.real_addaudithook(hook)

    def capture(self):
        """What the reset puts back: taken once, when the worker is ready."""
        self.modules = dict(sys.modules)
        self.dicts = {name: dict(vars(m)) for name, m in self.modules.items()
                      if m is not None and name != '__main__' and hasattr(m, '__dict__')}
        self.lists = [(sys.path, list(sys.path)), (sys.meta_path, list(sys.meta_path)),
                      (sys.path_hooks, list(sys.path_hooks))]
        self.handlers = {s: signal.getsignal(s) for s in SIGNALS}
        self.recursion = sys.getrecursionlimit()
        self.switch = sys.getswitchinterval()
        self.digits = sys.get_int_max_str_digits()
        self.gc = (gc.isenabled(), gc.get_threshold())
        self.threads = set(threading.enumerate())
        self.thread_exits = len(getattr(threading, '_threading_atexits', ()))

    def reply(self, **msg):
        os.write(self.replies, (MARK + json.dumps(msg) + '\n').encode('ascii'))

    def serve(self):
        os.chdir(ROOT)
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(self.null, 1)
        os.dup2(self.null, 2)
        self.reply(ready=os.getpid())
        for line in self.commands:
            cmd = json.loads(line)
            rc, retire, stuck = self.unit(cmd['unit'], cmd['env'], cmd['out'])
            self.reply(rc=rc, retire=retire, stuck=stuck)
            if retire:
                return

    def unit(self, unit, env, out):
        retire, stuck = [], []
        os.chdir(ROOT)
        os.environ.clear()
        os.environ.update(env)
        tempfile.tempdir = None
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | BINARY)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
        hooks = self.audit_hooks
        argv = [ARGV0, unit]
        sys.argv = list(argv)
        rc = 0
        try:
            try:
                with warnings.catch_warnings():
                    unittest.main(module=None, argv=argv)
            except SystemExit as e:
                rc, text = exit_status(e.code)
                if text is not None:
                    sys.stderr.write(text + '\n')
            except KeyboardInterrupt:
                traceback.print_exc()
                rc = CTRL_C_EXIT
                retire.append('Ctrl+C')
            except BaseException as e:      # noqa: BLE001 - what an uncaught one does
                traceback.print_exc()
                rc = 1
                retire.append('%s out of unittest' % type(e).__name__)
            ended, stuck = self.end_unit()
            retire += ended
            if self.audit_hooks != hooks:
                retire.append('an audit hook was added')
        except BaseException as e:          # noqa: BLE001 - never reuse a half-reset worker
            try:
                traceback.print_exc()
            except BaseException:           # noqa: BLE001
                pass
            retire.append('the reset failed: %s' % type(e).__name__)
        finally:
            for f in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
                try:
                    f.flush()
                except BaseException:       # noqa: BLE001 - a closed or broken stream
                    pass
            os.dup2(self.null, 1)
            os.dup2(self.null, 2)
        return rc, retire, stuck

    def end_unit(self):
        """The interpreter's own shutdown for the unit's part, then the reset: (retire reasons,
        the names of the non-daemon threads it left running)."""
        retire = []
        added = getattr(threading, '_threading_atexits', None)
        while added is not None and len(added) > self.thread_exits:
            func = added.pop()
            try:
                func()
            except BaseException:           # noqa: BLE001
                traceback.print_exc()
        deadline = time.monotonic() + THREAD_GRACE
        for t in [t for t in threading.enumerate() if t not in self.threads]:
            t.join(max(0.0, deadline - time.monotonic()))
        self.exits.run()
        left = [t for t in threading.enumerate() if t not in self.threads and t.is_alive()]
        if left:
            retire.append('threads still running: %s' % (
                ', '.join(sorted(t.name for t in left)) or 'unnamed'))
        # A non-daemon thread would have kept the unit's own process from exiting.
        stuck = sorted(t.name for t in left if not t.daemon)
        self.reset()
        return retire, stuck

    def reset(self):
        for name in [n for n in sys.modules if n not in self.modules]:
            del sys.modules[name]
        for name, m in self.modules.items():
            if sys.modules.get(name) is not m:
                sys.modules[name] = m
        for name, saved in self.dicts.items():
            live = vars(self.modules[name])
            for k in [k for k in live if k not in saved]:
                del live[k]
            for k, v in saved.items():
                if live.get(k, _MISSING) is not v:
                    live[k] = v
        for live, saved in self.lists:
            live[:] = saved
        sys.path_importer_cache.clear()
        importlib.invalidate_caches()
        for s, h in self.handlers.items():
            if signal.getsignal(s) is not h:
                signal.signal(s, h)
        sys.setrecursionlimit(self.recursion)
        sys.setswitchinterval(self.switch)
        sys.set_int_max_str_digits(self.digits)
        sys.settrace(None)
        sys.setprofile(None)
        threading.settrace(None)
        threading.setprofile(None)
        gc.set_threshold(*self.gc[1])
        (gc.enable if self.gc[0] else gc.disable)()
        tempfile.tempdir = None
        linecache.clearcache()
        random.seed()
        os.chdir(ROOT)
        gc.collect()


def main():
    try:
        Worker().serve()
    except KeyboardInterrupt:
        return 130                  # Ctrl+C while idle: end quietly
    return 0


if __name__ == '__main__':
    sys.exit(main())
