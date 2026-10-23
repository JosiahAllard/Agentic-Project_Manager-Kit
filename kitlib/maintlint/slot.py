"""kitlib.maintlint.slot: the machine-lock slot a plain maint-lint run holds (docs/maint-lint.md
"Schedule").

  take(repo, store, table, book)   the held Slot (kitlib.lock.run.book, kind maint-lint, slug
                                   maint-lint) for the run; a `with` block releases it. It waits
                                   at most the kind's warn_min. Raises Skipped when the lock stays
                                   busy that long (or an owner hold reserves the machine), and
                                   Refused, with the lock's own exit code, for any other refusal.

The lint runs in this process, so it books the slot with kitlib.lock.run.book and holds it around
the run, as kitlib.redfirst.run.book_slot does; kitlib.gate.runner.gated runs gate steps as
commands and does not fit an in-process run. --dry-run takes no slot (tools/maint_lint.py).
Nothing here prints or exits.
"""
from kitlib.lock import kinds as kinds_mod
from kitlib.lock import run as lockrun
from kitlib.lock.store import (EXIT_BUSY, EXIT_REQUIRE, EXIT_RESERVED, EXIT_TIMEOUT, LockError,
                               Store)

KIND = 'maint-lint'
SLUG = 'maint-lint'
EXIT_SKIPPED = 3
# The lock's refusals that mean "not now": busy past the wait, busy with no wait, or a quiet-hours
# owner hold. The run is skipped and the previous result is left as it was.
SKIP_CODES = (EXIT_BUSY, EXIT_TIMEOUT, EXIT_RESERVED)


class Skipped(Exception):
    """The lock was busy: the run did not start. str() is the one line to say."""


class Refused(Exception):
    """The lock refused the run for another reason; `code` is the lock's exit code."""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


def wait_s(table):
    """The longest wait for the slot: the maint-lint kind's warn_min, in seconds."""
    return table[KIND]['warn_min'] * 60


def take(repo, store=None, table=None, book=None):
    """The held Slot of a maint-lint run for `repo` (see the module doc). store, table and book
    default to the machine's store, its kinds table and kitlib.lock.run.book (a test stubs book)."""
    store = store or Store()
    book = book or lockrun.book
    try:
        table = table or kinds_mod.load(store.kinds_path)
    except kinds_mod.KindsError as e:
        raise Refused(EXIT_REQUIRE, 'REFUSED: the machine lock kinds table: %s; nothing was run'
                      % e)
    if KIND not in table:
        raise Refused(EXIT_REQUIRE, 'REFUSED: the machine lock has no %s kind; nothing was run'
                      % KIND)
    wait = wait_s(table)
    try:
        return book(KIND, repo, SLUG, wait_s=wait, store=store, kinds_table=table,
                    cmd=['maint_lint.py'], note='the maintenance lint of %s' % repo)
    except LockError as e:
        if e.code in SKIP_CODES:
            raise Skipped('SKIPPED: the machine lock gave no %s slot within %d min (exit %d: %s);'
                          ' nothing was run and the last result was left as it was'
                          % (KIND, wait // 60, e.code, e.message))
        raise Refused(e.code, 'REFUSED: the machine lock refused the run (exit %d): %s; nothing'
                              ' was run' % (e.code, e.message))
