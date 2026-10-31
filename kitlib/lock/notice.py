"""Lock-window notices: a run that makes a heavy or exclusive window on the machine writes a start
notice when it starts holding and an end notice when it ends, so the owner hears of the window
without anyone relaying it. Off unless the machine config's machine_lock.notice.dir is set
(kitlib.lock.site).

Which runs: those of the kinds machine_lock.notice.kinds names; unset, a kind whose cpu size
reaches the heavy size (agent_cap, "heavy" or a whole number that large) or whose gpu is not
shared, and any run inside an owner whole hold (an exclusive window). A nested run writes none
(its outer run is the window). Notices are per run, not per window: two heavy runs side by side
write two start notices and two end notices.

Each notice is <dir>/<run id>-start.json or -end.json, an Action Hub inbox doc
(docs/action-hub.md "Lock-window notices"): {id: "L-<run id>-start|end", title, text, at, repo, from,
kind: "lock-window", run, phase}. The hub server lists them (kitlib.hubsite.derived.notices) and the
PM watcher prints LOCKNOTICE for each new one (both through kitlib.pmwatch, which reads the folder
without importing the lock).
A notice that cannot be written is said and never fails the run. A start notice once written
stays: its end notice follows from the wrapper even when the run's record on disk cannot be
updated. A background run (it yields to others) writes none, and a wrapper killed outright writes
no end notice: its slots free when the next look finds it gone.

A run the lock itself asks to stop because it is stale (kitlib.lock.stale) gets a third notice,
<run id>-stale.json (phase "stale"), whenever the folder is set, whatever its kind: the owner hears
that the lock ended a run nobody had stopped. Its end notice follows only when it wrote a start one.
"""
import os

from kitlib.lock import kinds as kinds_mod
from kitlib.lock import layout as lay
from kitlib.lock import site
from kitlib.lock.store import iso, local_text, parse_iso, write_json

FROM = "machine lock"
KIND = "lock-window"


def target(store, kind, k, L, whole=False):
    """The notice folder for a run of `kind` (its kinds-table entry k, layout L), or None for no
    notice. Heavy is a size: a kind whose cpu size (layout.kind_cores) reaches the heavy alias's,
    agent_cap, whether the table says "heavy" or a measured whole number. A kind listed in the
    machine config's notice kinds matches under its retired name too (kinds.canonical)."""
    n = site.notice(store)
    if n is None:
        return None
    d, kinds = n
    if kinds is not None:
        al = kinds_mod.aliases(store.kinds_path)
        want = kinds_mod.canonical(kind, table_aliases=al)
        return d if any(kinds_mod.canonical(x, table_aliases=al) == want for x in kinds) else None
    heavy = lay.kind_cores(L, k) >= L["agent_cap"]
    return d if whole or heavy or (k.get("gpu") and not k.get("share")) else None


def _whole(rec):
    return rec.get("hold_scope") == "whole" and rec.get("hold_owner") == "owner"


def _what(rec):
    return "%s window" % ("Exclusive" if _whole(rec) else "Heavy")


def _held(rec):
    parts = []
    if rec.get("cores"):
        parts.append("%d cpu slot%s" % (len(rec["cores"]), "" if len(rec["cores"]) == 1 else "s"))
    if rec.get("gpus"):
        parts.append("the gpu" if not rec.get("gpu_share") else "a gpu training slot")
    return " and ".join(parts) or "its slots"


def start_doc(rec):
    end = None
    try:
        if rec.get("expect_s"):
            end = local_text(parse_iso(rec.get("held_since") or rec["since"]).timestamp()
                             + float(rec["expect_s"]))
    except (KeyError, TypeError, ValueError):
        end = None
    return {"id": "L-%s-start" % rec["id"], "phase": "start", "run": rec["id"],
            "title": "%s started: %s/%s (%s)" % (_what(rec), rec["repo"], rec["slug"], rec["kind"]),
            "text": "Run %s holds %s%s. Heavy work on the machine until its end notice."
                    % (rec["id"], _held(rec), ", expected to end about %s" % end if end else ""),
            "at": iso(), "repo": rec["repo"], "from": FROM, "kind": KIND}


def end_doc(rec):
    return {"id": "L-%s-end" % rec["id"], "phase": "end", "run": rec["id"],
            "title": "%s ended: %s/%s (%s)" % (_what(rec), rec["repo"], rec["slug"], rec["kind"]),
            "text": "Run %s ended (%s, exit %s); its slots are free."
                    % (rec["id"], rec.get("how") or "ended", rec.get("code")),
            "at": iso(), "repo": rec["repo"], "from": FROM, "kind": KIND}


def stale_doc(rec, why):
    return {"id": "L-%s-stale" % rec["id"], "phase": "stale", "run": rec["id"],
            "title": "Lock ended a stale run: %s/%s (%s)" % (rec["repo"], rec["slug"], rec["kind"]),
            "text": "Run %s: %s. The lock asked it to stop; its wrapper ends its process tree and "
                    "its slots free when it ends." % (rec["id"], why),
            "at": iso(), "repo": rec["repo"], "from": FROM, "kind": KIND}


def _path(d, doc):
    return os.path.join(d, "%s-%s.json" % (doc["run"], doc["phase"]))


def _write(store, d, doc):
    try:
        os.makedirs(d, exist_ok=True)
        write_json(_path(d, doc), doc)
        return True
    except (OSError, TypeError, ValueError) as e:
        store.say("WARNING: run %s: the lock-window notice could not be written to %s: %s"
                  % (doc.get("run"), d, e))
        return False


def started(store, run, kind, k, L):
    """After a run starts holding: its start notice when it makes a window. Never raises. A start
    notice written is never taken back: the run's record keeps its folder in memory, so the
    wrapper writes the end notice at settle even when the record on disk cannot be updated."""
    try:
        d = target(store, kind, k, L, _whole(run.rec))
        if not d:
            return
        if not _write(store, d, start_doc(run.rec)):
            return
        run.rec["notice"] = d           # the end notice goes where the start one went
        try:
            run.save()
        except Exception as e:
            store.say("WARNING: run %s: its record on disk does not name its start notice (%s); "
                      "this wrapper still writes the end notice" % (run.id, e))
    except Exception as e:              # a notice never fails a run
        store.say("WARNING: run %s: no lock-window notice: %s" % (run.id, e))


def ended(store, rec):
    """At settle: the end notice of a run that wrote a start notice. Never raises."""
    try:
        if rec and rec.get("notice"):
            _write(store, rec["notice"], end_doc(rec))
    except Exception as e:
        store.say("WARNING: run %s: no lock-window end notice: %s" % ((rec or {}).get("id"), e))


def stale(store, rec, why):
    """When the lock asked stale run rec to stop (kitlib.lock.stale.reap): its stale notice, when
    the notice folder is set (whatever the run's kind). Never raises."""
    try:
        n = site.notice(store)
        if n is not None:
            _write(store, n[0], stale_doc(rec, why))
    except Exception as e:
        store.say("WARNING: run %s: no stale-run notice: %s" % ((rec or {}).get("id"), e))
