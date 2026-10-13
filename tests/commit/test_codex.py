from tests.commit.test_commit import LinkedCase


class CodexCommits(LinkedCase):
    def test_codex_commit_stages_only_owned_paths_and_uses_openai_identity(self):
        self.put('a.py', 'a = 1\n')
        self.put('b.py', 'b = 1\n')
        code, out = self.commit('Codex work', 'a.py', model='gpt-6.1-sol', provider='codex')
        self.assertEqual(code, 0, out)
        self.assertIn('Codex gpt-6.1-sol <noreply@openai.com>', self.message())
        self.assertEqual(self.git('show', '--name-only', '--format=', 'HEAD'), 'a.py')
        self.assertIn('?? b.py', self.git('status', '--porcelain'))

    def test_bad_model_leaves_the_index_and_head_untouched(self):
        self.put('a.py', 'a = 1\n')
        before = self.head()
        code, out = self.commit('bad', 'a.py', model='bad\nmodel', provider='codex')
        self.assertEqual(code, 2, out)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
