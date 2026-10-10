"""What a parsed command runs (docs/guard.md "Parsing").

invocation(cmd) finds the program a command runs past the words that only start it: variable
assignments (FOO=1 prog), env, command, exec, nohup, nice, time, timeout, xargs, PowerShell's call
operator (& prog) and Start-Process (-FilePath, -ArgumentList), cmd's start. expand(parsed) parses
the text a nested shell runs as commands of their own (bash/sh -c, powershell/pwsh -Command,
cmd /c, Invoke-Expression, eval); a nested text the guard cannot read (a variable, an encoded
command) marks the parse unclean. A program's arguments are never commands of their own, so
`machine_lock.py run -- godot ...` runs python, not godot.
"""
import ntpath
import re

from . import shell as sh

PYTHON_RE = re.compile(r'^(python|pythonw|py)(\d+(\.\d+)*)?$')
BASH_SHELLS = {'bash', 'sh', 'zsh', 'dash', 'ksh'}
PS_SHELLS = {'powershell', 'pwsh'}
EXE_SUFFIXES = ('.exe', '.cmd', '.bat', '.com')
PY_VALUE_OPTS = {'-X', '-W'}
ASSIGN_RE = re.compile(r'^[A-Za-z_]\w*\+?=')         # FOO=x, and bash's FOO+=x
# bash words that start a command without being its program, and words that start a construct
# whose first command is not a program run (for f in ...; select; the closing words)
BASH_SKIP = sh.BASH_PREFIX_WORDS
BASH_NO_PROGRAM = {'for', 'select', 'case', 'fi', 'done', 'esac', 'function', 'in', '}', '{', '[[', ']]'}
PS_KEYWORDS = {'if', 'elseif', 'else', 'foreach', 'for', 'while', 'do', 'until', 'switch', 'function',
               'filter', 'param', 'begin', 'process', 'end', 'try', 'catch', 'finally', 'trap',
               'return', 'throw', 'break', 'continue', 'exit', 'data', 'class', 'enum', 'using',
               'workflow', 'parallel', 'sequence', 'inlinescript', 'dynamicparam'}
PS_ASSIGN_OPS = {'=', '+=', '-=', '*=', '/=', '%=', '??='}
# the environment that moves git's directory, work tree or index, or sets its config (core.worktree)
GIT_ENV_NAMES = (r'GIT_(?:DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|CONFIG_COUNT|CONFIG_KEY_\d+|CONFIG_VALUE_\d+'
                 r'|CONFIG_PARAMETERS)')
GIT_ENV_PS_RE = re.compile(r'(?i)^\$env:' + GIT_ENV_NAMES + r'$')
GIT_ENV_NET_RE = re.compile(r'(?i)SetEnvironmentVariable\s*\(\s*[\'"](' + GIT_ENV_NAMES + r')[\'"]')
EXPORTERS = {'export', 'declare', 'typeset', 'local', 'readonly', 'set', 'setx'}
SOURCED_STDIN = ('/dev/stdin', '/dev/fd/0', '/proc/self/fd/0')     # source / . of these reads standard input
# wrappers whose options take a value: the option words to step over before the program
WRAPPER_VALUES = {'time': ('-f', '-o', '--format', '--output'), 'stdbuf': ('-i', '-o', '-e'),
                  'sudo': ('-u', '-g', '-C', '-D', '-p', '-U', '-h', '-r', '-t', '-T')}
# Start-Process parameters: the switches, and the ones that take a value
SP_SWITCHES = ('-wait', '-nonewwindow', '-passthru', '-usenewenvironment', '-loaduserprofile',
               '-whatif', '-confirm')
SP_VALUES = ('-filepath', '-argumentlist', '-workingdirectory', '-verb', '-windowstyle', '-credential',
             '-redirectstandardinput', '-redirectstandardoutput', '-redirectstandarderror', '-environment')
SP_ALIASES = {'-args': '-argumentlist', '-path': '-filepath', '-lup': '-loaduserprofile',
              '-nnw': '-nonewwindow', '-rso': '-redirectstandardoutput', '-rse': '-redirectstandarderror',
              '-rsi': '-redirectstandardinput', '-wd': '-workingdirectory'}


class Invocation(object):
    """`name` the program's lower-case base name without .exe; `path` as written (the text bash
    passes when a backslash in it is dropped); `args` its Words (Start-Process: the -ArgumentList
    words); `cmd` the Command; `dyn` the program word is not literal; `index` the program word's
    place in cmd.words; `via` 'start-process' when Start-Process runs it, 'xargs' when xargs
    feeds it its arguments; `writes` the files Start-Process redirects into; `word` the program
    Word."""
    __slots__ = ('name', 'path', 'args', 'cmd', 'dyn', 'index', 'via', 'writes', 'word')

    def __init__(self, name, path, args, cmd, dyn, index, via=None, writes=(), word=None):
        self.name, self.path, self.args, self.cmd, self.dyn, self.index = (
            name, path, args, cmd, dyn, index)
        self.via, self.writes, self.word = via, list(writes), word

    @property
    def argv(self):
        return [w.text for w in self.args]

    def __repr__(self):
        return 'Invocation(%r, %r)' % (self.name, self.argv)


def base_name(text):
    """`C:\\x\\Godot_v4.exe` -> `godot_v4`: a program's lower-case base name without .exe."""
    b = ntpath.basename(text.replace('/', '\\')).lower()
    for suf in EXE_SUFFIXES:
        if b.endswith(suf):
            return b[:-len(suf)]
    return b


# xargs (GNU and BSD): short options that take a value, the ones whose value is optional and
# attached, and the long options that take one
XARGS_SHORT_VALUES = 'adEILnPsJR'
XARGS_SHORT_OPTIONAL = 'eil'
XARGS_LONG_VALUES = ('arg-file', 'delimiter', 'max-args', 'max-chars', 'max-procs', 'process-slot-var')


def xargs_opts(cmd, words, i):
    """For xargs at words[i]: (the replace string of -I, -J, -i or --replace (and its unique
    prefixes), else None; the index of the first word after its options). With a replace
    string xargs puts its input into the words that hold it, wherever they are."""
    k, rep = i + 1, None
    while k < len(words):
        t = passed(cmd, words[k])
        if t == '--':
            return rep, k + 1
        if not t.startswith('-') or t == '-':
            break
        if t.startswith('--'):
            nm, eq, val = t[2:].partition('=')
            if nm and 'replace'.startswith(nm):
                rep = val if eq else '{}'
            elif nm and not eq and any(o.startswith(nm) for o in XARGS_LONG_VALUES):
                k += 1
            k += 1
            continue
        j = 1
        while j < len(t):
            ch, rest = t[j], t[j + 1:]
            if ch in XARGS_SHORT_VALUES:
                val = rest
                if not rest:
                    k += 1
                    val = passed(cmd, words[k]) if k < len(words) else ''
                if ch in 'IJ':
                    rep = val
                break
            if ch in XARGS_SHORT_OPTIONAL:
                if ch == 'i':
                    rep = rest or '{}'
                break
            j += 1
        k += 1
    return rep, k


def fill_words(words, token):
    """Mark each word holding `token` (xargs' replace string, find's {}) as computed, in place:
    the program fills the token in at run time, so the word says nothing the guard can read."""
    for w in words:
        if token in w.text:
            w.dyn = True


def _skip_opts(words, i, with_value=()):
    while i < len(words) and words[i].text.startswith('-') and words[i].text != '--':
        i += 2 if words[i].text in with_value else 1
    if i < len(words) and words[i].text == '--':
        i += 1
    return i


def _param(words, names):
    """Index of the value of a PowerShell parameter (any unambiguous prefix of `names`, or
    -Name:value), or None."""
    for k, w in enumerate(words):
        t = w.text.lower()
        if not t.startswith('-') or len(t) < 2:
            continue
        name, colon, _v = t.partition(':')
        if any(n.startswith(name) and len(name) >= 3 or n == name for n in names):
            return ('colon', k) if colon else ('next', k + 1)
    return None


def quoted(cmd, w):
    """True when the word's source starts with a quote: a string, never a parameter or a program."""
    return 0 <= w.start < len(cmd.source) and cmd.source[w.start] in '\'"'


def canon_param(text, names, aliases=None):
    """A PowerShell parameter word (-Name, -Name:value) as the canonical name it selects among
    `names` (exact, an alias, or the one name it is a prefix of), or None."""
    t = text.lower().split(':', 1)[0]
    if not t.startswith('-') or len(t) < 2:
        return None
    if t in names:
        return t
    if aliases and t in aliases:
        return aliases[t]
    hits = [n for n in names if n.startswith(t)]
    return hits[0] if len(hits) == 1 else None


def _start_process(cmd, words, i):
    """Start-Process [-FilePath] prog [[-ArgumentList] args] with its switches anywhere."""
    rest = words[i + 1:]
    names = SP_SWITCHES + SP_VALUES
    prog, args, writes, positional = None, [], [], []
    k = 0
    while k < len(rest):
        w = rest[k]
        p = None if quoted(cmd, w) or w.dyn else canon_param(w.text, names, SP_ALIASES)
        if p is None:
            positional.append(w)
            k += 1
            continue
        if p in SP_SWITCHES:
            k += 1
            continue
        if ':' in w.text:
            vals, k = [sh.Word(w.text.split(':', 1)[1], w.dyn, w.start, w.end)], k + 1
        elif p == '-argumentlist':
            j = k + 1
            while j < len(rest) and (quoted(cmd, rest[j]) or rest[j].dyn or
                                     canon_param(rest[j].text, names, SP_ALIASES) is None):
                j += 1
            vals, k = rest[k + 1:j], j
        else:
            vals, k = rest[k + 1:k + 2], k + 2
        if p == '-filepath' and vals:
            prog = vals[0]
        elif p == '-argumentlist':
            for v in vals:
                args.extend(_split_list(v))
        elif p in ('-redirectstandardoutput', '-redirectstandarderror'):
            writes.extend(vals)
    if prog is None and positional:
        prog = positional.pop(0)
    if not args and positional:
        args = _split_list(positional[0])
    if prog is None:
        return None
    return Invocation(base_name(prog.text), prog.text, args, cmd, prog.dyn, i, via='start-process',
                      writes=writes, word=prog)


def _split_list(w):
    """An -ArgumentList string's words (split on spaces outside double quotes)."""
    if w.dyn:
        return [w]
    out = []
    for m in re.finditer(r'"([^"]*)"|(\S+)', w.text):
        out.append(sh.Word(m.group(1) if m.group(1) is not None else m.group(2), False, w.start, w.end))
    return out


def program_alt(w):
    """The program text bash runs for a word with a backslash bash drops (g\\odot runs godot), or
    None. A Windows path (<drive>:\\x, .\\x, \\\\server) keeps its backslashes, as the user meant."""
    if w.alt is None or re.match(r'^([A-Za-z]:|\.|\\)', w.text):
        return None
    return w.alt


def said(cmd, w):
    """The word's text as the shell passes it on: bash drops a backslash (_git_word), so
    `export GIT_D\\IR=x` and `env GIT\\_DIR=x` hand over GIT_DIR=x."""
    return _git_word(w).text if cmd.shell == sh.BASH else w.text


def passed(cmd, w):
    """The word's text as bash hands it to a builtin or to env: every backslash bash drops is
    dropped, a leading one too (`export \\GIT_DIR=x` sets GIT_DIR). said keeps a leading one,
    which starts a Windows path in a program word."""
    if cmd.shell == sh.BASH and w.alt is not None:
        return w.alt
    return w.text


def invocation(cmd):
    """The Invocation `cmd` runs, or None (an assignment only, an empty command, a keyword that
    runs no program, a PowerShell expression)."""
    words = cmd.words
    i = 0
    called = False
    via = None
    while i < len(words):
        w = words[i]
        t = w.text
        name = base_name(t)
        if cmd.shell == sh.BASH and not quoted(cmd, w):
            if t == 'time':
                i = _skip_opts(words, i + 1, WRAPPER_VALUES['time'])
                continue
            if t in BASH_SKIP:
                i += 1
                continue
            if t in BASH_NO_PROGRAM:
                return None
        if cmd.shell == sh.BASH and ASSIGN_RE.match(said(cmd, w)):
            i += 1
            continue
        if cmd.shell == sh.PS and not called:
            if i + 1 < len(words) and words[i + 1].text in PS_ASSIGN_OPS:
                i += 2                          # $x = <command>: the command after the =
                continue
            if quoted(cmd, w) or (not w.dyn and t.lower() in PS_KEYWORDS):
                return None                     # a string or a keyword: no program
            if w.dyn and t not in ('&', '.'):
                return None                     # $x.Count: an expression
        if cmd.shell == sh.PS and t in ('&', '.'):
            i += 1
            called = True
            continue
        if name == 'env':
            if env_nested(words, i, cmd) is not None:
                # env -S text / env -C dir: the program is read as a nested command (expand)
                return Invocation(name, t, words[i + 1:], cmd, w.dyn, i, word=w)
            i += 1
            # env reads every word holding an = as an assignment, whatever the name
            while i < len(words) and (words[i].text.startswith('-') or '=' in passed(cmd, words[i])):
                i += 2 if words[i].text in ENV_VALUE_OPTS else 1
            continue
        if name in ('command', 'exec', 'nohup', 'builtin', 'time', 'sudo', 'stdbuf', 'caffeinate'):
            i = _skip_opts(words, i + 1, WRAPPER_VALUES.get(name, ()))
            continue
        if name == 'nice':
            i = _skip_opts(words, i + 1, ('-n',))
            continue
        if name == 'timeout' and cmd.shell != sh.CMD:
            i = _skip_opts(words, i + 1, ('-s', '-k', '--signal', '--kill-after'))
            i += 1                      # the duration
            continue
        if name == 'xargs':
            rep, i = xargs_opts(cmd, words, i)
            via = 'xargs'
            if rep is not None:                 # -I {}: the input may land in any word, git's place too
                # every later word holding it is filled in at run time: the program word, a
                # nested shell's or env -S's text, git's own words (marked in place, so the
                # program check and _nested_text read them as computed)
                fill_words(words[i:], rep)
                g = next((k for k in range(i, len(words)) if not quoted(cmd, words[k]) and
                          base_name(passed(cmd, words[k])) == 'git'), None)
                if g is not None:
                    return Invocation('git', words[g].text, words[g + 1:], cmd, words[g].dyn, g, via=via,
                                      word=words[g])
            continue
        if cmd.shell == sh.PS and name in ('start-process', 'saps', 'start'):
            return _start_process(cmd, words, i)
        if cmd.shell == sh.CMD and name == 'start':
            i += 1
            if i < len(words) and quoted(cmd, words[i]):
                i += 1                          # start "title" prog: a quoted first word is the title
            while i < len(words) and words[i].text.startswith('/'):
                i += 2 if words[i].text.lower() == '/d' else 1
            continue
        alt = program_alt(w) if cmd.shell == sh.BASH else None
        if alt is not None:
            return Invocation(base_name(alt), alt, words[i + 1:], cmd, w.dyn, i, via=via, word=w)
        return Invocation(name, t, words[i + 1:], cmd, w.dyn, i, via=via, word=w)
    return None


ENV_VALUE_OPTS = ('-u', '--unset')


def env_nested(words, i, cmd=None):
    """For `env` at words[i]: ('split', value Word, rest index) for env -S text, ('chdir', dir
    Word, rest index) for env -C dir, or None. The rest index is where the words after the
    option's value start."""
    k = i + 1
    while k < len(words):
        t = words[k].text
        if t in ('-S', '--split-string', '-C', '--chdir'):
            if k + 1 >= len(words):
                return None
            return ('split' if t in ('-S', '--split-string') else 'chdir'), words[k + 1], k + 2
        for pre, kind in (('--split-string=', 'split'), ('--chdir=', 'chdir'), ('-S', 'split'), ('-C', 'chdir')):
            if t.startswith(pre) and len(t) > len(pre):
                v = words[k]
                return kind, sh.Word(t[len(pre):], v.dyn, v.start, v.end), k + 1
        if t in ENV_VALUE_OPTS:
            k += 2
            continue
        if t.startswith('-') and t != '--' or '=' in (passed(cmd, words[k]) if cmd is not None else t):
            k += 1
            continue
        return None
    return None


# ---------------------------------------------------------------- python and the wrappers

def python_script(inv):
    """(script word index in inv.args, script base name) when inv runs a python script; else None."""
    if not PYTHON_RE.match(inv.name):
        return None
    a = inv.args
    k = 0
    while k < len(a):
        t = a[k].text
        if t in ('-c', '-m'):
            return None
        if t in PY_VALUE_OPTS:
            k += 2
            continue
        if t.startswith('-') and t != '-':
            k += 1
            continue
        return k, ntpath.basename(t.replace('/', '\\')).lower()
    return None


def wrapper(inv):
    """'gate' for gate.py, 'lock-run' for `machine_lock.py run`, else None. These are the two
    entry points that may run heavy programs (ARCHITECTURE.md "Guard rules")."""
    if inv is None:
        return None
    if inv.name in ('gate.py', 'gate'):
        return 'gate'
    if inv.name == 'machine_lock.py':
        return 'lock-run' if inv.args[:1] and inv.args[0].text == 'run' else None
    found = python_script(inv)
    if not found:
        return None
    k, script = found
    if script == 'gate.py':
        return 'gate'
    if script == 'machine_lock.py':
        nxt = inv.args[k + 1:k + 2]
        return 'lock-run' if nxt and nxt[0].text == 'run' else None
    return None


# ---------------------------------------------------------------- nested shells

STDIN = object()                # _nested_text: the shell reads its commands from standard input
# a word anywhere in a line that may change the shell's directory (a trap handler's place)
LINE_CD_RE = re.compile(r'(?i)(?<![\w.-])(?:%s)(?![\w.-])' % '|'.join(
    re.escape(v) for v in sorted(sh.CD_VERBS | sh.CD_UNKNOWN)))
BASH_VALUE_OPTS = {'-o', '+o', '-O', '+O', '--rcfile', '--init-file'}
PS_VALUE_PARAMS = ('executionpolicy', 'windowstyle', 'workingdirectory', 'configurationname',
                   'outputformat', 'inputformat', 'version', 'psconsolefile', 'settingsfile',
                   'custompipename')
PS_VALUE_ALIASES = {'ep', 'ex', 'w', 'wd', 'of', 'if', 'v', 'ea'}


def _ps_takes_value(t):
    body = t[1:]
    if body in PS_VALUE_ALIASES:
        return True
    return len(body) >= 2 and body not in ('in', 'i') and any(p.startswith(body) for p in PS_VALUE_PARAMS)


def span_text(cmd, words):
    """The words as the command's source writes them (quotes kept), or joined when the source
    positions do not fit."""
    src = cmd.source
    if words and all(0 <= x.start <= x.end <= len(src) for x in words) and words[0].start <= words[-1].end:
        return src[words[0].start:words[-1].end]
    return ' '.join(x.text for x in words)


def _nested_text(inv, parsed):
    """(text, shell) a nested shell or eval runs, (text, shell, cwd) when it runs elsewhere
    (env -C), (STDIN, shell) when the shell reads its commands from standard input, or None."""
    name, a = inv.name, inv.args
    if name == 'env':
        found = env_nested(inv.cmd.words, inv.index, inv.cmd)
        if found is None:
            return None
        kind, val, k = found
        rest = inv.cmd.words[k:]
        if kind == 'split':
            if val.dyn:
                parsed.flag('an env -S string that is not literal')
                return None
            return (val.text + ' ' + span_text(inv.cmd, rest)).strip(), inv.cmd.shell
        if not rest:
            return None
        where = None if val.dyn else sh.resolve_path(val.text, inv.cmd.cwd)
        return span_text(inv.cmd, rest), inv.cmd.shell, where
    if name in ('source', '.') and inv.cmd.shell == sh.BASH:
        if a and a[0].dyn:
            parsed.flag('a script sourced from text that is not literal')
        if a and a[0].text in SOURCED_STDIN:
            return STDIN, sh.BASH                # . /dev/stdin <<< "...": the text runs in this shell
        return None
    if name in BASH_SHELLS:
        k = 0
        while k < len(a):
            t = a[k].text
            if t in BASH_VALUE_OPTS:
                k += 2
                continue
            if t.startswith('-') and not t.startswith('--') and 'c' in t[1:]:
                if k + 1 >= len(a):
                    return None
                if a[k + 1].dyn:
                    parsed.flag('a nested bash -c whose text is not literal')
                    return None
                return a[k + 1].text, sh.BASH
            if t in ('-', '--') or (t[:1] in '-+' and len(t) > 1):
                k += 1
                continue
            if a[k].dyn:
                parsed.flag('a shell running a script the text does not name')
            return None                         # bash script.sh: a file, not text
        return STDIN, sh.BASH
    if name in PS_SHELLS:
        k = 0
        while k < len(a):
            t = a[k].text.lower()
            if t.startswith('-e') and 'encodedcommand'.startswith(t[1:]) or t in ('-ec', '-enc'):
                parsed.flag('an encoded PowerShell command')
                return None
            if t.startswith('-') and len(t) > 1 and 'command'.startswith(t[1:]):
                rest = a[k + 1:]
                if [w.text for w in rest] == ['-']:
                    return STDIN, sh.PS
                if any(w.dyn for w in rest):
                    parsed.flag('a nested powershell -Command whose text is not literal')
                    return None
                return ' '.join(w.text for w in rest), sh.PS
            if t.startswith('-') and len(t) > 1 and 'file'.startswith(t[1:]):
                return None
            if t.startswith('-') and _ps_takes_value(t):
                k += 2
                continue
            if t.startswith('-'):
                k += 1
                continue
            rest = a[k:]
            if any(w.dyn for w in rest):
                parsed.flag('a nested powershell command whose text is not literal')
                return None
            return ' '.join(w.text for w in rest), sh.PS
        return STDIN, sh.PS
    if name == 'cmd':
        for k, w in enumerate(a):
            if w.text.lower() in ('/c', '/k', '/r', '//c', '//k', '//r'):
                rest = a[k + 1:]
                if any(x.dyn for x in rest):
                    parsed.flag('a nested cmd /c whose text is not literal')
                    return None
                if len(rest) == 1:
                    return rest[0].text, sh.CMD
                src = inv.cmd.source
                if rest and all(0 <= x.start <= x.end <= len(src) for x in rest):
                    # as written, so a quoted start "title" keeps its quotes
                    return src[rest[0].start:rest[-1].end], sh.CMD
                return ' '.join(x.text for x in rest), sh.CMD
        return None
    if name in ('invoke-expression', 'iex'):
        p = _param(a, ('-command',))
        words = a
        if p:
            how, k = p
            words = [sh.Word(a[k].text.split(':', 1)[1], a[k].dyn, 0, 0)] if how == 'colon' else a[k:k + 1]
        words = [w for w in words if not w.text.startswith('-')][:1]
        if not words:
            if inv.cmd.piped_in:
                return STDIN, sh.PS
            parsed.flag('Invoke-Expression of a value the text computes')
            return None
        if words[0].dyn:
            parsed.flag('Invoke-Expression on text that is not literal')
            return None
        return words[0].text, sh.PS
    if name == 'eval' and inv.cmd.shell == sh.BASH:
        if any(w.dyn for w in a):
            parsed.flag('eval of text that is not literal')
            return None
        return ' '.join(w.text for w in a), sh.BASH
    if name == 'trap' and inv.cmd.shell == sh.BASH:
        # the handler runs in this shell later (at exit, or on a signal), wherever it then is:
        # where the trap was set only when nothing on the line, nested text too, changes directory
        found = trap_operands(inv.cmd, a)
        if found is None:
            return None
        if found[0].dyn:
            parsed.flag('a trap handler that is not literal')
            return None
        where = None if LINE_CD_RE.search(parsed.text or '') else inv.cmd.cwd
        return (found[0].text, sh.BASH, where) if found[0].text.strip() else None
    if name in ('mapfile', 'readarray') and inv.cmd.shell == sh.BASH:
        cb = mapfile_callback(inv.cmd, a)       # runs here, now, once per line read
        if cb is None:
            return None
        if cb.dyn:
            parsed.flag('a mapfile callback that is not literal')
            return None
        return cb.text, sh.BASH
    return None


STDIN_WRITERS = {'echo', 'printf', 'write-output', 'write-host', 'echo.exe'}


def _stdin_text(cmd, parsed):
    """The text a shell reads on standard input when the line says what it is (a heredoc body, a
    here-string, a pipe from echo, printf, Write-Output or a PowerShell string), else None (the
    parse is marked unclean when a pipe feeds it something else)."""
    for r in cmd.redirects:
        if r.op in ('<<', '<<<') and r.target is not None:
            if r.target.dyn and ('$' in r.target.text or '`' in r.target.text):
                parsed.flag('a shell reading text that is not literal')
            return r.target.text
    src = cmd.pipe_from
    if src is not None:
        inv = invocation(src)
        if inv is not None and inv.name in STDIN_WRITERS:
            args = [w for w in inv.args if not (inv.name == 'echo' and w.text in ('-e', '-n', '-E'))]
            if any(w.dyn for w in args):
                parsed.flag('a shell reading text that is not literal')
            return ' '.join(w.text for w in args)
        if inv is None and src.shell == sh.PS and src.words and quoted(src, src.words[0]):
            return ' '.join(w.text for w in src.words)
        parsed.flag('a shell reading its commands from a pipe')
        return None
    if cmd.piped_in:
        parsed.flag('a shell reading its commands from a pipe')
    return None


# $env:FOO = v, $env:FOO='v' (one word to the parser), ${env:FOO} += v: read from the source
PS_ENV_SET_RE = re.compile(r'(?i)\$(?:\{env:([^}]+)\}|env:(\w+))\s*(?:[-+*/%]|\?\?)?=(?!=)\s*(.*)')
PS_ENV_ITEM_RE = re.compile(r'(?i)^(?:-\w+:)?env:[\\/]?(.+)$')
PS_VALUE_OPT_RE = re.compile(r'(?i)^-va(?:l(?:ue?)?)?(?::(.*))?$')      # -Va, -Val, -Value, -Value:x
NET_SET_ENV_RE = re.compile(r'(?i)SetEnvironmentVariable\s*\(\s*[\'"]([^\'"]+)[\'"]\s*,\s*(?:\'([^\']*)|"([^"]*))')
BASH_ASSIGN_RE = ASSIGN_RE                                               # FOO=x and FOO+=x
# the cmdlets that set an env: item (Set-Content env:FOO x, New-Item -Path env: -Name FOO), the env:
# drive named bare, and New-Item's -Name (-N, -Na, -Nam, -Name, -Name:x)
PS_ENV_SETTERS = ('set-item', 'si', 'new-item', 'ni', 'set-content', 'sc', 'add-content', 'ac')
PS_ENV_DRIVE_RE = re.compile(r'(?i)^(?:-\w+:)?env:[\\/]?$')
PS_NAME_OPT_RE = re.compile(r'(?i)^-n(?:a(?:me?)?)?(?::(.*))?$')
# any cmdlet that may make an env: item under a new name (Rename-Item env:X -NewName FOO, Copy-Item)
PS_ENV_ITEM_CMDLETS = PS_ENV_SETTERS + ('rename-item', 'ren', 'rni', 'copy-item', 'copy', 'cp', 'cpi', 'move-item',
                                        'mi', 'move', 'mv', 'set-variable', 'sv')
PS_ENV_REF_RE = re.compile(r'(?i)^(?:-\w+:)?env:[\\/]?')
PS_ENV_DRIVE_PREFIX_RE = re.compile(r'(?i)^env:[\\/]?')
PS_OPT_PREFIX_RE = re.compile(r'^-\w+:')
GIT_ENV_NAME_RE = re.compile(r'(?i)^' + GIT_ENV_NAMES + r'$')


def _unquote(text):
    text = text.strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in '\'"':
        return text[1:-1]
    return text


def _assigned(c, words, name_re):
    out = []
    for w in words:
        name, eq, val = said(c, w).partition('=')
        name = name[:-1] if name.endswith('+') else name
        if eq and name_re.match(name):
            out.append((name, val))
    return out


def env_assignments(c, names):
    """[(variable, value text)] of the environment variables matching the regex `names` (case
    ignored) that the command sets: an assignment before the program (FOO=1 prog, env FOO=1 prog),
    export/declare/setx, $env:FOO = ..., ${env:FOO} += ..., Set-Item, Set-Content or Add-Content
    env:FOO, New-Item env:FOO or New-Item -Path env: -Name FOO, or
    [Environment]::SetEnvironmentVariable('FOO', ...). A word that only mentions one (echo
    "FOO=1", curl -d foo=1, a commit message) does not."""
    name_re = re.compile(r'(?i)^(?:%s)$' % names)
    words = c.words
    out = []
    if not words:
        return out
    src = c.source or ''
    if c.shell == sh.PS:
        end = c.end if c.end is not None else len(src)
        m = PS_ENV_SET_RE.match(src, words[0].start, end) if words[0].start is not None else None
        if m and name_re.match(m.group(1) or m.group(2)):
            out.append((m.group(1) or m.group(2), _unquote(m.group(3))))
        for w in words:
            # any word: $null = [Environment]::SetEnvironmentVariable(...). The parser reads the
            # ( ) argument list as a command of its own, so read the source from the word on
            if w.text.lower().endswith('::setenvironmentvariable') and w.start is not None:
                g = NET_SET_ENV_RE.search(src, w.start)
                if g and g.start() < (w.end or len(src)) and name_re.match(g.group(1)):
                    out.append((g.group(1), g.group(2) if g.group(2) is not None else g.group(3) or ''))
    inv = invocation(c)
    if c.shell == sh.BASH:
        lead = []
        for w in words:                          # FOO+=x is an assignment the program finder does not skip
            if not BASH_ASSIGN_RE.match(said(c, w)):
                break
            lead.append(w)
        before = words[:inv.index] if inv is not None else words
        out += _assigned(c, lead + [w for w in before if w not in lead], name_re)
    if inv is None:
        return out
    args = inv.args
    if inv.name in EXPORTERS:
        out += _assigned(c, args, name_re)
        if inv.name == 'setx' and len(args) > 1 and name_re.match(args[0].text):
            out.append((args[0].text, args[1].text))
    elif inv.name in PS_ENV_SETTERS:
        vals = _option_values(args, PS_VALUE_OPT_RE)
        for k, w in enumerate(args):
            m = PS_ENV_ITEM_RE.match(w.text)
            if not (m and name_re.match(m.group(1))):
                continue
            rest = [v.text for v in args[k + 1:] if not v.text.startswith('-')]
            out.append((m.group(1), (vals or rest or [''])[0]))
        if inv.name in ('new-item', 'ni') and any(PS_ENV_DRIVE_RE.match(w.text) for w in args):
            for name in _option_values(args, PS_NAME_OPT_RE):     # New-Item -Path env: -Name FOO
                if name_re.match(name):
                    out.append((name, (vals or [''])[0]))
    return out


def _option_values(args, opt_re):
    """The values a PowerShell option matching `opt_re` is given (group 1: the -Opt:value form)."""
    out = []
    for j, v in enumerate(args):
        vm = opt_re.match(v.text)
        if vm and vm.group(1) is not None:
            out.append(vm.group(1))
        elif vm and j + 1 < len(args):
            out.append(args[j + 1].text)
    return out


def git_env_set(c):
    """The variable of GIT_ENV_NAMES (GIT_DIR, GIT_WORK_TREE, GIT_COMMON_DIR, GIT_INDEX_FILE,
    GIT_CONFIG_COUNT, GIT_CONFIG_KEY_<n>, GIT_CONFIG_VALUE_<n>, GIT_CONFIG_PARAMETERS) the command
    sets, or None: every spelling env_assignments reads, export (or the like) naming one with no
    value (export GIT_DIR after read GIT_DIR), and a word naming one in a cmdlet that may make an
    env: item (PS_ENV_ITEM_CMDLETS: Rename-Item env:X -NewName GIT_DIR, Copy-Item env:X
    env:GIT_DIR). A word that only names one (echo "GIT_DIR=x", Write-Output $env:GIT_DIR,
    Get-Content env:GIT_DIR) does not. Every word is read as the shell passes it (said: a
    backslash bash drops, dropped). Anything else that may change the environment is
    git_env_unsafe's."""
    for name, _val in env_assignments(c, GIT_ENV_NAMES):
        return name
    words = c.words
    if not words:
        return None
    if c.shell == sh.PS:
        if len(words) > 1 and GIT_ENV_PS_RE.match(words[0].text) and words[1].text in PS_ASSIGN_OPS:
            return words[0].text.split(':', 1)[1]
        m = GIT_ENV_NET_RE.search(c.source[c.start:c.end]) if 0 <= c.start <= c.end <= len(c.source) else None
        if m:
            return m.group(1)
    inv = invocation(c)
    if inv is None:
        return None
    if inv.name in EXPORTERS:
        for w in inv.args:
            name = said(c, w).split('=', 1)[0]
            name = name[:-1] if name.endswith('+') else name
            if GIT_ENV_NAME_RE.match(name):
                return name
    if inv.name in PS_ENV_ITEM_CMDLETS and any(PS_ENV_REF_RE.match(said(c, w)) for w in inv.args):
        for w in inv.args:
            name = PS_ENV_DRIVE_PREFIX_RE.sub('', PS_OPT_PREFIX_RE.sub('', said(c, w)))
            if GIT_ENV_NAME_RE.match(name):
                return name
    return None


# ---------------------------------------------------------------- fail closed: the rest of the environment

# GIT_* names that only set who and when (identity, dates) or turn a prompt off: a line may set them
# and still place its git. Every other GIT_* name may move git (GIT_EXEC_PATH, GIT_CONFIG_GLOBAL,
# GIT_OBJECT_DIRECTORY, GIT_EDITOR, ...)
GIT_SAFE_NAMES = frozenset(('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_AUTHOR_DATE', 'GIT_COMMITTER_NAME',
                            'GIT_COMMITTER_EMAIL', 'GIT_COMMITTER_DATE', 'GIT_TERMINAL_PROMPT',
                            'GIT_MERGE_AUTOEDIT', 'GIT_OPTIONAL_LOCKS'))
# names that change what a shell runs or which options it starts with
SHELL_ENV_NAMES = frozenset(('SHELLOPTS', 'BASHOPTS', 'BASH_ENV', 'ENV'))
LITERAL_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
BASH_EXPORTERS = ('export', 'declare', 'typeset', 'local', 'readonly')
NAMEREF_DECLARERS = ('declare', 'typeset', 'local')
# PowerShell's item cmdlets and their aliases; the setters' second positional word is the value
PS_ITEM_CMDLETS = frozenset(('set-item', 'si', 'new-item', 'ni', 'remove-item', 'ri', 'rm', 'del', 'erase', 'rd',
                             'rmdir', 'rename-item', 'ren', 'rni', 'copy-item', 'copy', 'cp', 'cpi', 'move-item',
                             'mi', 'move', 'mv', 'set-content', 'sc', 'add-content', 'ac', 'clear-item', 'cli',
                             'clear-content', 'clc'))
PS_VALUE_SETTERS = ('set-item', 'si', 'set-content', 'sc', 'add-content', 'ac')
# Set-Content and Add-Content bind piped input to -Value: their item is the path the text names
# (with none, `not items` in _ps_item_unsafe still fails closed)
PS_PIPED_VALUE = ('set-content', 'sc', 'add-content', 'ac')
PS_ITEM_PARAMS = ('-path', '-literalpath', '-pspath', '-destination', '-name', '-newname')
PS_ITEM_VALUE_PARAMS = ('-value', '-itemtype', '-type', '-encoding', '-filter', '-include', '-exclude',
                        '-credential', '-stream', '-delimiter')
PS_ITEM_SWITCHES = ('-force', '-recurse', '-passthru', '-whatif', '-confirm', '-nonewline', '-asbytestream',
                    '-container', '-raw')
PS_ITEM_ALIASES = {'-lp': '-literalpath'}
PS_CD_CMDLETS = ('set-location', 'sl', 'cd', 'chdir', 'push-location', 'pushd')
PS_SCRIPT_RE = re.compile(r'(?i)\.psm?1$')
# the env: drive, bare or provider-qualified (Environment::FOO)
ENV_DRIVE_RE = re.compile(r'(?i)^(?:env:|(?:microsoft\.powershell\.core\\)?environment::)[\\/]?')
# a path that starts with an environment variable's value: $env:TEMP, "$env:TEMP\x", ${env:TEMP}/x
PS_ENV_READ_RE = re.compile(r'(?i)^\$(?:env:([A-Za-z_]\w*)|\{env:([A-Za-z_]\w*)\})(?=$|[\\/])')
PS_ENV_REF_ANY_RE = re.compile(r'(?i)\$(?:\{env:([^}]*)\}|env:([\w`]*))')
PS_ENV_ONE_WORD_RE = re.compile(r'(?i)^\$(?:\{env:[^}]*\}|env:[\w`]*)\s*(?:[-+*/%]|\?\?)?=(?!=)')
PS_ASSIGN_PREFIX_RE = re.compile(r'(?:[-+*/%]|\?\?)?=(?!=)')    # matched at a word's start: $env:X ="v"
# a splatted argument (@h): a hashtable's keys become parameters the text does not show
PS_SPLAT_RE = re.compile(r'^@[A-Za-z_?]')
# text PowerShell compiles and runs in this process: InvokeScript, NewScriptBlock, ExpandString,
# GetScriptBlock, AddScript, [scriptblock]::Create and [powershell]::Create under any namespace
# prefix, and a static call on a type the text computes (([type]'scriptblock')::Create; the parser
# splits the ( ) off, so the call's word starts with ::)
PS_SCRIPT_TEXT_RE = re.compile(r'(?i)\b(?:invokescript|newscriptblock|expandstring|getscriptblock|addscript)\b|'
                               r'\[\s*(?:system\.)?(?:management\.)?(?:automation\.)?'
                               r'(?:scriptblock|powershell)\s*\]\s*::\s*create\b|'
                               r'(?:^|\))\s*::\s*[A-Za-z_]')
# a PowerShell string the shell passes as it is written: '...' or "..." with no $ or ` in it
PS_PLAIN_STRING_RE = re.compile(r'''(?:'(?:[^']|'')*'|"(?:[^"$`]|"")*")''', re.S)
# item cmdlets whose env: item may be renamed, copied or made under a name a later path reads
PS_ENV_NAMERS = frozenset(('rename-item', 'ren', 'rni', 'copy-item', 'copy', 'cp', 'cpi', 'move-item', 'mi',
                           'move', 'mv', 'new-item', 'ni', 'set-item', 'si'))
NET_ENV_RE = re.compile(r'(?i)\[(?:system\.)?environment\]')
NET_GET_LITERAL_RE = re.compile(r'''(?i)\[(?:system\.)?environment\]::GetEnvironmentVariable\s*\(\s*'''
                                r'''(?:'[^']*'|"[^"$`]*")\s*[,)]''')
PS_VARIABLE_CMDLETS = ('set-variable', 'sv', 'new-variable', 'nv', 'set')
PS_COMMON_VAR_PARAMS = ('-outvariable', '-ov', '-errorvariable', '-ev', '-warningvariable', '-wv',
                        '-informationvariable', '-iv', '-pipelinevariable', '-pv')
PS_RESHAPERS = ('set-alias', 'sal', 'new-alias', 'nal', 'new-psdrive', 'ndr', 'mount', 'import-module', 'ipmo')


def env_name_unsafe(name):
    """True unless `name` is a literal environment variable name ([A-Za-z_][A-Za-z0-9_]*) that
    cannot move git or the shell: not a GIT_* name outside GIT_SAFE_NAMES, not SHELL_ENV_NAMES."""
    if not LITERAL_NAME_RE.match(name):
        return True
    up = name.upper()
    return up.startswith('GIT_') and up not in GIT_SAFE_NAMES or up in SHELL_ENV_NAMES


def _assign_name(text):
    name = text.split('=', 1)[0]
    return name[:-1] if name.endswith('+') else name


def _shown(c):
    """The command as written, for a refusal: '`export \\GIT_DIR=x`'."""
    w = c.words
    if w and 0 <= w[0].start <= w[-1].end <= len(c.source or ''):
        text = c.source[w[0].start:w[-1].end]
    else:
        text = ' '.join(c.argv)
    text = ' '.join(text.split())
    return '`%s`' % (text if len(text) <= 60 else text[:57] + '...')


def ps_env_targets(c):
    """The environment variable names (as written) a PowerShell expression assigns: $env:X = v,
    ${env:X} += v, $env:X='v' in one word, $env:X ="v" (the = starts the value's word), $env:A, $env:B = 1, 2, $a = $env:X = v, foreach
    ($env:X in ...)."""
    if c.shell != sh.PS:
        return []
    words = c.words
    src = c.source or ''
    ops = [k for k, w in enumerate(words) if w.text in PS_ASSIGN_OPS and not w.dyn and not quoted(c, w) or
           0 <= w.start < len(src) and PS_ASSIGN_PREFIX_RE.match(src, w.start)]
    targets = list(words[:ops[-1]]) if ops else []
    targets += [w for k, w in enumerate(words)
                if not quoted(c, w) and (PS_ENV_ONE_WORD_RE.match(w.text) or
                                         k + 1 < len(words) and words[k + 1].text.lower() == 'in')]
    out = []
    for w in targets:
        if quoted(c, w):
            continue
        for m in PS_ENV_REF_ANY_RE.finditer(w.text):
            out.append(m.group(1) if m.group(1) is not None else m.group(2))
    return out


def line_env_names(commands):
    """The upper-case names of the environment variables the line's commands set literally, and
    every name an item cmdlet on env: names (Rename-Item env:FOO TEMP, Copy-Item env:A env:TEMP,
    New-Item env:TEMP): such a name may then hold any text."""
    names = set()
    for c in commands:
        names.update(n.upper() for n, _v in env_assignments(c, r'[A-Za-z_]\w*'))
        names.update(n.upper() for n in ps_env_targets(c))
        names.update(_ps_env_item_names(c))
    return names


def _ps_env_item_names(c):
    inv = invocation(c) if c.shell == sh.PS else None
    if inv is None or inv.name not in PS_ENV_NAMERS:
        return set()
    vals = []
    for w in inv.args:
        t = said(c, w)
        if not quoted(c, w) and t.startswith('-'):
            if ':' not in t:
                continue                        # a parameter name; its value is the next word
            t = t.split(':', 1)[1]
        vals.append(t)
    if not any(ENV_DRIVE_RE.match(t) for t in vals):
        return set()
    out = set()
    for t in vals:
        m = ENV_DRIVE_RE.match(t)
        rest = t[m.end():] if m else t
        if LITERAL_NAME_RE.match(rest):
            out.add(rest.upper())
    return out


def git_env_unsafe(c, line_sets=frozenset()):
    """A short description of the command ('`...`') when it may change the environment in a way
    the guard cannot read, else None: fail closed, so a git on the same line cannot be placed
    (mark_git_env). Literal NAME=value forms with a name of LITERAL_NAME_RE that env_name_unsafe
    passes are read; everything else counts: a name the shell computes or unquotes (export
    \\GIT_DIR=, env "$n"=x, local -x $n=), a GIT_* name outside GIT_SAFE_NAMES, SHELLOPTS and the
    like, declare/typeset/local -n, bash's set -a / -o allexport in any place, shopt -o
    allexport, a nested bash/sh -a or -o allexport, alias, a sourced script; PowerShell item
    cmdlets (Set-Item, New-Item, Remove-Item, Rename-Item, Copy-Item, Move-Item, Set-Content,
    Add-Content, Clear-Item) whose item is computed (other than a path under $env:VAR, a read)
    or an env: name env_name_unsafe refuses, or whose item comes from a pipe or an expression;
    Set-Location to env: or a computed place; [Environment]:: other than a literal-name
    GetEnvironmentVariable; InvokeProvider; Set-Variable or -OutVariable on env:; aliases, PS
    drives and modules; a script run in the same process (. x.ps1, cmd's call or x.bat);
    Start-Process -Environment; cmd's set with a name that is not literal. `line_sets`: the
    names the line sets literally (line_env_names), so $env:TEMP in a path is a read only when
    the line does not set TEMP."""
    return _shown(c) if _env_unsafe(c, line_sets) else None


def _env_unsafe(c, line_sets):
    words = c.words
    if not words:
        return False
    inv = invocation(c)
    if c.shell == sh.BASH:
        if _bash_env_unsafe(c, inv):
            return True
    if c.shell == sh.PS and _ps_env_unsafe(c, inv, line_sets):
        return True
    if inv is None:
        return False
    if inv.via == 'xargs' and inv.name == 'git' and any(
            not quoted(c, w) and base_name(passed(c, w)) == 'xargs' and xargs_opts(c, words, k)[0] is not None
            for k, w in enumerate(words[:inv.index])):
        return True                             # xargs -I {} env {} git: its input may be GIT_DIR=
    if c.shell == sh.CMD:
        if inv.name == 'call' or inv.path.lower().endswith(('.bat', '.cmd')):
            return True                         # a batch file runs in this cmd and may set anything
        if inv.name == 'set':
            for w in inv.args:
                t = _unquote(passed(c, w))
                if t.startswith('/') or '=' not in t:
                    continue
                name = _assign_name(t).rstrip('*/%&|^<>-')
                if env_name_unsafe(name) or w.dyn:
                    return True
    if inv.name == 'setx':
        names = [w for w in inv.args if not w.text.startswith('/')][:1]
        if names and (names[0].dyn or env_name_unsafe(names[0].text)):
            return True
    return False


def _bash_env_unsafe(c, inv):
    words = c.words
    end = inv.index if inv is not None else len(words)
    for w in words[:end]:                       # FOO=1 prog, and assignments with no program
        t = said(c, w)
        if ASSIGN_RE.match(t) and env_name_unsafe(_assign_name(t)):
            return True
    for i, w in enumerate(words[:end + 1]):     # env NAME=v ... (env -S / -C too)
        if not quoted(c, w) and base_name(passed(c, w)) == 'env':
            k = i + 1
            while k < len(words):
                t = passed(c, words[k])
                if t in ENV_VALUE_OPTS:
                    k += 2
                    continue
                if t.startswith('-'):
                    k += 1
                    continue
                if '=' not in t:
                    break
                if words[k].dyn or env_name_unsafe(_assign_name(t)):
                    return True
                k += 1
    if inv is None:
        return False
    name, args = inv.name, inv.args
    if name in BASH_EXPORTERS:
        for w in args:
            t = passed(c, w)
            if t[:1] in '-+' and len(t) > 1 and not w.dyn and '=' not in t:
                if name in NAMEREF_DECLARERS and t[0] == '-' and 'n' in t[1:]:
                    return True                 # declare -n r=GIT_DIR: r names another variable
                continue
            if t == '--':
                continue
            # the name as the shell unquotes it; an expansion in it ($n, `...`, {a,b}) is not
            # LITERAL_NAME_RE. A computed value is not split here: bash reads it as an assignment
            if env_name_unsafe(_assign_name(t)):
                return True
    if name == 'set':
        want_o = False
        for w in args:
            t = passed(c, w)
            if want_o:
                if 'allexport' in t or w.dyn:
                    return True
                want_o = False
                continue
            if w.dyn:
                return True
            if t[:1] in '-+' and len(t) > 1 and t != '--':
                if t[0] == '-' and ('a' in t[1:] or 'allexport' in t):
                    return True                 # set -a, set +e -a, set -ea: every assignment exported
                want_o = t[0] == '-' and 'o' in t[1:]
                continue
            break
    if name == 'shopt' and any(w.dyn or 'allexport' in passed(c, w) for w in args):
        return True
    if name in BASH_SHELLS:
        k = 0
        while k < len(args):
            t = passed(c, args[k])
            if args[k].dyn:
                return True
            if t in ('-o', '+o'):
                if t == '-o' and (k + 1 >= len(args) or args[k + 1].dyn or 'allexport' in passed(c, args[k + 1])):
                    return True
                k += 2
                continue
            if t.startswith('-') and not t.startswith('--') and len(t) > 1:
                if 'a' in t[1:]:
                    return True                 # bash -a -c / bash -ac: the nested text exports all
                if 'c' in t[1:]:
                    break
                k += 1
                continue
            if t.startswith('+') or t.startswith('--'):
                k += 1
                continue
            break
    if name in ('source', '.') and not (args and passed(c, args[0]) in SOURCED_STDIN):
        return True                             # a script the guard does not read runs in this shell
    if name == 'alias':
        return True
    if name == 'trap' and _trap_unsafe(c, args):
        return True
    if name in ('mapfile', 'readarray') and any(w.dyn or MAPFILE_CALLBACK_RE.match(passed(c, w)) for w in args):
        return True                             # mapfile -C cmd: runs cmd in this shell as it reads
    return False


MAPFILE_CALLBACK_RE = re.compile(r'^-[A-Za-z]*C')
MAPFILE_VALUE_OPTS = 'dnOsuCc'                  # mapfile/readarray options that take a value
TRAP_AT_EXIT = ('EXIT', '0')                    # the only trap that cannot run before a later command


def trap_operands(c, args):
    """(handler Word, [signal Words]) a trap sets, or None when it lists (-p, -l) or resets
    (trap SIG, trap - SIG)."""
    ops = []
    for w in args:
        t = passed(c, w)
        if not ops and not w.dyn and t in ('-p', '-l', '-lp', '-pl', '--'):
            continue
        ops.append(w)
    if len(ops) < 2 or not ops[0].dyn and passed(c, ops[0]) == '-':
        return None
    return ops[0], ops[1:]


def _trap_unsafe(c, args):
    """A trap whose handler may run in this shell before a later command: any signal but EXIT
    (or 0), since the line may raise one itself (kill -USR1 $$, a child ending, DEBUG, ERR,
    RETURN). trap '' SIG ignores and trap - SIG resets; trap -p lists."""
    found = trap_operands(c, args)
    if found is None:
        return False
    hw, sigs = found
    if not hw.dyn and passed(c, hw) == '':
        return False
    for w in sigs:
        up = passed(c, w).upper()
        if w.dyn or (up[3:] if up.startswith('SIG') else up) not in TRAP_AT_EXIT:
            return True
    return False


def mapfile_callback(c, args):
    """The -C callback Word of mapfile/readarray, or None."""
    k = 0
    while k < len(args):
        w = args[k]
        t = passed(c, w)
        if w.dyn or not t.startswith('-') or t in ('-', '--'):
            k += 1
            continue
        for j in range(1, len(t)):
            if t[j] in MAPFILE_VALUE_OPTS:
                if t[j + 1:]:
                    val = sh.Word(t[j + 1:], w.dyn, w.start, w.end) if t[j] == 'C' else None
                else:
                    k += 1
                    val = args[k] if k < len(args) and t[j] == 'C' else None
                if val is not None:
                    return val
                break
        k += 1
    return None


def _ps_env_unsafe(c, inv, line_sets):
    words = c.words
    src = c.source or ''
    for n in ps_env_targets(c):
        if env_name_unsafe(n):
            return True
    plain = [_ps_plain_string(c, w) for w in words]
    if words and 0 <= words[0].start <= words[-1].end <= len(src):
        lo = words[0].start
        scan = list(src[lo:words[-1].end])
        for w, p in zip(words, plain):
            if p:                               # a string's words are its value, not code
                scan[w.start - lo:w.end - lo] = ' ' * (w.end - w.start)
        if PS_SCRIPT_TEXT_RE.search(''.join(scan)):
            return True                         # $ExecutionContext.InvokeCommand.InvokeScript('...')
    for w, p in zip(words, plain):
        if p:
            continue                            # git commit -m "Handle InvokeScript": a message
        low = w.text.lower()
        if 'invokeprovider' in low or 'setenvironmentvariable' in low or PS_SCRIPT_TEXT_RE.search(w.text):
            return True
        if not quoted(c, w) and PS_SPLAT_RE.match(w.text):
            return True                         # Set-Item @h: the item and value are in the hashtable
        if NET_ENV_RE.search(w.text):
            if not (0 <= w.start < len(src) and NET_GET_LITERAL_RE.match(src, w.start)):
                return True                     # [Environment]::<anything but a literal GetEnvironmentVariable>
        elif w.dyn and '::' in w.text:
            return True                         # $t::SetEnvironmentVariable, $t::$m(...)
        if low in PS_COMMON_VAR_PARAMS or low.split(':', 1)[0] in PS_COMMON_VAR_PARAMS and ':' in low:
            k = words.index(w)
            val = low.split(':', 1)[1] if ':' in low else (words[k + 1].text.lower() if k + 1 < len(words) else '')
            if 'env:' in val:
                return True
    if inv is None:
        return False
    name, args = inv.name, inv.args
    if name in PS_RESHAPERS:
        return True
    if PS_SCRIPT_RE.search(inv.path) or inv.index > 0 and words[inv.index - 1].text == '.':
        return True                             # a script dot-sourced or run in this process
    if name in ('invoke-command', 'icm') and any(PS_SCRIPT_RE.search(w.text) or w.dyn for w in args):
        return True
    if inv.via == 'start-process' and any(
            not w.dyn and canon_param(w.text, SP_SWITCHES + SP_VALUES, SP_ALIASES) == '-environment'
            for w in words):
        return True
    if name in PS_VARIABLE_CMDLETS:
        if any('env:' in w.text.lower() for w in args):
            return True
        first = [w for w in args if not (w.text.startswith('-') and not w.dyn)][:1]
        if first and first[0].dyn:
            return True
    if name in PS_CD_CMDLETS:
        for w in args:
            if not w.dyn and w.text.startswith('-') and ':' not in w.text:
                continue
            t = w.text.split(':', 1)[1] if not w.dyn and PS_OPT_PREFIX_RE.match(w.text) else w.text
            if w.dyn or ENV_DRIVE_RE.match(t) or '::' in t:
                return True
        if _expression_follows(c):
            return True
    if name in PS_ITEM_CMDLETS:
        return _ps_item_unsafe(c, inv, line_sets)
    return False


def _ps_plain_string(c, w):
    """True when the word is one PowerShell string as written, with nothing it computes: its
    text is a value, never code this command runs."""
    src = c.source or ''
    return (not w.dyn and 0 <= w.start < w.end <= len(src) and
            PS_PLAIN_STRING_RE.fullmatch(src, w.start, w.end) is not None)


def _expression_follows(c):
    """True when the command's text ends where a ( expression starts: the parser reads
    Set-Item -Path ("env:GIT_"+"DIR") as Set-Item -Path, then the expression apart."""
    if not c.words:
        return False
    src, k = c.source or '', c.words[-1].end
    while k < len(src) and src[k] in ' \t':
        k += 1
    return k < len(src) and src[k] == '('


def _ps_item_unsafe(c, inv, line_sets):
    """An item cmdlet whose item the text does not show as a file path or a safe env: name."""
    if c.piped_in and inv.name not in PS_PIPED_VALUE or _expression_follows(c):
        return True                             # the item comes from a pipe or an expression
    args = inv.args
    names = PS_ITEM_PARAMS + PS_ITEM_VALUE_PARAMS + PS_ITEM_SWITCHES
    items, k, pos = [], 0, 0
    while k < len(args):
        w = args[k]
        literal_opt = not quoted(c, w) and not w.dyn and w.text.startswith('-') and len(w.text) > 1
        p = canon_param(w.text, names, PS_ITEM_ALIASES) if literal_opt else None
        if literal_opt and p is None or p in PS_ITEM_SWITCHES:
            k += 1                              # a switch, or a parameter the guard does not know
            continue
        if p is None:
            if not (inv.name in PS_VALUE_SETTERS and pos == 1):
                items.append(w)
            pos += 1
            k += 1
            continue
        if ':' in w.text:
            val, k = sh.Word(w.text.split(':', 1)[1], w.dyn, w.start, w.end), k + 1
        elif k + 1 < len(args):
            val, k = args[k + 1], k + 2
        else:
            return True                         # a parameter whose value the text does not show
        if p in PS_ITEM_PARAMS:
            items.append(val)
    if not items:
        return True
    on_env = any(not w.dyn and ENV_DRIVE_RE.match(w.text) for w in items)
    for w in items:
        if w.dyn:
            m = PS_ENV_READ_RE.match(w.text)
            if not m or on_env or (m.group(1) or m.group(2)).upper() in line_sets:
                return True
            continue
        m = ENV_DRIVE_RE.match(w.text)
        if m:
            rest = w.text[m.end():]
            if rest and env_name_unsafe(rest):
                return True
        elif on_env and env_name_unsafe(w.text):
            return True                         # New-Item -Path env: -Name X, Rename-Item env:A X
    return False


def mark_git_env(parsed):
    """When any command of the line sets GIT_DIR or the like (git_env_set), or may change the
    environment in a way the guard cannot read (git_env_unsafe), every git of the line acts on a
    place the text does not say: c.git_env is that variable's name or the command
    ('`set -a`'), else False. The whole line, not the commands after the setter: expand appends
    nested text (eval, bash -c, Invoke-Expression, a sourced here-string) after the line's own
    commands, so order says nothing about which runs first. A line with no git is not
    affected. c.home_env is the first of HOME_ENV_NAMES the line sets literally (line_env_names),
    else False: git then reads another user config (rules.home_config judges a push with it)."""
    cmds = parsed.commands
    sets = line_env_names(cmds)
    name = next((n for n in map(git_env_set, cmds) if n), None)
    if name is None:
        name = next((why for why in (git_env_unsafe(c, sets) for c in cmds) if why), None)
    home = next((n for n in HOME_ENV_NAMES if n in sets), None)
    for c in cmds:
        c.git_env = name or False
        c.home_env = home or False


# The variables git for Windows finds the user config through: $XDG_CONFIG_HOME/git/config, and
# ~/.gitconfig with ~ from HOME, else HOMEDRIVE+HOMEPATH, else USERPROFILE (observed with git 2.55)
HOME_ENV_NAMES = ('HOME', 'XDG_CONFIG_HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH')


FIND_EXEC = ('-exec', '-execdir', '-ok', '-okdir')


def find_exec_words(cmd, inv):
    """(option, word list) for each command find -exec/-execdir/-ok/-okdir runs, each up to its
    ; or {} +."""
    out = []
    a = inv.args
    k = 0
    while k < len(a):
        if a[k].text in FIND_EXEC and not a[k].dyn:
            j = k + 1
            while j < len(a) and not (a[j].text == ';' or a[j].text == '+' and j > k + 1 and a[j - 1].text == '{}'):
                j += 1
            if j > k + 1:
                out.append((a[k].text, a[k + 1:j]))
            k = j + 1
            continue
        k += 1
    return out


def git_dyn_word(inv):
    """A reason when a git invocation has a word the shell computes where the git rules read it
    (the subcommand, or an option of a subcommand they check), else None."""
    if inv is None or inv.name != 'git':
        return None
    gc = git_call(inv)
    if gc is None:
        return None
    if gc.sub_dyn:
        return 'a git subcommand that is not literal'
    if gc.sub not in GIT_READ_SUBS:
        return None
    values = GIT_SUB_VALUES.get(gc.sub, ())
    rest = gc.rest
    k = 0
    while k < len(rest):
        w = rest[k]
        if w.text == '--' and not w.dyn:
            break
        if not w.dyn and w.text in values:
            k += 2
            continue
        if w.dyn:
            return 'a git %s word that is not literal' % gc.sub
        k += 1
    return None


def expand(parsed):
    """Parse every nested shell's text into `parsed` (in place); returns parsed."""
    k = 0
    while k < len(parsed.commands):
        cmd = parsed.commands[k]
        k += 1
        first = cmd.words[0].text.lower() if cmd.words else ''
        if cmd.shell == sh.CMD and first == 'if':
            parsed.flag('a cmd if statement')
        if cmd.shell == sh.CMD and first == 'for':
            parsed.flag('a cmd for loop')
        inv = invocation(cmd)
        if inv is None:
            continue
        if inv.dyn:
            parsed.flag('a command whose program is not literal (%s)' % inv.path)
            continue
        why = git_dyn_word(inv)
        if why:
            parsed.flag(why)
        if inv.name == 'find' and cmd.shell == sh.BASH:
            for opt, words in find_exec_words(cmd, inv):
                # -execdir/-okdir run in each found file's folder; {} is the found path, filled
                # into any word that holds it (its own nested commands come later in the loop)
                where = None if opt in ('-execdir', '-okdir') else cmd.cwd
                for c in _nest(parsed, cmd, span_text(cmd, words), sh.BASH, where):
                    fill_words(c.words, '{}')
            continue
        if inv.word is not None and inv.path != inv.word.text:
            parsed.flag('a backslash in a program name')
        if inv.word is not None and inv.word.synth:
            # an argument the shell rewrites reads as it runs; a program name so spelled is hiding
            src = cmd.source[inv.word.start:inv.word.end]
            parsed.flag('an ANSI-C escape in a program name' if "$'" in src else 'a brace expansion in a program name')
        found = _nested_text(inv, parsed)
        if not found:
            continue
        text, shell = found[:2]
        where = found[2] if len(found) > 2 else cmd.cwd
        if text is STDIN:
            text = _stdin_text(cmd, parsed)
            if text is None:
                continue
        _nest(parsed, cmd, text, shell, where)
    return parsed


def _nest(parsed, cmd, text, shell, cwd):
    """Parse `text` as commands `cmd` runs, at `cwd`, into parsed; returns those commands."""
    if cmd.depth + 1 > sh.MAX_DEPTH:
        parsed.flag('shells nested deeper than %d' % sh.MAX_DEPTH)
        return []
    _p, cmds = sh.parse(text, shell, cwd, depth=cmd.depth + 1, parsed=parsed)
    for c in cmds:
        if c.nested_in is None:
            c.nested_in = cmd
    return cmds


def read(text, shell, cwd):
    """parse() then expand(): every command `text` runs, nested shells included."""
    parsed, _cmds = sh.parse(text, shell, cwd)
    expand(parsed)
    mark_git_env(parsed)
    return parsed


# ---------------------------------------------------------------- git

GIT_VALUE_OPTS = {'-c', '--namespace', '--super-prefix', '--config-env', '--exec-path'}
# subcommands whose options the git rules read, and per subcommand the options whose value is a
# message, a name or a path (a variable there is only a value)
GIT_READ_SUBS = {'add', 'stage', 'commit', 'checkout', 'switch', 'reset', 'clean', 'stash', 'restore',
                 'branch', 'push', 'worktree', 'rm', 'mv', 'checkout-index', 'read-tree', 'update-ref',
                 'merge', 'rebase', 'cherry-pick', 'revert', 'am', 'pull', 'apply'}
GIT_SUB_VALUES = {
    'commit': ('-m', '--message', '-F', '--file', '-C', '-c', '--reuse-message', '--reedit-message',
               '--author', '--date', '--fixup', '--squash', '-t', '--template', '--trailer'),
    'merge': ('-m', '--message', '-F', '--file', '-s', '--strategy', '-X', '--strategy-option'),
    'stash': ('-m', '--message'),
    'checkout': ('-b', '-B', '--orphan'),
    'switch': ('-c', '-C', '--create', '--force-create', '--orphan'),
    'worktree': ('-b', '-B', '--reason'),
    'push': ('-o', '--push-option', '--repo', '--receive-pack', '--exec'),
}
CORE_WORKTREE_RE = re.compile(r'(?i)^core\.worktree=')


def _git_word(w):
    """The word as the shell passes it to git (a backslash bash drops, dropped)."""
    alt = program_alt(w)
    if alt is None:
        return w
    return sh.Word(alt, w.dyn, w.start, w.end, w.synth)


class GitCall(object):
    """A git invocation: `dir` the directory it acts on (git -C applied to the command's cwd;
    None when the text cannot tell), `sub` its subcommand, `rest` the Words after it, `alias`
    (name, value) when the line defines a git alias (-c alias.name=value). Every word is the
    text the shell passes (a backslash bash drops is dropped); `sub_dyn` the subcommand is not
    literal; `blind` what points git at a place the text does not show, which a -C cannot undo (a
    GIT_ENV_NAMES variable, --git-dir, --work-tree, -c core.worktree, --config-env, a computed
    -c), else None."""
    __slots__ = ('dir', 'sub', 'rest', 'inv', 'unknown', 'alias', 'sub_dyn', 'blind')

    def __init__(self, dir_, sub, rest, inv, unknown, alias=None, sub_dyn=False, blind=None):
        self.dir, self.sub, self.rest, self.inv, self.unknown = dir_, sub, rest, inv, unknown
        self.alias, self.sub_dyn, self.blind = alias, sub_dyn, blind

    @property
    def argv(self):
        return [w.text for w in self.rest]


def git_call(inv):
    """GitCall for a git invocation, or None when inv is not git or names no subcommand. git
    that xargs runs with no subcommand gets one with sub '' and blind 'xargs': the input xargs
    appends may hold --git-dir and the subcommand."""
    if inv is None or inv.name != 'git':
        return None
    d = inv.cmd.cwd
    git_env = inv.cmd.git_env
    unknown = d is None or bool(git_env)
    # what sets a git dir or work tree the text cannot place: -C never undoes it
    blind = (git_env if isinstance(git_env, str) else 'GIT_DIR or the like') if git_env else None
    if blind:
        d = None
    a = [_git_word(w) if inv.cmd.shell == sh.BASH else w for w in inv.args]
    alias = None
    k = 0
    while k < len(a):
        t = a[k].text
        cfg = None
        if t == '-c' or t == '--config-env':
            cfg = a[k + 1].text if k + 1 < len(a) else ''
        elif t.startswith('-c') and not t.startswith('--'):
            cfg = t[2:]
        elif t.startswith('--config-env='):
            cfg = t.split('=', 1)[1]
        if cfg is not None and cfg.lower().startswith('alias.'):
            nm, _eq, val = cfg[len('alias.'):].partition('=')
            alias = (nm, val)
        why = None
        if cfg is not None:
            if t.startswith('--config-env'):
                why = '--config-env'
            elif CORE_WORKTREE_RE.match(cfg):
                why = '-c core.worktree'
            elif t == '-c' and k + 1 < len(a) and a[k + 1].dyn or t != '-c' and t.startswith('-c') and a[k].dyn:
                why = 'the computed -c'
        if why:                                 # core.worktree, or a config from the environment or computed
            d, unknown, blind = None, True, blind or why
        if t == '-C':
            if k + 1 >= len(a) or a[k + 1].dyn:
                d, unknown = None, True
            elif not blind:                     # after --git-dir and the like, -C moves only the cwd
                d = sh.resolve_path(a[k + 1].text, None if unknown else d)
                unknown = d is None
            k += 2
            continue
        if t.startswith('--git-dir') or t.startswith('--work-tree'):
            d, unknown, blind = None, True, blind or ('--git-dir' if t.startswith('--git-dir') else '--work-tree')
            k += 1 if '=' in t else 2
            continue
        if t in GIT_VALUE_OPTS:
            k += 2
            continue
        if t.startswith('-'):
            k += 1
            continue
        break
    if k >= len(a):
        if inv.via == 'xargs':                  # xargs adds the subcommand, and options before it
            return GitCall(None, '', [], inv, True, alias, False, blind or 'xargs')
        return None
    return GitCall(d, a[k].text, a[k + 1:], inv, unknown or a[k].dyn, alias, a[k].dyn, blind)
