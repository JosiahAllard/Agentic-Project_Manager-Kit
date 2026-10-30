"""The git pre-push check (ARCHITECTURE.md "Guard rules"; docs/guard.md "Pre-push").

check(root, lines, remote=<name or URL>, url=<URL>) -> (ok, message). `lines` are what git writes
to a pre-push hook's stdin, one `<local ref> <local sha> <remote ref> <remote sha>` per ref pushed.

First, when the repository's git common dir holds kit-pm-data.json (kitlib.pmdata; docs/guard.md
"Pre-push", "PM data branch"), leak_refusal judges every line whatever its remote ref: the data
branch, its history and its objects go only to the recorded private remote, and that remote gets
nothing else. A record that cannot be read, or a recorded root this clone lacks, refuses every push.

Next, secrets_refusal judges every line too, for every remote: a commit the push sends that adds or
changes a file in a top .pm-kit/hub/ folder or .pm-kit/local.json (case ignored) is refused, even
when a later commit deletes it (docs/guard.md "Pre-push", "Owner machine setup").

Then, for a push that updates the remote's main branch, whoever pushes (the owner, an agent, a
lander):
- a push that deletes main is refused;
- a push that is not a fast-forward of the remote's main is refused whatever push_block
  says. A remote main this clone lacks cannot be an ancestor of the pushed commit, so git
  cannot tell: refused too (fail closed). Git passes such a ref to the hook only when the push is
  forced (--force, a + refspec, --force-with-lease);
- then, when the repository sets repo.push_block, the pushed commit needs a gate-OK record, read by
  kitlib.gate.runner.verify_record: it must name a config_rev in the pushed commit's history and
  hash the gate config that commit holds. With a dev_drive only the record folder counts and a
  .git copy is not read; with none (a load node set up without one) the .git copy is the only
  record (kitlib.records.gate_ok.read);
- then, under push_block too, a commit the push adds that changes an owner-only path the push
  changes on main (its net change) and whose author email is not the owner's is refused, record
  or not (kitlib.guard.ownercommits; the base's
  owner rules merged with the pushed commit's): a branch gate records its own tip, so without this
  a gated agent branch could go straight to main.
The first rule that refuses names the reason; the order is the list's.
push_block and the main branch's name are read from the base: the remote main commit being
replaced when this clone has it; else (a new branch, or a remote sha this clone lacks) the default
branch's committed .kit.json: the first of refs/remotes/<remote>/HEAD, <remote>/main,
<remote>/master, local main, local master. The working tree is never the base, so an uncommitted
edit cannot switch the check off; no such ref means the defaults. A .kit.json that does not load
refuses the push (fail closed). A base that sets no main branch (no .kit.json at all among them)
reads it from the repository (kitlib.git.defaults: <remote>/HEAD, then main or master), so the
delete and fast-forward rules guard the real main branch (docs/kit-json.md "With no file"), and
`main` as well (Config.guarded_mains): <remote>/HEAD is local, so repointing it never takes the
protection off `main`.
"""
import hashlib
import os
import re

from kitlib import config as kconfig
from kitlib import kitroot
from kitlib import pmdata
from kitlib.config import schema as kschema
from kitlib.git import cmd, refs
from kitlib.git import defaults as gitdefaults
from kitlib.git import repo as gitrepo
from kitlib.guard import ownercommits

ZERO = '0' * 40
FIXED_MAIN = kschema.SECTIONS['repo']['main_branch'].default


# ---------------------------------------------------------------- PM data branch (leak stage)

_URL_SCHEME = re.compile(r'([A-Za-z][A-Za-z0-9+.-]*)://([^/]*)(.*)\Z', re.S)
_URL_SCP = re.compile(r'([^/@:]+@)?([^/:]{2,}):(.*)\Z', re.S)    # host:path; `D:` is a drive


def _url_path(path):
    path = path.rstrip('/')
    if path.lower().endswith('.git'):
        path = path[:-4]
    return path.rstrip('/')


def _norm_url(url):
    """A remote URL in the form two are compared in (git's rewriting is not done here: the URLs
    compared are the ones git already rewrote). Always: a trailing slash, then a trailing `.git`,
    then a trailing slash dropped. A remote URL (`<scheme>://` other than `file://`, or scp-like
    `[user@]host:path`) has its scheme and host case folded and keeps its path's case, so two
    repositories whose paths differ only in case stay two. A local path (a drive path, a UNC
    path, `file://`, anything else) has backslashes as slashes and is case folded whole, as
    Windows reads it."""
    u = str(url or '').strip()
    m = _URL_SCHEME.match(u)
    if m and m.group(1).lower() != 'file':
        scheme, auth, path = m.groups()
        user, at, host = auth.rpartition('@')
        return '%s://%s%s%s%s' % (scheme.casefold(), user, at, host.casefold(), _url_path(path))
    m = _URL_SCP.match(u)
    if m and not m.group(3).startswith('//'):
        return '%s%s:%s' % (m.group(1) or '', m.group(2).casefold(), _url_path(m.group(3)))
    return _url_path(u.replace('\\', '/')).casefold()


def _config_env():
    """This process's environment for a git child without the configuration a `git -c` on the
    push command line hands its hooks (GIT_CONFIG_PARAMETERS, GIT_CONFIG_COUNT,
    GIT_CONFIG_KEY_<n>, GIT_CONFIG_VALUE_<n>): `git -c remote.<private>.pushurl=<public> push`
    must not make the public URL look like the private remote's."""
    def dropped(k):
        k = k.upper()
        return (k in ('GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT')
                or k.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')))
    return {k: v for k, v in cmd.scrubbed_env().items() if not dropped(k)}


def _get_urls(root, name, env, push):
    """`(set of normalised URLs, None)` from `git remote get-url --all [--push] <name>` (git's own
    answer: pushurl else url, insteadOf and pushInsteadOf applied), an empty set when git answers
    "No such remote" (the raw config values still count), or `(None, why)` for any other failure
    or no answer: a remote whose URLs cannot be read refuses the push."""
    r = cmd.call(['remote', 'get-url', '--all'] + (['--push'] if push else []) + ['--', name], root, env=env)
    if r.rc is None:
        return None, r.err
    if r.rc != 0:
        if 'no such remote' in (r.err or '').lower():
            return set(), None
        return None, r.err or 'git remote get-url exited %s' % r.rc
    return {_norm_url(u) for u in cmd.text(r.out).splitlines() if u.strip()}, None


def _remote_config(root, env):
    """`({remote name: its raw url and pushurl values, normalised}, None)` from the repository's
    configuration as `env` reads it, or `(None, why)`. No remote is `({}, None)`."""
    r = cmd.call(['config', '-z', '--get-regexp', r'^remote\..*\.(url|pushurl)$'], root, env=env)
    if r.rc == 1 and not r.err:
        return {}, None
    if r.rc != 0:
        return None, r.err or 'git config exited %s' % r.rc
    raw = {}
    for entry in cmd.text(r.out).split('\0'):
        if not entry:
            continue
        key, _, value = entry.partition('\n')
        for suffix in ('.url', '.pushurl'):
            if key.startswith('remote.') and key.endswith(suffix) and len(key) > len('remote.' + suffix):
                raw.setdefault(key[len('remote.'):-len(suffix)], set()).add(_norm_url(value))
                break
    return raw, None


def _remote_names(root):
    """`(set of the repository's remote names, None)` (those with a url or pushurl, read as
    _remote_urls reads them), or `(None, why)`."""
    raw, why = _remote_config(root, _config_env())
    return (None, why) if raw is None else (set(raw), None)


def _remote_urls(root):
    """`({remote name: {'push': its push URLs, 'all': every URL it has}}, None)`, normalised, or
    `(None, why)`. Read with _config_env(), so only the repository's own configuration counts.
    'push' is git's answer (`remote get-url --push --all`); 'all' adds the fetch URLs as git
    rewrites them and the raw `url` and `pushurl` values. No remote is `({}, None)`."""
    env = _config_env()
    raw, why = _remote_config(root, env)
    if raw is None:
        return None, why
    out = {}
    for name, urls in raw.items():
        push, why = _get_urls(root, name, env, True)
        fetch, why2 = _get_urls(root, name, env, False) if push is not None else (None, None)
        if push is None or fetch is None:
            return None, 'git remote get-url %s: %s' % (name, why or why2)
        out[name] = {'push': push, 'all': push | fetch | urls}
    return out, None


def _private(record, remotes, name, url):
    """True when the push goes to the recorded private remote, else False (public). `url` is the
    URL git hands the hook (its second argument, after insteadOf and pushInsteadOf; None: not
    known); `name` (its first) does not decide. Private needs both: `url` is one of the record's
    remote's push URLs (_remote_urls 'push'), and no other remote has it among any of its URLs.
    Anything else, unclear cases included, is public: the stricter side for the data branch."""
    own = (remotes.get(record.remote) or {}).get('push')
    if not own or not url:
        return False
    target = _norm_url(url)
    if target not in own:
        return False
    for other, urls in remotes.items():
        if other != record.remote and target in urls['all']:
            return False
    return True


def _empty_objects(root_sha):
    """The empty tree and the empty blob in the object format `root_sha` is in: objects any two
    histories may share, so they never count as PM data."""
    algo = hashlib.sha256 if len(root_sha) == 64 else hashlib.sha1
    return {algo(b'tree 0\0').hexdigest(), algo(b'blob 0\0').hexdigest()}


def _have(root, sha):
    return cmd.call(['cat-file', '-e', sha], root).rc == 0


def _ref_tip(ref, root):
    """`(commit, None)` of `ref`, `('', None)` when it does not exist, or `(None, why)` when git
    cannot say (an error, a timeout): "cannot read" is never "no such ref"."""
    r = cmd.call(['rev-parse', '--verify', '--quiet', '%s^{commit}' % ref], root)
    out = cmd.text(r.out).strip() if r.rc == 0 else ''
    if out:
        return out, None
    if r.rc == 1 and not r.err:
        return '', None
    return None, r.err or 'git rev-parse exited %s' % r.rc


def _data_objects(root, record):
    """`({object: path or ''}, None)`: every object reachable from the recorded root, the local
    data branch and the private remote's tracking ref of it (those that exist), less the empty
    tree and blob; or `(None, why)`, also when one of those refs cannot be read."""
    starts = [record.root]
    for ref in ('refs/heads/%s' % record.branch, 'refs/remotes/%s/%s' % (record.remote, record.branch)):
        sha, why = _ref_tip(ref, root)
        if sha is None:
            return None, 'cannot read %s: %s' % (ref, why)
        if sha and sha not in starts:
            starts.append(sha)
    out, why = cmd.run_text(['rev-list', '--objects'] + starts + ['--'], root)
    if out is None:
        return None, why
    empty = _empty_objects(record.root)
    objects = {}
    for line in out.splitlines():
        sha, _, path = line.partition(' ')
        if refs.is_sha(sha) and sha not in empty:
            objects[sha] = path
    return objects, None


def _pushed_objects(root, local_sha, remote_sha, name, remotes):
    """`(set of objects the push may send, None)` or `(None, why)`: reachable from `local_sha`,
    not from the remote sha it replaces (when this clone has it) nor from the named remote's
    tracking refs. `--remotes=<name>` is a pattern over refs/remotes/<name>/, so it is left out
    (nothing subtracted) when another remote's name starts with `<name>/`, whose tracking refs it
    would match too, or when the name holds a pattern character."""
    args = ['rev-list', '--objects', '--no-object-names', local_sha, '--not']
    if remote_sha.strip('0') and _have(root, remote_sha):
        args.append(remote_sha)
    if _tracking_counts(name, remotes):
        args.append('--remotes=%s' % name)
    out, why = cmd.run_text(args + ['--'], root)
    if out is None:
        return None, why
    return {s for s in out.split() if refs.is_sha(s)}, None


def _tracking_counts(name, remotes):
    """True when the remote `name` (its name, or a URL) is one of `remotes` and its tracking refs,
    refs/remotes/<name>/, hold only its own: left out (nothing subtracted) as _pushed_objects says."""
    nested = any(other.startswith(name + '/') for other in remotes)
    return name in remotes and not nested and not refs.bad_rev(name) and not set('*?[\\') & set(name)


MAX_TRACKING_ARGS = 500                 # more tracking tips than this: `--remotes=<name>` instead


def _remote_has(root, remote_sha, name, remotes):
    """`(revisions, None)` a push to the remote `name` (its name, or a URL) does not send, as git
    arguments after `--not`, or `(None, why)`: the remote sha it replaces when this clone has it,
    and the tips of the remote's tracking refs when they count (_tracking_counts). The tips are
    named one by one (for-each-ref) up to MAX_TRACKING_ARGS, past that by `--remotes=<name>`, which
    git reads the same way, so a long list never outgrows the command line."""
    out = []
    if remote_sha.strip('0') and _have(root, remote_sha):
        out.append(remote_sha)
    if not _tracking_counts(name, remotes):
        return out, None
    tips, why = cmd.run_text(['for-each-ref', '--format=%(objectname)', 'refs/remotes/%s/' % name], root)
    if tips is None:
        return None, why
    tips = sorted({t for t in tips.split() if refs.is_sha(t)})
    return out + (tips if len(tips) <= MAX_TRACKING_ARGS else ['--remotes=%s' % name]), None


# ---------------------------------------------------------------- machine secrets

SECRET_FILE = '.pm-kit/local.json'      # a checkout's machine settings (docs/guard.md "Owner machine setup")
SECRET_FOLDER = '.pm-kit/hub/'          # the hub's token folder there (docs/guard.md "Hub secrets")


def is_secret_path(path):
    """True when the repository path `path` (as git log prints it, from the top) is the machine
    config folder's local.json or lies in its hub token folder, the case ignored."""
    p = path.strip()
    if p.startswith('"'):
        p = p[1:]                       # a quoted name (core.quotepath): its start is plain text
    p = p.replace('\\', '/').casefold()
    return p == SECRET_FILE or p.startswith(SECRET_FOLDER)


def secrets_refusal(root, lines, name):
    """Why this push is refused because a commit it sends adds or changes a file of a top
    .pm-kit/hub/ folder or .pm-kit/local.json (is_secret_path), or None. Every pushed ref counts,
    whatever its remote; a commit the remote already has (_remote_has) does not. A file a later
    commit deletes still counts: its content is in the commit that added it. `name` is git's first
    hook argument (the remote's name, or the URL for a push by URL). A deletion, and a local sha
    this clone lacks, send nothing; when git cannot list the commits, the push is refused."""
    remotes = None
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if not local_sha.strip('0') or not _have(root, local_sha):
            continue                                # a deletion, or a commit this clone lacks, sends nothing
        if remotes is None:
            remotes, why = _remote_names(root)
            if remotes is None:
                return ('pre-push: refused, cannot read the remotes to check what %s sends for this '
                        'machine\'s secrets (%s).' % (local_ref, why))
        what = '%s (%s)' % (local_ref, local_sha[:12])
        has, why = _remote_has(root, remote_sha, name, remotes)
        out = None
        if has is not None:
            out, why = cmd.run_text(['-c', 'core.quotepath=off', 'log', '-m', '--no-renames', '--name-only',
                                     '--diff-filter=ACMRT', '--format=commit %H', local_sha, '--not'] +
                                    has + ['--'], root)
        if out is None:
            return ('pre-push: refused, cannot list the files %s sends to check them for this '
                    'machine\'s secrets (%s).' % (what, why))
        commit = ''
        for row in out.splitlines():
            if row.startswith('commit '):
                commit = row[len('commit '):].strip()
            elif row.strip() and is_secret_path(row):
                return ('pre-push: refused, %s sends %s, added or changed in commit %s: a checkout\'s '
                        '.pm-kit/hub/ folder (the Action Hub\'s tokens) and .pm-kit/local.json (this '
                        'machine\'s settings) never leave this machine (docs/guard.md "Pre-push"). '
                        'Take the file out of those commits, then push again.'
                        % (what, row.strip(), commit[:12]))
    return None


def leak_refusal(root, lines, name, url=None):
    """Why this push is refused to keep PM data off a public remote, or None. Runs only when the
    git common dir of `root` holds kit-pm-data.json; no record, no change. `name` is git's first
    hook argument (the remote's name, or the URL for a push by URL), `url` its second."""
    common, why = gitrepo.common_dir(root)
    if common is None:
        return ('pre-push: refused, cannot find the git directory to look for %s (%s).'
                % (pmdata.FILE_NAME, why))
    try:
        record = pmdata.read(common)
    except pmdata.PmDataError as e:
        return ('pre-push: refused, the PM data record cannot be read: %s. Every push is refused '
                'until it is fixed.' % e)
    if record is None:
        return None
    path = pmdata.record_path(common)
    if not _have(root, '%s^{commit}' % record.root):
        return ('pre-push: refused, the PM data root commit %s that %s names is not in this clone, '
                'so no push can be checked against it. Every push is refused until it is.'
                % (record.root[:12], path))
    remotes, why = _remote_urls(root)
    if remotes is None:
        return ('pre-push: refused, cannot read the remotes to tell whether %s is the private '
                'remote %s (%s).' % (name, record.remote, why))
    private = _private(record, remotes, name, url)
    data_ref = 'refs/heads/' + record.branch
    data = []                                       # computed once, for the first line that needs it
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        deleting = not local_sha.strip('0')
        if private:
            if deleting:
                return ('pre-push: refused, this push deletes %s on %s, the private remote for PM '
                        'data, which is never deleted from.' % (remote_ref, name))
            if remote_ref != data_ref:
                return ('pre-push: refused, %s is the private remote for PM data and receives only '
                        '%s, not %s.' % (name, data_ref, remote_ref))
            held, why = refs.is_ancestor(record.root, local_sha, root)
            if held is not True:
                return ('pre-push: refused, %s (%s) %s the PM data root commit %s, and the private '
                        'remote %s receives only the data branch\'s history.'
                        % (local_ref, local_sha[:12],
                           'does not hold' if held is False else 'cannot be checked for (%s)' % why,
                           record.root[:12], name))
            continue
        if deleting:
            continue                                # sends nothing; deleting a leaked data branch is cleanup
        what = '%s (%s)' % (local_ref, local_sha[:12])
        if data_ref in (local_ref, remote_ref):
            return ('pre-push: refused, %s pushes the PM data branch %s to %s, which is not its '
                    'private remote %s. Push it only with: git push %s %s:%s'
                    % (what, record.branch, name, record.remote, record.remote, data_ref, data_ref))
        held, why = refs.is_ancestor(record.root, local_sha, root)
        if held is not False:
            return ('pre-push: refused, %s %s the PM data root commit %s, and %s is not the private '
                    'remote %s. Rebuild it without the data branch\'s commits.'
                    % (what, 'holds' if held else 'cannot be checked for (%s)' % why,
                       record.root[:12], name, record.remote))
        pushed, why = _pushed_objects(root, local_sha, remote_sha, name, remotes)
        if pushed is None:
            return ('pre-push: refused, cannot list the objects %s pushes to check them against '
                    'the PM data branch (%s).' % (what, why))
        if not pushed:
            continue
        if not data:
            objects, why = _data_objects(root, record)
            if objects is None:
                return ('pre-push: refused, cannot list the PM data branch\'s objects to check %s '
                        'against them (%s).' % (what, why))
            data.append(objects)
        shared = sorted(pushed & set(data[0]))
        if shared:
            named = [s for s in shared if data[0][s]]
            first = named[0] if named else shared[0]
            label = '%s %s' % (first[:12], data[0][first]) if data[0][first] else first[:12]
            return ('pre-push: refused, %s sends %d object(s) the PM data branch holds (%s) to %s, '
                    'which is not its private remote %s: a data commit copied in (cherry-pick, '
                    'checkout of its files). A file whose content is identical to a PM data file '
                    'matches too; change one of them if that is the case.'
                    % (what, len(shared), label, name, record.remote))
    return None


def _base_rev(root, remote_sha, remote):
    if remote_sha and remote_sha.strip('0'):
        have, _ = refs.resolve(remote_sha, root)
        if have:
            return have
    names = ['refs/heads/main', 'refs/heads/master']
    if remote and not refs.bad_rev(remote):
        names = ['refs/remotes/%s/%s' % (remote, b) for b in ('HEAD', 'main', 'master')] + names
    for name in names:
        have, _ = refs.resolve(name, root)
        if have:
            return have
    return None


def _default_mains(root, remote):
    """The branches held as main in a repository with no base commit: the main branch read from
    its git files with the remote git named (an URL names none: the default remote's), and
    `main` (kconfig.Config.guarded_mains)."""
    name = remote if remote and not refs.bad_rev(remote) and '://' not in remote and ':' not in remote \
        else kschema.SECTIONS['repo']['remote'].default
    main = gitdefaults.main_branch(root, name)
    return [main] if main == FIXED_MAIN else [main, FIXED_MAIN]


def _cfg(root, remote_sha, remote):
    """The base Config, or None when there is no base commit at all (the defaults: no push_block)."""
    rev = _base_rev(root, remote_sha, remote)
    return None if rev is None else kconfig.load_at(root, rev)


def verify_record(root, sha):
    """kitlib.gate.runner.verify_record, imported only when a push to main needs it (its imports
    load the Windows process modules); any failure is `(None, why)`, so the push is refused."""
    try:
        from kitlib.gate import runner
        return runner.verify_record(root, sha)
    except Exception as e:
        return None, 'cannot read gate-OK records: %s: %s' % (type(e).__name__, e)


def _exclude(root, cfg, remote_sha, remote):
    """The commits a push to main does not add: the remote main it replaces when this clone has it,
    else <remote>/<main> as last fetched, else none (every commit is judged). Never other
    remote-tracking refs: a pushed agent branch would hide its own commits."""
    if remote_sha and remote_sha.strip('0'):
        have, _ = refs.resolve(remote_sha, root)
        if have:
            return [have]
    if remote and not refs.bad_rev(remote):
        have, _ = refs.resolve('refs/remotes/%s/%s' % (remote, cfg.main_branch), root)
        if have:
            return [have]
    return []


def owner_refusal(root, cfg, local_sha, remote_sha, remote):
    """None when every commit the push adds to main that changes an owner-only path is the
    owner's (by author email), counting only the paths the push changes on main; else why not."""
    try:
        work = kconfig.load_at(root, local_sha).owner_rules()
    except kconfig.ConfigError:
        work = kconfig.OwnerRules()
    rules = kconfig.merge_owner_rules(work, cfg.owner_rules())
    bad, why = ownercommits.violations(root, rules, local_sha, _exclude(root, cfg, remote_sha, remote))
    if bad is None:
        return 'cannot check owner-only paths (%s)' % why
    return ownercommits.describe(rules, bad) if bad else None


def not_fast_forward(root, local_sha, remote_sha, main):
    """Why moving the remote's `main` from `remote_sha` to `local_sha` is refused, or None
    for a fast-forward (a creation, an all-zero `remote_sha`, is one)."""
    ff, why = refs.is_fast_forward(remote_sha, local_sha, root)
    if ff:
        return None
    if ff is None:
        return ('pre-push: refused, cannot tell whether %s is a fast-forward of %s %s (%s). '
                'Fetch, merge %s into it, then push again; never force-push %s.'
                % (local_sha[:12], main, remote_sha[:12], why.strip(), main, main))
    return ('pre-push: refused, %s is not a fast-forward of %s %s. Merge %s into it, then '
            'push again; never force-push %s.' % (local_sha[:12], main, remote_sha[:12], main, main))


def check(root, lines, gate_ok=None, remote='origin', url=None):
    gate_ok = gate_ok or verify_record
    why = leak_refusal(root, lines, remote, url) or secrets_refusal(root, lines, remote)
    if why:
        return False, why
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if not remote_ref.startswith('refs/heads/'):
            continue
        try:
            cfg = _cfg(root, remote_sha, remote)
        except kconfig.ConfigError as e:
            return False, ('pre-push: refused, .kit.json does not load (%s). Fix it, then push again.' % e)
        mains = _default_mains(root, remote) if cfg is None else cfg.guarded_mains()
        main = remote_ref[len('refs/heads/'):]
        if main not in mains:
            continue
        if not local_sha.strip('0'):
            return False, 'pre-push: refused, this push deletes %s.' % main
        why = not_fast_forward(root, local_sha, remote_sha, main)
        if why:
            return False, why
        if cfg is None or not cfg.push_block:
            continue
        rec, why = gate_ok(root, local_sha)
        if rec is None:
            return False, ('pre-push: refused, %s has no gate-OK record (%s). Land with: python "%s" -C "%s" '
                           '<branch> --expect-tip %s'
                           % (local_sha[:12], why, kitroot.tool('land').replace('\\', '/'),
                              (gitrepo.main_checkout_toplevel(root) or root).replace('\\', '/'),
                              local_sha))
        why = owner_refusal(root, cfg, local_sha, remote_sha, remote)
        if why:
            return False, 'pre-push: refused, %s.' % why
    return True, None


def main(argv, stdin, root=None):
    """The hook body: (exit code, message or None). argv is git's: the remote's name (its URL for a
    push by URL) and the URL."""
    root = root or os.getcwd()
    remote = argv[0] if argv else 'origin'
    url = argv[1] if len(argv) > 1 else None
    ok, msg = check(root, stdin.read().splitlines(), remote=remote, url=url)
    return (0, None) if ok else (1, msg)
