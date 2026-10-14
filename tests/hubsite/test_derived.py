"""kitlib.hubsite.derived: notices, repos, usage and agents are read from their source on every call,
stored nowhere, and nothing is written. Temp folders and fakes only."""
import json
import os
import shutil
import tempfile
import unittest

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
        self.assertEqual(got['accounts'], {'1': {'five': 40, 'week': 71},
                                           'codex': {'five': 0, 'week': 12}})
        self.assertEqual(got['as_of'], CLOCK())

    def test_no_source_is_not_available(self):
        got = derived.usage(None, CLOCK)
        self.assertFalse(got['available'])
        self.assertIn('no launcher', got['reason'])
        self.assertNotIn('accounts', got)

    def test_source_that_fails_or_prints_nothing_is_not_available(self):
        def boom():
            raise OSError('disk gone')
        self.assertIn('disk gone', derived.usage(boom, CLOCK)['reason'])
        self.assertFalse(derived.usage(lambda: ['account 2: no usage file'], CLOCK)['available'])


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


if __name__ == '__main__':
    unittest.main()
