"""The four ways a backlog verb stops (ported from an earlier tools/backlog.py). Each fixes the
label the CLI prints and its exit code; nothing was written when one is raised."""


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
