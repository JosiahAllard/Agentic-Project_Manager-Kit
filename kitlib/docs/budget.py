"""Byte budgets for tracked markdown, stale budget keys and retired paths: the doc budget check
(from the PM kit's tools/doc_budget.py; docs/doc-tools.md "doc_budget").

`run(root, out, base=None, head='HEAD', worktree=False, report=False)` returns the exit status
and hands every line to `out`:
  (default)          `head` (HEAD) against `base` (default: docs/gate.md "Default base")
  head=<branch>      a branch against the base
  worktree=True      the files ON DISK, uncommitted and new ones included
  report=True        every .md with its size and budget; its first line says which tree

Config: `.kit.json` "doc_budget" (docs/kit-json.md): "budgets" by path (a key with `*`, `?` or `[`
is a glob, matched in the order written, after the literal keys; `null` is uncapped), "default"
for every other tracked .md (none: uncapped), "uncapped_dirs", "capped" (when set, the closed list
of paths that may carry a budget, each an integer, and no "default"), "retired_paths" (globs that
must not exist), "state_docs".

A file fails its budget when its size on the branch (or on disk) is over it and bigger than on the
base: a file already over may shrink or hold, never grow. A file the branch did not change since
the merge base never fails it. The budgets applied are the head's (on disk under `worktree`).
Nothing projects the merge: the caps are checked on the real merge, by the landing gate of a repo
whose gate runs this check as a `cmd` step (docs/gate.md "Not ported").

A configuration that does not read (a key written twice, an unknown key, a bad type) refuses on
disk (exit 2); at the head it fails the branch (exit 1) unless the base carries the same fault,
which is a WARNING so the branch removing it can land.

Owner-only paths are judged by commit.py and kitlib.guard.ownercommits (landing and pre-push), and
the doc index by kitlib.docs.index (tools/doc_index.py), not here.

Every check runs in one pass; a failure prints its block and the LAST line names every failed
check. A capped file over its budget that this run did not fail is a WARNING, counted on the OK
line. Exit status: 0 clean (`DOC BUDGET OK (...)`), 1 a check failed (`DOC BUDGET FAILED: ...`),
2 usage, configuration or git error.
"""
import collections
import fnmatch
import os

from kitlib import config
from kitlib.config.types import is_note
from kitlib.git import cmd
from kitlib.git import refs as gitrefs

SECTION = 'doc_budget'
GIT_TIMEOUT = 120                # seconds; a large tree's listing is slow, not hung

Git = collections.namedtuple('Git', 'returncode stdout stderr')


class _Exit(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def git(root, *args):
    """`git <args>` in `root` as Git(returncode, stdout text, stderr text); returncode None when git
    did not run (not found, timed out), which run() turns into a refusal (exit 2)."""
    r = cmd.call(list(args), root, timeout=GIT_TIMEOUT)
    return Git(r.rc, cmd.text(r.out), r.err)


def failure(r):
    """A failed git call's exit code and first stderr line, for the refusal that names it."""
    stderr = (r.stderr or '').strip().splitlines()
    return 'git exit %s: %s' % (r.returncode, stderr[0] if stderr else '(nothing on stderr)')


def sizes_from(stdout):
    """{path: bytes} for the blobs in one `ls-tree -r --long -z` listing. Pure."""
    out = {}
    for record in stdout.split('\0'):         # -z: paths are raw, never quoted or escaped
        meta, _, path = record.partition('\t')
        fields = meta.split()
        if len(fields) == 4 and fields[1] == 'blob':
            out[path] = int(fields[3])
    return out


def is_glob(key):
    return any(c in key for c in '*?[')


def stale_budget_keys(cfg, files):
    """The "budgets" keys and "state_docs" entries of `cfg` naming no path in `files`, sorted and
    de-duplicated; a glob key is never stale. Pure."""
    present = set(files)
    named = set(cfg.get('budgets', {})) | set(cfg.get('state_docs', []))
    return sorted(k for k in named if k not in present and not is_glob(k))


def budget_for(cfg, path):
    """The budget for `path`: an int, or None if uncapped. A literal "budgets" entry (an explicit
    `null` included) wins; else the first glob key matching `path`, in the order written; else a
    path under "uncapped_dirs" is uncapped; else "default" (none: uncapped)."""
    budgets = cfg.get('budgets', {})
    if path in budgets:
        return budgets[path]
    for key, budget in budgets.items():
        if is_glob(key) and fnmatch.fnmatch(path, key):
            return budget
    for d in cfg.get('uncapped_dirs', ()):
        if path == d or path.startswith(d + '/'):
            return None
    return cfg.get('default')


def config_errors(cfg):
    """What is wrong with "capped" against the rest of the section, as sentences (empty when it is
    usable): with "capped" set, a "default", "budgets" naming anything else, or a capped path
    without an integer cap. Key names and types are the schema's (kitlib.config.schema)."""
    errors = []
    budgets = cfg.get('budgets', {})
    capped = cfg.get('capped')
    if capped is not None:
        if 'default' in cfg:
            errors.append('"default" is gone: only %s are capped' % ', '.join(capped))
        for path in sorted(set(budgets) - set(capped)):
            errors.append('"budgets" names %s: only %s are capped' % (path, ', '.join(capped)))
        for path in capped:
            v = budgets.get(path)
            if not isinstance(v, int) or isinstance(v, bool):
                errors.append('"budgets" %s must be an integer byte cap, not %r' % (path, v))
    return errors


def budget_verdict(path, budget, head, base, head_label='branch', base_label='main'):
    """One file's report, or None if it may land: over `budget` at `head` bytes (the branch, or the
    disk) and bigger than the `base` bytes, so a file already over may hold or shrink, never grow.
    `budget` None is uncapped: None before any arithmetic."""
    if budget is None or head <= budget or head <= base:
        return None
    return ('  %s: over budget on the %s by %d bytes.\n'
            '      budget %d | %s %d | %s %d'
            % (path, head_label, head - budget, budget, head_label, head, base_label, base))


def section_of(conf):
    """The "doc_budget" section of a Config as written (Config.raw: no defaults merged, so an
    unset key stays unset), with note keys (`_...`, `comment`) dropped from it and from its
    "budgets", so a note is never a budget, a stale key or a path outside "capped"."""
    raw = {k: v for k, v in conf.raw(SECTION).items() if not is_note(k)}
    if isinstance(raw.get('budgets'), dict):
        raw['budgets'] = {k: v for k, v in raw['budgets'].items() if not is_note(k)}
    return raw


def _cfg_at(root, rev):
    """(Config, None) at `rev`, or (None, why) when it does not read; `why` drops the
    `<rev>:.kit.json: ` prefix so two revisions' faults compare as text."""
    try:
        return config.load_at(root, rev), None
    except config.ConfigError as e:
        why = str(e)
        prefix = '%s:%s: ' % (rev, '.kit.json')
        return None, why[len(prefix):] if why.startswith(prefix) else why


def run(root, out, base=None, head='HEAD', worktree=False, report=False):
    """The doc budget check of the repository at `root`; every line to `out`; the exit status."""
    failures = []
    try:
        return _check(root, out, base, head, worktree, report, failures)
    except _Exit as e:
        return e.code
    except Exception:
        # A crash after a failure still prints the failure, then the traceback: never an OK.
        for _name, block in failures:
            out('\n'.join(block))
        raise


def _check(root, out, base, head, worktree, report, failures):
    def g(*args):
        r = git(root, *args)
        if r.returncode is None:
            out('doc_budget: `git %s` could not run: %s' % (' '.join(args[:2]), r.stderr))
            raise _Exit(2)
        return r

    def stop(msg, code=2):
        out(msg)
        raise _Exit(code)

    try:
        disk = config.load(root)
        base_note = None
        if not base:
            base, base_note = gitrefs.default_base(root, disk.remote, disk.main_branch)
        cfg = section_of(disk)
    except config.ConfigError as e:
        stop('doc_budget: refused: kit configuration: %s' % e)
    rel = disk.section_file(SECTION)
    label = rel if rel else '.kit.json "%s"' % SECTION
    bad_cfg = config_errors(cfg)
    if bad_cfg:
        stop('doc_budget: %s: %s.' % (label, '; '.join(bad_cfg)))
    retired = cfg.get('retired_paths', [])
    if report:
        # The FIRST line, before any WARNING: which tree is measured.
        out('measuring %s' % ('the working tree, uncommitted and new files included' if worktree
                              else '%s as committed; for uncommitted edits run --worktree --report'
                              % head))
    if base_note:
        out('doc_budget: %s' % base_note)

    # A base that does not resolve would make every comparison below empty, and an empty one passes.
    # known=True: '' is git's "no such commit"; None is no answer, a git ended from outside
    # included (KIT-GIT-EXIT1-ANSWERS-2: `rev-parse --verify --quiet` said "no such commit" as exit
    # 1 with no output, a killed git's shape, gitrefs.KILLED_SHAPE).
    verified, why = gitrefs.resolve(base, root, known=True)
    if verified == '':
        stop('doc_budget: base ref %s does not exist here. Pass --base <ref>.' % base)
    if verified is None:
        stop('doc_budget: could not read base ref %s (%s).' % (base, why))

    def sizes_or_why(ref):
        r = g('ls-tree', '-r', '--long', '-z', ref)
        if r.returncode != 0:
            return None, failure(r)
        return sizes_from(r.stdout), None

    def sizes_or_refuse(ref):
        sizes, why = sizes_or_why(ref)
        if sizes is None:
            stop('doc_budget: cannot list the sizes at %s (%s).' % (ref, why))
        return sizes

    if worktree:
        listing = g('ls-files', '-co', '--exclude-standard')
        files = [p for p in listing.stdout.splitlines() if os.path.isfile(os.path.join(root, p))]

        def size(path):
            # LF-normalised, so a CRLF checkout measures like the committed blob
            with open(os.path.join(root, path), 'rb') as fh:
                return len(fh.read().replace(b'\r\n', b'\n'))
        where = 'the working tree'
    else:
        listing = g('ls-tree', '-r', '--name-only', head)
        files = listing.stdout.splitlines()
        head_sizes = sizes_or_refuse(head) if listing.returncode == 0 else {}

        def size(path):
            return head_sizes.get(path, 0)
        where = head
    if listing.returncode != 0:
        stop('doc_budget: cannot list %s (%s).' % (where, failure(listing)))
    if not files:
        stop('doc_budget: cannot list %s (git exit 0, but it listed no file).' % where)

    head_ref = 'HEAD' if worktree else head
    head_label = 'worktree' if worktree else 'branch'
    mb_read = g('merge-base', base, head_ref)
    mb = mb_read.stdout.strip()
    if mb_read.returncode == 1 and not (mb_read.stderr or '').strip():
        # Exit 1 with nothing on stderr is both "no common history" and a git ended from outside.
        out('doc_budget: WARNING: git merge-base %s %s exit 1, nothing on stderr: no common '
            'history, or a git ended from outside; measuring against %s itself.'
            % (base, head_ref, base))
        mb = base
    elif mb_read.returncode != 0 or not mb:
        stop('doc_budget: cannot find the merge base of %s and %s (%s).'
             % (base, head_ref, failure(mb_read) if mb_read.returncode
                else 'git exit 0, but it printed nothing'))
    base_sha = verified
    touched = None                      # None: up to date with the base, so every file is the branch's
    if mb not in (base_sha, base):
        mb_sizes, why = sizes_or_why(mb)
        if not mb_sizes:
            stop('doc_budget: cannot list the merge base %s of %s and %s (%s).'
                 % (mb[:12], base, head_ref, why or 'git exit 0, but it listed no file'))
        # What the branch itself changed since the merge base: a file it did not change cannot fail
        # it. A file absent at the merge base (an untracked one under `worktree` too) is its own.
        d = g('diff', '--name-only', '--no-renames', '-z', mb, *(() if worktree else (head,)))
        if d.returncode != 0:
            stop('doc_budget: cannot diff %s against the merge base %s (%s).'
                 % (head_ref, mb[:12], failure(d)))
        touched = set(d.stdout.split('\0')) | (set(files) - set(mb_sizes))

    def conf_at(ref):
        # A git read that FAILED (not a config that does not read) refuses: warning past it would
        # measure by the branch's budgets for a reason that is nobody's fault in the config.
        conf, err = _cfg_at(root, ref)
        if err and err.startswith('git show ') and ' failed: ' in err:
            stop('doc_budget: cannot read the kit configuration at %s (%s).' % (ref, err))
        return conf, err

    # A config fault the BRANCH brings at its head fails it; one the base already carries warns,
    # so the branch removing it can land.
    _base_conf, base_err = conf_at(base)
    if base_err:
        out('doc_budget: WARNING: cannot read the kit configuration at %s (%s). Not this branch\'s '
            'doing, so not failed here - fix it.' % (base, base_err))
    head_cfg = cfg
    if not worktree:
        head_conf, err = conf_at(head)
        if err and err != base_err:
            stop('DOC BUDGET FAILED: the kit configuration in %s does not read: %s. Fix it on the '
                 'branch.' % (head, err), 1)
        # A head with no "doc_budget" section measures by this checkout's.
        head_cfg = (section_of(head_conf) if head_conf else {}) or cfg

    base_sizes = sizes_or_refuse(base)
    over, held = [], {}
    for path in files:
        if not path.endswith('.md'):
            continue
        budget = budget_for(head_cfg, path)
        now, was = size(path), base_sizes.get(path, 0)
        if report:
            out('%8d / %8s  %s' % (now, 'uncapped' if budget is None else str(budget), path))
        bad = None
        if touched is None or path in touched:
            bad = budget_verdict(path, budget, now, was, head_label, base)
        if bad:
            over.append(bad)
        elif budget is not None and now > budget:
            held[path] = ('doc_budget: WARNING: %s is over its budget by %d bytes (budget %d, %s %d);'
                          ' not grown here, so not failed - trim it.'
                          % (path, now - budget, budget, head_label, now))

    # Every check below runs and each failure is collected, so one pass prints the whole verdict
    # and its last line names every failed check.
    if over:
        capped = sorted(k for k, v in head_cfg.get('budgets', {}).items() if v is not None)
        failures.append(('over budget', [
            'DOC BUDGET FAILED: over budget and grew on the %s.' % head_label] + over + [
            'Only %s are capped. Cut entries to keywords, move history into the commit message. '
            'Do not raise the budget to pass - no agent raises a cap, for any doc; if nothing here '
            'can go, stop and report to the owner.' % (', '.join(capped) or 'the budgets\' paths')]))

    gone = sorted(p for p in files if any(fnmatch.fnmatch(p, gl) for gl in retired))
    if gone:
        failures.append(('retired path', [
            'DOC BUDGET FAILED: retired path(s) exist on %s:' % where] + ['  ' + p for p in gone] + [
            'The config\'s "retired_paths" retires them: a retired doc stays gone. Put what is '
            'still true where the config\'s owner moved it, and delete the file.']))

    stale = stale_budget_keys(head_cfg, files)
    if stale:
        failures.append(('stale budget key', [
            'DOC BUDGET FAILED: %s "budgets"/"state_docs" names no file on %s (moved or deleted? '
            'rename the entry with the file, or delete it):' % (label, where)]
            + ['  ' + k for k in stale]))

    for line in held.values():
        out(line)
    n_md = sum(1 for p in files if p.endswith('.md'))
    if failures:
        for _name, block in failures:
            out('\n'.join(block))
        out('DOC BUDGET FAILED: %d check(s) failed (%s, %d .md files): %s.'
            % (len(failures), where, n_md, ', '.join(name for name, _block in failures)))
        return 1
    out('DOC BUDGET OK (%s, %d .md files%s)'
        % (where, n_md, '; %d over cap, held - see WARNING' % len(held) if held else ''))
    return 0
