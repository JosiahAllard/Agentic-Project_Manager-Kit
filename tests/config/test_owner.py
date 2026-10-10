"""kitlib.config.owner: owner-only paths, the owner's identity (fail-closed) and the landing merge."""
import unittest

from kitlib.config import owner
from tests.config.helpers import AGENT, OWNER


def rules(**kw):
    kw.setdefault('owner_email', OWNER)
    kw.setdefault('agent_emails', [AGENT])
    return owner.Rules(**kw)


class IdentityTest(unittest.TestCase):

    def test_an_unset_owner_proves_nothing(self):
        r = owner.Rules()
        self.assertIn('not configured', r.identity_problem())
        self.assertFalse(r.is_owner(''))
        self.assertFalse(r.is_owner(OWNER))

    def test_an_owner_also_listed_as_an_agent_proves_nothing(self):
        r = rules(agent_emails=[OWNER.upper()])
        self.assertIn('also listed', r.identity_problem())
        self.assertFalse(r.is_owner(OWNER))

    def test_the_owner_matches_case_insensitively_and_nobody_else_does(self):
        r = rules()
        self.assertIsNone(r.identity_problem())
        self.assertTrue(r.is_owner(' Owner@Example.Invalid '))
        self.assertFalse(r.is_owner(AGENT))

    def test_email_of_reads_a_git_ident(self):
        self.assertEqual(owner.email_of('Kit Test <Kit-Test@Example.Invalid> 1700000000 +0000'),
                         'kit-test@example.invalid')
        self.assertEqual(owner.email_of('no address'), '')
        self.assertEqual(owner.email_of(None), '')


class PathsTest(unittest.TestCase):

    def test_the_default_locks_and_its_exception(self):
        r = rules()
        for p in ('CLAUDE.md', 'claude.md', '.claude/settings.json', '.github/workflows/ci.yml',
                  '.kit.json', 'tools\\gate.json'):
            self.assertTrue(r.is_locked(p), p)
        for p in ('docs/CLAUDE.md', '.claude/worktrees/x/CLAUDE2.md', 'tools/gate.py', '.claudex'):
            self.assertFalse(r.is_locked(p), p)

    def test_a_blanket_approves_only_the_paths_under_it(self):
        r = rules()
        self.assertEqual(r.blanket_for('.claude/agents/x.md'), '.claude/')
        self.assertIsNone(r.blanket_for('CLAUDE.md'))
        self.assertEqual(r.approved_by('.claude/agents/x.md', ['.CLAUDE/']), '.CLAUDE/')
        self.assertIsNone(r.approved_by('CLAUDE.md', ['.claude/']))
        self.assertEqual(r.approved_by('CLAUDE.md', ['claude.md']), 'claude.md')

    def test_trailer_matches_needs_a_changed_path(self):
        r = rules()
        self.assertTrue(r.trailer_matches('.claude/', ['.claude/agents/x.md']))
        self.assertFalse(r.trailer_matches('.claude/', ['.claude/worktrees/a/b']))
        self.assertTrue(r.trailer_matches('CLAUDE.md', ['claude.md']))
        self.assertFalse(r.trailer_matches('CLAUDE.md', ['README.md']))
        self.assertEqual(r.canonical('claude.md', ['CLAUDE.md']), 'CLAUDE.md')
        self.assertEqual(r.canonical('.CLAUDE/', []), '.claude/')
        self.assertTrue(r.is_blanket('.Claude/'))


class MergeTest(unittest.TestCase):

    def test_a_branch_cannot_unlock_a_path_or_name_the_owner(self):
        base = rules(entries=['CLAUDE.md', '.claude/'], excepts=['.claude/worktrees/'])
        work = owner.Rules(entries=['README.md'], excepts=['.claude/worktrees/', '.claude/agents/'],
                           owner_email=AGENT, agent_emails=[])
        m = owner.merge(work, base)
        self.assertEqual(m.entries, ['CLAUDE.md', '.claude/', 'README.md'])
        self.assertEqual(m.excepts, ['.claude/worktrees/'])
        self.assertEqual(m.owner_email, OWNER)
        self.assertTrue(m.is_locked('.claude/agents/x.md'))

    def test_an_unreadable_base_leaves_no_owner(self):
        m = owner.merge(rules(), None)
        self.assertIsNotNone(m.identity_problem())
        self.assertTrue(m.is_locked('CLAUDE.md'))

    def test_from_section_falls_back_to_the_default_paths_when_empty(self):
        r = owner.from_section({'paths': [], 'paths_except': [], 'email': None, 'agent_emails': [],
                                'trailer': 'Owner-Approved'})
        self.assertTrue(r.is_locked('CLAUDE.md'))
        self.assertIsNotNone(r.identity_problem())


if __name__ == '__main__':
    unittest.main()
