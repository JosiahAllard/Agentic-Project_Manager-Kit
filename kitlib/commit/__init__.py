r"""Stage explicit paths and commit with the right trailers, in one call (from the PM kit's
tools/commit.py; docs/commit.md).

commit(Request) -> Outcome(code, lines). Nothing here prints or exits; tools/commit.py does.

- The work tree is Request.root (tools/commit.py resolves `-C DIR`, default `.`, with
  kitlib.git.repo.toplevel). A path is relative to that work tree's root, or absolute inside it
  (outside: a usage error); a relative --body-file is the caller's (Request.cwd).
- Explicit paths only: a directory, `.`, `-A` or a glob is a usage error. A named path already
  staged as a deletion is committed as that deletion; nothing unnamed is added.
- The body: `-b` is the TEXT (a typed `\n` becomes a newline); --body-file reads it from a file
  (UTF-8, a BOM dropped, CRLF made LF; empty, unreadable or not UTF-8 is a usage error). A `-b`
  whose whole value is one line naming an existing file, or shaped like an absolute path, is a
  usage error: pass --body-file.
- A machine path in the subject or body is refused (kitlib.machinepaths.find); an
  --owner-authored commit is exempt. A machine path in the STAGED copy of a file the commit would
  record is refused by kitlib.machinepaths.scan_staged under the repository's own "machine_paths"
  config, with no --owner-authored exemption; the paths this call staged are unstaged again.
- Anything ELSE already staged is refused (it may be another session's), with one exception: with
  .kit.json "commit".merge_fold, during a merge in progress (one MERGE_HEAD), the files the merge
  staged itself (merge_owned), so only the resolved conflicts are named.
- Owner-only paths (.kit.json "owner", kitlib.config.owner) are committed only by the owner:
  --owner-authored, which refuses unless the author identity git will record (`git var
  GIT_AUTHOR_IDENT`) is "owner".email (unset, an agent_emails address or any other: refused).
  --owner-approved is required only when an owner-only path is in the commit, and
  every value given must be a blanket entry or a path committed in this call. An --owner-authored
  commit carries `Owner-Authored: yes` in place of Co-Authored-By.
- Otherwise the trailer is `Co-Authored-By: Claude <model> <noreply@anthropic.com>`, "Claude "
  added when the name lacks it - unless the name is a family in .kit.json
  "commit".coauthor_families or starts with one followed by a space, '.' or '-'.
  The model is required: --model, else CLAUDE_AGENT_MODEL (blank is unset).
- The main checkout is refused (kitlib.git.checkout.main_refusal); --lander-main-checkout is the
  one way past it, and a usage error anywhere else.
- After the commit: the hash, the files, and the doc-budget verdict when a .md or owner-only file
  was committed (the kit's tools/doc_budget.py run with `-C <root>`, judged by kitlib.verdict), then
  the doc-index verdict (tools/doc_index.py, same call; nothing when "doc_index".enabled is false).

Codes: OK 0 committed, REFUSED 1 refused or git failed, USAGE 2 a usage error, the main-checkout
refusal or a refused .kit.json.
"""
import collections
import os
import posixpath
import re
import subprocess
import sys

from kitlib import config as kconfig
from kitlib import machinepaths, verdict
from kitlib.docs import callers as doc_callers
from kitlib.config import owner as ownermod
from kitlib.git import checkout, cmd, repo

OK, REFUSED, USAGE = 0, 1, 2
TOOL = 'commit.py'
MODEL_ENV = 'CLAUDE_AGENT_MODEL'
OWNER_AUTHORED_TRAILER = 'Owner-Authored: yes'
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOC_BUDGET_TIMEOUT = 600     # seconds

norm = ownermod.norm

# root: the work tree's top level; cwd: the caller's directory (a relative --body-file); env: the
# environment CLAUDE_AGENT_MODEL is read from (None: os.environ).
Request = collections.namedtuple(
    'Request', 'root subject b body_file model owner_authored owner_approved '
               'lander_main_checkout paths cwd env')
Outcome = collections.namedtuple('Outcome', 'code lines')


def request(root, subject, paths, b='', body_file=None, model=None, owner_authored=False,
            owner_approved=(), lander_main_checkout=False, cwd=None, env=None):
    return Request(root, subject, b, body_file, model, owner_authored, list(owner_approved),
                   lander_main_checkout, list(paths), cwd or os.getcwd(), env)


class _Stop(Exception):
    def __init__(self, code, *lines):
        Exception.__init__(self, code)
        self.code = code
        self.lines = [l for l in lines if l is not None]


def settings(cfg):
    """`{'merge_fold', 'coauthor_families', 'main_branch', 'rule_pointer'}` from a checked Config.
    Raises ConfigError on a family name that is blank or not single-spaced."""
    sec = cfg.section('commit')
    families = sec['coauthor_families'] or []
    if not all(f.strip() and f == ' '.join(f.split()) for f in families):
        raise kconfig.ConfigError('.kit.json "commit".coauthor_families must be a list of names '
                                  '(one or more words, single-spaced), e.g. ["Codex"]')
    return {'merge_fold': bool(sec['merge_fold']), 'coauthor_families': families,
            'main_branch': cfg.main_branch,
            'rule_pointer': cfg.section('checkout_guard')['rule_pointer']}


def coauthor(model, families=()):
    """The co-author trailer for `model`: `Claude <model>`, never a bare model name - unless the
    name is one of `families` or starts with one followed by a space, '.' or '-' (`Codex GPT-5`,
    `Codex-mini`, `Codex.2`; never `Codexer`): that name is kept as given."""
    name = ' '.join((model or '').split())
    if not name:
        raise ValueError('--model is empty')
    if any(name == f or (name.startswith(f) and name[len(f)] in ' .-') for f in families):
        return 'Co-Authored-By: %s <noreply@anthropic.com>' % name
    if name != 'Claude' and not name.startswith('Claude '):
        name = 'Claude ' + name
    return 'Co-Authored-By: %s <noreply@anthropic.com>' % name


def repo_path(path, root):
    """A path as typed, in the repo-relative, forward-slash form `git diff --cached` reports: an
    absolute path is made relative to `root`, None when outside it (or on another drive). A
    relative path has its slashes flipped and its `./` and `..` segments folded, so
    `./CLAUDE.md` and `tools/../CLAUDE.md` reach the owner-only check as the `CLAUDE.md` that
    `git add` stages; one that climbs out of the work tree is None too. Case is left alone."""
    p = path.replace('\\', '/')
    if not os.path.isabs(p):
        p = posixpath.normpath(p)
        return None if p == '..' or p.startswith('../') else p
    try:
        rel = os.path.relpath(os.path.abspath(p), root).replace('\\', '/')
    except ValueError:
        return None
    if rel == '..' or rel.startswith('../'):
        return None
    return rel


def locked_paths(rules, paths):
    """The owner-only paths among `paths`."""
    return [p for p in paths if rules.is_locked(p)]


def unapproved_locked(rules, locked, owner_approved):
    """The locked paths no --owner-approved value covers (the exact path, or its blanket)."""
    return [p for p in locked if not rules.approved_by(p, owner_approved)]


def canonical_approved(rules, owner_approved, paths):
    """Each --owner-approved value in the spelling a landing check compares literally: the
    committed path it names, or the blanket entry as configured; anything else as typed."""
    return [rules.canonical(value, paths) for value in owner_approved]


def resolve_model(owner_authored, flag, env=None):
    """`(model, where it came from, usage message or None)`. --model is required on every
    ordinary commit: the flag, else MODEL_ENV (blank is unset). --owner-authored takes none:
    --model there is contradictory, and MODEL_ENV is ignored."""
    env = os.environ if env is None else env
    if owner_authored:
        if flag:
            return None, None, ('--owner-authored and --model contradict each other - one commit '
                                'cannot be both the owner\'s own and an agent\'s; drop one')
        return None, None, None
    if flag and flag.strip():
        return flag, '--model', None
    fallback = (env.get(MODEL_ENV) or '').strip()
    if fallback:
        return fallback, MODEL_ENV, None
    return None, None, ('--model is required: name the model you are running on, '
                        '--model "<model name>" (or set %s for this session only). If you do '
                        'not know it, say so and stop.' % MODEL_ENV)


def model_requirement_error(rules, owner_authored, owner_approved, paths):
    """None unless an --owner-authored commit's --owner-approved coverage fails: every value must
    name a blanket entry or one of the paths this call commits (an empty or unrelated value must
    not silently pass). No --owner-approved at all is refused only when an owner-only path is in
    the commit."""
    if not owner_authored:
        return None
    named = [norm(p) for p in paths]
    unrelated = [o for o in owner_approved
                 if not o.strip() or (not rules.is_blanket(o) and norm(o) not in named)]
    if (not owner_approved and locked_paths(rules, paths)) or unrelated:
        return ('--owner-authored requires every --owner-approved value to be a blanket '
                'directory entry or one of the paths committed in this call%s' % (
                    ' (not: %s)' % ', '.join(repr(o) for o in unrelated) if unrelated else ''))
    return None


def build_trailers(rules, owner_approved, owner_authored, model, families=()):
    """`<owner trailer>: <value>` lines (the configured "owner".trailer), then Co-Authored-By on
    an agent's commit, or `Owner-Authored: yes` on the owner's own."""
    trailers = ['%s: %s' % (rules.trailer, o) for o in owner_approved]
    trailers.append(OWNER_AUTHORED_TRAILER if owner_authored else coauthor(model, families))
    return trailers


def build_message(subject, body, trailers):
    parts = [subject]
    if body:
        parts.append(body)
    parts.append('\n'.join(trailers))
    return '\n\n'.join(parts) + '\n'


def _git(root, *args, timeout=None):
    """`(rc, stdout text unstripped, stderr text)`; rc None when git did not run."""
    kw = {} if timeout is None else {'timeout': timeout}
    r = cmd.call(list(args), root, **kw)
    return r.rc, r.out.decode('utf-8', 'replace'), r.err


def author_email(root):
    """The author email git will record for a commit in `root`, or None when git cannot say.
    `git var GIT_AUTHOR_IDENT` honours the GIT_AUTHOR_* environment."""
    rc, out, _ = _git(root, 'var', 'GIT_AUTHOR_IDENT')
    email = ownermod.email_of(out.strip()) if rc == 0 else ''
    return email or None


def owner_identity_error(rules, email):
    """None when `email` is provably the owner's under `rules`, else the refusal message."""
    problem = rules.identity_problem()
    if problem:
        return 'refused: --owner-authored, but %s. Nothing was committed.' % problem
    if email is None:
        return ('refused: --owner-authored, but git could not say which author identity it would '
                'record (git var GIT_AUTHOR_IDENT failed). Nothing was committed.')
    if email in rules.agent_emails:
        return ('refused: --owner-authored, but the author identity %s is an agent\'s '
                '("owner".agent_emails). The owner commits from his own terminal. Nothing was '
                'committed.' % email)
    if email != rules.owner_email:
        return ('refused: --owner-authored, but the author identity %s is not the owner\'s (%s). '
                'Nothing was committed.' % (email, rules.owner_email))
    return None


def staged_status(root):
    """`{path: status letter}` of every staged change (renames off: A, M or D only); raises _Stop
    when git cannot list them."""
    rc, out, err = _git(root, 'diff', '--cached', '--name-status', '--no-renames', '-z')
    if rc != 0:
        raise _Stop(REFUSED, 'git diff --cached failed: %s' % (err or 'exit %s' % rc))
    cols = [c for c in out.split('\0') if c]
    return {cols[i + 1]: cols[i][:1] for i in range(0, len(cols) - 1, 2)}


def merge_heads(root):
    """The commits a merge in progress is bringing in (MERGE_HEAD, one per line), or []."""
    path, _why = repo.git_path(root, 'MERGE_HEAD')
    if not path:
        return []
    try:
        with open(path, encoding='utf-8') as fh:
            return [ln.strip() for ln in fh.read().splitlines() if ln.strip()]
    except OSError:
        return []


def parse_merge_tree(raw):
    """`(tree, conflicted paths)` from `git merge-tree --write-tree --name-only --no-messages -z`
    (the tree OID, then each conflicted path once, NUL-terminated). A newline after the OID is
    tolerated as well as a NUL."""
    tokens = raw.split('\0')
    head = tokens[0]
    if '\n' in head:
        head, rest = head.split('\n', 1)
        tokens = [rest] + tokens[1:]
    else:
        tokens = tokens[1:]
    return head.strip(), {t for t in tokens if t not in ('', '\n')}


def same_blob(root, a, b):
    """True when both revisions name an object and it is the same one."""
    rc_a, oid_a, _ = _git(root, 'rev-parse', '-q', '--verify', a)
    rc_b, oid_b, _ = _git(root, 'rev-parse', '-q', '--verify', b)
    return rc_a == 0 and rc_b == 0 and bool(oid_a.strip()) and oid_a.strip() == oid_b.strip()


def merge_owned(root, rules, staged, heads):
    """`(paths, why)`: the staged paths a merge in progress put there itself. A path counts only
    when its staged content equals the clean merge result (`git merge-tree --write-tree`), merge-tree
    does not list it as conflicted, and, for an owner-only path, its staged blob is the merged
    branch's own (`<MERGE_HEAD>:<path>`). No merge, or an octopus one, gives `(set(), None)`; a git
    failure gives `(set(), why)`."""
    candidates = set(staged)
    if len(heads) != 1 or not candidates:
        return set(), None
    rc, out, err = _git(root, 'merge-tree', '--write-tree', '--name-only', '--no-messages', '-z',
                        'HEAD', heads[0])
    tree, conflicted = parse_merge_tree(out) if rc in (0, 1) else ('', set())
    if not tree:
        return set(), 'git merge-tree failed (rc %s): %s' % (rc, err or out.strip() or 'no output')
    rc, out, err = _git(root, 'diff', '--cached', '--name-only', '--no-renames', '-z', tree)
    if rc != 0:
        return set(), 'git diff --cached against the merge result failed: %s' % (err or 'no output')
    differs = {p for p in out.split('\0') if p}
    owned = candidates - differs - conflicted
    for p in [p for p in owned if rules.is_locked(p)]:
        if not same_blob(root, ':' + p, '%s:%s' % (heads[0], p)):
            owned.discard(p)
    return owned, None


def foreign_refusal(foreign, merge_why=None, in_merge=False):
    """The refusal text for files staged that the caller did not name."""
    notes = ''
    if merge_why is not None:
        notes += (' (a merge is in progress, but its own staged files could not be told apart: %s)'
                  % merge_why)
    elif in_merge:
        notes += (' (a merge is in progress; these differ from the clean merge result, are its '
                  'conflicts, or are owner-only and not the merged branch\'s own copy: name the '
                  'ones that are yours)')
    return ('refused: already staged and not in your list (another session?): %s%s'
            % (', '.join(sorted(foreign)), notes))


def doc_budget_check(root, kit_root=None):
    """`(verdict line, ok)` from the kit's tools/doc_budget.py run with `-C root`, or a NOT
    CHECKED line when the kit has none. Judged conjunctively (kitlib.verdict): exit 0 AND the OK
    line AND no FAILED line. `kit_root` defaults to KIT_ROOT, read at call time."""
    tool = os.path.join(kit_root or KIT_ROOT, 'tools', 'doc_budget.py')
    if not os.path.isfile(tool):
        return 'docs      NOT CHECKED - no tools/doc_budget.py in the kit, so nothing was measured', True
    try:
        r = subprocess.run([sys.executable, tool, '-C', root], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=DOC_BUDGET_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 'docs      FAILED (doc_budget.py did not finish in %d s)' % DOC_BUDGET_TIMEOUT, False
    except OSError as e:
        return 'docs      FAILED (doc_budget.py could not start: %s)' % e, False
    out = (r.stdout or '') + (r.stderr or '')
    return verdict.line('docs', r.returncode, out, keep=4), verdict.judge(r.returncode, out)[0]


def doc_index_check(root, kit_root=None):
    """`(verdict line, ok)` from the kit's tools/doc_index.py run with `-C root` (the index
    checks doc_budget no longer makes), `(None, True)` when `.kit.json` "doc_index".enabled is
    false, or a NOT CHECKED line when the kit has no tool. Judged as doc_budget_check judges."""
    try:
        if doc_callers.switched_off(kconfig.load(root)):
            return None, True
    except kconfig.ConfigError:
        pass                                  # the tool reports a bad config itself
    tool = doc_callers.tool(kit_root or KIT_ROOT)
    if not os.path.isfile(tool):
        return doc_callers.not_checked(), True
    try:
        r = subprocess.run([sys.executable, tool, '-C', root], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=doc_callers.TIMEOUT)
    except subprocess.TimeoutExpired:
        return 'doc index FAILED (doc_index.py did not finish in %d s)' % doc_callers.TIMEOUT, False
    except OSError as e:
        return 'doc index FAILED (doc_index.py could not start: %s)' % e, False
    line, failed = doc_callers.line(r.returncode, (r.stdout or '') + (r.stderr or ''))
    return line, not failed


# The whole -b value is a path: drive-rooted, UNC or `/`-rooted with no whitespace, or
# drive-rooted and ending in a file extension (a Windows path may hold spaces), or a `%VAR%\` path
# with no whitespace. A drive-rooted value with spaces and no extension is prose and passes.
PATH_SHAPED = re.compile(r'(?:[A-Za-z]:[\\/]|\\\\|/|%[A-Za-z_][A-Za-z0-9_]*%[\\/])\S*'
                         r'|[A-Za-z]:[\\/].*\.[A-Za-z0-9]{1,8}')


def path_shaped_body(value, root, cwd):
    """The usage error when `-b`'s whole value is one line that is a path rather than a body, else
    None. Judged on the value as typed, before a typed `\\n` becomes a newline."""
    v = (value or '').strip()
    if not v or '\n' in v or '\r' in v:
        return None
    exists = os.path.isfile(os.path.join(cwd, v)) or os.path.isfile(os.path.join(root, v))
    if not exists and not PATH_SHAPED.fullmatch(v):
        return None
    return ('-b is the body TEXT, and %r %s. To take the body from a file, pass --body-file %s '
            'instead.' % (v, 'is an existing file' if exists else 'looks like an absolute path', v))


def read_body_file(path):
    """`(body, None)` from a --body-file, or `(None, usage error)`: UTF-8 with a BOM dropped, CRLF
    made LF, trailing whitespace and leading blank lines trimmed."""
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError as exc:
        return None, '--body-file %s could not be read: %s' % (path, exc.strerror or exc)
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        return None, '--body-file %s is not UTF-8 (%s)' % (path, exc.reason)
    text = text.replace('\r\n', '\n').rstrip().lstrip('\n')
    if not text.strip():
        return None, '--body-file %s is empty' % path
    return text, None


def message_body(b, body_file, root, cwd):
    """`(body, None)` from -b or --body-file, or `(None, usage error)`."""
    if body_file is not None:
        return read_body_file(os.path.join(cwd, body_file))
    if not b:
        return '', None
    err = path_shaped_body(b, root, cwd)
    if err:
        return None, err
    return b.replace('\\n', '\n'), None


def machine_path_error(subject, body, typed=''):
    """The refusal when the subject or body carries a machine path, else None. `typed` is -b as
    typed, scanned too: the `\\n` conversion cuts a `<drive>:\\Users\\nick` path short."""
    found = []
    for text in (subject, typed, body):
        found += [f for f in machinepaths.find(text) if f not in found]
    if not found:
        return None
    return ('refused: the commit message carries a machine path: %s. '
            'tools/machine_paths.py fails a file with one, and history cannot be fixed once it '
            'lands. Write %%VAR%%\\... (the variable that applies), or leave it out.'
            % ', '.join(found))


def unstage_added(root, rels, status_before, lines):
    """`lines` plus how the index was left, as one refusal text, after resetting the paths among
    `rels` this call staged that were not staged before it."""
    added = sorted(set(rels) - set(status_before))
    rc, _out, gerr = _git(root, 'reset', '-q', '--', *added) if added else (0, '', '')
    return lines + ['Nothing was committed; %s' % (
        'fix it and run this again.' if rc == 0 else
        'the files this call staged are still staged (git reset failed: %s).' % gerr)]


def staged_machine_path_error(root, status_after, status_before):
    """`(refusal lines or None, warning lines)`: kitlib.machinepaths.scan_staged over the STAGED
    copy of every staged file but a deletion. Fail-closed: an index that cannot be read, a staged
    path with no blob, or a refused config is a refusal too. Every refusal unstages the paths this
    call staged that were not staged before it."""
    rels = sorted(p for p, s in status_after.items() if s != 'D')
    staged = sorted(status_after)
    try:
        got = machinepaths.scan_staged(root, rels)
    except kconfig.ConfigError as e:
        got = (None, 'its .kit.json "machine_paths" config is refused: %s' % e)
    if got[0] is None:
        return unstage_added(root, staged, status_before, [
            'refused: the staged files could not be checked for a machine path: %s.'
            % got[1]]), []
    hits, _used, read, skipped = got
    warnings = machinepaths.skipped_lines(skipped)
    if not hits:
        return None, warnings
    lines = warnings + machinepaths.hit_lines(hits, len(read))
    lines.append('refused: a staged file carries a machine path, and the '
                 'machine-paths check would fail the branch.')
    return unstage_added(root, staged, status_before, lines), []


def main_checkout_error(lander_main_checkout, root, main_branch=None, rule_pointer=None):
    """None if this commit may go ahead in `root`, else the refusal or usage error (exit 2).
    Without the flag: refused in the main checkout, and wherever git cannot tell. With it: a usage
    error everywhere EXCEPT the main checkout, so it cannot be carried by habit from a worktree."""
    if not lander_main_checkout:
        return checkout.main_refusal(root, 'a commit', TOOL, rule_pointer)
    place = repo.classify(root)
    if place.kind != repo.MAIN:
        return ('USAGE: --lander-main-checkout was given but %s is %s, not the main checkout%s. '
                'The flag is for a commit on %s in the main checkout only; drop it.'
                % (root, place.kind, ' (%s)' % place.detail if place.detail else '',
                   main_branch or 'the main branch'))
    return None


def commit(req):
    """Outcome(code, lines) for one commit request (module docstring)."""
    lines = []
    try:
        _commit(req, lines)
    except _Stop as stop:
        return Outcome(stop.code, lines + stop.lines)
    return Outcome(OK, lines)


def _usage(text):
    return _Stop(USAGE, 'USAGE: %s' % text)


def _commit(req, lines):
    root = req.root
    try:
        cfg = kconfig.load(root)
        opts = settings(cfg)
        rules = cfg.owner_rules()
    except kconfig.ConfigError as e:
        raise _Stop(USAGE, 'REFUSED: the kit configuration is refused: %s. Nothing was committed.'
                    % e)
    model, source, err = resolve_model(req.owner_authored, req.model, req.env)
    if err:
        raise _usage(err)
    if source == MODEL_ENV:
        lines.append('model     %s (from %s)' % (model, MODEL_ENV))

    body, err = message_body(req.b, req.body_file, root, req.cwd)
    if err:
        raise _usage(err)
    # The owner's own commit is exempt: the owner may keep a path literal on purpose.
    err = None if req.owner_authored else machine_path_error(req.subject, body, req.b)
    if err:
        raise _Stop(REFUSED, err)

    err = main_checkout_error(req.lander_main_checkout, root, opts['main_branch'],
                              opts['rule_pointer'])
    if err:
        raise _Stop(USAGE, err)

    paths = []
    for p in req.paths:
        rel = repo_path(p, root)
        if rel is None:
            raise _usage('"%s" is outside this worktree (%s); name a file inside it' % (p, root))
        paths.append(rel)
    for p in paths:
        if p in ('.', '-A', '--all') or '*' in p or os.path.isdir(os.path.join(root, p)):
            raise _usage('"%s" is not an explicit file path; name each file' % p)

    err = model_requirement_error(rules, req.owner_authored, req.owner_approved, paths)
    if err:
        raise _usage(err)

    status_before = staged_status(root)
    heads = merge_heads(root) if opts['merge_fold'] else []
    merged, merge_why = merge_owned(root, rules, set(status_before) - set(paths), heads)
    foreign = set(status_before) - set(paths) - merged
    if foreign:
        raise _Stop(REFUSED, foreign_refusal(foreign, merge_why, len(heads) == 1))
    approved = canonical_approved(rules, req.owner_approved, paths)
    locked = locked_paths(rules, paths)
    if locked and not req.owner_authored:
        raise _Stop(REFUSED, 'refused: %s is owner-only, and only a commit the owner authors (his '
                    'own git identity, --owner-authored) can carry its approval. Propose the '
                    'change to the owner.' % ', '.join(locked))
    for p in unapproved_locked(rules, locked, approved):
        raise _Stop(REFUSED, 'refused: %s is owner-only and no --owner-approved value names it.' % p)
    if req.owner_authored:
        err = owner_identity_error(rules, author_email(root))
        if err:
            raise _Stop(REFUSED, err)
    trailers = build_trailers(rules, approved, req.owner_authored, model, opts['coauthor_families'])

    # A listed path is always added again. The one skip: a listed path staged as a deletion and
    # absent from the work tree (`git add` would fail "pathspec did not match"); its deletion is
    # committed because it was named. Nothing unnamed is ever added.
    to_add = [p for p in paths
              if not (status_before.get(p) == 'D' and not os.path.lexists(os.path.join(root, p)))]
    if to_add:
        rc, _out, err = _git(root, 'add', '--', *to_add, timeout=None)
        if rc != 0:
            raise _Stop(REFUSED, 'git add failed: %s' % (err or 'exit %s' % rc))
    status_after = staged_status(root)
    if not status_after:
        raise _Stop(REFUSED, 'nothing to commit in: %s' % ', '.join(paths))
    refusal, warnings = staged_machine_path_error(root, status_after, status_before)
    if refusal:
        raise _Stop(REFUSED, *refusal)
    lines.extend(warnings)
    msg = build_message(req.subject, body, trailers)
    rc, out, err = _git(root, 'commit', '-q', '-m', msg, timeout=None)
    if rc != 0:
        raise _Stop(REFUSED, 'commit failed: %s' % (err or out.strip() or 'exit %s' % rc))
    _rc, short, _err = _git(root, 'rev-parse', '--short', 'HEAD')
    lines.append('committed %s  %s' % (short.strip(), req.subject))
    lines.append('files     %s' % ', '.join(
        paths + sorted((merged & set(status_after)) - set(paths))))
    if any(p.endswith('.md') for p in status_after) or locked:
        line, ok = doc_budget_check(root)
        lines.append(line)
        iline, iok = doc_index_check(root)
        if iline:
            lines.append(iline)
        if not (ok and iok):
            lines.append('          fix it in a follow-up commit before this branch can land')
