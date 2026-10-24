"""One tick of the automatic tasks (kitlib.autotasks): read the hub's schedules, start each one that
is due (kitlib.autotasks.due) from the fixed TASKS table, and record it.

The hub only says when: a schedule names a task by its name, and the program and its arguments come
from TASKS alone (argv). A name TASKS does not hold is refused, never run. The tools run are this
kit's own (`kit`: the folder holding tools/auto_tasks.py), with the python.exe beside the running
interpreter, each detached with its output in logs/<schedule>.log of the state folder.

The state folder is `auto-tasks` in the PM repository's machine config folder (<PM repo>/.pm-kit,
kitlib.config.user.locate's folder), which git ignores (kitlib.config.move GITIGNORE): the tick
starts nothing when the PM-repo pointer is not good, when that folder is not ignored (unignored), or
while a state is still in the retired home place, ~/.pm-kit/auto-tasks (home_problem names the
command that moves or removes it; it is never read). state.json holds, per schedule, the last thing
done for it ({'at', 'what', ...}; its `at` is the schedule's baseline), tick.lock is the guard,
tick.log the tick's own lines (one file of LOG_MAX bytes and one older one).

Two ticks that overlap never start one task twice. The tick takes tick.lock (kitlib.fs.treelock: an
OS byte-range lock, which the OS lets go when the holder exits, so a crashed tick leaves no stale
lock behind, unlike a file made with O_EXCL, which would need an age rule to clear), reads the state,
writes every start record (a temporary file, then os.replace) and only then starts the programs,
releasing the lock after. A second tick that finds the lock held starts nothing. A program whose
start fails keeps its record (`what` failed): it runs again at its next slot, not at every tick.

Nothing here prints or exits; tools/auto_tasks.py is the entry.
"""
import collections
import datetime
import json
import os
import subprocess
import sys

from kitlib.config import ConfigError, user as kuser
from kitlib.fs.treelock import TreeBusy, TreeLock, TreeLockError
from kitlib.proc import spawn

from . import due

DIR_NAME = 'auto-tasks'
STATE_NAME = 'state.json'
LOCK_NAME = 'tick.lock'
LOG_NAME = 'tick.log'
LOGS = 'logs'
LOG_MAX = 256 * 1024

EXIT_OK = 0
EXIT_FAILED = 1

STARTED, SKIPPED, SEEN, FAILED = 'started', 'skipped', 'seen', 'failed'

Task = collections.namedtuple('Task', 'tool per_repo args')

# The fixed table: task name -> the tool under the kit, whether it runs for one repository (-C <its
# main checkout>), and its arguments. Nothing the hub sends is ever a program or an argument.
TASKS = {
    'maint-lint': Task(('tools', 'maint_lint.py'), True, ()),
    'worktree-sweep': Task(('tools', 'worktree_remove.py'), True, ('--sweep-merged', '--apply')),
    'agent-temp-sweep': Task(('optional', 'agenttemp', 'agent_temp_sweep.py'), False, ('--apply',)),
}


class UnknownTask(ValueError):
    """A task name the fixed table does not hold."""


def tool_path(name, kit):
    """The tool the task `name` runs under `kit`. UnknownTask for a name TASKS does not hold."""
    task = TASKS.get(name) if isinstance(name, str) else None
    if task is None:
        raise UnknownTask('unknown task %r: only %s are run' % (name, ', '.join(sorted(TASKS))))
    return os.path.join(kit, *task.tool)


def argv(name, kit, main=None, python=None):
    """The command line of the task `name`: `python` (default the python.exe beside this one), its
    tool under `kit`, `-C main` for a task that runs for one repository, then its fixed arguments.
    UnknownTask for a name TASKS does not hold; ValueError for a per-repository task with no `main`."""
    tool = tool_path(name, kit)
    task = TASKS[name]
    out = [python or spawn.console_python(), tool]
    if task.per_repo:
        if not main:
            raise ValueError('task %s needs a repository' % name)
        out += ['-C', os.path.normpath(main)]
    return out + list(task.args)


NO_POINTER = ('no PM repo pointer is set, so the auto-tasks state has no folder: set it '
              '(tools/machine_setup.py --pm-repo, %s)' % kuser.POINTER_DOC)
# home_problem's lines: (the old folder, the new one, each with every ' doubled for PowerShell's
# single-quoted string).
HOME_MOVE = ("the auto-tasks state moved to the PM repository (%s); it is still in the home folder "
             "(%s) and the tick starts nothing until it is moved: move it (Copy-Item -LiteralPath "
             "'%s' -Destination '%s' -Recurse, then, once the copy is there, Remove-Item -LiteralPath "
             "'%s' -Recurse)")
HOME_BOTH = ("the auto-tasks state is in the PM repository (%s) and an old copy is still in the home "
             "folder (%s); the tick starts nothing until it is gone: check it, then remove it "
             "(Remove-Item -LiteralPath '%s' -Recurse)")
UNIGNORED = ("git does not ignore the auto-tasks state folder in %s: add the line auto-tasks/ to %s "
             "and commit it")


def state_dir(home=None):
    """<PM repo>/.pm-kit/auto-tasks: the PM repository's git-ignored machine config folder
    (kitlib.config.user.locate). ConfigError (NO_POINTER) with no PM-repo pointer, and the
    location's error when the machine config cannot be read."""
    return _pm_state_dir(kuser.locate(home))


def _pm_state_dir(loc):
    """state_dir for the Location `loc`."""
    if loc.state == kuser.NONE:
        raise ConfigError(NO_POINTER)
    if loc.error or not loc.folder:
        raise ConfigError(loc.error or NO_POINTER)
    return os.path.join(loc.folder, DIR_NAME)


def home_state_dir(home=None):
    """~/.pm-kit/auto-tasks: the state's retired place, never read; home_problem names its move."""
    return os.path.join(home or kuser.home_dir(), kuser.DIR_NAME, DIR_NAME)


def _quoted(path):
    return path.replace("'", "''")


def home_problem(folder, home=None):
    """None when the retired home state folder (home_state_dir) is not there; else the line that
    names its move to `folder` (state_dir's) or, when `folder` is there too, its removal."""
    old = home_state_dir(home)
    if not os.path.lexists(old):
        return None
    if os.path.lexists(folder):
        return HOME_BOTH % (folder, old, _quoted(old))
    return HOME_MOVE % (folder, old, _quoted(old), _quoted(folder), _quoted(old))


def unignored(pm):
    """None when git ignores the state folder in the PM repository `pm`; else the problem: the
    line its .pm-kit/.gitignore lacks, or why git could not tell."""
    from kitlib.config import move
    left, why = move.ignored(pm, [DIR_NAME + '/' + STATE_NAME])
    if left is None:
        return 'git could not tell whether the auto-tasks state folder is ignored in %s (%s)' % (pm, why)
    if left:
        return UNIGNORED % (pm, os.path.join(pm, kuser.DIR_NAME, '.gitignore'))
    return None


def read_state(path):
    """(records by schedule key, problem or None). A missing file is no records; one that cannot be
    read as a JSON object is no records too, and the problem says so."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return {}, 'state file %s could not be read (%s); taken as empty' % (path, type(e).__name__)
    records = data.get('tasks') if isinstance(data, dict) else None
    if not isinstance(records, dict):
        return {}, 'state file %s holds no task records; taken as empty' % path
    return {k: v for k, v in records.items() if isinstance(v, dict)}, None


def write_state(path, records):
    """Write `records` to `path` in one step: a temporary file beside it, then os.replace."""
    tmp = '%s.%d.tmp' % (path, os.getpid())
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump({'version': 1, 'tasks': records}, f, indent=1, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)


def baseline(record):
    """The naive local time of a state record's `at`, None when it has none."""
    return due.local(record.get('at')) if isinstance(record, dict) else None


def key(schedule):
    """The state key of a hub schedule: its document id, else `<task>-<repo>`."""
    sid = schedule.get('id')
    if isinstance(sid, str) and sid:
        return sid
    return '%s-%s' % (schedule.get('task'), schedule.get('repo'))


def launcher_repos(home=None):
    """[(label, main checkout, repo name)] of the machine config's launcher.repos, the name as
    kitlib.machinesetup.worktreesweep.repo_task gives it; a folder that cannot be read is left out."""
    from kitlib.config import load as load_kit_config
    from kitlib.launcher import settings as launcher
    from kitlib.worktree.remove import main_checkout
    out = []
    for label, folder in sorted(launcher.load(home).repos.items()):
        try:
            main = main_checkout(folder)
            name = load_kit_config(main).repo.get('name') or os.path.basename(os.path.normpath(main))
        except (RuntimeError, ConfigError, OSError):
            continue
        out.append((label, os.path.normpath(main), name))
    return out


def find_repo(repo, repos):
    """The main checkout of the repository a schedule names (its name, launcher label or checkout
    folder name, either case), from launcher_repos' list; None when none or more than one checkout
    matches."""
    if not isinstance(repo, str) or not repo:
        return None
    want = repo.lower()
    found = set(os.path.normcase(m) for label, m, name in repos
                if want in (label.lower(), str(name).lower(), os.path.basename(m).lower()))
    if len(found) != 1:
        return None
    return next(m for label, m, name in repos if os.path.normcase(m) in found)


def hub_schedules(home=None):
    """The hub's schedule documents (GET /schedules). serve.ServeError (NotRunning among them)
    when the hub cannot be reached; ConfigError for a hub setting that cannot be read."""
    from kitlib.hubsite import client, settings as hub_settings
    return client.Client(hub_settings.load(home=home).port).listing('schedules')


def launch(command, log, cwd):
    """Start `command` detached from this process (its own process group, out of this one's job
    when the job allows that), stdin empty, stdout and stderr to the file `log`; its pid."""
    kw = dict(stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT, cwd=cwd, env=spawn.agent_temp_env(),
              close_fds=True)
    group = subprocess.CREATE_NEW_PROCESS_GROUP
    with open(log, 'wb') as out:
        try:
            p = spawn.popen(command, stdout=out, creationflags=group | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
        except OSError:      # a job that does not allow breaking away refuses the flag
            p = spawn.popen(command, stdout=out, creationflags=group, **kw)
    return p.pid


def _stamp(t):
    return t.replace(microsecond=0).isoformat()


def _write_log(folder, lines, now):
    """Append `lines` to tick.log, the file moved to tick.log.1 once it passes LOG_MAX. A log that
    cannot be written is skipped."""
    path = os.path.join(folder, LOG_NAME)
    try:
        if os.path.isfile(path) and os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + '.1')
        with open(path, 'a', encoding='utf-8', newline='\n') as f:
            for line in lines:
                f.write('%s %s\n' % (_stamp(now), line))
    except OSError:
        pass


def tick(home=None, kit=None, now=None, fetch=None, start=None, repos=None, python=None):
    """One tick: (exit code, lines). `kit` the kit folder whose tools run (default this kitlib's);
    `now` naive local time (default the clock); `fetch` returns the hub's schedules (default
    hub_schedules); `start(command, log, cwd)` starts one program and returns its pid (default
    launch); `repos` returns launcher_repos' list (default launcher_repos(home)); `python` the
    interpreter the tools run with. The seams are for tests.

    The hub not reachable: EXIT_OK, one line, nothing started. A hub setting, an answer, the
    state folder (state_dir, home_problem, unignored) or the lock that cannot be used: EXIT_FAILED,
    nothing started."""
    from kitlib.hubsite import client, serve
    kit = os.path.normpath(kit or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    now = now or datetime.datetime.now()
    try:
        schedules = (fetch or (lambda: hub_schedules(home)))()
    except client.Refused as e:
        return EXIT_FAILED, ['hub refused the schedules (%s): nothing started' % e]
    except serve.ServeError as e:
        return EXIT_OK, ['hub not reachable (%s): nothing started' % e]
    except ConfigError as e:
        return EXIT_FAILED, ['hub settings could not be read (%s): nothing started' % e]
    try:
        folder = state_dir(home)
    except ConfigError as e:
        return EXIT_FAILED, ['%s: nothing started' % e]
    problem = home_problem(folder, home) or unignored(os.path.dirname(os.path.dirname(folder)))
    if problem:
        return EXIT_FAILED, ['%s: nothing started' % problem]
    try:
        os.makedirs(os.path.join(folder, LOGS), exist_ok=True)
    except OSError as e:
        return EXIT_FAILED, ['state folder %s could not be made (%s): nothing started' % (folder, e)]
    lock = TreeLock(os.path.join(folder, LOCK_NAME), 'auto_tasks.py tick')
    try:
        lock.acquire()
    except TreeBusy:
        return EXIT_OK, ['another tick holds %s: nothing started' % lock.path]
    except TreeLockError as e:
        return EXIT_FAILED, ['%s: nothing started' % e]
    try:
        code, lines = _tick_locked(folder, kit, now, schedules, start or launch,
                                   repos or (lambda: launcher_repos(home)), python)
    finally:
        lock.release()
    _write_log(folder, lines, now)
    return code, lines


def _tick_locked(folder, kit, now, schedules, start, repos, python):
    path = os.path.join(folder, STATE_NAME)
    records, problem = read_state(path)
    lines = [problem] if problem else []
    known = None
    plans = []                      # (key, command, log)
    machine_wide = set()            # a machine-wide task starts once a tick, whichever schedule asks
    for schedule in sorted((s for s in schedules if isinstance(s, dict)), key=key):
        k = key(schedule)
        name = schedule.get('task')
        verdict, detail = due.decide(schedule, baseline(records.get(k)), now)
        if verdict in (due.DISABLED, due.NOT_DUE):
            continue
        if verdict == due.BAD:
            lines.append('%s skipped: %s' % (k, detail))
            continue
        if verdict == due.FIRST:
            records[k] = {'at': _stamp(now), 'what': SEEN}
            continue
        slot = _stamp(detail)
        try:
            tool = tool_path(name, kit)
        except UnknownTask as e:
            records[k] = {'at': _stamp(now), 'what': SKIPPED, 'slot': slot, 'why': str(e)}
            lines.append('%s refused: %s' % (k, e))
            continue
        if not os.path.isfile(tool):
            why = 'no %s in this kit' % tool
            records[k] = {'at': _stamp(now), 'what': SKIPPED, 'slot': slot, 'why': why}
            lines.append('%s skipped: %s' % (k, why))
            continue
        main = None
        if TASKS[name].per_repo:
            if known is None:
                try:
                    known = repos()
                except (ConfigError, OSError) as e:
                    known = []
                    lines.append('launcher.repos could not be read (%s)' % e)
            main = find_repo(schedule.get('repo'), known)
            if main is None:
                why = 'repository %r is not one checkout in launcher.repos' % (schedule.get('repo'),)
                records[k] = {'at': _stamp(now), 'what': SKIPPED, 'slot': slot, 'why': why}
                lines.append('%s skipped: %s' % (k, why))
                continue
        elif name in machine_wide:
            records[k] = {'at': _stamp(now), 'what': SKIPPED, 'slot': slot,
                          'why': '%s already started this tick' % name}
            continue
        else:
            machine_wide.add(name)
        exe = python or spawn.console_python()
        if not exe:
            why = 'no python.exe beside %s to run it with' % sys.executable
            records[k] = {'at': _stamp(now), 'what': SKIPPED, 'slot': slot, 'why': why}
            lines.append('%s skipped: %s' % (k, why))
            continue
        command = argv(name, kit, main, exe)
        log = os.path.join(folder, LOGS, '%s.log' % _safe(k))
        records[k] = {'at': _stamp(now), 'what': STARTED, 'slot': slot, 'argv': command, 'log': log}
        plans.append((k, command, log))
    try:
        write_state(path, records)       # every start is on record before any program starts
    except OSError as e:
        return EXIT_FAILED, lines + ['state file %s could not be written (%s): nothing started' % (path, e)]
    if not plans:
        return EXIT_OK, lines
    code = EXIT_OK
    for k, command, log in plans:
        try:
            pid = start(command, log, kit)
        except OSError as e:
            records[k].update(what=FAILED, why=str(e))
            lines.append('%s could not be started (%s)' % (k, e))
            code = EXIT_FAILED
            continue
        records[k]['pid'] = pid
        lines.append('%s started (pid %s): %s; log %s' % (k, pid, ' '.join(command), log))
    try:
        write_state(path, records)
    except OSError as e:
        lines.append('state file %s could not be updated (%s)' % (path, e))
    return code, lines


def _safe(text):
    return ''.join(c if c.isalnum() or c in '-_.' else '_' for c in text)
