"""This machine's lock settings: the "machine_lock" object of the machine config
(kitlib.config.user). Every key is optional; an absent file or key is the defaults below.

  "daemons": "gates" (default), "stop" or "leave". At the release of a gate run (one booked with
      book(gate=True), as kitlib.gate.runner books every branch and merged gate) that ended
      "done", the Gradle and Kotlin daemons still in its job are stopped with their children,
      each only when every loopback client of every process in its tree is in the run's job
      (kitlib.proc.daemons stop); one that serves a client outside it, or whose clients cannot
      be told, is left running and the reason said. MSBuild nodes and the compiler server
      (named pipes, whose client cannot be told) are always left. Any other run leaves them all
      running, unpinned, for the next build. "stop" applies the same test after every run,
      "leave" stops none.
  "notice": {"dir": <absolute folder>, "kinds": [<kind>, ...]}. Set, a run that holds a heavy or
      exclusive window writes a start notice there when it starts and an end notice when it ends
      (kitlib.lock.notice). Unset: none (the default). "kinds" unset means every kind whose cpu
      size reaches the heavy size (agent_cap) or whose gpu is not shared, and any run inside an owner whole hold.

A scratch store (tests) reads no machine config: store.site, when a test sets it, is used instead.
A config that cannot be read, or a bad value, is said once and the defaults are used: a lock run
never fails on it.
"""
import os

KEY = "machine_lock"
DAEMON_POLICIES = ("gates", "stop", "leave")


def check(cfg):
    """The errors in a "machine_lock" object, as text lines (empty: valid)."""
    errs = []
    if not isinstance(cfg, dict):
        return ["%s must be an object" % KEY]
    for k in cfg:
        if k not in ("daemons", "notice"):
            errs.append("%s: unknown key %r" % (KEY, k))
    if "daemons" in cfg and cfg["daemons"] not in DAEMON_POLICIES:
        errs.append("%s.daemons must be one of %s" % (KEY, ", ".join(DAEMON_POLICIES)))
    n = cfg.get("notice")
    if n is not None:
        if not isinstance(n, dict):
            errs.append("%s.notice must be an object" % KEY)
        else:
            for k in n:
                if k not in ("dir", "kinds"):
                    errs.append("%s.notice: unknown key %r" % (KEY, k))
            d = n.get("dir")
            if d is not None and not (isinstance(d, str) and os.path.isabs(d)):
                errs.append("%s.notice.dir must be an absolute path" % KEY)
            ks = n.get("kinds")
            if ks is not None and not (isinstance(ks, list)
                                       and all(isinstance(x, str) for x in ks)):
                errs.append("%s.notice.kinds must be a list of kind names" % KEY)
    return errs


def load(store):
    """The checked "machine_lock" object for this store ({} for defaults); read once per store."""
    got = getattr(store, "site", None)
    if got is not None:
        return got
    cfg = {}
    if not store.testing:
        try:
            from kitlib.config import user
            cfg = user.user_config().get(KEY) or {}
        except Exception as e:          # a bad machine config never fails a lock run
            store.say("WARNING: machine config not read (%s); machine lock defaults used" % e)
            cfg = {}
        errs = check(cfg)
        if errs:
            store.say("WARNING: %s; machine lock defaults used" % "; ".join(errs))
            cfg = {}
    store.site = cfg
    return cfg


def daemon_policy(store):
    return load(store).get("daemons") or "gates"


def stops_daemons(store, gate):
    """Are a run's leftover build daemons stopped at its release? gate: it is a gate run."""
    policy = daemon_policy(store)
    return policy == "stop" or (policy == "gates" and bool(gate))


def notice(store):
    """(dir, kinds or None) when notices are on, else None."""
    n = load(store).get("notice") or {}
    return (n["dir"], n.get("kinds")) if n.get("dir") else None
