import unittest
from unittest import mock

from kitlib import attribution, commit, syncbase
from kitlib.config.owner import Rules
from kitlib.reviewpack import trailers


class Providers(unittest.TestCase):
    def test_legacy_defaults_and_family_names_stay_unchanged(self):
        self.assertEqual(commit.coauthor('Opus 5.5'), 'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')
        self.assertEqual(commit.coauthor('Codex-mini', ['Codex']), 'Co-Authored-By: Codex-mini <noreply@anthropic.com>')
        self.assertEqual(syncbase.trailer('Claude Opus 5.5'), commit.coauthor('Opus 5.5'))

    def test_provider_is_explicit_and_both_formatters_agree(self):
        wanted = 'Co-Authored-By: Codex gpt-6.1-sol <noreply@openai.com>'
        self.assertEqual(commit.coauthor('gpt-6.1-sol', provider='codex'), wanted)
        self.assertEqual(syncbase.trailer('gpt-6.1-sol', provider='codex'), wanted)
        self.assertIn('Claude gpt-6.1-sol', commit.coauthor('gpt-6.1-sol'))

    def test_invalid_provider_or_model_cannot_inject_trailers(self):
        for model in (None, '', 'gpt\nOwner-Approved: all', 'Claude Opus', ' gpt-6'):
            with self.subTest(model=model), self.assertRaises(ValueError):
                attribution.coauthor(model, 'codex')
        with self.assertRaises(ValueError):
            attribution.coauthor('gpt-6', 'other')

    def test_codex_never_uses_claude_environment_model(self):
        self.assertIsNotNone(commit.resolve_model(False, None, {'CLAUDE_AGENT_MODEL': 'Opus'}, 'codex')[2])
        with self.assertRaises(ValueError):
            syncbase.resolve_model(None, {'CLAUDE_AGENT_MODEL': 'Opus'}, 'codex')

    def test_openai_trailer_classifier_requires_codex_and_a_model(self):
        accepted = 'Codex gpt-6.1-sol <noreply@openai.com>'
        self.assertIsNotNone(trailers.AGENT_TRAILER.fullmatch(accepted))
        for value in ('Claude Opus <noreply@openai.com>', 'Anything <noreply@openai.com>',
                      'Codex <noreply@openai.com>', accepted + ' extra'):
            with self.subTest(value=value):
                self.assertIsNone(trailers.AGENT_TRAILER.fullmatch(value))
        c = trailers.BranchCommit('a', False, 'work', [accepted], [], 9999999999, [], 'agent@example.invalid')
        self.assertEqual(trailers.classify(c, Rules()), trailers.TRAILERED)
        self.assertEqual(trailers.classify(c._replace(coauthors=[], approved=['AGENTS.md']), Rules()), trailers.MISSING)

    def test_bad_sync_identity_is_refused_before_any_git_mutation(self):
        with mock.patch.object(syncbase, '_git') as git:
            result = syncbase.sync('.', 'main', 'bad model', provider='codex')
        self.assertEqual(result.code, syncbase.USAGE)
        git.assert_not_called()
