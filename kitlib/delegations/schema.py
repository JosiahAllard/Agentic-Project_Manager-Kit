"""The owner's delegation list, schema 1 (docs/delegations.md): the checks of one parsed file.

Pure: no file system. kitlib.delegations.load reads the file and calls check(). Every problem is
appended to one list, so one refusal names them all. Nothing here grants or runs anything.

Built on kitlib.config.types, with one change: Strict refuses `null` for every key, where Obj
skips the check of a `null` whose default is None (a required `"elevated": null` would pass).

A command is split on whitespace only, and a character a shell would read as more than text
(COMMAND_CHARS) is refused, so the tokens are exactly what a later runner passes, with only
declared `{name}` arguments filled in (check_arg). The hard exclusions (exclusion_hits,
operation_exclusions) are fixed here; the file cannot widen or disable them.
"""
import collections
import os
import re

from kitlib.config import user
from kitlib.config.types import (ConfigError, Bool, Enum, Field, Int, ListOf, Obj, Str, T,
                                 is_note)
from kitlib.host import registry

SCHEMA_VERSION = 1
ID = r'D-[0-9]{3,}'
REPO = r'[a-z0-9][a-z0-9-]*|\*'
ARG_NAME = r'[a-z][a-z0-9_]{0,30}'
TRIGGERS = ('owner-session', 'hub-answer', 'hub-approval', 'standing:after-landing',
            'standing:pm-start-checklist')
SHELLS = ('PowerShell', 'Bash')
KIT_SIGIL = '<kit>'
# Characters refused anywhere in a command: quotes, chaining, redirection, variables and
# expansion, subexpressions and script blocks, splatting, arrays, wildcards, comments, line breaks.
# `{` and `}` are allowed only as a whole `{name}` token, `<` and `>` only as the KIT_SIGIL.
COMMAND_CHARS = set('"\'`;|&<>$%(){}[]@,*?#\r\n\0')
PLACEHOLDER = re.compile(r'\{(%s)\}' % ARG_NAME)
ANGLE = re.compile(r'<[^<>]*>')
# An argument value: this charset whatever the owner's pattern says, no leading `-`, no `..`.
ARG_CHARS = re.compile(r'[A-Za-z0-9._:/\\-]{1,200}')
# A pattern that fullmatches any of these is too broad: it would pass a separator or a variable.
ARG_PROBES = ('', 'a b', 'a;b', 'a|b', 'a&b', '$x', 'a\nb', 'a"b')


class Strict(Obj):
    """Obj that refuses `null` for every key it knows: no key of this schema may be null."""

    def check(self, v, where, errs):
        if isinstance(v, dict):
            for k, x in v.items():
                if x is None and k in self.fields and not is_note(k):
                    errs.append('%s.%s must not be null' % (where, k))
        Obj.check(self, v, where, errs)


class Pattern(T):
    """An argument pattern: a regular expression that compiles and passes no ARG_PROBES value."""

    def check(self, v, where, errs):
        if not isinstance(v, str) or not v:
            errs.append('%s must be a non-empty regular expression' % where)
            return
        try:
            rx = re.compile(v)
        except re.error as e:
            errs.append('%s does not compile: %s' % (where, e))
            return
        broad = [p for p in ARG_PROBES if rx.fullmatch(p)]
        if broad:
            errs.append('%s is too broad: it matches %s' % (where, ', '.join(map(repr, broad))))

    def describe(self):
        return 'regex'


class UniqueList(ListOf):
    def check(self, v, where, errs):
        ListOf.check(self, v, where, errs)
        if isinstance(v, list):
            seen = []
            for x in v:
                if x in seen:
                    errs.append('%s names %r twice' % (where, x))
                seen.append(x)


ARG = Strict({
    'name': Field(Str(pattern=ARG_NAME, what='an argument name'), None, 'the name a {name} token uses'),
    'pattern': Field(Pattern(), None, 'the regex a value must fullmatch'),
}, required=('name', 'pattern'), what='{name, pattern}')

PRECONDITIONS = {
    'file-exists': Strict({
        'kind': Field(Enum('file-exists'), None, ''),
        'path': Field(Str(), None, 'the file that must exist'),
    }, required=('kind', 'path')),
    'branch-landed': Strict({
        'kind': Field(Enum('branch-landed'), None, ''),
        'repo_path': Field(Str(pattern=r'[A-Za-z]:[\\/].*', what='an absolute path'), None, 'the main checkout'),
        'branch': Field(Str(), None, 'the branch that must have landed'),
    }, required=('kind', 'repo_path', 'branch')),
    'checklist-line': Strict({
        'kind': Field(Enum('checklist-line'), None, ''),
        'text': Field(Str(), None, 'the text a PM start checklist line must hold'),
    }, required=('kind', 'text')),
}


class Precondition(T):
    def check(self, v, where, errs):
        if not isinstance(v, dict):
            errs.append('%s must be an object' % where)
            return
        kind = v.get('kind')
        t = PRECONDITIONS.get(kind) if isinstance(kind, str) else None
        if t is None:
            errs.append('%s.kind must be one of %s' % (where, ', '.join(map(repr, PRECONDITIONS))))
            return
        t.check(v, where, errs)

    def describe(self):
        return 'precondition'


OPERATION = Strict({
    'id': Field(Str(pattern=ID, what='an id D-nnn'), None, 'the operation id'),
    'what': Field(Str(), None, 'what it does, in words'),
    'repo': Field(Str(pattern=REPO, what='a lower-case repo name or *'), None, 'the repository it acts on'),
    'command': Field(Str(), None, 'the command, split on whitespace'),
    'shell': Field(Enum(*SHELLS), None, 'the host tool that runs it'),
    'args': Field(ListOf(ARG), None, 'the arguments a {name} token takes'),
    'preconditions': Field(ListOf(Precondition()), None, 'what must hold before a run'),
    'reversible': Field(Bool(), None, 'whether it can be undone'),
    'undo': Field(Str(), None, 'how it is undone; required when reversible'),
    'trigger': Field(UniqueList(Enum(*TRIGGERS), nonempty=True), None, 'what may start it'),
    'elevated': Field(Bool(), None, 'must be false'),
}, required=('id', 'what', 'repo', 'command', 'shell', 'args', 'preconditions', 'reversible',
             'trigger', 'elevated'), what='operation')

FILE = Strict({
    'schema': Field(Int(), None, 'must be 1'),
    'operations': Field(ListOf(OPERATION), None, 'the delegated operations'),
    'never': Field(ListOf(Str()), None, 'free text for the owner; the exclusions are in code'),
}, required=('schema', 'operations'), what='delegation list')

Operation = collections.namedtuple(
    'Operation', 'id what repo shell command argv args preconditions reversible undo trigger')
Arg = collections.namedtuple('Arg', 'name pattern')


def _norm(token):
    """A token as the exclusions compare it: lower case, `/` for `\\`, outer quotes dropped."""
    return token.strip().strip('"\'').replace('\\', '/').lower()


# --- Hard exclusions -------------------------------------------------------------------------

# Each is matched against a normalised token (_norm) and its executable name (_base). A prefix
# entry (name, n) refuses any option `-x` that is a prefix of `name` at least n characters long,
# because PowerShell accepts any unambiguous prefix of a parameter name.
WHY = {
    'E1': 'elevation',
    'E2': 'a settings, rules or kit control file',
    'E3': "the machine lock's owner controls",
    'E4': 'opaque code',
    'E5': 'a launcher option the owner keeps',
}
E1_WORDS = {'runas', 'sudo', 'gsudo', 'psexec', 'psexec64', 'schtasks', 'register-scheduledtask',
            'new-scheduledtaskprincipal', 'set-scheduledtask', 'set-executionpolicy', '/rl'}
E1_PARAMS = (('-verb', 5), ('-runlevel', 4), ('-credential', 3))
# Matched as a substring of the token with a `/` added, so a folder name matches with or
# without its trailing separator. Every host's own paths (hosts.json profile owner_paths: its
# rules file and settings folder) and its home config file (state_directory + .json) are named
# from the host registry. The machine config folders, current and retired, are the ones
# kitlib.config.user.locate names: _CONFIG_DIRS asks it about a home that does not exist, so it
# reads nothing real, and the retired name stays spelled in the config package alone.
_CONFIG_DIRS = tuple(os.path.basename(os.path.dirname(p)) + '/' for p in (
    lambda loc: (loc.new, loc.old))(user.locate(os.path.join(os.sep, 'no-such-home'))))
E2_FRAGMENTS = tuple(dict.fromkeys(f.lower() for f in (
    ('settings.json', 'settings.local.json', user.DIR_NAME + '/', 'agents.md',
     'owner-delegations', '.kit.json', 'content/roles', 'content/skills', '.git/hooks',
     '.git/config', 'hookspath', 'machine_setup', 'render_content')
    + _CONFIG_DIRS
    + tuple(registry.union('owner_paths'))
    + tuple(d.rstrip('/') + '.json' for d in registry.union('state_directory')))))
E3_FRAGMENTS = ('machine_lock', 'machine-lock', 'lock_ui', 'lockmcp', 'lock_server', 'lock_set_',
                'lock_hold')
E4_WORDS = {'iex', 'invoke-expression', 'invoke-command', 'cmd', 'wsl'}
POWERSHELL = {'powershell', 'pwsh'}
PS_CODE = ('command', 'encodedcommand', 'ec')
# PowerShell options that take the next token as their value; any other bare token before -File
# is a command PowerShell would run.
PS_VALUE = ('executionpolicy', 'windowstyle', 'version', 'workingdirectory', 'wd', 'inputformat',
            'outputformat', 'configurationname', 'psconsolefile', 'settingsfile', 'custompipename')
PYTHON = re.compile(r'python[0-9.]*w?|py|pyw')
SHELLS_C = {'bash', 'sh', 'zsh', 'dash'}
E5_OPTIONS = ('--no-agent', '--prompt', '--force', '--replace', '--pid')
E5_WORDS = {'install-autostart', 'homes'}


def _base(n):
    b = n.rsplit('/', 1)[-1]
    return b[:-4] if b.endswith('.exe') else b


def _option_prefix(n, name, least):
    return len(n) >= least and name.startswith(n)


def _powershell_code(rest):
    """Why the arguments `rest` of powershell/pwsh run code that is not a -File script, or None."""
    i = 0
    while i < len(rest):
        n = _norm(rest[i])
        if n[:1] in '-/' and len(n) >= 2:
            opt = n[1:].split(':', 1)[0]
            if 'file'.startswith(opt):
                return None
            if any(c.startswith(opt) for c in PS_CODE[:2]) or opt == PS_CODE[2]:
                return 'powershell %s' % rest[i]
            if any(v.startswith(opt) for v in PS_VALUE):
                i += 1
            i += 1
            continue
        return 'powershell with the command %s' % rest[i]
    return 'powershell without -File'


def _python_code(rest):
    """Why `python ...` runs code from an option (-c, -m), or None."""
    i = 0
    while i < len(rest):
        t = rest[i].strip('"\'')
        if t == '--' or not t.startswith('-') or t == '-':
            return None
        if t.startswith('--'):
            i += 1
            continue
        for j, c in enumerate(t[1:], 1):
            if c in 'cm':
                return 'python %s' % rest[i]
            if c in 'XW':
                if j == len(t) - 1:
                    i += 1
                break
        i += 1
    return None


def _shell_code(rest):
    for t in rest:
        t = t.strip('"\'')
        if t == '--' or not t.startswith('-'):
            return None
        if not t.startswith('--') and 'c' in t[1:]:
            return 'a shell %s' % t
    return None


def exclusion_hits(tokens):
    """[(exclusion id, token, why)] for the tokens that hit a hard exclusion. A runner must call
    this again on the filled-in argv: check_arg covers one value, not the line it lands in."""
    hits = []
    for i, tok in enumerate(tokens):
        n = _norm(tok)
        if not n:
            continue
        b = _base(n)
        param = n.split(':', 1)[0]                                   # -Verb:RunAs
        if n in E1_WORDS or b in E1_WORDS or any(_option_prefix(param, p, k) for p, k in E1_PARAMS):
            hits.append(('E1', tok, WHY['E1']))
        if any(f in n + '/' for f in E2_FRAGMENTS):
            hits.append(('E2', tok, WHY['E2']))
        if any(f in n for f in E3_FRAGMENTS):
            hits.append(('E3', tok, WHY['E3']))
        code = None
        if n == '.' or b in E4_WORDS:
            code = tok
        elif b in POWERSHELL:
            code = _powershell_code(tokens[i + 1:])
        elif PYTHON.fullmatch(b):
            code = _python_code(tokens[i + 1:])
        elif b in SHELLS_C:
            code = _shell_code(tokens[i + 1:])
        if code:
            hits.append(('E4', tok, '%s (%s)' % (WHY['E4'], code)))
        opt = n.split('=', 1)[0]
        if (opt.startswith('--') and len(opt) > 2 and any(o.startswith(opt) for o in E5_OPTIONS)) \
                or n in E5_WORDS:
            hits.append(('E5', tok, WHY['E5']))
    return hits


def operation_exclusions(op, where):
    """The problems of one operation object `op` (already type-checked) that are hard exclusions."""
    out = []
    if op['elevated']:
        out.append('%s.elevated: true is %s (E1, never delegable)' % (where, WHY['E1']))
    fields = [('command', op['command'])]
    for j, pre in enumerate(op['preconditions']):
        for s in _strings({k: x for k, x in pre.items() if k != 'kind'}):
            fields.append(('preconditions[%d]' % j, s))
    for name, text in fields:
        for eid, tok, why in exclusion_hits(text.split()):
            out.append('%s.%s: %r is %s (%s, never delegable)' % (where, name, tok, why, eid))
    return out


# --- Commands and arguments ------------------------------------------------------------------

def command_problems(text, declared, where):
    """(tokens, problems) for a command string: every token's characters and placeholders."""
    tokens = text.split()
    out = []
    for i, tok in enumerate(tokens):
        w = '%s token %d %r' % (where, i, tok)
        m = PLACEHOLDER.fullmatch(tok)
        if m:
            if m.group(1) not in declared:
                out.append('%s: {%s} is not a declared argument' % (w, m.group(1)))
            continue
        rest = tok[len(KIT_SIGIL):] if tok.startswith(KIT_SIGIL) else tok
        if ANGLE.search(rest):
            out.append('%s: an unfilled placeholder %s (only %s is kept)'
                       % (w, ANGLE.search(rest).group(0), KIT_SIGIL))
            continue
        bad = sorted(set(rest) & COMMAND_CHARS)
        if bad:
            out.append('%s: refused character(s) %s' % (w, ' '.join(map(repr, bad))))
    return tokens, out


def text_problems(text, declared, where):
    """The placeholder problems of a precondition string: only declared {name}, no <...>."""
    out = []
    for name in re.findall(r'\{([^{}]*)\}', text):
        if name not in declared:
            out.append('%s: {%s} is not a declared argument' % (where, name))
    for m in ANGLE.finditer(text.replace(KIT_SIGIL, '')):
        out.append('%s: an unfilled placeholder %s (only %s is kept)' % (where, m.group(0), KIT_SIGIL))
    return out


def check_arg(arg, value):
    """The problems of `value` for the argument `arg` (an Arg): [] when it may be used. The owner's
    pattern must fullmatch, and the value must also pass ARG_CHARS, not start with `-`, hold no
    `..` and hit no hard exclusion."""
    if not isinstance(value, str):
        return ['%s: the value must be a string' % arg.name]
    out = []
    if not arg.pattern.fullmatch(value):
        out.append('%s: %r does not match %s' % (arg.name, value, arg.pattern.pattern))
    if not ARG_CHARS.fullmatch(value):
        out.append('%s: %r holds a character outside %s' % (arg.name, value, ARG_CHARS.pattern))
    if value.startswith('-'):
        out.append('%s: %r starts with -, an option' % (arg.name, value))
    if '..' in value:
        out.append('%s: %r holds ..' % (arg.name, value))
    for eid, tok, why in exclusion_hits([value]):
        out.append('%s: %r is %s (%s, never delegable)' % (arg.name, tok, why, eid))
    return out


# --- The file --------------------------------------------------------------------------------

def _strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for k, x in v.items():
            if not is_note(k):
                yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


def check(data, where):
    """The Operations of the parsed file `data`, or ConfigError naming every problem."""
    errs = []
    FILE.check(data, where, errs)
    if errs:
        raise ConfigError('; '.join(errs))
    if data['schema'] != SCHEMA_VERSION:
        raise ConfigError('%s: unsupported schema %r; this kit reads %d'
                          % (where, data['schema'], SCHEMA_VERSION))
    ops, seen = [], set()
    for i, op in enumerate(data['operations']):
        w = '%s: operations[%d] (%s)' % (where, i, op['id'])
        if op['id'] in seen:
            errs.append('%s: id written twice' % w)
        seen.add(op['id'])
        names = [a['name'] for a in op['args']]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            errs.append('%s.args names %s twice' % (w, ', '.join(dup)))
        declared = set(names)
        argv, probs = command_problems(op['command'], declared, w + '.command')
        errs.extend(probs)
        used = {m.group(1) for t in argv for m in [PLACEHOLDER.fullmatch(t)] if m}
        for j, pre in enumerate(op['preconditions']):
            for s in _strings({k: x for k, x in pre.items() if k != 'kind'}):
                errs.extend(text_problems(s, declared, '%s.preconditions[%d]' % (w, j)))
                used.update(n for n in re.findall(r'\{([^{}]*)\}', s))
        for n in names:
            if n not in used:
                errs.append('%s.args: %s is used by no {%s} token' % (w, n, n))
        if op['reversible'] and 'undo' not in op:
            errs.append('%s: reversible needs "undo"' % w)
        if 'hub-answer' in op['trigger'] and not op['reversible']:
            errs.append('%s: trigger hub-answer needs reversible true (an irreversible operation '
                        'waits for the owner at the machine)' % w)
        errs.extend(operation_exclusions(op, w))
        ops.append(Operation(
            op['id'], op['what'], op['repo'], op['shell'], op['command'], tuple(argv),
            tuple(Arg(a['name'], re.compile(a['pattern'])) for a in op['args']),
            tuple(tuple(sorted((k, x) for k, x in p.items() if not is_note(k)))
                  for p in op['preconditions']),
            op['reversible'], op.get('undo'), tuple(op['trigger'])))
    if errs:
        raise ConfigError('; '.join(errs))
    return tuple(ops)
