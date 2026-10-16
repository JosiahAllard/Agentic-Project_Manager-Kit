"""Carrying a verdict forward (docs/gate.md "Carrying a verdict forward", owner ruling R-2050): a
branch reviewed at one tip and then synced with main needs no fresh review, when the sync is all
that changed. check() decides that mechanically, read-only; kitlib.gate.verdicts.carry records it
and the landing checks it again under its lock.

The tip carries the review given at `reviewed` against the base `base` (main as fetched) when:

  1. every commit the tip holds that neither `reviewed` nor `base` holds is a merge with exactly
     two parents, on one first-parent line from the tip down to `reviewed` (no commit of the
     branch's own after the review, none before a merge either);
  2. each merge's second parent is in `base`, and its tree is the tree `git merge-tree
     --write-tree` makes of its two parents with no conflict (no conflict resolved by hand, no
     merge edited before its commit);
  3. the branch's own diff against main is the one reviewed: the same paths changed, and the
     patch from its merge base with `base` to the tip (-U3, no renames, no external diff or
     textconv) equals the patch from the reviewed tip's merge base to it, once the `index` lines
     (blob hashes) and the hunk positions are taken out. So main's edits more than three lines
     from every branch hunk carry; an edit inside a hunk's context does not. A binary file is
     taken as equal only when both sides hold the very same blobs for it.

Every git call that gives no answer, or an answer of the wrong shape, refuses: (None, why). A pass
is (facts, None), facts = {from, merges (oldest first), main, diff (sha256 of the tip's normalized
patch)}. Nothing here prints or writes a ref; merge-tree writes only objects.
"""
import hashlib

from kitlib.git import cmd, refs

CONTEXT = 3                 # diff context lines (-U3): the fail-closed choice of KIT-VERDICT-CARRY-FORWARD
FILE_MODES = ('100644', '100755')
BINARY_MARKS = (b'Binary files ', b'GIT binary patch')


class _No(Exception):
    """A refusal, with its why."""


def check(root, reviewed, tip, base, git=None):
    """`(facts, None)` when `tip` carries the review given at `reviewed` against `base`, else
    `(None, why)` (the module docstring says when). Every rev is a full commit hash. `git`:
    a callable taking git's arguments and returning kitlib.git.cmd.Result (default: git in
    `root`)."""
    for name, rev in (('reviewed', reviewed), ('tip', tip), ('base', base)):
        if not refs.is_sha(rev):
            return None, 'the %s commit is a full commit hash, not %r' % (name, rev)
    if reviewed == tip:
        return None, 'the tip is the reviewed commit %s: nothing to carry' % tip[:10]
    run = git or (lambda args: cmd.call(args, root))
    try:
        merges = _merges(run, reviewed, tip, base)
        for m, p1, p2 in merges:
            _clean(run, m, p1, p2)
        digest = _same_diff(run, reviewed, tip, base)
    except _No as e:
        return None, str(e)
    return {'from': reviewed, 'merges': [m for m, _, _ in merges], 'main': base,
            'diff': digest}, None


def _git(run, args, what=None, ok=(0,)):
    """`(rc, out bytes)` of git `args`, or _No when git gives no answer or exits outside `ok`."""
    what = what or ' '.join(args[:2])
    r = run(list(args))
    if r.rc is None:
        raise _No('git %s gave no answer: %s' % (what, r.err))
    if r.rc not in ok:
        raise _No('git %s exited %s: %s' % (what, r.rc, (r.err or '').strip()[:200]
                                           or 'no message'))
    return r.rc, r.out or b''


def _merges(run, reviewed, tip, base):
    """[(merge, first parent, second parent)], oldest first: the commits after the review, each a
    two-parent merge on the first-parent line from `tip` to `reviewed` whose second parent is in
    `base`."""
    if not _is_ancestor(run, reviewed, tip):
        raise _No('the tip %s does not hold the reviewed %s' % (tip[:10], reviewed[:10]))
    _, out = _git(run, ['rev-list', '--parents', tip, '^' + reviewed, '^' + base, '--'],
                  'rev-list %s ^%s ^%s' % (tip[:10], reviewed[:10], base[:10]))
    parents = {}
    for line in cmd.text(out).splitlines():
        shas = line.split()
        if not shas or not all(refs.is_sha(s) for s in shas):
            raise _No('git rev-list printed %r, not commits with their parents' % line[:200])
        parents[shas[0]] = shas[1:]
    chain, c = [], tip
    while c != reviewed:
        if c not in parents:
            raise _No('the first-parent line from the tip %s does not lead back to the reviewed '
                      '%s (it reaches %s)' % (tip[:10], reviewed[:10], c[:10]))
        ps = parents[c]
        if len(ps) != 2:
            raise _No('%s came after the review and is not a merge of main: it has %d parent%s'
                      % (c[:10], len(ps), '' if len(ps) == 1 else 's'))
        chain.append((c, ps[0], ps[1]))
        c = ps[0]
    for m, _, p2 in chain:
        if not _is_ancestor(run, p2, base):
            raise _No('%s merges %s, which is not in main %s' % (m[:10], p2[:10], base[:10]))
    left = sorted(set(parents) - set(m for m, _, _ in chain))
    if left:
        raise _No('%s came after the review and is not a merge of main on the branch\'s line'
                  % ', '.join(s[:10] for s in left))
    chain.reverse()
    return chain


def _is_ancestor(run, ancestor, descendant):
    """True when the commit `ancestor` is `descendant` or one of its ancestors, False when git
    says it is not. Asked as `merge-base --independent`, which prints `ancestor` exactly when
    `descendant` does not reach it (refs.is_ancestor's confirming question)."""
    if ancestor == descendant:
        return True
    _, out = _git(run, ['merge-base', '--independent', ancestor, descendant],
                  'merge-base --independent %s %s' % (ancestor[:10], descendant[:10]))
    shas = cmd.text(out).split()
    if not shas or not all(refs.is_sha(s) for s in shas):
        raise _No('git merge-base --independent printed %r, not commits' % cmd.text(out)[:200])
    return ancestor not in shas


def _tree(run, rev):
    _, out = _git(run, ['rev-parse', '--verify', '%s^{tree}' % rev], 'rev-parse %s^{tree}' % rev[:10])
    tree = cmd.text(out).strip()
    if not refs.is_sha(tree):
        raise _No('git rev-parse %s^{tree} printed %r, not a tree' % (rev[:10], tree[:200]))
    return tree


def _clean(run, m, p1, p2):
    """_No unless the merge `m` holds the tree git merges of `p1` and `p2`, with no conflict."""
    rc, out = _git(run, ['merge-tree', '--write-tree', '--name-only', '--no-messages', p1, p2],
                   'merge-tree %s %s' % (p1[:10], p2[:10]), ok=(0, 1))
    lines = cmd.text(out).splitlines()
    if rc == 1:
        raise _No('%s merges main with a conflict (%s): a conflict resolved by hand needs a '
                  'fresh review' % (m[:10], ', '.join(lines[1:6]) or 'no path named'))
    if not lines or not refs.is_sha(lines[0].strip()):
        raise _No('git merge-tree printed %r, not a tree' % cmd.text(out)[:200])
    if lines[0].strip() != _tree(run, m):
        raise _No('%s is not the tree git merges of %s and %s: a merge changed by hand needs a '
                  'fresh review' % (m[:10], p1[:10], p2[:10]))


def _merge_base(run, a, b):
    _, out = _git(run, ['merge-base', '--all', a, b], 'merge-base --all %s %s' % (a[:10], b[:10]))
    shas = cmd.text(out).split()
    if len(shas) != 1 or not refs.is_sha(shas[0]):
        raise _No('%s and main %s have %s, not one merge base' % (
            a[:10], b[:10], '%d' % len(shas) if shas else 'none'))
    return shas[0]


def _blobs(run, rev):
    """{path: (mode, oid)} of every file `rev` holds."""
    _, out = _git(run, ['ls-tree', '-r', '-z', '--full-tree', rev], 'ls-tree -r %s' % rev[:10])
    got = {}
    for rec in out.split(b'\0'):
        if not rec:
            continue
        head, tab, path = rec.partition(b'\t')
        parts = head.split()
        if not tab or len(parts) != 3:
            raise _No('git ls-tree printed %r, not a tree entry' % rec[:200])
        got[path.decode('utf-8', 'surrogateescape')] = (parts[0].decode('ascii', 'replace'),
                                                       parts[2].decode('ascii', 'replace'))
    return got


def _changed(before, after):
    return {p: (before.get(p), after.get(p)) for p in set(before) | set(after)
            if before.get(p) != after.get(p)}


def _patch(run, a, b):
    """The patch from `a` to `b`, normalized: lines, with `index` lines dropped and each hunk
    header cut to `@@`."""
    args = ['diff', '--no-color', '--no-ext-diff', '--no-renames', '-U%d' % CONTEXT,
            '--src-prefix=a/', '--dst-prefix=b/']
    if not cmd.fake_active():       # the fake git has no textconv to turn off
        args.append('--no-textconv')
    _, out = _git(run, args + [a, b, '--'], 'diff %s %s' % (a[:10], b[:10]))
    lines = []
    for line in out.split(b'\n'):
        if line.startswith(b'index '):
            continue
        lines.append(b'@@' if line.startswith(b'@@ ') else line)
    return lines


def _same_diff(run, reviewed, tip, base):
    """The sha256 of the tip's normalized patch against its merge base with `base`; _No unless
    it is the reviewed one (the module docstring, 3)."""
    old_base, new_base = _merge_base(run, reviewed, base), _merge_base(run, tip, base)
    old = _changed(_blobs(run, old_base), _blobs(run, reviewed))
    new = _changed(_blobs(run, new_base), _blobs(run, tip))
    said = "the branch's own diff against main is not the one reviewed"
    if set(old) != set(new):
        gone, came = sorted(set(old) - set(new)), sorted(set(new) - set(old))
        raise _No('%s: %s' % (said, '; '.join(
            ['it now changes %s' % ', '.join(came[:5])] * bool(came)
            + ['it no longer changes %s' % ', '.join(gone[:5])] * bool(gone))))
    moved = sorted(p for p in old if old[p] != new[p])
    old_patch, new_patch = _patch(run, old_base, reviewed), _patch(run, new_base, tip)
    if old_patch != new_patch:
        raise _No('%s in %s (main changed lines within %d of its changes)'
                  % (said, ', '.join(moved[:5]) or 'its patch', CONTEXT))
    for p in moved:
        modes = [side[0] for side in old[p] + new[p] if side]
        if any(m not in FILE_MODES for m in modes):
            raise _No('%s: %s is not a plain file on both sides and main changed it' % (said, p))
    if moved and any(line.startswith(BINARY_MARKS) for line in new_patch):
        raise _No('%s: it holds a binary file and main changed %s, so the two cannot be compared'
                  % (said, ', '.join(moved[:5])))
    return hashlib.sha256(b'\n'.join(new_patch)).hexdigest()
