"""--home-worktree on start, deep, consultant and handoff (kitlib/launcher/start.py home_folder,
docs/launcher.md "The home worktree"): who may use it (deep-worker and consultant sessions), where the window
starts, what the session is told, what is refused before any window or folder, and that without the
flag nothing changes. The home worktree itself is FakeHost.home_worktree here;
tests/launcher/test_home.py covers home.ensure."""
import os
import sys
import unittest
from unittest import mock

from kitlib.git import repo as gitrepo
from kitlib.host import registry
from kitlib.launcher import handoff, home, start, window
from kitlib.launcher import settings as S
from tests.launcher.fakes import FakeHost, Home, session
from tests.launcher.test_start import Base

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'tools'))
import launch                                                       # noqa: E402

PERMISSION = '--permission-mode auto'
DEEP = 'pm-kit:deep-worker'
NAME = 'alpha-deep-worker-a1'          # start()'s name for a deep-worker on alpha, account 1


class HomeBase(Base):
    def setUp(self):
        Base.setUp(self)
        self.host = FakeHost(self.h.root, ignored=True)
        self.host.where = gitrepo.MAIN

    def home(self, name):
        return home.home_path(self.repo, name)

    def go(self, **kw):
        args = dict(repo='alpha', account='1', whatif=True, home_worktree=True, agent=DEEP)
        args.update(kw)
        return '\n'.join(start.start(self.h.settings, self.host, **args))

    def no_folder(self):
        self.assertFalse(os.path.exists(os.path.join(self.repo, os.path.dirname(home.folder()))))

    def assert_starts_in_home(self, out, name):
        path = self.home(name)
        self.assertIn('Set-Location %s' % window.q(path), out)
        self.assertNotIn('Set-Location %s;' % window.q(self.repo), out)
        self.assertIn('--add-dir %s %s' % (window.q(self.repo), PERMISSION), out)
        self.assertIn(start.HOME_NOTE % (path, self.repo), out.replace("''", "'"))
        return path


class StartInHome(HomeBase):
    def test_a_deep_worker_starts_in_its_home_with_the_main_checkout_added(self):
        out = self.go()
        path = self.assert_starts_in_home(out, NAME)
        self.assertLess(out.index('HOME %s whatif=True' % path), out.index('Would open a new window running:'))
        self.assertEqual(self.host.homes, [(self.repo, path, True)])
        self.assertEqual(self.host.opened, [])

    def test_the_session_is_told_not_to_write_in_the_home_and_never_that_it_is_read_only(self):
        out = self.go(prompt='go now').replace("''", "'")
        self.assertIn("'go now%s'" % (start.HOME_NOTE % (self.home(NAME), self.repo)), out)
        self.assertIn('do not write there; your branch work goes in your own worktree', start.HOME_NOTE)
        self.assertIn('the main checkout, by absolute path, is read-only to you', start.HOME_NOTE)
        self.assertNotIn('detached and read-only', out)

    def test_a_real_start_makes_the_home_and_opens_the_window_there(self):
        out = self.go(whatif=False)
        path = self.home(NAME)
        self.assertEqual(self.host.homes, [(self.repo, path, False)])
        self.assertTrue(os.path.isdir(os.path.dirname(path)))
        text, = self.host.opened
        self.assertIn('Set-Location %s' % window.q(path), text)
        self.assertIn('Started %s' % NAME, out)

    def test_what_if_creates_no_folder(self):
        self.go()
        self.no_folder()

    def test_without_the_flag_nothing_changes(self):
        self.host.where = None              # place() would fail the test
        for agent in (None, DEEP):          # None: the coordinator agent
            out = self.go(home_worktree=False, agent=agent)
            self.assertIn('Set-Location %s' % window.q(self.repo), out)
            self.assertNotIn('--add-dir', out)
        self.assertIn(window.q(start.DEFAULT_PROMPT), self.go(home_worktree=False, agent=None))
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
        self.assertEqual(self.host.homes, [])

    def test_refused_before_any_window_or_folder(self):
        self.host.where = gitrepo.UNKNOWN
        with self.assertRaisesRegex(S.LaunchError, 'git cannot place'):
            self.go(whatif=False)
        self.host.where = gitrepo.MAIN
        for ignored in (False, None):
            self.host.ignored = ignored
            for whatif in (True, False):
                with self.assertRaisesRegex(S.LaunchError, 'does not ignore .*Nothing was started'):
                    self.go(whatif=whatif)
        self.assertEqual(self.host.opened, [])
        self.assertEqual(self.host.homes, [])
        self.no_folder()                    # the ignore check runs before any folder is made in main

    def test_a_running_session_of_the_same_name_is_refused_and_its_home_left(self):
        self.host = FakeHost(self.h.root, procs=[session(4242, NAME, agent=DEEP)], ignored=True)
        self.host.where = gitrepo.MAIN
        for whatif in (True, False):
            with self.assertRaisesRegex(S.LaunchError, "a session named '%s' is already running \\(PID 4242\\) "
                                                       "in its home worktree" % NAME):
                self.go(whatif=whatif)
        self.assertEqual(self.host.homes, [])
        self.assertEqual(self.host.opened, [])
        self.no_folder()

    def test_a_running_session_whose_name_differs_only_in_case_is_refused(self):
        for running, name in ((NAME, NAME.upper()), (NAME.title(), NAME), (NAME, NAME + '-')):
            self.host = FakeHost(self.h.root, procs=[session(4242, running, agent=DEEP)], ignored=True)
            self.host.where = gitrepo.MAIN
            with self.assertRaisesRegex(S.LaunchError, "a session named '%s' is already running \\(PID 4242\\) in "
                                                       "its home worktree .*, the one '%s' would use"
                                        % (running, name)):
                self.go(whatif=False, name=name)
            self.assertEqual(self.host.homes, [])
            self.assertEqual(self.host.opened, [])
        self.no_folder()

    def test_a_broken_hosts_json_is_a_launch_error(self):
        def broken():
            raise registry.RegistryError('hosts.json is broken')
        with mock.patch.object(registry, 'default_profile', broken):
            for whatif in (True, False):
                with self.assertRaisesRegex(S.LaunchError, 'hosts.json is broken; nothing was started'):
                    self.go(whatif=whatif)
        self.assertEqual(self.host.opened, [])
        self.assertEqual(self.host.homes, [])

    def test_a_local_settings_file_is_warned_about(self):
        local = os.path.join(self.repo, os.path.dirname(home.folder()), 'settings.local.json')
        os.makedirs(os.path.dirname(local))
        with open(local, 'w', encoding='utf-8') as f:
            f.write('{}')
        out = self.go()
        self.assertIn('Warning: %s is untracked and is not in the home worktree' % local, out)


class OnlyDeepWorkersAndConsultants(HomeBase):
    """A coordinator and other roles work in the main checkout, which a session in a linked worktree
    may not write (docs/guard.md "Write jail"): refused, nothing made."""

    def assert_refused(self, role, call):
        self.host.where = None              # place() would fail the test
        with self.assertRaises(S.LaunchError) as c:
            call()
        self.assertEqual(str(c.exception), start.HOME_REFUSED % role)
        self.assertEqual(self.host.opened, [])
        self.assertEqual(self.host.homes, [])
        self.no_folder()

    def test_a_coordinator_and_any_other_agent_are_refused(self):
        for agent, role in ((None, 'coordinator'), ('coordinator', 'coordinator'), ('pm-kit:pm', 'pm'),
                            ('my-agent', 'my-agent')):
            for whatif in (True, False):
                self.assert_refused(role, lambda: self.go(agent=agent, whatif=whatif))

    def test_the_refusal_names_the_roles_that_may(self):
        self.assertIn('for deep-worker and consultant sessions only', start.HOME_REFUSED)
        self.assertEqual(start.HOME_ROLES, (window.DEEP_WORKER, window.CONSULTANT))

    def test_the_refusal_says_where_the_state_note_is(self):
        self.assertNotIn('but its other work is', start.HOME_REFUSED)
        self.assertIn('kept clean for landings', start.HOME_REFUSED)
        self.assertIn('state note is in agent temp', start.HOME_REFUSED)

    def test_a_handoff_is_refused_before_any_successor_starts(self):
        self.h.write_state('sessions.json', {
            'deep': {'repo': 'alpha', 'agent': 'deep-worker', 'stem': 'deep-x', 'account': 1, 'name': 'deep-x-a1'},
            'alpha': {'repo': 'alpha', 'agent': True, 'stem': 'alpha-coord', 'account': 1, 'name': 'alpha-coord-a1'}})
        before = self.h.read_state('sessions.json')
        self.assert_refused('coordinator', lambda: handoff.handoff(self.h.settings, self.host, all_=True,
                                                                   account='2', home_worktree=True))
        self.assert_refused('coordinator', lambda: handoff.handoff(self.h.settings, self.host, 'alpha', '2',
                                                                   home_worktree=True))
        self.assertEqual(self.h.read_state('sessions.json'), before)    # the deep entry was not handed off

    def test_the_flag_parses_on_each_verb_and_reaches_the_refusal(self):
        h = Home(self, accounts=('1', '2'))
        host = FakeHost(h.root, ignored=True)
        with self.assertRaises(S.LaunchError) as c:
            launch.run(launch.parser().parse_args(['start', '--repo', 'alpha', '--account', '1', '--what-if',
                                                   '--home-worktree']), h.settings, host)
        self.assertEqual(str(c.exception), start.HOME_REFUSED % 'coordinator')


class ConsultantInHome(HomeBase):
    """A consultant's notes file is in agent temp, which the write jail leaves open, so it may start
    in its home worktree; a --notes-path in the main checkout is refused there instead."""

    def setUp(self):
        HomeBase.setUp(self)
        self.temp = os.path.join(self.h.root, 'agent-temp')
        self.host = FakeHost(self.h.root, env={'AGENT_TEMP': self.temp}, ignored=True)
        self.host.where = gitrepo.MAIN

    def cons(self, **kw):
        args = dict(repo_path=self.repo, account='1', plan=True, home_worktree=True)
        args.update(kw)
        return '\n'.join(start.consultant(self.h.settings, self.host, **args))

    def test_a_consultant_starts_in_its_home_and_keeps_its_notes_in_agent_temp(self):
        for plan in (True, False):
            out = self.cons(plan=plan)
            if not plan:
                out = self.host.opened[-1]       # the window script it opened
            self.assert_starts_in_home(out, 'consultant-alpha')
            self.assertIn('your notes file %s ' % os.path.join(self.temp, 'alpha-consultant', 'notes.md'), out)
        self.assertEqual(len(self.host.opened), 1)

    def test_a_notes_path_in_the_main_checkout_is_refused_before_anything(self):
        self.host.where = None              # place() would fail the test
        for notes in ('docs/ops/consultant-notes.local.md', os.path.join(self.repo, 'n.md')):
            for plan in (True, False):
                with self.assertRaisesRegex(S.LaunchError, r"--home-worktree: the notes file .* is in the main "
                                                           r"checkout .*, which a session started in a home worktree "
                                                           r"cannot write \(the guard's write jail\); leave out "
                                                           r"--notes-path \(agent temp\)\. Nothing was started\."):
                    self.cons(notes_path=notes, plan=plan)
        self.assertEqual(self.host.opened, [])
        self.assertEqual(self.host.homes, [])
        self.no_folder()

    def test_a_notes_path_outside_the_main_checkout_is_allowed(self):
        elsewhere = os.path.join(self.h.root, 'notes.md')
        out = self.cons(notes_path=elsewhere)
        self.assert_starts_in_home(out, 'consultant-alpha')
        self.assertIn('your notes file %s ' % elsewhere, out)


class DeepInHome(HomeBase):
    def setUp(self):
        HomeBase.setUp(self)
        self.brief = os.path.join(self.repo, 'brief.md')
        with open(self.brief, 'w', encoding='utf-8') as f:
            f.write('brief')

    def deep(self, slug, **kw):
        args = dict(repo_path=self.repo, requester='alpha-coord-a1', slug=slug, brief=self.brief, account='1',
                    plan=True, home_worktree=True)
        args.update(kw)
        return '\n'.join(start.deep(self.h.settings, self.host, **args))

    def test_deep_starts_in_home_deep_slug_and_reads_its_brief_from_the_main_checkout(self):
        out = self.deep('net-race')
        self.assert_starts_in_home(out, 'deep-net-race')
        self.assertIn('Read your brief %s first' % self.brief, out)

    def test_a_slug_with_a_trailing_dash_is_refused_while_its_twin_runs(self):
        self.host = FakeHost(self.h.root, procs=[session(4242, 'deep-x1', agent=DEEP)], ignored=True)
        self.host.where = gitrepo.MAIN
        for whatif in (True, False):        # --slug takes no upper case; start --name does (above)
            with self.assertRaisesRegex(S.LaunchError, "a session named 'deep-x1' is already running \\(PID 4242\\) "
                                                       "in its home worktree .*, the one 'deep-x1-' would use"):
                self.deep('x1-', plan=whatif)
        self.assertEqual(self.host.homes, [])
        self.assertEqual(self.host.opened, [])

    def test_each_slug_gets_its_own_home(self):
        for slug in ('x1', 'x1-r2', 'x1-a1'):
            self.deep(slug)
        self.assertEqual([p for _, p, _ in self.host.homes],
                         [self.home('deep-x1'), self.home('deep-x1-r2'), self.home('deep-x1-a1')])

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
        self.h.write_state('sessions.json', {'deep': {'repo': 'alpha', 'agent': 'deep-worker', 'stem': 'deep-x',
                                                      'account': 1, 'name': 'deep-x-a1'}})
        self.host = FakeHost(self.h.root, ignored=True)
        self.host.where = gitrepo.MAIN
        self.repo = self.h.repos['alpha']

    def test_a_deep_worker_successor_gets_its_own_home_never_its_running_predecessors(self):
        out = handoff.handoff(self.h.settings, self.host, 'deep', '2', whatif=True, home_worktree=True)
        self.assertEqual([p for _, p, _ in self.host.homes], [home.home_path(self.repo, 'deep-x-a2')], out)

    def test_without_the_flag_no_home(self):
        self.host.where = None
        handoff.handoff(self.h.settings, self.host, 'deep', '2', whatif=True)
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
        repo = h.repos['alpha']
        with open(os.path.join(repo, 'b.md'), 'w', encoding='utf-8') as f:
            f.write('brief')
        launch.run(launch.parser().parse_args(['deep', '--repo-path', repo, '--brief', 'b.md', '--requester', 'r',
                                               '--slug', 'net-race', '--account', '1', '--plan',
                                               '--home-worktree']), h.settings, host)
        self.assertEqual([p for _, p, _ in host.homes], [home.home_path(repo, 'deep-net-race')])


if __name__ == '__main__':
    unittest.main()
