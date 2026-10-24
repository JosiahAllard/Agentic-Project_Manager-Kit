"""The ledger rule over the rulebooks and the standing digest (docs/guard.md "Rules": `ledger`):
the `rules` folder and the `.md` files directly in it, and STANDING.md, in `ledger_dir` or beside
`ledger_path` (kitlib.ledger.files.of_config), are written by ledger.py alone, by any role; other
files in those folders stay open, and a broken machine config still refuses every subagent write.

A subagent's write here runs in the ledger's folder, its own root outside git, so no other rule
(write-jail, main-checkout) would refuse it: the refusal is the ledger rule's."""
import os
import unittest

from kitlib.guard import context

from . import world as W
from .claudehost import decide
from .world import bash, event, ps

BOOK = 'writes a rulebook, which only ledger.py writes'
DIGEST = 'writes the standing digest, which the rulebooks replace'
LOG = 'writes the rulings ledger, which only ledger.py writes'


class Rulebooks(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.folder = os.path.dirname(self.w.ledger)
        self.rules = os.path.join(self.folder, 'rules')
        self.digest = os.path.join(self.folder, 'STANDING.md')
        os.makedirs(self.rules)
        self.alt = ('Instead: python "%s/tools/ledger.py" conflicts --repo <repo> --topic "<topic>" '
                    '--key <key> --text "<rule>"' % W.fwd(self.w.kit_dir))
        self.log_alt = 'Instead: python "%s/tools/ledger.py" add --repo <repo>' % W.fwd(self.w.kit_dir)

    def refused(self, ev, why=BOOK):
        d = decide(ev, self.w.ctx())
        self.assertIsNotNone(d, ev['tool_input'])
        self.assertEqual(d.rule, 'ledger', d.reason)
        self.assertIn(why, d.reason)
        self.assertIn(self.log_alt if why == LOG else self.alt, d.reason)

    def allowed(self, ev):
        d = decide(ev, self.w.ctx())
        self.assertIsNone(d, d and d.reason)

    def book(self, name='alpha.md'):
        return os.path.join(self.rules, name)

    def test_a_subagent_edit_of_a_rulebook_is_refused(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        f = self.folder
        self.refused(event('Write', {'file_path': self.book(), 'content': '# Alpha rules\n'}, f, sub=True))
        self.refused(event('Edit', {'file_path': self.book(), 'old_string': 'a', 'new_string': 'b'}, f, sub=True))
        self.refused(event('Write', {'file_path': self.book('Beta.MD'), 'content': ''}, f, sub=True))
        self.refused(event('Write', {'file_path': self.book('alpha.md.'), 'content': ''}, f, sub=True))
        self.refused(event('Write', {'file_path': self.book('alpha.md '), 'content': ''}, f, sub=True))
        self.refused(bash('echo "- a rule" >> rules/alpha.md', f, sub=True))
        self.refused(bash('cp draft.md rules/alpha.md', f, sub=True))
        self.refused(ps('Set-Content -Path %s -Value x' % self.book(), f, sub=True))

    def test_the_rulebooks_folder_itself_is_refused(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        self.refused(bash('rm -r rules', self.folder, sub=True))
        self.refused(ps('Remove-Item -Recurse -Force %s' % self.rules, self.folder, sub=True))

    def test_the_standing_digest_is_refused(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        self.refused(event('Write', {'file_path': self.digest, 'content': ''}, self.folder, sub=True), DIGEST)
        self.refused(bash('echo x >> STANDING.md', self.folder, sub=True), DIGEST)

    def test_every_role_is_refused_a_rulebook(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        self.refused(event('Write', {'file_path': self.book(), 'content': ''}, self.w.plain))
        self.w.environ = {context.ROLE_ENV: 'pm'}
        self.refused(event('Edit', {'file_path': self.book(), 'old_string': 'a', 'new_string': 'b'}, self.w.plain))
        self.refused(event('Write', {'file_path': self.digest, 'content': ''}, self.w.plain), DIGEST)

    def test_the_rules_folder_of_a_split_ledger_is_in_ledger_dir(self):
        split = os.path.join(self.w.base, 'pm', 'split')
        os.makedirs(os.path.join(split, 'rules'))
        self.w.user_config({'ledger_dir': split, 'ledger_repos': ['kit']})
        self.refused(event('Write', {'file_path': os.path.join(split, 'rules', 'kit.md'), 'content': ''},
                           split, sub=True))
        self.refused(event('Write', {'file_path': os.path.join(split, 'STANDING.md'), 'content': ''},
                           split, sub=True), DIGEST)
        # with ledger_dir set, the folder beside a ledger_path that is not set holds nothing of it
        self.allowed(event('Write', {'file_path': self.book(), 'content': ''}, self.folder, sub=True))

    def test_a_junction_still_reaches_them(self):
        # the names compare as the filesystem opens them, links and junctions resolved
        self.w.user_config({'ledger_path': self.w.ledger})
        open(self.book(), 'w').close()
        open(self.digest, 'w').close()
        link = os.path.join(self.w.base, 'link')
        try:
            import _winapi
            _winapi.CreateJunction(self.folder, link)
        except (ImportError, OSError) as e:
            self.skipTest('no junction: %s' % e)
        self.addCleanup(os.rmdir, link)
        for path, why in ((os.path.join(link, 'rules', 'alpha.md'), BOOK), (os.path.join(link, 'STANDING.md'), DIGEST),
                          (os.path.join(link, 'rules', 'gamma.md'), BOOK)):
            for sub in (True, False):    # the owner's session has no write jail to stop it first
                with self.subTest(path, sub=sub):
                    self.refused(event('Write', {'file_path': path, 'content': ''}, self.w.plain, sub=sub), why)
        self.refused(bash('rm -r %s' % W.fwd(os.path.join(link, 'rules')), self.w.plain, sub=True))
        self.refused(bash('rm %s/*' % W.fwd(os.path.join(link, 'rules')), self.w.plain, sub=True))
        self.allowed(event('Write', {'file_path': os.path.join(link, 'NOTES.md'), 'content': ''}, self.folder,
                           sub=True))

    def test_a_short_name_still_reaches_them(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        book = self.book('a-long-rulebook-name.md')
        open(book, 'w').close()
        open(self.digest, 'w').close()
        self.refused(event('Write', {'file_path': self.short(book), 'content': ''}, self.folder, sub=True))
        short_digest = os.path.join(self.short(self.folder), 'STANDING.md')
        self.refused(event('Write', {'file_path': short_digest, 'content': ''}, self.folder, sub=True), DIGEST)

    def short(self, path):
        """The short (8.3) spelling of `path`; the test is skipped when its volume gives none."""
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.WinDLL('kernel32').GetShortPathNameW(path, buf, len(buf))
        short = buf.value if 0 < n < len(buf) else ''
        if not short or os.path.normcase(short) == os.path.normcase(path):
            self.skipTest('the temp folder\'s volume gives no short (8.3) names')
        return short

    def test_a_stream_suffix_is_the_file_itself(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        f = self.folder
        for path, why in ((self.digest + '::$DATA', DIGEST), (self.book() + '::$DATA', BOOK),
                          (self.book() + ':notes', BOOK), (self.book() + ':notes:$DATA', BOOK),
                          (self.digest + ':x', DIGEST)):
            with self.subTest(path):
                self.refused(event('Write', {'file_path': path, 'content': ''}, f, sub=True), why)
        self.refused(bash("echo x > 'STANDING.md::$DATA'", f, sub=True), DIGEST)
        # PowerShell writes .\x::$DATA to x's stream (rules\x::$DATA it reads as a provider's name and refuses)
        self.refused(ps("Set-Content -Path '.\\rules\\alpha.md::$DATA' -Value x", f, sub=True))
        self.refused(ps("Set-Content -Path '%s::$DATA' -Value x" % self.digest, f, sub=True), DIGEST)
        self.allowed(event('Write', {'file_path': os.path.join(f, 'NOTES.md::$DATA'), 'content': ''}, f, sub=True))

    def test_a_wildcard_that_can_name_them_is_refused(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        f = self.folder
        for cmd, why in (('rm rules/*', BOOK), ('rm -f rules/*.txt', BOOK), ('rm rules/[ab]*', BOOK),
                         ('rm STAND*', DIGEST), ('rm ?TANDING.md', DIGEST), ('rm -r ru*', BOOK),
                         ('rm -r *', LOG), ('rm %s/*' % W.fwd(self.rules), BOOK), ('rm r*/alpha.md', BOOK)):
            with self.subTest(cmd):
                self.refused(bash(cmd, f, sub=True), why)
        for cmd, why in (('Remove-Item rules\\*', BOOK), ('Remove-Item %s\\*' % self.rules, BOOK),
                         ('Remove-Item STAND*', DIGEST), ('Remove-Item -Recurse ru*', BOOK)):
            with self.subTest(cmd):
                self.refused(ps(cmd, f, sub=True), why)
        self.allowed(bash('rm -f *.txt', f, sub=True))
        self.allowed(bash('rm -f rules/drafts/*', f, sub=True))
        self.allowed(ps('Remove-Item NOTES*', f, sub=True))

    def test_a_wildcard_reaches_the_ledger_file_too(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        d = decide(bash('rm *.md', self.folder, sub=True), self.w.ctx())
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, 'ledger', d.reason)
        self.assertIn(LOG, d.reason)

    def git_refused(self, ev, why=BOOK):
        d = decide(ev, self.w.ctx())
        self.assertIsNotNone(d, ev['tool_input'])
        self.assertEqual(d.rule, 'ledger', d.reason)
        self.assertIn(why, d.reason)

    def test_git_that_removes_moves_or_restores_them_is_refused(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        f = self.folder
        for cmd, why in (('git rm rules/alpha.md', BOOK), ('git rm --cached STANDING.md', DIGEST),
                         ('git rm -r rules', BOOK), ('git rm "rules/*.md"', BOOK), ('git rm -r .', LOG),
                         ('git mv STANDING.md old.md', DIGEST), ('git mv draft.md rules/new.md', BOOK),
                         ('git checkout -- STANDING.md', DIGEST), ('git checkout HEAD~1 -- rules/alpha.md', BOOK),
                         ('git checkout STANDING.md', DIGEST), ('git restore rules/alpha.md', BOOK),
                         ('git restore --staged --worktree STANDING.md', DIGEST),
                         ('git checkout-index -f rules/alpha.md', BOOK),
                         ('git -C %s rm rules/alpha.md' % W.fwd(f), BOOK)):
            with self.subTest(cmd):
                self.git_refused(bash(cmd, f, sub=True), why)
        self.git_refused(ps('git rm rules\\alpha.md', f, sub=True))
        self.git_refused(ps('git mv STANDING.md old.md', f), DIGEST)
        # the PM is refused too: only ledger.py writes them
        self.w.environ = {context.ROLE_ENV: 'pm'}
        self.git_refused(bash('git -C %s rm STANDING.md' % W.fwd(f), self.w.plain), DIGEST)

    def test_git_that_leaves_them_alone_passes_the_rule(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        f = self.folder
        for cmd in ('git rm NOTES.md', 'git restore --staged STANDING.md', 'git mv a.txt b.txt',
                    'git rm ":!STANDING.md"', 'git status', 'git diff STANDING.md', 'git rm rules/drafts/a.md'):
            with self.subTest(cmd):
                d = decide(bash(cmd, f, sub=True), self.w.ctx())
                self.assertTrue(d is None or d.rule != 'ledger', d and d.reason)
        # git in a tree that does not hold the ledger: even a computed pathspec is not this rule's
        d = decide(bash('git rm "$f"', self.w.wt, sub=True), self.w.ctx())
        self.assertTrue(d is None or d.rule != 'ledger', d and d.reason)

    def test_git_that_takes_its_paths_from_elsewhere_is_refused(self):
        # a path list the guard cannot read can name every path, the books included: fail closed
        self.w.user_config({'ledger_path': self.w.ledger})
        f = self.folder
        for cmd in ('git rm --pathspec-from-file=list.txt', 'git rm -r --pathspec-from-file - < list.txt',
                    'git rm --pathspec-fr=list.txt', 'git restore --pathspec-from-file=list.txt',
                    'git restore --staged --worktree --pathspec-from-file=list.txt',
                    'git checkout --pathspec-from-file=list.txt', 'git checkout HEAD --pathspec-from-file list.txt',
                    'git checkout-index -a', 'git checkout-index -f -a', 'git checkout-index -af',
                    'git checkout-index --all', 'git checkout-index --al', 'git checkout-index --stdin < list.txt'):
            with self.subTest(cmd):
                self.git_refused(bash(cmd, f, sub=True), LOG)
        self.git_refused(ps('git checkout-index --all', f, sub=True), LOG)
        for cmd, cwd in (('git restore --staged --pathspec-from-file=list.txt', f),
                         ('git checkout-index -a', self.w.wt), ('git rm --pathspec-from-file=list.txt', self.w.wt)):
            with self.subTest(cmd, cwd=cwd):
                d = decide(bash(cmd, cwd, sub=True), self.w.ctx())
                self.assertTrue(d is None or d.rule != 'ledger', d and d.reason)

    def test_restore_staged_with_a_long_option_not_pinned_is_judged(self):
        # a one-letter long option is an abbreviation to git (`--w` is `--worktree`): not pinned, so judged
        self.w.user_config({'ledger_path': self.w.ledger})
        f = self.folder
        for cmd, why in (('git restore --staged --w rules/alpha.md', BOOK), ('git restore -S --w STANDING.md', DIGEST),
                         ('git restore --staged --no-staged STANDING.md', DIGEST),
                         # every prefix git takes for --worktree, or for a negation (`--no-st` is
                         # `--no-staged`; `--no` and `--no-` may negate any option)
                         ('git restore --staged --wor rules/alpha.md', BOOK),
                         ('git restore --staged --work STANDING.md', DIGEST),
                         ('git restore -S --workt STANDING.md', DIGEST),
                         ('git restore --staged --no-st STANDING.md', DIGEST),
                         ('git restore --staged --no STANDING.md', DIGEST),
                         ('git restore -S --no- rules/alpha.md', BOOK),
                         ('git restore --staged --no-w STANDING.md', DIGEST)):
            with self.subTest(cmd):
                self.git_refused(bash(cmd, f, sub=True), why)
        # options the guard names in full or by a prefix it pins keep the index-only exception
        for cmd in ('git restore --staged --quiet STANDING.md', 'git restore --sta --source HEAD STANDING.md',
                    'git restore --staged --no-ov STANDING.md'):
            with self.subTest(cmd):
                d = decide(bash(cmd, f, sub=True), self.w.ctx())
                self.assertTrue(d is None or d.rule != 'ledger', d and d.reason)

    def test_a_bracket_in_a_folder_name_is_that_name(self):
        # an edit's path is never a wildcard: `[PM]` names the folder, not the letter P or M
        folder = os.path.join(self.w.base, '[PM]', 'rulings')
        os.makedirs(os.path.join(folder, 'rules'))
        ledger = os.path.join(folder, 'LEDGER.md')
        self.w.user_config({'ledger_path': ledger})
        for path, why in ((os.path.join(folder, 'STANDING.md'), DIGEST), (os.path.join(folder, 'rules', 'kit.md'), BOOK),
                          (ledger, LOG)):
            for sub in (True, False):
                with self.subTest(path, sub=sub):
                    self.refused(event('Write', {'file_path': path, 'content': ''}, folder, sub=sub), why)
        self.refused(bash("rm '[PM]/rulings/STANDING.md'", self.w.base, sub=True), DIGEST)
        self.refused(bash("rm '[PM]'/rulings/*", self.w.base, sub=True), LOG)
        self.allowed(event('Write', {'file_path': os.path.join(folder, 'NOTES.md'), 'content': ''}, folder, sub=True))

    def test_a_namespace_prefix_is_the_file_itself(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        for pre in ('\\\\?\\', '//?/', '\\\\.\\', '\\??\\'):
            for path, why in ((self.digest, DIGEST), (self.book(), BOOK), (self.w.ledger, LOG)):
                for sub in (True, False):
                    with self.subTest(pre + path, sub=sub):
                        self.refused(event('Write', {'file_path': pre + path, 'content': ''}, self.folder, sub=sub),
                                     why)
        self.refused(bash("echo x > '//?/%s'" % W.fwd(self.digest), self.folder, sub=True), DIGEST)
        self.refused(ps("Set-Content -LiteralPath '\\\\?\\%s' -Value x" % self.book(), self.folder, sub=True))
        # (a subagent's prefixed write is the write jail's to refuse; the owner's here is no one's)
        self.allowed(event('Write', {'file_path': '\\\\?\\' + os.path.join(self.folder, 'NOTES.md'), 'content': ''},
                           self.w.plain))
        from kitlib.guard import rules
        self.assertEqual(rules.unprefixed('\\\\?\\UNC\\host\\share\\STANDING.md'), '\\\\host\\share\\STANDING.md')
        self.assertEqual(rules.unprefixed('//?/unc/host/share/x'), '\\\\host/share/x')
        self.assertEqual(rules.unprefixed('Z:\\pm\\x'), 'Z:\\pm\\x')
        self.assertEqual(rules.unprefixed('C:\\??\\Z:\\pm\\x'), 'Z:\\pm\\x')

    def volume_name(self):
        """The volume GUID name (`\\\\?\\Volume{...}\\`) of the test folder's drive; skipped when
        the volume gives none."""
        import ctypes
        buf = ctypes.create_unicode_buffer(100)
        drive = os.path.splitdrive(self.folder)[0] + os.sep
        if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive, buf, 100):
            self.skipTest('GetVolumeNameForVolumeMountPointW gives no volume name for %s' % drive)
        return buf.value

    def test_a_volume_guid_name_is_the_file_itself(self):
        # realpath gives the drive's name of a volume GUID name
        self.w.user_config({'ledger_path': self.w.ledger})
        vol = self.volume_name()
        for path, why in ((self.digest, DIGEST), (self.book('new.md'), BOOK)):
            volume = vol + os.path.splitdrive(path)[1].lstrip('\\')
            with self.subTest(volume):
                self.refused(event('Write', {'file_path': volume, 'content': ''}, self.w.plain), why)
        rules_glob = vol + os.path.splitdrive(self.rules)[1].lstrip('\\') + '\\*.md'
        self.refused(ps("Remove-Item -Path '%s'" % rules_glob, self.w.plain))

    def test_a_namespace_prefix_with_a_wildcard_is_still_matched(self):
        # the prefix's own `?` is no wildcard: the one after it is
        self.w.user_config({'ledger_path': self.w.ledger})
        for pre in ('\\\\?\\', '\\??\\'):
            for folder, pattern, why in ((self.rules, '*.md', BOOK), (self.folder, 'STAND*', DIGEST),
                                         (self.folder, '*', LOG)):
                target = pre + folder + '\\' + pattern
                # bash: the prefix quoted (its `?` would glob), the wildcard bare (quoted it is literal)
                for maker, cmd in ((ps, 'cmd /c del "%s"' % target), (ps, "Remove-Item -Path '%s'" % target),
                                   (bash, "rm '%s\\'%s" % (pre + folder, pattern))):
                    with self.subTest(cmd):
                        self.refused(maker(cmd, self.w.plain), why)
        self.allowed(ps("Remove-Item -Path '\\\\?\\%s'" % os.path.join(self.folder, 'NOTES*'), self.w.plain))

    def test_a_question_mark_folder_after_the_drive_root_is_a_wildcard(self):
        # `<drive>:/??/` spells the NT prefix `\??\` too, but with no drive after it it is a `??`
        # wildcard folder of the drive root: it names a two-letter folder there (nothing is written)
        drive = os.path.splitdrive(self.w.plain)[0]
        for ledger, rest in ((os.path.join(drive + os.sep, 'q7', 'LEDGER.md'), '??'),
                             (os.path.join(drive + os.sep, 'q7', 'pm', 'LEDGER.md'), '??\\*')):
            self.w.user_config({'ledger_path': ledger})
            for name, why in (('STANDING.md', DIGEST), ('rules\\alpha.md', BOOK), ('LEDGER.md', LOG)):
                path = rest + '\\' + name
                for maker, cmd in ((bash, 'rm %s:/%s' % (drive[0], path.replace('\\', '/'))),
                                   (ps, 'Remove-Item %s\\%s' % (drive, path)), (ps, 'cmd /c del %s\\%s' % (drive, path)),
                                   (ps, 'Remove-Item \\%s' % path)):
                    with self.subTest(cmd, ledger=ledger):
                        self.refused(maker(cmd, self.w.plain), why)
            self.allowed(ps('Remove-Item %s\\%s\\NOTES.md' % (drive, rest), self.w.plain))

    def test_git_in_a_work_tree_that_holds_a_split_ledger(self):
        split = os.path.join(self.w.wt, 'ledger')
        os.makedirs(os.path.join(split, 'rules'))
        os.makedirs(os.path.join(self.w.wt, 'src'))
        self.w.user_config({'ledger_dir': split, 'ledger_repos': ['kit']})
        src = os.path.join(self.w.wt, 'src')
        for cmd, why in (('git rm :/ledger/STANDING.md', DIGEST), ('git rm ":(top)ledger/rules/kit.md"', BOOK),
                         ('git rm -r ../ledger', BOOK), ('git rm "$f"', BOOK)):
            with self.subTest(cmd):
                self.git_refused(bash(cmd, src, sub=True), why)
        d = decide(bash('git rm a.py', src, sub=True), self.w.ctx())
        self.assertTrue(d is None or d.rule != 'ledger', d and d.reason)

    def test_the_pm_tool_write_passes(self):
        # the command line is not refused; the hook never sees the tool's own writes
        self.w.user_config({'ledger_path': self.w.ledger})
        self.w.environ = {context.ROLE_ENV: 'pm'}
        tool = W.fwd(os.path.join(self.w.kit_dir, 'tools', 'ledger.py'))
        self.allowed(bash('python "%s" conflicts --repo kit --topic "alpha" --key alpha-one --text "a rule"'
                          % tool, self.w.plain))
        self.allowed(bash('python "%s" add --repo kit --prov typed --q "q" --a "a" --decides "d" --source "s" '
                          '--rule alpha-one --topic alpha --checked 0123abcd' % tool, self.w.plain))
        self.allowed(bash('python "%s" rule-drop alpha-one --repo kit --prov typed --why "w" --source "s"'
                          % tool, self.w.plain))

    def test_an_unrelated_file_in_the_same_folder_passes(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        f = self.folder
        for path in (os.path.join(f, 'NOTES.md'), os.path.join(f, 'STANDING.md.bak'),
                     self.book('alpha.txt'), os.path.join(self.rules, 'drafts', 'alpha.md')):
            with self.subTest(path):
                self.allowed(event('Write', {'file_path': path, 'content': ''}, f, sub=True))
        self.allowed(bash('echo x >> NOTES.md', f, sub=True))
        self.allowed(bash('cat rules/alpha.md', f, sub=True))
        self.allowed(event('Read', {'file_path': self.book()}, f, sub=True))

    def test_no_ledger_config_no_rulebook_rule(self):
        self.allowed(event('Write', {'file_path': self.book(), 'content': ''}, self.folder, sub=True))
        self.allowed(event('Write', {'file_path': self.digest, 'content': ''}, self.w.plain))

    def write_config(self, raw):
        self.w.user_config({})
        with open(self.w.config_file, 'wb') as f:
            f.write(raw)

    def test_a_broken_ledger_dir_refuses_every_subagent_write(self):
        target = os.path.join(self.w.wt, 'a.py')
        for raw in (b'{"ledger_dir": 5}', b'{"ledger_dir": ["Z:/pm/split"], "ledger_repos": ["kit"]}'):
            with self.subTest(raw):
                self.write_config(raw)
                self.assertIn('ledger_dir must be a string', self.w.ctx().user().get('broken') or '')
                for ev in (event('Write', {'file_path': target, 'content': ''}, self.w.wt, sub=True),
                           bash('echo x > notes.txt', self.w.wt, sub=True)):
                    d = decide(ev, self.w.ctx())
                    self.assertIsNotNone(d, ev['tool_name'])
                    self.assertEqual(d.rule, 'ledger', d.reason)
                    self.assertIn('The owner or the PM session fixes it. Instead: python -m json.tool "%s"'
                                  % W.fwd(self.w.config_file), d.reason)
                self.assertIsNone(decide(event('Write', {'file_path': target, 'content': ''}, self.w.wt),
                                         self.w.ctx()))

    def test_a_strict_reader_refusal_still_yields_the_rulebooks(self):
        # a key this kit's schema does not know: the tolerant read, with ledger_dir relative to the
        # PM repository
        self.write_config(b'{"ledger_dir": "split", "ledger_repos": ["kit"], "a_key_this_kit_does_not_know": {}}')
        split = os.path.join(self.w.pm_repo, 'split')
        os.makedirs(os.path.join(split, 'rules'))
        user = self.w.ctx().user()
        self.assertIn('error', user)
        self.assertNotIn('broken', user)
        self.assertEqual(user['ledger_dir'], split)
        self.refused(event('Write', {'file_path': os.path.join(split, 'rules', 'kit.md'), 'content': ''},
                           split, sub=True))
        self.refused(event('Write', {'file_path': os.path.join(split, 'STANDING.md'), 'content': ''},
                           split, sub=True), DIGEST)


if __name__ == '__main__':
    unittest.main()
