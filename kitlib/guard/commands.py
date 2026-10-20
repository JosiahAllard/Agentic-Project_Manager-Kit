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
SP_ALIASES = {'-args': '-argumentlist', '-path': '-filepath', '-pspath': '-filepath', '-lup': '-loaduserprofile',
              '-nnw': '-nonewwindow', '-rso': '-redirectstandardoutput', '-rse': '-redirectstandarderror',
              '-rsi': '-redirectstandardinput', '-wd': '-workingdirectory', '-runas': '-credential',
              '-wi': '-whatif', '-cf': '-confirm',
              '-vb': '-verbose', '-db': '-debug', '-ea': '-erroraction', '-wa': '-warningaction',
              '-infa': '-informationaction', '-ev': '-errorvariable', '-wv': '-warningvariable',
              '-iv': '-informationvariable', '-ov': '-outvariable', '-ob': '-outbuffer',
              '-pv': '-pipelinevariable', '-proga': '-progressaction'}
# the common parameters every cmdlet takes, Start-Process among them: the switches, the ones that take a
# value. Kept apart from SP_SWITCHES and SP_VALUES, which name Start-Process's own
PS_COMMON_SWITCHES = ('-verbose', '-debug')
PS_COMMON_VALUES = ('-erroraction', '-warningaction', '-informationaction', '-errorvariable', '-warningvariable',
                    '-informationvariable', '-outvariable', '-outbuffer', '-pipelinevariable', '-progressaction')


class Invocation(object):
    """`name` the program's lower-case base name without .exe; `path` as written (the text bash
    passes when a backslash in it is dropped); `args` its Words (Start-Process: the -ArgumentList
    words); `cmd` the Command; `dyn` the program word is not literal; `index` the program word's
    place in cmd.words; `via` 'start-process' when Start-Process runs it, 'xargs' when xargs
    feeds it its arguments; `writes` the files Start-Process redirects into; `word` the program
    Word. Start-Process only: `listed` the -ArgumentList elements as written, before any split
    (the program's command line is their texts joined with spaces), `workdir` the
    -WorkingDirectory Word or None, `unread` why the guard cannot read the call's parameters (expand
    flags it), or None."""
    __slots__ = ('name', 'path', 'args', 'cmd', 'dyn', 'index', 'via', 'writes', 'word', 'listed', 'workdir',
                 'unread')

    def __init__(self, name, path, args, cmd, dyn, index, via=None, writes=(), word=None, listed=(),
                 workdir=None, unread=None):
        self.name, self.path, self.args, self.cmd, self.dyn, self.index = (
            name, path, args, cmd, dyn, index)
        self.via, self.writes, self.word = via, list(writes), word
        self.listed, self.workdir, self.unread = list(listed), workdir, unread

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
        t = sh.ps_dashed(w.text.lower())
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
    `names` (exact, an alias, or the one name it is a prefix of), or None. Its dash may be any of
    shell.PS_DASHES."""
    t = sh.ps_dashed(text.lower().split(':', 1)[0])
    if not t.startswith('-') or len(t) < 2:
        return None
    if t in names:
        return t
    if aliases and t in aliases:
        return aliases[t]
    hits = [n for n in names if n.startswith(t)]
    return hits[0] if len(hits) == 1 else None


def _ps_param_word(cmd, w):
    """True when PowerShell reads the word as a parameter name: not quoted, not computed, a dash
    (any of shell.PS_DASHES) and then a letter, _ or ?. `--headless` and `-1` are values."""
    if quoted(cmd, w) or w.dyn:
        return False
    t = sh.ps_dashed(w.text)
    return len(t) > 1 and t[0] == '-' and (t[1].isalpha() or t[1] in '_?')


class Unread(object):
    """A Start-Process call with no program the guard can name and a parameter it cannot read:
    `why`. invocation gives it only to expand (which flags it); every other caller gets None, as
    for any call with no program, so rolepolicy.lock_verb still reads its words (NO_PROGRAM)."""
    __slots__ = ('why',)

    def __init__(self, why):
        self.why = why


# PowerShell's stop-parsing token between two words (the scanner keeps no word for it)
PS_STOP_PARSING_RE = re.compile('[%s]{2}%%' % ''.join(sh.PS_DASHES))


def _sp_unreadable(cmd, words, i):
    """Why a Start-Process call's binding cannot be read from its words, or None: an unquoted `--`
    (every later word positional), a splat (`@p`: its parameters are in the hashtable) or `--%`
    (the rest of the line passed as written). The guard refuses these rather than read them."""
    for w in words[i + 1:]:
        if quoted(cmd, w):
            continue
        if PS_SPLAT_RE.match(w.text):
            return 'a splatted Start-Process argument (%s)' % w.text
        t = w.text
        if len(t) >= 2 and t[0] in sh.PS_DASHES and t[1] in sh.PS_DASHES and (len(t) == 2 or t[2] == '%'):
            return 'a Start-Process %s (end of parameters or stop-parsing)' % t
    src = cmd.source or ''
    for a, b in zip(words[i:], words[i + 1:]):
        if a.end is None or b.start is None or not 0 <= a.end <= b.start <= len(src):
            return 'a Start-Process call whose words the guard cannot place in its line'
        if PS_STOP_PARSING_RE.search(src[a.end:b.start]):
            return 'a Start-Process --% (stop-parsing)'
    return None


def _start_process(cmd, words, i):
    """Start-Process [-FilePath] prog [[-ArgumentList] args] with its switches anywhere.

    The words carry no commas, so an array (`'/c','echo x'`) is several words. -ArgumentList, in
    its space or colon form, takes every word up to the next parameter word: a FilePath given by
    position after it is the last of them. With no -ArgumentList, every positional word after the
    program is the list (PowerShell binds the whole array). A parameter word that is neither a
    Start-Process parameter nor a common one (or an ambiguous prefix), or a switch given `-Name:`
    and its value after a space, sets `unread`: the guard cannot tell what PowerShell binds, so the
    line is refused, never read with the program shifted. The readings are close to PowerShell's
    binding, not exact: a line PowerShell refuses is read as more text, never less."""
    rest = words[i + 1:]
    names = SP_SWITCHES + SP_VALUES + PS_COMMON_SWITCHES + PS_COMMON_VALUES
    switches = SP_SWITCHES + PS_COMMON_SWITCHES
    prog, writes, positional, listed, workdir = None, [], [], [], None
    unread = _sp_unreadable(cmd, words, i)
    k = 0

    def list_words(j):
        # the words from rest[j] up to the next parameter word
        e = j
        while e < len(rest) and not _ps_param_word(cmd, rest[e]):
            e += 1
        return rest[j:e], e

    while k < len(rest):
        w = rest[k]
        if not _ps_param_word(cmd, w):
            positional.append(w)
            k += 1
            continue
        p = canon_param(w.text, names, SP_ALIASES)
        if p is None:
            unread = unread or 'a Start-Process parameter the guard does not know (%s)' % w.text
            k += 1
            continue
        raw = cmd.source[w.start:w.end] if 0 <= w.start < w.end <= len(cmd.source) else w.text
        # -Name: value, its value the next word; -Name:C: is its value C:, never bare
        bare = ':' in w.text and ':' in raw and raw.split(':', 1)[1] == ''
        if p in switches:
            if bare:
                unread = unread or 'a Start-Process switch given a value after a space (%s)' % w.text
            k += 1
            continue
        if ':' in w.text and not bare:
            vals, k = [sh.Word(w.text.split(':', 1)[1], w.dyn, w.start, w.end)], k + 1
            if p == '-argumentlist':
                more, k = list_words(k)             # -ArgumentList:'/c','echo x'
                vals += more
        elif p == '-argumentlist':
            vals, k = list_words(k + 1)
        else:
            vals, k = rest[k + 1:k + 2], k + 2
        if p == '-filepath' and vals:
            prog = vals[0]
        elif p == '-argumentlist':
            listed.extend(vals)
        elif p in ('-redirectstandardoutput', '-redirectstandarderror'):
            writes.extend(vals)
        elif p == '-workingdirectory' and vals:
            workdir = vals[0]
    if prog is None and positional:
        prog = positional.pop(0)
    if prog is None and len(listed) > 1:
        prog = listed.pop()                         # -ArgumentList '/c ...' cmd: FilePath by position
    listed = positional + listed
    if prog is None:
        # no program the guard can name: None as for any such call, an Unread for expand to refuse
        return None if unread is None else Unread(unread)
    args = [x for v in listed for x in _split_list(v)]
    return Invocation(base_name(prog.text), prog.text, args, cmd, prog.dyn, i, via='start-process',
                      writes=writes, word=prog, listed=listed, workdir=workdir, unread=unread)


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


def invocation(cmd, unread=False):
    """The Invocation `cmd` runs, or None (an assignment only, an empty command, a keyword that
    runs no program, a PowerShell expression). With `unread` (expand only), a Start-Process call
    with no program and a parameter the guard cannot read gives an Unread instead of None."""
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
            sp = _start_process(cmd, words, i)
            return sp if unread or not isinstance(sp, Unread) else None
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


# what cmd drops before a command name: @ (no echo) and its delimiters , ; =
CMD_LEAD_RE = re.compile(r'^[@;,=]+')


def cmd_invocation(cmd, inv):
    """`inv`, cmd's invocation of `cmd`, with the @ and delimiters cmd drops before a command name
    (CMD_LEAD_RE) left out, as a prefix of the program word or as words of their own: @set, ;set
    and @ set all run set, @ path runs path. Another shell's, a computed or quoted program word,
    and one with nothing to drop come back as they are. The environment rules read cmd's set, path
    and call through it; invocation itself still names @git as `@git` (the program rules do not
    read the lead)."""
    if cmd.shell != sh.CMD or inv is None or inv.dyn or inv.word is None:
        return inv
    words, i = cmd.words, inv.index

    def placed_quoted(w):               # a word with no place in the source reads as unquoted
        return w.start is not None and quoted(cmd, w)
    while (i + 1 < len(words) and not words[i].dyn and not placed_quoted(words[i])
           and CMD_LEAD_RE.sub('', words[i].text) == ''):
        i += 1
    w = words[i]
    if w.dyn or placed_quoted(w):
        return inv if i == inv.index else Invocation(base_name(w.text), w.text, words[i + 1:], cmd, w.dyn, i,
                                                      via=inv.via, word=w)
    t = CMD_LEAD_RE.sub('', w.text)
    if not t or (i == inv.index and t == w.text):
        return inv
    return Invocation(base_name(t), t, words[i + 1:], cmd, w.dyn, i, via=inv.via, word=w)


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
                   'custompipename', 'configurationfile')
PS_VALUE_ALIASES = {'ep', 'ex', 'w', 'wd', 'of', 'o', 'if', 'v', 'ea'}
# switches whose next word is read both as their value and as the start of the command: -i and
# -in are -InputFormat with a value to powershell.exe and -Interactive without one to pwsh; an
# -EncodedArguments prefix is judged both ways too (so it is read before PS_VALUE_PARAMS is)
PS_VALUE_EITHER = {'i', 'in', 'ea'}
# such switches on one reading path that _nested_text follows both ways; past that it flags the
# line and reads each further one as taking no value
PS_FORKS_MAX = 16
# the switches powershell.exe 5.1 knows, each (name, shortest prefix it takes), and its aliases
# (observed on 5.1 for every prefix). It runs any other switch, and every word after it, as the
# command. The server modes are left out: they read no command line, so treating them as
# unknown only reads more.
PS51_SWITCHES = (('command', 'c'), ('configurationname', 'config'), ('encodedcommand', 'e'),
                 ('encodedarguments', 'encodeda'), ('executionpolicy', 'ex'), ('file', 'f'),
                 ('help', 'h'), ('inputformat', 'i'), ('mta', 'mta'), ('noexit', 'noe'),
                 ('nologo', 'nol'), ('noninteractive', 'noni'), ('noprofile', 'nop'),
                 ('outputformat', 'o'), ('sta', 'st'), ('windowstyle', 'w'), ('workingdirectory', 'wo'))
PS51_ALIASES = {'ec', 'ea', 'ep', 'if', 'of', 'wd', '?'}


def _ps51_known(t):
    """True when powershell.exe 5.1 reads the switch word `t` ('-name', lower case) as a switch."""
    body = t[1:]
    return body in PS51_ALIASES or any(body.startswith(short) and name.startswith(body)
                                       for name, short in PS51_SWITCHES)


def _ps_takes_value(t):
    body = t[1:]
    if body in PS_VALUE_ALIASES:
        return True
    return len(body) >= 2 and body not in ('in', 'i') and any(p.startswith(body) for p in PS_VALUE_PARAMS)


def _ps_value_either(t):
    body = t[1:]
    return body in PS_VALUE_EITHER or (body.startswith('encodeda') and 'encodedarguments'.startswith(body))


def _ps_switch(w):
    """A powershell.exe or pwsh switch word as '-name' in lower case (it reads /Command and
    –Command as -Command), or its text in lower case."""
    t = w.text.lower()
    return '-' + t[1:] if t[:1] == '/' else sh.ps_dashed(t)


def _ps_command_switch(t):
    return t.startswith('-') and len(t) > 1 and ('command'.startswith(t[1:]) or t == '-cwa' or
                                                 'commandwithargs'.startswith(t[1:]))


def span_text(cmd, words):
    """The words as the command's source writes them (quotes kept), or joined when the source
    positions do not fit."""
    src = cmd.source
    if words and all(0 <= x.start <= x.end <= len(src) for x in words) and words[0].start <= words[-1].end:
        return src[words[0].start:words[-1].end]
    return ' '.join(x.text for x in words)


SP_SHELLS = BASH_SHELLS | PS_SHELLS | {'cmd'}
CMD_RUN_SWITCHES = ('/c', '/k', '/r', '//c', '//k', '//r')
# a word that may hold a run switch cmd reads, not on its own: cmd splits its switches at , ; =
# as well as spaces and finds a / switch inside a word (/cecho, /q/c, ;/c, x/c, "/c"), so a /c,
# /k or /r anywhere in a word before the run switch is searched for, never matched at its start
CMD_RUN_HIDDEN_RE = re.compile(r'(?i)/[ckr]')


def _cmd_hidden_switch(text):
    """True when a cmd word that is not a run switch on its own may still hold one."""
    return bool(CMD_RUN_HIDDEN_RE.search(text))


def _nested_text(inv, parsed):
    """(text, shell) a nested shell or eval runs, (text, shell, cwd) when it runs elsewhere
    (env -C, Start-Process -WorkingDirectory), (STDIN, shell) when the shell reads its commands
    from standard input, or None. For powershell and pwsh, and cmd started by Start-Process, the
    text is a tuple of readings, each a text or STDIN, once each."""
    if inv.via == 'start-process' and inv.name in SP_SHELLS:
        return _sp_shell_text(inv, parsed)
    return _shell_text(inv, parsed)


# the characters PowerShell reads in a -WorkingDirectory as a wildcard pattern (Start-Process
# resolves the text as a pattern, so '<drive>\repo?' finds <drive>\repo1) and its pattern escape
SP_WORKDIR_WILD = frozenset('*?[`')
SP_WORKDIR_DEVICE = ('\\\\.\\', '\\\\?\\')


def sp_workdir_unplaceable(text):
    """True when the literal -WorkingDirectory `text` of a Start-Process call names no one folder
    the guard can place exactly: empty; a wildcard character (SP_WORKDIR_WILD); a bare drive C:
    or a drive-relative C:x (PowerShell reads both from that drive's current location, which the
    text does not tell); a colon past the drive (another PowerShell drive or provider, a stream);
    a device path \\\\.\\ or \\\\?\\; a Git Bash /c/x spelling (shell.msys_to_win), which PowerShell
    reads as a folder on the current drive. An absolute path, a share, a FileSystem::<abs> path,
    ~ and a plain relative path are placed (shell.resolve_path)."""
    p = sh.ps_filesystem_path(text) or text
    if not p or any(c in SP_WORKDIR_WILD for c in p):
        return True
    if re.match(r'^[A-Za-z]:(?![\\/])', p) or ':' in p[2:]:
        return True
    if p[:4].replace('/', '\\') in SP_WORKDIR_DEVICE:
        return True
    return sh.msys_to_win(p) != p


def run_dir(inv):
    """The directory the program of `inv` runs in: its Command's (Command.cwd), or for a
    Start-Process call with a -WorkingDirectory the folder that names (shell.resolve_path from the
    Command's directory), None when the text is computed or names no one folder exactly
    (sp_workdir_unplaceable). PowerShell itself still places what Start-Process redirects into
    (-RedirectStandardOutput, -RedirectStandardError) from its own location, the Command's
    directory: only what the program reads relative to its directory moves."""
    if inv.via != 'start-process' or inv.workdir is None:
        return inv.cmd.cwd
    w = inv.workdir
    if w.dyn or sp_workdir_unplaceable(w.text):
        return None
    return sh.resolve_path(w.text, inv.cmd.cwd, shell=sh.PS, home=inv.cmd.home)


def _sp_shell_text(inv, parsed):
    """_nested_text for a shell Start-Process starts. Start-Process hands the program one command
    line, its -ArgumentList elements joined with spaces; a computed element, or a computed
    -WorkingDirectory, makes the text unreadable. A literal -WorkingDirectory the guard cannot
    place exactly (sp_workdir_unplaceable) gives the nested line no directory (None), so a write
    there is refused as one whose directory is unknown. cmd runs the rest of that line after its
    /c (/k, /r), read with its outer quotes kept and dropped (cmd keeps or drops them by what lies
    between); bash and powershell read the words _split_list made of each element."""
    if any(w.dyn for w in inv.listed):
        parsed.flag('a shell Start-Process starts with an argument list that is not literal (%s)' % inv.path)
        return None
    where = ()
    if inv.workdir is not None:
        if inv.workdir.dyn:
            parsed.flag('a shell Start-Process starts in a working directory that is not literal (%s)' % inv.path)
            return None
        where = (run_dir(inv),)
    if inv.name != 'cmd':
        found = _shell_text(inv, parsed)
        return found and found[:2] + where
    line = ' '.join(w.text for w in inv.listed)
    for m in re.finditer(r'\S+', line):
        t = m.group(0).lower()
        if t in CMD_RUN_SWITCHES:
            rest = line[m.end():].strip()
            if not rest:
                return None
            readings = (rest,)
            if rest.startswith('"'):
                k = rest.rfind('"')
                readings += ((rest[1:k] + rest[k + 1:]) if k > 0 else rest[1:],)
            return (readings, sh.CMD) + where
        if _cmd_hidden_switch(t):
            parsed.flag('a cmd switch Start-Process passes that the guard cannot read (%s)' % m.group(0))
            return None
    return None


def _shell_text(inv, parsed):
    """_nested_text for every program but a shell Start-Process starts."""
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
        where = None if val.dyn else sh.resolve_path(val.text, inv.cmd.cwd, home=parsed.home)
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
        # the readings, each a text or STDIN, kept once each and parsed each on its own
        # (commands.expand): one reading's unclosed <# or quote hides no other
        lit = 'a nested powershell command whose text is not literal'
        seen = {}

        def nested(why, *readings):
            # every way the shells may read the words is judged
            texts = [words for words in readings if words]
            if any(w.dyn for words in texts for w in words):
                parsed.flag(why)
                return None
            return joined(tuple(' '.join(w.text for w in words) for words in texts))

        def joined(*found):
            out = []
            for r in (r for f in found for r in f or ()):
                if r not in out:
                    out.append(r)
            return tuple(out) or None

        def scan(k, unknown, forks):
            # forks is in the key: past PS_FORKS_MAX a path reads less, so its result is its own
            if (k, unknown, forks) not in seen:
                seen[(k, unknown, forks)] = read_from(k, unknown, forks)
            return seen[(k, unknown, forks)]

        def read_from(k, unknown, forks):
            while k < len(a):
                t = _ps_switch(a[k])
                if t.startswith('-e') and 'encodedcommand'.startswith(t[1:]) or t in ('-ec', '-enc'):
                    parsed.flag('an encoded PowerShell command')
                    return None
                # powershell.exe runs a switch it does not know, and every word after it, as the
                # command (unknown); pwsh refuses it, or reads it as a switch, and goes on
                first = a[unknown:] if unknown is not None else []
                if _ps_command_switch(t):
                    rest = a[k + 1:]
                    if [w.text for w in rest] == ['-']:
                        if unknown is None:
                            return (STDIN,)
                        parsed.flag(lit)
                        return None
                    return nested('a nested powershell -Command whose text is not literal', first, rest)
                if unknown is None and t.startswith('-') and t != '-' and not _ps51_known(t):
                    # a value switch included: powershell.exe never takes its value
                    unknown = k
                if t.startswith('-') and len(t) > 1 and 'file'.startswith(t[1:]):
                    if k + 1 < len(a) and not a[k + 1].dyn and a[k + 1].text == '-':
                        # `-File -` reads the script from standard input, as `-Command -` does;
                        # the words after it, if any, never make it a file
                        if unknown is None:
                            return (STDIN,)
                        parsed.flag(lit)
                        return None
                    return nested(lit, first) if first else None
                if t.startswith('-') and _ps_value_either(t) and k + 1 < len(a):
                    if forks < PS_FORKS_MAX:
                        # the next word as the switch's value, and as the start of the command
                        return joined(scan(k + 1, unknown, forks + 1), scan(k + 2, unknown, forks + 1))
                    # past the cap the line is flagged, and the reading with no value still judged
                    parsed.flag('a powershell switch line with too many readings')
                    k += 1
                    continue
                if t.startswith('-'):
                    k += 2 if _ps_takes_value(t) else 1      # as pwsh reads it
                    continue
                # a word that is no switch starts the command: pwsh's, and powershell.exe's when no
                # unknown switch started it before (pwsh reads `--c x` as `-c x`, powershell.exe
                # runs `--c x`); a shell that took it as the value of a switch before it reads a
                # later -Command
                later = next((j for j in range(k + 1, len(a)) if _ps_command_switch(_ps_switch(a[j]))), None)
                then = a[later + 1:] if later is not None else []
                if [w.text for w in then] == ['-']:
                    parsed.flag(lit)
                    return None
                return nested(lit, first, a[k:], then)
            if unknown is not None:
                # powershell.exe runs the words from the unknown switch on; pwsh, when it knows
                # the switch, reads standard input
                found = nested(lit, a[unknown:])
                return found and joined(found, (STDIN,))
            return (STDIN,)
        readings = scan(0, None, 0)
        return (readings, sh.PS) if readings else None
    if name == 'cmd':
        for k, w in enumerate(a):
            if w.dyn:
                # a computed word before the run switch may itself be /c
                parsed.flag('a cmd switch that is not literal')
                return None
            if w.text.lower() not in CMD_RUN_SWITCHES and _cmd_hidden_switch(w.text):
                parsed.flag('a cmd switch the guard cannot read (%s)' % w.text)
                return None
            if w.text.lower() in CMD_RUN_SWITCHES:
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
        words = [w for w in words if not sh.ps_dashed(w.text).startswith('-')][:1]
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
            # "$b" | iex runs the string after its $ expands: the text as written is not the code
            if any(w.dyn for w in src.words):
                parsed.flag('a shell reading text that is not literal')
            return ' '.join(w.text for w in src.words)
        parsed.flag('a shell reading its commands from a pipe')
        return None
    if cmd.piped_in:
        parsed.flag('a shell reading its commands from a pipe')
    return None


# $env:FOO = v, $env:FOO='v' (one word to the parser), ${env:FOO} += v: read from the source
PS_ENV_SET_RE = re.compile(r'(?i)\$(?:\{env:([^}]+)\}|env:(\w+))\s*(?:[-+*/%]|\?\?)?=(?!=)\s*(.*)')
# the environment provider's drive in every spelling a path may give it: env:, Environment:: and
# the module-qualified Microsoft.PowerShell.Core\Environment:: (any qualifier, either slash: fail
# closed), each with an optional separator before the name
ENV_DRIVE = r'(?:env:|(?:[\w.]+[\\/])?environment::)[\\/]?'
PS_ENV_ITEM_RE = re.compile(r'(?i)^(?:-\w+:)?' + ENV_DRIVE + r'(.+)$')
PS_VALUE_OPT_RE = re.compile(r'(?i)^-va(?:l(?:ue?)?)?(?::(.*))?$')      # -Va, -Val, -Value, -Value:x
NET_SET_ENV_RE = re.compile(r'(?i)SetEnvironmentVariable\s*\(\s*[\'"]([^\'"]+)[\'"]\s*,\s*(?:\'([^\']*)|"([^"]*))')
BASH_ASSIGN_RE = ASSIGN_RE                                               # FOO=x and FOO+=x
# the cmdlets that set an env: item (Set-Content env:FOO x, New-Item -Path env: -Name FOO), the env:
# drive named bare, and New-Item's -Name (-N, -Na, -Nam, -Name, -Name:x)
PS_ENV_SETTERS = ('set-item', 'si', 'new-item', 'ni', 'set-content', 'sc', 'add-content', 'ac')
PS_ENV_DRIVE_RE = re.compile(r'(?i)^(?:-\w+:)?' + ENV_DRIVE + r'$')
PS_NAME_OPT_RE = re.compile(r'(?i)^-n(?:a(?:me?)?)?(?::(.*))?$')
# any cmdlet that may make an env: item under a new name (Rename-Item env:X -NewName FOO, Copy-Item)
PS_ENV_ITEM_CMDLETS = PS_ENV_SETTERS + ('rename-item', 'ren', 'rni', 'copy-item', 'copy', 'cp', 'cpi', 'move-item',
                                        'mi', 'move', 'mv', 'set-variable', 'sv')
PS_ENV_REF_RE = re.compile(r'(?i)^(?:-\w+:)?' + ENV_DRIVE)
PS_ENV_DRIVE_PREFIX_RE = re.compile(r'(?i)^' + ENV_DRIVE)
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
    env:FOO, New-Item env:FOO or New-Item -Path env: -Name FOO (the drive in any spelling of
    ENV_DRIVE: Environment::FOO, Microsoft.PowerShell.Core\\Environment::FOO), or
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
    inv = cmd_invocation(c, invocation(c))
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
    sets, or None: every spelling env_assignments and ps_env_targets read, export (or the like) naming one with no
    value (export GIT_DIR after read GIT_DIR), and a word naming one in a cmdlet that may make an
    env: item (PS_ENV_ITEM_CMDLETS: Rename-Item env:X -NewName GIT_DIR, Copy-Item env:X
    env:GIT_DIR). A word that only names one (echo "GIT_DIR=x", Write-Output $env:GIT_DIR,
    Get-Content env:GIT_DIR) does not. Every word is read as the shell passes it (said: a
    backslash bash drops, dropped). A name the text computes, and any other way to change the
    environment, is not read: the guard catches honest mistakes, not evasion."""
    for name, _val in env_assignments(c, GIT_ENV_NAMES):
        return name
    words = c.words
    if not words:
        return None
    if c.shell == sh.PS:
        for name in ps_env_targets(c):          # $env:A, $env:GIT_DIR = 1, 'x' and $a = $env:GIT_DIR = 'x'
            if GIT_ENV_NAME_RE.match(name):
                return name
        m = GIT_ENV_NET_RE.search(c.source[c.start:c.end]) if 0 <= c.start <= c.end <= len(c.source) else None
        if m:
            return m.group(1)
    inv = cmd_invocation(c, invocation(c))
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


# ---------------------------------------------------------------- the environment a line sets

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
    """The upper-case names of the environment variables the line's commands set literally:
    NAME=value in every spelling env_assignments reads, and $env:NAME = v (ps_env_targets)."""
    names = set()
    for c in commands:
        names.update(n.upper() for n, _v in env_assignments(c, r'[A-Za-z_]\w*'))
        names.update(n.upper() for n in ps_env_targets(c))
    return names


MAPFILE_VALUE_OPTS = 'dnOsuCc'                  # mapfile/readarray options that take a value


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


def mark_git_env(parsed):
    """When any command of the line sets GIT_DIR or the like (git_env_set), every git of the
    line acts on a place the text does not say: c.git_env is that variable's name, else False.
    The whole line, not the commands after the setter: expand appends nested text (eval, bash
    -c, Invoke-Expression, a sourced here-string) after the line's own commands, so order says
    nothing about which runs first. A line with no git is not affected. c.home_env is the first
    of HOME_ENV_NAMES the line sets literally (line_env_names), else False: git then reads
    another user config (rules.home_config judges git with it). c.path_env is True when the line
    sets PATH literally: git then finds a filter's bare program name (git-lfs) elsewhere
    (rules.archive_filter_set). A name the text computes is not read (honest mistakes only)."""
    cmds = parsed.commands
    sets = line_env_names(cmds)
    name = next((n for n in map(git_env_set, cmds) if n), None)
    home = next((n for n in HOME_ENV_NAMES if n in sets), None)
    path = 'PATH' in sets
    for c in cmds:
        c.git_env = name or False
        c.home_env = home or False
        c.path_env = path


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


def call_subexpr(inv):
    """True when PowerShell's call operator runs what a ( ) subexpression computes (`& (Get-Command
    git) push`, `& (Join-Path $r x.exe) a`; kitlib.guard.shell reads the ( ) as the program word).
    The program is not literal, but the line stays clean: rules.call_subexpr_pmdata judges it (a PM
    data repository in every session but the owner's own; an agent session anywhere), so it makes
    no other line unreadable."""
    if inv is None or not inv.dyn or inv.cmd.shell != sh.PS or inv.word is None or inv.via is not None:
        return False
    k = inv.index
    return inv.word.text.startswith('(') and k > 0 and inv.cmd.words[k - 1].text == '&'


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
        inv = invocation(cmd, unread=True)
        if isinstance(inv, Unread):
            parsed.flag(inv.why)                # Start-Process, no program and a parameter unread
            continue
        if inv is None:
            continue
        if inv.unread:
            # Start-Process with a parameter the guard cannot read: refused, its program still read
            parsed.flag(inv.unread)
        if cmd.split:
            # `Set-Content -Value $s.Trim() -Path x`: the scanner cut the command at a ( ) or { }
            # and read the rest as commands of their own; its arguments are not all known
            parsed.flag('a command the line splits around a ( ) or { } group, its arguments past it '
                        'read as other commands (%s)' % inv.path)
        if inv.dyn:
            if not call_subexpr(inv):           # rules.call_subexpr_pmdata judges `& (...)`
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
                for c in _nest(parsed, cmd, span_text(cmd, words), sh.BASH, where,
                               None if where is None else cmd.cwd_home):
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
        where, where_home = (found[2], found[2]) if len(found) > 2 else (cmd.cwd, cmd.cwd_home)
        # a tuple is a powershell line's readings: each is parsed on its own, standard input once
        for one in text if isinstance(text, tuple) else (text,):
            if one is STDIN:
                one = _stdin_text(cmd, parsed)
                if one is None:
                    continue
            _nest(parsed, cmd, one, shell, where, where_home)
    return parsed


# the nested texts one line's read parses at most (every one passes through _nest), past which the
# line is flagged: each nested powershell reading may fork again, so without a cap on the total a
# line of repeated `-i pwsh` makes some 17 readings per level, 17^6 parses down to MAX_DEPTH. The
# readings are not merged across levels: the same text under another outer command is judged with
# that command's chain (nested_in), so only this total bounds the work.
MAX_NESTS = 64


def _nest(parsed, cmd, text, shell, cwd, cwd_home):
    """Parse `text` as commands `cmd` runs, at `cwd` (cwd_home: shell.track), into parsed;
    returns those commands."""
    if cmd.depth + 1 > sh.MAX_DEPTH:
        parsed.flag('shells nested deeper than %d' % sh.MAX_DEPTH)
        return []
    if parsed.nests >= MAX_NESTS:
        parsed.flag('more than %d nested shell texts' % MAX_NESTS)
        return []
    parsed.nests += 1
    _p, cmds = sh.parse(text, shell, cwd, depth=cmd.depth + 1, parsed=parsed, cwd_home=cwd_home)
    for c in cmds:
        if c.nested_in is None:
            c.nested_in = cmd
    return cmds


def read(text, shell, cwd, home=None):
    """parse() then expand(): every command `text` runs, nested shells included. `~` is `home`
    (shell.home_folder)."""
    parsed, _cmds = sh.parse(text, shell, cwd, home=home)
    expand(parsed)
    mark_git_env(parsed)
    return parsed


# ---------------------------------------------------------------- git

# git's global options read with the next word as their value (-C, --git-dir and --work-tree are
# read apart): one missing here makes its value look like the subcommand. --attr-source is in
# git's manual; --shallow-file is git's own, undocumented. Listing an option git does not take is
# safe: git stops at an option it does not know, so nothing after it runs.
GIT_VALUE_OPTS = {'-c', '--namespace', '--super-prefix', '--config-env', '--exec-path', '--attr-source',
                  '--shallow-file'}
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
    """A git invocation: `dir` the directory it acts on (git -C applied to the directory git
    starts in, run_dir: the command's cwd, or a Start-Process -WorkingDirectory; None when the
    text cannot tell), `sub` its subcommand, `rest` the Words after it, `alias`
    (name, value) when the line defines a git alias (-c alias.name=value). Every word is the
    text the shell passes (a backslash bash drops is dropped); `sub_dyn` the subcommand is not
    literal; `blind` what points git at a place the text does not show, which a -C cannot undo (a
    GIT_ENV_NAMES variable, --git-dir, --work-tree, -c core.worktree, --config-env, a computed
    -c), else None. `synth`: how many Words at the start of `rest` a git alias's value gave (the
    guard's expansion of one), not words of the line."""
    __slots__ = ('dir', 'sub', 'rest', 'inv', 'unknown', 'alias', 'sub_dyn', 'blind', 'synth')

    def __init__(self, dir_, sub, rest, inv, unknown, alias=None, sub_dyn=False, blind=None, synth=0):
        self.dir, self.sub, self.rest, self.inv, self.unknown = dir_, sub, rest, inv, unknown
        self.alias, self.sub_dyn, self.blind, self.synth = alias, sub_dyn, blind, synth

    @property
    def argv(self):
        return [w.text for w in self.rest]

    def before(self):
        """The invocation's Words before the word that names the subcommand (git's own options)."""
        a = list(self.inv.args)
        return a[:len(a) - (len(self.rest) - self.synth) - 1]

    def after(self):
        """The invocation's Words after the word that names the subcommand (`rest` without the
        words an alias's value gave)."""
        a = list(self.inv.args)
        n = len(self.rest) - self.synth
        return a[len(a) - n:] if n else []


def git_call(inv):
    """GitCall for a git invocation, or None when inv is not git or names no subcommand. git
    that xargs runs with no subcommand gets one with sub '' and blind 'xargs': the input xargs
    appends may hold --git-dir and the subcommand."""
    if inv is None or inv.name != 'git':
        return None
    d = run_dir(inv)                            # Start-Process -WorkingDirectory: git starts there
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
                d = sh.resolve_path(a[k + 1].text, None if unknown else d, home=inv.cmd.home)
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
