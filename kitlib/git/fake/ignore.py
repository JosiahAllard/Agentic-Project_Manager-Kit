"""gitignore rules (dir.c) and wildmatch (wildmatch.c), ported."""
import os

from . import config as _config

# wildmatch results
_MATCH, _NOMATCH, _ABORT_ALL, _ABORT_TO_STARSTAR = 0, 1, -1, -2


def wildmatch(pattern, text, pathname=True, casefold=False):
    """True when `text` matches glob `pattern` (git's wildmatch with WM_PATHNAME)."""
    if casefold:
        pattern, text = pattern.lower(), text.lower()
    return _dowild(pattern, 0, text, 0, pathname) == _MATCH


_CLASSES = {
    'alnum': str.isalnum, 'alpha': str.isalpha, 'blank': lambda c: c in ' \t',
    'cntrl': lambda c: ord(c) < 32 or ord(c) == 127, 'digit': str.isdigit,
    'graph': lambda c: c.isprintable() and c != ' ', 'lower': str.islower,
    'print': str.isprintable, 'punct': lambda c: c.isprintable() and not c.isalnum() and c != ' ',
    'space': str.isspace, 'upper': str.isupper, 'xdigit': lambda c: c in '0123456789abcdefABCDEF',
}


def _dowild(p, pi, t, ti, pathname):
    plen, tlen = len(p), len(t)
    while pi < plen:
        pch = p[pi]
        if ti >= tlen and pch != '*':
            return _ABORT_ALL
        tch = t[ti] if ti < tlen else ''
        if pch == '\\':
            pi += 1
            if pi >= plen:
                return _NOMATCH
            if t[ti] != p[pi]:
                return _NOMATCH
        elif pch == '?':
            if pathname and tch == '/':
                return _NOMATCH
        elif pch == '*':
            pi += 1
            if pi < plen and p[pi] == '*':
                prev_start = pi - 1
                while pi < plen and p[pi] == '*':
                    pi += 1
                if not pathname:
                    match_slash = True
                elif ((prev_start == 0 or p[prev_start - 1] == '/') and
                      (pi >= plen or p[pi] == '/' or
                       (p[pi] == '\\' and pi + 1 < plen and p[pi + 1] == '/'))):
                    if (pi < plen and p[pi] == '/' and
                            _dowild(p, pi + 1, t, ti, pathname) == _MATCH):
                        return _MATCH
                    match_slash = True
                else:
                    match_slash = False
            else:
                match_slash = not pathname
            if pi >= plen:
                if not match_slash:
                    if '/' in t[ti:]:
                        return _ABORT_TO_STARSTAR
                return _MATCH
            elif not match_slash and p[pi] == '/':
                slash = t.find('/', ti)
                if slash < 0:
                    return _ABORT_ALL
                ti = slash
                pi += 0
                # continue matching the '/' below
                continue
            while True:
                if ti >= tlen:
                    break
                tch = t[ti]
                if pi < plen and p[pi] not in '*?[\\':
                    # fast skip to the next possible match
                    want = p[pi]
                    while ti < tlen and t[ti] != want:
                        if not match_slash and t[ti] == '/':
                            break
                        ti += 1
                    if ti >= tlen:
                        break
                    if t[ti] != want:
                        # stopped on '/'
                        return _ABORT_TO_STARSTAR
                    tch = t[ti]
                r = _dowild(p, pi, t, ti, pathname)
                if r != _NOMATCH:
                    if not match_slash or r != _ABORT_TO_STARSTAR:
                        return r
                elif not match_slash and tch == '/':
                    return _ABORT_TO_STARSTAR
                ti += 1
            return _ABORT_ALL
        elif pch == '[':
            pi += 1
            if pi >= plen:
                return _ABORT_ALL
            negated = p[pi] in '!^'
            if negated:
                pi += 1
            prev = ''
            matched = False
            first = True
            while True:
                if pi >= plen:
                    return _ABORT_ALL
                pch = p[pi]
                if pch == ']' and not first:
                    break
                first = False
                if pch == '\\':
                    pi += 1
                    if pi >= plen:
                        return _ABORT_ALL
                    if tch == p[pi]:
                        matched = True
                    prev = p[pi]
                elif pch == '-' and prev and pi + 1 < plen and p[pi + 1] != ']':
                    pi += 1
                    hi = p[pi]
                    if hi == '\\':
                        pi += 1
                        if pi >= plen:
                            return _ABORT_ALL
                        hi = p[pi]
                    if prev <= tch <= hi:
                        matched = True
                    prev = ''
                elif pch == '[' and pi + 1 < plen and p[pi + 1] == ':':
                    end = p.find(':]', pi + 2)
                    if end < 0:
                        if tch == '[':
                            matched = True
                        prev = pch
                    else:
                        name = p[pi + 2:end]
                        fn = _CLASSES.get(name)
                        if fn is None:
                            return _ABORT_ALL
                        if tch and fn(tch):
                            matched = True
                        pi = end + 1
                        prev = ''
                else:
                    if tch == pch:
                        matched = True
                    prev = pch
                pi += 1
            if matched == negated or (pathname and tch == '/'):
                return _NOMATCH
        else:
            if tch != pch:
                return _NOMATCH
        pi += 1
        ti += 1
    return _MATCH if ti >= tlen else _NOMATCH


class Pattern:
    __slots__ = ('pattern', 'negative', 'mustbedir', 'nodir', 'base', 'anchored')

    def __init__(self, line, base):
        self.negative = False
        if line.startswith('!'):
            self.negative = True
            line = line[1:]
        self.mustbedir = False
        if line.endswith('/'):
            self.mustbedir = True
            line = line.rstrip('/')
        self.nodir = '/' not in line
        if line.startswith('/'):
            line = line[1:]
        self.anchored = not self.nodir
        self.pattern = line
        self.base = base              # '' or 'dir/sub/' (where the .gitignore lives)

    def matches(self, path, basename, isdir, casefold):
        if self.mustbedir and not isdir:
            return False
        if self.nodir:
            return wildmatch(self.pattern, basename, True, casefold)
        if self.base:
            pre = self.base
            if casefold:
                if not path.lower().startswith(pre.lower()):
                    return False
            elif not path.startswith(pre):
                return False
            rel = path[len(pre):]
        else:
            rel = path
        return wildmatch(self.pattern, rel, True, casefold)


def parse_lines(text, base):
    out = []
    for raw in text.split('\n'):
        line = raw.rstrip('\r')
        if not line or line.startswith('#'):
            continue
        # trailing spaces are dropped unless escaped
        while line.endswith(' ') and not line.endswith('\\ '):
            line = line[:-1]
        if line.endswith('\\ '):
            line = line[:-2] + ' '
        if not line:
            continue
        if line.startswith('\\#') or line.startswith('\\!'):
            line = line[1:]
            p = Pattern('x', base)
            p.negative = False
            body = line
            p.mustbedir = body.endswith('/')
            body = body.rstrip('/')
            p.nodir = '/' not in body
            if body.startswith('/'):
                body = body[1:]
            p.anchored = not p.nodir
            p.pattern = body
            out.append(p)
            continue
        out.append(Pattern(line, base))
    return out


def _read(path):
    try:
        with open(path, 'rb') as f:
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return None


class Ignore:
    """Exclusion for one work tree. Ask is_ignored(path, isdir) for paths whose parent directories
    have been asked about first (a directory that is ignored hides its contents)."""

    def __init__(self, repo, index_blob=None):
        self.repo = repo
        self.root = repo.worktree
        self.casefold = repo.config.bool('core.ignorecase', False)
        self.base_lists = []
        ex = repo.config.get('core.excludesfile')
        if ex is None:
            env = repo.env
            xdg = env.get('XDG_CONFIG_HOME')
            home = env.get('HOME') or env.get('USERPROFILE') or os.path.expanduser('~')
            ex = os.path.join(xdg, 'git', 'ignore') if xdg else os.path.join(home, '.config', 'git', 'ignore')
        else:
            ex = os.path.expanduser(ex)
        t = _read(ex)
        if t:
            self.base_lists.append(parse_lines(t, ''))
        t = _read(os.path.join(repo.commondir, 'info', 'exclude'))
        if t:
            self.base_lists.append(parse_lines(t, ''))
        self._dir_cache = {}
        self.index_blob = index_blob      # f(path) -> bytes or None, for skip-worktree (unused)

    def _dir_patterns(self, d):
        """Patterns of `<d>/.gitignore` (d '' or 'a/b')."""
        got = self._dir_cache.get(d)
        if got is None:
            p = os.path.join(self.root, d.replace('/', os.sep), '.gitignore') if d else os.path.join(self.root, '.gitignore')
            t = _read(p)
            got = parse_lines(t, d + '/' if d else '') if t else []
            self._dir_cache[d] = got
        return got

    def match(self, path, isdir):
        """True (ignored), False (re-included by a negative pattern) or None (no rule) for one
        path, looking at its own rules only (not its parents')."""
        base = path.rsplit('/', 1)[-1]
        parts = path.split('/')
        lists = []
        for i in range(len(parts)):
            d = '/'.join(parts[:i])
            lists.append(self._dir_patterns(d))
        # deepest .gitignore first, then info/exclude, then core.excludesFile
        for plist in reversed(lists):
            for pat in reversed(plist):
                if pat.matches(path, base, isdir, self.casefold):
                    return not pat.negative
        for plist in reversed(self.base_lists):
            for pat in reversed(plist):
                if pat.matches(path, base, isdir, self.casefold):
                    return not pat.negative
        return None

    def is_ignored(self, path, isdir=False):
        """Whether `path` is excluded, its parent directories included."""
        parts = path.split('/')
        for i in range(1, len(parts)):
            if self.match('/'.join(parts[:i]), True):
                return True
        return bool(self.match(path, isdir))
