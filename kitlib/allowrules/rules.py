"""The text of the land permission rules, and a reference matcher for them (docs/allow-rules.md).

  allow_lines(kit, repo)         one PowerShell allow per script spelling
  deny_lines(kit, repo)          a second -C, --log-dir, a bare --, and each shell metacharacter,
                                 after the script
  decide(lines, shell, command)  'allow', 'deny' or None under the matching model in the doc

`decide` is a model of Claude Code's documented rule matching, written so the tests can pin what
each line lets through. It is a test oracle, not Claude Code: where Claude Code's own behaviour is
undocumented (runs of spaces, quotes, case in Bash) the doc marks the model's reading INFERENCE.
"""
import re

SHELLS = ('Bash', 'PowerShell')
# The shells that get allow lines. Not Bash: it globs (`-[C]` or `-?` becomes a file named -C), and a
# deny cannot name a literal `*`, so no deny set closes a second -C there. PowerShell and Python do
# not glob arguments on Windows. Both shells get the deny lines.
ALLOW_SHELLS = ('PowerShell',)
SCRIPT = 'tools/land.py'
# Shell metacharacters that redirect, chain, substitute, expand, quote, escape or cut the command
# short: any of them in a land command is denied, so no `*` of an allow can carry one (`$` and `(`
# stand for `$(`, `@(` and PowerShell's `(expr)`; quotes, the backslash, braces and the tab hide a
# second -C or glue a flag into a value, as in `abc\ --submit`). `#` (a comment), CR (PowerShell
# ends a statement there) and NUL (a native command line ends there) drop the trailing --submit.
# `@` is PowerShell's splat: `@var` passes a variable's contents as arguments, an automatic one
# (`@PSVersionTable`: -CLRVersion:..., a second -C to argparse) or one set by an earlier call
# (`$x=,'--'` then `@x`: a bare --; probed 5.1).
# PowerShell also takes U+2018-U+201B and U+201C-U+201E as quotes, and U+2013-U+2015 as dashes.
# SEPARATORS: the characters besides space, tab, CR and LF that split a native argument in
# PowerShell as a space does (probed on 5.1: VT, FF, U+0085 and Unicode's space, line and paragraph
# separators). Denied, so a space is the only separator an allow's `*` carries, and the `--` deny
# (END_OF_OPTIONS) cannot be stepped around with another one.
SEPARATORS = ('\x0b', '\x0c', '\x85', '\xa0', ' ') + tuple(chr(c) for c in range(0x2000, 0x200b)) + (
    ' ', ' ', ' ', ' ', '　')
METACHARS = ('>', '<', '|', ';', '&', '$', '(', '`', '%', "'", '"', '\\', '{', '}', '#', '@',
             '\t', '\r', '\n', '\0',
             '‘', '’', '‚', '‛', '“', '”', '„',
             '–', '—', '―') + SEPARATORS
# The prefix of land.py's --log-dir that no other option shares (land.py takes abbreviations).
LOG_DIR = '--lo'
# A bare `--` ends argparse's options: everything after it, the trailing --submit included, is a
# branch name, so land.py would run a foreground landing (and with --submitted before the `--`,
# skip its "--submit and --submitted, not both" check).
END_OF_OPTIONS = ' -- '
# The operators a command is split on before each part is matched (newlines too).
_SPLIT = {
    'Bash': re.compile(r'\|&|&&|\|\||[;|&\n]'),
    'PowerShell': re.compile(r'&&|\|\||[;|\n]'),
}
_RULE = re.compile(r'^(\w+)\((.*)\)$', re.S)


def scripts(kit):
    """The two spellings of the export's land.py: bare and double-quoted."""
    path = '%s/%s' % (kit, SCRIPT)
    return (path, '"%s"' % path)


def allow_lines(kit, repo):
    """The allow rules: for each allowed shell and spelling, the plain submit."""
    return ['%s(python %s -C %s * --expect-tip * --submit)' % (shell, script, repo)
            for shell in ALLOW_SHELLS for script in scripts(kit)]


def deny_lines(kit, repo):
    """The deny rules, for each shell and spelling: a -C after the first (spaced, or any text -C past
    the fixed `-C <repo>` prefix), --log-dir by its shortest prefix, a bare `--`, and each
    metacharacter."""
    out = []
    for shell in SHELLS:
        for script in scripts(kit):
            out.append('%s(python %s * -C*)' % (shell, script))
            out.append('%s(python %s -C %s *-C*)' % (shell, script, repo))
            out.append('%s(python %s *%s*)' % (shell, script, LOG_DIR))
            out.append('%s(python %s *%s*)' % (shell, script, END_OF_OPTIONS))
            out += ['%s(python %s *%s*)' % (shell, script, c) for c in METACHARS]
    return out


def _pattern(text, shell):
    if text.endswith(':*'):
        text = text[:-2] + ' *'
    body = '.*'.join(re.escape(part) for part in text.split('*'))
    return re.compile(body, re.S | (re.I if shell == 'PowerShell' else 0))


def _patterns(lines, shell):
    out = []
    for line in lines:
        m = _RULE.match(line)
        if m and m.group(1) == shell:
            out.append(_pattern(m.group(2), shell))
    return out


def parts(shell, command):
    """The command split on the shell's chain operators, each part stripped, empty parts dropped."""
    split = _SPLIT.get(shell)
    pieces = split.split(command) if split else [command]
    return [p.strip() for p in pieces if p.strip()]


def decide(lines, shell, command):
    """'deny' when a deny rule of `shell` matches the whole command or any part; else 'allow' when
    every part matches an allow rule; else None (the normal prompt). `lines` is {"allow", "deny"}."""
    if shell not in SHELLS:
        return None
    pieces = parts(shell, command)
    whole = command.strip()
    deny = _patterns(lines.get('deny', ()), shell)
    if any(p.fullmatch(text) for p in deny for text in [whole] + pieces):
        return 'deny'
    allow = _patterns(lines.get('allow', ()), shell)
    if pieces and all(any(p.fullmatch(text) for p in allow) for text in pieces):
        return 'allow'
    return None
