"""kitlib.git.fake: the config text and packed-refs caches never answer from before a rewrite.

Both are keyed on a file's stat. A rewrite that keeps the size and lands in the same clock tick
(`git remote rename origin private` on a fixture config) kept the old
key, so the next command read the old text. The mtime is pinned here with os.utime, so the test
does not depend on the clock."""
import os
import shutil
import tempfile
import types
import unittest

from kitlib.git.fake import config, refs

PINNED_NS = 1_700_000_000_000_000_000
OLD = '1111111111111111111111111111111111111111'
NEW = '2222222222222222222222222222222222222222'


def pin(path):
    os.utime(path, ns=(PINNED_NS, PINNED_NS))


def write_pinned(path, text):
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    pin(path)


def external_replace(path, text):
    """Another writer's atomic replace (lock file, then rename), mtime pinned to the old one."""
    lock = path + '.ext'
    write_pinned(lock, text)
    os.replace(lock, path)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-fakecache-')
        self.addCleanup(shutil.rmtree, self.dir, True)


class ConfigCache(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.dir, 'config')
        write_pinned(self.path, '[remote "origin"]\n\turl = a\n')
        self.assertEqual(config.read_file(self.path), [('remote.origin.url', 'a')])    # primes it

    def test_same_size_rewrite_by_the_fake_is_read_fresh(self):
        config.rename_section(self.path, 'remote', 'origin', 'private')                # +1 char
        pin(self.path)
        self.assertEqual(config.read_file(self.path), [('remote.private.url', 'a')])
        config.set_value(self.path, 'remote.private.url', 'b')                         # same size
        pin(self.path)
        self.assertEqual(config.read_file(self.path), [('remote.private.url', 'b')])

    def test_same_size_rewrite_by_the_fake_with_the_mtime_pinned_before_the_read(self):
        config.set_value(self.path, 'remote.origin.url', 'b')
        pin(self.path)
        self.assertEqual(config.read_file(self.path), [('remote.origin.url', 'b')])

    def test_same_size_replace_by_another_writer_is_read_fresh(self):
        external_replace(self.path, '[remote "origin"]\n\turl = b\n')
        self.assertEqual(config.read_file(self.path), [('remote.origin.url', 'b')])


class PackedRefsCache(Base):
    def setUp(self):
        super().setUp()
        self.repo = types.SimpleNamespace(commondir=self.dir)
        self.path = os.path.join(self.dir, 'packed-refs')
        write_pinned(self.path, '%s refs/heads/main\n' % OLD)
        self.assertEqual(refs.packed(self.repo), {'refs/heads/main': (OLD, None)})      # primes it

    def test_same_size_rewrite_by_the_fake_is_read_fresh(self):
        lk = refs.RefLock(self.path, 'packed-refs')
        lk.acquire()
        lk.commit(('%s refs/heads/main\n' % NEW).encode('utf-8'))
        pin(self.path)
        self.assertEqual(refs.packed(self.repo), {'refs/heads/main': (NEW, None)})

    def test_same_size_replace_by_another_writer_is_read_fresh(self):
        external_replace(self.path, '%s refs/heads/main\n' % NEW)
        self.assertEqual(refs.packed(self.repo), {'refs/heads/main': (NEW, None)})


if __name__ == '__main__':
    unittest.main()
