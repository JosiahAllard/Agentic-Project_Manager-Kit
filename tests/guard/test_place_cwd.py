"""test_place_decisions' rows decided from inside a checkout the filesystem reader hands to git.

The gate runs the suite from a live worktree, whose common config other programs rewrite (the
desktop app turning extensions.worktreeConfig on and off). Rows that make the rules place a
relative path (the `<main checkout>` placeholder) resolve it against this process's folder, so
SameDecisions runs from its own world's base: the checkout the suite started in is never read and
nothing falls back to git. Here that checkout's config has extensions.objectformat, which the
reader always hands to git (kitlib.guard.place)."""
import os
import shutil
import subprocess
import tempfile
import unittest

from tests import gitfixture
from tests.realgit import real_git

from . import test_place_decisions as decisions


@real_git("checks the filesystem's places against git's own rev-parse")
class SameDecisionsFromAnotherCheckout(decisions.SameDecisions):

    def setUp(self):
        outside = tempfile.mkdtemp(prefix='kit-place-cwd-')
        self.addCleanup(shutil.rmtree, outside, True)
        env = gitfixture.fixture_env()
        for args in (('init', '-q', '-b', 'main'), ('config', 'core.repositoryformatversion', '1'),
                     ('config', 'extensions.objectformat', 'sha1')):
            subprocess.run(('git',) + args, cwd=outside, env=env, check=True, capture_output=True)
        here = os.getcwd()
        os.chdir(outside)
        self.addCleanup(os.chdir, here)
        decisions.SameDecisions.setUp(self)


if __name__ == '__main__':
    unittest.main()
