"""A command line the parser cannot read cleanly (docs/guard.md "Fallback").

kitlib.guard.shell marks a parse unclean when the text holds something it does not model (an
unclosed quote, an encoded command, a program named by a variable, a word the shell rewrites).
The rules have already run on what it did read; check() then looks at the raw text, so an
unreadable line is never a way around a rule, and a plain line is never denied by it:

  - a git or root-search pattern that matches: the text from the match to the next ; | & or
    newline is read again as a command of its own and the rules run on it. A rule that denies it
    gives the deny, its real alternative included; none: the `unreadable` deny, which shows the
    line written plainly. The location-dependent git patterns are checked when the call's
    directory is a main checkout or one git cannot place; the push --no-verify pattern when that
    directory's repository has a PM data record (or git cannot place it), in every session but
    the owner's own.
  - a heavy program's name: the same, and a program named by a variable is denied as heavy with
    the lock line that runs it.
  - anything else: a subagent is denied (`unreadable`); another jailed session (a mark in
    guard_agent_marks) is denied (`write-jail`) when the line holds the word git, as the unread
    part may move git to a checkout the jail cannot place; a session the jail does not hold only
    in a main checkout or a place git cannot tell, and only when the line runs a program the text does not name, runs
    a shell, eval, Invoke-Expression, source or an encoded command, pipes into a shell, or names
    a git subcommand or option by a variable. Such a line in a repository with a PM data record
    is denied too (`git-push-pmdata`), wherever in it, in every session but the owner's own: the
    hidden program may be git push --no-verify.

Where a pattern matches, the directory is the one the command holding the match runs in (a
literal cd before it is followed), not only the call's.

The git patterns are an earlier per-repository git guard's ($g and its four rules) plus the
kit's own git rules.
"""
import fnmatch
import re

from kitlib import config as kconfig
from kitlib.git import repo

from . import commands as cm
from . import rules

G = r'\bgit(?:\.exe)?\s+(?:-[Cc]\s+(?:"[^"]*"|\'[^\']*\'|\S+)\s+|--?\S+\s+)*'
GIT_WORD_RE = re.compile(r'(?i)(?<![\w.-])git(?:\.exe)?(?![\w-])')  # git as a word, not .git or GIT_DIR
# programs that run text as commands: on an unclean line their commands may be what it hides
RUNNERS = cm.BASH_SHELLS | cm.PS_SHELLS | {'cmd', 'eval', 'iex', 'invoke-expression', 'source', '.'}

# git with no subcommand run by xargs: the input names the repository and the command, so it is
# judged machine-wide, not by the record of the directory it runs in
XARGS_BARE_GIT = r'\bxargs\b[^|;&\n]*\bgit(?:\.exe)?[ \t]*(?=[|;&\n]|$)'

MAIN_ONLY = (
    ('git-bulk-add', G + r'(?:add|stage)\b[^|;&]*(\s-A\b|\s--all\b|\s-u\b|\s--update\b|\s\.(\s|$))'),
    ('git-commit-all', G + r'commit\b[^|;&]*\s(-a|-am|-a[a-zA-Z]+|--all)\b'),
    ('git-checkout', G + r'(?:checkout|switch)\b'),
    ('git-reset-hard', G + r'reset\b[^|;&]*\s--hard\b'),
    ('git-clean', G + r'clean\b(?![^|;&]*\s(-[a-zA-Z]*n|--dry-run)\b)'),
    ('git-stash', G + r'stash\b(?!\s+(list|show)\b)'),
    ('git-restore', G + r'restore\b(?:(?![^|;&]*\s(?:--staged|-[a-zA-Z]*S[a-zA-Z]*)\b)'
                        r'|(?=[^|;&]*\s(?:--worktree|-[a-zA-Z]*W[a-zA-Z]*)\b))'),
)
SUBAGENT_MAIN = (
    ('git-main-history', G + r'(?:commit|merge|rebase|cherry-pick|revert|am|pull|apply)\b'),
)
ANYWHERE = (
    ('git-branch-force-delete', G + r'branch\s+[^|;&]*(-D|--delete\s+--force|-[a-zA-Z]*D[a-zA-Z]*)\b'),
    ('git-worktree-remove', G + r'worktree\s+remove\b'),
    ('git-push-main', G + r'push\b[^|;&]*(\s|:|/)(?:main|master)\b'),
    ('git-push-pmdata', G + r'push\b[^|;&]*\s--no(?:-v|-?(?![\w-]))'),
    ('git-push-pmdata', G + r'send-pack\b'),
    # a computed push word or subcommand ($X, `...`, $(...), %X%, @(...)): may be --no-verify or push
    ('git-push-pmdata', G + r'(?:push\b[^|;&\n]*)?(?:\$|`|%[A-Za-z_]\w*%|@\()'),
    # git push, or git with no subcommand, run by xargs: the input it appends may be --no-verify or push
    ('git-push-pmdata', r'\bxargs\b[^|;&\n]*\bgit(?:\.exe)?\b[^|;&\n]*\bpush\b'),
    ('git-push-pmdata', XARGS_BARE_GIT),
    # git http-push and the remote helpers, run by hand, talk to a remote without the hook too
    ('git-push-pmdata', G + r'(?:http-push|remote-[\w-]+)\b'),
    ('git-push-pmdata', r'(?i)(?<![\w.-])git-(?:send-pack|http-push|remote-[\w-]+)(?:\.exe)?\b'),
    ('wrapped-gate', r'(?:\bgate\.py|\bmachine_lock\.py\s+run)\b[^;&]*?(?<!\|)\|(?!\|)'),
    ('root-search', r'\bfind\s+(?:-[HLP]\s+)*(?:/|/[a-zA-Z]/?|[A-Za-z]:[\\/]?)(?=\s|$|["\'])'),
    ('root-search', r'(?i)\b(?:get-childitem|gci|ls|dir)\b[^|;&]*\s["\']?[A-Za-z]:[\\/]?["\']?(?=\s|$)[^|;&]*\s-r'),
    ('root-search', r'(?i)\b(?:get-childitem|gci|ls|dir)\b[^|;&]*\s-r[a-z]*\b[^|;&]*\s["\']?[A-Za-z]:[\\/]?["\']?(?=\s|$)'),
    ('root-search', r'(?i)\bdir\s+(?:/\w+\s+)*["\']?[A-Za-z]:\\?["\']?\s*(?:/\w+\s+)*/s\b'),
    ('root-search', r'(?i)\bdir\b[^|;&]*/s\b[^|;&]*\s["\']?[A-Za-z]:\\?["\']?(?=\s|$)'),
)
PARSED_RULES = ('owner_lock', 'wrapped_gate', 'root_search', 'heavy', 'git_rules', 'shell_writes')
WHY = 'The guard could not read this command; write the program name literally. Why: %s.'
PLAIN = ' Write it plainly:'


def heavy_patterns(cfg):
    """(entry, regex) per heavy program; a match starts at the program word."""
    out = []
    for e in kconfig.heavy_programs(cfg):
        glob = fnmatch.translate(e['program'].lower())
        # fnmatch ends the pattern with \Z (\z from Python 3.14): the match here goes on
        body = re.sub(r'\\[Zz]', '', glob).replace('(?s:', '(?:')
        body = body.replace('.*', r'[^\s"\'/\\|;&]*')
        out.append((e, re.compile(r'(?i)(?<![^\s;&|("\'/\\])%s(?:\.exe)?(?=$|[\s"\';&|)])' % body)))
    return out


def segment(text, start):
    """The text from `start` to the next ; | & or newline, an unbalanced closing quote dropped."""
    m = re.compile(r'[;|&\n]').search(text, start)
    seg = text[start:m.start() if m else len(text)].rstrip()
    while seg and seg[-1] in '\'"' and seg.count(seg[-1]) % 2:
        seg = seg[:-1].rstrip()
    return seg


def _dyn_commands(parsed):
    out = []
    for c in parsed.commands:
        inv = cm.invocation(c)
        if inv is not None and inv.dyn:
            out.append((c, inv))
    return out


def named_run(cmd, inv):
    """`<the program $X names> args` for a command whose program is a variable."""
    return ' '.join(['<the program %s names>' % inv.path] + [w.text for w in inv.args])


def plain(parsed, text):
    """The line written so the guard can read it, as far as the text tells."""
    dyn = _dyn_commands(parsed)
    if dyn:
        c, inv = dyn[0]
        return named_run(c, inv)
    for c in parsed.commands:
        if any(w.synth or w.alt is not None for w in c.words):
            return ' '.join(w.alt if w.alt is not None else w.text for w in c.words)
    t = text.rstrip()
    if t.count("'") % 2:
        return t + "'"
    if t.count('"') % 2:
        return t + '"'
    return t


def cwd_at(parsed, pos, cwd):
    """The directory the top-level command holding text position `pos` runs in (a literal cd
    before it followed; None when a cd the text cannot place came first)."""
    best = None
    for c in parsed.commands:
        if c.depth == 0 and c.start is not None and c.start <= pos and (best is None or c.start >= best.start):
            best = c
    return cwd if best is None else best.cwd


def main_like(ctx, where):
    place = ctx.place(where) if where else None
    return place is None or place.kind in (repo.MAIN, repo.UNKNOWN)


def runs_hidden(inv):
    """Why a command may run what the text does not show, or None."""
    if inv is None:
        return None
    if inv.dyn:
        return 'a program the text does not name'
    name = inv.name.lower()
    if name in RUNNERS:
        return 'a command that runs text (%s)' % inv.name
    if name == 'git' and cm.git_dyn_word(inv):
        return 'a git subcommand or option the text does not name'
    return None


def check(event, ctx, text, cwd, role, parsed):
    why = WHY % ('; '.join(parsed.unclean) or 'the parser gave no reason')
    shell = parsed.shell

    def reread(start, where):
        p = cm.read(segment(text, start), shell, where)
        for cmd in p.commands:
            inv = cm.invocation(cmd)
            for name in PARSED_RULES:
                d = getattr(rules, name)(event, ctx, cmd, inv, cwd, role)
                if d:
                    return d
        return None

    def answer(d, rule):
        if d is not None:
            return rules.Decision(d.kind, d.rule, '%s %s' % (why, d.reason))
        return rules.refuse('unreadable', role, '%s Its text matches the %s rule.%s' % (why, rule, PLAIN),
                            plain(parsed, text))

    main_rules = set(r for r, _p in MAIN_ONLY + SUBAGENT_MAIN)
    for rule, pat in list(ANYWHERE) + list(MAIN_ONLY) + list(SUBAGENT_MAIN):
        if rule == 'git-push-main':
            cfg = ctx.config(cwd) if cwd else None
            if cwd and ctx.is_pm_data(cwd):      # the kit's .kit.json decides; unloadable: read it again
                kit, _why = rules.kit_judged(ctx, cwd)
                cfg = kit.config(cwd) if kit is not None else None
            if cfg is not None and not cfg.push_block:
                continue
        m = re.search(pat, text)
        if not m:
            continue
        where = cwd_at(parsed, m.start(), cwd)
        if rule in main_rules and not main_like(ctx, where):
            continue
        if rule == 'git-push-pmdata' and (rules.is_owner_session(event, ctx) or (
                pat != XARGS_BARE_GIT and not rules.pmdata_recorded(ctx, where or cwd))):
            continue
        return answer(reread(m.start(), where or cwd), rule)
    held = None
    cfg = ctx.config(cwd) if cwd else None
    for entry, pat in heavy_patterns(cfg):
        m = pat.search(text)
        if not m or (entry.get('when_args') and not any(a in text for a in entry['when_args'])):
            continue
        if held is None:
            held = ctx.lock_held()[0]
        if held:
            break
        dyn = _dyn_commands(parsed)
        if dyn:
            c, inv = dyn[0]
            return rules.refuse('heavy', role, '%s Its text names %s, a heavy program (kind %s), and this session '
                                'holds no machine lock.%s' % (why, entry['program'], entry['kind'],
                                                              rules.run_from(c, cwd)),
                                rules.lock_alternative(ctx, c, entry, cwd, named_run(c, inv)))
        d = reread(m.start(), cwd_at(parsed, m.start(), cwd) or cwd)
        if d is not None:
            return answer(d, 'heavy')
    if role == 'subagent':
        return rules.refuse('unreadable', role, why + PLAIN, plain(parsed, text))
    if GIT_WORD_RE.search(text) and rules.agent_session(event, ctx):
        return rules.refuse('write-jail', role, '%s git on a line the guard cannot read may write a checkout it '
                            'cannot place; run the git in its own call.%s' % (why, PLAIN), plain(parsed, text))
    owner = rules.is_owner_session(event, ctx)
    for c in parsed.commands:
        inv = cm.invocation(c)
        hidden = runs_hidden(inv)
        if not hidden:
            continue
        if cm.call_subexpr(inv):
            # `& (...)`: judged only where PM data is (rules.call_subexpr_pmdata), never unreadable
            d = rules.call_subexpr_pmdata(event, ctx, c, inv, cwd, role)
            if d:
                return rules.Decision(d.kind, d.rule, '%s %s' % (why, d.reason))
            continue
        if main_like(ctx, c.cwd):
            return rules.refuse('unreadable', role, '%s It runs %s in a main checkout or a place git cannot '
                                'tell.%s' % (why, hidden, PLAIN), plain(parsed, text))
        if not owner and rules.pmdata_recorded(ctx, c.cwd):
            return rules.refuse('git-push-pmdata', role, '%s It runs %s in a repository with a PM data record (%s): '
                                'it may be git push --no-verify, which skips the pre-push hook that keeps PM data '
                                'off public remotes.%s' % (why, hidden, rules.pmdata.FILE_NAME, PLAIN),
                                plain(parsed, text))
    return None
