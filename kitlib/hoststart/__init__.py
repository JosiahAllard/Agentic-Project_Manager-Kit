"""Read-only startup checks reusable by session-host adapters.

Reuse the PM/coordinator check functions, without invoking Claude process discovery,
automatic kit updates, a watcher, or any installation/scheduling operation.
"""
import datetime
import os

from kitlib import coordinatorstart, pmstart
from kitlib.config import user as kuser


def _rows(checks):
    rows = []
    for label, check in checks:
        try:
            rows.extend(check())
        except Exception as error:
            rows.append('could not check %s: %s: %s' % (label, type(error).__name__, error))
    return rows


def _config(loader, label, rows):
    try:
        cfg = loader()
        if cfg is None:
            rows.append('%s config not configured' % label)
        return cfg or {}
    except Exception as error:
        rows.append('could not check %s config: %s: %s' % (label, type(error).__name__, error))
        return {}


def _quiet(loader):
    """`loader()`, or {} when it fails or gives None: for a config read only to add an optional
    line (the ledger line needs `ledger_dir`), so a broken config adds no row."""
    try:
        return loader() or {}
    except Exception:
        return {}


def machine_lines(snapshot):
    """Summarize the shared lock API's snapshot, without inferring process liveness."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('revision'), int):
        raise ValueError('invalid shared lock snapshot')
    runs, holds, queue = snapshot.get('runs'), snapshot.get('holds'), snapshot.get('queue')
    if not isinstance(runs, list) or not isinstance(holds, list) or not isinstance(queue, dict):
        raise ValueError('incomplete shared lock snapshot')
    if snapshot.get('holds_error') or queue.get('error'):
        raise ValueError('shared lock holds/queue could not be read')
    entries = queue.get('entries')
    if not isinstance(entries, list):
        raise ValueError('shared lock queue is unavailable')
    return ['machine lock revision %s: %d runs, %d queued entries, %d holds; inspect the lock '
            'snapshot before assigning resources' % (snapshot['revision'], len(runs), len(entries), len(holds))]


def shared_checks(role, cwd, repo_name=None, state_note=None, now=None, pm_config=None,
                  coordinator_config=None, lock_reader=None, session_host=None, machine_config=None):
    """A tuple of check lines. Missing connections/configuration are reported, never guessed.
    `machine_config`: the machine config the coordinator's ledger line reads (default
    kitlib.config.user.user_config(), read only for a coordinator with a repo_name; one that
    cannot be read adds no row)."""
    if role not in {'pm', 'coordinator'}:
        return ()
    now = now or datetime.datetime.now().astimezone()
    rows, checks = [], []
    if role == 'pm':
        cfg = pm_config if pm_config is not None else _config(pmstart.load_config, 'PM', rows)
        checks += [('standing', lambda: pmstart.standing(cfg)),
                   ('eng_review', lambda: pmstart.eng_review(cfg))]
        if cfg.get('memory_dir'):
            checks.append(('handoff', lambda: pmstart.handoff(cfg)))
        if cfg.get('reminders_file'):
            checks.append(('reminders', lambda: pmstart.reminders(cfg, now.date())))
    else:
        cfg = coordinator_config if coordinator_config is not None else _config(coordinatorstart.load_config, 'coordinator', rows)
        checks.append(('standing', lambda: coordinatorstart.context(cfg, live=False).splitlines()))
        if repo_name:
            checks.append(('cap', lambda: coordinatorstart.cap(cfg, repo_name, now.date())))
            machine = machine_config if machine_config is not None else _quiet(kuser.user_config)
            checks.append(('ledger', lambda: coordinatorstart.ledger_lines(machine, repo_name)))
        if state_note:
            note = os.path.realpath(state_note)
            root = os.path.realpath(cwd)
            from kitlib.git import repo
            place = repo.classify(cwd)
            if place.common and os.path.basename(place.common).lower() == '.git':
                root = os.path.realpath(os.path.dirname(place.common))
            try:
                inside = os.path.commonpath((note, root)) == root
            except ValueError:
                inside = False
            if not os.path.isabs(state_note) or inside:
                rows.append('state note must be an absolute path outside the checkout')
    if lock_reader is not None:
        checks.append(('machine', lambda: machine_lines(lock_reader())))
    else:
        rows.append('machine: shared lock reader not connected')
    if session_host is not None:
        def inventory():
            value = session_host.sessions(cwd=cwd)
            if not isinstance(value, dict) or not isinstance(value.get('sessions'), list):
                raise ValueError('invalid session inventory')
            return ['session inventory: %d records, %s; loaded state is not process liveness' %
                    (len(value['sessions']), 'complete' if value.get('complete') else 'incomplete')]
        checks.append(('sessions', inventory))
    else:
        rows.append('sessions: host reader not connected; duplicate-coordinator check unavailable')
    return tuple(rows + _rows(checks))
