"""The pack's co-author section end to end: `.kit.json` "review_pack".coauthor as the base holds
it, judged by kitlib.reviewpack.trailers, including the owner check on Owner-Authored and
Owner-Approved claims."""
import json
from unittest import mock

from kitlib.reviewpack import trailers
from tests.gitfixture import EMAIL
from tests.reviewpack.repo import AGENT, TRAILERED_MSG, PackCase


class CoAuthorCase(PackCase):
    CONFIG = {'owner': {'email': EMAIL}, 'review_pack': {'coauthor': True}}

    def setUp(self):
        super().setUp()
        self.enter(mock.patch.object(trailers, 'TRAILER_FAIL_SINCE', 0))    # every commit is new
        self.git('checkout', '-q', '-b', 'feature')

    def pack(self):
        return self.run_pack('feature', '--base', 'main')

    def row(self, out, subject):
        [line] = [l for l in out.splitlines() if l.startswith('  ') and '"%s"' % subject in l]
        return line


class CoAuthorSection(CoAuthorCase):
    def test_a_new_trailerless_commit_is_listed_as_fail_and_exits_1(self):
        self.write('f.txt', '1')
        self.commit_all('work with the trailer\n\n' + TRAILERED_MSG)
        self.write('f.txt', '2')
        self.commit_all('work without the trailer')
        code, out = self.pack()
        self.assertEqual(code, 1, out)
        self.assertIn('co-author  2 commit(s) | 1 without a Co-Authored-By trailer | 1 failing', out)
        self.assertIn(' FAIL ', self.row(out, 'work without the trailer'))
        self.assertNotIn('"work with the trailer"', out)

    def test_an_old_trailerless_commit_is_listed_and_does_not_stop_a_landing(self):
        self.enter(mock.patch.object(trailers, 'TRAILER_FAIL_SINCE', 2 ** 62))
        self.write('f.txt', '1')
        self.commit_all('old work without the trailer')
        code, out = self.pack()
        self.assertEqual(code, 0, out)
        self.assertIn('1 without a Co-Authored-By trailer | 0 failing', out)
        self.assertIn(' old ', self.row(out, 'old work without the trailer'))

    def test_a_fully_trailered_branch_lists_nothing_and_exits_0(self):
        self.write('f.txt', '1')
        self.commit_all('work\n\n' + TRAILERED_MSG)
        code, out = self.pack()
        self.assertEqual(code, 0, out)
        self.assertIn('co-author  1 commit(s) | 0 without a Co-Authored-By trailer | 0 failing', out)

    def test_a_bare_name_in_the_trailer_is_not_an_agent_trailer(self):
        self.write('f.txt', '1')
        self.commit_all('work\n\nCo-Authored-By: Claude')
        code, out = self.pack()
        self.assertEqual(code, 1, out)
        self.assertIn('1 failing', out)

    def test_an_unreadable_range_exits_2(self):
        self.write('f.txt', '1')
        self.commit_all('work\n\n' + TRAILERED_MSG)
        with mock.patch.object(trailers, 'branch_commits', lambda *a, **k: (None, 'boom')):
            code, out = self.pack()
        self.assertEqual(code, 2, out)
        self.assertIn('the Co-Authored-By range main..feature could not be read: boom', out)

    def test_the_branch_cannot_switch_its_own_check_off(self):
        # The section is read at the base; the branch's .kit.json edit is an owner-only change too.
        self.write('.kit.json', json.dumps({'owner': {'email': EMAIL},
                                            'review_pack': {'coauthor': False}}))
        self.commit_all('switch the check off', author=AGENT)
        code, out = self.pack()
        self.assertEqual(code, 1, out)
        self.assertIn('co-author  1 commit(s) | 1 without a Co-Authored-By trailer | 1 failing', out)
        self.assertIn('MISSING   .kit.json', out)


class OwnerClaims(CoAuthorCase):
    """An owner claim excuses a trailerless commit only when the base's "owner".email authored it.
    Without the is_owner() check in trailers.classify() the agent cases below exit 0 and print the
    owner label."""

    def test_the_owners_owner_approved_commit_does_not_stop_a_landing(self):
        self.write('f.txt', '1')
        self.commit_all('owner wrote this\n\nOwner-Approved: f.txt')
        code, out = self.pack()
        self.assertEqual(code, 0, out)
        self.assertIn(' owner-approved ', self.row(out, 'owner wrote this'))
        self.assertNotIn('WARNING', out)

    def test_the_owners_owner_authored_commit_is_labelled_and_does_not_stop_a_landing(self):
        self.write('f.txt', '1')
        self.commit_all('owner wrote this\n\nOwner-Authored: yes')
        code, out = self.pack()
        self.assertEqual(code, 0, out)
        self.assertIn('0 failing', out)
        self.assertIn(' owner-authored ', self.row(out, 'owner wrote this'))
        self.assertNotIn('WARNING', out)

    def test_an_agents_owner_authored_marker_is_an_unproven_claim_and_fails(self):
        self.write('f.txt', '1')
        self.commit_all('agent claims the owner wrote this\n\nOwner-Authored: yes', author=AGENT)
        code, out = self.pack()
        self.assertEqual(code, 1, out)
        self.assertIn(' FAIL ', self.row(out, 'agent claims the owner wrote this'))
        self.assertNotIn('owner-authored', out)
        self.assertIn('carries Owner-Authored: yes, an unproven owner claim (authored by %s, not '
                      'the owner) - judged as an agent commit' % AGENT, out)

    def test_an_agents_owner_approved_trailer_is_an_unproven_claim_and_fails(self):
        self.write('f.txt', '1')
        self.commit_all('agent approves itself\n\nOwner-Approved: f.txt', author=AGENT)
        code, out = self.pack()
        self.assertEqual(code, 1, out)
        self.assertIn(' FAIL ', self.row(out, 'agent approves itself'))
        self.assertIn('carries Owner-Approved, an unproven owner claim', out)


class OwnerApproval(CoAuthorCase):
    """Owner-Approval: hub:A-<n> (commit.py --owner-approval; docs/commit.md "Owner approval") is
    shown on the commit's row, trailered or not, and is never an owner claim: what it proves is
    checked by gate land and the pre-push hook against the hub, not here."""

    def test_an_agents_approval_commit_shows_the_approval_on_its_row(self):
        self.write('f.txt', '1')
        self.commit_all('approved change\n\nOwner-Approval: hub:A-7\n' + TRAILERED_MSG,
                        author=AGENT)
        code, out = self.pack()
        self.assertEqual(code, 0, out)
        self.assertIn('owner approval hub:A-7', self.row(out, 'approved change'))
        self.assertIn('0 without a Co-Authored-By trailer | 0 failing', out)
        self.assertNotIn('WARNING', out)

    def test_an_approval_without_a_coauthor_is_no_claim_and_fails(self):
        self.write('f.txt', '1')
        self.commit_all('approval alone\n\nOwner-Approval: hub:A-7', author=AGENT)
        code, out = self.pack()
        self.assertEqual(code, 1, out)
        row = self.row(out, 'approval alone')
        self.assertIn(' FAIL ', row)
        self.assertIn('owner approval hub:A-7', row)
        self.assertNotIn('unproven owner claim', out)

    def test_the_owners_approval_commit_without_a_coauthor_is_not_owner_approved(self):
        self.write('f.txt', '1')
        self.commit_all('owner approval alone\n\nOwner-Approval: hub:A-7')   # the owner's identity
        code, out = self.pack()
        self.assertEqual(code, 1, out)
        self.assertIn(' FAIL ', self.row(out, 'owner approval alone'))
        self.assertNotIn('owner-approved', out)


class OwnerClaimsWithNoOwnerConfigured(CoAuthorCase):
    CONFIG = {'review_pack': {'coauthor': True}}

    def test_nothing_is_the_owners(self):
        self.write('f.txt', '1')
        self.commit_all('owner wrote this\n\nOwner-Authored: yes')      # the fixture identity
        code, out = self.pack()
        self.assertEqual(code, 1, out)
        self.assertIn(' FAIL ', self.row(out, 'owner wrote this'))
        self.assertNotIn('owner-authored', out)
        self.assertIn("an unproven owner claim (the owner's identity is not configured", out)


class CoAuthorConfig(PackCase):
    def test_off_by_default_there_is_no_coauthor_section(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('f.txt', '1')
        self.commit_all('work without the trailer')
        with mock.patch.object(trailers, 'branch_commits') as read:
            code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertNotIn('co-author', out)
        read.assert_not_called()


class UnknownKey(PackCase):
    CONFIG = {'review_pack': {'coauthors': True}}

    def test_an_unknown_key_refuses(self):
        self.git('checkout', '-q', '-b', 'feature')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 2, out)
        self.assertIn('review_pack: refused: kit configuration', out)
        self.assertIn('unknown key(s) coauthors', out)


class NonBoolean(PackCase):
    CONFIG = {'review_pack': {'coauthor': 'yes'}}

    def test_a_non_boolean_refuses(self):
        self.git('checkout', '-q', '-b', 'feature')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 2, out)
        self.assertIn('coauthor must be true or false', out)


if __name__ == '__main__':
    import unittest
    unittest.main()
