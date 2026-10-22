"""Merge the base-branch commit the coordinator named into a branch, and check the result (from the
PM kit's tools/sync_base.py; docs/sync-base.md).

`sync(path, target, model, ...)` is the whole command; tools/sync_base.py parses, calls it, prints
`Outcome.lines` and exits with `Outcome.code`.

- Merges EXACTLY <target> (never "whatever local main is", never a rebase, never force).
- The checkout is the work tree `path` is in (tools/sync_base.py's `-C DIR`, default `.`), found
  with kitlib.git.repo.toplevel. The main checkout is lander-only: a merge there is refused
  (kitlib.git.checkout.main_refusal), naming the -C form.
- `model` names the model actually running this: a clean merge makes a real merge commit, and
  every commit message ends with `Co-Authored-By: Claude <model name> <noreply@anthropic.com>`
  ("Claude " is added unless the name already starts with it). resolve_model() reads the flag,
  else the CLAUDE_AGENT_MODEL environment variable (a per-session fallback for the model name, not
  a root or config override); neither is a usage error in the entry point.
- The base defaults to `.kit.json` "repo".main_branch; the remote is "repo".remote.
- Fetches <remote> <base> first, then refuses unless <target> is on the FETCHED
  refs/remotes/<remote>/<base> (refusal-log P27): a commit on local <base> alone is one the
  remote may never get (someone's unpushed commit, a local <base> ahead of the remote; a
  landing merges in its own landing tree and moves local <base> only after its push is
  verified, kitlib.gate.land). The refusal names the hash to use instead: the
  fetched <remote>/<base> (`git ls-remote <remote> <base>`), never local <base>. A dirty tree is
  refused; a <target> that is not a commit here is NOT FOUND.
- With `.kit.json` "landing".in_flight set (kitlib.records.inflight), a landing in flight is
  waited for before the fetch, up to its wait_s (`wait` False: not at all); at the bound a line
  says so and the merge goes on. Still exactly <target>: when the fetched <remote>/<base> is past
  it, one `note` line says so and names the tip, for the coordinator to decide.
- Conflicts: lists them, writes the `Co-Authored-By:` trailer into the message git prepared (so
  the resolution commit carries it whether it is finished with the commit tool (tools/commit.py,
  "commit".merge_fold) or an editor - git's auto-generated message never carried one), and stops
  with the merge in progress. Docs conflicts: a union of both sides' lines is WRONG - it silently
  resurrects rows the base deleted on purpose. `conflict_advice()` below carries the rule and
  the per-line audit that verifies a resolution; anything you did not write goes back to the
  coordinator.
- The merge runs with merge.directoryRenames=true: a file this branch added under a
  folder the base moved lands in the new folder instead of resurrecting the old one.
- Clean merge: checks the silent-undo hazard - a file the base shrank a lot that came out of
  the merge big again (a stale branch reinstating deleted text). The merge itself reports no
  conflict for that, so this check is the only warning you get. The sizes come from one shared
  `git cat-file --batch-check` (kitlib.git.batch.CatFile); if that reader dies the check did not
  run, and that is said and exits 1 rather than passing as clean.
- Then says which gate to run. The first line is the worktree it acts on, so a path that walked
  into another checkout shows at once.

Exit codes: OK 0 merged and clean (or nothing to merge); FAILED 1 refused / not found /
conflicts / regrowth found / the size reader died (the merge stands, the check did not run);
USAGE 2 the checkout, the configuration or the main-checkout refusal.
"""
import collections
import os
import time

from kitlib import attribution
from kitlib import kitroot

from kitlib.config import ConfigError, load as load_config
from kitlib.git import checkout as gitcheckout
from kitlib.git import cmd as gitcmd
from kitlib.git import refs as gitrefs
from kitlib.git import repo as gitrepo
from kitlib.git.batch import CatFile
from kitlib.records import inflight, landing

TOOL = 'sync_base'
MODEL_ENV = attribution.model_environment()       # the default host's (hosts.json)
OK, FAILED, USAGE = 0, 1, 2
FETCH_TIMEOUT = 120       # seconds: a fetch is network
MERGE_TIMEOUT = 600       # seconds: a merge of a large tree is slower than a ref read

Outcome = collections.namedtuple('Outcome', 'code lines')


def resolve_model(flag, env=None, provider=None):
    """`(model, where it came from)`, or `(None, None)` when neither the flag nor MODEL_ENV (a
    per-session fallback; blank counts as unset) names one."""
    env = os.environ if env is None else env
    model_env = attribution.model_environment(provider)
    if attribution.requires_model_id(provider):
        attribution.coauthor(flag, provider)
    if flag and flag.strip():
        return flag, '--model'
    fallback = (env.get(model_env) or '').strip() if model_env else ''
    return (fallback, model_env) if fallback else (None, None)


def trailer(model, provider=None):
    """The attribution line every commit message ends with: always `Claude <model>`, never a
    bare model name (commit.py's `coauthor()`)."""
    return attribution.coauthor(model, provider)


def _git(root, *args, timeout=gitcmd.GIT_TIMEOUT, stdin=None, env=None):
    """`(rc, stdout text, stderr text)`; rc None when git did not run."""
    r = gitcmd.call(list(args), root, stdin=stdin, timeout=timeout, env=env)
    return r.rc, gitcmd.text(r.out).strip(), r.err


def merge_msg_path(root):
    """Absolute path of the message git prepared for the merge commit (`.git/MERGE_MSG`).

    GOTCHA: git's merge message has no trailer and a bare `git commit` takes it as-is, so a conflicted
    merge lands untrailered unless something appends one here (not as `# Conflicts:` - git ignores
    trailing comments). Read back with `git log --format=%(trailers:key=Co-Authored-By)`."""
    rc, out, _ = _git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'MERGE_MSG')
    return os.path.normpath(out) if rc == 0 and out else ''


def last_paragraph(text):
    """The message's last paragraph with comment-only lines dropped - what git parses trailers
    from. `git interpret-trailers` ignores a trailing comment block, so `# Conflicts:` does not
    end the message for this purpose."""
    lines = [l for l in text.splitlines() if not l.startswith('#')]
    out = []
    for line in reversed(lines):
        if not line.strip():
            if out:
                break
            continue
        out.append(line)
    return '\n'.join(reversed(out))


def arm_merge_msg(root, model, provider=None):
    """Put the trailer into the message git prepared, so the RESOLUTION commit carries it.

    The clean path makes the merge commit itself and can add the trailer as it goes. The conflict
    path cannot: it hands the tree to a human, who finishes with the commit tool (or an
    editor) on git's own AUTO-GENERATED message, which never contained a trailer. That is the
    whole cause of the untrailered merge commits that reached a base branch on 2026-09-21 -
    nothing subtler - and amending a commit to add one afterwards is not allowed. Writing it here
    makes the trailer arrive BY DEFAULT rather than by the caller remembering an instruction.

    It is APPENDED, after git's `# Conflicts:` block. Not because that block would hide it -
    measured, it would not: git's trailer parser ignores a trailing comment-only block, and merge
    commits carrying a trailer written ABOVE `# Conflicts:` parse correctly. Appending is chosen
    because it is the only placement that survives a NON-comment last paragraph, which `merge.log`
    (a shortlog of the merged commits) and anything typed in the editor both produce: measured, a
    trailer above such a paragraph is NOT parsed. Appending also needs no guess about where git's
    own text ends.

    Returns True when the message now carries the trailer. Changes nothing about what is merged.
    """
    # Not `rev-parse --verify --quiet MERGE_HEAD`: its "none" is a killed git's shape
    # (gitrefs.KILLED_SHAPE). A merge that cannot be seen is not armed, and the advice says so.
    if not gitrefs.merge_in_progress(root)[0]:
        return False           # no merge in progress, or none git could show: nothing to arm
    line = trailer(model, provider)
    path = merge_msg_path(root)
    if not path or not os.path.isfile(path):
        return False
    with open(path, 'rb') as handle:
        raw = handle.read()
    text = raw.decode('utf-8', errors='surrogateescape')   # round-trips bytes we cannot decode
    if line in last_paragraph(text):
        # Only the LAST paragraph counts: the line can appear in a `merge.log` shortlog or in a
        # draft body without being a trailer git will parse, and skipping on that would report
        # "already in the prepared message" about a trailer that is not there.
        return True
    with open(path, 'wb') as handle:
        handle.write((text.rstrip('\n') + '\n\n' + line + '\n').encode('utf-8', 'surrogateescape'))
    return True


def conflict_advice(base, target, model=None, armed=False, provider=None):
    """What to print when the merge stops on conflicts. A message only - it decides nothing.

    It deliberately does NOT say "keep both sides' lines". A row present on your branch and absent
    from the base is either new on your branch or deleted there on purpose, and the two texts
    cannot tell those apart, so a union resurrects retirements. That mistake has been made and
    corrupted a file; so has its over-correction, "the base's text always wins", which reverted a
    branch's own row to a pre-fix state.

    Nor does it say "the diff must show zero deletions", which is worse than merely strict.
    Measured on a staged three-way conflict (base retires a row and updates another; branch
    rewrites a row and adds one), `git diff <base> -- <file>` on each resolution gives: the
    CORRECT one 1 deletion and 2 additions; the union that resurrects the retired row the SAME 1
    deletion, differing only by an addition; and the over-correction that takes the base
    wholesale 0 and 0. So zero-deletions fails the correct resolution,
    is blind to the resurrection it was introduced to catch,
    and hands a clean bill to the one outcome that destroys the branch's work. Every line has to
    be justified instead, on both sides, which is what this prints.

    `armed` says whether `arm_merge_msg` got the attribution trailer into the prepared message. It
    changes only what this text says about the commit, never what the caller has to do.
    """
    if armed:
        armed = ('trailer: already in the prepared message, so finish with\n'
                 '  tools/commit.py (it adds the trailer; "commit".merge_fold lets the merge\'s staged\n'
                 '  files through). Writing your own message instead? End it with `%s`.'
                 % trailer(model, provider))
    else:
        armed = ('trailer: could NOT be written into the prepared message. End your resolution\n'
                 '  commit message with `%s` by hand.' % trailer(model, provider))
    return (
        'yours to resolve: files you changed. Anything else: `git merge --abort` and tell the '
        'coordinator.\n'
        'docs, keyword form: do NOT simply keep both sides\' lines. A row missing from the base is\n'
        '  either new on your branch or deliberately deleted there, and the two texts cannot tell\n'
        '  those apart, so a union silently resurrects retirements.\n'
        '  %s\'s text wins for a row %s changed; yours wins for a row your own work changed.\n'
        '  verify with `git diff %s -- <file>` and JUSTIFY EVERY LINE it prints, one sentence\n'
        '  each. Counting proves nothing - a correct resolution and a resurrection print the\n'
        '  same number of deletions:\n'
        '    a deletion is %s text you no longer have - fine only for a row your own work\n'
        '      changed OR RETIRED (finished work is deleted here); anything else is a loss.\n'
        '    an addition is text %s does not have - fine only for a row new on your branch;\n'
        '      anything else is a row %s retired, coming back.\n'
        '    NOTHING on either side means you took %s wholesale: your own work is gone.\n'
        '  a line you cannot justify: stop. Do not pick a side to make the diff quiet.\n'
        '  which row and why, when you need it: `git log -S"<ROW-ID>" %s -- <file>`.\n'
        '%s'
        % (base, base, target[:7], base, base, base, base, base, armed))


def regrown(was, now_base, merged):
    """True when the base shrank a file a lot and the merge result is big again."""
    return (was - now_base > 300 and now_base < 0.9 * was
            and merged - now_base > max(200, 0.1 * now_base))


def not_on_remote(target, upstream, remote, base, tip):
    """The P27 refusal: <target> is not on the fetched <remote>/<base> (at `tip`)."""
    return ('refused: %s is not on %s (fetched just now; it is at %s). Only a pushed %s commit is '
            'synced: local %s may hold a lander\'s unpushed merge. Sync to the fetched %s hash '
            '(`git ls-remote %s %s`), never local %s. If that is not the hash you were given, '
            'tell the coordinator.' % (target, upstream, tip[:7], base, base, upstream, remote, base,
                                     base))


def in_flight_reader(top, cfg):
    """kitlib.records.inflight's reader for the repository `top` is in, or None when `.kit.json`
    "landing".in_flight is unset. Its command runs in the main checkout. Raises ConfigError or
    kitlib.records.landing.LockError for a lock_file or a git common dir that cannot be used."""
    if cfg.section('landing').get('in_flight') is None:
        return None
    common, why = gitrepo.common_dir(top)
    if not common:
        raise landing.LockError('no git common dir for %s: %s' % (top, why))
    main = os.path.realpath(os.path.abspath(os.path.dirname(common)))
    return inflight.reader(main, cfg, landing.LandingLock.for_config(main, cfg))


def _size(cat, rev, path):
    got = cat.size(rev, path)
    return got if got is not None else 0


def sync(path, target, model, model_source='--model', base=None, wait=True, say=None,
         sleep=time.sleep, clock=time.monotonic, provider=None):
    """The whole command for the checkout `path` is in. Returns an Outcome; never prints. `wait`
    False is --no-wait; `say(line)` gets the lines said while waiting for a landing in flight
    (None: they go in the Outcome's lines); `sleep` and `clock` are time's, for tests."""
    lines = []

    def done(code, *more):
        lines.extend(more)
        return Outcome(code, lines)

    try:
        attribution_line = trailer(model, provider)
    except ValueError as error:
        return done(USAGE, 'USAGE: %s' % error)

    top, why = gitrepo.toplevel(path)
    if top is None:
        return done(USAGE, '%s: refused: %s' % (TOOL, why))
    try:
        cfg = load_config(top)
        rule_pointer = cfg.section('checkout_guard').get('rule_pointer')
    except ConfigError as e:
        return done(USAGE, '%s: refused: kit configuration: %s' % (TOOL, e))
    base = base if base is not None else cfg.main_branch
    remote = cfg.remote
    upstream = '%s/%s' % (remote, base)
    refusal = gitcheckout.main_refusal(top, 'a merge', 'sync_base.py', rule_pointer)
    if refusal:
        return done(USAGE, refusal)
    lines.append('worktree  %s' % top)
    if model_source not in (None, '--model'):
        lines.append('model     %s (from %s)' % (model, model_source))
    # The fetch comes first, so a hash pushed since the last fetch is found. Only a PUSHED base
    # commit, judged against that fresh fetch (P27). A hash on the local base branch alone
    # (someone's unpushed commit, a local base ahead of the remote; a landing never leaves one,
    # as it moves local base only after its push is verified) may never reach the remote; merged
    # in here, it stays in this branch as a base commit the remote never gets - a trailerless
    # "branch commit" a gate's trailer check then fails for good.
    for name in (remote, base):
        if gitrefs.bad_rev(name):
            return done(USAGE, '%s: refused: %s' % (TOOL, gitrefs.bad_rev(name)))
    try:
        reader = in_flight_reader(top, cfg)
    except (ConfigError, landing.LockError) as e:
        return done(USAGE, '%s: refused: kit configuration: %s' % (TOOL, e))
    if reader is not None:                    # before the fetch: a landing that pushes is fetched
        lines.extend(inflight.settle(reader, wait, say, sleep, clock)[1])
    rc, _, err = _git(top, 'fetch', '--quiet', remote,
                      '+refs/heads/%s:refs/remotes/%s' % (base, upstream), timeout=FETCH_TIMEOUT,
                      env=dict(gitcmd.scrubbed_env(), GIT_TERMINAL_PROMPT='0'))
    if rc != 0:
        return done(FAILED, 'refused: could not fetch %s %s, so whether %s is pushed cannot be '
                            'judged (%s). Fix the remote, then retry once; if still refused, tell '
                            'the coordinator.' % (remote, base, target,
                                                err or 'git fetch exited %s' % rc))
    full, why = gitrefs.resolve(target, top, known=True)
    if full is None and not gitrefs.bad_rev(target):
        return done(FAILED, 'refused: whether %s is a commit here could not be read (%s). Retry '
                            'once; if still refused, tell the coordinator.' % (target, why))
    if not full:
        return done(FAILED, 'NOT FOUND: %s is not a commit in this repository, even after fetching '
                            '%s. Do not substitute another hash: tell the coordinator.'
                    % (target, upstream))
    tip, _ = gitrefs.remote_tip(remote, base, top)
    if tip is None:
        return done(FAILED, 'refused: %s does not resolve - fetch first.' % upstream)
    on, why = gitrefs.is_ancestor(full, tip, top)
    if on is None:
        return done(FAILED, 'refused: could not tell whether %s is on %s (%s).'
                    % (target, upstream, why))
    if not on:
        return done(FAILED, not_on_remote(target, upstream, remote, base, tip))
    if reader is not None and tip != full:
        lines.append('note      %s is at %s, past %s: ask the coordinator whether to sync to it'
                     % (upstream, tip[:7], full[:7]))
    dirty, why = gitrefs.status(top, untracked='no')
    if dirty is None:
        return done(FAILED, 'refused: could not read the work tree status (%s).' % why)
    if dirty:
        return done(FAILED, 'refused: uncommitted changes. Commit work in progress first '
                            '(%s ...); a record backlog.py staged rides in that '
                            'commit too:' % kitroot.command('commit', top),
                    *['  %s %s' % (xy, p) for xy, p in dirty])

    contained, why = gitrefs.is_ancestor(full, 'HEAD', top)
    if contained is None:
        return done(FAILED, 'refused: could not tell whether HEAD contains %s (%s).' % (target, why))
    if contained:
        return done(OK, 'already contains %s; nothing to merge.' % full[:7])
    old_mb, why = gitrefs.merge_base('HEAD', full, top)
    if not old_mb:
        return done(FAILED, 'refused: no merge-base of HEAD and %s (%s).'
                    % (target, why or 'no common history'))
    before = _git(top, 'rev-parse', '--short', 'HEAD')[1]
    rc, out, err = _git(top, '-c', 'merge.directoryRenames=true', 'merge', '--no-commit', full,
                        timeout=MERGE_TIMEOUT)
    if rc != 0:
        conflicted = _git(top, 'diff', '--name-only', '--diff-filter=U')[1].splitlines()
        in_progress, _ = gitrefs.merge_in_progress(top)
        if not conflicted and not in_progress:
            return done(FAILED, 'merge failed, nothing was merged: %s'
                        % (err or out or 'git merge exited %s' % rc))
        lines.append('CONFLICTS (%d), merge left in progress:' % len(conflicted))
        lines.extend('  ' + f for f in conflicted)
        return done(FAILED, conflict_advice(base, full, model, arm_merge_msg(top, model, provider), provider))
    # GOTCHA: this asked `rev-parse --verify --quiet MERGE_HEAD`, whose
    # "no MERGE_HEAD" is exit 1 with no output, a killed git's shape (gitrefs.KILLED_SHAPE): a kill
    # read as a fast-forward, so the merge was left uncommitted and reported merged.
    merging, why = gitrefs.merge_in_progress(top)
    if merging is None:
        return done(FAILED, 'refused: git merge --no-commit %s exited 0, but whether it left a '
                            'merge to commit could not be read (%s). Nothing was committed and the '
                            'merge may be in progress: `git status` says which. Tell the '
                            'coordinator.' % (full[:7], why))
    if merging:
        # A real merge commit is needed (not a fast-forward): --no-commit stopped short of
        # making it so the message can carry the Co-Authored-By trailer.
        path = merge_msg_path(top)
        if not path:
            # '' is no answer from `rev-parse --git-path` (a git ended from outside included):
            # opening it would raise. The merge stands uncommitted; say so.
            return done(FAILED, 'refused: git merge --no-commit %s left a merge to commit, but '
                                'where git keeps its message (MERGE_MSG) could not be read. '
                                'Nothing was committed and the merge is left in progress: '
                                '`git status` shows it. Tell the coordinator.' % full[:7])
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            merge_msg = f.read().rstrip('\n')
        msg = merge_msg + '\n\n' + attribution_line + '\n'
        rc, out, err = _git(top, 'commit', '-q', '-F', '-',
                            stdin=msg.encode('utf-8'))
        if rc != 0:
            return done(FAILED, 'commit failed: %s' % (err or out or 'git commit exited %s' % rc))
    # else: the merge fast-forwarded already (no new commit of ours, so no trailer to add).
    branch = gitrefs.current_branch(top)[0] or 'HEAD'
    lines.append('merged %s into %s (%s -> %s)' % (full[:7], branch, before,
                                                   _git(top, 'rev-parse', '--short', 'HEAD')[1]))
    rc, names, err = _git(top, 'diff', '--name-only', '-z', old_mb, full)
    if rc != 0:
        return done(FAILED, 'REGROWTH CHECK FAILED - `git diff --name-only` failed (%s), so the '
                            'merge is NOT checked.' % err,
                    'rerun the check by hand or ask the coordinator; do not gate on this merge '
                    'until then.')
    found = []
    with CatFile(top, check=True) as cat:
        for f in [n for n in names.split('\0') if n]:
            was, now_base, merged = _size(cat, old_mb, f), _size(cat, full, f), _size(cat, 'HEAD', f)
            if regrown(was, now_base, merged):
                found.append('  %s: base shrank it %d -> %d bytes, your merge result is %d'
                             % (f, was, now_base, merged))
    if cat.dead:
        return done(FAILED, 'REGROWTH CHECK FAILED - the git size reader died (%s), so the merge '
                            'is NOT checked.' % (getattr(cat, 'dead_reason', None) or 'see stderr'),
                    'rerun the check by hand or ask the coordinator; do not gate on this merge '
                    'until then.')
    if found:
        return done(FAILED, 'REGROWTH - the base deleted text and this merge brought it back (no '
                            'conflict was reported):', *found,
                    'fix: `git checkout %s -- <path>`, re-apply only your own change, commit. '
                    'That checkout discards an uncommitted edit of <path>: this work tree had '
                    'none when the merge ran; commit any made since before running it.'
                    % full[:7])
    return done(OK, 'no regrowth. Now rerun your gate: %s' % kitroot.command('gate', top))
