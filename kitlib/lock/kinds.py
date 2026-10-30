"""The kinds table: what each run kind asks the lock for, from kitlib/lock/kinds.json as
kitlib.config.policy.kinds_table reads it; this module checks each entry for the lock.

An entry: {"cpu": N | "medium" | "heavy", "gpu": 0 | "pool", "share": bool, "warn_min": int}.
cpu N is a whole number of cpu slots, 1..the layout's agent_cap, set only by
measurement (gate.py --measure) and a reviewed commit; a run may ask for fewer
(--cores), never more. medium and heavy stay as aliases, the sizes the table had before whole-core sizes:
medium takes the division's medium_cores; heavy takes the half cap (every core inside an owner
whole hold). gpu "pool" takes the whole gpu pool, or one gpu slot with --gpu-share training when
share is true.

Repository kinds: a repository may name its own kinds in its `.kit.json` "kinds" section, each
entry this shape plus a `why`. with_repo adds one to the table for a run from one of its checkouts,
only for a name that is neither a kit kind nor a kit alias: the kit wins a clash, and the repo
entry is ignored. It is read from the main checkout's `.kit.json` (where agents do not work),
never a linked worktree's, so an agent's uncommitted worktree edit cannot size a run.
"""
from kitlib.config import ConfigError, load as load_config, policy
from kitlib.git import repo as gitrepo

# One file, one reader: kitlib.config.policy owns both.
KINDS_FILE = policy.KINDS_FILE


class KindsError(ValueError):
    """The kinds table is missing or malformed (fail closed: no run without it)."""


def _read(path):
    """The table as kitlib.config.policy.kinds_table reads it ({"kinds": {...}} or a flat object;
    `_x` and `comment` keys are notes); its ConfigError becomes a KindsError."""
    try:
        return policy.kinds_table(path)
    except ConfigError as e:
        raise KindsError(str(e))


ALIASES = ("medium", "heavy")


def resolve(kind, table, path=None):
    """The canonical kind name for `kind`: itself when `table` has it, else what the `aliases`
    object of kinds.json maps it to (a retired name some caller still passes), else itself.
    New lock records name the canonical kind. KindsError when the aliases cannot be read. A
    `kind` that is not text comes back unchanged (the caller's own check refuses it)."""
    if not isinstance(kind, str) or kind in table:
        return kind
    try:
        to = policy.kinds_aliases(path or KINDS_FILE).get(kind)
    except ConfigError as e:
        raise KindsError(str(e))
    return to if to in table else kind


def aliases(path=None):
    """{retired name: kind} from the `aliases` object of the kinds table at `path` (None: the
    kit's); {} when it cannot be read. For comparing kind names only: load() and resolve() fail
    closed on a bad aliases object, a comparison falls back to the names as written."""
    try:
        return policy.kinds_aliases(path or KINDS_FILE)
    except ConfigError:
        return {}


def canonical(kind, path=None, table_aliases=None):
    """`kind` under its current name: the kind a retired name is an alias of, else `kind` as
    it is. A record written before a rename (a hold, a history line, a landing's booked kind)
    then compares equal to one written after it. table_aliases: aliases(path), already read."""
    if not isinstance(kind, str):
        return kind
    al = aliases(path) if table_aliases is None else table_aliases
    return al.get(kind, kind)


def is_size(cpu):
    """True for a whole number of cores of at least one (a bool is not one)."""
    return type(cpu) is int and cpu >= 1


def check_cap(table, agent_cap):
    """KindsError naming the first kind (by name) whose cpu is a whole number past agent_cap;
    `table` is kinds.load()'s, or one a caller passed in."""
    for name, e in sorted(table.items()):
        cpu = e.get("cpu") if isinstance(e, dict) else None
        if is_size(cpu) and cpu > agent_cap:
            raise KindsError("kind %s: cpu %d is more than the agent cap %d (a kind's "
                             "size is 1..agent_cap)" % (name, cpu, agent_cap))


def check_entry(name, e, agent_cap=None):
    """{cpu, gpu, share, warn_min} of entry `e` of kind `name`, checked; KindsError when the lock
    cannot size it (fail closed). Its other keys (a repo kind's `why`) are dropped."""
    cpu, gpu = e.get("cpu"), e.get("gpu", 0)
    warn = e.get("warn_min", 60)
    if cpu not in ALIASES and not is_size(cpu):
        raise KindsError("kind %s: cpu must be medium or heavy, or a whole number of cores "
                         "from 1%s, not %r" % (
                             name, " to %d" % agent_cap if agent_cap else "", cpu))
    if not (gpu == "pool" or (type(gpu) is int and gpu == 0)):
        raise KindsError("kind %s: gpu must be 0 or \"pool\", not %r" % (name, gpu))
    if type(warn) is not int or warn <= 0:
        raise KindsError("kind %s: warn_min must be a whole number of minutes" % name)
    return {"cpu": cpu, "gpu": gpu, "share": bool(e.get("share")) and gpu == "pool",
            "warn_min": warn}


def repo_entry(kind, checkout):
    """The checked entry (check_entry) the `.kit.json` "kinds" section of the main checkout of
    `checkout`'s repository gives `kind`, or None when `checkout` is in no git work tree git can
    place or the section does not name `kind`. KindsError when that `.kit.json` cannot be read or
    the entry cannot be sized: fail closed, no run on a repo kind whose size is unreadable."""
    top = gitrepo.main_checkout_toplevel(checkout)
    if not top:
        return None
    try:
        e = policy.repo_kinds(load_config(top)).get(kind)
    except ConfigError as err:
        raise KindsError("the repository kinds of %s cannot be read: %s" % (top, err))
    return None if e is None else check_entry(kind, e)


def with_repo(table, kind, checkout, path=None):
    """`table`, or a copy with `kind`'s repo entry (repo_entry) added when `checkout` is given and
    `kind` is text that is neither a kind of `table` nor an alias of the kinds table at `path`
    (None: the kit's): the kit wins a clash, so a kit name never reads `.kit.json`. KindsError as
    repo_entry. The caller checks the cap (check_cap) on what it gets."""
    if checkout is None or not isinstance(kind, str) or kind in table or kind in aliases(path):
        return table
    e = repo_entry(kind, checkout)
    if e is None:
        return table
    out = dict(table)
    out[kind] = e
    return out


def load(path=None, agent_cap=None):
    """{kind: {cpu, gpu, share, warn_min}} checked; KindsError on any entry the lock cannot
    size (fail closed). With `agent_cap` (the layout in force) a whole-number cpu past it fails
    the table too; without one that check is the caller's (check_cap)."""
    path = path or KINDS_FILE
    out = {}
    for name, e in sorted(_read(path).items()):
        out[name] = check_entry(name, e, agent_cap)
    if not out:
        raise KindsError("the kinds table %s names no kinds" % path)
    try:                            # the aliases are checked with the table (fail closed)
        policy.kinds_aliases(path)
    except ConfigError as e:
        raise KindsError(str(e))
    if agent_cap is not None:
        check_cap(out, agent_cap)
    return out


def describe(table):
    """'test: 3 cores' ('medium cpu' for an alias) ... one line per kind, for help text
    and status."""
    lines = []
    for k, e in sorted(table.items()):
        cpu = e["cpu"]
        parts = ["%d core%s" % (cpu, "" if cpu == 1 else "s") if is_size(cpu) else "%s cpu" % cpu]
        if e["gpu"]:
            parts.append("the whole gpu" + (" (or one slot with --gpu-share training)"
                                             if e["share"] else ""))
        lines.append("%s: %s" % (k, ", ".join(parts)))
    return lines
