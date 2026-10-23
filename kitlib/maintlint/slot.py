"""kitlib.maintlint.slot: the machine-lock slot a plain maint-lint run holds, and the skip marker a
skipped run leaves (docs/maint-lint.md "Schedule").

  take(repo, store, table, book)   the held Slot (kitlib.lock.run.book, kind maint-lint, slug
                                   maint-lint) for the run; a `with` block releases it. It waits
                                   at most WAIT_MIN, fixed here. Raises Skipped when the lock stays
                                   busy (reason busy or timeout) or a strong hold, the owner's or a
                                   game's, reserves the machine (reason hold, at once), and Refused
                                   for any other refusal: the lock's exit code, except its 1 and 2,
                                   which become EXIT_LOCK so they cannot read as maint_lint's own.
  marker_path(result_path)         the skip marker's path: MARKER beside result.json
  write_skip(result_path, skipped, now)  writes the marker for a Skipped; its path
  read_skip(result_path)           the marker as {'reason', 'at', 'code'}, or None (no marker,
                                   or one that cannot be read): the stable shape a reader calls
  clear_skip(result_path)          removes the marker; a run that wrote result.json calls it

The lint runs in this process, so it books the slot with kitlib.lock.run.book and holds it around
the run, as kitlib.redfirst.run.book_slot does; kitlib.gate.runner.gated runs gate steps as
commands and does not fit an in-process run. --dry-run takes no slot (tools/maint_lint.py).
Nothing here prints or exits.
"""
import datetime
import json
import os

from kitlib.lock import kinds as kinds_mod
from kitlib.lock import run as lockrun
from kitlib.lock.store import (EXIT_BUSY, EXIT_FAIL, EXIT_REQUIRE, EXIT_RESERVED, EXIT_TIMEOUT,
                               EXIT_USAGE, LockError, Store)

KIND = 'maint-lint'
SLUG = 'maint-lint'
EXIT_SKIPPED = 3
# The lock's own 1 (fail) and 2 (usage) would read as maint_lint's 1 (findings) and 2 (usage).
EXIT_LOCK = 4
# The wait is fixed here, not read from the kind's warn_min, which `gate.py --measure` rewrites
# from run history (kitlib/gate/measure.py).
WAIT_MIN = 15
# The lock's refusals that mean "not now", by skip reason: busy with no wait left, busy past the
# wait, or a strong (owner or game) hold. The run is skipped and the previous result is left as it
# was.
REASONS = {EXIT_BUSY: 'busy', EXIT_TIMEOUT: 'timeout', EXIT_RESERVED: 'hold'}
SKIP_CODES = tuple(REASONS)
MARKER = 'skipped.json'


class Skipped(Exception):
    """The run did not start: `reason` is busy, timeout or hold, `code` the lock's exit code.
    str() is the one line to say."""

    def __init__(self, reason, code, message):
        Exception.__init__(self, message)
        self.reason = reason
        self.code = code


class Refused(Exception):
    """The lock refused the run for another reason; `code` is the exit code to return."""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


def wait_s(table=None):
    """The longest wait for the slot, in seconds: WAIT_MIN, whatever the kind's warn_min says."""
    return WAIT_MIN * 60


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
        reason = REASONS.get(e.code)
        if reason == 'hold':
            raise Skipped(reason, e.code, 'SKIPPED: an owner or game hold reserves the machine'
                          ' (exit %d: %s); nothing was run and the last result was left as it was'
                          % (e.code, e.message))
        if reason:
            raise Skipped(reason, e.code, 'SKIPPED: the machine lock gave no %s slot within %d min'
                          ' (exit %d: %s); nothing was run and the last result was left as it was'
                          % (KIND, wait // 60, e.code, e.message))
        code = EXIT_LOCK if e.code in (EXIT_FAIL, EXIT_USAGE) else e.code
        raise Refused(code, 'REFUSED: the machine lock refused the run (lock exit %d): %s;'
                            ' nothing was run' % (e.code, e.message))


def marker_path(result_path):
    return os.path.join(os.path.dirname(os.path.abspath(result_path)), MARKER)


def write_skip(result_path, skipped, now=None):
    """Writes the skip marker for `skipped` (a Skipped) beside result_path, whole (a temp file,
    then os.replace); returns its path. OSError is the caller's."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    path = marker_path(result_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump({'reason': skipped.reason, 'at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
                   'code': skipped.code}, fh, sort_keys=True)
        fh.write('\n')
    os.replace(tmp, path)
    return path


def read_skip(result_path):
    """The skip marker beside result_path as {'reason': str, 'at': str, 'code': int}, or None
    when there is none or it cannot be read or does not hold that shape."""
    try:
        with open(marker_path(result_path), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('reason') not in REASONS.values() \
            or not isinstance(data.get('at'), str) or not isinstance(data.get('code'), int):
        return None
    return {'reason': data['reason'], 'at': data['at'], 'code': data['code']}


def clear_skip(result_path):
    """Removes the skip marker beside result_path; True when one was there."""
    try:
        os.remove(marker_path(result_path))
    except FileNotFoundError:
        return False
    return True
