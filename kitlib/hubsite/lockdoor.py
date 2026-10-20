"""kitlib.hubsite.lockdoor: the hub server's door to the machine lock, which it calls in-process
through kitlib.lock.api, by the owner's decision. The Api's lock routes reach the lock only
through a door (kitlib.hubsite.api, "Machine lock"); kitlib.hubsite.serve gives the machine's
server this one.

    LockDoor(make_store=None, actor=None)
      .revision()                    the lock store's revision (lock api.revision)
      .view(remote=False)            {lock_revision, view, writer}: the schedule window's view of
                                     one snapshot and the history it draws, as kitlib.lockui.model
                                     to_json gives it (remote: the tunnel's fixed phrases), and
                                     who the lock takes this hub for (writer())
      .stats(days, since, until)     lock api.stats; a bad time or day count is HubError invalid
      .writer()                      'owner' when the lock takes this hub's process for the owner
                                     (lock api is_owner of who), else 'agent'; one that cannot be
                                     told is 'agent'

The writes, each one lock api call, each the result's allow-listed fields (RESULT_KEYS) or a
HubError; remote is true for the tunnel listener's calls:
      .reserve(scope, start, end, note, remote)        a whole or half owner hold over [start,
                                                       end) (ISO times with their offset), as the
                                                       page's drag gives it (actions.reserve_spec)
      .hold(scope, start, end, note, cpu, gpu, remote) an owner hold from the Hold dialog's text
                                                       (actions.hold_spec)
      .edit(hold_id, start, end, note, remote)         the Edit dialog (actions.edit_changes); a
                                                       note of None keeps the hold's own
      .end_hold(hold_id, remote)
      .end_run(run_id, remote)
      .move_to_top(run_id, remote)
      .game_mode(enabled, remote)
      .games(text, remote)                             the Games dialog's text (games_from_text)
      .division(fields, remote)                        the Division dialog's fields
                                                       (division_from_fields); None: every default

Typed times are read in this PC's local zone (kitlib.lockui.actions.parse_local), whatever zone
the browser is in. The page makes owner holds only: every spec the parsers give is an owner one.
Who writes: the lock decides (lock api who): on the machine's store the hub's own process tree,
so a hub started under an agent session is an agent and every write is refused; the actor's name
is LOCAL_ACTOR, or REMOTE_ACTOR for the tunnel's calls. `actor` is the base actor (a scratch
store in a test takes it as given; the machine's keeps only the name). A write the lock would
refuse because this hub is no owner is refused here first, `forbidden` with WHY_AGENT.

The writes run one at a time under the door's write mutex, so an Edit reads the hold and writes
its change with no other hub write between. The lock's refusals become HubErrors: invalid (400,
the lock's field), forbidden (403, field lock), not_found (404, field id), conflict and busy
(409: the lock is busy, try again), store (`unavailable`, 503, field lock). A parser's ValueError
is invalid. For a remote caller a store message, and any message or note that names a folder, is
a fixed phrase (REMOTE_SAYS).

The lock store is made on the first call, not when the door is made, so a server that never
shows the lock never opens the machine's lock folder. make_store() gives it (default: the
machine's kitlib.lock.store.Store).
"""
import datetime
import re
import threading

from kitlib.lock import api as lock_api
from kitlib.lockui import actions
from kitlib.lockui import model

from .store import HubError

LOCAL_ACTOR = 'hub'
REMOTE_ACTOR = 'hub-tunnel'
WHY_AGENT = ('the machine lock takes this hub for an agent (it was started under an agent '
             'session): restart the hub from your own console or its scheduled task to change '
             'the lock from the page')
REMOTE_SAYS = {
    'store': 'the machine lock could not be written; the schedule window on this machine says '
             'why',
    'path': 'the machine lock refused the change; the schedule window on this machine says why'}
# a drive or UNC folder in a message: never sent over the tunnel
_FOLDER = re.compile(r'[A-Za-z]:[\\/]|\\\\')
# the lock's refusal code -> (the hub's code, the field when the lock names none)
CODES = {'invalid': ('invalid', 'lock'), 'forbidden': ('forbidden', 'lock'),
         'not_found': ('not_found', 'id'), 'conflict': ('conflict', 'lock'),
         'busy': ('conflict', 'lock'), 'store': ('unavailable', 'lock')}
HOLD_KEYS = ('id', 'owner', 'scope', 'start', 'end', 'note', 'by')
RESULT_KEYS = {'end_run': ('id', 'requested'), 'move_to_top': ('id', 'position'),
               'game_mode': ('enabled',), 'division': ('applied', 'reason')}


def _machine_store():
    from kitlib.lock.store import Store
    return Store()


def _pick(d, keys):
    d = d if isinstance(d, dict) else {}
    return {k: d.get(k) for k in keys}


def _said(text, remote):
    text = text if isinstance(text, str) else ''
    return REMOTE_SAYS['path'] if remote and _FOLDER.search(text) else text


def _aware(text, field):
    t = None
    if isinstance(text, str):
        try:
            t = datetime.datetime.fromisoformat(text.strip().replace('Z', '+00:00'))
        except ValueError:
            t = None
    if t is None or t.tzinfo is None:
        raise HubError('invalid', '%s is an ISO time with its offset, not %r' % (field, text),
                       field)
    return t


def _parsed(fn, field, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise HubError('invalid', str(e), field)


class LockDoor(object):
    def __init__(self, make_store=None, actor=None):
        self._make = make_store or _machine_store
        self._mutex = threading.Lock()
        self._write_mutex = threading.Lock()
        self._actor = dict(actor or {})
        self._store = None

    def _lock_store(self):
        with self._mutex:
            if self._store is None:
                self._store = self._make()
            return self._store

    def _who(self, remote):
        return dict(self._actor, name=REMOTE_ACTOR if remote else LOCAL_ACTOR)

    # -------------------------------------------------------------- reads
    def revision(self):
        return lock_api.revision(self._lock_store())

    def writer(self):
        store = self._lock_store()
        try:
            return 'owner' if lock_api.is_owner(lock_api.who(self._who(False), store)) else 'agent'
        except Exception:           # noqa: BLE001 - who cannot be told: no owner (fail closed)
            return 'agent'

    def view(self, remote=False):
        store = self._lock_store()
        snap = lock_api.snapshot(store)
        rows = lock_api.history(store, days=model.BACK_DAYS + 1)
        return {'lock_revision': snap.get('revision'),
                'view': model.to_json(model.build(snap, rows), snap, remote=remote),
                'writer': self.writer()}

    def stats(self, days=None, since=None, until=None):
        try:
            return lock_api.stats(self._lock_store(), days=7 if days is None else days,
                                  since=since or None, until=until or None)
        except lock_api.ApiError as e:
            raise HubError('invalid', e.message, e.field or '')
        except OverflowError:
            raise HubError('invalid', 'days is out of range', 'days')

    # -------------------------------------------------------------- writes
    def _write(self, remote, call):
        """call(store, actor) -> the lock api's write answer, under the write mutex; its result,
        or the refusal as a HubError."""
        store = self._lock_store()
        with self._write_mutex:
            if self.writer() != 'owner':
                raise HubError('forbidden', WHY_AGENT, 'lock')
            got = call(store, self._who(remote))
        if not isinstance(got, dict):
            raise HubError('unavailable', 'the machine lock gave no answer', 'lock')
        if got.get('ok') is True:
            return got.get('result') if isinstance(got.get('result'), dict) else {}
        raise self._refusal(got.get('error'), remote)

    @staticmethod
    def _refusal(error, remote):
        e = error if isinstance(error, dict) else {}
        code, field = CODES.get(e.get('code'), ('unavailable', 'lock'))
        message = e.get('message') if isinstance(e.get('message'), str) else ''
        if e.get('code') == 'busy':
            message = 'the machine lock is busy, try again: %s' % message
        if e.get('code') == 'store' and remote:
            message = REMOTE_SAYS['store']
        message = 'the machine lock refused: %s' % _said(message, remote)
        if code == 'invalid' and isinstance(e.get('field'), str) and e.get('field'):
            field = e['field']
        return HubError(code, message, field)

    def _hold_result(self, result, remote):
        out = {'hold': _pick(result.get('hold'), HOLD_KEYS)}
        if 'notes' in result:
            out['notes'] = [_said(n, remote) for n in result.get('notes') or []
                            if isinstance(n, str)]
        return out

    def _create(self, spec, remote):
        got = self._write(remote, lambda store, me: lock_api.hold_create(me, spec, store=store))
        return self._hold_result(got, remote)

    def reserve(self, scope, start, end, note, remote=False):
        s, e = _aware(start, 'start'), _aware(end, 'end')
        now = datetime.datetime.now(datetime.timezone.utc)
        spec = _parsed(actions.reserve_spec, 'scope', scope, s, e, note or '', now)
        return self._create(spec, remote)

    def hold(self, scope, start, end, note, cpu, gpu, remote=False):
        ref = datetime.datetime.now().astimezone()
        spec = _parsed(actions.hold_spec, 'hold', scope, start or '', end or '', note or '',
                       cpu or '', gpu or '', ref)
        return self._create(spec, remote)

    def edit(self, hold_id, start, end, note, remote=False):
        def call(store_, me):
            rec = lock_api.hold_record(hold_id, store=store_)
            if isinstance(rec, dict) and rec.get('ok') is False:
                return rec
            if not (isinstance(rec, dict) and rec.get('live_record')):
                return {'ok': False, 'error': {'code': 'not_found', 'field': 'id',
                                               'message': 'no live hold %s' % hold_id}}
            ref = datetime.datetime.now().astimezone()
            keep = rec.get('note') or ''
            try:
                changes = actions.edit_changes(rec, start or '', end or '',
                                               keep if note is None else note, ref)
            except ValueError as e:
                return {'ok': False, 'error': {'code': 'invalid', 'message': str(e),
                                               'field': 'edit'}}
            if not changes:
                return {'ok': False, 'error': {'code': 'invalid', 'field': 'edit',
                                               'message': 'nothing to change'}}
            return lock_api.hold_edit(me, hold_id, changes, store=store_)
        return self._hold_result(self._write(remote, call), remote)

    def end_hold(self, hold_id, remote=False):
        got = self._write(remote, lambda store, me: lock_api.hold_end(me, hold_id, store=store))
        return self._hold_result(got, remote)

    def end_run(self, run_id, remote=False):
        got = self._write(remote, lambda store, me: lock_api.end_run(me, run_id, store=store))
        return _pick(got, RESULT_KEYS['end_run'])

    def move_to_top(self, run_id, remote=False):
        got = self._write(remote, lambda store, me: lock_api.move_to_top(me, run_id, store=store))
        return _pick(got, RESULT_KEYS['move_to_top'])

    def game_mode(self, enabled, remote=False):
        if type(enabled) is not bool:
            raise HubError('invalid', 'enabled must be true or false', 'enabled')
        got = self._write(remote, lambda store, me: lock_api.set_game_mode(me, enabled,
                                                                           store=store))
        return _pick(got, RESULT_KEYS['game_mode'])

    def games(self, text, remote=False):
        rows = _parsed(actions.games_from_text, 'text', text if isinstance(text, str) else '')
        got = self._write(remote, lambda store, me: lock_api.set_games(me, rows, store=store))
        return {'games': [_pick(g, ('exe', 'name', 'pin')) for g in got.get('games') or []
                          if isinstance(g, dict)]}

    def division(self, fields, remote=False):
        division = None
        if fields is not None:
            if not isinstance(fields, dict) or not all(isinstance(v, str)
                                                       for v in fields.values()):
                raise HubError('invalid', 'fields is an object of division keys and their '
                                          'text', 'fields')
            division = _parsed(actions.division_from_fields, 'fields', fields)
        got = self._write(remote, lambda store, me: lock_api.set_division(me, division,
                                                                          store=store))
        out = _pick(got, RESULT_KEYS['division'])
        out['reason'] = _said(out.get('reason'), remote) if out.get('reason') else out['reason']
        return out
