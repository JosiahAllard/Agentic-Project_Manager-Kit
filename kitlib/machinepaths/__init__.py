r"""Refuse a hard-coded machine path anywhere in the tree (from the PM kit's
tools/machine_paths.py).

Machine paths stay out of the tree's text so the repository and the owner's folders can
move drives without a tree-wide edit: code reads a location from configuration or an environment
variable, and prose writes `%VAR%\...`. This rule keeps a new literal from creeping back. Every
file git tracks (tracked_files; an untracked file is never searched, so a local note beside the
checkout cannot fail it) is read from the WORKING TREE (so an edit in progress counts) and
searched for

  - a drive-rooted machine folder: `<drive>:` then `\`, `\\` or `/`, then one of the folder
    names folders() gives - the machine config's `machine_path_folders` (kitlib.config.user).
    The kit codes no folder name (DEFAULT_FOLDERS is empty): unset, no drive-rooted folder is
    searched and check() prints a NOTE saying so;
  - a posix home: `/home/` + a name, not preceded by a path character (so `src/home/x`,
    `../home/x` and `$HOME/home/x` are not one). The same placeholders pass: a name in angle
    brackets, `$USER`, a `%VAR%`, a `~`; a dot-name (`/home/.config`) is not a user. `/home` alone and `/homepage` are not a home. It
    carries no folder list, and `/root` is not searched: the Windows form treats an admin
    profile as any other name, with no special case;
  - a drive-rooted user profile: `<drive>:` + separator + `Users` + separator + a name, which
    covers every `AppData`, `Documents` and `.claude` path under one. A placeholder name in angle
    brackets or a `%VAR%` is not a name and never matches.

Every spelling of the separator is matched (single and doubled backslash, forward slash).

What a repository allows is `.kit.json` "machine_paths" (load_config, docs/commit.md):
  allow          [{glob, line, hit?, why, temporary?}] - a line kind a literal may stay on;
  fixtures       [{glob, hits, line?, why?}] - a test file's invented paths, exactly those;
  known_binary   {path: reason} - files not searched and not warned about;
  scan           [globs] - only files matching one are searched (default: every file);
  fallback_vars  [names] - a hit on a line that reads one of these variables
                 (`environ.get('NAME'`, `getenv('NAME'`, `$env:NAME`) is the sanctioned fallback.
There is no built-in allow entry: the PM kit's (`_paths.py` fallbacks, the `test_paths.py` table,
kit stubs, backlog.py's ledger) named the older kit's files and one drive, and are not ported.

A hit is refused unless an entry allows it. An entry names its file (a glob; `fnmatch`'s `*`
crosses `/`), the line kind it covers (a regex over the line), optionally the exact path it allows
(a regex the matched text must fullmatch), and why. A configured entry that allowed nothing while
its glob names at least one file read is a WARNING (stale), never a failure.

Not searched: a file listed but gone from the working tree, files outside `scan`, and
`known_binary`. Any OTHER file this cannot search - binary (a NUL in its first 8 KiB), over 4 MiB,
or unreadable - is a WARNING naming it, so a skip is never silent.

A machine config the kit cannot read does not stop the check: user-profile paths are still
searched and a WARNING (naming no value from the file) says so, never NOT CHECKED.

find(text, folders=None) is the same search over one string and reads no configuration: `folders`
names the folders (default none: a user profile only). scan_staged(root, rels)
is the same rule over the STAGED copies (the index) of `rels`, in one `git cat-file --batch`;
kitlib.commit runs it on every file it is about to commit. It fails closed: an index git cannot
read, or a named path with no staged blob, is `(None, why)`.

check(root) is the whole tree sweep as `(code, lines)`: 0 clean (WARNING lines included), 1 a hit
outside the allow-list, 2 git could not list the tree, nothing was read, or the `.kit.json`
configuration was refused. Nothing here prints or exits.
"""
import collections
import fnmatch
import os
import re

from kitlib import config as kconfig
from kitlib.config import user as kuser
from kitlib.git import batch, cmd

SEP = r'(?:\\\\|\\|/)+'
DEFAULT_FOLDERS = ()
_PATTERNS = {}


def pattern(folders):
    """The compiled machine-path pattern for the drive-rooted folder names `folders` (a user
    profile is always one); cached per folder list."""
    key = tuple(folders)
    got = _PATTERNS.get(key)
    if got is None:
        # (?![A-Za-z0-9_]), not \b: a name ending in a symbol (`C#`) has no word boundary after it.
        named = r'(?:%s)(?![A-Za-z0-9_])|' % '|'.join(re.escape(f) for f in key) if key else ''
        got = _PATTERNS[key] = re.compile(
            r'(?<![A-Za-z0-9])[A-Za-z]:' + SEP +
            r'(?:' + named + r'Users' + SEP + r'[^\\/\s\'"`<>%$*]+)',
            re.IGNORECASE)
    return got


MACHINE_PATH = pattern(DEFAULT_FOLDERS)
# A posix home directory: /home/<name>. The lookbehind refuses a path that merely contains
# `home` (src/home/x, ../home/x, $HOME/home/x); the name class refuses the placeholders the
# docs write (<user>, $USER, %USER%, {user}) as the Windows form does; a name starts with a
# letter, digit or underscore, so `/home/.config` (a file under a home) is not a user.
POSIX_HOME = re.compile(r'(?<![A-Za-z0-9_.~:/\$%-])/home/[A-Za-z0-9_][^\/\s\'"`<>%$*{}]*')
MAX_BYTES = 4 * 1024 * 1024
SECTION = 'machine_paths'
GIT_TIMEOUT = 60

OK, FAILED, NOT_CHECKED = 0, 1, 2

Allow = collections.namedtuple('Allow', 'glob line_re hit_re reason temporary')
# folders: the drive-rooted folder names searched (None: DEFAULT_FOLDERS); note: the WARNING line
# when the machine config could not be read, else None (check() adds the NOTE for an unset list).
Config = collections.namedtuple('Config', 'allow known_binary scan fallback folders note',
                                defaults=(None, None))

FIXTURE = 'test fixture: an invented path the test feeds in, not a path on this machine'
VAR_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
ConfigError = kconfig.ConfigError


def _fixture(glob, *hits, line_re=r'.', why=''):
    """A test file's invented paths: exactly these, nothing else in the file."""
    return Allow(glob, line_re, '|'.join('(?:%s)' % h for h in hits), FIXTURE + why, False)


def from_section(data, where='.kit.json "%s"' % SECTION):
    """`Config` from a checked "machine_paths" section over its defaults (kitlib.config already
    refused unknown keys and wrong types). Raises ConfigError on what the schema cannot say."""
    allow = []
    for i, e in enumerate(data.get('allow') or []):
        w = '%s.allow[%d]' % (where, i)
        if not (e.get('why') or '').strip():
            raise ConfigError('%s needs a "why": the whole record of why the literal stays' % w)
        if not e.get('glob'):
            raise ConfigError('%s needs a "glob"' % w)
        if not e.get('line'):
            raise ConfigError('%s needs a "line": the regex of the line kind it allows' % w)
        allow.append(Allow(e['glob'], e['line'], e.get('hit') or None, e['why'],
                           bool(e.get('temporary', False))))
    for i, e in enumerate(data.get('fixtures') or []):
        w = '%s.fixtures[%d]' % (where, i)
        if not e.get('glob'):
            raise ConfigError('%s needs a "glob"' % w)
        hits = e.get('hits') or []
        if not hits or not all(hits):
            raise ConfigError('%s.hits must be a list of non-empty regex strings' % w)
        allow.append(_fixture(e['glob'], *hits, line_re=e.get('line') or '.', why=e.get('why') or ''))
    kb = data.get('known_binary') or {}
    if not all(isinstance(v, str) and v.strip() for v in kb.values()):
        raise ConfigError('%s.known_binary must map each path to its reason' % where)
    scan_globs = data.get('scan')
    if not scan_globs or not all(scan_globs):
        raise ConfigError('%s.scan must name at least one glob (omit it to search every file)'
                          % where)
    names = data.get('fallback_vars') or []
    for n in names:
        if not VAR_NAME.match(n):
            raise ConfigError('%s.fallback_vars: %r is not a variable name' % (where, n))
    fallback = None
    if names:
        alt = '|'.join(re.escape(n) for n in names)
        fallback = re.compile(r'''(?:environ\.get\(\s*['"]|getenv\(\s*['"]|\$env:)(?:%s)\b''' % alt,
                              re.IGNORECASE)
    return Config(tuple(allow), dict(kb), tuple(scan_globs), fallback)


def folders(home=None):
    """(folder names, note): the machine config's `machine_path_folders` (unset: none) and None;
    or, when the machine config cannot be read, no folder names and a WARNING line that names no
    value from the file. HomeFenced is a BaseException and is never caught here."""
    try:
        return kuser.machine_path_folders(home), None
    except ConfigError:
        return DEFAULT_FOLDERS, ('WARNING: machine config %s could not be read, so only '
                                 'user-profile paths were searched - run tools/preflight.py to see '
                                 'what it refuses' % kuser.display(home))


def load_config(root, home=None):
    """The repository's allow-list and scan settings (`Config`) from the working tree's
    `.kit.json` "machine_paths", with the folder names of the machine config under `home`
    (default the user's; folders()). Raises ConfigError on a `.kit.json` it cannot use."""
    cfg = from_section(kconfig.load(root).section(SECTION))
    names, note = folders(home)
    return cfg._replace(folders=names, note=note)


class ListError(Exception):
    """git could not list the tree; the message is git's exit code and first stderr line (or the
    timeout, or why git could not start)."""


def tracked_files(root):
    """Every file git tracks (the index), sorted, from one `ls-files --cached`: the files the
    tree sweep searches. ListError when git cannot list them."""
    return _listed(root, ['ls-files', '-z', '--cached'])


def tree_files(root):
    """Every tracked file plus every untracked one not ignored, sorted, from one `ls-files -co`
    (`--exclude-standard` filters only the untracked half, so a tracked file a .gitignore pattern
    matches is still listed); ListError when git cannot list them. The tree sweep does not use
    it (tracked_files); the guards in tests/proc do."""
    return _listed(root, ['ls-files', '-z', '--cached', '--others', '--exclude-standard'])


def _listed(root, args):
    """The sorted, de-duplicated paths `git <args>` prints NUL-separated; ListError on failure."""
    r = cmd.call(args, root, timeout=GIT_TIMEOUT)
    shown = 'git %s' % ' '.join(args)
    if r.rc is None:
        raise ListError('`%s`: %s' % (shown, r.err))
    if r.rc != 0:
        first = r.err.splitlines()[0] if r.err else '(nothing on stderr)'
        raise ListError('`%s` exit %d: %s' % (shown, r.rc, first))
    return sorted(set(p.decode('utf-8', 'replace') for p in r.out.split(b'\0') if p))


def allowed(allow, rel, line, hit):
    """The index of the `allow` entry covering this hit on this line, or None."""
    for i, a in enumerate(allow):
        if fnmatch.fnmatchcase(rel, a.glob) and re.search(a.line_re, line) and \
                (a.hit_re is None or re.fullmatch(a.hit_re, hit)):
            return i
    return None


def find(text, folders=None):
    """Every machine path in `text`, as matched strings, for the drive-rooted folder names
    `folders` (default none: user-profile paths only). Reads no configuration."""
    rx = MACHINE_PATH if folders is None else pattern(folders)
    return [m.group(0) for r in (rx, POSIX_HOME) for m in r.finditer(text or '')]


def searched(cfg, rel):
    """True when `rel` is one this rule searches: not known_binary, and inside `scan`."""
    return rel not in cfg.known_binary and any(fnmatch.fnmatchcase(rel, g) for g in cfg.scan)


def check_data(cfg, rel, data, hits, used, read, skipped):
    """The rule for ONE file's bytes, shared by scan and scan_staged so the two never drift: a
    binary file is skipped, every hit off the allow-list is appended to `hits` as (rel, line
    number, text), every allow-list entry used goes into `used`."""
    if b'\0' in data[:8192]:
        skipped.append((rel, 'binary (a NUL in its first 8 KiB)'))
        return
    read.append(rel)
    rx = MACHINE_PATH if cfg.folders is None else pattern(cfg.folders)
    for n, line in enumerate(data.decode('utf-8', 'replace').splitlines(), 1):
        for m in [*rx.finditer(line), *POSIX_HOME.finditer(line)]:
            if cfg.fallback is not None and cfg.fallback.search(line):
                continue
            i = allowed(cfg.allow, rel, line, m.group(0))
            if i is None:
                hits.append((rel, n, m.group(0)))
            else:
                used.add(i)


def scan(root, cfg=None):
    """(hits, used allow indices, files read, skipped [(rel, why)]) over the working tree;
    ListError when git could not list it. `cfg` defaults to load_config(root)."""
    cfg = cfg or load_config(root)
    hits, used, read, skipped = [], set(), [], []
    for rel in tracked_files(root):
        if not searched(cfg, rel):
            continue
        path = os.path.join(root, rel)
        if not os.path.lexists(path):
            continue                                 # listed, but deleted in the worktree
        try:
            size = os.path.getsize(path)
            if size > MAX_BYTES:
                skipped.append((rel, 'over %d bytes (%d)' % (MAX_BYTES, size)))
                continue
            with open(path, 'rb') as fh:
                data = fh.read()
        except OSError as e:
            skipped.append((rel, 'unreadable: %s' % e.strerror))
            continue
        check_data(cfg, rel, data, hits, used, read, skipped)
    return hits, used, read, skipped


def scan_staged(root, rels, cfg=None):
    """scan()'s answer for the STAGED copies (`:<rel>`) of `rels`: `(hits, used, read, skipped)`,
    or `(None, why)` when git could not answer or a path searched has no staged blob (fail-closed;
    the caller leaves out a staged deletion). `cfg` defaults to load_config(root)."""
    cfg = cfg or load_config(root)
    rels = [r for r in rels if searched(cfg, r)]
    got, why = batch.cat_file([':' + r for r in rels], root, timeout=GIT_TIMEOUT)
    if got is None:
        return None, why
    missing = [r for r in rels if got.get(':' + r) is None or got[':' + r][0] != 'blob']
    if missing:
        return None, 'no staged blob for %s' % ', '.join(missing)
    hits, used, read, skipped = [], set(), [], []
    for rel in rels:
        data = got[':' + rel][1]
        if len(data) > MAX_BYTES:
            skipped.append((rel, 'over %d bytes (%d)' % (MAX_BYTES, len(data))))
            continue
        check_data(cfg, rel, data, hits, used, read, skipped)
    return hits, used, read, skipped


def hit_lines(hits, n_read):
    """The failure text for `hits` (empty for none): one line per hit, then the verdict line.
    The tree sweep and kitlib.commit both use exactly these."""
    if not hits:
        return []
    return ['%s:%d: hard-coded machine path %s' % h for h in hits] + [
        'MACHINE PATHS FAILED: %d hit(s) in %d file(s) read. Read the location from configuration '
        'or an environment variable, write `%%VAR%%\\...` in prose, or - only for a '
        'path an owner decision rests on - allow-list it in .kit.json "machine_paths" with the '
        'reason.' % (len(hits), n_read)]


def skipped_lines(skipped):
    return ['WARNING: %s was not searched: %s - search it another way, or name it in '
            '.kit.json "machine_paths".known_binary with the reason' % s for s in skipped]


def stale_lines(cfg, used, read):
    """A WARNING per configured entry that allowed nothing while its glob names a file read."""
    out = []
    for i, e in enumerate(cfg.allow):
        if i not in used and any(fnmatch.fnmatchcase(r, e.glob) for r in read):
            out.append('WARNING: allow-list entry %s /%s/%s matched nothing%s - remove it from '
                       '.kit.json "machine_paths"' % (e.glob, e.line_re,
                                                      ' hit /%s/' % e.hit_re if e.hit_re else '',
                                                      ' (it was temporary)' if e.temporary else ''))
    return out


UNSET_NOTE = ('NOTE: the machine config sets no "machine_path_folders", so only drive-rooted '
              'user-profile paths were searched - name the folders of this machine there '
              '(docs/commit.md "machine_paths.py")')


def note_lines(cfg):
    """The config's WARNING when the machine config could not be read; else UNSET_NOTE when it
    names no folder; else nothing."""
    if cfg.note:
        return [cfg.note]
    return [] if cfg.folders else [UNSET_NOTE]


def check(root):
    """The tree sweep: `(code, lines)` with code OK, FAILED or NOT_CHECKED (module docstring)."""
    try:
        cfg = load_config(root)
    except ConfigError as e:
        return NOT_CHECKED, ['MACHINE PATHS NOT CHECKED: the .kit.json "machine_paths" config is '
                             'refused: %s' % e]
    try:
        hits, used, read, skipped = scan(root, cfg)
    except ListError as e:
        return NOT_CHECKED, ['MACHINE PATHS NOT CHECKED: git could not list the tree at %s: %s'
                             % (root, e)]
    if not read:
        return NOT_CHECKED, ['MACHINE PATHS NOT CHECKED: no file was read under %s' % root]
    lines = note_lines(cfg) + skipped_lines(skipped) + \
        stale_lines(cfg, used, read) + hit_lines(hits, len(read))
    if hits:
        return FAILED, lines
    return OK, lines + ['MACHINE PATHS OK (%d file(s) read, %d allow-listed line kind(s) in use, '
                        '%d not searched)' % (len(read), len(used), len(skipped))]
