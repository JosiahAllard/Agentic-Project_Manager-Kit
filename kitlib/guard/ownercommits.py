"""Owner-only paths by commit author (docs/guard.md "Owner-only commits").

violations(root, rules, tip, exclude) judges the net change first: an owner-only path
(rules.is_locked) counts only when landing `tip` on an `exclude` commit (the target main) would
change it, that is, when the target differs for it from the clean merge of the target and `tip`
(`git merge-tree --write-tree`; the tip itself for a fast-forward), or that merge conflicts on it.
For each path that counts, it lists every commit reachable from `tip` and from none of `exclude`
that changes the path and whose AUTHOR email is not the owner's (rules.is_owner). So an agent's
change that a later commit undoes, a rename away and back, and an owner's change the branch brought
in by merging main do not count; a change that stays is still judged commit by commit. With no
`exclude` there is no target: every path a commit changes counts. `gate land` refuses a branch with
any (kitlib.gate.land), and the pre-push hook refuses a push to main with any
(kitlib.guard.prepush), so a plain `git commit` cannot carry an owner-only change to main past
either.

What a commit changes:
  one parent or none   its diff against that parent (`git diff-tree -r --root`)
  two parents          what differs from the clean merge of the parents
                       (`git merge-tree --write-tree`), plus every path that merge conflicts on:
                       a merge that only joins two histories changes nothing of its own, and one
                       that edits an owner-only path (an "evil merge") or resolves a conflict in
                       one is judged by its author like any other commit
  more parents         the union of its diffs against each parent (strict)

The author email is git's `%ae` (never mailmapped), read with replace refs off (pushes do not carry
them, so the commits judged are the ones that leave). Renames are not followed: a rename is a
delete and an add, and both paths count. Any git failure is `(None, why)`: the caller refuses.

The author email is what the committer says it is (`--author`, GIT_AUTHOR_EMAIL): this check
stops a plain agent commit, not an agent that forges the owner's identity. The guard's
owner-identity rule (kitlib.guard.rules) denies a session other than the owner's setting the
owner's email on the command line; an identity in the session's starting environment is not seen.
"""
import collections

from kitlib.git import cmd, refs

Violation = collections.namedtuple('Violation', 'sha author paths')
NOREPLACE = ['--no-replace-objects']
LIST_TIMEOUT = 120    # seconds


def _git(root, args, env, timeout=cmd.GIT_TIMEOUT):
    r = cmd.call(NOREPLACE + list(args), root, timeout=timeout, env=env)
    if r.rc != 0:
        return None, 'git %s failed: %s' % (args[0], r.err or 'exit %s' % r.rc)
    return cmd.text(r.out), None


def _paths(out):
    return [p for p in out.split('\0') if p]


def _parse_merge_tree(raw):
    """`(tree, conflicted paths)` from `git merge-tree --write-tree --name-only --no-messages -z`."""
    from kitlib.commit import parse_merge_tree
    return parse_merge_tree(raw)


def commits(root, tip, exclude=(), env=None):
    """`([(sha, [parents], author email)], None)` reachable from `tip` and from no `exclude`
    revision, or `(None, why)`."""
    for rev in [tip] + list(exclude):
        why = refs.bad_rev(rev)
        if why:
            return None, why
    args = ['log', '--no-use-mailmap', '--format=%H %P%x00%ae%x00', tip]
    if exclude:
        args += ['--not'] + list(exclude)
    out, why = _git(root, args + ['--'], env, timeout=LIST_TIMEOUT)
    if out is None:
        return None, why
    fields = out.split('\0')
    found = []
    for i in range(0, len(fields) - 1, 2):
        ids = fields[i].split()
        if not ids:
            continue
        found.append((ids[0], ids[1:], fields[i + 1].strip().lower()))
    return found, None


def plain_changes(root, shas, env=None):
    """`({sha: sorted paths}, None)` for commits with one parent or none, in one
    `git diff-tree --stdin` (a commit that changes nothing prints nothing), or `(None, why)`."""
    if not shas:
        return {}, None
    r = cmd.call(NOREPLACE + ['diff-tree', '--stdin', '-r', '--root', '--name-only', '--no-renames',
                              '-z'], root, stdin=('\n'.join(shas) + '\n').encode('ascii'),
                 timeout=LIST_TIMEOUT, env=env)
    if r.rc != 0:
        return None, 'git diff-tree failed: %s' % (r.err or 'exit %s' % r.rc)
    order = {sha: i for i, sha in enumerate(shas)}
    found = {sha: set() for sha in shas}
    current, at = None, -1
    for token in _paths(cmd.text(r.out)):
        i = order.get(token)
        if i is not None and i > at:          # git answers in the order it was asked
            current, at = token, i
        elif current is None:
            return None, 'git diff-tree printed a path before any commit: %r' % token
        else:
            found[current].add(token)
    return {sha: sorted(paths) for sha, paths in found.items()}, None


def _against_merge(root, rev, one, two, env=None):
    """`(set of paths, None)`: what differs between `rev` and the clean merge of `one` and `two`
    (`git merge-tree --write-tree`), plus every path that merge conflicts on; or `(None, why)`."""
    r = cmd.call(NOREPLACE + ['merge-tree', '--write-tree', '--name-only', '--no-messages',
                              '--allow-unrelated-histories', '-z', one, two], root, env=env)
    tree, conflicted = _parse_merge_tree(cmd.text(r.out)) if r.rc in (0, 1) else ('', set())
    if not refs.SHA_RE.fullmatch(tree or ''):
        return None, 'git merge-tree failed for %s and %s (rc %s): %s' % (
            one[:10], two[:10], r.rc, r.err or 'no output')
    out, why = _git(root, ['diff', '--name-only', '--no-renames', '-z', tree, rev], env)
    return (None, why) if out is None else (set(_paths(out)) | set(conflicted), None)


def net_change(root, tip, targets, env=None):
    """`(set of paths, None)`: every path that landing `tip` on any of `targets` would change (the
    target against the clean merge of the target and `tip`, plus that merge's conflicts); or
    `(None, why)`. For a target `tip` descends from, the merge is `tip`'s own tree."""
    paths = set()
    for target in targets:
        found, why = _against_merge(root, target, target, tip, env)
        if found is None:
            return None, why
        paths |= found
    return paths, None


def changed(root, sha, parents, env=None):
    """`(sorted paths the commit itself changes, None)` (module docstring), or `(None, why)`."""
    if len(parents) <= 1:
        out, why = _git(root, ['diff-tree', '-r', '--root', '--no-commit-id', '--name-only',
                               '--no-renames', '-z', sha], env)
        return (None, why) if out is None else (sorted(set(_paths(out))), None)
    if len(parents) == 2:
        paths, why = _against_merge(root, sha, parents[0], parents[1], env)
        return (None, why) if paths is None else (sorted(paths), None)
    paths = set()
    for parent in parents:
        out, why = _git(root, ['diff', '--name-only', '--no-renames', '-z', parent, sha], env)
        if out is None:
            return None, why
        paths.update(_paths(out))
    return sorted(paths), None


def violations(root, rules, tip, exclude=(), env=None):
    """`([Violation], None)`: the commits from `tip` not in `exclude` that change an owner-only path
    that landing `tip` on `exclude` would change (module docstring) and are not authored by the
    owner; `(None, why)` when git cannot tell."""
    found, why = commits(root, tip, exclude, env)
    if found is None:
        return None, why
    net = None
    if exclude:
        net, why = net_change(root, tip, exclude, env)
        if net is None:
            return None, why
        if not any(rules.is_locked(p) for p in net):
            return [], None
    judged = [c for c in found if not rules.is_owner(c[2])]
    plain, why = plain_changes(root, [sha for sha, parents, _ in judged if len(parents) <= 1], env)
    if plain is None:
        return None, why
    bad = []
    for sha, parents, author in judged:
        paths, why = (plain[sha], None) if sha in plain else changed(root, sha, parents, env)
        if paths is None:
            return None, why
        locked = [p for p in paths if rules.is_locked(p) and (net is None or p in net)]
        if locked:
            bad.append(Violation(sha, author, locked))
    return bad, None


def describe(rules, bad, limit=5):
    """One line naming the offending commits (at most `limit`), their authors and paths, and why
    no author counts as the owner when that is the case."""
    parts = ['%s by %s changes %s' % (v.sha[:10], v.author or '(no email)',
                                      ', '.join(v.paths[:5]) + (' ...' if len(v.paths) > 5 else ''))
             for v in bad[:limit]]
    more = ' (and %d more)' % (len(bad) - limit) if len(bad) > limit else ''
    line = ('owner-only paths changed by a commit not authored by the owner: %s%s'
            % ('; '.join(parts), more))
    problem = rules.identity_problem()
    if problem:
        line += '; %s' % problem
    return line + ('. Only the owner commits these paths (author email = "owner".email in '
                   '.kit.json); ask the owner to make the change')
