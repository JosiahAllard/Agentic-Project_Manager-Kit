"""The text of the land permission rules, and a reference matcher for them (docs/allow-rules.md).

  allow_lines(kit, repo, lanes)  one allow per shell, script spelling and lane (plus the plain one)
  deny_lines(kit)                a second -C, and each shell metacharacter, anywhere after the script
  decide(lines, shell, command)  'allow', 'deny' or None under the matching model in the doc

`decide` is a model of Claude Code's documented rule matching, written so the tests can pin what
each line lets through. It is a test oracle, not Claude Code: where Claude Code's own behaviour is
undocumented (runs of spaces, quotes, case in Bash) the doc marks the model's reading INFERENCE.
"""
import re

SHELLS = ('Bash', 'PowerShell')
SCRIPT = 'tools/land.py'
# Shell metacharacters that redirect, chain, substitute or expand: any of them in a land command is
# denied, so no `*` of an allow can carry one (`$` and `(` stand for `$(`, `@(` and PowerShell's `(expr)`).
METACHARS = ('>', '<', '|', ';', '&', '$', '(', '`', '%')
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


def allow_lines(kit, repo, lanes):
    """The allow rules: for each shell and spelling, the plain submit and one per lane, in order."""
    out = []
    for shell in SHELLS:
        for script in scripts(kit):
            base = 'python %s -C %s * --expect-tip * --submit' % (script, repo)
            out.append('%s(%s)' % (shell, base))
            out += ['%s(%s --lane %s)' % (shell, base, lane) for lane in lanes]
    return out


def deny_lines(kit):
    """The deny rules: a -C after the first, and each metacharacter, for each shell and spelling."""
    out = []
    for shell in SHELLS:
        for script in scripts(kit):
            out.append('%s(python %s * -C*)' % (shell, script))
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
