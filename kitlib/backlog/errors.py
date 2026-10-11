"""The four ways a backlog verb stops (ported from an earlier tools/backlog.py). Each fixes the
label the CLI prints and its exit code; nothing was written when one is raised, except
Uncommitted and IndexStale (each a Failure). IndexStale's write is committed, so tools/backlog.py
reports it as written, with a WARNING line and exit 0, not by this label and code."""


class Refusal(Exception):
    """A well-formed write a rule forbids (an owner hold or price, a tool-written field, an
    overwrite)."""
    label = 'REFUSED'
    code = 2


class Usage(Refusal):
    """The command is wrong: a missing, malformed or contradictory argument, or a value the
    record's schema rejects."""
    label = 'USAGE'
    code = 2


class NotFound(Refusal):
    """The id names no record in the store."""
    label = 'NOT FOUND'
    code = 1


class Failure(Refusal):
    """The tool could not finish: the store or the ledger could not be read, git failed, or the
    write lost every compare-and-swap race it was allowed."""
    label = 'ERROR'
    code = 1


class Uncommitted(Failure):
    """The write's record files are on disk in the PM data worktree but its commit failed
    (kitlib.backlog.store.MainBackend): `backlog.py catch-up --apply` commits them (not after an
    Overtaken). `paths`: every record path the write wrote, as git spells it."""

    def __init__(self, message, paths=()):
        Failure.__init__(self, message)
        self.paths = list(paths)


class Overtaken(Uncommitted):
    """Uncommitted because another commit changed one of the write's paths on HEAD after the write
    began (`changed`: those paths): committing the worktree's files now would overwrite that
    commit, so `catch-up --apply` is not the next step; comparing each path with HEAD is
    (`compare`: the command that shows the difference)."""

    def __init__(self, message, paths=(), changed=(), compare=''):
        Uncommitted.__init__(self, message, paths)
        self.changed = list(changed)
        self.compare = compare


class IndexStale(Failure):
    """The write is committed (HEAD has `commit`, or already held the paths when it is None) but
    the PM data worktree's own index was not updated for `paths`, so a plain `git commit` there
    would revert them. `repair`: the command that resets those index entries to HEAD, each path
    in double quotes."""

    def __init__(self, message, commit=None, paths=(), repair=''):
        Failure.__init__(self, message)
        self.commit = commit
        self.paths = list(paths)
        self.repair = repair
