"""git config files: read (every layer git reads) and edit (git's own line-preserving rules).

Layers, lowest first: system (unless GIT_CONFIG_NOSYSTEM; GIT_CONFIG_SYSTEM or Git for Windows'
<prefix>/etc/gitconfig, after %PROGRAMDATA%/Git/config), global (GIT_CONFIG_GLOBAL, else
$XDG_CONFIG_HOME/git/config or ~/.config/git/config, then ~/.gitconfig), the repository's config,
this worktree's config.worktree when the repository config sets extensions.worktreeConfig
(kitlib.git.fake.repo.Repo.config), GIT_CONFIG_COUNT/KEY_n/VALUE_n, then `-c`. An include or
includeIf is Unsupported, and so is writing config.worktree (`git config --worktree`). Editing keeps every other line as written, as git does: a new key goes after the
last key of the last matching section, a removed key that empties its section takes the header
with it, and a value is quoted and escaped as git writes it.
"""
import os
import re
import shutil
import threading

from .errors import GitExit, Unsupported, die

_CACHE = {}
_CACHE_LOCK = threading.Lock()


class Entry:
    __slots__ = ('section', 'sub', 'name', 'value', 'start', 'end', 'block')

    def __init__(self, section, sub, name, value, start, end, block):
        self.section, self.sub, self.name, self.value = section, sub, name, value
        self.start, self.end, self.block = start, end, block

    @property
    def key(self):
        return canon(self.section, self.sub, self.name)


def canon(section, sub, name):
    return section.lower() + ('.' + sub if sub is not None else '') + '.' + name.lower()


def split_key(key):
    """'a.b.c' -> ('a', 'b', 'c'); 'a.c' -> ('a', None, 'c'). Raises GitExit on a bad key."""
    first = key.find('.')
    last = key.rfind('.')
    if first <= 0 or last == len(key) - 1:
        raise GitExit(1, 'error: key does not contain a section: %s\n' % key if first < 0
                      else 'error: invalid key: %s\n' % key)
    section = key[:first]
    name = key[last + 1:]
    sub = key[first + 1:last] if last > first else None
    if not re.match(r'^[A-Za-z0-9-]+$', section) or not re.match(r'^[A-Za-z][A-Za-z0-9-]*$', name):
        raise GitExit(1, 'error: invalid key: %s\n' % key)
    return section, sub, name


def canon_key(key):
    s, sub, n = split_key(key)
    return canon(s, sub, n)


class Block:
    """A section header and the extent of its body: [hstart, hend) header, body to `end`."""
    __slots__ = ('section', 'sub', 'hstart', 'hend', 'end')

    def __init__(self, section, sub, hstart, hend):
        self.section, self.sub, self.hstart, self.hend, self.end = section, sub, hstart, hend, hend


def parse(text, origin='config'):
    """(entries, blocks) of a config file's text."""
    entries, blocks = [], []
    i, n = 0, len(text)
    block = None
    line_no = 1

    def bad():
        raise die('bad config line %d in file %s' % (line_no, origin))

    while i < n:
        c = text[i]
        if c == '\n':
            line_no += 1
            i += 1
            continue
        if c in ' \t\r':
            i += 1
            continue
        if c in '#;':
            j = text.find('\n', i)
            i = n if j < 0 else j
            continue
        if text.startswith('\ufeff', i) and i == 0:
            i += 1
            continue
        line_start = text.rfind('\n', 0, i) + 1
        if c == '[':
            j = i + 1
            name = []
            while j < n and (text[j].isalnum() or text[j] in '-.'):
                name.append(text[j])
                j += 1
            name = ''.join(name)
            sub = None
            if j < n and text[j] in ' \t':
                while j < n and text[j] in ' \t':
                    j += 1
                if j >= n or text[j] != '"':
                    bad()
                j += 1
                buf = []
                while j < n and text[j] != '"':
                    if text[j] == '\n':
                        bad()
                    if text[j] == '\\':
                        j += 1
                        if j >= n or text[j] == '\n':
                            bad()
                    buf.append(text[j])
                    j += 1
                if j >= n:
                    bad()
                j += 1
                sub = ''.join(buf)
                if '.' in name:
                    bad()
            elif '.' in name:
                name, _, sub = name.partition('.')
                sub = sub.lower()
            if j >= n or text[j] != ']' or not name:
                bad()
            j += 1
            if block is not None:
                block.end = line_start
            block = Block(name, sub, line_start, j)
            blocks.append(block)
            i = j
            continue
        if not c.isalpha():
            bad()
        if block is None:
            bad()
        j = i
        while j < n and (text[j].isalnum() or text[j] == '-'):
            j += 1
        name = text[i:j]
        while j < n and text[j] in ' \t':
            j += 1
        value = None
        if j < n and text[j] == '=':
            j += 1
            value, j, extra_lines = _value(text, j, bad)
            line_no += extra_lines
        elif j < n and text[j] not in '\r\n#;':
            bad()
        else:
            while j < n and text[j] not in '\n':
                if text[j] in '#;':
                    k = text.find('\n', j)
                    j = n if k < 0 else k
                    break
                j += 1
        end = j + 1 if j < n else n
        entries.append(Entry(block.section, block.sub, name, value, line_start, end, block))
        block.end = end
        i = end
        if end <= n and end > 0 and text[end - 1] == '\n':
            line_no += 1
    if block is not None:
        block.end = n
    return entries, blocks


_ESCAPES = {'t': '\t', 'b': '\b', 'n': '\n', '\\': '\\', '"': '"'}


def _value(text, j, bad):
    """(value, index of the line end, continuation lines) for a value starting at j: git's
    parse_value (leading blanks skipped, trailing blanks outside quotes trimmed)."""
    out = []
    quote = comment = False
    trim = None
    extra = 0
    n = len(text)

    def next_char(j):
        if j >= n:
            return '\n', j
        c = text[j]
        if c == '\r' and j + 1 < n and text[j + 1] == '\n':
            return '\n', j + 1
        return c, j

    while True:
        c, j = next_char(j)
        if c == '\n':
            if quote:
                bad()
            if trim is not None:
                del out[trim:]
            return ''.join(out), j, extra
        j += 1
        if comment:
            continue
        if c in ' \t\v\f\r' and not quote:
            if trim is None:
                trim = len(out)
            if out:
                out.append(c)
            continue
        if not quote and c in ';#':
            comment = True
            continue
        trim = None
        if c == '\\':
            e, j = next_char(j)
            j += 1
            if e == '\n':
                extra += 1
                continue
            mapped = _ESCAPES.get(e)
            if mapped is None:
                bad()
            out.append(mapped)
            continue
        if c == '"':
            quote = not quote
            continue
        out.append(c)


def quote_value(value):
    out = value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') \
        .replace('\t', '\\t').replace('\b', '\\b')
    if value[:1] in (' ', '\t') or value[-1:] in (' ', '\t') or ';' in value or '#' in value:
        out = '"%s"' % out
    return out


def _read_text(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    # st_ino (the file id on Windows): on NTFS a replace gives the path a new one, so a same-size
    # rewrite in the same clock tick still misses. That is filesystem-dependent: where the id is
    # reused or reported as 0 the key degrades to mtime+size, and the eviction in _write is the
    # guard for the fake's own rewrites (tests/git/test_fake_cache.py).
    key = (os.path.normcase(os.path.abspath(path)), st.st_mtime_ns, st.st_size, st.st_ino)
    with _CACHE_LOCK:
        got = _CACHE.get(key)
    if got is not None:
        return got
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError:
        return None
    text = raw.decode('utf-8', 'replace')
    with _CACHE_LOCK:
        if len(_CACHE) > 4000:
            _CACHE.clear()
        _CACHE[key] = text
    return text


def read_file(path):
    """[(key, value)] of one file, or [] when it does not exist."""
    text = _read_text(path)
    if text is None:
        return []
    entries, _ = parse(text, path.replace('\\', '/'))
    out = []
    for e in entries:
        k = e.key
        if k == 'include.path' or k.startswith('includeif.'):
            raise Unsupported('config include in %s' % path)
        out.append((k, e.value))
    return out


_GIT_PREFIX = None


def _git_prefix():
    global _GIT_PREFIX
    if _GIT_PREFIX is None:
        found = shutil.which('git')
        prefix = ''
        if found:
            d = os.path.dirname(os.path.abspath(found))
            # <prefix>/cmd/git.exe, <prefix>/bin/git.exe or <prefix>/mingw64/bin/git.exe
            for up in (d, os.path.dirname(d), os.path.dirname(os.path.dirname(d))):
                if os.path.isfile(os.path.join(up, 'etc', 'gitconfig')):
                    prefix = up
                    break
        _GIT_PREFIX = prefix
    return _GIT_PREFIX


def system_files(env):
    if _truthy(env.get('GIT_CONFIG_NOSYSTEM')):
        return []
    if env.get('GIT_CONFIG_SYSTEM') is not None:
        return [env['GIT_CONFIG_SYSTEM']]
    out = []
    if env.get('PROGRAMDATA'):
        out.append(os.path.join(env['PROGRAMDATA'], 'Git', 'config'))
    prefix = _git_prefix()
    if prefix:
        out.append(os.path.join(prefix, 'etc', 'gitconfig'))
    return out


def home_of(env):
    """The home folder of a git run with the environment `env`: its HOME, else its USERPROFILE,
    else the process's own through kitlib.config.user.home_dir(), which raises HomeFenced under
    the kit's tests when that is a real home (docs/test-tools.md "Home fence"). A home the
    environment names is taken as given. The import is here, not at the top: kitlib.config
    imports kitlib.git."""
    home = env.get('HOME') or env.get('USERPROFILE')
    if home:
        return home
    from kitlib.config import user
    return user.home_dir()


def expand_user(path, env, key):
    """The config value `path` of `key` with a leading `~` read as home_of(env): `~`, `~/x` and
    `~\\x`, joined as os.path.expanduser joins them. Any other path is returned unchanged, and no
    home is looked up for it. `~name` (another user's home) is Unsupported: the fake knows no
    users."""
    if not path.startswith('~'):
        return path
    if len(path) > 1 and path[1] not in '/\\':
        raise Unsupported('%s naming a user\'s home (%s)' % (key, path))
    return home_of(env) + path[1:]


def global_files(env):
    if env.get('GIT_CONFIG_GLOBAL') is not None:
        return [env['GIT_CONFIG_GLOBAL']]
    home = home_of(env)
    xdg = env.get('XDG_CONFIG_HOME')
    first = os.path.join(xdg, 'git', 'config') if xdg else os.path.join(home, '.config', 'git', 'config')
    return [first, os.path.join(home, '.gitconfig')]


def _truthy(v):
    return v is not None and v.strip().lower() in ('1', 'true', 'yes', 'on')


def env_pairs(env):
    out = []
    if env.get('GIT_CONFIG_PARAMETERS'):
        raise Unsupported('GIT_CONFIG_PARAMETERS in the environment')
    if env.get('GIT_CONFIG') is not None:
        raise Unsupported('GIT_CONFIG in the environment')
    raw = env.get('GIT_CONFIG_COUNT')
    if raw:
        try:
            count = int(raw)
        except ValueError:
            raise die('bogus count in GIT_CONFIG_COUNT')
        for i in range(count):
            k = env.get('GIT_CONFIG_KEY_%d' % i)
            v = env.get('GIT_CONFIG_VALUE_%d' % i)
            if k is None:
                raise die('missing config key GIT_CONFIG_KEY_%d' % i)
            if v is None:
                raise die('missing config value GIT_CONFIG_VALUE_%d' % i)
            out.append((canon_key(k), v))
    return out


def parse_cli(item):
    """`-c key[=value]` -> (canonical key, value or None)."""
    k, eq, v = item.partition('=')
    try:
        return canon_key(k), (v if eq else None)
    except GitExit:
        raise die("bogus config parameter: %s" % item)


class Config:
    """The merged view: `layers` is [(scope, [(key, value)])], lowest first."""

    def __init__(self, layers):
        self.layers = layers
        self._map = {}
        for _scope, pairs in layers:
            for k, v in pairs:
                self._map.setdefault(k, []).append(v)

    def get(self, key, default=None):
        vals = self._map.get(key)
        if not vals:
            return default
        return vals[-1]

    def get_all(self, key):
        return list(self._map.get(key, ()))

    def has(self, key):
        return key in self._map

    def items(self):
        for _scope, pairs in self.layers:
            for k, v in pairs:
                yield k, v

    def bool(self, key, default=False):
        if key not in self._map:
            return default
        return parse_bool(self._map[key][-1], key)

    def int(self, key, default=0):
        v = self.get(key)
        if v is None:
            return default
        return parse_int(v, key)


def parse_bool(v, key=''):
    if v is None:
        return True
    s = v.strip().lower()
    if s in ('true', 'yes', 'on'):
        return True
    if s in ('false', 'no', 'off', ''):
        return False
    try:
        return parse_int(v, key) != 0
    except GitExit:
        raise die("bad boolean config value '%s' for '%s'" % (v, key))


def parse_int(v, key=''):
    m = re.match(r'^\s*(-?\d+)\s*([kmgKMG]?)\s*$', v or '')
    if not m:
        raise die("bad numeric config value '%s' for '%s': invalid unit" % (v, key))
    mult = {'': 1, 'k': 1024, 'm': 1024 ** 2, 'g': 1024 ** 3}[m.group(2).lower()]
    return int(m.group(1)) * mult


# -- editing ---------------------------------------------------------------------------------

def _load_for_edit(path):
    try:
        with open(path, 'rb') as f:
            text = f.read().decode('utf-8')
    except FileNotFoundError:
        text = ''
    entries, blocks = parse(text, path.replace('\\', '/'))
    return text, entries, blocks


def _write(path, text):
    lock = path + '.lock'
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o666)
    except FileExistsError:
        raise GitExit(255, 'error: could not lock config file %s: File exists\n' % path.replace('\\', '/'))
    try:
        os.write(fd, text.encode('utf-8'))
    finally:
        os.close(fd)
    os.replace(lock, path)
    norm = os.path.normcase(os.path.abspath(path))
    with _CACHE_LOCK:
        for k in [k for k in _CACHE if k[0] == norm]:
            del _CACHE[k]


def _section_header(section, sub):
    if sub is None:
        return '[%s]\n' % section
    return '[%s "%s"]\n' % (section, sub.replace('\\', '\\\\').replace('"', '\\"'))


def _matches(entries, key, value_re=None):
    out = []
    for e in entries:
        if e.key != key:
            continue
        if value_re is not None:
            neg = value_re.startswith('!')
            pat = value_re[1:] if neg else value_re
            hit = re.search(pat, e.value or '') is not None
            if hit == neg:
                continue
        out.append(e)
    return out


def set_value(path, key, value, add=False, replace_all=False, value_re=None):
    """git config [--add|--replace-all] key value [value-regex] in file `path`."""
    section, sub, name = split_key(key)
    ckey = canon(section, sub, name)
    text, entries, blocks = _load_for_edit(path)
    line = '\t%s = %s\n' % (name, quote_value(value))
    found = [] if add else _matches(entries, ckey, value_re)
    if len(found) > 1 and not replace_all:
        raise GitExit(5, 'warning: %s has multiple values\nerror: cannot overwrite multiple values '
                         'with a single value\n       Use a regexp, --add or --replace-all to '
                         'change %s.\n' % (key, key))
    if found:
        # replace the last, drop the others (replace_all)
        out, pos = [], 0
        for e in found:
            out.append(text[pos:e.start])
            if e is found[-1]:
                out.append(line)
            pos = e.end
            if e.end > 0 and text[e.end - 1] != '\n' and e is found[-1]:
                pass
        out.append(text[pos:])
        _write(path, ''.join(out))
        return
    same = [e for e in entries if e.key == ckey]
    target_blocks = [b for b in blocks if b.section.lower() == section.lower() and b.sub == sub]
    if same:
        at = same[-1].end
    elif target_blocks:
        b = target_blocks[-1]
        body = [e for e in entries if e.block is b]
        at = body[-1].end if body else _line_end(text, b.hend)
    else:
        if text and not text.endswith('\n'):
            text += '\n'
        _write(path, text + _section_header(section, sub) + line)
        return
    if at > 0 and text[at - 1:at] != '\n':
        line = '\n' + line
    _write(path, text[:at] + line + text[at:])


def _line_end(text, i):
    j = text.find('\n', i)
    return len(text) if j < 0 else j + 1


def unset(path, key, all_=False, value_re=None):
    section, sub, name = split_key(key)
    ckey = canon(section, sub, name)
    text, entries, blocks = _load_for_edit(path)
    found = _matches(entries, ckey, value_re)
    if not found:
        raise GitExit(5, '')
    if len(found) > 1 and not all_:
        raise GitExit(5, 'warning: %s has multiple values\n' % key)
    _remove_entries(path, text, entries, blocks, found)


def _remove_entries(path, text, entries, blocks, gone):
    gone_ids = set(id(e) for e in gone)
    cuts = [(e.start, e.end) for e in gone]
    for b in blocks:
        body = [e for e in entries if e.block is b]
        if body and all(id(e) in gone_ids for e in body):
            # The section is now empty: its header goes too when nothing else is in it.
            rest = text[_line_end(text, b.hend):b.end]
            left = rest
            for e in body:
                left = left.replace(text[e.start:e.end], '', 1)
            if not left.strip() and not text[b.hend:_line_end(text, b.hend)].strip():
                cuts.append((b.hstart, _line_end(text, b.hend)))
    cuts.sort()
    out, pos = [], 0
    for s, e in cuts:
        if s < pos:
            s = pos
        out.append(text[pos:s])
        pos = max(pos, e)
    out.append(text[pos:])
    _write(path, ''.join(out))


def rename_section(path, section, sub, new_sub):
    """Rename `[section "sub"]` to `new_sub`, or remove it when new_sub is None. False when there
    was no such section."""
    text, entries, blocks = _load_for_edit(path)
    hits = [b for b in blocks if b.section.lower() == section.lower() and b.sub == sub]
    if not hits:
        return False
    out, pos = [], 0
    for b in hits:
        out.append(text[pos:b.hstart])
        if new_sub is None:
            pos = b.end
        else:
            out.append(_section_header(b.section, new_sub).rstrip('\n'))
            pos = b.hend
    out.append(text[pos:])
    _write(path, ''.join(out))
    return True


def file_entries(path):
    text = _read_text(path)
    if text is None:
        return []
    entries, _ = parse(text, path.replace('\\', '/'))
    return entries
