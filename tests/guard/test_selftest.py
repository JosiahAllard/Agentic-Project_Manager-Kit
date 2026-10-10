"""claude.guard_selftest.agent_id_check over a scratch projects folder shaped like
~/.claude/projects/<project>/<session>/subagents/agent-*.jsonl."""
import json
import os
import shutil
import tempfile
import unittest

from claude import guard_selftest as selftest


def result_entry(text, is_error=True):
    return {'type': 'user', 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'toolu_01', 'is_error': is_error, 'content': text}]}}


class AgentIdCheck(unittest.TestCase):
    def setUp(self):
        self.projects = tempfile.mkdtemp(prefix='kit-selftest-')
        self.addCleanup(shutil.rmtree, self.projects, True)
        self.dir = os.path.join(self.projects, 'Z--repo', 'sess-1', 'subagents')
        os.makedirs(self.dir)

    def transcript(self, *entries, name='agent-a1.jsonl'):
        with open(os.path.join(self.dir, name), 'w', encoding='utf-8') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')

    def deny(self, role):
        return result_entry('Root search. Instead: Glob. [kit guard: rule=root-search role=%s]' % role)

    def test_no_transcripts_is_inconclusive(self):
        self.assertIsNone(selftest.agent_id_check(self.projects).ok)

    def test_subagent_roles_pass(self):
        self.transcript(self.deny('subagent'), self.deny('subagent'))
        r = selftest.agent_id_check(self.projects)
        self.assertTrue(r.ok)
        self.assertIn('2 guard decision(s)', r.detail)

    def test_a_main_role_in_a_subagent_transcript_alarms(self):
        self.transcript(self.deny('subagent'))
        self.transcript(self.deny('main'), name='agent-a2.jsonl')
        r = selftest.agent_id_check(self.projects)
        self.assertIs(r.ok, False)
        self.assertIn('ALARM', r.detail)
        self.assertIn('agent-a2.jsonl', r.detail)

    def test_quoted_tags_are_not_decisions(self):
        self.transcript(result_entry('[kit guard: rule=heavy role=main]', is_error=False),
                        {'type': 'assistant', 'message': {'content': [
                            {'type': 'text', 'text': '[kit guard: rule=heavy role=main]'}]}})
        self.assertIsNone(selftest.agent_id_check(self.projects).ok)

    def test_since_skips_older_transcripts(self):
        self.transcript(self.deny('main'))
        path = os.path.join(self.dir, 'agent-a1.jsonl')
        os.utime(path, (1000, 1000))
        self.assertIsNone(selftest.agent_id_check(self.projects, since=2000).ok)
        self.assertIs(selftest.agent_id_check(self.projects, since=500).ok, False)


if __name__ == '__main__':
    unittest.main()
