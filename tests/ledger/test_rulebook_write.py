"""The rulebooks' write side (docs/ledger.md "Rulebooks"): kitlib.ledger.rulebook's conflicts,
set_rule, drop_rule and applied; kitlib.ledger.write.add with a RuleChange (one lock over the log
write and the rulebook write); vcs.prepare's extra files (one commit); and tools/ledger.py's
conflicts, add --rule, rule-drop and rule-set. Ids are placeholders built by rid(n)."""
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib.ledger import rulebook, vcs, write
from kitlib.ledger.errors import Refused, Usage
from tests.gitfixture import TempRepo, fixture_env
from tests.home import machine_config
from tests.ledger.test_rulebook import ALL, KIT, R1, R2, R3, TOOL, Base, log_text, rid

MODEL = 'Test Model 1'
WHEN = '2026-10-07 09:00Z'

BETA = f'''# Beta rules
Repos: beta

## Landing
- landing.queue: Heavy runs batch in the landing queue. (from {R1})
'''


def parsed(*named):
    return {n: rulebook.parse(t, n)[0] for n, t in named}


class ConflictsTest(unittest.TestCase):

    def setUp(self):
        self.books = parsed(('kit', KIT), ('beta', BETA), ('all', ALL))

    def test_kinds_in_order_each_rule_once(self):
        got = rulebook.conflicts(self.books, 'kit', 'Landing', 'machine.lock',
                                 'Heavy runs report verbatim refusal.')
        self.assertEqual(got.scope, ['kit', 'all'])
        self.assertEqual([(h.kind, h.book, h.rule.key) for h in got.hits], [
            ('same key', 'kit', 'machine.lock'),
            ('same key elsewhere', 'all', 'machine.lock'),
            ('same topic', 'kit', 'landing.batch'),
            ('same topic', 'kit', 'landing.alone'),
            ('shared words', 'all', 'process.refusal'),
        ])
        self.assertEqual(got.hits[-1].words, ('refusal', 'report', 'verbatim'))
        self.assertGreater(got.hits[-1].score, 0)

    def test_topic_slug_and_word_ranking(self):
        got = rulebook.conflicts(self.books, 'kit', 'Hardware', 'machine.gpu',
                                 'Branches lands alone, batch.')
        kinds = [(h.kind, h.rule.key) for h in got.hits]
        self.assertEqual(kinds[0], ('same topic slug', 'machine.lock'))
        self.assertEqual(kinds[1], ('same topic slug', 'machine.lock'))     # kit's, then shared's
        self.assertEqual([h.book for h in got.hits[:2]], ['kit', 'all'])
        words = [h for h in got.hits if h.kind == 'shared words']
        # `batch` is in beta.md too, so the two words landing.alone shares weigh more.
        self.assertEqual([(h.rule.key, h.words) for h in words],
                         [('landing.alone', ('alone', 'lands')), ('landing.batch', ('batch', 'branches'))])
        self.assertGreater(words[0].score, words[1].score)

    def test_scope_a_repo_book_is_read_with_the_shared_one_only(self):
        got = rulebook.conflicts(self.books, 'kit', 'Landing', 'landing.queue', 'x')
        self.assertNotIn('beta', {h.book for h in got.hits})
        self.assertNotIn(('same key elsewhere', 'beta'), {(h.kind, h.book) for h in got.hits})

    def test_scope_a_shared_add_is_read_against_every_book(self):
        got = rulebook.conflicts(self.books, 'all', 'Queue', 'landing.queue', 'x')
        self.assertEqual(got.scope, ['all', 'beta', 'kit'])
        self.assertIn(('same key elsewhere', 'beta', 'landing.queue'),
                      [(h.kind, h.book, h.rule.key) for h in got.hits])

    def test_the_digest_follows_the_books_in_scope_and_the_rule(self):
        base = rulebook.digest(self.books, 'kit', 'Landing', 'landing.new', 'Text.')
        self.assertEqual(base, rulebook.conflicts(self.books, 'kit', 'Landing', 'landing.new', 'Text.').digest)
        other = dict(self.books, beta=rulebook.parse(BETA + f'- landing.more: More. (from {R2})\n', 'beta')[0])
        self.assertEqual(rulebook.digest(other, 'kit', 'Landing', 'landing.new', 'Text.'), base)
        shared = dict(self.books, all=rulebook.parse(ALL.replace('verbatim', 'in full'), 'all')[0])
        self.assertNotEqual(rulebook.digest(shared, 'kit', 'Landing', 'landing.new', 'Text.'), base)
        self.assertNotEqual(rulebook.digest(self.books, 'kit', 'Landing', 'landing.new', 'Text 2.'), base)


class SetDropTest(unittest.TestCase):

    def setUp(self):
        self.kit = rulebook.parse(KIT, 'kit')[0]

    def test_replace_keeps_one_key_in_its_place(self):
        book, old = rulebook.set_rule(self.kit, 'landing.batch', 'Landing', 'Batch two.', (R3,))
        self.assertEqual(old.key, 'landing.batch')
        self.assertEqual([r.key for r in book.rules], ['landing.batch', 'landing.alone', 'machine.lock'])
        self.assertEqual(book.rules[0].text, 'Batch two.')
        self.assertEqual(book.rules[0].sources, (R3,))
        again, errs = rulebook.parse(rulebook.render(book), 'kit')
        self.assertEqual(errs, [])
        self.assertEqual([r.key for r in again.rules].count('landing.batch'), 1)

    def test_a_new_topic_moves_the_rule_and_a_new_rule_joins_its_topic(self):
        book, _ = rulebook.set_rule(self.kit, 'landing.batch', 'Machine', 'Moved.', (R3,))
        self.assertEqual([(r.key, r.topic) for r in book.rules],
                         [('landing.alone', 'Landing'), ('machine.lock', 'Machine'), ('landing.batch', 'Machine')])
        book, old = rulebook.set_rule(self.kit, 'landing.fast', 'Landing', 'New.', (R3,))
        self.assertIsNone(old)
        self.assertEqual([r.key for r in book.rules][:3], ['landing.batch', 'landing.alone', 'landing.fast'])
        book, _ = rulebook.set_rule(self.kit, 'docs.cite', 'Docs', 'New heading.', (R3,))
        self.assertEqual(rulebook.render(book).splitlines()[-2:], ['## Docs', '- docs.cite: New heading. (from %s)' % R3])

    def test_bad_input_and_drop(self):
        for key, topic, text in (('Bad', 'T', 'x'), ('a.b', ' ', 'x'), ('a.b', 'T', '  ')):
            with self.assertRaises(ValueError):
                rulebook.set_rule(self.kit, key, topic, text, (R3,))
        book, old = rulebook.drop_rule(self.kit, 'landing.alone')
        self.assertEqual(old.key, 'landing.alone')
        self.assertNotIn('landing.alone', [r.key for r in book.rules])
        with self.assertRaises(KeyError):
            rulebook.drop_rule(book, 'landing.alone')


class WriteBase(Base):

    def setUp(self):
        super().setUp()
        self.rules = os.path.join(self.dir, 'rules')
        self.book('kit', KIT.replace('Alpha_Checkout', 'kit'))
        self.book('all', ALL)

    def read(self, path):
        with open(path, encoding='utf-8') as f:
            return f.read()

    def kit_path(self):
        return os.path.join(self.rules, 'kit.md')

    def checked(self, key, topic, text, repo='kit'):
        books = write.load_books(self.rules)
        return rulebook.digest(books, rulebook.target(books, repo), topic, key, text)

    def set(self, key='landing.fast', topic='Landing', text='Small branches land fast.', checked=None,
            repo='kit', decides='Small branches land fast.'):
        if checked is None:
            checked = self.checked(key, topic, text, repo)
        change = write.RuleChange(self.rules, 'set', key, topic, text, checked)
        return write.add(self.log, repo, 'selected', 'q', 'a', decides, 's', when=WHEN, rule=change)


class WriteTest(WriteBase):

    def test_add_pairs_the_log_entry_and_the_rule(self):
        got = self.set()
        self.assertEqual(got.id, rid(4))
        entry = write.show(self.read(self.log), rid(4))
        self.assertIn('Decides: Small branches land fast. [rule kit.md landing.fast set]', entry)
        kit = rulebook.parse(self.read(self.kit_path()), 'kit')[0]
        rule = next(r for r in kit.rules if r.key == 'landing.fast')
        self.assertEqual((rule.topic, rule.text, rule.sources), ('Landing', 'Small branches land fast.', (rid(4),)))
        self.assertEqual(rulebook.applied({'kit': kit}, [self.read(self.log)]), [])
        self.assertIn('kit.md has 4 rules, over its budget of 2', ' '.join(got.warnings))

    def test_missing_or_stale_checked_is_refused_and_nothing_written(self):
        log, kit = self.read(self.log), self.read(self.kit_path())
        with self.assertRaises(Usage) as e:
            self.set(checked='')
        self.assertIn('--rule needs --checked', str(e.exception))
        stale = self.checked('landing.fast', 'Landing', 'Small branches land fast.')
        self.book('all', ALL + f'- process.new: Changed since. (from {R3})\n')
        with self.assertRaises(Refused) as e:
            self.set(checked=stale)
        self.assertIn('is stale', str(e.exception))
        self.assertIn('conflicts', str(e.exception))
        with self.assertRaises(Refused):
            self.set(checked=self.checked('landing.fast', 'Landing', 'Other text.'))
        self.assertEqual(self.read(self.log), log)
        self.assertEqual(self.read(self.kit_path()), kit)

    def test_replace_keeps_one_key(self):
        self.set()
        self.set(text='Small branches land first.', decides='Small branches land first.')
        kit = rulebook.parse(self.read(self.kit_path()), 'kit')[0]
        same = [r for r in kit.rules if r.key == 'landing.fast']
        self.assertEqual(len(same), 1)
        self.assertEqual((same[0].text, same[0].sources), ('Small branches land first.', (rid(5),)))

    def test_drop_writes_a_log_entry(self):
        got = write.add(self.log, 'kit', 'selected', 'Drop it?', 'Yes.', 'dropped landing.alone: obsolete',
                        's', when=WHEN, rule=write.RuleChange(self.rules, 'dropped', 'landing.alone'))
        self.assertIn('Decides: dropped landing.alone: obsolete [rule kit.md landing.alone dropped]',
                      write.show(self.read(self.log), got.id))
        self.assertNotIn('landing.alone', self.read(self.kit_path()))
        log = self.read(self.log)
        with self.assertRaises(Refused):
            write.add(self.log, 'kit', 'selected', 'q', 'a', 'd', 's', when=WHEN,
                      rule=write.RuleChange(self.rules, 'dropped', 'landing.alone'))
        self.assertEqual(self.read(self.log), log)

    def test_header_makes_a_new_book(self):
        write.add(self.log, 'beta', 'selected', 'q', 'a', 'rulebook beta.md header', 's', when=WHEN,
                  rule=write.RuleChange(self.rules, 'header', repos=['Beta_Checkout'], budget=10))
        beta, errs = rulebook.parse(self.read(os.path.join(self.rules, 'beta.md')), 'beta')
        self.assertEqual(errs, [])
        self.assertEqual((beta.repos, beta.budget, beta.rules), (('Beta_Checkout',), 10, ()))
        with self.assertRaises(Usage):
            write.add(self.log, 'gamma', 'selected', 'q', 'a', 'd', 's', when=WHEN,
                      rule=write.RuleChange(self.rules, 'header', budget=10))

    def test_the_lock_is_held_across_both_writes(self):
        real, seen = write.write_atomic, []

        def spy(path, text):
            seen.append((os.path.basename(path), write.held(self.log), write.held(self.kit_path())))
            real(path, text)
        with mock.patch.object(write, 'write_atomic', spy):
            self.set()
        # Both locks at both writes: the log's, and the book's (two log files can route to one book).
        self.assertEqual(seen, [('LEDGER.md', True, True), ('kit.md', True, True)])
        self.assertFalse(write.held(self.kit_path()))

    def test_a_crash_between_the_two_writes_is_reported_by_check(self):
        real, calls = write.write_atomic, []

        def crash(path, text):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('simulated crash')
            real(path, text)
        with mock.patch.object(write, 'write_atomic', crash):
            with self.assertRaises(OSError):
                self.set()
        self.assertIn(rid(4), self.read(self.log))
        self.assertNotIn('landing.fast', self.read(self.kit_path()))
        r = self.run_tool('check')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('FAIL logged rule change not applied: %s sets kit.md landing.fast' % rid(4), r.stdout)

    def test_a_crash_between_the_writes_of_a_header_change_is_reported_by_check(self):
        real, calls = write.write_atomic, []

        def crash(path, text):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('simulated crash')
            real(path, text)
        with mock.patch.object(write, 'write_atomic', crash):
            with self.assertRaises(OSError):
                write.add(self.log, 'kit', 'selected', 'q', 'a', 'rulebook kit.md header: Budget 12', 's',
                          when=WHEN, rule=write.RuleChange(self.rules, 'header', budget=12))
        self.assertNotIn('Budget: 12', self.read(self.kit_path()))
        r = self.run_tool('check')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('FAIL logged rule change not applied: %s sets the header of kit.md, which is not '
                      'the header there' % rid(4), r.stdout)

    def retire(self, n):
        write.retire(self.log, rid(n), 'spent', 'undone')

    def assert_check_ok(self):
        books = write.load_books(self.rules)
        self.assertEqual(rulebook.applied(books, [self.read(self.log)]), [])
        r = self.run_tool('check')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_set_then_set_then_retire_the_second_is_not_judged(self):
        self.set()
        self.set(text='Small branches land first.', decides='Small branches land first.')
        self.retire(5)      # the rule now cites only the retired entry
        self.assert_check_ok()

    def test_set_then_drop_then_retire_the_drop_is_not_judged(self):
        self.set()
        write.add(self.log, 'kit', 'selected', 'q', 'a', 'dropped landing.fast: no', 's', when=WHEN,
                  rule=write.RuleChange(self.rules, 'dropped', 'landing.fast'))
        self.retire(5)
        self.assert_check_ok()

    def crash_second_write(self):
        real, calls = write.write_atomic, []

        def crash(path, text):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('simulated crash')
            real(path, text)
        return mock.patch.object(write, 'write_atomic', crash)

    def test_a_rule_citing_an_older_retired_entry_hides_no_later_crash(self):
        self.set()                      # rid(4) sets the rule; the rule cites it
        self.retire(4)                  # the rule still cites the retired rid(4)
        with self.crash_second_write(), self.assertRaises(OSError):
            self.set(text='Small branches land first.', decides='Small branches land first.')
        errs = rulebook.applied(write.load_books(self.rules), [self.read(self.log)])
        self.assertEqual(len(errs), 1, errs)
        self.assertIn('%s sets kit.md landing.fast' % rid(5), errs[0])
        with self.crash_second_write(), self.assertRaises(OSError):
            write.add(self.log, 'kit', 'selected', 'q', 'a', 'dropped landing.fast: no', 's', when=WHEN,
                      rule=write.RuleChange(self.rules, 'dropped', 'landing.fast'))
        errs = rulebook.applied(write.load_books(self.rules), [self.read(self.log)])
        self.assertEqual(len(errs), 1, errs)
        self.assertIn('%s drops kit.md landing.fast, which is still there' % rid(6), errs[0])

    def test_a_book_rerouted_while_waiting_for_its_lock_is_refused(self):
        real = write.rule_target

        def reroute(folder, repo):
            got = real(folder, repo)        # kit.md, the book whose lock add takes
            self.book('kit', KIT.replace('Alpha_Checkout', 'elsewhere'))
            self.book('beta', '# Beta rules\nRepos: kit\n')
            return got
        log = self.read(self.log)
        with mock.patch.object(write, 'rule_target', reroute), self.assertRaises(Refused) as e:
            write.add(self.log, 'kit', 'selected', 'q', 'a', 'rulebook header: Budget 10', 's', when=WHEN,
                      rule=write.RuleChange(self.rules, 'header', budget=10))
        self.assertIn('changed from kit.md to beta.md while waiting for its lock', str(e.exception))
        self.assertEqual(self.read(self.log), log)

    def test_an_older_retired_entry_of_the_book_hides_no_later_crash(self):
        write.add(self.log, 'kit', 'selected', 'q', 'a', 'Plain.', 's', when=WHEN)
        self.retire(4)
        real, calls = write.write_atomic, []

        def crash(path, text):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('simulated crash')
            real(path, text)
        with mock.patch.object(write, 'write_atomic', crash), mock.patch.object(write, 'now_utc',
                                                                                 return_value='2026-10-08 09:00Z'):
            with self.assertRaises(OSError):
                change = write.RuleChange(self.rules, 'set', 'landing.fast', 'Landing', 'Fast.',
                                          self.checked('landing.fast', 'Landing', 'Fast.'))
                write.add(self.log, 'kit', 'selected', 'q', 'a', 'Fast.', 's', rule=change)
        errs = rulebook.applied(write.load_books(self.rules), [self.read(self.log)])
        self.assertEqual(len(errs), 1, errs)
        self.assertIn('%s sets kit.md landing.fast' % rid(5), errs[0])


class AppliedTest(unittest.TestCase):

    def test_the_last_change_per_rule_must_be_in_force(self):
        from kitlib.ledger import fmt
        log = log_text(0) + fmt.render(rid(1), '2026-10-07 09:00Z', 'kit', 'selected', 'q', 'a',
                                       'Use it. [rule kit.md landing.fast set]', 's', '-') \
            + fmt.render(rid(2), '2026-10-07 10:00Z', 'kit', 'selected', 'q', 'a',
                         'dropped landing.fast: no [rule kit.md landing.fast dropped]', 's', '-')
        kit = rulebook.parse('# K\nRepos: kit\n', 'kit')[0]
        self.assertEqual(rulebook.applied({'kit': kit}, [log]), [])
        set_back, _ = rulebook.set_rule(kit, 'landing.fast', 'Landing', 'x', (rid(1),))
        errs = rulebook.applied({'kit': set_back}, [log])
        self.assertEqual(len(errs), 1)
        self.assertIn('%s drops kit.md landing.fast, which is still there' % rid(2), errs[0])
        first = log.split('## %s' % rid(2))[0]
        self.assertIn('there is no kit.md', rulebook.applied({}, [first])[0])
        self.assertIn('it has no such rule', rulebook.applied({'kit': kit}, [first])[0])


class CliTest(WriteBase):

    def conflicts(self, *more):
        r = self.run_tool('conflicts', '--repo', 'kit', '--topic', 'Landing', '--key', 'landing.fast',
                          '--text', 'Small branches land fast.', *more)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_conflicts_then_add_rule(self):
        r = self.conflicts()
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], 'conflicts for kit.md landing.fast, read against kit.md, all.md:')
        self.assertIn('same topic: kit.md landing.batch: Landers batch up to three branches. (from %s, %s)'
                      % (R1, R2), lines)
        self.assertTrue(any(l.startswith('guidelines: python ') and 'doc_index.py' in l for l in lines))
        checked = lines[-1].split('checked: ', 1)[1]
        add = self.run_tool('add', '--repo', 'kit', '--prov', 'selected', '--q', 'q', '--a', 'a',
                            '--decides', 'Small branches land fast.', '--source', 's',
                            '--rule', 'landing.fast', '--topic', 'Landing', '--checked', checked)
        self.assertEqual(add.returncode, 0, add.stderr)
        self.assertEqual(add.stdout.split(), [rid(4)])
        self.assertIn('over its budget', add.stderr)
        self.assertIn('- landing.fast: Small branches land fast. (from %s)' % rid(4), self.read(self.kit_path()))
        self.assertEqual(self.run_tool('check').returncode, 0)

    def test_add_rule_arguments(self):
        base = ['add', '--repo', 'kit', '--prov', 'selected', '--q', 'q', '--a', 'a', '--decides', 'd',
                '--source', 's']
        for more in (['--topic', 'T'], ['--checked', 'abc'], ['--rule', 'landing.fast', '--checked', 'abc']):
            r = self.run_tool(*(base + more))
            self.assertEqual(r.returncode, 2, more)
            self.assertIn('USAGE', r.stderr)
        r = self.run_tool(*(base + ['--rule', 'landing.fast', '--topic', 'Landing']))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('--rule needs --checked', r.stderr)
        self.assertIn('ledger.py conflicts', r.stderr)
        r = self.run_tool(*(base + ['--rule', 'landing.fast', '--topic', 'Landing', '--checked', '0' * 16]))
        self.assertEqual(r.returncode, 1)
        self.assertIn('is stale', r.stderr)
        self.assertEqual(self.read(self.log), log_text())

    def test_rule_drop_and_rule_set(self):
        r = self.run_tool('rule-drop', 'landing.alone', '--repo', 'kit', '--prov', 'selected',
                          '--why', 'it is obsolete', '--source', 's')
        self.assertEqual(r.returncode, 0, r.stderr)
        entry = write.show(self.read(self.log), rid(4))
        self.assertIn('Q: Drop rule landing.alone from kit.md?', entry)
        self.assertIn('Decides: dropped landing.alone: it is obsolete [rule kit.md landing.alone dropped]', entry)
        self.assertNotIn('landing.alone', self.read(self.kit_path()))
        r = self.run_tool('rule-set', '--repo', 'kit', '--budget', '12', '--prov', 'selected',
                          '--why', 'room', '--source', 's')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Budget: 12', self.read(self.kit_path()))
        kit = rulebook.parse(self.read(self.kit_path()), 'kit')[0]
        self.assertIn('[rulebook kit.md header %s]' % rulebook.header_digest(kit),
                      write.show(self.read(self.log), rid(5)))
        self.assertEqual(self.run_tool('rule-set', '--repo', 'kit', '--prov', 'selected', '--why', 'x',
                                       '--source', 's').returncode, 2)
        self.assertEqual(self.run_tool('check').returncode, 0)


class OneCommitTest(unittest.TestCase):

    def setUp(self):
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.env = fixture_env()
        self.path = self.repo.path('rulings', 'LEDGER.md')
        self.rules = self.repo.path('rulings', 'rules')
        self.repo.write('rulings/rules/kit.md', KIT.replace('Alpha_Checkout', 'kit'))
        self.repo.write('rulings/LEDGER.md', log_text())
        self.repo.commit('seed', 'rulings/rules/kit.md', 'rulings/LEDGER.md')

    def test_the_log_and_the_rulebook_land_in_one_commit(self):
        _, book = write.rule_target(self.rules, 'kit')
        plan = vcs.prepare(self.path, model=MODEL, env=self.env, extra=[book])
        books = write.load_books(self.rules)
        checked = rulebook.digest(books, 'kit', 'Landing', 'landing.fast', 'Fast.')
        got = write.add(self.path, 'kit', 'selected', 'q', 'a', 'Fast.', 's', when=WHEN,
                        rule=write.RuleChange(self.rules, 'set', 'landing.fast', 'Landing', None, checked))
        vcs.commit_entry(plan, got.id, None, 'Fast.')
        names = self.repo.git('show', '--name-only', '--format=', 'HEAD').split()
        self.assertEqual(sorted(names), ['rulings/LEDGER.md', 'rulings/rules/kit.md'])
        self.assertEqual(self.repo.git('status', '--porcelain'), '')

    def test_an_extra_file_with_uncommitted_changes_is_refused(self):
        self.repo.write('rulings/rules/kit.md', KIT + '\n')
        with self.assertRaises(Refused) as e:
            vcs.prepare(self.path, model=MODEL, env=self.env, extra=[self.repo.path('rulings', 'rules', 'kit.md')])
        self.assertIn('rulings/rules/kit.md has uncommitted changes', str(e.exception))


class CliCommitTest(unittest.TestCase):
    """`ledger.py add --rule ... --commit`: one commit of exactly the log and the rulebook."""

    def setUp(self):
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.repo.write('rulings/rules/kit.md', KIT.replace('Alpha_Checkout', 'kit'))
        self.repo.write('rulings/LEDGER.md', log_text())
        self.repo.commit('seed', 'rulings/rules/kit.md', 'rulings/LEDGER.md')
        self.home = os.path.join(self.repo.base, 'home')
        os.makedirs(self.home)
        machine_config(self.home, {'ledger_path': self.repo.path('rulings', 'LEDGER.md')}, case=self)
        self.env = dict(fixture_env(), USERPROFILE=self.home, HOME=self.home)

    def run_tool(self, *args):
        return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True,
                              env=self.env, timeout=120, cwd=self.repo.root)

    def test_add_rule_commit_commits_the_log_and_the_rulebook_alone(self):
        rule = ['--repo', 'kit', '--topic', 'Landing', '--key', 'landing.fast', '--text', 'Fast.']
        r = self.run_tool('conflicts', *rule)
        self.assertEqual(r.returncode, 0, r.stderr)
        checked = r.stdout.splitlines()[-1].split('checked: ', 1)[1]
        before = int(self.repo.git('rev-list', '--count', 'HEAD'))
        r = self.run_tool('add', '--repo', 'kit', '--prov', 'selected', '--q', 'q', '--a', 'a',
                          '--decides', 'Fast.', '--source', 's', '--rule', 'landing.fast', '--topic', 'Landing',
                          '--checked', checked, '--commit', '--model', MODEL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], rid(4))
        names = self.repo.git('show', '--name-only', '--format=', 'HEAD').split()
        self.assertEqual(sorted(names), ['rulings/LEDGER.md', 'rulings/rules/kit.md'])
        self.assertEqual(int(self.repo.git('rev-list', '--count', 'HEAD')), before + 1)
        self.assertEqual(self.repo.git('status', '--porcelain'), '')
        self.assertIn('- landing.fast: Fast. (from %s)' % rid(4),
                      self.repo.git('show', 'HEAD:rulings/rules/kit.md'))


if __name__ == '__main__':
    unittest.main()
