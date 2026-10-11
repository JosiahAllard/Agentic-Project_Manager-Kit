"""kitlib.git.fake: the config text and packed-refs caches never answer from before a rewrite.

Both are keyed on a file's stat. A rewrite that keeps the size and lands in the same clock tick
(`git remote rename origin private` on a fixture config) kept the old
key, so the next command read the old text. The mtime is pinned here with os.utime, so the test
does not depend on the clock.

Two guards, each with its own tests. The file id in the key (st_ino) catches any writer's replace,
but only where the filesystem gives the replaced path a new id (NTFS does). The eviction after the
fake's own rewrite (config._write, refs._rewrite_packed) does not depend on that: the
`..._when_the_stat_key_does_not_change` tests hold os.stat still for the path, so only the eviction
can make the next read fresh."""
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

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


def frozen_stat(path):
    """A patch under which os.stat(path) keeps answering as it does now (same st_ino, mtime and
    size, whatever is written): a filesystem that reuses or zeroes the file id, in a rewrite that
    keeps the size within one clock tick. Every other path is stat'ed for real."""
    real = os.stat
    held = real(path)
    norm = os.path.normcase(os.path.abspath(path))

    def fake(p, *args, **kwargs):
        if isinstance(p, str) and os.path.normcase(os.path.abspath(p)) == norm:
            return held
        return real(p, *args, **kwargs)

    return mock.patch.object(os, 'stat', fake)


def on_disk(path):
    with open(path, encoding='utf-8', newline='') as fh:
        return fh.read()


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

    def test_rewrite_by_the_fake_is_read_fresh_when_the_stat_key_does_not_change(self):
        with frozen_stat(self.path):
            before = os.stat(self.path)
            self.assertTrue(config.rename_section(self.path, 'remote', 'origin', 'private'))
            self.assertEqual(on_disk(self.path), '[remote "private"]\n\turl = a\n')
            self.assertEqual(os.stat(self.path), before)                               # same key
            self.assertEqual(config.read_file(self.path), [('remote.private.url', 'a')])
            config.set_value(self.path, 'remote.private.url', 'b')
            self.assertEqual(on_disk(self.path), '[remote "private"]\n\turl = b\n')
            self.assertEqual(os.stat(self.path), before)
            self.assertEqual(config.read_file(self.path), [('remote.private.url', 'b')])


class PackedRefsCache(Base):
    def setUp(self):
        super().setUp()
        self.repo = types.SimpleNamespace(commondir=self.dir)
        self.path = os.path.join(self.dir, 'packed-refs')
        write_pinned(self.path, '%s refs/heads/main\n' % OLD)
        self.assertEqual(refs.packed(self.repo), {'refs/heads/main': (OLD, None)})      # primes it

    def test_same_size_replace_through_a_ref_lock_is_read_fresh(self):
        # RefLock.commit alone, not _rewrite_packed: nothing is evicted, the file id is the guard.
        lk = refs.RefLock(self.path, 'packed-refs')
        lk.acquire()
        lk.commit(('%s refs/heads/main\n' % NEW).encode('utf-8'))
        pin(self.path)
        self.assertEqual(refs.packed(self.repo), {'refs/heads/main': (NEW, None)})

    def test_packed_ref_deleted_by_the_fake_is_gone_when_the_stat_key_does_not_change(self):
        repo = types.SimpleNamespace(commondir=self.dir, gitdir=self.dir, hexlen=40, linked=False)
        both = '%s refs/heads/main\n%s refs/heads/topic\n' % (OLD, NEW)
        write_pinned(self.path, both)
        self.assertEqual(refs.packed(repo), {'refs/heads/main': (OLD, None),
                                             'refs/heads/topic': (NEW, None)})          # primes it
        with frozen_stat(self.path):
            before = os.stat(self.path)
            self.assertTrue(refs.delete(repo, 'refs/heads/topic'))     # through _rewrite_packed
            self.assertEqual(on_disk(self.path), '%s refs/heads/main\n' % OLD)
            self.assertEqual(os.stat(self.path), before)                               # same key
            self.assertEqual(refs.packed(repo), {'refs/heads/main': (OLD, None)})

    def test_same_size_replace_by_another_writer_is_read_fresh(self):
        external_replace(self.path, '%s refs/heads/main\n' % NEW)
        self.assertEqual(refs.packed(self.repo), {'refs/heads/main': (NEW, None)})


if __name__ == '__main__':
    unittest.main()
