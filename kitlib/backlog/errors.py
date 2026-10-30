"""The four ways a backlog verb stops. Each fixes the
label the CLI prints and its exit code; nothing was written when one is raised, except
Uncommitted (an Overtaken only for the paths in its `left`) and IndexStale (each a Failure). IndexStale's write is committed, so tools/backlog.py
reports it as written, with a WARNING line and exit 0, not by this label and code. Dirty and
Busy are Failures raised before anything is written."""


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


class Ended(NotFound):
    """The id is no longer in the store because a commit (or a tracked file's removal here) ended
    it: closed, retired or deleted. Not a mistake, so `show` prints it and exits 0."""
    label = 'ENDED'
    code = 0


class Failure(Refusal):
    """The tool could not finish: the store or the ledger could not be read, git failed, or the
    write lost every compare-and-swap race it was allowed."""
    label = 'ERROR'
    code = 1


class Uncommitted(Failure):
    """The write's record files are on disk in the PM data worktree but its commit failed
    (kitlib.backlog.store.MainBackend): `backlog.py catch-up --apply` commits them (not after an
    Overtaken, which puts them back). `paths`: every record path the write wrote, as git spells
    it."""

    def __init__(self, message, paths=()):
        Failure.__init__(self, message)
        self.paths = list(paths)


class Overtaken(Uncommitted):
    """Refused because another commit changed one of the write's paths on HEAD (`head`) after the
    write began (`changed`: those paths): committing the files would overwrite that commit.
    `left` is a list after a write (MainBackend.publish), which puts each file it wrote back (a
    path in `changed` to what `head` holds, any other to what it held before the write): the
    paths it left as they are (the file no longer held what the write wrote, or could not be
    put back), empty when nothing of the write remains on disk. `left` is None for
    `catch-up --apply`, whose files are not its own and are all left. `compare`: the command that shows each path left against
    HEAD ('' when none is), the step before any `catch-up --apply`."""

    def __init__(self, message, paths=(), changed=(), compare='', head='', left=None):
        Uncommitted.__init__(self, message, paths)
        self.changed = list(changed)
        self.compare = compare
        self.head = head
        self.left = None if left is None else list(left)


class IndexStale(Failure):
    """The write is committed (HEAD has `commit`, or already held the paths when it is None) but
    the PM data worktree's own index was not updated for `paths`, so a plain `git commit` there
    would revert them. `repair`: the command that resets those index entries to HEAD, each path
    in double quotes, or the whole index (`git reset -q`, no paths) when it has no index file."""

    def __init__(self, message, commit=None, paths=(), repair=''):
        Failure.__init__(self, message)
        self.commit = commit
        self.paths = list(paths)
        self.repair = repair


class Dirty(Failure):
    """Refused before anything was written: `paths` (the record paths the write would change, as
    git spells them) differ from HEAD in the PM data worktree, a change made there and not
    committed (a hand edit, a staged change, a record never committed), which the write would
    delete or commit (kitlib.backlog.store.MainBackend with `clean`, the landing's close).
    `compare`: the command that shows them against HEAD (`git diff HEAD`, or `git status
    --short` when one is not in HEAD, which a diff would not show). `repair` instead, `compare`
    empty, when only the index differs from HEAD for them (the worktree holds HEAD's content):
    the command that resets those index entries to HEAD."""

    def __init__(self, message, paths=(), compare='', repair=''):
        Failure.__init__(self, message)
        self.paths = list(paths)
        self.compare = compare
        self.repair = repair


class Busy(Failure):
    """Refused before anything was written: git's lock on the PM data worktree's index (`lock`,
    its `index.lock`) stayed held while the write waited for it (kitlib.backlog.store
    MainBackend._lock_index): a git command there is writing the index, or one that was stopped
    left the file behind."""

    def __init__(self, message, lock=''):
        Failure.__init__(self, message)
        self.lock = lock
