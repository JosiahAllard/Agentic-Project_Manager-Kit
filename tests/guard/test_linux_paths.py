"""The guard's path semantics on Linux (docs/linux-support-plan.md section 3, "Guard path semantics
and the safety review", slice 8): one class per finding of that section's table that lives in the
guard, each read through tests/guard/linux.py (kitlib.platform.NAME 'linux', posixpath, a
stand-in mount table), so it runs on Windows and on Linux alike. A subagent session in the linked
worktree /work/repo/.claude/worktrees/wt of the main checkout /work/repo is refused what it is
refused on Windows: the main checkout, a root search, a write outside its roots."""
import os
import posixpath
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import guard
from kitlib.guard import context, jail, paths, rules
from kitlib.guard import shell as sh
from tests.platforms import posix_only

from .claudehost import decide
from .linux import PosixWorld, on_linux
from .world import bash

VFAT = [('/', 'ext4'), ('/media/usb', 'vfat')]
UNKNOWN = [('/', 'ext4'), ('/data', 'somefs')]


class LinuxCase(unittest.TestCase):
    mounts = [('/', 'ext4')]

    def setUp(self):
        cm = on_linux(self.mounts)
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        self.w = PosixWorld()

    def sub(self, command, cwd=None, ctx=None):
        """The Decision for a subagent's Bash `command` in `cwd` (the worktree), None for an allow."""
        return decide(bash(command, cwd or self.w.wt, sub=True), ctx or self.w.ctx())

    def refused(self, command, rule, **kw):
        d = self.sub(command, **kw)
        self.assertIsNotNone(d, command)
        self.assertEqual((d.verdict, d.rule), ('deny', rule), '%s: %s' % (command, d.reason))
        return d


class F1AbsolutePaths(LinuxCase):
    """Finding 1: an absolute /path is placed, not None (shell.resolve_path), so shell writes and
    git targets outside the worktree are judged."""

    def test_resolve_path_places_an_absolute_path(self):
        self.assertEqual(sh.resolve_path('/work/repo/f', None), '/work/repo/f')
        self.assertEqual(sh.resolve_path('/work/repo/../x', '/w'), '/work/x')
        self.assertEqual(sh.resolve_path('f', '/work/wt'), '/work/wt/f')

    def test_a_drive_spelling_is_a_relative_name(self):
        self.assertEqual(sh.resolve_path('C:x', '/w'), '/w/C:x')
        self.assertEqual(sh.resolve_path('C:/x', '/w'), '/w/C:/x')

    def test_a_write_into_the_main_checkout_is_refused(self):
        self.refused('echo x > /work/repo/f', 'main-checkout')

    def test_a_cd_into_the_main_checkout_then_a_write_is_refused(self):
        self.refused('cd /work/repo && echo x > f', 'main-checkout')

    def test_git_dash_c_on_the_main_checkout_is_refused(self):
        self.refused('git -C /work/repo commit -m x', 'git-main-history')

    def test_a_write_outside_every_root_is_refused(self):
        self.refused('echo x > /srv/elsewhere/f', 'write-jail')

    def test_a_write_in_the_worktree_passes(self):
        self.assertIsNone(self.sub('echo x > /work/repo/.claude/worktrees/wt/f'))
        self.assertIsNone(self.sub('echo x > f'))


class F2SlashIsARoot(LinuxCase):
    """Finding 2: `/` is a root (rules.is_root, root_search, jail.own_root)."""

    def test_slash_is_a_root(self):
        self.assertTrue(rules.is_root('/', None))
        self.assertTrue(rules.is_root('//', None))
        self.assertTrue(rules.is_root('/..', '/work'))
        self.assertFalse(rules.is_root('/work', None))

    def test_a_search_of_slash_is_refused(self):
        self.refused('grep -r x /', 'root-search')
        self.refused('du /', 'root-search')

    def test_a_search_run_from_slash_is_refused(self):
        self.refused('grep -r x', 'root-search', cwd='/')

    def test_a_session_at_slash_owns_no_root(self):
        self.assertIsNone(jail.own_root(self.w.ctx(), '/', lambda p: rules.is_root(p, None)))


class F3LedgerGlob(LinuxCase):
    """Finding 3: ledger_glob folds the pattern and the held path the same way."""

    def ctx(self, ledger):
        ctx = self.w.ctx()
        ctx.user = lambda: {'ledger_path': ledger}
        ctx.ledger_files = lambda: None
        return ctx

    def test_a_wildcard_matches_a_held_name_spelt_in_capitals(self):
        ctx = self.ctx('/pm/Rulings/LEDGER.md')
        self.assertEqual(rules.ledger_glob(ctx, '/pm/Rulings/LED*'), 'log')
        self.assertEqual(rules.ledger_glob(ctx, '/pm/Rulings/*.md'), 'log')

    def test_a_case_sensitive_mount_does_not_match_another_case(self):
        self.assertIsNone(rules.ledger_glob(self.ctx('/pm/Rulings/LEDGER.md'), '/pm/Rulings/led*'))


class F3LedgerGlobFolding(F3LedgerGlob):
    mounts = VFAT

    def test_a_case_sensitive_mount_does_not_match_another_case(self):
        pass

    def test_a_folding_mount_matches_any_case(self):
        ctx = self.ctx('/media/usb/pm/LEDGER.md')
        self.assertEqual(rules.ledger_glob(ctx, '/media/usb/pm/led*'), 'log')
        self.assertEqual(rules.ledger_glob(ctx, '/media/usb/PM/LED*'), 'log')


class F4Backslash(LinuxCase):
    """Finding 4: bash drops every unquoted backslash, so a write target built with escapes is the
    path bash writes."""

    def word(self, text):
        p, cmds = sh.parse(text, sh.BASH, '/w')
        return cmds[0].words[-1].text

    def test_bash_drops_an_unquoted_backslash(self):
        self.assertEqual(self.word('echo \\.\\./x'), '../x')
        self.assertEqual(self.word('echo \\/work'), '/work')
        self.assertEqual(self.word('echo C:\\x'), 'C:x')
        self.assertEqual(self.word('echo "a\\.b"'), 'a\\.b')      # in double quotes it stays

    def test_an_escaped_dot_dot_into_the_main_checkout_is_refused(self):
        self.refused('echo x > \\.\\./\\.\\./\\.\\./f', 'main-checkout')

    def test_an_escaped_slash_is_absolute(self):
        self.refused('echo x > \\/work/repo/f', 'main-checkout')


class F5NoMsysOrTmpMapping(LinuxCase):
    """Finding 5: /c/x is /c/x, and /tmp is /tmp: no Git Bash mapping."""

    def test_a_one_letter_folder_is_not_a_drive(self):
        self.assertEqual(sh.resolve_path('/c/Users/f', '/w'), '/c/Users/f')
        d = self.refused('echo x > /c/Users/f', 'write-jail')
        self.assertNotIn('C:', d.reason)

    def test_slash_tmp_is_not_the_agent_temp_folder(self):
        self.w.environ['AGENT_TEMP'] = '/agent'
        self.refused('echo x > /tmp/f', 'write-jail')
        self.assertIsNone(self.sub('echo x > /agent/f'))

    def test_slash_tmp_passes_when_it_is_the_temp_folder(self):
        self.w.temp = '/tmp'
        self.assertIsNone(self.sub('echo x > /tmp/f'))


class F6CaseFolding(LinuxCase):
    """Finding 6: the placer folds case where the mount does (paths.norm, the mount table). A
    known-folding mount folds; an unknown fstype, or no table, folds only for what the guard
    refuses, never for what it allows (the jail's allowed roots compare case-sensitively)."""
    mounts = VFAT

    def judge(self, target, own, ro=None):
        rs = [jail.Root(own, jail.CHECKOUT)] + ([jail.Root(ro, jail.DATA, jail.READ_ONLY)] if ro else [])
        return jail.judge(target, rs)[0]

    def test_a_folding_mount_folds_below_its_mount_point(self):
        self.assertEqual(paths.norm('/media/usb/Repo/X.md'), '/media/usb/repo/x.md')
        self.assertEqual(paths.norm('/Home/U/X'), '/Home/U/X')
        self.assertEqual(paths.norm('/media/usb/Repo/X.md', fold=False), '/media/usb/Repo/X.md')

    def test_a_folding_mount_judges_a_case_variant_as_the_same_folder(self):
        self.assertIsNone(self.judge('/media/usb/repo/f', '/media/usb/Repo'))
        self.assertEqual(self.judge('/media/usb/wt/ro/f', '/media/usb/wt', ro='/media/usb/wt/RO'),
                         jail.READ_ONLY)

    def test_a_case_sensitive_mount_keeps_case_variants_apart(self):
        self.assertEqual(self.judge('/work/repo/f', '/work/Repo'), jail.OUTSIDE)
        self.assertIsNone(self.judge('/work/wt/ro/f', '/work/wt', ro='/work/wt/RO'))

    def test_a_case_variant_of_the_temp_folder_on_a_folding_mount_passes(self):
        self.w.temp = '/media/usb/Tmp'
        self.assertIsNone(self.sub('echo x > /media/usb/tmp/f'))


class F6UnknownMount(F6CaseFolding):
    mounts = UNKNOWN

    def test_a_folding_mount_folds_below_its_mount_point(self):
        self.assertEqual(paths.norm('/data/Repo/X.md'), '/data/repo/x.md')
        self.assertEqual(paths.norm('/data/Repo/X.md', strict=True), '/data/Repo/X.md')

    def test_a_folding_mount_judges_a_case_variant_as_the_same_folder(self):
        pass

    def test_a_case_variant_of_the_temp_folder_on_a_folding_mount_passes(self):
        pass

    def test_an_unknown_mount_never_widens_an_allowed_root(self):
        self.assertEqual(self.judge('/data/repo/f', '/data/Repo'), jail.OUTSIDE)
        self.assertIsNone(self.judge('/data/Repo/f', '/data/Repo'))

    def test_an_unknown_mount_folds_for_a_read_only_root(self):
        self.assertEqual(self.judge('/data/wt/ro/f', '/data/wt', ro='/data/wt/RO'), jail.READ_ONLY)

    def test_a_case_variant_sibling_of_an_allowed_root_is_refused(self):
        self.w.temp = '/data/Tmp'
        self.refused('echo x > /data/tmp/f', 'write-jail')
        self.assertIsNone(self.sub('echo x > /data/Tmp/f'))

    def test_a_case_variant_of_the_worktree_is_refused(self):
        w = PosixWorld(base='/data')
        d = self.sub('echo x > /data/repo/.claude/worktrees/WT/f', cwd=w.wt, ctx=w.ctx())
        self.assertEqual(d.verdict, 'deny')


class F6NoMountTable(F6UnknownMount):
    mounts = None

    def test_a_case_sensitive_mount_keeps_case_variants_apart(self):
        self.assertEqual(self.judge('/work/repo/f', '/work/Repo'), jail.OUTSIDE)


class F7HomeVariables(LinuxCase):
    """Finding 7: $HOME and ~ name the home folder (shell.HOME_VAR_RE, rules._ROOT_NAMES). No gap
    in the variable names on Linux; the test pins it."""

    def test_home_is_a_root(self):
        for t in ('$HOME', '${HOME}', '~', '/home/u'):
            self.assertTrue(rules.is_root(t, None, self.w.ctx()), t)

    def test_a_search_of_home_is_refused(self):
        self.refused('grep -r x $HOME', 'root-search')
        self.refused('du "${HOME}"', 'root-search')

    def test_a_write_in_home_is_refused(self):
        self.refused('echo x > ~/f', 'write-jail')
        self.refused('cd ~ && echo x > f', 'write-jail')


class F10Symlinks(LinuxCase):
    """Finding 10: the jail resolves links (jail.real through the placer): a link in the worktree
    into the main checkout is judged where it leads."""

    def test_a_link_into_the_main_checkout_is_judged_there(self):
        link = self.w.wt + '/link'
        real = posixpath.realpath

        def fake(p, *a, **k):
            return self.w.main + p[len(link):] if p.startswith(link) else real(p, *a, **k)
        with mock.patch('posixpath.realpath', fake):
            rs = [jail.Root(self.w.wt, jail.CHECKOUT)]
            self.assertEqual(jail.judge(link + '/f', rs)[0], jail.OUTSIDE)

    @posix_only('a symbolic link made on the real filesystem')
    def test_a_real_link_out_of_a_root_is_outside(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        wt, out = os.path.join(base, 'wt'), os.path.join(base, 'out')
        os.makedirs(wt)
        os.makedirs(out)
        os.symlink(out, os.path.join(wt, 'link'))
        rs = [jail.Root(wt, jail.CHECKOUT)]
        self.assertEqual(jail.judge(os.path.join(wt, 'link', 'f'), rs)[0], jail.OUTSIDE)
        self.assertIsNone(jail.judge(os.path.join(wt, 'f'), rs)[0])


class Placed(LinuxCase):
    """context._placed: an absolute /path names one place on Linux; a relative or empty one does
    not (fail closed)."""

    def test_an_absolute_path_is_placed(self):
        self.assertTrue(context._placed('/pm/rulings/LEDGER.md'))

    def test_a_relative_or_empty_path_is_not(self):
        for p in ('pm/LEDGER.md', '', 'C:/pm', '~/pm'):
            self.assertFalse(context._placed(p), p)


class TarFolders(LinuxCase):
    """rules._TarDirs.move: tar reads each -C from the one in force with `/` joins; only a
    leading `/` (or `~`) is rooted, and `\\` is a name's character."""

    def denied(self, command):
        d = self.sub(command)
        self.assertIsNotNone(d, command)
        self.assertEqual(d.verdict, 'deny', '%s: %s' % (command, d.reason))

    def test_dot_then_dot_dot_reaches_the_main_checkout(self):
        self.denied('tar -C . -C .. -xf a.tar')

    def test_a_folder_then_dot_dot_dot_dot_reaches_the_main_checkout(self):
        self.denied('tar -C x -C ../.. -xf a.tar')

    def test_a_backslash_folder_is_read_in_the_one_in_force(self):
        self.denied("tar -C /work/repo -C '\\x' -xf a.tar")

    def test_folders_in_the_worktree_pass(self):
        self.assertIsNone(self.sub('tar -C x -C y -xf a.tar'))
        self.assertIsNone(self.sub('tar -C /work/repo/.claude/worktrees/wt -C y -xf a.tar'))


class CurlOutputDir(LinuxCase):
    """rules._under: on Linux an -o name is absolute only when it starts with `/`; a quoted `\\x`
    or `C:/x` is a name in --output-dir."""

    def test_a_backslash_or_drive_name_is_placed_in_the_output_dir(self):
        for name in ("'\\x'", "'C:/x'"):
            command = 'curl --output-dir /work/repo -o %s https://example.invalid/a' % name
            d = self.sub(command)
            self.assertIsNotNone(d, command)
            self.assertEqual(d.verdict, 'deny', '%s: %s' % (command, d.reason))

    def test_an_absolute_name_ignores_the_output_dir(self):
        self.assertIsNone(self.sub('curl --output-dir /work/repo -o /work/repo/.claude/worktrees/wt/x '
                                   'https://example.invalid/a'))


class InTempStrict(LinuxCase):
    """rules._in_temp lifts checks (worktree remove's clean check, the scratch repository
    exemption), so on Linux it compares with the strict fold: a mount in doubt does not fold."""
    mounts = UNKNOWN

    def test_a_case_variant_of_the_temp_folder_is_not_temp(self):
        self.w.temp = '/data/Tmp'
        self.assertFalse(rules._in_temp(self.w.ctx(), '/data/tmp/x'))
        self.assertTrue(rules._in_temp(self.w.ctx(), '/data/Tmp/x'))


class InTempFolding(InTempStrict):
    mounts = VFAT

    def test_a_case_variant_of_the_temp_folder_is_not_temp(self):
        self.w.temp = '/media/usb/Tmp'
        self.assertTrue(rules._in_temp(self.w.ctx(), '/media/usb/tmp/x'))


class InDoubtFilesystems(LinuxCase):
    """nfs, nfs4 and zfs can fold case (a server's or a dataset's setting), so they are in doubt:
    folded for what the guard refuses, not for what it allows."""
    mounts = [('/', 'ext4'), ('/data', 'nfs4')]

    def test_they_fold_only_for_refusals(self):
        for fstype in ('nfs', 'nfs4', 'zfs'):
            self.assertTrue(paths._folds(fstype, False), fstype)
            self.assertFalse(paths._folds(fstype, True), fstype)

    def test_a_case_variant_of_the_temp_folder_on_nfs_is_refused(self):
        self.w.temp = '/data/Tmp'
        self.refused('echo x > /data/tmp/f', 'write-jail')


class InsideOnLinux(LinuxCase):
    """jail.inside: on Linux a root's trailing `\\` is part of its name, not a separator."""

    def test_a_trailing_backslash_is_kept(self):
        self.assertFalse(jail.inside('/a/x', '/a\\'))
        self.assertTrue(jail.inside('/a\\/x', '/a\\'))
        self.assertTrue(jail.inside('/a/x', '/a/'))


class MountInfo(unittest.TestCase):
    """paths._read_mounts: /proc/self/mountinfo's mount point (field 5, octal escapes decoded) and
    fstype (the first field after ` - `); a later line at the same point wins."""
    TABLE = ('22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n'
             '30 22 8:17 / /media/my\\040usb rw,relatime shared:5 - vfat /dev/sdb1 rw\n'
             '31 22 0:50 / /data rw - nfs4 host:/x rw\n'
             '32 22 0:51 / /data rw master:2 - tmpfs tmpfs rw\n'
             'not a mountinfo line\n')

    def read(self, text):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        f = os.path.join(folder, 'mountinfo')
        if text is not None:
            with open(f, 'w', encoding='utf-8', newline='\n') as out:
                out.write(text)
        with mock.patch.object(paths, 'MOUNTINFO', f):
            return paths._read_mounts()

    def test_the_table_is_read(self):
        self.assertEqual(self.read(self.TABLE),
                         [('/', 'ext4'), ('/data', 'tmpfs'), ('/media/my usb', 'vfat')])

    def test_no_table_or_an_empty_one_is_none(self):
        self.assertIsNone(self.read(None))
        self.assertIsNone(self.read(''))

    def test_octal_escapes_are_decoded(self):
        self.assertEqual(paths._unescape('/a\\040b\\011c\\134d'), '/a b\tc\\d')


class Ready(unittest.TestCase):
    def test_the_guard_declares_linux_ready(self):
        self.assertEqual(guard.READY_PLATFORMS, ('windows', 'linux'))


if __name__ == '__main__':
    unittest.main()
