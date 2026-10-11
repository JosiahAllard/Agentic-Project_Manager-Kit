"""The trailers rule: which branch commits lack an agent's Co-Authored-By trailer, and which of those
stop a landing (from the PM kit's tools/gate.py, `branch_commits` and `classify_commit`; moved here
so review_pack judges commits by a rule it owns, and a later gate step can call the same one).

A value counts only in the address form the commit rule names (`<name> <noreply@anthropic.com>`),
and only where git's own trailer parser finds it (the message's last paragraph). A branch commit
made since TRAILER_FAIL_SINCE (committer date, UTC) without one FAILs; an older one could only be
fixed by a rewrite, so it is listed and does not fail.

An owner claim - `Owner-Approved: <path>`, or `Owner-Authored: yes` - excuses a commit
only when its AUTHOR email is the owner's (`kitlib.config.owner.Rules.is_owner`). Anyone can type a
trailer; with "owner".email unset nothing is the owner's. A claim on any other commit is an
unproven claim: `unproven_claim()` says why, and the commit is judged as if it carried no claim.
"""
import calendar
import collections
import re

from kitlib.git import cmd as gitcmd
from kitlib.git import refs as gitrefs

AGENT_TRAILER = re.compile(r'^\S.*\s<noreply@anthropic\.com>$')
TRAILER_FAIL_SINCE = calendar.timegm((2026, 10, 2, 0, 0, 0))
OWNER_APPROVED = 'Owner-Approved'
OWNER_AUTHORED = 'Owner-Authored'
OWNER_AUTHORED_VALUE = 'yes'

# One branch commit as the rule reads it: the Co-Authored-By, Owner-Approved and Owner-Authored
# values git's trailer parser finds, `committed` the committer date in epoch seconds (or None), and
# `author` the author email.
BranchCommit = collections.namedtuple(
    'BranchCommit', 'hash merge subject coauthors approved committed authored author')

TRAILERED, LANDER, OWNER, OLD, MISSING, UNDATED = (
    'trailered', 'lander-merge', 'owner-approved', 'old', 'missing', 'undated')
FAILED_VERDICTS = frozenset((MISSING, UNDATED))

_SEP, _END = '\x1f', '\x1e'


def log_format(approved_key=OWNER_APPROVED):
    """The `git log --format` parse_log() reads; `approved_key` is the owner's approval trailer."""
    return _SEP.join(('%H', '%P', '%ct', '%ae', '%(trailers:key=Co-Authored-By,valueonly)',
                      '%(trailers:key=' + approved_key + ',valueonly)',
                      '%(trailers:key=' + OWNER_AUTHORED + ',valueonly)', '%s')) + _END


def _values(text):
    return [v.strip() for v in text.splitlines() if v.strip()]


def parse_log(out):
    """[BranchCommit] from the text of `git log --format=<log_format()>`. The subject goes last, so a
    separator in it is harmless."""
    commits = []
    for record in out.split(_END):
        record = record.strip('\n')
        if not record:
            continue
        h, parents, ct, author, coauthors, approved, authored, subject = record.split(_SEP, 7)
        try:
            committed = int(ct.strip())
        except ValueError:
            committed = None
        commits.append(BranchCommit(h, len(parents.split()) > 1, subject, _values(coauthors),
                                    _values(approved), committed, _values(authored),
                                    author.strip()))
    return commits


def branch_commits(base, root, head='HEAD', remote='origin', approved_key=OWNER_APPROVED):
    """`([BranchCommit], None)` for every commit `head` has that `base` does not, merges included;
    or `(None, why)`. `<head> --not <base>`, not `merge-base..<head>`: a base commit brought in by a
    merge never counts. `refs/remotes/<remote>/<base>` is excluded too when it resolves."""
    for rev in (base, head):
        why = gitrefs.bad_rev(rev)
        if why:
            return None, why
    exclude = [base]
    if not base.startswith('refs/'):         # a full ref has no tracking ref of its own
        tracking = 'refs/remotes/%s/%s' % (remote, base)
        # known=True: a git ended from outside is no answer, never "no tracking ref", which
        # counted the remote's newer base commits as the branch's (KIT-GIT-EXIT1-ANSWERS-2).
        got, why = gitrefs.resolve(tracking, root, peel=None, known=True)
        if got is None:
            return None, 'cannot read %s: %s' % (tracking, why)
        if got:
            exclude.append(tracking)
    r = gitcmd.call(['log', '--format=' + log_format(approved_key), head, '--not'] + exclude
                    + ['--'], root)
    if r.rc != 0:
        return None, r.err or '(git printed nothing on stderr, exit %s)' % r.rc
    return parse_log(gitcmd.text(r.out)), None


def claims_owner(c):
    """True when the commit carries an owner claim: any Owner-Approved value, or
    `Owner-Authored: yes` (the exact value commit.py writes; any other value claims nothing)."""
    return bool(c.approved) or any(v.lower() == OWNER_AUTHORED_VALUE for v in c.authored)


def unproven_claim(c, rules):
    """None when the commit makes no owner claim or its author is the owner; else why the claim
    proves nothing (the author is someone else, or the owner's identity is not usable)."""
    if not claims_owner(c) or rules.is_owner(c.author):
        return None
    problem = rules.identity_problem()
    if problem:
        return problem
    return 'authored by %s, not the owner' % (c.author or '?')


def classify(c, rules, exempt=(), since=None):
    """The rule for one BranchCommit. Pure. In order: a matching Co-Authored-By -> TRAILERED; a
    hash in `exempt` -> LANDER; an owner claim on a commit the owner authored -> OWNER; no
    readable committer date -> UNDATED (fail closed); older than `since` -> OLD; else MISSING."""
    since = TRAILER_FAIL_SINCE if since is None else since
    if any(AGENT_TRAILER.match(v) for v in c.coauthors):
        return TRAILERED
    if c.hash in exempt:
        return LANDER
    if claims_owner(c) and rules.is_owner(c.author):
        return OWNER
    if c.committed is None:
        return UNDATED
    if c.committed < since:
        return OLD
    return MISSING


def label(c, verdict):
    """The row label: FAIL for a failing verdict; an OWNER commit with no Owner-Approved value is
    `owner-authored` (the `Owner-Authored: yes` marker alone); any other verdict as given."""
    if verdict in FAILED_VERDICTS:
        return 'FAIL'
    if verdict == OWNER and not c.approved:
        return 'owner-authored'
    return verdict
