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

BASH, PS, CMD = 'bash', 'ps', 'cmd'
MAX_DEPTH = 6

# PowerShell splits words on every whitespace character it knows; bash only on space and tab.
PS_SPACE = (' \t\f\v\u00a0\u0085\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008'
            '\u2009\u200a\u2028\u2029\u202f\u205f\u3000')
BASH_ESC = ' \t"\'\\$`&|;<>(){}#*?[]!~'
CD_VERBS = {'cd', 'chdir', 'set-location', 'sl', 'push-location', 'pushd'}
CD_UNKNOWN = {'popd', 'pop-location'}
PS_ESC = {'n': '\n', 't': '\t', 'r': '\r', '0': '\0', 'a': '\a', 'b': '\b', 'f': '\f', 'v': '\v',
          'e': '\x1b'}
_PS_VAR = re.compile(r'\$(?:[A-Za-z_]\w*:)?[\w?^$]+')
_BASH_VAR = re.compile(r'\$(?:[A-Za-z_]\w*|\d|[@*#?$!\-])')
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
                 'pipe_from', 'git_env')

    def __init__(self, source, shell, depth):
        self.words, self.redirects, self.subs = [], [], []
        self.start = self.end = None
        self.source, self.shell, self.depth = source, shell, depth
        self.cwd = None
        self.piped_out = self.piped_in = False
        self.groups = ()            # the ( ) or { } groups it is inside, outermost first: (id, '(' or '{')
        self.prev_op = self.next_op = None      # the operators around it: ; && || & newline ( ) { } |
        self.pipe_from = None       # the Command piping into this one
        self.nested_in = None       # the Command whose word held this one ($(), a nested shell)
        self.git_env = False        # the GIT_DIR-like variable some command of the line sets (commands.mark_git_env), else False

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
    def __init__(self, text, shell, cwd):
        self.text, self.shell, self.start_cwd = text, shell, cwd
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
        while self.i < self.n:
            c = s[self.i]
            if self.space(c) or c in '\r\n':
                break
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
            c = Command(self.s, self.shell, self.depth)
            c.groups = tuple(stack)
            c.prev_op = prev
            return c

        def keep(c):
            if c.words or c.redirects:
                out.append(c)
                return True
            return False

        cur = new(None)
        for t in self.toks:
            kind = t[0]
            if kind == 'op':
                o = t[1]
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
                cur.subs.extend(t[1])
                continue
            if kind == 'r':
                r = t[1]
                cur.redirects.append(r)
                if r.target is not None and not r.body:
                    self.span(cur, r.target)
                continue
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


def resolve_path(p, cwd):
    """`p` (a word's text) as an absolute normalised path, or None when the text cannot tell (a
    drive-relative C:x, ~user, a rooted \\x with no cwd to take the drive from)."""
    if not p:
        return None
    if p.startswith('~'):
        if p == '~' or p[1:2] in ('/', '\\'):
            p = os.path.expanduser('~') + p[1:]
        else:
            return None
    p = msys_to_win(p)
    if re.match(r'^[A-Za-z]:([\\/]|$)', p):
        return os.path.normpath(p if len(p) > 2 else p + '\\')
    if re.match(r'^[A-Za-z]:', p):
        return None
    if p[:2] in ('\\\\', '//'):
        return os.path.normpath(p)
    if not cwd:
        return None
    if p[:1] in ('\\', '/'):
        drive = os.path.splitdrive(cwd)[0]
        return os.path.normpath(drive + p) if drive else None
    return os.path.normpath(os.path.join(cwd, p))


def cd_target(cmd, cwd):
    """(is a cd, the directory after it): None when the text cannot tell."""
    if not cmd.words:
        return False, cwd
    verb = cmd.words[0].text.lower()
    if verb in CD_UNKNOWN:
        return True, None
    if verb not in CD_VERBS:
        return False, cwd
    rest = cmd.words[1:]
    if rest and rest[0].text.lower() in ('-path', '-literalpath', '-lp'):
        rest = rest[1:]
    elif rest and verb in ('cd', 'chdir') and rest[0].text.lower() == '/d':
        rest = rest[1:]
    if len(rest) != 1 or rest[0].dyn:
        return True, None
    p = rest[0].text
    if p in ('-', '+') or p.startswith('-') or any(ch in p for ch in '*?'):
        return True, None
    return True, resolve_path(p, cwd)


BASH_PREFIX_WORDS = {'if', 'then', 'else', 'elif', 'do', 'while', 'until', '!', 'time'}


def _keyword_cd(c):
    """A cd behind a bash keyword (if cd x; then): it runs, but whether it ran is unknown."""
    if c.shell != BASH:
        return False
    k = 0
    while k < len(c.words) and c.words[k].text in BASH_PREFIX_WORDS:
        k += 1
    return 0 < k < len(c.words) and c.words[k].text.lower() in CD_VERBS | CD_UNKNOWN


def track(cmds, cwd):
    """Each command's cwd, in order: a cd changes it for the commands after it. A bash ( ) group
    is a subshell: a cd inside it holds to its end, then the directory before it comes back; a
    bash { } group shares the directory. A cd the text cannot tell, one inside a PowerShell or
    cmd group, one behind a keyword, one in a pipeline, in the background or beside || makes the
    directory unknown (None) from there on. Substitutions run in the directory of the command
    that holds them."""
    here = cwd
    open_ = []                  # (group, the directory before it)
    for c in cmds:
        common = 0
        while common < len(open_) and common < len(c.groups) and open_[common][0] == c.groups[common]:
            common += 1
        while len(open_) > common:
            g, before = open_.pop()
            if g[1] == '(' and c.shell == BASH:
                here = before
        for g in c.groups[common:]:
            open_.append((g, here))
        c.cwd = here
        changed, to = cd_target(c, here)
        if _keyword_cd(c):
            changed, to = True, None
        if changed:
            unsure = (c.piped_in or c.piped_out or '||' in (c.prev_op, c.next_op) or (c.next_op == '&' and c.shell == BASH)
                      or (c.groups and c.shell != BASH))
            here = None if unsure else to
    for c in cmds:
        if c.subs:
            track(c.subs, c.cwd)
    return cmds


def flatten(cmds):
    """Every command, substitutions included, each substitution before its holder."""
    out = []
    for c in cmds:
        out.extend(flatten(c.subs))
        out.append(c)
    return out


def parse(text, shell, cwd, depth=0, parsed=None):
    """Parsed for `text` read as `shell` (BASH, PS or CMD), starting in `cwd`. With `parsed`, the
    commands are added to it (a nested shell's text)."""
    parsed = parsed if parsed is not None else Parsed(text, shell, cwd)
    if depth > MAX_DEPTH:
        parsed.flag('shells nested deeper than %d' % MAX_DEPTH)
        return parsed, []
    sc = _Scan(text, shell, depth, parsed)
    top = sc.commands()
    track(top, cwd)
    cmds = flatten(top)
    parsed.commands.extend(cmds)
    return parsed, cmds
