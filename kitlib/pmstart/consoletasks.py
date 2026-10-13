"""pm_start check: a registered kit scheduled task whose program is a console program. Read only.

A task whose program is python.exe or powershell.exe (any console program) is started by a
process with no console, so Windows gives it a new console and shows its window on the desktop
(docs/daily-full.md "The schedule"). The kit's own task lines run pythonw.exe with the carrier
tools/no_window.py as its first argument (kitlib.proc.spawn.CARRIER), which opens none; a task
registered by hand, or before the carrier, may not.

A kit task is one whose name is a name the kit registers (`kit_names`) or whose action names the
kit's folder or one of its tools (`tools\\<file>` for a file in the kit's tools folder). Its
program is judged by file name only: a console program in `CONSOLE` is flagged; pythonw.exe and
anything else is not. `read_tasks` reads every task's actions from `schtasks /Query /XML ONE`;
`judge` is pure and is what the tests drive.
"""
import os
import subprocess
import xml.etree.ElementTree as ET

from kitlib.launcher import autostart
from kitlib.machinesetup import agenttemp
from kitlib.proc import spawn

TIMEOUT_S = 4       # the session start's own TIMEOUT (kitlib.pmstart.TIMEOUT)
NS = '{http://schemas.microsoft.com/windows/2004/02/mit/task}'
CONSOLE = frozenset(('python.exe', 'py.exe', 'powershell.exe', 'pwsh.exe', 'cmd.exe', 'bash.exe'))
NAME_SUFFIXES = ('-worktree-sweep', '-daily-full')    # kitlib.worktree.task.task_name; docs/daily-full.md
KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def kit_names():
    """The task names the kit registers itself: the launcher's sign-in task (and its old name) and
    the agent temp sweep. `NAME_SUFFIXES` covers the per-repository ones."""
    return {autostart.TASK_NAME, autostart.OLD_TASK_NAME, agenttemp.TASK}


def kit_tools(kit=KIT):
    """The file names in the kit's tools folder, lower case; empty when it cannot be listed."""
    try:
        return {n.lower() for n in os.listdir(os.path.join(kit, 'tools')) if n.endswith('.py')}
    except OSError:
        return set()


def program(exe):
    """The file name of a task's program, lower case, `.exe` added when it has no extension
    (`python` -> `python.exe`)."""
    name = (exe or '').strip().strip('"').replace('/', '\\').split('\\')[-1].lower()
    return name if os.path.splitext(name)[1] else name + '.exe'


def is_kit(task, kit=KIT, tools=None):
    """Whether `task` ({"path", "actions": [{"exe", "args"}]}) is one of the kit's."""
    name = task['path'].rsplit('\\', 1)[-1]
    if name in kit_names() or name.lower().endswith(NAME_SUFFIXES):
        return True
    root = os.path.normcase(os.path.normpath(kit)).rstrip('\\') + '\\'
    tools = kit_tools(kit) if tools is None else tools
    for a in task['actions']:
        text = ('%s %s' % (a.get('exe') or '', a.get('args') or '')).replace('/', '\\').lower()
        if root in os.path.normcase(text).replace('"', ''):
            return True
        if any('\\tools\\' + t in text for t in tools):
            return True
    return False


def judge(tasks, kit=KIT, tools=None):
    """One warning line per action of a kit task in `tasks` whose program is a console program."""
    tools = kit_tools(kit) if tools is None else tools
    lines = []
    for task in sorted(tasks, key=lambda t: t['path'].lower()):
        if not is_kit(task, kit, tools):
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


def parse(xml_text):
    """[{"path", "actions": [{"exe", "args"}]}] for every task in `schtasks /Query /XML ONE`'s
    output: the path from the comment before each <Task> (its URI when there is none), the Exec
    actions only. Raises ET.ParseError on text that is not that XML."""
    root = ET.fromstring(xml_text, ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
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
    """Every registered task's Exec actions (`parse`), from `schtasks /Query /XML ONE`. Raises
    RuntimeError when schtasks fails or times out, OSError when it cannot be run, ET.ParseError on
    output that is not the XML. run: the tests' stand-in for spawn.run."""
    try:
        r = (run or spawn.run)(['schtasks', '/Query', '/XML', 'ONE'], capture_output=True,
                               timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError('schtasks /Query /XML timed out after %s s' % timeout)
    out = (r.stdout or b'').decode('oem', errors='replace')
    if r.returncode != 0:
        said = (out + (r.stderr or b'').decode('oem', errors='replace')).strip()
        raise RuntimeError('schtasks /Query /XML exit %d: %s' % (r.returncode, said[:200] or 'no output'))
    return parse(out)


def check(cfg, read=None):
    """Warning lines for kit tasks that start a console program (`judge`); [] when there are none.
    Always on: no config key. `cfg` is unused, kept for the checks pattern."""
    return judge((read or read_tasks)())
