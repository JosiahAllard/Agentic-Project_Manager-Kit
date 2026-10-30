"""tools/verdict.py add checks --reviewer-model against the reviewer's subagent transcript
(hosts.claude.adapter.agent_models, kitlib.gate.verdicts.seen_models and model_mismatch_why), on
the scratch repository of tests/gate/fixtures.py GateRepoCase and transcripts planted under a
temp home: no real config folder is read."""
import json
import os
import unittest
from unittest import mock

from hosts.claude.adapter import agent_models as AM
from kitlib.gate import verdicts as V
from tests.gate.fixtures import GateRepoCase
from tests.gate import test_verdict_cli as VC
from tests.home import temp_home

RID = 'a13560c0143b859e7'           # the shape of every Claude subagent id
RID2 = 'a082f4af73b3c82f2'


def records(*models):
    """Transcript records: a user turn, then one assistant turn per model of `models`."""
    out = [{'type': 'user', 'agentId': RID, 'message': {'role': 'user', 'content': 'review it'}}]
    for m in models:
        out.append({'type': 'assistant', 'message': {'role': 'assistant', 'model': m,
                                                     'content': [{'type': 'text', 'text': 'ok'}]}})
    return out


class ReviewerModel(GateRepoCase):
    # VerdictCli's helpers; the module is imported, not the class, so its tests run once
    run_tool = VC.VerdictCli.run_tool
    add_args = VC.VerdictCli.add_args

    def setUp(self):
        VC.VerdictCli.setUp(self)
        self.home = temp_home(self, prefix='kit-reviewer-model-')
        env = mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': os.path.join(self.home, '.claude')})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('PM_KIT_HOST', None)

    def plant(self, rid, *models, folder='.claude', session='sess-1'):
        """A transcript of subagent `rid` whose assistant turns ran on `models`, in config folder
        `folder` of the temp home."""
        d = os.path.join(self.home, folder, 'projects', 'Q--repo', session, 'subagents')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'agent-%s.jsonl' % rid), 'w', encoding='utf-8') as f:
            for r in records(*models):
                f.write(json.dumps(r) + '\n')

    def add(self, **over):
        a = {'--risk': 'High', '--reviewer': 'pm-kit:analyst', '--reviewer-id': RID}
        a.update(over)
        return self.run_tool(*self.add_args(**a))

    def assert_queued(self, got, notes=()):
        code, out, err = got
        self.assertEqual(code, 0, (out, err))
        lines = out.strip().splitlines()
        self.assertEqual(lines[-1], 'QUEUED kit/a %s' % self.tip)
        self.assertEqual([l for l in lines if l.startswith('NOTE')],
                         ['NOTE: --reviewer-model opus is not checked: no transcript found for the '
                          'reviewer id %s' % n for n in notes])

    def assert_refused(self, **over):
        """add(**over) is refused and leaves the queue as it was; returns its stdout."""
        before = V.scan(self.repo.common_dir())
        code, out, err = self.add(**over)
        self.assertEqual(code, 2, (out, err))
        self.assertTrue(out.startswith('REFUSED: --reviewer-model '), out)
        self.assertNotIn('QUEUED', out)
        self.assertEqual(V.scan(self.repo.common_dir()), before)
        return out

    def test_an_opus_verdict_for_a_haiku_review_is_refused(self):
        """The observed mistake: an analyst dispatched with no override ran on haiku, and the
        coordinator typed opus."""
        self.plant(RID, 'claude-haiku-5-5', '<synthetic>', 'claude-haiku-5-5')
        out = self.assert_refused(**{'--reviewer-model': 'opus'})
        self.assertEqual(V.scan(self.repo.common_dir()), ([], []))
        self.assertIn('REFUSED: --reviewer-model opus, but the reviewer %s ran on claude-haiku-5-5 '
                      '(its transcript)' % RID, out)
        self.assertIn('\nInstead: queue it with --reviewer-model haiku, the model it ran on; a '
                      'branch land.py --check-strict calls STRICT needs a review on opus: dispatch '
                      'a fresh analyst with model opus (skill branch-review)', out)

    def test_the_model_it_ran_on_is_queued(self):
        self.plant(RID, 'claude-haiku-5-5')
        for typed in ('haiku', 'claude-haiku-5-5', 'HAIKU'):
            code, out, err = self.add(**{'--reviewer-model': typed})
            self.assertEqual((code, out.strip()), (0, 'QUEUED kit/a %s' % self.tip), (typed, err))
        self.plant(RID2, 'claude-opus-5-5')
        self.assert_queued(self.add(**{'--reviewer-id': RID2}))
        self.assert_refused(**{'--reviewer-id': RID2, '--reviewer-model': 'sonnet'})

    def test_an_agent_id_with_no_transcript_is_queued_with_a_note(self):
        self.assert_queued(self.add(), notes=[RID])
        self.plant(RID, 'claude-opus-5-5')
        self.assert_queued(self.add(**{'--reviewer-id': '%s,%s' % (RID, RID2)}), notes=[RID2])

    def test_a_self_review_or_a_session_name_is_not_looked_up(self):
        self.plant('a' + '0' * 16, 'claude-haiku-5-5')
        self.assert_queued(self.add(**{'--risk': 'Low', '--reviewer': 'pm-kit:coordinator'}))
        self.assert_queued(self.add(**{'--risk': 'Low', '--reviewer-id': 'coord-1'}))  # --session
        self.assert_queued(self.add(**{'--reviewer-id': 'demo-coordinator-b7-r1'}))
        self.assert_queued(self.add(**{'--reviewer-id': 'agent-r1'}))
        # a self-review is not looked up even when its id has a haiku transcript
        self.assert_queued(self.add(**{'--risk': 'Low', '--reviewer': 'pm-kit:coordinator',
                                       '--reviewer-id': 'a' + '0' * 16}))

    def test_transcripts_in_two_config_folders_give_the_union_of_their_models(self):
        self.plant(RID, 'claude-opus-5-5', folder='.claude-account1')
        self.plant(RID, 'claude-opus-5-5', folder='.claude-account2', session='sess-2')
        self.assert_queued(self.add())
        self.plant(RID, 'claude-haiku-5-5', folder='.claude')         # CLAUDE_CONFIG_DIR
        self.assertEqual(AM.models(RID), ['claude-haiku-5-5', 'claude-opus-5-5'])
        fresh = ('\nInstead: the review ran on more than one model (claude-haiku-5-5, '
                 'claude-opus-5-5), which no --reviewer-model can name: dispatch a fresh analyst '
                 'with model %s, the model the verdict needs (opus for a branch land.py '
                 '--check-strict calls STRICT; skill branch-review)')
        out = self.assert_refused()
        self.assertIn('but the reviewer %s ran on claude-haiku-5-5 and claude-opus-5-5' % RID, out)
        self.assertIn(fresh % 'opus', out)
        out = self.assert_refused(**{'--reviewer-model': 'haiku'})   # no model is accepted
        self.assertIn(fresh % 'haiku', out)

    def test_one_of_two_reviewers_on_another_model_is_refused(self):
        self.plant(RID, 'claude-opus-5-5')
        self.plant(RID2, 'claude-sonnet-5-5')
        out = self.assert_refused(**{'--reviewer-id': '%s/%s' % (RID, RID2)})
        self.assertIn('but the reviewer %s ran on claude-sonnet-5-5 (its transcript)' % RID2, out)
        self.assertNotIn('reviewer %s ran' % RID, out)
        self.assertIn('Instead: the review ran on more than one model (claude-opus-5-5, '
                      'claude-sonnet-5-5), which no --reviewer-model can name: dispatch a fresh '
                      'analyst with model opus', out)
        self.assert_refused(**{'--reviewer-id': '%s/%s' % (RID, RID2), '--reviewer-model': 'sonnet'})

    def test_a_host_with_no_reader_checks_nothing(self):
        self.plant(RID, 'claude-haiku-5-5')
        with mock.patch.dict(os.environ, {'PM_KIT_HOST': 'codex'}):
            self.assertIsNone(self.tool.transcript_reader())
            self.assert_queued(self.add())
        self.assertIs(self.tool.transcript_reader(), AM.models)


class AgentModels(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self, prefix='kit-agent-models-')
        self.env = {'CLAUDE_CONFIG_DIR': os.path.join(self.home, '.claude')}
        self.dir = os.path.join(self.home, '.claude', 'projects', 'Q--repo', 's', 'subagents')
        os.makedirs(self.dir)

    def write(self, rid, lines):
        path = os.path.join(self.dir, 'agent-%s.jsonl' % rid)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(''.join(l + '\n' for l in lines))
        return path

    def test_only_an_agent_id_is_looked_up(self):
        self.assertEqual(AM.models(RID, self.env, self.home), [])
        for rid in ('demo-coordinator-b7-r1', 'agent-r1', 'a*', 'A13560C0143B859E7', RID + '0', ''):
            self.assertIsNone(AM.models(rid, self.env, self.home), rid)

    def test_the_read_stops_at_two_models_and_skips_what_is_not_a_turn(self):
        lines = [json.dumps(r) for r in records('claude-sonnet-5-5')]
        lines += ['not json "assistant"', json.dumps(['assistant']),
                  json.dumps({'type': 'assistant', 'message': 'assistant'}),
                  json.dumps({'type': 'user', 'message': {'model': 'claude-haiku-5-5', 'x': 'assistant'}})]
        lines += [json.dumps(r) for r in records('claude-sonnet-5-5', '<synthetic>', 'claude-opus-5-5',
                                                 'claude-haiku-5-5')]
        path = self.write(RID, lines)
        self.assertEqual(AM.file_models(path), {'claude-sonnet-5-5', 'claude-opus-5-5'})
        self.assertEqual(AM.models(RID, self.env, self.home), ['claude-opus-5-5', 'claude-sonnet-5-5'])
        self.assertIsNone(AM.file_models(os.path.join(self.dir, 'gone.jsonl')))

    def test_a_folder_reached_twice_is_read_once(self):
        self.write(RID, [json.dumps(r) for r in records('claude-opus-5-5')])
        dirs = AM.projects_dirs(self.env, self.home)
        self.assertEqual(dirs, [os.path.join(self.home, '.claude', 'projects')])
        self.assertEqual(len(AM.transcripts(RID, self.env, self.home)), 1)


if __name__ == '__main__':
    unittest.main()
