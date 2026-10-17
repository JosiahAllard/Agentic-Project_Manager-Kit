"""Bash, PowerShell and cmd command lines read as commands, not matched as text (docs/guard.md
"Parsing").

parse(text, shell, cwd) -> Parsed. Each Command has its words (a quoted word is one word; a
heredoc or here-string body is data, never a command), its redirections, where it starts and ends
in the text, whether it pipes into the next command, and the directory it runs in as far as the
text says (cd / Set-Location / pushd with one literal path; anything else makes the directory
unknown, None). Substitutions ($(...), backticks, <(...), @(...)) are parsed as commands of their
own; nested shells (bash -c, powershell -Command, cmd /c, Invoke-Expression, eval) are expanded by
kitlib.guard.commands. Anything the reader does not model marks the parse unclean (Parsed.unclean
names why) and the caller runs the regex fallback as well (kitlib.guard.fallback).

Deliberate departures from the shells, all toward reading what a Windows user meant:
  - bash: a backslash before a character that is not special to bash stays a backslash, so an
    unquoted Windows path (C:\\tmp\\x) reads as written; Git Bash would drop it. `C:\\ ` (a drive
    letter, a backslash, a space) is the drive root and the space ends the word.
  - a heredoc body is never parsed, even an unquoted one bash would expand.
"""
import os
import re

from kitlib.config import user as kuser

from . import paths

BASH, PS, CMD = 'bash', 'ps', 'cmd'
MAX_DEPTH = 6

# PowerShell splits words on every whitespace character it knows; bash only on space and tab.
PS_SPACE = (' \t\f\v\u00a0\u0085\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008'
            '\u2009\u200a\u2028\u2029\u202f\u205f\u3000')
BASH_ESC = ' \t"\'\\$`&|;<>(){}#*?[]!~'
CD_VERBS = {'cd', 'chdir', 'set-location', 'sl', 'push-location', 'pushd'}
CD_UNKNOWN = {'popd', 'pop-location'}
# PowerShell keywords a ( ) group follows (if (...), foreach (...), switch -regex (...)): the ( after
# them, or in a command they start, is a group, not an argument (_Scan.ps_paren_arg)
PS_PAREN_KEYWORDS = {'if', 'elseif', 'else', 'foreach', 'for', 'while', 'do', 'until', 'switch',
                     'function', 'filter', 'param', 'catch', 'trap', 'try', 'finally', 'return',
                     'throw', 'exit', 'break', 'continue', 'data', 'dynamicparam', 'begin', 'process',
                     'end', 'class', 'enum', 'using', 'in'}
PS_ESC = {'n': '\n', 't': '\t', 'r': '\r', '0': '\0', 'a': '\a', 'b': '\b', 'f': '\f', 'v': '\v',
          'e': '\x1b'}
_PS_VAR = re.compile(r'\$(?:[A-Za-z_]\w*:)?[\w?^$]+')
_BASH_VAR = re.compile(r'\$(?:[A-Za-z_]\w*|\d|[@*#?$!\-])')
# A PowerShell word a touching ( calls a method of, as PowerShell reads a command's arguments: a
# member access on an expression ($s.Replace, 'abc'.Replace, @a.Count, [IO.Path]::Combine), with a
# -Param: before it or not (_Scan.ps_paren_arg; `.Name` after a ) ] or } is one too)
_PS_MEMBER_HEAD = re.compile(r'^(?:-[^\s:]+:)?[$\'"@(\[]')
_PS_MEMBER_TAIL = re.compile(r'(?:\.|::)[A-Za-z_]\w*$')
_BRACE = re.compile(r'\{([^\s{}\'"$`\\;|&()<>]*,[^\s{}\'"$`\\;|&()<>]*)\}')
_ANSI = {'n': '\n', 't': '\t', 'r': '\r', '\\': '\\', "'": "'", '"': '"', '?': '?', 'a': '\a',
         'b': '\b', 'f': '\f', 'v': '\v', 'e': '\x1b', 'E': '\x1b'}
_ANSI_NUM = re.compile(r'x[0-9A-Fa-f]{1,2}|[0-7]{1,3}|u[0-9A-Fa-f]{1,4}|U[0-9A-Fa-f]{1,8}|c.')


class Word(object):
    """One argument: `text` as the shell would pass it (variables left as written), `dyn` when it
    holds anything only the shell knows at run time (a variable, a substitution). `synth` when the
    text is not what the source spells (an ANSI-C escape decoded, a brace expansion); `alt` the
    text bash really passes when it differs from `text` (a backslash bash would drop)."""
    __slots__ = ('text', 'dyn', 'start', 'end', 'synth', 'alt')

    def __init__(self, text, dyn, start, end, synth=False, alt=None):
        self.text, self.dyn, self.start, self.end = text, dyn, start, end
        self.synth, self.alt = synth, alt

    def __repr__(self):
        return 'Word(%r%s)' % (self.text, ', dyn' if self.dyn else '')


class Redirect(object):
    """`op` is > >> < << <<< with its stream (`fd`: a number, & or *) apart; `target` is a Word,
    None for a dup (2>&1). A heredoc's body is its target (`body` True)."""
    __slots__ = ('op', 'fd', 'target', 'dup', 'body')

    def __init__(self, op, fd, target=None, dup=False, body=False):
        self.op, self.fd, self.target, self.dup, self.body = op, fd, target, dup, body

    @property
    def writes(self):
        return self.op in ('>', '>>') and not self.dup

    def __repr__(self):
        return 'Redirect(%s%s %r)' % (self.fd, self.op, self.target)


class Command(object):
    __slots__ = ('words', 'redirects', 'start', 'end', 'source', 'shell', 'depth', 'cwd',
                 'piped_out', 'piped_in', 'subs', 'groups', 'nested_in', 'prev_op', 'next_op',
                 'pipe_from', 'git_env', 'home_env', 'path_env', 'cwd_ps_drive', 'cwd_home', 'home', 'subs_cwd',
                 'split')

    def __init__(self, source, shell, depth, home=None):
        self.words, self.redirects, self.subs = [], [], []
        self.start = self.end = None
        self.source, self.shell, self.depth = source, shell, depth
        self.home = home            # the folder `~` names, its Parsed's home (home_folder: None is the user's)
        self.cwd = None
        self.cwd_ps_drive = False   # cwd None after a PowerShell cd into a place ps_unplaceable names (track)
        self.cwd_home = None        # cwd, with a cd to a home variable ($HOME/x) followed too (track)
        self.subs_cwd = None        # the directory its substitutions start in; cwd unless a cd in one moved it (track)
        self.piped_out = self.piped_in = False
        self.groups = ()            # the ( ) or { } groups it is inside, outermost first: (id, '(' or '{')
        self.prev_op = self.next_op = None      # the operators around it: ; && || & newline ( ) { } |
        self.pipe_from = None       # the Command piping into this one
        self.nested_in = None       # the Command whose word held this one ($(), a nested shell)
        self.git_env = False        # the GIT_DIR-like variable some command of the line sets (commands.mark_git_env), else False
        self.home_env = False       # the variable naming git's user config folder the line sets (HOME, ...), else False
        self.path_env = False       # True when some command of the line sets PATH (commands.mark_git_env)
        # PowerShell: a ( ) or { } it touches is read as a group (a method call's arguments, a script
        # block) and more of the line follows the group before a separator, words or another group:
        # its arguments go on past the group, in commands of their own (_Scan.commands)
        self.split = False

    @property
    def grouped(self):
        return len(self.groups)

    @property
    def argv(self):
        return [w.text for w in self.words]

    @property
    def text(self):
        """The command's own source text, its words and redirections."""
        if self.start is None:
            return ''
        return self.source[self.start:self.end]

    def __repr__(self):
        return 'Command(%r, cwd=%r%s)' % (self.argv, self.cwd, ', |' if self.piped_out else '')


class Parsed(object):
    def __init__(self, text, shell, cwd, home=None):
        self.text, self.shell, self.start_cwd = text, shell, cwd
        self.home = home            # the folder `~` names (home_folder)
        self.commands = []
        self.unclean = []

    @property
    def clean(self):
        return not self.unclean

    def flag(self, why):
        if why not in self.unclean:
            self.unclean.append(why)


# ---------------------------------------------------------------- the scanner

class _Scan(object):
    """One level of one shell's text: tokens, then commands. Spans are offsets in `s`."""

    def __init__(self, text, shell, depth, parsed):
        self.s, self.n, self.i = text, len(text), 0
        self.shell, self.depth, self.parsed = shell, depth, parsed
        self.toks = []              # ('w', Word, subs) | ('op', text) | ('r', Redirect) | ('subs', cmds)
        self.heredocs = []          # (delimiter, strip_tabs, quoted, Redirect) waiting for a newline
        self.brace = 0
        self.closed = False

    def peek(self, k=0):
        j = self.i + k
        return self.s[j] if 0 <= j < self.n else ''

    def startswith(self, t):
        return self.s.startswith(t, self.i)

    def flag(self, why):
        self.parsed.flag(why)

    def space(self, c):
        return c != '' and c in (PS_SPACE if self.shell == PS else ' \t')

    def nested(self, text):
        """Commands of a backtick substitution's text, parsed one level down (its own spans)."""
        if self.depth + 1 > MAX_DEPTH:
            self.flag('substitutions nested deeper than %d' % MAX_DEPTH)
            return []
        sc = _Scan(text, self.shell, self.depth + 1, self.parsed)
        cmds = sc.commands()
        for c in _walk(cmds):
            c.source = text
        return cmds

    def sub_until(self):
        """Commands of a $( ... ) or ( ... ) body that starts at self.i, through its ), consumed."""
        if self.depth + 1 > MAX_DEPTH:
            self.flag('substitutions nested deeper than %d' % MAX_DEPTH)
        sc = _Scan(self.s, self.shell, self.depth + 1, self.parsed)
        sc.i = self.i
        sc.n = self.n
        cmds = sc.commands(closer=')')
        if not sc.closed:
            self.flag('an unclosed substitution')
        self.i = sc.i
        return cmds

    # -- tokens
    def tokens(self, closer=None):
        self.closed = False
        depth = 0
        while self.i < self.n:
            c = self.s[self.i]
            if closer and c == closer and depth == 0:
                self.i += 1
                self.closed = True
                return
            if self.space(c):
                self.i += 1
                continue
            if c == '\r':
                if self.shell == PS or self.peek(1) != '\n':
                    self.op('\n', 1)
                else:
                    self.i += 1
                continue
            if c == '\n':
                self.op('\n', 1)
                self.read_heredocs()
                continue
            if self.shell == BASH and c == '\\' and self.peek(1) == '\n':
                self.i += 2
                continue
            if self.shell == PS and c == '`' and self.peek(1) in ('\n', '\r'):
                self.i += 3 if self.s[self.i:self.i + 3] == '`\r\n' else 2
                continue
            if self.shell == PS and self.startswith('<#'):
                j = self.s.find('#>', self.i + 2)
                self.i = self.n if j < 0 else j + 2
                continue
            if c == '#' and self.shell != CMD:
                j = self.s.find('\n', self.i)
                self.i = self.n if j < 0 else j
                continue
            if self.shell == PS and self.startswith('--%'):
                self.i += 3
                self.stop_parsing()
                continue
            o = self.operator()
            if o is not None:
                if o == '(':
                    depth += 1
                elif o == ')':
                    if depth == 0:
                        self.flag('an unmatched )')
                    depth = max(0, depth - 1)
                continue
            if self.redirect():
                continue
            self.word()

    def op(self, text, width):
        self.toks.append(('op', text))
        self.i += width

    def operator(self):
        c = self.s[self.i]
        two = self.s[self.i:self.i + 2]
        if two in ('&&', '||'):
            self.op(two, 2)
            return two
        if self.shell == BASH:
            if two in (';;', ';&'):
                self.flag('a case statement')
                self.op(';', 2)
                return ';'
            if two == '|&':
                self.op('|', 2)
                return '|'
            if c == '&' and self.peek(1) == '>':
                return None                     # &> is a redirection
            if c in ';|&()':
                self.op(c, 1)
                return c
            nx = self.peek(1)
            if c == '{' and (nx == '' or self.space(nx) or nx == '\n'):
                self.brace += 1
                self.op('{', 1)
                return '{'
            if c == '}' and self.brace > 0:
                self.brace -= 1
                self.op('}', 1)
                return '}'
            return None
        if self.shell == PS:
            if c == '(' and self.ps_paren_arg():
                # `Set-Content (Join-Path a b) x`: PowerShell runs what the ( ) holds and passes its
                # value, one argument (not literal) whose commands are read as nested ones;
                # `-Path:(...)` is that parameter's value, one word as `-Path:$x` is
                j = self.i
                self.i += 1
                cmds = self.sub_until()
                last = self.toks[-1]
                if self.s[j - 1] == ':':
                    w = last[1]
                    self.toks[-1] = ('w', Word(w.text + self.s[j:self.i], True, w.start, self.i), last[2] + cmds)
                else:
                    self.toks.append(('w', Word(self.s[j:self.i], True, j, self.i), cmds))
                return 'arg'
            if c in ';|(){}':
                self.op(c, 1)
                return c
            if c == '&':
                # the call operator: `& prog args` runs prog
                self.toks.append(('w', Word('&', False, self.i, self.i + 1), []))
                self.i += 1
                j = self.i
                while j < self.n and self.s[j] in ' \t':
                    j += 1
                if j < self.n and self.s[j] == '(':
                    # `& (Get-Command git) args`: the program is what the ( ) computes, one word
                    # (not literal) whose commands are read as nested ones
                    self.i = j + 1
                    cmds = self.sub_until()
                    self.toks.append(('w', Word(self.s[j:self.i], True, j, self.i), cmds))
                return '&'
            if c == ',':
                self.i += 1                     # an array: each element is an argument
                return ','
            return None
        if c in '&|()':
            self.op(c, 1)
            return c
        return None

    def ps_paren_arg(self):
        """True when the PowerShell ( at self.i opens an argument: it follows a word of the
        command after a space or a comma, or is the value of a `-Param:` or a `-Param` it touches
        (`-Destination(...)`, after the command's first word), and the word in first position (the
        command's first, or the first after an assignment's `=`) is no keyword that takes a ( )
        (if, foreach, while...): `Copy-Item data (...)` passes an argument. A ( touching any other
        word passes the word and then the ( ) as the next argument, as PowerShell reads it
        (`func(arg)`: `Copy-Item a.txt(...)`, `./x(...)`, `'x'(...)`, `$x(...)`, `[string](...)`),
        unless the word is a member access (_PS_MEMBER_HEAD/_TAIL: `$s.Replace(...)`,
        `'abc'.Replace(...)`, `[IO.Path]::Combine(...)`, `(Get-Date).AddDays(...)`): that ( calls
        the method, a group, and the word, the method's result, is marked not literal. A ( that
        starts a command is a group too; one that touches a group's ) (`$a.M(1)(...)`) as well,
        and the command it cuts is marked split (commands)."""
        if not self.toks or self.toks[-1][0] != 'w':
            return False
        last = self.toks[-1][1]
        before = self.s[self.i - 1] if self.i else ''
        words = []
        for t in reversed(self.toks):
            if t[0] == 'op':
                break
            if t[0] == 'w':
                words.insert(0, t[1])
        touch = last.end == self.i and last.start < last.end
        src = self.s[last.start:last.end] if touch else ''
        if touch and _PS_MEMBER_TAIL.search(src) and (
                _PS_MEMBER_HEAD.match(src) or (src[0] == '.' and self.s[last.start - 1:last.start] in (')', ']', '}'))):
            # `$s.Replace(...)`: a method call, its value only known at run time
            self.toks[-1] = ('w', Word(last.text, True, last.start, last.end, last.synth, last.alt), self.toks[-1][2])
            return False
        param = touch and last.text.startswith('-') and len(last.text) > 1 and not last.dyn
        plain = touch and (src[0] != '-' or ':' in src)        # -Path:$x(...) too, not -Path(...)
        if not (self.space(before) or before == ',' or (before == ':' and param) or plain or
                (param and before != ':' and len(words) > 1)):
            return False
        first = words[0]
        for k, w in enumerate(words[:-1]):
            if w.text in ('=', '+=', '-=', '*=', '/=', '%=', '??='):
                first = words[k + 1]            # `$r = if (...)`: the statement after it
        return first.text.lower() not in PS_PAREN_KEYWORDS

    _BASH_REDIR = re.compile(r'(\d+|&)?(>>|>\||>&|>|<<<|<<-|<<|<&|<>|<)')
    _PS_REDIR = re.compile(r'(\d|\*)?(>>|>|<)(&\d)?')
    _CMD_REDIR = re.compile(r'(\d)?(>>|>|<)(&\d)?')

    def redirect(self):
        rx = {BASH: self._BASH_REDIR, PS: self._PS_REDIR, CMD: self._CMD_REDIR}[self.shell]
        m = rx.match(self.s, self.i)
        if not m:
            return False
        fd, op = m.group(1) or '', m.group(2)
        if self.shell == BASH and op in ('<', '>') and not fd and self.s[m.end():m.end() + 1] == '(':
            start = self.i
            self.i = m.end() + 1
            cmds = self.sub_until()
            self.toks.append(('w', Word('%s(...)' % op, True, start, self.i), cmds))
            return True
        self.i = m.end()
        if self.shell == BASH:
            if op == '>|':
                op = '>'
            if op in ('>&', '<&'):
                t = self.target()
                if t is None or re.fullmatch(r'\d+-?|-', t.text):
                    self.toks.append(('r', Redirect(op[0], fd or '1', None, dup=True)))
                else:
                    self.toks.append(('r', Redirect('>', '&', t)))       # >&file: both streams
                return True
            if op in ('<<', '<<-'):
                t = self.target(raw=True)
                if t is None:
                    self.flag('a heredoc without a delimiter')
                    return True
                quoted = any(q in t.text for q in '\'"\\')
                delim = t.text.replace("'", '').replace('"', '').replace('\\', '')
                r = Redirect('<<', fd or '0', None, body=True)
                self.toks.append(('r', r))
                self.heredocs.append((delim, op == '<<-', quoted, r))
                return True
            if op == '<>':
                op = '>'
            t = self.target()
            if t is None:
                self.flag('a redirection without a target')
            self.toks.append(('r', Redirect(op, fd or ('0' if op.startswith('<') else '1'), t)))
            return True
        if m.group(3):
            self.toks.append(('r', Redirect(op, fd or '1', None, dup=True)))
            return True
        t = self.target()
        if t is None:
            self.flag('a redirection without a target')
        self.toks.append(('r', Redirect(op, fd or ('0' if op == '<' else '1'), t)))
        return True

    def target(self, raw=False):
        while self.i < self.n and self.space(self.s[self.i]):
            self.i += 1
        if self.i >= self.n or self.s[self.i] in '\r\n;|&()<>':
            return None
        if raw:
            j = self.i
            while j < self.n and not self.space(self.s[j]) and self.s[j] not in '\r\n;|&()<>':
                j += 1
            w = Word(self.s[self.i:j], False, self.i, j)
            self.i = j
            return w
        before = len(self.toks)
        self.word()
        if len(self.toks) == before:
            return None
        _kind, w, subs = self.toks.pop()
        if subs:
            self.toks.append(('subs', subs))
        return w

    def read_heredocs(self):
        """At a newline: the bodies of the heredocs opened on the line before, consumed as data."""
        while self.heredocs:
            delim, strip, quoted, r = self.heredocs.pop(0)
            start = self.i
            body = []
            ended = False
            while self.i < self.n:
                j = self.s.find('\n', self.i)
                line_end = self.n if j < 0 else j
                line = self.s[self.i:line_end].rstrip('\r')
                self.i = self.n if j < 0 else j + 1
                if (line.lstrip('\t') if strip else line) == delim:
                    ended = True
                    break
                body.append(line)
            if not ended:
                self.flag('a heredoc whose end line %r never comes' % delim)
            r.target = Word('\n'.join(body), not quoted, start, self.i)

    def stop_parsing(self):
        """PowerShell --%: the rest of the line is passed as written."""
        while self.i < self.n and self.s[self.i] not in '\r\n|':
            if self.space(self.s[self.i]):
                self.i += 1
                continue
            j = self.i
            while j < self.n and not self.space(self.s[j]) and self.s[j] not in '\r\n|':
                j += 1
            self.toks.append(('w', Word(self.s[self.i:j], '%' in self.s[self.i:j], self.i, j), []))
            self.i = j

    # -- words
    def word(self):
        start = self.i
        out, dyn, subs = [], False, []
        sh, s = self.shell, self.s
        self._kept = []             # bash: indexes in out of backslashes bash would drop
        self._synth = False
        self._brace = None          # bash: (index in out, [alternatives]) of an unquoted {a,b}
        if sh == PS and (self.startswith("@'") or self.startswith('@"')):
            rest = s[self.i + 2:self.i + 4]
            if rest[:1] == '\n' or rest == '\r\n':
                q = s[self.i + 1]
                j = s.find('\n' + q + '@', self.i + 2)
                if j < 0:
                    self.flag('an unclosed here-string')
                    self.i = self.n
                    return
                body = s[s.find('\n', self.i) + 1:j].rstrip('\r')
                if q == '"' and '$' in body:
                    dyn = True
                    if '$(' in body:
                        self.flag('a substitution inside a here-string')
                self.i = j + 3
                self.toks.append(('w', Word(body, dyn, start, self.i), []))
                return
        if sh == PS and self.startswith('@{'):
            j = self.balanced(self.i + 1, '{', '}')
            self.i = j
            self.toks.append(('w', Word(s[start:j], True, start, j), []))
            return
        if sh == PS and self.startswith('@('):
            self.i += 2
            cmds = self.sub_until()
            self.toks.append(('w', Word('@(...)', True, start, self.i), cmds))
            return
        index = 0                   # PowerShell: [ ] open in the word
        while self.i < self.n:
            c = s[self.i]
            if self.space(c) or c in '\r\n':
                break
            if sh == PS and c == '(' and index:
                # `$x[(sv PWD C:\m)]`: a ( inside the word's [ ] index is part of the word, and its
                # commands nested ones
                j = self.i
                self.i += 1
                subs = subs + self.sub_until()
                out.append(s[j:self.i])
                dyn = True
                continue
            if sh == BASH:
                if c in ';|&()<>':
                    break
                if c == '}' and self.brace > 0:
                    break
                if c == '\\':
                    nx = self.peek(1)
                    if nx == '\n':
                        self.i += 2
                    elif nx in (' ', '\t') and ''.join(out).endswith(':'):
                        out.append('\\')            # C:\ before a space: a drive root, as meant
                        self.i += 1
                    elif nx and nx in BASH_ESC:
                        out.append(nx)
                        self.i += 2
                    else:
                        self._kept.append(len(out))
                        out.append('\\')
                        self.i += 1
                    continue
                if c == '{' and self._brace is None:
                    m = _BRACE.match(s, self.i)
                    if m:
                        self._brace = (len(out), m.group(1).split(','))
                        out.append('')
                        self.i = m.end()
                        continue
                if c == "'":
                    j = s.find("'", self.i + 1)
                    if j < 0:
                        self.flag('an unclosed quote')
                        j = self.n
                    out.append(s[self.i + 1:j])
                    self.i = j + 1
                    continue
                if c == '$' and self.peek(1) == "'":
                    self.ansi_c(out)
                    continue
                if c == '"':
                    d, sb = self.bash_dq(out)
                    dyn, subs = dyn or d, subs + sb
                    continue
                if c == '$':
                    d, sb = self.dollar(out)
                    dyn, subs = dyn or d, subs + sb
                    continue
                if c == '`':
                    subs = subs + self.backtick(out)
                    dyn = True
                    continue
                out.append(c)
                self.i += 1
                continue
            if sh == PS:
                if c in ';|(){},<>':
                    break
                if c == '&' and self.peek(1) == '&':
                    break
                if c == '>' or (c in '0123456789*' and self.peek(1) == '>' and not out
                                and self.i == start):
                    break
                if c == '`':
                    nx = self.peek(1)
                    out.append(PS_ESC.get(nx, nx))
                    self.i += 2
                    continue
                if c == "'":
                    self.ps_sq(out)
                    continue
                if c == '"':
                    d, sb = self.ps_dq(out)
                    dyn, subs = dyn or d, subs + sb
                    continue
                if c == '$':
                    d, sb = self.dollar(out)
                    dyn, subs = dyn or d, subs + sb
                    continue
                if c == '[':
                    index += 1
                elif c == ']' and index:
                    index -= 1
                out.append(c)
                self.i += 1
                continue
            # cmd
            if c in '&|()<>':
                break
            if c == '^':
                out.append(self.peek(1))
                self.i += 2
                continue
            if c == '"':
                j = s.find('"', self.i + 1)
                if j < 0:
                    self.flag('an unclosed quote')
                    j = self.n
                part = s[self.i + 1:j]
                dyn = dyn or '%' in part or '!' in part
                out.append(part)
                self.i = j + 1
                continue
            if c in '%!':
                dyn = True
            out.append(c)
            self.i += 1
        if self.i == start:
            # a character no rule consumed (a stray > in PowerShell): a word of its own
            out.append(s[self.i])
            self.i += 1
        if self._brace is not None:
            # bash expands {a,b} into one word per alternative
            k, alts = self._brace
            for a in alts:
                parts = list(out)
                parts[k] = a
                self.toks.append(('w', Word(''.join(parts), dyn, start, self.i, synth=True), subs))
                subs = []
            return
        alt = None
        if self._kept:
            alt = ''.join(p for k, p in enumerate(out) if k not in self._kept)
        self.toks.append(('w', Word(''.join(out), dyn, start, self.i, synth=self._synth, alt=alt), subs))

    def balanced(self, j, open_, close):
        depth = 0
        while j < self.n:
            c = self.s[j]
            if c in '\'"':
                k = self.s.find(c, j + 1)
                j = self.n if k < 0 else k + 1
                continue
            if c == open_:
                depth += 1
            elif c == close:
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        self.flag('an unclosed %s' % open_)
        return self.n

    def ansi_c(self, out):
        """$'...': the escapes bash decodes. A decoded escape other than a quote or a backslash
        marks the word synthetic: the text the guard reads is not the text written (in a program
        name, commands.expand marks the parse unclean)."""
        s, j, buf = self.s, self.i + 2, []
        while j < self.n and s[j] != "'":
            if s[j] == '\\' and j + 1 < self.n:
                nx = s[j + 1]
                m = _ANSI_NUM.match(s, j + 1)
                if m:
                    e = m.group(0)
                    if e[0] == 'c':
                        ch = chr(ord(e[1]) & 0x1f)
                    elif e[0] in 'xuU':
                        ch = chr(int(e[1:], 16))
                    else:
                        ch = chr(int(e, 8) & 0xff)
                    buf.append(ch)
                    self._synth = True
                    j = m.end()
                    continue
                if nx in _ANSI:
                    buf.append(_ANSI[nx])
                    if nx not in '\\\'"':
                        self._synth = True
                    j += 2
                    continue
                buf.append('\\' + nx)
                j += 2
                continue
            buf.append(s[j])
            j += 1
        if j >= self.n:
            self.flag('an unclosed quote')
        out.append(''.join(buf))
        self.i = j + 1

    def ps_sq(self, out):
        s, j, buf = self.s, self.i + 1, []
        while True:
            k = s.find("'", j)
            if k < 0:
                self.flag('an unclosed quote')
                buf.append(s[j:])
                j = self.n
                break
            buf.append(s[j:k])
            if s[k + 1:k + 2] == "'":
                buf.append("'")
                j = k + 2
                continue
            j = k + 1
            break
        out.append(''.join(buf))
        self.i = j

    def bash_dq(self, out):
        s, dyn, subs = self.s, False, []
        self.i += 1
        while self.i < self.n:
            c = s[self.i]
            if c == '"':
                self.i += 1
                return dyn, subs
            if c == '\\' and self.peek(1) in ('$', '`', '"', '\\', '\n'):
                if self.peek(1) != '\n':
                    out.append(self.peek(1))
                self.i += 2
                continue
            if c == '$':
                d, sb = self.dollar(out)
                dyn, subs = dyn or d, subs + sb
                continue
            if c == '`':
                subs = subs + self.backtick(out)
                dyn = True
                continue
            out.append(c)
            self.i += 1
        self.flag('an unclosed quote')
        return dyn, subs

    def ps_dq(self, out):
        s, dyn, subs = self.s, False, []
        self.i += 1
        while self.i < self.n:
            c = s[self.i]
            if c == '"':
                if self.peek(1) == '"':
                    out.append('"')
                    self.i += 2
                    continue
                self.i += 1
                return dyn, subs
            if c == '`':
                nx = self.peek(1)
                out.append(PS_ESC.get(nx, nx))
                self.i += 2
                continue
            if c == '$':
                d, sb = self.dollar(out)
                dyn, subs = dyn or d, subs + sb
                continue
            out.append(c)
            self.i += 1
        self.flag('an unclosed quote')
        return dyn, subs

    def dollar(self, out):
        """At a $: a variable or a substitution, its text kept as written. (dyn, subs)."""
        s, i = self.s, self.i
        nx = self.peek(1)
        if nx == '(':
            if self.shell == BASH and self.peek(2) == '(':
                j = s.find('))', i + 3)
                j = self.n if j < 0 else j + 2
                out.append(s[i:j])
                self.i = j
                return True, []
            self.i += 2
            cmds = self.sub_until()
            out.append(s[i:self.i])
            return True, cmds
        if nx == '{':
            j = self.balanced(i + 1, '{', '}')
            inner = s[i + 2:j - 1]
            out.append(s[i:j])
            self.i = j
            subs = []
            if '$(' in inner or '`' in inner:
                # ${x:-$(cmd)} runs cmd: the substitutions inside are commands of their own
                self.flag('a substitution inside ${...}')
                for c in self.nested(inner):
                    subs.extend(c.subs)
            return True, subs
        m = (_PS_VAR if self.shell == PS else _BASH_VAR).match(s, i)
        if not m:
            out.append('$')
            self.i += 1
            return False, []
        out.append(m.group(0))
        self.i = m.end()
        if self.shell == PS and m.group(0).lower() in ('$true', '$false', '$null'):
            return False, []
        return True, []

    def backtick(self, out):
        s, i = self.s, self.i
        j = i + 1
        while j < self.n and s[j] != '`':
            j += 2 if s[j] == '\\' else 1
        if j >= self.n:
            self.flag('an unclosed backtick substitution')
            j = self.n
        inner = s[i + 1:j].replace('\\`', '`')
        out.append(s[i:j + 1])
        self.i = j + 1
        return self.nested(inner)

    # -- commands
    def commands(self, closer=None):
        self.tokens(closer)
        if self.heredocs:
            self.read_heredocs()
        out = []
        stack = []
        ids = [0]

        def new(prev):
            c = Command(self.s, self.shell, self.depth, getattr(self.parsed, 'home', None))
            c.groups = tuple(stack)
            c.prev_op = prev
            return c

        def keep(c):
            if c.words or c.redirects:
                out.append(c)
                return True
            return False

        # PowerShell: a command a ( or { cuts (`Set-Content -Value $s.Trim() -Path x`) is split when
        # more of the line follows the group's close before a separator, words or another group
        ps = self.shell == PS
        cuts = []                   # per open group, the command it cut (None: it cut none)
        after = None                # the command the group that just closed cut

        def go_on():
            if ps and after is not None:
                after.split = True

        cur = new(None)
        for t in self.toks:
            kind = t[0]
            if kind == 'op':
                o = t[1]
                if o in ('(', '{'):
                    go_on()                     # `$a.M(1)(...)`: a group right after a group
                    cuts.append(cur if cur.words or cur.redirects else after)
                    after = None
                elif o in (')', '}') and stack:
                    after = cuts.pop()
                else:
                    after = None                # ; && || | newline: the command ended
                if o == '|':
                    src = None
                    if keep(cur):
                        cur.piped_out = True
                        cur.next_op = '|'
                        src = cur
                    cur = new('|')
                    cur.piped_in = True
                    cur.pipe_from = src
                    continue
                if keep(cur):
                    cur.next_op = o
                if o in ('(', '{'):
                    ids[0] += 1
                    stack.append((ids[0], o))
                elif o in (')', '}') and stack:
                    stack.pop()
                cur = new(o)
                continue
            if kind == 'subs':
                go_on()
                after = None
                cur.subs.extend(t[1])
                continue
            if kind == 'r':
                r = t[1]
                cur.redirects.append(r)
                if r.target is not None and not r.body:
                    self.span(cur, r.target)
                continue
            go_on()
            after = None
            w, subs = t[1], t[2]
            cur.words.append(w)
            cur.subs.extend(subs)
            self.span(cur, w)
        keep(cur)
        for c in out:
            for sc in c.subs:
                sc.nested_in = c
        return out

    @staticmethod
    def span(cmd, w):
        if cmd.start is None or w.start < cmd.start:
            cmd.start = w.start
        if cmd.end is None or w.end > cmd.end:
            cmd.end = w.end


def _walk(cmds):
    for c in cmds:
        yield c
        for x in _walk(c.subs):
            yield x


# ---------------------------------------------------------------- directories

def msys_to_win(p):
    m = re.match(r'^/([a-zA-Z])(/.*)?$', p)
    return (m.group(1).upper() + ':' + (m.group(2) or '/')) if m else p


# A PowerShell provider-qualified path: [<module>\]<provider>::<rest>. FileSystem (bare or from
# its own module) holds files, so its <rest>, when absolute, is the file; any other provider, or
# a module the guard does not know, is a place it cannot tell.
PS_PROVIDER_RE = re.compile(r'^(?:([^\\/:]+)\\)?([^\\/:]+)::(.*)$', re.S)
PS_FILESYSTEM_MODULES = (None, 'microsoft.powershell.core')
# A PowerShell drive whose name is anything but one ASCII letter (Main:\x, Temp:\x, env:FOO, 1:\x,
# _:\x, a non-ASCII letter): a drive the session may have made with New-PSDrive, which takes such
# names, so the text cannot tell its folder.
PS_LONG_DRIVE_RE = re.compile(r'^(?![A-Za-z]:)[^\\/:]+:')
_ABS_RE = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\|//)')


def ps_filesystem_path(p):
    """The file path a PowerShell `FileSystem::<abs>` (or
    `Microsoft.PowerShell.Core\\FileSystem::<abs>`) spells, else None."""
    m = PS_PROVIDER_RE.match(p or '')
    if not m or m.group(2).lower() != 'filesystem':
        return None
    module = m.group(1).lower() if m.group(1) is not None else None
    if module not in PS_FILESYSTEM_MODULES or not _ABS_RE.match(m.group(3)):
        return None
    return m.group(3)


def ps_unplaceable(p):
    """True when PowerShell reads the text `p` as a place the guard cannot tell: a
    provider-qualified path other than an absolute FileSystem:: one, or a drive whose name is not
    one ASCII letter. A one-letter drive (absolute or drive-relative) and a relative path are not."""
    if not p or p[:2] in ('\\\\', '//'):
        return False
    if PS_PROVIDER_RE.match(p):
        return ps_filesystem_path(p) is None
    return bool(PS_LONG_DRIVE_RE.match(p))


def home_folder(home=None):
    """`home`, else the user's home folder as kitlib.config.user.home_dir() gives it, so the
    tests' home fence applies (docs/test-tools.md "Home fence"). Its HomeFenced is a
    BaseException: it passes every `except Exception` in the guard and kitlib.host.hookmain, and
    boot/hook.py run() blocks the call."""
    return home or kuser.home_dir()


# a word's text that starts with a variable naming the home folder, in any shell's spelling,
# then ends or goes on with a separator: $HOME ${HOME} $USERPROFILE $env:USERPROFILE ${env:HOME}
# %USERPROFILE%, and the drive and path pair $env:HOMEDRIVE$env:HOMEPATH ${HOMEDRIVE}${HOMEPATH}
# %HOMEDRIVE%%HOMEPATH% (case ignored: PowerShell and cmd ignore it, and only a deny reads it)
HOME_VAR_RE = re.compile(r'(?i)^(?:(?:\$(?:env:)?HOMEDRIVE|\$\{(?:env:)?HOMEDRIVE\})'
                         r'(?:\$(?:env:)?HOMEPATH|\$\{(?:env:)?HOMEPATH\})|%HOMEDRIVE%%HOMEPATH%|'
                         r'\$(?:env:)?(?:HOME|USERPROFILE)|\$\{(?:env:)?(?:HOME|USERPROFILE)\}|'
                         r'%(?:HOME|USERPROFILE)%)(?=$|[\\/])')
# bash cd's own options (cd -P, cd -L -e)
BASH_CD_OPT_RE = re.compile(r'^-[LPe@]+$')


def home_text(text, home=None, dyn=False):
    """`text` with a leading `~` (alone, or before a separator) as the home folder (home_folder);
    other text as it is. With `dyn` (a word the shell fills in), only a leading home variable
    (HOME_VAR_RE) is read, as the home folder, when nothing after it is filled in too; None
    otherwise. It is a reading for the rules that only deny: a line can set HOME elsewhere."""
    if dyn:
        m = HOME_VAR_RE.match(text)
        rest = text[m.end():] if m else None
        if rest is None or any(c in rest for c in '$%`'):
            return None
        return home_folder(home) + rest
    if text == '~' or text[:2] in ('~/', '~\\'):
        return home_folder(home) + text[1:]
    return text


def resolve_path(p, cwd, shell=None, home=None):
    """`p` (a word's text) as an absolute normalised path, or None when the text cannot tell (a
    drive-relative C:x, ~user, a rooted \\x with no cwd to take the drive from). With `shell` PS,
    `FileSystem::<abs>` is <abs> and any other provider or a drive not named by one ASCII letter
    is None (ps_unplaceable); bash and cmd text is read as it always was. `~` is `home`
    (home_folder)."""
    if not p:
        return None
    if shell == PS:
        fs = ps_filesystem_path(p)
        if fs is not None:
            p = fs
        elif ps_unplaceable(p):
            return None
    if p.startswith('~'):
        if p == '~' or p[1:2] in ('/', '\\'):
            p = home_folder(home) + p[1:]
        else:
            return None
    p = msys_to_win(p)
    if re.match(r'^[A-Za-z]:([\\/]|$)', p):
        return paths.norm(p if len(p) > 2 else p + '\\', fold=False)
    if re.match(r'^[A-Za-z]:', p):
        return None
    if p[:2] in ('\\\\', '//'):
        return paths.norm(p, fold=False)
    if not cwd:
        return None
    if p[:1] in ('\\', '/'):
        drive = os.path.splitdrive(cwd)[0]
        return paths.norm(drive + p, fold=False) if drive else None
    return paths.norm(os.path.join(cwd, p), fold=False)


def cd_words(cmd):
    """The command's words, with a bash `builtin` or `command` before a cd or popd left out
    (`builtin cd ~`, `command cd x`: the cd itself runs)."""
    words = cmd.words
    if (cmd.shell == BASH and len(words) > 1 and not words[0].dyn and words[0].text in ('builtin', 'command')
            and words[1].text.lower() in CD_VERBS | CD_UNKNOWN):
        return words[1:]
    return words


def cd_target(cmd, cwd, home=None, home_vars=False):
    """(is a cd, the directory after it): None when the text cannot tell. `~` is `home`
    (home_folder); with `home_vars`, a target that starts with a home variable (home_text) is
    read too, bash cd's own options and a `--` are passed over, and a bash or PowerShell cd with
    no place goes home (bash's cd, and PowerShell 7's; Windows PowerShell's stays put, but only a
    deny reads it). A bash cd behind `builtin` or `command` is the cd itself (cd_words), and
    PowerShell's `cd $null` is a cd with no place."""
    words = cd_words(cmd)
    if not words:
        return False, cwd
    verb = words[0].text.lower()
    if verb in CD_UNKNOWN:
        return True, None
    if verb not in CD_VERBS:
        return False, cwd
    rest = words[1:]
    if cmd.shell == PS and len(rest) == 1 and rest[0].text.lower() == '$null':
        rest = []                               # no place, never a folder named $null
    if rest and rest[0].text.lower() in ('-path', '-literalpath', '-lp'):
        rest = rest[1:]
    elif rest and verb in ('cd', 'chdir') and rest[0].text.lower() == '/d':
        rest = rest[1:]
    if home_vars and cmd.shell in (BASH, PS):
        while cmd.shell == BASH and rest and not rest[0].dyn and BASH_CD_OPT_RE.match(rest[0].text):
            rest = rest[1:]
        if rest and not rest[0].dyn and rest[0].text == '--':
            rest = rest[1:]
        if not rest and verb in ('cd', 'chdir', 'set-location', 'sl'):
            return True, resolve_path(home_folder(home), cwd, cmd.shell, home)
    if len(rest) != 1:
        return True, None
    p = rest[0].text
    if rest[0].dyn:
        p = home_text(p, home, dyn=True) if home_vars else None
        if p is None:
            return True, None
    if p in ('-', '+') or p.startswith('-') or any(ch in p for ch in '*?'):
        return True, None
    return True, resolve_path(p, cwd, cmd.shell, home)


def ps_cd_unplaceable(cmd):
    """True when `cmd` is a PowerShell cd whose one literal place is one ps_unplaceable names
    (cd Main:\\, Set-Location -LiteralPath Registry::x)."""
    if cmd.shell != PS or len(cmd.words) < 2 or cmd.words[0].text.lower() not in CD_VERBS:
        return False
    rest = cmd.words[1:]
    if rest[0].text.lower() in ('-path', '-literalpath', '-lp'):
        rest = rest[1:]
    return len(rest) == 1 and not rest[0].dyn and ps_unplaceable(rest[0].text)


BASH_PREFIX_WORDS = {'if', 'then', 'else', 'elif', 'do', 'while', 'until', '!', 'time'}


def _keyword_cd(c):
    """A cd behind a bash keyword (if cd x; then): it runs, but whether it ran is unknown."""
    if c.shell != BASH:
        return False
    k = 0
    while k < len(c.words) and c.words[k].text in BASH_PREFIX_WORDS:
        k += 1
    first = k
    if k + 1 < len(c.words) and c.words[k].text in ('builtin', 'command'):
        k += 1
    return 0 < first and k < len(c.words) and c.words[k].text.lower() in CD_VERBS | CD_UNKNOWN


def track(cmds, cwd, ps_drive=False, home=None, cwd_home=None):
    """Each command's cwd, in order: a cd changes it for the commands after it. A bash ( ) group
    is a subshell: a cd inside it holds to its end, then the directory before it comes back; a
    bash { } group shares the directory. A cd the text cannot tell, one inside a PowerShell or
    cmd group, one behind a keyword, one in a pipeline, in the background or beside || makes the
    directory unknown (None) from there on. Substitutions run in the directory of the command
    that holds them; a cd inside a PowerShell one makes its holder's directory unknown, and every
    later one's. A command whose cwd is None after a PowerShell cd into a place
    ps_unplaceable names (cd Main:\\) gets cwd_ps_drive, until a cd reaches a known place. `~` is
    `home` (home_folder). cwd_home is the directory tracked from `cwd_home` (default `cwd`) with
    a cd to a home variable read too (cd_target home_vars): only for the rules that deny, since a
    line can set the variable to another folder."""
    here, drive = cwd, ps_drive
    here_h = cwd if cwd_home is None else cwd_home
    open_ = []                  # (group, the directory before it, its drive flag, its cwd_home)
    sub_from = {}               # id(command): (cwd, drive flag, cwd_home) its substitutions start in
    for c in cmds:
        common = 0
        while common < len(open_) and common < len(c.groups) and open_[common][0] == c.groups[common]:
            common += 1
        while len(open_) > common:
            g, before, before_drive, before_h = open_.pop()
            if g[1] == '(' and c.shell == BASH:
                here, drive, here_h = before, before_drive, before_h
        for g in c.groups[common:]:
            open_.append((g, here, drive, here_h))
        sub_from[id(c)] = (here, drive, here_h)
        c.subs_cwd = here
        # PowerShell runs a $( ) or an argument's ( ) in the caller's scope: a cd there moves this
        # command and every one after it (bash's $( ) is a subshell)
        if c.shell == PS and any(cd_target(x, None, home)[0] for x in flatten(c.subs)):
            here = here_h = None
        c.cwd, c.cwd_home = here, here_h
        c.cwd_ps_drive = drive and here is None
        changed, to = cd_target(c, here, home)
        to_h = cd_target(c, here_h, home, home_vars=True)[1]
        if _keyword_cd(c):
            changed, to, to_h = True, None, None
        if changed:
            unsure = (c.piped_in or c.piped_out or '||' in (c.prev_op, c.next_op) or (c.next_op == '&' and c.shell == BASH)
                      or (c.groups and c.shell != BASH))
            here = None if unsure else to
            here_h = None if unsure else to_h
            drive = ps_cd_unplaceable(c) or (drive and here is None)
    for c in cmds:
        if c.subs:
            at, at_drive, at_h = sub_from[id(c)]
            track(c.subs, at, at_drive and at is None, home, at_h)
    return cmds


def flatten(cmds):
    """Every command, substitutions included, each substitution before its holder."""
    out = []
    for c in cmds:
        out.extend(flatten(c.subs))
        out.append(c)
    return out


def parse(text, shell, cwd, depth=0, parsed=None, home=None, cwd_home=None):
    """Parsed for `text` read as `shell` (BASH, PS or CMD), starting in `cwd` (and `cwd_home`,
    default `cwd`: track). `~` is `home` (home_folder). With `parsed`, the commands are added to
    it (a nested shell's text) and its home is the one read."""
    parsed = parsed if parsed is not None else Parsed(text, shell, cwd, home)
    if depth > MAX_DEPTH:
        parsed.flag('shells nested deeper than %d' % MAX_DEPTH)
        return parsed, []
    sc = _Scan(text, shell, depth, parsed)
    top = sc.commands()
    track(top, cwd, home=parsed.home, cwd_home=cwd_home)
    cmds = flatten(top)
    parsed.commands.extend(cmds)
    return parsed, cmds
