"""The throwaway repository the reviewpack end-to-end tests run against (tests/gitfixture.py).

`main` holds README.md, CLAUDE.md 'v0', f.txt '0' and a `.kit.json` naming the fixture identity
(gitfixture.EMAIL) as the owner, so a plain commit is the owner's and an agent commit passes
`-c user.email=agent@example.invalid`. The doc-budget checker is pointed at a path that does not
exist, so the docs line reads NOT CHECKED unless a test installs a stub; so is the doc-index checker.
"""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

from kitlib import reviewpack
from tests.gitfixture import EMAIL, TempRepo, fixture_env
from tests.home import no_machine_owner

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT, 'tools'))
import review_pack as cli  # noqa: E402

AGENT = 'agent@example.invalid'
TRAILERED_MSG = 'Co-Authored-By: Claude Test <noreply@anthropic.com>'


class PackCase(unittest.TestCase):
    """No tests of its own. CONFIG is the `.kit.json` committed on main."""

    CONFIG = {'owner': {'email': EMAIL}}

    @classmethod
    def _prepare_base(cls, r):
        """The 'base' commit, built once per CONFIG and copied by TempRepo (tests/gitfixture.py)."""
        r.write('CLAUDE.md', 'v0')
        r.write('f.txt', '0')
        if cls.CONFIG is not None:
            r.write('.kit.json', json.dumps(cls.CONFIG))
        r.git('add', '-A')
        r.git('commit', '-q', '-m', 'base')

    def setUp(self):
        env = mock.patch.dict(os.environ, fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        # no owner email in .kit.json: the owner rules read the machine config's, here none
        no_machine_owner(self)
        self.r = TempRepo('main', prepare=self._prepare_base,
                          prepare_key=('reviewpack.PackCase', json.dumps(self.CONFIG, sort_keys=True)))
        self.addCleanup(self.r.cleanup)
        self.root = self.r.root
        self.enter(mock.patch.object(reviewpack, 'DOC_BUDGET',
                                     os.path.join(self.r.base, 'no-kit', 'doc_budget.py')))
        self.enter(mock.patch.object(reviewpack, 'DOC_INDEX',
                                     os.path.join(self.r.base, 'no-kit', 'doc_index.py')))

    def enter(self, patcher):
        got = patcher.start()
        self.addCleanup(patcher.stop)
        return got

    def git(self, *args):
        return self.r.git(*args)

    def write(self, rel, text):
        self.r.write(rel, text)

    def commit_all(self, message, author=None):
        self.git('add', '-A')
        who = ('-c', 'user.email=%s' % author) if author else ()
        self.git(*(who + ('commit', '-q', '-m', message)))

    def owner_only(self, base='main', branch='feature'):
        mb = self.git('merge-base', base, branch)
        return reviewpack.owner_only_section(self.root, base, branch, mb,
                                             reviewpack.owner_rules(self.root, base, branch))

    def run_pack(self, *argv):
        """(exit status, stdout) of tools/review_pack.py main() over the repository."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['-C', self.root] + list(argv))
        return code, out.getvalue()


def failing_call(words, rc=128, err='fatal: injected read failure'):
    """A stand-in for kitlib.git.cmd.call that fails the one git read starting with `words`
    (after any `-c core.quotepath=false`) and passes every other call through."""
    real = reviewpack.gitcmd.call

    def call(args, cwd, *a, **k):
        bare = list(args)
        if bare[:2] == list(reviewpack.QUOTEPATH):
            bare = bare[2:]
        if tuple(bare[:len(words)]) == tuple(words):
            return reviewpack.gitcmd.Result(rc, b'', err)
        return real(args, cwd, *a, **k)
    return call
