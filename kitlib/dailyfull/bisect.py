"""kitlib.dailyfull.bisect: after a red daily full run, the first-parent commit that turned its red
steps red (docs/daily-full.md "Bisect").

G is the commit of the latest earlier green run that is an ancestor of the red run's commit S. The
commits searched are `git rev-list --first-parent G..S`. Probes go in this order:

- First, S itself is probed. If it is green again, the verdict is FLAKE and nothing more runs.
- Then a binary search runs to the first red commit: about 1 + log2(n) probes for n commits.

At most `max_probes` probes run. If the cap is reached first, the verdict names the range the
culprit lies in.

Each probe does three things:

- It switches the daily worktree to the commit. The caller holds the tree's lock throughout.
- It plans that commit's own gate (`plan`, daily argv), keeping only the red steps and setup.
- It runs those steps through kitlib.gate.runner.gated, in a machine-lock slot of their own,
  logging to `<log dir>/bisect-<n>-<sha10>`.

A probe is red when a red step FAILs. It is green when every red step it has passed or warned. A
commit whose gate lacks a red step counts as green for that step. Anything else (a switch, config
or plan failure, a lock refusal, a red step that did not run) stops the bisect as inconclusive.
Afterwards the worktree is put back at S.

Nothing here prints or exits; the caller says the lines.
"""
import os

from kitlib.config import load as load_config
from kitlib.config.types import ConfigError
from kitlib.gate import runner
from kitlib.gate import steps as S

FLAKE = 'did not reproduce: possible flake'
RED, GREEN = 'red', 'green'
# Result names that are not steps a probe can run: the gate's own and the window report's.
NOT_STEPS = ('gate', 'windows')
DEFAULT_PROBES = 5


def bisectable(result):
    """The red step names a probe can run again. Empty when the run's red is `infra` (an error,
    or nothing ran)."""
    red = result.get('red') or []
    if 'infra' in red or result.get('error'):
        return []
    return [n for n in red if n not in NOT_STEPS]


def last_green(results, sha, wt, git):
    """The commit of the latest green run among `results` (oldest first) that is a strict ancestor
    of `sha`, or None."""
    for r in reversed(results):
        g = r.get('sha')
        if r.get('verdict') != 'green' or not g or g == sha:
            continue
        rc, _, _ = git(wt, 'merge-base', '--is-ancestor', g, sha)
        if rc == 0:
            return g
    return None


class Prober(object):
    """Runs the red steps `red` at one commit of the daily worktree `wt`. `plan(cfg, wt, sha)`:
    the daily steps of that commit's gate (kitlib.dailyfull.plan)."""

    def __init__(self, wt, red, kind, name, slug, log_dir, say, book_kw, git, plan):
        self.wt, self.red, self.kind, self.name, self.slug = wt, list(red), kind, name, slug
        self.log_dir, self.say, self.book_kw, self.git, self.plan = log_dir, say, book_kw, git, plan
        self.count = 0

    def __call__(self, sha):
        """`(RED | GREEN | None, failed red step names, why)`; None is inconclusive."""
        self.count += 1
        rc, _, err = self.git(self.wt, 'switch', '--detach', sha)
        if rc != 0:
            return None, [], 'could not put %s at %s: %s' % (self.wt, sha[:10], err or 'exit %d' % rc)
        try:
            cfg = load_config(self.wt)
            chosen = self.plan(cfg, self.wt, sha)
        except (ConfigError, S.GateConfigError) as e:
            return None, [], 'the gate at %s: %s' % (sha[:10], e)
        names = [s.name for s in chosen if s.name in self.red]
        if not names:
            return GREEN, [], None
        chosen = [s for s in chosen if s.name in self.red or s.name == 'setup']
        logs = os.path.join(self.log_dir, 'bisect-%d-%s' % (self.count, sha[:10]))
        os.makedirs(logs, exist_ok=True)
        self.say('daily: bisect probe %d at %s: %s' % (self.count, sha[:10], ', '.join(names)))
        results, refused = runner.gated(cfg, self.wt, sha, [], chosen, self.kind, self.name,
                                        self.slug, logs, self.say, self.book_kw)
        if refused is not None:
            return None, [], 'the machine lock refused a probe: %s' % '; '.join(refused.lines)
        status = {r['name']: r['status'] for r in results}
        failed = [n for n in names if status.get(n) == 'FAIL']
        if failed:
            return RED, failed, None
        if status.get('setup') == 'FAIL':
            return None, [], 'setup failed at %s' % sha[:10]
        idle = [n for n in names if status.get(n) not in ('PASS', 'WARN')]
        if idle:
            return None, [], 'red step(s) %s did not run at %s' % (', '.join(idle), sha[:10])
        return GREEN, [], None


def search(commits, probe, max_probes):
    """Bisects `commits` (oldest first, the last is the red run's commit; the commit before the
    first is green) with `probe(sha) -> (verdict, failed, why)`, at most `max_probes` probes.
    Returns {bad, culprit, probes, verdict}: `probes` lists {sha, verdict, red}; `bad` is the
    oldest commit seen red; `culprit` is set only when the commit before it was seen green."""
    probes = []
    good, bad = -1, None

    def run(i):
        verdict, failed, why = probe(commits[i])
        probes.append({'sha': commits[i], 'verdict': verdict or 'inconclusive', 'red': failed,
                       **({'why': why} if why else {})})
        return verdict, why

    out = {'bad': None, 'culprit': None, 'probes': probes}
    if max_probes < 1 or not commits:
        out['verdict'] = 'not run: no probe allowed' if commits else 'not run: no commit to probe'
        return out
    verdict, why = run(len(commits) - 1)
    if verdict is None:
        out['verdict'] = 'inconclusive: %s' % why
        return out
    if verdict == GREEN:
        out['verdict'] = FLAKE
        return out
    bad = len(commits) - 1
    while bad - good > 1:
        if len(probes) >= max_probes:
            out['bad'] = commits[bad]
            out['verdict'] = ('probe cap %d reached: the culprit is one of the %d commit(s) '
                              'after %s up to %s' % (max_probes, bad - good,
                                                     commits[good][:10] if good >= 0 else 'the green run',
                                                     commits[bad][:10]))
            return out
        mid = (good + bad) // 2
        verdict, why = run(mid)
        if verdict is None:
            out['bad'] = commits[bad]
            out['verdict'] = 'inconclusive: %s' % why
            return out
        if verdict == RED:
            bad = mid
        else:
            good = mid
    out['bad'] = out['culprit'] = commits[bad]
    out['verdict'] = 'culprit %s' % commits[bad][:10]
    return out


def bisect(result, out_results, prober, wt, git, max_probes):
    """The bisect dict for the red `result` (its `sha` is S), or None with no bisect to run
    (no bisectable red step). `out_results`: every earlier result, oldest first. The worktree is
    put back at S afterwards."""
    red = bisectable(result)
    if not red:
        return None
    sha = result['sha']
    good = last_green(out_results, sha, wt, git)
    if good is None:
        return {'good': None, 'bad': sha, 'culprit': None, 'probes': [],
                'verdict': 'not run: no earlier green run is an ancestor of %s' % sha[:10]}
    rc, text, err = git(wt, 'rev-list', '--first-parent', '--reverse', '%s..%s' % (good, sha))
    if rc != 0:
        return {'good': good, 'bad': sha, 'culprit': None, 'probes': [],
                'verdict': 'inconclusive: git rev-list failed: %s' % (err or 'exit %d' % rc)}
    commits = text.split()
    try:
        found = search(commits, prober, max_probes)
    finally:
        rc, _, err = git(wt, 'switch', '--detach', sha)
    if rc != 0:
        found['back'] = 'could not put %s back at %s: %s' % (wt, sha[:10], err or 'exit %d' % rc)
    return dict(found, good=good)
