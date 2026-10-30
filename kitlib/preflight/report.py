"""preflight's report: one session's view of the checkout it starts in (docs/preflight.md).

Every reading that fails says so on its own line (`UNKNOWN`, `NOT CHECKED`, `?`), never as a
count of 0 or an `(equal)`: a measurement nobody took is never printed as a result.
"""
import os
import sys

from kitlib import verdict
from kitlib.docs import callers as doc_callers
from kitlib.git import cmd, refs, repo, stray
from kitlib.preflight import (configline, defaultstore, hooks, leftovers, lockline, machine,
                              nextblock, settings as st)

MAX_LISTED = 12
LIST_INDENT = ' ' * 12


def _git(top, *args):
    """git's stripped stdout, or '' when it failed (the callers print '?' for '')."""
    return cmd.run_text(list(args), top)[0] or ''


def _slash(path):
    return path.replace('\\', '/').strip('/')


def _worktree_line(w):
    head = (w.head or '?')[:7]
    where = '[%s]' % w.branch if w.branch else '(bare)' if w.bare else '(detached HEAD)'
    return '%s  %s %s' % (w.path, head, where)


def report(top, cfg, kit_root, base=None, fetch=False, lock_reader=lockline.read_snapshot,
           home=None):
    """The report's lines for the work tree whose top level is `top`. `cfg` is its loaded
    kitlib.config Config (kitlib.config.ConfigError for a "preflight" value preflight cannot
    use). `base` is --base (any revision), else the configured or guessed base branch. `home` is
    the home folder the `config` line reads the machine config under (default the user's)."""
    s = st.settings(cfg)
    out = []
    remote = cfg.remote
    if fetch:
        done, why = cmd.run(['fetch', remote, '--quiet'], top, timeout=120)
        if done is None:
            out.append('fetch     FAILED - git fetch %s: %s' % (remote, why))
    explicit = base
    base, b, ob = st.base_pair(top, cfg, base)
    branch = _git(top, 'rev-parse', '--abbrev-ref', 'HEAD')
    out.append('repo      %s' % top)
    out.append('branch    %s @ %s' % (branch or '?', _git(top, 'rev-parse', '--short', 'HEAD') or '?'))
    # `b and b == ob`, not `b == ob`: when NEITHER ref was read both are '' and compare equal, and
    # the line would report `(equal)` from two readings that were never taken.
    out.append('base      %s @ %s | %s/%s @ %s %s' % (
        base, b or '?', remote, base, ob or '?',
        '(equal)' if b and b == ob else '(DIFFERENT - use --fetch, then tell the coordinator)'))
    behind = None
    measure = st.measure_rev(cfg, base, b, ob, explicit)
    if branch != base:
        lr = _git(top, 'rev-list', '--left-right', '--count', '%s...HEAD' % measure).split()
        out.append('vs base   merge-base %s | behind %s, ahead %s%s%s' % (
            _git(top, 'merge-base', measure, 'HEAD')[:7] or '?', lr[0] if lr else '?',
            lr[1] if len(lr) > 1 else '?',
            '' if measure == base else ' (against %s/%s)' % (remote, base),
            '  <- merge the hash the coordinator names before landing' if lr and lr[0] != '0' else ''))
        behind = lr[0] if lr else '?'
    # untracked=normal: a new folder is one `?? dir/` entry, as plain `git status` counts it.
    dirty, _why = refs.status(top, untracked='normal')
    out.append('dirty     %s' % ('%d file(s)' % len(dirty) if dirty is not None
                                 else 'UNKNOWN - git status failed here'))
    out.extend(LIST_INDENT + '%s %s' % e for e in (dirty or [])[:MAX_LISTED])
    wts, _why = repo.worktrees(top)
    out.append('worktrees %s' % (len(wts) if wts is not None
                                 else 'UNKNOWN - git worktree list failed here'))
    out.extend(LIST_INDENT + _worktree_line(w) for w in (wts or [])[:MAX_LISTED])
    out.extend(leftovers.lines(top))
    # The kit's own tools folder: a repository holds no copy of the kit's tools (decision 21 in
    # docs/multi-host-plan.md section 7), so every command printed runs them from the kit, with -C.
    tools = kit_root.replace('\\', '/').rstrip('/') + '/tools'
    out.extend(defaultstore.lines(top, cfg, tools))
    here = '"%s"' % top.replace('\\', '/')
    in_main = repo.classify(top).kind == repo.MAIN
    if in_main:
        out.append('NOTE: this is the MAIN checkout %s. Agents: create your worktree (%s -C %s '
                   '<area>/<slug>) and edit only under %s/<slug>/.'
                   % (top, nextblock.command(tools, 'new_worktree'), here,
                      _slash(cfg.repo['worktrees_dir'])))
    budget = os.path.join(kit_root, 'tools', 'doc_budget.py')
    if os.path.isfile(budget):
        rc, o, e = machine.run([sys.executable, budget, '-C', top], timeout=120)
        # Green is asserted, never inferred from the last line: exit 0 AND the OK line AND no
        # FAILED line (kitlib.verdict). The tail is quoted after the verdict as a guide.
        out.append(verdict.line('docs', rc, o + ('\n' + e if e else '')))
    else:
        out.append('docs      NOT CHECKED - no tools/doc_budget.py in the kit, so nothing was measured')
    if not doc_callers.switched_off(cfg):
        di = doc_callers.tool(kit_root)
        if os.path.isfile(di):
            rc, o, e = machine.run([sys.executable, di, '-C', top], timeout=120)
            out.append(doc_callers.line(rc, o + ('\n' + e if e else ''))[0])
        else:
            out.append(doc_callers.not_checked())
    _code, stray_lines = stray.check(top)
    out.extend(stray_lines)
    out.extend(hooks.lines(os.path.join(top, cfg.tools_dir), s['report_hooks']))
    out.extend(machine.preflight_lines(s))
    out.append(configline.line(home))
    out.append(configline.pointer_line())
    # The default names a file; with no "preflight".traps set and no such file, there is no line.
    if (cfg.section(st.SECTION)['traps'] is not None
            or os.path.isfile(os.path.join(top, st.DEFAULT_TRAPS_FILE))):
        out.append('traps     %s' % s['traps'])
    index = cfg.section('doc_index')['index']
    gone = '' if os.path.isfile(os.path.join(top, index)) else ' (none: the doc index is opt-in)'
    out.append('index     docs: %s%s | a doc section: %s -C %s --section "<title>"'
               % (index, gone, nextblock.command(tools, 'doc_index'), here))
    out.extend(nextblock.lines(top, tools, branch, base, cfg.main_branch, behind, dirty or [],
                               in_main))
    out.append('lock      %s' % lockline.line_for(kit_root, lock_reader))
    return out


# The short report (the default; --full prints `report` whole). A line is dropped only when its
# label is one of QUIET and it carries none of PROBLEM; every other line, including any label added
# later, is kept.
QUIET = ('worktrees', 'dirty', 'docs', 'doc index', 'config', 'pointer', 'index', 'lock', 'daily',
         'main checkout clean', 'next', 'traps', 'engines', 'ports', 'hook', 'leftovers')
PROBLEM = ('FAIL', 'NOT CHECKED', 'UNKNOWN', 'WARN', 'DIFFERENT', 'RED', 'STALE', 'BROKEN', 'refused', 'unknown (')


def _label(line):
    head = line[:10].strip()
    if line.startswith('main checkout clean'):
        return 'main checkout clean'
    return head


def brief(lines, kit_root):
    """The short report: `lines` (from `report`) without the readings that are fine and that a
    builder does not use, then a `kit` line (the kit's tools folder) and the way to the rest. A
    non-zero `dirty` count stays, with its file list."""
    out = []
    keep = True
    for line in lines:
        if line[:1] != ' ':
            label = _label(line)
            quiet = label in QUIET and not any(m in line for m in PROBLEM)
            if label == 'dirty' and not line.startswith('dirty     0 '):
                quiet = False
            keep = not quiet
        if keep:
            out.append(line)
    out.append('kit       %s' % (kit_root.replace(chr(92), '/').rstrip('/')))
    out.append('          (only problems shown; preflight.py --full prints every check)')
    return out
