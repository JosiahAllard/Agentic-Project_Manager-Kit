"""kitlib.hubsite.derived: notices, repos, usage and agents are read from their source on every call,
stored nowhere, and nothing is written. Temp folders and fakes only."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.hubsite import derived

CLOCK = lambda: '2026-01-02T03:04:05+00:00'      # noqa: E731


def snapshot(root):
    out = {}
    for dp, _d, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dp, f)
            with open(p, 'rb') as fh:
                out[p] = (fh.read(), os.path.getmtime(p))
    return out


class Tmp(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def put(self, rel, text, binary=False):
        p = os.path.join(self.d, *rel.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(text)
        return p


class NoticesTests(Tmp):
    def doc(self, run, phase, at):
        return json.dumps({'id': 'L-%s-%s' % (run, phase), 'title': 't', 'at': at,
                           'kind': 'lock-window', 'run': run, 'phase': phase})

    def test_new_lock_notice_file_appears_with_no_other_tool_run(self):
        notices = os.path.join(self.d, 'n')
        self.assertEqual(derived.notices(notices), [])         # folder not there yet
        os.makedirs(notices)
        self.assertEqual(derived.notices(notices), [])
        self.put('n/r1-start.json', self.doc('r1', 'start', '2026-01-01T10:00:00Z'))
        self.assertEqual([n['id'] for n in derived.notices(notices)], ['L-r1-start'])
        self.put('n/r1-end.json', self.doc('r1', 'end', '2026-01-01T11:00:00Z'))
        self.assertEqual([n['id'] for n in derived.notices(notices)], ['L-r1-start', 'L-r1-end'])

    def test_oldest_first_and_bad_files_skipped(self):
        self.put('n/b-start.json', self.doc('b', 'start', '2026-01-02T00:00:00Z'))
        self.put('n/a-start.json', self.doc('a', 'start', '2026-01-01T00:00:00Z'))
        self.put('n/half.json', '{"id": "L-x')
        self.put('n/noid.json', '{"title": "x"}')
        self.put('n/note.txt', 'not json')
        self.put('n/posted/old-start.json', self.doc('old', 'start', '2025-01-01T00:00:00Z'))
        got = derived.notices(os.path.join(self.d, 'n'))
        self.assertEqual([n['id'] for n in got], ['L-a-start', 'L-b-start'])

    def test_notice_by_id_and_unset_folder(self):
        self.put('n/a-start.json', self.doc('a', 'start', '2026-01-01T00:00:00Z'))
        n = os.path.join(self.d, 'n')
        self.assertEqual(derived.notice(n, 'L-a-start')['run'], 'a')
        self.assertIsNone(derived.notice(n, 'L-zzz-start'))
        self.assertEqual(derived.notices(None), [])

    def test_reading_writes_nothing(self):
        self.put('n/a-start.json', self.doc('a', 'start', '2026-01-01T00:00:00Z'))
        before = snapshot(self.d)
        derived.notices(os.path.join(self.d, 'n'))
        self.assertEqual(snapshot(self.d), before)
        self.assertFalse(os.path.exists(os.path.join(self.d, 'n', 'posted')))


class ReposTests(unittest.TestCase):
    CFG = {'sessions_file': 'S:/sessions.json', 'repos': ['Zeta']}
    READ = staticmethod(lambda p: json.dumps({'a': {'repo': 'alpha'}, 'b': {'repo': 'Beta'}}))

    def call(self, stored=(), cfg=None):
        return derived.repos(cfg or self.CFG, stored, read=self.READ,
                             toplevel=lambda d: None, kit_dir='K')

    def test_names_from_known_repos_with_no_aliases(self):
        self.assertEqual(self.call(), [{'name': 'alpha', 'aliases': []},
                                       {'name': 'Beta', 'aliases': []},
                                       {'name': 'Zeta', 'aliases': []}])

    def test_aliases_joined_by_name_ignoring_case_and_unknown_stored_repo_kept(self):
        got = self.call([{'name': 'beta', 'aliases': ['b', 'bb', 'b']},
                         {'name': 'Gamma', 'aliases': ['g']},
                         {'name': 'alpha', 'aliases': 'oops'}, 'junk', {'aliases': ['x']}])
        self.assertEqual(got, [{'name': 'alpha', 'aliases': []},
                               {'name': 'Beta', 'aliases': ['b', 'bb']},
                               {'name': 'Gamma', 'aliases': ['g']},
                               {'name': 'Zeta', 'aliases': []}])

    def test_config_errors_propagate(self):
        with self.assertRaises(KeyError):
            derived.repos({}, read=self.READ)


class UsageTests(unittest.TestCase):
    LINES = ['account 1: five_hour=40% (resets Sun 10:00)  seven_day=71% (resets Mon 09:00)  [updated 3 min ago]',
             'account 2: no usage file',
             'current account: 1',
             'account codex: five_hour=reset  seven_day=12% (resets Tue 09:00)  [updated 5 min ago]']

    def test_parsed_accounts(self):
        got = derived.usage(lambda: self.LINES, CLOCK)
        self.assertTrue(got['available'])
        self.assertEqual(got['accounts'], {
            '1': {'five': 40, 'week': 71, 'age': 3, 'stale': False, 'missing': False},
            '2': {'five': None, 'week': None, 'age': None, 'stale': True, 'missing': True},
            'codex': {'five': 0, 'week': 12, 'age': 5, 'stale': False, 'missing': False}})
        self.assertEqual(got['as_of'], CLOCK())

    def test_stale_missing_and_unknown_readings_reach_the_hub_from_the_files(self):
        # End to end: usage-status.json files -> kitlib.launcher.usage lines -> parse_usage -> the hub view.
        import datetime
        from kitlib.launcher import usage as launcher_usage
        from tests.launcher.fakes import NOW, Home
        h = Home(self, accounts=('1', '2', '3', '4'))
        day = datetime.timedelta(days=1)
        h.usage('1', week=(98, NOW + day), updated=NOW - datetime.timedelta(minutes=3735))   # five_hour null
        h.usage('2', five=(8, NOW + day / 8), week=(18, NOW + 5 * day))
        h.usage('3', five=(70, NOW - day), week=(None, NOW + day), updated=NOW - datetime.timedelta(minutes=668))
        got = derived.usage(lambda: launcher_usage.report(h.settings, NOW), CLOCK)
        self.assertTrue(got['available'])
        acc = got['accounts']
        self.assertEqual(acc['1'], {'five': None, 'week': 98, 'age': 3735, 'stale': True, 'missing': False})
        self.assertEqual(acc['2'], {'five': 8, 'week': 18, 'age': 0, 'stale': False, 'missing': False})
        self.assertEqual(acc['3'], {'five': 0, 'week': None, 'age': 668, 'stale': True, 'missing': False})
        self.assertEqual(acc['4'], {'five': None, 'week': None, 'age': None, 'stale': True, 'missing': True})

    def test_no_source_is_not_available(self):
        got = derived.usage(None, CLOCK)
        self.assertFalse(got['available'])
        self.assertIn('no launcher', got['reason'])
        self.assertNotIn('accounts', got)

    def test_source_that_fails_or_prints_nothing_is_not_available(self):
        def boom():
            raise OSError('disk gone')
        self.assertIn('disk gone', derived.usage(boom, CLOCK)['reason'])
        self.assertFalse(derived.usage(lambda: ['current account: unknown'], CLOCK)['available'])
        only = derived.usage(lambda: ['account 2: no usage file'], CLOCK)       # listed as missing, not dropped
        self.assertEqual(only['accounts']['2']['missing'], True)


class AgentsTests(Tmp):
    def test_agents_from_frontmatter(self):
        self.put('a/worker.md', '---\nname: worker\ndescription: Does work.\nmodel: sonnet\neffort: medium\n---\nbody\n')
        self.put('a/lander.md', '---\nname: lander\ndescription: Lands.\nmodel: haiku\n---\nbody\n')
        self.put('a/bare.md', 'no frontmatter\n')
        self.put('a/readme.txt', 'ignored')
        got = derived.agents(os.path.join(self.d, 'a'))
        self.assertTrue(got['available'])
        self.assertEqual(got['agents'], [
            {'name': 'bare', 'description': '', 'model': '', 'effort': '-'},
            {'name': 'lander', 'description': 'Lands.', 'model': 'haiku', 'effort': '-'},
            {'name': 'worker', 'description': 'Does work.', 'model': 'sonnet', 'effort': 'medium'}])

    def test_missing_folder_is_not_available(self):
        got = derived.agents(os.path.join(self.d, 'nope'))
        self.assertFalse(got['available'])
        self.assertIn('reason', got)

    def test_the_server_reads_the_selected_hosts_rendered_agents(self):
        from kitlib.host import registry
        from kitlib.hubsite import serve
        for name in registry.names():
            with self.subTest(host=name):
                want = os.path.join(serve.pmstart.KIT_ROOT, os.path.normpath(registry.host(name).shell), 'agents')
                self.assertEqual(serve.agents_dir({registry.ENV: name}), want)
        default = registry.host(registry.default()).shell
        got = derived.agents(serve.agents_dir({}))
        self.assertTrue(got['available'], (default, got))
        self.assertIn('worker', [a['name'] for a in got['agents']])
        self.assertIsNone(serve.agents_dir({registry.ENV: 'no-such-host'}))


class SkillsTests(Tmp):
    def tree(self):
        self.put('s/builder/SKILL.md', '---\nname: builder\ndescription: Builds.\n---\n\n# Builder\n\nStep one.\n')
        self.put('s/bare/SKILL.md', 'no frontmatter\n')
        self.put('s/builder/extra.md', 'not a skill file')
        self.put('s/stray.txt', 'a file, not a skill folder')
        os.makedirs(os.path.join(self.d, 's', 'empty'))
        return os.path.join(self.d, 's')

    def test_the_list_is_the_sources_names_and_descriptions_with_no_body(self):
        got = derived.skills(self.tree())
        self.assertTrue(got['available'])
        self.assertEqual(got['skills'], [{'name': 'bare', 'description': ''},
                                         {'name': 'builder', 'description': 'Builds.'}])

    def test_one_skill_has_its_body_and_only_a_listed_name_is_read(self):
        root = self.tree()
        got = derived.skill(root, 'builder')
        self.assertEqual((got['name'], got['description']), ('builder', 'Builds.'))
        self.assertEqual(got['body'].replace('\r\n', '\n'), '# Builder\n\nStep one.')
        self.assertEqual(derived.skill(root, 'bare')['body'], 'no frontmatter')
        self.put('secret/SKILL.md', 'outside')
        for bad in ('empty', 'nope', '..', '../secret', '..' + os.sep + 'secret', 's', 'stray.txt', '', None, 7):
            with self.subTest(name=bad):
                self.assertIsNone(derived.skill(root, bad))
        self.assertIsNone(derived.skill(os.path.join(self.d, 'nope'), 'builder'))

    def test_missing_folder_is_not_available(self):
        got = derived.skills(os.path.join(self.d, 'nope'))
        self.assertFalse(got['available'])
        self.assertIn('reason', got)

    def test_the_server_reads_the_kits_content_skills_never_a_rendered_copy(self):
        from kitlib.hubsite import serve
        got = derived.skills(serve.skills_dir())
        self.assertTrue(got['available'], got)
        self.assertEqual(serve.skills_dir(), os.path.join(serve.pmstart.KIT_ROOT, 'content', 'skills'))
        by = {s['name']: s for s in got['skills']}
        self.assertIn('builder', by)
        self.assertEqual(sorted(by['builder']), ['description', 'name'])
        self.assertTrue(by['builder']['description'])
        one = derived.skill(serve.skills_dir(), 'builder')
        self.assertTrue(one['body'].startswith('# Builder'))
        self.assertNotIn('name: builder', one['body'])


class LintRunsTests(Tmp):
    """derived.lint_runs: each repository's last maintenance lint run and its skip marker, from
    <lint_dir>/<repo>/ (KIT-HUB-FINDINGS-LINT-RUNS); what is on disk never makes it raise."""
    SKIP = {'reason': 'busy', 'at': '2026-01-09T03:00:00Z', 'code': 3}

    def run_file(self, repo, run):
        return self.put('%s/result.json' % repo, run if isinstance(run, str) else json.dumps(run))

    def test_a_run_shows_its_start_and_ledger_and_a_marker_shows_beside_it(self):
        led = {'stale_cites': 1, 'superseded': 0}
        self.run_file('kit', {'repo': 'kit', 'started': '2026-01-02T03:04:05Z', 'ledger': led,
                              'findings': []})
        self.put('Alpha/skipped.json', json.dumps(self.SKIP))
        self.assertEqual(derived.lint_runs(self.d), [
            {'repo': 'Alpha', 'started': None, 'ledger': None, 'skip': self.SKIP},
            {'repo': 'kit', 'started': '2026-01-02T03:04:05Z', 'ledger': led, 'skip': None}])

    def test_wrong_types_read_as_none(self):
        self.run_file('kit', {'repo': 5, 'started': 7, 'ledger': [1]})
        self.assertEqual(derived.lint_runs(self.d),
                         [{'repo': 'kit', 'started': None, 'ledger': None, 'skip': None}])

    def test_nothing_readable_is_no_run_and_never_an_error(self):
        self.assertEqual(derived.lint_runs(None), [])
        self.assertEqual(derived.lint_runs(''), [])
        self.assertEqual(derived.lint_runs(os.path.join(self.d, 'missing')), [])
        self.assertEqual(derived.lint_runs(self.put('a-file', 'x')), [])      # not a folder
        self.run_file('garbage', '{not json')
        self.run_file('list', '[1, 2]')
        os.makedirs(os.path.join(self.d, 'dir', 'result.json'))             # a folder by its name
        self.put('badskip/skipped.json', '{"reason": 3}')
        self.put('stray.txt', 'not a repo folder')
        os.makedirs(os.path.join(self.d, 'empty'))
        self.assertEqual(derived.lint_runs(self.d), [])

    def test_an_unreadable_folder_is_no_run(self):
        self.run_file('kit', {'repo': 'kit'})
        with mock.patch.object(derived.os, 'listdir', side_effect=PermissionError('denied')):
            self.assertEqual(derived.lint_runs(self.d), [])


if __name__ == '__main__':
    unittest.main()
