"""Refuse a forbidden token anywhere in the tree, without writing it down (docs/repo-checks.md).

Ported from the PM kit's tools/forbidden.py. Some repositories must never contain a word - a
former product name, a client's name, a term the owner ruled out - while the history still
carries it. A grep-based checker would have to contain the string it forbids, so this holds
salted SHA-256 digests in a token store and hashes every window of the scanned text instead. The
plaintext is never in the tree, never in this package and never in a result line: a hit names
the file and line and stops there, because a checker that printed the match would put the token
back into build logs.

WHERE THE STORE LIVES: `<tools_dir>/forbidden-tokens.json` (`.kit.json` "repo".tools_dir), unless
`.kit.json` "forbidden" names a repo-relative file (`"forbidden": "ops/tokens.json"`). The kit
config reads a named file when it loads `.kit.json` and refuses one that does not exist, so a
named store is created holding `{}` before the first `add`. An inline object refuses: the store
is written by `add`, never by hand.

WHAT THIS IS NOT. The salt sits beside the digests, so this does not hide a token from anyone who
sets out to recover it; a short word falls to a dictionary in seconds. The threat is an agent
reintroducing the word by accident. This makes the rule checkable by someone who does not know
the word; it does not make the word a secret.

A DISARMED STORE NEVER REPORTS A CLEAN TREE. The store carries the digest of CANARY (plaintext
here precisely because it is not a token), and a run that cannot find CANARY in a string it just
built refuses (exit 2). Each token record carries its own window length, and a salted checksum
over the records means an edit to one of them refuses the same way (`self_test`).

NORMALISATION. Both forms lowercase and drop punctuation, so `Xy-zzz`, `xy.zzz`, `XY_ZZZ` and
`path/xyzzz/name` are one string. The default keeps spaces (one per run of whitespace): the form
an accidental reintroduction takes. `paranoid` also removes spaces, which catches a token spelled
with gaps at the cost of false positives (neighbouring words weld together), so a gate runs the
default form only.

Exit codes (`Outcome.code`): 0 clean, 1 a hit, 2 the store is missing, disarmed or unreadable,
nothing was read, git failed, or the configuration was refused.
"""
import collections
import hashlib
import json
import os

from kitlib.config import ConfigError, load as load_config
from kitlib.git import cmd as gitcmd
from kitlib.git import refs as gitrefs

# The known-positive. Plaintext on purpose: it is not a token, and a self-test whose subject
# nobody can read is a self-test nobody can check.
CANARY = 'vorpalquinceaster'
STORE_NAME = 'forbidden-tokens.json'
SECTION = 'forbidden'
MIN_TOKEN = 4             # a shorter token would fire on ordinary text
# Files whose bytes are not text worth scanning; a NUL byte is the general test.
BINARY_EXT = ('.jar', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.zip', '.gz', '.class',
              '.pdf', '.dll', '.exe', '.so', '.dylib', '.wav', '.ogg', '.mp3', '.bin')
MAX_BYTES = 8 * 1024 * 1024
GIT_TIMEOUT = 120

OK, HIT, REFUSED = 0, 1, 2
Outcome = collections.namedtuple('Outcome', 'code lines')


class ForbiddenError(Exception):
    """The run cannot answer: the store or git failed, or the input is unusable (exit 2)."""


# ---- the store ---------------------------------------------------------------------------------

def store_path(root, cfg=None):
    """Absolute path of the repository's token store (module doc). ConfigError when `.kit.json`
    holds the store inline or names a path the kit config refuses."""
    cfg = cfg if cfg is not None else load_config(root)
    if not cfg.has(SECTION):
        return os.path.join(root, cfg.tools_dir, STORE_NAME)
    rel = cfg.section_file(SECTION)
    if not rel:
        raise ConfigError('.kit.json "%s" must name the token store, a repo-relative file (e.g. '
                          '"tools/%s"), not hold it inline' % (SECTION, STORE_NAME))
    return os.path.join(root, *rel.split('/'))


def load_store(path):
    """The store as a dict, or None when the file does not exist. ForbiddenError when it cannot
    be read or is not a JSON object."""
    try:
        with open(path, encoding='utf-8-sig') as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ForbiddenError('cannot read the token store %s: %s' % (path, e))
    if not isinstance(data, dict):
        raise ForbiddenError('the token store %s does not hold a JSON object' % path)
    return data


def normalise(text, drop_spaces):
    """`(normalised, index)`: lowercased alphanumerics (plus one space per whitespace run unless
    `drop_spaces`), and the original offset of each character kept."""
    out = []
    index = []
    for i, ch in enumerate(text):
        if ch.isalnum():
            out.append(ch.lower())
            index.append(i)
        elif not drop_spaces and ch.isspace():
            # A token split across a line break is the same string as one split by a space.
            if out and out[-1] != ' ':
                out.append(' ')
                index.append(i)
    return ''.join(out), index


def digest(salt_bytes, window):
    return hashlib.sha256(salt_bytes + window.encode('utf-8')).hexdigest()


def hits_in(text, store, drop_spaces):
    """Sorted original-text offsets where a window hashes to one of the store's digests."""
    salt_bytes = bytes.fromhex(store['salt'])
    by_length = {}
    for t in store['tokens']:
        by_length.setdefault(int(t['length']), set()).add(t['digest'])
    norm, index = normalise(text, drop_spaces)
    found = []
    for n, wanted in sorted(by_length.items()):
        for start in range(0, len(norm) - n + 1):
            if digest(salt_bytes, norm[start:start + n]) in wanted:
                found.append(index[start])
    return sorted(set(found))


def tokens_checksum(store):
    """A salted digest over the token records themselves.

    The canary proves the machinery works; it cannot prove a real token's record is intact,
    because it is a separate record. This notices an edited `length` or `digest` (by hand, or a
    merge resolving the store badly). Tamper-evidence against accidents, not tamper-proofing:
    anyone with the tool can recompute it. It cannot notice a merge resolution that discards one
    side's `add` wholesale: the survivor is self-consistent."""
    body = json.dumps(store['tokens'], sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(bytes.fromhex(store['salt']) + b'tokens:' + body.encode('utf-8')).hexdigest()


def self_test(store, drop_spaces=False):
    """None when the store is sound, else the reason it is not. Each failure has its own
    message: a broken checksum sends you to the records, an unfindable canary to the salt."""
    if store.get('algo') != 'sha256':
        return 'the store names algo %r; this tool only implements sha256' % store.get('algo')
    try:
        bytes.fromhex(store.get('salt') or '')
        intact = store.get('checksum') == tokens_checksum(store)
    except (KeyError, TypeError, ValueError):
        return 'the store has no usable salt or token records'
    if not intact:
        return 'the token records do not match the stored checksum (edited, or a merge resolved it)'
    if not store.get('canary'):
        return 'the store has no known-positive to test itself against'
    probe = {'salt': store['salt'],
             'tokens': [{'length': len(normalise(CANARY, True)[0]), 'digest': store['canary']}]}
    if not hits_in('prefix %s suffix' % CANARY, probe, drop_spaces):
        return 'the store cannot find its own known-positive'
    return None


def add_token(path, token):
    """Add the digest of `token` to the store at `path`, creating it when absent or `{}`.
    Returns `(added, count, why)`: whether a record was written, the number of tokens, and the
    self-test's answer for the store as written (None: sound). ForbiddenError for an empty or
    short token, or a file that is not a token store. The caller reads `token` from stdin: an
    argument would land in shell history and the process list."""
    token = (token or '').strip()
    if not token:
        raise ForbiddenError('nothing on stdin; pipe the token in, and do not pass it as an argument')
    norm = normalise(token, drop_spaces=True)[0]
    if len(norm) < MIN_TOKEN:
        raise ForbiddenError('a token under %d characters would fire on ordinary text' % MIN_TOKEN)
    store = load_store(path)
    if not store:
        store = {'algo': 'sha256', 'salt': os.urandom(16).hex(), 'canary': None, 'tokens': []}
    if not isinstance(store.get('salt'), str) or not isinstance(store.get('tokens'), list):
        raise ForbiddenError('%s is not a token store (no salt or token list); nothing written' % path)
    salt_bytes = bytes.fromhex(store['salt'])
    d = digest(salt_bytes, norm)
    if any(t.get('digest') == d for t in store['tokens']):
        return False, len(store['tokens']), self_test(store)
    store['canary'] = digest(salt_bytes, normalise(CANARY, True)[0])
    store['algo'] = 'sha256'
    store['tokens'] = sorted(store['tokens'] + [{'length': len(norm), 'digest': d}],
                             key=lambda t: t['digest'])
    store['checksum'] = tokens_checksum(store)
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(store, fh, indent=2, sort_keys=True)
        fh.write('\n')
    return True, len(store['tokens']), self_test(store)


# ---- what is scanned ---------------------------------------------------------------------------

def _git_lines(root, args, what):
    """NUL-separated git output as a list of paths; ForbiddenError naming `what` on failure."""
    out, why = gitcmd.run(list(args), root, timeout=GIT_TIMEOUT)
    if out is None:
        raise ForbiddenError('cannot list %s: %s' % (what, why))
    return [p for p in gitcmd.text(out).split('\0') if p.strip()]


def default_base(root, cfg=None):
    """`(rev, note)`: the main branch's name (`.kit.json` "repo".main_branch when written, else
    main if that branch exists, else master) measured by the shared rule kitlib.git.refs.
    default_base (docs/gate.md "Default base"): its `<remote>/` tracking ref when that resolves.
    `note` says when the local branch differs from the one measured against."""
    cfg = cfg if cfg is not None else load_config(root)
    if 'main_branch' in cfg.raw('repo'):
        name = cfg.main_branch
    else:
        # known=True: a git ended from outside is no answer, never "no
        # main", which measured against master.
        sha, why = gitrefs.resolve('refs/heads/main', root, known=True)
        if sha is None:
            raise ForbiddenError('cannot tell whether the branch main exists, so the base is '
                                 'unknown: %s' % why)
        name = 'main' if sha else 'master'
    return gitrefs.default_base(root, cfg.remote, name)


def tracked_paths(root):
    return _git_lines(root, ['ls-files', '-z'], 'the tracked files')


def untracked_paths(root):
    return _git_lines(root, ['ls-files', '-z', '--others', '--exclude-standard'], 'the untracked files')


def tree_paths(root):
    """Every tracked path plus every untracked, non-ignored one, from one git listing
    (`--exclude-standard` filters only the untracked half)."""
    return _git_lines(root, ['ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                      'the tracked and untracked files')


def changed_paths(root, base, worktree=False):
    """`(paths, swept)`: what this branch changed against `base`, or, on a merged tree (the merge
    base is HEAD), every tracked path with `swept` True.

    `worktree` compares the working tree rather than HEAD and adds untracked files: the gate runs
    with work in progress. The merged-tree sweep wins over `worktree`, which then only adds the
    untracked files: suppressing it would make a merged-tree gate scan nothing and pass."""
    mb, why = gitrefs.merge_base(base, 'HEAD', root)
    if not mb:
        raise ForbiddenError('cannot find the merge base with %s: %s' % (base, why or 'none'))
    head, _why = gitrefs.resolve('HEAD', root)
    if head == mb:
        return sorted(set(tree_paths(root) if worktree else tracked_paths(root))), True
    args = ['diff', '--name-only', '-z', '--no-renames', '--diff-filter=d', mb]
    paths = _git_lines(root, args + ([] if worktree else ['HEAD']), 'the changed files')
    if worktree:
        paths += untracked_paths(root)
    return sorted(set(paths)), False


def read_text(path):
    """The file's text, or None when it is not text this scans (binary, too big, unreadable)."""
    if path.lower().endswith(BINARY_EXT):
        return None
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None
        with open(path, 'rb') as fh:
            raw = fh.read()
    except OSError:
        return None
    if b'\0' in raw:
        return None
    return raw.decode('utf-8', errors='replace')


def line_of(text, offset):
    return text.count('\n', 0, offset) + 1


def scan(root, paths, store, drop_spaces):
    """`(report, read)`: (path, line) for every hit, and how many files were actually read.
    `line` is None when the PATH itself carries the token: the rule covers file names too, and a
    renamed file carries no content hit. The count is of files read, not selected, so a run that
    skipped most of its subject cannot read as one that cleared it."""
    report = []
    read = 0
    for rel in paths:
        if hits_in(rel, store, drop_spaces):
            report.append((rel, None))
        text = read_text(os.path.join(root, rel))
        if text is None:
            continue
        read += 1
        for offset in hits_in(text, store, drop_spaces):
            report.append((rel, line_of(text, offset)))
    return report, read


# ---- one run -----------------------------------------------------------------------------------

def check(root, base=None, worktree=False, sweep_all=False, paths=None, paranoid=False, cfg=None):
    """One run as an Outcome(code, lines). `paths` (repo-relative) are scanned as given;
    `sweep_all` scans every tracked path; otherwise what the branch changed against `base`
    (default `default_base`). ConfigError and ForbiddenError propagate (the caller's exit 2)."""
    cfg = cfg if cfg is not None else load_config(root)
    path = store_path(root, cfg)
    store = load_store(path)
    if not store or not store.get('tokens'):
        return Outcome(REFUSED, ['forbidden: no tokens configured (%s); nothing to check' % path])
    why = self_test(store, paranoid)
    if why:
        return Outcome(REFUSED, ['forbidden: SELF-TEST FAILED - %s.' % why,
                                 '  %s is disarmed or corrupt; this run checked nothing.' % path])
    lines = []
    if paths is not None:
        selected = list(paths)
    elif sweep_all:
        selected = tracked_paths(root)
    else:
        if not base:
            base, note = default_base(root, cfg)
            if note:
                lines.append('forbidden: %s' % note)
        selected, swept = changed_paths(root, base, worktree)
        if swept:
            lines.append('forbidden: merged tree (merge base is HEAD); sweeping every tracked path')
    if not selected:
        lines.append('forbidden: OK (nothing changed against %s)' % base if base else
                     'forbidden: OK (no tracked paths)')
        return Outcome(OK, lines)
    report, read = scan(root, selected, store, paranoid)
    if not read:
        # Every selected path was binary or unreadable: say so rather than report a clean tree.
        lines.append('forbidden: read no text in %d path(s); nothing checked' % len(selected))
        return Outcome(REFUSED, lines)
    form = 'space-removed' if paranoid else 'default'
    if report:
        lines.append('forbidden: a forbidden token appears in %d place(s) (%s form).' % (len(report), form))
        lines.append('  The token is deliberately not printed. Open each site and look.')
        for rel, line in report:
            lines.append('  %s%s' % (rel, '   <- in the PATH, not the contents' if line is None
                                     else ':%d' % line))
        if paranoid:
            lines.append('  --paranoid welds words across removed spaces; verify by eye before acting.')
        return Outcome(HIT, lines)
    skipped = len(selected) - read
    lines.append('forbidden: OK (%d path(s) read%s, %s form)'
                 % (read, ', %d skipped as binary or unreadable' % skipped if skipped else '', form))
    return Outcome(OK, lines)
