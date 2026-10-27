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


def _file_bytes(path):
    try:
        with open(path, 'rb') as f:
            return f.read()
    except FileNotFoundError:
        return None


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


# A script is a list of steps: ('git', argv[, cwd]), ('write', relpath, text[, cwd]),
# ('bytes', relpath, data) (the file's exact bytes), ('mtime', relpath, ns) (the file's mtime),
# ('setup', argv) (real git on both sides: a state the fake cannot reach itself) or
# ('read', relpath) (the two sides' file bytes compared, None when it is absent).
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

# A root .gitattributes that sets text and eol (the kit's own file is the first three lines):
# hashing follows the work tree's file, a checkout writes under the result index's.
_KIT_ATTRS = '* text=auto eol=lf\n*.cmd text eol=crlf\n*.bat text eol=crlf\n'
_OLD = 1600000000 * 10 ** 9
_ATTR_FILES = ['crlf.txt', 'eq.txt', 'tool.cmd', 'mixed.bat', 'bin.dat', 'lonecr.txt']
ATTRIBUTES = [
    ('git', ['switch', '-q', '-c', 'kit/attr']),
    ('write', '.gitattributes', _KIT_ATTRS),
    ('bytes', 'crlf.txt', b'one\r\ntwo\r\n'),
    ('bytes', 'eq.txt', b'a\r\nb\n'),
    ('bytes', 'tool.cmd', b'@echo off\nexit /b 0\n'),
    ('bytes', 'mixed.bat', b'a\r\nb\nc'),
    ('bytes', 'bin.dat', b'\x00\x01\r\n'),
    ('bytes', 'lonecr.txt', b'a\rb\r\n'),
    ('git', ['status', '--porcelain=v1', '--untracked-files=all']),
    ('git', ['hash-object'] + _ATTR_FILES),
    ('git', ['add', '--', '.gitattributes'] + _ATTR_FILES),
    ('git', ['ls-files', '-s']),
    ('git', ['status', '--porcelain=v1']),
    ('git', ['diff', '--name-only']),
    ('git', ['commit', '-q', '-m', 'attributes']),
    # a checkout writes each file in its own line ending
    ('bytes', 'tool.cmd', b'changed\n'),
    ('git', ['checkout', '--', 'tool.cmd', 'mixed.bat']),
    ('read', 'tool.cmd'),
    ('read', 'mixed.bat'),
    # the same blob in other line endings: status's size check calls it modified, diff does not
    ('bytes', 'crlf.txt', b'one\ntwo\n'),
    ('git', ['status', '--porcelain=v1']),
    ('git', ['diff', '--name-only']),
    ('git', ['diff']),
    ('git', ['checkout', '--', 'crlf.txt']),
    ('read', 'crlf.txt'),
    # the same blob and the same size: a checkout rewrites a file whose stat differs, in whole
    # seconds (Git for Windows keeps no nanoseconds); _OLD is never racy against the index
    ('bytes', 'eq.txt', b'a\nb\r\n'),
    ('mtime', 'eq.txt', _OLD),
    ('git', ['checkout', '--', 'eq.txt']),
    ('read', 'eq.txt'),
    ('bytes', 'eq.txt', b'a\r\nb\n'),
    ('mtime', 'eq.txt', _OLD),
    ('git', ['add', '--', 'eq.txt']),
    ('bytes', 'eq.txt', b'a\nb\r\n'),
    ('mtime', 'eq.txt', _OLD + 300000000),
    ('git', ['checkout', '--', 'eq.txt']),
    ('read', 'eq.txt'),
    ('mtime', 'eq.txt', _OLD + 2000000000),
    ('git', ['reset', '-q', '--hard', 'HEAD']),
    ('read', 'eq.txt'),
    ('git', ['status', '--porcelain=v1']),
    # commit -a hashes under the attributes too
    ('bytes', 'tool.cmd', b'@echo off\r\nexit /b 2\r\n'),
    ('git', ['commit', '-q', '-a', '-m', 'tool']),
    ('git', ['ls-files', '-s']),
    # a branch switch writes under the result index's .gitattributes: none on main, then the
    # branch's, which the same checkout writes
    ('git', ['switch', '-q', 'main']),
    ('read', '.gitattributes'),
    ('read', 'tool.cmd'),
    ('git', ['switch', '-q', 'kit/attr']),
    ('read', '.gitattributes'),
    ('read', 'tool.cmd'),
    ('read', 'mixed.bat'),
    ('read', 'crlf.txt'),
    ('git', ['status', '--porcelain=v1']),
    # merge --abort writes an unmerged path from HEAD under HEAD's attributes (real git makes
    # the conflict: the fake refuses a merge with a .gitattributes on a side)
    ('git', ['switch', '-q', '-c', 'kit/del']),
    ('git', ['rm', '-q', '--', 'tool.cmd']),
    # the merge takes this side's .gitattributes into the work tree; the abort must not read it
    ('write', '.gitattributes', '* text=auto eol=lf\n'),
    ('git', ['add', '--', '.gitattributes']),
    ('git', ['commit', '-q', '-m', 'drop tool']),
    ('git', ['switch', '-q', 'kit/attr']),
    ('bytes', 'tool.cmd', b'@echo on\nexit /b 3\n'),
    ('git', ['commit', '-q', '-a', '-m', 'tool again']),
    ('read', 'tool.cmd'),
    ('setup', ['merge', '-q', '-m', 'conflict', 'kit/del']),
    ('git', ['status', '--porcelain=v1']),
    ('read', '.gitattributes'),
    ('git', ['merge', '--abort']),
    ('read', 'tool.cmd'),
    ('read', '.gitattributes'),
    ('git', ['status', '--porcelain=v1']),
]

# core.autocrlf decides where an attribute sets text with no eol, and for the paths no line
# rules ('!text' unsets text again).
_AUTOCRLF_ATTRS = '*.txt text\nraw.txt -text\n*.lf eol=lf\nauto.x text=auto\nkeep.txt !text\n'
_AUTOCRLF_FILES = ['t.txt', 'raw.txt', 'n.lf', 'auto.x', 'keep.txt', 'plain.md']
_AUTOCRLF_READS = [('read', p) for p in ['.gitattributes'] + _AUTOCRLF_FILES]
# m.txt's stat is set back each time it is written, so that its entry is never racily clean:
# git and the fake would otherwise answer by the clock.
ATTRIBUTES_AUTOCRLF = [
    ('bytes', 'm.txt', b'a\r\nb\n'),
    ('mtime', 'm.txt', _OLD),
    ('git', ['add', '--', 'm.txt']),
    ('git', ['commit', '-q', '-m', 'a blob with a CRLF and a lone LF']),
    ('git', ['config', 'core.autocrlf', 'true']),
    ('git', ['switch', '-q', '-c', 'kit/attr']),
    ('write', '.gitattributes', _AUTOCRLF_ATTRS),
    ('bytes', 't.txt', b'a\r\nb\nc'),
    ('bytes', 'raw.txt', b'a\r\nb\n'),
    ('bytes', 'n.lf', b'a\r\nb\n'),
    ('bytes', 'auto.x', b'x\ny\n'),
    ('bytes', 'keep.txt', b'k\r\n'),
    ('bytes', 'plain.md', b'm\nn\n'),
    ('git', ['hash-object'] + _AUTOCRLF_FILES),
    ('git', ['add', '--', '.gitattributes'] + _AUTOCRLF_FILES),
    ('git', ['ls-files', '-s']),
    ('git', ['commit', '-q', '-m', 'attributes']),
    ('git', ['switch', '-q', 'main']),
    ('git', ['switch', '-q', 'kit/attr']),
] + _AUTOCRLF_READS + [
    ('git', ['status', '--porcelain=v1']),
    # a path an attribute rules: a size that differs is a change to status, not to diff
    ('bytes', 't.txt', b'a\nb\nc'),
    ('git', ['status', '--porcelain=v1']),
    ('git', ['diff', '--name-only']),
    ('git', ['reset', '-q', '--hard', 'HEAD']),
    ('read', 't.txt'),
    # text with a CRLF output turns only the blob's lone LFs into CRLF
    ('bytes', 'm.txt', b'z\n'),
    ('git', ['checkout', '--', 'm.txt']),
    ('read', 'm.txt'),
    ('mtime', 'm.txt', _OLD),
    ('git', ['status', '--porcelain=v1']),
    ('git', ['checkout', '-q', 'HEAD', '--', 'm.txt']),
    ('mtime', 'm.txt', _OLD),
    ('git', ['config', 'core.autocrlf', 'input']),
    ('git', ['switch', '-q', 'main']),
    ('git', ['switch', '-q', 'kit/attr']),
] + _AUTOCRLF_READS + [
    ('git', ['hash-object'] + _AUTOCRLF_FILES),
    ('git', ['status', '--porcelain=v1']),
]

# convert.c has_crlf_in_index: an index blob with a CRLF stops the conversion, unless the blob
# is binary (a lone CR makes it so).
AUTOCRLF_INDEX_CR = [
    ('bytes', 'cr.txt', b'a\rb\r\n'),
    ('bytes', 'crlf.txt', b'c\r\nd\r\n'),
    ('git', ['add', '--', 'cr.txt', 'crlf.txt']),
    ('git', ['commit', '-q', '-m', 'cr']),
    ('git', ['config', 'core.autocrlf', 'true']),
    ('bytes', 'cr.txt', b'a\r\nb\r\n'),
    ('bytes', 'crlf.txt', b'c\r\nd\r\ne\r\n'),
    ('git', ['add', '--', 'cr.txt', 'crlf.txt']),
    ('git', ['ls-files', '-s']),
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
            if step[0] == 'bytes':
                for s in sides:
                    with open(s.repo.path(step[1]), 'wb') as f:
                        f.write(step[2])
                continue
            if step[0] == 'mtime':
                for s in sides:
                    os.utime(s.repo.path(step[1]), ns=(step[2], step[2]))
                continue
            if step[0] == 'setup':
                got = [s.real(step[1])[0] for s in sides]
                self.assertEqual(got[0], got[1], 'set-up step %d git %s' % (n, ' '.join(step[1])))
                continue
            if step[0] == 'read':
                got = [_file_bytes(s.repo.path(step[1])) for s in sides]
                if got[0] != got[1]:
                    diffs.append('step %d read %s:\n  fake: %r\n  real: %r' % (
                        n, step[1], got[0], got[1]))
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

    def test_gitattributes_line_endings(self):
        self.run_script(BRANCH_AND_LAND[:3] + ATTRIBUTES)

    def test_gitattributes_under_core_autocrlf(self):
        self.run_script(BRANCH_AND_LAND[:3] + ATTRIBUTES_AUTOCRLF)

    def test_a_binary_index_blob_with_a_cr_leaves_conversion_on(self):
        self.run_script(BRANCH_AND_LAND[:3] + AUTOCRLF_INDEX_CR)


if __name__ == '__main__':
    unittest.main()
