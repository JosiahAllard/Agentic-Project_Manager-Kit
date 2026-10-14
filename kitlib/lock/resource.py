"""Exclusive resources: one holder at a time per named resource, the callers that want it served
first come, first served. No kit code calls it now: a landing books its turn in the slot queue
with kitlib.lock.run.book(resource=..., priority="land") (docs/lock-api.md "Exclusive resources").

resources.json is {"ticket": the last ticket given, "entries": [entry]}; an entry is
{id, ticket, resource, label, state: waiting | held, pid, ctime, since, held_at}. It is read and
written only under the store's "resources" mutex, apart from queue.json: the slot queue's writers
rewrite queue.json whole and keep only the entries they know.

An entry stands while its caller holds the run mutex of its id (store.run_mutex), as a run's queue
entry does: a caller that dies leaves an entry the next look drops (store.run_dead). The holder
is the entry with the lowest ticket for its resource; a later caller never goes ahead of an
earlier one, and nothing takes a resource from its holder.
"""
import json
import os
import time

from kitlib.lock.store import iso, new_id, write_json
from kitlib.proc import process

FILE = "resources.json"
MUTEX = "resources"
STATES = ("waiting", "held")


class ResourceError(OSError):
    """The resource queue could not be read, written or joined; nothing was taken."""


class ResourceUnreadable(ResourceError):
    """resources.json exists but could not be opened or parsed: the queue is not known."""


def load(store):
    """{'ticket', 'entries'}; entries in any other shape dropped. No file is an empty queue. A
    file that exists but cannot be read is read again for settings.queue_read_s, then
    ResourceUnreadable: never taken for an empty queue, which the next save would write over."""
    path = store.path(FILE)
    end = time.monotonic() + store.settings.queue_read_s
    while True:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or not isinstance(d.get("entries", []), list):
                raise ValueError("not a resource queue")
            break
        except FileNotFoundError:
            d = {}
            break
        except (OSError, ValueError) as err:
            if time.monotonic() >= end:
                raise ResourceUnreadable("resource queue unreadable: %s could not be read (%s); "
                                         "nothing is taken from it or written over it"
                                         % (path, err)) from err
            time.sleep(0.1)
    out = [e for e in d.get("entries") or []
           if isinstance(e, dict) and isinstance(e.get("id"), str)
           and isinstance(e.get("resource"), str) and type(e.get("ticket")) is int
           and e.get("state") in STATES]
    tickets = [t for t in [d.get("ticket")] + [e["ticket"] for e in out] if type(t) is int]
    return {"ticket": max(tickets or [0]), "entries": out}


def save(store, d):
    write_json(store.path(FILE), d, store.settings.queue_write_s)
    store.bump()


def _locked(store):
    return store.lock(MUTEX, store.settings.queue_lock_s)


def _prune(store, d):
    """Drops dead callers' entries from d in place; True when any went."""
    n = len(d["entries"])
    d["entries"] = [e for e in d["entries"] if not store.run_dead(e)]
    return len(d["entries"]) != n


def entries(store, resource=None):
    """The live entries (of one resource, or all), lowest ticket first. Read only: nothing is
    written. Raises ResourceUnreadable."""
    d = load(store)
    out = [e for e in d["entries"] if resource is None or e["resource"] == resource]
    return sorted((e for e in out if not store.run_dead(e)), key=lambda e: e["ticket"])


def text(e):
    """'<label> (pid N, held since T)' or '... waiting since T'."""
    if e.get("state") == "held":
        return "%s (pid %s, held since %s)" % (e.get("label") or e["id"], e.get("pid"),
                                                e.get("held_at") or e.get("since"))
    return "%s (pid %s, waiting since %s)" % (e.get("label") or e["id"], e.get("pid"),
                                               e.get("since"))


class Turn:
    """This caller's place in the queue for one resource, from join() until leave(). A Turn is
    used by one thread; its entry lives as long as the Turn's run mutex."""

    def __init__(self, store, resource, label):
        self.store, self.resource, self.label = store, resource, label
        self.id = new_id("l")
        self.ticket = None
        self.held = False
        self.mutex = None
        try:
            self.mutex = store.run_mutex(self.id)
            with _locked(store):
                d = load(store)
                _prune(store, d)
                d["ticket"] += 1
                self.ticket = d["ticket"]
                d["entries"].append({"id": self.id, "ticket": self.ticket, "resource": resource,
                                     "label": label, "state": "waiting", "pid": os.getpid(),
                                     "ctime": process.own_ctime(), "since": iso(),
                                     "held_at": None})
                save(store, d)
        except ResourceError:
            self._drop_mutex()
            raise
        except OSError as e:
            self._drop_mutex()
            raise ResourceError("cannot join the queue for %s: %s" % (resource, e)) from e

    def _drop_mutex(self):
        if self.mutex is not None:
            self.mutex.release()
            self.mutex = None

    def try_take(self):
        """[] when this caller now holds the resource; else the live entries ahead of it, the
        holder first. Raises ResourceError (the caller then holds nothing)."""
        if self.mutex is None:
            raise ResourceError("this turn for %s has already left the queue" % self.resource)
        try:
            with _locked(self.store):
                d = load(self.store)
                changed = _prune(self.store, d)
                mine = next((e for e in d["entries"] if e["id"] == self.id), None)
                if mine is None:
                    if changed:
                        save(self.store, d)
                    raise ResourceError("this caller's place in the queue for %s is gone from "
                                        "%s" % (self.resource, self.store.path(FILE)))
                ahead = sorted((e for e in d["entries"] if e["resource"] == self.resource
                                and e["ticket"] < mine["ticket"]), key=lambda e: e["ticket"])
                if not ahead and mine["state"] != "held":
                    mine.update(state="held", held_at=iso())
                    changed = True
                if changed:
                    save(self.store, d)
        except ResourceError:
            raise
        except OSError as e:
            raise ResourceError("cannot read the queue for %s: %s" % (self.resource, e)) from e
        self.held = not ahead
        return ahead

    def wait(self, timeout_s, poll_s=None, on_wait=None):
        """try_take until it takes or timeout_s passes: [] taken, else the entries still ahead.
        timeout_s runs from the first look that found entries ahead, so a first look slowed by a
        loaded machine never uses it up. on_wait(ahead) is called once, at that first look, when
        timeout_s lets it wait."""
        poll_s = self.store.settings.queue_poll_s if poll_s is None else poll_s
        end = None
        while True:
            ahead = self.try_take()
            if not ahead:
                return ahead
            now = time.monotonic()
            if end is None:
                end = now + max(0.0, timeout_s)
                if timeout_s > 0 and on_wait is not None:
                    on_wait(ahead)
            left = end - now
            if left <= 0:
                return ahead
            time.sleep(min(poll_s, left))

    def leave(self):
        """Gives up the resource or the place in line. Never raises: an entry that cannot be
        removed now goes at the next look, once the run mutex is released here."""
        if self.mutex is None:
            return
        try:
            with _locked(self.store):
                d = load(self.store)
                n = len(d["entries"])
                d["entries"] = [e for e in d["entries"] if e["id"] != self.id]
                if len(d["entries"]) != n:
                    save(self.store, d)
        except OSError:
            pass
        finally:
            self.held = False
            self._drop_mutex()

    release = leave

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.leave()


def join(store, resource, label):
    """A new Turn at the back of the queue for resource. Raises ResourceError."""
    return Turn(store, resource, label)
