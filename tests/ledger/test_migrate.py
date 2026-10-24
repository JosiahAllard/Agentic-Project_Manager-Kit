"""The digest-to-rulebook migration (docs/ledger.md "Rulebooks"): kitlib.ledger.migrate's classify,
merge grouping, report text and plan, and tools/ledger.py's migrate-report and rule-apply. Every
line and id is made up; ids are placeholders built by rid(n)."""
import os
import subprocess
import sys
import tempfile
import unittest

from kitlib.ledger import migrate, rulebook
from tests.home import machine_config
from tests.ledger.test_cli_split import frozen_text
from tests.ledger.test_rulebook import Base, R1, R2, R3, TOOL, rid

DIGEST = f'''# Standing

## Kit
### Landing
- {R1}: Agents must report a refusal verbatim to the coordinator.
- {R2}: A refusal must be reported verbatim to the coordinator by agents.
- {R3}: Never push to the shared remote until 2031-01-02.

## Shared
### Process
- {R1}: Workers never edit files outside their own worktree.
- {R2}: The sandbox folder lives in the scratch drive.
'''


def item(text, ids=(R1,), retired=frozenset()):
    return migrate.classify(text, ids, retired)


class ClassifyTest(unittest.TestCase):

    def cut(self, text, reason, **kw):
        self.assertEqual(item(text, **kw), ('CUT', reason), text)

    def test_each_cut_signal(self):
        self.cut('Agents never merge on 2031-04-05.', 'dated')
        self.cut('Agents never merge on Jan 5.', 'dated')
        self.cut('Agents never merge until the freeze ends.', 'one-off')
        self.cut('Agents must pause for now.', 'one-off')
        self.cut('Agents never touch MADE-UP-RECORD in the queue.', 'names a record')
        self.cut('Agents never skip the p1 items.', 'names a priority')
        self.cut('Agents never skip step 3 of the plan.', 'spec detail')
        self.cut('Agents never rely on the interactive prompt.', 'stale term')
        self.cut('Agents never omit the co-author trailer.', 'the kit enforces it')
        self.cut('The scratch folder lives in the temp drive.', 'a fact or history, not a behaviour')
        self.cut('Agents never lose it.', 'cites only retired entries', retired=frozenset({R1}))

    def test_a_retired_citation_cuts_only_when_every_cited_id_is_retired(self):
        self.assertEqual(item('Agents never lose it.', (R1, R2), frozenset({R1}))[0], 'KEEP')

    def test_the_keep_signal(self):
        self.assertEqual(item('Agents never edit outside their worktree.')[0], 'KEEP')
        self.assertEqual(item('Landers batch branches, but a risky one lands alone only.')[0], 'KEEP')

    def test_no_behaviour_word_is_a_cut(self):
        self.cut('The queue is long on Fridays.', 'no behaviour word')

    def test_a_cut_signal_beats_a_behaviour_word(self):
        self.assertEqual(item('Agents must stop until the freeze ends.')[0], 'CUT')

    def test_a_ruling_or_question_id_is_not_a_record(self):
        for text in ('Agents never reopen R-' + '0012.', 'Agents never reopen Q-12.',
                     'Agents never reopen H-kit-' + '0007.', 'Agents never reopen H-kit-' + '0007 or UTF-8 files.',
                     'Agents never reopen X-AB-12.'):
            self.assertEqual(item(text)[0], 'KEEP', text)


class ProposeTest(unittest.TestCase):

    def setUp(self):
        self.items = migrate.propose(DIGEST)

    def test_repos_and_topics_come_from_the_headings(self):
        self.assertEqual([(i.repo, i.topic) for i in self.items],
                         [('kit', 'Landing')] * 3 + [('all', 'Process')] * 2)

    def test_near_duplicates_group_with_the_shortest_first(self):
        merged = [i for i in self.items if i.tag == 'MERGE']
        self.assertEqual(len(merged), 2)
        self.assertEqual(len({i.group for i in merged}), 1)
        self.assertEqual(merged[0].text, 'Agents must report a refusal verbatim to the coordinator.')
        self.assertEqual(self.items[0], merged[0])

    def test_the_rest_are_tagged_one_by_one(self):
        self.assertEqual([i.tag for i in self.items], ['MERGE', 'MERGE', 'CUT', 'KEEP', 'CUT'])
        self.assertTrue(all(not i.reviewed for i in self.items))

    def test_the_same_words_in_another_topic_do_not_group(self):
        got = migrate.propose(DIGEST.replace('Workers never edit files outside their own worktree.',
                                             'Agents must report a refusal verbatim to the coordinator.'))
        self.assertEqual([i.repo for i in got if i.tag == 'MERGE'], ['kit', 'kit'])
        self.assertEqual(got[3].repo, 'all')
        self.assertNotEqual(got[3].tag, 'MERGE')

    def test_below_the_threshold_nothing_groups(self):
        got = migrate.propose('## Kit\n- %s: Agents never merge unreviewed branches.\n'
                              '- %s: Agents must report a refusal to the coordinator.\n' % (R1, R2))
        self.assertEqual([i.tag for i in got], ['KEEP', 'KEEP'])

    def test_keys_are_valid_and_unique_in_a_repo(self):
        text = '## Kit\n' + ''.join('- %s: Agents never merge %s branches.\n' % (R1, w)
                                     for w in ('red', 'red'))
        keys = [i.key for i in migrate.propose(text)]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertTrue(all(rulebook.KEY_RE.match(k) for k in keys), keys)


class ReportTest(unittest.TestCase):

    def test_render_then_parse_gives_the_items_back(self):
        items = migrate.propose(DIGEST)
        text = migrate.render(items, 'abc123', 'STANDING.md', [(R3, 'A later rule.')])
        found, back, errs = migrate.parse(text)
        self.assertEqual((found, errs), ('abc123', []))
        self.assertEqual([i._replace(line=0) for i in back], items)
        self.assertIn('# uncited: %s | A later rule.' % R3, text)

    def test_a_bar_in_the_text_survives_as_a_slash(self):
        items = [migrate.Item('KEEP', '', False, 'kit', 'a.b', 'T', 'Use a | b or c # d.', (R1,), 'r', 0)]
        _, back, errs = migrate.parse(migrate.render(items, 'x', 's'))
        self.assertEqual((errs, back[0].text), ([], 'Use a / b or c / d.'))

    def test_parse_reports_bad_lines_and_tags(self):
        _, _, errs = migrate.parse('Basis: x\nnonsense\nFOO | kit | a.b | T | t | from: ' + R1 + '\n'
                                   'MERGE | kit | a.b | T | t | from: ' + R1 + '\n')
        self.assertEqual(len(errs), 3, errs)


def reviewed(text):
    return text.replace('\n? ', '\n')


class PlanTest(unittest.TestCase):

    def setUp(self):
        self.basis = migrate.basis(DIGEST, set())
        self.text = migrate.render(migrate.propose(DIGEST), self.basis, 'STANDING.md')

    def test_an_unedited_report_is_refused(self):
        changes, errs = migrate.plan(self.text, self.basis)
        self.assertIn('5 line(s) still marked `?`', ' '.join(errs))

    def test_only_keep_and_merge_lines_are_planned(self):
        changes, errs = migrate.plan(reviewed(self.text), self.basis)
        self.assertEqual(errs, [])
        self.assertEqual([(c.repo, c.text) for c in changes],
                         [('kit', 'Agents must report a refusal verbatim to the coordinator.'),
                          ('all', 'Workers never edit files outside their own worktree.')])

    def test_a_merge_group_is_one_change_citing_every_member(self):
        changes, _ = migrate.plan(reviewed(self.text), self.basis)
        self.assertEqual(changes[0].ids, (R1, R2))

    def test_a_cut_line_is_never_carried_and_an_edit_is(self):
        text = reviewed(self.text).replace('CUT | kit', 'KEEP | kit', 1)
        changes, errs = migrate.plan(text, self.basis)
        self.assertEqual(errs, [])
        self.assertEqual(len(changes), 3)

    def test_a_stale_basis_is_refused(self):
        _, errs = migrate.plan(reviewed(self.text), migrate.basis(DIGEST + '\n- x', set()))
        self.assertTrue(any('stale' in e for e in errs), errs)
        _, errs = migrate.plan('# no basis\n', self.basis)
        self.assertTrue(any('no Basis' in e for e in errs), errs)

    def test_bad_and_doubled_keys_are_refused(self):
        text = reviewed(self.text)
        _, errs = migrate.plan(text.replace('| all | process.', '| all | Bad Key.', 1), self.basis)
        self.assertTrue(any('is not <topic-slug>.<slug>' in e for e in errs), errs)
        keys = [i.key for i in migrate.parse(text)[1] if i.repo == 'all']
        _, errs = migrate.plan(text.replace(keys[1], keys[0]).replace(
            'CUT | all', 'KEEP | all'), self.basis)
        self.assertTrue(any('is already on line' in e for e in errs), errs)

    def test_a_merge_group_across_repos_is_refused(self):
        items = [migrate.Item('MERGE', 'g1', True, 'kit', 'a.one', 'T', 'Agents never guess.', (R1,), 'r', 0),
                 migrate.Item('MERGE', 'g1', True, 'pm', 'a.two', 'T', 'Agents never guess at all.', (R2,), 'r', 0)]
        _, errs = migrate.plan(migrate.render(items, self.basis, 's'), self.basis)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn('MERGE g1 joins repo pm', errs[0])

    def test_a_rule_text_citing_a_ruling_or_question_id_is_refused(self):
        for cite in ('R-' + '9999', 'H-kit-' + '0003', 'Q-' + '12'):
            items = [migrate.Item('KEEP', '', True, 'kit', 'a.one', 'T', 'Agents never reopen %s.' % cite,
                                  (R1,), 'r', 0)]
            _, errs = migrate.plan(migrate.render(items, self.basis, 's'), self.basis)
            self.assertEqual(len(errs), 1, (cite, errs))
            self.assertIn('cites a ruling or question id', errs[0])

    def test_shared_lines_are_refused_in_the_split_layout_only(self):
        _, errs = migrate.plan(reviewed(self.text), self.basis, split=True)
        self.assertEqual(len(errs), 1)
        self.assertIn('shared rule', errs[0])
        self.assertIn('no writer path', errs[0])


class CliTest(Base):

    def setUp(self):
        super().setUp()
        self.digest = os.path.join(self.dir, 'STANDING.md')
        self.put(self.digest, DIGEST)
        self.report = os.path.join(self.dir, 'scratch-report.md')

    def read(self, path):
        with open(path, encoding='utf-8') as f:
            return f.read()

    def make_report(self, edit=True):
        r = self.run_tool('migrate-report', '--out', self.report)
        self.assertEqual(r.returncode, 0, r.stderr)
        if edit:
            self.put(self.report, reviewed(self.read(self.report)))

    def apply(self, *more):
        return self.run_tool('rule-apply', self.report, *more)

    def write_flags(self):
        return ['--write', '--prov', 'selected', '--source', 'test']

    def test_migrate_report_is_read_only_and_prints_the_report(self):
        log, digest = self.read(self.log), self.read(self.digest)
        r = self.run_tool('migrate-report')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Basis: ', r.stdout)
        self.assertIn('? MERGE g1 | kit | ', r.stdout)
        self.assertEqual((self.read(self.log), self.read(self.digest)), (log, digest))
        self.assertEqual(os.listdir(os.path.join(self.dir, 'rules')), [])

    def out_from(self, cwd, *args):
        env = dict(os.environ, USERPROFILE=self.home, HOME=self.home)
        return subprocess.run([sys.executable, TOOL, 'migrate-report'] + list(args), capture_output=True,
                              text=True, env=env, timeout=60, cwd=cwd)

    def test_out_refuses_a_ledger_file_in_every_spelling(self):
        log = self.read(self.log)
        rules = os.path.join(self.dir, 'rules')
        for cwd, out in ((self.dir, 'LEDGER.md'), (self.dir, self.log.upper()),
                         (self.dir, os.path.join(rules, '..', 'LEDGER.md')),
                         (rules, os.path.join('..', 'LEDGER.md'))):
            r = self.out_from(cwd, '--out', out)
            self.assertEqual(r.returncode, 2, (out, r.stderr))
        self.assertEqual(self.read(self.log), log)

    def test_out_refuses_the_digest_and_the_digest_actually_read(self):
        before = self.read(self.digest)
        self.assertEqual(self.out_from(self.dir, '--out', self.digest).returncode, 2)
        other = os.path.join(self.dir, 'elsewhere.md')
        self.put(other, DIGEST)
        r = self.out_from(self.dir, '--digest', other, '--out', other)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual((self.read(self.digest), self.read(other)), (before, DIGEST))

    def test_out_refuses_the_rules_folder_and_anything_under_it(self):
        rules = os.path.join(self.dir, 'rules')
        os.makedirs(os.path.join(rules, 'sub'))
        for out in (rules, os.path.join(rules, 'new.md'), os.path.join(rules, 'sub', 'x.md'),
                    os.path.join(rules, '..', 'rules', 'y.md')):
            r = self.out_from(self.dir, '--out', out)
            self.assertEqual(r.returncode, 2, (out, r.stderr))
            self.assertNotIn('Traceback', r.stderr)
        self.assertEqual(os.listdir(os.path.join(rules, 'sub')), [])

    def test_an_unwritable_out_is_a_clean_failure(self):
        r = self.out_from(self.dir, '--out', os.path.join(self.dir, 'no-such-folder', 'r.md'))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('cannot write', r.stderr)
        self.assertNotIn('Traceback', r.stderr)

    def test_a_rule_citing_an_id_is_refused_before_any_write(self):
        self.make_report()
        self.put(self.report, self.read(self.report).replace(
            'Workers never edit files outside their own worktree.',
            'Workers never edit files outside their own worktree (R-' + '9999).'))
        log = self.read(self.log)
        r = self.apply(*self.write_flags())
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('cites a ruling or question id', r.stderr)
        self.assertEqual(self.read(self.log), log)

    def test_an_unedited_report_is_refused_and_nothing_is_written(self):
        self.make_report(edit=False)
        log = self.read(self.log)
        r = self.apply(*self.write_flags())
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('still marked `?`', r.stderr)
        self.assertEqual(self.read(self.log), log)

    def test_the_dry_run_writes_nothing(self):
        self.make_report()
        log = self.read(self.log)
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.count('would set '), 2)
        self.assertIn('dry run: 2 rule(s)', r.stdout)
        self.assertEqual(self.read(self.log), log)
        self.assertEqual(os.listdir(os.path.join(self.dir, 'rules')), [])

    def test_write_needs_prov_and_source(self):
        self.make_report()
        self.assertEqual(self.apply('--write').returncode, 2)

    def test_write_sets_keep_and_merge_lines_with_one_entry_per_rule(self):
        self.make_report()
        r = self.apply(*self.write_flags())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), [rid(4), rid(5)])
        log = self.read(self.log)
        self.assertNotIn(rid(6), log)
        kit = rulebook.parse(self.read(os.path.join(self.dir, 'rules', 'kit.md')), 'kit')[0]
        shared = rulebook.parse(self.read(os.path.join(self.dir, 'rules', 'all.md')), 'all')[0]
        self.assertEqual([(r.sources, r.text) for r in kit.rules],
                         [((rid(4),), 'Agents must report a refusal verbatim to the coordinator.')])
        self.assertEqual([r.sources for r in shared.rules], [(rid(5),)])
        entry = log[log.index('## %s' % rid(4)):log.index('## %s' % rid(5))]
        self.assertIn('Q: Carry the standing rule from STANDING.md (%s, %s)' % (R1, R2), entry)
        decides = [l for l in entry.splitlines() if l.startswith('Decides:')][0]
        self.assertNotIn(R1, decides)
        self.assertNotIn(R2, decides)
        check = self.run_tool('check')
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_a_rerun_skips_the_rules_already_set(self):
        self.make_report()
        self.apply(*self.write_flags())
        log = self.read(self.log)
        r = self.apply(*self.write_flags())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.count('skip '), 2)
        self.assertEqual(self.read(self.log), log)

    def test_a_key_set_with_other_text_is_refused_and_nothing_is_written(self):
        self.make_report()
        self.apply(*self.write_flags())
        self.put(self.report, self.read(self.report).replace(
            'Workers never edit files outside their own worktree.', 'Workers never edit anywhere.'))
        log = self.read(self.log)
        r = self.apply(*self.write_flags())
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('with other text', r.stderr)
        self.assertEqual(self.read(self.log), log)

    def test_a_changed_digest_makes_the_report_stale(self):
        self.make_report()
        self.put(self.digest, DIGEST + '- %s: Agents never guess.\n' % R1)
        log = self.read(self.log)
        r = self.apply(*self.write_flags())
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('stale', r.stderr)
        self.assertEqual(self.read(self.log), log)


SPLIT_DIGEST = f'''# Standing

## Alpha
### Landing
- {R1}: Agents never merge an unreviewed branch.

## Pm
### Landing
- {R2}: Agents never merge an unreviewed plan.

## Zeta
### Landing
- {R1}: Agents never merge an unreviewed zeta.
'''


class SplitTest(unittest.TestCase):
    """The split layout (ledger_dir set): the repo checks of do_rule_apply, through the verb."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.dir = os.path.join(self.home, 'rulings')
        os.makedirs(os.path.join(self.dir, 'rules'))
        self.frozen = os.path.join(self.dir, 'LEDGER.md')
        self.put(self.frozen, frozen_text())
        self.put(os.path.join(self.dir, 'STANDING.md'), SPLIT_DIGEST)
        machine_config(self.home, {'ledger_dir': self.dir, 'ledger_repos': ['alpha', 'pm'],
                                   'ledger_path': self.frozen}, case=self)
        self.report = os.path.join(self.home, 'report.md')

    def put(self, path, text):
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)

    def read(self, path):
        with open(path, encoding='utf-8') as f:
            return f.read()

    def run_tool(self, *args):
        env = dict(os.environ, USERPROFILE=self.home, HOME=self.home, CLAUDE_AGENT_MODEL='')
        return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True,
                              env=env, timeout=60)

    def make_report(self, edit=lambda t: t):
        r = self.run_tool('migrate-report', '--out', self.report)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.put(self.report, edit(reviewed(self.read(self.report))))

    def apply(self, *more):
        return self.run_tool('rule-apply', self.report, *more)

    def test_a_repo_not_in_ledger_repos_is_refused_and_nothing_is_written(self):
        self.make_report()
        r = self.apply('--write', '--prov', 'selected', '--source', 'test')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('not one of ledger_repos', r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'alpha.md')))
        self.assertEqual(os.listdir(os.path.join(self.dir, 'rules')), [])

    def test_a_refused_rule_apply_prints_a_REFUSED_line_and_exits_1(self):
        self.make_report(lambda t: t.replace('| alpha |', '| all |', 1))
        r = self.apply()
        self.assertEqual(r.returncode, 1, r.stdout)
        refused = [l for l in r.stderr.splitlines() if 'REFUSED' in l]
        self.assertTrue(refused)
        for line in refused:
            self.assertTrue(line.startswith('REFUSED:'), line)

    def test_known_repos_write_to_their_own_files(self):
        self.make_report(lambda t: t.replace('KEEP | zeta', 'CUT | zeta'))
        dry = self.apply()
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn('dry run: 2 rule(s)', dry.stdout)
        r = self.apply('--write', '--prov', 'selected', '--source', 'test')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ['H-alpha-' + '0001', 'H-pm-' + '0001'])
        self.assertTrue(os.path.exists(os.path.join(self.dir, 'rules', 'alpha.md')))
        c = self.run_tool('check')
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)

    def test_a_shared_rule_is_refused_through_the_verb(self):
        self.make_report(lambda t: t.replace('| alpha |', '| all |', 1))
        r = self.apply()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('shared rule', r.stderr)

    def test_two_repos_of_one_book_proposing_one_key_are_refused_before_any_write(self):
        self.put(os.path.join(self.dir, 'rules', 'pm.md'), '# T\nRepos: alpha, pm\n')

        def same_key(t):
            keys = [l.split(' | ')[2] for l in t.splitlines() if l.startswith('KEEP')]
            return t.replace(keys[1], keys[0]).replace('KEEP | zeta', 'CUT | zeta')
        self.make_report(same_key)
        r = self.apply('--write', '--prov', 'selected', '--source', 'test')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('lands in pm.md', r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'alpha.md')))


if __name__ == '__main__':
    unittest.main()
