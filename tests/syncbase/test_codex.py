from kitlib import syncbase
from tests.syncbase.test_sync import SyncCase


class CodexSync(SyncCase):
    def test_clean_merge_carries_codex_attribution(self):
        target = self.stage_clean()
        result = syncbase.sync(self.repo, target, 'gpt-6.1-sol', provider='codex')
        self.assertEqual(result.code, 0, result.lines)
        self.assertTrue(self.merged(target))
        self.assertIn('Codex gpt-6.1-sol <noreply@openai.com>', self.trailers_of_head())

    def test_conflicted_merge_prepares_codex_resolution_trailer(self):
        target = self.stage_a_conflict()
        result = syncbase.sync(self.repo, target, 'gpt-6.1-sol', provider='codex')
        self.assertEqual(result.code, 1, result.lines)
        with open(self.git_path('MERGE_MSG'), encoding='utf-8') as handle:
            self.assertIn('Codex gpt-6.1-sol <noreply@openai.com>', handle.read())
        self.assertTrue(any('Codex gpt-6.1-sol' in line for line in result.lines))
