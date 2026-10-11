"""Which unmerged branches change files in a given set (tools/branch_overlap.py;
docs/branch-overlap.md). Read-only: every git child is a read (for-each-ref, rev-list, merge-base,
diff, rev-parse); nothing fetches, writes a file or moves a ref.

The set is either paths the caller names, in the backlog scope language (a literal repo-relative
path or `<path>/**`), or the files one branch changed since its merge base with the base. A
branch's changes are `git diff --name-only --no-renames <merge base> <tip>`, so a rename counts
as both its old and its new path. A changed file overlaps a set member exactly when the backlog
pull collision check would call them colliding: kitlib.backlog.scope.members_collide, component by
component and case-blind, one a prefix of the other. A comparison that matcher cannot decide (a
changed file whose name holds a wildcard character) counts as an overlap, as it does there.

A branch is every ref under refs/heads/ and refs/remotes/ except a symbolic one (`origin/HEAD`);
it is merged, and skipped, when its tip is an ancestor of the base. Anything git gives no answer
for makes the whole answer CANNOT: "no overlap" is only said after every branch was read.
"""
import collections

from kitlib.backlog import scope as smod
from kitlib.config import ConfigError, load as load_config

from . import batch, cmd, refs

CLEAR, OVERLAP, CANNOT = 0, 1, 2

Outcome = collections.namedtuple('Outcome', 'code lines')
Branch = collections.namedtuple('Branch', 'ref name tip')     # full refname, short name, commit


class Usage(Exception):
    """A set or argument the caller must fix (the entry point's exit 2)."""


def normalise(member):
    """`member` as a scope member: backslashes read as slashes, a leading `./` dropped. Raises
    Usage when the result is not a legal scope member (the only wildcard is a trailing `/**`)."""
    m = member.strip().replace('\\', '/')
    while m.startswith('./'):
        m = m[2:]
    err = smod.scope_member_error(m)
    if err is None and m == smod.NONE:
        err = '`none` names no file'
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
    r = cmd.call(['for-each-ref', '--format=%(objectname) %(symref) %(refname)',
                  'refs/heads/', 'refs/remotes/'], root)
    if r.rc != 0:
        return None, 'git for-each-ref failed: %s' % (r.err or 'exit %s' % r.rc)
    out = []
    for line in cmd.text(r.out).splitlines():
        parts = line.split(' ', 2)
        if len(parts) != 3:
            continue
        sha, sym, ref = parts
        if sym:
            continue
        if not refs.is_sha(sha):
            return None, 'git for-each-ref printed %r, not a ref line' % line[:80]
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
        try:
            if smod.members_collide(path, m):
                hits.append((m, ''))
        except smod.UndecidableScope as exc:
            hits.append((m, 'undecidable, so counted: %s' % exc))
    return hits


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
            lines.append(note)
    why = refs.bad_rev(base)
    if why:
        raise Usage('--base: %s' % why)
    base_sha, why = refs.resolve(base, root)
    if not base_sha:
        return Outcome(CANNOT, lines + ['CANNOT ANSWER: base %s: %s' % (base, why)])
    lines.append('base      %s (%s)' % (base, base_sha[:10]))

    if branch is not None:
        why = refs.bad_rev(branch)
        if why:
            raise Usage('--branch: %s' % why)
        tip, why = refs.resolve(branch, root)
        if not tip:
            return Outcome(CANNOT, lines + ['CANNOT ANSWER: --branch %s: %s' % (branch, why)])
        members, why = changed_files(base_sha, tip, root)
        if members is None:
            return Outcome(CANNOT, lines + ['CANNOT ANSWER: --branch %s: %s' % (branch, why)])
        lines.append('set       %d file(s) %s changed since its merge base with %s'
                     % (len(members), branch, base))
    else:
        members = [normalise(f) for f in files]
        if not members:
            raise Usage('the file set is empty')
        lines.append('set       %d path(s) given' % len(members))
    if not members:
        lines.append('no overlap: the set is empty, so no branch can overlap it')
        return Outcome(CLEAR, lines)

    every, why = branches(root)
    if every is None:
        return Outcome(CANNOT, lines + ['CANNOT ANSWER: %s' % why])
    got, why = batch.ancestry(base_sha, root)
    if got is None:
        return Outcome(CANNOT, lines + ['CANNOT ANSWER: the ancestry of %s: %s' % (base, why)])
    merged_set = got[1]
    excluded = set()
    if branch is not None:
        own = stem(_full_ref(branch, every) or branch)
        excluded = {b.ref for b in every if b.tip == tip or stem(b.ref) == own}

    blocks, failed, merged, checked = [], [], 0, 0
    for b in every:
        if b.ref in excluded:
            continue
        if b.tip in merged_set:
            merged += 1
            continue
        checked += 1
        changed, why = changed_files(base_sha, b.tip, root)
        if changed is None:
            failed.append('CANNOT READ %s (%s): %s' % (b.name, b.tip[:10], why))
            continue
        hit_lines = []
        for path in changed:
            for m, note in matches(path, members):
                hit_lines.append('  %s  (matches %s%s)' % (path, m, '; ' + note if note else ''))
        if hit_lines:
            blocks.append(['%s (%s): %d overlapping' % (b.name, b.tip[:10], len(hit_lines))]
                          + hit_lines)
    for block in blocks:
        lines += block
    lines += failed
    summary = ('%d unmerged branch(es) checked, %d merged skipped%s'
               % (checked, merged, ', %d excluded as --branch itself' % len(excluded)
                  if branch is not None else ''))
    if failed:
        lines.append('CANNOT ANSWER: %d branch(es) could not be read; %s' % (len(failed), summary))
        return Outcome(CANNOT, lines)
    if blocks:
        lines.append('OVERLAP: %d branch(es) change files in the set; %s' % (len(blocks), summary))
        return Outcome(OVERLAP, lines)
    lines.append('no overlap: no unmerged branch changes a file in the set; %s' % summary)
    return Outcome(CLEAR, lines)


def _full_ref(rev, every):
    """The refname of `every` that `rev` names as given (full, short, or `heads/`-less), or None."""
    for b in every:
        if rev in (b.ref, b.name):
            return b.ref
    return None
