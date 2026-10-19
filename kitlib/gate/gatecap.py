"""The session gate cap (docs/gate.md "The session gate cap"): before a branch gate books its
machine-lock run, it waits while its agent session already has the configured number of gate runs
of the same repository in flight (booked, waiting or held).

A run is the session's when its recorded ancestors hold the session's nearest agent host
(kitlib.lock.store.owns): a coordinator and its in-process subagents share one host. The owner's
own runs (no agent host above the caller) are never counted and never wait. A landing's runs
(priority "land") are not counted; a landing and the daily full run never call wait_turn
(kitlib.gate.runner `_gate_checkout` is the one caller).

The cap is the machine config's "coordinator_start".gate_cap {repo name: int}, read under the
older "conductor_start" too. Unset, 0 or null: no cap. A value the cap cannot use, or a machine
config that cannot be read, is said once as a WARNING and nothing is capped.
"""
import time

from kitlib.config import user as kuser
from kitlib.config.types import ConfigError
from kitlib.lock import store as lockstore

KEY = 'coordinator_start'
LEGACY_KEY = 'conductor_start'
CAP_KEY = 'gate_cap'
POLL_S = 5.0        # how often a waiting gate counts again; never below the lock's queue_poll_s


def configured(repo, home=None):
    """(cap or None, warning or None) for repo from the machine config. None: no cap."""
    try:
        data = kuser.user_config(home)
    except ConfigError as e:
        return None, 'WARNING: the session gate cap is not applied: %s' % e
    section = data.get(KEY)
    if not isinstance(section, dict):
        section = data.get(LEGACY_KEY)
    if not isinstance(section, dict):
        return None, None
    caps = section.get(CAP_KEY)
    if caps is None:
        return None, None
    if not isinstance(caps, dict):
        return None, ('WARNING: the session gate cap is not applied: %s.%s is not an object of '
                      'repo name to count' % (KEY, CAP_KEY))
    value = caps.get(repo)
    if value is None:
        value = caps.get(str(repo).lower())
    if value is None or value == 0:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, ('WARNING: the session gate cap is not applied: %s.%s[%s] is %r, not a '
                      'whole number 0 or more' % (KEY, CAP_KEY, repo, value))
    return value, None


def actor():
    """Who is asking (kitlib.lock.store.caller): read from this process's tree."""
    return lockstore.caller()


def _gate_runs(runs, repo):
    """The live gate runs of repo that a cap counts: not a landing's."""
    want = str(repo).lower()
    return [r for r in runs if r.get('gate') and r.get('priority') != 'land'
            and str(r.get('repo') or '').lower() == want]


def in_flight(store, who, repo, runs=None):
    """The session's live gate runs of repo (records), oldest first. [] for the owner (no agent
    host). A run this process is inside (its wrapper is among the caller's ancestors) is not
    counted: a gate nested in it never waits for itself."""
    if not lockstore.actor_host(who):
        return []
    runs = _gate_runs(store.live_runs() if runs is None else runs, repo)
    inside = [list(a) for a in who.get('ancestors') or []]
    return [r for r in runs if lockstore.owns(who, r)
            and [r.get('pid'), r.get('ctime')] not in inside]


def wait_turn(store, repo, say, cap=None, who=None, home=None, poll_s=None, sleep=None):
    """Returns once the session has fewer than the cap gate runs of repo in flight. Waiting, it
    says one line naming the runs in flight and nothing per poll. The cap and the caller are read
    only when some gate run of repo is live, so a gate with none in flight reads neither."""
    if not _gate_runs(store.live_runs(), repo):
        return
    who = actor() if who is None else who
    mine = in_flight(store, who, repo)
    if not mine:
        return
    if cap is None:
        cap, warning = configured(repo, home)
        if warning:
            say(warning)
    if not cap or len(mine) < cap:
        return
    say('the gate waits for the session gate cap: %d of %d gate runs of this session in %s are '
        'in flight (%s); it books when one ends' % (
            len(mine), cap, repo, ', '.join(str(r.get('id')) for r in mine)))
    interval = max(poll_s or POLL_S, store.settings.queue_poll_s)
    sleep = sleep or time.sleep
    while True:
        sleep(interval)
        if len(in_flight(store, who, repo)) < cap:
            return
