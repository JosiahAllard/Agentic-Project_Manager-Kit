"""The write verbs as plans: `plan_<verb>(snap, ctx, ...) -> Change`.

A plan only decides. It reads the snapshot it is given, refuses (Usage, Refusal, NotFound,
Failure) or returns the records to write, and never touches a file, a ref or the screen. The
store runs it again after a lost race, so a plan is a pure function of the snapshot and its
arguments (plus the git probes it names: branch liveness, the `Context.liveness` probe of a
pulled branch's worktree, `requires: commit:`, and the diff of the branch `add --found-on` names).
"""
import datetime
import re

from kitlib import kitroot
from kitlib.git import cmd, refs
from kitlib import machinepaths

from . import conditions as condmod
from . import offer as omod
from . import policy
from . import record as rmod
from . import scope as smod
from .errors import Failure, NotFound, Refusal, Usage
from .store import PLAIN_ID, Change, add_plan_note, id_refusal

MIN_REASON = 12
RULED_PRICE_BY = ('owner', 'pm')
ADD_BODY = ('## Why this is here\n\nRecorded by `backlog.py add`. Unprioritised on purpose: '
            'prioritising it is a separate act by somebody not mid-discovery.\n')
# The longest `--summary` the CLI's `add` takes: the summary is the record's title, one line a
# list shows whole; the detail goes in the body (`--body-file`). The Action Hub's add is not held
# to it (opener.planner passes it; recordwrite.add does not).
SUMMARY_MAX = 120
# Keys only a verb writes, each with the verb that does.
WRITTEN_BY = {
    'id': 'nothing - the filename is the id; add the record under its new id and retire this one',
    'displaced': '`pull`, which computes it from the offer',
    'preempt': '`pull --reason`', 'preempt_by': '`pull --reason`', 'preempt_at': '`pull --reason`',
    'pulled_at': '`pull`', 'pulled_by': '`pull`',
    'q': '`set-status parked [--q Q-nnn]`', 'parked_at': '`set-status parked [--q Q-nnn]`',
    'review': '`add` from the Action Hub (the library\'s `review=`); a coordinator clears it with '
              '`clear-review ID --reason T`',
    'answer': 'the Action Hub, filing the owner\'s answer to a question bound to the record '
              '(the library\'s `plan_answer`)',
    'until': '`block ID --on M --until "<condition>"`; `unblock --on M` drops it',
}
# Keys no verb writes any more: an owner gate is a role (kitlib/backlog/roles.py), not a cited
# id. A record that carries one keeps it until `set ID --unset K` clears it (a price move drops
# `priority_ruling`, which named the old price; `uncite` drops `priority_ruling` and
# `hold_ruling`; `priority_per` is kept). In WRITTEN_BY too, so the Action Hub shows them as no
# edit's to make.
UNWRITTEN = ('priority_ruling', 'hold_ruling', 'resume', 'priority_per')
WRITTEN_BY.update((k, 'no verb: an owner gate is a role now (kitlib/backlog/roles.py); '
                      '`set ID --unset %s` clears it' % k)
                  for k in UNWRITTEN)


class Context(object):
    """What a plan needs beyond the snapshot. `branch` None: the branch checked out in `root`.
    `code_main` (None: `main_branch`) is the branch code lands on, which `add --found-on`
    measures a branch's diff from (`.kit.json` backlog.code_main), as last fetched from
    `code_remote` (None: `remote`; the main checkout's remote in the PM data worktree,
    opener.open_store). `liveness` (None: no probe) is `liveness(branch) -> (why it is live, None)
    or (None, tip)`: whether the worktree on a pulled branch is dead, so a pull its holder left
    may be blocked or released by anybody (stop_pull; opener.open_store passes
    kitlib.worktree.classify.branch_liveness, so this module imports nothing from worktree).
    `cap` (None: offer.DEFAULT_CAP; a callable: read on first use) is the most records `next` offers and `pull` takes in order
    (`.kit.json` backlog.cap, opener.repo_cap); `next --cap` overrides it for that call."""

    def __init__(self, store, root, remote='origin', main_branch='main', branch=None, today=None,
                 code_main=None, code_remote=None, liveness=None, cap=None):
        self.store = store
        self.root = root
        self.remote = remote
        self.main_branch = main_branch
        self.code_main = code_main or main_branch
        self.code_remote = code_remote or remote
        self.branch = branch
        self.today = today
        self.liveness = liveness
        self._cap = cap

    @property
    def cap(self):
        """The cap, a callable given for it called on first read (opener.open_store: so only an
        offer reads `.kit.json`, and a verb that makes none never fails on it)."""
        if callable(self._cap):
            self._cap = self._cap()
        return omod.DEFAULT_CAP if self._cap is None else self._cap

    @property
    def session(self):
        return self.store.session

    def date(self):
        return self.today or datetime.date.today().isoformat()

    def current_branch(self):
        if self.branch:
            return self.branch
        name, why = refs.current_branch(self.root)
        if name is None:
            raise Failure('cannot read the branch checked out in %s: %s' % (self.root, why))
        if not name:
            raise Failure('HEAD is detached in %s, so no branch can be named as the one that '
                          'pulls; pass `--as <branch>`' % self.root)
        return name

    def history(self, snap):
        return lambda rid: self.store.history(rid, snap)

    def unmet(self, snap):
        hist = self.history(snap)
        return lambda rec: omod.unmet_requires(rec, snap, hist, self.root, self.main_branch)


# --- shared ---------------------------------------------------------------------------------------

def require(snap, rid):
    rec = snap.get(rid)
    if rec is None:
        raise NotFound('no record with `id: %s` in %s' % (rid, snap.where))
    return rec


def require_shown(snap, ctx, rid):
    """require() for `show`: a missing id's NotFound also says which store commit removed it
    (sha, date, session, subject; never an author name), or that none ever did. A main store's
    record whose file HEAD tracks and the worktree no longer holds says so (closed or retired
    here - MainBackend writes commit nothing - or removed by hand). An id that is
    not plain (a glob or path character) is not looked up, and a history git cannot read (an
    unborn HEAD, a failing git) still leaves the plain NOT FOUND, with a line saying so."""
    rec = snap.get(rid)
    if rec is not None:
        return rec
    plain = 'no record with `id: %s` in %s' % (rid, snap.where)
    if not PLAIN_ID.match(rid):
        raise NotFound('%s\n  history not searched: `%s` is not a plain id' % (plain, rid))
    try:
        pending = ctx.store.uncommitted_removal(rid)
        gone = None if pending else ctx.store.removal(rid, snap)
    except Failure as exc:
        raise NotFound('%s\n  the store\'s history could not be read: %s' % (plain, exc))
    if pending:
        then = ('its file is tracked at HEAD but missing from this worktree (closed or retired '
                'here, or removed by hand); no commit names it yet')
    elif gone is None:
        then = 'no commit in the store\'s history removed it: the id is unknown'
    else:
        then = 'removed by %s on %s (session %s): %s' % (gone.sha, gone.when, gone.session or '-',
                                                       gone.subject)
    raise NotFound('%s\n  %s' % (plain, then))


def _text(pairs, body):
    """The record text of `pairs` and `body`. An `until` line whose member left `blocked_on` is
    dropped here (record.prune_until), so every write that clears a blocker drops its condition."""
    return rmod.render(rmod.prune_until(pairs), body)


def clears_owner_blocker(rec, after):
    return 'owner' in rec.all('blocked_on') and 'owner' not in after


def refuse_machine_paths(field, *texts):
    """Usage when free text carries a machine path: it reaches a record or a commit subject, and
    history cannot be fixed once it lands (commit.machine_path_error, same rule: the machine
    config's `machine_path_folders` and a user profile). Empty texts read no machine config. A
    machine config it cannot read searches user-profile paths only, and its WARNING ends the
    refusal or, with no refusal, is printed with the write (store.add_plan_note)."""
    if not any(texts):
        return
    folders, note = machinepaths.folders()
    found = []
    for t in texts:
        found += [f for f in machinepaths.find(t or '', folders) if f not in found]
    if not found:
        if note:
            add_plan_note(note)
        return
    raise Usage('refused: `%s` carries a machine path: %s. tools/machine_paths.py fails a '
                'file with one. Write %%VAR%%\\... (the variable that applies), or leave it out.'
                % (field, ', '.join(found)) + ('\n' + note if note else ''))


def reason_error(reason, flag):
    text = ' '.join((reason or '').split())
    if len(text) < MIN_REASON:
        return ('`%s` needs at least %d characters of reason: it is the only trace the change '
                'leaves' % (flag, MIN_REASON))
    return None


def own_branch(ctx, branch=None):
    """The branch whose own live pulls an offer leaves out, so a record overlapping only them is
    bundled onto it rather than passed over: `branch`, else `--as` (ctx.branch), else the branch
    checked out in ctx.root. None when that cannot be read, HEAD is detached, or it is the main
    branch or the code main: a main bundles nothing, so its pulls hold their scope. Compared
    exactly, as `pulled_by` is written: another letter case or a ref spelling is another branch."""
    if branch is None:
        branch = ctx.branch
    if not branch:
        branch, _why = refs.current_branch(ctx.root)
    if not branch or branch in (ctx.main_branch, ctx.code_main):
        return None
    return branch


def offer_for(snap, ctx, branch=None, cap=omod.DEFAULT_CAP, seeds=(), holding=None):
    """`(offers, passed, holding, own)`: the offer `next` prints for `ctx.session`, with the live
    pulls of `own` (own_branch) taking no scope in it; `holding` is still every live pull (read
    here unless the caller passes the live pulls it already read). A record whose scope folds
    into another live pull's branch (`_offer_folds`, the fold-in guard `pull` runs) is passed
    over, not offered. `next` and `pull` both build their offer here, so they cannot disagree on
    it."""
    offers, passed, holding, own = _offer(snap, ctx, branch, cap, seeds, holding)
    return offers, passed, holding, own


def _offer(snap, ctx, branch, cap, seeds, holding, diffs=None):
    """offer_for; `diffs` is the diff cache `_offer_folds` shares with current_offer's `also`."""
    own = own_branch(ctx, branch)
    if holding is None:
        holding, _dead, _why = omod.in_flight_split(snap, ctx.root, ctx.remote)
    taken = [r for r in holding if not (own and r.get('pulled_by') == own)]
    folded = _offer_folds(ctx, holding, own, diffs)
    result, refusals, _ranks = omod.next_offer(snap, taken, cap=cap, seeds=seeds,
                                               session=ctx.session, unmet=ctx.unmet(snap),
                                               folded=folded)
    if refusals:
        raise Failure('`next` cannot compute an offer: %s' % '; '.join(refusals))
    return result[0], result[1], holding, own


def current_offer(snap, ctx, branch=None):
    """`(offers, passed, holding, also, notes)` for a pull onto `branch`: the offer `next` prints
    as that branch (offer_for), and `also`, the records `next` offers without it (as the main
    checkout prints it), [] when the two are the same. A pull takes a record either one offers,
    so a coordinator's `next` and a worker's `pull` never disagree on what is in order.
    `also` only widens what a pull takes: when it cannot be computed (one of the branch's own
    pulls has a scope that cannot be compared, which the branch's offer never reads) it is [],
    so a record `next` offered as the branch still pulls plainly and nothing else is let in;
    `notes` then holds one NOTE naming why, for the pull to print."""
    diffs = []
    offers, passed, holding, own = _offer(snap, ctx, branch, ctx.cap, (), None, diffs)
    also, notes = [], []
    if own is not None and any(r.get('pulled_by') == own for r in holding):
        # The fold check `next` runs in the main checkout (no own branch, so the branch's own
        # diff counts too): `also` must be exactly that offer, or a record the main checkout
        # offers because a higher one folds into this branch would be refused here. The diffs
        # are read once for both offers (`diffs`).
        result, refusals, _ranks = omod.next_offer(snap, holding, cap=ctx.cap,
                                                   session=ctx.session,
                                                   unmet=ctx.unmet(snap),
                                                   folded=_offer_folds(ctx, holding, None, diffs))
        if refusals:
            notes.append('NOTE: the offer without branch %s cannot be computed (%s); only the '
                         'offer as %s was checked' % (own, '; '.join(refusals), own))
        else:
            also = [o.rec for o in result[0]]
    return offers, passed, holding, also, notes


def read_body_file(path):
    """The body `--body-file` would write: UTF-8, a BOM dropped, never empty."""
    try:
        with open(path, 'r', encoding='utf-8-sig') as fh:
            text = fh.read()
    except OSError as exc:
        raise Usage('cannot read `--body-file %s`: %s' % (path, exc))
    except UnicodeDecodeError as exc:
        raise Usage('`--body-file %s` is not UTF-8: %s' % (path, exc))
    if not text.strip():
        raise Usage('`--body-file %s` is empty; an empty body is refused rather than written' % path)
    return text.replace('\r\n', '\n')


# --- add ------------------------------------------------------------------------------------------

def plan_add(snap, ctx, rid, summary, scopes, effort=None, ref=None, reopen=False, session=None,
             found_on=None, why_not_folded=None, review=None, body=None, summary_max=None):
    """One unprioritised record. `priority: -` always: pricing is a separate act.

    `review` (`<source> <YYYY-MM-DD>`, the library's alone: the Action Hub passes it, the CLI has
    no flag) writes the review mark: `pull` refuses the record and `next` never offers it until a
    coordinator runs `clear-review` (plan_clear_review).

    The fold-in guard (docs/backlog.md, "The fold-in guard"): `found_on` is the branch the finding
    was made on, or `none`; `why_not_folded` says why it is not fixed there. A record whose scope
    overlaps that branch's diff from the code main (`ctx.code_main`), or the scope of a record the
    branch holds pulled, is refused: it belongs on that branch, or after it lands. Both go into a
    `## Found` body section, and the branch into the commit subject; the summary stays the title.

    `body` (`--body-file`) opens the body, before the `## Found` and ADD_BODY sections.
    `summary_max`: a longer summary is a usage error naming `--body-file` (the CLI passes
    SUMMARY_MAX; None, the Action Hub's add, takes any length)."""
    why = snap.rules.id_error(rid)
    if why is not None:
        raise Usage('cannot add: %s' % why)
    why = id_refusal(snap, rid)
    if why is not None:
        raise Refusal('cannot add: %s' % why)
    if session is not None and rmod._session(session) is not None:
        raise Usage('bad `--session %s`: %s' % (session, rmod._session(session)))
    if not (summary or '').strip():
        raise Usage('`add` requires a non-blank `--summary`')
    refuse_machine_paths('--summary', summary)
    if summary_max is not None and len(summary.strip()) > summary_max:
        raise Usage('`--summary` is %d characters; a summary is the record\'s title, at most %d: '
                    'keep it to one short line and put the detail in the body (`--body-file F`)'
                    % (len(summary.strip()), summary_max))
    refuse_machine_paths('--ref', ref)
    refuse_machine_paths('--body-file', body)
    found_on, not_folded = _fold_arguments(ctx, found_on, why_not_folded)
    if not scopes:
        raise Usage('`add` requires at least one `--scope`; `none` (no tracked files) is written out')
    for s in scopes:
        bad = smod.scope_member_error(s)
        if bad:
            raise Usage('bad `--scope %s`: %s' % (s, bad))
    if effort is not None and rmod._effort(effort) is not None:
        raise Usage('bad `--effort %s`: %s' % (effort, rmod._effort(effort)))
    if review is not None and rmod._review(review) is not None:
        raise Usage('bad review mark `%s`: %s' % (review, rmod._review(review)))
    prior = ctx.store.history(rid, snap)
    if prior and not reopen:
        raise Refusal('`%s` was already closed, retired or deleted: %s. Re-adding it under the '
                      'same id makes that history read as though it were about this item: pass '
                      '`--reopen` if it is the same work coming back, or pick a new id'
                      % (rid, prior))
    notes = [] if found_on == smod.NONE else _fold_overlap(snap, ctx, rid, found_on, scopes)
    where = 'no branch (or on a branch since landed)' if found_on == smod.NONE else found_on
    sections = [body.rstrip('\n') + '\n'] if (body or '').strip() else []
    sections += ['## Found\n\nFound on %s; not folded: %s\n' % (where, not_folded), ADD_BODY]
    summary = summary.strip()
    pairs = [('id', rid), ('area', smod.area_from_scope(scopes)), ('priority', '-'),
             ('priority_by', '-'), ('effort', effort or '-'), ('state', 'open'),
             ('blocked_on', '-')]
    if review is not None:
        pairs.append(('review', review))
    pairs += [('scope', s) for s in scopes]
    if session is not None:
        pairs.append(('session', session))
    if ref:
        pairs.append(('ref', ref))
    pairs.append(('summary', summary))
    lines = notes + ['ADDED %s, found on %s' % (rid, where)]
    lines += ['  %-11s %s' % (k + ':', v) for k, v in pairs]
    lines.append('Unprioritised: not in `next` until somebody prices it (`list --needs-priority`).')
    if review is not None:
        lines.append('Marked for review: not pullable until a coordinator runs `clear-review %s '
                     '--reason T`.' % rid)
    lines.append('  body:       %s`## Found`, `## Why this is here`'
                 % ('your `--body-file`, ' if len(sections) == 3 else ''))
    return Change({rid: _text(pairs, '\n'.join(sections))},
                  'backlog: add %s, found on %s' % (rid, where),
                  lines)


def _fold_arguments(ctx, found_on, why_not_folded):
    """`(found_on, the reason with its whitespace collapsed)`, or Usage. No git but the
    branch-name check."""
    if found_on is None:
        raise Usage('`add` requires `--found-on BRANCH|none`: the branch the finding was made on, '
                    'or `none` when it was found on no branch, or on a branch that landed and was removed')
    if why_not_folded is None:
        raise Usage('`add` requires `--why-not-folded TEXT`: why the finding is not fixed on the '
                    'branch that found it')
    text = ' '.join(why_not_folded.split())
    bad = reason_error(text, '--why-not-folded')
    if bad:
        raise Usage(bad)
    refuse_machine_paths('--why-not-folded', text)
    if found_on != smod.NONE:
        bad = branch_name_error(found_on, ctx.root)
        if bad:
            raise Usage('bad `--found-on`: %s; write a branch, or `none`' % bad)
        bare = _bare_branch(ctx, found_on)
        if bare.lower() == ctx.code_main.lower():
            raise Usage('`--found-on %s` names the code main: a finding on main has no branch: '
                        'write none' % found_on)
        if bare != found_on:
            raise Usage('`--found-on %s` is a ref, not a branch name: write the local branch '
                        'name `%s`' % (found_on, bare))
    return found_on, text


def _bare_branch(ctx, name):
    """`name` without a `refs/heads/`, `heads/`, `refs/remotes/<remote>/`, `remotes/<remote>/` or
    `<remote>/` prefix, matched in any letter case: the local branch a ref spelling means."""
    for prefix in ('refs/heads/', 'heads/', 'refs/remotes/%s/' % ctx.remote,
                   'remotes/%s/' % ctx.remote, '%s/' % ctx.remote):
        if name.lower().startswith(prefix.lower()) and len(name) > len(prefix):
            return name[len(prefix):]
    return name


def _fold_overlap(snap, ctx, rid, branch, scopes):
    """The fold-in guard's git probe: Refusal when a member of `scopes` overlaps a path
    `branch` changed since its merge base with the code main, or the scope of a record `branch`
    holds pulled; a member that cannot be compared counts as an overlap. Refusal too when
    `branch` is neither a local branch, nor a branch of any remote as last fetched, nor a branch
    that landed on the code main (a `Merge <branch>` merge on its first-parent chain,
    `landing_merge`); Failure, not that Refusal, when the code main does not resolve.
    Returns the NOTE lines: a branch only remotes have has the diff of each remote's tip
    compared; a landed branch has no diff to compare; the pulled scopes are checked either way.
    Every ref name is matched in any letter case, against git's own list of names
    (`_ref_names`), so a packed ref is found as a loose one is. Failure when git cannot answer
    or there is no base to measure from."""
    notes, hits = [], []
    tips, note = _branch_tips(ctx, branch, _ref_names(ctx, branch))
    for label, tip in tips:
        hits += _diff_hits(ctx, label, tip, scopes)
    if note:
        notes.append(note)
    if not tips:
        base, sha = _code_main(ctx)
        if not sha:
            raise Failure('the code main `%s` does not resolve here, so whether %s landed cannot '
                          'be told: set `backlog.code_main` in .kit.json to the branch code lands '
                          'on' % (base, branch))
        merge = landing_merge(ctx.root, ctx.code_main, branch, rev=sha, any_case=True)
        if not merge:
            raise Refusal('cannot add %s: `--found-on %s` names a branch that exists neither '
                          'here nor on a remote (as last fetched): pass an existing branch, or '
                          '`none` when no branch found it\na branch that landed is found by its '
                          '`Merge %s` merge on %s; one removed without landing is written as '
                          '`none`, with the branch named in --why-not-folded'
                          % (rid, branch, branch, ctx.code_main))
        notes.append('NOTE: branch %s landed on %s as %s; its diff was not compared'
                     % (branch, ctx.code_main, merge))
    unread = [r.stem for r in snap.unreadable]
    if unread:
        raise Failure('cannot tell which records %s holds pulled: unreadable record(s): %s'
                      % (branch, ', '.join(unread)))
    for r in snap.readable:
        if r.get('state') == 'pulled' and (r.get('pulled_by') or '').lower() == branch.lower():
            hits += _overlaps(scopes, r.all('scope'), '(scope of %s, pulled by %s)'
                              % (r.stem, branch))
    if hits:
        raise Refusal('cannot add %s: its scope overlaps the work of %s, so the finding belongs '
                      'there:\n  %s\nfold it into %s, or file it after %s lands'
                      % (rid, branch, '\n  '.join(hits), branch, branch))
    return notes


def _branch_tips(ctx, branch, names):
    """`(tips, note)`: `tips` is `[(label, commit)]` to diff for `branch`, shared by `add` and
    `pull`: its local tip, else the tip of each remote that has it as last fetched (each distinct
    commit once, labelled `<remote>/<branch>`), with a NOTE naming those remotes; `[]` and no
    note when neither has it. `names` is `_ref_names`. Failure when git cannot read a ref."""
    tip, why = refs.resolve(_spelling(names, 'refs/heads/%s' % branch), ctx.root, known=True)
    if tip is None:
        raise Failure('cannot read branch %s: %s' % (branch, why))
    if tip:
        return [(branch, tip)], None
    remotes = _remotes_with(ctx, branch, names)
    tips = []
    for _remote, ref, rtip in remotes:
        if rtip not in [t for _label, t in tips]:
            tips.append((ref[len('refs/remotes/'):], rtip))
    note = remotes and ('NOTE: branch %s is not a local branch; its diff was compared as last '
                        'fetched from %s' % (branch, ', '.join(r for r, _ref, _t in remotes)))
    return tips, note or None


def _remotes_with(ctx, branch, names=None):
    """`[(remote, ref, tip)]`: each remote whose `branch` git holds a remote-tracking ref for (as
    last fetched; no network), the ref as git spells it (`_spelling`; `names` is `_ref_names`,
    None: asked here) and its commit. Every remote is asked, not `ctx.remote` alone: a store's
    checkout can name another remote than the one the code is pushed to. Failure when git cannot
    list the remotes or read a ref: "cannot tell" is never "no such branch"."""
    out, why = cmd.run_text(['remote'], ctx.root)
    if out is None:
        raise Failure('cannot list the remotes to look for branch %s: %s' % (branch, why))
    if names is None:
        names = _ref_names(ctx, branch)
    found = []
    for remote in out.splitlines():
        remote = remote.strip()
        if not remote:
            continue
        ref = _spelling(names, 'refs/remotes/%s/%s' % (remote, branch))
        tip, why = refs.resolve(ref, ctx.root, known=True)
        if tip is None:
            raise Failure('cannot read %s: %s' % (ref, why))
        if tip:
            found.append((remote, ref, tip))
    return found


def _ref_names(ctx, branch):
    """Every branch and remote-tracking ref name git lists (`for-each-ref refs/heads/
    refs/remotes/`), to look `branch` up in. Asking git for the list, not for the one name, is
    what makes the letter case of a lookup consistent: on a case-insensitive file system a loose
    ref's file opens under any case, a packed-refs line only under its own. Failure when git
    cannot list them."""
    out, why = cmd.run_text(['for-each-ref', '--format=%(refname)', 'refs/heads/',
                             'refs/remotes/'], ctx.root)
    if out is None:
        raise Failure('cannot list the refs to look for branch %s: %s' % (branch, why))
    return [n.strip() for n in out.splitlines() if n.strip()]


def _spelling(names, ref):
    """`ref` as git spells it in `names` (`_ref_names`): the one name equal to it ignoring case,
    else `ref` unchanged (git then answers for it: a broken loose ref for-each-ref skips is no
    answer, not "no such ref"). Usage when several listed names equal it ignoring case, even
    one exactly: on a case-insensitive file system `rev-parse` of the packed one can open the
    other's loose file, so neither can be read for certain."""
    near = [n for n in names if n.casefold() == ref.casefold()]
    if len(near) > 1:
        raise Usage('`%s` matches %s, which differ only in letter case and cannot be told apart '
                    'on this file system: rename one' % (ref, ', '.join(sorted(near))))
    return near[0] if near else ref


def _overlaps(scopes, others, source):
    out = []
    for s in scopes:
        for o in others:
            try:
                hit = smod.members_collide(s, o)
            except smod.UndecidableScope as exc:
                out.append('%s vs %s %s - cannot be compared, so it counts: %s'
                           % (s, o, source, exc))
                continue
            if hit:
                out.append('%s vs %s %s' % (s, o, source))
    return out


def _diff_hits(ctx, branch, tip, scopes, base=None, mb=None):
    """An overlap line for each member of `scopes` that overlaps a path `branch` (at `tip`)
    changed since its merge base with the code main; one that cannot be compared counts."""
    hits = []
    for path in _branch_changes(ctx, branch, tip, base, mb):
        hits += _overlaps(scopes, [path], '(changed on %s)' % branch)
    return hits


def _code_main(ctx):
    """`(rev, sha)` of the code main as `refs.default_base` picks it on the code remote
    (`<code_remote>/<code_main>` as last fetched, else the local branch); `sha` is '' when it does
    not resolve. Failure when git cannot read it."""
    base, _note = refs.default_base(ctx.root, ctx.code_remote, ctx.code_main)
    sha, why = refs.resolve(base, ctx.root, known=True)
    if sha is None:
        raise Failure('cannot read the code main `%s`: %s' % (base, why))
    return base, sha


def _live_changes(ctx, holding, branch):
    """`(changes, notes, unknown)` for the fold-in guard (docs/backlog.md, "The fold-in guard"):
    `changes` is `{name: [(label, paths)]}`, the paths each live pull's branch in `holding`
    changed since its merge base with the code main, the caller's `branch` (any letter case) and
    its own pulls aside; each branch is diffed once. A code main that does not resolve here (a
    store whose git is not the code's) skips every diff with a NOTE, and so does a live branch
    that shares no history with it (the code main is not that branch's history:
    backlog.code_main unset). A live branch only remotes have is diffed at each remote's tip as
    last fetched, with a NOTE, as `add` does (`_branch_tips`); one neither here nor on a remote
    is noted, not compared. `unknown` is `{name: error}` for each branch git could not answer
    for (a ref it cannot read, a merge base it cannot find, a diff it cannot run: the Failure,
    or the Usage of `_spelling`); nothing is raised, so `pull` raises it and `next` passes over."""
    names = []
    for other in holding:
        name = other.get('pulled_by') or ''
        if name and name.lower() != (branch or '').lower() and name not in names:
            names.append(name)
    changes, notes, unknown = {}, [], {}
    if not names:
        return changes, notes, unknown
    try:
        base, sha = _code_main(ctx)
        if not sha:
            return changes, ['NOTE: code main %s does not resolve here; branch diffs not '
                             'compared' % base], unknown
        refnames = _ref_names(ctx, ', '.join(sorted(names)))
    except (Failure, Usage) as exc:
        return changes, notes, dict((name, exc) for name in names)
    for name in sorted(names):
        try:
            changes[name] = _one_branch(ctx, name, refnames, base, sha, notes)
        except (Failure, Usage) as exc:
            unknown[name] = exc
    return changes, notes, unknown


def _one_branch(ctx, name, refnames, base, sha, notes):
    """`[(label, paths)]` for one live branch of `_live_changes`, its NOTEs added to `notes`."""
    out = []
    tips, note = _branch_tips(ctx, name, refnames)
    if note:
        notes.append(note)
    if not tips:
        notes.append('NOTE: branch %s is neither a local branch nor on a remote; its diff was '
                     'not compared' % name)
    for label, tip in tips:
        mb, why = refs.merge_base(sha, tip, ctx.root)
        if mb is None:
            raise Failure('cannot find the merge base of %s and %s: %s' % (label, base, why))
        if not mb:
            notes.append('NOTE: code main %s shares no history with %s; set '
                         'backlog.code_main; branch diffs not compared' % (base, label))
            continue
        out.append((label, _branch_changes(ctx, label, tip, (base, sha), mb)))
    return out


def _change_hits(scopes, changes):
    """An overlap line for each member of `scopes` that overlaps a path in `changes`
    (`_live_changes`), branch by branch in name order; one that cannot be compared counts."""
    hits = []
    for name in sorted(changes):
        for label, paths in changes[name]:
            for path in paths:
                hits += _overlaps(scopes, [path], '(changed on %s)' % label)
    return hits


def _pull_fold_hits(ctx, scopes, holding, branch):
    """The fold-in guard on `pull`: `(hits, notes)`, the overlaps of `scopes` with what the other
    live pulls' branches changed (`_live_changes`). Failure (or Usage) when git cannot answer for
    one of them: on `pull`, "cannot tell" is never "no overlap"."""
    changes, notes, unknown = _live_changes(ctx, holding, branch)
    for name in sorted(unknown):
        raise unknown[name]
    return _change_hits(scopes, changes), notes


def _offer_folds(ctx, holding, own, diffs=None):
    """The `folded(rec)` check an offer (`omod.next_offer`) passes over with: the reason `rec`'s
    scope folds into another live pull's branch than `own` (any letter case), None when it does
    not. The branches are diffed on the first call only, and never when no candidate asks. A
    branch git cannot answer for passes every candidate over with a "cannot tell" note instead
    of failing `next`; `pull` still fails on it (`_pull_fold_hits`). `diffs`, a list two checks
    share (current_offer), caches every live branch's diff, `own`'s too, and each check leaves
    its own branch out of it; without it only the other branches are diffed."""
    seen = [] if diffs is None else diffs

    def folded(rec):
        if not seen:
            seen.append(_live_changes(ctx, holding, own if diffs is None else None))
        changes, _notes, unknown = seen[0]
        if diffs is not None and own:
            mine = own.lower()
            changes = dict((k, v) for k, v in changes.items() if k.lower() != mine)
            unknown = dict((k, v) for k, v in unknown.items() if k.lower() != mine)
        hits = _change_hits(rec.all('scope'), changes)
        if hits:
            where = _fold_where(hits)
            return ('scope overlaps the work of %s (%s); fold it into %s, or pull it after %s '
                    'lands' % (where, hits[0], where, where))
        for name in sorted(unknown):
            return ('cannot tell whether %s folds into %s\'s work; pull after %s lands or check '
                    'git (%s)' % (rec.id, name, name, unknown[name]))
        return None
    return folded


def _fold_where(hits):
    """The branches the overlap lines `hits` name, in order, comma-separated."""
    branches = []
    for h in hits:
        b = h.rpartition('(changed on ')[2].partition(')')[0]
        if b not in branches:
            branches.append(b)
    return ', '.join(branches)


def _fold_message(rid, hits, override, verb='pull'):
    """The fold refusal's text (`verb` `pull`, or `widen` for `set --scope`): the overlapping
    pairs, where the work belongs, and the override (`override`: the row in words, and any form
    it takes). The caller raises it as a Refusal (tests/backlog/test_refusal_kinds.py audits each
    raise by name)."""
    where = _fold_where(hits)
    return ('cannot %s %s: its scope overlaps the work of %s, so it belongs there:\n  %s\n'
            'fold it into %s, or %s it after %s lands. Overriding that is %s'
            % (verb, rid, where, '\n  '.join(hits), where, verb, where, override))


def _collision_change(change, verb):
    """`change` marked as one that took the collision row of `verb` (policy.COLLISION_ROWS): its
    `rows` for the audit log (tools/backlog.py), and the mark on its subject the audit check reads
    back from the store's history."""
    change.rows = [policy.COLLISION_ROWS[verb]]
    change.subject += policy.COLLISION_MARK
    return change


def _branch_changes(ctx, branch, tip, base=None, mb=None):
    """The paths `branch` changed since its merge base with the code main (no rename pairing,
    so a rename names both paths). `base` is `(rev, sha)` when the caller already resolved the
    code main, `mb` the merge base when it already found it."""
    key = 'set `backlog.code_main` in .kit.json to the branch code lands on'
    base, sha = base or _code_main(ctx)
    if not sha:
        raise Failure('the code main `%s` does not resolve here, so the diff of %s cannot be '
                      'measured: %s' % (base, branch, key))
    why = None
    if not mb:
        mb, why = refs.merge_base(sha, tip, ctx.root)
    if mb is None:
        raise Failure('cannot find the merge base of %s and %s: %s' % (branch, base, why))
    if not mb:
        raise Failure('%s shares no history with the code main `%s`, so its diff cannot be '
                      'measured: %s' % (branch, base, key))
    out, why = cmd.run(['diff', '--no-renames', '--name-only', '-z', mb, tip, '--'], ctx.root)
    if out is None:
        raise Failure('cannot diff %s against its merge base %s: %s' % (branch, mb[:10], why))
    return sorted(set(p for p in out.decode('utf-8', errors='replace').split('\0') if p))


# --- set ------------------------------------------------------------------------------------------

def refuse_top(rid, verb, rows):
    """A write planned without the role check (`role_checked` false: a library caller, not
    tools/backlog.py or an owner route) refuses when it takes a row only the owner and the PM may
    take (policy.top_only of sort's `rows`): the role check decides those, not the plan."""
    top = policy.top_only(rows)
    if top:
        raise Refusal('`%s`: this %s is %s (kitlib/backlog/roles.py), and no role check ran: '
                      'make it with tools/backlog.py, whose role check decides it'
                      % (rid, verb, policy.row_text(top[0])))


WIDEN_ROW = policy.COLLISION_ROWS['set']


def plan_set(snap, ctx, rid, changes, unset=(), *, body=None, role_checked=False,
             may_collide=False):
    """Edit fields and/or the body. `changes` is `[(key, [values])]`.

    - Who may move an owner's or the PM's price, or clear the owner from `blocked_on`, is the
      role check's (kitlib/backlog/roles.py: set-price-owner, unblock-owner). `role_checked`
      says the caller ran it (opener for tools/backlog.py, the hub's owner routes); without it
      such a write refuses (refuse_top).
    - `--priority -1` is refused (record.RETIRED_PRIORITY): the owner hold is `blocked_on:
      owner`, written by `block --on owner`.
    - An owner-held record's priority changes only with `priority_by` owner or pm.
    - A price field moved drops the record's `priority_ruling` (it named the old price); so does
      a result of `priority_by: agent`.
    - No ruling id is taken: an owner gate is a role. Past `unset` every argument is a keyword,
      so a sixth positional (the old ruling slot) is a TypeError.
    - `state: pulled` is entered only by `pull` and left only by `release`; `state: parked` only
      by `set-status parked` and `unpark`.
    - `priority_ruling`, `hold_ruling`, `resume` and `priority_per` (UNWRITTEN) are never
      written; `unset` of
      one clears it. Clearing `resume` is the owner's and the PM's (policy.OWNER_CLEARS, the
      `set-price-owner` row), so without the role check it refuses (refuse_top).
    - On a pulled record, `scope` members no old member covers (widened_members) that collide
      with a live pull, or overlap a path another live pull's branch changed (the fold-in guard,
      `_pull_fold_hits`, the record's own branch aside), take the `set-widen-collision` row:
      refused unless `may_collide` (the caller's role may take it). Taken, the output names each
      collision and the Change names the row (_collision_change). Narrowing, and widening into
      free scope, are free. Each `--scope` value is checked first: a malformed member is USAGE,
      not a collision.
    - `scope` replaces the whole list; the output names each member it removed.
    """
    for key, vals in changes:
        refuse_machine_paths('--' + key, *vals)
    refuse_machine_paths('--body-file', body)
    rec = require(snap, rid)
    for key in unset:
        if key not in rmod.FIELDS:
            raise Usage('`--unset %s` names no field' % key)
        if key in rmod.REQUIRED:
            raise Usage('`%s` is required in every record, so it cannot be unset' % key)
    if not changes and not unset and body is None:
        raise Usage('`set` was given nothing to change')
    for key, vals in list(changes) + [(k, None) for k in unset]:
        if key not in rmod.FIELDS:
            raise Usage('`%s` is not a field' % key)
        if key in UNWRITTEN:
            if vals is None:
                continue                # clearing one is the write left (the role check's row)
            raise Refusal('`%s` is no longer written: an owner gate is a role now '
                          '(kitlib/backlog/roles.py), not a cited id; `set %s --unset %s` '
                          'clears it' % (key, rid, key))
        if key in WRITTEN_BY:
            raise Refusal('`%s` is written only by %s' % (key, WRITTEN_BY[key]))
        if key in rmod.SINGLE_VALUED and vals is not None and len(vals) > 1:
            raise Usage('`%s` takes one value and was given %d' % (key, len(vals)))
    if rmod.RETIRED_PRIORITY in dict(changes).get('priority', []):
        raise Usage('%s (`block %s --on owner --owner-ask "<the question>"`)'
                    % (rmod.RETIRED_PRIORITY_WHY, rid))
    for s in dict(changes).get('scope', []):
        # Before the widening check, which counts a member it cannot compare as a collision.
        bad = smod.scope_member_error(s)
        if bad:
            raise Usage('bad `--scope %s`: %s; give one `--scope <path>` per path' % (s, bad))

    keys = [k for k, _ in changes]
    pairs = [(k, v) for k, v in rec.pairs() if k not in unset]
    pairs = rmod.apply_changes(pairs, changes)
    get = lambda k: rmod.values(pairs, k)  # noqa: E731
    was_pulled, now_pulled = rec.get('state') == 'pulled', get('state') == ['pulled']
    if was_pulled != now_pulled:
        raise Refusal('`state: pulled` is entered only by `pull` and left only by `release`, which '
                      'record who has it; `set` does neither')
    if (rec.get('state') == 'parked') != (get('state') == ['parked']):
        raise Refusal('`state: parked` is entered only by `set-status parked [--q Q-nnn]` and left '
                      'only by `unpark`, which record the wait and its end')
    if not role_checked:
        refuse_top(rid, 'set', policy.sort('set', policy.given_of_set(changes, unset, body), rec))

    new_by = dict(changes).get('priority_by', [None])[0]
    if 'priority' in keys and rmod.is_owner_held(rec) and new_by not in RULED_PRICE_BY:
        raise Refusal('`%s` is owner-held (%s), so its priority changes only with `--priority-by '
                      'owner|pm`' % (rid, rmod.owner_hold_label(rec)))

    moved = [k for k in rmod.PRIORITY_KEYS if rec.all(k) != get(k)]
    priced = [k for k in moved if k != 'priority_ruling']
    widen_hits, folds, notes, added = [], [], [], []
    if 'scope' in keys and was_pulled:
        added = widened_members(rec.all('scope'), get('scope'))
        if added:
            holding, _dead, _why = omod.in_flight_split(snap, ctx.root, ctx.remote)
            widen_hits = live_hits(added, holding, skip=rid, branch=rec.get('pulled_by'))
            # The fold-in guard, as `pull` runs it.
            folds, notes = _pull_fold_hits(ctx, added, holding, rec.get('pulled_by'))
    if (widen_hits or folds) and not may_collide:
        override = '%s: `set %s --scope <path> [--scope <path> ...]` (the whole new list)' \
            % (policy.row_text(WIDEN_ROW), rid)
        if widen_hits:
            raise Refusal('`%s` is pulled, and this widens its scope into a live pull: %s. '
                          'Narrowing is free; widening into a live pull is %s'
                          % (rid, '; '.join(widen_hits), override))
        raise Refusal(_fold_message(rid, folds, override, 'widen'))
    dropped = dropped_why = None
    if get('priority_ruling') and (priced or get('priority_by') == ['agent']):
        dropped = get('priority_ruling')[0]
        dropped_why = ('`priority_by: agent` carries no ruling' if get('priority_by') == ['agent']
                       else 'it named the old price')
        pairs = [(k, v) for k, v in pairs if k != 'priority_ruling']

    lines = notes + ['SET %s' % rid]
    for key in sorted(set(keys) | set(unset) | ({'priority_ruling'} if dropped else set())):
        lines.append('  %-11s %s -> %s' % (key + ':', ' / '.join(rec.all(key)) or '(absent)',
                                           ' / '.join(rmod.values(pairs, key)) or '(absent)'))
    if body is not None:
        lines.append('  body:       replaced (%d bytes)' % len(body.encode('utf-8')))
    lines += ['  widened into a live pull: %s' % h for h in widen_hits]
    lines += ['  folds into the work of %s: %s'
              % (h.rpartition('(changed on ')[2].partition(')')[0], h) for h in folds]
    if 'scope' in keys:
        # `--scope` replaces the whole list; say plainly what that dropped.
        gone = [m for m in rec.all('scope') if m not in get('scope')]
        if gone:
            lines.append('  removed %d scope %s: %s' % (len(gone), 'entry' if len(gone) == 1
                                                         else 'entries', ', '.join(gone)))
    if dropped is not None:
        lines.append('  priority_ruling %s dropped: %s' % (dropped, dropped_why))
    subject = 'backlog: set %s' % rid
    text = _text(pairs, body if body is not None else rec.body)
    if text == rec.text:
        return Change({}, subject, lines + ['  (no change)'])
    change = Change({rid: text}, subject, lines)
    if widen_hits or folds:
        _collision_change(change, 'set')
    return change


# --- block / unblock ------------------------------------------------------------------------------

def plan_block(snap, ctx, rid, members, owner_ask=None, reason=None, until=None):
    """`block` ADDS `members` to `blocked_on`, in order, after the blockers already there (one
    already present is not repeated); it never removes one, so it never releases the owner's hold:
    removing a blocker is `unblock --on`'s job.
    `until`, when given, is the end condition of the one member (exactly one `--on`, a member with
    no space in it), written as `until: <member> <condition>` and replacing that member's line;
    kitlib/backlog/conditions.py names the conditions the Action Hub checks, and an `owner` member
    takes only an owner event (a to-do or a question).
    `reason`, when given, goes into the commit subject and onto one line appended to the body
    (`Blocked on M (date): T`), so `show` says why the record is held; no header key is written
    for it. A PULLED record is blocked only by whoever may stop its pull (stop_pull), and only
    with a `reason`: the one commit also clears the pull and pre-emption keys, which frees its
    scope, and a body line `Stopped on branch B at <tip>, branch kept.` goes before the `Blocked
    on` line while that branch exists. The branch is never touched."""
    refuse_machine_paths('--owner-ask', owner_ask)
    if reason is not None:
        refuse_machine_paths('--reason', reason)
        bad = reason_error(reason, '--reason')
        if bad:
            raise Usage(bad)
        reason = ' '.join(reason.split())
    rec = require(snap, rid)
    if not members:
        raise Usage('`block` requires at least one `--on <member>`')
    for m in members:
        if m == '-':
            raise Usage('`--on -` is not a blocker; `unblock` writes `-`')
        err = rmod.blocked_on_error(m, snap.rules)
        if err:
            raise Usage('bad `--on %s`: %s' % (m, err))
    if 'item:%s' % rid in members:
        raise Usage('`%s` cannot be blocked on itself' % rid)
    if 'owner' in members and not owner_ask and not rec.has('owner_ask'):
        raise Usage('blocking on the owner needs `--owner-ask "<the question>"`')
    if until is not None:
        until = ' '.join(until.split())
        refuse_machine_paths('--until', until)
        if not until:
            raise Usage('`--until` needs the condition that ends the hold')
        if len(members) != 1:
            raise Usage('`--until` is the end condition of one blocker: give exactly one `--on`')
        if len(members[0].split()) != 1:
            raise Usage('`--until` needs a one-word member, and `--on %s` has a space in it'
                        % members[0])
        why = condmod.owner_error(members[0], until)
        if why:
            raise Usage('refused `--until %s`: %s' % (until, why))
    stop = None
    if rec.get('state') == 'pulled':
        if reason is None:
            raise Usage('blocking a pulled record frees its scope: it needs `--reason T`, '
                        'written to the body')
        holder = rec.get('pulled_by')
        if not holder:
            raise Failure('`%s` is pulled with no `pulled_by`; `check` names it' % rid)

        def live(why):
            return ('`%s` is in flight (pulled by %s), and %s. Its holder blocks or releases it: %s'
                    % (rid, holder, 'its tree is live: %s' % why if why else
                       'a branch or worktree by that name exists',
                       holder_form(rec, holder, 'block %s --on <member> --reason "<why>"' % rid)))
        stop = stop_pull(ctx, rid, holder, live)
    old =[m for m in rec.all('blocked_on') if m != '-']
    after = list(old)
    for m in members:
        if m not in after:
            after.append(m)
    changes = [('state', ['blocked']), ('blocked_on', after)]
    if owner_ask:
        changes.append(('owner_ask', [owner_ask]))
    if until is not None:
        kept = [v for v in rec.all('until') if condmod.split(v)[0] != members[0]]
        changes.append(('until', kept + ['%s %s' % (members[0], until)]))
    if stop is not None:
        changes += [(k, []) for k in rmod.PULL_KEYS + rmod.PREEMPT_KEYS]
    pairs = rmod.apply_changes(rec.pairs(), changes)
    lines = ['BLOCKED %s on %s' % (rid, ', '.join(members))]
    if old:
        lines.append('  blocked_on: %s -> %s' % (', '.join(old), ', '.join(after)))
    if until is not None:
        lines.append('  until: %s %s%s' % (members[0], until, '' if condmod.parse(until) else
                                           ' (free text: no program checks it; clear it with '
                                           '`unblock --on %s`)' % members[0]))
    if stop is not None:
        lines += stop[1]
    subject = 'backlog: block %s on %s' % (rid, ', '.join(members))
    body = rec.body
    if reason is not None:
        subject += ': %s' % reason
        note = '%sBlocked on %s (%s): %s' % (stop[0] if stop else '', ', '.join(members),
                                             ctx.date(), reason)
        body = (body or '').rstrip('\n')
        body = (body + '\n\n' if body.strip() else '') + note + '\n'
        lines.append('  reason (in the body): %s' % reason)
    text = _text(pairs, body)
    if text == rec.text:
        return Change({}, subject, lines + ['  (no change)'])
    return Change({rid: text}, subject, lines)


def plan_unblock(snap, ctx, rid, on=None, reason=None, role_checked=False):
    """Clear one blocker. A record with one blocker opens, `on` and `reason` optional; one with
    several needs `on` (the member to clear) and `reason`, and keeps the rest blocked. The reason
    goes into the commit subject, as `release`'s does. Clearing the owner is the role check's
    `unblock-owner` row (kitlib/backlog/roles.py): without `role_checked` it refuses
    (refuse_top)."""
    rec = require(snap, rid)
    if rec.get('state') != 'blocked':
        raise Usage('`%s` is `state: %s`, not `blocked`: there is nothing to unblock'
                    % (rid, rec.get('state')))
    members = rec.all('blocked_on')
    several = len(members) > 1
    if on is None and several:
        raise Refusal('`%s` is blocked on %s; `unblock` clears one blocker, named, with a reason:\n%s'
                      % (rid, ', '.join(members), '\n'.join(
                          '  %s -C <worktree> unblock %s --on %s --reason "<why>"'
                          % (kitroot.command('backlog'), rid, m) for m in members)))
    if on is not None and on not in members:
        raise Usage('`--on %s` is not a blocker of `%s`; it is blocked on %s'
                    % (on, rid, ', '.join(members)))
    if reason is not None or several:
        refuse_machine_paths('--reason', reason)
        bad = reason_error(reason, '--reason')
        if bad:
            raise Usage(bad)
    left = [m for m in members if m != on] if several else []
    unheld = clears_owner_blocker(rec, left)
    if unheld and not role_checked:
        refuse_top(rid, 'unblock', ['unblock-owner'])
    pairs = [(k, v) for k, v in rec.pairs() if not (unheld and k == 'owner_ask')]
    restored, restore_lines, note = ([], [], '') if left else restore_pull(snap, ctx, rec)
    changes = [('blocked_on', left)] if left else [('state', ['open']), ('blocked_on', ['-'])]
    pairs = rmod.apply_changes(pairs, changes + restored)
    if left:
        lines = ['UNBLOCKED %s - `%s` removed -> %s' % (rid, on, ', '.join(left))]
    else:
        lines = ['UNBLOCKED %s - state: %s, blocked_on: -' % (rid, 'pulled' if restored else 'open')]
    lines += restore_lines
    if unheld and rec.has('owner_ask'):
        lines.append('  owner_ask dropped: nothing waits on the owner now. It was: %s' % rec.get('owner_ask'))
    subject = 'backlog: unblock %s' % rid
    if reason is not None:
        named = next((m for m in ([on] if on else members) if m != '-'), None)
        subject += '%s: %s' % (' from %s' % named if named else '', ' '.join(reason.split()))
    body = _append_line(rec.body, note) if note else rec.body
    return Change({rid: _text(pairs, body)}, subject, lines)


CONDITION_MET = 'condition met: '      # the unblock reason plan_condition_met writes


def plan_condition_met(snap, ctx, rid, member, cond):
    """Clear blocker `member` of `rid` because its end condition `cond` is met: plan_unblock with
    the reason `condition met: <cond>` (the commit subject). The caller checked the condition
    (kitlib.hubjobs.unblock_conditions); this checks the record still holds it: `rid` blocked on
    `member`, with the line `until: <member> <cond>`. When it does not (an unblock or a re-block
    got there first), nothing is written, so a second run changes nothing. A condition that is
    not one of conditions.py's forms is refused, and so is any but an owner event on `owner`:
    only those clear the owner's hold (role_checked, the unblock-owner row: the owner's own act
    ended it)."""
    if condmod.parse(cond) is None:
        raise Refusal('`%s`: `until: %s %s` is free text, which no program acts on; it is cleared '
                      'by hand with `unblock --on %s`' % (rid, member, cond, member))
    why = condmod.owner_error(member, cond)
    if why:
        raise Refusal('`%s`: %s' % (rid, why))
    rec = require(snap, rid)
    line = '%s %s' % (member, cond)
    subject = 'backlog: unblock %s from %s: %s%s' % (rid, member, CONDITION_MET, cond)
    if (rec.get('state') != 'blocked' or member not in rec.all('blocked_on')
            or line not in rec.all('until')):
        return Change({}, subject, ['%s no longer waits on `until: %s`: nothing to clear'
                                    % (rid, line), '  (no change)'])
    return plan_unblock(snap, ctx, rid, on=member, reason=CONDITION_MET + cond,
                        role_checked=member == 'owner')


# --- park / unpark -----------------------------------------------------------------------

def plan_set_status(snap, ctx, rid, status, q=None):
    """Park an open or pulled record, on the owner's question `q` when one is given (`q` is
    optional: a record can wait with no question id). A pull is given up: a parked record holds no
    scope and takes no place in the offer. A parked record is re-pointed to another `q`; with no
    `q` its recorded question is dropped (`parked_at` kept). The same question again, or no `q`
    on a record parked with none, refuses: nothing would change."""
    if status != 'parked':
        raise Usage('`set-status` sets `parked` only; `%s` is reached by its own verb (open: '
                    '`unblock`/`release`/`unpark`, blocked: `block`, pulled: `pull`)' % status)
    if q is not None and rmod._question(q) is not None:
        raise Usage('bad `--q %s`: %s' % (q, rmod._question(q)))
    rec = require(snap, rid)
    state = rec.get('state')
    if state == 'parked':
        was = rec.get('q')
        if was == q:
            raise Refusal('`%s` is already parked on %s' % (rid, q) if q else
                          '`%s` is already parked with no question' % rid)
        # Re-point: only `q` changes; `parked_at` keeps the day the wait began, and the old
        # question stays in the store's log through the commit subject.
        pairs = rmod.apply_changes(rec.pairs(), [('q', [q] if q else [])])
        was = was or 'no recorded question'
        on = 'on %s' % q if q else 'with no question'
        return Change({rid: _text(pairs, rec.body)},
                      'backlog: re-park %s %s (was %s)' % (rid, on, was),
                      ['PARKED %s %s, re-pointed (was %s)' % (rid, on, was)])
    if state not in ('open', 'pulled'):
        raise Refusal('`%s` is `state: %s`; only an open or pulled record is parked. %s first'
                      % (rid, state, 'Unblock it' if state == 'blocked' else 'Open it'))
    pairs = [(k, v) for k, v in rec.pairs() if k not in rmod.PULL_KEYS + rmod.PREEMPT_KEYS]
    pairs = rmod.apply_changes(pairs, [('state', ['parked']), ('q', [q] if q else []),
                                       ('parked_at', [ctx.date()])])
    lines = ['PARKED %s %s' % (rid, 'on %s' % q if q else '(no question)')]
    if state == 'pulled':
        lines.append('  pull by %s given up: its scope is free and it is out of the offer'
                     % rec.get('pulled_by'))
    return Change({rid: _text(pairs, rec.body)},
                  'backlog: park %s%s' % (rid, ' on %s' % q if q else ''), lines)


def plan_unpark(snap, ctx, rid):
    """Open a parked record once its question is answered: the park keys go and it is `state:
    open` again, an ordinary record in `next`. Who may is the role check's `unpark` row."""
    rec = require(snap, rid)
    if rec.get('state') != 'parked':
        raise Refusal('`%s` is `state: %s`, not `parked`: there is nothing to unpark'
                      % (rid, rec.get('state')))
    pairs = [(k, v) for k, v in rec.pairs() if k not in rmod.PARK_KEYS]
    pairs = rmod.apply_changes(pairs, [('state', ['open'])])
    lines = ['UNPARKED %s - state: open%s' % (rid, ' (was parked on %s)' % rec.get('q')
                                                if rec.get('q') else '')]
    return Change({rid: _text(pairs, rec.body)}, 'backlog: unpark %s' % rid, lines)


# --- review mark ----------------------------------------------------------------------------------

def plan_clear_review(snap, ctx, rid, reason):
    """Drop the review mark (`review:`, written by `add` from the Action Hub): a coordinator has
    reviewed the record, which `pull` and `next` then treat as any other. Any state; no ruling,
    and the price is not touched."""
    rec = require(snap, rid)
    refuse_machine_paths('--reason', reason)
    bad = reason_error(reason, '--reason')
    if bad:
        raise Usage(bad)
    if not rec.has('review'):
        raise Refusal('`%s` carries no `review:` mark, so there is nothing to clear' % rid)
    mark = rec.get('review')
    pairs = [(k, v) for k, v in rec.pairs() if k != 'review']
    text = ' '.join(reason.split())
    return Change({rid: _text(pairs, rec.body)}, 'backlog: clear review %s: %s' % (rid, text),
                  ['CLEARED the review mark on %s (was: %s)' % (rid, mark),
                   '  state: %s, priority: %s; `pull` and `next` now treat it as any other record'
                   % (rec.get('state'), rec.get('priority'))])


# --- the owner's answer ---------------------------------------------------------------------------

ANSWER_REVIEW = 'owner-answer'
ASKED_Q = re.compile(r'(?<![A-Za-z0-9-])Q-[0-9]{3,}(?![A-Za-z0-9_])')


def _waits_on(rec):
    """The questions the record's own wait names: `q` on a parked record, the Q-ids `owner_ask`
    cites on one blocked on the owner. Empty: nothing binds the wait to a question."""
    state = rec.get('state')
    if state == 'parked':
        return [rec.get('q')] if rec.get('q') else []
    if state == 'blocked' and 'owner' in rec.all('blocked_on'):
        return ASKED_Q.findall(rec.get('owner_ask') or '')
    return []


def plan_answer(snap, ctx, rid, qid, at, title, label, note=None, via=None, other=False,
                waiting=(), role_checked=False):
    """File the owner's answer to question `qid` (answered at `at`, the hub's timestamp) into
    the record it is bound to, in one write: an `answer: <qid> <at>` line (one per question; a
    re-answer replaces its line) and a `## Owner answer (Action Hub, <at>)` body section with
    `title`, the choice `label` and `note` verbatim, and `via`. Only the Action Hub calls it
    (WRITTEN_BY: `set` neither writes nor unsets `answer`).

    The record's wait ends only on the last answer: when `waiting` (the other questions bound to
    the record, still unanswered; the hub knows them) is empty and every question the wait names
    (_waits_on) has an answer line, a parked record is unparked (as plan_unpark) and the `owner`
    blocker is cleared (as plan_unblock: other blockers stay, `owner_ask` goes), which is the
    `unblock-owner` row, so it needs `role_checked` (else a
    Refusal: the owner's or the PM's role clears an owner blocker). A pulled or open record, or
    a wait no question names, only gets the answer.
    `other` (a free-text answer) also writes the review mark `review: owner-answer <date>`
    unless one stands, so `pull` refuses the record and `next` lists it to review until a
    coordinator's `clear-review`. The same `qid` and `at` again changes nothing, and so does an
    `at` older than the record's line for `qid` (a stale re-file never overwrites a newer
    answer). A record that
    is gone is NotFound: the hub leaves the answer to the PM."""
    err = rmod._question(qid) if qid else 'no question id'
    if err:
        raise Usage('bad question `%s`: %s' % (qid, err))
    line = '%s %s' % (qid, at)
    err = rmod._answer(line)
    if err:
        raise Usage('bad answer time: %s' % err)
    if not (title or '').strip() or not (label or '').strip():
        raise Usage('an owner answer needs the question\'s title and the choice\'s label')
    for w in waiting:
        if rmod._question(w):
            raise Usage('bad waiting question `%s`: %s' % (w, rmod._question(w)))
    refuse_machine_paths('answer', title, label, note, via)
    rec = require(snap, rid)
    subject = 'backlog: owner answer %s on %s' % (rid, qid)
    if line in rec.all('answer'):
        return Change({}, subject, ['ANSWERED %s on %s (%s)' % (rid, qid, at), '  (no change)'])
    when = datetime.datetime.fromisoformat(at)
    newer = [v for v in rec.all('answer') if v.partition(' ')[0] == qid
             and rmod._answer(v) is None
             and datetime.datetime.fromisoformat(v.partition(' ')[2]) > when]
    if newer:
        return Change({}, subject, ['ANSWERED %s on %s (%s)' % (rid, qid, at),
                                    '  (no change: the record holds a newer answer, `%s`)'
                                    % newer[0]])
    others = [v for v in rec.all('answer') if v.partition(' ')[0] != qid]
    pairs = rmod.apply_changes(rec.pairs(), [('answer', others + [line])])
    lines = ['ANSWERED %s on %s (%s): %s' % (rid, qid, at, ' '.join(label.split()))]
    done = set(rmod.answered(rec)) | {qid}
    asked = _waits_on(rec)
    state = rec.get('state')
    pending = [q for q in asked if q not in done] + [w for w in waiting if w not in done]
    stop_note = ''
    if not asked:
        lines.append('  state: %s, unchanged: its wait names no question' % state)
    elif pending:
        lines.append('  state: %s, unchanged: still waits on %s' % (state, ', '.join(pending)))
    elif state == 'parked':
        pairs = [(k, v) for k, v in pairs if k not in rmod.PARK_KEYS]
        pairs = rmod.apply_changes(pairs, [('state', ['open'])])
        lines.append('  UNPARKED - state: open (was parked on %s)' % rec.get('q'))
    else:
        if not role_checked:
            raise Refusal('`%s`: filing the answer to %s clears the record\'s owner blocker, '
                          'which needs the owner\'s or the PM\'s role (`unblock-owner`, '
                          'kitlib/backlog/roles.py), and no role check ran: the Action Hub files '
                          'it from the owner\'s answer route, with role_checked' % (rid, qid))
        left = [m for m in rec.all('blocked_on') if m != 'owner']
        pairs = [(k, v) for k, v in pairs if k != 'owner_ask']
        restored, restore_lines, stop_note = (([], [], '') if left
                                              else restore_pull(snap, ctx, rec))
        changes = [('blocked_on', left)] if left else [('state', ['open']),
                                                         ('blocked_on', ['-'])]
        pairs = rmod.apply_changes(pairs, changes + restored)
        lines.append('  owner blocker cleared - %s' % ('still blocked on %s' % ', '.join(left)
                                                       if left else 'state: %s' % (
                                                           'pulled' if restored else 'open')))
        lines += restore_lines
    marked = other and not rec.has('review')
    if marked:
        mark = '%s %s' % (ANSWER_REVIEW, ctx.date())
        pairs = rmod.apply_changes(pairs, [('review', [mark])])
        lines.append('  review: %s - a free-text answer: `pull` refuses it until a coordinator '
                     'reads it and runs `clear-review %s --reason T`' % (mark, rid))
    section = ['## Owner answer (Action Hub, %s)' % at, '',
               'Question: %s, %s' % (qid, ' '.join(title.split())), 'Answer: %s' % label]
    if note:
        section.append('Note: %s' % note)
    if via:
        section.append('Via: %s' % via)
    if marked:
        section.append('Marked for review: a free-text answer, read by a coordinator before '
                       'anyone pulls the record.')
    body = (rec.body or '').rstrip('\n')
    body = (body + '\n\n' if body.strip() else '') + '\n'.join(section) + '\n'
    if stop_note:
        body = _append_line(body, stop_note)
    lines.append('  body: section `## Owner answer (Action Hub, %s)` appended' % at)
    return Change({rid: _text(pairs, body)}, subject, lines)


# --- pull / release -------------------------------------------------------------------------------

def plan_pull(snap, ctx, rid, reason=None, may_collide=False, blocker=None):
    """Take an open, priced record and record the branch. One in `next`'s offer pulls plainly;
    one outside it needs `--reason` and records `preempt` and the `displaced` offer, computed
    here. In the offer means in `next`'s offer as the caller's branch prints it (its own pulls
    left out, so a record overlapping only them is offered) or as `next` prints it without a
    branch (current_offer).
    A record held on the owner is `state: blocked`, so it is refused (as tools/backlog.py's role
    check refuses `pull-held` to every role). One whose scope collides with a live pull on another branch (one on the caller's
    branch is no collision) is the `pull-collision` row (kitlib/backlog/roles.py): refused unless
    `may_collide` (the caller's role may take it), and then it needs `--reason`, recorded in
    `preempt`, and the Change names the row (_collision_change). An unmet `requires:` refuses.
    The fold-in guard (`_pull_fold_hits`): one whose scope overlaps a path another live pull's
    branch changed is that row too; `next` passes such a record over, so its refusal comes
    before the not-offered one.
    A pulled record refuses, unless its pull is dead and it is this session's: see
    `_plan_takeover`."""
    refuse_machine_paths('--reason', reason)
    rec = require(snap, rid)
    if reason is not None:
        reason = ' '.join(reason.split())
        if not reason:
            raise Usage('`--reason` was given blank')
    state = rec.get('state')
    if state == 'pulled':
        return _plan_takeover(snap, ctx, rec, reason, may_collide, blocker)
    if state != 'open':
        raise Refusal('`%s` is `state: %s`, and `pull` takes an open record. %s first, in its own '
                      'step' % (rid, state, 'Unblock it' if state == 'blocked' else 'Open it'))
    if rec.has('review'):
        raise Refusal('`%s` is marked for review (review: %s); a coordinator clears it with '
                      '`clear-review %s --reason T` before anyone pulls it'
                      % (rid, rec.get('review'), rid))
    if rec.get('priority') == rmod.RETIRED_PRIORITY:
        # a leftover owner hold: pricing it would lift the hold, so name the hold, not a price
        raise Refusal('`%s` is `priority: -1`, a retired owner hold, never pulled or priced away: '
                      'the owner or the PM moves it to `block %s --on owner --owner-ask "<the '
                      'question>"`' % (rid, rid))
    if not rmod.is_band(rec.get('priority')):
        raise Refusal('`%s` is `priority: %s`, and `pull` takes a priced record (`1`-`5`). Price it '
                      'first: `set %s --priority N --priority-by agent`'
                      % (rid, rec.get('priority'), rid))
    if blocker is not None:
        other = snap.get(blocker)
        if other is None or 'item:%s' % rid not in other.all('blocked_on'):
            raise Usage('`--blocker %s` is not a record blocked on `item:%s`' % (blocker, rid))
    missing = ctx.unmet(snap)(rec)
    if missing:
        raise Refusal('`%s` requires %s, which is unmet: %s' % ((rid,) + missing[0]))
    branch = ctx.current_branch()
    offers, passed, holding, also, offer_notes = current_offer(snap, ctx, branch)
    offered = rec in [o.rec for o in offers] or rec in also
    hits = live_hits(rec.all('scope'), holding, skip=rid, branch=branch)
    folds, notes = _pull_fold_hits(ctx, rec.all('scope'), holding, branch)
    # The fold refusal first: `next` passes a folding record over, so "not in the offer" would
    # hide the reason and the override that this one names.
    override = '%s, with `pull %s --reason "<why>"`' % (policy.row_text('pull-collision'), rid)
    if folds and (not may_collide or reason is None):
        raise Refusal(_fold_message(rid, folds, override))
    if not offered and reason is None:
        raise Refusal('`%s` is not in `next`\'s offer - %s. An out-of-order pull is a pre-emption, '
                      'the owner\'s decision: a coordinator asks it through the PM and, on the '
                      'owner\'s yes, runs `pull %s --reason "<why>"` (recorded); a worker stops '
                      'and reports%s'
                      % (rid, omod.not_offered_why(rec, passed, ctx.session), rid,
                         '. Its scope collides with a live pull, so it is %s' % override
                         if hits else ''))
    if hits and (not may_collide or reason is None):
        raise Refusal('`%s` collides with a live pull: %s. `--reason` alone never overrides it: '
                      'pulling it anyway is %s' % (rid, '; '.join(hits), override))
    lines = offer_notes + notes + ['PULLED %s - state: open -> pulled' % rid, '  pulled_at:  %s' % ctx.date(),
                     '  pulled_by:  %s' % branch]
    resume = resume_line(rec.body)
    if resume:
        lines.append(resume)
    today = ctx.date()
    changes = [('state', ['pulled']), ('pulled_at', [today]), ('pulled_by', [branch])]
    subject = 'backlog: pull %s onto %s' % (rid, branch)
    if offered and not folds:
        changes += [(k, []) for k in rmod.PREEMPT_KEYS]
    else:
        recorded = reason
        if blocker is not None:
            recorded += ' (unblocks %s)' % blocker
        lines += _collisions(rec, holding)
        lines += ['warning: %s' % f for f in folds]
        offers = [o for o in offers if o.rec is not rec]
        displaced = ['%s rank %s priority %s' % (o.rec.id, o.rank, o.rec.get('priority'))
                     for o in offers] or ['-']
        changes += [('preempt', [recorded]), ('preempt_by', [branch]), ('preempt_at', [today]),
                    ('displaced', displaced)]
        lines.append('  preempt:    %s' % recorded)
        lines += ['  displaced:  %s' % d for d in displaced]
        subject += ', pre-empting %d offered item%s' % (len(offers), '' if len(offers) == 1 else 's')
    pairs = rmod.apply_changes(rec.pairs(), changes)
    change = Change({rid: _text(pairs, rec.body)}, subject, lines)
    if hits or folds:
        _collision_change(change, 'pull')
    return change


def live_hits(scopes, holding, skip=None, branch=None):
    """`scope A collides with B already taken by ID (in flight on BRANCH)` for each collision of
    `scopes` with a live pull in `holding`, bar the record id `skip` and any pull by `branch`: one
    branch holding overlapping records is not a collision. A comparison that cannot be decided is
    a hit, never a clearance."""
    hits = []
    for other in holding:
        if other.id == skip or (branch and other.get('pulled_by') == branch):
            continue
        who = '%s (in flight on %s)' % (other.id, other.get('pulled_by') or '?')
        try:
            pairs = smod.colliding_pairs(scopes, other.all('scope'))
        except smod.UndecidableScope as exc:
            hits.append('scope cannot be compared with %s, so it is a collision: %s' % (who, exc))
            continue
        hits += ['scope %s collides with %s already taken by %s' % (a, b, who) for a, b in pairs]
    return hits


def widened_members(old, new):
    """The members of `new` that no member of `old` covers: what a `set --scope` adds. An old
    member covers a new one when they are equal (components casefolded, the same `/**`-ness), or
    when the old one is `X/**` and the new one lies under X. `none` covers only `none`, and a new
    `none` adds nothing. A member the grammar cannot read is never covered."""
    def shape(m):
        if m == smod.NONE:
            return None
        try:
            return smod.scope_components(m), m.endswith(smod.GLOB_SUFFIX)
        except smod.UndecidableScope:
            return False
    olds = [s for s in (shape(m) for m in old) if s]
    added = []
    for m in new:
        if m == smod.NONE:
            continue
        s = shape(m)
        if s is False or not any(o == s or (o[1] and s[0][:len(o[0])] == o[0]) for o in olds):
            added.append(m)
    return added


def _collisions(rec, holding):
    """A warning line for each in-flight record in `holding` whose scope collides with `rec`'s
    (or cannot be compared with it)."""
    lines = []
    for other in holding:
        try:
            if smod.scopes_collide(rec.all('scope'), other.all('scope')):
                lines.append('warning: scope collides with `%s`, in flight on %s'
                             % (other.id, other.get('pulled_by')))
        except smod.UndecidableScope as exc:
            lines.append('warning: scope cannot be compared with `%s` (in flight), so it is a '
                         'collision: %s' % (other.id, exc))
    return lines


def landing_merge(root, main_branch, branch, since=None, rev=None, any_case=False):
    """The landing merge of `branch` on `main_branch` (`<short sha> <subject>`, the subject
    `Merge <branch>` or `Merge <branch>: <summary>` as kitlib/gate/land.py writes it), or None
    when its history has none. Only merges on the first-parent chain count: a `Merge <branch>`
    merge brought in inside another branch was not made on `main_branch`. `since` (a
    `YYYY-MM-DD` day, the record's `pulled_at`) skips a merge committed on an earlier day: an
    older landing of a branch whose name was later reused.
    A merge on that day still counts. `rev` is the history searched (None: the local
    `main_branch`); `any_case` matches the branch name in any letter case. Failure when git
    cannot say."""
    want = 'Merge %s' % branch
    args = ['log', '--merges', '--first-parent', '--format=%h %cs %s', '-F', '--grep', want]
    if any_case:
        args.append('-i')
    out, why = cmd.run_text(args + [rev or 'refs/heads/%s' % main_branch, '--'], root)
    if out is None:
        raise Failure('cannot read the history of %s to see whether %s landed: %s'
                      % (main_branch, branch, why))
    fold = (lambda t: t.casefold()) if any_case else (lambda t: t)
    for line in out.split('\n'):
        sha, _, rest = line.partition(' ')
        day, _, subject = rest.partition(' ')
        if since and day < since:
            continue
        if fold(subject) == fold(want) or fold(subject).startswith(fold(want + ': ')):
            return '%s %s' % (sha, subject)
    return None


def _plan_takeover(snap, ctx, rec, reason, may_collide, blocker):
    """A pull of a pulled record. It refuses `already in flight`, unless the record's `session:` is
    the caller's session and its pull is dead by `next`'s test (omod.in_flight_split: git
    answered and no branch, remote-tracking branch or worktree has its `pulled_by`); then the
    pull moves to this branch, `pulled_at` restamped, any pre-emption lines kept as the first pull
    wrote them, with the scope-collision warnings a pre-empting pull prints. A takeover whose scope
    collides with a live pull on another branch, or folds into one's diff (`_pull_fold_hits`),
    is the `pull-collision` row, as a pull is: refused unless `may_collide`, and the Change names
    the row (_collision_change). It takes no flag. A dead pull marked for review refuses, as `pull` of a marked record does,
    until `clear-review`. A failed git probe
    holds the pull (fail closed). A `pulled_by` that landed on the main branch (a `Merge <branch>`
    merge on its first-parent line, committed on or after the `pulled_at` day; one brought in
    inside another branch does not count) is not dead but unclosed:
    that refuses, and a git that cannot say fails."""
    rid, old = rec.id, rec.get('pulled_by')
    busy = Refusal('`%s` is already in flight - pulled %s by %s'
                   % (rid, rec.get('pulled_at'), old))
    if not ctx.session or rec.get('session') != ctx.session:
        raise busy
    holding, dead, _why = omod.in_flight_split(snap, ctx.root, ctx.remote)
    if rec not in dead:
        raise busy
    if rec.has('review'):
        raise Refusal('`%s` is a dead pull of this session, and it is marked for review (review: '
                      '%s); a coordinator clears it with `clear-review %s --reason T` before '
                      'anyone takes it over' % (rid, rec.get('review'), rid))
    hits = live_hits(rec.all('scope'), holding, skip=rid, branch=ctx.current_branch())
    folds, notes = _pull_fold_hits(ctx, rec.all('scope'), holding, ctx.current_branch())
    given = [f for f, v in (('--reason', reason), ('--blocker', blocker)) if v is not None]
    if given:
        raise Usage('`%s` is a dead pull of this session (pulled_by %s has no branch): `pull %s` '
                    'takes it over as it stands, with no %s' % (rid, old, rid, ', '.join(given)))
    merge = landing_merge(ctx.root, ctx.main_branch, old, since=rec.get('pulled_at'))
    if merge:
        raise Refusal('`%s` was pulled by %s, which has landed on %s (%s): close the record, do '
                      'not take it over' % (rid, old, ctx.main_branch, merge))
    override = '%s, with `pull %s`' % (policy.row_text('pull-collision'), rid)
    if hits and not may_collide:
        raise Refusal('`%s` is a dead pull of this session, and its scope collides with a live '
                      'pull: %s. Taking it over anyway is %s' % (rid, '; '.join(hits), override))
    if folds and not may_collide:
        raise Refusal(_fold_message(rid, folds, override))
    branch = ctx.current_branch()
    today = ctx.date()
    pairs = rmod.apply_changes(rec.pairs(), [('pulled_at', [today]), ('pulled_by', [branch])])
    lines = notes + ['PULLED %s - took over a dead pull from %s (it has no branch)' % (rid, old),
                     '  pulled_at:  %s -> %s' % (rec.get('pulled_at'), today),
                     '  pulled_by:  %s -> %s' % (old, branch)]
    resume = resume_line(rec.body)
    if resume:
        lines.append(resume)
    lines += _collisions(rec, holding)
    lines += ['warning: %s' % f for f in folds]
    change = Change({rid: _text(pairs, rec.body)},
                    'backlog: pull %s onto %s, taking over a dead pull from %s' % (rid, branch, old),
                    lines)
    if hits or folds:
        _collision_change(change, 'pull')
    return change


def branch_name_error(name, root):
    r = cmd.call(['check-ref-format', '--branch', name], root)
    if r.rc != 0 or cmd.text(r.out).strip() != name:
        return '`%s` is not a branch name' % name
    return None


def holder_form(rec, holder, verb):
    """How a live pull's holder stops it, for stop_pull's refusals: from the branch's worktree, or
    from any checkout with `--as <holder>` (a coordinator that pulled with `--as` is the holder;
    a worker on that branch did not pull it). The record's `session:` is named as `--session S`
    when it has one, as pull_branch.refusal does."""
    session = rec.get('session')
    return ('from that branch, or as that branch from any checkout (backlog.py -C <store> '
            '%s--as %s %s)' % ('--session %s ' % session if session else '', holder, verb))


def stop_pull(ctx, rid, holder, live, kept=True):
    """Whether the caller may stop `rid`'s pull by `holder` (block or release it): its holder (the
    caller's branch) may; anybody may when the pull is dead (no local branch, remote-tracking
    branch or worktree by that name: offer.live_branches), or when `ctx.liveness` finds the
    holder's worktree dead. Refusal(live(why)) otherwise, `why` None when there is no probe.
    Returns `(stopped, lines)`: `stopped` is the body line `Stopped on branch B at <tip>, branch
    kept.` with its newline (its own line, so the `Blocked on` line after it still starts one:
    record.hold_citations), '' when there is no local branch B; `lines` say what was found.
    `kept` False (a landing's release, whose cleanup deletes the branch) reads no branch and
    gives ''."""
    mine = ctx.branch or refs.current_branch(ctx.root)[0]
    if holder == mine:
        how = ''
    else:
        bad = branch_name_error(holder, ctx.root)
        if bad:
            raise Refusal('%s, so what holds the pull of `%s` cannot be checked' % (bad, rid))
        names, why = omod.live_branches(ctx.root, ctx.remote)
        if names is None:
            raise Failure('cannot tell whether the pull of `%s` by `%s` is dead: %s'
                          % (rid, holder, why))
        if holder not in names:
            how = ' (dead)'
        elif ctx.liveness is None:
            raise Refusal(live(None))
        else:
            why, _tip = ctx.liveness(holder)
            if why is not None:
                raise Refusal(live(why))
            how = ' (its worktree is dead)'
    lines = ['  was pulled_by: %s%s' % (holder, how)]
    if not kept:
        return '', lines
    tip, why = refs.ref_commit('refs/heads/%s' % holder, ctx.root)
    if tip is None:
        raise Failure('cannot read branch `%s` for the body line: %s' % (holder, why))
    if not tip:
        return '', lines
    lines.append('  branch kept: %s at %s' % (holder, tip[:10]))
    return STOPPED_NOTE % (holder, tip[:10]) + '\n', lines


# The body line stop_pull writes, and the pattern resume_line reads it back with, made from it.
STOPPED_NOTE = 'Stopped on branch %s at %s, branch kept.'
STOPPED_RE = re.compile('^%s$' % r'(\S+)'.join(re.escape(p) for p in STOPPED_NOTE.split('%s')),
                        re.M)


def resume_line(body):
    """The line `pull` prints for a record whose body carries stop_pull's note (STOPPED_NOTE),
    naming the branch and tip the work stopped on (the latest note when there are several); None
    when there is none."""
    found = STOPPED_RE.findall(body or '')
    if not found:
        return None
    branch, tip = found[-1]
    return '  resume on %s? Work stopped there at %s, branch kept' % (branch, tip)


# plan_block's `Blocked on M (date): T` line, and the lines that end a stopped pull's wait.
BLOCKED_NOTE_RE = re.compile(r'Blocked on .+? \((\d{4}-\d{2}-\d{2})\): ')
_STOP_ENDED = ('Unblocked (', 'Released (')


def blocked_pull(body):
    """`(branch, tip)` of the pull a `block` stopped and nothing has ended since: the latest
    STOPPED_NOTE is followed straight by plan_block's `Blocked on` line, and no
    `Unblocked (` or `Released (` line comes after it. None otherwise."""
    found = list(STOPPED_RE.finditer(body or ''))
    if not found:
        return None
    rest = body[found[-1].end():].split('\n')[1:]
    blocked = BLOCKED_NOTE_RE.match(rest[0]) if rest else None
    if not blocked or any(line.startswith(_STOP_ENDED) for line in rest[1:]):
        return None
    return found[-1].group(1), found[-1].group(2)


def _git_answer(got, what):
    """`got`'s value, `(value, why)` from a kitlib.git.refs probe; Failure when git cannot tell."""
    value, why = got
    if value is None:
        raise Failure('cannot tell %s: %s' % (what, why))
    return value


def _restore_why_not(snap, ctx, rec, branch, tip):
    """Why the pull `block` stopped on `branch` at `tip` does not come back, None when it does.
    Failure when git cannot tell any of it."""
    what = 'whether the pull of `%s` by %s comes back' % (rec.id, branch)
    names = _git_answer(omod.live_branches(ctx.root, ctx.remote), what)
    if branch not in names:
        return 'no branch or worktree by that name'
    full = _git_answer(refs.resolve(tip, ctx.root, known=True), what)
    if not full:
        return 'its stopped tip %s is gone' % tip
    head = ''
    for ref in ('refs/heads/%s' % branch, 'refs/remotes/%s/%s' % (ctx.remote, branch)):
        head = _git_answer(refs.ref_commit(ref, ctx.root), what)
        if head:
            break
    if not head or not _git_answer(refs.is_ancestor(full, head, ctx.root), what):
        return '%s no longer holds its stopped tip %s' % (branch, tip)
    for ref in ('refs/heads/%s' % ctx.main_branch,
                'refs/remotes/%s/%s' % (ctx.remote, ctx.main_branch)):
        main = _git_answer(refs.ref_commit(ref, ctx.root), what)
        if main and _git_answer(refs.is_ancestor(full, main, ctx.root), what):
            return 'its stopped tip %s has landed on %s' % (tip, ref.split('/', 2)[2])
    holding, _dead, _why = omod.in_flight_split(snap, ctx.root, ctx.remote)
    lead = '; a lead may re-pull it (the pull-collision row)'
    hits = live_hits(rec.all('scope'), holding, skip=rec.id, branch=branch)
    if hits:
        return hits[0] + lead
    folds, notes = _pull_fold_hits(ctx, rec.all('scope'), holding, branch)
    if folds:
        return 'scope folds into %s (%s)%s' % (_fold_where(folds), folds[0], lead)
    # A branch the fold check could not compare is "cannot tell", never "no overlap" (as
    # _branch_changes refuses to measure it): the record opens for a lead to look at.
    unmeasured = [n for n in notes if 'not compared' in n]
    if unmeasured:
        return ('cannot tell whether its scope folds into another pull\'s work (%s)%s'
                % (unmeasured[0][len('NOTE: '):] if unmeasured[0].startswith('NOTE: ')
                   else unmeasured[0], lead))
    return None


def restore_pull(snap, ctx, rec):
    """What opening the blocked `rec` does with the pull its `block` stopped (blocked_pull):
    `(changes, lines, note)`, all empty when there is none. The pull comes back (`state:
    pulled`, `pulled_by` the branch, `pulled_at` today) only while the branch is live by `next`'s
    test (offer.live_branches: a local, remote-tracking or worktree branch by that name), still
    holds the stopped tip, which is on neither the local nor the remote main branch, and its scope neither collides with another live pull nor folds
    into one's work (those are a lead's call, the pull-collision row). Otherwise the record opens
    as before. `note` is the body line that says which, and ends the stopped pull's wait. A git
    that cannot say fails (_restore_why_not)."""
    found = blocked_pull(rec.body)
    if found is None:
        return [], [], ''
    branch, tip = found
    why = _restore_why_not(snap, ctx, rec, branch, tip)
    if why:
        return [], ['  pull by %s not restored: %s' % (branch, why)], \
            'Unblocked (%s): pull by %s not restored: %s.' % (ctx.date(), branch, why)
    changes = [('state', ['pulled']), ('pulled_at', [ctx.date()]), ('pulled_by', [branch])]
    lines = ['  pull restored - state: pulled, pulled_by: %s, pulled_at: %s'
             % (branch, ctx.date())]
    return changes, lines, 'Unblocked (%s): pull by %s restored.' % (ctx.date(), branch)


def stale_stop_note(rec, day, why):
    """The body line that ends `rec`'s stopped pull (blocked_pull) when something other than an
    unblock opens it, None when it has none: `why` says what opened it. It never restores."""
    found = blocked_pull(rec.body)
    if found is None:
        return None
    return 'Unblocked (%s): %s; pull by %s not restored.' % (day, why, found[0])


def _append_line(body, line):
    """`body` with `line` appended as its own paragraph."""
    body = (body or '').rstrip('\n')
    return (body + '\n\n' if body.strip() else '') + line + '\n'


def plan_release(snap, ctx, rid, reason, kept=True):
    """Return a pull to `state: open`: the caller's own pull, or one anybody may stop (stop_pull:
    no branch or worktree by its `pulled_by`, or a worktree `ctx.liveness` finds dead). Pre-emption
    lines go with it, or the released record would keep its pre-emption sort. While the branch
    exists a body line `Stopped on branch B at <tip>, branch kept.` and then `Released (date): T`
    say where the work was left; the branch is never touched. `kept` False (at_landing's release
    of a part landed: the landing's cleanup deletes the branch) writes neither line and reads no
    branch."""
    refuse_machine_paths('--reason', reason)
    bad = reason_error(reason, '--reason')
    if bad:
        raise Usage(bad)
    rec = require(snap, rid)
    if rec.get('state') != 'pulled':
        raise Usage('`%s` is `state: %s`, not `pulled`: there is no pull to release'
                    % (rid, rec.get('state')))
    holder = rec.get('pulled_by')
    if not holder:
        raise Failure('`%s` is pulled with no `pulled_by`; `check` names it' % rid)
    def live(why):
        return ('the pull of `%s` by `%s` is live (a branch or worktree by that name exists)%s. '
                'Its holder releases it: %s'
                % (rid, holder, '; its tree is live: %s' % why if why else '',
                   holder_form(rec, holder, 'release %s --reason "<why>"' % rid)))
    stopped, found = stop_pull(ctx, rid, holder, live, kept)
    changes = [('state', ['open'])] + [(k, []) for k in rmod.PULL_KEYS + rmod.PREEMPT_KEYS]
    pairs = rmod.apply_changes(rec.pairs(), changes)
    text = ' '.join(reason.split())
    body = rec.body
    if stopped:
        body = (body or '').rstrip('\n')
        body = ((body + '\n\n' if body.strip() else '') + '%sReleased (%s): %s\n'
                % (stopped, ctx.date(), text))
    return Change({rid: _text(pairs, body)},
                  'backlog: release %s from %s: %s' % (rid, holder, text),
                  ['RELEASED %s - state: pulled -> open' % rid,
                   '  was pulled_at: %s' % rec.get('pulled_at')] + found)


# --- close / retire -------------------------------------------------------------------------------

def dependent_rewrites(snap, rid, day, gone='closing'):
    """`({stem: text}, lines)`: every record blocked on `item:<rid>`, without that member; one
    left with none takes `blocked_on: -` and, if blocked, `state: open`. One that opens with a
    stopped pull (blocked_pull) gets the body line that ends it (stale_stop_note, dated `day`,
    `gone` saying what became of `rid`), so a later unblock cannot restore it; this never
    restores a pull."""
    target = 'item:%s' % rid
    writes, lines = {}, []
    for other in snap.readable:
        members = other.all('blocked_on')
        if other.id == rid or target not in members:
            continue
        left = [m for m in members if m != target] or ['-']
        opened = left == ['-'] and other.get('state') == 'blocked'
        changes = [('blocked_on', left)] + ([('state', ['open'])] if opened else [])
        note = stale_stop_note(other, day, 'opened by %s %s' % (target, gone)) if opened else None
        body = _append_line(other.body, note) if note else other.body
        writes[other.stem] = _text(rmod.apply_changes(other.pairs(), changes), body)
        lines.append('UNBLOCKED %s - `%s` removed -> %s%s' % (other.id, target, ', '.join(left),
                                                              '; state: open' if opened else ''))
    return writes, lines


def _claim_subject(verb, rid, reason, flag):
    refuse_machine_paths(flag, reason)
    bad = reason_error(reason, flag)
    if bad:
        raise Usage(bad)
    return '%s %s: %s' % (verb, rid, ' '.join(reason.split()))


def refuse_unchecked_end(rec, verb, role_checked):
    """`close`, `retire` and `delete` of an owner-held record (`close-held`), and `retire` or
    `delete` of one the owner or the PM priced (`retire-priced`), are the owner's and the PM's
    (kitlib/backlog/roles.py): without `role_checked` (a landing's close, a library caller) the
    write refuses (refuse_top)."""
    if not role_checked:
        refuse_top(rec.id, verb, policy.sort(verb, {}, rec))


def plan_close(snap, ctx, rid, reason, role_checked=False):
    """The work is done: delete the record. An owner-held record is the `close-held` row, so a
    close without `role_checked` (opener ran the role check; a landing's close, at_landing, runs
    none) refuses it (refuse_unchecked_end)."""
    rec = require(snap, rid)
    subject = _claim_subject('closes', rid, reason, '--reason')
    refuse_unchecked_end(rec, 'close', role_checked)
    writes, lines = dependent_rewrites(snap, rid, ctx.date())
    writes[rid] = None
    lines.insert(0, 'CLOSED %s' % rid)
    if rec.get('state') != 'pulled':
        lines.append('warning: `%s` was `state: %s`, never pulled; it closes anyway'
                     % (rid, rec.get('state')))
    return Change(writes, subject, lines)


def plan_retire(snap, ctx, rid, why, role_checked=False):
    """The work should not be done: delete the record. An owner-held or owner-priced record is
    the owner's and the PM's (refuse_unchecked_end). A pull whose branch is live refuses, as
    `delete` does (`_refuse_live_pulls`); a dead pull goes with the record."""
    rec = require(snap, rid)
    subject = _claim_subject('retires', rid, why, '--why')
    notes = _refuse_live_pulls(snap, ctx, [rec])
    refuse_unchecked_end(rec, 'retire', role_checked)
    writes, lines = dependent_rewrites(snap, rid, ctx.date(), 'retiring')
    writes[rid] = None
    lines.insert(0, 'RETIRED %s' % rid)
    lines[1:1] = notes
    return Change(writes, subject, lines)


def _refuse_live_pulls(snap, ctx, recs, note='  dead pull on `%s` dropped with the record',
                       retarget=False):
    """The guard `retire`, `delete` and `retarget` share: a pulled record in `recs` whose branch
    is live refuses (its holder releases it first; with `retarget`, every live one is named in
    the one refusal, which also offers retargeting after they land); `note % branch` for each
    dead pull (none when `note` is None). The live pulls are read once, and only when one of
    `recs` is pulled. Failure when they cannot be read: "cannot tell" is never "dead"."""
    pulled = [r for r in recs if r.get('state') == 'pulled']
    if not pulled:
        return []
    holding, _dead, probe = omod.in_flight_split(snap, ctx.root, ctx.remote)
    live = set(r.id for r in holding)
    notes, building = [], []
    for rec in pulled:
        holder = rec.get('pulled_by') or '?'
        if probe:
            raise Failure('cannot tell whether the pull of `%s` by `%s` is live: %s'
                          % (rec.id, holder, probe))
        if rec.id in live:
            building.append((rec.id, holder))
            if not retarget:
                break
        elif note:
            notes.append(note % holder)
    if building:
        raise Refusal(_live_pull_refusal(building, retarget))
    return notes


def _live_pull_refusal(building, retarget):
    """The refusal `_refuse_live_pulls` raises for `building`, [(id, branch)] of live pulls."""
    where = '; '.join('`%s` is being built on `%s`' % pair for pair in building)
    releases = ', '.join('`release %s --reason T`' % rid for rid, _b in building)
    if len(building) == 1:
        how = 'release it first (%s, from that branch)' % releases
        return '%s; %s%s' % (where, 'retarget after it lands, or ' if retarget else '', how)
    return '%s; retarget after they land, or release them first (%s, each from its branch)'         % (where, releases)


def plan_delete(snap, ctx, rid, why, role_checked=False):
    """The record should not exist (the owner's delete from the Action Hub, or a coordinator's):
    delete it, with the subject `deletes ID: <why>`, which history reads as `closes`/`retires`
    are read (`add` again needs `--reopen`; a `requires: item:` on it never lands). The owner
    gates are `retire`'s (refuse_unchecked_end). A pull whose branch is live refuses: its
    holder releases it first. A dead pull goes with the record; a parked record's kept branch is
    left for its owner to remove."""
    rec = require(snap, rid)
    subject = _claim_subject('deletes', rid, why, '--why')
    state = rec.get('state')
    notes = _refuse_live_pulls(snap, ctx, [rec])
    if state == 'parked':
        notes.append('  parked on %s; a branch kept for it is not touched: remove it by hand'
                     % (rec.get('q') or 'no recorded question'))
    refuse_unchecked_end(rec, 'delete', role_checked)
    writes, lines = dependent_rewrites(snap, rid, ctx.date(), 'being deleted')
    writes[rid] = None
    return Change(writes, subject, ['DELETED %s' % rid] + notes + lines)


# --- uncite ---------------------------------------------------------------------------------------

# An owner ruling or question id as a record body cites it: `R-` and four or more digits, `Q-` and
# three or more, not glued to a word after it. `T-` ids, `item:` members and an `R-` id of fewer
# than four digits are not ones.
CITE_ID = r'(?:R-[0-9]{4,}|Q-[0-9]{3,})(?![A-Za-z0-9_])'
CITED = re.compile(r'(?<![A-Za-z0-9-])' + CITE_ID)
# One glued to a word before it by a hyphen (`pre-R-nnnn`): no rule reads it, so its line is left
# for a hand edit.
GLUED = re.compile(r'(?<=[A-Za-z0-9]-)' + CITE_ID)
# The header keys `uncite` drops: each holds only a cited id. `resume`, `q` and `priority_per`
# are kept (offer ranking and the park read them).
UNCITED_KEYS = ('priority_ruling', 'hold_ruling')
# One cite as a sentence names it: a `PM ` or `owner ` before it and a `(c)` part after it are its.
_CITE = r'(?:(?:PM|[Oo]wner) )?' + CITE_ID + r'(?:\([a-z0-9]{1,3}\))?'
_RUN = r'(?:, | and |/|; | & )'
# One cite or a run of them (`R-nnnn, R-nnnn and Q-nnn`), never glued to a word before it.
_G = r'(?<![A-Za-z0-9-])' + _CITE + r'(?:' + _RUN + _CITE + r')*'
# Inside a parenthesis a cite may name the part of it meant (`O2`, `Q2`, `b`).
_PART = r'(?: (?:[A-Z][0-9]{1,2}[a-z]?|[a-z]))?'
_GP = r'(?<![A-Za-z0-9-])' + _CITE + _PART + r'(?:' + _RUN + _CITE + _PART + r')*'
# A key or lead word before a cite that goes with it (`value_ruling R-nnnn`, `ref: R-nnnn`).
_KEY = r'(?:(?:[a-z][a-z0-9]*_[a-z0-9_]+|[Rr]ef|[Pp]er|[Ss]ee|PM|[Oo]wner):? )?'
_BULLET = r'\s*(?:[-*] |[0-9]+\. )?'
# A cite (with a part it names) that ends the text before a tag rule's mark: the mark joins a run
# of cites (`by R-nnnn, R-nnnn`), which is no tag, so the run stays whole for the object rule.
_CITE_END = re.compile(CITE_ID + r'(?:\([a-z0-9]{1,3}\))?' + _PART + '$')
# The mark after a leading cite list that goes with it: not when the words after it join on to
# the cites (`(R-nnnn, and the rule)`, `R-nnnn, which says`), which without them reads wrong.
# After a colon `which` and `that` start the text the label names (`Q-nnn: which one?`).
_JOINS = r'(?!(?:and|or|but|nor|plus|then|with)\b)'
_LIST_MARK = r'(?:[:;]|,(?!\s+(?:which|that)\b))\s+' + _JOINS
# The words a cite can follow as their object (`per R-nnnn`, `answered by R-nnnn`).
_PREP = (r'(?:\b(?:[Pp]er|[Uu]nder|[Ii]n|[Bb]y|[Ff]rom|[Ss]ince|[Aa]fter|[Bb]efore|[Uu]ntil|'
         r'[Ff]ollowing|[Ss]ee|[Cc]iting|[Cc]ites|[Oo]n|[Oo]f|[Ww]ith|[Aa]gainst|[Bb]reaks?|'
         r'[Oo]verrides?|[Rr]everses|[Cc]ontradicts|[Rr]espects|[Ff]ollows) )')
# What may follow a cite as an object: punctuation, the line's end, a parenthesis, a joining word.
_AFTER = (r'(?=[.,;:)!?]|$| \(| (?:and|or|but|so|then|which|that|to|for|when|while|because|as|'
          r'until|unless|if|before|after)\b)')
# The words a question id can follow as an object and still read as the question (`waiting on
# Q-nnn`, `asked in Q-nnn`). After any other (`done per Q-nnn`) the id stands for its answer, which
# `an owner question` would not say: that line is left for a hand edit.
_Q_PREPS = ('on', 'in', 'of', 'see', 'citing', 'cites')
_NOUN = r'(?:ruling|question|answer|decision)s?\b'
# What may stand before `ruling`, `question` and the like for the noun rule to drop the cite after
# it: an article, `this`/`that`/`its`, or a possessive, with an `owner` or `PM` between. A bare
# `Owner ruling R-nnnn` has none: dropping the cite would leave `Owner ruling wants`.
_DET = r"(?:The|the|[Aa]n?|[Tt]hat|This|this|[Ii]ts|[Oo]wner's|PM's)"
# What a rewrite can leave that reads wrong: an empty or space-led parenthesis, one opened at the
# line's end, a space before punctuation, a doubled comma or space.
_ODD = re.compile(r'\(\s*\)|\( |\($|\s[,;.]|,,|\S  \S')


def _opens_at(s, i, opens):
    """Whether position `i` of line `s` starts a sentence: the line's start (after a bullet) when
    the line `opens` one, or just after `.`, `!` or `?` and a space."""
    before = s[:i]
    if re.fullmatch(_BULLET, before):
        return opens
    return re.search(r'[.!?]\s+$', before) is not None


def _words(cites, alone):
    """The words for the cite run `cites`: `an owner ruling` / `an owner question` for one (`an
    owner` when `alone` is False: a noun follows), `owner rulings` / `owner questions` for more
    of one kind (`owner`), None for a run that mixes rulings and questions."""
    ids = CITED.findall(cites)
    kinds = {i[0] for i in ids}
    if len(kinds) != 1:
        return None
    noun = 'question' if kinds == {'Q'} else 'ruling'
    if len(ids) == 1:
        return 'an owner %s' % noun if alone else 'an owner'
    return 'owner %ss' % noun if alone else 'owner'


def _uncite_rules(opens, state):
    """The rewrites in the order they run, each `(pattern, replacement)`. The first ones drop a
    cite that is a tag on the sentence (in a parenthesis, after a comma, a label before a colon,
    one cite after a noun with an article that already says it); the rest put `an owner ruling` /
    `an owner question` where the cite is a word of the sentence: an object (`per R-nnnn`), before
    a noun after a preposition (`under the R-nnnn ruling`), or a dated label of rulings opening a
    line. No rule
    words a cite that is a subject (`R-nnnn says`) or a possessive (`R-nnnn's`): `an owner
    ruling` there would say some ruling where the line meant one. A tag rule whose mark follows a
    cite leaves the run (`by R-nnnn, R-nnnn`) for the object rule, which keeps it plural; one whose
    list would leave a joining word first (`(R-nnnn, and the rule)`) does not run. A cite no rule
    places, a run a rule cannot word, or a word rule at the start of a line that continues a
    sentence, sets state['hand']: uncite_line then leaves the line as it was. state['words']
    counts the word rules that ran."""
    def hand(m):
        state['hand'] = True
        return m.group(0)

    def tag(m):
        # a mark after a cite joins a run of them: no tag, left whole for the object rule
        return m.group(0) if _CITE_END.search(m.string[:m.start()]) else ''

    def continued(m):
        # the line's first words continue the line before's sentence: no rule knows the word
        # before the cite
        return not opens and m.start('pre') == 0 and re.fullmatch(_BULLET, m.group('pre'))

    def prefix(m):
        if not m.group(2) and not opens:
            return hand(m)
        nxt = m.group(3)
        if not m.group(2):
            nxt = nxt.upper()
        return m.group(1) + m.group(2) + nxt

    def noun(m):
        # one cite after the noun: more (`the ruling R-nnnn and Q-nnn say`) would leave the
        # noun saying one where the line named several
        return hand(m) if len(CITED.findall(m.group(2))) > 1 else m.group(1)

    def word(alone=True, suffix='', obj=False, rulings=False):
        def rep(m):
            text = _words(m.group('c'), alone)
            q_word = 'question' in (text or '')
            if text is None or continued(m) or (rulings and q_word) or (
                    obj and q_word and m.group('pre').strip().lower() not in _Q_PREPS):
                return hand(m)
            state['words'] += 1
            start = m.end('pre')
            if _opens_at(m.string, start, opens):
                text = text[:1].upper() + text[1:]
            return m.group('pre') + text + suffix
        return rep

    return [
        # a parenthesis holding only cites (a lead word and parts with them): gone, with the
        # space before it (never the line's indent)
        (r'(?:(?<=\S)\s+)?\(' + _KEY + _GP + r'\)', ''),
        # a sentence that only points at cites: `See R-nnnn.`
        (r'^(' + _BULLET + r')[Ss]ee ' + _G + r'\.\s*', r'\1'),
        (r'(?<=[.!?]) [Ss]ee ' + _G + r'\.', ''),
        # `(CITES: rest`, `rest; CITES)`, `rest, CITES, more`
        (r'\(' + _KEY + _GP + _LIST_MARK, '('),
        (r'[,;]\s+' + _KEY + _GP + r'(?=\))', tag),
        (r'[,;]\s+' + _KEY + _G + r'(?=[,;]\s)', tag),
        # a trailing `, CITES` (or `, key CITES`) before the line's end, a full stop or a
        # parenthesis
        (r'\s*[,;|]\s*' + _KEY + _G + r'(?=\s*(?:\.|\(|$))', tag),
        # a leading `CITES: ` or `CITES, ` (after a bullet and a short `Note: ` style label)
        (r'^(' + _BULLET + r')((?:[A-Za-z][A-Za-z _-]{0,20}: )?)' + _G + r'(?!;)' + _LIST_MARK +
         r'(\S?)', prefix),
        # `the ruling CITES`, `an owner question CITES`: the noun already says it
        (r'\b(' + _DET + r" (?:(?:[Oo]wner|PM)(?:'s)? )?" + _NOUN + r') (' + _G + r')', noun),
        # `R-nnnn (2026-10-07): ...` opening a line; a `Q-nnn (date):` label names the answer,
        # which `an owner question` would not say
        (r'^(?P<pre>' + _BULLET + r')(?P<c>' + _G + r')(?= \([0-9]{4}-[0-9]{2}-[0-9]{2}\):)',
         word(rulings=True)),
        # `under the R-nnnn ruling`
        (r'(?P<pre>' + _PREP + r')(?:(?:The|the) )?(?P<c>' + _G + r') (?=' + _NOUN + r')',
         word(alone=False, suffix=' ')),
        # `per R-nnnn`, `answered by R-nnnn`
        (r'(?P<pre>' + _PREP + r')(?P<c>' + _G + r')' + _AFTER, word(obj=True)),
    ]


def uncite_line(line, opens=True):
    """`(after, hand)`: one body line with each owner ruling or question id (CITE_ID) rewritten
    into plain words (_uncite_rules), or the line as it was and `hand` True where no rule words it
    cleanly: a cite no rule places, one glued to a word by a hyphen (GLUED), more than one cite
    put into words in the line (distinct rulings would read as one), or odd punctuation (_ODD) or
    indentation the rewrite left. Such a line is listed for a hand edit and never rewritten: no
    rewrite says `the owner` or `the owner's`. `opens`: the line starts a sentence (the line before
    it is blank, absent, or ends a sentence). A line left opening on a mark (`, then x`, after a
    parenthesis went) is the body's to join (_uncite_body). A `Blocked on M (date): T` line
    (record.BLOCK_NOTE, written by plan_block) has only its reason T worded, as a line that opens
    a sentence; one whose reason would be left empty or opening on a mark, or that has no such
    prefix, is left for a hand edit. Idempotent: a line with no cite comes back as it was."""
    if not (CITED.search(line) or GLUED.search(line)):
        return line, False
    if line.startswith(rmod.BLOCK_NOTE):
        head = _BLOCK_HEAD.match(line)
        if not head:
            return line, True
        reason, hand = _uncite_text(line[head.end():], True)
        if hand or not reason.strip() or re.match(r'[,;.:]', reason):
            return line, True
        return head.group(0) + reason, False
    return _uncite_text(line, opens)


# The prefix of the body line plan_block writes for `--reason`: `Blocked on M (yyyy-mm-dd): `.
_BLOCK_HEAD = re.compile(re.escape(rmod.BLOCK_NOTE) + r'[^()]*\([0-9]{4}-[0-9]{2}-[0-9]{2}\): ')


def _uncite_text(line, opens):
    """uncite_line for one line of text that is not a `Blocked on` line: a body line, or a
    `summary` or `ref` value (plan_uncite)."""
    if not (CITED.search(line) or GLUED.search(line)):
        return line, False
    if GLUED.search(line):
        return line, True
    opens = opens or re.match(r'\s*(?:[-*]|[0-9]+\.) ', line) is not None
    state = {'hand': False, 'words': 0}
    after = line
    for pattern, rep in _uncite_rules(opens, state):
        after = re.sub(pattern, rep, after)
    if re.fullmatch(r'\s*(?:[-*]|[0-9]+\.)?\s*', after):
        return '', False                     # only cites (and a bullet): the line goes
    lead = re.match(r'\s*', line).group(0)
    joined = re.sub(r'^(\s*)[,;.:]\s*', r'\1', after)    # a leading mark is the body's to join
    if (state['hand'] or state['words'] > 1 or CITED.search(after) or
            len(_ODD.findall(joined)) > len(_ODD.findall(line)) or
            (after.strip() and re.match(r'\s*', after).group(0) != lead)):
        return line, True
    return after, False


def _uncite_body(body, keep_block=False):
    """`(new body, rewritten, hand)`: `rewritten` the `[before, after]` of each line the rewrite
    changed, in order (`after` None for a line that was only cites and goes); `hand` the
    `(n, line)` of each line uncite_line left for a hand edit, `n` its 1-based place in the new
    body. `keep_block`: each `Blocked on` line that cites one is left for a hand edit unworded
    (it is a hold's only citation: plan_uncite). A rewritten line left opening on a stray mark (`, not this one.` once a parenthesis of
    cites went) gives the mark to the line above it, or drops it where that line is blank, a
    heading or already ends on a mark, capitalising what is left after a blank line, a heading or
    a finished sentence; it is left for a hand edit when the line above is one."""
    out, rewritten, hand = [], [], []
    at = {}                                  # out index -> its rewritten entry
    prev = ''
    for line in (body or '').split('\n'):
        opens = not prev.strip() or prev.rstrip()[-1:] in '.!?:'
        prev = line
        if keep_block and line.startswith(rmod.BLOCK_NOTE) and CITED.search(line):
            after, needs = line, True
        else:
            after, needs = uncite_line(line, opens)
        stray = re.match(r'(\s*)([,;.:])\s*', after)
        if stray and not needs and after != line and not re.match(r'\s*[,;.:]', line):
            above = out[-1] if out else ''
            if hand and hand[-1][0] == len(out):
                needs = True
            else:
                rest = after[stray.end():]
                if above.strip() and above.rstrip()[-1:] not in '.,;:!?' and \
                        not above.lstrip().startswith('#'):
                    out[-1] = above.rstrip() + stray.group(2)
                    if len(out) - 1 in at:
                        at[len(out) - 1][1] = out[-1]
                    else:
                        at[len(out) - 1] = [above, out[-1]]
                        rewritten.append(at[len(out) - 1])
                elif (not above.strip() or above.rstrip()[-1:] in '.!?' or
                      above.lstrip().startswith('#')):
                    # the line starts a sentence (after a blank line, a finished one or a
                    # heading): it opens with a capital
                    rest = rest[:1].upper() + rest[1:]
                after = stray.group(1) + rest if rest else ''
        if needs:
            out.append(line)
            hand.append((len(out), line))
        elif after != line and not after.strip():
            rewritten.append([line, None])
        elif after != line:
            out.append(after)
            at[len(out) - 1] = [line, after]
            rewritten.append(at[len(out) - 1])
        else:
            out.append(line)
    return '\n'.join(out), rewritten, hand


UNCITED_FIELDS = ('summary', 'ref')     # the header values uncite words as it words a body line


def _uncite_fields(pairs):
    """`(pairs, worded, hand)` for one record's header pairs: each UNCITED_FIELDS value that cites
    one worded as a line that opens a sentence (_uncite_text). `worded` the `(key, before,
    after)` of each value changed, `after` None for a `ref` of only cites, which is dropped;
    `hand` the `(n, key, value)` of each value kept for a hand edit, `n` its line in the written
    record: one no rule words cleanly, a `summary` of only cites (it is required) and a value
    left opening on a mark."""
    out, worded, hand = [], [], []
    for key, value in pairs:
        if key in UNCITED_FIELDS and (CITED.search(value) or GLUED.search(value)):
            after, needs = _uncite_text(value, True)
            if key == 'ref' and re.fullmatch(r'\s*' + _KEY + _GP + r'\s*\.?\s*', value):
                after, needs = '', False         # only cites: no rule takes a bare one
            if not needs and not after.strip() and key == 'ref':
                worded.append((key, value, None))
                continue
            if needs or not after.strip() or re.match(r'\s*[,;.:]', after):
                hand.append((len(out) + 1, key, value))
            else:
                worded.append((key, value, after.strip()))
                value = after.strip()
        out.append((key, value))
    return out, worded, hand


def _hold_needs_block_lines(rec, body):
    """Whether `rec` is a blocked record with a `deliberate` or `owner` hold whose citations
    (record.hold_citations) would all be gone with its `Blocked on` lines worded as in `body`:
    nothing in `q`, `owner_ask` or `blocked_on` cites one, and no `Blocked on` line of `body`
    does. Its `Blocked on` lines that cite one are then kept for a hand edit, so `check`'s hold
    citations still read the hold."""
    if rec.get('state') != 'blocked' or not any(
            m in rmod.UNCITED_KINDS for m in rec.all('blocked_on')):
        return False
    if any(rmod.cites(v) for key in ('q', 'owner_ask', 'blocked_on') for v in rec.all(key)):
        return False
    return not any(rmod.cites(line) for line in (body or '').split('\n')
                   if line.startswith(rmod.BLOCK_NOTE))


def plan_uncite(snap, ctx):
    """Rewrite every owner ruling and question id (CITE_ID) cited in the readable records' bodies,
    their `Blocked on` lines' reasons among them, and in their `summary` and `ref` values
    (UNCITED_FIELDS) into plain words (uncite_line), and drop the header keys that hold only one
    (UNCITED_KEYS), in one store commit. `owner_ask`, `q`, `resume`, `priority_per` and
    `requires` are kept as they are, and so is a `Blocked on` line that is a deliberate or owner
    hold's only citation (_hold_needs_block_lines). A line or value no rule words cleanly is left
    as it was and listed under NEEDS HAND EDIT, with the store, the record and its line in the
    written record, in the dry run and in the `--apply` output alike. A second run rewrites
    nothing; it lists the lines still left for a hand edit."""
    writes, out, hands = {}, [], []
    cites = body_lines = fields = headers = 0
    for rec in snap.readable:
        body, rewritten, hand = _uncite_body(rec.body)
        only = _hold_needs_block_lines(rec, body)
        if only:
            body, rewritten, hand = _uncite_body(rec.body, keep_block=True)
        drop = [(k, rec.get(k)) for k in UNCITED_KEYS if rec.has(k)]
        pairs, worded, field_hand = _uncite_fields(
            [(k, v) for k, v in rec.pairs() if k not in UNCITED_KEYS])
        hands += ['  %s %s line %d (%s): %s' % (snap.where, rec.stem, n, key, value.strip())
                  for n, key, value in field_hand]
        # the header, its blank line, then the body: line n of the body is line len(pairs)+1+n
        hands += ['  %s %s line %d: %s%s' % (
            snap.where, rec.stem, len(pairs) + 1 + n, line.strip(),
            " (the hold's only citation)" if only and line.startswith(rmod.BLOCK_NOTE) else '')
            for n, line in hand]
        if not rewritten and not drop and not worded:
            continue
        writes[rec.stem] = _text(pairs, body if rewritten else rec.body)
        out.append('  %s' % rec.stem)
        for key, before, after in worded:
            cites += len(CITED.findall(before))
            out += ['    - %s: %s' % (key, before),
                    '    + %s: %s' % (key, after if after is not None else '(dropped)')]
        fields += len(worded)
        for before, after in rewritten:
            cites += len(CITED.findall(before))
            out += ['    - %s' % before.strip(),
                    '    + %s' % (after.strip() if after is not None else '(line removed)')]
        for key, val in drop:
            out.append('    dropped %s: %s' % (key, val))
        body_lines += len(rewritten)
        headers += len(drop)
    tail = ['NEEDS HAND EDIT (%d line(s) left unchanged: no rule words their cites cleanly; edit '
            'them by hand)' % len(hands)] + hands if hands else []
    if not writes:
        return Change({}, 'backlog: uncite', ['UNCITE: nothing to rewrite in %s%s' % (
            snap.where, '; %d line(s) still cite one' % len(hands) if hands else
            ': no owner ruling or question id is cited')] + tail)
    lines = ['UNCITE: %d cite(s) in %d record(s): %d body line(s) rewritten, %d field(s) '
             'rewritten, %d header line(s) dropped, %d line(s) left for a hand edit'
             % (cites, len(writes), body_lines, fields, headers, len(hands))] + out + tail
    return Change(writes, 'backlog: uncite %d cite(s) in %d record(s)' % (cites, len(writes)),
                  lines)
