"""Has a branch landed? One read-only look, never a wait (ported from the PM kit's
tools/landed.py; docs/landed.md). tools/landed.py is the entry point.

`look(root, ref)` -> Outcome(code, lines). It never loops, sleeps, fetches or writes.

LANDED: the tip is an ancestor of the main branch (`.kit.json` "repo".main_branch) ON THE REMOTE
("repo".remote, read with `git ls-remote`) when the clone has that remote, else, in a clone with
no remote at all, of the local main branch (that remote missing beside others: CANNOT ANSWER). A
remote that cannot be reached falls back to the last fetched `refs/remotes/<remote>/<main>`
and says so. A remote sha this clone has never fetched cannot be judged: not yet, with a hint.

Not landed, it reports:
  lock      the kit landing lock (kitlib.records.landing): free, or HELD with its holder
            (landing.describe); a file there that is no lock record is held.
  gate-ok   whether the tip has a gate-OK record (kitlib.records.gate_ok.read): information only.
  verdict   the newest kit verdict line in the branch's gate logs. The folders read are
            `<log base>/<log_folder(ref)>` and `<log base>/<log_folder('land-' + ref)>` (the
            branch gate's and the landing's merged gate's), recursively, where the log base is
            kitlib.gate.runner.log_base for the checkout -C names, its main checkout and every
            worktree that has the branch checked out (the gate names the base after the checkout
            it ran in unless "repo".name is set). With no such folder, files directly in a log
            base whose name holds log_folder(ref) are read. The newest file (by modification time)
            holding a verdict line decides, its LAST verdict line: VERDICT below. kitlib.gate
            prints these lines to stdout and writes none of them into its step logs, so a verdict
            is found only where a caller saved the gate's output under the log folder.
A failed verdict older than the tip commit is stale (the branch moved on) and not counted.

Exit codes: LANDED 0; NOT_YET 1 (no verdict, a passing one, a landing holding the lock, or the
remote sha unknown here); FAILED 2 (the newest verdict failed and the lock is free), and 2 too when
it cannot answer (CANNOT ANSWER: the ref does not resolve, or git gives no answer for it or for
whether the remote exists; or the configuration was refused).
"""
import collections
import os
import re

from kitlib.config import ConfigError, load
from kitlib.gate import runner
from kitlib.git import cmd, refs, repo
from kitlib.records import gate_ok, landing

LANDED, NOT_YET, FAILED = 0, 1, 2
CANNOT = 2

# The kit's own verdict lines (kitlib.gate): a branch gate's GATE OK / GATE FAILED, a landing's
# LANDED / NOT LANDED / STUCK. `failed` takes part only in a failing one.
VERDICT = re.compile(r'^(?:GATE OK\b|LANDED \S|(?P<failed>GATE FAILED\b|NOT LANDED:|STUCK:))', re.M)
MAX_LOGS = 200
TAIL_BYTES = 256 * 1024
GIT_TIMEOUT = 60

Outcome = collections.namedtuple('Outcome', 'code lines')


def git(root, *args):
    out, _why = cmd.run_text(list(args), root, timeout=GIT_TIMEOUT)
    return out


NO_SUCH_REMOTE = 2      # `git remote get-url <name>` for a remote this clone does not have
NOT_HERE = 'not in this clone'


class NoAnswer(Exception):
    """Git gave no answer to a question look() cannot go on without."""


def remote_main(root, remote, main):
    """`(sha or None, how)`: the remote's main branch ('' when it has none), or None when this
    clone has no remote at all. NoAnswer when git cannot say whether it has the remote, or when
    `remote` is missing but other remotes exist.

    GOTCHA: KIT-GIT-EXIT1-ANSWERS-2 - any failure of `remote get-url` read as "no remote", so a
    git ended from outside (exit 1, no output: refs.KILLED_SHAPE) let the LOCAL main decide and
    could answer LANDED for a branch the remote does not have. Only git's own "No such remote"
    exit (2) means no remote; any other failure is no answer.

    GOTCHA: a remote missing while others exist is a misnamed one (`.kit.json` "repo".remote), and
    the local main must not decide then either: NoAnswer naming the remotes there are, the rule of
    kitlib/backlog/store.py find_remote. Only a clone with no remote at all falls back."""
    r = cmd.call(['remote', 'get-url', remote], root, timeout=GIT_TIMEOUT)
    if r.rc == NO_SUCH_REMOTE:
        names, why = cmd.run_text(['remote'], root, timeout=GIT_TIMEOUT)
        if names is None:
            raise NoAnswer('there is no remote `%s`, and the remotes there are could not be '
                           'listed: %s' % (remote, why))
        if names:
            raise NoAnswer('there is no remote `%s`, but this clone has %s: set .kit.json '
                           '"repo".remote' % (remote, ', '.join(names.split())))
        return None, 'no remote `%s`' % remote
    if r.rc != 0:
        raise NoAnswer('whether this clone has the remote `%s` could not be read: git remote '
                       'get-url exited %s%s' % (remote, r.rc, ': ' + r.err if r.err else
                                                ' and printed nothing'))
    sha, _why = refs.ls_remote(remote, 'refs/heads/' + main, root, timeout=GIT_TIMEOUT)
    if sha is not None:
        return sha, 'ls-remote %s' % remote
    tracked, _ = refs.remote_tip(remote, main, root)
    return (tracked or ''), '%s unreachable: last fetched refs/remotes/%s/%s' % (remote, remote, main)


def contains(root, tip, target):
    """`(True/False, None)`; `(None, NOT_HERE)` when `target` is not a commit this clone has;
    `(None, why)` when git gives no answer (refs.is_ancestor's None, a git ended from outside
    included: KIT-GIT-EXIT1-ANSWERS-2, where `bool()` turned it into a NO)."""
    have, why = refs.resolve(target, root, known=True)
    if have == '':
        return None, NOT_HERE
    if have is None:
        return None, why
    return refs.is_ancestor(tip, target, root)


def log_bases(cfg, root, ref, note=runner.say_note):
    """The gate log bases to read for `ref`, each once, in a stable order: runner.log_base of
    each checkout, then (no "landing".log_dir) runner.fallback_log_base of each, where a gate that
    could not make the default base, or ran before the kit temp moved, wrote. note: as
    runner.log_base's (a machine config that cannot be read)."""
    tops = [root]
    main = repo.main_checkout_toplevel(root)
    if main:
        tops.append(main)
    wts, _why = repo.worktrees(root)
    tops.extend(w.path for w in (wts or []) if w.branch == ref)
    bases = [runner.log_base(cfg, top, runner.repo_name(cfg, top), note) for top in tops]
    if not cfg.section('landing').get('log_dir'):
        for top in tops:
            try:
                bases.append(runner.fallback_log_base(runner.repo_name(cfg, top)))
            except RuntimeError:        # no shared temp to read: the kit temp was read
                pass
    out, seen = [], set()
    for base in bases:
        base = os.path.normpath(base)
        if repo.norm(base) not in seen:
            seen.add(repo.norm(base))
            out.append(base)
    return out


def candidate_logs(bases, ref):
    """Log files for branch `ref`, newest first (at most MAX_LOGS)."""
    key = runner.log_folder(ref)
    found = []
    for base in bases:
        subs = [os.path.join(base, f) for f in (key, runner.log_folder('land-' + ref))]
        subs = [s for s in subs if os.path.isdir(s)]
        for sub in subs:
            for d, _dirs, files in os.walk(sub):
                found.extend(os.path.join(d, f) for f in files)
        if not subs and os.path.isdir(base):
            found.extend(os.path.join(base, f) for f in os.listdir(base)
                         if key in f and os.path.isfile(os.path.join(base, f)))
    dated = []
    for p in found:
        try:
            dated.append((os.path.getmtime(p), p))
        except OSError:
            continue
    return [p for _t, p in sorted(dated, reverse=True)[:MAX_LOGS]]


def last_verdict(paths, verdict=VERDICT):
    """`(path, line, failed, mtime)` of the newest log holding a verdict line, or None."""
    for p in paths:
        try:
            with open(p, 'rb') as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - TAIL_BYTES))
                text = cmd.text(fh.read())
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        hits = list(verdict.finditer(text))
        if hits:
            m = hits[-1]
            line = text[text.rfind('\n', 0, m.start()) + 1:].split('\n', 1)[0].strip()
            return p, line, m.group('failed') is not None, mtime
    return None


def lock_state(lock):
    """`(held, the lock line)`. A file that is no lock record is held, by nobody we can name."""
    try:
        record = lock.read()
    except landing.LockError as e:
        return True, 'lock       HELD: %s cannot be read (%s)' % (lock.path, e)
    if record is None:
        return False, 'lock       free (%s)' % lock.path
    return True, 'lock       %s' % landing.describe(record)


def gate_ok_line(root, tip):
    record, why = gate_ok.read(root, tip)
    if record is not None:
        return 'gate-ok    a gate-OK record for %s (passed %s)' % (tip[:12], record.get('ended_utc'))
    if why == 'none':
        return 'gate-ok    none for %s' % tip[:12]
    return 'gate-ok    unreadable: %s' % why


def look(root, ref):
    """Outcome(code, lines) of one look at `ref` (a branch name or a commit) in `root`'s repository."""
    try:
        cfg = load(root)
        lanes = tuple(cfg.section('landing').get('lanes') or ())
        lock = landing.LandingLock.for_config(root, cfg, lanes=lanes)
    except (ConfigError, landing.LockError) as e:
        return Outcome(CANNOT, ['landed: refused: kit configuration: %s' % e])
    main_branch, remote = cfg.main_branch, cfg.remote
    tip, why = refs.resolve(ref, root, known=True)
    if tip is None:
        return Outcome(CANNOT, ['landed: CANNOT ANSWER: `%s` could not be read in %s: %s'
                                % (ref, root, why)])
    if not tip:
        return Outcome(CANNOT, ['landed: CANNOT ANSWER: `%s` does not resolve to a commit in %s'
                                % (ref, root)])
    out = []
    # known=True: a git ended from outside is no answer, never "no such branch"
    # (KIT-GIT-EXIT1-ANSWERS-2). An unread local main is shown as such and measures nothing.
    local, why = refs.resolve('refs/heads/%s' % main_branch, root, known=True)
    out.append('tip        %s  %s' % (tip[:12], ref))
    out.append('local      %s  %s%s' % ((local or '-')[:12], main_branch,
                                        '' if local else ' (no such branch here)' if local == ''
                                        else ' (UNKNOWN: %s)' % why))
    try:
        target, how = remote_main(root, remote, main_branch)
    except NoAnswer as e:
        return Outcome(CANNOT, out + ['landed: CANNOT ANSWER: %s' % e])
    if target is None:
        target, where = local, 'local %s (%s)' % (main_branch, how)
    else:
        out.append('remote     %s  %s/%s  (%s)' % ((target or '-')[:12], remote, main_branch, how))
        where = '%s/%s' % (remote, main_branch)
    landed, why = contains(root, tip, target) if target else (None if target is None else False,
                                                              None)
    if target is None:
        out.append('contained  UNKNOWN: local %s could not be read' % main_branch)
    elif landed is None and why == NOT_HERE:
        out.append('contained  UNKNOWN: %s is not in this clone - `git fetch %s`, then re-run'
                   % (target[:12], remote))
    elif landed is None:
        out.append('contained  UNKNOWN: whether %s holds %s could not be read: %s'
                   % (where, tip[:12], why))
    else:
        out.append('contained  %s in %s' % ('YES' if landed else 'no', where))
    if landed:
        out.append('LANDED')
        return Outcome(LANDED, out)
    if local and target != local and contains(root, tip, local)[0]:
        out.append('note       merged into local %s, not on %s yet' % (main_branch, where))
    held, line = lock_state(lock)
    out.append(line)
    out.append(gate_ok_line(root, tip))
    by_name = ref != tip and not tip.startswith(ref)
    notes = []
    bases = log_bases(cfg, root, ref, lambda t: t in notes or notes.append(t)) if by_name else []
    out.extend('note       gate logs: %s' % t for t in notes)
    found = last_verdict(candidate_logs(bases, ref)) if by_name else None
    if found is None:
        out.append('verdict    none found under %s' % ', '.join(bases) if by_name
                   else 'verdict    not read: a sha names no branch log')
        out.append('NOT YET')
        return Outcome(NOT_YET, out)
    path, line, failed, mtime = found
    stamp = git(root, 'log', '-1', '--format=%ct', tip)
    stale = failed and bool(stamp) and mtime < int(stamp)
    out.append('verdict    %s  (%s%s)' % (line, path, ', older than the tip: stale' if stale else ''))
    if failed and not stale and not held:
        out.append('FAILED')
        return Outcome(FAILED, out)
    out.append('NOT YET%s' % (' (a landing holds the lock)' if failed and held else ''))
    return Outcome(NOT_YET, out)
