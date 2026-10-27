"""The home fence (docs/test-tools.md "Home fence"): in a test process kitlib.config.user.home_dir()
refuses the real home folder; a temp home passes; a process that does not import the tests
package is not fenced unless it carries the fence's marker (KIT_HOME_FENCE), which the tests'
children inherit: then it refuses the home the marker names and the profile folder Windows
reports, under any spelling.

Every child here gets temp folders as its home and as its marker. The two tests that hand a
child the real profile folder only compute the home's path: nothing under it is read."""
import _winapi
import ctypes
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import tests
from kitlib.config import user
from kitlib.lock import store as lockstore
from tests import gitfixture
from tests.home import child_home, fence_decoy, temp_home, unmarked_env

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MARK = 'KIT_HOME_FENCE'

# A process that imports kitlib and not the tests package: one line, `fenced <message>` or
# `passed <home>`, then whether it asked Windows for the profile folder.
RESOLVE = '''\
    import os, sys
    sys.path.insert(0, sys.argv[1])
    from kitlib.config import user
    try:
        got = user.home_dir()
    except user.HomeFenced as e:
        print('fenced', e)
    else:
        print('passed', ascii(os.path.normcase(got)))
    print('asked' if user._PROFILE is not None else 'not asked')
    '''


def same(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def shown(path):
    """How a child prints a folder: ascii() of its normcase, whatever the console encodes."""
    return ascii(os.path.normcase(path))


def extended(path):
    """The extended-length spelling of the folder `path`: the `?` prefix before a drive path, or
    its UNC form before a share's."""
    path, bs = os.path.abspath(path), chr(92)
    if path.startswith(bs * 2):
        return bs * 2 + '?' + bs + 'UNC' + bs + path[2:]
    return bs * 2 + '?' + bs + path


class ChildCase(unittest.TestCase):
    def folder(self, prefix='kit-fence-'):
        d = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(gitfixture.rmtree, d)
        return d

    def env(self, home, marker):
        """A child's environment: USERPROFILE and HOME are `home`, the marker is `marker` (None:
        an unmarked child)."""
        env = unmarked_env()
        env.update(USERPROFILE=home, HOME=home)
        if marker is not None:
            env[MARK] = marker
        return env

    def child(self, body, home, marker=None, args=()):
        """Run the script `body` in a child: (exit code, its output lines)."""
        script = os.path.join(self.folder('kit-fence-child-'), 'child.py')
        with open(script, 'w', encoding='utf-8') as fh:
            fh.write(textwrap.dedent(body))
        r = subprocess.run([sys.executable, '-I', script, ROOT] + list(args), env=self.env(home, marker),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        return r.returncode, r.stdout.decode(errors='replace').splitlines()

    def junction(self, target):
        """A new junction to the folder `target`. The link goes when the test ends, before any
        folder does: os.rmdir removes the link and never what it points at."""
        link = os.path.join(self.folder('kit-fence-link-'), 'home')
        _winapi.CreateJunction(target, link)
        self.addCleanup(os.rmdir, link)
        return link


class HomeFenceTest(unittest.TestCase):
    def test_the_real_home_is_refused_in_a_test_process(self):
        # resolving the home is a path computation only: nothing under it is read
        with self.assertRaises(user.HomeFenced) as cm:
            user.home_dir()
        self.assertIn('Home fence', str(cm.exception))

    def test_a_temp_home_passes(self):
        home = temp_home(self)
        self.assertEqual(os.path.normcase(user.home_dir()), os.path.normcase(home))
        self.assertIsNone(user.user_config()['ledger_path'])

    def test_a_fenced_home_refuses_every_lookup_through_it(self):
        fence_decoy(self)
        with self.assertRaises(user.HomeFenced):
            user.home_dir()
        with self.assertRaises(user.HomeFenced):
            user.user_config()
        with self.assertRaises(user.HomeFenced):
            user.locate()
        given = tempfile.mkdtemp(prefix='kit-given-home-')
        self.addCleanup(gitfixture.rmtree, given)
        self.assertEqual(user.locate(home=given).state, user.NONE)      # an explicit home passes

    def test_importing_the_tests_package_set_the_fence(self):
        self.assertIs(tests._kuser, user)
        self.assertIsNotNone(user._FENCED_HOME)

    def test_except_exception_does_not_swallow_the_refusal(self):
        fence_decoy(self)
        self.assertFalse(issubclass(user.HomeFenced, Exception))
        with self.assertRaises(user.HomeFenced):
            try:
                user.home_dir()
            except Exception:           # how kitlib isolates one check
                self.fail('HomeFenced was caught as an Exception')


class RealRunTest(ChildCase):
    """Guards, not red-first tests (green before the marker too): a process that imports kitlib
    and not the tests package, and carries no marker, resolves the home as ever and never asks
    Windows for the profile folder."""

    def test_a_process_without_the_tests_package_is_not_fenced(self):
        d = tempfile.mkdtemp(prefix='kit-fence-child-')
        self.addCleanup(gitfixture.rmtree, d)
        script = os.path.join(d, 'child.py')
        with open(script, 'w', encoding='utf-8') as fh:
            fh.write(textwrap.dedent('''\
                import os, sys
                sys.path.insert(0, sys.argv[1])
                from kitlib.config import user
                print(user.home_dir() == os.path.expanduser('~'), 'tests' in sys.modules,
                      getattr(user, '_FENCED_HOME', None) is None)
                '''))
        r = subprocess.run([sys.executable, '-I', script, ROOT], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=60, env=unmarked_env())
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(r.stdout.decode().split(), ['True', 'False', 'True'])

    def test_an_unmarked_child_passes_and_never_asks_windows(self):
        home = self.folder('kit-home-')
        rc, out = self.child(RESOLVE, home)
        self.assertEqual((rc, out), (0, ['passed ' + shown(home), 'not asked']))

    def test_an_empty_marker_is_no_marker(self):
        home = self.folder('kit-home-')
        rc, out = self.child(RESOLVE, home, marker='')
        self.assertEqual((rc, out), (0, ['passed ' + shown(home), 'not asked']))

    def test_an_unmarked_lookup_never_asks_windows(self):
        home = temp_home(self)
        asked = mock.Mock(side_effect=AssertionError('an unfenced lookup asked Windows'))
        resolved = mock.Mock(side_effect=os.path.realpath)
        with mock.patch.dict(os.environ), \
                mock.patch.object(user, '_FENCED_HOME', None), \
                mock.patch.object(user, '_system_profile', asked), \
                mock.patch.object(os.path, 'realpath', resolved):
            os.environ.pop(MARK, None)
            got = user.home_dir()
        self.assertTrue(same(got, home), got)
        asked.assert_not_called()
        resolved.assert_not_called()


class ChildFenceGuards(ChildCase):
    """A guard (green before the marker too): the marker never refuses a temp home."""

    def test_a_marked_child_with_a_temp_home_passes(self):
        home = self.folder('kit-home-')
        rc, out = self.child(RESOLVE, home, marker=self.folder('kit-fenced-home-'))
        self.assertEqual((rc, out[:1]), (0, ['passed ' + shown(home)]), out)


class ChildFenceTest(ChildCase):
    """A child of the tests never imports the tests package; the marker it inherits fences it."""

    def test_a_marked_child_refuses_the_recorded_home(self):
        decoy = self.folder('kit-fenced-home-')
        rc, out = self.child(RESOLVE, decoy, marker=decoy)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out[0].startswith('fenced '), out)
        self.assertIn('Home fence', out[0])

    def test_a_marked_child_refuses_the_windows_profile_whatever_the_marker_says(self):
        # The marker names another folder (stale, or wrong): the profile folder Windows reports
        # is refused all the same. The child only computes the path.
        rc, out = self.child(RESOLVE, lockstore.profile_dir(), marker=self.folder('kit-fenced-home-'))
        self.assertEqual(rc, 0, out)
        self.assertTrue(out[0].startswith('fenced '), out)

    def test_a_kit_tool_started_with_the_recorded_home_is_refused(self):
        # tools/ledger.py reads the machine config first (kitlib.config.user.ledger_path)
        decoy = self.folder('kit-fenced-home-')
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'ledger.py'), 'list',
                            '--prov', 'typed', '--since', '2026-01-01'], env=self.env(decoy, decoy),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        text = r.stdout.decode(errors='replace')
        self.assertNotEqual(r.returncode, 0, text)
        self.assertIn('HomeFenced', text)
        self.assertIn('Home fence', text)
        self.assertNotIn('no ledger_path', text)        # it never read the config under that home


class FailClosedTest(unittest.TestCase):
    def test_a_marked_process_that_cannot_ask_windows_refuses_every_home(self):
        temp_home(self)
        marker = tempfile.mkdtemp(prefix='kit-fenced-home-')
        self.addCleanup(gitfixture.rmtree, marker)
        with mock.patch.dict(os.environ, {MARK: marker}), \
                mock.patch.object(user, '_PROFILE', None), \
                mock.patch.object(user, '_system_profile', side_effect=OSError('no answer')):
            with self.assertRaises(user.HomeFenced) as cm:
                user.home_dir()
        self.assertIn('Home fence', str(cm.exception))
        self.assertIn('no answer', str(cm.exception))


class SpellingTest(ChildCase):
    """Another spelling of a refused home is refused too: both sides are compared as the folders
    they really are (os.path.realpath)."""

    def test_a_junction_to_the_fenced_home_is_refused(self):
        link = self.junction(fence_decoy(self))
        with mock.patch.dict(os.environ, {'USERPROFILE': link, 'HOME': link}):
            with self.assertRaises(user.HomeFenced):
                user.home_dir()

    def test_a_marked_child_refuses_a_junction_to_the_recorded_home(self):
        decoy = self.folder('kit-fenced-home-')
        rc, out = self.child(RESOLVE, self.junction(decoy), marker=decoy)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out[0].startswith('fenced '), out)

    def test_a_marked_child_refuses_a_home_that_is_a_junction_when_the_marker_names_its_target(self):
        # the other way round: the marker is the link, the home is the folder it points at
        decoy = self.folder('kit-fenced-home-')
        rc, out = self.child(RESOLVE, decoy, marker=self.junction(decoy))
        self.assertEqual(rc, 0, out)
        self.assertTrue(out[0].startswith('fenced '), out)

    def short(self, folder):
        """The short (8.3) spelling of `folder`; the test is skipped when its volume gives none."""
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.WinDLL('kernel32').GetShortPathNameW(folder, buf, len(buf))
        short = buf.value if 0 < n < len(buf) else ''
        if not short or same(short, folder):
            self.skipTest('the temp folder\'s volume gives no short (8.3) names')
        return short

    def as_profile(self, folder):
        """Until the test ends, `folder` is the profile folder Windows reports to this process."""
        for patch in (mock.patch.object(user, '_PROFILE', None),
                      mock.patch.object(user, '_system_profile', return_value=folder)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_short_name_of_the_fenced_home_is_refused(self):
        short = self.short(fence_decoy(self))
        with mock.patch.dict(os.environ, {'USERPROFILE': short, 'HOME': short}):
            with self.assertRaises(user.HomeFenced):
                user.home_dir()

    # Guards (green before they were written): the spellings above, for the profile folder and
    # for a child. The profile is a temp folder standing in for it, so no link to the real one
    # is ever made.

    def test_a_junction_to_the_profile_folder_is_refused_whatever_else_is_fenced(self):
        fence_decoy(self)                       # the recorded home is another folder
        profile = self.folder('kit-profile-')
        link = self.junction(profile)
        with mock.patch.dict(os.environ, {'USERPROFILE': link, 'HOME': link}):
            self.assertEqual(user.home_dir(), link)         # a temp folder, until it is the profile
            self.as_profile(profile)
            with self.assertRaises(user.HomeFenced):
                user.home_dir()

    def test_the_short_name_of_the_profile_folder_is_refused_whatever_else_is_fenced(self):
        fence_decoy(self)
        profile = self.folder('kit-profile-')
        short = self.short(profile)
        with mock.patch.dict(os.environ, {'USERPROFILE': short, 'HOME': short}):
            self.assertEqual(user.home_dir(), short)        # a temp folder, until it is the profile
            self.as_profile(profile)
            with self.assertRaises(user.HomeFenced):
                user.home_dir()

    def test_a_marked_child_refuses_the_short_name_of_the_recorded_home(self):
        decoy = self.folder('kit-fenced-home-')
        rc, out = self.child(RESOLVE, self.short(decoy), marker=decoy)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out[0].startswith('fenced '), out)

    def test_a_marked_child_refuses_the_home_when_the_marker_is_its_short_name(self):
        decoy = self.folder('kit-fenced-home-')
        rc, out = self.child(RESOLVE, decoy, marker=self.short(decoy))
        self.assertEqual(rc, 0, out)
        self.assertTrue(out[0].startswith('fenced '), out)

    def test_the_extended_length_spelling_of_the_fenced_home_is_refused(self):
        long = extended(fence_decoy(self))
        with mock.patch.dict(os.environ, {'USERPROFILE': long, 'HOME': long}):
            with self.assertRaises(user.HomeFenced):
                user.home_dir()

    def test_a_marked_child_refuses_the_extended_length_spelling_of_the_recorded_home(self):
        decoy = self.folder('kit-fenced-home-')
        rc, out = self.child(RESOLVE, extended(decoy), marker=decoy)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out[0].startswith('fenced '), out)

    def test_a_marked_child_refuses_the_home_when_the_marker_has_the_extended_length_spelling(self):
        decoy = self.folder('kit-fenced-home-')
        rc, out = self.child(RESOLVE, decoy, marker=extended(decoy))
        self.assertEqual(rc, 0, out)
        self.assertTrue(out[0].startswith('fenced '), out)

    def test_the_extended_length_spelling_of_a_temp_home_passes(self):
        # a guard (green before the spelling was handled too): only a refused folder is refused
        fence_decoy(self)
        long = extended(self.folder('kit-home-'))
        with mock.patch.dict(os.environ, {'USERPROFILE': long, 'HOME': long}):
            self.assertEqual(user.home_dir(), long)


class HelperHomeTest(unittest.TestCase):
    """The shared helpers that build a child's environment give the child a temp home: a kit tool
    it starts reads no real machine config, and the fence never refuses it."""

    def test_child_home_is_a_temp_folder_made_again_when_it_is_gone(self):
        home = child_home()
        self.assertTrue(os.path.isdir(home), home)
        self.assertEqual(child_home(), home)
        with mock.patch.dict(os.environ, {'USERPROFILE': home, 'HOME': home}):
            self.assertTrue(same(user.home_dir(), home))        # not a real home: it passes
        gitfixture.rmtree(home)
        self.assertTrue(os.path.isdir(child_home()))

    def test_the_gate_fixtures_environment_carries_a_temp_home(self):
        from tests.gate import fixtures
        env = fixtures.test_env()
        with mock.patch.dict(os.environ, {'USERPROFILE': env['USERPROFILE']}):
            os.environ.pop('HOME', None)
            self.assertTrue(same(user.home_dir(), env['USERPROFILE']))      # the real one is refused
        self.assertEqual(env['HOME'], env['USERPROFILE'])
        self.assertTrue(same(env['USERPROFILE'], child_home()))


class MarkerTest(ChildCase):
    """Importing the tests package exports the marker, so every child that inherits the
    environment is fenced; a value already there is kept."""

    def test_importing_the_tests_package_exports_the_marker(self):
        home = self.folder('kit-home-')
        rc, out = self.child('''\
            import os, sys
            sys.path.insert(0, sys.argv[1])
            import tests
            print(ascii(os.path.normcase(os.environ.get('KIT_HOME_FENCE', 'unset'))))
            ''', home)
        self.assertEqual((rc, out), (0, [shown(home)]))

    def test_a_nested_tests_import_keeps_the_inherited_marker(self):
        # A tests process started by a test, under a temp home: the marker still names the outer
        # run's home, which stays refused beside the home this process started with.
        outer, home, other = (self.folder('kit-fenced-home-'), self.folder('kit-home-'),
                              self.folder('kit-home-'))
        rc, out = self.child('''\
            import os, sys
            sys.path.insert(0, sys.argv[1])
            import tests
            from kitlib.config import user
            print(ascii(os.path.normcase(os.environ.get('KIT_HOME_FENCE', 'unset'))))
            for home in sys.argv[2:]:
                os.environ['USERPROFILE'] = os.environ['HOME'] = home
                try:
                    user.home_dir()
                except user.HomeFenced:
                    print('fenced')
                else:
                    print('passed')
            ''', home, marker=outer, args=[home, outer, other])
        self.assertEqual((rc, out), (0, [shown(outer), 'fenced', 'fenced', 'passed']))


class FenceEnvTest(unittest.TestCase):
    """fence_env: what kit_tests and the tests package mark an environment with."""

    def test_it_records_the_home_the_process_resolves(self):
        home = temp_home(self)
        env = user.fence_env({})
        self.assertEqual(list(env), [MARK])
        self.assertTrue(os.path.isabs(env[MARK]))
        self.assertTrue(same(env[MARK], home), env)

    def test_a_marker_already_there_is_kept_and_an_empty_one_is_not(self):
        home = temp_home(self)
        self.assertEqual(user.fence_env({MARK: 'kept'}), {MARK: 'kept'})
        self.assertTrue(same(user.fence_env({MARK: ''})[MARK], home))

    def test_a_home_that_does_not_expand_marks_nothing(self):
        with mock.patch.dict(os.environ):
            for name in ('USERPROFILE', 'HOME', 'HOMEDRIVE', 'HOMEPATH'):
                os.environ.pop(name, None)
            self.assertEqual(user.fence_env({}), {})

    def test_the_name_is_the_one_the_tests_spell(self):
        self.assertEqual(user.FENCE_ENV, MARK)


class ProfileTest(ChildCase):
    """real_profile: the profile folder from Windows, which no environment variable moves."""

    def test_real_profile_ignores_the_environment(self):
        home = self.folder('kit-home-')
        rc, out = self.child('''\
            import os, sys
            sys.path.insert(0, sys.argv[1])
            os.environ['HOMEDRIVE'], os.environ['HOMEPATH'] = os.path.splitdrive(os.environ['HOME'])
            from kitlib.config import user
            print(ascii(user.real_profile()))
            print(ascii(os.path.normcase(user.home_dir())))
            ''', home)
        self.assertEqual(rc, 0, out)
        # the lock's own lookup is the second opinion (kitlib.lock.store.profile_dir)
        self.assertEqual(out, [ascii(lockstore.profile_dir()), shown(home)])

    def test_the_tests_process_refuses_the_windows_profile_too(self):
        # The fence and the marker both name a decoy; the profile Windows reports stays refused.
        # Only the path is computed.
        decoy = fence_decoy(self)
        profile = lockstore.profile_dir()
        with mock.patch.dict(os.environ, {MARK: decoy, 'USERPROFILE': profile, 'HOME': profile}):
            with self.assertRaises(user.HomeFenced):
                user.home_dir()


if __name__ == '__main__':
    unittest.main()
