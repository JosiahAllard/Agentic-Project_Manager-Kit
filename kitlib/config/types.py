"""The small type language the `.kit.json` schema is written in (schema.py).

A type checks a value and appends one message per problem to `errs` (`where` names the value, as
`"gate".steps[2].cmd`), so one load reports every problem at once. `describe()` is the type as
docs/kit-json.md prints it. Objects accept notes at every level: a key starting `_`, or `comment`.
parse_json() reads config text and refuses a key written twice in one object (json.loads would
silently keep the last one).
"""
import json
import re


class ConfigError(Exception):
    """A kit configuration exists but cannot be used. Its message names every problem found."""


def parse_json(text, where):
    """The JSON value `text` holds. ConfigError when it is not JSON, or when any object in it
    writes a key twice (notes included), naming every such key."""
    twice = []

    def pairs(items):
        out = {}
        for k, v in items:
            if k in out and k not in twice:
                twice.append(k)
            out[k] = v
        return out

    try:
        value = json.loads(text, object_pairs_hook=pairs)
    except ValueError as e:
        raise ConfigError('%s is not valid JSON: %s' % (where, e))
    if twice:
        raise ConfigError('%s: key(s) written twice in one object: %s' % (where, ', '.join(twice)))
    return value


def is_note(key):
    return isinstance(key, str) and (key.startswith('_') or key == 'comment')


class Field(object):
    """One key: its type, its default (None: absent unless set; the reading tool decides) and a
    one-line description."""

    def __init__(self, type_, default, doc):
        self.type = type_
        self.default = default
        self.doc = doc


class T(object):
    def check(self, v, where, errs):
        raise NotImplementedError

    def describe(self):
        raise NotImplementedError


class Any(T):
    def __init__(self, what='any JSON value'):
        self.what = what

    def check(self, v, where, errs):
        pass

    def describe(self):
        return self.what


class Bool(T):
    def check(self, v, where, errs):
        if not isinstance(v, bool):
            errs.append('%s must be true or false' % where)

    def describe(self):
        return 'bool'


class Int(T):
    def __init__(self, lo=None, hi=None):
        self.lo, self.hi = lo, hi

    def check(self, v, where, errs):
        if type(v) is not int or (self.lo is not None and v < self.lo) or (self.hi is not None and v > self.hi):
            errs.append('%s must be a whole number%s' % (where, self._range()))

    def _range(self):
        if self.lo is not None and self.hi is not None:
            return ' from %d to %d' % (self.lo, self.hi)
        if self.lo is not None:
            return ' >= %d' % self.lo
        return ''

    def describe(self):
        return 'int' + self._range()


class Str(T):
    """A string; `nonempty` refuses '' and blanks; `pattern` must match the whole string."""

    def __init__(self, nonempty=True, pattern=None, what=None):
        self.nonempty = nonempty
        self.pattern = re.compile(pattern) if pattern else None
        self.what = what

    def check(self, v, where, errs):
        if not isinstance(v, str):
            errs.append('%s must be a string' % where)
        elif self.nonempty and not v.strip():
            errs.append('%s must be a non-empty string' % where)
        elif self.pattern and not self.pattern.fullmatch(v):
            errs.append('%s must be %s, not %r' % (where, self.what or 'like %s' % self.pattern.pattern, v))

    def describe(self):
        return self.what or ('string' if self.nonempty else 'string, may be empty')


class Email(Str):
    def __init__(self):
        Str.__init__(self, pattern=r'[^@\s<>]+@[^@\s<>]+', what='an email address')

    def describe(self):
        return 'email'


class Regex(T):
    """A string that compiles as a Python regular expression, holding the named `groups`."""

    def __init__(self, groups=()):
        self.groups = tuple(groups)

    def check(self, v, where, errs):
        if not isinstance(v, str) or not v:
            errs.append('%s must be a non-empty regular expression' % where)
            return
        try:
            rx = re.compile(v)
        except re.error as e:
            errs.append('%s does not compile: %s' % (where, e))
            return
        missing = [g for g in self.groups if g not in rx.groupindex]
        if missing:
            errs.append('%s needs the named group(s) %s' % (where, ', '.join(missing)))

    def describe(self):
        return 'regex' + (' with group %s' % ', '.join(self.groups) if self.groups else '')


class RelPath(T):
    """A path inside the repository, relative to its root: no drive, no leading `/`, no `..`."""

    def check(self, v, where, errs):
        if not isinstance(v, str) or not v.strip():
            errs.append('%s must be a non-empty repo-relative path' % where)
            return
        p = v.strip().replace('\\', '/')
        if p.startswith('/') or re.match(r'^[A-Za-z]:', p) or '..' in p.split('/'):
            errs.append('%s must be a path inside the repository, relative to it: %r' % (where, v))

    def describe(self):
        return 'repo path'


class Enum(T):
    def __init__(self, *values):
        self.values = values

    def check(self, v, where, errs):
        if v not in self.values or isinstance(v, bool):
            errs.append('%s must be one of %s' % (where, ', '.join(map(repr, self.values))))

    def describe(self):
        return '|'.join(str(v) for v in self.values)


class ListOf(T):
    def __init__(self, item, nonempty=False, length=None):
        self.item, self.nonempty, self.length = item, nonempty, length

    def check(self, v, where, errs):
        if not isinstance(v, list):
            errs.append('%s must be a list' % where)
            return
        if self.nonempty and not v:
            errs.append('%s must not be empty' % where)
        if self.length is not None and len(v) != self.length:
            errs.append('%s must hold exactly %d items' % (where, self.length))
        for i, x in enumerate(v):
            self.item.check(x, '%s[%d]' % (where, i), errs)

    def describe(self):
        n = '%d ' % self.length if self.length else ''
        return 'list of %s%s' % (n, self.item.describe())


class MapOf(T):
    """An object whose keys are free (non-empty strings; `key`, a type, checks each one when
    given) and whose values share one type."""

    def __init__(self, value, what='key', key=None):
        self.value, self.what, self.key = value, what, key

    def check(self, v, where, errs):
        if not isinstance(v, dict):
            errs.append('%s must be an object' % where)
            return
        for k, x in v.items():
            if is_note(k):
                continue
            if not k.strip():
                errs.append('%s has an empty key' % where)
            elif self.key is not None:
                self.key.check(k, '%s key' % where, errs)
            self.value.check(x, '%s[%r]' % (where, k), errs)

    def describe(self):
        return 'map %s -> %s' % (self.what, self.value.describe())


class Obj(T):
    """An object with known keys (`fields`: name -> Field); an unknown key refuses (a typo must
    never silently mean the default). `required` keys must be present."""

    def __init__(self, fields, required=(), what=None):
        self.fields = fields
        self.required = tuple(required)
        self.what = what

    def check(self, v, where, errs):
        if not isinstance(v, dict):
            errs.append('%s must be an object' % where)
            return
        bad = sorted(k for k in v if k not in self.fields and not is_note(k))
        if bad:
            errs.append('%s: unknown key(s) %s (allowed: %s)'
                        % (where, ', '.join(bad), ', '.join(sorted(self.fields)) or 'none'))
        for k in self.required:
            if k not in v:
                errs.append('%s needs "%s"' % (where, k))
        for k, f in self.fields.items():
            if k in v and not (v[k] is None and f.default is None):
                f.type.check(v[k], '%s.%s' % (where, k), errs)

    def describe(self):
        return self.what or 'object {%s}' % ', '.join(self.fields)


class Either(T):
    def __init__(self, *types):
        self.types = types

    def check(self, v, where, errs):
        for t in self.types:
            e = []
            t.check(v, where, e)
            if not e:
                return
        errs.append('%s must be %s' % (where, ' or '.join(t.describe() for t in self.types)))

    def describe(self):
        return ' or '.join(t.describe() for t in self.types)


def check(type_, value, where):
    """Raise ConfigError naming every problem, or return `value`."""
    errs = []
    type_.check(value, where, errs)
    if errs:
        raise ConfigError('; '.join(errs))
    return value
