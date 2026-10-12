"""kitlib.ledger.safepath: the rulings ledger's path checks (docs/ledger.md "Path checks"), and
every reader and writer of the ledger refusing through them."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.config import user as kuser
from kitlib.ledger import read, safepath, standing, write
from kitlib.ledger.errors import Refused

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'ledger.py')
TEXT = '# Rulings ledger\n\n## R-9001 | 2026-10-01 08:00Z | alpha | delegated\nDecides: One.\n'
FOREIGN = 'S-1-5-21-1-2-3-1001'


def put(path, text=TEXT):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def junction(test, link, target):
    try:
        import _winapi
        _winapi.CreateJunction(target, link)
    except (ImportError, AttributeError, OSError) as e:
        test.skipTest('cannot create a junction here: %s' % e)
    test.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))   # the junction, not its target


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = os.path.realpath(tmp.name)
        self.real = os.path.join(self.d, 'real', 'LEDGER.md')
        put(self.real)

    def through_junction(self):
        """`<d>/rulings/LEDGER.md`, where `rulings` is a junction to the real folder."""
        link = os.path.join(self.d, 'rulings')
        junction(self, link, os.path.dirname(self.real))
        return os.path.join(link, 'LEDGER.md')


class ReparseTest(Case):
    def test_a_plain_path_passes_and_a_missing_one_too(self):
        self.assertIsNone(safepath.refusal(self.real))
        self.assertIsNone(safepath.refusal(os.path.join(self.d, 'new', 'deeper', 'LEDGER.md')))

    def test_a_junction_folder_on_the_path_is_refused(self):
        path = self.through_junction()
        why = safepath.refusal(path)
        self.assertIsNotNone(why)
        self.assertIn('%s is a reparse point' % os.path.join(self.d, 'rulings'), why)

    def test_a_junction_further_up_is_refused(self):
        sub = os.path.join(self.d, 'real', 'a', 'b')
        put(os.path.join(sub, 'LEDGER.md'))
        link = os.path.join(self.d, 'up')
        junction(self, link, os.path.join(self.d, 'real'))
        why = safepath.refusal(os.path.join(link, 'a', 'b', 'LEDGER.md'))
        self.assertIn('%s is a reparse point' % link, why or '')

    def test_a_symlinked_ledger_file_is_refused(self):
        link = os.path.join(self.d, 'real', 'LINK.md')
        try:
            os.symlink(self.real, link)
        except OSError as e:
            self.skipTest('cannot create a symlink here (no Developer Mode or privilege): %s' % e)
        self.assertIn('%s is a reparse point' % link, safepath.refusal(link) or '')

    def test_a_reparse_refusal_names_the_fix(self):
        self.assertIn(safepath.REPARSE_FIX, safepath.refusal(self.through_junction()) or '')

    def test_a_hard_linked_ledger_file_is_refused_by_either_name(self):
        other = os.path.join(self.d, 'other.md')
        try:
            os.link(self.real, other)
        except OSError as e:
            self.skipTest('cannot create a hard link here: %s' % e)
        for name in (self.real, other):
            why = safepath.refusal(name) or ''
            self.assertIn('the file has 2 hard links', why, name)
            self.assertIn(safepath.LINK_FIX, why)
        os.remove(other)
        self.assertIsNone(safepath.refusal(self.real))

    def test_the_writer_replace_leaves_one_link(self):
        path = os.path.join(self.d, 'fresh', 'LEDGER.md')
        for _ in range(2):
            write.add(path, 'alpha', 'selected', 'q', 'a', 'd', 's', when='2026-10-06 08:00Z')
            self.assertEqual(os.lstat(path).st_nlink, 1)
            self.assertIsNone(safepath.refusal(path))

    def test_the_drive_root_is_not_checked(self):
        root = os.path.splitdrive(self.d)[0] + os.sep
        seen = []
        real_lstat = os.lstat

        def spy(p, *a, **k):
            seen.append(os.path.normcase(p))
            return real_lstat(p, *a, **k)
        with mock.patch.object(safepath.os, 'lstat', spy):
            self.assertIsNone(safepath.refusal(self.real))
        self.assertIn(os.path.normcase(self.real), seen)
        self.assertNotIn(os.path.normcase(root), seen)


class OwnerTest(Case):
    def test_the_real_owner_of_a_new_file_is_this_user_or_administrators(self):
        me, why = safepath.user_sid()
        self.assertIsNone(why)
        self.assertRegex(me, r'^S-1-5-')
        sid, why = safepath.owner_sid(self.real)
        self.assertIsNone(why)
        self.assertIn(sid, (me, safepath.ADMINISTRATORS))

    def owners(self, by_path):
        def fake(p):
            return by_path.get(os.path.normcase(p), (safepath.user_sid()[0], None))
        return mock.patch.object(safepath, 'owner_sid', fake)

    def test_a_foreign_owned_file_is_refused(self):
        with self.owners({os.path.normcase(self.real): (FOREIGN, None)}):
            why = safepath.refusal(self.real)
        self.assertIn('%s is owned by %s' % (self.real, FOREIGN), why or '')
        self.assertIn(safepath.OWNER_FIX, why)

    def test_the_real_lookup_failing_is_refused(self):
        """GetNamedSecurityInfoW's own failure (rc != 0, here no such file) fails closed."""
        absent = self.real + '.absent'
        sid, why = safepath.owner_sid(absent)
        self.assertIsNone(sid)
        self.assertEqual(why, 'GetNamedSecurityInfoW returned 2')
        real_owner_sid = safepath.owner_sid
        with mock.patch.object(safepath, 'owner_sid', lambda p: real_owner_sid(p + '.absent')):
            why = safepath.refusal(self.real) or ''
        self.assertIn('cannot read the owner of %s (GetNamedSecurityInfoW returned 2)' % self.real, why)
        self.assertIn(safepath.OWNER_FIX, why)

    def test_a_foreign_owned_folder_is_refused(self):
        folder = os.path.dirname(self.real)
        with self.owners({os.path.normcase(folder): (FOREIGN, None)}):
            why = safepath.refusal(self.real)
        self.assertIn('%s is owned by %s' % (folder, FOREIGN), why or '')

    def test_a_foreign_owner_above_the_folder_is_not_checked(self):
        with self.owners({os.path.normcase(self.d): (FOREIGN, None)}):
            self.assertIsNone(safepath.refusal(self.real))

    def test_administrators_and_system_are_accepted(self):
        for sid in (safepath.ADMINISTRATORS, safepath.SYSTEM):
            with self.owners({os.path.normcase(self.real): (sid, None)}):
                self.assertIsNone(safepath.refusal(self.real), sid)

    def test_an_owner_that_cannot_be_read_is_refused(self):
        with self.owners({os.path.normcase(self.real): (None, 'GetNamedSecurityInfoW returned 5')}):
            why = safepath.refusal(self.real)
        self.assertIn('cannot read the owner of %s (GetNamedSecurityInfoW returned 5)' % self.real,
                      why or '')


class ConsumerTest(Case):
    """Every reader and writer of the ledger refuses a path the checks refuse."""

    def test_the_one_reader_refuses(self):
        path = self.through_junction()
        got, why = read.read_entries(path)
        self.assertIsNone(got)
        self.assertIn('reparse point', why)
        self.assertEqual(read.read_file(path)[0], None)

    def test_the_standing_digest_beside_it_refuses(self):
        path = self.through_junction()
        put(os.path.join(os.path.dirname(self.real), standing.NAME), '# Standing\n')
        digest, why = standing.read_digest(standing.path_beside(path))
        self.assertIsNone(digest)
        self.assertIn('reparse point', why or '')

    def test_the_writer_refuses_and_writes_nothing(self):
        path = self.through_junction()
        with self.assertRaisesRegex(Refused, 'reparse point'):
            write.read_text(path)
        with self.assertRaisesRegex(Refused, 'reparse point'):
            write.add(path, 'alpha', 'selected', 'q', 'a', 'd', 's', when='2026-10-06 08:00Z')
        with self.assertRaisesRegex(Refused, 'reparse point'):
            write.write_atomic(path, 'x\n')
        with open(self.real, encoding='utf-8') as f:
            self.assertEqual(f.read(), TEXT)

    def test_a_missing_ledger_behind_a_junction_is_refused_not_started_from_the_template(self):
        os.remove(self.real)
        path = self.through_junction()
        with self.assertRaisesRegex(Refused, 'reparse point'):
            write.read_text(path)


class CliTest(Case):
    """tools/ledger.py: exit codes only."""

    def run_tool(self, path, *args):
        home = os.path.join(self.d, 'home')
        os.makedirs(kuser.config_dir(home), exist_ok=True)
        with open(kuser.config_file(home), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'ledger_path': path}))
        env = dict(os.environ, USERPROFILE=home, HOME=home, CLAUDE_AGENT_MODEL='')
        return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True,
                              env=env, timeout=60)

    def test_list_and_add_exit_1_through_a_junction(self):
        path = self.through_junction()
        r = self.run_tool(path, 'list', '--prov', 'delegated', '--since', '2026-01-01')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('reparse point', r.stderr)
        r = self.run_tool(path, 'add', '--repo', 'alpha', '--prov', 'selected', '--q', 'q', '--a', 'a',
                          '--decides', 'd', '--source', 's')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('reparse point', r.stderr)
        r = self.run_tool(self.real, 'list', '--prov', 'delegated', '--since', '2026-01-01')
        self.assertEqual((r.returncode, r.stdout.strip()), (0, 'R-9001 | 2026-10-01 | One.'), r.stderr)


if __name__ == '__main__':
    unittest.main()
