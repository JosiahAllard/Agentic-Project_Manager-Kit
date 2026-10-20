"""The fake git against real git, command by command (docs/test-tools.md "Fake git").

Each scenario starts from two copies of one fixture repository (tests/gitfixture.py: the same
commit, refs, config and bare remote) and runs the same script on both: on copy A every git
command is answered by kitlib.git.fake in this process, on copy B by real git. Author and
committer dates are pinned, so the commits the two sides make have the same ids.

Per command it compares the exit code, stdout byte for byte (each side's own temp folder written
as <BASE>), and whether stderr carries a `fatal:` or `error:` line. Other stderr (hints,
"Switched to branch") is not compared: the kit reads git's stdout and exit code, and those two
words of stderr. A command the fake raises Unsupported for is a difference too. Then real git
reads both repositories' end state (refs, HEAD, index, status, the remote's refs), and
`git fsck` must accept what the fake wrote.

Every difference is collected before the test fails, each named by its step number and argv.
The commands are the forms the kit's code and fixtures send to git."""
import os
import subprocess
import unittest

from kitlib.git import cmd as gitcmd
from kitlib.git import fake
from kitlib.git.fake.errors import Unsupported
from tests import gitfixture
from tests.realgit import real_git

DATES = {'GIT_AUTHOR_DATE': '1700000000 +0000', 'GIT_COMMITTER_DATE': '1700000000 +0000'}
WT = '<WT>'         # a linked worktree's path in a script: <base>/wt
WT2 = '<WT2>'       # a second one: <base>/wt2


def _alarm(err):
    return any(line.startswith(('fatal:', 'error:')) for line in err.splitlines())


class Side(object):
    """One copy of the fixture and how its git commands are answered."""

    def __init__(self, use_fake):
        self.use_fake = use_fake
        self.repo = gitfixture.TempRepo(bare_remote=True)
        # English messages on both sides, as kitlib.git.cmd gives every git it starts: the
        # fatal:/error: comparison and any message text must not depend on the machine's locale.
        self.env = gitfixture.fixture_env(dict(DATES, **gitcmd.LOCALE_ENV))

    def cleanup(self):
        self.repo.cleanup()

    def _arg(self, a):
        return a.replace(WT2, os.path.join(self.repo.base, 'wt2')).replace(
            WT, os.path.join(self.repo.base, 'wt'))

    def git(self, argv, cwd=None, stdin=None):
        """(rc, stdout bytes, stderr alarm) of `git <argv>`; rc 'UNSUPPORTED' when the fake
        raised it."""
        argv = [self._arg(a) for a in argv]
        cwd = self._arg(cwd) if cwd else self.repo.root
        if self.use_fake:
            try:
                rc, out, err = fake.run(argv, cwd, stdin, self.env)
            except Unsupported as e:
                return 'UNSUPPORTED: %s' % e, b'', True
        else:
            r = subprocess.run(['git'] + argv, cwd=cwd, input=stdin, env=self.env,
                               capture_output=True)
            rc, out, err = r.returncode, r.stdout, r.stderr.decode('utf-8', 'replace')
        return rc, self.normal(out), _alarm(err)

    def normal(self, out):
        base = self.repo.base
        for form in (base, base.replace('\\', '/')):
            for f in (form, form.lower(), form.upper()[:1] + form[1:]):
                out = out.replace(f.encode('utf-8'), b'<BASE>')
        return out

    def real(self, argv, cwd=None):
        """Real git's answer about this side, whichever side it is (the end-state reads)."""
        r = subprocess.run(['git'] + argv, cwd=cwd or self.repo.root, env=self.env,
                           capture_output=True)
        return r.returncode, self.normal(r.stdout)


# A script is a list of steps: ('git', argv[, cwd]) or ('write', relpath, text[, cwd]).
BRANCH_AND_LAND = [
    ('write', 'a.txt', 'a\n'),
    ('git', ['add', '--', 'a.txt']),
    ('git', ['commit', '-q', '-m', 'a']),
    ('git', ['rev-parse', 'HEAD']),
    ('git', ['rev-parse', '--abbrev-ref', 'HEAD']),
    ('git', ['rev-parse', '--show-toplevel']),
    ('git', ['rev-parse', '--git-common-dir']),
    ('git', ['rev-parse', '--git-dir']),
    ('git', ['rev-parse', '--verify', '-q', 'refs/heads/nope']),
    ('git', ['rev-parse', '--verify', 'HEAD~1']),
    ('git', ['rev-parse', '--verify', '--quiet', 'HEAD^{commit}']),
    ('git', ['rev-parse', 'HEAD^{tree}']),
    ('git', ['symbolic-ref', 'HEAD']),
    ('git', ['symbolic-ref', '--short', 'HEAD']),
    ('git', ['branch', '--show-current']),
    ('write', 'b.txt', 'b\n'),
    ('write', 'README.md', 'hello\nchanged\n'),
    ('write', 'sub/c.txt', 'c\n'),
    ('git', ['status', '--porcelain']),
    ('git', ['status', '--porcelain=v1', '-z']),
    ('git', ['status', '--porcelain=v1', '--untracked-files=all']),
    ('git', ['status', '--porcelain=v1', '-uno']),
    ('git', ['diff', '--name-only']),
    ('git', ['diff', '--name-status', 'HEAD']),
    ('git', ['diff', 'HEAD', '--', 'README.md']),
    ('git', ['ls-files', '--others', '--exclude-standard']),
    ('git', ['add', '--', 'README.md', 'b.txt']),
    ('git', ['diff', '--cached', '--name-only']),
    ('git', ['diff', '--cached', '--name-status']),
    ('git', ['ls-files', '-s']),
    ('git', ['commit', '-q', '-m', 'b and readme']),
    ('git', ['switch', '-q', '-c', 'kit/x']),
    ('git', ['add', '--', 'sub/c.txt']),
    ('git', ['commit', '-q', '-m', 'c']),
    ('git', ['log', '--format=%H%x00%P%x00%s', '-n', '5']),
    ('git', ['log', '--format=%an <%ae> %at', '-1']),
    ('git', ['rev-list', '--count', 'main..kit/x']),
    ('git', ['rev-list', 'main..kit/x']),
    ('git', ['rev-list', '--count', 'kit/x', '--not', '--branches', '--remotes', '--tags']),
    ('git', ['rev-list', '--count', 'kit/x', '--not', '--remotes']),
    ('git', ['rev-list', '--count', '--remotes', '--not', 'main']),
    ('git', ['merge-base', 'main', 'kit/x']),
    ('git', ['merge-base', '--is-ancestor', 'main', 'kit/x']),
    ('git', ['merge-base', '--is-ancestor', 'kit/x', 'main']),
    ('git', ['for-each-ref', '--format=%(refname)%00%(objectname)', 'refs/heads']),
    ('git', ['for-each-ref', '--format=%(refname:short) %(upstream:short)', 'refs/heads/']),
    ('git', ['ls-tree', '-r', 'HEAD']),
    ('git', ['ls-tree', '--name-only', 'HEAD']),
    ('git', ['cat-file', '-t', 'HEAD']),
    ('git', ['cat-file', '-p', 'HEAD^{tree}']),
    ('git', ['cat-file', '-e', 'HEAD:sub/c.txt']),
    ('git', ['show', 'HEAD:sub/c.txt']),
    ('git', ['show', 'main:sub/c.txt']),
    ('git', ['diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD']),
    ('git', ['diff', '--name-status', 'main...kit/x']),
    ('git', ['check-ref-format', '--branch', 'kit/x']),
    ('git', ['check-ref-format', 'refs/heads/bad..name']),
    ('git', ['switch', '-q', 'main']),
    ('git', ['merge', '--no-ff', '-q', '-m', 'merge kit/x', 'kit/x']),
    ('git', ['log', '--format=%H %P', '-1']),
    ('git', ['merge', '--ff-only', '-q', 'kit/x']),
    ('git', ['push', '-q', 'origin', 'main']),
    ('git', ['ls-remote', 'origin']),
    ('git', ['fetch', '-q', 'origin']),
    ('git', ['rev-parse', 'origin/main']),
    ('git', ['branch', '-d', 'kit/x']),
    ('git', ['branch', '-d', 'kit/nope']),
    ('git', ['update-ref', 'refs/heads/tmp', 'HEAD']),
    ('git', ['update-ref', '-d', 'refs/heads/tmp']),
    ('git', ['tag', 't1']),
    ('git', ['rev-parse', 't1']),
    ('git', ['config', '--get', 'user.name']),
    ('git', ['config', '--get', 'kit.nope']),
    ('git', ['var', 'GIT_AUTHOR_IDENT']),
]

WORKTREES_AND_CONFLICTS = [
    ('git', ['worktree', 'add', '-q', WT, '-b', 'kit/wt']),
    ('git', ['worktree', 'list', '--porcelain']),
    ('git', ['rev-parse', '--show-toplevel'], WT),
    ('git', ['rev-parse', '--path-format=absolute', '--git-common-dir'], WT),
    ('git', ['rev-parse', '--path-format=absolute', '--git-dir', '--git-common-dir',
             '--show-toplevel'], WT),
    ('git', ['branch', '--show-current'], WT),
    ('write', 'README.md', 'hello\nfrom wt\n', WT),
    ('git', ['commit', '-q', '-a', '-m', 'wt edit'], WT),
    ('git', ['status', '--porcelain=v1'], WT),
    ('write', 'README.md', 'hello\nfrom main\n'),
    ('git', ['commit', '-q', '-a', '-m', 'main edit']),
    ('git', ['merge-tree', '--write-tree', '--name-only', '--no-messages', '-z', 'main',
             'kit/wt']),
    ('git', ['merge', '-q', '-m', 'conflict', 'kit/wt']),
    ('git', ['status', '--porcelain=v1']),
    ('git', ['ls-files', '-s']),
    ('git', ['diff', '--name-only', '--diff-filter=U']),
    ('git', ['reset', '-q', '--hard', 'HEAD']),
    ('git', ['status', '--porcelain=v1']),
    ('git', ['worktree', 'remove', WT]),
    ('git', ['worktree', 'list', '--porcelain']),
    ('git', ['worktree', 'prune']),
    ('git', ['push', '-q', 'origin', 'main']),
    ('git', ['push', '-q', 'origin', 'kit/wt']),
    ('git', ['ls-remote', '--heads', 'origin']),
    ('git', ['revert', '--no-edit', 'HEAD']),
    ('git', ['log', '--format=%s', '-n', '3']),
    # A lease that expects the remote main absent is stale: rejected, the remote left alone.
    ('git', ['push', '-q', '--force-with-lease=refs/heads/main:%s' % ('0' * 40), 'origin',
             'HEAD:refs/heads/main']),
    # kitlib/gate/cleanup.py's branch delete: a lease on the tip it saw.
    ('git', ['push', '--quiet', '--force-with-lease=refs/heads/kit/wt:kit/wt', 'origin',
             ':refs/heads/kit/wt']),
    ('git', ['ls-remote', '--heads', 'origin']),
    ('git', ['mv', 'README.md', 'READ.md']),
    ('git', ['rm', '-q', '--cached', 'a.txt']),
    ('git', ['status', '--porcelain=v1']),
]

# extensions.worktreeConfig (set by the Claude desktop app in the kit's own repository): each
# worktree's config.worktree is a layer after the repository config and before -c.
_LINKED_WT_CONFIG = '.git/worktrees/wt/config.worktree'
_KIT_KEYS = ['config', '--get-regexp', r'^kit\.']
WORKTREE_CONFIG = [
    ('git', ['config', 'kit.scope', 'local']),
    ('git', ['worktree', 'add', '-q', WT, '-b', 'kit/wt']),
    ('write', _LINKED_WT_CONFIG, '[kit]\n\tscope = linked\n\tonly = linked-only\n'),
    # extension off: config.worktree is not read
    ('git', ['config', '--get', 'kit.scope'], WT),
    ('git', ['config', '--get', 'kit.only'], WT),
    ('git', _KIT_KEYS, WT),
    ('git', ['config', 'extensions.worktreeConfig', 'true']),
    # extension on, no config.worktree anywhere but the linked one (worktree add copies none)
    ('git', ['worktree', 'add', '-q', WT2, '-b', 'kit/wt2']),
    ('git', ['config', '--get', 'kit.scope'], WT2),
    ('git', ['config', '--get', 'kit.only'], WT2),
    ('git', _KIT_KEYS, WT2),
    ('git', ['config', '--get', 'kit.scope']),
    # extension on, the linked worktree's file overrides the local key
    ('git', ['config', '--get', 'kit.scope'], WT),
    ('git', ['config', 'kit.scope'], WT),
    ('git', ['config', '--get-all', 'kit.scope'], WT),
    ('git', ['config', '--get', 'kit.only'], WT),
    ('git', _KIT_KEYS, WT),
    ('git', ['config', '--get-regexp', r'^kit\.', 'link'], WT),
    ('git', ['config', '--local', '--get', 'kit.scope'], WT),
    ('git', ['config', '--local', '--get', 'kit.only'], WT),
    ('git', ['-c', 'kit.scope=command', 'config', '--get', 'kit.scope'], WT),
    # a plain write still goes to the repository config, under the worktree layer
    ('git', ['config', 'kit.scope', 'local2'], WT),
    ('git', ['config', '--get-all', 'kit.scope'], WT),
    ('git', ['config', '--get', 'kit.scope']),
    # the main worktree's file, in the common dir, read by the main worktree alone
    ('write', '.git/config.worktree', '[kit]\n\tscope = main\n\tmain = yes\n'),
    ('git', ['config', '--get', 'kit.scope']),
    ('git', ['config', '--bool', '--get', 'kit.main']),
    ('git', _KIT_KEYS),
    ('git', ['config', '--get', 'kit.main'], WT),
    ('git', ['config', '--get', 'kit.main'], WT2),
    # format version 1 accepts the extension too
    ('git', ['config', 'core.repositoryformatversion', '1']),
    ('git', ['config', '--get', 'kit.scope'], WT),
    ('git', ['status', '--porcelain=v1'], WT),
    ('git', ['config', 'core.repositoryformatversion', '0']),
    # extension off again: the files stay, unread
    ('git', ['config', 'extensions.worktreeConfig', 'false']),
    ('git', ['config', '--get', 'kit.scope']),
    ('git', ['config', '--get', 'kit.main']),
    ('git', ['config', '--get', 'kit.scope'], WT),
]

# Real git reads both repositories after a script.
END_STATE = [
    ['for-each-ref', '--format=%(refname) %(objectname)'],
    ['rev-parse', 'HEAD'],
    ['symbolic-ref', '-q', 'HEAD'],
    ['ls-files', '-s'],
    ['status', '--porcelain=v1', '--untracked-files=all'],
    ['worktree', 'list', '--porcelain'],
]


@real_git('differential: the fake answers next to real git, on two copies of one fixture')
class FakeAgainstRealGit(unittest.TestCase):

    def run_script(self, script):
        sides = []
        for use_fake in (True, False):          # each cleanup registered as soon as it exists
            s = Side(use_fake)
            self.addCleanup(s.cleanup)
            sides.append(s)
        a, b = sides
        self.assertEqual(a.repo.head(), b.repo.head())
        diffs = []
        for n, step in enumerate(script, 1):
            if step[0] == 'write':
                for s in sides:
                    s.repo.write(step[1], step[2],
                                 root=s._arg(step[3]) if len(step) > 3 else None)
                continue
            argv, cwd = step[1], step[2] if len(step) > 2 else None
            got = [s.git(argv, cwd) for s in sides]
            if got[0] != got[1]:
                diffs.append('step %d git %s%s:\n  fake: %r\n  real: %r' % (
                    n, ' '.join(argv), ' (in %s)' % cwd if cwd else '', got[0], got[1]))
        for argv in END_STATE:
            ends = [s.real(argv) for s in sides]
            if ends[0] != ends[1]:
                diffs.append('end state git %s:\n  fake: %r\n  real: %r' % (
                    ' '.join(argv), ends[0], ends[1]))
        ends = [s.real(['for-each-ref', '--format=%(refname) %(objectname)'], cwd=s.repo.remote)
                for s in sides]
        if ends[0] != ends[1]:
            diffs.append('end state of the bare remote:\n  fake: %r\n  real: %r' % tuple(ends))
        rc, out = a.real(['fsck', '--no-dangling', '--no-progress'])
        if rc != 0:
            diffs.append('git fsck of the fake side: exit %d %r' % (rc, out))
        self.assertEqual(diffs, [], '\n' + '\n'.join(diffs))

    def test_branch_commit_merge_and_push(self):
        self.run_script(BRANCH_AND_LAND)

    def test_worktrees_conflicts_and_lease(self):
        self.run_script(BRANCH_AND_LAND[:3] + WORKTREES_AND_CONFLICTS)

    def test_worktree_config(self):
        self.run_script(BRANCH_AND_LAND[:3] + WORKTREE_CONFIG)


if __name__ == '__main__':
    unittest.main()
