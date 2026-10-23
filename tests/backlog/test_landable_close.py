"""`landable` and a landing's close (kitlib.backlog.landable; docs/backlog.md "Reads"): a branch
that holds a pull in a store its landing closes records in (a named `ref` or `branch` store, the
PM data branch's folder store) is LANDABLE, and the verdict names what the landing does to each
record: closes it, or releases it when the branch has a `Part-of:` line and no `Closes:` line
names it (kitlib.backlog.at_landing.close_landed). A folder store in the code repository and the
implicit default store still say NOT LANDABLE; lines that cannot be read say CANNOT TELL.
Scratch repositories with a bare remote, as tests/backlog/test_at_landing_part.py builds them."""
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib.backlog import at_landing
from kitlib.backlog import landable as L
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from tests.backlog.helpers import priced
from tests.backlog import test_at_landing_part as part   # not its classes: their tests run there
from tests.gate import test_land_store as tls
from tests.gate.test_land_store import BRANCH, StoreLandCase, pulled

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'backlog.py')
COMMAND = 'python backlog.py'
SLICE = '(a slice branch needs a `Part-of: %s` line)'


class RefStore(StoreLandCase):
    branch_with = part.PartLanding.branch_with

    def verdict(self, message='work\n\n%s\n' % part.TRAILERS, backend=None):
        self.branch_with(message)
        backend = backend or self.backend()
        got = L.check(backend, backend.load(rmod.Rules()), self.repo.root, [BRANCH],
                      base=self.main)
        self.assertEqual(len(got), 1, got)
        return got[0]

    def test_a_pull_with_no_part_of_line_is_landable_and_the_landing_closes_it(self):
        self.seed(pulled('FOO'), pulled('QUX', by='kit/other'), priced('BAZ'))
        v = self.verdict()
        self.assertEqual((v.verdict, v.held, v.closes, v.releases, v.partial),
                         (L.LANDABLE, ('FOO',), ('FOO',), (), False))
        lines = L.lines(v, COMMAND)
        self.assertTrue(lines[0].startswith('LANDABLE %s: ' % BRANCH), lines)
        self.assertEqual(lines[1:], ['  closes FOO, which it holds pulled ' + SLICE % 'FOO'])
        self.assertEqual(L.exit_code([v]), 0)

    def test_a_part_of_line_releases_each_pull_no_closes_line_names(self):
        self.seed(pulled('FOO'), pulled('BAR'))
        v = self.verdict('part one\n\nPart-of: FOO\n%s\n' % part.TRAILERS)
        self.assertEqual((v.verdict, v.closes, v.releases, v.partial),
                         (L.LANDABLE, (), ('BAR', 'FOO'), True))
        lines = L.lines(v, COMMAND)
        self.assertEqual(lines[1:], ['  releases %s, which it holds pulled (the branch has a '
                                     'Part-of line and no Closes line names it)' % rid
                                     for rid in ('BAR', 'FOO')])
        self.assertNotIn('slice', '\n'.join(lines))
        self.assertEqual(L.exit_code([v]), 0)

    def test_closes_and_part_of_split_the_pulls_and_give_no_slice_hint(self):
        self.seed(pulled('FOO'), pulled('BAR'))
        v = self.verdict('fix\n\nCloses: FOO\nPart-of: BAR\n%s\n' % part.TRAILERS)
        self.assertEqual((v.verdict, v.closes, v.releases), (L.LANDABLE, ('FOO',), ('BAR',)))
        lines = L.lines(v, COMMAND)
        self.assertEqual(lines[1:], [
            '  closes FOO, which it holds pulled (a Closes line names it)',
            '  releases BAR, which it holds pulled (the branch has a Part-of line and no Closes '
            'line names it)'])
        self.assertNotIn('slice', '\n'.join(lines))

    def test_a_branch_holding_nothing_is_landable_as_before(self):
        self.seed(pulled('QUX', by='kit/other'))
        v = self.verdict()
        self.assertEqual((v.verdict, v.held, v.closes, v.releases), (L.LANDABLE, (), (), ()))
        self.assertEqual(L.lines(v, COMMAND),
                         ['LANDABLE %s: no record is pulled by it (%s)' % (BRANCH, v.detail)])

    def test_lines_that_cannot_be_read_cannot_tell(self):
        self.seed(pulled('FOO'))
        with mock.patch.object(at_landing, 'closes_trailers', return_value=(None, 'boom')):
            v = self.verdict()
        self.assertEqual(v.verdict, L.CANNOT_TELL)
        self.assertIn('boom', v.detail)
        self.assertIn('Part-of', v.detail)
        self.assertEqual(L.exit_code([v]), 1)
        with mock.patch.object(at_landing, 'part_trailers', return_value=(None, 'bang')):
            got = L.check(self.backend(), self.backend().load(rmod.Rules()), self.repo.root,
                          [BRANCH], base=self.main)
        self.assertEqual([(x.verdict, 'bang' in x.detail) for x in got], [(L.CANNOT_TELL, True)])

    def test_no_base_to_read_the_lines_against_cannot_tell(self):
        self.seed(pulled('FOO'))
        self.branch_with('work\n\n%s\n' % part.TRAILERS)
        got = L.check(self.backend(), self.backend().load(rmod.Rules()), self.repo.root, [BRANCH])
        self.assertEqual([x.verdict for x in got], [L.CANNOT_TELL])

    def test_the_implicit_default_store_still_says_not_landable(self):
        # A landing only reads the default store (at_landing._read_default): it closes nothing there.
        self.seed(pulled('FOO'))
        implicit = smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF,
                                   implicit=True)
        v = self.verdict(backend=implicit)
        self.assertEqual((v.verdict, v.held, v.closes), (L.NOT_LANDABLE, ('FOO',), ()))
        self.assertEqual(L.exit_code([v]), 2)


class FolderStore(StoreLandCase):

    def test_a_folder_store_in_the_code_repository_still_says_not_landable(self):
        # Its records travel with the branch, so a landing closes none of them.
        wt = self.repo.worktree('feature')
        self.repo.write('docs/backlog/FOO.md', pulled('FOO')['FOO'], root=wt)
        self.repo.commit('work\n\n%s\n' % part.TRAILERS, 'docs/backlog', cwd=wt)
        got = L.check(smod.MainBackend(self.repo.root, 'docs/backlog'), None, self.repo.root,
                      [BRANCH], base=self.main)
        self.assertEqual([(v.verdict, v.held, v.closes) for v in got],
                         [(L.NOT_LANDABLE, ('FOO',), ())])
        lines = L.lines(got[0], COMMAND)
        self.assertTrue(lines[0].startswith('NOT LANDABLE %s: FOO is still pulled by it; '
                                            % BRANCH), lines)
        self.assertIn('  Done:     %s -C <worktree> close FOO' % COMMAND, lines[1])
        self.assertIn('  Not done: %s -C <worktree> --as %s release FOO' % (COMMAND, BRANCH),
                      lines[2])
        self.assertEqual(L.exit_code(got), 2)


class PmDataStore(StoreLandCase):
    """In the PM data worktree (test_land_store.PmDataClose's set-up) the records are the
    worktree's own store, which the landing closes in; the code branch's tree holds none."""
    setUp = tls.PmDataClose.setUp
    pm_worktree = tls.PmDataClose.pm_worktree
    branch_with = part.PartLanding.branch_with

    def seeded(self):
        wt = self.pm_worktree(pulled('FOO'), priced('BAZ'))
        self.repo.commit('seed the PM data store', 'kit/backlog', '.kit.json', cwd=wt)
        self.branch_with('work\n\n%s\n' % part.TRAILERS)
        return wt

    def test_landable_there_reads_the_store_and_the_landing_closes_the_pull(self):
        wt = self.seeded()
        backend = smod.MainBackend(wt, 'kit/backlog')
        snap = smod.Store(backend, rmod.Rules()).load()
        got = L.check(backend, snap, wt, [BRANCH], base=self.main)
        self.assertEqual([(v.verdict, v.held, v.closes) for v in got],
                         [(L.LANDABLE, ('FOO',), ('FOO',))])

    def test_the_tool_there_exits_0_and_names_the_close(self):
        wt = self.seeded()
        p = subprocess.run([sys.executable, TOOL, '-C', wt, '--no-fetch', 'landable', BRANCH],
                           capture_output=True, text=True, encoding='utf-8', timeout=120)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn('  closes FOO, which it holds pulled ' + SLICE % 'FOO', out)


if __name__ == '__main__':
    unittest.main()
