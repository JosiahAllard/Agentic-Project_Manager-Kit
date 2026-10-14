"""kitlib.guard.place reads a path's place from the filesystem and must answer exactly what
kitlib.git.repo.classify (one git rev-parse) answers (docs/guard.md "State"). Every case here is
asked of both on real scratch repositories; each also says whether the filesystem answered or
handed the path to git, so a case that silently always falls back is caught."""
import os
import shutil
import subprocess
import tempfile
import unittest

from kitlib.git import repo
from kitlib.guard import context, place
from tests import gitfixture
from tests.realgit import real_git

FS, GIT = 'fs', 'git'


def files_are_mine():
    """True when a folder this process creates is owned by its own user. An elevated run makes
    files owned by Administrators, so every place is handed to git on the ownership check (git's
    safe.directory rule): a test that expects the filesystem answer then expects git's instead."""
    d = tempfile.mkdtemp(prefix='kit-owner-')
    try:
        return place.owned_by_me(d)
    finally:
        shutil.rmtree(d, True)


MINE = files_are_mine()
OWNER_NOTE = 'files made by this run are not owned by its user (an elevated run): the owner check hands to git'


def free_drive():
    """A drive letter nothing uses now, or None."""
    for letter in 'YXWVUTSRQPONMLKJIHG':
        if not os.path.exists(letter + ':' + os.sep):
            return letter + ':'
    return None


class Counted(object):
    def __init__(self):
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        return repo.classify(path)


def answers_like_git(test, cases):
    """Each (name, path, kind, via): the filesystem's place equals git's, is of `kind` (None: any),
    and came from the filesystem (FS) or from the fallback (GIT)."""
    for name, path, kind, via in cases:
        with test.subTest(name):
            want = repo.classify(path)
            fb = Counted()
            got = place.classify(path, fallback=fb)
            test.assertEqual(got, want)
            if kind is not None:
                test.assertEqual(got.kind, kind)
            if via == FS and not MINE and kind != repo.NONE:
                via = GIT                # OWNER_NOTE (NONE has no owner to check)
            test.assertEqual(fb.calls, 1 if via == GIT else 0,
                             '%s: expected the %s answer' % (name, via))


@real_git("checks the filesystem's places against git's own rev-parse")
class FilesystemPlaces(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = gitfixture.TempRepo()
        r = cls.repo
        r.write('src/a.py', 'a\n')
        r.commit('src', 'src/a.py')
        cls.wt = r.worktree('wt')
        os.makedirs(os.path.join(cls.wt, 'sub'))
        cls.outside = os.path.join(r.base, 'outside-wt')
        r.git('worktree', 'add', '-q', cls.outside, '-b', 'kit/outside')
        cls.plain = os.path.join(r.base, 'plain', 'deep')
        os.makedirs(cls.plain)
        # A work tree whose git directory lives elsewhere (`.git` is a gitdir file, no commondir):
        # git calls it a main checkout.
        cls.sep = os.path.join(r.base, 'sep')
        cls.sep_git = os.path.join(r.base, 'sep-store.git')
        r.git('init', '-q', '--separate-git-dir', cls.sep_git, cls.sep, cwd=r.base)
        # Shapes the filesystem reader hands to git.
        cls.badfile = os.path.join(r.root, 'badfile')
        r.write('badfile/.git', 'not a gitdir line\n')
        cls.deadlink = os.path.join(r.root, 'deadlink')
        r.write('deadlink/.git', 'gitdir: ../nowhere\n')
        cls.emptygit = os.path.join(r.root, 'emptygit', 'inner')
        os.makedirs(os.path.join(r.root, 'emptygit', '.git'))
        os.makedirs(cls.emptygit)
        cls.bare = os.path.join(r.base, 'bare.git')
        r.git('init', '-q', '--bare', cls.bare, cwd=r.base)
        cls.wtcfg = os.path.join(r.base, 'wtcfg')
        r.git('init', '-q', cls.wtcfg, cwd=r.base)
        r.git('config', 'core.worktree', cls.wtcfg, cwd=cls.wtcfg)
        cls.ext = os.path.join(r.base, 'ext')
        r.git('init', '-q', cls.ext, cwd=r.base)
        r.git('config', 'extensions.worktreeConfig', 'true', cwd=cls.ext)
        cls.headerbare = os.path.join(r.base, 'headerbare')
        r.git('init', '-q', cls.headerbare, cwd=r.base)
        cls.headerfalse = os.path.join(r.base, 'headerfalse')
        r.git('init', '-q', cls.headerfalse, cwd=r.base)
        for top, line in ((cls.headerbare, '[core] bare = true'), (cls.headerfalse, '[core] bare = false')):
            with open(os.path.join(top, '.git', 'config'), 'a', encoding='utf-8') as f:
                f.write(line + chr(10))
        cls.junction = cls.subjunction = cls.drive = None
        if os.name == 'nt':
            import _winapi
            cls.junction = os.path.join(r.base, 'junction')
            _winapi.CreateJunction(r.root, cls.junction)
            cls.subjunction = os.path.join(r.base, 'subjunction')
            _winapi.CreateJunction(r.path('src'), cls.subjunction)
            letter = free_drive()
            if letter:
                try:
                    done = subprocess.run(['subst', letter, r.path('src')], capture_output=True,
                                          timeout=30).returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    done = False
                if done and os.path.isdir(letter + os.sep):
                    cls.drive = letter

    @classmethod
    def tearDownClass(cls):
        for j in (cls.junction, cls.subjunction):
            if j and os.path.isdir(j):
                os.rmdir(j)                 # the junction only, never its target
        if cls.drive:
            subprocess.run(['subst', cls.drive, '/D'], capture_output=True, timeout=30)
        cls.repo.cleanup()

    def cases(self):
        r = self.repo
        out = [
            ('main root', r.root, repo.MAIN, FS),
            ('main folder', r.path('src'), repo.MAIN, FS),
            ('main file', r.path('src', 'a.py'), repo.MAIN, FS),
            ('main path not yet there', r.path('src', 'new', 'b.py'), repo.MAIN, FS),
            ('main, other case', r.path('SRC').upper(), repo.MAIN, FS),
            ('nested worktree root', self.wt, repo.LINKED, FS),
            ('nested worktree folder', os.path.join(self.wt, 'sub'), repo.LINKED, FS),
            ('nested worktree path not yet there', os.path.join(self.wt, 'x', 'y.txt'), repo.LINKED, FS),
            ('worktree outside main', self.outside, repo.LINKED, FS),
            ('worktree .claude folder of main', r.path('.claude'), repo.MAIN, FS),
            ('no checkout', self.plain, repo.NONE, FS),
            ('no checkout, path not yet there', os.path.join(self.plain, 'q', 'z.txt'), repo.NONE, FS),
            ('separate git dir', self.sep, repo.MAIN, FS),
            ('inside .git', r.path('.git', 'objects'), repo.UNKNOWN, GIT),
            ('inside a worktree gitdir', os.path.join(r.path('.git'), 'worktrees', 'wt'), repo.UNKNOWN, GIT),
            ('bare repository', self.bare, repo.UNKNOWN, GIT),
            ('invalid gitfile', self.badfile, None, GIT),
            ('gitfile to nowhere', self.deadlink, None, GIT),
            ('invalid .git folder', self.emptygit, repo.MAIN, GIT),
            ('core.worktree set', self.wtcfg, None, GIT),
            ('extensions.worktreeConfig set', self.ext, repo.MAIN, FS),
            ('core.bare true on the header line', self.headerbare, None, GIT),
            ('core.bare false on the header line', self.headerfalse, repo.MAIN, FS),
        ]
        # git walks the path as written, so a link on the way is git's to answer
        if self.junction:
            out.append(('junction to main', os.path.join(self.junction, 'src'), repo.MAIN, GIT))
        if self.subjunction:
            out.append(('junction to a main subfolder', self.subjunction, None, GIT))
            out.append(('below a junction to a main subfolder', os.path.join(self.subjunction, 'a.py'), None, GIT))
        if self.drive:
            out.append(('subst drive on a main subfolder', self.drive + os.sep, None, GIT))
            out.append(('below a subst drive', os.path.join(self.drive + os.sep, 'a.py'), None, GIT))
        return out

    def test_links_and_drives_were_made(self):
        if os.name != 'nt':
            self.skipTest('junctions and subst drives are Windows')
        self.assertTrue(self.subjunction and os.path.isdir(self.subjunction))
        if not self.drive:
            self.skipTest('subst could not map a free drive letter here')

    def test_every_case_answers_what_git_answers(self):
        answers_like_git(self, self.cases())

    def test_a_failing_read_hands_to_git(self):
        def broken(path):
            raise RuntimeError('not an OSError')
        fb = Counted()
        got = place.classify(self.repo.root, fallback=fb, owned=broken)
        self.assertEqual(fb.calls, 1)
        self.assertEqual(got, repo.classify(self.repo.root))

    def test_discovery_env_hands_to_git(self):
        fb = Counted()
        got = place.classify(self.repo.root, fallback=fb, environ={'GIT_OBJECT_DIRECTORY': 'x'})
        self.assertEqual(fb.calls, 1)
        self.assertEqual(got, repo.classify(self.repo.root))

    def test_a_foreign_owner_hands_to_git(self):
        fb = Counted()
        got = place.classify(self.repo.root, fallback=fb, owned=lambda p: False)
        self.assertEqual(fb.calls, 1)
        self.assertEqual(got, repo.classify(self.repo.root))

    def test_ctx_reads_places_without_git_by_default(self):
        ctx = context.Ctx(environ={}, home=self.repo.base)
        self.assertIs(ctx._classify, place.classify)
        self.assertEqual(ctx.main_top(os.path.join(self.wt, 'sub')),
                         context.Ctx(environ={}, home=self.repo.base, classify=repo.classify).main_top(
                             os.path.join(self.wt, 'sub')))


DESKTOP_WT_CONFIG = '[core]\n\tlongpaths = true\n\thooksPath = C:/somewhere/githooks\n'


def pair(r, name, common='', main_wt=None, linked_wt=None, ext=True):
    """A main checkout base/<name> with one commit and its linked worktree base/<name>-wt.
    `ext`: turn extensions.worktreeConfig on with `git config` (what the Claude desktop app does).
    `common` is appended to the common config; `main_wt` and `linked_wt`, when given, are the
    main's and the linked worktree's config.worktree."""
    main = os.path.join(r.base, name)
    linked = main + '-wt'
    r.git('init', '-q', '-b', 'main', main, cwd=r.base)
    r.git('-c', 'user.name=%s' % gitfixture.NAME, '-c', 'user.email=%s' % gitfixture.EMAIL,
          'commit', '-q', '--allow-empty', '-m', 'one', cwd=main)
    r.git('worktree', 'add', '-q', linked, cwd=main)
    common_dir = os.path.join(main, '.git')
    if ext:
        r.git('config', 'extensions.worktreeConfig', 'true', cwd=main)
    for path, text in ((os.path.join(common_dir, 'config'), common),
                       (os.path.join(common_dir, 'config.worktree'), main_wt),
                       (os.path.join(common_dir, 'worktrees', name + '-wt', 'config.worktree'), linked_wt)):
        if text:
            with open(path, 'a', encoding='utf-8', newline='\n') as f:
                f.write(text)
    return main, linked


@real_git("checks the filesystem's places against git's own rev-parse")
class WorktreeConfigPlaces(unittest.TestCase):
    """extensions.worktreeConfig (git-worktree "CONFIGURATION FILE"): git then also reads the
    work tree's own <gitdir>/config.worktree, after the common config, so a key there wins. The
    filesystem reader reads it too and answers what git answers."""

    @classmethod
    def setUpClass(cls):
        cls.repo = gitfixture.TempRepo()

    @classmethod
    def tearDownClass(cls):
        cls.repo.cleanup()

    def test_worktree_config_cases_answer_what_git_answers(self):
        r = self.repo
        today_main, today_linked = pair(r, 'today', linked_wt=DESKTOP_WT_CONFIG)
        over_main, _ = pair(r, 'overmain', common='[core]\n\tbare = true\n', main_wt='[core]\n\tbare = false\n')
        upper_main, upper_linked = pair(r, 'upper', ext=False,
                                        common='[EXTENSIONS]\n\tWORKTREECONFIG\n[core]\n\tbare = true\n',
                                        linked_wt='[core]\n\tbare = false\n')
        main_bare, _ = pair(r, 'mainbare', main_wt='[core]\n\tbare = true\n')
        _, linked_bare = pair(r, 'linkedbare', common='[core]\n\tbare = false\n', linked_wt='[core]\n\tbare = true\n')
        other = os.path.join(r.base, 'elsewhere')
        os.makedirs(other)
        _, linked_wtree = pair(r, 'linkedwtree', linked_wt='[core]\n\tworktree = %s\n' % other.replace('\\', '/'))
        incl, _ = pair(r, 'incl', main_wt='[include]\n\tpath = %s\n' % os.path.join(r.base, 'x.cfg').replace('\\', '/'))
        off_main, _ = pair(r, 'off', ext=False, main_wt='[core]\n\tbare = true\n')
        false_main, _ = pair(r, 'false', ext=False, common='[extensions]\n\tworktreeConfig = false\n',
                             main_wt='[core]\n\tbare = true\n')
        noop, _ = pair(r, 'noop', common='[extensions]\n\tnoop = true\n')
        wt_ext, _ = pair(r, 'wtext', main_wt='[extensions]\n\tnoop = true\n')
        v1, _ = pair(r, 'v1', common='[core]\n\trepositoryformatversion = 1\n')
        answers_like_git(self, [
            ("today's state: the main checkout", today_main, repo.MAIN, FS),
            ("today's state: the linked worktree with the desktop app's config.worktree", today_linked, repo.LINKED, FS),
            ("today's state: a path not yet there in it", os.path.join(today_linked, 'src', 'new.py'), repo.LINKED, FS),
            ('main config.worktree bare = false wins over the common bare = true', over_main, repo.MAIN, FS),
            ('upper-case [EXTENSIONS] WORKTREECONFIG with no value is on; linked bare = false wins',
             upper_linked, repo.LINKED, FS),
            ('the common bare = true holds where no config.worktree overrides it', upper_main, None, GIT),
            ('main config.worktree bare = true', main_bare, None, GIT),
            ('linked config.worktree bare = true over the common bare = false', linked_bare, None, GIT),
            ('linked config.worktree core.worktree', linked_wtree, None, GIT),
            ('an include in config.worktree', incl, None, GIT),
            ('extension off: config.worktree is not read', off_main, repo.MAIN, FS),
            ('worktreeConfig = false: config.worktree is not read', false_main, repo.MAIN, FS),
            ('another extension beside worktreeConfig', noop, repo.MAIN, GIT),
            ('an [extensions] section inside config.worktree', wt_ext, repo.MAIN, GIT),
            ('repositoryformatversion 1 with worktreeConfig', v1, repo.MAIN, FS),
        ])

    def test_a_repository_format_git_refuses_hands_to_git(self):
        r = self.repo
        v2, v2_linked = pair(r, 'v2', ext=False, common='[core]\n\trepositoryformatversion = 2\n')
        quoted, _ = pair(r, 'quoted', ext=False, common='[core]\n\trepositoryformatversion = " 0 "\n')
        answers_like_git(self, [
            ('repositoryformatversion 2: git refuses the repository', v2, repo.UNKNOWN, GIT),
            ('repositoryformatversion 2, from a linked worktree', v2_linked, repo.UNKNOWN, GIT),
            ('a repositoryformatversion git cannot read as a number', quoted, repo.UNKNOWN, GIT),
        ])

    def test_the_last_core_bare_wins(self):
        r = self.repo
        last_false, _ = pair(r, 'lastfalse', ext=False, common='[core]\n\tbare = true\n\tbare = false\n')
        answers_like_git(self, [('core.bare true, then false', last_false, repo.MAIN, FS)])

    def test_a_continued_line_hands_to_git(self):
        # `x = a\` continues onto the next line, so the `bare = false` there is part of core.x:
        # git still reads bare = true. A reader that took it for a key would answer MAIN.
        r = self.repo
        cont, _ = pair(r, 'cont', ext=False, common='[core]\n\tbare = true\n\tx = a\\\n\tbare = false\n')
        cont_wt, _ = pair(r, 'contwt', common='[core]\n\tbare = true\n',
                          main_wt='[core]\n\tx = a\\\n\tbare = false\n')
        answers_like_git(self, [
            ('a continued line in the common config', cont, None, GIT),
            ('a continued line in config.worktree', cont_wt, None, GIT),
        ])


def rewrite(path, crlf=False, bom=False):
    """Rewrite config file `path` with CRLF line ends and/or a UTF-8 byte order mark."""
    with open(path, 'rb') as f:
        data = f.read()
    if crlf:
        data = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
    if bom:
        data = b'\xef\xbb\xbf' + data
    with open(path, 'wb') as f:
        f.write(data)


BARE = '[core]\n\tbare = true\n'
WORK_TREE = (repo.MAIN, FS)         # git answers a work tree, and so does the filesystem
REFUSED = (None, GIT)               # git refuses (bad config line, or bare): handed to git


@real_git("checks the filesystem's places against git's own rev-parse")
class ConfigLineForms(unittest.TestCase):
    """How a config line is written: git's parser (config.c get_next_char, get_value,
    parse_value) decides what core.bare and extensions.worktreeConfig are, or refuses the file.
    Each form is checked against git; the reader answers what git answers, or hands it to git.
    A row ending in `true` is preceded by BARE, so a reader that misread the last core.bare
    would answer MAIN where git refuses."""

    @classmethod
    def setUpClass(cls):
        cls.repo = gitfixture.TempRepo()

    @classmethod
    def tearDownClass(cls):
        cls.repo.cleanup()

    # (name, appended to the common config, main config.worktree (extension on when given),
    #  rewrite the common config / config.worktree as (crlf, bom), expected (kind, via))
    CASES = [
        ('CR CR LF after a key: only the CR before LF ends the line',
         '[extensions]\n\tworktreeConfig\r\r\n', None, None, None, REFUSED),
        ('CR CR LF after a plain core key', '[core]\n\tlongpaths\r\r\n', None, None, None, REFUSED),
        ('CR CR LF after a value: the extra CR is white space', '[core]\n\tlongpaths = true\r\r\n',
         None, None, None, WORK_TREE),
        ('a CR inside the line before =', '[core]\n\tlongpaths\r= true\n', None, None, None, REFUSED),
        ('quoted "false" over true', BARE + '\tbare = "false"\n', None, None, None, WORK_TREE),
        ('quoted "true"', '[core]\n\tbare = "true"\n', None, None, None, REFUSED),
        ('; comment after false', BARE + '\tbare = false ; c\n', None, None, None, WORK_TREE),
        ('# comment right after false', BARE + '\tbare = false# c\n', None, None, None, WORK_TREE),
        ('# comment after true', '[core]\n\tbare = true # c\n', None, None, None, REFUSED),
        ('valid escapes in another value', BARE + '\tx = a\\tb\\"c\\\\\n\tbare = false\n',
         None, None, None, WORK_TREE),
        ('an unknown escape', '[core]\n\tx = a\\qb\n', None, None, None, REFUSED),
        ('an escape inside the bare value', BARE + '\tbare = f\\alse\n', None, None, None, REFUSED),
        ('a CRLF file, last bare false', BARE + '\tbare = false\n', None, (True, False), None, WORK_TREE),
        ('a byte order mark, last bare false', BARE + '\tbare = false\n', None, (False, True), None,
         WORK_TREE),
        ('[core "x"] bare is core.x.bare', '[core "x"]\n\tbare = true\n', None, None, None, WORK_TREE),
        ('[core.x] bare is core.x.bare', '[core.x]\n\tbare = true\n', None, None, None, WORK_TREE),
        ('a [core.bare] section', '[core.bare]\n\tx = true\n', None, None, None, WORK_TREE),
        ('[core] bare = true on the header line with a comment', '[core] bare = true # c\n',
         None, None, None, REFUSED),
        ('[core] bare = false on the header line with a comment, over true',
         BARE + '[core] bare = false ; c\n', None, None, None, WORK_TREE),
        ('odd white space around false, over true', BARE + ' \t bare\t=\t false \t\n',
         None, None, None, WORK_TREE),
        ('a space inside [ core ]', '[ core ]\n\tx = 1\n', None, None, None, REFUSED),
        ('a vertical tab before a key (not git white space)', BARE + '\x0bbare = false\n',
         None, None, None, REFUSED),
        ('a comment after a key with no =', '[core]\n\tlongpaths # c\n', None, None, None, REFUSED),
        ('config.worktree with CRLF and a byte order mark: bare false over true', BARE,
         '[core]\n\tbare = false\n', None, (True, True), WORK_TREE),
        ('config.worktree: CR CR LF after a key', BARE, '[core]\n\tbare = false\n\tlongpaths\r\r\n',
         None, None, REFUSED),
    ]

    def test_each_line_form_answers_what_git_answers(self):
        rows = []
        for n, (name, common, main_wt, common_rw, wt_rw, (kind, via)) in enumerate(self.CASES):
            main, _ = pair(self.repo, 'form%02d' % n, ext=main_wt is not None, common=common, main_wt=main_wt)
            if common_rw:
                rewrite(os.path.join(main, '.git', 'config'), *common_rw)
            if wt_rw:
                rewrite(os.path.join(main, '.git', 'config.worktree'), *wt_rw)
            rows.append((name, main, kind, via))
        answers_like_git(self, rows)


class NoGitLaunched(unittest.TestCase):
    """The observable: a guarded call in a checkout starts no git child (KIT-HOOK-SPAWNS)."""

    @unittest.skipUnless(MINE, OWNER_NOTE)
    def test_classify_of_a_checkout_starts_no_process(self):
        r = gitfixture.TempRepo()
        self.addCleanup(r.cleanup)
        started = []
        real_popen = subprocess.Popen

        def spy(*a, **kw):
            started.append(a[0] if a else kw.get('args'))
            return real_popen(*a, **kw)
        subprocess.Popen = spy
        try:
            p = place.classify(r.path('README.md'))
        finally:
            subprocess.Popen = real_popen
        self.assertEqual(p.kind, repo.MAIN)
        self.assertEqual(started, [])


if __name__ == '__main__':
    unittest.main()
