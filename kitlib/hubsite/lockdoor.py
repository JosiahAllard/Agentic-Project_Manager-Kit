"""kitlib.hubsite.lockdoor: the hub server's door to the machine lock, which it calls in-process
through kitlib.lock.api (R-2295). The Api's GET lock/view, lock/stats and lock/wait read the lock
only through a door (kitlib.hubsite.api, "Machine lock"); kitlib.hubsite.serve gives the machine's
server this one.

    LockDoor(make_store=None)
      .revision()                    the lock store's revision (lock api.revision)
      .view(remote=False)            {lock_revision, view}: the schedule window's view of one
                                     snapshot and the history it draws, as kitlib.lockui.model
                                     to_json gives it (remote: the tunnel's fixed phrases)
      .stats(days, since, until)     lock api.stats; a bad time or day count is HubError invalid

The lock store is made on the first read, not when the door is made, so a server that never
shows the lock never opens the machine's lock folder. make_store() gives it (default: the
machine's kitlib.lock.store.Store). Only reads: no write of the lock goes through this door.
"""
import threading

from kitlib.lock import api as lock_api
from kitlib.lockui import model

from .store import HubError


def _machine_store():
    from kitlib.lock.store import Store
    return Store()


class LockDoor(object):
    def __init__(self, make_store=None):
        self._make = make_store or _machine_store
        self._mutex = threading.Lock()
        self._store = None

    def _lock_store(self):
        with self._mutex:
            if self._store is None:
                self._store = self._make()
            return self._store

    def revision(self):
        return lock_api.revision(self._lock_store())

    def view(self, remote=False):
        store = self._lock_store()
        snap = lock_api.snapshot(store)
        rows = lock_api.history(store, days=model.BACK_DAYS + 1)
        return {'lock_revision': snap.get('revision'),
                'view': model.to_json(model.build(snap, rows), snap, remote=remote)}

    def stats(self, days=None, since=None, until=None):
        try:
            return lock_api.stats(self._lock_store(), days=7 if days is None else days,
                                  since=since or None, until=until or None)
        except lock_api.ApiError as e:
            raise HubError('invalid', e.message, e.field or '')
        except OverflowError:
            raise HubError('invalid', 'days is out of range', 'days')
