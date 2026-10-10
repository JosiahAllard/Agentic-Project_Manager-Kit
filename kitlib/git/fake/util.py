"""Shared pieces of the fake's commands: option parsing in git's parse-options style, C-style path
quoting (quote.c), pathspecs (literal paths, directory prefixes and globs), identities and dates."""
import os
import re
import time

from . import ignore as _ignore
from .errors import GitExit, Unsupported, die

# -- options --------------------------------------------------------------------------------


class Spec:
    """Options of one command: `flags`, `values` (take an argument), `optional` (an argument only
    after '='), `multi` (repeatable values). Each entry is 'q|quiet' style: the first name is the
    key. '--no-<long>' of a flag listed in `negatable` sets it False."""

    def __init__(self, cmd, flags='', values='', optional='', multi='', negatable=''):
        self.cmd = cmd
        self.kind = {}
        self.key = {}
        for kind, group in (('flag', flags), ('value', values), ('optional', optional), ('multi', multi)):
            for item in group.split():
                names = item.split('|')
                for n in names:
                    self.kind[n] = kind
                    self.key[n] = names[0]
        self.negatable = set(negatable.split())


class Opts(dict):
    """Parsed options, stored under each entry's first name; a lookup by any alias of the entry
    ('quiet' for 'q|quiet') finds the same value."""

    def __init__(self, spec):
        super().__init__()
        self._key = spec.key

    def get(self, name, default=None):
        return super().get(self._key.get(name, name), default)

    def __getitem__(self, name):
        return super().__getitem__(self._key.get(name, name))

    def __contains__(self, name):
        return super().__contains__(self._key.get(name, name))


def parse(spec, args, stop_at_nonopt=False, keep_unknown=False):
    """(opts, positionals, paths_after_dashdash or None). Options may follow positionals (git
    permutes) unless stop_at_nonopt. An unknown option is Unsupported (git <cmd> <option>)."""
    opts, pos = Opts(spec), []
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        if a == '--':
            return opts, pos, list(args[i + 1:])
        if a.startswith('--') and len(a) > 2:
            name, eq, val = a[2:].partition('=')
            kind = spec.kind.get(name)
            if kind is None and name.startswith('no-') and name[3:] in spec.negatable:
                opts[spec.key[name[3:]]] = False
                i += 1
                continue
            if kind is None:
                if keep_unknown:
                    pos.append(a)
                    i += 1
                    continue
                raise Unsupported('git %s --%s' % (spec.cmd, name))
            key = spec.key[name]
            if kind == 'flag':
                if eq:
                    raise GitExit(129, "error: option `%s' takes no value\n" % name)
                opts[key] = True
            elif kind == 'optional':
                opts[key] = val if eq else True
            else:
                if not eq:
                    i += 1
                    if i >= n:
                        raise GitExit(129, "error: option `%s' requires a value\n" % name)
                    val = args[i]
                if kind == 'multi':
                    opts.setdefault(key, []).append(val)
                else:
                    opts[key] = val
            i += 1
            continue
        if a.startswith('-') and len(a) > 1 and a != '-':
            j = 1
            while j < len(a):
                ch = a[j]
                kind = spec.kind.get(ch)
                if kind is None:
                    if keep_unknown and j == 1:
                        pos.append(a)
                        break
                    raise Unsupported('git %s -%s' % (spec.cmd, ch))
                key = spec.key[ch]
                if kind == 'flag':
                    opts[key] = True
                    j += 1
                    continue
                if kind == 'optional':
                    rest = a[j + 1:]
                    opts[key] = rest if rest else True
                    break
                rest = a[j + 1:]
                if rest:
                    val = rest
                else:
                    i += 1
                    if i >= n:
                        raise GitExit(129, "error: switch `%s' requires a value\n" % ch)
                    val = args[i]
                if kind == 'multi':
                    opts.setdefault(key, []).append(val)
                else:
                    opts[key] = val
                break
            i += 1
            continue
        if stop_at_nonopt:
            return opts, pos + list(args[i:]), None
        pos.append(a)
        i += 1
    return opts, pos, None


# -- quoting --------------------------------------------------------------------------------

_CQ = {7: 'a', 8: 'b', 9: 't', 10: 'n', 11: 'v', 12: 'f', 13: 'r', 34: '"', 92: '\\'}


def cquote(path, quotepath=True):
    """`path` as git prints it without -z: C-quoted in double quotes when it has a control
    character, '"', '\\', or (quotepath) a byte above 0x7f."""
    b = path.encode('utf-8', 'surrogateescape') if isinstance(path, str) else path
    need = False
    for c in b:
        if c < 0x20 or c in (34, 92, 0x7f) or (quotepath and c >= 0x80):
            need = True
            break
    if not need:
        return b.decode('utf-8', 'surrogateescape')
    out = ['"']
    for c in b:
        if c in _CQ:
            out.append('\\' + _CQ[c])
        elif c < 0x20 or c == 0x7f or (quotepath and c >= 0x80):
            out.append('\\%03o' % c)
        else:
            out.append(chr(c))
    out.append('"')
    s = ''.join(out)
    return s.encode('latin-1', 'surrogateescape').decode('utf-8', 'surrogateescape') if not quotepath else s


def quotepath(repo):
    return repo.config.bool('core.quotepath', True)


def path_out(ctx, path, z):
    """Bytes for one path field: raw with -z, else C-quoted per core.quotePath."""
    if z:
        return path.encode('utf-8', 'surrogateescape')
    q = cquote(path, quotepath(ctx.repo))
    return q.encode('utf-8', 'surrogateescape')


# -- pathspecs ------------------------------------------------------------------------------

_GLOB = re.compile(r'[*?\[]')


class Pathspec:
    """Literal paths and globs from the command line, relative to `prefix` ('' or 'a/b/').
    No magic (':(...)', ':/', ':!') - those are Unsupported."""

    def __init__(self, prefix, specs, literal=False):
        self.items = []
        prefix = prefix or ''
        for s in specs:
            if s.startswith(':') and not literal:
                raise Unsupported('pathspec magic %s' % s)
            s = s.replace('\\', '/')
            full = _normalize(prefix + s if not s.startswith('/') else s, s)
            glob = bool(_GLOB.search(full)) and not literal
            self.items.append((full, glob, s))
        self.empty = not self.items

    def match(self, path, isdir=False):
        if self.empty:
            return True
        for full, glob, _orig in self.items:
            if full == '':
                return True
            if glob:
                if _ignore.wildmatch(full, path, pathname=False):
                    return True
                lit = _GLOB.split(full, 1)[0]
                if lit.endswith('/') and path.startswith(lit) and _ignore.wildmatch(full, path, False):
                    return True
                continue
            f = full.rstrip('/')
            if path == f or path.startswith(f + '/'):
                if full.endswith('/') and path == f and not isdir:
                    continue
                return True
        return False

    def matched_items(self, path, isdir=False):
        """The indexes of items that match `path` (for 'did not match any files')."""
        out = []
        for i, (full, glob, _o) in enumerate(self.items):
            if full == '':
                out.append(i)
            elif glob:
                if _ignore.wildmatch(full, path, pathname=False):
                    out.append(i)
            else:
                f = full.rstrip('/')
                if path == f or path.startswith(f + '/'):
                    out.append(i)
        return out

    def leading_dirs(self):
        """For the walk: the literal directory prefix of each item."""
        out = []
        for full, glob, _o in self.items:
            if glob:
                lit = _GLOB.split(full, 1)[0]
                out.append(lit.rsplit('/', 1)[0] if '/' in lit else '')
            else:
                out.append(full.rstrip('/'))
        return out


def _normalize(path, orig):
    parts = []
    trail = path.endswith('/')
    for p in path.split('/'):
        if p in ('', '.'):
            continue
        if p == '..':
            if not parts:
                raise die("%s: '%s' is outside repository" % (orig, orig))
            parts.pop()
            continue
        parts.append(p)
    out = '/'.join(parts)
    return out + '/' if trail and out else out


def rel_to_prefix(path, prefix):
    """`path` (from the top) as git shows it relative to the cwd prefix."""
    if not prefix:
        return path
    if path.startswith(prefix):
        return path[len(prefix):]
    p = prefix.rstrip('/').split('/')
    q = path.split('/')
    i = 0
    while i < len(p) and i < len(q) - 1 and p[i] == q[i]:
        i += 1
    return '../' * (len(p) - i) + '/'.join(q[i:])


def to_top(ctx, arg):
    """A command-line path, relative to the cwd, as a path from the top of the work tree."""
    repo = ctx.repo
    a = arg.replace('\\', '/')
    if os.path.isabs(arg) or re.match(r'^[A-Za-z]:/', a):
        top = repo.worktree.replace('\\', '/').rstrip('/') + '/'
        full = os.path.normpath(arg).replace('\\', '/')
        if os.path.normcase(full + '/').startswith(os.path.normcase(top)) or \
                os.path.normcase(full) == os.path.normcase(top.rstrip('/')):
            return full[len(top):] if len(full) >= len(top) else ''
        raise die("%s: '%s' is outside repository at '%s'" % (arg, arg, top.rstrip('/')))
    return _normalize((ctx.prefix or '') + a, arg)


# -- identities and dates -------------------------------------------------------------------


def tz_offset(ts=None):
    """'+HHMM' for the local time zone at `ts`."""
    lt = time.localtime(ts if ts is not None else time.time())
    off = lt.tm_gmtoff // 60
    sign = '+' if off >= 0 else '-'
    off = abs(off)
    return '%s%02d%02d' % (sign, off // 60, off % 60)


def parse_date(text):
    """(timestamp, tz) for the date forms git's GIT_*_DATE and --date take that the kit uses:
    '@<ts> <tz>', '<ts> <tz>', ISO 8601 'YYYY-MM-DD[T ]HH:MM:SS[ tz|Z]'. Others Unsupported."""
    t = text.strip()
    m = re.match(r'^@?(\d+)(?:\s+([+-]\d{4}))?$', t)
    if m:
        return int(m.group(1)), m.group(2) or '+0000'
    m = re.match(r'^(\d{4})-(\d\d)-(\d\d)(?:[T ](\d\d):(\d\d)(?::(\d\d))?)?\s*(Z|[+-]\d\d:?\d\d)?$', t)
    if m:
        import calendar
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm, ss = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
        tz = m.group(7)
        fields = (y, mo, d, hh, mm, ss)
        if tz is None:
            ts = int(time.mktime(fields + (0, 0, -1)))
            return ts, tz_offset(ts)
        if tz == 'Z':
            tzs, off = '+0000', 0
        else:
            tzs = tz.replace(':', '')
            off = (int(tzs[1:3]) * 60 + int(tzs[3:5])) * (1 if tzs[0] == '+' else -1)
        ts = calendar.timegm(fields) - off * 60
        return ts, tzs
    raise Unsupported('date format %r' % text)


def ident(ctx, who, override=None):
    """'Name <email> ts tz' for 'author' or 'committer', as git builds it (env, then config).
    Dies as git does when no email is configured (the auto-detected default is never used)."""
    env, cfg = ctx.env, ctx.repo.config if ctx.repo else None
    up = who.upper()
    name = env.get('GIT_%s_NAME' % up)
    email = env.get('GIT_%s_EMAIL' % up)
    if override is not None:
        name, email = override
    if name is None and cfg is not None:
        name = cfg.get('%s.name' % who) or cfg.get('user.name')
    if email is None and cfg is not None:
        email = cfg.get('%s.email' % who) or cfg.get('user.email')
    if email is None:
        email = env.get('EMAIL')
    if not email:
        raise GitExit(128, '\n*** Please tell me who you are.\n\nRun\n\n'
                           '  git config --global user.email "you@example.com"\n'
                           '  git config --global user.name "Your Name"\n\n'
                           "to set your account's default identity.\n"
                           'Omit --global to set the identity only in this repository.\n\n'
                           'fatal: unable to auto-detect email address\n')
    if name is None:
        # git falls back to the system account's name here; the fake never reads or emits it
        raise Unsupported('a %s ident with no configured name' % who)
    if not name:
        raise GitExit(128, 'fatal: empty ident name (for <%s>) not allowed\n' % email)
    d = env.get('GIT_%s_DATE' % up)
    if d:
        ts, tz = parse_date(d)
    else:
        ts = int(time.time())
        tz = tz_offset(ts)
    return '%s <%s> %d %s' % (_crud(name), _crud(email), ts, tz)


def _crud(s):
    s = s.strip(' .,:;<>"\'\\\n\t')
    return s.replace('<', '').replace('>', '').replace('\n', '')


def parse_author_arg(ctx, text):
    """--author 'Name <email>' -> (name, email). The search form is Unsupported."""
    m = re.match(r'^\s*(.*?)\s*<([^>]*)>\s*$', text)
    if not m:
        raise Unsupported('commit --author %r (search form)' % text)
    return m.group(1), m.group(2)


def cleanup_message(msg, mode='strip', comment='#'):
    """The commit message as git's default cleanup leaves it: 'strip' drops comment lines,
    'whitespace' keeps them, 'verbatim' changes nothing. Trailing spaces go, runs of blank lines
    become one, leading and trailing blank lines go, and a final newline is added."""
    if mode == 'verbatim':
        return msg
    lines = msg.replace('\r\n', '\n').split('\n')
    out = []
    blank = False
    for line in lines:
        if mode == 'strip' and line.startswith(comment):
            continue
        line = line.rstrip(' \t\r\v\f')
        if not line:
            blank = True
            continue
        if blank and out:
            out.append('')
        blank = False
        out.append(line)
    if not out:
        return ''
    return '\n'.join(out) + '\n'
