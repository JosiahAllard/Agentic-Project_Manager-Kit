"""The mountinfo reader of kitlib.platform.linux.paths (read_mountinfo, parse_mountinfo, _unescape),
which the guard's per-mount case fold (kitlib.guard.paths._read_mounts) and the holders scan
(aliased, proc_options) share. Pure text: runs on any platform."""
import os
import shutil
import tempfile
import unittest

from kitlib.platform.linux import paths

TABLE = ('22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n'
         '30 22 8:17 / /media/my\\040usb rw,relatime shared:5 - vfat /dev/sdb1 rw\n'
         '41 22 8:1 /srv/a\\134b /mnt/bind rw,relatime shared:1 - ext4 /dev/sda1 rw\n'
         '31 22 0:50 / /data rw - nfs4 host:/x rw\n'
         '32 22 0:51 / /data rw master:2 - tmpfs tmpfs rw\n'
         'not a mountinfo line\n')


class Parse(unittest.TestCase):

    def test_each_mount_in_table_order(self):
        self.assertEqual(paths.parse_mountinfo(TABLE), [
            paths.Mount('8:1', '/', '/', 'ext4'),
            paths.Mount('8:17', '/', '/media/my usb', 'vfat'),
            paths.Mount('8:1', '/srv/a\\b', '/mnt/bind', 'ext4'),
            paths.Mount('0:50', '/', '/data', 'nfs4'),
            paths.Mount('0:51', '/', '/data', 'tmpfs')])

    def test_a_line_with_no_fstype_has_none(self):
        self.assertEqual(paths.parse_mountinfo('22 1 8:1 / / rw - \n'),
                         [paths.Mount('8:1', '/', '/', None)])

    def test_a_short_line_is_skipped(self):
        self.assertEqual(paths.parse_mountinfo('22 1 8:1 / - ext4 /dev/sda1 rw\n'), [])

    def test_mounts_keeps_device_root_and_point(self):
        self.assertEqual(paths._mounts(TABLE)[2], ('8:1', '/srv/a\\b', '/mnt/bind'))
        self.assertEqual(len(paths._mounts(TABLE)), 5)


class Unescape(unittest.TestCase):

    def test_octal_escapes_are_decoded(self):
        self.assertEqual(paths._unescape('/a\\040b\\011c\\134d\\012e'), '/a b\tc\\d\ne')

    def test_a_backslash_and_digits_that_are_not_octal_stay(self):
        for text in ('/a\\089', '/a\\8', '/a\\04', '/a\\١٢٣', '/a\\'):
            with self.subTest(text):
                self.assertEqual(paths._unescape(text), text)

    def test_a_non_octal_escape_does_not_stop_a_later_one(self):
        self.assertEqual(paths._unescape('/a\\089\\040b'), '/a\\089 b')


class Read(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_the_text_is_read(self):
        f = os.path.join(self.folder, 'mountinfo')
        with open(f, 'wb') as out:
            out.write(TABLE.encode('utf-8'))
        self.assertEqual(paths.read_mountinfo(f), TABLE)

    def test_a_missing_table_is_an_oserror(self):
        with self.assertRaises(OSError):
            paths.read_mountinfo(os.path.join(self.folder, 'none'))


if __name__ == '__main__':
    unittest.main()
