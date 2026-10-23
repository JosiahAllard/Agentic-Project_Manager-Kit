"""The rulebooks (docs/ledger.md "Rulebooks"): kitlib.ledger.rulebook's parse, render, check and
repo mapping, the rules folder of files.of_config, the pre-commit targets, and tools/ledger.py's
`rules` and `check` arguments and exit codes. Ids are placeholders built by rid(n)."""
import os
import subprocess
import sys
import tempfile
import unittest

from kitlib.ledger import files, fmt, precommit, rulebook
from tests.home import machine_config

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'ledger.py')
WHEN = '2026-10-06 08:00Z'

KIT = '''# Kit rules
Repos: Agentic-Project_Manager-Kit
Budget: 2

## Landing
- landing.batch: Landers batch up to three branches. (from R-0001, R-0002)
- landing.alone: A high-risk branch lands alone. (from R-0002)

## Machine
- machine.lock: Heavy runs take the machine lock. (from R-0003)
'''
ALL = '''# Shared rules
Repos: all

## Process
- process.refusal: A refusal is final; report it verbatim. (from R-0001)
- machine.lock: Heavy runs wait in the queue. (from R-0002)
'''


def rid(n):
    return 'R-%04d' % n


def log_text(count=3):
    return '# Ledger\n\n' + ''.join(
        fmt.render(rid(n), WHEN, 'all', 'selected', 'q', 'a', 'Rule %d.' % n, 's', '-')
        for n in range(1, count + 1))


class ParseTest(unittest.TestCase):

    def test_round_trip(self):
        book, errs = rulebook.parse(KIT, 'kit')
        self.assertEqual(errs, [])
        self.assertEqual(book.title, 'Kit rules')
        self.assertEqual(book.repos, ('Agentic-Project_Manager-Kit',))
        self.assertEqual(book.budget, 2)
        self.assertEqual([r.key for r in book.rules], ['landing.batch', 'landing.alone', 'machine.lock'])
        self.assertEqual(book.rules[0].sources, (rid(1), rid(2)))
        self.assertEqual(book.rules[2].topic, 'Machine')
        text = rulebook.render(book)
        self.assertEqual(text, KIT)
        again, errs = rulebook.parse(text, 'kit')
        self.assertEqual(errs, [])
        self.assertEqual([r[:4] for r in again.rules], [r[:4] for r in book.rules])
        self.assertEqual(again[:4], book[:4])

    def test_default_budget(self):
        book, errs = rulebook.parse(ALL, 'all')
        self.assertEqual(errs, [])
        self.assertEqual(book.budget, rulebook.DEFAULT_BUDGET)

    def test_duplicate_key(self):
        _, errs = rulebook.parse(KIT + '- machine.lock: Again. (from R-0001)\n', 'kit')
        self.assertEqual(len(errs), 1)
        self.assertIn('duplicate key machine.lock', errs[0])
        self.assertIn('kit.md:', errs[0])

    def test_missing_source(self):
        _, errs = rulebook.parse(KIT + '- machine.free: No source here.\n', 'kit')
        self.assertEqual(len(errs), 1)
        self.assertIn('machine.free has no source', errs[0])

    def test_a_source_must_be_a_log_id(self):
        _, errs = rulebook.parse(KIT + '- machine.free: Cites a rule. (from machine.lock)\n', 'kit')
        self.assertEqual(len(errs), 1)
        self.assertIn('not a log entry id', errs[0])

    def test_unknown_line(self):
        _, errs = rulebook.parse(KIT + 'stray prose\n', 'kit')
        self.assertEqual(len(errs), 1)
        self.assertIn("unknown line 'stray prose'", errs[0])

    def test_header_problems(self):
        _, errs = rulebook.parse('# T\n\n## A\n- a.b: x (from R-0001)\n', 'kit')
        self.assertEqual(errs, ['kit.md: no `Repos:` header line'])
        _, errs = rulebook.parse('# T\nRepos: x\nBudget: many\n', 'kit')
        self.assertIn('Budget needs a positive number', errs[0])
        _, errs = rulebook.parse('# T\nRepos: x\n- a.b: x (from R-0001)\n', 'kit')
        self.assertIn('before any `## topic` heading', errs[0])
        _, errs = rulebook.parse('# T\nRepos: x\n## A\nRepos: y\n', 'kit')
        self.assertIn('unknown line', errs[0])

    def test_a_bad_key(self):
        _, errs = rulebook.parse('# T\nRepos: x\n## A\n- Landing: x (from R-0001)\n', 'kit')
        self.assertEqual(len(errs), 1)
        self.assertIn('not <topic-slug>.<slug>', errs[0])


class CheckTest(unittest.TestCase):

    def books(self):
        kit, _ = rulebook.parse(KIT, 'kit')
        shared, _ = rulebook.parse(ALL, 'all')
        return {'kit': kit, 'all': shared}

    def ids(self, count):
        return {rid(n) for n in range(1, count + 1)}

    def test_clean_books_list_the_override_and_the_budget(self):
        got = rulebook.check(self.books(), self.ids(3))
        self.assertEqual(got.errors, [])
        self.assertEqual(sorted(got.notes), sorted([
            'kit.md:10: rule machine.lock overrides the shared rule',
            'kit.md: 3 rules, over its budget of 2']))

    def test_a_cited_id_missing_from_the_log(self):
        got = rulebook.check(self.books(), self.ids(2))
        self.assertEqual(got.errors, ['kit.md:10: rule machine.lock cites R-0003, which is not in the log'])


class ForRepoTest(unittest.TestCase):

    def books(self):
        out = {}
        for name, repos in (('kit', 'Agentic-Project_Manager-Kit'), ('pm', 'project-manager, Pm_Tools'),
                            ('all', 'all'), ('griffball', 'GriffBall')):
            out[name] = rulebook.parse('# T\nRepos: %s\n' % repos, name)[0]
        out['retribution'] = rulebook.parse('# T\nRepos: elsewhere\n', 'retribution')[0]
        return out

    def test_repos_line_maps_a_folder(self):
        books = self.books()
        self.assertEqual(rulebook.for_repo(books, 'Agentic-Project_Manager-Kit'), 'kit')
        self.assertEqual(rulebook.for_repo(books, 'agentic-project-manager-kit'), 'kit')
        self.assertEqual(rulebook.for_repo(books, 'griffball'), 'griffball')
        self.assertEqual(rulebook.for_repo(books, 'project-manager'), 'pm')
        self.assertEqual(rulebook.for_repo(books, 'Pm_Tools'), 'pm')

    def test_the_file_stem_is_the_fallback_and_shared_is_never_a_repo(self):
        books = self.books()
        self.assertEqual(rulebook.for_repo(books, 'Retribution'), 'retribution')
        self.assertIsNone(rulebook.for_repo(books, 'unknown'))
        self.assertIsNone(rulebook.for_repo(books, 'all'))

    def test_view_marks_an_override_and_drops_the_shared_copy(self):
        kit, _ = rulebook.parse(KIT, 'kit')
        shared, _ = rulebook.parse(ALL, 'all')
        views = rulebook.view({'kit': kit, 'all': shared}, 'Agentic-Project_Manager-Kit')
        self.assertEqual([b.name for b, _ in views], ['all', 'kit'])
        shared_lines, n = rulebook.lines(*views[0])
        self.assertEqual(n, 1)
        self.assertNotIn('machine.lock', '\n'.join(shared_lines))
        kit_lines, _ = rulebook.lines(*views[1], sources=True)
        self.assertIn('- machine.lock: Heavy runs take the machine lock. [overrides shared] (from R-0003)',
                      kit_lines)
        self.assertEqual(kit_lines[0], '# kit (3 rules, over its budget of 2)')

    def test_topic_filter(self):
        kit, _ = rulebook.parse(KIT, 'kit')
        out, n = rulebook.lines(kit, set(), topic='landing')
        self.assertEqual(n, 2)
        self.assertEqual(out[1], '## Landing')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.dir = os.path.join(self.home, 'rulings')
        os.makedirs(os.path.join(self.dir, 'rules'))
        self.log = os.path.join(self.dir, 'LEDGER.md')
        self.put(self.log, log_text())
        machine_config(self.home, {'ledger_path': self.log}, case=self)

    def put(self, path, text):
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)

    def book(self, name, text):
        self.put(os.path.join(self.dir, 'rules', name + '.md'), text)

    def run_tool(self, *args):
        env = dict(os.environ, USERPROFILE=self.home, HOME=self.home)
        return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True,
                              env=env, timeout=60)


class FilesTest(Base):

    def test_the_rules_folder_is_beside_the_log(self):
        f = files.of_config({'ledger_path': self.log, 'ledger_dir': None})
        self.assertEqual(f.rules, os.path.join(self.dir, 'rules'))

    def test_split_rules_are_in_the_ledger_dir(self):
        f = files.of_config({'ledger_path': self.log, 'ledger_dir': self.dir, 'ledger_repos': ['pm']})
        self.assertEqual(f.rules, os.path.join(self.dir, 'rules'))

    def test_the_precommit_targets_list_every_rulebook(self):
        got = precommit.targets(self.home)
        self.assertEqual(got[:2], (self.log, os.path.join(self.dir, 'STANDING.md')))
        self.assertEqual(got[2:], tuple(os.path.join(self.dir, 'rules', n + '.md')
                                        for n in ('kit', 'retribution', 'griffball', 'pm', 'all')))


class CliTest(Base):

    def setUp(self):
        super().setUp()
        self.book('kit', KIT)
        self.book('all', ALL)

    def test_repo_prints_shared_then_its_own(self):
        r = self.run_tool('rules', '--repo', 'Agentic-Project_Manager-Kit')
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], '# shared (2 rules)')
        self.assertIn('- process.refusal: A refusal is final; report it verbatim.', lines)
        self.assertNotIn('- machine.lock: Heavy runs wait in the queue.', lines)
        self.assertIn('# kit (3 rules, over its budget of 2)', lines)
        self.assertLess(lines.index('# shared (2 rules)'), lines.index('# kit (3 rules, over its budget of 2)'))
        self.assertNotIn('(from', r.stdout)

    def test_sources_and_topic(self):
        r = self.run_tool('rules', '--repo', 'Agentic-Project_Manager-Kit', '--sources', '--topic', 'landing')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('(from R-0001, R-0002)', r.stdout)
        self.assertNotIn('process.refusal', r.stdout)

    def test_all_prints_every_book(self):
        r = self.run_tool('rules', '--all')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('# kit (', r.stdout)
        self.assertIn('# shared (', r.stdout)

    def test_an_unmapped_repo_gets_the_shared_rules_and_a_note(self):
        r = self.run_tool('rules', '--repo', 'nowhere')
        self.assertEqual(r.returncode, 0)
        self.assertIn('process.refusal', r.stdout)
        self.assertIn("no rulebook for 'nowhere'", r.stderr)

    def test_arguments(self):
        for args in (['rules'], ['rules', '--repo', 'x', '--all'], ['rules', '--nope']):
            r = self.run_tool(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn('USAGE', r.stderr)

    def test_a_topic_with_no_match_and_no_rulebooks_exit_1(self):
        self.assertEqual(self.run_tool('rules', '--all', '--topic', 'zzzz').returncode, 1)
        os.remove(os.path.join(self.dir, 'rules', 'kit.md'))
        os.remove(os.path.join(self.dir, 'rules', 'all.md'))
        r = self.run_tool('rules', '--all')
        self.assertEqual(r.returncode, 1)
        self.assertIn('no rulebooks', r.stderr)

    def test_check_passes_with_notes(self):
        r = self.run_tool('check', '--base', 'HEAD')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('note: kit.md:10: rule machine.lock overrides the shared rule', r.stdout)
        self.assertIn('rulebooks: 5 rules in 2 files', r.stdout)

    def test_check_fails_on_a_bad_rulebook(self):
        self.book('pm', '# T\nRepos: p\n## A\n- a.b: no source\n- a.c: x (from R-0099)\njunk\n')
        r = self.run_tool('check', '--base', 'HEAD')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('FAIL pm.md:4: rule a.b has no source', r.stdout)
        self.assertIn('FAIL pm.md:5: rule a.c cites R-0099, which is not in the log', r.stdout)
        self.assertIn('unknown line', r.stdout)
        self.assertIn('3 problem(s)', r.stdout)


if __name__ == '__main__':
    unittest.main()
