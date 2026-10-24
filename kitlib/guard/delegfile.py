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

Two shell forms the edit rules cannot place are judged here for the machine config folder too
(rules.machine_config, docs/guard.md "Owner machine setup"), each after the list's own check: a
wildcard write target whose literal folder is or lies in a machine config folder
(rules.machine_config_folder: <pointer>/.pm-kit, or a .pm-kit folder at a checkout's top), or
whose wildcard parts match on disk a path that is or lies in one (glob_config_below), and
git in the pointer's work tree whose pathspec lands in <pointer>/.pm-kit (rm, mv, restore,
checkout-index, checkout with paths after --, clean).

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
from . import paths
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
    xargs (upstream). With none of those, a wildcard target that starts in or can name a machine
    config folder (glob_config) is refused as machine-config."""
    into = rules.copy_into(cmd, inv)
    where = cmd.cwd_home or cmd.cwd
    config = None                               # (Word, folder): a wildcard in a machine config folder
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
            if f is None and config is None:
                found = glob_config(ctx, w, where, cmd.shell)
                config = (w,) + found if found else None
        if f is not None:
            return refusal(event, ctx, role, what, f)
    if config is not None:
        w, folder, over = config
        if over:
            return walk_limit_refusal(role, rules.bare(cmd), w.text, folder)
        return rules.machine_config_refusal(event, ctx, role, '`%s` writes %s, a wildcard in %s' % (
            rules.bare(cmd), w.text, rules.quote(folder)), folder)
    return None


def walk_limit_refusal(role, bare, word, folder):
    """The machine-config refusal of a wildcard write target `word` whose match read past
    WALK_LIMIT names in and below `folder` (glob_config's over): the guard could not check it, so
    the advice is to narrow the word or name the files, not the machine config folder's own
    wording, which would be wrong where no .pm-kit is near (rule and mode as
    rules.machine_config_refusal)."""
    return rules.refuse(rules.MACHINE_CONFIG, role, (
        '`%s` writes %s, a wildcard too wide for the guard to check: it read more than %d names in and '
        'below %s and stopped, so it cannot tell whether the wildcard names a machine config folder (fail '
        'closed). Narrow it, or name the files: start the wildcard in a deeper folder, or list the folder '
        'and name the files the command writes. To list the folder:') % (
            bare, word, WALK_LIMIT, rules.quote(folder)), 'Get-ChildItem -Force -Name %s' % rules.quote(folder))


def glob_config(ctx, w, where, shell):
    """(folder, over) for the wildcard write target Word `w`, read from `where`: the machine
    config folder (rules.machine_config_folder) it starts in or can name, its literal folder
    being that folder or lying in it, or a path its parts after that folder match on disk being
    or lying in it (glob_config_below), with `over` False; or (the literal folder, True) when
    that match passed WALK_LIMIT names (fail closed). None for a word with no wildcard, one the
    guard cannot place, or one that names none."""
    text = sh.home_text(w.text, ctx.home, w.dyn)
    if not text or not hubg.has_glob(text):
        return None
    base, rest = glob_base(ctx, text, where, shell)
    if not base:
        return None
    folder = rules.machine_config_folder(ctx, base)
    if folder:
        return folder, False
    try:
        folder = glob_config_below(ctx, base, rest, shell)
    except WalkLimit:
        return base, True
    return (folder, False) if folder else None


# the most folder entries glob_config_below reads for one wildcard before it stops and the write
# is refused (fail closed): the guard answers within the host's hook deadline (Codex 7 s)
WALK_LIMIT = 4000
# a bash set that starts `[^` is negated as `[!` is; fnmatch reads only `[!`
BASH_NEGATE_RE = re.compile(r'\[\^')


class WalkLimit(Exception):
    """glob_config_below read more than WALK_LIMIT folder entries."""


def glob_config_below(ctx, base, rest, shell=None):
    """The machine config folder an existing path the wildcard parts `rest` match below the
    folder `base` is or lies in, resolved as FindFirstFile resolves them: a wildcard part (a DOS
    one as `*`, rules.DOS_WILDCARDS) against the names in each folder reached so far, long or
    8.3 short (rules.glob_part), case folded; a part with none joined as written. For a bash
    word (sh.BASH) a part that starts with `*`, `?` or `[` matches no name that starts with a
    dot, as bash without dotglob reads it, and a `[^` set is negated; PowerShell and cmd match
    a leading dot like any other character. Every part is matched, the last too (`rm -r .pm-k*`
    names the folder); a folder part matches folders only, and only a path that exists is
    judged (`*/__pycache__` names no new folder in .pm-kit; a new file there is glob_config's
    literal-folder check). None when no path it matches is in one; WalkLimit past WALK_LIMIT
    folder entries read."""
    bash = shell == sh.BASH
    parts = [x for x in re.split(r'[\\/]', rest) if x and x != '.']
    level, read = [base], 0
    found = []
    for i, part in enumerate(parts):
        found = []
        pattern = part.lower().translate(rules.DOS_WILDCARDS)
        if bash:
            pattern = BASH_NEGATE_RE.sub('[!', pattern)
        wild = hubg.has_glob(pattern)
        no_dot = bash and part[:1] in hubg.GLOB_CHARS
        for folder in level:
            if not wild:
                found.append(paths.norm(os.path.join(folder, part), fold=False))
                continue
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            read += len(names)
            if read > WALK_LIMIT:
                raise WalkLimit()
            for name in names:
                if no_dot and name.startswith('.'):
                    continue
                path = os.path.join(folder, name)
                long_name = name.lower()            # the short name is read only when the long one fails
                if long_name == pattern or fnmatch.fnmatchcase(long_name, pattern) or rules.glob_part(
                        long_name, rules.short_parts(path, 1)[0], pattern):
                    found.append(path)
        if i < len(parts) - 1:
            level = [p for p in found if os.path.isdir(p)]
    for path in found:
        if os.path.lexists(path) and config_named(path):
            folder = rules.machine_config_folder(ctx, path)
            if folder:
                return folder
    return None


def config_named(path):
    """True when `path`, as written or as the filesystem reaches it (jail.real), passes through
    a folder named as a machine config folder is (kitlib.config.user.DIR_NAME): the cheap test
    before rules.machine_config_folder, which asks git where the path is."""
    name = user.DIR_NAME.lower()
    return any(name in re.split(r'[\\/]', p.lower()) for p in (path, jail.real(path)))


def glob_readings(text):
    """The wildcard character sets the path `text` is read with as a wildcard: hub's GLOB_CHARS
    when it has one of them, and rules.LEDGER_GLOB_CHARS (the DOS `<`, `>`, `"` too, which
    FindFirstFile reads as wildcards) when it has a DOS one; empty for no wildcard. Each reading
    only adds what the word can reach."""
    out = []
    if hubg.has_glob(text):
        out.append(hubg.GLOB_CHARS)
    if any(c in text for c in '<>"'):
        out.append(rules.LEDGER_GLOB_CHARS)
    return out


def here_word(cmd, w):
    """True when the write `w` is a stand-in for the folder the program runs in (rules.here)."""
    return w.text == '.' and not w.dyn and cmd.source[w.start:w.end] != '.'


def word_reaches(ctx, w, where, shell, into=None):
    """The copy of the list a write of the path Word `w`, read from `where`, reaches (reaches,
    glob_reaches); None when it reaches none or the guard cannot place it."""
    text = sh.home_text(w.text, ctx.home, w.dyn)
    if not text:
        return None
    for chars in glob_readings(text):
        f = glob_reaches(ctx, text, where, shell, chars)
        if f is not None:
            return f
    if hubg.has_glob(text):
        return None
    try:                                        # a DOS wildcard is read as a literal name too
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


def glob_base(ctx, text, where, shell, chars=hubg.GLOB_CHARS):
    """(folder, rest) of the wildcard path `text` read from `where`: the literal folder it starts
    in (before its first wildcard character, one of `chars`), resolved (None when the guard
    cannot place it), and the text after it."""
    first = min(text.index(c) for c in chars if c in text)
    cut = max(text.rfind('/', 0, first), text.rfind('\\', 0, first))
    lit, rest = (text[:cut + 1], text[cut + 1:]) if cut >= 0 else ('.', text)
    try:
        base = sh.resolve_path(lit, where, shell, ctx.home)
    except (OSError, ValueError):
        base = None
    return base, rest


def glob_reaches(ctx, text, where, shell, chars=hubg.GLOB_CHARS):
    """The copy of the list the wildcard path `text`, read from `where`, can name: the file or a
    folder above it, each part of the wildcard matched (case folded, a leading dot like any
    other character: fail closed; a DOS wildcard as `*`, rules.DOS_WILDCARDS) against the
    matching part of the copy's path below the literal folder the wildcard starts in (glob_base
    with `chars`); `**` or `..` after the wildcard, any copy below that folder. None when it can
    name none, or the guard cannot place that folder. A part matches the 8.3 short name too
    (rules.glob_part), as FindFirstFile matches."""
    base, rest = glob_base(ctx, text, where, shell, chars)
    if not base:
        return None
    b = jail.real(base)
    parts = [x.lower().translate(rules.DOS_WILDCARDS) for x in re.split(r'[\\/]', rest) if x and x != '.']
    for f in files(ctx):
        r = jail.real(f)
        if r == b or not jail.inside(r, b):
            continue
        below = r[len(b.rstrip('\\/')) + 1:].split(os.sep)
        if any('**' in x or x == '..' for x in parts):
            return f
        shorts = rules.short_parts(r, len(below))
        if parts and len(parts) <= len(below) and all(
                rules.glob_part(n, s, x) for n, s, x in zip(below, shorts, parts)):
            return f
    return None


# a word of the command before a | that reads as a path: a separator, a wildcard, or a leading
# dot, tilde or $ (a name with none of these may be a subcommand: `git ls-files`)
PATHISH_RE = re.compile(r'[\\/*?\[]|^[.~$]')
# a command before a | that names the list or its folder
UP_NAMES_RE = re.compile(r'(?i)owner-delegations|\.pm-kit')


def upstream(ctx, cmd):
    """The copy of the list the paths the commands before `cmd`'s | (xargs's input: `cmd` itself
    when nothing pipes in) can name: a word of theirs that reaches it (word_reaches: a bare name,
    and a computed word through a leading home variable, are read too); a
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
            if not w.dyn and w.text.startswith('-'):
                continue
            pathish = pathish or (not w.dyn and bool(PATHISH_RE.search(w.text)))
            if is_git(c) and not w.dyn:         # a pathspec: its wildcard crosses folders
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
    short, longs, pos = rules._opts(sub, args, wide=True)
    file_real = jail.real(copy)
    hit = any(covers(ctx, w, gc.dir, top, file_real) for w in pos)
    why = None
    if sub in ('rm', 'mv') and hit:
        why = 'removes or moves it'
    elif sub == 'restore' and hit and not rules.restore_index_only(short, longs):
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
    elif sub == 'clean' and not rules.clean_dry_run(args) and (hit or not pos):
        why = 'may delete it'
    elif sub == 'stash' and not rules.stash_reads(args):
        why = 'may take it away or overwrite it'
    elif sub == 'sparse-checkout' and not (pos[:1] and pos[0].text in SPARSE_READS):
        why = 'may take it out of the work tree'
    elif sub == 'bisect' and not (pos[:1] and pos[0].text in BISECT_READS):
        why = 'may check out another commit\'s version'
    if why is not None:
        return refusal(event, ctx, role, '%s runs in the PM repository %s and %s' % (text, rules.fwd(top), why),
                       copy)
    return git_config(event, ctx, role, text, top, gc.dir, sub, short, longs, args, pos)


def git_config(event, ctx, role, text, top, base, sub, short, longs, args, pos):
    """machine-config for git run in the pointer's work tree `top`: a pathspec that lands in
    <pointer>/.pm-kit (lands_in) given to rm, mv, restore (not --staged alone), checkout-index,
    checkout after --, or clean (not -n)."""
    folder = jail.pointer_config_dir(ctx)
    if folder is None:
        return None
    folder_real = jail.real(folder)

    def lands(words):
        return any(lands_in(ctx, w, base, top, folder_real) for w in words or ())

    if sub in ('rm', 'mv', 'checkout-index'):
        hit = lands(pos)
    elif sub == 'restore':
        hit = lands(pos) and not rules.restore_index_only(short, longs)
    elif sub == 'checkout':
        hit = lands(after_dashes(args))
    elif sub == 'clean':
        hit = not rules.clean_dry_run(args) and lands(pos)
    else:
        hit = False
    if not hit:
        return None
    return rules.machine_config_refusal(event, ctx, role, '%s runs in the PM repository %s and changes a file in %s'
                                        % (text, rules.fwd(top), rules.quote(folder)), folder)


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
    r = spec_real(ctx, w, base, top)
    if r is True or r is False:
        return r
    return r == file_real or jail.inside(file_real, r)


def lands_in(ctx, w, base, top, folder_real):
    """True when the git pathspec Word `w` (read as covers reads it) names the folder whose real
    path is `folder_real` or a path in it: a glob by its literal folder; an exclusion never; any
    other magic, and a computed word, always (fail closed)."""
    r = spec_real(ctx, w, base, top)
    if r is True or r is False:
        return r
    return jail.inside(r, folder_real)


def spec_real(ctx, w, base, top):
    """The real path the git pathspec Word `w`, read from `base` (`top` the work tree), names (a
    glob: its literal folder); False for an exclusion (`:!`, `:^`, `:(exclude)`); True when it
    cannot be read (a computed word, other magic, a path that does not resolve)."""
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
    return jail.real(p)
