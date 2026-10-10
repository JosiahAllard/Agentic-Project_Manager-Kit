"""The git pre-push check (ARCHITECTURE.md "Guard rules"; docs/guard.md "Pre-push").

check(root, lines, remote=<name or URL>, url=<URL>) -> (ok, message). `lines` are what git writes
to a pre-push hook's stdin, one `<local ref> <local sha> <remote ref> <remote sha>` per ref pushed.

First, when the repository's git common dir holds kit-pm-data.json (kitlib.pmdata; docs/guard.md
"Pre-push", "PM data branch"), leak_refusal judges every line whatever its remote ref: the data
branch, its history and its objects go only to the recorded private remote, and that remote gets
nothing else. A record that cannot be read, or a recorded root this clone lacks, refuses every push.

Then, for a push that updates the remote's main branch, whoever pushes (the owner, an agent, a
lander):
- a push that deletes main is refused;
- a push that is not a fast-forward of the remote's main is refused whatever push_block
  says. A remote main this clone lacks cannot be an ancestor of the pushed commit, so git
  cannot tell: refused too (fail closed). Git passes such a ref to the hook only when the push is
  forced (--force, a + refspec, --force-with-lease);
- then, when the repository sets repo.push_block, the pushed commit needs a gate-OK record, read by
  kitlib.gate.runner.verify_record: it must name a config_rev in the pushed commit's history and
  hash the gate config that commit holds;
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
refuses the push (fail closed).
"""
import hashlib
import os

from kitlib import config as kconfig
from kitlib import pmdata
from kitlib.config import schema as kschema
from kitlib.git import cmd, refs
from kitlib.git import repo as gitrepo
from kitlib.guard import ownercommits

ZERO = '0' * 40
MAIN_DEFAULT = kschema.SECTIONS['repo']['main_branch'].default


# ---------------------------------------------------------------- PM data branch (leak stage)

def _norm_url(url):
    """A remote URL in the form two are compared in: backslashes as slashes, a trailing slash
    then a trailing `.git` dropped, case folded. Not git's own rewriting (insteadOf): a URL that
    only matches after it is not matched, so it counts as public."""
    u = str(url or '').strip().replace('\\', '/').rstrip('/')
    if u.lower().endswith('.git'):
        u = u[:-4]
    return u.rstrip('/').casefold()


def _remote_urls(root):
    """`({remote name: {normalised url and pushurl}}, None)` from the repository's config, or
    `(None, why)`. One `git config` read; no remote is `({}, None)`."""
    r = cmd.call(['config', '-z', '--get-regexp', r'^remote\..*\.(url|pushurl)$'], root)
    if r.rc == 1 and not r.err:
        return {}, None
    if r.rc != 0:
        return None, r.err or 'git config exited %s' % r.rc
    out = {}
    for entry in cmd.text(r.out).split('\0'):
        if not entry:
            continue
        key, _, value = entry.partition('\n')
        for suffix in ('.url', '.pushurl'):
            if key.startswith('remote.') and key.endswith(suffix) and len(key) > len('remote.' + suffix):
                out.setdefault(key[len('remote.'):-len(suffix)], set()).add(_norm_url(value))
                break
    return out, None


def _private(record, remotes, name, url):
    """True when a push to remote `name` at `url` (git's two hook arguments; `url` None: not
    known) goes to the recorded private remote, else False (public). Private needs all of: the
    record's remote is configured; `name` is it, or `url` is one of its URLs; and the URL pushed
    to (else each of the private remote's own URLs) is no other remote's URL. Anything else,
    unclear cases included, is public: the stricter side for the data branch."""
    own = remotes.get(record.remote)
    if not own:
        return False
    target = _norm_url(url) if url else None
    if name != record.remote and (target is None or target not in own):
        return False
    others = set()
    for other, urls in remotes.items():
        if other != record.remote:
            others |= urls
    return not ({target} if target is not None else own) & others


def _empty_objects(root_sha):
    """The empty tree and the empty blob in the object format `root_sha` is in: objects any two
    histories may share, so they never count as PM data."""
    algo = hashlib.sha256 if len(root_sha) == 64 else hashlib.sha1
    return {algo(b'tree 0\0').hexdigest(), algo(b'blob 0\0').hexdigest()}


def _have(root, sha):
    return cmd.call(['cat-file', '-e', sha], root).rc == 0


def _data_objects(root, record):
    """`({object: path or ''}, None)`: every object reachable from the recorded root, the local
    data branch and the private remote's tracking ref of it (those that exist), less the empty
    tree and blob; or `(None, why)`."""
    starts = [record.root]
    for ref in ('refs/heads/%s' % record.branch, 'refs/remotes/%s/%s' % (record.remote, record.branch)):
        sha, _why = refs.resolve(ref, root)
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
    tracking refs."""
    args = ['rev-list', '--objects', '--no-object-names', local_sha, '--not']
    if remote_sha.strip('0') and _have(root, remote_sha):
        args.append(remote_sha)
    if name in remotes and not refs.bad_rev(name):
        args.append('--remotes=%s' % name)
    out, why = cmd.run_text(args + ['--'], root)
    if out is None:
        return None, why
    return {s for s in out.split() if refs.is_sha(s)}, None


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
    why = leak_refusal(root, lines, remote, url)
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
        main = MAIN_DEFAULT if cfg is None else cfg.main_branch
        if remote_ref != 'refs/heads/' + main:
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
            return False, ('pre-push: refused, %s has no gate-OK record (%s). Land with: python %s/land.py '
                           '<branch> --expect-main <%s sha> --expect-tip %s'
                           % (local_sha[:12], why, cfg.tools_dir, main, local_sha))
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
