"""kitlib.hubsite.plugin_job: the hub server's plugin refresh job (docs/plugin-source.md "Hub
refresh").

  make(sources) -> jobs.Job      the job, registered in serve.machine_sources (Sources.jobs);
                                 sources.plugin_copies (callable() -> [step], given by the host's
                                 entry point, tools/hub.py) gives each host's plugin-copy step
  PluginJob(copies=None, ...)    its run(ctx): one check, and the refresh when it is due

Every EVERY_S the job reads the kit's refs/remotes/<remote>/main and the export's state.json,
as pm_watch's check does (kitlib.pluginsource poll), and runs kitlib.pluginsource.refresh with
apply on this job's own thread when the export is behind that main, or a migrated account folder's
last refresh at it did not go through. It runs no refresh, and says why in its note, when there is
no tracking ref, no export yet, a folder not migrated, plugin.auto_refresh off, or a live process
holding refresh.pid (pm_watch's spawn, a refresh by hand). The refresh takes refresh.pid itself,
so two never run at once; a refresh that finds it taken is a note, not a failure. A refresh that
exits non-zero is a failure (jobs: its text in health, `failing` pushed at the third in a row),
and the same main is tried again RETRY_S after it failed, not sooner (the runs between raise the
same failure without running). Its lines are appended to <export root>/refresh.log, the file
pm_watch's spawn writes, under a header line. The copy steps run as tools/plugin_refresh.py runs
them, so the Codex plugin copy is refreshed too (a hub started from a session PM_KIT_ROLE marks
skips it, as plugin_refresh.py does: docs/plugin-source.md "Codex").
"""
import datetime
import os
import time

from kitlib import pluginsource
from kitlib.proc import process     # noqa: F401 - loaded now: refresh.pid reads it mid-run
from kitlib.hubsite import jobs

NAME = 'plugin-refresh'
EVERY_S = 300
RETRY_S = 1800
BUSY = 'a refresh is already running'


class RefreshFailed(Exception):
    """A refresh that exited non-zero: its exit code and its last refusal or failure line."""


def make(sources):
    return jobs.Job(NAME, EVERY_S, PluginJob(copies=getattr(sources, 'plugin_copies', None),
                                          home=getattr(sources, 'home', None)))


def _host():
    """The plugin's host module (account_folders, Runner), through kitlib.pluginsource's own seam,
    so this module names no host (tests/host test_leaks)."""
    return pluginsource._host()


def _kit():
    kit, why = pluginsource.default_kit(pluginsource.kit_root())
    if why:
        raise pluginsource.RefreshError(why)
    return kit


def _rc(state, folder):
    k = pluginsource.folder_key(folder)
    for f, r in (state.get('folders') or {}).items():
        if pluginsource.folder_key(f) == k and isinstance(r, dict):
            return r.get('rc')
    return None


class PluginJob(object):
    """run(ctx) is the job. Every part a test replaces is an argument: `kit` (default the
    checkout this kit runs from, or the one its export's marker names), `load(kit)` the plugin
    Settings, `folders()` the account folders, `refresh(kit, s, folders)` -> (exit code, lines),
    the refresh with apply (default kitlib.pluginsource.refresh with `runner`, default
    the plugin host's Runner(home), the in-use reader and the copy steps), `alive` refresh.pid's liveness
    test, `clock` epoch seconds, `in_use(root)` the in-use reader the prune reads (default
    kitlib.pluginsource.read_in_use; None: none, so the prune keeps its 7-day rule)."""

    def __init__(self, copies=None, home=None, kit=None, load=None, folders=None, refresh=None,
                 alive=None, clock=None, runner=None, in_use=pluginsource.read_in_use):
        self.copies = copies
        self.in_use = in_use
        self.home = home
        self.runner = runner            # None: the host's Runner(home), made at each refresh
        self.kit = kit
        self.load = load or (lambda kit: pluginsource.load(home, kit=kit))
        self.folders = folders or (lambda: _host().account_folders(home))
        self.refresh = refresh or self._refresh
        self.alive = alive
        self.clock = clock or time.time
        self._failed = None             # (main, at, text) of the last refresh that failed

    def __call__(self, ctx):
        kit = self.kit or _kit()
        s = self.load(kit)
        main, why = pluginsource.tracking_main(pluginsource.source_kit(kit), s.remote)
        if main is None:
            raise pluginsource.RefreshError('cannot read %s/%s: %s' % (s.remote, pluginsource.MAIN, why))
        if not main:
            return 'no %s/%s tracking ref: nothing to check' % (s.remote, pluginsource.MAIN)
        state = pluginsource.read_state(s.export_dir)       # StateError: a failure
        if state is None:
            return 'no export yet: %s' % pluginsource.refresh_cmd(True)
        folders = list(self.folders())
        todo = pluginsource.unmigrated(state, folders)
        if todo:
            return 'not migrated in %s: %s' % (', '.join(todo), pluginsource.refresh_cmd(True))
        behind = state.get('sha') != main
        failed = [f for f in folders if _rc(state, f) != 0]
        if not behind and not failed:
            return 'current at %s' % main[:12]
        what = ('behind kit main %s' % main[:12] if behind
                else 'not refreshed in %s' % ', '.join(failed))
        if not s.auto_refresh:
            return '%s; plugin.auto_refresh is off: %s' % (what, pluginsource.refresh_cmd())
        if pluginsource.lock_live(s.export_dir, self.alive):
            return '%s; %s' % (what, BUSY)
        last = self._failed
        if last is not None and last[0] == main and self.clock() - last[1] < RETRY_S:
            raise RefreshFailed(last[2])
        if ctx.stop.is_set():
            return what
        code, lines = self.refresh(kit, s, folders)
        if code != 0 and any(BUSY in line for line in lines):
            return '%s; %s' % (what, BUSY)
        if code != 0:
            said = [ln for ln in lines if ln.startswith('REFUSED') or 'FAILED' in ln or 'failed' in ln]
            text = 'the refresh to %s exited %s: %s' % (main[:12], code,
                                                       (said or lines or ['no output'])[-1])
            self._failed = (main, self.clock(), text)
            raise RefreshFailed(text)
        self._failed = None
        return 'refreshed to %s' % main[:12]

    def _steps(self, out):
        if self.copies is None:
            return []
        try:
            return list(self.copies())
        except Exception as e:          # noqa: BLE001 - never stop a refresh over a host's copy
            out('WARNING: no host plugin copy is refreshed: %s: %s' % (type(e).__name__, e))
            return []

    def _refresh(self, kit, s, folders):
        """(exit code, lines) of one kitlib.pluginsource.refresh with apply, each line appended
        to <export root>/refresh.log too."""
        lines = []
        log = _Log(s.export_dir)
        log.write('hub plugin job %s' % datetime.datetime.now().isoformat(timespec='seconds'))

        def out(line):
            lines.append(line)
            log.write(line)
        try:
            runner = self.runner or _host().Runner(self.home)
            reader = self.in_use
            in_use = None if reader is None else (lambda: reader(s.export_dir))
            code = pluginsource.refresh(kit, s, folders, out, apply=True, runner=runner,
                                        alive=self.alive, in_use=in_use,
                                        copies=self._steps(out))
        finally:
            log.close()
        return code, lines


class _Log(object):
    """refresh.log, appended to; a log that cannot be opened or written is dropped silently (the
    lines still reach health through the job)."""

    def __init__(self, root):
        try:
            os.makedirs(root, exist_ok=True)
            self.fh = open(os.path.join(root, pluginsource.LOGFILE), 'a', encoding='utf-8',
                           errors='replace', newline='\n')
        except OSError:
            self.fh = None

    def write(self, line):
        if self.fh is None:
            return
        try:
            self.fh.write(line + '\n')
            self.fh.flush()
        except OSError:
            self.fh = None

    def close(self):
        if self.fh is not None:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None
