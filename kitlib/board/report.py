"""The board's lines for one checkout (docs/preflight.md "board.py").

A dashboard cannot refuse, so every cell is a READING: a git command that failed prints `?` (or
`? (git diff failed)`), never a value. `-` means "no owner-only paths" and nothing else, `0` means
"counted, none dirty", and `has base tip` is `yes`/`NO` only when git answered. A half-deleted
worktree is the live case: every git command in it fails, and its row would otherwise read clean.
"""
import os
import sys

from kitlib.git import cmd, repo
from kitlib.git import refs as gitrefs
from kitlib.preflight import lockline, machine, settings as st

HEADER = 'branch | tip | ahead/behind | has base tip | last commit | dirty | owner-only touched'


def _git(cwd, *args):
    """(rc or None, stripped stdout text)."""
    r = cmd.call(list(args), cwd)
    return r.rc, cmd.text(r.out).strip()


def _own_tree(path):
    """True when git, asked in `path`, answers for the work tree AT `path`. A half-deleted
    worktree nested in the main checkout (the kit layout, .claude/worktrees/<slug>) has no .git of
    its own, so git walks up and answers for the main checkout: every cell would read clean."""
    rc, top = _git(path, 'rev-parse', '--show-toplevel')
    return rc == 0 and bool(top) and repo.norm(top) == repo.norm(path)


def row(w, base, rules):
    """One worktree's row against `base`."""
    if not _own_tree(w.path):
        return '%s | %s | +? -? | ? | ? | ? | ? (not its own work tree: git cannot read it)' % (
            w.branch, (w.head or '?')[:7])
    rc, lr_out = _git(w.path, 'rev-list', '--left-right', '--count', '%s...HEAD' % base)
    lr = lr_out.split() if rc == 0 else []
    # Only True and False are ANSWERS (contained / not); no answer (an unknown rev, a failed git, a
    # git ended from outside: kitlib.git.refs.is_ancestor) reads `?`.
    has = {True: 'yes', False: 'NO'}.get(gitrefs.is_ancestor(base, 'HEAD', w.path)[0], '?')
    rc, age = _git(w.path, 'log', '-1', '--format=%cr')
    age = age if rc == 0 else '?'
    rc, status_out = _git(w.path, 'status', '--porcelain', '--untracked-files=no')
    dirty = str(len(status_out.splitlines())) if rc == 0 else '?'
    rc, diff_out = _git(w.path, 'diff', '--name-only', '--no-renames', '%s...HEAD' % base)
    locked = [p for p in diff_out.splitlines() if p and rules.is_locked(p)]
    locked = (', '.join(locked) or '-') if rc == 0 else '? (git diff failed)'
    return '%s | %s | +%s -%s | %s | %s | %s | %s' % (
        w.branch, (w.head or '?')[:7], lr[1] if len(lr) > 1 else '?', lr[0] if lr else '?',
        has, age, dirty, locked)


def docs_line(top, kit_root):
    """`docs: <the stale report's last line>` from the kit's doc_index.py --stale."""
    tool = os.path.join(kit_root, 'tools', 'doc_index.py')
    if not os.path.isfile(tool):
        return 'docs: NOT CHECKED - no tools/doc_index.py in the kit'
    rc, out, _err = machine.run([sys.executable, tool, '--stale', '-C', top], timeout=120)
    stale = [l for l in out.splitlines() if l.strip()]
    return 'docs: %s%s' % ('' if rc == 0 else 'doc_index --stale FAILED (exit %d) ' % rc,
                           stale[-1].strip() if stale else 'no stale report')


def lines(top, cfg, kit_root, base=None, lock_reader=lockline.read_snapshot):
    """The board's lines for the repository whose work tree `top` is (`cfg` its loaded Config;
    kitlib.config.ConfigError for a "preflight" value it cannot use)."""
    s = st.settings(cfg)
    rules = cfg.owner_rules()
    remote = cfg.remote
    explicit = base
    base, b, ob = st.base_pair(top, cfg, base)
    measure = st.measure_rev(cfg, base, b, ob, explicit)
    out = ['%s @ %s | %s/%s @ %s %s' % (
        base, b or '?', remote, base, ob or '?',
        '(equal)' if b and b == ob else '(DIFFERENT%s)' % (
            '' if measure == base else '; rows measured against %s/%s' % (remote, base))),
           HEADER]
    wts, why = repo.worktrees(top)
    if wts is None:
        out.append('(worktrees UNKNOWN - %s)' % why)
    else:
        rows = [row(w, measure, rules) for w in wts
                if w.branch and w.branch != base and os.path.isdir(w.path)]
        out += rows or ['(no worktrees besides the main checkout)']
    out.append('machine: lock %s' % lockline.line_for(kit_root, lock_reader))
    out.append(docs_line(top, kit_root))
    line = machine.board_line(s)
    if line:
        out.append(line)
    return out


def state_file(cfg):
    """`.kit.json` "board".state_file, or None when unset (the schema checks the value)."""
    return cfg.section('board').get('state_file')
