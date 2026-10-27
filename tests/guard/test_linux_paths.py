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
from kitlib.guard import context, jail, modes, paths, rules
from kitlib.guard import shell as sh
from tests.platforms import posix_only

from .claudehost import action, decide
from .linux import PosixWorld, on_linux
from .world import bash, event

VFAT = [('/', 'ext4'), ('/media/usb', 'vfat')]
UNKNOWN = [('/', 'ext4'), ('/data', 'somefs')]


class LinuxCase(unittest.TestCase):
    mounts = [('/', 'ext4')]
    # the folders that exist (posixpath.isdir, by real path): a bash cd on Linux into any other is
    # unknown (shell._linux_cd)
    dirs = ('/work/repo', '/work/repo/.claude/worktrees/wt', '/work/repo/.claude/worktrees/wt/sub',
            '/home/u', '/d')
    unplaced_denied = False     # hold the rule unplaced at deny (world.deny_unplaced), else its default

    def setUp(self):
        cm = on_linux(self.mounts)
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        self.w = PosixWorld()
        p = mock.patch('posixpath.isdir', lambda path: posixpath.realpath(path) in self.dirs)
        p.start()
        self.addCleanup(p.stop)
        if self.unplaced_denied:
            p = mock.patch.dict(modes.DEFAULTS, {rules.UNPLACED: modes.DENY})
            p.start()
            self.addCleanup(p.stop)

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
        dirs = rules._TarDirs()
        for text in ('/work/repo', '\\x', '..', '/srv', '~/y'):
            dirs.move(sh.Word(text, False, 0, 0), True)
        self.assertEqual([w.text for w in dirs.seen],
                         ['/work/repo', '/work/repo/\\x', '/work/repo/\\x/..', '/srv', '~/y'])

    def test_a_backslash_folder_after_the_main_checkout_is_refused(self):
        # the main checkout's own -C is judged too, so this line was refused before the fix as well
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

    def test_an_absolute_name_is_judged_alone_and_in_the_output_dir(self):
        # curl joins --output-dir to every -o name, an absolute one too; each reading is judged
        for command in ('curl --output-dir /work/repo -o /work/repo/.claude/worktrees/wt/x https://example.invalid/a',
                        'curl --output-dir /work/repo/.claude/worktrees/wt -o /srv/x https://example.invalid/a'):
            d = self.sub(command)
            self.assertIsNotNone(d, command)
            self.assertEqual(d.verdict, 'deny', '%s: %s' % (command, d.reason))
        self.assertIsNone(self.sub('curl --output-dir /work/repo/.claude/worktrees/wt -o '
                                   '/work/repo/.claude/worktrees/wt/x https://example.invalid/a'))

    def test_a_trailing_backslash_of_the_output_dir_is_kept(self):
        # on Linux `wt\` is a sibling folder of the worktree, not the worktree
        command = "curl --create-dirs --output-dir '/work/repo/.claude/worktrees/wt\\' -o x https://example.invalid/a"
        d = self.sub(command)
        self.assertIsNotNone(d, command)
        self.assertEqual(d.verdict, 'deny', '%s: %s' % (command, d.reason))


class CopyIntoAddOnly(LinuxCase):
    """rules.into_folder on Linux: a source's name is what follows its last `/`, so `a\\b` copied
    into a folder lands at <folder>/a\\b; an add-only folder that already holds that name refuses."""

    def setUp(self):
        LinuxCase.setUp(self)
        add, wt = '/work/add', self.w.wt
        dirs, files = {add, wt}, {wt + '/a\\b', add + '/a\\b'}
        for name, fake in (('isdir', lambda p: posixpath.normpath(p) in dirs),
                           ('isfile', lambda p: posixpath.normpath(p) in files),
                           ('lexists', lambda p: posixpath.normpath(p) in files | dirs),
                           ('exists', lambda p: posixpath.normpath(p) in files | dirs)):
            p = mock.patch('posixpath.' + name, fake)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(jail, 'data_roots', lambda ctx, cwd: [(add, jail.ADD_ONLY)])
        p.start()
        self.addCleanup(p.stop)

    def test_a_backslash_name_already_there_is_refused(self):
        d = self.sub("cp 'a\\b' /work/add/")
        self.assertIsNotNone(d)
        self.assertEqual(d.verdict, 'deny', d.reason)

    def test_the_name_is_taken_after_the_last_slash_only(self):
        into = (sh.Word('/work/add/', False, 0, 10), [sh.Word('x/a\\b\\', False, 0, 0)], False)
        self.assertEqual(rules.into_folder('/work/add', into[0], into, self.w.wt), ['/work/add/a\\b\\'])


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


def fake_links(links):
    """A posixpath.realpath that follows `links` ({link: where it points}, absolute), chains too;
    any other path is itself (normalised)."""
    def realpath(p, *a, **k):
        p = posixpath.normpath(p)
        for _ in range(40):
            for link, to in links.items():
                if p == link or p.startswith(link + '/'):
                    p = posixpath.normpath(to + p[len(link):])
                    break
            else:
                return p
        return p
    return realpath


class DotDotAfterALink(LinuxCase):
    """shell.resolve_path (paths.physical): Linux takes `..` after a symbolic link from where the
    link points (the kernel resolves it after the link), so `link/..` is the target's parent, never
    the link's own folder: a worktree link d -> /work/repo/src makes d/../f the main checkout's f.
    A cd into the link goes where the text cannot tell (CdRealPath): unplaced, held at deny here."""
    unplaced_denied = True

    def setUp(self):
        LinuxCase.setUp(self)
        wt = self.w.wt
        links = {wt + '/d': '/work/repo/src', wt + '/a': wt + '/b', wt + '/b': '/work/repo/src',
                 wt + '/in': wt + '/x/y'}
        p = mock.patch('posixpath.realpath', fake_links(links))
        p.start()
        self.addCleanup(p.stop)

    def denied(self, command, **kw):
        d = self.sub(command, **kw)
        self.assertIsNotNone(d, command)
        self.assertEqual(d.verdict, 'deny', '%s: %s' % (command, d.reason))

    def test_resolve_path_takes_dot_dot_from_the_link_target(self):
        self.assertEqual(sh.resolve_path('d/../STANDING.md', self.w.wt), '/work/repo/STANDING.md')
        self.assertEqual(sh.resolve_path(self.w.wt + '/d/../f', None), '/work/repo/f')
        self.assertEqual(sh.resolve_path('../f', self.w.wt + '/d'), '/work/repo/f')

    def test_a_shell_write_through_a_link_and_dot_dot_is_refused(self):
        self.denied('echo x > d/../STANDING.md')

    def test_a_write_tool_call_through_a_link_and_dot_dot_is_refused(self):
        payload = event('Write', {'file_path': self.w.wt + '/d/../STANDING.md', 'content': ''}, self.w.wt, sub=True)
        d = decide(payload, self.w.ctx())
        self.assertIsNotNone(d)
        self.assertEqual(d.verdict, 'deny', d.reason)

    def test_a_cd_into_a_link_then_dot_dot_is_refused(self):
        self.denied('cd d && echo x > ../f')

    def test_a_chain_of_links_is_followed(self):
        self.assertEqual(sh.resolve_path('a/../f', self.w.wt), '/work/repo/f')
        self.denied('echo x > a/../f')

    def test_dot_dot_after_a_plain_folder_stays_in_the_worktree(self):
        self.assertEqual(sh.resolve_path('sub/../f', self.w.wt), self.w.wt + '/f')
        self.assertIsNone(self.sub('echo x > sub/../f'))
        self.assertIsNone(self.sub('cd sub && echo x > ../f'))

    def test_a_link_inside_the_worktree_then_dot_dot_stays_in_it(self):
        self.assertEqual(sh.resolve_path('in/../f', self.w.wt), self.w.wt + '/x/f')
        self.assertIsNone(self.sub('echo x > in/../f'))


class LogicalCd(LinuxCase):
    """shell.cd_target on Linux: bash's cd (and pushd) is -L by default, folding `..` in its place
    against $PWD, not after the link; only cd -P is physical. With a link in -> x/y in the
    worktree, the logical reading of in/.. (the worktree) is not its physical one (x), and cd in
    names a folder by a link, so both go where the text cannot tell (CdRealPath); cd -P in/.. is
    x. A write is still read physically from the tracked folder (resolve_path)."""
    dirs = LinuxCase.dirs + ('/work/repo/.claude/worktrees/wt/x', '/work/repo/.claude/worktrees/wt/x/y')
    unplaced_denied = True

    def setUp(self):
        LinuxCase.setUp(self)
        wt = self.w.wt
        p = mock.patch('posixpath.realpath', fake_links({wt + '/in': wt + '/x/y'}))
        p.start()
        self.addCleanup(p.stop)

    def cd(self, text, home_vars=False):
        _parsed, cmds = sh.parse(text, sh.BASH, self.w.wt, home=self.w.home)
        return sh.cd_target(cmds[-1], self.w.wt, self.w.home, home_vars=home_vars)

    def denied(self, command):
        d = self.sub(command)
        self.assertIsNotNone(d, command)
        self.assertEqual(d.verdict, 'deny', '%s: %s' % (command, d.reason))

    def test_cd_dot_dot_after_a_link_folds_from_the_text(self):
        self.denied('cd in && cd .. && cd .. && touch f')
        self.denied('cd in/../.. && touch f')
        self.denied('pushd in && pushd .. && pushd .. && touch f')

    def test_cd_minus_l_and_minus_p(self):
        wt = self.w.wt
        self.assertEqual(self.cd('cd in/..'), (True, None))
        self.assertEqual(self.cd('cd -L in/..', home_vars=True), (True, None))
        self.assertEqual(self.cd('cd -P in/..', home_vars=True), (True, wt + '/x'))
        self.assertEqual(self.cd('cd -P in', home_vars=True), (True, wt + '/x/y'))

    def test_a_cd_into_the_link_is_unknown(self):
        self.assertEqual(self.cd('cd in'), (True, None))
        self.refused('cd in && echo x > ../f', rules.UNPLACED)

    def test_a_write_from_inside_the_link_is_read_physically(self):
        self.assertEqual(sh.resolve_path('../f', self.w.wt + '/in'), self.w.wt + '/x/f')

    def test_cdpath_set_minus_p_and_pushd_plus_n_are_unknown(self):
        for text in ('export CDPATH=/work/repo; cd x', 'CDPATH=/work/repo cd x', 'set -P; cd x',
                     'set -o physical; cd x', 'pushd +1', 'pushd -1'):
            self.assertEqual(self.cd(text), (True, None), text)


class CdRealPath(LinuxCase):
    """shell.cd_target and the env -C folder on Linux: the folder tracked is a real path. A cd
    whose logical reading (`..` folded from the text) is not the real path of its text, or whose
    folder does not exist, goes where the text cannot tell: bash falls back to the unfolded text
    when the folded folder is missing, and a shell started by env -C in a folder reached through a
    link starts at its real path. A CDPATH, SHELLOPTS, cdable_vars or set -P anywhere on the line
    makes a cd in any nested shell, eval or substitution unknown too. A write after a cd the text
    cannot tell is unplaced (a warned allow by default), held at deny here as the owner's machine
    config can (world.deny_unplaced)."""
    dirs = ('/work/repo', '/work/repo/src', '/work/repo/build', '/work/repo/docs',
            '/work/repo/.claude/worktrees/wt', '/work/repo/.claude/worktrees/wt/sub',
            '/work/repo/.claude/worktrees/wt/docs')
    unplaced_denied = True

    def setUp(self):
        LinuxCase.setUp(self)
        p = mock.patch('posixpath.realpath', fake_links({self.w.wt + '/d': self.w.main + '/src'}))
        p.start()
        self.addCleanup(p.stop)

    def unplaced(self, command):
        self.refused(command, rules.UNPLACED)

    def test_a_cd_whose_folded_folder_is_missing_is_unknown(self):
        self.unplaced('cd d/../build && echo x > f')

    def test_a_shell_started_by_env_minus_c_through_a_link_starts_at_its_real_path(self):
        self.refused("env -C d bash -c 'cd .. && echo x > f'", 'main-checkout')
        self.assertEqual(sh.chdir_real(self.w.wt + '/d', self.w.wt), '/work/repo/src')

    def test_cdpath_on_the_line_reaches_a_nested_shell(self):
        self.unplaced("CDPATH=/work/repo bash -c 'cd docs && echo x > STANDING.md'")

    def test_cdpath_on_the_line_reaches_an_eval(self):
        self.unplaced("export CDPATH=/work/repo; eval 'cd docs; echo x > STANDING.md'")

    def test_set_minus_p_on_the_line_reaches_a_substitution(self):
        self.unplaced('set -P; x=$(cd d/..; touch f)')
        self.unplaced('CDPATH=/work/repo; x=`cd docs; echo x > STANDING.md`')

    def test_cdable_vars_is_unknown(self):
        self.unplaced("shopt -s cdable_vars; bash -c 'cd docs && echo x > STANDING.md'")

    def test_a_cd_into_a_missing_folder_is_unknown(self):
        self.unplaced('cd nowhere; echo x > f')

    def test_unplaced_warns_at_its_default_mode(self):
        with mock.patch.dict(modes.DEFAULTS, {rules.UNPLACED: modes.WARN}):
            d = guard.decide(action(bash('cd d/../build && echo x > f', self.w.wt, sub=True)), self.w.ctx())
        self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED), d.reason)
        self.assertIn('cannot tell where it writes', d.reason)

    def test_a_plain_cd_in_the_worktree_is_still_tracked(self):
        self.assertIsNone(self.sub('cd sub && echo x > f'))
        self.assertIsNone(self.sub("bash -c 'cd sub && echo x > f'"))


class DotDotAfterARealLink(unittest.TestCase):
    @posix_only('a symbolic link made on the real filesystem')
    def test_dot_dot_after_a_real_link_is_the_target_parent(self):
        base = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        os.makedirs(os.path.join(base, 'wt', 'sub'))
        os.makedirs(os.path.join(base, 'out', 'src'))
        os.symlink(os.path.join(base, 'out', 'src'), os.path.join(base, 'wt', 'd'))
        with on_linux():
            self.assertEqual(paths.physical(base + '/wt/d/../f'), base + '/out/f')
            self.assertEqual(paths.physical(base + '/wt/sub/../f'), base + '/wt/f')


class UnderTempStrict(LinuxCase):
    """rules._under_temp lifts the core.hooksPath checks for a repository under temp, so on Linux
    it compares with the strict fold: on a mount in doubt a case variant of temp is not temp."""
    mounts = UNKNOWN

    def test_a_case_variant_of_the_temp_folder_is_not_temp(self):
        self.w.temp = '/data/Tmp'
        self.assertFalse(rules._under_temp(self.w.ctx(), '/data/tmp/r'))
        self.assertTrue(rules._under_temp(self.w.ctx(), '/data/Tmp/r'))

    def test_a_hooks_path_change_there_is_refused(self):
        # the main session: a subagent's write jail refuses it first
        self.w.temp = '/data/Tmp'
        d = decide(bash('git -C /data/tmp/r config core.hooksPath /x', self.w.wt), self.w.ctx())
        self.assertIsNotNone(d)
        self.assertEqual((d.verdict, d.rule), ('deny', 'git-hooks-path'), d.reason)


class BashCdOnLinux(LinuxCase):
    """shell.cd_target on Linux: bash runs `cd` (and pushd, popd) by that exact name, so `CD` or
    `chdir` moves nothing, and `/d` is a folder, not cmd's drive switch."""

    def cd(self, text, cwd):
        _parsed, cmds = sh.parse(text, sh.BASH, cwd, home=self.w.home)
        return sh.cd_target(cmds[0], cwd, self.w.home)

    def test_only_the_exact_verb_is_a_cd(self):
        for verb in ('CD', 'Cd', 'chdir', 'Set-Location', 'sl'):
            self.assertEqual(self.cd('%s /work/repo' % verb, self.w.wt), (False, self.w.wt), verb)
        self.assertEqual(self.cd('cd /work/repo', self.w.wt), (True, '/work/repo'))
        self.assertEqual(self.cd('pushd /work/repo', self.w.wt), (True, '/work/repo'))

    def test_slash_d_is_a_folder(self):
        self.assertEqual(self.cd('cd /d', self.w.wt), (True, '/d'))
        self.assertEqual(self.cd('cd /d /work/repo/.claude/worktrees/wt', '/work/repo'), (True, None))

    def test_a_write_after_a_failed_upper_case_cd_is_judged_where_it_runs(self):
        self.refused('cd /work/repo; CD .claude/worktrees/wt; echo x > STANDING.md', 'main-checkout')

    def test_a_cd_after_an_assignment_moves_the_shell(self):
        self.refused('X=1 cd /work/repo; echo x > STANDING.md', 'main-checkout')


class BashCdOnWindows(unittest.TestCase):
    """shell.cd_target on Windows is unchanged: Git Bash's cd verb in any case, and cmd's /d
    passed over."""

    def test_an_upper_case_cd_with_slash_d_is_a_cd(self):
        _parsed, cmds = sh.parse('CD /d C:/x', sh.BASH, 'C:/w')
        with mock.patch('kitlib.platform.NAME', 'windows'):
            self.assertTrue(sh.cd_target(cmds[0], 'C:/w')[0])


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
