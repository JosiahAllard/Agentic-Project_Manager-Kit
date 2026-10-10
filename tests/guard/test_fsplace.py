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
            ('extensions set', self.ext, repo.MAIN, GIT),
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
        for name, path, kind, via in self.cases():
            with self.subTest(name):
                want = repo.classify(path)
                fb = Counted()
                got = place.classify(path, fallback=fb)
                self.assertEqual(got, want)
                if kind is not None:
                    self.assertEqual(got.kind, kind)
                if via == FS and not MINE and kind != repo.NONE:
                    via = GIT                # OWNER_NOTE (NONE has no owner to check)
                self.assertEqual(fb.calls, 1 if via == GIT else 0,
                                 '%s: expected the %s answer' % (name, via))

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
