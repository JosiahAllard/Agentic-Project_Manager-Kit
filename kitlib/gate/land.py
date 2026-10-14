"""One branch landed on main (docs/gate.md "Landing"; ported from an earlier per-repository
tools/land.py).

`land()` runs in the repository's MAIN checkout, on its main branch:

  1  usage       --expect-main (optional) and every --expect-tip are full hashes, one tip per
                 branch, the branches distinct; the lane is `full` or one `.kit.json`
                 "landing".lanes declares (read from the config at the main checkout's HEAD)
  2  refusals    not the main checkout, HEAD not on main, no such branch, a branch is main, the
                 lane's branch prefix does not match, more branches than "landing".batch_max:
                 REFUSED, nothing changed
  3  turn        ONE booking in the machine lock (kitlib.lock.run.book): a gate run of the
                 config's "gate".kind for <repo>/<branch[+branch...]>, holding the resource
                 `landing:<git common dir>` at priority "land" ("The queue" below). At once, or
                 waited for up to --wait seconds (default "landing".wait_s); not given: the lock's
                 code and a NOT RUN line, nothing changed. The merged gate (7) runs in this slot
                 and books nothing more
  4  lock        fetch <remote>, then the landing lock file (kitlib.records.landing), with what is
                 left of --wait; not had: BUSY. Held at phase stuck: BUSY at once; held by a
                 process that is surely gone: broken and taken unless something is unpushed
  5  follow      fetch again under the lock; HEAD still on main, or REFUSED. The base is <remote>/<main> as fetched: a local main
                 only behind it (nothing unpushed) is fast-forwarded first (`git merge --ff-only`,
                 HEAD on main, a clean checkout); a local main with commits the remote lacks, or
                 one the fast-forward cannot move: REFUSED, as before. When the base is not the
                 commit the config was read at, its config is read at the base (it must name the
                 same main branch, remote and landing lock, and still take this landing). With
                 --expect-main given and not the base, it says `main moved on from X to Y; merging
                 onto Y`; it never refuses for that. Then the locked checks: the branch tip ==
                 --expect-tip, a clean main checkout, no merge in progress, the branch not already
                 in main, (a lane) every changed path in the lane's globs, every branch commit that
                 changes an owner-only path the landing would change authored by the owner
                 (kitlib.guard.ownercommits; rules of the base merged with the tip's), and the
                 branch's history free of the backlog store's root commit (`ref` and `branch`
                 stores; kitlib.backlog.at_landing). Any miss: REFUSED, the lock released, nothing
                 merged
  6  merge       `git merge --no-ff <tip>`; a conflict is aborted (main back at the base), the lock
                 released: FAILED, the verdict line `CONFLICT <branch> with main <base>: the
                 author merges main`
  7  gate        the merged tree gated in the slot of step 3 (kitlib.gate.runner.merged_gate,
                 slot=) with the config of the base; a step that passed in the branch gate of
                 exactly this tree may count without running (kitlib.gate.reuse; never with
                 --fresh, --full or a batch). Red, or a gate where nothing ran: the merge
                 is undone with `git reset --keep <base>` when HEAD is on main,
                 main is exactly one merge ahead and no tracked file changed (untracked files a
                 step left are kept and named), the lock released: FAILED / REFUSED. Never
                 undone once <remote>/<main> holds the merge (someone pushed it while the gate
                 ran): STUCK, and the report says no reset
  7b unchanged   green: HEAD on main at the merge, no tracked change, no untracked file; else
                 undone as on red and nothing pushed (the pushed commit is the gated one).
                 Except (adopt): someone committed on top of the merge in the main checkout and
                 pushed while the gate ran: <remote>/<main> == the tracking ref == main, HEAD on
                 main, no tracked change, no untracked file, and main's first-parent line beyond
                 the base is commits on top of exactly this landing's merges. Then the landing is
                 finished (11, 12) with a LANDED line naming those commits: OK when they select no
                 gate step (the merge's gate-OK record is written), LANDED_UNGATED (exit 9) when
                 they select one (a WARNING naming the full gate to run; no record)
  8  record      green: the gate-OK record for the merge commit (pre-push reads it), its
                 config_rev the base
  9  push        `git push <remote> <merge>:refs/heads/<main>`, never forced
  10 verify      `git ls-remote` must show the merge on <remote>/<main>; then the tracking ref
                 is fetched, the lock file and the slot released: OK (LANDED)
  11 close       after the lock: every record the `ref` or `branch` store still holds pulled by
                 the branch is closed with reason `landed <merge>`, one store commit each
                 (kitlib.backlog.at_landing), and so is every pulled record a `Closes: <ID>`
                 trailer in `<tip> --not <the main it merged onto>` names, whoever pulled it (a
                 pull made in the main checkout records main; a live holder outside the batch is
                 a WARNING: tell its builder). A close that fails, or an exception or Ctrl+C here,
                 is a WARNING naming the command to finish it; the landing stands
  12 cleanup     then each landed branch goes (kitlib.gate.cleanup; docs/gate.md "Branch
                 cleanup"): its remote branch, its worktree (kitlib.worktree.remove, never
                 forced) and its local branch (`git branch -d`). What main does not contain, a
                 worktree with work in it or in use, and anything git refuses are kept and named;
                 an exception or Ctrl+C here is a WARNING naming `land.py --sweep-landed`. The
                 landing stands whatever is kept. --keep-branch (keep_branch) skips it

An exception or Ctrl+C under the lock (recover): before the merge the lock is released; after it
the merge is undone and the lock released, and KeyboardInterrupt is raised again (exit 130); one
that ends STUCK is returned as STUCK (exit 3) whatever raised it. After the push is verified
(phase pushed) the landing is LANDED whatever stops it: a lock release that fails, or an
exception or Ctrl+C there, is LANDED with a WARNING (exit 0; exit 9 when adopt already found main
not gated whole). An adopt counts as a verified push. The slot of step 3 is released whenever the
landing ends (Lander.run): with the lock file, or at the end; STUCK included (the stuck lock file
keeps the next landing out).

A landing's verdict is its exit code and its LANDED / NOT LANDED / CONFLICT / STUCK line; a `GATE
OK` line appears only in a landing that LANDED (a green merged gate that is not landed is
reworded).

Anything that goes wrong after the merge and cannot be undone safely (an undo that does not
verify, a push interrupted, refused or unverified, git failing) is STUCK: the landing lock stays
held at phase `stuck` so no other landing starts on top of it, and the message says what is left
and the commands that clear it.

The queue: a landing is ONE entry in the machine lock's queue (step 3; kitlib.lock.run.book with
`resource=queue_key(<git common dir>)`, `priority="land"`). The resource lets one landing of a
repository hold at a time, every checkout of it sharing the key; the others wait in the machine
lock's queue, landings ahead of ordinary waiting runs. `land.py --queue` (queue()) lists the
entries of priority "land". The lock file (step 4) stays: the stuck record, a gone holder's break,
and a landing of a tool outside the machine lock.

A batch (several branches, one --expect-tip each, merged in the order given): every branch passes
the locked checks, and no tip may contain another; the branches are merged one after another with
--no-ff, the merged tree is gated once against the base, and the last merge is pushed. A
conflict on any branch aborts that merge and undoes the earlier ones; a red gate undoes them all.
Each branch gets its own result record.

A submitted landing (kitlib.gate.submit; `land.py --submit`) is this landing run by a detached
process: its records are written `running` before it starts, and every outcome finishes them.
"""
import itertools
import os
import time

from kitlib import gate as G
from kitlib.backlog import at_landing
import kitlib.config as kconfig
from kitlib.config.types import ConfigError
from kitlib.gate import cleanup, result, reuse, runner
from kitlib.gate import detect
from kitlib.gate import steps as S
from kitlib.git import cmd, refs, repo
from kitlib.guard import ownercommits
from kitlib.lock import queue as lockqueue
from kitlib.lock import run as lockrun
from kitlib.lock.store import EXIT_USAGE, LockError, Store
from kitlib.records import gate_ok
from kitlib.records import landing

PUSH_TIMEOUT = 600      # seconds
FETCH_TIMEOUT = 300
MERGE_TIMEOUT = 300
POLL_S = 5.0
STEP_CMD = '    $ '      # a STUCK clear step's command line starts with this


class _Stuck(Exception):
    pass


def _paths(entries):
    return ', '.join('%s %s' % (xy.strip(), p) for xy, p in entries[:10]) or 'none'


def _leftover_lines(root, untracked):
    if not untracked:
        return []
    return ['the main checkout needs cleaning: the gate left untracked file(s) %s in %s; they block '
            'the next landing until removed' % (', '.join(untracked[:10]), root)]


def _as_list(x):
    """A branch or tip given alone, or several (a batch), as a list."""
    return [x] if x is None or isinstance(x, str) else list(x)


def _what(e):
    """An exception as a report says it: `Ctrl+C`, or `Type: message` on one line."""
    if isinstance(e, KeyboardInterrupt):
        return 'Ctrl+C'
    return ' '.join(('%s: %s' % (type(e).__name__, e)).split())


def queue_key(common):
    """The resource a landing holds in the machine lock (kitlib.lock.run.book `resource=`): its
    repository's git common dir, so every checkout of one repository shares one key and one
    landing of it holds at a time."""
    return 'landing:' + os.path.normcase(os.path.abspath(common))


def _not_landed(lines):
    """A green merged gate's lines in a landing that is not landed: its `GATE OK` verdict line is
    reworded, so a reader keying on `^GATE (OK|FAILED)` never takes a NOT LANDED for a pass."""
    return ['merged gate passed' + l[len('GATE OK'):] if l.startswith('GATE OK') else l
            for l in lines]


class Lander(object):
    """One landing of a branch, or a batch: `branch` and `expect_tip` are each a string or a list
    of the same length. `merged_gate(lander) -> Outcome` replaces the merged-tree gate (tests);
    `git_env` is the environment git's writes get (None: this process's, scrubbed); `book_kw`
    goes to kitlib.lock.run.book (its `store`: the machine lock's, default the machine store);
    `book` replaces kitlib.lock.run.book (tests); `lock_kw` goes to LandingLock (tests pass
    process probes). `wait_s` None: the config's "landing".wait_s; one budget covers the booking
    and the lock file. `keep_branch` True: no branch cleanup after the landing (step 12).
    `fresh` True: the merged gate runs every step, reusing none (kitlib.gate.reuse).
    `submitted` (kitlib.gate.submit.restore): the detached landing of a --submit, a dict with
    `runs` (the result.Run per branch whose running records the submission wrote; every outcome
    finishes them), `output` (the file this process's output goes to) and `replaced` (the land
    records those running records replaced)."""

    def __init__(self, root, branch, expect_main, expect_tip, lane=landing.FULL, summary=None,
                 full=False, wait_s=0, say=None, book_kw=None, git_env=None, lock_kw=None,
                 merged_gate=None, log_dir=None, poll_s=POLL_S, pid=None, keep_branch=False,
                 fresh=False, book=None, submitted=None):
        self.root, self.lane = root, lane
        self.branches, self.tips = _as_list(branch), _as_list(expect_tip)
        self.branch = ', '.join(str(b) for b in self.branches)     # how the landing is named
        self.expect_main_given = expect_main or None    # optional; never a refusal
        self.expect_main = None     # the base: the commit the config was read at, then (step 5)
                                    # <remote>/<main> as fetched under the lock
        self.summary, self.full, self.wait_s, self.poll_s = summary, full, wait_s, poll_s
        self.say = say or (lambda line: None)
        self.book_kw, self.git_env, self.lock_kw = book_kw, git_env, dict(lock_kw or {})
        self.book_fn = book         # None: kitlib.lock.run.book, looked up when the landing books
        self.slot = None            # the machine-lock Slot the landing holds (book_turn)
        self.store = None
        self.deadline = None        # the --wait budget's end, set when the landing books
        self.merged_gate_fn, self.log_dir = merged_gate, log_dir
        self.pid = os.getpid() if pid is None else pid
        self.lock = self.token = self.cfg = self.merge = None
        self.merges = []            # the merge commits made, in order; self.merge is the last
        self.phase = 'before'
        self.gate_logs = None       # the merged gate's log folder, once it ran
        self.gate_out = None        # the merged gate's Outcome, once it ran
        sub = submitted or {}
        self.submitted = submitted is not None
        # kitlib.gate.result.Run per branch: once the locked checks passed; a submitted landing's
        # from its start (the submission wrote them running)
        self.result_runs = list(sub.get('runs') or [])
        self.own_runs = set(rr.run_id for rr in self.result_runs)
        self.output_log = sub.get('output')
        self.replaced = [r for r in (sub.get('replaced') or []) if isinstance(r, dict)]
        self.keep_branch = keep_branch  # True: no branch cleanup after the landing
        self.cleaned = False        # the branch cleanup ran (clean_up)
        self.cleanup = None         # its facts: {branch: kitlib.gate.cleanup entry, or None}
        self.fresh = fresh          # True: the merged gate reuses no branch-gate step
        self.prior = None           # result records naming the tips, read before this landing
                                    # writes its own (_locked); None: reuse nothing
        self.landed_at = None       # the remote's main once verified to hold the merge: the merge
                                    # (push) or a commit on top of it (adopt)
        self.ungated = None         # adopt's WARNING that main was not gated whole, once said

    @property
    def pairs(self):
        return list(zip(self.branches, self.tips))

    # ---------------------------------------------------------------- git

    def git(self, *args, timeout=cmd.GIT_TIMEOUT):
        r = cmd.call(list(args), self.root, timeout=timeout, env=self.git_env)
        return r

    def ref(self, name):
        sha, _ = refs.resolve(name, self.root)
        return sha

    @property
    def main_ref(self):
        return 'refs/heads/%s' % self.cfg.main_branch

    @property
    def origin_ref(self):
        return 'refs/remotes/%s/%s' % (self.cfg.remote, self.cfg.main_branch)

    def fetch(self):
        r = self.git('fetch', '--quiet', '--no-tags', self.cfg.remote, timeout=FETCH_TIMEOUT)
        if r.rc != 0:
            return 'git fetch %s failed: %s' % (self.cfg.remote, r.err or 'exit %s' % r.rc)
        return None

    # ---------------------------------------------------------------- steps

    def usage(self):
        if len(self.tips) != len(self.branches):
            return 'one --expect-tip per branch: %d branch(es), %d tip(s)' % (
                len(self.branches), len(self.tips))
        given = [('--expect-main', self.expect_main_given)] if self.expect_main_given else []
        for flag, sha in given + [('--expect-tip', t) for t in self.tips]:
            if not (isinstance(sha, str) and refs.SHA_RE.fullmatch(sha)):
                return '%s must be a full commit hash, not %r' % (flag, sha)
        for b in self.branches:
            if not b or not isinstance(b, str) or refs.bad_rev(b):
                return 'the branch to land must be a branch name, not %r' % (b,)
        if len(set(self.branches)) != len(self.branches) or len(set(self.tips)) != len(self.tips):
            return 'a batch names each branch and each tip once'
        return None

    def preconditions(self):
        place = repo.classify(self.root)
        if place.kind != repo.MAIN:
            return ('%s is not a main checkout (%s): a landing runs in the main checkout, on its '
                    'main branch' % (self.root, place.kind))
        self.root = place.top
        head = self.ref('HEAD')
        if not head:
            return 'the main checkout %s has no HEAD commit' % self.root
        try:
            self.cfg = kconfig.load_at(self.root, head)
        except ConfigError as e:
            return 'the config of main %s: %s' % (head[:10], e)
        self.expect_main = head     # until step 5 moves it to <remote>/<main>
        return self.config_refusal()

    def config_refusal(self):
        """The refusals self.cfg decides (lane, main branch checked out, batch_max, the branches
        and their prefixes): None, a refusal, or 'USAGE: ...' for a lane the config lacks."""
        lanes = self.cfg.section('landing')['lanes']
        if self.lane != landing.FULL and self.lane not in lanes:
            return 'USAGE: no lane %r (lanes: %s)' % (self.lane, ', '.join(
                [landing.FULL] + sorted(lanes)))
        current, why = refs.current_branch(self.root)
        if current != self.cfg.main_branch:
            return 'the main checkout is on %s, not %s' % (current or why or '(detached HEAD)',
                                                         self.cfg.main_branch)
        most = self.cfg.section('landing')['batch_max']
        if len(self.branches) > most:
            return 'a batch of %d branches is more than "landing".batch_max (%d)' % (
                len(self.branches), most)
        prefixes = (lanes.get(self.lane) or {}).get('branch_prefix') or []
        for b in self.branches:
            if b == self.cfg.main_branch:
                return 'the branch to land is %s itself' % b
            if not self.ref('refs/heads/%s' % b):
                return 'no branch %s in this repository' % b
            if self.lane != landing.FULL and prefixes and not any(b.startswith(p) for p in prefixes):
                return 'lane %s lands branches starting %s; %s does not' % (
                    self.lane, ' or '.join(prefixes), b)
        return None

    def queue_store(self):
        if self.store is None:
            self.store = (self.book_kw or {}).get('store') or Store()
        return self.store

    def wait_budget(self):
        wait_s = self.wait_s if self.wait_s is not None else self.cfg.section('landing')['wait_s']
        return max(0, wait_s or 0)

    def book_turn(self):
        """Step 3: the landing's ONE booking in the machine lock, waited for up to the --wait
        budget: a gate run of the config's "gate".kind for <repo>/<branches>, holding the
        resource queue_key(<git common dir>) at priority "land". The merged gate runs in this
        slot (self.slot). Raises kitlib.lock.store.LockError when the lock does not give it, and
        landing.LockError when the landing lock's place cannot be worked out."""
        lanes = sorted(self.cfg.section('landing')['lanes'])
        self.lock = landing.LandingLock.for_config(self.root, self.cfg, lanes, **self.lock_kw)
        wait_s = self.wait_budget()
        kw = dict(self.book_kw or {}, store=self.queue_store(), gate=True,
                  resource=queue_key(self.lock.common), priority='land', wait_s=wait_s,
                  no_wait=wait_s == 0, note='landing %s: %s' % (self.lane, self.branch))
        if self.submitted:
            kw['detached'] = True   # its launcher's ancestry is in ORIGIN_ENV (kitlib.gate.submit)
        self.deadline = time.monotonic() + wait_s
        book = self.book_fn or lockrun.book
        self.slot = book(self.cfg.section('gate')['kind'], runner.repo_name(self.cfg, self.root),
                         runner.slug_of('+'.join(self.branches)), **kw)
        self.say(self.slot.held_text())

    def take(self):
        """Step 4, the slot held: None when this landing holds the landing lock file; else why not
        (BUSY), once what is left of the --wait budget ran out. Raises landing.LockError or
        ConfigError (REFUSED)."""
        said = False
        while True:
            why, final = self.try_take()
            if why is None:
                return None
            left = (self.deadline or 0) - time.monotonic()
            if final or left <= 0:
                return why
            if not said:
                said = True
                self.say('waiting up to %d s for the landing lock: %s' % (left, why))
            time.sleep(max(0.01, min(self.poll_s, left)))

    def try_take(self):
        """One look: `(None, False)` taken, `(why, final)` not; final: waiting cannot help."""
        try:
            rec = self.lock.take(self.lane, self.branches, pid=self.pid)
        except landing.Busy as e:
            return self.held_by(e.record)
        self.token = rec['token']
        return None, False

    def held_by(self, rec):
        """`(None, False)` when a gone holder's lock was broken and taken; else `(why, final)`."""
        if rec.get('phase') == 'stuck':
            return ('%s: a stuck landing is cleared by hand, as its STUCK report says'
                    % landing.describe(rec)), True
        if rec.get('lane') == '?' or self.lock.holder_state(rec) != 'dead':
            return landing.describe(rec), False         # alive, a token holder, or unreadable
        why = self.lock.unpushed()
        hint = self.behind_hint(self.ref(self.main_ref), self.ref(self.origin_ref)) if why else ''
        if hint and not os.path.exists(os.path.join(self.lock.common, 'MERGE_HEAD')):
            return ('%s, and its holder is gone: not broken, because %s. The next landing breaks '
                    'it itself once main has caught up, or break it with: %s' % (
                        landing.describe(rec), hint[2:], self.break_command())), True
        if why:
            return ('%s, and its holder is gone: not broken, because %s. Settle that by hand, '
                    'then break it with: %s' % (landing.describe(rec), why,
                                                self.break_command())), True
        broken, got = self.lock.break_lock()
        if not broken:
            return '%s (not broken: %s)' % (landing.describe(rec), got), False
        self.say('broke the landing lock of a holder that is gone, with nothing unpushed: %s'
                 % landing.describe(got))
        try:
            rec = self.lock.take(self.lane, self.branches, pid=self.pid)
        except landing.Busy as e:
            return landing.describe(e.record), False
        self.token = rec['token']
        return None, False

    def drop_slot(self, code=0, how='done'):
        """Release the machine-lock slot, once: its queue entry goes, and the next landing of this
        repository may start. A failure is a WARNING said here (the entry goes once this process
        ends); Ctrl+C is raised."""
        slot, self.slot = self.slot, None
        if slot is None:
            return
        try:
            slot.release(code, how)
        except Exception as e:      # noqa: BLE001 - the outcome stands
            self.say('WARNING: the machine-lock slot of this landing could not be released (%s); '
                     'its queue entry goes once this process ends' % _what(e))

    def locked_checks(self):
        why = self.fetch()
        if why:
            return why
        # Again under the lock: the turn may have come long after preconditions(), and the merge
        # goes onto HEAD.
        current, why = refs.current_branch(self.root)
        if current != self.cfg.main_branch:
            return 'the main checkout is on %s, not %s, at the landing\'s turn' % (
                current or why or '(detached HEAD)', self.cfg.main_branch)
        why = self.onto_origin()
        if why:
            return why
        for b, t in self.pairs:
            tip = self.ref('refs/heads/%s' % b)
            if tip != t:
                return '%s is at %s, and the caller expects %s' % (b, (tip or '?')[:10], t[:10])
        entries, why = refs.status(self.root)
        if entries is None:
            return 'git status failed: %s' % why
        if entries:
            return 'the main checkout is not clean: %s' % ', '.join(
                '%s %s' % (xy.strip(), p) for xy, p in entries[:10])
        merging, why = refs.merge_in_progress(self.root)
        if merging is not False:
            return why or 'a merge is in progress in the main checkout'
        for b, t in self.pairs:
            landed, why = refs.is_ancestor(t, self.expect_main, self.root)
            if landed is not False:
                return why or '%s (%s) is already in %s: nothing to land' % (
                    b, t[:10], self.cfg.main_branch)
        for (b, t), (b2, t2) in itertools.permutations(self.pairs, 2):
            inside, why = refs.is_ancestor(t, t2, self.root)
            if inside is not False:
                return why or '%s (%s) is already in %s (%s), in the same batch: land %s alone' % (
                    b, t[:10], b2, t2[:10], b2)
        if self.lane != landing.FULL:
            globs = self.cfg.section('landing')['lanes'][self.lane].get('paths') or []
            for b, t in self.pairs:
                who = 'the branch' if len(self.pairs) == 1 else b
                mb, why = refs.merge_base(self.expect_main, t, self.root)
                changed, why2 = runner.changed_between(self.root, mb, t) if mb else (None, why)
                if changed is None:
                    return 'git cannot list the paths of %s: %s' % (who, why2 or why)
                outside = [p for p in changed if not S._match(p, globs)]
                if outside:
                    return 'lane %s takes only %s; %s changes %s' % (
                        self.lane, ', '.join(globs) or 'nothing', who, ', '.join(outside[:10]))
        for t in self.tips:
            why = self.owner_refusal(t)
            if why:
                return why
        for t in self.tips:
            why = at_landing.ancestry_refusal(self.root, self.cfg, t, env=self.git_env)
            if why:
                return why
        return None

    def onto_origin(self):
        """Step 5, after the fetch: the base becomes <remote>/<main> as fetched. A local main only
        behind it is fast-forwarded (HEAD on main, a clean checkout, no merge in progress); then,
        when the base is not the commit the config was read at, follow() reads the base's config.
        With --expect-main given and not the base, it says so and goes on. None, or why not
        (REFUSED, nothing merged): main with commits the remote lacks, or one that cannot be
        fast-forwarded, or a config at the base that cannot take this landing."""
        main_b, remote = self.cfg.main_branch, self.cfg.remote
        main, origin = self.ref(self.main_ref), self.ref(self.origin_ref)
        if not origin:
            return 'there is no %s/%s after the fetch' % (remote, main_b)
        if main != origin:
            behind, why = refs.is_ancestor(main, origin, self.root) if main else (None, 'no %s'
                                                                                    % main_b)
            if behind is None:
                return 'git cannot tell whether %s (%s) is behind %s/%s (%s): %s' % (
                    main_b, (main or '?')[:10], remote, main_b, origin[:10], why)
            if not behind:
                return ('%s is at %s, with commits %s/%s (%s) does not have: push them or set '
                        'them aside by hand; a landing merges onto %s/%s' % (
                            main_b, main[:10], remote, main_b, origin[:10], remote, main_b))
            why = self.fast_forward(main, origin)
            if why:
                return why
        if origin != self.expect_main:
            why = self.follow(origin)
            if why:
                return why
        given = self.expect_main_given
        if given and given != self.expect_main:
            self.say('main moved on from %s to %s; merging onto %s' % (
                given[:10], self.expect_main[:10], self.expect_main[:10]))
        return None

    def fast_forward(self, main, origin):
        """The local main only behind <remote>/<main>: `git merge --ff-only` it there. None, or
        why not: HEAD not on main, a checkout that is not clean, a merge in progress, or a
        fast-forward git refuses or that does not land main at <remote>/<main>."""
        main_b, remote = self.cfg.main_branch, self.cfg.remote
        behind = '%s (%s) is behind %s/%s (%s)' % (main_b, main[:10], remote, main_b, origin[:10])
        current, why = refs.current_branch(self.root)
        if current != main_b:
            return '%s, and the main checkout is on %s, not %s' % (
                behind, current or why or '(detached HEAD)', main_b)
        entries, why = refs.status(self.root)
        if entries is None:
            return 'git status failed: %s' % why
        if entries:
            return '%s, and the main checkout is not clean, so it is not fast-forwarded: %s' % (
                behind, _paths(entries))
        merging, why = refs.merge_in_progress(self.root)
        if merging is not False:
            return why or '%s, and a merge is in progress in the main checkout' % behind
        r = self.git('merge', '--ff-only', '--quiet', self.origin_ref, timeout=MERGE_TIMEOUT)
        now = self.ref(self.main_ref)
        if r.rc != 0 or now != origin:
            return '%s, and `git merge --ff-only %s/%s` did not bring it there: %s' % (
                behind, remote, main_b, r.err or 'main is at %s' % (now or '?')[:10])
        self.say('fast-forwarded %s from %s to %s/%s %s' % (main_b, main[:10], remote, main_b,
                                                            origin[:10]))
        return None

    def follow(self, base):
        """The base <remote>/<main> is not the commit the config was read at: read the config at
        `base`. None when it names the same main branch, remote and landing lock and still takes
        this landing (config_refusal); then self.expect_main and self.cfg are the base's, so the
        locked checks, the merged gate, the records and an undo all use it. Else why not
        (REFUSED, nothing merged; self.cfg and self.expect_main unchanged)."""
        was, main_b, remote = self.expect_main, self.cfg.main_branch, self.cfg.remote
        try:
            cfg = kconfig.load_at(self.root, base)
            same = (cfg.main_branch, cfg.remote, landing.lock_name(cfg)) == (
                main_b, remote, landing.lock_name(self.cfg))
        except (ConfigError, landing.LockError) as e:
            return 'main moved on from %s to %s, and the config there: %s' % (
                was[:10], base[:10], e)
        if not same:
            return ('main moved on from %s to %s, and the config there names another main '
                    'branch, remote or landing lock than this landing holds: land again (the '
                    'next landing reads the config at %s)' % (was[:10], base[:10], base[:10]))
        old = self.cfg
        self.cfg, self.expect_main = cfg, base
        why = self.config_refusal()
        if why:
            self.cfg, self.expect_main = old, was
            return 'main moved on from %s to %s, and by its config: %s' % (
                was[:10], base[:10], why[len('USAGE: '):] if why.startswith('USAGE: ') else why)
        return None

    def behind_hint(self, main, origin):
        """'; <main> is only behind ...: fast-forward it with: <command>' when the local main is
        an ancestor of the tracking ref (nothing unpushed, main just not caught up), else ''."""
        if not main or not origin or main == origin:
            return ''
        behind, _ = refs.is_ancestor(main, origin, self.root)
        if behind is not True:
            return ''
        return ('; %s is only behind %s/%s, with nothing unpushed: fast-forward it with: '
                'git -C "%s" merge --ff-only %s/%s' % (
                    self.cfg.main_branch, self.cfg.remote, self.cfg.main_branch, self.root,
                    self.cfg.remote, self.cfg.main_branch))

    def owner_refusal(self, tip):
        """None when every branch commit that changes an owner-only path is the owner's (by author
        email), counting only the paths that landing `tip` on the base would change; else why
        not (kitlib.guard.ownercommits). The rules are the base's merged
        with the tip's (kitlib.config.owner.merge): the branch cannot unlock a path or name its own
        owner; a tip config that does not load adds the default paths."""
        try:
            work = kconfig.load_at(self.root, tip).owner_rules()
        except ConfigError:
            work = kconfig.OwnerRules()
        rules = kconfig.merge_owner_rules(work, self.cfg.owner_rules())
        bad, why = ownercommits.violations(self.root, rules, tip, [self.expect_main],
                                           env=self.git_env)
        if bad is None:
            return 'git cannot list the commits of the branch to check owner-only paths: %s' % why
        return ownercommits.describe(rules, bad) if bad else None

    def do_merge(self):
        """Merge each branch in turn with --no-ff: `(None, [], None)` when all merged
        (self.merges), or `(why, untracked left, branch)` when `branch` does not merge cleanly:
        that merge aborted and the earlier ones undone, main back at the base. Raises _Stuck when
        that cannot be verified."""
        self.lock.set_phase(self.token, 'merge')
        self.phase = 'merge'
        for b, t in self.pairs:
            prev = self.merges[-1] if self.merges else self.expect_main
            title = 'Merge %s%s' % (b, ': %s' % self.summary if self.summary else '')
            r = self.git('merge', '--no-ff', '--no-edit', '-m', title, t, timeout=MERGE_TIMEOUT)
            if r.rc == 0:
                head = self.ref('HEAD')
                first, _ = cmd.run_text(['rev-parse', 'HEAD^1'], self.root)
                second, _ = cmd.run_text(['rev-parse', 'HEAD^2'], self.root)
                if not head or first != prev or second != t:
                    raise _Stuck('the merge of %s reported success, and HEAD (%s) is not a merge '
                                 'of %s onto %s' % (b, (head or '?')[:10], t[:10], prev[:10]))
                self.merges.append(head)
                self.merge = head
                continue
            conflict = r.err or cmd.text(r.out).strip() or 'exit %s' % r.rc
            merging, _ = refs.merge_in_progress(self.root)
            if merging:
                a = self.git('merge', '--abort')
                if a.rc != 0:
                    raise _Stuck('the merge of %s failed (%s) and `git merge --abort` failed too: '
                                 '%s' % (b, conflict, a.err))
            if self.ref(self.main_ref) != prev or refs.status(self.root)[0] != []:
                raise _Stuck('the merge of %s failed (%s) and main is not back at %s with a clean '
                             'tree' % (b, conflict, prev[:10]))
            why = '%s does not merge cleanly into %s: %s; the merge was aborted' % (
                b, self.cfg.main_branch, conflict)
            if not self.merges:
                return why, [], b
            done = ', '.join(self.branches[:len(self.merges)])
            left = self.undo('the merge of %s failed' % b)
            return ('%s, and the merge of %s before it was undone (%s is back at %s)' % (
                why, done, self.cfg.main_branch, self.expect_main[:10])), left, b
        return None, [], None

    def tracked_and_untracked(self):
        """`(tracked entries, untracked paths)` of the main checkout's status; None, None when git
        cannot say."""
        entries, _ = refs.status(self.root)
        if entries is None:
            return None, None
        return ([(xy, p) for xy, p in entries if xy != '??'],
                [p for xy, p in entries if xy == '??'])

    def undo(self, why):
        """Undo the merges: main back to the base with `git reset --keep`, which keeps
        untracked files. Returns the untracked paths left in the main checkout (they block a landing
        until cleaned, and are reported, never deleted); raises _Stuck when it cannot verify. Safe
        only while HEAD is on main, main's first-parent line beyond the base is exactly this
        landing's merges and no tracked file changed, and never once the remote's main holds a
        merge of this landing (not_on_remote)."""
        self.lock.set_phase(self.token, 'undo')
        self.phase = 'undo'
        tracked, untracked = self.tracked_and_untracked()
        current, _ = refs.current_branch(self.root)
        main = self.ref(self.main_ref)
        if current == self.cfg.main_branch and main == self.expect_main and tracked == []:
            return untracked                             # already undone (a retry)
        self.not_on_remote(why)
        out, err = cmd.run_text(['rev-list', '--first-parent', '%s..%s' % (self.expect_main, self.main_ref)],
                                self.root)
        ahead = out.split() if out else []
        if (out is None or not self.merges or ahead != list(reversed(self.merges)) or tracked != [] or
                current != self.cfg.main_branch):
            raise _Stuck('%s, and the merge cannot be undone safely (HEAD on %s; main is %s ahead of '
                         '%s; tracked changes: %s)' % (
                             why, current or 'no branch', ', '.join(c[:10] for c in ahead) or err or '?',
                             self.expect_main[:10], _paths(tracked) if tracked is not None else
                             'git status failed'))
        r = self.git('reset', '--quiet', '--keep', self.expect_main, timeout=MERGE_TIMEOUT)
        tracked, untracked = self.tracked_and_untracked()
        if r.rc != 0 or self.ref(self.main_ref) != self.expect_main or tracked != []:
            raise _Stuck('%s, and `git reset --keep %s` did not put main back: %s' % (
                why, self.expect_main[:10], r.err or 'main is %s' % (self.ref(self.main_ref) or '?')[:10]))
        return untracked

    def not_on_remote(self, why):
        """Before an undo: raises _Stuck when the remote's main already holds a merge of this
        landing (someone pushed main while the gate ran), or may hold it and git cannot tell even
        after a fetch. A remote main that is the base lets the undo go on (its own checks still
        apply). One that ls-remote cannot read is judged by the tracking ref (a push from this
        checkout sets it): the undo goes on only when that does not hold a merge of this landing."""
        remote, main_b = self.cfg.remote, self.cfg.main_branch
        pushed, unread = refs.ls_remote(remote, 'refs/heads/%s' % main_b, self.root)
        if not pushed:
            tracking = self.ref(self.origin_ref)
            on = self.on_remote(tracking)
            if on is False:
                return
            raise _Stuck('%s, and %s/%s could not be read (%s) while the tracking ref %s (%s) %s: '
                         'the merge may be on %s, so it is left as it is' % (
                             why, remote, main_b, unread or 'ls-remote failed', self.origin_ref,
                             (tracking or '?')[:10],
                             'holds the merge' if on else 'may hold the merge (git cannot tell)',
                             remote))
        if pushed == self.expect_main:
            return
        on = self.on_remote(pushed)
        if on is None:              # not fetched yet: fetch, then ask again
            self.fetch()
            on = self.on_remote(pushed)
        if on is False:
            return
        raise _Stuck('%s, and %s/%s (%s) %s while the gate ran: the merge cannot be undone here, '
                     'so it is left as it is' % (
                         why, remote, main_b, pushed[:10],
                         'is the merge, pushed' if pushed in self.merges else
                         'holds the merge, pushed with commits on top' if on else
                         'may hold the merge (git cannot tell), pushed'))

    def unchanged(self):
        """None when the main checkout is exactly the merge the gate passed (HEAD on main, main
        at the merge, no tracked change, no untracked file), else why not."""
        problems = []
        current, _ = refs.current_branch(self.root)
        head, main = self.ref('HEAD'), self.ref(self.main_ref)
        if current != self.cfg.main_branch or head != self.merge or main != self.merge:
            problems.append('HEAD moved while the merged gate ran (HEAD %s on %s, main %s; the '
                            'gated merge is %s)' % ((head or '?')[:10], current or 'no branch',
                                                     (main or '?')[:10], self.merge[:10]))
        tracked, untracked = self.tracked_and_untracked()
        if tracked is None:
            problems.append('git status failed')
        else:
            if tracked:
                problems.append('a step changed tracked files: %s' % _paths(tracked))
            if untracked:
                problems.append('a step left untracked files: %s' % ', '.join(untracked[:10]))
        return '; '.join(problems) or None

    def gate(self):
        self.lock.set_phase(self.token, 'gate')
        self.phase = 'gate'
        refusal = self.lock.gate_refusal(self.lane)
        if refusal:
            return G.Outcome(G.REFUSED, ['REFUSED: %s' % refusal])
        if self.merged_gate_fn is not None:
            return self.merged_gate_fn(self)
        lane_args = None
        if self.lane != landing.FULL:
            lane_args = self.cfg.section('landing')['lanes'][self.lane].get('merged_args')
        plan = reuse.planner(self.root, self.cfg, self.branches[0], self.tips[0], self.expect_main,
                             self.merge, self.prior, fresh=self.fresh, full=self.full,
                             batch=len(self.branches))
        out = runner.merged_gate(self.root, self.cfg, self.expect_main, full=self.full,
                                 slug='+'.join(self.branches), log_dir=self.log_dir, say=self.say,
                                 book_kw=self.book_kw, lane_args=lane_args, reuse=plan,
                                 slot=self.slot)
        self.gate_logs = out.facts.get('log_dir')
        return out

    def source(self):
        """The merged gate's source (kitlib.gate.detect), once it ran; else None."""
        return self.gate_out.facts.get('source') if self.gate_out is not None else None

    def write_record(self, started, results):
        """The gate-OK record for the merge commit (pre-push reads it), its config_rev the
        base; its path. Raises gate_ok.RecordError."""
        path, _ = gate_ok.record_pass(self.root, self.merge, runner.gate_config(self.cfg),
                                      runner.kit_identity(), runner.repo_name(self.cfg, self.root),
                                      started, runner.record_steps(results or []),
                                      base=self.expect_main, config_rev=self.expect_main,
                                      source=self.source())
        return path

    def push(self, started, results):
        try:
            path = self.write_record(started, results)
        except gate_ok.RecordError as e:
            left = self.undo('the merged gate passed and its gate-OK record could not be written '
                             '(%s)' % e)
            return '; '.join(['the gate-OK record could not be written: %s; the merge was undone'
                              % e] + _leftover_lines(self.root, left))
        self.say('gate-OK record: %s' % path)
        self.lock.set_phase(self.token, 'push')
        self.phase = 'push'
        r = self.git('push', '--quiet', self.cfg.remote,
                     '%s:refs/heads/%s' % (self.merge, self.cfg.main_branch), timeout=PUSH_TIMEOUT)
        remote, why = refs.ls_remote(self.cfg.remote, 'refs/heads/%s' % self.cfg.main_branch,
                                     self.root)
        if remote != self.merge:
            raise _Stuck('the push %s, and %s/%s is %s, not the merge %s' % (
                'exited 0' if r.rc == 0 else 'failed (%s)' % (r.err or 'exit %s' % r.rc),
                self.cfg.remote, self.cfg.main_branch,
                'unreadable (%s)' % why if remote is None else (remote or 'absent')[:10],
                self.merge[:10]))
        self.landed_at = remote
        self.phase = 'pushed'       # verified on the remote: landed, whatever stops it from here
        if self.fetch() or self.ref(self.origin_ref) != self.merge:
            self.git('update-ref', self.origin_ref, self.merge)
        return None

    # ---------------------------------------------------------------- the landing

    def run(self):
        """_run(), and the landing's result records (kitlib.gate.result, kind "land"), one per
        branch at its --expect-tip: `running` once the locked checks pass, just before the merge
        (_locked), then the outcome's, or `error` when an exception or Ctrl+C escapes _run (raised
        again, unchanged). A landing that ends before that (USAGE, REFUSED before the merge, BUSY,
        the machine lock's refusal: a retry or a duplicate) writes no record, so it never replaces
        a real result; a submitted landing's records were written running before it started, so
        every outcome finishes them. The record lines are said as progress: the verdict stays the
        outcome's last line. The machine-lock slot is released however _run ends.
        facts['result_record']: the path (one branch), the list of paths (a batch), or None."""
        try:
            out = self._run()
        except BaseException as e:
            ctrl_c = isinstance(e, KeyboardInterrupt)
            try:
                self.drop_slot(130 if ctrl_c else 1, 'interrupted' if ctrl_c else 'error')
            except BaseException:   # noqa: BLE001 - never replace the exception being raised
                pass
            for rr in self.result_runs:
                try:
                    self.say(rr.error(e)[1])
                except Exception:   # noqa: BLE001 - never replace the exception being raised
                    pass
            raise
        try:
            self.drop_slot(0 if out.code in (G.OK, G.LANDED_UNGATED) else out.code or 1, 'done')
        except KeyboardInterrupt as e:  # the outcome stands: the records still say it
            out.lines.insert(max(0, len(out.lines) - 1), 'WARNING: %s while the machine-lock '
                             'slot was released; its queue entry goes once this process ends'
                             % _what(e))
        paths = []
        for rr in self.result_runs:
            path, line = rr.finish(out, base=self.expect_main,
                                   log=self.gate_logs or self.output_log, source=self.source())
            self.say(line)
            paths.append(path)
        out.facts['result_record'] = (paths[0] if len(self.branches) == 1 else paths) if paths else None
        return out

    def _run(self):
        why = self.usage()
        if why:
            return G.Outcome(G.USAGE, ['USAGE: %s' % why])
        why = self.preconditions()
        if why:
            if why.startswith('USAGE: '):
                return G.Outcome(G.USAGE, [why])
            return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed' % why])
        try:
            self.book_turn()
        except (landing.LockError, ConfigError) as e:
            return G.Outcome(G.REFUSED, ['REFUSED: the landing lock: %s; nothing was changed' % e])
        except LockError as e:
            if e.code == EXIT_USAGE:
                return runner.lock_refusal(e)
            return G.Outcome(e.code, ['NOT RUN: the machine lock did not give the landing its '
                                      'turn: %s; nothing was changed' % e.message])
        why = self.fetch()
        if why:
            return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed' % why])
        try:
            why = self.take()
        except (landing.LockError, ConfigError) as e:
            return G.Outcome(G.REFUSED, ['REFUSED: the landing lock: %s; nothing was changed' % e])
        if why:
            return G.Outcome(G.BUSY, ['BUSY: %s; nothing was changed' % why])
        try:
            return self._locked()
        except _Stuck as e:
            return self.stuck(str(e))
        except BaseException as e:  # after the lock is taken nothing may leave it silently
            out = self.recover(e)
            if out.code in (G.STUCK, G.OK, G.LANDED_UNGATED):  # Ctrl+C included: STUCK is exit
                return out      # 3, with what is left; OK or LANDED_UNGATED: stopped after the
                                # verified push (or the adopt), landed
            if isinstance(e, ConfigError) and out.code == G.REFUSED:
                return out
            if isinstance(e, KeyboardInterrupt) or (isinstance(e, Exception) and
                                                    self.phase == 'before'):
                for line in out.lines:  # Ctrl+C undone and released (exit 130), or a bug
                    self.say(line)      # before the merge: raised again
                raise
            return out                  # an exception or SystemExit after the merge: undone

    def recover(self, e):
        """After an exception `e` under the lock: release it before the merge, undo the merge
        after it (or STUCK when that cannot be done safely). An Outcome saying what happened."""
        what = 'Ctrl+C' if isinstance(e, KeyboardInterrupt) else '%s: %s' % (type(e).__name__, e)
        try:
            if self.phase == 'before':
                self.release()
                if isinstance(e, ConfigError):
                    return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed' % e])
                return G.Outcome(G.FAILED, ['NOT LANDED: %s before the merge; the landing lock was '
                                            'released; nothing was changed' % what])
            if self.phase in ('pushed', 'landed'):
                return self.landed_anyway(what)
            if self.phase == 'push':
                raise _Stuck('%s during the push: the merge may be on %s' % (what, self.cfg.remote))
            if self.phase == 'merge':
                self.half_merge(what)
                if not self.merges:
                    self.release()
                    return G.Outcome(G.FAILED, ['NOT LANDED: %s during the merge; it was aborted, '
                                                'and %s is back at %s' % (
                                                    what, self.cfg.main_branch,
                                                    self.expect_main[:10])], undone=True)
            left = self.undo('%s in phase %s' % (what, self.phase))
            self.release()
            return G.Outcome(G.FAILED, ['NOT LANDED: %s in phase %s; the merge of %s was undone, '
                                        'and %s is back at %s' % (
                                            what, self.phase, self.branch, self.cfg.main_branch,
                                            self.expect_main[:10])]
                             + _leftover_lines(self.root, left), undone=True)
        except _Stuck as s:
            return self.stuck(str(s))
        except BaseException as s:  # a second failure while recovering: leave the lock held
            return self.stuck('%s, and then %s: %s while recovering' % (what, type(s).__name__, s))

    def landed_anyway(self, what):
        """`what` stopped the landing after its push was verified (phase pushed): it is landed.
        Release the lock (a failure is a WARNING with the break command) and say what was skipped:
        an OK Outcome (LANDED_UNGATED with its WARNING while self.ungated, which adopt() sets
        before phase pushed and clears only once the commits on top select no gate step), never
        STUCK. Each step is guarded against BaseException, so a second
        Ctrl+C or failure here is one more WARNING, never a STUCK for a landed merge."""
        lines = list(self.gate_out.lines) if self.gate_out is not None else []
        at = self.landed_at or self.merge   # what ls-remote showed: the merge, or one on top
        if at == self.merge:
            how = 'pushed and verified on %s' % self.cfg.remote
        else:
            how = 'already on %s: %s/%s %s has it' % (self.cfg.remote, self.cfg.remote,
                                                     self.cfg.main_branch, at[:10])
        lines.append('LANDED %s on %s at %s (%s)' % (self.branch, self.cfg.main_branch,
                                                     self.merge, how))
        if self.ungated:
            lines.append(self.ungated)
        lines.append('WARNING: %s after the verified push; the landing stands' % what)
        try:                        # ls-remote showed `at`, so the tracking ref may say so; never
            if self.ref(self.origin_ref) != at:  # set back to the merge under commits on top
                self.git('update-ref', self.origin_ref, at)
        except BaseException:       # noqa: BLE001 - the next landing's fetch puts it right
            pass
        released = None
        try:
            self.release(landed=True)
        except BaseException as e:  # noqa: BLE001 - landed: only the lock file is left
            released = _what(e)
        if self.token:
            lines.append('WARNING: the landing lock was not released (%s); check that no landing '
                         'is running, then break it with: %s' % (
                             released or 'still held', self.break_command()))
        elif released is not None:
            lines.append('WARNING: the landing lock was released, and releasing the landing\'s '
                         'machine-lock slot stopped (%s); its queue entry goes once this process '
                         'ends' % released)
        for b in self.branches:
            try:
                hows = at_landing.landable_commands(self.root, self.cfg, b)
            except BaseException:   # noqa: BLE001 - cannot tell: name the tool anyway
                hows = ['the backlog tool']
            for how in hows:
                lines.append('WARNING: the records %s holds pulled in the backlog store were not '
                             'closed; see what is left with: %s' % (b, how))
        if not self.cleaned and not self.keep_branch:
            lines.append('WARNING: the branch cleanup did not run; once no landing is running, '
                         'clean up with: %s' % cleanup.sweep_command(self.root))
        verdict = self.gate_out.facts.get('verdict') if self.gate_out is not None else None
        return G.Outcome(G.LANDED_UNGATED if self.ungated else G.OK, lines, merge=self.merge,
                         verdict=verdict)

    def half_merge(self, what):
        """Merging stopped part way (phase merge): abort a merge in progress; when main is exactly
        the merge of the next tip onto the last recorded merge (or the base), record it in
        self.merges. Returns when main is at the last recorded merge with a clean tree; else
        _Stuck."""
        merging, _ = refs.merge_in_progress(self.root)
        if merging:
            a = self.git('merge', '--abort', timeout=MERGE_TIMEOUT)
            if a.rc != 0:
                raise _Stuck('%s during the merge, and `git merge --abort` failed: %s' % (what, a.err))
        prev = self.merges[-1] if self.merges else self.expect_main
        nxt = self.tips[len(self.merges)] if len(self.merges) < len(self.tips) else None
        main = self.ref(self.main_ref)
        tracked, _ = self.tracked_and_untracked()
        if main == prev and tracked == []:
            return
        first, _ = cmd.run_text(['rev-parse', '%s^1' % self.main_ref], self.root)
        second, _ = cmd.run_text(['rev-parse', '%s^2' % self.main_ref], self.root)
        if main and nxt and first == prev and second == nxt and tracked == []:
            self.merges.append(main)
            self.merge = main
            return
        raise _Stuck('%s during the merge, and main (%s) is neither %s nor the merge of %s' % (
            what, (main or '?')[:10], prev[:10], (nxt or '?')[:10]))

    def _locked(self):
        why = self.locked_checks()
        if why:
            self.release()
            return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was merged' % why])
        if not self.fresh:          # before this landing's own records replace them
            self.prior = self.prior_records()
        if self.submitted:          # running since the submission: now with the base
            for rr in self.result_runs:
                rr.base = self.expect_main
                self.say(rr.start(log=self.output_log)[1])
        else:
            for b, t in self.pairs:
                rr = result.Run.maybe(self.root, 'land', b, t, self.expect_main)
                if rr is not None:
                    self.result_runs.append(rr)
                    self.say(rr.start()[1])
        started = gate_ok.utc()
        conflict, left, failed = self.do_merge()
        if conflict:
            self.release()
            return G.Outcome(G.FAILED, [conflict] + _leftover_lines(self.root, left) + [
                'CONFLICT %s with main %s: the author merges main' % (failed, self.expect_main)],
                undone=bool(self.merges), conflict=failed)
        self.say('merged %s at %s; gating the merged tree' % (self.branch, self.merge[:10]))
        out = self.gate_out = self.gate()
        if out.code == G.OK:
            changed = self.unchanged()      # the commit pushed must be the commit gated
            if changed:
                adopted = self.adopt(started, out)  # the owner pushed commits on top of the merge
                if adopted is not None:
                    return adopted
                left = self.undo('the merged gate passed, and %s' % changed)
                self.release()
                return G.Outcome(G.FAILED, _not_landed(out.lines) + [
                    'NOT LANDED: the merged gate passed, and %s; nothing was pushed, and the merge '
                    'of %s was undone (%s is back at %s)' % (
                        changed, self.branch, self.cfg.main_branch, self.expect_main[:10])]
                    + _leftover_lines(self.root, left),
                    verdict=out.facts.get('verdict'), undone=True)
        if out.code != G.OK:
            left = self.undo('the merged gate did not pass')
            self.release()
            return G.Outcome(out.code,
                             out.lines + ['NOT LANDED: the merge of %s was undone; %s is back at %s'
                                          % (self.branch, self.cfg.main_branch,
                                             self.expect_main[:10])]
                             + _leftover_lines(self.root, left),
                             verdict=out.facts.get('verdict'), undone=True)
        why = self.push(started, out.facts.get('results'))
        if why:
            self.release()
            return G.Outcome(G.FAILED, _not_landed(out.lines) + ['NOT LANDED: %s' % why])
        landed = ['LANDED %s on %s at %s (pushed and verified on %s)' % (
            self.branch, self.cfg.main_branch, self.merge, self.cfg.remote)]
        try:
            self.release(landed=True)
        except landing.LockError as e:  # the landing is done; only the lock file is left
            landed.append('WARNING: the landing lock could not be released: %s; check that no '
                          'landing is running, then break it with: %s' % (e, self.break_command()))
        landed.extend(self.close_pulled())
        tidy = self.clean_up()
        return G.Outcome(G.OK, out.lines + tidy + landed, merge=self.merge,
                         verdict=out.facts.get('verdict'), cleanup=self.cleanup)

    def adopt(self, started, out):
        """The merged gate passed and the main checkout is no longer the merge it gated (step 7b):
        when someone committed on top of the merge in the main checkout and pushed while the gate
        ran, the landing is finished, not undone (an undo would put main behind a remote that
        holds the merge). None (go on to the undo) unless all of: the fetch works; <remote>/<main>
        (ls-remote) is the tracking ref, and main is too, with no merge in progress
        (LandingLock.unpushed); HEAD is on main with no tracked change and no untracked file; and
        main's first-parent line beyond the base is commits on top of exactly this landing's
        merges. Then the landing Outcome, its LANDED line naming those commits: OK when they
        select no gate step (the merge's gate-OK record is written), LANDED_UNGATED when they
        select one or that cannot be told (a WARNING naming the full gate to run; no record)."""
        if self.fetch():
            return None
        main_b, remote = self.cfg.main_branch, self.cfg.remote
        pushed, _ = refs.ls_remote(remote, 'refs/heads/%s' % main_b, self.root)
        if not pushed or pushed != self.ref(self.origin_ref) or self.lock.unpushed() is not None:
            return None
        current, _ = refs.current_branch(self.root)
        tracked, untracked = self.tracked_and_untracked()
        if current != main_b or tracked != [] or untracked != []:
            return None
        text, _ = cmd.run_text(['rev-list', '--first-parent', '%s..%s' % (self.expect_main, pushed)],
                               self.root)
        chain, n = (text or '').split(), len(self.merges)
        if text is None or not n or len(chain) < n or chain[len(chain) - n:] != list(
                reversed(self.merges)):
            return None
        log, _ = cmd.run_text(['log', '--first-parent', '--reverse', '--format=%H %s',
                               '%s..%s' % (self.merge, pushed)], self.root)
        on_top = [l for l in (log or '').splitlines() if l.strip()] or chain[:len(chain) - n]
        said = '; '.join(l[:10] + l[40:] for l in on_top[:10]) + (
            '; and %d more' % (len(on_top) - 10) if len(on_top) > 10 else '')
        gate_cmd = 'python "%s" -C "%s" --full' % (os.path.join(S.kit_live(), 'tools', 'gate.py'),
                                                   self.root)
        # Fail closed: until unseen() shows the commits on top select no gate step, anything that
        # stops the landing from here (landed_anyway) still says main was not gated whole.
        self.ungated = ('WARNING: %s landed, and main was not gated whole: %s, made while the '
                        'merged gate ran, were not checked against the gate steps; gate it with: '
                        '%s' % (self.branch, ', '.join(l[:10] for l in on_top[:10]), gate_cmd))
        self.landed_at = pushed
        self.phase = 'pushed'       # on the remote: landed, whatever stops it from here
        landed = ['LANDED %s on %s at %s (already on %s: %s/%s %s has it, with %d commit(s) on top '
                  'made while the merged gate ran: %s)' % (
                      self.branch, main_b, self.merge, remote, remote, main_b, pushed[:10],
                      len(on_top), said)]
        paths, steps, why = self.unseen(pushed)
        if steps == []:
            code = G.OK
            self.ungated = None
            try:
                self.say('gate-OK record: %s' % self.write_record(started, out.facts.get('results')))
            except gate_ok.RecordError as e:
                landed.append('WARNING: the gate-OK record of the merge %s could not be written: '
                              '%s; the landing stands' % (self.merge[:10], e))
        else:
            code = G.LANDED_UNGATED
            shown = ', '.join(paths[:10]) + (' and %d more' % (len(paths) - 10)
                                             if len(paths) > 10 else '')
            self.ungated = ('WARNING: %s landed, and main was not gated whole: %s %s, which %s; '
                            'gate it with: %s' % (
                                self.branch, ', '.join(l[:10] for l in on_top[:10]),
                                'change %s' % shown if paths else 'change paths git cannot list',
                                'select %s' % ', '.join(steps) if steps else
                                'may select a gate step (%s)' % why, gate_cmd))
            landed.append(self.ungated)
        try:
            self.release(landed=True)
        except landing.LockError as e:  # the landing is done; only the lock file is left
            landed.append('WARNING: the landing lock could not be released: %s; check that no '
                          'landing is running, then break it with: %s' % (e, self.break_command()))
        landed.extend(self.close_pulled())
        tidy = self.clean_up()
        return G.Outcome(code, out.lines + tidy + landed, merge=self.merge,
                         verdict=out.facts.get('verdict'), cleanup=self.cleanup, adopted=True,
                         on_top=[l[:40] for l in on_top], unseen=steps)

    def unseen(self, pushed):
        """What the commits on top of the merge (merge..`pushed`) change, and the gate steps that
        change selects, by the base's gate config as the merged gate ran it: `(paths,
        [step names], None)` (setup not counted), or `(paths, None, why)` when that cannot be
        worked out, which the caller counts as selecting a step (fail closed)."""
        paths, why = runner.changed_between(self.root, self.merge, pushed)
        if paths is None:
            return [], None, 'git cannot list the paths: %s' % why
        if not paths:
            return [], [], None
        try:
            gate = detect.effective(self.cfg, self.root, base=self.expect_main).gate
            built = S.build(gate, self.root, self.expect_main, self.cfg.tools_dir, merged=True)
            chosen, _ = S.select(gate, built, paths, read=S.working_tree_reader(self.root))
        except Exception as e:      # noqa: BLE001 - landed already; what cannot be told is unseen
            return paths, None, _what(e)
        return paths, [s.name for s in chosen if s.name != 'setup'], None

    def on_remote(self, pushed):
        """True when one of this landing's merges is `pushed` (the remote's main) or an ancestor
        of it: the merge is landed, and nothing may undo it; None when git cannot tell (`pushed`
        not fetched); else False."""
        if not pushed or not self.merges:
            return False
        unknown = False
        for m in self.merges:
            if m == pushed:
                return True
            yes, _ = refs.is_ancestor(m, pushed, self.root)
            if yes:
                return True
            unknown = unknown or yes is None
        return None if unknown else False

    def close_pulled(self):
        """After the verified push and the release: close what the branch holds pulled in the
        backlog store (kitlib.backlog.at_landing). The landing stands whatever happens here, so
        every failure, Ctrl+C included, is a WARNING line and never reaches recover().
        Each branch's `Closes:` lines are read from its tip down to the main it was merged onto
        (the batch's previous merge, else the base), so a stacked branch never re-reads the
        lines of the branch merged before it; the batch's other branches are no holder to tell."""
        self.phase = 'landed'
        lines = []
        stacked = len(self.merges) == len(self.pairs)
        for i, (b, t) in enumerate(self.pairs):
            base = self.merges[i - 1] if stacked and i else self.expect_main
            try:
                lines.extend(at_landing.close_landed(
                    self.root, self.cfg, b, self.merge, tip=t, base=base,
                    batch=[o for o in self.branches if o != b]))
            except BaseException as e:
                what = 'Ctrl+C' if isinstance(e, KeyboardInterrupt) else '%s: %s' % (
                    type(e).__name__, e)
                try:
                    hows = at_landing.landable_commands(self.root, self.cfg, b) or [
                        at_landing.backlog_command(self.root, 'landable', b)]
                except BaseException:   # noqa: BLE001 - still say what stopped
                    hows = ['the backlog tool']
                lines.append('WARNING: %s landed, and closing the records it holds pulled in the '
                             'backlog store stopped (%s); see what is left with: %s' % (
                                 b, ' '.join(what.split()), ' and '.join(hows)))
        return lines

    def clean_up(self):
        """After close_pulled: the branch cleanup (kitlib.gate.cleanup.after_landing); its lines,
        which go before the LANDED line, and self.cleanup. The landing stands whatever happens
        here, so an exception or Ctrl+C is a WARNING naming the sweep and never reaches recover()."""
        self.cleaned = True
        try:
            self.cleanup, lines = cleanup.after_landing(self.root, self.branches, self.merge,
                                                        keep_branch=self.keep_branch,
                                                        git_env=self.git_env)
        except BaseException as e:
            self.cleanup = {b: None for b in self.branches}
            lines = ['WARNING: %s landed, and its branch cleanup stopped (%s); finish it with: %s'
                     % (self.branch, ' '.join(_what(e).split()),
                        cleanup.sweep_command(self.root))]
        return lines

    def prior_records(self):
        """The result records naming the tips, as before this landing: a submitted landing's own
        running records left out, and the land records they replaced put back
        (kitlib.gate.reuse.prior_records; None: reuse nothing)."""
        prior = reuse.prior_records(self.root, self.tips)
        if prior is None or not self.submitted:
            return prior
        return ([r for r in prior if r.get('run_id') not in self.own_runs]
                + [r for r in self.replaced if r.get('tip') in self.tips])

    def release(self, landed=False):
        """Release the landing lock, then the machine-lock slot (even when the release fails)."""
        try:
            if self.token:
                self.lock.release(self.token)
                self.token = None
        finally:
            self.drop_slot(0 if landed else 1, 'done')

    def break_command(self):
        return ('python -c "import sys; sys.path.insert(0, r\'%s\'); from kitlib.records.landing '
                'import LandingLock as L; print(L(r\'%s\', \'%s\', \'%s\', name=\'%s\')'
                '.break_lock())"' % (S.kit_live(), self.root, self.cfg.main_branch, self.cfg.remote,
                                     os.path.basename(self.lock.path)))

    def stuck(self, why):
        state = 'the landing lock could not be marked stuck'
        try:
            if self.token:
                self.lock.set_phase(self.token, 'stuck')
                state = 'the landing lock is left held at phase stuck (%s)' % self.lock.path
        except landing.LockError as e:
            state = '%s: %s' % (state, e)
        lines = ['STUCK: landing %s stopped in phase %s: %s' % (self.branch, self.phase, why)]
        if self.cfg is None or self.lock is None:
            main_b = self.cfg.main_branch if self.cfg else 'main'
            remote = self.cfg.remote if self.cfg else 'origin'
            lines.append('%s. Check %s against %s/%s (git log --first-parent), finish or undo by '
                         'hand, then break the lock.' % (state, main_b, remote, main_b))
            return G.Outcome(G.STUCK, lines, merge=self.merge)
        lines.append('%s. To clear it, in this order (each command on a "$" line):' % state)
        try:
            left = self.leftovers()
        except Exception as e:      # the report must not fail
            left = None
            lines.append('left: unknown (%s: %s); check %s against %s/%s by hand' % (
                type(e).__name__, e, self.cfg.main_branch, self.cfg.remote, self.cfg.main_branch))
        if left is not None:
            lines.append(self.left_line(left))
            steps = self.clear_steps(left)
        else:
            steps = []
        steps.append(('break the landing lock (it refuses while the holder is alive, or while %s '
                      'differs from %s/%s)' % (self.cfg.main_branch, self.cfg.remote,
                                               self.cfg.main_branch), [self.break_command()]))
        for n, (text, commands) in enumerate(steps, 1):
            lines.append('%d. %s' % (n, text))
            lines.extend(STEP_CMD + c for c in commands)
        return G.Outcome(G.STUCK, lines, merge=self.merge)

    def leftovers(self):
        """What a STUCK landing leaves, as a dict: the branch HEAD is on, main, the remote's main
        (None when ls-remote cannot read it, and why), the tracking ref, tracked changes,
        untracked files, and the commits on main beyond the merge (and the branch it merged)."""
        current, _ = refs.current_branch(self.root)
        main = self.ref(self.main_ref)
        tracked, untracked = self.tracked_and_untracked()
        pushed, pushed_why = refs.ls_remote(self.cfg.remote, 'refs/heads/%s' % self.cfg.main_branch,
                                            self.root)
        extra = []
        if main and main != self.expect_main:
            known = [self.expect_main] + self.tips + self.merges
            out, _ = cmd.run_text(['log', '--format=%H %s', main, '--not'] + known, self.root)
            extra = [l for l in (out or '').splitlines() if l.strip()]
        return {'current': current, 'main': main, 'tracked': tracked, 'untracked': untracked,
                'pushed': pushed, 'pushed_why': pushed_why, 'tracking': self.ref(self.origin_ref),
                'extra': extra}

    def left_line(self, left):
        main_b, remote = self.cfg.main_branch, self.cfg.remote
        tracked = left['tracked']
        return ('left: HEAD on %s; %s at %s, %s/%s at %s (tracking ref %s; base %s, merge '
                '%s); tracked changes: %s; untracked: %s' % (
                    left['current'] or 'no branch (detached)', main_b, (left['main'] or '?')[:10],
                    remote, main_b,
                    'unreadable' if left['pushed'] is None else (left['pushed'] or 'absent')[:10],
                    (left['tracking'] or '?')[:10], self.base_text(),
                    (self.merge or 'none')[:10],
                    _paths(tracked) if tracked is not None else '?',
                    ', '.join((left['untracked'] or [])[:10]) or 'none'))

    def base_text(self):
        """The base, short, and the --expect-main it moved on from when one was given and differs
        (onto_origin)."""
        given = self.expect_main_given
        if not given or given == self.expect_main:
            return (self.expect_main or '?')[:10]
        return '%s, moved on from --expect-main %s' % ((self.expect_main or '?')[:10], given[:10])

    def clear_steps(self, left):
        """`[(what, [commands])]` that fit what is left: never a reset that moves a branch other
        than main, drops commits nobody else holds, or runs before an unknown push is checked, and
        none at all when the remote's main (or, when ls-remote cannot read it, the tracking ref)
        holds the merge (on_origin_steps) or may hold it and git cannot tell (check_first)."""
        main_b, remote = self.cfg.main_branch, self.cfg.remote
        g = 'git -C "%s"' % self.root
        tracking = 'refs/remotes/%s/%s' % (remote, main_b)
        steps = []
        pushed = left['pushed']
        if pushed is None:
            on = self.on_remote(left['tracking'])
            if on:                  # a push from this checkout set it: the merge is landed
                return [('the push may have landed: %s/%s could not be read (%s), and the '
                         'tracking ref %s (%s) holds the merge %s, so it is on %s and nothing '
                         'here undoes it. Fetch, then check what %s/%s is' % (
                             remote, main_b, left['pushed_why'] or 'ls-remote failed', tracking,
                             left['tracking'][:10], (self.merge or '?')[:10], remote, remote,
                             main_b),
                         ['%s fetch %s' % (g, remote), '%s rev-parse %s' % (g, tracking)])
                        ] + self.on_origin_steps(left, left['tracking'])
            if on is None:
                return [self.check_first(left['tracking'])]
            steps.append(('the push may have landed: %s/%s could not be read (%s). Fetch, then '
                          'check what %s/%s is, before any reset' % (
                              remote, main_b, left['pushed_why'] or 'ls-remote failed', remote,
                              main_b), ['%s fetch %s' % (g, remote), '%s rev-parse %s' % (
                                  g, tracking)]))
            steps.append(('if that printed the merge %s, the landing is done: skip to the last '
                          'step, no reset. If it printed %s, the push did not land: go on. If '
                          'the fetch failed, stop: what it printed is stale, and main is not '
                          'moved' % (self.merge or '(none)', self.expect_main), []))
            if self.merge:          # neither: someone pushed, maybe on top of the merge
                steps.append(('if it printed anything else, list the remote branches that hold '
                              'the merge (as in the check when git cannot tell) before any reset: '
                              'if the list names %s/%s, the merge is landed: skip to the last '
                              'step, no reset; if it does not, settle %s/%s with whoever pushed it '
                              'before going on' % (remote, main_b, remote, main_b),
                              ['%s branch -r --contains %s' % (g, self.merge)]))
        else:
            on = self.on_remote(pushed)
            if on:
                return self.on_origin_steps(left, pushed)
            if on is None:          # may be landed: nothing is undone until that is known
                return [self.check_first(pushed)]
            if pushed != self.expect_main:
                steps.append(('%s/%s is at %s, neither the base (%s) nor the merge: '
                              'someone else pushed. Settle %s with them by hand; the steps below '
                              'only undo this landing' % (remote, main_b, (pushed or 'absent')[:10],
                                                          self.base_text(), main_b), []))
        if left['current'] != main_b:
            steps.append(('HEAD is on %s, not %s: switch back to %s first (a reset now would move '
                          '%s, not %s)%s' % (
                              left['current'] or 'no branch (detached)', main_b, main_b,
                              left['current'] or 'HEAD', main_b,
                              '; keep or drop its tracked changes first' if left['tracked'] else ''),
                          ['%s switch %s' % (g, main_b)]))
        if left['tracked']:
            steps.append(('keep or drop the tracked changes by hand (%s): reset --keep refuses to '
                          'overwrite them' % _paths(left['tracked']), []))
        if left['main'] and left['main'] != self.expect_main:
            if left['extra']:
                keep = 'kit-stuck-%s' % left['main'][:8]
                steps.append(('%s has %d commit(s) beyond the merge that the reset would drop: %s. '
                              'Keep them on a branch first, and re-apply them (git cherry-pick) '
                              'once the lock is broken' % (
                                  main_b, len(left['extra']),
                                  '; '.join(l[:10] + l[40:] for l in left['extra'][:10])),
                              ['%s branch %s %s' % (g, keep, left['main'])]))
            steps.append(('put %s back at its base (%s)' % (main_b, self.base_text()),
                          ['%s reset --keep %s' % (g, self.expect_main)]))
        return steps

    def check_first(self, pushed):
        """clear_steps when git cannot tell whether the remote's main (`pushed`, or the tracking
        ref when ls-remote could not be read) holds the merge: one step that finds out, and
        nothing that undoes the merge (no reset, no branch kept for one) until it is known."""
        main_b, remote = self.cfg.main_branch, self.cfg.remote
        g = 'git -C "%s"' % self.root
        upstream = '%s/%s' % (remote, main_b)
        return ('%s is at %s, which this checkout cannot place against the merge %s, so whether '
                'it holds the merge cannot be told here, and nothing is undone until it can. '
                'Fetch, then list the remote branches that hold the merge. If the fetch failed, '
                'the list is stale: stop here and do not move %s. If the list names %s, '
                'the merge is landed and nothing is to be undone: bring the checkout to %s by hand '
                '(HEAD on %s, no tracked change or untracked file, %s fast-forwarded to %s), then '
                'go to the last step. If it does not, the merge is not on %s: settle %s with '
                'whoever pushed it, put %s back at its base (%s) by hand, keeping '
                'any commits beyond the merge on a branch first, then go to the last step' % (
                    upstream, (pushed or '?')[:10], (self.merge or '?')[:10], main_b, upstream,
                    upstream,
                    main_b, main_b, upstream, remote, upstream, main_b, self.base_text()),
                ['%s fetch %s' % (g, remote), '%s branch -r --contains %s' % (g, self.merge)])

    def on_origin_steps(self, left, pushed):
        """clear_steps when the remote's main (`pushed`) is a merge of this landing or descends
        from one: the merge is landed, so no step undoes it and none says reset. The steps bring
        the main checkout to the remote's main (fetch, switch back, a fast-forward), leave
        anything only the checkout has to a person, and say how a merge that reached the remote
        without a passing merged gate is taken back (a revert by hand, never run here). Commits
        on the remote's main on top of the landing are named, with the full gate to run."""
        main_b, remote = self.cfg.main_branch, self.cfg.remote
        g = 'git -C "%s"' % self.root
        upstream = '%s/%s' % (remote, main_b)
        steps = []
        if left['tracking'] != pushed:
            steps.append(('the tracking ref %s is at %s, not %s as on %s: fetch it, or break_lock '
                          'refuses' % (upstream, (left['tracking'] or '?')[:10], pushed[:10],
                                       remote), ['%s fetch %s' % (g, remote)]))
        gate_out = self.gate_out
        if gate_out is None or gate_out.code != G.OK:
            merges = list(reversed(self.merges))
            steps.append(('the merge%s %s reached %s without a passing merged gate (pushed while '
                          'the gate ran): nothing here takes it back. Fix forward, or revert it by '
                          'hand once the lock is broken%s: %s' % (
                              's' if len(merges) > 1 else '',
                              ', '.join(m[:10] for m in merges), upstream,
                              ', newest first' if len(merges) > 1 else '',
                              '; '.join('`git revert -m 1 %s`' % m for m in merges)), []))
        known = [self.expect_main] + self.tips + self.merges
        out, _ = cmd.run_text(['log', '--format=%H %s', pushed, '--not'] + known, self.root)
        on_top = ([l for l in (out or '').splitlines() if l.strip()] if out is not None
                  else left['extra'])
        if on_top:
            steps.append(('%s has %d commit(s) on top of the merge that no merged gate saw: %s. '
                          'Once the lock is broken, gate %s whole: `python "%s" -C "%s" --full`' % (
                              upstream, len(on_top),
                              '; '.join(l[:10] + l[40:] for l in on_top[:10]) + (
                                  '; and %d more' % (len(on_top) - 10) if len(on_top) > 10
                                  else ''),
                              main_b, os.path.join(S.kit_live(), 'tools', 'gate.py'), self.root),
                          []))
        if left['current'] != main_b:
            steps.append(('HEAD is on %s, not %s: switch back to %s first%s' % (
                left['current'] or 'no branch (detached)', main_b, main_b,
                '; keep or drop its tracked changes first' if left['tracked'] else ''),
                ['%s switch %s' % (g, main_b)]))
        if left['tracked']:
            steps.append(('keep or drop the tracked changes by hand (%s): the merge is landed, '
                          'and the next landing refuses a checkout that is not clean'
                          % _paths(left['tracked']), []))
        if left['untracked']:
            steps.append(('move or delete the untracked file(s) by hand (%s): the next landing '
                          'refuses a checkout that is not clean' % ', '.join(
                              left['untracked'][:10]), []))
        main = left['main']
        if main and main != pushed:
            behind, _ = refs.is_ancestor(main, pushed, self.root)
            if behind:
                steps.append(('%s (%s) is only behind %s (%s): fast-forward it' % (
                    main_b, main[:10], upstream, pushed[:10]),
                    ['%s merge --ff-only %s' % (g, upstream)]))
            else:
                out, _ = cmd.run_text(['log', '--format=%H %s', main, '--not', pushed], self.root)
                own = [l for l in (out or '').splitlines() if l.strip()]
                steps.append(('%s (%s) has commits %s (%s) does not have%s: push them or set them '
                              'aside by hand (the merge is landed; nothing is to be undone); '
                              'break_lock refuses until %s equals %s' % (
                                  main_b, main[:10], upstream, pushed[:10],
                                  ': %s' % '; '.join(l[:10] + l[40:] for l in own[:10])
                                  if own else '', main_b, upstream), []))
        return steps


def land(root, branch, expect_main, expect_tip, **kw):
    """Land `branch` (or a batch: a list, with a list of tips) from the main checkout `root`: an
    Outcome (OK, FAILED, USAGE, STUCK, REFUSED, BUSY, LANDED_UNGATED, or a machine lock code when
    the booking was refused and the merge undone)."""
    return Lander(root, branch, expect_main, expect_tip, **kw).run()


def queue(root, store=None):
    """Read-only: the repository's landing lock file and the landings in the machine lock's queue
    (entries of priority "land"; kitlib.lock.queue), as an OK Outcome: one line for the lock file,
    then one per landing, holding first, then waiting in turn order. A landing of another
    repository is marked so: they share the machine lock's slots, not the turn. A lock file that
    is not a record is shown held, lane ?, with the read error: that is when an operator checks
    here before clearing it by hand. REFUSED when the config, the lock's place or the queue cannot
    be read."""
    try:
        cfg = kconfig.load(root)
        lock = landing.LandingLock.for_config(root, cfg, sorted(cfg.section('landing')['lanes']))
        try:
            rec, unreadable = lock.read(), None
        except landing.LockError as e:
            rec, unreadable = {'lane': '?', 'phase': '?'}, str(e)
        q = lockqueue.load(store or Store())
    except (ConfigError, landing.LockError, OSError) as e:
        return G.Outcome(G.REFUSED, ['REFUSED: the landing queue of %s cannot be read: %s' % (
            root, e)])
    key = queue_key(lock.common)
    lands = [e for e in q.get('entries') or [] if e.get('priority') == 'land']
    held = sorted([e for e in lands if e.get('state') != 'waiting'],
                  key=lambda e: str(e.get('held_at') or ''))
    waiting = sorted([e for e in lands if e.get('state') == 'waiting'],
                     key=lambda e: e.get('ticket') or 0)
    lines = [landing.describe(rec) + ('; unreadable: %s' % unreadable if unreadable else ''),
             'landing queue: %s' % ('%d holding or waiting' % len(lands) if lands else 'empty')]
    for n, e in enumerate(held + waiting, 1):
        lines.append('  %d. %s %s/%s (%s, run %s, pid %s) since %s%s' % (
            n, e.get('state'), e.get('repo'), e.get('slug'), e.get('kind'), e.get('id'),
            e.get('pid'), e.get('held_at') or e.get('since') or '?',
            '' if e.get('resource') == key else '; another repository'))
    return G.Outcome(G.OK, lines)
