"""kitlib.loadnode.mirror: after a landing, the merge goes to each opted-in node's mirror of main,
never forced, all nodes at once under one time cap; every failure is a WARNING line and nothing
raises; no registry, no opted-in node or a test store asks no node. The mirror is a real bare one
made by kitlib.loadnode.box (its deny settings), pushed to by real git; only ssh is stood in for
(the push URL is mapped to the mirror's folder)."""
import contextlib
import shutil
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from kitlib.loadnode import box, job, mirror, registry, remote
from tests import gitfixture
from tests.loadnode import test_cli
from tests.loadnode.test_registry import node, text
from tests.realgit import real_git

REF = box.MAIN_REF + 'main'


class Link(object):
    """remote.Link's stand-in: a git_env with no real ssh; `opened` names the nodes linked."""
    opened = None

    def __init__(self, n):
        self.node = n

    def __enter__(self):
        Link.opened.append(self.node.name)
        return self

    def __exit__(self, *exc):
        pass

    def git_env(self, base=None):
        env = dict(base or {})
        env['GIT_SSH_COMMAND'] = 'ssh-stand-in'
        return env


@real_git('git receive-pack into a bare mirror with deny settings: the fake models neither')
class Refresh(unittest.TestCase):
    write = test_cli.Cli.write

    def setUp(self):
        test_cli.Cli.setUp(self)
        self.write(node(repos=['my-repo']), node(name='b2', repos=['other-repo']))
        self.repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(self.repo.cleanup)
        self.base = tempfile.mkdtemp(prefix='kit-mirror-')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.box = box.Box(self.base, runner=['py'], alive=lambda pid, job: False,
                           detach=lambda argv, cwd, env: (99, None))
        self.mirror = self.box.mirror_for_push('my-repo')
        self.store = types.SimpleNamespace(testing=True)
        self.said, self.pushes = [], []
        Link.opened = []

    def git(self, args, cwd, env=None, timeout=None):
        """job.push_refs's git: the node's push URL mapped to the mirror's folder, real git."""
        self.pushes.append((list(args), cwd, env, timeout))
        url = [a for a in args if a.endswith(':pmkit-node/mirrors/my-repo.git')]
        self.assertEqual(url, [remote.push_url(node_of('box1'), 'my-repo')])
        return job._git_default([self.mirror if a in url else a for a in args], cwd, env, timeout)

    def commit(self, name):
        self.repo.write(name, name + '\n')
        return self.repo.commit('add ' + name, name)

    def refresh(self, sha, **kw):
        kw.setdefault('registry_file', self.file)
        kw.setdefault('open_link', Link)
        kw.setdefault('git', self.git)
        mirror.refresh(self.repo.root, 'my-repo', 'main', sha, store=self.store,
                       say=self.said.append, **kw)

    def mirror_ref(self):
        return self.repo.git('--git-dir', self.mirror, 'rev-parse', '--verify', '-q', REF,
                             check=False).strip()

    def test_the_merge_goes_to_the_opted_in_nodes_mirror_never_forced(self):
        first = self.commit('a.txt')
        self.refresh(first)
        second = self.commit('b.txt')
        self.refresh(second)
        self.assertEqual(self.mirror_ref(), second)
        self.assertEqual(self.said, ["pushed %s to load node box1's mirror of main" % first[:10],
                                     "pushed %s to load node box1's mirror of main" % second[:10]])
        args, cwd, env, timeout = self.pushes[-1]
        self.assertEqual(args[-1], '%s:%s' % (second, REF))         # no '+': never forced
        self.assertNotIn('--force', args)
        self.assertEqual((cwd, env['GIT_SSH_COMMAND'], env['GIT_TERMINAL_PROMPT'], timeout),
                         (self.repo.root, 'ssh-stand-in', '0', mirror.TIMEOUT_S))
        self.assertEqual(Link.opened, ['box1', 'box1'])             # b2 did not opt in

    def test_a_push_that_is_not_a_fast_forward_is_a_warning_and_the_mirror_stays(self):
        old = self.commit('a.txt')
        new = self.commit('b.txt')
        self.refresh(new)
        del self.said[:]
        self.refresh(old)                       # main rewound: never forced over the mirror
        self.assertEqual(self.mirror_ref(), new)
        [line] = self.said
        self.assertTrue(line.startswith("WARNING: %s was not pushed to load node box1's mirror of "
                                        'main: ' % old[:10]), line)
        self.assertIn('! [rejected]', line)
        self.assertIn('(non-fast-forward)', line)

    def test_a_push_that_times_out_is_a_warning(self):
        def git(args, cwd, env=None, timeout=None):
            return None, b'', 'git push timed out after %s seconds' % timeout
        self.refresh('c' * 40, git=git, timeout_s=7)
        self.assertEqual(self.said, ["WARNING: cccccccccc was not pushed to load node box1's mirror "
                                     'of main: git push timed out after 7 seconds'])

    def test_a_push_with_no_answer_by_the_cap_is_a_warning_and_refresh_returns(self):
        gone = threading.Event()
        self.addCleanup(gone.set)

        def git(args, cwd, env=None, timeout=None):
            gone.wait(30)
            return 0, b'', ''
        t0 = time.monotonic()
        with mock.patch.object(mirror, 'JOIN_SLACK_S', 0.2):
            self.refresh('c' * 40, git=git, timeout_s=0.3)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(self.said, ["WARNING: cccccccccc was not pushed to load node box1's mirror "
                                     'of main: no answer in 0.3 s'])

    def test_every_node_is_pushed_to_at_once(self):
        self.write(node(repos=['my-repo']), node(name='b2', repos=['my-repo']))
        both = threading.Barrier(2, timeout=10)  # one push at a time would break it

        def git(args, cwd, env=None, timeout=None):
            both.wait()
            return 0, b'', ''
        self.refresh('c' * 40, git=git)
        self.assertEqual(self.said, ["pushed cccccccccc to load node box1's mirror of main",
                                     "pushed cccccccccc to load node b2's mirror of main"])

    def test_a_link_that_cannot_open_is_a_warning(self):
        @contextlib.contextmanager
        def no_link(n):
            raise OSError("no ssh client: install Windows' OpenSSH client")
            yield
        self.refresh('c' * 40, open_link=no_link)
        self.assertEqual(self.said, ["WARNING: cccccccccc was not pushed to load node box1's mirror "
                                     "of main: OSError: no ssh client: install Windows' OpenSSH "
                                     'client'])

    def test_no_node_opted_in_asks_no_node(self):
        self.write(node(repos=['other-repo']))
        self.refresh('c' * 40, git=no_git, open_link=no_link)
        self.assertEqual(self.said, [])

    def test_no_registry_asks_no_node(self):
        self.refresh('c' * 40, registry_file=self.file + '.gone', git=no_git, open_link=no_link)
        self.assertEqual(self.said, [])

    def test_a_test_store_asks_no_node(self):
        with mock.patch.object(registry, 'path', return_value=self.file):
            self.refresh('c' * 40, registry_file=None, git=no_git, open_link=no_link)
        self.assertEqual(self.said, [])

    def test_a_registry_that_cannot_be_used_is_a_warning(self):
        with open(self.file, 'w', encoding='utf-8') as f:
            f.write('{not json')
        self.refresh('c' * 40, git=no_git, open_link=no_link)
        [line] = self.said
        self.assertTrue(line.startswith("WARNING: the load nodes' mirrors of main were not "
                                        'refreshed: the load node registry cannot be used: '), line)


def node_of(name):
    return registry.parse(text(node(name=name)))[0]


def no_git(*a, **kw):
    raise AssertionError('git was asked')


def no_link(n):
    raise AssertionError('a node was linked')


if __name__ == '__main__':
    unittest.main()
