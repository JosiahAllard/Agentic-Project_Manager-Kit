"""tools/kit_tests.py's two passes and its flags around them (docs/test-tools.md "kit_tests"): the
fake-git pass gives cpu slots back down to what the real-git pass can use at once, through one
drainer for both passes; --full's base and its line; an outer KIT_FAKE_GIT=0; and --list's
environment and refusal. The pool (kitlib.tooldeps.selfrun.run), its count child and the lock's
drainer (kitlib.lock.giveback.drainer) are faked; nothing here runs a test or touches the lock."""
import importlib.util
import io
import os
import unittest
from unittest import mock

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('kit_tests_entry_for_passes_tests',
                                               os.path.join(KIT, 'tools', 'kit_tests.py'))
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

MODULES = ['tests.a.test_m', 'tests.git.test_g', 'tests.gate.test_land_x', 'tests.gate.test_other']
SELECTED = ['tests.a.test_m', 'tests.gate.test_other']
NO_OFFER = ('kit_tests: give back: no offer (only a run of this kit\'s lock wrapper offers one); '
            'the run keeps its cpu slots to its end')
GIVE_ENV = 'MACHINE_LOCK_GIVE_BACK'


class Harness(object):
    """One kit_tests.main run with the pool, the count child, the drainer and git faked; what each
    was asked is kept on the instance."""

    def __init__(self, offer=True, counted=({}, {}), changed=([], None), outer=None,
                 collected=()):
        self.offer, self.counted, self.changed = offer, counted, changed
        self.outer, self.collected = outer, collected
        self.drainers, self.given, self.counts, self.runs = [], [], [], []
        self.bases, self.calls, self.listed = [], [], []

    def drainer(self, say):
        self.drainers.append(say)
        if not self.offer:
            return None

        def give(n):
            self.given.append(n)
            say('give back: keeps %d' % n)
            return True
        return give

    def count_units(self, root, modules, environ=None):
        self.counts.append((list(modules), dict(environ)))
        return self.counted

    def run(self, modules, root, jobs, **kw):
        self.runs.append({'fake': kw['environ'].get('KIT_FAKE_GIT'), 'modules': list(modules),
                          'give_back': kw['give_back'], 'counts': kw.get('counts'),
                          'classes': kw.get('classes'), 'jobs': jobs,
                          'give_env': GIVE_ENV in kw['environ']})
        if kw['give_back'] is not None:
            kw['give_back'](1)                  # the pool's last unit: one still running
        kw['out'].write('kit_tests: OK in 1s\n')
        return 0, ''

    def paths(self, base, top):
        self.bases.append(base)
        return self.changed

    def call(self, cmd, cwd=None, env=None, **kw):
        self.calls.append(env.get('KIT_FAKE_GIT'))
        return 0

    def collect(self, top, modules, environ=None):
        self.listed.append(dict(environ))
        return None if self.collected is None else list(self.collected)

    def main(self, argv):
        out = io.StringIO()
        here = os.path.abspath('.')
        env = {GIVE_ENV: 'offer-from-the-lock-wrapper'}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cli.gitrepo, 'toplevel', return_value=(here, None)), \
                mock.patch.object(cli.gitrepo, 'common_dir', return_value=(here, None)), \
                mock.patch.object(cli.selftree, 'config', return_value={'serial': []}), \
                mock.patch.object(cli.selftree, 'all_test_modules', return_value=MODULES), \
                mock.patch.object(cli.selftree, 'changed_paths', side_effect=self.paths), \
                mock.patch.object(cli, 'default_base', return_value=('main', None)), \
                mock.patch.object(cli, 'plan', return_value=SELECTED), \
                mock.patch.object(cli.giveback, 'drainer', side_effect=self.drainer), \
                mock.patch.object(cli.selfrun, 'count_units', side_effect=self.count_units), \
                mock.patch.object(cli.selfrun, 'run', side_effect=self.run), \
                mock.patch.object(cli.selflist, 'collect', side_effect=self.collect), \
                mock.patch.object(cli.selflist, 'render', return_value=['tests.a.test_m.A.test_x']), \
                mock.patch.object(cli.subprocess, 'call', side_effect=self.call):
            os.environ.pop('KIT_FAKE_GIT', None)
            if self.outer is not None:
                os.environ['KIT_FAKE_GIT'] = self.outer
            rc = cli.main(list(argv), out=out)
        return rc, out.getvalue()


TWO_CHANGED = ['--select', '--changed', 'tests/a/test_m.py', 'tests/gate/test_other.py']


class GiveBackBetweenPasses(unittest.TestCase):
    """The fake pass no longer holds every slot to the end of the real pass: it gives back as it
    drains, never below the threads the real pass's units can use at once; the real pass then
    gives back all it does not use. One drainer serves both."""

    def test_the_fake_pass_keeps_what_the_real_pass_can_use_and_the_real_pass_gives_the_rest(self):
        h = Harness(counted=({'tests.a.test_m': 3, 'tests.gate.test_other': 1},
                             {'tests.a.test_m': ['A', 'B', 'C'], 'tests.gate.test_other': ['D']}))
        rc, text = h.main(TWO_CHANGED + ['--jobs', '8'])
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(h.drainers), 1, text)              # one drainer for both passes
        self.assertEqual([r['fake'] for r in h.runs], ['1', '0'], text)
        self.assertEqual(h.given, [4, 1], text)                 # 3 classes + 1 module; then 1
        self.assertIn('kit_tests: give back: the fake-git pass keeps 4 thread(s) for the real-git '
                      'pass (2 module(s), up to 4 unit(s))\n', text)
        # the real pass plans from the very count the fake pass kept threads for
        real = h.runs[1]
        self.assertEqual(real['modules'], SELECTED)
        self.assertEqual((real['counts'], real['classes']), h.counted)
        # and so does the fake pass: one count child for both, not one more in its pool
        self.assertEqual((h.runs[0]['counts'], h.runs[0]['classes']), h.counted)
        self.assertEqual(len(h.counts), 1, text)
        # the drainer's lines are tagged with the pass that runs
        self.assertIn('kit_tests [fake git]: give back: keeps 4\n', text)
        self.assertIn('kit_tests [real git]: give back: keeps 1\n', text)

    def test_the_count_is_asked_once_for_both_passes_on_the_fake_tier_without_the_offer(self):
        h = Harness()
        rc, text = h.main(TWO_CHANGED + ['--jobs', '4'])
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(h.counts), 1, text)
        modules, env = h.counts[0]
        self.assertEqual(modules, SELECTED)
        self.assertEqual(env.get('KIT_FAKE_GIT'), '1')       # as the fake pass's own child had it
        self.assertNotIn(GIVE_ENV, env)
        self.assertTrue(h.runs[0]['give_env'], 'the units env still carries it; selfrun drops it')

    def test_what_the_fake_pass_keeps_is_capped_at_the_jobs(self):
        h = Harness(counted=({}, {'tests.a.test_m': ['A', 'B', 'C', 'D', 'E']}))
        rc, text = h.main(TWO_CHANGED + ['--jobs', '2'])
        self.assertEqual((rc, h.given), (0, [2, 1]), text)
        self.assertIn('keeps 2 thread(s) for the real-git pass (2 module(s), up to 6 unit(s))', text)

    def test_a_failed_count_keeps_one_thread_per_module(self):
        # count_units answers ({}, {}) when its child fails; the real pass then splits nothing
        h = Harness(counted=({}, {}))
        rc, text = h.main(TWO_CHANGED + ['--jobs', '8'])
        self.assertEqual((rc, h.given), (0, [2, 1]), text)
        self.assertEqual((h.runs[1]['counts'], h.runs[1]['classes']), ({}, {}))

    def test_both_tiers_keeps_for_its_real_git_modules(self):
        h = Harness(changed=([], None))
        rc, text = h.main(['--full', '--both-tiers', '--jobs', '8'])
        self.assertEqual(rc, 0, text)
        self.assertEqual(h.counts, [(MODULES, h.counts[0][1])], text)   # the real ones among them
        self.assertEqual(h.runs[1]['modules'], ['tests.git.test_g', 'tests.gate.test_land_x'])
        self.assertEqual(h.given, [2, 1], text)

    def test_one_count_holds_a_real_git_module_the_fake_pass_does_not_run(self):
        # plan (faked) selects SELECTED; the changed test module it leaves out runs on real git only
        h = Harness(changed=([], None))
        rc, text = h.main(['--select', '--changed', 'tests/git/test_g.py', '--jobs', '4'])
        self.assertEqual(rc, 0, text)
        self.assertEqual([r['modules'] for r in h.runs], [SELECTED, ['tests.git.test_g']], text)
        self.assertEqual([m for m, _env in h.counts], [SELECTED + ['tests.git.test_g']], text)
        self.assertEqual([(r['counts'], r['classes']) for r in h.runs], [h.counted] * 2)

    def test_no_offer_still_asks_one_count_for_both_passes_and_says_so_once(self):
        h = Harness(offer=False)
        rc, text = h.main(TWO_CHANGED + ['--jobs', '4'])
        self.assertEqual(rc, 0, text)
        self.assertEqual((len(h.drainers), len(h.counts)), (1, 1), text)
        self.assertEqual([(r['counts'], r['classes']) for r in h.runs], [h.counted] * 2)
        self.assertEqual([r['give_back'] for r in h.runs], [None, None])
        self.assertEqual(text.count(NO_OFFER), 1, text)
        self.assertNotIn('for the real-git pass', text)

    def test_one_job_has_no_drainer_and_no_count(self):
        h = Harness()
        rc, text = h.main(TWO_CHANGED + ['--jobs', '1'])
        self.assertEqual(rc, 0, text)
        self.assertEqual((h.drainers, h.counts, h.calls), ([], [], ['1', '0']), text)

    def test_one_pass_gives_back_all_it_does_not_use(self):
        h = Harness()
        rc, text = h.main(['--select', '--changed', 'kitlib/x.py', '--jobs', '4'])
        self.assertEqual(rc, 0, text)
        self.assertEqual((len(h.runs), h.counts, h.given), (1, [], [1]), text)


class FullBase(unittest.TestCase):
    """--full's real-git pass diffs against --base or the default base, and says which."""

    def test_the_default_base_is_said(self):
        h = Harness(changed=(['tests/gate/test_other.py', 'docs/x.md'], None))
        rc, text = h.main(['--full', '--jobs', '2'])
        self.assertEqual((rc, h.bases), (0, ['main']), text)
        self.assertIn('kit_tests: --full: the real-git pass\'s change list against the '
                      'merge-base with main: 2 changed path(s)\n', text)

    def test_an_explicit_base_is_diffed_against_and_said(self):
        h = Harness(changed=(['tests/gate/test_other.py'], None))
        rc, text = h.main(['--full', '--base', 'refs/remotes/origin/topic', '--jobs', '2'])
        self.assertEqual((rc, h.bases), (0, ['refs/remotes/origin/topic']), text)
        self.assertIn('merge-base with refs/remotes/origin/topic: 1 changed path(s)\n', text)
        self.assertEqual(h.runs[1]['modules'], ['tests.gate.test_other'], text)

    def test_real_git_asks_no_diff_and_says_no_base(self):
        h = Harness(changed=(['tests/gate/test_other.py'], None))
        rc, text = h.main(['--full', '--real-git', '--base', 'main', '--jobs', '2'])
        self.assertEqual((rc, h.bases, len(h.runs)), (0, [], 1), text)
        self.assertNotIn('merge-base', text)

    def test_the_base_help_names_both_select_and_full(self):
        helps = {a.dest: a.help for a in cli.build_parser()._actions}
        self.assertIn('--select and --full', helps['base'])
        self.assertIn('real-git pass', helps['base'])


class OuterRealGit(unittest.TestCase):
    """An outer KIT_FAKE_GIT=0 already runs every test on real git: one pass, no change list."""

    def test_select_is_one_pass_on_real_git(self):
        for argv in (['--select', '--changed', 'tests/a/test_m.py'], ['--select']):
            h = Harness(outer='0', changed=(['tests/a/test_m.py'], None))
            rc, text = h.main(argv + ['--jobs', '2'])
            self.assertEqual(rc, 0, text)
            self.assertEqual([r['fake'] for r in h.runs], ['0'], text)
            self.assertEqual(h.counts, [], text)
            self.assertNotIn('real-git pass', text)

    def test_one_process_is_on_real_git_too(self):
        h = Harness(outer='0')
        rc, text = h.main(['--select', '--changed', 'tests/a/test_m.py', '--jobs', '1'])
        self.assertEqual((rc, h.calls), (0, ['0']), text)


class ListFlag(unittest.TestCase):
    """--list loads the tests on the fake git unless the environment says otherwise, and refuses
    when the child that loads them fails."""

    def test_the_child_loads_on_the_fake_by_default_and_keeps_an_outer_tier(self):
        for outer, want in ((None, '1'), ('0', '0'), ('1', '1')):
            h = Harness(outer=outer, collected=[])
            rc, text = h.main(['--list'])
            self.assertEqual(rc, 0, text)
            self.assertEqual([e.get('KIT_FAKE_GIT') for e in h.listed], [want], outer)
            self.assertEqual(text, 'tests.a.test_m.A.test_x\n')
            self.assertEqual((h.runs, h.counts, h.drainers), ([], [], []))

    def test_a_failed_child_is_a_refusal_and_lists_nothing(self):
        h = Harness(collected=None)
        rc, text = h.main(['--list'])
        self.assertEqual(rc, cli.USAGE, text)
        self.assertEqual(text, 'kit_tests: refused: the child that loads the tests failed - '
                               'nothing listed\n')


if __name__ == '__main__':
    unittest.main()
