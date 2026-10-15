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
- A machine path in the subject or body is refused (kitlib.machinepaths.find, over the machine
  config's `machine_path_folders` and a user profile); an --owner-authored commit is exempt. A
  machine path in the STAGED copy of a file the commit would record is refused by
  kitlib.machinepaths.scan_staged under the repository's own "machine_paths" config, with no
  --owner-authored exemption; the paths this call staged are unstaged again. A machine config the
  kit cannot read is a WARNING, printed once (by the message check, or by the staged check of an
  --owner-authored commit), and user-profile paths are still refused.
- Anything ELSE already staged is refused (it may be another session's), with one exception: with
  .kit.json "commit".merge_fold, during a merge in progress (one MERGE_HEAD), the files the merge
  staged itself (merge_owned), so only the resolved conflicts are named. An owner-only path is the
  merge's only when its staged state is exactly the merged branch's copy and that branch changed
  it (a deletion included; a conflict resolved to that copy too); named, it is refused as below.
- The commit records only the paths this call named, rendered or let a merge carry (commit_only):
  it is made from a private index (GIT_INDEX_FILE), HEAD's tree plus those paths' staged entries,
  so a file another writer stages after the check above is not swept in. The real index is not
  written; what is still staged after the commit is named in a WARNING line. HEAD and those
  entries are read once, before the machine-path check; an entry staged again after it, a HEAD
  that cannot be read (other than an unborn branch), and a HEAD another writer moves before the
  commit refuse it. A commit git made on a HEAD moved after the last check is undone (update-ref
  back to its parent, the merge state written back) and refused.
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
- Last, the session line (session_lines): `<key>: <value>` lines that end the body (the hosts'
  attribution.session_trailer keys, `Claude-Session`) move into the trailer block, where git
  reads them; with none, an agent's commit gets its host's own, `Claude-Session:
  https://claude.ai/code/<id>` from CLAUDE_CODE_BRIDGE_SESSION_ID when that is set to a plain id.
- The main checkout is refused (kitlib.git.checkout.main_refusal); --lander-main-checkout is the
  one way past it, and a usage error anywhere else.
- Render on commit (kitlib.content.oncommit; docs/content.md "Render on commit"): in a work tree
  with the kit's layout, when a named path is under `content/` or is `kit.json` or `hosts.json`,
  the work tree's own tools/render_content.py --write runs first, and every rendered file it wrote
  or removed is staged into the same commit (listed on a `rendered` line). A render that fails
  refuses the commit, naming the command to run; what it wrote stays in the work tree.
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
import shutil
import subprocess
import sys
import tempfile

from kitlib import config as kconfig
from kitlib import attribution, machinepaths, verdict
from kitlib.docs import callers as doc_callers
from kitlib.config import owner as ownermod
from kitlib.content import oncommit
from kitlib.git import checkout, cmd, repo
from kitlib.proc import spawn

OK, REFUSED, USAGE = 0, 1, 2
TOOL = 'commit.py'
MODEL_ENV = attribution.model_environment()       # the default host's (hosts.json)
OWNER_AUTHORED_TRAILER = 'Owner-Authored: yes'
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOC_BUDGET_TIMEOUT = 600     # seconds

norm = ownermod.norm

# root: the work tree's top level; cwd: the caller's directory (a relative --body-file); env: the
# environment the host's model variable is read from (None: os.environ); provider: the host name
# (None: the hosts.json default).
Request = collections.namedtuple(
    'Request', 'root subject b body_file model owner_authored owner_approved '
               'lander_main_checkout paths cwd env provider', defaults=(None,))
Outcome = collections.namedtuple('Outcome', 'code lines')


def request(root, subject, paths, b='', body_file=None, model=None, owner_authored=False,
            owner_approved=(), lander_main_checkout=False, cwd=None, env=None, provider=None):
    return Request(root, subject, b, body_file, model, owner_authored, list(owner_approved),
                   lander_main_checkout, list(paths), cwd or os.getcwd(), env, provider)


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


def coauthor(model, families=(), provider=None):
    """The co-author trailer for `model`: `Claude <model>`, never a bare model name - unless the
    name is one of `families` or starts with one followed by a space, '.' or '-' (`Codex GPT-5`,
    `Codex-mini`, `Codex.2`; never `Codexer`): that name is kept as given."""
    return attribution.coauthor(model, provider, families)


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
    """Each --owner-approved value in the one spelling a trailer is written in
    (kitlib.config.owner, Rules.canonical): the committed path it names, or the blanket entry as
    configured; anything else as typed."""
    return [rules.canonical(value, paths) for value in owner_approved]


def resolve_model(owner_authored, flag, env=None, provider=None):
    """`(model, where it came from, usage message or None)`. --model is required on every
    ordinary commit: the flag, else MODEL_ENV (blank is unset). --owner-authored takes none:
    --model there is contradictory, and MODEL_ENV is ignored."""
    env = os.environ if env is None else env
    try:
        model_env = attribution.model_environment(provider)
        if not owner_authored and attribution.requires_model_id(provider):
            attribution.coauthor(flag, provider)
    except ValueError as error:
        return None, None, str(error)
    if owner_authored:
        if flag:
            return None, None, ('--owner-authored and --model contradict each other - one commit '
                                'cannot be both the owner\'s own and an agent\'s; drop one')
        return None, None, None
    if flag and flag.strip():
        return flag, '--model', None
    fallback = (env.get(model_env) or '').strip() if model_env else ''
    if fallback:
        return fallback, model_env, None
    return None, None, ('--model is required: name the model you are running on, '
                        '--model "<model name>" (or set %s for this session only). If you do '
                        'not know it, say so and stop.' % (model_env or MODEL_ENV))


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


def build_trailers(rules, owner_approved, owner_authored, model, families=(), provider=None,
                   session=()):
    """`<owner trailer>: <value>` lines (the configured "owner".trailer), then Co-Authored-By on
    an agent's commit, or `Owner-Authored: yes` on the owner's own, then the `session` lines."""
    trailers = ['%s: %s' % (rules.trailer, o) for o in owner_approved]
    trailers.append(OWNER_AUTHORED_TRAILER if owner_authored else coauthor(model, families, provider))
    trailers.extend(session)
    return trailers


def lift_session_lines(body, keys):
    """`(body, lines)`: the `<key>: <value>` lines (a key of `keys`, any case; one value with no
    space) that END the body, taken off it. They go in the trailer block: left in the body, the
    blank line before the trailers keeps git from reading them as trailers (a `-b "...~~
    Claude-Session: <url>"`). A line anywhere else in the body stays where it is."""
    if not body or not keys:
        return body, []
    line_re = re.compile(r'(?:%s): \S+\Z' % '|'.join(re.escape(k) for k in keys), re.IGNORECASE)
    rows = body.rstrip().split('\n')
    lifted = []
    while rows and line_re.match(rows[-1].strip()):
        lifted.insert(0, rows.pop().strip())
    return '\n'.join(rows).rstrip(), lifted


def session_lines(body, owner_authored, env=None, provider=None):
    """`(body, session trailer lines)`: a body-final session line is lifted out of the body
    (lift_session_lines) and wins; otherwise an agent's commit gets the host's own
    (kitlib.attribution.session_trailer, from `env`, default os.environ). The owner's own commit
    gets none from the environment."""
    body, lifted = lift_session_lines(body, attribution.session_keys())
    if lifted or owner_authored:
        return body, lifted
    line = attribution.session_trailer(os.environ if env is None else env, provider)
    return body, [line] if line else []


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


def _entries(out, path):
    """The `(meta fields, name)` rows of a `-z` ls-files/ls-tree listing whose name is exactly
    `path` (a pathspec may match more: a directory's contents, or a glob)."""
    rows = []
    for row in out.split('\0'):
        meta, tab, name = row.partition('\t')
        if tab and name == path:
            rows.append(meta.split())
    return rows


def index_entry(root, path):
    """`(known, entry)`: `path`'s staged index entry as `(mode, oid)`, or None when the index holds
    none (a staged deletion). `known` is False when git cannot say, or when the path is unmerged
    (a conflict stage is in the index): nothing is proven then."""
    rc, out, _ = _git(root, 'ls-files', '--stage', '-z', '--', path)
    if rc != 0:
        return False, None
    rows = _entries(out, path)
    if not rows:
        return True, None
    if len(rows) != 1 or len(rows[0]) != 3 or rows[0][2] != '0':
        return False, None
    return True, (rows[0][0], rows[0][1])


def tree_entry(root, rev, path):
    """`(known, entry)`: `path`'s entry in commit `rev` as `(mode, oid)`, or None when the commit
    has none (it deleted the path). `known` is False when git cannot say."""
    rc, out, _ = _git(root, 'ls-tree', '-z', rev, '--', path)
    if rc != 0:
        return False, None
    rows = _entries(out, path)
    if not rows:
        return True, None
    if len(rows) != 1 or len(rows[0]) != 3:
        return False, None
    return True, (rows[0][0], rows[0][2])


def merge_bases(root, head):
    """Every merge base of HEAD and `head`, or None when git cannot say (or finds none)."""
    rc, out, _ = _git(root, 'merge-base', '--all', 'HEAD', head)
    bases = out.split() if rc == 0 else []
    return bases or None


def carries_merged_copy(root, path, head, bases):
    """True when `path`'s staged state is exactly the merged-in commit `head`'s (the same mode and
    blob, or absent from both: the merged branch deleted it and the deletion is staged) AND `head`
    changed the path from every merge base in `bases`, so the change came in with the merged
    branch rather than being the merge undoing this branch's own. An unmerged path, one git cannot
    list, or no known merge base is False."""
    if not bases:
        return False
    known_i, staged = index_entry(root, path)
    known_t, theirs = tree_entry(root, head, path)
    if not (known_i and known_t and staged == theirs):
        return False
    for base in bases:
        known_b, at_base = tree_entry(root, base, path)
        if not known_b or at_base == theirs:
            return False
    return True


def merge_owned(root, rules, staged, heads):
    """`(paths, why)`: the staged paths a merge in progress put there itself. An ordinary path
    counts only when its staged content equals the clean merge result (`git merge-tree
    --write-tree`) and merge-tree does not list it as conflicted. An owner-only path counts only
    when its staged state is exactly the merged branch's own copy, which that branch changed from
    the merge base (carries_merged_copy: same mode and blob as `<MERGE_HEAD>:<path>`, or a deletion
    of a path the merged branch deleted), whether the merge took it cleanly or a conflict was
    resolved to it, and every commit the merge brings that changes it is the owner's
    (non_owner_paths); any other staged state of an owner-only path is never the merge's. No
    merge, or an octopus one, gives `(set(), None)`; a git failure gives `(set(), why)` (or the
    ordinary paths and `why`, when only the authors could not be checked)."""
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
    locked = {p for p in candidates if rules.is_locked(p)}
    owned = (candidates - locked) - differs - conflicted
    if locked:
        foreign, why = non_owner_paths(root, rules, heads[0])
        if foreign is None:
            return owned, why
        bases = merge_bases(root, heads[0])
        owned |= {p for p in locked - foreign if carries_merged_copy(root, p, heads[0], bases)}
    return owned, None


def non_owner_paths(root, rules, head):
    """`(paths, None)`: the owner-only paths that a commit the merged-in `head` brings (one not
    reachable from HEAD) changes without the owner as its author, judged as `gate land` judges a
    branch (kitlib.guard.ownercommits.violations against HEAD); or `(None, why)` when git cannot
    tell. A merged copy one of those paths carries is not the owner's, so it is never the
    merge's."""
    from kitlib.guard import ownercommits     # here: kitlib.guard reaches back into this module
    bad, why = ownercommits.violations(root, rules, head, ['HEAD'])
    if bad is None:
        return None, 'the authors of the merged commits could not be checked: %s' % why
    return {p for v in bad for p in v.paths}, None


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
        r = spawn.run([sys.executable, tool, '-C', root], capture_output=True, text=True,
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
        r = spawn.run([sys.executable, tool, '-C', root], capture_output=True, text=True,
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
    """`(body, None)` from -b or --body-file, or `(None, usage error)`. In -b a typed `\\n` and a
    `~~` each become a newline (`~~` survives shells that eat a backslash); a single `~` is kept."""
    if body_file is not None:
        return read_body_file(os.path.join(cwd, body_file))
    if not b:
        return '', None
    err = path_shaped_body(b, root, cwd)
    if err:
        return None, err
    return b.replace('\\n', '\n').replace('~~', '\n'), None


def machine_path_error(subject, body, typed='', folders=None):
    """`(refusal or None, warning lines)`: the refusal when the subject or body carries a machine
    path. `typed` is -b as typed, scanned too: the `\\n` conversion cuts a
    `<drive>:\\Users\\nick` path short. `folders` are the drive-rooted folder names refused
    besides a user profile (None: the machine config's, machinepaths.folders(); one it cannot
    read refuses user-profile paths only, and its WARNING is the one warning line)."""
    note = None
    if folders is None:
        folders, note = machinepaths.folders()
    warnings = [note] if note else []
    found = []
    for text in (subject, typed, body):
        found += [f for f in machinepaths.find(text, folders) if f not in found]
    if not found:
        return None, warnings
    return ('refused: the commit message carries a machine path: %s. '
            'tools/machine_paths.py fails a file with one, and history cannot be fixed once it '
            'lands. Write %%VAR%%\\... (the variable that applies), or leave it out.'
            % ', '.join(found)), warnings


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
    path with no blob, or a refused config is a refusal too. A machine config the kit cannot read
    is not: its WARNING leads the warning lines. Every refusal unstages the paths this call staged
    that were not staged before it."""
    rels = sorted(p for p, s in status_after.items() if s != 'D')
    staged = sorted(status_after)
    note = []
    try:
        cfg = machinepaths.load_config(root)
        note = [cfg.note] if cfg.note else []
        got = machinepaths.scan_staged(root, rels, cfg)
    except kconfig.ConfigError as e:
        got = (None, 'its .kit.json "machine_paths" config is refused: %s' % e)
    if got[0] is None:
        return unstage_added(root, staged, status_before, note + [
            'refused: the staged files could not be checked for a machine path: %s.'
            % got[1]]), []
    hits, _used, read, skipped = got
    warnings = note + machinepaths.skipped_lines(skipped)
    if not hits:
        return None, warnings
    lines = warnings + machinepaths.hit_lines(hits, len(read))
    lines.append('refused: a staged file carries a machine path, and the '
                 'machine-paths check would fail the branch.')
    return unstage_added(root, staged, status_before, lines), []


def left_staged_line(others):
    """The warning for files another writer staged during this call, which it left staged."""
    return ('WARNING: staged by another writer during this call, left staged and not committed: '
            '%s' % ', '.join(sorted(others)))


def head_commit(root):
    """HEAD's commit id, or '' on an unborn branch (HEAD names a branch that does not exist yet).
    Any other failure to read HEAD raises _Stop: an empty tree is never guessed."""
    rc, out, err = _git(root, 'rev-parse', '-q', '--verify', 'HEAD^{commit}')
    if rc == 0 and out.strip():
        return out.strip()
    src, ref, _e = _git(root, 'symbolic-ref', '-q', 'HEAD')
    if src == 0 and ref.strip():
        rrc, rout, _e = _git(root, 'rev-parse', '-q', '--verify', ref.strip())
        if rrc == 1 and not rout.strip():
            return ''
    raise _Stop(REFUSED, 'refused: git could not read HEAD (rev-parse HEAD: %s). Nothing was '
                'committed.' % (err.strip() or 'exit %s' % rc))


def index_snapshot(root, keep):
    """`{path: (mode, oid), or None for a deletion}`: the real index's entry for each path of
    `keep` ({path: status letter}), read in one `git ls-files --stage`. Raises _Stop when git cannot
    list the index or a kept path is not one stage-0 entry (unmerged, or changed meanwhile)."""
    rc, out, err = _git(root, 'ls-files', '--stage', '-z')
    if rc != 0:
        raise _Stop(REFUSED, 'git ls-files --stage failed: %s. Nothing was committed.'
                    % (err or 'exit %s' % rc))
    entries = {}
    for row in out.split('\0'):
        meta, tab, name = row.partition('\t')
        if tab and name in keep:
            entries.setdefault(name, []).append(meta.split())
    snap = {}
    for path in sorted(keep):
        metas = entries.get(path, [])
        if keep[path] == 'D' and not metas:
            snap[path] = None
        elif len(metas) == 1 and len(metas[0]) == 3 and metas[0][2] == '0':
            snap[path] = (metas[0][0], metas[0][1])
        else:
            raise _Stop(REFUSED, 'refused: %s is not one staged entry in the index (unmerged, or '
                        'changed during this call). Nothing was committed; run this again.' % path)
    return snap


def private_index_file(tmp):
    """The private index's path in the folder `tmp`. Its name starts `next-index-`, the name
    git gives the index of a partial commit, so the kit's pre-commit hook
    (kitlib.githooks.render.partial) treats this commit as one."""
    return os.path.join(tmp, 'next-index-%d' % os.getpid())


def private_index(root, tmp, head, snap):
    """`(env, None)` for a `git commit` that records exactly `snap` (index_snapshot): a private
    index (private_index_file) holding `head`'s tree (empty for '', an unborn branch) with each
    snapshot entry set, a deletion (None) removed; or `(None, why)`. Every other entry of the real
    index, staged by anyone, is left out of the commit and left as it is. read-tree records no
    stat information, so `git commit` may read tracked work-tree files again to compare them; the
    cost grows with the work tree, and was accepted over `read-tree -m`, which refuses an
    unmerged real index."""
    env = dict(cmd.scrubbed_env(), GIT_INDEX_FILE=private_index_file(tmp))
    r = cmd.call(['read-tree', head] if head else ['read-tree', '--empty'], root, env=env)
    if r.rc != 0:
        return None, 'git read-tree into a private index failed: %s' % (r.err or 'exit %s' % r.rc)
    zero = '0' * (len(head) or 40)
    lines = ['0 %s\t%s' % (zero, p) if e is None else '%s %s\t%s' % (e[0], e[1], p)
             for p, e in sorted(snap.items())]
    if lines:
        r = cmd.call(['update-index', '--index-info'], root, env=env,
                     stdin=('\n'.join(lines) + '\n').encode('utf-8'))
        if r.rc != 0:
            return None, 'git update-index on the private index failed: %s' % (
                r.err or 'exit %s' % r.rc)
    return env, None


MERGE_STATE = ('MERGE_HEAD', 'MERGE_MSG', 'MERGE_MODE')


def merge_state(root):
    """`{path: bytes}` of the merge-state files that exist now (MERGE_STATE), to put back when a
    commit is undone."""
    saved = {}
    for name in MERGE_STATE:
        path, _why = repo.git_path(root, name)
        try:
            with open(path, 'rb') as fh:
                saved[path] = fh.read()
        except (OSError, TypeError):
            pass
    return saved


def head_moved_line(head, now):
    return ('refused: HEAD moved during this call (another writer committed %s on top of %s); '
            'this commit would have reverted their change.' % (now[:12] or 'nothing',
                                                                 head[:12] or 'an unborn branch'))


def undo_commit(root, new, parent, saved):
    """Point HEAD back at `parent` if it is still `new` (update-ref with the old value), and write
    back the merge-state files `saved` that the commit removed. The refusal lines either way."""
    rc, _out, err = _git(root, 'update-ref', '-m', 'commit.py: undo a commit made on a moved HEAD',
                         'HEAD', parent, new)
    if rc != 0:
        return ['refused, and NOT undone: commit %s was made on the moved HEAD and lacks the other '
                'writer\'s change; git update-ref failed: %s. Undo it by hand: git update-ref HEAD '
                '%s %s' % (new[:12], err.strip() or 'exit %s' % rc, parent, new)]
    for path, data in saved.items():
        if not os.path.exists(path):
            try:
                with open(path, 'wb') as fh:
                    fh.write(data)
            except OSError as e:
                return ['Undone: HEAD is back at %s, but %s could not be written back (%s).'
                        % (parent[:12], path, e.strerror)]
    return ['Undone: HEAD is back at %s; nothing was committed, and the files this call named '
            'are still staged. Run this again.' % parent[:12]]


def commit_only(root, head, snap, msg):
    """`git commit` of exactly `snap` (index_snapshot) on `head` (head_commit) through
    private_index; `(rc, out, err)` as _git gives them. The real index is never written: its
    entries for `snap` already match the new HEAD, and every other staged entry stays staged.
    HEAD must not move under the call: checked just before `git commit` (refused), and after it,
    where a commit made on a HEAD another writer moved is undone (undo_commit) and refused."""
    tmp = tempfile.mkdtemp(prefix='kit-commit-')
    try:
        env, why = private_index(root, tmp, head, snap)
        if env is None:
            return 1, '', why
        now = head_commit(root)
        if now != head:
            raise _Stop(REFUSED, head_moved_line(head, now), 'Nothing was committed; the files '
                        'this call named are still staged. Run this again.')
        saved = merge_state(root)
        r = cmd.call(['commit', '-q', '-m', msg], root, env=env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if r.rc != 0:
        return r.rc, r.out.decode('utf-8', 'replace'), r.err
    rc, out, err = _git(root, 'log', '-1', '--format=%H %P', 'HEAD')
    ids = out.split()
    if rc != 0 or not ids:
        raise _Stop(REFUSED, 'git commit ran, but HEAD could not be read back to check it was '
                    'made on %s: %s. Check `git log -2` by hand.'
                    % (head[:12] or 'the unborn branch', err.strip() or 'exit %s' % rc))
    new, parents = ids[0], ids[1:]
    if (parents[0] if parents else '') != head:
        if not parents:
            raise _Stop(REFUSED, head_moved_line(head, ''), 'NOT undone: commit %s has no parent; '
                        'check `git log` by hand.' % new[:12])
        raise _Stop(REFUSED, head_moved_line(head, parents[0]),
                    *undo_commit(root, new, parents[0], saved))
    return 0, r.out.decode('utf-8', 'replace'), r.err


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


def render_sources(root, paths):
    """The repo-relative rendered paths the kit's renderer wrote or removed in `root`, when `root`
    has the kit's layout and a path in `paths` is a render source (kitlib.content.oncommit); else
    None (no render ran). A render that fails raises _Stop, naming the command to run."""
    if not (oncommit.wants_render(paths) and oncommit.is_kit(root)):
        return None
    got, why = oncommit.render(root)
    if got is None:
        raise _Stop(REFUSED, 'refused: a render source is in this commit, and the render failed: %s. '
                    'Fix it and render by hand, then commit again: %s. Nothing was committed.'
                    % (why, oncommit.command(root)))
    return got


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
    model, source, err = resolve_model(req.owner_authored, req.model, req.env, req.provider)
    if err:
        raise _usage(err)
    if source not in (None, '--model'):
        lines.append('model     %s (from %s)' % (model, source))

    body, err = message_body(req.b, req.body_file, root, req.cwd)
    if err:
        raise _usage(err)
    # The owner's own commit is exempt: the owner may keep a path literal on purpose.
    err, msg_warnings = ((None, []) if req.owner_authored
                         else machine_path_error(req.subject, body, req.b))
    if err:
        raise _Stop(REFUSED, err, *msg_warnings)
    lines.extend(msg_warnings)

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

    ran = render_sources(root, paths)
    rendered = ran or []
    named = set(paths) | set(rendered)
    status_before = staged_status(root)
    heads = merge_heads(root) if opts['merge_fold'] else []
    merged, merge_why = merge_owned(root, rules, set(status_before) - named, heads)
    foreign = set(status_before) - named - merged
    if foreign:
        raise _Stop(REFUSED, foreign_refusal(foreign, merge_why, len(heads) == 1))
    approved = canonical_approved(rules, req.owner_approved, paths)
    locked = locked_paths(rules, paths + [p for p in rendered if p not in paths])
    if locked and not req.owner_authored:
        # The hint only for a path the merge would carry unnamed, as it is staged now.
        carried = sorted(merge_owned(root, rules, set(locked) & set(status_before), heads)[0])
        raise _Stop(REFUSED, 'refused: %s is owner-only, and only a commit the owner authors (his '
                    'own git identity, --owner-authored) can carry its approval. Propose the '
                    'change to the owner.%s' % (', '.join(locked), (
                        ' (A merge in progress carries the merged branch\'s own copy of %s '
                        'without it being named: leave it out of your list.)'
                        % ', '.join(carried)) if carried else ''))
    for p in unapproved_locked(rules, locked, approved):
        raise _Stop(REFUSED, 'refused: %s is owner-only and no --owner-approved value names it.' % p)
    if req.owner_authored:
        err = owner_identity_error(rules, author_email(root))
        if err:
            raise _Stop(REFUSED, err)
    body, session = session_lines(body, req.owner_authored, req.env, req.provider)
    trailers = build_trailers(rules, approved, req.owner_authored, model, opts['coauthor_families'],
                              req.provider, session)

    # A listed path is always added again. The one skip: a listed path staged as a deletion and
    # absent from the work tree (`git add` would fail "pathspec did not match"); its deletion is
    # committed because it was named. Nothing unnamed is ever added.
    to_add = [p for p in paths
              if not (status_before.get(p) == 'D' and not os.path.lexists(os.path.join(root, p)))]
    if to_add:
        rc, _out, err = _git(root, 'add', '--', *to_add, timeout=None)
        if rc != 0:
            raise _Stop(REFUSED, 'git add failed: %s' % (err or 'exit %s' % rc))
    err = oncommit.stage(root, rendered) if rendered else None
    if err:
        raise _Stop(REFUSED, *unstage_added(root, sorted(named), status_before, [
            'refused: the rendered files could not be staged: %s.' % err]))
    # Only what this call named, rendered or the merge staged is committed: anything another
    # writer staged since the check above stays staged, out of this commit.
    staged_now = staged_status(root)
    status_after = {p: s for p, s in staged_now.items() if p in named or p in merged}
    others = set(staged_now) - set(status_after)
    if not status_after:
        raise _Stop(REFUSED, 'nothing to commit in: %s' % ', '.join(paths))
    # What is committed is this one read of HEAD and of the index; the index must still hold it
    # after the machine-path check, so the blobs checked are the blobs committed.
    head = head_commit(root)
    snap = index_snapshot(root, status_after)
    refusal, warnings = staged_machine_path_error(root, status_after, status_before)
    # The message check printed the same machine-config WARNING already: once is enough.
    if refusal:
        raise _Stop(REFUSED, *[l for l in refusal if l not in msg_warnings])
    changed = sorted(p for p, e in index_snapshot(root, status_after).items() if snap[p] != e)
    if changed:
        raise _Stop(REFUSED, 'refused: %s was staged again during this call, after its staged copy '
                    'was checked. Nothing was committed; run this again.' % ', '.join(changed))
    lines.extend(l for l in warnings if l not in msg_warnings)
    msg = build_message(req.subject, body, trailers)
    rc, out, err = commit_only(root, head, snap, msg)
    if rc != 0:
        raise _Stop(REFUSED, 'commit failed: %s' % (err or out.strip() or 'exit %s' % rc))
    _rc, short, _err = _git(root, 'rev-parse', '--short', 'HEAD')
    lines.append('committed %s  %s' % (short.strip(), req.subject))
    lines.append('files     %s' % ', '.join(
        paths + sorted((merged & set(status_after)) - set(paths))))
    try:
        others = set(staged_status(root))    # against the new HEAD: what is still staged
    except _Stop:
        pass                                 # the commit stands; the earlier read says enough
    if others:
        lines.append(left_staged_line(others))
    if ran is not None:
        lines.append('rendered  %s' % (', '.join(p for p in rendered if p not in paths)
                                       or 'no rendered file changed'))
    if any(p.endswith('.md') for p in status_after) or locked:
        line, ok = doc_budget_check(root)
        lines.append(line)
        iline, iok = doc_index_check(root)
        if iline:
            lines.append(iline)
        if not (ok and iok):
            lines.append('          fix it in a follow-up commit before this branch can land')
