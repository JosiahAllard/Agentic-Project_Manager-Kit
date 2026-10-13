"""No console window from any start in a kit process: a process-wide hook on CreateProcess.

Why: Windows opens a console window for a console program only when its parent has NO console and
the start makes no console choice of its own. A child of a process that has a console, shown or
hidden, shares it and opens none. kitlib.proc.spawn and kitlib.proc.job cover the kit's own starts
one call at a time (no_window_flags); a bare subprocess call, multiprocessing and the tests do not
go through them. With Windows Terminal as the default terminal SW_HIDE does not help: the window
shows anyway.

The hook: install() wraps _winapi.CreateProcess, which subprocess.Popen and multiprocessing's
spawn both call at call time. On the first start whose creationflags make no console choice
(CREATE_NEW_CONSOLE, DETACHED_PROCESS or CREATE_NO_WINDOW), the wrapper calls
ensure_windowless_console() once: a process with no console (pythonw, a scheduled task, a hook run
by a windowed program) gets a console with no window, and every child after that shares it, so
none opens a window. A process that has a console is left as it is. After that first start the
wrapper costs one module-global check per start. When no console can be had, every start that
makes no console choice gets CREATE_NO_WINDOW instead (what no_window_flags does).

kitlib/__init__.py calls install() on `import kitlib`, so every kit process has it. Nothing here
loads ctypes until the first such start.

ensure_windowless_console(): AllocConsoleWithOptions with ALLOC_CONSOLE_MODE_NO_WINDOW where
kernel32 exports it (measured 2026-10-13 on Windows 11 10.0.26300 under pythonw: hr 0, a new
console, no Windows Terminal or OpenConsole process, the std handles unchanged, a bare child's
GetConsoleWindow() 0). Else the fallback: a holder program started with CREATE_NO_WINDOW, so it
has a console with no window; AttachConsole(holder), then the holder ends and the console stays
with this process (measured the same day: attached after about 16 ms of ERROR_INVALID_HANDLE while
the holder started, std handles unchanged, a bare child's GetConsoleWindow() 0). FORCE_FALLBACK
makes ensure take the fallback where the export exists (the tests).

Incident log: each time ensure gives a process a console it appends one JSON line (time, how, pid,
exe, this process's argv, the child's command line, the parent's pid and exe) to LOG_NAME in
the kit temp folder (kitlib.config.user.work_temp of the kit checkout; docs/test-tools.md "Temp
folders"). LOG_PATH overrides that (the tests). Over LOG_MAX bytes the log is moved to
`<log>.1` first. The log can fail in any way (a fenced test process, a bad machine config, a full
disk): the start goes on. A command line can carry a token or a prompt, so both go in redacted
(redact_command): the program and the first KEEP_WORDS words after it, each cut to WORD_MAX
characters, with secrets masked (_mask); it never raises.

Ctrl+C: a console Ctrl+C has no window to be typed into here; only GenerateConsoleCtrlEvent from a
process on the same console sends one (no kit code calls it). A pythonw process that gains a
console would be ended by one at once (exit 0xC000013A, measured 2026-10-13: Python's SIGINT
handler, SIG_IGN included, never runs under pythonw), where before it had no console and got none.
So ensure gives it a console control handler that takes Ctrl+C and Ctrl+Break for this process
(_take_ctrl). A handler is not inherited: a child on the console gets them as before
(docs/daily-full.md "The schedule").

Windows only: off Windows install() does nothing.
"""
import _thread
import os
import sys

if os.name == 'nt':
    import _winapi
else:                                   # pragma: no cover - Windows only
    _winapi = None

CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CONSOLE_CHOICE = CREATE_NEW_CONSOLE | DETACHED_PROCESS | CREATE_NO_WINDOW

ALLOC_CONSOLE_MODE_NO_WINDOW = 2        # ALLOC_CONSOLE_MODE
ALLOC_CONSOLE_RESULT_NEW = 1            # ALLOC_CONSOLE_RESULT: 0 none, 1 new, 2 existing
ALLOC_CONSOLE_RESULT_EXISTING = 2
ERROR_INVALID_HANDLE = 6                # AttachConsole: the holder has no console yet
CTRL_C_EVENT, CTRL_BREAK_EVENT = 0, 1   # the console control events _take_ctrl takes
HOLDER_WAIT_S = 5.0                     # how long the fallback waits for the holder's console

MARK = '__kit_windowless__'             # set on the wrapper: install() is done in this process
LOG_NAME = 'kit-console-incidents.log'
LOG_MAX = 1 << 20
KEEP_WORDS = 6                          # a logged command line: the program and this many words
WORD_MAX = 120                          # each word cut to its start and end past this length
MASK = '***'
# a flag or NAME=VALUE name holding one of these hides its value
SECRET_WORDS = ('token', 'secret', 'pass', 'pw', 'key', 'auth', 'credential', 'cookie',
                'bearer')

# ensure_windowless_console's answers
PRESENT = 'present'                     # the process had a console already
ALLOCATED = 'alloc'                     # AllocConsoleWithOptions gave it one with no window
ATTACHED = 'attach'                     # the fallback: attached to a holder's windowless console
NONE = 'none'                           # no console could be had

FORCE_FALLBACK = False                  # tests: ensure takes the holder path regardless
LOG_PATH = None                         # tests: the incident log file; None: the kit temp folder

_UNKNOWN, _DONE, _NO_WINDOW = 0, 1, 2
_mode = _UNKNOWN                        # the wrapper's state; _DONE after the first resolve
_lock = _thread.RLock()
_original = None                        # the CreateProcess the wrapper delegates to
_kernel32 = None
_ctrl_handler = None                    # _take_ctrl's handler, kept so it is never freed


def _k32():
    """A private kernel32 binding, made on first use (ctypes loads here, never on import)."""
    global _kernel32
    if _kernel32 is None:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.GetConsoleProcessList.argtypes = (ctypes.POINTER(wintypes.DWORD), wintypes.DWORD)
        k.GetConsoleProcessList.restype = wintypes.DWORD
        k.AttachConsole.argtypes = (wintypes.DWORD,)
        k.AttachConsole.restype = wintypes.BOOL
        k.GetConsoleWindow.argtypes = ()
        k.GetConsoleWindow.restype = ctypes.c_void_p
        _kernel32 = k
    return _kernel32


def has_console():
    """True when this process is attached to a console, with a window or without one.
    GetConsoleProcessList, not GetConsoleWindow: a console made with CREATE_NO_WINDOW, or by
    ensure_windowless_console, has a GetConsoleWindow() of 0 (kitlib/proc/job.py, measured
    2026-10-10)."""
    from ctypes import wintypes
    one = (wintypes.DWORD * 1)()
    return _k32().GetConsoleProcessList(one, 1) != 0


def _alloc_no_window():
    """AllocConsoleWithOptions(ALLOC_CONSOLE_MODE_NO_WINDOW): ALLOCATED (a new console),
    PRESENT (one was there), NONE (refused), or None when kernel32 does not export it."""
    import ctypes
    from ctypes import wintypes
    k = _k32()
    try:
        fn = k.AllocConsoleWithOptions
    except AttributeError:
        return None

    class OPTIONS(ctypes.Structure):
        _fields_ = [('mode', ctypes.c_int), ('useShowWindow', wintypes.BOOL),
                    ('showWindow', wintypes.WORD)]
    fn.argtypes = (ctypes.POINTER(OPTIONS), ctypes.POINTER(ctypes.c_int))
    fn.restype = ctypes.c_long
    result = ctypes.c_int(-1)
    hr = fn(ctypes.byref(OPTIONS(ALLOC_CONSOLE_MODE_NO_WINDOW, 0, 0)), ctypes.byref(result))
    if hr < 0:
        return NONE
    if result.value == ALLOC_CONSOLE_RESULT_NEW:
        return ALLOCATED
    if result.value == ALLOC_CONSOLE_RESULT_EXISTING:
        return PRESENT
    return NONE


def _holder_argv():
    """A console program that waits for its stdin to close: cmd.exe (ComSpec), else the console
    python beside this interpreter; None when there is neither."""
    comspec = os.environ.get('ComSpec')
    if comspec and os.path.isfile(comspec):
        return [comspec, '/d', '/q', '/c', 'set /p _=']
    py = os.path.join(os.path.dirname(sys.executable), 'python.exe')
    if os.path.isfile(py):
        return [py, '-I', '-S', '-c', 'import sys; sys.stdin.read()']
    return None


def _attach_holder(wait_s=HOLDER_WAIT_S):
    """The fallback: ATTACHED when this process is now on a holder's windowless console, else
    NONE. The holder is this process's own child; it ends when its stdin closes."""
    import ctypes
    import subprocess
    import time
    argv = _holder_argv()
    if argv is None:
        return NONE
    holder = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    k = _k32()
    attached = False
    try:
        end = time.monotonic() + wait_s
        while True:
            if k.AttachConsole(holder.pid):
                attached = True
                break
            if (ctypes.get_last_error() != ERROR_INVALID_HANDLE or holder.poll() is not None
                    or time.monotonic() > end):
                break
            time.sleep(0.005)
    finally:
        try:
            holder.stdin.close()
            holder.wait(wait_s)
        except Exception:               # noqa: BLE001 - our own child: end it by its handle
            holder.kill()
            holder.wait()
    return ATTACHED if attached else NONE


def ensure_windowless_console(fallback=None, child=None):
    """Give this process a console with no window unless it has one. PRESENT (it had one: nothing
    done), ALLOCATED (AllocConsoleWithOptions), ATTACHED (the holder fallback) or NONE (no console
    could be had). `fallback` True takes the holder path even where AllocConsoleWithOptions is
    exported (None: FORCE_FALLBACK). `child` (the command line about to start) goes in the
    incident log. Idempotent: a second call answers PRESENT. Windows only."""
    with _lock:
        if has_console():
            return PRESENT
        how = None
        if not (FORCE_FALLBACK if fallback is None else fallback):
            how = _alloc_no_window()
        if how is None or how == NONE:
            how = _attach_holder()
        if how in (ALLOCATED, ATTACHED):
            try:
                _take_ctrl()
            except Exception:           # noqa: BLE001 - the console stays; the start goes on
                pass
            _log_incident(how, child)
        return how


def ctrl_taken(event):
    """_take_ctrl's handler: True (handled) for Ctrl+C and Ctrl+Break, False for any other event
    (it goes on to the next handler)."""
    return event in (CTRL_C_EVENT, CTRL_BREAK_EVENT)


def _take_ctrl():
    """Register a console control handler (ctrl_taken) that answers Ctrl+C and Ctrl+Break as
    handled, so this process, which had no console, is not ended by one sent to the console it now
    has. Per process: a child does not inherit it. True when registered."""
    global _ctrl_handler
    import ctypes
    from ctypes import wintypes
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    if _ctrl_handler is None:
        _ctrl_handler = proto(ctrl_taken)
    k = _k32()
    k.SetConsoleCtrlHandler.argtypes = (proto, wintypes.BOOL)
    k.SetConsoleCtrlHandler.restype = wintypes.BOOL
    return bool(k.SetConsoleCtrlHandler(_ctrl_handler, True))


def _create_process(app, cmd, proc_attrs, thread_attrs, inherit, flags, env, cwd, startup):
    """_winapi.CreateProcess, with this process given a windowless console before its first
    child that makes no console choice (install)."""
    if _mode != _DONE and not flags & CONSOLE_CHOICE:
        flags = flags | CREATE_NO_WINDOW if _mode == _NO_WINDOW else _resolve(flags, app, cmd)
    return _original(app, cmd, proc_attrs, thread_attrs, inherit, flags, env, cwd, startup)


setattr(_create_process, MARK, True)


def _resolve(flags, app, cmd):
    """The first start with no console choice: ensure once, then `flags` (this process has a
    console now, which the child shares) or `flags` | CREATE_NO_WINDOW (it has none)."""
    global _mode
    with _lock:
        if _mode == _UNKNOWN:
            try:
                how = ensure_windowless_console(child=cmd if cmd is not None else app)
            except Exception:           # noqa: BLE001 - never stop the start
                how = NONE
            _mode = _NO_WINDOW if how == NONE else _DONE
    return flags | CREATE_NO_WINDOW if _mode == _NO_WINDOW else flags


def installed():
    """True when _winapi.CreateProcess is a kit wrapper (this module's, or another kit copy's)."""
    return _winapi is not None and bool(getattr(_winapi.CreateProcess, MARK, False))


def install():
    """Wrap _winapi.CreateProcess once per process (idempotent: a wrapper already there, from
    this module or another kit copy's, is kept). Off Windows nothing."""
    global _original
    if _winapi is None:
        return False
    with _lock:
        if installed():
            return False
        _original = _winapi.CreateProcess
        _winapi.CreateProcess = _create_process
    return True


def log_path():
    """The incident log file: LOG_PATH, else LOG_NAME in the kit temp folder of this kit
    checkout (kitlib.config.user.work_temp). Raises when that cannot be worked out (a fenced test
    process: HomeFenced; a bad machine config: ConfigError)."""
    if LOG_PATH:
        return LOG_PATH
    from ..config import user
    kit = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(user.work_temp(kit), LOG_NAME)


def _parent():
    """(parent pid, parent exe name) from one Toolhelp snapshot, or (None, None)."""
    from . import process
    rows = process.snapshot()
    me = os.getpid()
    ppid = next((pp for pid, pp, _n in rows if pid == me), None)
    exe = next((n for pid, _pp, n in rows if pid == ppid), None)
    return ppid, exe


def split_command_line(line):
    """A Windows command line as its words, by the C runtime's rules (the ones
    subprocess.list2cmdline writes for): blanks part words outside quotes, 2n backslashes before
    a quote are n and the quote counts, 2n+1 are n and a literal quote, "" inside quotes is a
    literal quote; other backslashes are themselves."""
    words, cur, quoted, have, i, n = [], [], False, False, 0, len(line)
    while i < n:
        c = line[i]
        if c == '\\':
            j = i
            while j < n and line[j] == '\\':
                j += 1
            if j < n and line[j] == '"':
                cur.append('\\' * ((j - i) // 2))
                if (j - i) % 2:
                    cur.append('"')
                    j += 1
            else:
                cur.append('\\' * (j - i))
            i, have = j, True
        elif c == '"':
            if quoted and line[i + 1:i + 2] == '"':
                cur.append('"')
                i += 1
            else:
                quoted = not quoted
            i, have = i + 1, True
        elif c in ' \t' and not quoted:
            if have:
                words.append(''.join(cur))
                cur, have = [], False
            i += 1
        else:
            cur.append(c)
            i, have = i + 1, True
    if have:
        words.append(''.join(cur))
    return words


def _secret(name):
    name = name.lower()
    return any(s in name for s in SECRET_WORDS)


USER_FLAGS = ('-u', '--user')            # curl's: a `user:password` value hides the password


def _user_value(v):
    """`user:password` as `user:***`; anything else (a drive path `C:\\x`, a URL: pip's
    `--user` takes no value) as it is."""
    import re
    m = re.fullmatch(r'(?![A-Za-z]:[\\/])(?!.*://)([^:\\/]+):.+', v, re.S)
    return m.group(1) + ':' + MASK if m else v


def _pairs(w):
    """`w` with the value of each `name=value` (a URL query, a form body) and `"name": value`
    (JSON) whose name holds a secret word masked, wherever in the word they stand."""
    import re
    pair = re.compile(r'(?<![\w.-])([\w.-]+)=|"([^"\\]*)"\s*:\s*')
    plain = re.compile(r'[^&;\s"\']*')
    json_value = re.compile(r'"(?:[^"\\]|\\.)*"|[^,}\]\s]*', re.S)
    out, pos = [], 0
    while True:
        m = pair.search(w, pos)
        if not m:
            break
        name, is_json = (m.group(1), False) if m.group(1) is not None else (m.group(2), True)
        out.append(w[pos:m.end()])
        pos = m.end()
        if _secret(name):
            v = (json_value if is_json else plain).match(w, pos)
            out.append('"%s"' % MASK if is_json and v.group().startswith('"') else MASK)
            pos = v.end()
    out.append(w[pos:])
    return ''.join(out)


def _mask(words):
    """`words` with secrets masked: the value after a secret flag (`--token VALUE`, `/key VALUE`)
    or after `Bearer`/`Basic`; the password of `-u`/`--user user:password`; the value of
    `--token=VALUE`, `/password:VALUE` and `GITHUB_TOKEN=VALUE`; a secret `name=value` or
    `"name": value` pair anywhere in a word (_pairs); the user info of `scheme://user:pass@`;
    and words shaped like well-known API tokens."""
    import re
    flag = re.compile(r'(?:--?|/)([A-Za-z][\w.-]*)')
    named = re.compile(r'((?:--?|/)?([A-Za-z_][\w.-]*))([=:])(.*)', re.S)
    url_user = re.compile(r'(://)[^\s@/]*(?::[^\s@]*)?@')
    token = re.compile(r'(?:sk-|gh[pousr]_|github_pat_|xox[abprs]-|glpat-|AKIA|ASIA)'
                       r'[A-Za-z0-9_\-]{8,}|eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]*')
    out, hide = [], None
    for w in words:
        if hide == 'user':
            out.append(_user_value(w))
            hide = None
            continue
        if hide:
            out.append(MASK)
            hide = None
            continue
        m = flag.fullmatch(w)
        if (m and _secret(m.group(1))) or w.lower() in ('bearer', 'basic'):
            out.append(w)
            hide = 'all'
            continue
        if w.lower() in USER_FLAGS:
            out.append(w)
            hide = 'user'
            continue
        m = named.fullmatch(w)
        if m and _secret(m.group(2)):
            out.append(m.group(1) + m.group(3) + MASK)
            continue
        if m and m.group(1).lower() in USER_FLAGS and m.group(3) == '=':
            out.append(m.group(1) + '=' + _user_value(m.group(4)))
            continue
        out.append(token.sub(MASK, url_user.sub(r'\1' + MASK + '@', _pairs(w))))
    return out


def _shorten(words):
    """The program and the first KEEP_WORDS words after it, each longer one cut to its start and
    end (a path keeps its file name), and `...(+N)` for the N words left out."""
    head, tail = WORD_MAX * 2 // 3, WORD_MAX // 3 - 3
    kept = [w if len(w) <= WORD_MAX else w[:head] + '...' + w[-tail:]
            for w in words[:1 + KEEP_WORDS]]
    if len(words) > len(kept):
        kept.append('...(+%d)' % (len(words) - len(kept)))
    return kept


def _text(b):
    """Bytes as text: as the file system decodes them, else with each bad byte as `\\xNN`."""
    try:
        return os.fsdecode(bytes(b))
    except UnicodeDecodeError:
        return bytes(b).decode('utf-8', 'backslashreplace')


def redact_command(cmd):
    """A command line as the incident log keeps it: secrets masked (_mask), then shortened
    (_shorten). A string (or bytes) command line gives a string, a list of words a list; None
    gives None. It never raises: a command line it cannot read is logged as MASK alone."""
    if cmd is None:
        return None
    try:
        if isinstance(cmd, (bytes, bytearray)):
            cmd = _text(cmd)
        if isinstance(cmd, str):
            import subprocess
            return subprocess.list2cmdline(_shorten(_mask(split_command_line(cmd))))
        return _shorten(_mask([_text(w) if isinstance(w, (bytes, bytearray)) else str(w)
                               for w in cmd]))
    except Exception:                   # noqa: BLE001 - the log keeps nothing it cannot read
        return MASK if isinstance(cmd, (str, bytes, bytearray)) else [MASK]


def _incident(how, child):
    """The incident line's fields; both command lines redacted (redact_command)."""
    import time
    try:
        ppid, pexe = _parent()
    except Exception:                   # noqa: BLE001 - the line goes without them
        ppid, pexe = None, None
    return {'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'how': how,
            'pid': os.getpid(), 'exe': sys.executable,
            'argv': redact_command(list(getattr(sys, 'orig_argv', sys.argv))),
            'child': redact_command(child), 'parent_pid': ppid, 'parent_exe': pexe}


def _log_incident(how, child):
    """Append one line to the incident log; any failure is swallowed (the start goes on)."""
    try:
        import json
        from ..config import user
        try:
            path = log_path()
        except user.HomeFenced:
            return
        line = json.dumps(_incident(how, child)) + '\n'
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > LOG_MAX:
                os.replace(path, path + '.1')
        except OSError:
            pass
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(line)
    except Exception:                   # noqa: BLE001 - a log that fails never stops the start
        pass
