"""kitlib.gate.land and the verdict every landing needs (docs/gate.md "Landing" step 4): a branch
that changes no strict path lands too only with a verdict queued at its tip (kitlib.gate.verdicts)
from a reviewer who is not its author; any reviewer model counts (the tier rule is the strict
paths' alone), a carried verdict is checked again against the base, and each branch of a batch
needs its own. Anything unreadable refuses (fail closed)."""
import unittest

from kitlib import gate as G
from kitlib.gate import verdicts
from tests.gate import test_land_strict

StrictCase = test_land_strict.StrictCase
BRANCH = test_land_strict.BRANCH
PLAIN = {'src/a.py': 'a\n'}             # no strict path of the running kit's STRICT_DEFAULTS


class VerdictCase(StrictCase):
    def plain(self, name='feature', files=None):
        return self.branch(name, dict(files or PLAIN))

    def lands(self, branch, tip):
        lander, out, said = self.land_it(branch, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertFalse(lander.full or lander.fresh)
        self.assertEqual([l for l in said if l.startswith('STRICT')], [])
        return lander, out


class EveryLandingNeedsAVerdict(VerdictCase):
    def test_a_plain_branch_with_no_queued_verdict_is_refused(self):
        _, tip = self.plain()
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, '%s: it lands only with a queued verdict (tools/verdict.py add), '
                            'and none is queued for it' % BRANCH)
        self.assertNotIn('strict path', out.lines[0])

    def test_a_verdict_given_at_another_tip_is_refused(self):
        wt, old = self.plain()
        self.queue(BRANCH, old)
        self.repo.write('b.py', 'b\n', root=wt)
        tip = self.repo.commit('more', 'b.py', cwd=wt)
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, '%s: the verdict was given at %s, and the branch is at %s'
                            % (BRANCH, old[:10], tip[:10]), 'tools/verdict.py carry')

    def test_a_verdict_whose_reviewer_is_the_author_is_refused(self):
        _, tip = self.plain()
        self.queue(BRANCH, tip, author='Kit Test / strong-worker agent-r1')
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, '%s: the reviewer agent-r1 (pm-kit:reviewer) is its author'
                            % BRANCH)
        self.queue(BRANCH, tip, reviewer='pm-kit:strong-worker', reviewer_id='agent-r2')
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, 'the reviewer agent-r2 (pm-kit:strong-worker) is its author')

    def test_an_entry_marked_failed_is_refused(self):
        _, tip = self.plain()
        self.queue(BRANCH, tip)
        verdicts.mark_failed(self.common(), BRANCH, 'red gate', tip=tip)
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, '%s: its verdict entry is marked failed (red gate)' % BRANCH)

    def test_an_entry_that_cannot_be_used_is_refused(self):
        _, tip = self.plain()
        self.queue_by_hand(BRANCH, tip, 'author')
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, '%s: its verdict entry cannot be used' % BRANCH, 'lacks author')


class AnyReviewerModel(VerdictCase):
    """The tier rule (land.REVIEWER_MODELS_BY_AUTHOR) is the strict paths' alone: a plain branch
    lands with a review on any model by a reviewer who did not write it."""

    def lands_on(self, model):
        _, tip = self.plain()
        self.queue(BRANCH, tip, reviewer_model=model, author='Kit Test / strong-worker')
        lander, _ = self.lands(BRANCH, tip)
        self.assertIsNone(lander.strict_review)

    def test_a_sonnet_verdict_lands_a_strong_worker_build(self):
        self.lands_on('sonnet')

    def test_a_haiku_verdict_lands_a_strong_worker_build(self):
        self.lands_on('haiku')

    def test_a_full_model_id_below_the_strong_tier_lands_it(self):
        self.lands_on('claude-sonnet-4-6')


class CarriedVerdict(VerdictCase):
    def synced(self):
        """kit/feature reviewed and queued, then main moved and was merged in: (wt, reviewed,
        the merge)."""
        wt, reviewed = self.plain()
        self.queue(BRANCH, reviewed)
        self.on_main({'m.py': 'm\n'})
        self.repo.git('merge', '-q', '--no-edit', 'main', cwd=wt)
        return wt, reviewed, self.repo.head(wt)

    def test_a_carried_verdict_lands(self):
        _, _, tip = self.synced()
        verdicts.carry(self.common(), self.repo.root, BRANCH, self.main)
        lander, _ = self.lands(BRANCH, tip)
        self.assertIsNone(lander.strict_carry)

    def test_a_stored_carry_that_does_not_hold_is_refused(self):
        wt, reviewed, _ = self.synced()
        self.repo.write('b.py', 'b\n', root=wt)
        tip = self.repo.commit('own work after the sync', 'b.py', cwd=wt)
        # facts no check would give: a carry record written past kitlib.gate.carry
        fake = {'from': reviewed, 'merges': [tip], 'main': self.main, 'diff': '0' * 64}
        verdicts.carry(self.common(), self.repo.root, BRANCH, self.main,
                       checker=lambda *a: (dict(fake), None))
        _, out, _ = self.land_it(BRANCH, tip)
        self.assert_refused(out, '%s: the verdict was given at %s and carried to %s, but the carry '
                            'does not hold against main %s' % (BRANCH, reviewed[:10], tip[:10],
                                                               self.main[:10]),
                            'review and queue the tip')


class Batch(VerdictCase):
    def two(self):
        _, a = self.plain('a', {'src/a.py': 'a\n'})
        _, b = self.plain('b', {'src/b.py': 'b\n'})
        return a, b

    def test_a_batch_with_a_branch_lacking_a_verdict_is_refused_naming_it(self):
        a, b = self.two()
        self.queue('kit/a', a)
        _, out, _ = self.land_it(['kit/a', 'kit/b'], [a, b])
        self.assert_refused(out, 'kit/b: it lands only with a queued verdict', 'none is queued')
        self.assertNotIn('kit/a:', out.lines[0])

    def test_a_batch_whose_branches_each_have_a_verdict_lands(self):
        a, b = self.two()
        self.queue('kit/a', a)
        self.queue('kit/b', b)
        _, out, _ = self.land_it(['kit/a', 'kit/b'], [a, b])
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)


if __name__ == '__main__':
    unittest.main()
