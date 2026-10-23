"""A per-process memo of git object answers, on in test processes only.

Starting a process is the dear part of a git read on Windows (a kernel token-index walk per
CreateProcess), and a test asks some questions more than once. This memo keeps only what a
repository's writes never change: what a full object name names (refs.resolve of a full hash,
peeled or not). A ref, HEAD, a branch, the work tree layout (classify, common_dir)
and the index are NOT kept: another process moves them at any time (a lander, a fetch in a
sibling worktree, a hook or test this process started), and kitlib re-reads some of them on
purpose to see such a move (records.landing.LandingLock.gate_refusal re-reads <remote>/<main>).

What could still change an object fact is the object being pruned, or the repository at a path
being replaced (a folder removed and a new repository made there), by something this process
cannot see: another process, or a git child not started through cmd.call. So:

- The memo is OFF unless the process calls enable(). Only tests/gitfixture.py does, so a test
  process keeps answers and every other process (a tool, a hook, the lock MCP server, the PM
  watcher, however long it lives) asks git every time, as before this module. A test's
  repositories are fresh temp folders that only the test changes.
- When on, it is forgotten by every git child cmd.call starts that is not a known read (`READS`),
  before it starts and again after it ends, so a read that ran alongside it is not kept either
  (`generation()` snapshots: put() keeps nothing taken under an older generation); and by
  clear(), which tests/gitfixture.py calls before each of its own git children and after a copy.

A time limit on entries was not chosen: an answer could still be wrong inside the limit.

Nothing is shared between processes, and nothing is written to disk.
"""
import threading

# git subcommands that never write a ref, the index, the config or the work tree. Anything else
# (an unknown subcommand too) forgets the memo.
READS = frozenset((
    'cat-file', 'describe', 'diff', 'diff-tree', 'for-each-ref', 'log', 'ls-files', 'ls-remote',
    'ls-tree', 'merge-base', 'name-rev', 'rev-list', 'rev-parse', 'show', 'show-ref', 'var',
))

_lock = threading.Lock()
_generation = 0
_memo = {}
_enabled = False


def enable():
    """Turn the memo on for this process. For test processes only (tests/gitfixture.py)."""
    global _enabled
    _enabled = True


def enabled():
    return _enabled


def generation():
    """The current generation: snapshot it before a read, hand it to put() after."""
    return _generation


def clear():
    """Forget every answer and start a new generation."""
    global _generation
    with _lock:
        _generation += 1
        _memo.clear()


def child(subcommand):
    """cmd.call's note that a git child with `subcommand` starts or has ended: anything but a
    known read forgets the memo."""
    if subcommand not in READS:
        clear()


def get(key):
    """The kept answer for `key`, or None (a None key is never kept; nothing is while off)."""
    if key is None or not _enabled:
        return None
    return _memo.get(key)


def put(key, gen, value):
    """Keep `value` for `key` when the memo is on and no write started since generation `gen`
    was taken."""
    if key is None or value is None or not _enabled:
        return
    with _lock:
        if gen == _generation:
            _memo[key] = value
