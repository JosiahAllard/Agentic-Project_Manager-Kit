"""kitlib.reviewpack's pure parts, no git: the owner-approval rows, trailer parsing, the risk map,
the trailers rule and its owner check, and the CLI's argument rules; the owner-only path rules
themselves are tests/config/test_owner.py's."""
import contextlib
import io
import unittest

from kitlib import reviewpack
from kitlib.config.owner import Rules
from kitlib.reviewpack import trailers
from tests.reviewpack.repo import cli

OWNER = 'owner@example.invalid'
AGENT = 'agent@example.invalid'


def rows(*commits):
    """owner_approval_rows over (sha, paths, values[, not_owner]) records, default rules."""
    return reviewpack.owner_approval_rows(
        [c if len(c) == 4 else c + (None,) for c in commits], Rules())


def statuses(sha, paths, values):
    return [(status, subject) for _, status, subject, _ in rows((sha, paths, values))]


class TrailerNamesADifferentPath(unittest.TestCase):
    def test_the_claude_blanket_does_not_approve_claude_md(self):
        self.assertEqual(statuses('a1b2c3d', ['CLAUDE.md'], ['.claude/']),
                         [('MISSING', 'CLAUDE.md'), ('UNMATCHED', 'Owner-Approved .claude/')])

    def test_a_trailer_for_a_path_the_commit_does_not_touch_is_reported(self):
        self.assertEqual(statuses('a1b2c3d', ['docs/notes.md'], ['CLAUDE.md']),
                         [('UNMATCHED', 'Owner-Approved CLAUDE.md')])

    def test_the_details_name_both_sides(self):
        [row] = rows(('a1b2c3d', ['docs/notes.md'], ['CLAUDE.md']))
        self.assertIn('docs/notes.md', row[3])
        missing = next(r for r in rows(('a1b2c3d', ['CLAUDE.md'], ['.claude/'])) if r[1] == 'MISSING')
        self.assertIn('Owner-Approved .claude/', missing[3])


class ApprovalIsPerCommit(unittest.TestCase):
    def test_a_sibling_commits_trailer_does_not_approve_this_one(self):
        got = rows(('a1b2c3d', ['CLAUDE.md'], ['CLAUDE.md']), ('e4f5a6b', ['CLAUDE.md'], []))
        self.assertEqual([(sha, status) for sha, status, _, _ in got],
                         [('a1b2c3d', 'ok'), ('e4f5a6b', 'MISSING')])
        self.assertIn('none', got[1][3])

    def test_each_locked_path_needs_its_own_cover(self):
        self.assertEqual(statuses('a', ['CLAUDE.md', '.claude/agent-profile.md'], ['CLAUDE.md']),
                         [('ok', 'CLAUDE.md'), ('MISSING', '.claude/agent-profile.md')])
        self.assertEqual(statuses('a', ['.claude/a.md', '.claude/agents/w.md'], ['.claude/']),
                         [('ok', '.claude/a.md'), ('ok', '.claude/agents/w.md')])

    def test_a_trailer_on_a_commit_not_provably_the_owners_approves_nothing(self):
        [row] = rows(('a', ['CLAUDE.md'], ['CLAUDE.md'], 'authored by x, not the owner'))
        self.assertEqual(row[1], 'MISSING')
        self.assertIn('is on a commit authored by x, not the owner', row[3])

    def test_the_configured_trailer_key_names_the_rows(self):
        got = reviewpack.owner_approval_rows([('a', ['f.txt'], ['CLAUDE.md'], None)],
                                             Rules(trailer='Owner-OK'))
        self.assertEqual(got[0][2], 'Owner-OK CLAUDE.md')


class TrailerParsing(unittest.TestCase):
    def test_it_reads_the_values_in_order(self):
        msg = ('subject\n\nbody\n\nOwner-Approved: CLAUDE.md\nOwner-Approved: .claude/\n'
               'Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n')
        self.assertEqual(reviewpack.trailers_of(msg), ['CLAUDE.md', '.claude/'])

    def test_a_windows_separator_names_the_same_path(self):
        self.assertEqual(reviewpack.trailers_of('s\n\nOwner-Approved: .claude\\agent-profile.md'),
                         ['.claude/agent-profile.md'])

    def test_a_body_sentence_that_quotes_the_phrase_is_not_a_trailer(self):
        msg = ('subject\n\nThe fix requires an Owner-Approved: CLAUDE.md trailer.\n\n'
               'Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n')
        self.assertEqual(reviewpack.trailers_of(msg), [])
        self.assertEqual(reviewpack.trailers_of(msg.rstrip('\n') + '\nOwner-Approved: CLAUDE.md'),
                         ['CLAUDE.md'])

    def test_a_longer_key_that_starts_with_the_name_is_not_the_trailer(self):
        self.assertEqual(reviewpack.trailers_of('s\n\nOwner-Approved-By: CLAUDE.md'), [])

    def test_a_configured_key_is_read(self):
        self.assertEqual(reviewpack.trailers_of('s\n\nOwner-OK: CLAUDE.md', 'Owner-OK'),
                         ['CLAUDE.md'])


class RiskMap(unittest.TestCase):
    def test_rules_from_lines_reads_levels_paths_and_folders(self):
        lines = ['# risk map\n', '- **High** network | net/, src/core/Wire.java\n',
                 '- Medium ui | ui\n', '- Low docs | docs/*.md, readme\n']
        self.assertEqual(reviewpack.rules_from_lines(lines, lambda tok: tok == 'ui'),
                         [('High', 'net'), ('High', 'src/core/Wire.java'), ('Medium', 'ui'),
                          ('Low', 'docs/*.md')])

    def test_level_is_the_longest_prefix_and_an_unlisted_doc_is_low(self):
        rules = [('High', 'net'), ('Low', 'net/docs')]
        self.assertEqual(reviewpack.level('net/docs/a.txt', rules), 'Low')
        self.assertEqual(reviewpack.level('net/x.py', rules), 'High')
        self.assertEqual(reviewpack.level('other/x.py', rules), 'unlisted (= High)')
        self.assertEqual(reviewpack.level('other/x.md', rules), 'Low')


def commit(coauthors=(), approved=(), authored=(), committed=2 ** 40, author=AGENT, h='a' * 40):
    return trailers.BranchCommit(h, False, 'subject', list(coauthors), list(approved), committed,
                                 list(authored), author)


class TheTrailersRule(unittest.TestCase):
    RULES = Rules(owner_email=OWNER)

    def test_an_agent_trailer_passes_and_a_bare_name_does_not(self):
        self.assertEqual(trailers.classify(
            commit(['Claude Opus 5 <noreply@anthropic.com>']), self.RULES), trailers.TRAILERED)
        self.assertEqual(trailers.classify(commit(['Claude Opus 5']), self.RULES), trailers.MISSING)

    def test_lander_undated_old_and_missing(self):
        self.assertEqual(trailers.classify(commit(), self.RULES, exempt={'a' * 40}),
                         trailers.LANDER)
        self.assertEqual(trailers.classify(commit(committed=None), self.RULES), trailers.UNDATED)
        self.assertEqual(trailers.classify(commit(committed=5), self.RULES), trailers.OLD)
        self.assertEqual(trailers.classify(commit(), self.RULES), trailers.MISSING)
        self.assertEqual(trailers.label(commit(), trailers.MISSING), 'FAIL')
        self.assertEqual(trailers.label(commit(), trailers.OLD), 'old')


class OwnerClaimsNeedTheOwnersAuthorship(unittest.TestCase):
    """An Owner-Authored or Owner-Approved trailer excuses a commit only when the owner authored
    it. Without the is_owner() check in classify() every case below but the first reads OWNER."""

    def test_the_owners_own_marker_commit_is_owner_authored(self):
        c = commit(authored=['yes'], author=OWNER)
        v = trailers.classify(c, Rules(owner_email=OWNER))
        self.assertEqual(v, trailers.OWNER)
        self.assertEqual(trailers.label(c, v), 'owner-authored')
        self.assertIsNone(trailers.unproven_claim(c, Rules(owner_email=OWNER)))

    def test_the_owners_approved_commit_reads_owner_approved(self):
        c = commit(approved=['f.txt'], author=OWNER.upper())
        v = trailers.classify(c, Rules(owner_email=OWNER))
        self.assertEqual(trailers.label(c, v), 'owner-approved')

    def test_an_agents_marker_is_an_unproven_claim_and_fails(self):
        c = commit(authored=['yes'])
        v = trailers.classify(c, Rules(owner_email=OWNER))
        self.assertEqual(v, trailers.MISSING)
        self.assertEqual(trailers.label(c, v), 'FAIL')
        self.assertEqual(trailers.unproven_claim(c, Rules(owner_email=OWNER)),
                         'authored by %s, not the owner' % AGENT)

    def test_an_agents_owner_approved_is_an_unproven_claim_too(self):
        c = commit(approved=['f.txt'])
        self.assertEqual(trailers.classify(c, Rules(owner_email=OWNER)), trailers.MISSING)
        self.assertIsNotNone(trailers.unproven_claim(c, Rules(owner_email=OWNER)))

    def test_with_no_owner_configured_nothing_is_the_owners(self):
        c = commit(authored=['yes'], author=OWNER)
        self.assertEqual(trailers.classify(c, Rules()), trailers.MISSING)
        self.assertIn('not configured', trailers.unproven_claim(c, Rules()))

    def test_an_owner_email_also_listed_as_an_agents_proves_nothing(self):
        rules = Rules(owner_email=OWNER, agent_emails=[OWNER])
        c = commit(authored=['yes'], author=OWNER)
        self.assertEqual(trailers.classify(c, rules), trailers.MISSING)
        self.assertIn('agent_emails', trailers.unproven_claim(c, rules))

    def test_an_old_unproven_claim_is_old_not_owner(self):
        c = commit(authored=['yes'], committed=5)
        self.assertEqual(trailers.classify(c, Rules(owner_email=OWNER)), trailers.OLD)

    def test_a_marker_value_other_than_yes_claims_nothing(self):
        c = commit(authored=['no'], author=OWNER)
        self.assertFalse(trailers.claims_owner(c))
        self.assertEqual(trailers.classify(c, Rules(owner_email=OWNER)), trailers.MISSING)
        self.assertIsNone(trailers.unproven_claim(c, Rules(owner_email=OWNER)))


class ParseLog(unittest.TestCase):
    def test_a_record_splits_into_its_fields(self):
        rec = '\x1f'.join(('h' * 40, 'p1 p2', '123', 'A@x', 'C <noreply@anthropic.com>\n', '',
                           'yes\n', 'hub:A-2\n', 'subject \x1f with a separator')) + '\x1e\n'
        [c] = trailers.parse_log(rec)
        self.assertEqual(c, trailers.BranchCommit('h' * 40, True, 'subject \x1f with a separator',
                                                  ['C <noreply@anthropic.com>'], [], 123, ['yes'],
                                                  'A@x', approval=['hub:A-2']))

    def test_an_owner_approval_is_not_an_owner_claim(self):
        c = trailers.BranchCommit('h', False, 's', [], [], 2 ** 40, [], OWNER,
                                  approval=['hub:A-2'])
        self.assertFalse(trailers.claims_owner(c))
        self.assertEqual(trailers.classify(c, Rules(owner_email=OWNER)), trailers.MISSING)

    def test_an_unreadable_date_is_none(self):
        rec = '\x1f'.join(('h', 'p', 'x', 'a', '', '', '', '', 's')) + '\x1e'
        self.assertIsNone(trailers.parse_log(rec)[0].committed)


class WarningsFirst(unittest.TestCase):
    def test_warnings_move_up_and_the_rest_keeps_its_order(self):
        self.assertEqual(reviewpack.warnings_first('a\nb\nx WARNING: w\nc'), 'x WARNING: w\na\nb\nc')
        self.assertEqual(reviewpack.warnings_first(''), '')


class Arguments(unittest.TestCase):
    def test_minus_c_defaults_to_here(self):
        a = cli.build_parser().parse_args(['feature'])
        self.assertEqual((a.root, a.branch, a.base, a.diff), ('.', 'feature', None, None))

    def usage(self, argv):
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as ctx:
            cli.main(argv)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn('USAGE:', err.getvalue())
        return err.getvalue()

    def test_a_prefix_of_a_flag_is_not_taken_as_the_flag(self):
        self.usage(['feature', '--ba', 'main'])

    def test_no_branch_or_a_blank_base_is_usage(self):
        self.usage([])
        self.usage(['feature', '--base', ' '])
        self.assertIn('-C needs a directory', self.usage(['-C', '', 'feature']))


if __name__ == '__main__':
    unittest.main()
