"""The git pre-push check (ARCHITECTURE.md "Guard rules"; docs/guard.md "Pre-push").

check(root, lines) -> (ok, message). `lines` are what git writes to a pre-push hook's stdin, one
`<local ref> <local sha> <remote ref> <remote sha>` per ref pushed. For a push that updates the
remote's main branch, whoever pushes (the owner, an agent, a lander):
- a push that deletes main is refused;
- a push that is not a fast-forward of the remote's main is refused whatever push_block
  says. A remote main this clone lacks cannot be an ancestor of the pushed commit, so git
  cannot tell: refused too (fail closed). Git passes such a ref to the hook only when the push is
  forced (--force, a + refspec, --force-with-lease);
- then, when the repository sets repo.push_block, the pushed commit needs a gate-OK record, read by
  kitlib.gate.runner.verify_record: it must name a config_rev in the pushed commit's history and
  hash the gate config that commit holds;
- then, under push_block too, a commit the push adds that changes an owner-only path the push
  changes on main (its net change) and whose author email is not the owner's is refused, record
  or not (kitlib.guard.ownercommits; the base's
  owner rules merged with the pushed commit's): a branch gate records its own tip, so without this
  a gated agent branch could go straight to main.
The first rule that refuses names the reason; the order is the list's.
push_block and the main branch's name are read from the base: the remote main commit being
replaced when this clone has it; else (a new branch, or a remote sha this clone lacks) the default
branch's committed .kit.json: the first of refs/remotes/<remote>/HEAD, <remote>/main,
<remote>/master, local main, local master. The working tree is never the base, so an uncommitted
edit cannot switch the check off; no such ref means the defaults. A .kit.json that does not load
refuses the push (fail closed).
"""
import os

from kitlib import config as kconfig
from kitlib.config import schema as kschema
from kitlib.git import refs
from kitlib.guard import ownercommits

ZERO = '0' * 40
MAIN_DEFAULT = kschema.SECTIONS['repo']['main_branch'].default


def _base_rev(root, remote_sha, remote):
    if remote_sha and remote_sha.strip('0'):
        have, _ = refs.resolve(remote_sha, root)
        if have:
            return have
    names = ['refs/heads/main', 'refs/heads/master']
    if remote and not refs.bad_rev(remote):
        names = ['refs/remotes/%s/%s' % (remote, b) for b in ('HEAD', 'main', 'master')] + names
    for name in names:
        have, _ = refs.resolve(name, root)
        if have:
            return have
    return None


def _cfg(root, remote_sha, remote):
    """The base Config, or None when there is no base commit at all (the defaults: no push_block)."""
    rev = _base_rev(root, remote_sha, remote)
    return None if rev is None else kconfig.load_at(root, rev)


def verify_record(root, sha):
    """kitlib.gate.runner.verify_record, imported only when a push to main needs it (its imports
    load the Windows process modules); any failure is `(None, why)`, so the push is refused."""
    try:
        from kitlib.gate import runner
        return runner.verify_record(root, sha)
    except Exception as e:
        return None, 'cannot read gate-OK records: %s: %s' % (type(e).__name__, e)


def _exclude(root, cfg, remote_sha, remote):
    """The commits a push to main does not add: the remote main it replaces when this clone has it,
    else <remote>/<main> as last fetched, else none (every commit is judged). Never other
    remote-tracking refs: a pushed agent branch would hide its own commits."""
    if remote_sha and remote_sha.strip('0'):
        have, _ = refs.resolve(remote_sha, root)
        if have:
            return [have]
    if remote and not refs.bad_rev(remote):
        have, _ = refs.resolve('refs/remotes/%s/%s' % (remote, cfg.main_branch), root)
        if have:
            return [have]
    return []


def owner_refusal(root, cfg, local_sha, remote_sha, remote):
    """None when every commit the push adds to main that changes an owner-only path is the
    owner's (by author email), counting only the paths the push changes on main; else why not."""
    try:
        work = kconfig.load_at(root, local_sha).owner_rules()
    except kconfig.ConfigError:
        work = kconfig.OwnerRules()
    rules = kconfig.merge_owner_rules(work, cfg.owner_rules())
    bad, why = ownercommits.violations(root, rules, local_sha, _exclude(root, cfg, remote_sha, remote))
    if bad is None:
        return 'cannot check owner-only paths (%s)' % why
    return ownercommits.describe(rules, bad) if bad else None


def not_fast_forward(root, local_sha, remote_sha, main):
    """Why moving the remote's `main` from `remote_sha` to `local_sha` is refused, or None
    for a fast-forward (a creation, an all-zero `remote_sha`, is one)."""
    ff, why = refs.is_fast_forward(remote_sha, local_sha, root)
    if ff:
        return None
    if ff is None:
        return ('pre-push: refused, cannot tell whether %s is a fast-forward of %s %s (%s). '
                'Fetch, merge %s into it, then push again; never force-push %s.'
                % (local_sha[:12], main, remote_sha[:12], why.strip(), main, main))
    return ('pre-push: refused, %s is not a fast-forward of %s %s. Merge %s into it, then '
            'push again; never force-push %s.' % (local_sha[:12], main, remote_sha[:12], main, main))


def check(root, lines, gate_ok=None, remote='origin'):
    gate_ok = gate_ok or verify_record
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if not remote_ref.startswith('refs/heads/'):
            continue
        try:
            cfg = _cfg(root, remote_sha, remote)
        except kconfig.ConfigError as e:
            return False, ('pre-push: refused, .kit.json does not load (%s). Fix it, then push again.' % e)
        main = MAIN_DEFAULT if cfg is None else cfg.main_branch
        if remote_ref != 'refs/heads/' + main:
            continue
        if not local_sha.strip('0'):
            return False, 'pre-push: refused, this push deletes %s.' % main
        why = not_fast_forward(root, local_sha, remote_sha, main)
        if why:
            return False, why
        if cfg is None or not cfg.push_block:
            continue
        rec, why = gate_ok(root, local_sha)
        if rec is None:
            return False, ('pre-push: refused, %s has no gate-OK record (%s). Land with: python %s/land.py '
                           '<branch> --expect-main <%s sha> --expect-tip %s'
                           % (local_sha[:12], why, cfg.tools_dir, main, local_sha))
        why = owner_refusal(root, cfg, local_sha, remote_sha, remote)
        if why:
            return False, 'pre-push: refused, %s.' % why
    return True, None


def main(argv, stdin, root=None):
    """The hook body: (exit code, message or None). argv is git's: remote name and URL."""
    root = root or os.getcwd()
    remote = argv[0] if argv else 'origin'
    ok, msg = check(root, stdin.read().splitlines(), remote=remote)
    return (0, None) if ok else (1, msg)
