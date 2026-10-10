"""kitlib.trailerscheck: the gate step that fails a branch holding a commit with no agent
Co-Authored-By trailer; docs/repo-checks.md "trailers".

The rule is kitlib.reviewpack.trailers (the one review_pack judges by); this module only reads the
branch's commits (`HEAD --not <base>`, merges included) and prints the verdicts. A commit made
since the rule's cutoff with no `Co-Authored-By: <name> <noreply@anthropic.com>` FAILs; an older
one, or one the owner authored and claims (`Owner-Approved:` / `Owner-Authored: yes`), is a
WARNING. The owner's identity is the base's `owner` section alone, so a branch cannot name itself
the owner.

`merged` (a landing's merged-tree gate, HEAD on the main branch): the first-parent merges in
`<base>..HEAD` are the lander's own and are not branch commits. On any other branch they are the
branch's sync merges, so `merged` there is refused, never read as an exemption.
"""
import collections

from kitlib.config import ConfigError, load as load_config, load_at
from kitlib.config import owner as ownermod
from kitlib.git import cmd as gitcmd
from kitlib.git import refs as gitrefs
from kitlib.reviewpack import trailers

OK, FAILED, REFUSED = 0, 1, 2
RECOVERY = ('never amend or rewrite it: redo the work on a fresh branch from the base through '
            'tools/commit.py, and report it to the conductor')

Outcome = collections.namedtuple('Outcome', 'code lines')


def lander_merges(base, root):
    """`(set of hashes, None)`: the first-parent merges in `<base>..HEAD`; or `(None, why)`."""
    r = gitcmd.call(['rev-list', '--first-parent', '--merges', '%s..HEAD' % base, '--'], root)
    if r.rc != 0:
        return None, r.err or '(git printed nothing on stderr, exit %s)' % r.rc
    return set(gitcmd.text(r.out).split()), None


def _on_main(root, main):
    r = gitcmd.call(['symbolic-ref', '-q', 'HEAD'], root)
    ref = gitcmd.text(r.out).strip() if r.rc == 0 else ''
    return ref == 'refs/heads/%s' % main, ref or 'detached'


def check(root, base=None, cfg=None, merged=False, since=None):
    """Outcome(code, lines) for the checkout `root`. `base` None is the default base."""
    cfg = cfg if cfg is not None else load_config(root)
    lines = []
    if base is None:
        base, note = gitrefs.default_base(root, cfg.remote, cfg.main_branch)
        if note:
            lines.append(note)
    why = gitrefs.bad_rev(base)
    if why:
        return Outcome(REFUSED, lines + ['trailers: NOT CHECKED: %s' % why])
    if merged:
        on_main, head = _on_main(root, cfg.main_branch)
        if not on_main:
            return Outcome(REFUSED, lines + [
                "trailers: REFUSED - --merged exempts the lander's merges and holds only on %s "
                "itself; HEAD is %s. On a branch, run without --merged." % (cfg.main_branch, head)])
    try:
        rules = ownermod.merge(load_at(root, 'HEAD').owner_rules(),
                               load_at(root, base).owner_rules())
    except ConfigError as e:
        return Outcome(REFUSED, lines + ['trailers: refused: kit configuration: %s' % e])
    commits, err = trailers.branch_commits(base, root, head='HEAD', remote=cfg.remote,
                                           approved_key=rules.trailer)
    if commits is None:
        return Outcome(REFUSED, lines + [
            'trailers: `git log HEAD --not %s` failed - the branch range was not read, so it '
            'cannot be reported as trailered. git said: %s' % (base, err)])
    exempt = set()
    if merged:
        exempt, err = lander_merges(base, root)
        if exempt is None:
            return Outcome(REFUSED, lines + [
                "trailers: `git rev-list --first-parent --merges %s..HEAD` failed - the lander's "
                "merges could not be told apart from the branch's commits. git said: %s"
                % (base, err)])
    judged = [(c, trailers.classify(c, rules, exempt, since)) for c in commits]
    lacking = [(c, v) for c, v in judged if v not in (trailers.TRAILERED, trailers.LANDER)]
    failed = [v for _c, v in lacking if v in trailers.FAILED_VERDICTS]
    lines.append('trailers: %d commit(s) on this branch and not on %s, %d without a Co-Authored-By '
                 'trailer (%d failing)%s'
                 % (len(commits), base, len(lacking), len(failed),
                    '; %d lander merge(s) exempt' % sum(v == trailers.LANDER for _c, v in judged)
                    if merged else ''))
    for c, v in lacking:
        head = '%s %s "%s" has no Co-Authored-By trailer' % (
            c.hash[:10], 'merge' if c.merge else 'commit', c.subject[:60])
        if v == trailers.MISSING:
            lines.append('FAILED: %s - %s%s' % (head, RECOVERY, ' (sync merges go through '
                                                'tools/sync_base.py)' if c.merge else ''))
        elif v == trailers.UNDATED:
            lines.append('FAILED: %s and git gave no readable committer date, so it cannot be '
                         'shown to predate the rule - %s' % (head, RECOVERY))
        elif v == trailers.OWNER:
            lines.append('WARNING: %s - %s; name it in your report'
                         % (head, 'owner-authored' if not c.approved
                            else 'carries Owner-Approved from the owner'))
        else:
            lines.append('WARNING: %s - made before the rule (never amend or rewrite it); name it '
                         'in your report' % head)
        claim = trailers.unproven_claim(c, rules)
        if claim:
            lines.append('WARNING: %s carries an owner claim that proves nothing (%s) - judged as '
                         'an agent commit' % (c.hash[:10], claim))
    return Outcome(FAILED if failed else OK, lines)
