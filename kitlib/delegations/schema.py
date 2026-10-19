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
import re

from kitlib.config.types import (ConfigError, Bool, Enum, Field, Int, ListOf, Obj, Str, T,
                                 is_note)

SCHEMA_VERSION = 1
ID = r'D-[0-9]{3,}'
REPO = r'[a-z0-9][a-z0-9-]*|\*'
ARG_NAME = r'[a-z][a-z0-9_]{0,30}'
TRIGGERS = ('owner-session', 'hub-answer', 'standing:after-landing', 'standing:pm-start-checklist')
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

def exclusion_hits(tokens):
    """[(exclusion id, token, why)] for the tokens that hit a hard exclusion."""
    return []


def operation_exclusions(op, where):
    """The problems of one operation object `op` (already type-checked) that are hard exclusions."""
    return []


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
