"""History commands: log, show and rev-list (revision.c walks with path limiting and the default
history simplification, pretty.c user formats, trailer.c trailer blocks, date.c dates).

Walk order is git's: a queue sorted newest commit date first, parents inserted after every queued
commit with an equal or newer date. Formats and options the kit does not use are Unsupported.
"""
import calendar
import re
import time

from . import command
from . import diffs as _diffs
from . import revs as _revs
from . import util as _u
from .errors import GitExit, Unsupported, die
from .objects import parse_ident

# -- dates ----------------------------------------------------------------------------------

_DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def _tz_minutes(tz):
    try:
        v = int(tz)
    except ValueError:
        return 0
    sign = -1 if v < 0 else 1
    v = abs(v)
    return sign * ((v // 100) * 60 + v % 100)


def _tm(ts, tz):
    return time.gmtime(ts + _tz_minutes(tz) * 60)


def date_default(ts, tz):
    t = _tm(ts, tz)
    return '%s %s %d %02d:%02d:%02d %d %s' % (_DAYS[t.tm_wday], _MONTHS[t.tm_mon - 1], t.tm_mday,
                                              t.tm_hour, t.tm_min, t.tm_sec, t.tm_year, tz)


def date_iso(ts, tz):
    t = _tm(ts, tz)
    return '%04d-%02d-%02d %02d:%02d:%02d %s' % (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour,
                                                 t.tm_min, t.tm_sec, tz)


def date_iso_strict(ts, tz):
    t = _tm(ts, tz)
    if _tz_minutes(tz) == 0 and tz.startswith('+'):
        zone = 'Z'
    else:
        zone = '%s:%s' % (tz[:3], tz[3:5])
    return '%04d-%02d-%02dT%02d:%02d:%02d%s' % (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour,
                                                t.tm_min, t.tm_sec, zone)


def date_short(ts, tz):
    t = _tm(ts, tz)
    return '%04d-%02d-%02d' % (t.tm_year, t.tm_mon, t.tm_mday)


def _plural(n, one, many):
    return (one if n == 1 else many) % n


def date_relative(ts, now=None):
    """date.c show_date_relative."""
    now = int(time.time()) if now is None else now
    if ts > now:
        return 'in the future'
    diff = now - ts
    if diff < 90:
        return _plural(diff, '%d second ago', '%d seconds ago')
    diff = (diff + 30) // 60
    if diff < 90:
        return _plural(diff, '%d minute ago', '%d minutes ago')
    diff = (diff + 30) // 60
    if diff < 36:
        return _plural(diff, '%d hour ago', '%d hours ago')
    diff = (diff + 12) // 24
    if diff < 14:
        return _plural(diff, '%d day ago', '%d days ago')
    if diff < 70:
        return _plural((diff + 3) // 7, '%d week ago', '%d weeks ago')
    if diff < 365:
        return _plural((diff + 15) // 30, '%d month ago', '%d months ago')
    if diff < 1825:
        total = (diff * 12 * 2 + 365) // (365 * 2)
        years, months = total // 12, total % 12
        if months:
            y = _plural(years, '%d year', '%d years')
            return y + ', ' + _plural(months, '%d month ago', '%d months ago')
        return _plural(years, '%d year ago', '%d years ago')
    return _plural((diff + 183) // 365, '%d year ago', '%d years ago')


def approxidate(text):
    """--since/--until values: the forms parse_date takes, and a bare date (YYYY-MM-DD), which
    approxidate completes with the current local time of day."""
    t = text.strip()
    m = re.match(r'^(\d{4})-(\d\d)-(\d\d)$', t)
    if m:
        now = time.localtime()
        fields = (int(m.group(1)), int(m.group(2)), int(m.group(3)), now.tm_hour, now.tm_min,
                  now.tm_sec, 0, 0, -1)
        return int(time.mktime(fields))
    try:
        return _u.parse_date(t)[0]
    except Unsupported:
        raise Unsupported('approxidate %r' % text)


# -- trailers -------------------------------------------------------------------------------

_GIT_GENERATED = ('Signed-off-by: ', '(cherry picked from commit ')


def _lines_with_ends(text):
    out = []
    i = 0
    while i < len(text):
        j = text.find('\n', i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j + 1])
        i = j + 1
    return out


def _is_blank(line):
    return line.strip() == ''


def _find_separator(line, seps=':'):
    ws = False
    for i, c in enumerate(line):
        if c in seps:
            return i
        if not ws and (c.isalnum() and c.isascii() or c == '-'):
            continue
        if i != 0 and c in ' \t':
            ws = True
            continue
        break
    return -1


def _ignored_tail(text, comment):
    """trailer.c/commit.c ignored_log_message_bytes (no scissors line)."""
    boc = None
    in_conflicts = False
    pos = 0
    for line in _lines_with_ends(text):
        if line.startswith(comment) or line.startswith('\n'):
            if boc is None:
                boc = pos
        elif line == 'Conflicts:\n':
            in_conflicts = True
            if boc is None:
                boc = pos
        elif in_conflicts and line.startswith('\t'):
            pass
        elif boc is not None:
            boc = None
            in_conflicts = False
        pos += len(line)
    if '------------------------ >8 ------------------------' in text:
        raise Unsupported('a scissors line in a commit message')
    return len(text) - boc if boc else 0


def trailer_items(msg, comment='#'):
    """[(token or None, value, raw line)] for the trailer block of message `msg`."""
    end = len(msg) - _ignored_tail(msg, comment)
    lines = _lines_with_ends(msg[:end])
    # the title paragraph
    k = 0
    while k < len(lines):
        if lines[k].startswith(comment):
            k += 1
            continue
        if _is_blank(lines[k]):
            break
        k += 1
    title_end = k
    start = None
    only_spaces = True
    recognized = False
    trailer_lines = non_trailer = possible = 0
    for idx in range(len(lines) - 1, title_end - 1, -1):
        line = lines[idx]
        if line.startswith(comment):
            non_trailer += possible
            possible = 0
            continue
        if _is_blank(line):
            if only_spaces:
                continue
            non_trailer += possible
            if recognized and trailer_lines * 3 >= non_trailer:
                start = idx + 1
            elif trailer_lines and not non_trailer:
                start = idx + 1
            break
        only_spaces = False
        if any(line.startswith(p) for p in _GIT_GENERATED):
            trailer_lines += 1
            possible = 0
            recognized = True
            continue
        sep = _find_separator(line)
        if sep >= 1 and not line[0].isspace():
            trailer_lines += 1
            possible = 0
        elif line[0].isspace():
            possible += 1
        else:
            non_trailer += 1
            non_trailer += possible
            possible = 0
    if start is None:
        return []
    raw = []
    last = None
    for line in lines[start:]:
        if line.startswith(comment):
            continue
        if last is not None and line[:1].isspace() and not _is_blank(line):
            raw[last] += line
            continue
        if _is_blank(line):
            continue
        raw.append(line)
        last = len(raw) - 1 if _find_separator(line) >= 1 else None
    out = []
    for r in raw:
        sep = _find_separator(r)
        if sep >= 1:
            out.append((r[:sep].strip(), r[sep + 1:].strip(), r))
        else:
            out.append((None, r.rstrip('\n'), r))
    return out


def _unfold(v):
    return re.sub(r'\s*\n\s*', ' ', v)


def format_trailers(msg, arg, comment='#'):
    """%(trailers[:options])."""
    opts = {}
    if arg:
        for part in arg.split(','):
            k, eq, v = part.partition('=')
            opts[k] = v if eq else True
    for k in opts:
        if k not in ('key', 'valueonly', 'only', 'unfold', 'separator', 'keyonly'):
            raise Unsupported('%%(trailers:%s)' % k)
    items = trailer_items(msg, comment)
    simple = not opts
    if simple:
        return ''.join(r for (_t, _v, r) in items)
    key = opts.get('key')
    if key is True:
        raise Unsupported('%(trailers:key) without a value')
    if key:
        key = key[:-1] if key.endswith(':') else key
    only = bool(key) or _bool_opt(opts.get('only'))
    sep = opts.get('separator')
    if sep is not None:
        sep = _expand_literal(sep)
    out = []
    for tok, val, r in items:
        if tok is not None:
            if key and tok.lower() != key.lower():
                continue
            v = _unfold(val) if _bool_opt(opts.get('unfold')) else val
            piece = ''
            if not _bool_opt(opts.get('valueonly')):
                piece += tok
            if not _bool_opt(opts.get('keyonly')) and not _bool_opt(opts.get('valueonly')):
                piece += ': '
            if not _bool_opt(opts.get('keyonly')):
                piece += v
            if sep is not None:
                if out:
                    out.append(sep)
                out.append(piece)
            else:
                out.append(piece + '\n')
        elif not only:
            if sep is not None:
                if out:
                    out.append(sep)
                out.append(r.rstrip('\n'))
            else:
                out.append(r if r.endswith('\n') else r + '\n')
    return ''.join(out)


def _bool_opt(v):
    if v is None:
        return False
    if v is True:
        return True
    low = v.lower()
    if low in ('true', 'yes', 'on', '1', ''):
        return True
    if low in ('false', 'no', 'off', '0'):
        return False
    raise Unsupported('trailer option value %r' % v)


def _expand_literal(s):
    def rep(m):
        return chr(int(m.group(1), 16))
    s = re.sub(r'%x([0-9a-fA-F]{2})', rep, s)
    return s.replace('%n', '\n').replace('%%', '%')


# -- pretty formats -------------------------------------------------------------------------


def _message(c):
    if c.encoding and c.encoding.lower() not in ('utf-8', 'utf8'):
        raise Unsupported('commit encoding %s' % c.encoding)
    return c.message.decode('utf-8', 'surrogateescape')


def subject_body(msg):
    """(subject joined with spaces, body) as pretty.c format_subject/body split a message."""
    lines = msg.split('\n')
    i = 0
    while i < len(lines) and _is_blank(lines[i]):
        i += 1
    subj = []
    while i < len(lines) and not _is_blank(lines[i]):
        subj.append(lines[i].strip() if False else lines[i].rstrip('\r'))
        i += 1
    while i < len(lines) and _is_blank(lines[i]):
        i += 1
    body = '\n'.join(lines[i:])
    return ' '.join(s.strip() for s in subj) if subj else '', body


_PH = re.compile(r'%(?:\(([^)]*)\)|x([0-9a-fA-F]{2})|([acgG][a-zA-Z])|([HhTtPpsbBnNdDem%fSrK]))')


class Formatter:
    """pretty.c user formats (%H %h %T %t %P %p %an %ae %ad... %s %b %B %n %xNN %(trailers))."""

    def __init__(self, repo, fmt, comment='#'):
        self.repo = repo
        self.fmt = fmt
        self.comment = comment
        self._abbrev = {}

    def abbrev(self, oid):
        got = self._abbrev.get(oid)
        if got is None:
            from .plumbing import abbrev
            got = self._abbrev[oid] = abbrev(self.repo, oid)
        return got

    def format(self, oid):
        c = _revs.commit(self.repo, oid)
        msg = None
        out = []
        pos = 0
        fmt = self.fmt
        while True:
            i = fmt.find('%', pos)
            if i < 0:
                out.append(fmt[pos:])
                break
            out.append(fmt[pos:i])
            m = _PH.match(fmt, i)
            if not m:
                raise Unsupported('format placeholder at %r' % fmt[i:i + 6])
            pos = m.end()
            paren, hexc, two, one = m.groups()
            if hexc is not None:
                out.append(chr(int(hexc, 16)))
                continue
            if paren is not None:
                name, _, arg = paren.partition(':')
                if name != 'trailers':
                    raise Unsupported('%%(%s)' % paren)
                if msg is None:
                    msg = _message(c)
                out.append(format_trailers(msg, arg, self.comment))
                continue
            if two is not None:
                who, what = two[0], two[1]
                if who in 'gG':
                    raise Unsupported('reflog placeholder %%%s' % two)
                raw = c.author if who == 'a' else c.committer
                name, email, ts, tz = parse_ident(raw)
                if what == 'n':
                    out.append(name)
                elif what == 'e':
                    out.append(email)
                elif what == 't':
                    out.append(str(ts))
                elif what == 'd':
                    out.append(date_default(ts, tz))
                elif what == 'i':
                    out.append(date_iso(ts, tz))
                elif what == 'I':
                    out.append(date_iso_strict(ts, tz))
                elif what == 's':
                    out.append(date_short(ts, tz))
                elif what == 'r':
                    out.append(date_relative(ts))
                elif what == 'l':
                    out.append(email.split('@', 1)[0])
                else:
                    raise Unsupported('format placeholder %%%s' % two)
                continue
            ch = one
            if ch == '%':
                out.append('%')
            elif ch == 'n':
                out.append('\n')
            elif ch == 'H':
                out.append(oid)
            elif ch == 'h':
                out.append(self.abbrev(oid))
            elif ch == 'T':
                out.append(c.tree)
            elif ch == 't':
                out.append(self.abbrev(c.tree))
            elif ch == 'P':
                out.append(' '.join(c.parents))
            elif ch == 'p':
                out.append(' '.join(self.abbrev(p) for p in c.parents))
            elif ch in 'sbB':
                if msg is None:
                    msg = _message(c)
                if ch == 'B':
                    out.append(msg)
                else:
                    s, b = subject_body(msg)
                    out.append(s if ch == 's' else b)
            elif ch == 'e':
                out.append(c.encoding or '')
            else:
                raise Unsupported('format placeholder %%%s' % ch)
        return ''.join(out)


def medium(repo, oid, abbrev_parents, prefix_commit='commit '):
    """The default (medium) pretty format, without the trailing separator."""
    c = _revs.commit(repo, oid)
    name, email, ts, tz = parse_ident(c.author)
    out = ['%s%s\n' % (prefix_commit, oid)]
    if len(c.parents) > 1:
        out.append('Merge: %s\n' % ' '.join(abbrev_parents(p) for p in c.parents))
    out.append('Author: %s <%s>\n' % (name, email))
    out.append('Date:   %s\n' % date_default(ts, tz))
    msg = _message(c)
    lines = msg.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    while lines and _is_blank(lines[0]):
        lines.pop(0)
    out.append('\n')
    for line in lines:
        if _is_blank(line):
            out.append('\n')
        else:
            out.append('    ' + line + '\n')
    return ''.join(out)


# -- argument parsing -----------------------------------------------------------------------

_FLAGS = {'--first-parent', '--no-merges', '--merges', '--parents', '--no-walk', '--count',
          '--left-right', '--no-renames', '--name-only', '--name-status', '-m', '--cc', '-c', '-s',
          '--no-patch', '-p', '-u', '--patch', '--no-use-mailmap', '--no-mailmap', '--reverse',
          '-E', '--extended-regexp', '-F', '--fixed-strings', '-i', '--regexp-ignore-case',
          '--no-color', '--no-decorate', '--oneline', '--all', '--branches', '--tags',
          '--remotes', '-z', '--abbrev-commit', '--no-abbrev-commit', '--no-ext-diff',
          '--all-match', '--topo-order', '--date-order', '--full-history', '--numstat',
          '--shortstat', '--stat', '--find-renames', '-M', '--no-notes', '--use-mailmap',
          '--simplify-merges', '--boundary', '--objects', '--quiet', '--root', '--invert-grep'}
_VALUES = {'--format', '--pretty', '--max-count', '-n', '--grep', '--since', '--after', '--until',
           '--before', '--diff-filter', '--skip', '--max-parents', '--min-parents', '--author',
           '--committer', '--date', '--decorate', '--no-walk', '--color', '--abbrev'}
_UNSUPPORTED_FLAGS = {'--stat', '--topo-order', '--simplify-merges', '--boundary',
                      '--objects', '--use-mailmap', '--all-match', '--invert-grep', '--date-order'}


class Args:
    def __init__(self):
        self.flags = set()
        self.values = {}
        self.multi = {}
        self.revs = []           # in order, with '--not' entries
        self.paths = None
        self.max_count = None


def parse_args(cmd, args):
    a = Args()
    i = 0
    n = len(args)
    while i < n:
        x = args[i]
        if x == '--':
            a.paths = list(args[i + 1:])
            break
        if x == '--not':
            a.revs.append(x)
            i += 1
            continue
        if x == '--end-of-options':
            raise Unsupported('%s --end-of-options' % cmd)
        if re.match(r'^-\d+$', x):
            a.max_count = int(x[1:])
            i += 1
            continue
        if x.startswith('-') and len(x) > 1:
            name, eq, val = x.partition('=')
            if name.startswith('-n') and not name.startswith('--') and len(name) > 2:
                a.max_count = int(name[2:])
                i += 1
                continue
            if name == '--no-walk' and not eq:
                a.flags.add('--no-walk')
                a.values['--no-walk'] = 'sorted'
                i += 1
                continue
            if name in ('--pretty', '--decorate', '--color', '--abbrev') and not eq:
                a.values[name] = True
                i += 1
                continue
            if name in _VALUES:
                if not eq:
                    if name in ('--format', '--max-count', '--since', '--after', '--until',
                                '--before', '--diff-filter', '--skip', '--max-parents',
                                '--min-parents', '--date', '--no-walk') and not name == '-n':
                        # git accepts '--format X' only for -n and --grep style options
                        pass
                    i += 1
                    if i >= n:
                        raise GitExit(129, "fatal: option '%s' requires a value\n" % name)
                    val = args[i]
                if name in ('--grep', '--author', '--committer'):
                    a.multi.setdefault(name, []).append(val)
                elif name in ('--max-count', '-n'):
                    a.max_count = int(val)
                else:
                    a.values[name] = val
                i += 1
                continue
            if eq:
                raise Unsupported('git %s %s' % (cmd, x))
            if name in _FLAGS:
                if name in _UNSUPPORTED_FLAGS:
                    raise Unsupported('git %s %s' % (cmd, x))
                a.flags.add(name)
                i += 1
                continue
            if name.startswith('-') and not name.startswith('--') and len(name) > 2:
                # bundled short flags (-sp etc.) are rare; refuse them
                raise Unsupported('git %s %s' % (cmd, x))
            raise Unsupported('git %s %s' % (cmd, x))
        a.revs.append(x)
        i += 1
    return a


def _grep_regex(pattern, flags):
    if '-F' in flags or '--fixed-strings' in flags:
        rx = re.escape(pattern)
    elif '-E' in flags or '--extended-regexp' in flags:
        rx = pattern
        if re.search(r'\\[<>bBwWsS]|\[\[:', rx):
            raise Unsupported('grep pattern %r' % pattern)
    else:
        rx = _bre(pattern)
    return re.compile(rx, re.M | (re.I if ('-i' in flags or '--regexp-ignore-case' in flags) else 0))


def _bre(p):
    out = []
    i = 0
    while i < len(p):
        ch = p[i]
        if ch == '\\' and i + 1 < len(p):
            nx = p[i + 1]
            if nx in '(){}|+?':
                out.append(nx)
            elif nx in '.*[]^$\\':
                out.append('\\' + nx)
            else:
                raise Unsupported('grep pattern %r' % p)
            i += 2
            continue
        if ch in '(){}|+?':
            out.append('\\' + ch)
        elif ch == '[' and p[i:i + 2] == '[[':
            raise Unsupported('grep pattern %r' % p)
        else:
            out.append(ch)
        i += 1
    return ''.join(out)


# -- the walk -------------------------------------------------------------------------------


class Walker:
    """One revision walk: start points, exclusions, path limiting and filters."""

    def __init__(self, ctx, a, cmd):
        self.ctx = ctx
        self.repo = ctx.repo
        self.a = a
        self.cmd = cmd
        self.first_parent = '--first-parent' in a.flags
        self.full_history = '--full-history' in a.flags
        self.reader = _diffs.Reader(self.repo)
        self.include, self.exclude, self.sym = [], [], []
        self.left = set()
        self.paths = []
        self.spec = None
        self.filter = a.values.get('--diff-filter')
        self._sides = {}

    def side(self, oid):
        """Tree side of commit `oid` ({} for None), path-limited."""
        got = self._sides.get(oid)
        if got is None:
            if oid is None:
                got = {}
            else:
                got = _diffs.tree_side(self.repo, _revs.commit(self.repo, oid).tree)
            self._sides[oid] = got
        return got

    def changes(self, oid, parent, renames=False):
        filt = self.spec.match if self.spec is not None else None
        ch = _diffs.compare(self.side(parent), self.side(oid), filt)
        if renames:
            ch = _diffs.detect_renames(ch, self.reader)
        return ch

    def treesame(self, oid, parent):
        ch = self.changes(oid, parent)
        if self.filter:
            ch = _diffs.apply_filter(ch, self.filter)
        return not ch

    def setup(self):
        ctx, repo, a = self.ctx, self.repo, self.a
        specs = list(a.revs)
        paths = list(a.paths) if a.paths is not None else []
        # without '--', a trailing argument that is not a revision must be a path
        if a.paths is None:
            k = 0
            while k < len(specs) and (specs[k] == '--not' or _diffs.is_rev(ctx, specs[k])):
                k += 1
            if k < len(specs):
                rest = specs[k:]
                for p in rest:
                    import os
                    if repo.worktree is None or not os.path.lexists(os.path.join(ctx.cwd, p)):
                        raise _diffs.ambiguous(p)
                    if p == '--not':
                        raise Unsupported('--not among paths')
                paths = rest
                specs = specs[:k]
        else:
            for s in specs:
                if s != '--not' and not _diffs.is_rev(ctx, s):
                    raise GitExit(128, "fatal: bad revision '%s'\n" % s)
        self.paths = paths
        if paths:
            self.spec = _u.Pathspec(ctx.prefix or '', paths)
        from .plumbing import index_reader
        neg = False
        for s in specs:
            if s == '--not':
                neg = not neg
                continue
            self._add(s, neg, index_reader(repo))
        for flag, prefix in (('--all', 'refs/'), ('--branches', 'refs/heads/'),
                             ('--tags', 'refs/tags/'), ('--remotes', 'refs/remotes/')):
            if flag in a.flags:
                from . import refs as _refs
                for name, oid in _refs.list_refs(repo, prefix):
                    c = _revs.peel(repo, oid, 'commit')
                    if c is not None:
                        self.include.append(c)
                if flag == '--all':
                    h, _ = _refs.resolve(repo, 'HEAD')
                    if h:
                        self.include.append(h)
        if not specs and not (a.flags & {'--all', '--branches', '--tags', '--remotes'}):
            if self.cmd == 'rev-list':
                raise GitExit(129, 'usage: git rev-list [<options>] <commit>... [--] [<path>...]\n')
            from . import refs as _refs
            h, _ = _refs.resolve(repo, 'HEAD')
            if h is None:
                br = _revs.current_branch(repo)
                if br is not None:
                    raise die("your current branch '%s' does not have any commits yet" % br)
                raise die("your current branch appears to be broken")
            self.include.append(h)

    def _add(self, s, neg, ir):
        repo = self.repo

        def need(x):
            o = _revs.commit_oid(repo, x or 'HEAD', ir)
            if o is None:
                raise _diffs.ambiguous(s)
            return o
        if '...' in s:
            l, r = s.split('...', 1)
            ol, orr = need(l), need(r)
            self.include += [ol, orr]
            self.exclude += _revs.merge_bases(repo, ol, [orr])
            self.sym.append((ol, orr))
            return
        if '..' in s:
            l, r = s.split('..', 1)
            ol, orr = need(l), need(r)
            if neg:
                self.include.append(ol)
                self.exclude.append(orr)
            else:
                self.exclude.append(ol)
                self.include.append(orr)
            return
        if s.startswith('^'):
            o = need(s[1:])
            (self.include if neg else self.exclude).append(o)
            return
        o = need(s)
        (self.exclude if neg else self.include).append(o)

    def run(self):
        """The commits to show, in order."""
        repo, a = self.repo, self.a
        pruning = bool(self.paths) or bool(self.filter)
        nw = a.values.get('--no-walk') if '--no-walk' in a.flags or '--no-walk' in a.values else None
        if nw is not None:
            if nw not in ('sorted', 'unsorted'):
                raise Unsupported('--no-walk=%s' % nw)
            if pruning:
                raise Unsupported('--no-walk with path limiting')
            hidden = set(self.exclude)
            seen = set()
            order = []
            for o in self.include:
                if o not in seen and o not in hidden:
                    seen.add(o)
                    order.append(o)
            if nw == 'sorted':
                order = _revs.sort_by_date(repo, order)
            out = order
        else:
            hidden = _revs.ancestors(repo, self.exclude) if self.exclude else set()
            start = []
            seen = set()
            for o in self.include:
                if o not in seen:
                    seen.add(o)
                    start.append(o)
            queue = _revs.sort_by_date(repo, start)
            out = []
            while queue:
                o = queue.pop(0)
                c = _revs.commit(repo, o)
                if c is None:
                    raise die('bad object %s' % o)
                parents = c.parents[:1] if self.first_parent else list(c.parents)
                show = True
                if pruning and o not in hidden:
                    if not parents:
                        show = not self.treesame(o, None)
                    elif len(parents) == 1:
                        show = not self.treesame(o, parents[0])
                    elif self.full_history:
                        # --full-history without parent rewriting: walk every parent; the merge
                        # shows unless it is TREESAME to all of them. With --diff-filter (and no
                        # -m/-c/--cc) git shows no diff for a merge, so the filter drops it.
                        if self.filter and not (a.flags & {'-m', '-c', '--cc'}):
                            show = False
                        else:
                            show = not all(self.treesame(o, p) for p in parents)
                    else:
                        for p in parents:
                            if self.treesame(o, p):
                                parents = [p]
                                show = False
                                break
                for p in parents:
                    if p not in seen:
                        seen.add(p)
                        _revs.insert_by_date(repo, queue, p)
                if o not in hidden and show:
                    out.append(o)
        return [o for o in out if self._keep(o)]

    def _keep(self, o):
        a = self.a
        c = _revs.commit(self.repo, o)
        np = len(c.parents)
        if '--no-merges' in a.flags and np > 1:
            return False
        if '--merges' in a.flags and np < 2:
            return False
        mx = a.values.get('--max-parents')
        if mx is not None and np > int(mx):
            return False
        mn = a.values.get('--min-parents')
        if mn is not None and np < int(mn):
            return False
        since = a.values.get('--since') or a.values.get('--after')
        if since is not None:
            if c.commit_time < approxidate(since):
                return False
        until = a.values.get('--until') or a.values.get('--before')
        if until is not None:
            if c.commit_time > approxidate(until):
                return False
        if a.multi.get('--author') or a.multi.get('--committer'):
            raise Unsupported('--author/--committer filters')
        greps = a.multi.get('--grep')
        if greps:
            msg = _message(c)
            if not any(_grep_regex(g, a.flags).search(msg) for g in greps):
                return False
        return True


def limit(a, commits):
    skip = a.values.get('--skip')
    if skip:
        commits = commits[int(skip):]
    if a.max_count is not None and a.max_count >= 0:
        commits = commits[:a.max_count]
    if '--reverse' in a.flags:
        commits = list(reversed(commits))
    return commits


# -- output ---------------------------------------------------------------------------------


def _pretty(a, default):
    """('user', fmt, terminator) | ('oneline',) | ('medium',)."""
    fmt = a.values.get('--format')
    pretty = a.values.get('--pretty')
    if '--oneline' in a.flags:
        return ('oneline', None, True)
    if fmt is not None:
        return _pretty_value(fmt, True)
    if pretty is not None:
        if pretty is True:
            return ('medium', None, False)
        return _pretty_value(pretty, False)
    return default


def _pretty_value(v, is_format):
    if v.startswith('format:'):
        return ('user', v[len('format:'):], False)
    if v.startswith('tformat:'):
        return ('user', v[len('tformat:'):], True)
    if v in ('oneline',):
        return ('oneline', None, True)
    if v in ('medium',):
        return ('medium', None, False)
    if v in ('short', 'full', 'fuller', 'raw', 'email', 'reference', 'mboxrd'):
        raise Unsupported('--pretty=%s' % v)
    if '%' in v or is_format:
        return ('user', v, True)
    raise Unsupported('--pretty=%s' % v)


def _diff_kind(a, default_patch):
    fl = a.flags
    if '-s' in fl or '--no-patch' in fl:
        return None
    for k in ('--name-only', '--name-status', '--numstat', '--shortstat'):
        if k in fl:
            return k[2:]
    if fl & {'-p', '-u', '--patch'}:
        return 'patch'
    if '--cc' in fl or '-c' in fl:
        return 'patch'
    return 'patch' if default_patch else None


class Shower:
    """Prints commits for log and show."""

    def __init__(self, ctx, walker, a, cmd):
        self.ctx = ctx
        self.repo = ctx.repo
        self.w = walker
        self.a = a
        self.cmd = cmd
        self.pretty = _pretty(a, ('medium', None, False))
        if self.pretty[0] == 'user':
            self.fmt = Formatter(self.repo, self.pretty[1], self.repo.config.get('core.commentchar') or '#')
        self.kind = _diff_kind(a, cmd == 'show')
        if '-z' in a.flags:
            raise Unsupported('%s -z' % cmd)
        if '--stat' in a.flags:
            raise Unsupported('%s --stat' % cmd)
        self.renames = '--no-renames' not in a.flags and _diffs.renames_wanted(self.repo, {})
        if a.values.get('--decorate') not in (None, 'no', 'short') and a.values.get('--decorate') is not True:
            raise Unsupported('--decorate=%s' % a.values.get('--decorate'))
        if a.values.get('--decorate') is True or a.values.get('--decorate') == 'short':
            raise Unsupported('--decorate')
        self.count = 0
        if a.values.get('--date') is not None:
            raise Unsupported('--date=%s' % a.values.get('--date'))
        col = a.values.get('--color')
        if col not in (None, 'never'):
            raise Unsupported('--color')

    def header(self, oid):
        kind = self.pretty[0]
        if kind == 'user':
            return self.fmt.format(oid)
        if kind == 'oneline':
            from .plumbing import abbrev
            c = _revs.commit(self.repo, oid)
            s, _b = subject_body(_message(c))
            return '%s %s' % (abbrev(self.repo, oid), s)
        from .plumbing import abbrev
        return medium(self.repo, oid, lambda p: abbrev(self.repo, p))

    def diff_text(self, oid):
        """The diff output for one commit ('' when none), as text or bytes."""
        if self.kind is None:
            return ''
        c = _revs.commit(self.repo, oid)
        a = self.a
        if len(c.parents) > 1:
            if '-m' in a.flags and self.w.first_parent:
                parent = c.parents[0]
            elif '-m' in a.flags or '--cc' in a.flags or '-c' in a.flags or \
                    (self.cmd == 'show' and self.kind == 'patch'):
                raise Unsupported('a combined or per-parent diff of a merge')
            elif self.w.first_parent and self.cmd == 'log':
                parent = c.parents[0]
            else:
                return ''
        else:
            parent = c.parents[0] if c.parents else None
        ch = self.w.changes(oid, parent, renames=self.renames)
        ch = _diffs.apply_filter(ch, self.w.filter)
        quote = _u.quotepath(self.repo)
        if self.kind == 'name-only':
            return _diffs.name_only(ch, quote, False)
        if self.kind == 'name-status':
            return _diffs.name_status(ch, quote, False)
        if self.kind == 'numstat':
            return _diffs.numstat(ch, self.w.reader, quote, False)
        if self.kind == 'shortstat':
            return _diffs.shortstat(ch, self.w.reader)
        from .plumbing import abbrev
        return _diffs.patch(ch, self.w.reader, self.repo, 3, 'a/', 'b/', lambda o: abbrev(self.repo, o))

    def show(self, oid):
        ctx = self.ctx
        kind, _fmt, term = self.pretty
        head = self.header(oid)
        diff = self.diff_text(oid)
        empty_fmt = kind == 'user' and self.pretty[1] == ''
        if kind == 'user' and not term:
            if self.count:
                ctx.write('\n')
            ctx.write(head)
        elif kind == 'medium':
            if self.count:
                ctx.write('\n')
            ctx.write(head)
        else:
            if not empty_fmt or True:
                ctx.write(head)
                if not empty_fmt:
                    ctx.write('\n')
        if diff:
            if kind == 'medium':
                ctx.write('\n')
            elif kind == 'user' and not empty_fmt:
                ctx.write('\n')
            elif kind == 'user' and not term:
                ctx.write('\n')
            ctx.write(diff)
        self.count += 1


# -- commands -------------------------------------------------------------------------------


@command('log')
def log_cmd(ctx, args):
    repo = ctx.need_repo()
    a = parse_args('log', args)
    if a.flags & {'--count', '--left-right', '--parents', '--all', '--root'} - {'--all'}:
        raise Unsupported('log %s' % ' '.join(sorted(a.flags & {'--count', '--left-right', '--parents', '--root'})))
    w = Walker(ctx, a, 'log')
    w.setup()
    commits = limit(a, w.run())
    s = Shower(ctx, w, a, 'log')
    for o in commits:
        s.show(o)
    return 0


@command('rev-list')
def rev_list(ctx, args):
    repo = ctx.need_repo()
    a = parse_args('rev-list', args)
    for f in a.flags:
        if f not in ('--first-parent', '--no-merges', '--merges', '--parents', '--count',
                     '--left-right', '--reverse', '--all', '--branches', '--tags', '--remotes',
                     '-E', '-F', '-i', '--extended-regexp', '--fixed-strings',
                     '--regexp-ignore-case', '--no-walk', '--quiet'):
            raise Unsupported('rev-list %s' % f)
    if a.values.get('--format') or a.values.get('--pretty'):
        raise Unsupported('rev-list --format')
    w = Walker(ctx, a, 'rev-list')
    w.setup()
    commits = limit(a, w.run())
    if '--count' in a.flags:
        if '--left-right' in a.flags:
            if len(w.sym) != 1:
                raise Unsupported('rev-list --left-right without one A...B')
            l, r = w.sym[0]
            left_side = _revs.ancestors(repo, [l])
            right_side = _revs.ancestors(repo, [r])
            nl = sum(1 for o in commits if o in left_side and o not in right_side)
            nr = sum(1 for o in commits if o in right_side and o not in left_side)
            ctx.write('%d\t%d\n' % (nl, nr))
        else:
            ctx.write('%d\n' % len(commits))
        return 0
    if '--left-right' in a.flags:
        raise Unsupported('rev-list --left-right without --count')
    if '--quiet' in a.flags:
        return 0
    if '--parents' in a.flags and w.paths:
        raise Unsupported('rev-list --parents with paths')
    for o in commits:
        if '--parents' in a.flags:
            c = _revs.commit(repo, o)
            ps = c.parents[:1] if w.first_parent else c.parents
            ctx.write(' '.join([o] + list(ps)) + '\n')
        else:
            ctx.write(o + '\n')
    return 0


@command('show')
def show_cmd(ctx, args):
    repo = ctx.need_repo()
    a = parse_args('show', args)
    if a.paths:
        raise Unsupported('show with paths')
    if a.max_count is not None and a.max_count != 1 and len(a.revs) <= 1:
        raise Unsupported('show -n')
    specs = a.revs or ['HEAD']
    if '--not' in specs or any('..' in s or s.startswith('^') for s in specs):
        raise Unsupported('show with a range')
    from .plumbing import resolve
    targets = []
    for s in specs:
        oid = resolve(ctx, s)
        if oid is None:
            colon = _revs._path_colon(s)
            if colon is not None:
                base = s[:colon]
                if resolve(ctx, base) is None:
                    raise die("invalid object name '%s'." % base)
                raise die("path '%s' does not exist in '%s'" % (s[colon + 1:], base))
            raise _diffs.ambiguous(s)
        targets.append((s, oid))
    w = Walker(ctx, a, 'show')
    w.filter = a.values.get('--diff-filter')
    sh = None
    for s, oid in targets:
        kind = _revs.obj_type(repo, oid)
        if kind == 'blob':
            ctx.write(repo.objects.read(oid)[1])
            continue
        if kind == 'commit':
            if sh is None:
                sh = Shower(ctx, w, a, 'show')
            sh.show(oid)
            continue
        raise Unsupported('show of a %s' % kind)
    return 0
