"""The Codex half's locate(): configured app and CLI paths follow a Codex update."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from optional.bridge.core.messages import DeliveryError
from optional.bridge.hosts.codex import locate as codex_locate
from optional.bridge.hosts.codex.locate import locate, package_path

PUBLISHER = '2p2nqsd0c76g0'
FAMILY = 'OpenAI.Codex_' + PUBLISHER


class LocateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-locate-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.apps = root / 'WindowsApps'
        self.old = self.apps / ('OpenAI.Codex_26.1021.2667.0_x64__' + PUBLISHER)
        self.new = self.package('26.1023.6123.0', b'current cli')
        self.bin = root / 'Local' / 'OpenAI' / 'Codex' / 'bin'
        self.current_cli = self.copy('08d187884f2330d1', b'current cli')
        (self.bin / '593bad775a30e636').mkdir()
        (self.bin / '593bad775a30e636' / 'rg.exe').write_bytes(b'current cli')  # not codex.exe
        self.stale_cli = str(self.bin / 'aaaaaaaaaaaaaaaa' / 'codex.exe')
        self.stale_app = str(self.old / 'app' / 'ChatGPT.exe')
        patcher = mock.patch.object(codex_locate, 'package_path', side_effect=self.installed)
        self.lookups = patcher.start()
        self.addCleanup(patcher.stop)

    def installed(self, family):
        if family != FAMILY:
            raise DeliveryError('codex-package-unavailable')
        return self.new

    def package(self, version, cli):
        folder = self.apps / ('OpenAI.Codex_%s_x64__%s' % (version, PUBLISHER))
        (folder / 'app' / 'resources').mkdir(parents=True)
        (folder / 'app' / 'ChatGPT.exe').write_bytes(b'app')
        (folder / 'app' / 'resources' / 'codex.exe').write_bytes(cli)
        return folder

    def copy(self, name, data):
        folder = self.bin / name
        folder.mkdir(parents=True)
        (folder / 'codex.exe').write_bytes(data)
        return str(folder / 'codex.exe')

    def test_stale_app_and_cli_paths_move_to_the_current_install(self):
        images, profile = locate([self.stale_cli], {'profile_root': self.temp.name,
            'desktop_executable': self.stale_app})
        self.assertEqual(images, [self.current_cli])
        self.assertEqual(profile, {'profile_root': self.temp.name,
            'desktop_executable': str(self.new / 'app' / 'ChatGPT.exe')})
        self.lookups.assert_called_once_with(FAMILY)

    def test_a_single_image_value_stays_a_single_value(self):
        image, _ = locate(self.stale_cli, {'desktop_executable': self.stale_app})
        self.assertEqual(image, self.current_cli)

    def test_the_cli_copy_is_chosen_by_its_bytes_not_its_folder(self):
        self.copy('ffffffffffffffff', b'older cli')
        images, _ = locate([self.stale_cli], {'desktop_executable': self.stale_app})
        self.assertEqual(images, [self.current_cli])

    def test_no_matching_cli_copy_names_the_image_setting(self):
        Path(self.current_cli).write_bytes(b'tampered cli')
        with self.assertRaises(DeliveryError) as raised:
            locate([self.stale_cli], {'desktop_executable': self.stale_app})
        self.assertEqual(raised.exception.code, 'codex-cli-image-unmatched-native-image-0')

    def test_two_identical_cli_copies_are_refused_not_guessed(self):
        self.copy('bbbbbbbbbbbbbbbb', b'current cli')
        with self.assertRaises(DeliveryError) as raised:
            locate([self.stale_cli], {'desktop_executable': self.stale_app})
        self.assertEqual(raised.exception.code, 'codex-cli-image-ambiguous-native-image-0')

    def test_an_uninstalled_package_is_refused(self):
        other = str(self.apps / 'OpenAI.Codex_1.0.0.0_x64__zzzzzzzzzzzzz' / 'app' / 'ChatGPT.exe')
        with self.assertRaises(DeliveryError) as raised:
            locate([self.stale_cli], {'desktop_executable': other})
        self.assertEqual(raised.exception.code, 'codex-package-unavailable')

    def test_two_package_families_are_refused(self):
        other = str(self.apps / 'OpenAI.Codex_1.0.0.0_x64__zzzzzzzzzzzzz' / 'app' / 'ChatGPT.exe')
        with self.assertRaises(DeliveryError) as raised:
            locate([self.stale_app], {'desktop_executable': other})
        self.assertEqual(raised.exception.code, 'codex-package-ambiguous')

    def test_without_a_package_path_nothing_is_looked_up_or_moved(self):
        terminal = str(Path(self.temp.name) / 'npm' / 'codex.exe')
        values = ([self.stale_cli, terminal], {'profile_root': self.temp.name})
        self.assertEqual(locate(*values), values)
        self.lookups.assert_not_called()

    def test_values_of_other_shapes_pass_through_for_the_configuration_check(self):
        for images in (True, [True], {'a': 1}):
            with self.subTest(images=images):
                result, profile = locate(images, {'desktop_executable': self.stale_app})
                self.assertEqual(result, images)
                self.assertEqual(profile['desktop_executable'], str(self.new / 'app' / 'ChatGPT.exe'))


class PackageLookupTests(unittest.TestCase):
    def test_a_family_not_installed_is_refused_by_windows(self):
        # The real kernel32 lookup, on any machine: no package has this publisher id.
        with self.assertRaises(DeliveryError) as raised:
            package_path('OpenAI.Codex_0000000000000')
        self.assertEqual(raised.exception.code, 'codex-package-unavailable')


if __name__ == '__main__':
    unittest.main()
