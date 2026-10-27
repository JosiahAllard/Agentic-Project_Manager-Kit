"""pm_start check: a registered kit scheduled task whose program is a console program. Read only.

A task whose program is python.exe or powershell.exe (any console program) is started by a
process with no console, so Windows gives it a new console and shows its window on the desktop
(docs/daily-full.md "The schedule"). The kit's own task lines run pythonw.exe with the carrier
tools/no_window.py as its first argument (kitlib.proc.spawn.CARRIER), which opens none; a task
registered by hand, or before the carrier, may not.

A kit task is one whose name is a name the kit registers (`kit_names`) or one with an action that
names a path inside a kit folder (`kit_roots`: this kit, the live checkout an export runs for
(KIT_LIVE_ROOT, else the main checkout the export's marker records), and the folder of every
export of the plugin when this kit is one). A file named like a kit tool in
another folder (another repository's tools\\gate.py) is not the kit's. Its program is judged by
file name only: a console program in `CONSOLE` is flagged; pythonw.exe and anything else is not.
`read_tasks` reads every task's actions from `schtasks /Query /XML ONE` (`xml_input` says how its
bytes are read); `judge` is pure and is what the tests drive.

On Linux a job opens no console window, so `check` says nothing; `read_tasks` gives every pm-kit
job read back (kitlib.platform.linux.schedule.read_back) in the same form, for the readers that
look tasks up by name.
"""
import codecs
import os
import re
import subprocess
import xml.etree.ElementTree as ET

from kitlib import platform
from kitlib.launcher import autostart
from kitlib.machinesetup import agenttemp
from kitlib.proc import spawn

TIMEOUT_S = 4       # the session start's own TIMEOUT (kitlib.pmstart.TIMEOUT)
NS = '{http://schemas.microsoft.com/windows/2004/02/mit/task}'
CONSOLE = frozenset(('python.exe', 'py.exe', 'powershell.exe', 'pwsh.exe', 'cmd.exe', 'bash.exe'))
NAME_SUFFIXES = ('-worktree-sweep', '-daily-full')    # kitlib.worktree.task.task_name; docs/daily-full.md
KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOMS = (codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
DECLARATION = re.compile(rb'^\s*<\?xml[^>]*?\?>')
DECLARED = re.compile(rb'encoding\s*=\s*["\']([A-Za-z0-9._-]+)["\']')
WORD = re.compile(r'"([^"]*)"|(\S+)')
ABSOLUTE = re.compile(r'^(?:[A-Za-z]:\\|\\\\)')
OPTION = re.compile(r'^--?[A-Za-z][\w-]*[:=]')     # `-opt:` / `--opt=` before a path


def kit_names():
    """The task names the kit registers itself: the launcher's sign-in task and the agent
    temp sweep. `NAME_SUFFIXES` covers the per-repository ones."""
    return {autostart.TASK_NAME, agenttemp.TASK}


def _norm(path):
    return os.path.normcase(os.path.normpath(path.replace('/', '\\'))).rstrip('\\')


def kit_roots(kit=KIT, environ=None):
    """The folders a kit task's action may name, normalised: `kit`; the live kit checkout when
    `kit` is an export (kitlib.pluginsource.live_root: KIT_LIVE_ROOT, else the main checkout its
    marker records when that holds kit.json and a tools folder); and, when `kit`
    is a plugin export (it holds pluginsource.MARKER), the folder that holds it, so a task line
    naming an export of another version is the kit's too."""
    from kitlib import pluginsource
    roots = {_norm(kit), _norm(pluginsource.live_root(kit, environ))}
    if os.path.isfile(os.path.join(kit, pluginsource.MARKER)):
        roots.add(_norm(os.path.dirname(os.path.abspath(kit))))
    return roots


def program(exe):
    """The file name of a task's program, lower case, `.exe` added when it has no extension
    (`python` -> `python.exe`)."""
    name = (exe or '').strip().strip('"').replace('/', '\\').split('\\')[-1].lower()
    return name if os.path.splitext(name)[1] else name + '.exe'


def _inside(word, roots):
    path = word.replace('/', '\\')
    if not ABSOLUTE.match(path):
        return False                    # a relative word or an option: no folder it is inside
    p = _norm(path)
    return any(p == r or p.startswith(r + '\\') for r in roots)


def is_kit(task, roots):
    """Whether `task` ({"path", "actions": [{"exe", "args"}]}) is one of the kit's: by its name, or
    by a word of an action (its program, or an argument, quoted or not) that is an absolute path
    inside one of `roots` (normalised, as `kit_roots` gives them)."""
    name = task['path'].rsplit('\\', 1)[-1]
    if name in kit_names() or name.lower().endswith(NAME_SUFFIXES):
        return True
    for a in task['actions']:
        text = '"%s" %s' % ((a.get('exe') or '').strip().strip('"'), a.get('args') or '')
        for word in words(text):
            if _inside(word, roots):
                return True
    return False


def words(text):
    """Each word of a task line that may be a path: a double-quoted run whole (a path with a
    space) and each of its own words, then each word with a leading `&` (PowerShell's call
    operator), quotes and an option's `-opt:` / `--opt=` prefix taken off, so
    `-Command "& '<kit>\\tools\\x.ps1'"` and `--flag=<kit>\\x` give the path."""
    out = []
    for m in WORD.finditer(text):
        whole = m.group(1) if m.group(1) is not None else m.group(2)
        for w in [whole] + whole.split():
            w = w.lstrip('&').strip('"\';')
            w = OPTION.sub('', w, count=1).strip('"\'')
            if w:
                out.append(w)
    return out


def judge(tasks, roots=None):
    """One warning line per action of a kit task in `tasks` whose program is a console program.
    `roots`: the kit folders (`kit_roots()` by default)."""
    roots = kit_roots() if roots is None else {_norm(r) for r in roots}
    lines = []
    for task in sorted(tasks, key=lambda t: t['path'].lower()):
        if not is_kit(task, roots):
            continue
        for a in task['actions']:
            prog = program(a.get('exe'))
            if prog in CONSOLE:
                lines.append(
                    "scheduled task %s starts %s, a console program: a console window opens each time "
                    "it runs. Its program should be pythonw.exe with the carrier tools\\%s as its first "
                    "argument (docs/daily-full.md \"The schedule\": Set-ScheduledTask, the owner's act)"
                    % (task['path'], prog, spawn.CARRIER))
    return lines


def xml_input(raw):
    """What `parse` reads for schtasks' output bytes `raw`. Bytes that start with a byte order mark
    are given to the XML parser as they are, so it reads the declaration and the encoding itself;
    UTF-16 with no mark (a NUL among the first bytes) gets its mark first. Single-byte output is
    text in the console code page whatever its declaration says: `schtasks /Query /XML` declares
    encoding="UTF-16" and writes one byte per character to a pipe (measured on Windows 11), and
    `/XML ONE` writes no declaration. So such bytes go to the parser as bytes only when they
    declare a single-byte encoding; otherwise they are decoded as the OEM code page (the console
    code page of a process with no console of its own) with the declaration dropped."""
    if raw.startswith(BOMS):
        return raw
    if b'\0' in raw[:64]:
        return (codecs.BOM_UTF16_BE if raw[:1] == b'\0' else codecs.BOM_UTF16_LE) + raw
    m = DECLARATION.match(raw)
    if m:
        enc = DECLARED.search(m.group(0))
        name = enc.group(1).decode('ascii').lower().replace('-', '').replace('_', '') if enc else ''
        if name and not name.startswith(('utf16', 'ucs2', 'utf32')):
            return raw
        raw = raw[:m.start()] + raw[m.end():]
    return raw.decode('oem', errors='replace')


def parse(data):
    """[{"path", "actions": [{"exe", "args"}]}] for every task in `schtasks /Query /XML ONE`'s
    output (`data`: text, or bytes as `xml_input` gives them): the path from the comment before
    each <Task> (its URI when there is none), the Exec actions only. Raises ET.ParseError on data
    that is not that XML."""
    root = ET.fromstring(data, ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    tasks, path = [], None
    for el in root:
        if el.tag is ET.Comment:
            path = (el.text or '').strip()
            continue
        if el.tag != NS + 'Task':
            continue
        uri = el.findtext('%sRegistrationInfo/%sURI' % (NS, NS))
        actions = [{'exe': (e.findtext(NS + 'Command') or '').strip(),
                    'args': (e.findtext(NS + 'Arguments') or '').strip()}
                   for e in el.iter(NS + 'Exec')]
        tasks.append({'path': path or uri or '?', 'actions': actions})
        path = None
    return tasks


def read_tasks(run=None, timeout=TIMEOUT_S):
    """Every registered task's Exec actions (`parse` of `xml_input`), from `schtasks /Query /XML
    ONE`. Raises RuntimeError when schtasks fails or times out, OSError when it cannot be run,
    ET.ParseError on output that is not the XML. run: the tests' stand-in for spawn.run. On Linux:
    schedule.read_back (ScheduleError, a RuntimeError, when the crontab cannot be read)."""
    if platform.NAME == 'linux':
        from kitlib.platform.linux import schedule
        return schedule.read_back(run=run)
    try:
        r = (run or spawn.run)(['schtasks', '/Query', '/XML', 'ONE'], capture_output=True,
                               timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError('schtasks /Query /XML timed out after %s s' % timeout)
    if r.returncode != 0:
        said = ((r.stdout or b'') + (r.stderr or b'')).decode('oem', errors='replace').strip()
        raise RuntimeError('schtasks /Query /XML exit %d: %s' % (r.returncode, said[:200] or 'no output'))
    return parse(xml_input(r.stdout or b''))


def check(cfg, read=None):
    """Warning lines for kit tasks that start a console program (`judge`); [] when there are none.
    Always on: no config key. `cfg` is unused, kept for the checks pattern. On Linux: []."""
    if platform.NAME == 'linux':
        return []
    return judge((read or read_tasks)())
