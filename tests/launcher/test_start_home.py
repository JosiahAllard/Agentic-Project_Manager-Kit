"""--home-worktree on start, deep, consultant and handoff (kitlib/launcher/start.py home_folder,
docs/launcher.md "The home worktree"): where the window starts, what the session is told, what is
refused before any window, and that without the flag nothing changes. The home worktree itself is
FakeHost.home_worktree here; tests/launcher/test_home.py covers home.ensure."""
import os
import sys
import unittest

from kitlib.git import repo as gitrepo
from kitlib.launcher import handoff, home, start, window
from kitlib.launcher import settings as S
from tests.launcher.fakes import FakeHost, Home
from tests.launcher.test_start import Base

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'tools'))
import launch                                                       # noqa: E402

PERMISSION = '--permission-mode auto'


class HomeBase(Base):
    def setUp(self):
        Base.setUp(self)
        self.host = FakeHost(self.h.root, ignored=True)
        self.host.where = gitrepo.MAIN

    def home(self, stem):
        return home.home_path(self.repo, stem)

    def go(self, **kw):
        args = dict(repo='alpha', account='1', whatif=True, home_worktree=True)
        args.update(kw)
        return '\n'.join(start.start(self.h.settings, self.host, **args))

    def assert_starts_in_home(self, out, stem):
        path = self.home(stem)
        self.assertIn('Set-Location %s' % window.q(path), out)
        self.assertNotIn('Set-Location %s;' % window.q(self.repo), out)
        self.assertIn('--add-dir %s %s' % (window.q(self.repo), PERMISSION), out)
        self.assertIn(start.HOME_NOTE % (path, self.repo), out.replace("''", "'"))
        return path


class StartInHome(HomeBase):
    def test_a_coordinator_starts_in_its_home_with_the_main_checkout_added(self):
        out = self.go()
        path = self.assert_starts_in_home(out, 'alpha-coordinator')
        self.assertLess(out.index('HOME %s whatif=True' % path), out.index('Would open a new window running:'))
        self.assertEqual(self.host.homes, [(self.repo, path, True)])
        self.assertEqual(self.host.opened, [])

    def test_the_default_prompt_names_the_state_note_in_the_main_checkout(self):
        out = self.go()
        self.assertIn('read %s, check in' % os.path.join(self.repo, start.STATE_NOTE), out)
        self.assertNotIn('read docs/ops/state.local.md', out)
        out = self.go(prompt='go now')
        self.assertIn("'go now %s'" % (start.HOME_NOTE % (self.home('alpha-coordinator'), self.repo)).strip(),
                      out.replace("''", "'"))

    def test_a_real_start_makes_the_home_and_opens_the_window_there(self):
        out = self.go(whatif=False)
        path = self.home('alpha-coordinator')
        self.assertEqual(self.host.homes, [(self.repo, path, False)])
        self.assertTrue(os.path.isdir(os.path.dirname(path)))
        text, = self.host.opened
        self.assertIn('Set-Location %s' % window.q(path), text)
        self.assertIn('Started alpha-coordinator-a1', out)

    def test_what_if_creates_no_folder_and_says_the_ignore_check_is_pending(self):
        out = self.go()
        self.assertFalse(os.path.exists(os.path.join(self.repo, '.claude')))
        self.assertIn('Would check that git ignores %s.' % self.home('alpha-coordinator'), out)

    def test_without_the_flag_nothing_changes(self):
        self.host.where = None              # place() would fail the test
        out = self.go(home_worktree=False)
        self.assertIn('Set-Location %s' % window.q(self.repo), out)
        self.assertNotIn('--add-dir', out)
        self.assertIn(window.q(start.DEFAULT_PROMPT), out)
        self.assertEqual(self.host.homes, [])

    def test_a_plain_session_keeps_its_folder(self):
        self.host.where = None
        out = self.go(no_agent=True)
        self.assertIn('Set-Location %s' % window.q(self.repo), out)
        self.assertNotIn('--add-dir', out)
        self.assertEqual(self.host.homes, [])

    def test_a_linked_worktree_or_a_folder_outside_git_is_used_as_given(self):
        for kind, why in ((gitrepo.LINKED, 'a linked worktree'), (gitrepo.NONE, 'not in git')):
            self.host.where = kind
            out = self.go()
            self.assertIn('is not a main checkout (%s); the session starts there.' % why, out)
            self.assertIn('Set-Location %s' % window.q(self.repo), out)
            self.assertNotIn('--add-dir', out)
            self.assertIn(window.q(start.DEFAULT_PROMPT), out)
        self.assertEqual(self.host.homes, [])

    def test_refused_before_any_window(self):
        self.host.where = gitrepo.UNKNOWN
        with self.assertRaisesRegex(S.LaunchError, 'git cannot place'):
            self.go(whatif=False)
        self.host.where = gitrepo.MAIN
        for ignored in (False, None):
            self.host.ignored = ignored
            with self.assertRaisesRegex(S.LaunchError, 'does not ignore .*Nothing was started'):
                self.go(whatif=False)
        self.assertEqual(self.host.opened, [])
        self.assertEqual(self.host.homes, [])

    def test_a_local_settings_file_is_warned_about(self):
        os.makedirs(os.path.join(self.repo, '.claude'))
        local = os.path.join(self.repo, '.claude', 'settings.local.json')
        with open(local, 'w', encoding='utf-8') as f:
            f.write('{}')
        out = self.go()
        self.assertIn('Warning: %s is untracked and is not in the home worktree' % local, out)


class DeepAndConsultantInHome(HomeBase):
    def setUp(self):
        HomeBase.setUp(self)
        self.brief = os.path.join(self.repo, 'brief.md')
        with open(self.brief, 'w', encoding='utf-8') as f:
            f.write('brief')

    def test_deep_starts_in_home_deep_slug_and_reads_its_brief_from_the_main_checkout(self):
        out = '\n'.join(start.deep(self.h.settings, self.host, repo_path=self.repo, requester='alpha-coord-a1',
                                   slug='net-race', brief=self.brief, account='1', plan=True, home_worktree=True))
        self.assert_starts_in_home(out, 'deep-net-race')
        self.assertIn('Read your brief %s first' % self.brief, out)

    def test_consultant_starts_in_home_consultant_repo_with_its_notes_in_the_main_checkout(self):
        out = '\n'.join(start.consultant(self.h.settings, self.host, repo_path=self.repo, account='1', plan=True,
                                         home_worktree=True))
        self.assert_starts_in_home(out, 'consultant-alpha')
        self.assertIn('your notes file %s' % os.path.join(self.repo, 'docs', 'ops', 'consultant-notes.local.md'),
                      out)

    def test_without_the_flag_deep_and_consultant_start_in_the_repo(self):
        self.host.where = None
        for out in (start.deep(self.h.settings, self.host, repo_path=self.repo, requester='r', slug='net-race',
                               brief=self.brief, account='1', plan=True),
                    start.consultant(self.h.settings, self.host, repo_path=self.repo, account='1', plan=True)):
            out = '\n'.join(out)
            self.assertIn('Set-Location %s' % window.q(self.repo), out)
            self.assertNotIn('--add-dir', out)


class HandoffInHome(unittest.TestCase):
    def setUp(self):
        self.h = Home(self, accounts=('1', '2'))
        self.h.write_state('sessions.json', {'alpha': {'repo': 'alpha', 'agent': True, 'stem': 'alpha-coord',
                                                       'state': 'entry-state.md', 'account': 1,
                                                       'name': 'alpha-coord-a1'}})
        self.h.write_state('coordinators.json', {'alpha': 1})
        self.host = FakeHost(self.h.root, ignored=True)
        self.host.where = gitrepo.MAIN
        self.repo = self.h.repos['alpha']

    def test_the_successor_shares_its_predecessors_home(self):
        handoff.handoff(self.h.settings, self.host, 'alpha', '2', whatif=True, home_worktree=True)
        self.assertEqual([p for _, p, _ in self.host.homes], [home.home_path(self.repo, 'alpha-coord')])

    def test_a_new_stem_gets_a_new_home(self):
        handoff.handoff(self.h.settings, self.host, 'alpha', '2', whatif=True, stem='Beta-Coord', home_worktree=True)
        self.assertEqual([p for _, p, _ in self.host.homes], [home.home_path(self.repo, 'beta-coord')])

    def test_without_the_flag_no_home(self):
        self.host.where = None
        handoff.handoff(self.h.settings, self.host, 'alpha', '2', whatif=True)
        self.assertEqual(self.host.homes, [])


class HomeFlag(unittest.TestCase):
    def test_the_flag_parses_on_each_verb_and_defaults_off(self):
        base = {'start': ['start', '--repo', 'alpha', '--account', '1'],
                'deep': ['deep', '--repo-path', '.', '--brief', 'b.md', '--requester', 'r', '--slug', 'net-race'],
                'consultant': ['consultant', '--repo-path', '.'],
                'handoff': ['handoff', '--session', 'alpha', '--account', '2']}
        for verb, argv in base.items():
            self.assertFalse(launch.parser().parse_args(argv).home_worktree, verb)
            self.assertTrue(launch.parser().parse_args(argv + ['--home-worktree']).home_worktree, verb)

    def test_run_passes_it_through(self):
        h = Home(self, accounts=('1', '2'))
        host = FakeHost(h.root, ignored=True)
        host.where = gitrepo.MAIN
        launch.run(launch.parser().parse_args(['start', '--repo', 'alpha', '--account', '1', '--what-if',
                                               '--home-worktree']), h.settings, host)
        self.assertEqual([p for _, p, _ in host.homes], [home.home_path(h.repos['alpha'], 'alpha-coordinator')])


if __name__ == '__main__':
    unittest.main()
