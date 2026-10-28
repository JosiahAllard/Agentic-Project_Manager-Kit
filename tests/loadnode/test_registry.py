"""kitlib.loadnode.registry: the load-node list is checked value by value before anything reaches
ssh, and is read only when git ignores it."""
import json
import os
import tempfile
import unittest

from kitlib.git import cmd as gitcmd
from kitlib.loadnode import registry
from tests import gitfixture
from tests.realgit import real_git

# A real key (ssh-keygen -t ed25519) and the fingerprint `ssh-keygen -lf` printed for it.
KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIArd/7SiNetSEEvPs3iov+f+z+POr8jO1Dub/nED09vn'
KEY_FP = 'SHA256:6F04k1g2ZLV7Ze4LwAdvXD51keDHYwe0gT0JoOTuYTA'
# An ssh-rsa blob in the ed25519 position: decodes, but is not an ed25519 key.
RSA_BLOB = 'ssh-ed25519 AAAAB3NzaC1yc2EAAAADAQABAAAAgQC7'


def node(**over):
    n = {'name': 'box1', 'address': '192.168.1.50', 'user': 'pmnode', 'host_key': KEY,
         'slots': 1, 'memory_mb': 12288}
    n.update(over)
    return n


def text(*nodes, **top):
    body = {'version': 1, 'nodes': list(nodes)}
    body.update(top)
    return json.dumps(body)


class Parse(unittest.TestCase):

    def bad(self, *nodes, **top):
        with self.assertRaises(registry.RegistryError) as cm:
            registry.parse(text(*nodes, **top))
        return str(cm.exception)

    def test_the_example_reads(self):
        got = registry.parse(text(node(port=2222, identity_file='~/.ssh/pmkit_node_ed25519'),
                                  node(name='box-2', address='node2.lan')))
        self.assertEqual(got[0], registry.Node('box1', '192.168.1.50', 'pmnode', KEY, 1, 12288,
                                               2222, '~/.ssh/pmkit_node_ed25519'))
        self.assertEqual((got[1].port, got[1].identity_file), (22, None))

    def test_notes_are_allowed(self):
        self.assertEqual(len(registry.parse(text(node(_why='spare box'), comment='mine'))), 1)

    def test_an_empty_list_reads(self):
        self.assertEqual(registry.parse(text()), [])

    def test_an_address_ssh_could_read_as_an_option_or_a_login_is_refused(self):
        for a in ('-oProxyCommand=calc', '-p', 'root@box', 'box 1', 'box\n1', '', 'box;id',
                  'fe80::1%eth0', 'a' * 254, 'box_1', '.box', 'box..lan', '%h', '$(id)', '[::1]'):
            with self.subTest(address=a):
                self.assertIn('address', self.bad(node(address=a)))

    def test_ip_addresses_and_host_names_read(self):
        for a in ('10.0.0.7', 'fd00::7', '::1', 'box1', 'box-1.lan', 'NODE2.example.org'):
            with self.subTest(address=a):
                self.assertEqual(registry.parse(text(node(address=a)))[0].address, a)

    def test_a_user_or_name_out_of_pattern_is_refused(self):
        for u in ('-oX', 'root@x', 'Pm', 'a b', '', 'x' * 33, 'pm;id'):
            with self.subTest(user=u):
                self.assertIn('.user', self.bad(node(user=u)))
        for n in ('Box', '-box', 'box_1', 'b' * 33, ''):
            with self.subTest(name=n):
                self.assertIn('.name', self.bad(node(name=n)))

    def test_a_host_key_that_is_not_one_ed25519_key_is_refused(self):
        body = KEY.split(' ')[1]
        for k in ('ssh-rsa ' + body, body, KEY + ' comment', KEY + '\n', 'ssh-ed25519 ' + body[:-4],
                  'ssh-ed25519 !!!!', RSA_BLOB, 'ssh-ed25519  ' + body, ''):
            with self.subTest(key=k):
                self.assertIn('host_key', self.bad(node(host_key=k)))

    def test_numbers_out_of_range_are_refused(self):
        for k, v in (('slots', 0), ('slots', True), ('slots', 1.5), ('memory_mb', 100),
                     ('port', 0), ('port', 65536), ('port', '22')):
            with self.subTest(key=k, value=v):
                self.assertIn('.' + k, self.bad(node(**{k: v})))

    def test_an_identity_file_must_be_absolute_and_free_of_tokens(self):
        for f in ('key', '.ssh/key', '~user/key', '~/%d/key', 'C:\\keys\\"k"', ' ~/k', '~/k\n'):
            with self.subTest(identity_file=f):
                self.assertIn('identity_file', self.bad(node(identity_file=f)))
        self.assertEqual(registry.parse(text(node(identity_file='C:\\keys\\k')))[0].identity_file,
                         'C:\\keys\\k')

    def test_unknown_keys_twice_written_keys_and_twice_used_names_are_refused(self):
        self.assertIn('unknown key', self.bad(node(proxy='x')))
        self.assertIn('unknown key', self.bad(node(), extra=1))
        self.assertIn('version', self.bad(node(), version=2))
        self.assertIn('used twice', self.bad(node(), node(address='10.0.0.2')))
        with self.assertRaises(registry.RegistryError):
            registry.parse('{"version": 1, "version": 1, "nodes": []}')
        with self.assertRaises(registry.RegistryError):
            registry.parse('not json')

    def test_every_problem_is_named_at_once(self):
        why = self.bad(node(slots=0, port=0))
        self.assertIn('.slots', why)
        self.assertIn('.port', why)

    def test_repos_is_the_opt_in_and_none_by_default(self):
        got = registry.parse(text(node(repos=['my-repo', 'Other_Repo.2']), node(name='b2')))
        self.assertEqual(got[0].repos, ('my-repo', 'Other_Repo.2'))
        self.assertEqual(got[1].repos, ())

    def test_a_repo_name_that_could_name_another_folder_is_refused(self):
        for r in ('..', 'a..b', '../x', 'a/b', 'a\\b', '.git', '-x', '', 'a b', 'x' * 101, 7):
            with self.subTest(repo=r):
                self.assertIn('.repos[0]', self.bad(node(repos=[r])))
        self.assertIn('.repos', self.bad(node(repos='my-repo')))


class Fingerprint(unittest.TestCase):

    def test_it_is_what_ssh_keygen_prints(self):
        self.assertEqual(registry.fingerprint(KEY), KEY_FP)


class Lines(unittest.TestCase):

    def test_a_line_names_the_fingerprint_and_the_identity_file(self):
        nodes = registry.parse(text(node(identity_file='~/.ssh/k'), node(name='b2')))
        lines = registry.list_lines(nodes, identity_exists=lambda p: False)
        self.assertIn('pmnode@192.168.1.50:22', lines[0])
        self.assertIn(KEY_FP, lines[0])
        self.assertIn('~/.ssh/k (MISSING)', lines[0])
        self.assertIn('ssh default key', lines[1])

    def test_a_line_names_the_repos_it_takes(self):
        nodes = registry.parse(text(node(repos=['r1', 'r2']), node(name='b2')))
        lines = registry.list_lines(nodes)
        self.assertTrue(lines[0].endswith('repos: r1, r2'), lines[0])
        self.assertTrue(lines[1].endswith('repos: none'), lines[1])


class FakeCall(object):
    """kitlib.git.cmd.call's stand-in, with its types: out is bytes, err is text."""

    def __init__(self, rc, err=''):
        self.rc, self.err, self.calls = rc, err, []

    def __call__(self, args, cwd):
        self.calls.append((args, cwd))
        return gitcmd.Result(self.rc, b'', self.err)


class Load(unittest.TestCase):

    def setUp(self):
        self.pm = tempfile.mkdtemp(prefix='kit-loadnode-')
        self.addCleanup(gitfixture.rmtree, self.pm)
        os.makedirs(os.path.join(self.pm, '.pm-kit'))
        self.file = os.path.join(self.pm, '.pm-kit', registry.FILE_NAME)

    def write(self, body):
        with open(self.file, 'w', encoding='utf-8') as f:
            f.write(body)

    def test_an_ignored_registry_reads(self):
        self.write(text(node()))
        call = FakeCall(0)
        self.assertEqual([n.name for n in registry.load(self.file, call)], ['box1'])
        self.assertEqual(call.calls, [(['check-ignore', '-q', '--', '.pm-kit/load-nodes.json'], self.pm)])

    def test_a_registry_git_does_not_ignore_is_refused(self):
        self.write(text(node()))
        with self.assertRaises(registry.RegistryError) as cm:
            registry.load(self.file, FakeCall(1))
        self.assertIn('not ignored', str(cm.exception))
        self.assertIn('.gitignore', str(cm.exception))

    def test_git_failing_refuses(self):
        self.write(text(node()))
        for rc, err in ((128, 'fatal: not a git repository'),
                        (None, 'git check-ignore timed out after 30 seconds'),
                        (None, 'could not run git in x: [WinError 2] not found')):
            with self.subTest(rc=rc):
                with self.assertRaises(registry.RegistryError) as cm:
                    registry.load(self.file, FakeCall(rc, err))
                self.assertIn(err, str(cm.exception))

    def test_a_missing_registry_and_no_pm_repository_are_refused(self):
        with self.assertRaises(registry.RegistryError) as cm:
            registry.load(self.file, FakeCall(0))
        self.assertIn('does not exist', str(cm.exception))
        with self.assertRaises(registry.RegistryError) as cm:
            registry.load(None, FakeCall(0))
        self.assertIn('no PM repository', str(cm.exception))


@real_git('git check-ignore on the PM repo: the fake lacks check-ignore')
class IgnoredOnRealGit(unittest.TestCase):

    def setUp(self):
        self.repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(self.repo.cleanup)
        os.makedirs(os.path.join(self.repo.root, '.pm-kit'), exist_ok=True)

    def test_the_pm_kit_gitignore_line_is_what_makes_it_ignored(self):
        self.assertIn('not ignored', registry.ignored(self.repo.root))
        with open(os.path.join(self.repo.root, '.pm-kit', '.gitignore'), 'w', encoding='utf-8') as f:
            f.write('local.json\nload-nodes.json\n')
        self.assertIsNone(registry.ignored(self.repo.root))

    def test_a_tracked_registry_is_refused_even_when_gitignore_lists_it(self):
        self.repo.write('.pm-kit/load-nodes.json', text())
        self.repo.commit('track the registry', '.pm-kit/load-nodes.json')
        self.repo.write('.pm-kit/.gitignore', 'load-nodes.json\n')
        self.assertIn('not ignored', registry.ignored(self.repo.root))


if __name__ == '__main__':
    unittest.main()
