"""Starting a program with no console window: subprocess's run, call and Popen with one change, and
what the carrier tools/no_window.py does.

`run`, `call` and `popen` take what `subprocess.run`, `subprocess.call` and `subprocess.Popen`
take and give back what they give. The one change is the child's creationflags, through
kitlib.proc.job.no_window_flags: when this process has no console (pythonw.exe, a scheduled task,
a hook run by a windowed program) and the caller made no console choice of its own, the child is
started with CREATE_NO_WINDOW, so it gets a console with no window instead of a new, visible one.
From a process that has a console the call is the very one subprocess got before: no keyword is
added, and the child shares that console. DETACHED_PROCESS is never added (a detached console
program has no console at all, and one that asks for one opens a window).

They call subprocess through the module, at call time, so a test that fakes `subprocess.run` on
any kit module (they all share the one module) still sees the start. A kit module that starts a
program uses these, or names `creationflags=` itself; tests/proc/test_spawn_sites.py holds every
start in the kit to that.

`carry` is the carrier: a scheduled task runs `pythonw tools/no_window.py PROGRAM [ARGS...]`, and
since pythonw has no console, PROGRAM starts with a hidden one, which everything it starts in turn
inherits. PROGRAM is not put in a job (kitlib.proc.job allows no breakaway, so a job here would
hold every window `launch.py after-reboot` opens until the carrier ends). `exit_code` is how the
carrier ends with PROGRAM's own code, a crash's included. PROGRAM's TEMP and TMP are the agents'
temp folder AGENT_TEMP when there is one (`agent_temp_env`): a scheduled task and a detached start
(kitlib.lock.detach, kitlib.gate.submit) inherit the user environment's TEMP, on the system drive,
while everything agents produce goes in AGENT_TEMP (docs/daily-full.md "The schedule"; a launched
session's window does the same, kitlib.launcher.window). Nothing here prints.
"""
import os
import subprocess
import sys

from . import job

CARRIER = 'no_window.py'    # the carrier's file name in the kit's tools folder
EXIT_USAGE = 2          # carry: no program named
EXIT_NO_START = 127     # carry: the program could not be started
PYTHON_WORDS = ('python', 'python.exe')


def _flagged(kw):
    """`kw` with the no-window creationflags, as a new dict; `kw` itself when nothing changes."""
    flags = job.no_window_flags(kw.get('creationflags', 0))
    if not flags or flags == kw.get('creationflags'):
        return kw
    return dict(kw, creationflags=flags)


def run(*args, **kw):
    """subprocess.run, with no console window for the child of a parent that has no console."""
    return subprocess.run(*args, **_flagged(kw))


def call(*args, **kw):
    """subprocess.call, with no console window for the child of a parent that has no console."""
    return subprocess.call(*args, **_flagged(kw))


def popen(*args, **kw):
    """subprocess.Popen, with no console window for the child of a parent that has no console."""
    return subprocess.Popen(*args, **_flagged(kw))


def console_python(exe=None):
    """The python.exe beside `exe` (default: this interpreter, which under the carrier is
    pythonw.exe), or None when there is none."""
    path = os.path.join(os.path.dirname(exe or sys.executable), 'python.exe')
    return path if os.path.isfile(path) else None


def pythonw_beside(python):
    """The pythonw.exe in the folder of `python` (either of the two pythons). Not checked to exist
    here, and no fallback: a caller that must refuse a missing one checks the path itself."""
    return os.path.join(os.path.dirname(python), 'pythonw.exe')


def exit_code(code):
    """A child's exit code as subprocess gives it on Windows (0 to 0xFFFFFFFF) as the int that,
    handed to sys.exit, ends this process with the same 32 bits: unchanged up to 0x7FFFFFFF, and
    above it (a crash such as 0xC0000005) the negative int with those bits. That int fits a C long
    on Windows, so no interpreter has to convert one that does not; Python 3.14 ends with the same
    bits for either form (tests/proc/test_no_window.py CarrierTest measures the negative one)."""
    code &= 0xFFFFFFFF
    return code - 0x100000000 if code > 0x7FFFFFFF else code


AGENT_TEMP = 'AGENT_TEMP'
TEMP_NAMES = ('TEMP', 'TMP')


def agent_temp_env(environ=None, makedirs=os.makedirs):
    """A new environment: `environ` (default os.environ) with TEMP and TMP set to its AGENT_TEMP,
    that folder made when missing. None, meaning "keep the environment as it is", when AGENT_TEMP
    is unset, empty or not an absolute path with a drive or share, or the folder cannot be made or
    is not a folder. `environ` itself is never changed, and nothing here raises."""
    try:
        source = os.environ if environ is None else environ
        folder = source.get(AGENT_TEMP)
        if not isinstance(folder, str) or not folder.strip():
            return None
        folder = folder.strip()
        if not os.path.isabs(folder) or not os.path.splitdrive(folder)[0]:
            return None
        makedirs(folder, exist_ok=True)
        if not os.path.isdir(folder):
            return None
        env = dict(source)
        for name in TEMP_NAMES:
            env[name] = folder
        return env
    except Exception:       # noqa: BLE001 - the carried program then gets the environment as is
        return None


def carry(argv):
    """Start argv[0] with argv[1:], wait for it, and give its exit code as exit_code has it, for
    sys.exit. A first word `python` or `python.exe` (any case) is the console python beside this
    interpreter, so a task line needs no second path; any other program is started as named. The
    program's TEMP and TMP are AGENT_TEMP when agent_temp_env gives an environment; else it gets
    this one unchanged. EXIT_USAGE with nothing started for an empty argv, EXIT_NO_START when the
    program cannot be started."""
    argv = list(argv)
    if not argv:
        return EXIT_USAGE
    if argv[0].lower() in PYTHON_WORDS:
        argv[0] = console_python() or argv[0]
    env = agent_temp_env()
    try:
        return exit_code(call(argv) if env is None else call(argv, env=env))
    except OSError:
        return EXIT_NO_START
