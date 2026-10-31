"""kitlib.hubjobs.session_watch: the hub server's session watch job. It runs pm_watch's checks
(kitlib.pmwatch Watcher, docs/pm-watch.md) on the code the hub loaded and pushes their events to
the PM as job pushes (docs/action-hub.md "Timed jobs").

  make(sources) -> jobs.Job      the job, or None when the default host has no watcher readers
  SessionWatch(host, ...)        its run(ctx): one Watcher poll, and the push of its events

Checks: BGFAIL, LOCKNOTICE, USAGE and RESET, IDLE and STOPPED, REMOTE, LIMIT, and the Watcher's
own ERROR (a check that failed three times running). Left to pm_watch: PLUGIN (the plugin-refresh job
owns the refresh), HUB (the hub would ask its own health) and BRANCH (its refs are pm_watch
--branch arguments, in no config key). Config is the machine config's "pm_watch" keys, as
pm_watch reads them: sessions_file (unset: every run is a Skip), alerts_dir, state_file.

State: the job's own file, <pm_watch state_file>.hub.json, never pm_watch's, loaded at the first
run and replaced whole after every poll. A fresh one records Watcher.start's baseline, so the
first run is silent (REMOTE excepted, as in pm_watch); a hub restart reads it back, so nothing is
reported twice. An unreadable one is a fresh baseline.

Push: at most one per run, through ctx.event: one event gives its kind as the word (idle,
stopped, usage, reset, bgfail, locknotice, remote, limit, error) and its subject (session, account, run
or notice id, the failed check) as the detail; several give `events` with their count. The event
lines themselves are free text, so they never go in a push: the run's note, health's
jobs.session-watch.note, lists the last RECENT_MAX pushed ones with their times, so a push the
worker replaced before it was written hides nothing.

pm_watch: while its heartbeat (kitlib.pmwatch heartbeat_file beside its state_file) is at most
LIVE_S old, pm_watch tells the PM itself: the job still polls and saves its state, so it is
current when it takes over, but pushes nothing and returns a Skip. Events the job saw in the last
LIVE_S before a pm_watch stopped are dropped, not pushed late (a duplicate is worse; the push
worker's `watcher down` covers that moment). A pm_watch run with its own --state is not seen.
A beat with "yielded": true is a pm_watch that stepped aside for this job (kitlib.pmwatch
hub_session_job_up): it is not waited for, so the job pushes; a beat without the field (an older
pm_watch) is waited for as before. So the two never both go silent.

Failures: a check that raises is the Watcher's (ERROR on its third in a row); what escapes (the
config, the Watcher's build or start, the state write) is the job's failure (kitlib.hubsite.jobs:
`failing` at the third). The state is written after the push, so a write that fails never
swallows the run's events.
"""
import collections
import importlib
import time

from kitlib import pmwatch as W
from kitlib.hubsite import jobs

NAME = 'session-watch'
EVERY_S = 60                        # pm_watch's POLL_SLEEP; the Watcher's own timers pace each check
LIVE_S = 300                        # a pm_watch heartbeat this fresh is live (push.WATCHER_STALE_S)
RECENT_MAX = 20                     # pushed event lines kept in the note
STATE_SUFFIX = '.hub.json'
HOST_MODULE = '%s_host'             # the watcher readers' module in the adapter package, by host name
_SUBJECT_SKIP = ('account', 'run')


def host_module():
    """The default host's watcher readers (newest_finder, rc_records, default_alerts_dir, PRELOAD):
    <its adapter package>.<its name>_host, where tools/pm_watch.py imports them. A host without
    one gives ImportError. kitlib never imports a host statically: the names come from the
    registry, as kitlib/machinesetup/extras.py managed_path imports the adapter."""
    from kitlib.host import registry
    name = registry.default()
    return importlib.import_module(registry.host(name).adapter + '.' + HOST_MODULE % name)


def make(sources):
    try:
        host = host_module()
    except (ImportError, AttributeError, OSError, ValueError):     # registry.RegistryError
        return None
    return jobs.Job(NAME, EVERY_S, SessionWatch(host))


def push_of(events):
    """(word, detail) of one run's push: module docstring "Push"."""
    if len(events) != 1:
        return 'events', str(len(events))
    parts = events[0].split()
    rest = [p for p in parts[1:3] if p not in _SUBJECT_SKIP]
    return parts[0].lower(), (rest[0].rstrip(':') if rest else None)


class SessionWatch(object):
    """run(ctx) is the job. Every part a test replaces is an argument: `host` the watcher readers
    (host_module), `config()` the "pm_watch" dict (default kitlib.pmwatch.load_config),
    `notice_dir()` the lock notice folder (default configured_notice_dir), `usage` the usage
    reader (default kitlib.pmwatch.usage when the config has a launcher key, else none), `rc` the
    REMOTE check (None: rc_check over the host's records; False: none), `clock` epoch seconds."""

    def __init__(self, host, config=None, notice_dir=None, usage=None, rc=None, clock=None):
        self.host = host
        self.config = config or W.load_config
        self.notice_dir = notice_dir or W.configured_notice_dir
        self.usage = usage
        self.rc = rc
        self.clock = clock or time.time
        self.watcher = None
        self.path = None                # the job's state file
        self.started = False
        self.recent = collections.deque(maxlen=RECENT_MAX)

    def __call__(self, ctx):
        cfg = self.config()
        sessions = cfg.get('sessions_file')
        if not sessions:
            return jobs.Skip('pm_watch.sessions_file is not set: nothing to watch')
        theirs = cfg.get('state_file') or W.default_state_file()
        if self.watcher is None:
            self.path = theirs + STATE_SUFFIX
            self.watcher = self._build(cfg, sessions, self.path)
        now = self.clock()
        if not self.started:
            self.watcher.start(now)
            self.started = True
        events = self.watcher.poll(now)
        hb = W.read_heartbeat(W.heartbeat_file(theirs))
        if hb is not None and hb.get('yielded') is not True and now - hb['beat'] <= LIVE_S:
            W.save_state(self.path, self.watcher.s)
            return jobs.Skip('pm_watch is live (pid %s): %d event(s) left to it'
                             % (hb['pid'], len(events)))
        if events:
            stamp = time.strftime('%H:%M:%S', time.localtime(now))
            self.recent.extend('%s %s' % (stamp, e) for e in events)
            ctx.event(*push_of(events))
        W.save_state(self.path, self.watcher.s)
        if not self.recent:
            return 'no events yet'
        return 'recent events, newest last: %s' % ' | '.join(self.recent)

    def _build(self, cfg, sessions, path):
        state = W.load_state(path)
        usage = self.usage
        if usage is None:
            usage = W.usage if W.launcher_configured() else (lambda: {})
        rc = self.rc
        if rc is None:
            rc = W.rc_check(sessions, self.host.rc_records)
        return W.Watcher(state if isinstance(state, dict) else {}, [], usage,
                         cfg.get('alerts_dir') or self.host.default_alerts_dir(), sessions,
                         self.host.newest_finder(), notice_dir=self.notice_dir(),
                         rc=rc or None, preload_modules=self.host.PRELOAD)
