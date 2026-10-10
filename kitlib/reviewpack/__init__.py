"""Everything a reviewer or lander needs about a branch, in one read-only call (from the PM kit's
tools/review_pack.py; docs/review-pack.md).

`pack(path, branch, base, diff)` is the whole command; tools/review_pack.py parses, calls it, prints
`Outcome.lines` and exits with `Outcome.code`. It never prints and never exits.

The pack: base and merge-base, whether the branch contains the base tip, commits, files changed
with +/- grouped by the risk level in docs/ops/risk-map.md (unlisted = High), owner-only paths
touched and whether an Owner-Approved trailer covers them, the branch's commits with no
Co-Authored-By trailer (when configured, below), and the doc-budget verdict. `diff` set: only the
diff of that one file against the merge-base.
GOTCHA: `git diff <main> -- <path>` from the main checkout diffs the main branch against the main
worktree and prints nothing; the scope check of a branch is this (a merge-base diff).

The checkout is the one `path` is in (kitlib.git.repo.toplevel); every git read runs there. The
base defaults to `.kit.json` "repo".main_branch as the working tree holds it.

Owner-only rows read OK (approved), MISSING or UNMATCHED (both block), or REVERTED: an unapproved
edit the branch itself takes back, printed so a reviewer sees the round-trip but not counted as a
problem, because the path is byte-identical to the merge base. The rules are the base's `owner`
section merged with the branch's (kitlib.config.owner.merge): a branch cannot unlock a path, and
the owner's email comes from the base alone. A trailer approves only on a commit whose AUTHOR
email is the owner's; with "owner".email unset every trailer is unverified and every owner-only
change blocks.

`.kit.json` "review_pack" as the BASE holds it (a branch cannot switch its own check off):
"coauthor" (default false) lists the branch's commits with no Co-Authored-By trailer, each with
the verdict of the trailers rule (kitlib.reviewpack.trailers), and a commit that rule fails stops
the landing. An Owner-Approved or `Owner-Authored: yes` trailer excuses a commit only when the
owner authored it; on anyone else's commit it is printed as an unproven claim and the commit is
judged as if it carried none.

The doc budget runs as `<python> <kit>/tools/doc_budget.py -C <checkout> --base <base> --head
<branch>`, judged by kitlib.verdict (exit 0, a DOC BUDGET OK line, no DOC BUDGET FAILED). A kit
with no tools/doc_budget.py prints NOT CHECKED: nothing was measured, so it must not read green,
and nothing failed, so the exit status is unchanged.

The doc index check runs next to it as `<python> <kit>/tools/doc_index.py -C <checkout> --head
<branch> --config-ref <base>` (the index checks doc_budget no longer makes), judged by
kitlib.verdict on `DOC INDEX OK` / `DOC INDEX FAILED` and printed as a `doc index` line; a failure
fails the pack like a failing doc budget. The BASE's `.kit.json` "doc_index".enabled false prints
no line and runs nothing; the tool reads every doc index setting at the base too, so a branch's own
`.kit.json` can neither switch the check off nor rename the index it checks. commit and preflight
still read the working tree's: they are advisory and gate no landing; this pack is the landing check.

Exit codes: OK 0 nothing to stop a landing; FAILED 1 an owner-only trailer problem, a commit the
trailers rule fails, a failing doc budget or a failing doc index listing; USAGE 2 no merge-base, a
git read that failed, or an unusable kit configuration.
"""
import collections
import os
import re
import subprocess
import sys

from kitlib.config import ConfigError, load as load_config, load_at
from kitlib.config import owner as ownermod
from kitlib.git import cmd as gitcmd
from kitlib.git import refs as gitrefs
from kitlib.git import repo as gitrepo
from kitlib import verdict
from kitlib.docs import callers as doc_callers

from . import trailers

TOOL = 'review_pack'
OK, FAILED, USAGE = 0, 1, 2
SECTION = 'review_pack'
RISK_MAP = 'docs/ops/risk-map.md'
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOC_BUDGET = os.path.join(KIT_ROOT, 'tools', 'doc_budget.py')
DOC_BUDGET_TIMEOUT = 600      # seconds
DOC_INDEX = doc_callers.tool(KIT_ROOT)
TRAILER = ownermod.TRAILER
norm = ownermod.norm
PASSING = ('ok', 'reverted')
REVERTED = ('reported, does not block - no approval on this commit, but the branch reverts it: '
            'the path is byte-identical to the merge base, so nothing unapproved reaches the base')
QUOTEPATH = ('-c', 'core.quotepath=false')

Outcome = collections.namedtuple('Outcome', 'code lines')


class Stop(Exception):
    """A read the pack cannot go on without failed: `line` says which, and the pack exits 2."""

    def __init__(self, line):
        Exception.__init__(self, line)
        self.line = line


def _shown(args):
    return ' '.join(a for a in args if a not in QUOTEPATH)


def git(root, *args, strip=True):
    """stdout text, or Stop naming the call, its exit code and first stderr line. Every read
    through here is one whose empty answer is a real answer (no commits, no changed files), so a
    failed call must never come back as ''."""
    r = gitcmd.call(QUOTEPATH + args, root)
    if r.rc != 0:
        first = (r.err or '').strip().splitlines()
        if r.rc is None:
            raise Stop('%s: `git %s` did not run: %s' % (TOOL, _shown(args), r.err))
        raise Stop('%s: `git %s` failed (exit %d): %s' % (
            TOOL, _shown(args), r.rc, first[0] if first else '(nothing on stderr)'))
    out = gitcmd.text(r.out)
    return out.strip() if strip else out


def git_answer(root, *args):
    """git()'s stdout for a read whose empty answer is NO answer (`rev-parse --short`)."""
    out = git(root, *args)
    if not out:
        raise Stop('%s: `git %s` exit 0 but printed nothing' % (TOOL, _shown(args)))
    return out


def _is_shallow(root):
    r = gitcmd.call(('rev-parse', '--is-shallow-repository'), root)
    return r.rc == 0 and gitcmd.text(r.out).strip() == 'true'


def merge_base(root, base, branch):
    """`(full hash, None)`, or `(None, why)` naming git's exit code and first stderr line. Exit 1
    with nothing on stderr is both git's "no common history" and a git ended from outside."""
    args = ('merge-base', base, branch)
    r = gitcmd.call(args, root)
    out = gitcmd.text(r.out).strip()
    stderr = (r.err or '').strip().splitlines()
    if r.rc == 0 and out:
        return out, None
    if r.rc == 0:
        return None, '`git %s` exit 0 but printed nothing' % ' '.join(args)
    if r.rc is None:
        return None, '`git %s` did not run: %s' % (' '.join(args), r.err)
    if not stderr:
        why = ('`git %s` exit %d, nothing on stderr: no common history, or a git ended '
               'from outside' % (' '.join(args), r.rc))
        if r.rc == 1 and _is_shallow(root):
            why += ('; this clone is shallow, so the two sides may share a commit it did not '
                    'fetch: `git fetch --unshallow`, or fetch both to one depth')
        return None, why
    return None, '`git %s` exit %d: %s' % (' '.join(args), r.rc, stderr[0])


# --- risk levels --------------------------------------------------------------------------------

def risk_rules(root):
    """[(level, prefix)] from the working tree's docs/ops/risk-map.md (`- Level ... | paths`
    lines); [] when there is none."""
    p = os.path.join(root, *RISK_MAP.split('/'))
    if not os.path.exists(p):
        return []
    with open(p, encoding='utf-8', errors='replace') as fh:
        return rules_from_lines(fh, lambda tok: os.path.isdir(os.path.join(root, tok)))


def rules_from_lines(lines, isdir):
    """risk_rules over any risk-map text: `isdir(token)` says whether a bare word (no `/`, no `.`)
    names a folder. Pure."""
    out = []
    for line in lines:
        m = re.match(r'^- \**(High|Medium|Low)\b[^|]*\|(.*)$', line)
        if not m:
            continue
        for tok in _TOKEN.findall(m.group(2)):
            if '/' in tok or '.' in tok or isdir(tok):
                out.append((m.group(1), tok.rstrip('/')))
    return out


# A risk-map path: word characters, `.`, `-`, `*`, `?` and `{a,b}` groups, joined by `/`.
_PIECE = r'(?:[\w.*?\-]|\{[\w.*?\-,/]*\})+'
_TOKEN = re.compile(_PIECE + '(?:/' + _PIECE + ')*/?')


def _is_glob(rule):
    return any(c in rule for c in '*?{')


def _glob_regex(rule):
    """The regex for a glob rule: `*` and `?` stay inside one path segment, `**` crosses `/`,
    `{a,b}` is a choice. A rule with no `/` matches a name at any depth. The match is of whole
    segments, then anything below (like a directory prefix)."""
    out, depth, i = [], 0, 0
    while i < len(rule):
        c = rule[i]
        if rule.startswith('**/', i):
            out.append('(?:.*/)?')
            i += 3
            continue
        if c == '*':
            if rule.startswith('**', i):
                out.append('.*')
                i += 2
                continue
            out.append('[^/]*')
        elif c == '?':
            out.append('[^/]')
        elif c == '{':
            depth += 1
            out.append('(?:')
        elif c == '}' and depth:
            depth -= 1
            out.append(')')
        elif c == ',' and depth:
            out.append('|')
        else:
            out.append(re.escape(c))
        i += 1
    out.append(')' * depth)
    lead = '' if '/' in rule else '(?:.*/)?'
    return re.compile(lead + ''.join(out) + '(?:/.*)?$')


_GLOBS = {}


def _matches(path, rule):
    """(matched, specificity): the longer the literal text a rule names, the more specific."""
    if _is_glob(rule):
        rx = _GLOBS.get(rule)
        if rx is None:
            rx = _GLOBS[rule] = _glob_regex(rule)
        return bool(rx.match(path)), len(re.sub(r'[*?{},]', '', rule))
    if path.startswith(rule):
        return True, len(rule)
    if '/' not in rule and '.' in rule and path.rsplit('/', 1)[-1] == rule:
        return True, len(rule)  # a bare file name names that file at any depth
    return False, 0


def level(path, levels):
    """The risk level of `path`: the most specific matching rule (a plain rule is a path prefix; a
    glob is read by _glob_regex); unlisted is High, except a `.md`."""
    best, best_len = 'unlisted (= High)', 0
    for lv, rule in levels:
        hit, size = _matches(path, rule)
        if hit and size > best_len:
            best, best_len = lv, size
    if path.endswith('.md') and best_len == 0:
        return 'Low'
    return best


# --- owner-only paths ---------------------------------------------------------------------------

def owner_rules(root, base, branch):
    """The rules a landing is judged by: the branch's `owner` section merged with the base's, the
    owner's identity from the base alone. ConfigError when either cannot be read."""
    return ownermod.merge(load_at(root, branch).owner_rules(), load_at(root, base).owner_rules())


def trailers_of(message, key=TRAILER):
    """The `<key>:` values (Owner-Approved unless "owner".trailer names another) in a commit
    message, in order, from its LAST PARAGRAPH only (git's trailer convention): a body sentence
    that quotes the phrase is prose, not a trailer."""
    out = []
    lead = key + ':'
    for line in message.rsplit('\n\n', 1)[-1].splitlines():
        line = line.strip()
        if line.startswith(lead):
            out.append(line[len(lead):].strip().replace('\\', '/'))
    return out


def owner_approval_rows(commits, rules):
    """[(sha, changed paths, Owner-Approved values, not_owner)] -> [(sha, status, subject, detail)].

    MISSING: a locked path this commit changes that no trailer OF ITS OWN covers (approval is per
    commit), or that a trailer covers on a commit `not_owner` says is not provably the owner's.
    UNMATCHED: a trailer naming a path this commit does not change. Each locked path needs its own
    cover."""
    rows = []
    key = rules.trailer
    for sha, paths, values, not_owner in commits:
        for path in paths:
            if not rules.is_locked(path):
                continue
            got = rules.approved_by(path, values)
            if got and not_owner:
                rows.append((sha, 'MISSING', path, 'do not land - %s %s is on a commit %s'
                             % (key, got, not_owner)))
            elif got:
                rows.append((sha, 'ok', path, '%s %s' % (key, got)))
            else:
                rows.append((sha, 'MISSING', path, 'do not land - trailers on this commit: %s'
                             % (', '.join('%s %s' % (key, t) for t in values) or 'none')))
        for value in values:
            if not rules.trailer_matches(value, paths):
                rows.append((sha, 'UNMATCHED', '%s %s' % (key, value),
                             'do not land - this commit changes no such path%s'
                             % (' (it changes: %s)' % ', '.join(paths[:6]) if paths else '')))
    return rows


def revert_downgrade(row, net):
    """A MISSING row for a locked path the branch AS A WHOLE does not change (not in `net`, the
    normalised merge-base diff) becomes a non-blocking `reverted` row. The row is still printed with
    the sha that made the edit; only the exit status changes. A branch that edits, reverts and
    edits again still blocks."""
    sha, status, subject, detail = row
    if status == 'MISSING' and norm(subject) not in net:
        return (sha, 'reverted', subject, REVERTED)
    return row


def commit_paths(root, sha):
    """The paths a MERGE commit authored: those in its own `--cc` patch. `--name-only` on a merge
    lists every file differing from all parents, which a clean merge of two sides that edited one
    file produces while authoring nothing; an empty `--cc` patch is "this merge authored nothing"."""
    patch = git(root, 'show', '-1', '--format=', '--cc', '--no-renames', sha)
    # Hunk lines are shifted by one marker per parent, so a `diff --cc` at column zero is a header.
    return re.findall(r'^diff --(?:cc|combined) (.+)$', patch, re.M)


def range_commits(root, rng):
    """[(sha, is_merge, author email, message, names)] for every commit in `rng`, newest first, from
    one `git log`. A merge's names are empty: its paths are commit_paths()."""
    out = []
    text = git(root, 'log', '--no-renames', '--name-only',
               '--format=%x00%H %P%x01%ae%x01%B%x01', rng, '--', strip=False)
    for chunk in text.split('\x00')[1:]:
        head, _, rest = chunk.partition('\x01')
        author, _, rest = rest.partition('\x01')
        body, _, names = rest.rpartition('\x01')
        sha, _, parents = head.partition(' ')
        out.append((sha, len(parents.split()) > 1, author.strip(), body.strip(),
                    [p for p in names.split('\n') if p]))
    return out


def owner_only_section(root, base, branch, mb, rules):
    """`(changed locked paths, rows)` in three layers:

    1. every non-merge commit in base..branch that changes a locked path or carries a trailer,
       one commit at a time, so no commit's trailer covers another's edit;
    2. every merge commit, over the paths its own `--cc` patch authored: a merge's own edit of a
       locked path needs its own trailer, on the merge;
    3. the backstop: a locked path in the merge-base diff neither layer reported.

    Rows from layers 1 and 2 pass through revert_downgrade(). The merge-base diff is read with
    --no-renames: with rename detection `git mv CLAUDE.md notes.md` prints only `notes.md`, and the
    removal of the owner's file would read as reverted. Which paths are locked is decided by
    rules.is_locked() on every path, never a git pathspec (those match case-sensitively)."""
    rng = '%s..%s' % (base, branch)
    identity = rules.identity_problem()
    simple, merges = [], []
    for sha, merge, author, body, names in range_commits(root, rng):
        values = trailers_of(body, rules.trailer)
        paths = commit_paths(root, sha) if merge else names
        if not values and not any(rules.is_locked(p) for p in paths):
            continue
        not_owner = None if rules.is_owner(author) else (
            'whose author cannot be verified: %s' % identity if identity
            else 'authored by %s, not the owner' % (author or '?'))
        (merges if merge else simple).append((sha[:7], paths, values, not_owner))
    changed = [p for p in git(root, 'diff', '--name-only', '--no-renames', mb, branch,
                              '--').splitlines() if rules.is_locked(p)]
    net = {norm(p) for p in changed}
    rows = [revert_downgrade(row, net) for row in owner_approval_rows(simple, rules)]
    rows += [revert_downgrade((sha, status, subject,
                               'the merge commit itself changes this - %s' % detail), net)
             for sha, status, subject, detail in owner_approval_rows(merges, rules)]
    accounted = {norm(subject) for _, status, subject, _ in rows if status != 'UNMATCHED'}
    for path in changed:
        if norm(path) not in accounted:
            rows.append(('-', 'MISSING', path,
                         'do not land - in the diff, but no commit in %s changes it' % rng))
    return changed, rows


# --- header, co-authors, docs --------------------------------------------------------------------

def tips(root, base, branch, mb):
    """(base short hash, branch short hash, the base tip is in the branch) from one `git log
    --no-walk=unsorted` (the base tip is in the branch exactly when it is the merge-base `mb`). A
    failed or odd answer falls back to one read per rev, each through git_answer(): a read that
    fails or prints nothing stops the pack naming it, so no answer comes from a read that failed.
    GOTCHA: TOOLS-REVIEWPACK-PERREV-FLAKE - a git ended from outside (a stale `taskkill /PID`
    that reached a reused pid, kitlib.proc.shield's GOTCHA) exits 1 having printed nothing, the
    same shape as `merge-base --is-ancestor`'s NO. The fallback asked that read, so a killed one
    printed a NO git never said; it now compares the base's full hash with `mb`, as the log does."""
    r = gitcmd.call(['log', '--no-walk=unsorted', '--format=%H %h', base, branch, '--'], root)
    lines = gitcmd.text(r.out).strip().splitlines() if r.rc == 0 else []
    pairs = [l.split(' ', 1) for l in lines if ' ' in l]
    if len(pairs) == 1:
        pairs = pairs * 2
    if len(pairs) == 2:
        (base_full, base_short), (_full, branch_short) = pairs
        return base_short, branch_short, base_full == mb
    base_full = git_answer(root, 'rev-parse', '--verify', '%s^{commit}' % base)
    return (git_answer(root, 'rev-parse', '--short', base),
            git_answer(root, 'rev-parse', '--short', branch), base_full == mb)


def coauthor_section(root, base, branch, remote, rules):
    """`(lines, failing count)`: the branch's commits with no Co-Authored-By trailer, each with
    its trailers-rule verdict, then a WARNING per unproven owner claim. Stop when the range cannot
    be read: an unread range must not read as "every commit trailered"."""
    commits, err = trailers.branch_commits(base, root, head=branch, remote=remote,
                                           approved_key=rules.trailer)
    if commits is None:
        raise Stop('%s: the Co-Authored-By range %s..%s could not be read: %s'
                   % (TOOL, base, branch, err))
    judged = [(c, trailers.classify(c, rules)) for c in commits]
    lacking = [(c, v) for c, v in judged if v != trailers.TRAILERED]
    failing = sum(v in trailers.FAILED_VERDICTS for _, v in lacking)
    lines = ['co-author  %d commit(s) | %d without a Co-Authored-By trailer | %d failing'
             % (len(commits), len(lacking), failing)]
    for c, v in lacking:
        lines.append('  %-7s %-14s %s "%s"' % (c.hash[:7], trailers.label(c, v),
                                               'merge' if c.merge else 'commit', c.subject[:60]))
    for c, _v in lacking:
        why = trailers.unproven_claim(c, rules)
        if why:
            lines.append('  WARNING: %s carries %s, an unproven owner claim (%s) - judged as an '
                         'agent commit' % (c.hash[:7], trailers.OWNER_APPROVED if c.approved
                                           else '%s: yes' % trailers.OWNER_AUTHORED, why))
    return lines, failing


def warnings_first(text):
    """doc_budget's output with its `WARNING:` lines moved to the top, the rest in order, so the
    quoted first lines carry them. Only the order changes. Pure."""
    lines = (text or '').splitlines()
    warn = [l for l in lines if 'WARNING:' in l]
    return '\n'.join(warn + [l for l in lines if 'WARNING:' not in l])


def docs_line(root, base, branch):
    """`(line, failed)`: the doc-budget verdict, or NOT CHECKED (not failed) with no checker."""
    if not os.path.isfile(DOC_BUDGET):
        return ('docs     NOT CHECKED - the kit has no tools/doc_budget.py, so nothing was '
                'measured'), False
    argv = [sys.executable, DOC_BUDGET, '-C', root, '--base', base, '--head', branch]
    try:
        res = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8',
                             errors='replace', timeout=DOC_BUDGET_TIMEOUT)
        rc, out = res.returncode, warnings_first((res.stdout or '') + (res.stderr or ''))
    except subprocess.TimeoutExpired:
        rc, out = 1, 'doc_budget.py did not finish in %d seconds' % DOC_BUDGET_TIMEOUT
    except OSError as e:
        rc, out = 1, 'doc_budget.py could not start: %s' % e
    failed = not verdict.judge(rc, out)[0]
    return verdict.line('docs', rc, out, width=8, keep=6, from_start=True), failed


def index_line(root, base, branch, base_cfg):
    """`(line, failed)`: the doc index check (the index checks doc_budget no longer makes) of the
    branch, `(None, False)` when the BASE's `.kit.json` sets "doc_index".enabled false, NOT CHECKED
    (not failed) with no checker."""
    if doc_callers.switched_off(base_cfg):
        return None, False
    if not os.path.isfile(DOC_INDEX):
        return doc_callers.not_checked(), False
    argv = [sys.executable, DOC_INDEX, '-C', root, '--head', branch, '--config-ref', base]
    try:
        res = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8',
                             errors='replace', timeout=doc_callers.TIMEOUT)
        rc, out = res.returncode, (res.stdout or '') + (res.stderr or '')
    except subprocess.TimeoutExpired:
        rc, out = 1, 'doc_index.py did not finish in %d seconds' % doc_callers.TIMEOUT
    except OSError as e:
        rc, out = 1, 'doc_index.py could not start: %s' % e
    return doc_callers.line(rc, out, width=8, keep=6)


# --- the command --------------------------------------------------------------------------------

def pack(path, branch, base=None, diff=None):
    """The whole command for the checkout `path` is in. Returns an Outcome; never prints."""
    lines = []
    try:
        return _pack(lines, path, branch, base, diff)
    except Stop as stop:
        lines.append(stop.line)
        return Outcome(USAGE, lines)


def _pack(lines, path, branch, base, diff):
    top, why = gitrepo.toplevel(path)
    if top is None:
        raise Stop('%s: refused: %s' % (TOOL, why))
    base_note = None
    try:
        if base is None:
            cfg = load_config(top)
            base, note = gitrefs.default_base(top, cfg.remote, cfg.main_branch)
            base_note = note
            if note and not diff:
                lines.append('%s: %s' % (TOOL, note))
    except ConfigError as e:
        raise Stop('%s: refused: kit configuration: %s' % (TOOL, e))
    for rev in (base, branch):
        if gitrefs.bad_rev(rev):
            raise Stop('%s: refused: %s' % (TOOL, gitrefs.bad_rev(rev)))
    mb, why = merge_base(top, base, branch)
    if not mb:
        note = ' [%s: %s]' % (TOOL, base_note) if diff and base_note else ''
        raise Stop('cannot find a merge-base for %s and %s (%s)%s' % (base, branch, why, note))
    if diff:
        lines.append(git(top, 'diff', mb, branch, '--', diff))
        return Outcome(OK, lines)
    try:
        base_cfg = load_at(top, base)
        coauthor = base_cfg.section(SECTION)['coauthor']
        rules = owner_rules(top, base, branch)
    except ConfigError as e:
        raise Stop('%s: refused: kit configuration: %s' % (TOOL, e))

    base_short, branch_short, tip_in = tips(top, base, branch, mb)
    lines.append('branch   %s @ %s' % (branch, branch_short))
    lines.append('base     %s @ %s | merge-base %s | contains base tip: %s' % (
        base, base_short, mb[:7], 'yes' if tip_in else 'NO - author merges it in and regates'))
    commits = git(top, 'log', '--oneline', '--no-merges', '%s..%s' % (base, branch),
                  '--').splitlines()
    lines.append('commits  %d (no merges)' % len(commits))
    lines.extend('           ' + c for c in commits[:40])

    levels = risk_rules(top)
    groups = {}
    for row in git(top, 'diff', '--numstat', mb, branch, '--').splitlines():
        add, rem, name = row.split('\t', 2)
        groups.setdefault(level(name, levels), []).append((name, add, rem))
    total = sum(len(v) for v in groups.values())
    lines.append('files    %d changed | %s' % (total, git(top, 'diff', '--shortstat', mb, branch,
                                                          '--')))
    for lv in ('High', 'unlisted (= High)', 'Medium', 'Low'):
        rows = groups.get(lv, [])
        if rows:
            lines.append('  %s (%d)' % (lv, len(rows)))
            lines.extend('    +%-5s -%-5s %s' % (add, rem, name) for name, add, rem in rows[:60])
            if len(rows) > 60:
                lines.append('    ... %d more' % (len(rows) - 60))

    changed, rows = owner_only_section(top, base, branch, mb, rules)
    bad = [row for row in rows if row[1] not in PASSING]
    if changed or rows:
        lines.append('owner-only  %d path(s) in the diff | %d trailer check(s) | %d problem(s)'
                     % (len(changed), len(rows), len(bad)))
        identity = rules.identity_problem()
        if identity:
            lines.append('  owner identity NOT VERIFIABLE: %s on %s - every Owner-Approved trailer '
                         'is unverified, so every owner-only change blocks' % (identity, base))
        lines.extend('  %-7s %-9s %s  %s' % (sha, status.upper(), subject, detail)
                     for sha, status, subject, detail in rows)

    co_failing = 0
    if coauthor:
        co_lines, co_failing = coauthor_section(top, base, branch, base_cfg.remote, rules)
        lines.extend(co_lines)
    docs, docs_failed = docs_line(top, base, branch)
    lines.append(docs)
    index, index_failed = index_line(top, base, branch, base_cfg)
    if index:
        lines.append(index)
    lines.append('next     python tools/review_pack.py %s --diff <path>' % branch)
    return Outcome(FAILED if (bad or docs_failed or index_failed or co_failing) else OK, lines)
