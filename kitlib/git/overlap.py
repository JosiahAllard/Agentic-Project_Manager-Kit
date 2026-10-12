"""Which unmerged branches change files in a given set (tools/branch_overlap.py;
docs/branch-overlap.md). Read-only: every git child is a read (for-each-ref, rev-list, merge-base,
diff, rev-parse); nothing fetches, writes a file or moves a ref.

The set is either paths the caller names, in the backlog scope language (a literal repo-relative
path or `<path>/**`), or the files one branch changed since its merge base with the base. A
branch's changes are `git diff --name-only --no-renames <merge base> <tip>`, so a rename counts
as both its old and its new path. A changed file overlaps a set member when the backlog pull
collision check would call them colliding: kitlib.backlog.scope.members_collide, component by
component and case-blind, one a prefix of the other. A comparison that matcher cannot decide
counts as an overlap, as it does there: a name holding a comma, a control character other than a
tab (a tab is an ordinary character, tab_ok=True), leading or trailing whitespace, a backslash or
a wildcard character (`*?[]{}`), or one starting with a drive letter (`c:x`). One difference: here
`none` (in any case) is the root file of that name, never the backlog's empty-scope sentinel, so
it is compared as a path.

A ref is every one under refs/heads/ and refs/remotes/ except a symbolic one (`origin/HEAD`); it
is merged, and skipped, when its tip is an ancestor of the base. Refs that carry one branch name
(`kit/x` and `origin/kit/x`) are one branch: counted once, reported in one block. Left out: the
base's own branch (the local `main` when the base is `origin/main`; it is named in a note when it
holds commits the base lacks) and, with --branch, every ref of that branch's name. Anything
git gives no answer for makes the whole answer CANNOT: "no overlap" is only said after every ref
was read.
"""
import collections
import os

from kitlib.backlog import scope as smod
from kitlib.config import ConfigError, load as load_config

from . import batch, cmd, refs
from . import repo as gitrepo

CLEAR, OVERLAP, CANNOT = 0, 1, 2

Outcome = collections.namedtuple('Outcome', 'code lines')
Branch = collections.namedtuple('Branch', 'ref name tip')     # full refname, short name, commit

_HIDE = 'x/'      # a component put before both sides so `none` is compared as a path


class Usage(Exception):
    """A set or argument the caller must fix (the entry point's exit 2)."""


def normalise(member):
    """`member` as a scope member: backslashes read as slashes, a leading `./` dropped. Raises
    Usage when the result is not a legal scope member (the only wildcard is a trailing `/**`).
    `none` in any case is the root file of that name here, not the backlog's empty-scope sentinel."""
    m = member.strip().replace('\\', '/')
    while m.startswith('./'):
        m = m[2:]
    if m.casefold() == smod.NONE:
        err = smod.path_shape_error(m)
    else:
        err = smod.scope_member_error(m)
    if err is not None:
        raise Usage('%r: %s' % (member, err))
    return m


def parse_list(text):
    """The members of a --files-from file: one per line; blank lines and lines starting with
    `#` are skipped. Raises Usage for an illegal member."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith('#'):
            out.append(normalise(s))
    return out


def work_tree(root):
    """`(top, None, None)` for the work tree holding directory `root`; else `(None, kind, why)`,
    kind 'usage' (not a directory, or in no git work tree: the caller's to fix) or 'cannot' (git
    could not name the work tree: no answer)."""
    if not os.path.isdir(root):
        return None, 'usage', '-C: %s is not a directory' % root
    place = gitrepo.classify(root)
    if place.kind == gitrepo.NONE:
        return None, 'usage', '-C: %s is in no git work tree (%s)' % (root, place.detail)
    if place.kind not in (gitrepo.MAIN, gitrepo.LINKED) or not place.top:
        return None, 'cannot', ('-C %s: git could not name its work tree (%s: %s)'
                                % (root, place.kind, place.detail))
    return place.top, None, None


def short_name(ref):
    for prefix in ('refs/heads/', 'refs/remotes/'):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def stem(ref):
    """The branch name a ref carries: `kit/x` for refs/heads/kit/x and refs/remotes/origin/kit/x."""
    if ref.startswith('refs/remotes/'):
        return ref[len('refs/remotes/'):].partition('/')[2]
    return short_name(ref)


def branches(root):
    """`([Branch], None)`: every local and remote-tracking branch, symbolic refs left out; or
    `(None, why)`."""
    what = 'for-each-ref refs/heads/ refs/remotes/'
    r = cmd.call(['for-each-ref', '--format=%(objectname) %(symref) %(refname)',
                  'refs/heads/', 'refs/remotes/'], root)
    if r.rc != 0:
        return None, 'git %s failed: %s' % (what, r.err or 'exit %s' % r.rc)
    # GOTCHA (as kitlib.git.refs.ref_commit): for-each-ref leaves a broken ref out with exit 0 and
    # "warning: ignoring broken ref <ref>" on stderr; skipping it quietly would hide a branch. So
    # stderr that names a ref, or says broken or ignoring, is no answer.
    if r.err and any(w in r.err for w in ('refs/', 'broken', 'ignoring')):
        return None, 'git %s said: %s' % (what, r.err.strip()[:200])
    out = []
    for line in cmd.text(r.out).splitlines():
        parts = line.split(' ', 2)
        if (len(parts) != 3 or not refs.is_sha(parts[0])
                or not parts[2].startswith(('refs/heads/', 'refs/remotes/'))):
            return None, 'git %s printed %r, not a ref line' % (what, line[:80])
        sha, sym, ref = parts
        if not sym:
            out.append(Branch(ref, short_name(ref), sha))
    return out, None


def changed_files(base, tip, root):
    """`([path], None)`: the files `tip` changed since its merge base with `base` (renames as
    their two paths); `(None, why)` when there is no merge base or git gives no answer."""
    mb, why = refs.merge_base(base, tip, root)
    if mb is None:
        return None, why
    if not mb:
        return None, 'no merge base with %s' % base
    data, why = cmd.run(['diff', '--name-only', '-z', '--no-renames', mb, tip, '--'], root)
    if data is None:
        return None, 'git diff %s %s: %s' % (mb[:10], tip[:10], why)
    return [p for p in cmd.text(data).split('\0') if p], None


def matches(path, members):
    """[(member, note)] of the set members `path` collides with; note is '' or why it counts."""
    hits = []
    for m in members:
        a, b = path, m
        if smod.NONE in (a.casefold(), b.casefold()):
            a, b = _HIDE + a, _HIDE + b
        try:
            if smod.members_collide(a, b):
                hits.append((m, ''))
        except smod.UndecidableScope as exc:
            hits.append((m, 'undecidable, so counted: %s' % exc))
    return hits


def _full_ref(rev, every):
    """The refname of `every` that `rev` names (full, short, `heads/x` or `remotes/o/x`), or None."""
    for b in every:
        if rev in (b.ref, b.name) or b.ref == 'refs/' + rev:
            return b.ref
    return None


def look(root, files=None, branch=None, base=None, cfg=None):
    """Outcome(code, lines) for the repository at `root`: CLEAR, OVERLAP or CANNOT. Exactly one of
    `files` (scope members) and `branch` (a rev naming a branch) is given. `base` None is the
    default base (kitlib.git.refs.default_base). Raises Usage for a bad set or argument."""
    if (files is None) == (branch is None):
        raise Usage('give exactly one of --files, --files-from and --branch')
    lines = []
    if base is None:
        try:
            c = cfg if cfg is not None else load_config(root)
        except ConfigError as exc:
            return Outcome(CANNOT, ['CANNOT ANSWER: kit configuration: %s' % exc])
        base, note = refs.default_base(root, c.remote, c.main_branch)
        if note:
            lines.append('note      %s' % note)
    why = refs.bad_rev(base)
    if why:
        raise Usage('--base: %s' % why)
    if branch is not None:
        why = refs.bad_rev(branch)
        if why:
            raise Usage('--branch: %s' % why)
    if files is not None:
        members = [normalise(f) for f in files]
        if not members:
            raise Usage('the file set is empty')
    base_sha, why = refs.resolve(base, root)
    if not base_sha:
        return Outcome(CANNOT, lines + ['CANNOT ANSWER: base %s: %s' % (base, why)])
    lines.append('base      %s (%s)' % (base, base_sha[:10]))
    every, why = branches(root)
    if every is None:
        return Outcome(CANNOT, lines + ['CANNOT ANSWER: %s' % why])

    excluded = {}                                       # ref -> why it is left out
    if branch is not None:
        tip, why = refs.resolve(branch, root)
        if not tip:
            return Outcome(CANNOT, lines + ['CANNOT ANSWER: --branch %s: %s' % (branch, why)])
        members, why = changed_files(base_sha, tip, root)
        if members is None:
            return Outcome(CANNOT, lines + ['CANNOT ANSWER: --branch %s: %s' % (branch, why)])
        lines.append('set       %d file(s) %s changed since its merge base with %s'
                     % (len(members), branch, base))
        # The branch's name: the ref it was given as, or the branch HEAD is on. Only refs of that
        # name (local and pushed copies, an older one included) are left out; another branch at
        # the same tip is checked. A bare commit names no branch: the branches at its tip are
        # taken as it, each named in a note.
        full = _full_ref(branch, every)
        if full is None and branch == 'HEAD':
            r = cmd.call(['symbolic-ref', '-q', 'HEAD'], root)
            on = cmd.text(r.out).strip() if r.rc == 0 else ''
            full = _full_ref(on, every) if on else None
        if full:
            own = {stem(full)}
        else:
            own = {stem(b.ref) for b in every if b.tip == tip}
            for name in sorted(own):
                lines.append('note      --branch %s names no branch; %s is at its tip, so its '
                             'refs are left out as --branch' % (branch, name))
        for b in every:
            if stem(b.ref) in own:
                excluded[b.ref] = '--branch'
    else:
        lines.append('set       %d path(s) given' % len(members))
    if not members:
        lines.append('no overlap: the set is empty, so no branch can overlap it')
        return Outcome(CLEAR, lines)

    got, why = batch.ancestry(base_sha, root)
    if got is None:
        return Outcome(CANNOT, lines + ['CANNOT ANSWER: the ancestry of %s: %s' % (base, why)])
    merged_set = got[1]
    base_full = _full_ref(base, every)
    if base_full:
        for b in every:
            if (b.ref != base_full and b.ref not in excluded and b.tip not in merged_set
                    and stem(b.ref) == stem(base_full)):
                excluded[b.ref] = 'base'
                lines.append('note      %s (%s) has commits the base %s lacks; it is the '
                                 "base's own branch, so it is left out and those commits are "
                                 'not checked' % (b.name, b.tip[:10], base))

    groups = collections.OrderedDict()                  # stem -> [(Branch, [hit line])]
    failed, cache = [], {}
    merged = checked = 0
    stems = set()
    for b in every:
        if b.ref in excluded:
            continue
        if b.tip in merged_set:
            merged += 1
            continue
        checked += 1
        stems.add(stem(b.ref))
        if b.tip not in cache:
            cache[b.tip] = changed_files(base_sha, b.tip, root)
        changed, why = cache[b.tip]
        if changed is None:
            failed.append('CANNOT READ %s (%s): %s' % (b.name, b.tip[:10], why))
            continue
        hit_lines = []
        for path in changed:
            for m, note in matches(path, members):
                hit_lines.append('  %s  (matches %s%s)' % (path, m, '; ' + note if note else ''))
        if hit_lines:
            groups.setdefault(stem(b.ref), []).append((b, hit_lines))

    for name, entries in groups.items():
        union = list(collections.OrderedDict.fromkeys(h for _, hl in entries for h in hl))
        lines.append('%s  [%s]: %d overlapping' % (
            name, ', '.join('%s %s' % (b.name, b.tip[:10]) for b, _ in entries), len(union)))
        for h in union:
            some = [b.name for b, hl in entries if h in hl]
            lines.append(h if len(some) == len(entries) else '%s  [only %s]' % (h, ', '.join(some)))
    lines += failed
    kinds = collections.Counter(excluded.values())
    summary = '%d unmerged branch(es) checked (%d ref(s)), %d merged ref(s) skipped' % (
        len(stems), checked, merged)
    if kinds['--branch']:
        summary += ', %d ref(s) of --branch itself left out' % kinds['--branch']
    if kinds['base']:
        summary += ", %d ref(s) of the base's own branch left out" % kinds['base']
    if failed:
        lines.append('CANNOT ANSWER: %d ref(s) could not be read; %s' % (len(failed), summary))
        return Outcome(CANNOT, lines)
    if groups:
        lines.append('OVERLAP: %d branch(es) change files in the set; %s' % (len(groups), summary))
        return Outcome(OVERLAP, lines)
    lines.append('no overlap: no unmerged branch changes a file in the set; %s' % summary)
    return Outcome(CLEAR, lines)
