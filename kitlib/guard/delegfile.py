"""Who may change the owner's delegation list (docs/guard.md "Owner machine setup").

  owner-delegations  a write, move or delete of the owner's delegation list by any session but
                     the owner's own (rules.is_owner_session): every subagent and every marked
                     main session, plain included, as owner-tool holds them.

The list says which owner operations an agent may run for the owner (docs/delegations.md), so an
agent that could change it could grant itself any of them. It has two copies, each held:

  - the home one, `kitlib.delegations.delegations_file(ctx.home)`
    (`~/.pm-kit/owner-delegations.json`), the file the loader reads;
  - the PM repository's, `<pointer>/.pm-kit/owner-delegations.json`, where <pointer> is the
    PM-repo pointer's value as stored (Ctx.pm_repo_pointer, kitlib.config.pointer.read_raw). A
    value the pointer check refuses is still held (fail closed); with no pointer only the home
    copy is.

What counts as a change (the guard catches honest mistakes, not evasion):

  - edit         a file-tool edit, and every write target the shell rules place (rules.edit_target,
                 from rules._write_from), of the file or of a folder that holds it (a delete or
                 move of ~/.pm-kit or of the PM repository). A stand-in target (the folder a
                 program runs in, rules.here) counts only as the file, never as a folder: python
                 run in the PM repository is not a write of the list.
  - shell        the same write targets read as hub-secrets reads paths (shell.home_text: `~` and
                 the home variables; a relative path after a cd home, cmd.cwd_home); python or
                 node code given on the line (rules.inline_code) that names the list and writes,
                 moves or deletes a file (WRITE_RE); and git in the pointer's work tree whose
                 pathspec reaches the file (rm, mv, restore, checkout-index), that switches or
                 checks out (checkout, switch), that resets files (reset --hard, --merge, --keep),
                 that cleans or stashes (clean, stash), that updates the work tree from an index
                 (read-tree -u), and a `worktree remove` or `move` of that work tree from any
                 place. merge, pull, rebase, cherry-pick, revert, am and apply are left to the
                 owner-only commit check (docs/guard.md "Owner-only commits").

Reads stay open: a Read or Grep, cat, Get-Content, git show, python that only reads, a copy of the
list to elsewhere (its source is not written).
"""
import os
import re

from kitlib.config import user
from kitlib import delegations
from kitlib.git import repo

from . import jail
from . import rules
from . import shell as sh

RULE = 'owner-delegations'
WHY = ('the owner\'s delegation list says which owner operations an agent may run for the owner '
       '(docs/delegations.md), so only the owner changes it.')
ASK = 'Ask the owner (through the PM) to change it. To read it:'

# code that names the list (or the loader's path function)
NAME_RE = re.compile(r'(?i)owner-delegations|delegations_file')
# code that writes, moves or deletes a file: python's open with a writing mode, pathlib's writers,
# os and shutil's file calls, node's fs writers
WRITE_RE = re.compile(
    r'''\bopen\s*\((?:[^()]|\([^()]*\))*?['"][rbtU]*[wax+][rbtwax+U]*['"]'''
    r'''|\.write_(?:text|bytes)\s*\('''
    r'''|\bos\.(?:remove|unlink|rename|renames|replace|truncate|rmdir|removedirs)\s*\('''
    r'''|\.(?:unlink|rename|touch|rmdir)\s*\('''
    r'''|\.replace\(\s*[^,()]+\)'''
    r'''|\bshutil\.\w+\s*\('''
    r'''|\b(?:writeFile|appendFile|unlink|rename|rm|copyFile|truncate|cp)(?:Sync)?\s*\(''')

GIT_RESETS = {'hard', 'merge', 'keep'}


def files(ctx):
    """[path]: the copies of the list this machine holds (module docstring)."""
    out = [delegations.delegations_file(ctx.home)]
    pointer = ctx.pm_repo_pointer()
    if pointer:
        out.append(os.path.join(pointer, user.DIR_NAME, delegations.FILE_NAME))
    return out


def reaches(ctx, target, folders=True):
    """The copy of the list a write of `target` changes: the file itself, or with `folders` a
    copy inside the folder `target` names; None for any other target. Paths are compared as the
    filesystem reaches them (jail.real: links and junctions resolved, case folded)."""
    t = jail.real(target)
    for f in files(ctx):
        r = jail.real(f)
        if t == r or (folders and jail.inside(r, t)):
            return f
    return None


def session(event, ctx):
    return rules.hub_session(event, ctx)


def refusal(event, ctx, role, what, path):
    return rules.refuse(RULE, role, '%s: %s is the owner\'s delegation list, and %s This session is %s. %s' % (
        what, rules.fwd(path), WHY, session(event, ctx), ASK), 'python -m json.tool %s' % rules.quote(path))


def edit(event, ctx, target, role, what, stand_in=False):
    """The rules.edit_target rule: `target` is the list, or (unless `stand_in`) a folder that
    holds it."""
    if not target or rules.is_owner_session(event, ctx):
        return None
    f = reaches(ctx, target, folders=not stand_in)
    return None if f is None else refusal(event, ctx, role, what, f)


def shell(event, ctx, cmd, inv, cwd, role):
    """The rules.shell_command rule: owner-delegations."""
    if rules.is_owner_session(event, ctx):
        return None
    return shell_paths(event, ctx, cmd, inv, role) or inline(event, ctx, cmd, inv, role) or \
        git(event, ctx, cmd, inv, role)


def shell_paths(event, ctx, cmd, inv, role):
    """A write target of `cmd` (rules.write_targets) that reaches the list, read with the home
    folder's spellings and from where a cd home moved the line (as rules.hub_secret_shell reads)."""
    into = rules.copy_into(cmd, inv)
    where = cmd.cwd_home or cmd.cwd
    for w in rules.write_targets(cmd, inv):
        if w.start == w.end or (w.text == '.' and cmd.source[w.start:w.end] != '.'):
            continue                            # a stand-in: the folder a program runs in
        text = sh.home_text(w.text, ctx.home, w.dyn)
        if not text:
            continue
        try:
            p = sh.resolve_path(text, where, cmd.shell, ctx.home)
        except (OSError, ValueError):
            p = None
        if not p:
            continue
        for t in rules.into_folder(p, w, into, where, ctx.home):
            f = reaches(ctx, t)
            if f is not None:
                return refusal(event, ctx, role, '`%s` writes %s' % (rules.bare(cmd), w.text), f)
    return None


def inline(event, ctx, cmd, inv, role):
    """python or node code on the line that names the list and writes, moves or deletes a file."""
    code = '\n'.join(rules.inline_code(cmd, inv))
    if not code or not NAME_RE.search(code) or not WRITE_RE.search(code):
        return None
    return refusal(event, ctx, role, '`%s` runs code that names the list and writes, moves or deletes a file'
                   % rules.bare(cmd), files(ctx)[0])


def git(event, ctx, cmd, inv, role):
    """git that removes or overwrites the PM repository's copy (module docstring)."""
    gc = rules.git_call_judged(inv)
    pointer = ctx.pm_repo_pointer()
    if gc is None or not pointer or gc.sub_dyn or not gc.dir or gc.unknown or gc.blind:
        return None
    copy = files(ctx)[-1]
    sub, args = gc.sub, gc.rest
    text = '`%s`' % rules.bare(cmd)
    if sub == 'worktree':
        if not args or args[0].text not in ('remove', 'move'):
            return None
        pos = rules._pos(args[1:])
        if not pos:
            return None
        target = None if pos[0].dyn else sh.resolve_path(pos[0].text, gc.dir, home=ctx.home)
        if target is None or reaches(ctx, target) is not None:
            return refusal(event, ctx, role, '%s takes away the work tree that holds it' % text, copy)
        return None
    top = tree_top(ctx, gc.dir, pointer)
    if top is None:
        return None
    short, longs, pos = rules._opts(sub, args)
    file_real = jail.real(copy)
    hit = any(covers(ctx, w, gc.dir, top, file_real) for w in pos)
    why = None
    if sub in ('rm', 'mv') and hit:
        why = 'removes or moves it'
    elif sub == 'restore' and hit and not (('S' in short or 'staged' in longs) and
                                           not ('W' in short or 'worktree' in longs)):
        why = 'overwrites it'
    elif sub == 'checkout-index' and (hit or 'a' in short or 'all' in longs):
        why = 'overwrites it'
    elif sub == 'checkout':
        paths_ = after_dashes(args)
        if paths_ is None or any(covers(ctx, w, gc.dir, top, file_real) for w in paths_):
            why = 'may overwrite it with another commit\'s version'
    elif sub == 'switch':
        why = 'may overwrite it with another branch\'s version'
    elif sub == 'reset' and GIT_RESETS & longs:
        why = 'may overwrite it with another commit\'s version'
    elif sub == 'read-tree' and 'u' in short:
        why = 'may overwrite it'
    elif sub == 'clean' and not ('n' in short or 'dry-run' in longs) and (hit or not pos):
        why = 'may delete it'
    elif sub == 'stash' and not (pos[:1] and pos[0].text in ('list', 'show')):
        why = 'may take it away or overwrite it'
    if why is None:
        return None
    return refusal(event, ctx, role, '%s runs in the PM repository %s and %s' % (text, rules.fwd(top), why), copy)


def tree_top(ctx, where, pointer):
    """The pointer's work tree when git run in `where` acts on it, else None: the top git gives
    for `where` is the pointer's folder, or, where git gives none, `where` is inside it."""
    place = ctx.place(where)
    target = jail.real(pointer)
    if place.kind in (repo.MAIN, repo.LINKED) and place.top:
        return pointer if jail.real(place.top) == target else None
    return pointer if jail.inside(jail.real(where), target) else None


def after_dashes(args):
    """The Words after `--`, or None when there is none."""
    texts = [w.text for w in args]
    if '--' not in texts:
        return None
    return list(args[texts.index('--') + 1:])


def covers(ctx, w, base, top, file_real):
    """True when the git pathspec Word `w`, read from `base` (`top` the work tree), can name the
    file whose real path is `file_real`: the file, a folder above it, a glob whose literal folder
    is above it, `:/` and `:(top)` paths from the top; an exclusion (`:!`, `:^`) never; any other
    magic, and a computed word, always (fail closed)."""
    if w.dyn:
        return True
    t = w.text
    if t.startswith(':'):
        if t.startswith((':!', ':^')):
            return False
        if t.startswith(':/'):
            t, base = t[2:], top
        elif t.startswith(':('):
            magic, _sep, rest = t[2:].partition(')')
            words = [m.strip().lower() for m in magic.split(',')]
            if 'exclude' in words:
                return False
            if 'top' in words:
                base = top
            t = rest
        else:
            return True
    if any(c in t for c in '*?['):
        first = min(t.index(c) for c in '*?[' if c in t)
        cut = max(t.rfind('/', 0, first), t.rfind('\\', 0, first))
        t = t[:cut] if cut > 0 else ''
    p = sh.resolve_path(t or '.', base, home=ctx.home)
    if not p:
        return True
    r = jail.real(p)
    return r == file_real or jail.inside(file_real, r)
