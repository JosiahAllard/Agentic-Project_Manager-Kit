"""Owner to-do steps (docs/hub-mcp.md "To-do steps"): the structured steps an agent gives for a
to-do, rendered as the PowerShell commands the owner pastes, and the rules an agent's to-do is
refused by. Pure logic: nothing here prints, reads a file or calls the hub.

A step is a dict with exactly one verb key:
  {edit: <file>, old: <text>, new: <text>}  an exact literal replace of the one place `old` is;
  {open: <file>, line: <n>}                 the file in Notepad++ (line optional);
  {run: <command>}                          a PowerShell command, passed through verbatim.

render(steps) -> [command]: one command per step, in order (StepError on a bad step).
summary(steps) -> [line]: what each step does, for the to-do's detail.
check(fields) -> None: Refused when the agent-written text of a to-do (title, detail, commands,
run steps) asks for plain Notepad, describes a hand edit in prose, or is coordinator work.

The edit command runs in Windows PowerShell 5.1 as one `& { ... }` block:
- Text goes in as a readable single-quoted literal, which expands nothing. A text holding a
  single quote of any form PowerShell reads as one (U+0027, U+2018-U+201B), a control character
  other than a newline (C0, DEL and C1, U+0080-U+009F), a tab (a tab pasted into a console can
  start completion), the line and paragraph separators U+2028 and U+2029, a bidi control or
  invisible character (U+202A-U+202E, U+2066-U+2069, U+200B-U+200F, U+FEFF, the no-break space
  U+00A0), or a character outside the Basic Multilingual Plane goes in as base64 of its UTF-8
  bytes instead, with a readable one-line preview in a <# #> comment. The preview shows only the
  first 60 characters (PREVIEW), with those characters escaped. A block whose texts hold no
  newline is one line. A file or text holding a lone surrogate (U+D800 to U+DFFF) is a bad step.
- An open step runs Notepad++ (NOTEPADPP) from Program Files, else Program Files (x86), read
  from the environment when the command runs; neither holds it: a MISS line.
- `old` and `new` reach the script with LF newlines (render drops every CR; the script drops any a
  paste brought back) and take the file's own line ending there: CRLF when the file has one, LF
  otherwise; a file with both is a MISS when old or new holds a newline.
- The file is read as bytes: a UTF-8 BOM, a UTF-16 BOM (either order) or none, which must then be
  valid UTF-8 (an ANSI file is a MISS and is not written). It is written back with the same BOM
  bytes and encoding; Get-Content and Set-Content are never used (5.1 reads and writes ANSI).
- `old` must occur exactly once, counted by ordinal search, overlapping matches included. Once:
  the replace is written and the block prints `ok <file>`. Otherwise, or on any error, it prints
  `MISS <file>: <why>` and the file is not written.
"""
import base64
import re

NOTEPADPP = r'Notepad++\notepad++.exe'   # under Program Files, then Program Files (x86)
TEXT_MAX = 16384            # characters in old or new
PATH_MAX = 1024
PREVIEW = 60                # characters of a base64 text shown in its comment
RUN_SHOWN = 60              # characters of a run command shown in its summary line
VERBS = {'edit': ('edit', 'old', 'new'), 'open': ('open', 'line'), 'run': ('run',)}

_QUOTES = "'\u2018\u2019\u201a\u201b"
# bidi controls (embeddings, overrides, isolates), zero-width characters and marks, the BOM, and
# the no-break space: each makes a literal read other than it runs, or hides in plain sight
_INVISIBLE = '\u202a-\u202e\u2066-\u2069\u200b-\u200f\ufeff\u00a0'
_PATH_RE = re.compile(r'(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+[\\/]).*\Z', re.S)
_PATH_BAD_RE = re.compile('["<>|?*\x00-\x1f\x7f\x85\u2028\u2029]')
_SURROGATE_RE = re.compile('[\ud800-\udfff]')
# what a readable literal may not hold: the quotes, control characters but LF (C0, DEL, C1), line
# and paragraph separators, the invisible characters, and astral characters (surrogates once
# encoded by a console)
_UNREADABLE_RE = re.compile("[%s\x00-\x09\x0b-\x1f\x7f-\x9f\u2028\u2029%s\U00010000-\U0010ffff]"
                            % (_QUOTES, _INVISIBLE))
_ESCAPED_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029%s]" % _INVISIBLE)

# agent-written text an owner's to-do may not hold; notepad\ is a folder, not the program
_NOTEPAD_RE = re.compile(r'(?i)(?<![\w+-])notepad(?:\.exe)?(?![\w+\\-]|\.\w)')
_PROSE_RES = (
    # within one sentence: a dot ends it only before a space or the end (config.json does not)
    re.compile(r'(?i)\bopen\b(?:[^.\n]|\.(?=\S)){0,80}?\b(?:and|then)\s+'
               r'(?:apply|make|edit|change|replace|paste)\b'),
    re.compile(r'(?i)\b(?:apply|make)\s+(?:the|these|this|those|following)\s+(?:following\s+)?'
               r'(?:edits?|changes?)\b'),
)
_SEGMENT_RE = re.compile(r'&&|\|\||[;|\n]')
_GIT = r'(?i)(?<![\w-])git(?:\.exe)?\b.*?\s'
_COORDINATOR_RES = (
    ('git worktree add/remove', re.compile(_GIT + r'worktree\s+(?:add|remove|move|prune)\b')),
    ('a kit landing, gate or worktree tool',
     re.compile(r'(?i)(?<![\w-])(?:gate|land|new_worktree|worktree_remove|sync_base)\.py\b')),
    ('a branch or tag delete',
     re.compile(_GIT + r'(?:branch|tag)\b.*?\s(?:-[A-Za-z]*[dD][A-Za-z]*|--delete)(?![\w-])')),
    ('a ref delete', re.compile(_GIT + r'update-ref\b.*?\s(?:-d|--stdin)(?![\w-])')),
    ('a remote ref delete or forced push',
     re.compile(_GIT + r'push\b.*?\s(?:--delete|-d|--force(?:-with-lease|-if-includes)?(?:=\S*)?|'
                       r'-[A-Za-z]*f[A-Za-z]*|--mirror|--prune|:\S+|\+\S+)(?![\w-])')),
)


class StepError(ValueError):
    """A step that is not one of the shapes; field is 'steps'."""

    def __init__(self, message, field='steps'):
        ValueError.__init__(self, message)
        self.field = field


class Refused(ValueError):
    """Text an agent's to-do may not hold; field names where it is."""

    def __init__(self, message, field):
        ValueError.__init__(self, message)
        self.field = field


# ------------------------------------------------------------------ steps
def _lf(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _no_surrogate(where, name, v):
    """StepError for a lone surrogate: half a character, which no encoding can write."""
    m = _SURROGATE_RE.search(v)
    if m:
        raise StepError('%s: %s holds a lone surrogate (U+%04X) at character %d, half of a '
                        'character that cannot be written' % (where, name, ord(m.group()),
                                                                m.start()))


def _path(where, v):
    if not isinstance(v, str) or not v:
        raise StepError('%s: the file is a non-empty string' % where)
    _no_surrogate(where, 'the file', v)
    if len(v) > PATH_MAX or not _PATH_RE.match(v) or _PATH_BAD_RE.search(v) or \
            v.endswith(('\\', '/')):
        raise StepError('%s: the file must be an absolute path to a file (<drive>:\\... or '
                        '\\\\server\\share\\...) with no control character or "<>|?*: %r'
                        % (where, v))
    return v


def _text(where, name, v, empty):
    if not isinstance(v, str):
        raise StepError('%s: %s is a string' % (where, name))
    _no_surrogate(where, name, v)
    v = _lf(v)
    if not empty and not v:
        raise StepError('%s: %s cannot be empty' % (where, name))
    if len(v) > TEXT_MAX:
        raise StepError('%s: %s is %d characters, more than %d' % (where, name, len(v), TEXT_MAX))
    return v


def _steps(steps):
    """[(verb, values)] for steps, each checked; StepError on the first bad one."""
    if not isinstance(steps, list) or not steps:
        raise StepError('steps is a non-empty list of {edit, old, new}, {open, line} or {run}')
    out = []
    for i, s in enumerate(steps):
        where = 'steps[%d]' % i
        if not isinstance(s, dict):
            raise StepError('%s is an object, not %s' % (where, type(s).__name__))
        verbs = [k for k in s if k in VERBS]
        if len(verbs) != 1:
            raise StepError('%s names exactly one of edit, open, run (it names %s)'
                            % (where, ', '.join(map(str, verbs)) or 'none'))
        verb = verbs[0]
        extra = sorted(str(k) for k in s if k not in VERBS[verb])
        if extra:
            raise StepError('%s: %s takes %s, not %s'
                            % (where, verb, ', '.join(VERBS[verb]), ', '.join(extra)))
        if verb == 'edit':
            for k in ('old', 'new'):
                if k not in s:
                    raise StepError('%s: edit needs %s' % (where, k))
            v = {'path': _path(where, s['edit']), 'old': _text(where, 'old', s['old'], False),
                 'new': _text(where, 'new', s['new'], True)}
            if v['old'] == v['new']:
                raise StepError('%s: new is the same as old' % where)
        elif verb == 'open':
            line = s.get('line')
            if line is not None and (not isinstance(line, int) or isinstance(line, bool)
                                     or line < 1):
                raise StepError('%s: line is a whole number from 1' % where)
            v = {'path': _path(where, s['open']), 'line': line}
        else:
            cmd = s['run']
            if not isinstance(cmd, str) or not cmd.strip():
                raise StepError('%s: run is a non-empty string' % where)
            v = {'run': cmd}
        out.append((verb, v))
    return out


# ------------------------------------------------------------------ PowerShell
def _preview(text):
    out = []
    for ch in text[:PREVIEW]:
        if ch == '\n':
            out.append('\\n')
        elif ch == '\t':
            out.append('\\t')
        elif _ESCAPED_RE.match(ch):
            out.append('\\x%02x' % ord(ch) if ord(ch) < 0x100 else '\\u%04x' % ord(ch))
        else:
            out.append(ch)
    s = ''.join(out).replace('#>', '# >').replace('<#', '< #')
    return s + ('...' if len(text) > PREVIEW else '')


def _literal(name, text):
    """`$name=<text as a PowerShell string>` and, for a base64 one, its preview comment."""
    if not _UNREADABLE_RE.search(text):
        return "$%s='%s'" % (name, text), None
    b64 = base64.b64encode(text.encode('utf-8')).decode('ascii')
    return ("$%s=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s'))"
            % (name, b64), '%s: %s' % (name, _preview(text)))


def _block(parts, notes, body):
    head = ''.join('<# %s #> ' % n for n in notes if n)
    return head + '& { ' + '; '.join(parts) + '; ' + body + ' }'


# The edit's body, after $p (file), $o (old) and $n (new) are set. One line; PowerShell 5.1.
_EDIT = (
    "try { $CR=[string][char]13; $LF=[string][char]10; $o=$o.Replace($CR,''); "
    "$n=$n.Replace($CR,''); $b=[IO.File]::ReadAllBytes($p); "
    "if ($b.Length -ge 4 -and $b[0] -eq 0xFF -and $b[1] -eq 0xFE -and $b[2] -eq 0 -and "
    "$b[3] -eq 0) { throw 'a UTF32 file is not edited here' }; "
    "if ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) "
    "{ $k=3; $e=New-Object Text.UTF8Encoding($true,$true) } "
    "elseif ($b.Length -ge 2 -and $b[0] -eq 0xFF -and $b[1] -eq 0xFE) "
    "{ $k=2; $e=New-Object Text.UnicodeEncoding($false,$true,$true) } "
    "elseif ($b.Length -ge 2 -and $b[0] -eq 0xFE -and $b[1] -eq 0xFF) "
    "{ $k=2; $e=New-Object Text.UnicodeEncoding($true,$true,$true) } "
    "else { $k=0; $e=New-Object Text.UTF8Encoding($false,$true) }; "
    "try { $t=$e.GetString($b,$k,$b.Length-$k) } "
    "catch { throw 'the file is not UTF-8 or UTF-16 text; it was not changed' }; "
    "$crlf=($t.Length-$t.Replace($CR+$LF,'').Length)/2; "
    "$bare=($t.Length-$t.Replace($LF,'').Length)-$crlf; "
    "if (($o+$n).Contains($LF)) { if ($crlf -gt 0 -and $bare -gt 0) "
    "{ throw 'the file mixes CRLF and LF line endings; it was not changed' }; "
    "if ($crlf -gt 0) { $o=$o.Replace($LF,$CR+$LF); $n=$n.Replace($LF,$CR+$LF) } }; "
    "$m=0; $s=0; $i=-1; while ($s -le $t.Length) "
    "{ $x=$t.IndexOf($o,$s,[StringComparison]::Ordinal); if ($x -lt 0) { break }; "
    "if ($m -eq 0) { $i=$x }; $m++; $s=$x+1 }; "
    "if ($m -ne 1) { 'MISS '+$p+': old text found '+$m+' times; the file was not changed' } "
    "else { $r=$e.GetBytes($t.Substring(0,$i)+$n+$t.Substring($i+$o.Length)); "
    "$w=New-Object byte[] ($k+$r.Length); [Array]::Copy($b,0,$w,0,$k); "
    "[Array]::Copy($r,0,$w,$k,$r.Length); [IO.File]::WriteAllBytes($p,$w); 'ok '+$p } } "
    "catch { 'MISS '+$p+': '+$_.Exception.Message }")

# The open's body, after $p (file) is set: the first Program Files folder that holds Notepad++.
_OPEN = ("$x=$null; foreach ($d in $env:ProgramFiles, ${env:ProgramFiles(x86)}) "
         "{ if ($d -and -not $x) { $c=Join-Path $d '%s'; "
         "if (Test-Path -LiteralPath $c) { $x=$c } } }; "
         "if ($x) { & $x%s $p; 'ok '+$p } "
         "else { 'MISS '+$p+': Notepad++ is not in Program Files or Program Files (x86)' }")


def _render(verb, v):
    if verb == 'run':
        return v['run']
    p, pn = _literal('p', v['path'])
    if verb == 'open':
        line = " '-n%d'" % v['line'] if v['line'] else ''
        return _block([p], [pn], _OPEN % (NOTEPADPP, line))
    o, on = _literal('o', v['old'])
    n, nn = _literal('n', v['new'])
    return _block([p, o, n], [pn, on, nn], _EDIT)


def render(steps):
    """The PowerShell command of each step, in order. StepError on a bad step."""
    return [_render(verb, v) for verb, v in _steps(steps)]


def summary(steps):
    """One line per step saying what it does. StepError on a bad step."""
    out = []
    for verb, v in _steps(steps):
        if verb == 'edit':
            n = v['old'].count('\n') + 1
            out.append('replace %d line%s in %s' % (n, '' if n == 1 else 's', v['path']))
        elif verb == 'open':
            out.append('open %s%s in Notepad++'
                       % (v['path'], ' at line %d' % v['line'] if v['line'] else ''))
        else:
            cmd = ' '.join(v['run'].split())
            out.append('run ' + (cmd if len(cmd) <= RUN_SHOWN else cmd[:RUN_SHOWN] + '...'))
    return out


# ------------------------------------------------------------------ refusals
def _texts(fields):
    """(field, where, text, is_command) for the agent-written text of a to-do."""
    for name in ('title', 'detail'):
        if isinstance(fields.get(name), str):
            yield name, name, fields[name], False
    cmds = fields.get('commands')
    for i, c in enumerate(cmds if isinstance(cmds, list) else ()):
        if isinstance(c, str):
            yield 'commands', 'commands[%d]' % i, c, True
    steps = fields.get('steps')
    for i, s in enumerate(steps if isinstance(steps, list) else ()):
        if isinstance(s, dict) and isinstance(s.get('run'), str):
            yield 'steps', 'steps[%d]' % i, s['run'], True


def check(fields):
    """Refused for the first agent-written text the owner's to-do rules forbid (module
    docstring). Fields of another type are left to the schema; edit text is never judged."""
    for field, where, text, is_command in _texts(fields):
        if is_command and _NOTEPAD_RE.search(text):
            raise Refused('%s runs plain Notepad: open a file with an {open: <file>} step, which '
                          'uses Notepad++' % where, field)
        if any(r.search(text) for r in _PROSE_RES):
            raise Refused('%s asks the owner to open a file and change it by hand: give each '
                          'change as an {edit: <file>, old, new} step' % where, field)
        if is_command:
            for segment in _SEGMENT_RE.split(text):
                for what, r in _COORDINATOR_RES:
                    if r.search(segment):
                        raise Refused('%s is coordinator work (%s): a coordinator does it, not '
                                      'the owner' % (where, what), field)
