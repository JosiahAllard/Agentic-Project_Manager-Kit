"""Scheduled jobs on Linux (docs/linux-support-plan.md section 2.2 and slice 10), where Windows
registers a Task Scheduler task: a systemd user unit when the user's systemd manager answers, else
a crontab line; a job at sign-in with no systemd is an XDG autostart entry when a desktop session
exists. Imported only from the Linux branches of the kit's task code (kitlib.launcher.autostart,
kitlib.hubsite.autostart, kitlib.worktree.task, kitlib.machinesetup, kitlib.pmstart.consoletasks).

  Job(name, argv, workdir, when, description, limit_min)
      when: ('daily', 'HH:MM') | ('every', minutes) | ('boot',) | ('logon',)
      boot: once each time the user's manager starts (at sign-in; at boot with linger), a oneshot
      logon: a long-running server started then (Type=simple), or an XDG autostart entry
  backend(run)          SYSTEMD, CRON or None: which this machine has for a job
  describe(job, kind)   the lines saying what install would write, writing nothing
  install(job, kind)    write the job and enable it; the lines saying what was done
  remove(name)          take the job out of every backend; the lines saying what went
  read_back()           every pm-kit job written, as consoletasks' task dicts (written(): the
                        files only, no crontab)
  find(name), exists(name), state(name), start(name), linger(), same(task, job)

Every file and line written carries the job's marker (MARK + name, or DESKTOP_KEY in a .desktop
entry). A unit file, autostart entry or crontab line without that marker is never changed or
removed: install refuses, naming it. `crontab -l` failing any way but "no crontab" refuses too,
so a crontab that could not be read is never written over. Files go in the user's own folders
under the home from the password database (pwd.getpwuid(os.getuid()).pw_dir), never $HOME alone;
a test passes `home`. A unit and an entry are written to a temporary file in their folder and
moved into place. Nothing here prints; a failure is ScheduleError.
"""
import collections
import os
import re
import shlex
import shutil
import subprocess
import sys

from kitlib.platform import PlatformUnsupported

SYSTEMD = 'systemd'
CRON = 'cron'
AUTOSTART = 'autostart'
PREFIX = 'pm-kit-'
MARK = '# pm-kit job: '
DESKTOP_KEY = 'X-PmKit-Job'
TIMEOUT_S = 30
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
AT = re.compile(r'^([01][0-9]|2[0-3]):([0-5][0-9])$')
NO_CRONTAB = 'no crontab for'
KILL_AFTER_S = 30        # timeout(1)'s grace after the limit, before it kills

Job = collections.namedtuple('Job', 'name argv workdir when description limit_min')
Job.__new__.__defaults__ = ('', None)


class ScheduleError(RuntimeError):
    """The job cannot be written, read or removed: the message says which and why."""


# -------------------------------------------------------------------------------- where things go

def home_dir(home=None):
    """`home`, else the user's home from the password database (never $HOME alone)."""
    if home:
        return home
    try:
        import pwd
    except ImportError:
        raise PlatformUnsupported('a scheduled job on Linux needs the password database (pwd)')
    return pwd.getpwuid(os.getuid()).pw_dir


def _config(home):
    """The user's config folder: XDG_CONFIG_HOME when absolute and no `home` is given (systemd
    reads its user units there), else <home>/.config."""
    if not home:
        env = os.environ.get('XDG_CONFIG_HOME') or ''
        if os.path.isabs(env):
            return env
    return os.path.join(home_dir(home), '.config')


def unit_dir(home=None):
    return os.path.join(_config(home), 'systemd', 'user')


def autostart_dir(home=None):
    return os.path.join(_config(home), 'autostart')


def unit(name):
    return PREFIX + name


def python():
    """The interpreter a job runs: this one's base install (a venv's python goes with the venv),
    else this interpreter. Not checked to exist here."""
    return getattr(sys, '_base_executable', None) or sys.executable


# ------------------------------------------------------------------------------------- checks

def check(job):
    """ScheduleError when `job` cannot be written as asked."""
    if not NAME.match(job.name or ''):
        raise ScheduleError('a job name is letters, digits, ".", "_" and "-": %r' % (job.name,))
    if not job.argv or not all(isinstance(a, str) and a for a in job.argv):
        raise ScheduleError('job %s: no command to run' % job.name)
    texts = list(job.argv) + [job.workdir or '', job.description or '']
    if any(c in t for t in texts for c in '\n\r\0'):
        raise ScheduleError('job %s: a line break or NUL in its command, folder or description'
                            % job.name)
    if not os.path.isabs(job.argv[0]) or not job.workdir or not os.path.isabs(job.workdir):
        raise ScheduleError('job %s: its program and folder must be absolute paths' % job.name)
    kind = job.when[0] if job.when else None
    if kind == 'daily':
        if len(job.when) != 2 or not AT.match(job.when[1] or ''):
            raise ScheduleError('job %s: a daily time is HH:MM, not %r' % (job.name, job.when[1:]))
    elif kind == 'every':
        if len(job.when) != 2 or not isinstance(job.when[1], int) or not 1 <= job.when[1] <= 59:
            raise ScheduleError('job %s: every N minutes, N from 1 to 59, not %r'
                                % (job.name, job.when[1:]))
    elif kind not in ('boot', 'logon'):
        raise ScheduleError('job %s: no such schedule %r' % (job.name, job.when))
    if job.limit_min is not None and (not isinstance(job.limit_min, int) or job.limit_min < 1):
        raise ScheduleError('job %s: a time limit is whole minutes, not %r' % (job.name, job.limit_min))


def when_text(when):
    kind = when[0]
    if kind == 'daily':
        return 'daily at %s' % when[1]
    if kind == 'every':
        return 'every %d min' % when[1]
    return 'at each start of your user session' if kind == 'boot' else 'at sign-in'


# ------------------------------------------------------------------------------ running commands

def _run(run, argv, what, input_text=None, timeout=TIMEOUT_S):
    """CompletedProcess of `argv` (text). ScheduleError naming `what` when it cannot run or times
    out; the caller reads the exit code."""
    run = run or subprocess.run
    kw = dict(capture_output=True, text=True, timeout=timeout)
    if input_text is not None:
        kw['input'] = input_text
    try:
        return run(argv, **kw)
    except subprocess.TimeoutExpired:
        raise ScheduleError('%s: %s timed out after %d s' % (what, argv[0], timeout))
    except OSError as e:
        raise ScheduleError('%s: %s could not be run: %s' % (what, argv[0], e))


def _said(r):
    return ' '.join(((r.stderr or '') + ' ' + (r.stdout or '')).split())[:300] or 'no output'


def _ok(run, argv, what, input_text=None):
    r = _run(run, argv, what, input_text)
    if r.returncode != 0:
        raise ScheduleError('%s: %s exit %d: %s' % (what, ' '.join(argv[:3]), r.returncode, _said(r)))
    return r.stdout or ''


def has_systemd(run=None):
    """True when the user's systemd manager answers (`systemctl --user show-environment`)."""
    try:
        r = _run(run, ['systemctl', '--user', 'show-environment'], 'user systemd')
    except ScheduleError:
        return False
    return r.returncode == 0


def has_cron(which=None):
    return bool((which or shutil.which)('crontab'))


def has_desktop(environ=None):
    """True when this runs in a desktop session (XDG_CURRENT_DESKTOP or DESKTOP_SESSION set)."""
    env = os.environ if environ is None else environ
    return bool(env.get('XDG_CURRENT_DESKTOP') or env.get('DESKTOP_SESSION'))


def backend(run=None, which=None):
    """SYSTEMD when the user's systemd manager answers, else CRON when crontab is installed, else
    None. A logon job takes SYSTEMD or AUTOSTART instead (logon_backend)."""
    if has_systemd(run):
        return SYSTEMD
    return CRON if has_cron(which) else None


def logon_backend(run=None, environ=None):
    """SYSTEMD, else AUTOSTART in a desktop session, else None."""
    if has_systemd(run):
        return SYSTEMD
    return AUTOSTART if has_desktop(environ) else None


def choose(job, run=None, which=None, environ=None):
    """The backend `job` goes to: ScheduleError naming what is missing when there is none."""
    if job.when[0] == 'logon':
        kind = logon_backend(run, environ)
        if kind is None:
            raise ScheduleError('job %s: no user systemd (systemctl --user does not answer) and no '
                                'desktop session for an XDG autostart entry; nothing was written'
                                % job.name)
        return kind
    kind = backend(run, which)
    if kind is None:
        raise ScheduleError('job %s: no user systemd (systemctl --user does not answer) and no '
                            'crontab command; nothing was written' % job.name)
    return kind


# ---------------------------------------------------------------------------------- systemd form

def _sd_escape(text):
    """`text` with systemd's specifier and variable characters doubled (% and $)."""
    return text.replace('%', '%%').replace('$', '$$')


def _sd_unescape(text):
    return text.replace('%%', '%').replace('$$', '$')


def exec_line(argv):
    """ExecStart's value: each word double-quoted, \\ and " escaped, % and $ doubled."""
    return ' '.join('"%s"' % _sd_escape(a).replace('\\', '\\\\').replace('"', '\\"') for a in argv)


def exec_argv(value):
    """exec_line's argv back from its value; ValueError on text that does not split."""
    return [_sd_unescape(w) for w in shlex.split(value)]


def service_text(job):
    """A timed job is a oneshot its limit ends (TimeoutStartSec); a boot or logon job is a simple
    service, so the session's start does not wait on it. Every job but the logon server keeps what
    it starts when it ends (KillMode=process), as a Windows task's children outlive it."""
    kind = job.when[0]
    timed = kind in ('daily', 'every')
    lines = [MARK + job.name, '[Unit]',
             'Description=%s' % _sd_escape(job.description or 'pm-kit job %s' % job.name), '',
             '[Service]', 'Type=%s' % ('oneshot' if timed else 'simple'),
             'WorkingDirectory=%s' % _sd_escape(job.workdir),
             'ExecStart=%s' % exec_line(job.argv)]
    if timed:
        lines.append('TimeoutStartSec=%s' % ('%dmin' % job.limit_min if job.limit_min else 'infinity'))
    if kind != 'logon':
        lines.append('KillMode=process')
    if kind in ('boot', 'logon'):
        lines += ['', '[Install]', 'WantedBy=default.target']
    return '\n'.join(lines) + '\n'


def timer_text(job):
    kind, value = job.when[0], job.when[1]
    if kind == 'daily':
        timing = ['OnCalendar=*-*-* %s:00' % value, 'Persistent=true']
    else:
        timing = ['OnCalendar=*:0/%d' % value]
    return '\n'.join([MARK + job.name, '[Unit]',
                      'Description=%s' % _sd_escape(job.description or 'pm-kit job %s' % job.name),
                      '', '[Timer]'] + timing + ['', '[Install]', 'WantedBy=timers.target']) + '\n'


def unit_files(job, home=None):
    """{path: text} of the job's systemd units: a .service, and a .timer for daily and every."""
    folder = unit_dir(home)
    out = {os.path.join(folder, unit(job.name) + '.service'): service_text(job)}
    if job.when[0] in ('daily', 'every'):
        out[os.path.join(folder, unit(job.name) + '.timer')] = timer_text(job)
    return out


def enabled_unit(job):
    """The unit `systemctl --user enable` names: the timer of a timed job, else the service."""
    return unit(job.name) + ('.timer' if job.when[0] in ('daily', 'every') else '.service')


# ------------------------------------------------------------------------------------- cron form

def cron_line(job):
    """The crontab line: the schedule, `cd <folder> &&`, timeout(1) when the job has a limit, the
    command shell-quoted with % escaped (cron's newline), output dropped, then the marker."""
    kind = job.when[0]
    if kind == 'daily':
        h, m = job.when[1].split(':')
        when = '%d %d * * *' % (int(m), int(h))
    elif kind == 'every':
        when = '*/%d * * * *' % job.when[1]
    elif kind == 'boot':
        when = '@reboot'
    else:
        raise ScheduleError('job %s: a sign-in job has no crontab form' % job.name)
    limit = (['timeout', '--kill-after=%ds' % KILL_AFTER_S, '%d' % (job.limit_min * 60)]
             if job.limit_min else [])
    command = 'cd %s && %s >/dev/null 2>&1' % (shlex.quote(job.workdir),
                                               ' '.join(shlex.quote(a) for a in limit + list(job.argv)))
    return '%s %s %s%s' % (when, command.replace('%', '\\%'), MARK, job.name)


def _cron_marked(line):
    """The job name a crontab line is marked with, or None."""
    i = line.rfind(' ' + MARK)
    if i < 0:
        return None
    name = line[i + 1 + len(MARK):].strip()
    return name if NAME.match(name) else None


def parse_cron(line):
    """A task dict from a marked crontab line, or None for any other line."""
    name = _cron_marked(line)
    if name is None:
        return None
    body = line[:line.rfind(' ' + MARK)].replace('\\%', '%')
    try:
        if body.startswith('@'):
            when_word, rest = body.split(None, 1)
            when = ('boot',) if when_word == '@reboot' else (when_word,)
        else:
            fields = body.split(None, 5)
            m, h, rest = fields[0], fields[1], fields[5]
            if m.startswith('*/') and h == '*':
                when = ('every', int(m[2:]))
            else:
                when = ('daily', '%02d:%02d' % (int(h), int(m)))
        words = shlex.split(rest)
    except (ValueError, IndexError):
        return _task(name, CRON, [], None, None, line=line)
    workdir = None
    if len(words) >= 3 and words[0] == 'cd' and words[2] == '&&':
        workdir, words = words[1], words[3:]
    if words[-2:] == ['>/dev/null', '2>&1']:
        words = words[:-2]
    limit = None
    if words[:1] == ['timeout'] and len(words) >= 3 and words[1].startswith('--kill-after='):
        limit, words = int(words[2]) // 60 or None, words[3:]
    return _task(name, CRON, words, workdir, when, line=line, limit_min=limit)


def read_crontab(run=None):
    """The user's crontab text: '' when there is none; ScheduleError when it cannot be read any
    other way (so it is never written over)."""
    r = _run(run, ['crontab', '-l'], 'reading the crontab')
    if r.returncode == 0:
        return r.stdout or ''
    if NO_CRONTAB in (r.stderr or '') + (r.stdout or ''):
        return ''
    raise ScheduleError('reading the crontab: crontab -l exit %d: %s; nothing was written'
                        % (r.returncode, _said(r)))


def _write_crontab(text, run):
    _ok(run, ['crontab', '-'], 'writing the crontab', input_text=text)


def _cron_without(text, name):
    kept = [l for l in text.splitlines() if _cron_marked(l) != name]
    return kept, len(kept) != len(text.splitlines())


# -------------------------------------------------------------------------------- autostart form

def _desktop_word(arg):
    """One Exec word of a desktop entry: double-quoted, with " ` $ and \\ escaped."""
    return '"%s"' % re.sub(r'(["`$\\])', r'\\\1', arg)


def desktop_exec(argv):
    """Exec's value as written in the file: the quoted words, % doubled, then the string escape
    (each \\ doubled), as the Desktop Entry Specification asks."""
    return ' '.join(_desktop_word(a) for a in argv).replace('%', '%%').replace('\\', '\\\\')


def desktop_argv(value):
    """desktop_exec's argv back from its value; ValueError on text that does not split."""
    text = re.sub(r'\\(.)', lambda m: {'s': ' ', 'n': '\n', 't': '\t', 'r': '\r'}.get(m.group(1), m.group(1)),
                  value)
    out, word, quoted, i = [], None, False, 0
    while i < len(text):
        c = text[i]
        if quoted:
            if c == '\\' and i + 1 < len(text):
                i += 1
                word += text[i]
            elif c == '"':
                quoted = False
            else:
                word += c
        elif c == '"':
            quoted, word = True, word or ''
        elif c == ' ':
            if word is not None:
                out.append(word)
            word = None
        else:
            word = (word or '') + c
        i += 1
    if quoted:
        raise ValueError('an unclosed quote in Exec')
    if word is not None:
        out.append(word)
    return [w.replace('%%', '%') for w in out]


def desktop_text(job):
    return '\n'.join(['[Desktop Entry]', 'Type=Application', 'Name=pm-kit %s' % job.name,
                      'Comment=%s' % (job.description or job.name).replace('\\', '\\\\'),
                      'Exec=%s' % desktop_exec(job.argv),
                      'Path=%s' % job.workdir.replace('\\', '\\\\'), 'Terminal=false',
                      'NoDisplay=true', 'X-GNOME-Autostart-enabled=true',
                      '%s=%s' % (DESKTOP_KEY, job.name)]) + '\n'


def desktop_path(name, home=None):
    return os.path.join(autostart_dir(home), unit(name) + '.desktop')


# ------------------------------------------------------------------------------------ the files

def _read(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise ScheduleError('cannot read %s: %s' % (path, e))


def _owner(text):
    """The job name a unit file or desktop entry is marked with, or None."""
    if text is None:
        return None
    first = text.split('\n', 1)[0]
    if first.startswith(MARK):
        return first[len(MARK):].strip()
    for line in text.splitlines():
        if line.startswith(DESKTOP_KEY + '='):
            return line[len(DESKTOP_KEY) + 1:].strip()
    return None


def _refuse_foreign(path, name):
    text = _read(path)
    if text is not None and _owner(text) != name:
        raise ScheduleError('%s is there and is not the pm-kit job %s (no "%s%s" marker); it is '
                            'left as is and nothing was written' % (path, name, MARK, name))


def _write(path, text):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, '.%s.%d.tmp' % (os.path.basename(path), os.getpid()))
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ScheduleError('cannot write %s: %s' % (path, e))


# -------------------------------------------------------------------------------- the verbs

def describe(job, kind, home=None):
    """The lines saying what install(job, kind) writes and runs; writes nothing."""
    check(job)
    head = 'pm-kit job %s (%s)' % (job.name, when_text(job.when))
    if kind == SYSTEMD:
        lines = ['%s as a systemd user unit:' % head]
        for path, text in sorted(unit_files(job, home).items()):
            lines.append('  write %s:' % path)
            lines += ['    ' + l for l in text.splitlines()]
        lines.append('  then: systemctl --user daemon-reload; systemctl --user enable%s %s'
                     % (' --now' if job.when[0] in ('daily', 'every') else '', enabled_unit(job)))
        return lines
    if kind == CRON:
        return ['%s as a crontab line:' % head, '  ' + cron_line(job)]
    if kind == AUTOSTART:
        path = desktop_path(job.name, home)
        return (['%s as an XDG autostart entry:' % head, '  write %s:' % path]
                + ['    ' + l for l in desktop_text(job).splitlines()])
    raise ScheduleError('no such backend %r' % (kind,))


def undo_text(name, kind):
    """How to take the job out by hand, as one line."""
    if kind == SYSTEMD:
        return ('systemctl --user disable --now %s.timer %s.service; rm -f ~/.config/systemd/user/%s.*; '
                'systemctl --user daemon-reload' % (unit(name), unit(name), unit(name)))
    if kind == CRON:
        return 'crontab -e, and delete the line ending "%s%s"' % (MARK, name)
    return 'rm ~/.config/autostart/%s.desktop' % unit(name)


def install(job, kind=None, home=None, run=None, which=None, environ=None):
    """Write `job` to `kind` (default: choose) and enable it; the lines saying what was done. A
    timed job's timer is started now; a boot or logon job is enabled for the next start of the
    user's session and not started. ScheduleError, with nothing changed, when a file or line of
    that name is not the kit's; when systemctl fails after the files are written the error says
    so."""
    check(job)
    kind = kind or choose(job, run, which, environ)
    if kind == SYSTEMD:
        files = unit_files(job, home)
        for path in files:
            _refuse_foreign(path, job.name)
        for path, text in sorted(files.items()):
            _write(path, text)
        _ok(run, ['systemctl', '--user', 'daemon-reload'], 'job %s' % job.name)
        now = job.when[0] in ('daily', 'every')
        _ok(run, ['systemctl', '--user', 'enable'] + (['--now'] if now else []) + [enabled_unit(job)],
            'job %s' % job.name)
        return ['installed the systemd user unit %s (%s)%s' % (enabled_unit(job), when_text(job.when),
                                                              ', started' if now else ', enabled')]
    if kind == CRON:
        text = read_crontab(run)
        kept, had = _cron_without(text, job.name)
        _write_crontab('\n'.join(kept + [cron_line(job)]) + '\n', run)
        return ['%s the crontab line of pm-kit job %s (%s)' % ('replaced' if had else 'added',
                                                               job.name, when_text(job.when))]
    if kind == AUTOSTART:
        if job.when[0] != 'logon':
            raise ScheduleError('job %s: an autostart entry is for a sign-in job only' % job.name)
        path = desktop_path(job.name, home)
        _refuse_foreign(path, job.name)
        _write(path, desktop_text(job))
        return ['wrote the XDG autostart entry %s (at sign-in to the desktop)' % path]
    raise ScheduleError('no such backend %r' % (kind,))


def remove(name, home=None, run=None, which=None):
    """Take the pm-kit job `name` out of every backend that has it; the lines saying what went.
    Only files and lines marked as that job are touched."""
    lines = []
    folder = unit_dir(home)
    paths = [os.path.join(folder, unit(name) + s) for s in ('.timer', '.service')]
    mine = [p for p in paths if _owner(_read(p)) == name]
    if mine:
        r = _run(run, ['systemctl', '--user', 'disable', '--now'] + [os.path.basename(p) for p in mine],
                 'job %s' % name)
        for p in mine:
            os.remove(p)
        _run(run, ['systemctl', '--user', 'daemon-reload'], 'job %s' % name)
        lines.append('removed %s%s' % (', '.join(os.path.basename(p) for p in mine),
                                       '' if r.returncode == 0 else ' (disable said: %s)' % _said(r)))
    if has_cron(which):
        text = read_crontab(run)
        kept, had = _cron_without(text, name)
        if had:
            _write_crontab('\n'.join(kept) + '\n' if kept else '', run)
            lines.append('removed the crontab line of %s' % name)
    path = desktop_path(name, home)
    if _owner(_read(path)) == name:
        os.remove(path)
        lines.append('removed %s' % path)
    return lines


def _words(argv):
    return ' '.join('"%s"' % a if ' ' in a else a for a in argv)


def _task(name, kind, argv, workdir, when, **more):
    task = {'path': name, 'backend': kind, 'argv': list(argv), 'workdir': workdir, 'when': when,
            'actions': [{'exe': argv[0] if argv else '', 'args': _words(argv[1:])}]}
    task.update(more)
    return task


def _ini(text):
    """{key: value} of a unit file or desktop entry (the last of a repeated key)."""
    out = {}
    for line in text.splitlines():
        if '=' in line and not line.startswith(('#', ';', '[')):
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    return out


def _from_units(folder):
    tasks = []
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return tasks
    except OSError as e:
        raise ScheduleError('cannot list %s: %s' % (folder, e))
    for n in names:
        if not (n.startswith(PREFIX) and n.endswith('.service')):
            continue
        path = os.path.join(folder, n)
        text = _read(path)
        name = _owner(text)
        if not name or n != unit(name) + '.service':
            continue
        keys = _ini(text)
        try:
            argv = exec_argv(keys.get('ExecStart', ''))
        except ValueError:
            argv = []
        timer = _read(os.path.join(folder, unit(name) + '.timer'))
        when = _timer_when(_ini(timer)) if timer is not None and _owner(timer) == name else (
            ('boot',) if keys.get('KillMode') == 'process' else ('logon',))
        limit = keys.get('TimeoutStartSec', '')
        tasks.append(_task(name, SYSTEMD, argv, _sd_unescape(keys.get('WorkingDirectory', '')) or None,
                           when, unit=unit(name) + ('.timer' if timer is not None else '.service'),
                           limit_min=int(limit[:-3]) if limit.endswith('min') and limit[:-3].isdigit()
                           else None, file=path))
    return tasks


def _timer_when(keys):
    cal = keys.get('OnCalendar', '')
    m = re.match(r'^\*-\*-\* (\d\d:\d\d):00$', cal)
    if m:
        return ('daily', m.group(1))
    m = re.match(r'^\*:0/(\d+)$', cal)
    return ('every', int(m.group(1))) if m else (cal,)


def _from_autostart(folder):
    tasks = []
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return tasks
    except OSError as e:
        raise ScheduleError('cannot list %s: %s' % (folder, e))
    for n in names:
        if not (n.startswith(PREFIX) and n.endswith('.desktop')):
            continue
        path = os.path.join(folder, n)
        text = _read(path)
        name = _owner(text)
        if not name or n != unit(name) + '.desktop':
            continue
        keys = _ini(text)
        try:
            argv = desktop_argv(keys.get('Exec', ''))
        except ValueError:
            argv = []
        tasks.append(_task(name, AUTOSTART, argv, keys.get('Path', '').replace('\\\\', '\\') or None,
                           ('logon',), file=path, hidden=keys.get('Hidden', '').lower() == 'true'))
    return tasks


def read_back(home=None, run=None, which=None):
    """Every pm-kit job written, from every backend: the unit files in the user's unit folder, the
    marked crontab lines and the autostart entries, each a task dict in consoletasks' form
    ({'path': name, 'actions': [{'exe', 'args'}]}) with 'backend', 'argv', 'workdir', 'when' and
    'limit_min' added. ScheduleError when a folder or the crontab cannot be read."""
    tasks = _from_units(unit_dir(home))
    if has_cron(which):
        for line in read_crontab(run).splitlines():
            t = parse_cron(line)
            if t is not None:
                tasks.append(t)
    return tasks + _from_autostart(autostart_dir(home))


def written(home=None):
    """read_back's unit files and autostart entries only: no crontab is read."""
    return _from_units(unit_dir(home)) + _from_autostart(autostart_dir(home))


def find(name, tasks=None, home=None, run=None, which=None):
    """The read-back task of the job `name` (the first, systemd before cron before autostart), or
    None."""
    for t in read_back(home, run, which) if tasks is None else tasks:
        if t['path'] == name:
            return t
    return None


def exists(name, home=None, run=None, which=None):
    return find(name, home=home, run=run, which=which) is not None


def same(task, job):
    """Whether the read-back `task` runs `job` as it is now: its command, folder and schedule."""
    if not task:
        return False
    return (task.get('argv') == list(job.argv) and task.get('workdir') == job.workdir
            and tuple(task.get('when') or ()) == tuple(job.when)
            and task.get('limit_min') == job.limit_min)


def state(name, run=None):
    """{'LoadState', 'UnitFileState', 'ActiveState'} of the job's service from `systemctl --user
    show`. ScheduleError when systemctl fails."""
    out = _ok(run, ['systemctl', '--user', 'show', unit(name) + '.service', '-p', 'LoadState',
                    '-p', 'UnitFileState', '-p', 'ActiveState'], 'reading the state of %s' % unit(name))
    return dict(l.split('=', 1) for l in out.splitlines() if '=' in l)


def start(name, run=None):
    """Start the job's service now (`systemctl --user start`). ScheduleError when it fails."""
    _ok(run, ['systemctl', '--user', 'start', unit(name) + '.service'], 'starting %s' % unit(name))


LINGER_COMMAND = 'loginctl enable-linger'


def linger(run=None, uid=None):
    """True when the user's manager is kept running without a sign-in (loginctl's Linger), False
    when not, None when it cannot be told. Reads only: enabling it is the owner's act
    (LINGER_COMMAND)."""
    if uid is None:
        if not hasattr(os, 'getuid'):
            return None
        uid = os.getuid()
    try:
        r = _run(run, ['loginctl', 'show-user', str(uid), '-p', 'Linger', '--value'], 'linger')
    except ScheduleError:
        return None
    said = (r.stdout or '').strip()
    if r.returncode != 0 or said not in ('yes', 'no'):
        return None
    return said == 'yes'


def linger_note(run=None, uid=None):
    """A line for a job that should run without a sign-in when linger is off, else None."""
    if linger(run, uid) is False:
        return ('linger is off for this user, so user systemd runs these jobs only while you are '
                'signed in; to run them from boot, the owner runs: %s' % LINGER_COMMAND)
    return None
