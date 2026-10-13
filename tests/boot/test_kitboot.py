"""boot/kitboot.py, the check every host entry runs before it imports kitlib (docs/kitboot.md): the
source checkout when nothing is pinned, a pinned release verified, and every refusal. Releases are
published with kitlib.packageexport into scratch folders; no git."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from kitlib import packageexport as P

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('kitboot_under_test', ROOT / 'boot' / 'kitboot.py')
K = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(K)
COMMIT = 'a' * 40


def release_files(**extra):
    files = {'kitlib/kitrelease/bootstrap.py': (ROOT / 'kitlib/kitrelease/bootstrap.py').read_bytes(),
             'kitlib/__init__.py': b'', 'tools/machine_lock.py': b'pass\n',
             'hosts.json': (ROOT / 'hosts.json').read_bytes(),
             'kit.json': b'{"name": "pm-kit", "version": "1.2.3"}\n'}
    files.update(extra)
    return files


class Source(unittest.TestCase):
    def test_nothing_set_is_the_checkout_beside_boot(self):
        kit = K.locate({})
        self.assertEqual(Path(kit.root), ROOT)
        self.assertEqual((kit.identity, kit.commit), (None, None))
        self.assertEqual(K.locate({}, here=str(ROOT / 'boot' / 'kitboot.py')), kit)

    def test_an_absolute_root_is_used(self):
        self.assertEqual(Path(K.locate({K.ROOT_ENV: str(ROOT)}).root), ROOT.resolve())

    def test_refusals(self):
        scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, scratch, True)
        for env, why in (({K.RELEASE_ENV: 'f' * 64}, 'needs'), ({K.ROOT_ENV: ''}, 'non-blank'),
                         ({K.ROOT_ENV: '  '}, 'non-blank'), ({K.ROOT_ENV: 'relative/kit'}, 'absolute'),
                         ({K.ROOT_ENV: scratch}, 'no kit at')):
            with self.subTest(env=env), self.assertRaises(K.BootError) as caught:
                K.locate(env)
            self.assertIn(why, str(caught.exception))

    def test_a_release_without_a_pin_is_refused(self):
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        root = P.publish(release_files(), COMMIT, K.SUBTREE, scratch / 'release')
        with self.assertRaises(K.BootError) as caught:
            K.locate({K.ROOT_ENV: str(root)})
        self.assertIn('only with %s pinned' % K.RELEASE_ENV, str(caught.exception))


class Pinned(unittest.TestCase):
    def setUp(self):
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        files = release_files()
        self.root = P.publish(files, COMMIT, K.SUBTREE, scratch / 'release')
        self.pin = hashlib.sha256(P.encoded(P.metadata(files, COMMIT, K.SUBTREE))).hexdigest()

    def env(self, pin=None):
        return {K.ROOT_ENV: str(self.root), K.RELEASE_ENV: pin or self.pin}

    def test_the_pinned_release_is_verified(self):
        kit = K.locate(self.env())
        self.assertEqual((Path(kit.root), kit.identity, kit.commit), (self.root, self.pin, COMMIT))
        self.assertEqual(K.verify(self.root, self.pin)['version'], '1.2.3')

    def test_another_pin_is_refused(self):
        for pin in ('f' * 64, 'F' * 64, 'short'):
            with self.subTest(pin=pin), self.assertRaises(K.BootError):
                K.locate(self.env(pin))
        with self.assertRaises(K.BootError):
            K.verify(self.root, self.pin, 'b' * 40)

    def test_a_changed_file_is_refused(self):
        (self.root / 'tools' / 'machine_lock.py').write_bytes(b'changed\n')
        with self.assertRaises(K.BootError) as caught:
            K.locate(self.env())
        self.assertIn('failed its check', str(caught.exception))

    def test_a_changed_verifier_is_refused_before_it_runs(self):
        path = self.root / 'kitlib' / 'kitrelease' / 'bootstrap.py'
        path.write_bytes(b'raise SystemExit("ran")\n')
        with self.assertRaises(K.BootError) as caught:
            K.locate(self.env())
        self.assertIn('verifier differs', str(caught.exception))

    def test_an_extra_file_is_refused(self):
        (self.root / 'tools' / 'extra.py').write_bytes(b'pass\n')
        with self.assertRaises(K.BootError):
            K.locate(self.env())


class Bind(unittest.TestCase):
    def setUp(self):
        saved = list(sys.path), sys.dont_write_bytecode
        self.addCleanup(lambda: (sys.path.__setitem__(slice(None), saved[0]),
                                 setattr(sys, 'dont_write_bytecode', saved[1])))

    def test_this_kit_goes_first_on_the_path(self):
        self.assertEqual(Path(K.boot({})), ROOT)
        self.assertEqual(Path(sys.path[0]), ROOT)
        before = list(sys.path)
        K.boot({})
        self.assertEqual(sys.path, before)                  # not inserted twice

    def test_a_kitlib_loaded_from_elsewhere_is_refused(self):
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        os.makedirs(os.path.join(other, 'kitlib'))
        with self.assertRaises(K.BootError) as caught:
            K.bind(K.Kit(other, None, None))
        self.assertIn('another kitlib is already loaded', str(caught.exception))

    def test_a_pinned_kit_writes_no_bytecode(self):
        K.bind(K.Kit(str(ROOT), 'f' * 64, COMMIT))
        self.assertTrue(sys.dont_write_bytecode)


if __name__ == '__main__':
    unittest.main()
