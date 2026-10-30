"""The agent-start block: a few lines about the checkout a dispatched kit agent starts in, which its
host's SubagentStart hook adds after the kit header (docs/preflight.md "Agent start block").

  lines(cwd, kit, budget, now)   the block's lines for the folder `cwd`; never raises
  text(cwd, kit, role, budget)   the block: one title line, then `- ` and each line
  is_dispatched(role, kit)       True for a kit role an agent is dispatched as (content/roles, not
                                 `run: session`, an old name taken as its new one); False for none,
                                 a session role, or a host's own agent (Explore and the like)

It is the cheap part of tools/preflight.py: where the folder is (the main checkout or a linked
worktree, and the command that makes a worktree), the base pair, and for a worktree how far it is
from the base and how many files it has changed; then the stray check of the main checkout. It
never fetches and never reaches a remote: the remote side is as of the last fetch, and the line
says how old that is (the mtime of FETCH_HEAD). As in preflight, a reading that failed says so (`?`,
`UNKNOWN`, `NOT CHECKED`), and `(equal)` needs both base refs read.

The hook waits for it at most `budget` seconds: the reading runs on a daemon thread, each git call
is given what is left of the budget as its timeout, and whatever is not done by then is one
`NOT CHECKED` line. No `budget` is `kitlib.clock.budget(BUDGET)`: BUDGET in a real run, the time
fence's ceiling under the kit's tests (docs/flaky-tests.md); a budget passed in is used as is.
At a subagent's start the folder is its parent's (the agent has not made its own worktree yet), so
the block describes where the agent starts, not where it will work.
"""
import os
import threading
import time

from kitlib import clock
from kitlib.git import cmd, repo, stray

BUDGET = 1.0
MAX_STRAY = 4
MIN_TIMEOUT = 0.05


def is_dispatched(role, kit):
    if not role or not isinstance(role, str):
        return False
    try:
        from kitlib import agentname, content
        name = agentname.bare(role)
        name = content.ALIASES.get(name, name)
        for entry in content.roster(kit):
            if entry.name == name:
                return entry.run != 'session'
    except Exception:
        return False
    return False


def text(cwd, kit, role=None, budget=None):
    start = time.monotonic()
    rows = lines(cwd, kit, budget)
    took = int((time.monotonic() - start) * 1000)
    title = ('Agent start (%s): the folder this agent starts in, read at %s by the start hook with no '
             'fetch (%d ms); tools/preflight.py --full has the full report:'
             % (role or 'subagent', time.strftime('%H:%M'), took))
    return title + '\n' + '\n'.join('- ' + r for r in rows)


def lines(cwd, kit, budget=None, now=None):
    out = []
    budget = clock.budget(BUDGET) if budget is None else budget
    deadline = time.monotonic() + budget

    def work():
        try:
            _read(cwd, kit, out, deadline, now)
        except Exception as e:
            out.append(_failed(e))
    try:
        worker = threading.Thread(target=work, name='agentstart', daemon=True)
        worker.start()
        worker.join(budget)
        if worker.is_alive():
            return list(out) + ['NOT CHECKED - the rest: over the %.1f s budget' % budget]
        return list(out)
    except Exception as e:
        return list(out) + [_failed(e)]


def _failed(e):
    return 'could not check: %s: %s' % (type(e).__name__, e)


def _slash(path):
    return path.replace('\\', '/').rstrip('/')


def _read(cwd, kit, out, deadline, now):
    if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd):
        out.append('checkout  NOT CHECKED - the hook gave no absolute folder (%r)' % (cwd,))
        return
    place = repo.classify(cwd)
    if place.kind not in (repo.MAIN, repo.LINKED):
        out.append('checkout  NOT CHECKED - %s is in no git work tree%s'
                   % (cwd, ' (%s)' % place.detail if place.detail else ''))
        return
    top = place.top

    def git(*args, strip=True):
        left = max(MIN_TIMEOUT, deadline - time.monotonic())
        return cmd.run_text(list(args), top, timeout=left, strip=strip)

    branch = git('rev-parse', '--abbrev-ref', 'HEAD')[0] or '?'
    head = git('rev-parse', '--short', 'HEAD')[0] or '?'
    kind = 'MAIN checkout' if place.kind == repo.MAIN else 'worktree'
    out.append('checkout  %s %s | branch %s @ %s' % (kind, top, branch, head))
    if place.kind == repo.MAIN:
        from kitlib.preflight import nextblock
        tools = _slash(kit) + '/tools'
        out.append('next      edit nothing here: make your worktree with %s -C "%s" <area>/<slug>, '
                   'then work only in it' % (nextblock.command(tools, 'new_worktree'), _slash(top)))
    try:
        from kitlib.config import load
        cfg = load(top)
    except Exception as e:
        out.append('base      NOT CHECKED - the repo config: %s' % _failed(e))
        cfg = None
    if cfg is not None:
        _base(top, cfg, git, place, out, branch, now)
    if place.kind == repo.LINKED:
        # --no-optional-locks: a status killed at the budget's timeout while it refreshed the index
        # would leave a stale index.lock in the agent's own worktree
        status = git('--no-optional-locks', 'status', '--porcelain', '--untracked-files=normal', strip=False)[0]
        out.append('dirty     %s' % ('%d file(s)' % len([s for s in status.splitlines() if s.strip()])
                                     if status is not None else 'UNKNOWN - git status failed here'))
    _code, stray_lines = stray.check(top)
    out.extend(stray_lines[:MAX_STRAY])
    if len(stray_lines) > MAX_STRAY:
        out.append('... and %d more stray line(s): tools/preflight.py lists them' % (len(stray_lines) - MAX_STRAY))


def _base(top, cfg, git, place, out, branch, now):
    from kitlib.preflight import settings
    # settings.base_pair's pick, with the budget's timeout on its one for-each-ref
    remote = cfg.remote
    names = settings.base_names(cfg)
    wanted = [r for n in names for r in ('refs/heads/%s' % n, 'refs/remotes/%s/%s' % (remote, n))]
    got = git('for-each-ref', '--format=%(refname) %(objectname:short)', *wanted)[0] or ''
    refs = dict(line.split(' ', 1) for line in got.splitlines() if ' ' in line)
    base = settings.pick_base(names, refs)
    b = refs.get('refs/heads/' + base, '')
    ob = refs.get('refs/remotes/%s/%s' % (remote, base), '')
    if b and ob:
        same = '(equal)' if b == ob else '(DIFFERENT)'
    else:
        same = '(not compared: %s not read)' % ' and '.join(
            n for n, v in ((base, b), ('%s/%s' % (remote, base), ob)) if not v)
    out.append('base      %s @ %s | %s/%s @ %s %s; %s'
               % (base, b or '?', remote, base, ob or '?', same, _fetch_age(place, remote, now)))
    if place.kind != repo.LINKED or branch == base:
        return
    measure = settings.measure_rev(cfg, base, b, ob)
    counts = (git('rev-list', '--left-right', '--count', '%s...HEAD' % measure)[0] or '').split()
    out.append('vs base   behind %s, ahead %s%s' % (
        counts[0] if len(counts) == 2 else '?', counts[1] if len(counts) == 2 else '?',
        '' if measure == base else ' (against %s/%s)' % (remote, base)))


def _fetch_age(place, remote, now):
    """How old the remote side is: FETCH_HEAD's mtime in the common git folder."""
    if not place.common:
        return '%s fetch time UNKNOWN' % remote
    try:
        when = os.path.getmtime(os.path.join(place.common, 'FETCH_HEAD'))
    except FileNotFoundError:
        return '%s never fetched here' % remote
    except OSError as e:
        return '%s fetch time UNKNOWN (%s)' % (remote, e)
    minutes = max(0, int(((time.time() if now is None else now) - when) // 60))
    if minutes < 120:
        age = '%d min' % minutes
    elif minutes < 48 * 60:
        age = '%d h' % (minutes // 60)
    else:
        age = '%d days' % (minutes // (24 * 60))
    return '%s as of the last fetch %s ago' % (remote, age)
