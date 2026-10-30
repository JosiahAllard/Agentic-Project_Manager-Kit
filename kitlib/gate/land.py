"""One branch landed on main (docs/gate.md "Landing").

`land()` runs in the repository's MAIN checkout, on its main branch:

  1  usage       --expect-main (optional) and every --expect-tip are full hashes, one tip per
                 branch, the branches distinct
  2  refusals    not the main checkout, HEAD not on main, no such branch, a branch is main,
                 more branches than "landing".batch_max (read from the config at the main
                 checkout's HEAD): REFUSED, nothing changed
  2b route      where the merged gate (7) will run, main or a load node, chosen before the
                 booking (kitlib.loadnode.landroute.plan; docs/load-nodes.md "A landing's merged
                 gate on a node"), its `gating here: ...` or `gating on load node ...` line said
                 unless it is quiet (a kit checkout, no registry, no node opted in)
  3  turn        ONE booking in the machine lock (kitlib.lock.run.book): a gate run of the
                 config's "gate".kind for <repo>/<branch[+branch...]>, holding the resource
                 `landing:<git common dir>` at priority "land" ("The queue" below); routed to a
                 node, with cores=1 (the landing's own work; the node runs the gate). At once, or
                 waited for up to --wait seconds (default "landing".wait_s); not given: the lock's
                 code and a NOT RUN line, nothing changed. The merged gate (7) runs in this slot
                 and books nothing more, unless a node-routed landing falls back to main (7)
  4  lock        fetch <remote>, then the landing lock file (kitlib.records.landing), with what is
                 left of --wait; not had: BUSY. Held at phase stuck: BUSY at once; held by a
                 process that is surely gone: broken and taken unless something is unpushed (one
                 gone at phase push with <remote>/<main> moved on since the take: BUSY naming the
                 likely landed push, the fast-forward, the next landing and the break)
  5  follow      fetch again under the lock; HEAD still on main, or REFUSED. The base is <remote>/<main> as fetched: a local main
                 only behind it (nothing unpushed) is fast-forwarded first (`git merge --ff-only`,
                 HEAD on main, a clean checkout); a local main with commits the remote lacks, or
                 one the fast-forward cannot move: REFUSED, as before. When the base is not the
                 commit the config was read at, its config is read at the base (it must name the
                 same main branch, remote and landing lock, and still take this landing). With
                 --expect-main given and not the base, it says `main moved on from X to Y; merging
                 onto Y`; it never refuses for that. Then the locked checks: the branch tip ==
                 --expect-tip, a clean main checkout, an empty stash stack (kitlib.gate.stash: the
                 repository's, whichever checkout pushed), no merge in progress, the branch not
                 already in main, every branch
                 commit that changes an owner-only path the landing would change authored by the owner
                 (kitlib.guard.ownercommits; rules of the base merged with the tip's), and the
                 branch's history free of the backlog store's root commit (`ref` and `branch`
                 stores; kitlib.backlog.at_landing). Then the tip gate check
                 (Lander.tip_gate_check): a base whose "gate" section has no step that is not
                 manual is refused first (nothing could gate a tip or the merge); then every tip
                 has a gate-OK record of exactly that commit written by a `gate.py --full` pass
                 (kitlib.records.gate_ok.verify_full), or by a default-mode or docs-only pass the
                 base's config takes (Lander.select_refusal: the record's diff covers the
                 landing's, and either the base's "gate".smoke_steps all passed in it and no gate
                 config file changes, or the change is docs-only under the base's
                 "gate".docs_only and its steps all passed, or the base sets
                 "landing".shared_paths and the change touches none of them, no strict path and
                 no gate config file; docs/gate.md "Shared paths", or the base sets
                 "landing".gate_once and the change touches no strict path and no gate config
                 file, and the merged gate then runs --full unless the merge is docs-only;
                 docs/gate.md "Gate once at landing"), or by none when the tip's
                 latest red --full run has a FLAKE targeted rerun (kitlib.gate.rerun.verify_flake);
                 a tip a test failed on, that failed twice on its branch, never lands
                 (rerun.twice_refusal);
                 a miss names the `gate.py -C <worktree>
                 --full` command that gates it, and, when the kit lands itself and the tip does
                 not contain the base, says to merge <remote>/<main> into the branch first (its
                 own gate.py may predate the record's `full` key). Then the
                 strict check (Lander.strict_check; docs/gate.md "Strict paths"): a branch that changes a path in STRICT_DEFAULTS as
                 the base holds it lands alone, with a verdict queued for its tip
                 (kitlib.gate.verdicts) by a strong-tier reviewer who is not its author (a
                 verdict carried to the tip, tools/verdict.py carry, counts only when
                 kitlib.gate.carry passes it again here against the base); its
                 merged gate then runs --full --fresh. Then the verdict check
                 (Lander.verdict_check): every other branch, each of a batch, lands only with a
                 verdict queued for its tip by any reviewer who is not its author, a carried one
                 checked the same way. Any miss: REFUSED, the lock released, nothing
                 merged. Last, the scope check (Lander.scope_check): every path the branch changes
                 in a scope of a record it holds, in SCOPE_ALWAYS or in a folder store's folder;
                 a miss is a WARNING after the LANDED line while SCOPE_REFUSES is False, else
                 REFUSED as above
  5b tree        the landing tree (prepare_tree): a detached linked worktree the kit owns, locked
                 with the reason TREE_LOCK, at <main checkout>/<worktrees_dir>/_landing
                 (Lander(tree=) names another place). Made the first time; reused when clean,
                 checked out at the base; recreated, with a line `the landing tree <path> <why>:
                 recreated`, when it is gone, dirty, mid-merge or not a work tree of its own. A
                 folder there that is not this tree (not listed by git, or listed and not the
                 locked detached tree): REFUSED, `move it aside`, the folder left as it is. The
                 merge, the merged gate and every check of the merged tree happen in this tree;
                 the main checkout is never merged into, reset or gated
  6  merge       `git merge --no-ff <tip>` in the landing tree; a conflict is aborted there (a
                 failed abort leaves only the tree dirty: the next landing recreates it), the
                 lock released: FAILED, the verdict line `CONFLICT <branch> with main <base>:
                 the author merges main`. Main is never moved
  7  gate        the landing tree gated in the slot of step 3 (kitlib.gate.runner.merged_gate,
                 tree=, slot=) with the config of the base: every step that is not manual, as
                 gate.py --full selects them, whatever paths the merge changes; a step that
                 passed in the branch gate
                 of exactly this tree may count without running (kitlib.gate.reuse; never with
                 --fresh or --full; a batch reuses per step like one branch; one branch also
                 from its tip's --full record of this tree, reuse.same_tree). A merge that is docs-only under the base's config
                 (not --full) runs only the "gate".docs_only steps (docs/gate.md "Docs-only
                 changes"). A base that sets "landing".gate_once makes any other merge --full
                 (Lander.once_escalate: each step's full_cmd, no reuse; docs/gate.md "Gate once
                 at landing"). Red, or a gate where nothing ran: NOT LANDED, the
                 merge dropped (it is only in the landing tree; the landing never moves main),
                 the lock released: FAILED / REFUSED.
                 Routed to a node (2b): main gates it instead when the tip's --full record has
                 the merge's tree (landroute.same_tree_why), when no node qualifies at the turn
                 (landroute.recheck; another that does takes it), or when nothing ran on the node
                 (landroute.Fallback): `gating here: <why>`, the 1-slot run released, then a
                 full run of the kind booked as in step 3 with no wait limit, and the gate run
                 in it as above. Else the land job runs it there (landroute.run_on_node, docs
                 included): a pass is this step's green; any other end is NOT LANDED, never
                 gated again on main, and so is any exception.
                 The base's config (step 4, follow) names another "gate".kind than step 3
                 booked: on main, `the base <b> books "gate".kind <k>, not <k0> ...`, the slot
                 released first, then the base's kind booked at its own size with no wait limit
                 (rebook_kind), the lock file still held; the lock refusing it is NOT LANDED.
                 Routed to a node, recheck asks for the base's kind's slots (route_need; a kind
                 that cannot be sized gates here as above), and the 1-slot run stays
  7b unchanged   green: the landing tree's HEAD still the merge, no tracked change, no untracked
                 file there, the stash stack still empty; else NOT LANDED as on red and nothing
                 pushed (the pushed commit is the gated one). What the owner does in the main
                 checkout meanwhile (a commit, an edit, a push) reaches neither the gated tree nor
                 the pushed commit
  8  record      green: the gate-OK record for the merge commit (pre-push reads it), its
                 config_rev the base, its `full` whether the merged gate ran --full, its `mode`
                 "docs" when it ran only the docs-only steps, its `ran_on` the node, host key
                 and job when the gate ran on a load node
  9  push        `git push <remote> <merge>:refs/heads/<main>` from the main checkout, never
                 forced. Someone pushed <remote>/<main> to a commit without the merge while the
                 gate ran: the push is refused, NOT LANDED (FAILED, `nothing landed`), the lock
                 released; so is a push git refuses (exit 1 with git's own `! [rejected]` or
                 `! [remote rejected]` line, PUSH_REFUSED) while <remote>/<main> is the base,
                 and a push that never connected (push_never_connected: exit 128, only
                 `fatal: unable to access '<url>': ` with a curl connect error,
                 PUSH_NEVER_CONNECTED) unless <remote>/<main> shows the merge
  10 verify      `git ls-remote` must show the merge on <remote>/<main>; then the tracking ref
                 is fetched, the main checkout's main brought to the merge (sync_main: a
                 fast-forward when HEAD is on main, main has nothing of its own and no local
                 change is in the way; else a WARNING naming the command, main left as it is),
                 the lock file and the slot released: OK (LANDED)
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
  13 mirror      once the slot is released too (Lander.run, nothing held): the merge pushed to
                 each opted-in load node's mirror of main (kitlib.loadnode.mirror.refresh, at
                 most its time cap); its lines go before the LANDED line, a failure or an
                 exception or Ctrl+C there is a WARNING, and the landing stands

An exception or Ctrl+C under the lock (recover): before the push the lock is released and the
merge dropped (a merge in progress in the landing tree aborted, best effort), main untouched, and
KeyboardInterrupt is raised again (exit 130); one that ends STUCK is returned as STUCK (exit 3)
whatever raised it. After the push is verified (phase pushed) the landing is LANDED whatever stops
it: a lock release that fails, or an exception or Ctrl+C there, is LANDED with a WARNING (exit 0).
The slot of step 3 is released whenever the landing ends (Lander.run): with the lock file, or at
the end; STUCK included (the stuck lock file keeps the next landing out). Exit 9
(LANDED_UNGATED) is retired: no landing produces it (docs/gate.md "Exit codes").

A landing's verdict is its exit code and its LANDED / NOT LANDED / CONFLICT / STUCK line; a `GATE
OK` line appears only in a landing that LANDED (a green merged gate that is not landed is
reworded).

STUCK is only a push that may have landed and cannot be told: interrupted, timed out, died (exit
128, except a push that never connected, push_never_connected: NOT LANDED), ended with exit 1 without git's refusal line (no message: a git ended from outside,
refs.KILLED_SHAPE; or only a stray warning) or reported done while <remote>/<main> is still the
base, or with <remote>/<main> unreadable. A push git refused (exit 1 with git's own `! [rejected]`
or `! [remote rejected]` line, PUSH_REFUSED) while <remote>/<main> is still the base published
nothing: NOT LANDED (FAILED), the lock released, as on a red merged gate. The landing lock stays held at phase
`stuck` so no other landing starts on top of it, and the message says what is left and the
commands that clear it. Nothing is ever reset: the main checkout was never merged into.

The queue: a landing is ONE entry in the machine lock's queue (step 3; kitlib.lock.run.book with
`resource=queue_key(<git common dir>)`, `priority="land"`). The resource lets one landing of a
repository hold at a time, every checkout of it sharing the key; the others wait in the machine
lock's queue, landings ahead of ordinary waiting runs. `land.py --queue` (queue()) lists the
entries of priority "land". The lock file (step 4) stays: the stuck record, a gone holder's break,
and a landing of a tool outside the machine lock. A node-routed landing that falls back to main
(step 7) gives the resource up while it books again, holding the lock file: the next landing of
the repository may then book and wait at the lock file, with the holder waiting in the queue
behind it. That landing sees it (Lander.yield_turn), releases its run and books again, behind the
holder, with what is left of its --wait, at most once a look (poll_s). That booking not given in
time is NOT RUN with the machine lock's exit code, not BUSY.

`land.py --check-strict BRANCH` (check_strict()) is the strict check's dry run: read-only, no lock
and no verdict, it says whether each branch changes a strict path (Lander.strict_paths).

A batch (several branches, one --expect-tip each, merged in the order given): every branch passes
the locked checks, and no tip may contain another; the branches are merged one after another with
--no-ff in the landing tree, the merged tree is gated once against the base, and the last merge
is pushed. A conflict on any branch aborts that merge and drops the earlier ones; a red gate drops
them all (main never held them). Each branch gets its own result record.

A submitted landing (kitlib.gate.submit; `land.py --submit`) is this landing run by a detached
process: its records are written `running` before it starts, and every outcome finishes them.
"""
import ast
import itertools
import os
import re
import shutil
import stat
import time

from kitlib import gate as G
from kitlib.backlog import at_landing
from kitlib.backlog import record as rmod
from kitlib.backlog import scope as scmod
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Refusal
import kitlib.config as kconfig
from kitlib.config.types import ConfigError
from kitlib.content import oncommit
from kitlib.gate import cleanup, detect, gitstatus, result, reuse, runner, stash, strict_defaults
from kitlib.gate import carry, rerun, verdicts
from kitlib.gate import steps as S
from kitlib.git import cmd, refs, repo
from kitlib.guard import ownercommits
from kitlib.host import registry
from kitlib.lock import queue as lockqueue
from kitlib.lock import run as lockrun
from kitlib.lock.store import EXIT_USAGE, LockError, Store
from kitlib.records import gate_ok
from kitlib.records import landing

PUSH_TIMEOUT = 600      # seconds
# git's own ref status line for a push it refused (transport.c; untranslated, and kitlib.git.cmd
# runs git under LC_ALL=C): `! [rejected]` decided before sending (non-fast-forward, fetch first),
# `! [remote rejected]` the remote's answer (a hook declined). `failed to push some refs` alone is
# not one: git prints it for any failed push, a status report lost after the upload included
PUSH_REFUSED = ('! [rejected] ', '! [remote rejected] ')
# git's line for a push over HTTP(S) that never connected (exit 128, nothing else on stderr):
# `fatal: unable to access '<url>': <curl's error>`, which git dies with only when its first
# request, the ref discovery (remote-curl.c discover_refs), fails, before any pack is sent; with
# curl's error one that only a connection never made gives: `Could not resolve host: <host>` (or
# `proxy`), `Failed to connect to <host>:<port>` or `<host> port <port>` (curl 8 adds `after <n>
# ms`; the reason after `: ` is the OS's or curl's, as `Connection refused`, `Timed out`, `Could
# not connect to server`), `Connection timed out after <n> milliseconds` and `Resolving timed out
# after <n> milliseconds` (curl's connect timeout in those states, before any request is sent).
# git 2.55.0.windows.5 with libcurl 8.21.0 printed the first two forms, `<host>:<port> after <n>
# ms: Could not connect to server` for both a refused port and a timed-out connect. Every other
# curl error (a reset, a TLS failure, `Operation timed out`) came after the connection: STUCK
PUSH_NEVER_CONNECTED = re.compile(
    r"fatal: unable to access '[^']*': (?:"
    r"Could not resolve (?:host|proxy): \S+"
    r"|Failed to connect to \S+(?::\d+| port \d+)(?: after \d+ ms)?: [^:]+"
    r"|(?:Connection|Resolving) timed out after \d+ milliseconds"
    r")\Z")
FETCH_TIMEOUT = 300
MERGE_TIMEOUT = 300
POLL_S = 5.0
STEP_CMD = '    $ '      # a STUCK clear step's command line starts with this
TREE_NAME = '_landing'   # the landing tree's folder, under the config's worktrees_dir
TREE_LOCK = 'kit landing tree (kitlib.gate.land)'     # its git worktree lock reason
# The scope check (step 5, Lander.scope_check): True refuses a branch that changes a path outside
# the scopes of the records it holds (REFUSED, nothing merged); False lands it with a WARNING
# after the LANDED line. It warns for now; making it refuse is this one line (a follow-up record).
SCOPE_REFUSES = False
# Paths every branch may change whatever records it holds, as scope members: tests, and the doc
# index the doc-budget check makes every new doc add a line to. A folder store's own folder is
# allowed too (Lander._scope_stores). The files tools/render_content.py writes are not: kitlib
# cannot import tools/ to learn them, so a rendering change needs them in a record's scope.
SCOPE_ALWAYS = ('tests/**', 'docs/INDEX.md')
SCOPE_SHOWN = 10        # outside paths a scope-check line names; more are counted
# The strict paths (Lander.strict_check; docs/gate.md "Strict paths"): the globs of this file's
# STRICT_DEFAULTS, owner-authored, read from the base with `git show` and parsed, never imported
# from the branch; a base without the file (a repository that is not the kit) has the running
# kit's list. A branch changing a matching path lands only alone, with a queued verdict at its tip
# from a strong-tier reviewer who is not its author, and its merged gate runs --full --fresh.
STRICT_FILE = 'kitlib/gate/strict_defaults.py'
STRICT_NAME = 'STRICT_DEFAULTS'
STRICT_SHOWN = 5        # strict paths a refusal names; more are counted


class _Stuck(Exception):
    pass


def push_never_connected(rc, err):
    """Whether a push that ended with exit `rc` and stderr `err` never connected to the remote,
    so no byte of it can have reached it: git died (exit 128) and every line it printed is
    PUSH_NEVER_CONNECTED. Any other line, a later-phase one included, or any other exit: False."""
    lines = [l.strip() for l in (err or '').splitlines() if l.strip()]
    return rc == 128 and bool(lines) and all(PUSH_NEVER_CONNECTED.match(l) for l in lines)


def _paths(entries):
    return ', '.join('%s %s' % (xy.strip(), p) for xy, p in entries[:10]) or 'none'


def _rmtree(path):
    """Remove the folder `path` and everything in it, read-only files included (OSError when
    something there cannot go, such as a file another process holds open)."""
    def again(fn, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        fn(p)
    try:
        shutil.rmtree(path, onexc=again)        # Python 3.12 and later
    except TypeError:
        shutil.rmtree(path, onerror=again)


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


def _covers(members, path):
    """True when a scope member in `members` covers `path`: one's components a prefix of the
    other's, casefolded (the comparison of kitlib.backlog.scope.members_collide). Only the member
    is read in the scope grammar; the path is a literal changed path, so a comma or a wildcard
    character in it is an ordinary character. A member the grammar cannot read, and a path
    _path_error refuses, cover nothing. Pure."""
    if _path_error(path):
        return False
    have = path.casefold().split('/')
    for m in members:
        try:
            want = scmod.scope_components(m)
        except scmod.UndecidableScope:
            continue
        n = min(len(want), len(have))
        if want[:n] == have[:n]:
            return True
    return False


def _path_error(path):
    """None when the scope check can decide `path`, else why not: such a path is outside every
    scope and left out of the widening command. A comma or a wildcard character is decidable
    (_unnamed). Pure."""
    why = scmod.path_shape_error(path, 'path', tab_ok=True, comma_ok=True)
    if why:
        return why
    if path.casefold() == scmod.NONE:
        return 'a path that reads as the `none` sentinel'
    return None


def _unnamed(path):
    """True when no scope member can name `path` as itself: a comma or a wildcard character is a
    parse error in the scope grammar (kitlib.backlog.scope). A folder member that holds it covers
    it; it is left out of the widening command. Pure."""
    return ',' in path or any(ch in scmod.WILDCARD_CHARS for ch in path)


def parse_strict(text):
    """The globs `text`, a strict_defaults.py, assigns to STRICT_DEFAULTS: read with ast, never
    run, so nothing of the file executes. Raises ValueError unless it assigns STRICT_DEFAULTS
    exactly once, at the top level, a literal tuple or list of non-empty strings. Pure."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as e:
        raise ValueError('it does not parse: %s' % ' '.join(str(e).split()))
    values = []
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign) else
                   [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
        if any(isinstance(t, ast.Name) and t.id == STRICT_NAME for t in targets):
            values.append(node.value if isinstance(node, ast.Assign) and len(targets) == 1
                          else None)
    if len(values) != 1:
        raise ValueError('it assigns %s %d times, not once' % (STRICT_NAME, len(values)))
    try:
        globs = ast.literal_eval(values[0]) if values[0] is not None else None
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        globs = None
    if not isinstance(globs, (tuple, list)):
        raise ValueError('%s is not a literal tuple of globs' % STRICT_NAME)
    if not globs or not all(isinstance(g, str) and g.strip() for g in globs):
        raise ValueError('%s is not a tuple of non-empty glob strings' % STRICT_NAME)
    return tuple(globs)


def tier_models(path=None):
    """`(strong, weak, None)`: the casefolded models hosts.json gives the strong or deep tier of
    any host (strong) and the light or standard tier of any host (weak); or `(None, None, why)`
    when hosts.json cannot be read. `path`: kitlib.host.registry.load's."""
    try:
        hosts = registry.load(path)
    except registry.RegistryError as e:
        return None, None, str(e)
    strong, weak = set(), set()
    for h in hosts.values():
        for tier, model in h.tiers:
            (strong if tier in ('strong', 'deep') else weak).add(model.strip().casefold())
    return strong - {''}, weak - {''}, None


def strong_models(path=None):
    """`(models, None)`: tier_models' strong models less every weak one (a model in both cannot
    show which tier ran: Codex's account-default); or `(None, why)` when hosts.json cannot be
    read."""
    strong, weak, why = tier_models(path)
    return (None, why) if strong is None else (strong - weak, None)


def named_model(model, strong, weak):
    """The one model of `strong` or `weak` that `model`, a verdict's reviewer_model, names; else
    None. `model` names it when it is that model (case and outer spaces aside), or a full model
    id holding it as a word of its own (`claude-opus-5-5` names opus) and no other model of
    `strong` or `weak`. An id naming none or two is None. Pure."""
    models, text = (strong | weak) - {''}, model.strip().casefold()
    if text in models:
        return text
    named = [m for m in models if re.search(r'(?<![0-9a-z])%s(?![0-9a-z])' % re.escape(m), text)]
    return named[0] if len(named) == 1 else None


def strong_alias(model, strong, weak):
    """The strong-only model (strong less weak, as strong_models) that `model`, a verdict's
    reviewer_model, names (named_model); else None. An id naming none, a weak one or two is
    None: fail closed. Pure."""
    named = named_model(model, strong, weak)
    return named if named in strong - weak else None


def _word_in(word, text):
    """True when `word` appears in `text` as a word of its own: no letter, digit, '_' or '-' on
    either side, so `worker` is not found in `strong-worker` nor `strong-worker` in
    `strong-worker-2`. A ':' before it (a `plugin:` prefix) is a boundary."""
    return bool(re.search(r'(?<![\w-])%s(?![\w-])' % re.escape(word), text))


def is_author(entry):
    """True when the verdict entry's reviewer may be the branch's author: an id its reviewer_id
    lists (split on '/', ',', ';' or white space), or an agent type its reviewer names
    (verdicts.reviewer_types), appears anywhere in `author` as a word of its own (_word_in),
    casefolded, whatever separates it from the rest: `Kit Test / strong-worker`,
    `Kit Test (agent) (pm-kit:strong-worker)`, `x | strong-worker` and `x strong-worker` all name
    strong-worker; `x / strong-worker` does not name worker. A reviewer naming no type is refused
    earlier, by verdicts.check. Pure."""
    author = entry['author'].casefold()
    ids = re.split(r'[/,;\s]+', entry['reviewer_id'].casefold())
    words = [w for w in ids if w] + sorted(verdicts.reviewer_types(entry['reviewer']))
    return any(_word_in(w, author) for w in words)


class _Held(object):
    """What the scope check found for one branch: `held`, id -> [scope members, command(*args)
    for its store's write]; `read`, a store holding a record was read; `unread`, why a store
    could not be; `folders`, folder stores' own folders (always allowed)."""

    def __init__(self):
        self.held, self.read, self.unread, self.folders = {}, False, [], []

    def hold(self, rid, members, command):
        if rid in self.held:
            self.held[rid][0].extend(m for m in members if m not in self.held[rid][0])
        else:
            self.held[rid] = [list(members), command]

    def members(self):
        return [m for members, _ in self.held.values() for m in members]


class Lander(object):
    """One landing of a branch, or a batch: `branch` and `expect_tip` are each a string or a list
    of the same length. `merged_gate(lander) -> Outcome` replaces the merged-tree gate (tests);
    `git_env` is the environment git's writes get (None: this process's, scrubbed); `book_kw`
    goes to kitlib.lock.run.book (its `store`: the machine lock's, default the machine store);
    `book` replaces kitlib.lock.run.book (tests); `lock_kw` goes to LandingLock (tests pass
    process probes). `wait_s` None: the config's "landing".wait_s; one budget covers the booking
    and the lock file. `keep_branch` True: no branch cleanup after the landing (step 12).
    `fresh` True: the merged gate runs every step, reusing none (kitlib.gate.reuse); a branch
    that changes a strict path sets it and `full` True itself (strict_escalate).
    `tree`: where the landing tree is (step 5b); None: <main checkout>/<worktrees_dir>/_landing.
    `submitted` (kitlib.gate.submit.restore): the detached landing of a --submit, a dict with
    `runs` (the result.Run per branch whose running records the submission wrote; every outcome
    finishes them), `output` (the file this process's output goes to) and `replaced` (the land
    records those running records replaced).
    `node_route`: where the merged gate runs (step 2b), an object with kitlib.loadnode.landroute's
    plan, route_line, recheck, same_tree_why, run_on_node and Fallback (None: that module)."""

    def __init__(self, root, branch, expect_main, expect_tip, summary=None,
                 full=False, wait_s=0, say=None, book_kw=None, git_env=None, lock_kw=None,
                 merged_gate=None, log_dir=None, poll_s=POLL_S, pid=None, keep_branch=False,
                 fresh=False, book=None, submitted=None, tree=None, node_route=None):
        self.root = root
        self.node_route = node_route    # None: kitlib.loadnode.landroute, imported when routed
        self.route = None           # the landroute.Route plan_route chose before the booking;
                                    # None again once a fallback gates on main (rebook_here)
        self.holder = None          # the landing lock file's record when another holds it
        self.yielded_to = None      # the holder yield_turn last gave the turn back to
        self.booked_kind = None     # the "gate".kind of the last booking (book_slot)
        self.tree_given = tree      # Lander(tree=), or None: the default place (tree_path)
        self.tree = None            # the landing tree's path, once prepare_tree placed it
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
        # a direct landing's result.Run per branch, made when it joins the landing queue (_run,
        # before book_turn) so started_at says so, as a submitted landing's does; started, and
        # moved into result_runs, only once the locked checks passed (_locked)
        self.queued_runs = []
        self.output_log = sub.get('output')
        self.replaced = [r for r in (sub.get('replaced') or []) if isinstance(r, dict)]
        self.keep_branch = keep_branch  # True: no branch cleanup after the landing
        self.cleaned = False        # the branch cleanup ran (clean_up)
        self.synced = False         # sync_main ran (the main checkout brought to the merge)
        self.cleanup = None         # its facts: {branch: kitlib.gate.cleanup entry, or None}
        self.fresh = fresh          # True: the merged gate reuses no branch-gate step
        self.prior = None           # result records naming the tips, read before this landing
                                    # writes its own (_locked); None: reuse nothing
        self.tip_records = {}       # tip -> the gate-OK record tip_refusal landed it on (none for
                                    # a FLAKE rerun): the merged gate's input tree (reuse.planner)
        self.landed_at = None       # the remote's main once verified to hold the merge: the merge,
                                    # or a commit on top of it pushed right after
        self.scope_notes = []       # scope-check NOTE lines, said before the LANDED line
        self.scope_warnings = []    # scope-check misses while SCOPE_REFUSES is False, said after it
        self.scope_reads = {}       # the stores the scope check read once per landing: key -> read
        self.strict_hits = {}       # branch -> the strict paths it changes (strict_check)
        self.strict_review = None   # the verdict entry a strict branch lands with (strict_check)
        self.strict_carry = None    # its carry facts, checked again here, when it was carried

    @property
    def pairs(self):
        return list(zip(self.branches, self.tips))

    # ---------------------------------------------------------------- git

    def git(self, *args, timeout=cmd.GIT_TIMEOUT, cwd=None):
        """git `args` in `cwd` (default the main checkout); the landing tree's commands pass
        cwd=self.tree."""
        r = cmd.call(list(args), cwd or self.root, timeout=timeout, env=self.git_env)
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
        """The refusals before the landing joins the queue, or None. A landing with no result
        folder (kitlib.gate.result.result_dir: no dev_drive in the machine config) is refused:
        its land records, which the session waiting on it reads, could not be written."""
        place = repo.classify(self.root)
        if place.kind != repo.MAIN:
            return ('%s is not a main checkout (%s): a landing runs in the main checkout, on its '
                    'main branch' % (self.root, place.kind))
        self.root = place.top
        try:
            result.result_dir(self.root)
        except result.ResultError as e:
            return 'no result folder for the land records: %s' % e
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
        """The refusals self.cfg decides (main branch checked out, batch_max, the branches):
        None or a refusal."""
        current, why = refs.current_branch(self.root)
        if current != self.cfg.main_branch:
            return 'the main checkout is on %s, not %s' % (current or why or '(detached HEAD)',
                                                         self.cfg.main_branch)
        most = self.cfg.section('landing')['batch_max']
        if len(self.branches) > most:
            return 'a batch of %d branches is more than "landing".batch_max (%d)' % (
                len(self.branches), most)
        for b in self.branches:
            if b == self.cfg.main_branch:
                return 'the branch to land is %s itself' % b
            if not self.ref('refs/heads/%s' % b):
                return 'no branch %s in this repository' % b
        return None

    def queue_store(self):
        if self.store is None:
            self.store = (self.book_kw or {}).get('store') or Store()
        return self.store

    def wait_budget(self):
        wait_s = self.wait_s if self.wait_s is not None else self.cfg.section('landing')['wait_s']
        return max(0, wait_s or 0)

    def routes(self):
        """The load-node routing: Lander(node_route=), or kitlib.loadnode.landroute."""
        if self.node_route is None:
            from kitlib.loadnode import landroute
            self.node_route = landroute
        return self.node_route

    def plan_route(self):
        """Step 2b, before the booking: where the merged gate runs (landroute.plan), its line said
        unless it is quiet. A test's merged gate always gates here. A plan that raises gates here
        too, said as `gating here: the load-node plan failed: <error>`."""
        lr = self.routes()
        try:
            self.route = lr.plan(self.root, runner.repo_name(self.cfg, self.root),
                                 self.cfg.section('gate')['kind'], store=self.queue_store(),
                                 merged_gate_fn=self.merged_gate_fn)
        except Exception as e:      # noqa: BLE001 - nothing booked or changed yet: gate on main
            self.route = None
            self.say('gating here: the load-node plan failed: %s' % _what(e))
            return
        line = lr.route_line(self.route)
        if line:
            self.say(line)

    def on_node(self):
        """True while the merged gate is routed to a load node."""
        return self.route is not None and self.route.node is not None

    def book_turn(self):
        """Step 3: the landing's ONE booking in the machine lock, waited for up to the --wait
        budget: a gate run of the config's "gate".kind for <repo>/<branches>, holding the
        resource queue_key(<git common dir>) at priority "land"; with cores=1 when the plan
        routed the merged gate to a load node. The merged gate runs in this slot (self.slot), or
        a node-routed landing's fallback books again (rebook_here). Raises
        kitlib.lock.store.LockError when the lock does not give it, and landing.LockError when the
        landing lock's place cannot be worked out."""
        self.lock = landing.LandingLock.for_config(self.root, self.cfg, **self.lock_kw)
        wait_s = self.wait_budget()
        self.deadline = time.monotonic() + wait_s
        self.book_slot(wait_s, cores=1 if self.on_node() else None)

    def book_slot(self, wait_s, cores=None):
        """Book the landing's run into self.slot: the gate kind, the landing resource, priority
        "land", waiting up to `wait_s` (None: no limit; 0: now or never), with `cores` cpu slots
        (None: the kind's size, or book_kw's). Says its held line. Raises LockError."""
        kw = dict(self.book_kw or {}, store=self.queue_store(), gate=True,
                  resource=queue_key(self.lock.common), priority='land', wait_s=wait_s,
                  no_wait=wait_s == 0, note='landing: %s' % self.branch)
        if cores is not None:
            kw['cores'] = cores
        if self.submitted:
            kw['detached'] = True   # its launcher's ancestry is in ORIGIN_ENV (kitlib.gate.submit)
        book = self.book_fn or lockrun.book
        kind = self.cfg.section('gate')['kind']
        self.slot = book(kind, runner.repo_name(self.cfg, self.root),
                         runner.slug_of('+'.join(self.branches)), **kw)
        self.booked_kind = kind
        self.say(self.slot.held_text())

    def rebook_here(self, why):
        """A node-routed landing's merged gate falls back to main after its booking: says
        `gating here: <why>`, releases the 1-slot run first (booking the full kind while holding
        it may wait on itself: a kind of every slot, an owner whole hold, a gpu pool), then books
        the full kind as book_turn does, with no wait limit. The landing lock file stays held, so
        no other landing merges meanwhile; the next landing in the queue may book, and it gives
        the turn back (yield_turn). Raises LockError."""
        self.say('gating here: %s' % why)
        self.say("releasing the landing's 1-slot run and booking a full %s run for main's "
                 "merged gate, waiting with no limit" % self.cfg.section('gate')['kind'])
        self.route = None
        self.drop_slot(0, 'done')
        self.book_slot(None)

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
            if self.yield_turn(left):
                time.sleep(max(0.01, min(self.poll_s, left)))   # one booking a look, at most
                continue
            if not said:
                said = True
                self.say('waiting up to %d s for the landing lock: %s' % (left, why))
            time.sleep(max(0.01, min(self.poll_s, left)))

    def try_take(self):
        """One look: `(None, False)` taken, `(why, final)` not; final: waiting cannot help."""
        try:
            rec = self.lock.take(self.branches, pid=self.pid)
        except landing.Busy as e:
            self.holder = e.record
            return self.held_by(e.record)
        self.holder = None
        self.token = rec['token']
        return None, False

    def turn_waiters(self):
        """The machine lock's waiting queue entries that name this repository's landing
        resource."""
        key = queue_key(self.lock.common)
        q = lockqueue.load(self.queue_store())
        return [e for e in q.get('entries') or []
                if e.get('state') == 'waiting' and e.get('resource') == key]

    def yield_turn(self, left):
        """At the landing lock file, holding the landing's turn: True when it gave the turn back,
        because the lock file's holder (self.holder, not gone) is a landing of this repository
        waiting in the machine lock's queue for that turn (a node-routed landing's fallback
        booking again, rebook_here): else each waits for the other until this landing's --wait
        runs out. Its run is released and booked again, behind the holder, waiting for what is
        left (`left`) of the --wait budget, with one cpu slot again when its merged gate is routed
        to a load node. Its line is said once per holder: the new booking may be granted at once
        (a backfill, the holder's start being far off) and given back again at the next look,
        one look (poll_s) apart (take). A queue that cannot be read: False. Raises LockError (the
        new booking)."""
        rec = self.holder or {}
        branches = rec.get('branches')
        if self.slot is None or not isinstance(branches, list) or not branches:
            return False
        slug = runner.slug_of('+'.join(str(b) for b in branches))
        try:
            waiting = [e for e in self.turn_waiters() if e.get('slug') == slug]
        except (OSError, ValueError):
            return False
        if not waiting:
            return False
        who = (rec.get('token'), rec.get('pid'), tuple(map(str, branches)))
        if who != self.yielded_to:
            self.yielded_to = who
            self.say('the landing holding the landing lock (%s) waits in the machine lock for '
                     "this repository's landing turn, which this landing holds: releasing it and "
                     'booking again behind it, waiting up to %d s'
                     % (', '.join(map(str, branches)), left))
        self.drop_slot(0, 'done')
        self.book_slot(left, cores=1 if self.on_node() else None)
        return True

    def held_by(self, rec):
        """`(None, False)` when a gone holder's lock was broken and taken; else `(why, final)`."""
        if rec.get('phase') == 'stuck':
            return ('%s: a stuck landing is cleared by hand, as its STUCK report says'
                    % landing.describe(rec)), True
        if not rec.get('token') or self.lock.holder_state(rec) != 'dead':
            return landing.describe(rec), False         # unreadable, alive or a token holder
        why = self.lock.unpushed()
        origin = self.ref(self.origin_ref)
        hint = self.behind_hint(self.ref(self.main_ref), origin) if why else ''
        if hint and not os.path.exists(os.path.join(self.lock.common, 'MERGE_HEAD')):
            took = rec.get('origin_at_take')
            if rec.get('phase') == 'push' and took and origin != took:   # killed after its push
                return ('%s, and its holder is gone after its push: %s/%s (%s) moved on from %s, '
                        'where it took the lock, so its push most likely landed and only the main '
                        'checkout\'s fast-forward was skipped. Not broken, because %s. Then land '
                        'again: the next landing breaks it itself; or break it with: %s' % (
                            landing.describe(rec), self.cfg.remote, self.cfg.main_branch,
                            origin[:10], took[:10], hint[2:], self.break_command())), True
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
            rec = self.lock.take(self.branches, pid=self.pid)
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
        entries, why = gitstatus.status(self.root)
        if entries is None:
            return 'git status failed: %s' % why
        if entries:
            return 'the main checkout is not clean: %s' % ', '.join(
                '%s %s' % (xy.strip(), p) for xy, p in entries[:10])
        why = stash.refusal(self.root)      # the repository's one stack, whichever checkout pushed
        if why:
            return why
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
        for t in self.tips:
            why = self.owner_refusal(t)
            if why:
                return why
        for t in self.tips:
            why = at_landing.ancestry_refusal(self.root, self.cfg, t, env=self.git_env)
            if why:
                return why
        return (self.tip_gate_check() or self.strict_check() or self.verdict_check()
                or self.scope_check())

    # ---------------------------------------------------------------- the tips' full gate

    def tip_gate_check(self):
        """A locked check (docs/gate.md "Landing"): every branch tip has a gate-OK record for
        exactly that commit that git still agrees with (gate_ok.verify), and the record lands it
        (tip_refusal): a `gate.py --full` pass's (gate_ok.full_of), or a default-mode or docs-only
        pass's that the base config takes (select_refusal). A record from before records said
        whether the gate was --full, one whose mode is unknown, and a record of any other commit
        do not count; an --only run writes none. A tip with neither lands on a FLAKE targeted
        rerun of its latest red --full run (kitlib.gate.rerun.verify_flake). A tip a test failed
        on, when that test failed twice over the branch's commits since its merge base with the
        base, or whose rerun records cannot be read, never lands, whatever record it has
        (rerun.twice_refusal). None, or the first miss: why, and what gates that tip whole
        (full_gate_advice; REFUSED, nothing merged). It only adds to the merged gate. A base
        gate that --full selects no step from refuses first (no_full_step).

        The record's `gate_config` is not compared with any config: the branch gate writes a
        record only for a clean tree, with `config_rev` the tip, so the hash is of the tip's own
        committed config. Re-reading that config here would use the landing kit's defaults and
        schema, which refuse honest records whenever the two kits differ (a kit branch that
        changes the gate's defaults or adds a config key); and the config that decides what
        lands is the base's, which the merged gate reads, and which alone decides whether a
        record that is not --full counts (a branch never loosens its own landing)."""
        why = self.no_full_step()
        if why:
            return why
        for b, t in self.pairs:
            why = rerun.twice_refusal(self.root, t, self.expect_main)
            if why:
                return '%s tip %s does not land: %s' % (b, t[:10], why)
            why = self.tip_refusal(t)
            if why:
                return '%s tip %s has no full-gate OK record: %s; %s' % (
                    b, t[:10], why, self.full_gate_advice(b, t))
        return None

    def tip_refusal(self, tip):
        """Why `tip`'s gate-OK record does not land it, or None: verify_full's reason, unless the
        record is a default-mode ("select") or docs-only ("docs") pass that select_refusal takes,
        or the tip's latest red --full run has a FLAKE targeted rerun (rerun.verify_flake; its
        reason is added only when the tip has rerun records). The record it lands on goes in
        self.tip_records."""
        self.tip_records.pop(tip, None)
        rec, why = gate_ok.verify_full(self.root, tip)
        if why is None:
            self.tip_records[tip] = rec
            return None
        rec, _ = gate_ok.verify(self.root, tip)
        mode = gate_ok.mode_of(rec) if rec is not None else None
        if mode in ('select', 'docs'):
            more = self.select_refusal(rec, tip, mode)
            if more is None:
                self.tip_records[tip] = rec
                return None
            why = '%s, and %s' % (why, more)
        flake = rerun.verify_flake(self.root, tip)
        if flake is None:
            return None
        if rerun.has_records(self.root, tip):
            why = '%s; its targeted rerun does not land it: %s' % (why, flake)
        return why

    def select_refusal(self, rec, tip, mode):
        """Why a verified record of `tip` that is not --full (`mode` "select" or "docs") does not
        land it, or None. Everything is read from the base's config (self.cfg), never the tip's.
        The landing diff is merge-base(tip, base)..tip. A record counts when the files its own
        diff (its `base`..tip) changes cover the landing diff (the steps it ran were picked for at
        least that change), and either
          docs    the landing diff is docs-only under the base (reuse.docs_only_change: its
                  "gate".docs_only, never a config, hook or strict path) and every base
                  docs_only step passed in the record (pass, warn or reused); or
          smoke   the record is a default-mode pass ("select"), the base sets "gate".smoke_steps,
                  every one of them passed in the record, and the landing diff changes none of the
                  gate's config files (reuse.config_hits: `.kit.json`, a section file, the hook
                  module); and
          shared  when the base sets "landing".shared_paths (docs/gate.md "Shared paths"), a
                  default-mode record lands without "gate".smoke_steps, and any record that is
                  not --full (docs or select, smoke steps or not) is refused when the landing diff
                  changes a shared path or a strict path (shared_refusal, checked before the
                  docs route), beside the config check above;
          once    when the base sets "landing".gate_once (docs/gate.md "Gate once at landing"),
                  a default-mode record lands whatever "gate".smoke_steps and "landing".shared_paths
                  say: only a strict path (strict_refusal, before the docs route) or a gate config
                  file refuses it, as the merged gate runs --full (once_escalate)."""
        gate = self.cfg.section('gate')
        smoke = list(gate.get('smoke_steps') or [])
        shared = self.cfg.section('landing').get('shared_paths')
        once = self.gate_once()
        if not once and not smoke and not S.docs_steps(gate) and shared is None:
            return ('the base %s sets no "gate".smoke_steps or "gate".docs_only, so only a --full '
                    'record lands' % self.expect_main[:10])
        mb, why = refs.merge_base(tip, self.expect_main, self.root)
        if not mb:
            return 'git cannot find the merge base of the tip and %s: %s' % (
                self.expect_main[:10], why or 'none')
        changed, why = runner.changed_between(self.root, mb, tip)
        if changed is None:
            return 'git cannot list what the tip changes: %s' % why
        if not refs.is_sha(rec.get('base') or ''):
            return 'the record names no base, so what its steps were picked for is unknown'
        gated, why = runner.changed_between(self.root, rec['base'], tip)
        if gated is None:
            return 'git cannot list what the record\'s gate saw: %s' % why
        seen = set(reuse.changed_list(gated))
        unseen = [p for p in reuse.changed_list(changed) if p not in seen]
        if unseen:
            return ('its gate ran against %s, whose diff to the tip leaves out %s: gate the tip '
                    'again against %s' % (rec['base'][:10], ', '.join(unseen[:10]),
                                          self.expect_main[:10]))
        passed = {s.get('name') for s in rec.get('steps') or []
                  if isinstance(s, dict) and s.get('result') in ('pass', 'warn', 'reused')}
        if once:                        # before the docs-only route: a docs record too
            why = self.strict_refusal(changed, '"landing".gate_once')
            if why:
                return why
        elif shared is not None:
            why = self.shared_refusal(shared, changed)
            if why:
                return why
        if reuse.docs_only_change(self.cfg, gate, changed):
            missing = [n for n in S.docs_steps(gate) if n not in passed]
            if not missing:
                return None
            return 'the change is docs-only, and the base\'s docs_only step %s did not pass in it' % (
                ', '.join(missing))
        if mode != 'select':
            return 'its gate ran only the docs-only steps, and the change is not docs-only under the base'
        if not once and not smoke and shared is None:
            return ('the change is not docs-only, and the base %s sets no "gate".smoke_steps'
                    % self.expect_main[:10])
        hit = reuse.config_hits(self.cfg, changed)
        if hit:
            return 'the tip changes the gate\'s config (%s), which only a --full record lands' % (
                ', '.join(hit[:5]))
        missing = [] if once else [n for n in smoke if n not in passed]
        if missing:
            return 'the base\'s smoke step %s did not pass in it' % ', '.join(missing)
        return None

    def gate_once(self):
        """True when the base's config sets "landing".gate_once true (docs/gate.md "Gate once at
        landing"): select_refusal takes a default-mode record of any tip that changes no strict
        path and no gate config file, and once_escalate makes the merged gate --full."""
        return self.cfg.section('landing').get('gate_once') is True

    def shared_refusal(self, shared, changed):
        """Why a record that is not --full does not land a landing diff `changed` under the base's
        "landing".shared_paths `shared`, or None: a changed path matching one of `shared`, or one
        of the strict paths (strict_globs; a strict branch always ends on --full), refuses, and so
        does a strict list that cannot be read. Globs match as step globs (steps._match)."""
        paths = reuse.changed_list(changed)
        hits = [p for p in paths if S._match(p, shared)]
        if hits:
            return ('the tip changes shared path %s ("landing".shared_paths), which only a --full '
                    'record lands' % ', '.join(hits[:5]))
        return self.strict_refusal(changed, '"landing".shared_paths')

    def strict_refusal(self, changed, setting):
        """Why a record that is not --full does not land a landing diff `changed` under the base
        setting `setting` that took it ("landing".shared_paths or "landing".gate_once), or None: a
        changed strict path (strict_globs; a strict branch always ends on --full) refuses, and so
        does a strict list that cannot be read."""
        paths = reuse.changed_list(changed)
        globs, why = self.strict_globs()
        if globs is None:
            return 'cannot read the strict paths from %s at %s: %s' % (
                self.cfg.main_branch, self.expect_main[:10], why)
        hits = [p for p in paths if S._match(p, globs)]
        if hits:
            return ('the tip changes strict path %s, which only a --full record lands under '
                    '%s' % (', '.join(hits[:5]), setting))
        return None

    def no_full_step(self):
        """Why no tip can be gated whole, or None: the base's `.kit.json` has a "gate" section
        (detect.configured) from which --full selects no step (steps.select with full and no
        changed path): every step is `manual`, there is none, or each is dropped by another
        selected step's skip_if_selected. `gate.py --full` and the merged gate select the same
        steps, so both would refuse and the tip check's command could never be met."""
        if not detect.configured(self.cfg):
            return None
        _, picked = S.select(self.cfg.section('gate'), [], [], full=True)
        if picked:
            return None
        return ('the "gate" section of .kit.json at %s leaves no step for --full to select (every '
                'step is manual, or is dropped by another selected step\'s skip_if_selected), so '
                'neither `gate.py --full` nor the merged gate would run anything (both refuse: '
                'no gate step selected); no branch can land until %s gives the gate a step that '
                'is not manual and that no other step\'s skip_if_selected names'
                % (self.expect_main[:10], self.cfg.main_branch))

    def full_gate_advice(self, branch, tip):
        """What gates `branch`'s tip whole: `gate that tip whole first: <full_gate_command>`. When
        the kit lands itself (oncommit.is_kit) the command runs the branch's own gate.py, and one
        from before records said `full` never writes it: a tip whose record lacks the key (the
        record gate_ok.verify takes, without `full`) is told to merge <remote>/<main> in first,
        then gate and land the merge. Any other repository's command runs the running kit's
        gate.py, which writes it whatever the branch holds."""
        command = self.full_gate_command(branch)
        if oncommit.is_kit(self.root):
            rec, _ = gate_ok.verify(self.root, tip)
            if rec is not None and 'full' not in rec:
                return ('merge %s/%s (%s) into %s first (a branch\'s own gate.py from before '
                        'records said `full` never writes it), then gate the merge whole and land '
                        'that tip: %s' % (self.cfg.remote, self.cfg.main_branch,
                                          self.expect_main[:10], branch, command))
        return 'gate that tip whole first: %s' % command

    def full_gate_command(self, branch):
        """The command that gates `branch`'s worktree --full: `python "<gate.py>" -C "<worktree>"
        --full`. The gate.py is the worktree's own when the repository being landed is the kit
        (its main checkout has the kit's layout, oncommit.is_kit: the branch is gated by its own
        gate code; docs/gate.md "The caller's checkout"), whether the running kit is a checkout or
        a plugin export; any other repository's is the running kit's. `<worktree>` stands in when
        no worktree has the branch out."""
        wts, _ = repo.worktrees(self.root)
        wt = next((w.path for w in wts or [] if w.branch == branch), None) or '<worktree>'
        gate_py = os.path.join(wt if oncommit.is_kit(self.root) else S.kit_live(), 'tools', 'gate.py')
        return 'python "%s" -C "%s" --full' % (gate_py, wt)

    # ---------------------------------------------------------------- strict paths

    def strict_globs(self):
        """`(globs, None)`: STRICT_DEFAULTS as STRICT_FILE holds it at the base (parse_strict;
        never imported, never read from a branch), or the running kit's list when the base has no
        such file (a repository that is not the kit); `(None, why)` when it cannot be read."""
        base = self.expect_main
        r = self.git('ls-tree', '--name-only', base, '--', STRICT_FILE)
        if r.rc != 0:
            return None, 'git ls-tree %s: %s' % (base[:10], r.err or 'exit %s' % r.rc)
        if not cmd.text(r.out or b'').strip():
            return tuple(strict_defaults.STRICT_DEFAULTS), None
        r = self.git('show', '%s:%s' % (base, STRICT_FILE))
        if r.rc != 0:
            return None, 'git show %s:%s: %s' % (base[:10], STRICT_FILE, r.err or 'exit %s' % r.rc)
        try:
            return parse_strict(cmd.text(r.out or b'')), None
        except ValueError as e:
            return None, '%s: %s' % (STRICT_FILE, e)

    def strict_paths(self):
        """`(hits, None)`: branch -> the strict paths it changes (merge base with the base to its
        tip, matched against strict_globs()), for each branch with any; or `(None, why)` when the
        list or a branch's paths cannot be read. Reads only: strict_check and check_strict (the
        dry check) both decide by it."""
        globs, why = self.strict_globs()
        if globs is None:
            return None, 'cannot read the strict paths from %s at %s: %s' % (
                self.cfg.main_branch, self.expect_main[:10], why)
        hits = {}
        for b, t in self.pairs:
            mb, why = refs.merge_base(self.expect_main, t, self.root)
            changed, why2 = runner.changed_between(self.root, mb, t) if mb else (None, why)
            if changed is None:
                return None, 'git cannot list the paths of %s to check strict paths: %s' % (
                    b, why2 or why or 'no common commit with %s' % self.expect_main[:10])
            found = [p for p in changed if S._match(p, globs)]
            if found:
                hits[b] = found
        return hits, None

    def strict_check(self):
        """A locked check (docs/gate.md "Strict paths"): the paths each branch changes (merge base
        with the base to its tip) against strict_globs(). A branch with a hit lands only alone,
        with a verdict queued for its tip (kitlib.gate.verdicts) whose reviewer ran on a
        strong-tier model (strong_alias), whoever built it, and is not its author (is_author); then
        self.strict_hits and self.strict_review are set, and _locked escalates the merged gate to
        --full --fresh. None, or why not (REFUSED, nothing merged). Every read that fails
        refuses."""
        self.strict_hits, self.strict_review, self.strict_carry = {}, None, None
        hits, why = self.strict_paths()
        if why:
            return why
        self.strict_hits = hits
        if not self.strict_hits:
            return None
        if len(self.pairs) > 1:
            b = next(b for b in self.branches if b in self.strict_hits)
            return ('%s: a branch that changes a strict path lands alone, never in a batch'
                    % self.strict_says(b))
        (b, t), = self.pairs
        why = self.review_refusal(b, t, strict=True)
        if why:
            return '%s: %s' % (self.strict_says(b), why)
        return None

    def verdict_check(self):
        """A locked check (docs/gate.md "Landing" step 4), after strict_check: every branch that
        changes no strict path lands too only with a verdict queued for its tip by a reviewer who
        is not its author (review_refusal, without the tier rule), each branch of a batch on its
        own entry. None, or why not for the first branch, in batch order, that misses (REFUSED,
        nothing merged)."""
        for b, t in self.pairs:
            if b in self.strict_hits:       # strict_check passed it, tier rule and all
                continue
            why = self.review_refusal(b, t, strict=False)
            if why:
                return '%s: %s' % (b, why)
        return None

    def strict_says(self, b):
        hits = self.strict_hits[b]
        more = len(hits) - STRICT_SHOWN
        return '%s changes strict path %s%s' % (
            b, ', '.join(hits[:STRICT_SHOWN]), ' and %d more' % more if more > 0 else '')

    def review_refusal(self, b, t, strict):
        """None when the verdict queued for branch `b` lets its tip `t` land; else why not. Every
        landing: an entry that can be used, queued (not marked failed), at `t`, its reviewer not
        the author (is_author). A carried entry (verdicts.carry) lands only when
        kitlib.gate.carry.check passes again, from the commit reviewed to `t` against
        self.expect_main. `strict` (a branch that changes a strict path) adds the tier rule
        (strong_alias: a strong-tier reviewer, whoever built the branch) and sets self.strict_review to the entry and self.strict_carry
        to the carry's facts, which strict_escalate reports."""
        common, why = repo.common_dir(self.root)
        if not common:
            return 'its verdict queue cannot be found: %s' % (why or 'no git common dir')
        try:
            entry = verdicts._load(verdicts.entry_path(common, b), b)
        except verdicts.VerdictError as e:
            return 'its verdict entry cannot be used: %s' % e
        if entry is None:
            return ('it lands only with a queued verdict (tools/verdict.py add), and none is '
                    'queued for it')
        if entry['state'] != verdicts.QUEUED:
            return ('its verdict entry is marked %s (%s): queue it again with tools/verdict.py '
                    'add' % (entry['state'], entry['last_result'] or 'no reason given'))
        if entry['tip'].lower() != t.lower():
            return ('the verdict was given at %s, and the branch is at %s: review and queue the '
                    'tip, or carry the verdict (tools/verdict.py carry) when a main sync is all '
                    'that changed' % (entry['tip'][:10], t[:10]))
        facts = None
        if verdicts.CARRIED in entry:
            # Never the stored facts: the carry is checked again here, against the base this
            # landing merges onto (docs/gate.md "Carrying a verdict forward").
            reviewed = entry[verdicts.CARRIED]['from']
            facts, why = carry.check(self.root, reviewed, t, self.expect_main,
                                     git=lambda args: self.git(*args))
            if facts is None:
                return ('the verdict was given at %s and carried to %s, but the carry does not '
                        'hold against %s %s: %s; review and queue the tip'
                        % (reviewed[:10], t[:10], self.cfg.main_branch, self.expect_main[:10], why))
        if strict:
            strong, weak, why = tier_models()
            if strong is None:
                return 'the strong-tier models cannot be read: %s' % why
            model = entry['reviewer_model']
            if strong_alias(model, strong, weak) is None:
                return ('the reviewer ran on %s, not a strong-tier model (%s, or a full model id '
                        'naming it; hosts.json "tiers"): a branch that changes a strict path '
                        'lands only on a strong-tier review, whoever built it (its author is %s)'
                        % (model, ', '.join(sorted(strong - weak)) or 'none', entry['author']))
        if is_author(entry):
            return ('the reviewer %s (%s) is its author (%s): it lands with a review by an agent '
                    'that did not write it' % (entry['reviewer_id'], entry['reviewer'],
                                              entry['author']))
        if strict:
            self.strict_review, self.strict_carry = entry, facts
        return None

    def once_escalate(self):
        """After the merge, before the merged gate: when the base sets "landing".gate_once
        (docs/gate.md "Gate once at landing") and the merged gate is not --full already, it runs
        --full (each step's full_cmd) on the merged or batched tree, said once; a merge whose diff
        to the base is docs-only (reuse.docs_only_change) stays on the docs-only route. A diff git
        cannot list escalates too."""
        if self.full or not self.gate_once():
            return
        changed, _ = runner.changed_between(self.root, self.expect_main, self.merge)
        if changed is not None and reuse.docs_only_change(self.cfg, self.cfg.section('gate'),
                                                           changed):
            return
        self.full = True
        self.say('GATE ONCE: the base %s sets "landing".gate_once: the merged gate runs --full on '
                 'the merged tree' % self.expect_main[:10])

    def strict_escalate(self):
        """A strict branch passed strict_check: its merged gate runs --full --fresh, whatever the
        caller passed (it only ever tightens), and one line says so."""
        (b, _), = self.pairs
        e, c = self.strict_review, self.strict_carry
        self.full = self.fresh = True
        carried = ''
        if c:
            n = len(c['merges'])
            carried = ' at %s, carried over %d main merge%s' % (c['from'][:10], n,
                                                                 '' if n == 1 else 's')
        self.say('STRICT: %s: reviewed by %s on %s (%s)%s; the merged gate runs --full --fresh' % (
            self.strict_says(b), e['reviewer'], e['reviewer_model'], e['reviewer_id'], carried))

    # ---------------------------------------------------------------- the scope check

    def scope_check(self):
        """The last locked check: every path each branch changes (merge base to tip, renames as a
        delete and an add) falls in the scope of a record the branch holds - pulled by it, or
        named by a `Closes:` trailer of its own commits while pulled by anyone - or in
        SCOPE_ALWAYS or a folder store's folder. A branch's own commits are `tip --not` the base
        and the batch's earlier tips, so a stacked branch is checked on its own paths. A miss, a
        store it cannot read while some path is outside what it could read, or a check that
        stops: with SCOPE_REFUSES the refusal; else a WARNING kept for after the LANDED line, and
        None. A branch holding no record while a store with records was read: a NOTE kept for
        before the LANDED line."""
        for i, (b, t) in enumerate(self.pairs):
            try:
                why = self._scope_of(b, t, [self.expect_main] + self.tips[:i])
            except Exception as e:      # noqa: BLE001 - a check that cannot finish is a miss
                why = 'the scope check of %s stopped (%s)' % (b, _what(e))
            if not why:
                continue
            if SCOPE_REFUSES:
                return why
            self.scope_warnings.append(
                'WARNING: %s; the landing went ahead: the scope check only warns for now '
                '(kitlib.gate.land SCOPE_REFUSES)' % why)
        return None

    def _scope_base(self, t, bases):
        """`(the merge base of tip t with every base, None)`, or `(None, why)`."""
        if len(bases) == 1:
            mb, why = refs.merge_base(t, bases[0], self.root)
            return (mb or None), (why or 'no common commit with %s' % bases[0][:10])
        r = cmd.call(['merge-base', t] + list(bases), self.root)
        out = cmd.text(r.out).strip() if r.out else ''
        if r.rc == 0 and refs.is_sha(out):
            return out, None
        return None, r.err or 'git merge-base exited %s' % r.rc

    def _scope_of(self, b, t, bases):
        """None when branch `b` at `t` keeps to its scopes; else why not (scope_check)."""
        mb, why = self._scope_base(t, bases)
        changed, why2 = runner.changed_between(self.root, mb, t) if mb else (None, why)
        if changed is None:
            return ('git cannot list the paths of %s to check them against the scopes of the '
                    'records it holds: %s' % (b, why2 or why))
        rest = [p for p in changed if not _covers(SCOPE_ALWAYS, p)]
        if not rest:
            return None
        out, why = cmd.run_text(['log', '--format=%B%x00', t, '--not'] + list(bases) + ['--'],
                                self.root, strip=False)
        if out is None:
            return ('git cannot read the Closes trailers of %s to check its paths against the '
                    'scopes of the records it holds: %s' % (b, why))
        values = []
        for message in out.split('\0'):
            values.extend(at_landing.closes_values(message))
        named = set(at_landing.closes_ids(values))
        found = self._scope_stores(b, t, named, changed)
        rest = [p for p in rest if not _covers(found.folders, p)]
        if not rest:
            return None
        if not found.held:
            if found.unread:
                return self._scope_unread(b, found)
            if found.read:
                self.scope_notes.append('NOTE: %s holds no pulled record; its paths were not '
                                        'checked against a scope' % b)
            return None
        members = found.members()
        outside = []
        for p in rest:
            if not _covers(members, p):
                err = _path_error(p)
                outside.append((p, 'undecidable: %s' % err if err else
                                'no scope member can name it' if _unnamed(p) else None))
        if not outside:
            return None
        if found.unread:            # a record it could not read may cover them
            return self._scope_unread(b, found)
        return self._scope_miss(b, t, mb, found, outside)

    def _scope_unread(self, b, found):
        return ('cannot read the backlog store to check %s\'s paths against the scopes of the '
                'records it holds: %s' % (b, '; '.join(found.unread)))

    def _scope_miss(self, b, t, mb, found, outside):
        ids = sorted(found.held)
        shown = ['%s (%s)' % (p, err) if err else p for p, err in outside[:SCOPE_SHOWN]]
        more = len(outside) - len(shown)
        listed = ', '.join(shown) + (', and %d more (git diff --name-only --no-renames %s %s)'
                                     % (more, mb, t) if more else '')
        members, command = found.held[ids[0]]
        new = [p for p, err in outside if not err]
        if not new:
            how = 'land them on their own branch'
        else:
            if len(outside) > SCOPE_SHOWN:
                new = ['<each path, or a folder/** that covers them>']
            args = ['set', ids[0]] + ['--scope "%s"' % m
                                      for m in members + new if m != scmod.NONE]
            how = ('widen a record\'s scope first (set checks the new members against every live '
                   'pull\'s scope): %s; or land them on their own branch' % command(*args))
        return '%s changes %d path(s) outside the scopes of the records it holds (%s): %s; %s' % (
            b, len(outside), ', '.join(ids), listed, how)

    def _scope_stores(self, b, t, named, changed):
        """_Held: the records `b` holds in the configured store and the PM data branch's store,
        with `named` the ids its `Closes:` trailers name. A folder store is read at the tip `t`
        (its records travel with the branch), counting a record file the branch deleted
        (`changed`) at its last version; a `ref` or `branch` store as the remote holds it."""
        found = _Held()
        root, cfg = self.root, self.cfg
        try:
            backend = smod.open_backend(root, cfg, fetch=False)
        except (Refusal, OSError) as e:
            found.unread.append('the configured store: %s' % _what(e))
            backend = None

        def close_cmd(*args):
            return at_landing.close_command(root, *args)

        if isinstance(backend, smod.MainBackend):
            rel = backend.rel.replace('\\', '/').strip('/')
            found.folders.append(rel + '/**')
            records, _raw, why = smod.records_in_tree(root, t, rel)
            if records is None:
                found.unread.append('%s at %s: %s' % (rel, t[:10], why))
            else:
                gone = [p[len(rel) + 1:-3] for p in changed
                        if p.startswith(rel + '/') and p.endswith('.md')
                        and '/' not in p[len(rel) + 1:]]
                self._scope_take(found, '%s at %s' % (rel, t[:10]), records, b, named, gone,
                                 lambda rid: self._scope_last(root, t, rel, rid), close_cmd)
        elif backend is not None:
            got = self.scope_reads.get('configured')
            if got is None:
                got = self.scope_reads['configured'] = self._scope_ref_records(root, backend)
            records, tip, why = got
            if records is None:
                found.unread.append(why)
            elif tip:
                self._scope_take(found, backend.describe(), records, b, named, (),
                                 lambda rid: self._scope_last(root, tip, smod.STORE_DIR, rid),
                                 close_cmd)
        configured = backend.describe() if isinstance(backend, smod.RefBackend) else None
        got = self.scope_reads.get('pm')
        if got is None:
            got = self.scope_reads['pm'] = self._scope_pm_records(configured)
        pm_root, where, records, last, why = got
        if why:
            found.unread.append('the PM data branch\'s store: %s' % why)
        elif records is not None:
            self._scope_take(found, where, records, b, named, (), last,
                             lambda *args: at_landing.backlog_command(pm_root, *args))
        return found

    def _scope_take(self, found, where, records, b, named, gone, last, command):
        """Add to `found` the records of one store `b` holds: pulled by it, or pulled and in
        `named`; an id in `named` or `gone` the store no longer holds counts at its last version
        (`last(rid)`: the record before the commit that deleted it, or None). A store holding an
        unreadable record is unread: that record may be the one that covers a path."""
        bad = sorted(r.stem for r in records if not r.readable)
        if bad:
            found.unread.append('%s: %d record(s) unreadable (%s)' % (
                where, len(bad), ', '.join(bad[:5])))
            return
        if records:
            found.read = True
        stems = set(r.stem for r in records)
        candidates = list(records)
        for rid in sorted((set(named) | set(gone)) - stems):
            rec = last(rid) if smod.PLAIN_ID.match(rid) else None
            if rec is not None and rec.readable:
                candidates.append(rec)
        for r in candidates:
            if r.get('state') == 'pulled' and (r.get('pulled_by') == b or r.stem in named):
                found.hold(r.stem, r.all('scope'), command)

    def _scope_last(self, root, rev, rel, rid):
        """The record `rid` as it was before the newest commit reachable from `rev` deleted
        `<rel>/<rid>.md`, or None."""
        gone = smod.last_removal(root, rev, rel, rid)
        if gone is None:
            return None
        return self._scope_blob(root, '%s^:%s/%s.md' % (gone.sha, rel, rid), rid)

    @staticmethod
    def _scope_blob(root, spec, rid):
        data, _ = cmd.run(['cat-file', '-p', spec], root)
        if data is None:
            return None
        return smod.parse_blob(rid, data, spec.partition(':')[2])

    def _scope_ref_records(self, root, backend):
        """`(records, tip, None)` of a `ref` or `branch` store as its remote holds it (the local
        ref without a remote), `([], '', None)` when there is none, or `(None, None, why)`. Moves
        no ref: a missing store commit is fetched with an empty --refmap, as ancestry_refusal
        does."""
        if backend.remote:
            tip, why = refs.ls_remote(backend.remote, backend.remote_ref, root,
                                      timeout=FETCH_TIMEOUT)
            if tip is None:
                return None, None, '%s: %s' % (backend.describe(), why)
            if tip and cmd.call(['cat-file', '-e', '%s^{commit}' % tip], root).rc != 0:
                cmd.call(['fetch', '--quiet', '--no-tags', '--no-write-fetch-head', '--refmap=',
                          backend.remote, backend.remote_ref], root, timeout=FETCH_TIMEOUT,
                         env=self.git_env)
                if cmd.call(['cat-file', '-e', '%s^{commit}' % tip], root).rc != 0:
                    return None, None, '%s: cannot fetch %s' % (backend.describe(), tip[:10])
        else:
            tip, why = refs.ref_commit(backend.local_ref, root)
            if tip is None:
                return None, None, '%s: %s' % (backend.describe(), why)
        if not tip:
            return [], '', None
        records, _raw, why = smod.records_in_tree(root, tip, smod.STORE_DIR)
        if records is None:
            return None, None, '%s at %s: %s' % (backend.describe(), tip[:10], why)
        return records, tip, None

    def _scope_pm_records(self, configured):
        """`(pm root, where, records, last(rid), None)` of the PM data branch's store, `(None,
        None, None, None, None)` when there is none, or `(..., why)`. A folder store there is read
        in its worktree, as a landing's close reads it; a record deleted there and not committed
        is read from HEAD."""
        pm_root, _cfg, backend, why = at_landing.pm_data_store(self.root, configured)
        if why:
            return None, None, None, None, why
        if backend is None:
            return None, None, None, None, None
        if isinstance(backend, smod.RefBackend):
            records, tip, why = self._scope_ref_records(pm_root, backend)
            if records is None:
                return None, None, None, None, why
            return (pm_root, backend.describe(), records,
                    lambda rid: self._scope_last(pm_root, tip, smod.STORE_DIR, rid) if tip
                    else None, None)
        try:
            snap = backend.load(rmod.Rules())
        except (Refusal, OSError) as e:
            return None, None, None, None, '%s: %s' % (backend.describe(), _what(e))
        rel = backend.rel.replace('\\', '/').strip('/')

        def last(rid):
            if backend.uncommitted_removal(rid):
                return self._scope_blob(pm_root, 'HEAD:%s/%s.md' % (rel, rid), rid)
            return self._scope_last(pm_root, 'HEAD', rel, rid)
        return pm_root, backend.describe(), snap.records, last, None

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
        entries, why = gitstatus.status(self.root)
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
        `base`. None when it names the same main branch and remote and still takes this landing
        (config_refusal); then self.expect_main and self.cfg are the base's, so the locked checks,
        the landing tree, the merged gate and the records all use it. Else why not (REFUSED,
        nothing merged; self.cfg and self.expect_main unchanged)."""
        was, main_b, remote = self.expect_main, self.cfg.main_branch, self.cfg.remote
        try:
            cfg = kconfig.load_at(self.root, base)
            same = (cfg.main_branch, cfg.remote) == (main_b, remote)
        except ConfigError as e:
            return 'main moved on from %s to %s, and the config there: %s' % (
                was[:10], base[:10], e)
        if not same:
            return ('main moved on from %s to %s, and the config there names another main '
                    'branch or remote than this landing holds: land again (the next landing '
                    'reads the config at %s)' % (was[:10], base[:10], base[:10]))
        old = self.cfg
        self.cfg, self.expect_main = cfg, base
        why = self.config_refusal()
        if why:
            self.cfg, self.expect_main = old, was
            return 'main moved on from %s to %s, and by its config: %s' % (
                was[:10], base[:10], why)
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

    def tree_path(self):
        """Where the landing tree is: Lander(tree=), else <main checkout>/<worktrees_dir>/_landing
        (TREE_NAME), the folder agent worktrees go in (kitlib.git.prep keeps it out of git
        status)."""
        if self.tree_given:
            return os.path.normpath(os.path.abspath(self.tree_given))
        return os.path.normpath(os.path.join(self.root, self.cfg.repo['worktrees_dir'], TREE_NAME))

    def tree_head(self):
        sha, _ = refs.resolve('HEAD', self.tree)
        return sha

    def prepare_tree(self):
        """Step 5b: the landing tree (self.tree) detached at the base, nothing else in it. Made
        the first time (`git worktree add --detach --lock`); reused when it is clean (a checkout of
        the base; its ignored files, such as build caches, stay); recreated, with a line saying
        why, when it is gone, dirty, mid-merge, not a work tree of its own, or cannot be put at
        the base. None, or why not (REFUSED, nothing merged): the main checkout inside it, a
        folder there that is not the landing tree (left as it is: `move it aside`), or git
        failing."""
        tree = self.tree = self.tree_path()
        if repo.inside(self.root, tree):
            return ('the landing tree %s is the main checkout or holds it; it must be a folder of '
                    'its own' % tree)
        wts, why = repo.worktrees(self.root)
        if wts is None:
            return 'git worktree list failed: %s' % why
        entry = next((w for w in wts[1:] if repo.same_path(w.path, tree)), None)
        if entry is None:
            if os.path.isdir(tree) and os.listdir(tree):
                return ("the landing tree's place %s is a folder git does not list as a worktree; "
                        "it was left as it is: move it aside" % tree)
            return self.add_tree(None)
        if not entry.detached or entry.locked != TREE_LOCK:
            return ("the landing tree's place %s is a worktree of this repository (%s), not the "
                    "landing tree; it was left as it is: move it aside" % (
                        tree, 'on %s' % entry.branch if entry.branch else
                        'locked: %s' % entry.locked if entry.locked else 'not locked'))
        why = self.tree_problem(entry)
        if why:
            return self.add_tree(why)
        r = self.git('checkout', '--quiet', '--detach', self.expect_main, cwd=tree,
                     timeout=PUSH_TIMEOUT)
        entries, _ = gitstatus.status(tree)
        if r.rc == 0 and self.tree_head() == self.expect_main and entries == []:
            return None
        return self.add_tree('could not be put at the base %s (%s)' % (
            self.expect_main[:10], (r.err or 'exit %s' % r.rc) if r.rc else 'not clean there'))

    def tree_problem(self, entry):
        """Why the listed landing tree `entry` cannot be reused as it is, or None."""
        tree = self.tree
        if entry.prunable is not None or not os.path.isdir(tree):
            return 'is gone'
        top, why = cmd.run_text(['rev-parse', '--show-toplevel'], tree)
        if not top or not repo.same_path(top, tree):
            return 'is not a work tree of its own (git top: %s)' % (top or why or 'none')
        merging, why = refs.merge_in_progress(tree)
        if merging is not False:
            return 'has a merge in progress' if merging else 'cannot be read (%s)' % why
        entries, why = gitstatus.status(tree)
        if entries is None:
            return 'cannot be read: git status failed (%s)' % why
        if entries:
            return 'is not clean (%s)' % _paths(entries)
        return None

    def add_tree(self, why):
        """Make the landing tree at the base: `why` None, the first time; else remove what is
        there first (it is the kit's own tree: unlocked, removed with --force --force, a gone one
        pruned) and say `the landing tree <path> <why>: recreated`. None, or why not."""
        tree = self.tree
        if why is not None:
            self.git('worktree', 'unlock', tree)     # not locked any more: git says so, no matter
            if os.path.isdir(tree):
                self.git('worktree', 'remove', '--force', '--force', tree, timeout=PUSH_TIMEOUT)
            else:
                self.git('worktree', 'prune')        # the gone tree's registration
            if os.path.isdir(tree):
                try:
                    _rmtree(tree)
                except OSError as e:
                    return ('the landing tree %s %s, and it could not be removed (%s); remove the '
                            'folder by hand, then land again' % (tree, why, e))
                self.git('worktree', 'prune')
        else:
            os.makedirs(os.path.dirname(tree), exist_ok=True)
        r = self.git('worktree', 'add', '--detach', '--lock', '--reason', TREE_LOCK, tree,
                     self.expect_main, timeout=PUSH_TIMEOUT)
        if r.rc != 0:
            return 'git worktree add %s failed: %s' % (tree, r.err or 'exit %s' % r.rc)
        entries, err = gitstatus.status(tree)
        if self.tree_head() != self.expect_main or entries != []:
            return 'the landing tree %s, just added, is not clean at the base %s: %s' % (
                tree, self.expect_main[:10], _paths(entries) if entries else err or 'HEAD differs')
        self.say('the landing tree %s %s: recreated' % (tree, why) if why else
                 'the landing tree %s: made' % tree)
        return None

    def do_merge(self):
        """Merge each branch in turn with --no-ff in the landing tree (self.merges): None when
        all merged, else the NOT LANDED Outcome: a branch that does not merge cleanly (that merge
        aborted, the earlier ones dropped; the CONFLICT verdict line), or a merge git reported
        that is not the merge asked for. Main is never moved."""
        self.lock.set_phase(self.token, 'merge')
        self.phase = 'merge'
        main_b, base = self.cfg.main_branch, self.expect_main
        for b, t in self.pairs:
            prev = self.merges[-1] if self.merges else base
            title = 'Merge %s%s' % (b, ': %s' % self.summary if self.summary else '')
            r = self.git('merge', '--no-ff', '--no-edit', '-m', title, t, timeout=MERGE_TIMEOUT,
                         cwd=self.tree)
            if r.rc == 0:
                head = self.tree_head()
                first, _ = cmd.run_text(['rev-parse', 'HEAD^1'], self.tree)
                second, _ = cmd.run_text(['rev-parse', 'HEAD^2'], self.tree)
                if not head or first != prev or second != t:
                    return G.Outcome(G.FAILED, [
                        'NOT LANDED: the merge of %s reported success, and HEAD of the landing tree '
                        '(%s) is not a merge of %s onto %s; nothing was pushed, and the landing '
                        'did not move %s from the base %s' % (
                            b, (head or '?')[:10], t[:10], prev[:10], main_b, base[:10])],
                        undone=True)
                self.merges.append(head)
                self.merge = head
                continue
            conflict = r.err or cmd.text(r.out).strip() or 'exit %s' % r.rc
            lines = []
            merging, _ = refs.merge_in_progress(self.tree)
            if merging:
                a = self.git('merge', '--abort', timeout=MERGE_TIMEOUT, cwd=self.tree)
                if a.rc != 0:
                    lines.append('WARNING: `git merge --abort` failed in the landing tree %s (%s); '
                                 'the next landing recreates it' % (
                                     self.tree, a.err or 'exit %s' % a.rc))
            why = '%s does not merge cleanly into %s: %s; the merge was aborted' % (
                b, main_b, conflict)
            if self.merges:
                why += (', and the merge of %s before it was undone (the landing did not move %s '
                        'from the base %s)') % (
                    ', '.join(self.branches[:len(self.merges)]), main_b, base[:10])
            return G.Outcome(G.FAILED, [why] + lines + [
                'CONFLICT %s with main %s: the author merges main' % (b, base)],
                undone=bool(self.merges), conflict=b)
        return None

    def tracked_and_untracked(self, where=None):
        """`(tracked entries, untracked paths)` of the status of `where` (default the main
        checkout); None, None when git cannot say."""
        entries, _ = gitstatus.status(where or self.root)
        if entries is None:
            return None, None
        return ([(xy, p) for xy, p in entries if xy != '??'],
                [p for xy, p in entries if xy == '??'])

    def unchanged(self):
        """None when the landing tree is exactly the merge the gate passed (HEAD at the merge, no
        tracked change, no untracked file) and the repository's stash stack is still empty
        (kitlib.gate.stash; the locked checks found it empty), else why not."""
        problems = []
        head = self.tree_head()
        if head != self.merge:
            problems.append('HEAD moved in the landing tree while the merged gate ran (HEAD %s; '
                            'the gated merge is %s)' % ((head or '?')[:10], self.merge[:10]))
        tracked, untracked = self.tracked_and_untracked(self.tree)
        if tracked is None:
            problems.append('git status failed in the landing tree')
        else:
            if tracked:
                problems.append('a step changed tracked files: %s' % _paths(tracked))
            if untracked:
                problems.append('a step left untracked files: %s' % ', '.join(untracked[:10]))
        stashed = stash.appeared(self.root)
        if stashed:
            problems.append(stashed)
        return '; '.join(problems) or None

    def gate(self):
        self.lock.set_phase(self.token, 'gate')
        self.phase = 'gate'
        refusal = self.lock.gate_refusal()
        if refusal:
            return G.Outcome(G.REFUSED, ['REFUSED: %s' % refusal])
        if self.merged_gate_fn is not None:
            return self.merged_gate_fn(self)
        if self.on_node():
            out = self.node_gate()
            if out is not None:
                return out
        out = self.rebook_kind()
        if out is not None:
            return out
        return self.main_gate()

    def rebook_kind(self):
        """Step 7 on main, just before main's merged gate: None when the slot held is of the
        base config's "gate".kind. Main moved on before the turn to a base whose config (read
        again by follow, step 4) names another kind than book_turn booked (the config read at
        preconditions): it says so, releases the slot first, then books the base's kind at its
        own size with no wait limit, as rebook_here does and for the same reason (booked while
        the old run is held, a kind of every slot, an owner whole hold or a gpu pool would wait
        on itself). The landing lock file stays held, so no other landing merges meanwhile.
        None once booked; the machine lock not giving it: a FAILED Outcome (NOT LANDED, the
        merge undone). After a node-routed fallback rebook_here booked the base's kind already:
        nothing to do."""
        kind, booked = self.cfg.section('gate')['kind'], self.booked_kind
        if booked is None or booked == kind:
            return None
        self.say('the base %s books "gate".kind %s, not %s as this landing did: releasing the '
                 "landing's %s run and booking a %s run for the merged gate, waiting with no "
                 'limit' % (self.expect_main[:10], kind, booked, booked, kind))
        self.drop_slot(0, 'done')
        try:
            self.book_slot(None)
        except LockError as e:
            line = ('the machine lock did not give the merged gate its %s run: %s'
                    % (kind, e.message))
            return G.Outcome(G.FAILED, [line], verdict=line, results=[])
        return None

    def route_need(self, lr, kind):
        """The slots the merged gate's kind `kind` needs on a node, for recheck: `(need, None)`,
        the plan's when the base's config names the kind booked (book_turn); else `kind` sized
        again (lr.need_of), or `(None, why)` when it cannot be, and the landing gates here."""
        if self.booked_kind in (None, kind):
            return self.route.need, None
        try:
            return lr.need_of(kind, self.queue_store()), None
        except Exception as e:      # noqa: BLE001 - gate here, at the base's kind
            return None, ('the base %s books "gate".kind %s, not %s, and %s cannot be sized '
                          'here (%s)' % (self.expect_main[:10], kind, self.booked_kind, kind,
                                         _what(e)))

    def node_gate(self):
        """Step 7 for a node-routed landing: the land job's Outcome (landroute.run_on_node), or
        None once it fell back to main (rebook_here booked main's full run): the tip's --full
        record has the merge's tree (same_tree_why), no node qualifies now (recheck, for the
        slots of the base's kind: route_need; another node that does takes it), or nothing ran
        on the node (Fallback). Only those fall back:
        any other exception goes on up (recover: NOT LANDED). The machine lock not giving the
        full run: a FAILED Outcome."""
        lr, kind = self.routes(), self.cfg.section('gate')['kind']
        repo_name = runner.repo_name(self.cfg, self.root)
        why = lr.same_tree_why(self.tree, self.cfg, self.branches[0], self.tips[0],
                               self.expect_main, self.merge, batch=len(self.branches),
                               fresh=self.fresh, full=self.full)
        node = need = None
        if not why:
            need, why = self.route_need(lr, kind)
        if not why:
            c = lr.recheck(self.route.nodes, repo_name, need)
            if c.node is None:
                why = c.why
            else:
                node = c.node
                if c.node.name != self.route.node.name:
                    self.say('gating on load node %s: %s' % (c.node.name, c.why))
        if node is not None:
            try:
                return lr.run_on_node(node, self.root, self.merge, self.expect_main, self.cfg,
                                      repo_name, runner.slug_of('+'.join(self.branches)),
                                      full=self.full, docs=True, tree=self.tree, kind=kind,
                                      say=self.say, store=self.queue_store())
            except lr.Fallback as e:
                why = '%s (nothing ran on load node %s)' % (e.why, node.name)
        try:
            self.rebook_here(why)
        except LockError as e:
            line = ("the machine lock did not give main's merged gate its full %s run: %s"
                    % (kind, e.message))
            return G.Outcome(G.FAILED, [line], verdict=line, results=[])
        return None

    def main_gate(self):
        """Step 7 on main: the merged gate in the landing's slot (runner.merged_gate)."""
        plan = reuse.planner(self.tree, self.cfg, self.branches[0], self.tips[0], self.expect_main,
                             self.merge, self.prior, fresh=self.fresh, full=self.full,
                             batch=len(self.branches),
                             record=self.tip_records.get(self.tips[0]))
        out = runner.merged_gate(self.root, self.cfg, self.expect_main, full=self.full,
                                 slug='+'.join(self.branches), log_dir=self.log_dir, say=self.say,
                                 book_kw=self.book_kw, reuse=plan,
                                 slot=self.slot, tree=self.tree, docs=True)
        self.gate_logs = out.facts.get('log_dir')
        return out

    def source(self):
        """The merged gate's source (kitlib.gate.detect), once it ran; else None."""
        return self.gate_out.facts.get('source') if self.gate_out is not None else None

    def write_record(self, started, results):
        """The gate-OK record for the merge commit (pre-push reads it), its config_rev the
        base, its mode "docs" when the merged gate ran only the docs-only steps, its ran_on the
        land job's when the merged gate ran on a load node; its path. Raises
        gate_ok.RecordError."""
        docs = self.gate_out is not None and self.gate_out.facts.get('docs') is True
        ran_on = self.gate_out.facts.get('ran_on') if self.gate_out is not None else None
        path, _ = gate_ok.record_pass(self.root, self.merge, runner.gate_config(self.cfg),
                                      runner.kit_identity(), runner.repo_name(self.cfg, self.root),
                                      started, runner.record_steps(results or []),
                                      base=self.expect_main, config_rev=self.expect_main,
                                      source=self.source(), full=bool(self.full) and not docs,
                                      mode='docs' if docs else None, ran_on=ran_on)
        return path

    def push(self, started, results):
        """Step 9: write the merge's gate-OK record, push the merge to <remote>/<main> (never
        forced) and verify it there with ls-remote. None once verified (phase pushed: landed);
        else why nothing landed (NOT LANDED: the record could not be written, the remote's main
        moved on while the gate ran, so the push was refused, or git refused the push, exit 1
        with git's own ref status line `! [rejected] ...` or `! [remote rejected] ...`
        (PUSH_REFUSED), with the remote's main still the base; or the push never connected,
        push_never_connected, with the remote's main anything but the merge or a commit holding
        it, unreadable included). _Stuck when the push may have
        landed and git cannot tell (ls-remote unreadable, or a remote main this checkout cannot
        place), or when any other push did not land on a remote main still at the base or absent:
        exit 0, no exit (timed out), exit 1 without that line (no message: a git ended from
        outside, refs.KILLED_SHAPE; a stray warning; a bare `failed to push some refs`) or exit
        128 (git died: a connection dropped after the upload may still be applied on the
        remote)."""
        main_b, remote = self.cfg.main_branch, self.cfg.remote
        try:
            path = self.write_record(started, results)
        except gate_ok.RecordError as e:
            return ('the gate-OK record could not be written: %s; nothing was pushed, and the merge '
                    'of %s was undone (the landing did not move %s from the base %s)' % (
                        e, self.branch, main_b, self.expect_main[:10]))
        self.say('gate-OK record: %s' % path)
        self.lock.set_phase(self.token, 'push')
        self.phase = 'push'
        r = self.git('push', '--quiet', remote, '%s:refs/heads/%s' % (self.merge, main_b),
                     timeout=PUSH_TIMEOUT)
        pushed, why = refs.ls_remote(remote, 'refs/heads/%s' % main_b, self.root)
        how = 'exited 0' if r.rc == 0 else 'failed (%s)' % (r.err or 'exit %s' % r.rc)
        lines = [l.strip() for l in (r.err or '').splitlines() if l.strip()]
        said = ' | '.join(lines)
        unconnected = push_never_connected(r.rc, r.err)

        def never_connected(state):
            # git died before connecting (PUSH_NEVER_CONNECTED): no byte reached the remote, so
            # the merge, made only here, is not on it whatever ls-remote read (in an outage it
            # fails the same way). A remote main that is or holds the merge still counts as landed
            return ('the push of the merge %s never connected to %s (%s), and %s/%s %s; nothing '
                    'landed, and the merge of %s was undone: land again once %s can be reached'
                    % (self.merge[:10], remote, said, remote, main_b, state, self.branch, remote))
        if pushed is None:
            if unconnected:
                return never_connected('is unreadable (%s)' % why)
            raise _Stuck('the push %s, and %s/%s is unreadable (%s), not known to be the merge %s'
                         % (how, remote, main_b, why, self.merge[:10]))
        if pushed != self.merge:
            on = self.on_remote(pushed)
            if on is None:                  # a remote main not fetched here: fetch, look again
                self.fetch()
                on = self.on_remote(pushed)
            if on is None:
                if unconnected:
                    return never_connected('is %s, which this checkout cannot place' % (
                        pushed[:10] or 'absent'))
                raise _Stuck('the push %s, and %s/%s is %s, which this checkout cannot place '
                             'against the merge %s' % (how, remote, main_b, pushed[:10] or 'absent',
                                                       self.merge[:10]))
            if not on:
                if unconnected:
                    return never_connected('is still the base %s' % self.expect_main[:10]
                                           if pushed == self.expect_main else
                                           'is %s' % (pushed[:10] or 'absent'))
                refused = any(l.startswith(PUSH_REFUSED) for l in lines)
                if r.rc == 1 and refused and pushed == self.expect_main:
                    # git ran to the end and printed the ref's refusal (PUSH_REFUSED): nothing was
                    # published. Exit 1 without it (no message: a git ended from outside,
                    # refs.KILLED_SHAPE; or only a stray warning) and exit 128 (git died) may have
                    # gone after the upload, which the remote may still apply after this
                    # ls-remote: STUCK
                    return ('the push of the merge %s failed (%s), and %s/%s is still the base %s; '
                            'nothing landed, and the merge of %s was undone: land again once the '
                            'push can go' % (self.merge[:10], said, remote, main_b,
                                             self.expect_main[:10], self.branch))
                if r.rc == 0 or not pushed or pushed == self.expect_main:
                    raise _Stuck('the push %s, and %s/%s is %s, not the merge %s%s' % (
                        how, remote, main_b, (pushed or 'absent')[:10], self.merge[:10],
                        '' if said or r.rc != 1 else '; %s' % refs.KILLED_SHAPE))
                return ('%s/%s moved on to %s while the merged gate ran, so the push of the merge '
                        '%s was refused (a landing never forces); nothing landed, and the merge '
                        'of %s was undone: land again' % (remote, main_b, pushed[:10],
                                                          self.merge[:10], self.branch))
        self.landed_at = pushed
        self.phase = 'pushed'       # verified on the remote: landed, whatever stops it from here
        if self.fetch() or self.ref(self.origin_ref) != pushed:
            self.git('update-ref', self.origin_ref, pushed)
        return None

    def sync_main(self):
        """Step 10, after the verified push: the main checkout's main fast-forwarded to the merge
        (`git merge --ff-only` there) when that is safe: HEAD on main, no merge in progress, main
        an ancestor of the merge, and no local change or untracked file on a path the merge
        changes (a local edit elsewhere is carried). Else nothing in the main checkout moves, and
        a WARNING says why and how to bring it there. [] or [WARNING lines]; never raises git's
        failures."""
        self.synced = True
        main_b, remote = self.cfg.main_branch, self.cfg.remote
        g = 'git -C "%s"' % self.root
        ff = '%s merge --ff-only %s/%s' % (g, remote, main_b)
        main = self.ref(self.main_ref)
        if main == self.merge:
            return []
        lead = 'WARNING: %s landed at %s, and the main checkout\'s %s (%s) was not fast-forwarded to it' % (
            self.branch, self.merge[:10], main_b, (main or '?')[:10])
        if not main:
            return ['%s: it cannot be read; bring it there with: %s' % (lead, ff)]
        out, why = cmd.run_text(['log', '--format=%H %s', main, '--not', self.merge], self.root)
        if out is None:
            return ['%s: git cannot tell whether it has commits the landing lacks (%s); check, '
                    'then: %s' % (lead, why, ff)]
        own = [l for l in out.splitlines() if l.strip()]
        if own:
            return ['%s: it has %d commit(s) made in the main checkout that the landing did not '
                    'gate or push: %s. They are kept: rebase them onto %s/%s (or set them aside), '
                    'then: %s' % (lead, len(own), '; '.join(l[:10] + l[40:] for l in own[:10]),
                                  remote, main_b, ff)]
        current, why = refs.current_branch(self.root)
        if current != main_b:
            return ['%s: the main checkout is on %s, not %s; switch back, then: %s' % (
                lead, current or why or 'no branch (detached)', main_b, ff)]
        merging, why = refs.merge_in_progress(self.root)
        if merging is not False:
            return ['%s: %s; finish it, then: %s' % (
                lead, 'a merge is in progress there' if merging else 'git cannot tell whether a '
                'merge is in progress there (%s)' % why, ff)]
        entries, why = gitstatus.status(self.root)
        changed, why2 = runner.changed_between(self.root, main, self.merge)
        if entries is None or changed is None:
            return ['%s: git cannot tell whether local changes are in the way (%s); check, then: %s'
                    % (lead, why or why2, ff)]
        theirs = set(os.path.normcase(p) for p in changed)
        clash = [(xy, p) for xy, p in entries if os.path.normcase(p) in theirs]
        if clash:
            return ['%s: local changes there are on paths the merge changes (%s); keep or drop '
                    'them, then: %s' % (lead, _paths(clash), ff)]
        r = self.git('merge', '--ff-only', '--quiet', self.merge, timeout=MERGE_TIMEOUT)
        now = self.ref(self.main_ref)
        if r.rc != 0 or now != self.merge:
            return ['%s: `git merge --ff-only` did not bring it there (%s); %s' % (
                lead, r.err or 'exit %s' % r.rc if r.rc else 'main is at %s' % (now or '?')[:10],
                ff)]
        return []

    # ---------------------------------------------------------------- the landing

    def run(self):
        """_run(), and the landing's result records (kitlib.gate.result, kind "land"), one per
        branch at its --expect-tip: `running` once the locked checks pass, just before the merge
        (_locked), then the outcome's, or `error` when an exception or Ctrl+C escapes _run (raised
        again, unchanged). The final and the `error` record name the same log: the merged gate's
        log folder once the merged gate returned, else a submitted landing's output, else none.
        Their started_at is when the landing joined the landing queue: a direct
        landing's just before book_turn, a submitted one's at the submission. A landing that ends
        before the locked checks pass (USAGE, REFUSED before the merge, BUSY, the machine lock's
        refusal: a retry or a duplicate) writes no record, so it never replaces a real result; a
        submitted landing's records were written running before it started, so every outcome
        finishes them. The record lines are said as progress: the verdict stays the
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
                    self.say(rr.error(e, log=self.gate_logs or self.output_log)[1])
                except Exception:   # noqa: BLE001 - never replace the exception being raised
                    pass
            raise
        try:
            self.drop_slot(0 if out.code == G.OK else out.code or 1, 'done')
        except KeyboardInterrupt as e:  # the outcome stands: the records still say it
            out.lines.insert(max(0, len(out.lines) - 1), 'WARNING: %s while the machine-lock '
                             'slot was released; its queue entry goes once this process ends'
                             % _what(e))
        if out.code == G.OK and out.facts.get('merge'):
            self.refresh_mirrors(out)
        paths = []
        for rr in self.result_runs:
            path, line = rr.finish(out, base=self.expect_main,
                                   log=self.gate_logs or self.output_log, source=self.source())
            self.say(line)
            paths.append(path)
        out.facts['result_record'] = (paths[0] if len(self.branches) == 1 else paths) if paths else None
        return out

    def refresh_mirrors(self, out):
        """Step 13, landed and nothing held: the merge pushed to the load nodes' mirrors of main
        (kitlib.loadnode.mirror.refresh), its lines put before the LANDED line of `out`. The
        landing stands whatever happens here: an exception or Ctrl+C is a WARNING line."""
        lines = []
        try:
            from kitlib.loadnode import mirror
            mirror.refresh(self.root, runner.repo_name(self.cfg, self.root), self.cfg.main_branch,
                           out.facts['merge'], store=self.queue_store(), say=lines.append)
        except BaseException as e:  # noqa: BLE001 - landed: only the nodes' mirrors lag
            lines.append("WARNING: refreshing the load nodes' mirrors of %s stopped (%s); the "
                         'landing stands' % (self.cfg.main_branch, _what(e)))
        at = next((i for i, x in enumerate(out.lines) if x.startswith('LANDED ')), len(out.lines))
        out.lines[at:at] = lines

    def _run(self):
        why = self.usage()
        if why:
            return G.Outcome(G.USAGE, ['USAGE: %s' % why])
        why = self.preconditions()
        if why:
            return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed' % why])
        if not self.submitted:      # joins the queue now: its records' started_at
            runs = (result.Run.maybe(self.root, 'land', b, t) for b, t in self.pairs)
            self.queued_runs = [rr for rr in runs if rr is not None]
        self.plan_route()
        try:
            self.book_turn()
        except (landing.LockError, ConfigError) as e:
            return G.Outcome(G.REFUSED, ['REFUSED: the landing lock: %s; nothing was changed' % e])
        except LockError as e:
            return self.not_run(e)
        why = self.fetch()
        if why:
            return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed' % why])
        try:
            why = self.take()
        except (landing.LockError, ConfigError) as e:
            return G.Outcome(G.REFUSED, ['REFUSED: the landing lock: %s; nothing was changed' % e])
        except LockError as e:      # booking again behind the lock file's holder (yield_turn)
            return self.not_run(e)
        if why:
            return G.Outcome(G.BUSY, ['BUSY: %s; nothing was changed' % why])
        try:
            return self._locked()
        except _Stuck as e:
            return self.stuck(str(e))
        except BaseException as e:  # after the lock is taken nothing may leave it silently
            out = self.recover(e)
            if out.code in (G.STUCK, G.OK):  # Ctrl+C included: STUCK is exit 3, with what is
                return out      # left; OK: stopped after the verified push, landed
            if isinstance(e, ConfigError) and out.code == G.REFUSED:
                return out
            if isinstance(e, KeyboardInterrupt) or (isinstance(e, Exception) and
                                                    self.phase == 'before'):
                for line in out.lines:  # Ctrl+C undone and released (exit 130), or a bug
                    self.say(line)      # before the merge: raised again
                raise
            return out                  # an exception or SystemExit after the merge: dropped

    def not_run(self, e):
        """The outcome of the machine lock's LockError `e` before the landing lock file is held."""
        if e.code == EXIT_USAGE:
            return runner.lock_refusal(e)
        return G.Outcome(e.code, ['NOT RUN: the machine lock did not give the landing its turn: '
                                  '%s; nothing was changed' % e.message])

    def recover(self, e):
        """After an exception `e` under the lock: release it before the merge; after the merge
        (phase merge or gate) put the landing tree's merge aside (half_merge) and release it:
        nothing was pushed, and the main checkout was never merged into. Phase push: STUCK (the
        merge may be on the remote). Phase pushed or landed: landed (landed_anyway). An Outcome
        saying what happened."""
        what = 'Ctrl+C' if isinstance(e, KeyboardInterrupt) else '%s: %s' % (type(e).__name__, e)
        main_b, base = self.cfg.main_branch, self.expect_main
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
            lines = self.half_merge(what)
            self.release()
            if self.phase == 'merge' and not self.merges:
                return G.Outcome(G.FAILED, lines + [
                    'NOT LANDED: %s during the merge; it was aborted, and the landing did not '
                    'move %s from the base %s' % (
                        what, main_b, base[:10])], undone=True)
            return G.Outcome(G.FAILED, lines + [
                'NOT LANDED: %s in phase %s; the merge of %s was undone, and the landing did not '
                'move %s from the base %s'
                % (what, self.phase, self.branch, main_b, base[:10])], undone=True)
        except _Stuck as s:
            return self.stuck(str(s))
        except BaseException as s:  # a second failure while recovering: leave the lock held
            return self.stuck('%s, and then %s: %s while recovering' % (what, type(s).__name__, s))

    def landed_anyway(self, what):
        """`what` stopped the landing after its push was verified (phase pushed): it is landed.
        Bring the main checkout to the merge if that has not run (sync_main), release the lock (a
        failure is a WARNING with the break command) and say what was skipped: an OK Outcome,
        never STUCK. Each step is guarded against BaseException, so a second Ctrl+C or failure
        here is one more WARNING, never a STUCK for a landed merge."""
        lines = list(self.gate_out.lines) if self.gate_out is not None else []
        at = self.landed_at or self.merge   # what ls-remote showed: the merge, or one on top
        if not self.synced:
            try:
                lines.extend(self.sync_main())
            except BaseException as e:  # noqa: BLE001 - landed: the main checkout is only behind
                lines.append('WARNING: bringing the main checkout to the merge stopped (%s); '
                             'fast-forward it with: git -C "%s" merge --ff-only %s/%s' % (
                                 _what(e), self.root, self.cfg.remote, self.cfg.main_branch))
        if at == self.merge:
            how = 'pushed and verified on %s' % self.cfg.remote
        else:
            how = 'already on %s: %s/%s %s has it' % (self.cfg.remote, self.cfg.remote,
                                                     self.cfg.main_branch, at[:10])
        lines.extend(self.scope_notes)
        lines.append('LANDED %s on %s at %s (%s)' % (self.branch, self.cfg.main_branch,
                                                     self.merge, how))
        lines.extend(self.scope_warnings)
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
        return G.Outcome(G.OK, lines, merge=self.merge, verdict=verdict)

    def half_merge(self, what):
        """Merging or gating stopped part way (phase merge or gate): abort a merge in progress in
        the landing tree, and when its HEAD is exactly the merge of the next tip onto the last
        recorded merge (or the base), record it in self.merges. Best effort: the main checkout was
        never merged into, and the next landing puts the tree back at its base or recreates it,
        so this never raises for git; WARNING lines, or []."""
        tree = self.tree
        if not tree or not os.path.isdir(tree):
            return []
        lines = []
        merging, _ = refs.merge_in_progress(tree)
        if merging:
            a = self.git('merge', '--abort', timeout=MERGE_TIMEOUT, cwd=tree)
            if a.rc != 0:
                lines.append('WARNING: %s during the merge, and `git merge --abort` failed in the '
                             'landing tree %s (%s); the next landing recreates it' % (
                                 what, tree, a.err or 'exit %s' % a.rc))
                return lines
        prev = self.merges[-1] if self.merges else self.expect_main
        nxt = self.tips[len(self.merges)] if len(self.merges) < len(self.tips) else None
        head = self.tree_head()
        if not head or head == prev or not nxt:
            return lines
        first, _ = cmd.run_text(['rev-parse', 'HEAD^1'], tree)
        second, _ = cmd.run_text(['rev-parse', 'HEAD^2'], tree)
        if first == prev and second == nxt:
            self.merges.append(head)
            self.merge = head
        return lines

    def _locked(self):
        why = self.locked_checks() or self.prepare_tree()
        if why:
            self.release()
            return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was merged' % why])
        if self.strict_hits:        # strict_check passed it: --full --fresh, said once
            self.strict_escalate()
        if not self.fresh:          # before this landing's own records replace them
            self.prior = self.prior_records()
        if self.submitted:          # running since the submission: now with the base
            for rr in self.result_runs:
                rr.base = self.expect_main
                self.say(rr.start(log=self.output_log)[1])
        else:                       # made when the landing joined the queue (_run)
            for rr in self.queued_runs:
                rr.base = self.expect_main
                self.result_runs.append(rr)
                self.say(rr.start()[1])
        started = gate_ok.utc()
        failed = self.do_merge()
        if failed is not None:
            self.release()
            return failed
        self.say('merged %s at %s in the landing tree %s; gating the merged tree' % (
            self.branch, self.merge[:10], self.tree))
        self.once_escalate()
        out = self.gate_out = self.gate()
        main_b, base = self.cfg.main_branch, self.expect_main
        if out.code == G.OK:
            changed = self.unchanged()      # the commit pushed must be the commit gated
            if changed:
                self.release()
                return G.Outcome(G.FAILED, _not_landed(out.lines) + [
                    'NOT LANDED: the merged gate passed, and %s; nothing was pushed, and the merge '
                    'of %s was undone (the landing did not move %s from the base %s)' % (
                        changed, self.branch, main_b, base[:10])],
                    merge=self.merge, verdict=out.facts.get('verdict'), undone=True)
        if out.code != G.OK:
            self.release()
            return G.Outcome(out.code,
                             out.lines + ['NOT LANDED: the merge of %s was undone; the landing '
                                          'did not move %s from the base %s'
                                          % (self.branch, main_b, base[:10])],
                             verdict=out.facts.get('verdict'), undone=True)
        why = self.push(started, out.facts.get('results'))
        if why:
            self.release()
            return G.Outcome(G.FAILED, _not_landed(out.lines) + ['NOT LANDED: %s' % why],
                             merge=self.merge)
        synced = self.sync_main()
        landed = ['LANDED %s on %s at %s (pushed and verified on %s)' % (
            self.branch, main_b, self.merge, self.cfg.remote)] + self.scope_warnings
        try:
            self.release(landed=True)
        except landing.LockError as e:  # the landing is done; only the lock file is left
            landed.append('WARNING: the landing lock could not be released: %s; check that no '
                          'landing is running, then break it with: %s' % (e, self.break_command()))
        landed.extend(self.close_pulled())
        tidy = self.clean_up()
        return G.Outcome(G.OK, out.lines + tidy + synced + self.scope_notes + landed,
                         merge=self.merge,
                         verdict=out.facts.get('verdict'), cleanup=self.cleanup)

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
        lines of the branch merged before it; the batch's other branches are no holder to tell.
        A batch not merged as a stack (fewer merges than branches) reads every branch's lines down
        to the base, which takes in an earlier batch branch's lines when the tip carries them: a
        WARNING says so for each branch after the first (its Part-of line can release what this
        branch would close), and nothing else changes."""
        self.phase = 'landed'
        lines = []
        stacked = len(self.merges) == len(self.pairs)
        for i, (b, t) in enumerate(self.pairs):
            base = self.merges[i - 1] if stacked and i else self.expect_main
            if i and not stacked:
                lines.append(
                    'WARNING: the batch was not merged as a stack (%d merge(s) for %d branches), '
                    'so %s\'s Closes and Part-of lines are read from its tip down to the base %s, '
                    'which takes in the lines of a batch branch before it that its tip carries: a '
                    'Part-of line of that branch can release records %s holds instead of closing '
                    'them; see the lines read with: git log --format=%%B %s --not %s'
                    % (len(self.merges), len(self.pairs), b, str(base)[:10], b, t, base))
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
                'import LandingLock as L; print(L(r\'%s\', \'%s\', \'%s\')'
                '.break_lock())"' % (S.kit_live(), self.root, self.cfg.main_branch, self.cfg.remote))

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
            lines.append('%s. Check %s against %s/%s (git log --first-parent), finish by hand, '
                         'then break the lock.' % (state, main_b, remote, main_b))
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
        """`[(what, [commands])]` that fit what is left. The main checkout was never merged into,
        so no step resets or undoes anything: a STUCK landing only has to find out whether its
        push landed. When the remote's main (or, when ls-remote cannot read it, the tracking ref)
        holds the merge: on_origin_steps (bring the main checkout there); when it may hold it and
        git cannot tell: check_first."""
        main_b, remote = self.cfg.main_branch, self.cfg.remote
        g = 'git -C "%s"' % self.root
        tracking = 'refs/remotes/%s/%s' % (remote, main_b)
        upstream = '%s/%s' % (remote, main_b)
        pushed = left['pushed']
        if pushed is None:
            on = self.on_remote(left['tracking'])
            if on:                  # a push from this checkout set it: the merge is landed
                return [('the push may have landed: %s could not be read (%s), and the '
                         'tracking ref %s (%s) holds the merge %s, so it is on %s. Fetch, then '
                         'check what %s is' % (
                             upstream, left['pushed_why'] or 'ls-remote failed', tracking,
                             left['tracking'][:10], (self.merge or '?')[:10], remote, upstream),
                         ['%s fetch %s' % (g, remote), '%s rev-parse %s' % (g, tracking)])
                        ] + self.on_origin_steps(left, left['tracking'])
            if on is None:
                return [self.check_first(left['tracking'])]
            steps = [('the push may have landed: %s could not be read (%s). Fetch, then check '
                      'what %s is' % (upstream, left['pushed_why'] or 'ls-remote failed', upstream),
                      ['%s fetch %s' % (g, remote), '%s rev-parse %s' % (g, tracking)]),
                     ('if that printed the merge %s, the landing is done: bring %s to it (HEAD '
                      'on %s, then `%s merge --ff-only %s`), then go to the last step. If it '
                      'printed %s, the push did not land, and nothing is to be undone: the main '
                      'checkout was never merged into. If the fetch failed, stop: what it printed '
                      'is stale' % ((self.merge or '(none)'), main_b, main_b, g, upstream,
                                    self.expect_main), [])]
            if self.merge:          # neither: someone pushed, maybe on top of the merge
                steps.append(('if it printed anything else, list the remote branches that hold '
                              'the merge: if the list names %s, the merge is landed: bring %s to '
                              'it as above; if it does not, nothing landed and nothing is to be '
                              'undone' % (upstream, main_b),
                              ['%s branch -r --contains %s' % (g, self.merge)]))
            return steps
        on = self.on_remote(pushed)
        if on:
            return self.on_origin_steps(left, pushed)
        if on is None:              # may be landed: nothing is settled until that is known
            return [self.check_first(pushed)]
        if pushed != self.expect_main:
            return [('%s is at %s, neither the base (%s) nor the merge: someone else pushed. The '
                     'merge did not land, and nothing is to be undone: the main checkout was '
                     'never merged into' % (upstream, (pushed or 'absent')[:10],
                                            self.base_text()), [])]
        return [('%s is still at the base (%s): the merge did not land, and nothing is to be '
                 'undone: the main checkout was never merged into' % (upstream, self.base_text()),
                 [])]

    def check_first(self, pushed):
        """clear_steps when git cannot tell whether the remote's main (`pushed`, or the tracking
        ref when ls-remote could not be read) holds the merge: one step that finds out. Nothing is
        to be undone either way: the main checkout was never merged into."""
        main_b, remote = self.cfg.main_branch, self.cfg.remote
        g = 'git -C "%s"' % self.root
        upstream = '%s/%s' % (remote, main_b)
        return ('%s is at %s, which this checkout cannot place against the merge %s, so whether '
                'it holds the merge cannot be told here. Fetch, then list the remote branches '
                'that hold the merge. If the fetch failed, the list is stale: stop here and do '
                'not move %s. If the list names %s, the merge is landed: bring the checkout to %s '
                'by hand (HEAD on %s, %s fast-forwarded to %s), then go to the last step. If it '
                'does not, the merge is not on %s, and nothing is to be undone (the main checkout '
                'was never merged into): settle %s with whoever pushed it, then go to the last '
                'step' % (
                    upstream, (pushed or '?')[:10], (self.merge or '?')[:10], main_b, upstream,
                    upstream, main_b, main_b, upstream, remote, upstream),
                ['%s fetch %s' % (g, remote), '%s branch -r --contains %s' % (g, self.merge)])

    def on_origin_steps(self, left, pushed):
        """clear_steps when the remote's main (`pushed`) is a merge of this landing or descends
        from one: the merge is landed, so no step undoes it and none says reset. The steps bring
        the main checkout to the remote's main (fetch, switch back, a fast-forward), leave
        anything only the checkout has to a person, and say how a merge that reached the remote
        without a passing merged gate is taken back (a revert by hand, never run here). Commits
        on the remote's main on top of the landing are named, with the full gate to run in a new
        worktree (tools/new_worktree.py), never in the main checkout."""
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
            # A full gate runs in a worktree, never the main checkout (AGENTS.md); in the kit,
            # with the worktree's own gate.py, as full_gate_command names it.
            wt = '<worktree>'
            gate_py = os.path.join(wt if oncommit.is_kit(self.root) else S.kit_live(), 'tools',
                                   'gate.py')
            steps.append(('%s has %d commit(s) on top of the merge that no merged gate saw: %s. '
                          'Once the lock is broken, gate %s whole in a worktree, never in the main '
                          'checkout: make one on a new branch from %s with `python "%s" -C "%s" '
                          '<area>/<slug>`, then gate it: `python "%s" -C "%s" --full`' % (
                              upstream, len(on_top),
                              '; '.join(l[:10] + l[40:] for l in on_top[:10]) + (
                                  '; and %d more' % (len(on_top) - 10) if len(on_top) > 10
                                  else ''),
                              main_b, upstream,
                              os.path.join(S.kit_live(), 'tools', 'new_worktree.py'), self.root,
                              gate_py, wt),
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
    Outcome (OK, FAILED, USAGE, STUCK, REFUSED, BUSY, or a machine lock code when the booking was
    refused; LANDED_UNGATED is retired, never returned)."""
    return Lander(root, branch, expect_main, expect_tip, **kw).run()


def check_strict(root, branches):
    """Read-only, the dry strict check a coordinator runs before it picks a reviewer (docs/gate.md
    "Strict paths"): for each of `branches`, its local tip against `<remote>/<main>` as last
    fetched (runner.default_base; no fetch), decided by Lander.strict_paths, the matcher
    strict_check refuses by. An OK Outcome, one line per branch: `STRICT <branch>: <paths>: ...`
    or `NOT STRICT <branch>: ...`. Takes no lock, needs no verdict and writes nothing. REFUSED
    when the config, the base, a branch or the strict paths cannot be read."""
    try:
        cfg = kconfig.load(root)
    except ConfigError as e:
        return G.Outcome(G.REFUSED, ['REFUSED: the config of %s: %s' % (root, e)])
    ref, why = runner.default_base(cfg, root)
    base = refs.resolve(ref, root)[0] if ref else None
    if not base:
        return G.Outcome(G.REFUSED, ['REFUSED: no base to check against: %s' % (
            why or '%s does not resolve' % ref)])
    tips = []
    for b in branches:
        tip = None if refs.bad_rev(b) else refs.resolve('refs/heads/%s' % b, root)[0]
        if not tip:
            return G.Outcome(G.REFUSED, ['REFUSED: no branch %s in this repository' % b])
        tips.append(tip)
    lander = Lander(root, list(branches), None, tips)
    lander.cfg, lander.expect_main = cfg, base
    hits, why = lander.strict_paths()
    if why:
        return G.Outcome(G.REFUSED, ['REFUSED: %s' % why])
    lines = []
    for b, t in zip(branches, tips):
        on = 'tip %s, base %s %s' % (t[:10], ref, base[:10])
        found = hits.get(b)
        if not found:
            lines.append('NOT STRICT %s: no strict path changed (%s)' % (b, on))
            continue
        more = len(found) - STRICT_SHOWN
        lines.append('STRICT %s: %s%s (%s): its reviewer must run on a strong-tier model, and it '
                     'lands alone' % (b, ', '.join(found[:STRICT_SHOWN]),
                                      ' and %d more' % more if more > 0 else '', on))
    return G.Outcome(G.OK, lines)


def queue(root, store=None):
    """Read-only: the repository's landing lock file and the landings in the machine lock's queue
    (entries of priority "land"; kitlib.lock.queue), as an OK Outcome: one line for the lock file,
    then one per landing, holding first, then waiting in turn order. A landing of another
    repository is marked so: they share the machine lock's slots, not the turn. A lock file that
    is not a record is shown held, phase ?, with the read error: that is when an operator checks
    here before clearing it by hand. REFUSED when the config, the lock's place or the queue cannot
    be read."""
    try:
        cfg = kconfig.load(root)
        lock = landing.LandingLock.for_config(root, cfg)
        try:
            rec, unreadable = lock.read(), None
        except landing.LockError as e:
            rec, unreadable = {'phase': '?'}, str(e)
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
