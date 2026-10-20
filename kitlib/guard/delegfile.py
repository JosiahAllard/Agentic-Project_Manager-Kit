"""Who may change the owner's delegation list (docs/guard.md "Owner delegation list").

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
                 move of ~/.pm-kit or of the PM repository). A stand-in target whose writes the
                 guard does not read (rules.unread_write: python's code and the like) counts only
                 as the file, never as a folder: python run in the PM repository is not a write
                 of the list.
  - shell        the same write targets read as hub-secrets reads paths (shell.home_text: `~` and
                 the home variables; a relative path after a cd home, cmd.cwd_home); a wildcard
                 that can match the list or a folder above it (glob_reaches); the folder a
                 program that writes there runs in (tar -x, unzip, curl -O: a stand-in, rules.here)
                 when it holds the list; the paths the commands before a | give a writer that
                 takes them from the pipeline or from xargs (upstream); python or
                 node code given on the line (rules.inline_code) that names the list and writes,
                 moves or deletes a file (WRITE_RE); and git in the pointer's work tree whose
                 pathspec reaches the file (rm, mv, restore, checkout-index), that switches or
                 checks out (checkout, switch), that resets files (reset --hard, --merge, --keep),
                 that cleans or stashes (clean, stash), that updates the work tree from an index
                 (read-tree -u), sparse-checkout and bisect that change it, and a `worktree
                 remove` or `move` of that work tree from any place. merge, pull, rebase,
                 cherry-pick, revert, am and apply are left to the owner-only commit check
                 (docs/guard.md "Owner-only commits").

Reads stay open: a Read or Grep, cat, Get-Content, git show, python that only reads, a copy of the
list to elsewhere (its source is not written).
"""
import fnmatch
import os
import re

from kitlib.config import user
from kitlib import delegations
from kitlib.git import repo

from . import hub as hubg
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
# the git sparse-checkout and bisect subcommands that leave the work tree as it is
SPARSE_READS = {'list', 'check-rules'}
BISECT_READS = {'log', 'visualize', 'view', 'terms', 'help'}


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
    folder's spellings and from where a cd home moved the line (as rules.hub_secret_shell reads):
    a path, a wildcard path that can match the list or a folder above it (glob_reaches), the
    folder a program that writes there runs in (a stand-in, rules.here: tar -x, unzip, curl -O),
    and the paths the command before a | gives a writer that takes them from the pipeline or from
    xargs (upstream)."""
    into = rules.copy_into(cmd, inv)
    where = cmd.cwd_home or cmd.cwd
    for w in rules.write_targets(cmd, inv):
        f, what = None, None
        if rules.pipe_stand_in_word(cmd, w) or (here_word(cmd, w) and inv is not None and
                                               inv.via == 'xargs'):
            f = upstream(ctx, cmd)
            what = '`%s` writes the paths the command before its | gives it' % rules.bare(cmd)
        elif w.start == w.end:
            continue                            # a word the guard does not read
        elif here_word(cmd, w):
            if rules.unread_write(cmd, inv, w) or not where:
                continue                        # python's code and the like: what it writes is not read
            f = reaches(ctx, where)
            what = '`%s` writes files in the directory it runs in, %s' % (rules.bare(cmd), rules.fwd(where))
        else:
            f = word_reaches(ctx, w, where, cmd.shell, into)
            what = '`%s` writes %s' % (rules.bare(cmd), w.text)
        if f is not None:
            return refusal(event, ctx, role, what, f)
    return None


def here_word(cmd, w):
    """True when the write `w` is a stand-in for the folder the program runs in (rules.here)."""
    return w.text == '.' and not w.dyn and cmd.source[w.start:w.end] != '.'


def word_reaches(ctx, w, where, shell, into=None):
    """The copy of the list a write of the path Word `w`, read from `where`, reaches (reaches,
    glob_reaches); None when it reaches none or the guard cannot place it."""
    text = sh.home_text(w.text, ctx.home, w.dyn)
    if not text:
        return None
    if hubg.has_glob(text):
        return glob_reaches(ctx, text, where, shell)
    try:
        p = sh.resolve_path(text, where, shell, ctx.home)
    except (OSError, ValueError):
        p = None
    if not p:
        return None
    for t in rules.into_folder(p, w, into, where, ctx.home):
        f = reaches(ctx, t)
        if f is not None:
            return f
    return None


def glob_reaches(ctx, text, where, shell):
    """The copy of the list the wildcard path `text`, read from `where`, can name: the file or a
    folder above it, each part of the wildcard matched (case folded, a leading dot like any
    other character: fail closed) against the matching part of the copy's path below the literal
    folder the wildcard starts in; `**` or `..` after the wildcard, any copy below that folder.
    None when it can name none, or the guard cannot place that folder."""
    first = min(text.index(c) for c in hubg.GLOB_CHARS if c in text)
    cut = max(text.rfind('/', 0, first), text.rfind('\\', 0, first))
    lit, rest = (text[:cut + 1], text[cut + 1:]) if cut >= 0 else ('.', text)
    try:
        base = sh.resolve_path(lit, where, shell, ctx.home)
    except (OSError, ValueError):
        base = None
    if not base:
        return None
    b = jail.real(base)
    parts = [x.lower() for x in re.split(r'[\\/]', rest) if x and x != '.']
    for f in files(ctx):
        r = jail.real(f)
        if r == b or not jail.inside(r, b):
            continue
        below = r[len(b.rstrip('\\/')) + 1:].split(os.sep)
        if any('**' in x or x == '..' for x in parts):
            return f
        if parts and len(parts) <= len(below) and all(fnmatch.fnmatchcase(n, x) for n, x in zip(below, parts)):
            return f
    return None


# a word of the command before a | that reads as a path: a separator, a wildcard, or a leading
# dot, tilde or $ (a name with none of these may be a subcommand: `git ls-files`)
PATHISH_RE = re.compile(r'[\\/*?\[]|^[.~$]')
# a command before a | that names the list or its folder
UP_NAMES_RE = re.compile(r'(?i)owner-delegations|\.pm-kit')


def upstream(ctx, cmd):
    """The copy of the list the paths the commands before `cmd`'s | (xargs's input: `cmd` itself
    when nothing pipes in) can name: a path-like word of theirs that reaches it (word_reaches); a
    command that names the list or .pm-kit (fail closed: a quoted or computed spelling); with no
    path-like literal word, the folder the first of them runs in when the list is in it or below
    it (fail closed: `git ls-files | xargs rm` in the PM repository). A git command's words are
    read as pathspecs (covers: a wildcard crosses folders)."""
    chain, up = [], cmd.pipe_from
    while up is not None and len(chain) < 32:
        chain.append(up)
        up = up.pipe_from
    pathish = False
    for c in chain:
        where = c.cwd_home or c.cwd
        for w in c.words[1:]:
            if w.dyn or w.text.startswith('-') or not PATHISH_RE.search(w.text):
                continue
            pathish = True
            if is_git(c):                       # a pathspec: its wildcard crosses folders
                f = next((x for x in files(ctx) if covers(ctx, w, where, where, jail.real(x))), None) \
                    if where else None
            else:
                f = word_reaches(ctx, w, where, c.shell)
            if f is not None:
                return f
    if UP_NAMES_RE.search(' '.join(c.text for c in chain) if chain else cmd.text):
        return files(ctx)[0]
    if pathish:
        return None
    first = chain[-1] if chain else cmd
    where = first.cwd_home or first.cwd
    return reaches(ctx, where) if where else None


def is_git(c):
    """True when the command `c` runs git."""
    return bool(c.words) and not c.words[0].dyn and \
        re.split(r'[\\/]', c.words[0].text)[-1].lower() in ('git', 'git.exe')


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
    elif sub == 'sparse-checkout' and not (pos[:1] and pos[0].text in SPARSE_READS):
        why = 'may take it out of the work tree'
    elif sub == 'bisect' and not (pos[:1] and pos[0].text in BISECT_READS):
        why = 'may check out another commit\'s version'
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
