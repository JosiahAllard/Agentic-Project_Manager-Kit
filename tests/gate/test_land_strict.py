"""kitlib.gate.land and strict paths (docs/gate.md "Strict paths"): a branch that changes a path
matching the list in kitlib/gate/strict_defaults.py, read from the base and never from the branch,
lands only alone, with a queued verdict (kitlib.gate.verdicts) at its tip from a strong-tier
reviewer who is not its author, whoever built it; its merged gate then runs --full --fresh
whatever the caller passed. Anything unreadable refuses (fail closed)."""
import json
import os
import unittest

from kitlib import gate as G
from kitlib.gate import land, strict_defaults, verdicts
from kitlib.git import repo as repomod
from kitlib.records import gate_ok
from tests import gitfixture
from tests.gate import fixtures, test_land

BRANCH = test_land.BRANCH
LIST_FILE = 'kitlib/gate/strict_defaults.py'    # read at the base (land.STRICT_FILE)
STRICT = 'kitlib/gate/thing.py'     # matches kitlib/gate/** of the running kit's STRICT_DEFAULTS
REVIEWER = {'verdict': 'LAND', 'risk': 'High', 'reviewer': 'pm-kit:reviewer',
            'reviewer_model': 'opus', 'reviewer_id': 'agent-r1',
            'author': 'Kit Test / strong-worker', 'session': 'coord-1', 'records': []}


class StrictCase(test_land.LandCase):
    REVIEW_TIPS = False     # every verdict here is queued (or not) by the test itself

    def common(self):
        common, why = repomod.common_dir(self.repo.root)
        self.assertIsNotNone(common, why)
        return common

    def queue(self, branch, tip, **over):
        entry = dict(REVIEWER, branch=branch, tip=tip, range='%s..%s' % (self.main[:10], tip))
        entry.update(over)
        verdicts.add(self.common(), entry)

    def queue_by_hand(self, branch, tip, drop):
        """An entry file written past add(), lacking the key `drop`."""
        entry = dict(REVIEWER, branch=branch, tip=tip, range='%s..%s' % (self.main[:10], tip),
                     queued_utc='2026-10-14T00:00:00Z', state='queued', last_result=None)
        del entry[drop]
        os.makedirs(verdicts.queue_dir(self.common()), exist_ok=True)
        with open(verdicts.entry_path(self.common(), branch), 'w', encoding='utf-8') as f:
            json.dump(entry, f)

    def on_main(self, files, message='main edit'):
        for rel, text in files.items():
            self.repo.write(rel, text)
        self.main = self.repo.commit(message, *files)
        self.repo.git('push', '-q', 'origin', 'main')

    def land_it(self, branch, tip, **kw):
        said = []
        lander = self.lander(branch, self.main, tip, say=said.append, **kw)
        return lander, lander.run(), said

    def assert_refused(self, out, *words):
        self.assertTrue(out.lines[0].startswith('REFUSED: '), out.lines)
        self.assertTrue(out.lines[0].endswith('; nothing was merged'), out.lines)
        for w in words:
            self.assertIn(w, out.lines[0])
        self.assert_untouched(out, G.REFUSED)


class NeedsAVerdict(StrictCase):
    def test_a_strict_change_with_no_queued_verdict_is_refused(self):
        _, tip = self.branch('feature', {STRICT: 'x = 1\n', 'a.py': 'a\n'})
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, '%s changes strict path %s' % (BRANCH, STRICT),
                            'none is queued for it')

    def test_a_verdict_from_a_model_below_the_strong_tier_is_refused(self):
        _, tip = self.branch('feature', {STRICT: 'x = 1\n'})
        for model in ('sonnet', 'haiku', 'account-default', 'claude-sonnet-4-6', 'gpt-5'):
            self.queue(BRANCH, tip, reviewer_model=model)
            _, out, _ = self.land_it(BRANCH, tip)
            self.assert_refused(out, 'the reviewer ran on %s, not a strong-tier model' % model)

    def test_a_verdict_whose_reviewer_is_the_author_is_refused(self):
        _, tip = self.branch('feature', {STRICT: 'x = 1\n'})
        self.queue(BRANCH, tip, author='Kit Test / strong-worker agent-r1')
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, 'the reviewer agent-r1 (pm-kit:reviewer) is its author')
        self.queue(BRANCH, tip, reviewer='pm-kit:strong-worker', reviewer_id='agent-r2')
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, 'the reviewer agent-r2 (pm-kit:strong-worker) is its author')

    def test_a_verdict_given_at_another_tip_is_refused(self):
        wt, old = self.branch('feature', {STRICT: 'x = 1\n'})
        self.queue(BRANCH, old)
        self.repo.write('b.py', 'b\n', root=wt)
        tip = self.repo.commit('more', 'b.py', cwd=wt)
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, 'the verdict was given at %s, and the branch is at %s'
                            % (old[:10], tip[:10]))

    def test_an_entry_lacking_the_reviewer_model_or_the_author_is_refused(self):
        _, tip = self.branch('feature', {STRICT: 'x = 1\n'})
        for missing in ('reviewer_model', 'author'):
            self.queue_by_hand(BRANCH, tip, missing)
            _, out, _ = self.land_it(BRANCH, tip)
            self.assert_refused(out, 'its verdict entry cannot be used', 'lacks %s' % missing)

    def test_an_entry_marked_failed_is_refused(self):
        _, tip = self.branch('feature', {STRICT: 'x = 1\n'})
        self.queue(BRANCH, tip)
        verdicts.mark_failed(self.common(), BRANCH, 'red gate', tip=tip)
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, 'its verdict entry is marked failed (red gate)')


class LandsAloneFullFresh(StrictCase):
    # a glob-only step: a landing that is not --full selects nothing for kitlib/gate/thing.py
    # and is refused (test_land.NoStepSelected); only the escalation to --full runs it
    GATE = {'steps': [{'name': 'unit', 'globs': ['src/*'], 'cmd': fixtures.RED_AWARE,
                       'pass': '^ok$'}]}

    def test_a_reviewed_strict_branch_lands_with_a_full_fresh_merged_gate(self):
        _, tip = self.branch('feature', {STRICT: 'x = 1\n'})
        self.queue(BRANCH, tip)
        lander, out, said = self.land_it(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertTrue(lander.full and lander.fresh)
        self.assertIn('STRICT: %s changes strict path %s: reviewed by pm-kit:reviewer on opus '
                      '(agent-r1); the merged gate runs --full --fresh' % (BRANCH, STRICT), said)
        rec, why = gate_ok.verify(self.repo.root, out.merge)
        self.assertIsNone(why)
        self.assertEqual([s['name'] for s in rec['steps']], ['unit'])

    def test_a_full_model_id_naming_the_strong_tier_counts(self):
        _, tip = self.branch('feature', {STRICT: 'x = 1\n'})
        self.queue(BRANCH, tip, reviewer_model='claude-opus-5-5')
        lander, out, said = self.land_it(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(lander.full and lander.fresh)
        self.assertIn('STRICT: %s changes strict path %s: reviewed by pm-kit:reviewer on '
                      'claude-opus-5-5 (agent-r1); the merged gate runs --full --fresh'
                      % (BRANCH, STRICT), said)

    def test_a_reviewed_plain_branch_is_not_escalated(self):
        _, tip = self.branch('feature', {'src/a.py': 'a\n'})
        self.queue(BRANCH, tip)
        lander, out, said = self.land_it(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse(lander.full or lander.fresh)
        self.assertEqual([l for l in said if l.startswith('STRICT')], [])

    def test_a_batch_holding_a_strict_branch_is_refused(self):
        _, strict_tip = self.branch('strict', {STRICT: 'x = 1\n'})
        _, plain_tip = self.branch('feature', {'src/a.py': 'a\n'})
        self.queue('kit/strict', strict_tip)
        _, out, _ = self.land_it([BRANCH, 'kit/strict'], [plain_tip, strict_tip])
        self.assert_refused(out, 'kit/strict changes strict path %s' % STRICT,
                            'lands alone, never in a batch')


class ReviewerTierByAuthor(StrictCase):
    """A strict branch needs a strong-tier review whoever built it: a haiku review counts for
    none, a worker or weak-worker build included. A plain branch built by worker lands on a haiku
    review (no tier rule off the strict paths)."""
    GATE = LandsAloneFullFresh.GATE

    def lands(self, model, author):
        _, tip = self.branch('feature', {STRICT: 'x = 1\n'})
        self.queue(BRANCH, tip, reviewer_model=model, author=author)
        lander, out, said = self.land_it(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertTrue(lander.full and lander.fresh)
        self.assertIn('STRICT: %s changes strict path %s: reviewed by pm-kit:reviewer on %s '
                      '(agent-r1); the merged gate runs --full --fresh' % (BRANCH, STRICT, model),
                      said)

    def refused(self, model, author):
        _, tip = self.branch('feature', {STRICT: 'x = 1\n'})
        self.queue(BRANCH, tip, reviewer_model=model, author=author)
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, 'the reviewer ran on %s, not a strong-tier model' % model,
                            'a branch that changes a strict path lands only on a strong-tier '
                            'review, whoever built it')

    def test_a_haiku_verdict_for_a_worker_build_is_refused(self):
        self.refused('haiku', 'Kit Test / worker')

    def test_a_haiku_model_id_for_a_weak_worker_build_is_refused(self):
        self.refused('claude-haiku-4-5', 'Kit Test / pm-kit:weak-worker')

    def test_a_haiku_verdict_lands_a_plain_worker_build(self):
        _, tip = self.branch('feature', {'src/a.py': 'a\n'})
        self.queue(BRANCH, tip, reviewer_model='haiku', author='Kit Test / worker')
        lander, out, said = self.land_it(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertIsNone(lander.strict_review)
        self.assertEqual([l for l in said if l.startswith('STRICT')], [])

    def test_a_haiku_verdict_for_a_strong_worker_build_is_refused(self):
        self.refused('haiku', 'Kit Test / strong-worker')

    def test_a_haiku_verdict_for_a_senior_worker_build_is_refused(self):
        self.refused('haiku', 'Kit Test / senior-worker')

    def test_a_haiku_verdict_with_no_author_type_is_refused(self):
        self.refused('haiku', 'Kit Test')

    def test_an_opus_verdict_still_lands_for_a_worker_build(self):
        self.lands('opus', 'Kit Test / worker')

    def test_an_opus_verdict_still_lands_for_a_senior_worker_build(self):
        self.lands('opus', 'Kit Test / senior-worker')


class CommitRecordsLandGate(StrictCase):
    """The commit and records code and the landing and gate entry points are strict paths of the
    running kit's list (kitlib/commit/**, kitlib/records/**, tools/land.py, tools/gate.py): a
    branch changing one is refused on a haiku review of a worker build, which lands a plain
    branch (ReviewerTierByAuthor)."""
    GATE = LandsAloneFullFresh.GATE

    def test_each_is_a_strict_path(self):
        for n, path in enumerate(('kitlib/commit/x.py', 'kitlib/records/x.py', 'tools/land.py',
                                  'tools/gate.py')):
            with self.subTest(path=path):
                branch = 'kit/strict-%d' % n
                _, tip = self.branch('strict-%d' % n, {path: 'x = 1\n'})
                self.queue(branch, tip, reviewer_model='haiku', author='Kit Test / worker')
                _, out, _ = self.land_it(branch, tip)
                self.assert_refused(out, '%s changes strict path %s' % (branch, path),
                                    'the reviewer ran on haiku, not a strong-tier model')


class CarriedVerdict(StrictCase):
    """A verdict carried over main merges (tools/verdict.py carry; docs/gate.md "Carrying a
    verdict forward") lets the merge land, and only when kitlib.gate.carry passes it again here
    against the base, never on the facts stored."""
    GATE = LandsAloneFullFresh.GATE

    def synced(self):
        """kit/feature reviewed and queued at a strict change, then main moved and was merged in:
        (wt, reviewed, the merge)."""
        wt, reviewed = self.branch('feature', {STRICT: 'x = 1\n'})
        self.queue(BRANCH, reviewed)
        self.on_main({'m.py': 'm\n'})
        self.repo.git('merge', '-q', '--no-edit', 'main', cwd=wt)
        return wt, reviewed, self.repo.head(wt)

    def test_the_merge_of_a_reviewed_branch_needs_a_carry(self):
        _, reviewed, tip = self.synced()
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, 'the verdict was given at %s, and the branch is at %s: review and '
                            'queue the tip, or carry the verdict (tools/verdict.py carry)'
                            % (reviewed[:10], tip[:10]))

    def test_a_carried_verdict_lands_with_a_full_fresh_merged_gate(self):
        _, reviewed, tip = self.synced()
        verdicts.carry(self.common(), self.repo.root, BRANCH, self.main)
        lander, out, said = self.land_it(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertTrue(lander.full and lander.fresh)
        self.assertEqual(lander.strict_carry['from'], reviewed)
        self.assertIn('STRICT: %s changes strict path %s: reviewed by pm-kit:reviewer on opus '
                      '(agent-r1) at %s, carried over 1 main merge; the merged gate runs --full '
                      '--fresh' % (BRANCH, STRICT, reviewed[:10]), said)

    def test_the_carry_is_checked_against_the_base_landed_on(self):
        _, reviewed, tip = self.synced()
        carried = self.main
        verdicts.carry(self.common(), self.repo.root, BRANCH, carried)
        self.on_main({'n.py': 'n\n'})          # main moves on after the carry: it still holds
        lander, out, _ = self.land_it(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual((lander.strict_carry['from'], lander.strict_carry['main']),
                         (reviewed, self.main))
        self.assertNotEqual(self.main, carried)

    def test_a_stored_carry_that_does_not_hold_is_refused(self):
        wt, reviewed, _ = self.synced()
        self.repo.write('b.py', 'b\n', root=wt)
        tip = self.repo.commit('own work after the sync', 'b.py', cwd=wt)
        # facts no check would give: a carry record written past kitlib.gate.carry
        fake = {'from': reviewed, 'merges': [tip], 'main': self.main, 'diff': '0' * 64}
        verdicts.carry(self.common(), self.repo.root, BRANCH, self.main,
                       checker=lambda *a: (dict(fake), None))
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, 'the verdict was given at %s and carried to %s, but the carry '
                            'does not hold against main %s' % (reviewed[:10], tip[:10],
                                                               self.main[:10]),
                            'not a merge', 'review and queue the tip')


class ReadFromTheBase(StrictCase):
    # The fixture identity is the owner: the list file is owner-only (kitlib.guard.ownercommits),
    # and these branches change it only to show the strict check reads the base's copy.
    KIT = dict(StrictCase.KIT, owner={'email': gitfixture.EMAIL})
    LIST = "STRICT_DEFAULTS = (\n    'special/**',\n)\n"

    def test_the_bases_list_is_read(self):
        self.on_main({LIST_FILE: self.LIST})
        _, tip = self.branch('feature', {'special/a.py': 'a\n'})
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, 'changes strict path special/a.py', 'none is queued')

    def test_the_branch_cannot_drop_a_path_from_the_list(self):
        self.on_main({LIST_FILE: self.LIST})
        _, tip = self.branch('feature', {LIST_FILE: 'STRICT_DEFAULTS = ()\n',
                                         'special/a.py': 'a\n'})
        _, out, _ = self.land_it(BRANCH, tip)
        # the base's list has only special/**: the branch's emptied copy is never read
        self.assert_refused(out, '%s changes strict path special/a.py:' % BRANCH, 'none is queued')

    def test_a_plain_branch_lands_beside_the_bases_list(self):
        self.on_main({LIST_FILE: self.LIST})
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        self.queue(BRANCH, tip)
        _, out, _ = self.land_it(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)

    def test_a_list_that_cannot_be_read_refuses_every_landing(self):
        self.on_main({LIST_FILE: 'STRICT_DEFAULTS = tuple(make())\n'})
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, 'cannot read the strict paths from main at %s' % self.main[:10],
                            'STRICT_DEFAULTS')


class Pure(unittest.TestCase):
    def test_parse_strict_reads_the_literal_and_runs_nothing(self):
        self.assertEqual(land.parse_strict("'''doc'''\nSTRICT_DEFAULTS = (\n  'a/**',\n  'b/*',\n)\n"),
                         ('a/**', 'b/*'))
        for text, why in (('STRICT_DEFAULTS = (', 'does not parse'),
                          ('X = 1\n', 'assigns STRICT_DEFAULTS 0 times'),
                          ("STRICT_DEFAULTS = ('a',)\nSTRICT_DEFAULTS = ('b',)\n", '2 times'),
                          ("STRICT_DEFAULTS += ('a',)\n", 'not a literal tuple'),
                          ('STRICT_DEFAULTS = tuple(make())\n', 'not a literal tuple'),
                          ("STRICT_DEFAULTS = 'a/**'\n", 'not a literal tuple'),
                          ('STRICT_DEFAULTS = ()\n', 'non-empty glob strings'),
                          ("STRICT_DEFAULTS = ('a', ' ')\n", 'non-empty glob strings'),
                          ("STRICT_DEFAULTS = ('a', 1)\n", 'non-empty glob strings')):
            with self.assertRaises(ValueError) as cm:
                land.parse_strict(text)
            self.assertIn(why, str(cm.exception))

    def test_the_kits_own_list_parses_as_it_imports(self):
        self.assertEqual(land.STRICT_FILE, LIST_FILE)
        path = os.path.join(os.path.dirname(strict_defaults.__file__), 'strict_defaults.py')
        with open(path, encoding='utf-8') as f:
            self.assertEqual(land.parse_strict(f.read()), tuple(strict_defaults.STRICT_DEFAULTS))

    def test_strong_models_are_the_strong_and_deep_tiers_of_no_weaker_tier(self):
        models, why = land.strong_models()
        self.assertIsNone(why)
        self.assertIn('opus', models)
        for weak in ('sonnet', 'haiku', 'account-default'):
            self.assertNotIn(weak, models)
        models, why = land.strong_models(os.path.join(os.path.dirname(__file__), 'no-such.json'))
        self.assertIsNone(models)
        self.assertIn('hosts.json', why)

    def test_is_author(self):
        def entry(**over):
            return dict(REVIEWER, **over)
        self.assertFalse(land.is_author(entry()))
        self.assertTrue(land.is_author(entry(author='opus / strong-worker Agent-R1')))
        self.assertFalse(land.is_author(entry(author='opus / strong-worker agent-r12')))
        self.assertTrue(land.is_author(entry(reviewer='pm-kit:strong-worker')))
        self.assertTrue(land.is_author(entry(reviewer='Strong-Worker')))
        self.assertFalse(land.is_author(entry(reviewer='pm-kit:worker')))

    def test_is_author_splits_the_author_on_slash_comma_and_semicolon(self):
        for author in ('Kit Test (agent), strong-worker', 'Kit Test (agent); strong-worker',
                       'Kit Test (agent),pm-kit:strong-worker'):
            with self.subTest(author=author):
                self.assertTrue(land.is_author(dict(REVIEWER, reviewer='pm-kit:strong-worker',
                                                    author=author)))
        self.assertFalse(land.is_author(dict(REVIEWER, author='Kit Test (agent), strong-worker')))

    def test_is_author_finds_the_reviewer_type_as_a_word_anywhere_in_the_author(self):
        for author in ('Kit Test (agent) (pm-kit:strong-worker)', 'Kit Test | strong-worker',
                       'Kit Test & strong-worker', 'Kit Test strong-worker',
                       'Kit Test (agent) other-plugin:Strong-Worker.', 'strong-worker'):
            with self.subTest(author=author):
                self.assertTrue(land.is_author(dict(REVIEWER, reviewer='pm-kit:strong-worker',
                                                    author=author)))

    def test_is_author_matches_each_id_of_a_reviewer_id_list(self):
        for rid, author in (('a169fe6a70777751b,a85bb01f9175819d1', 'a85bb01f9175819d1'),
                            ('a169fe6a70777751b,a85bb01f9175819d1',
                             'Kit Test / a85bb01f9175819d1'),
                            ('agent-r9/agent-r1', 'Kit Test / worker agent-r1'),
                            ('agent-r9 agent-r1', 'Kit Test / worker (agent-r1)')):
            with self.subTest(reviewer_id=rid, author=author):
                self.assertTrue(land.is_author(dict(REVIEWER, reviewer_id=rid, author=author)))

    def test_is_author_never_matches_part_of_a_longer_type(self):
        for reviewer, author in (('pm-kit:worker', 'Kit Test | strong-worker'),
                                 ('pm-kit:worker', 'Kit Test (agent) (pm-kit:strong-worker)'),
                                 ('worker', 'Kit Test & weak-worker'),
                                 ('pm-kit:worker', 'Kit Test workers'),
                                 ('pm-kit:strong-worker', 'Kit Test | worker'),
                                 ('pm-kit:strong-worker', 'Kit Test (pm-kit:worker)'),
                                 ('pm-kit:strong-worker', 'Kit Test strong-worker-2'),
                                 ('pm-kit:strong-worker', 'Kit Test strong-worker_x'),
                                 ('pm-kit:reviewer', 'Kit Test | strong-worker')):
            with self.subTest(reviewer=reviewer, author=author):
                self.assertFalse(land.is_author(dict(REVIEWER, reviewer=reviewer,
                                                     author=author)))
        for rid in ('agent-r9,agent-r1', 'agent-r9 / agent-r1', 'agent-r9 agent-r1'):
            with self.subTest(reviewer_id=rid):
                self.assertFalse(land.is_author(dict(REVIEWER, reviewer_id=rid,
                                                     author='Kit Test / worker agent-r12')))

    def test_strong_alias_maps_a_full_model_id_and_refuses_the_rest(self):
        strong, weak = {'opus'}, {'haiku', 'sonnet', 'account-default'}
        for model, alias in (('opus', 'opus'), (' Opus ', 'opus'),
                             ('claude-opus-5-5', 'opus'), ('Claude-Opus-4-1', 'opus'),
                             ('claude-opus-5-5[1m]', 'opus'),
                             ('claude-sonnet-4-6', None), ('claude-haiku-4-5', None),
                             ('sonnet', None), ('account-default', None), ('gpt-5', None),
                             ('opusx-1', None), ('claude-opus5', None),
                             ('claude-opus-sonnet', None), ('', None)):
            with self.subTest(model=model):
                self.assertEqual(land.strong_alias(model, strong, weak), alias)
        # a model in a strong and a weak tier (Codex's account-default) never counts
        self.assertIsNone(land.strong_alias('account-default', {'opus', 'account-default'},
                                            {'account-default'}))



if __name__ == '__main__':
    unittest.main()
