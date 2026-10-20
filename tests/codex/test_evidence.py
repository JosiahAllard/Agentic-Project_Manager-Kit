import unittest

from hosts.codex.adapter.evidence import inspect
from hosts.codex.adapter.runtime import RoleBindings
from tests.codex.test_runtime import ROOT, snapshot
from types import SimpleNamespace
from unittest import mock
from kitlib.git import repo


class HookIdentityEvidence(unittest.TestCase):
    def setUp(self):
        self.registry = RoleBindings(mock.Mock(), classify=lambda _: SimpleNamespace(kind=repo.LINKED))
        self.root = self.registry.add_root(snapshot(), 'worker')
        self.child = self.registry._capture(snapshot('kit-analyst', True, 'readOnly'), 'analyst', 'parent')

    def event(self, **extra):
        return dict(hook_event_name='PreToolUse', session_id='session', cwd=ROOT, model='gpt-6.1-sol', **extra)

    def test_parent_session_model_and_checkout_cannot_identify_a_child(self):
        result = inspect(self.event(), self.child)
        self.assertFalse(result.identity_matches)
        self.assertTrue(any('calling child identifier' in value for value in result.blockers))

    def test_same_payload_cannot_prove_root_origin_either(self):
        result = inspect(self.event(), self.root)
        self.assertFalse(result.identity_matches)
        self.assertTrue(any('also covers children' in value for value in result.blockers))

    def test_root_tool_hooks_require_exact_nonempty_calling_thread_identifier(self):
        for name in ('PreToolUse', 'PostToolUse'):
            for fields in ({}, {'agent_id': None}, {'agent_id': ''}, {'agent_id': 'child'}, {'agent_id': False}):
                event = self.event(**fields)
                event['hook_event_name'] = name
                with self.subTest(event=name, fields=fields):
                    result = inspect(event, self.root)
                    self.assertFalse(result.identity_matches)
                    self.assertTrue(any('exact bound calling thread identifier' in value for value in result.blockers))

    def test_exact_root_tool_hook_identifier_still_requires_authenticated_transport(self):
        for name in ('PreToolUse', 'PostToolUse'):
            event = self.event(agent_id=self.root.thread)
            event['hook_event_name'] = name
            result = inspect(event, self.root)
            self.assertTrue(result.identity_matches)
            self.assertEqual(len(result.blockers), 1)
            self.assertIn('authenticated runtime-to-hook', result.blockers[0])

    def test_exact_child_field_is_structural_evidence_without_activation(self):
        result = inspect(self.event(agent_id='child'), self.child)
        self.assertTrue(result.identity_matches)
        self.assertEqual(result.role, 'analyst')
        self.assertEqual(len(result.blockers), 1)
        self.assertIn('authenticated runtime-to-hook', result.blockers[0])

    def test_mismatch_never_uses_profile_name_or_inherited_worker_mark(self):
        result = inspect(self.event(agent_id='parent', agent_type='kit-analyst', PM_KIT_ROLE='worker'), self.child)
        self.assertFalse(result.identity_matches)
        self.assertEqual(result.role, 'analyst')

    def test_evidence_report_does_not_retain_sensitive_tool_arguments(self):
        event = self.event(agent_id='child', tool_input={'secret': 'do not retain'}, transcript_path='private-path')
        result = inspect(event, self.child)
        self.assertNotIn('do not retain', repr(result))
        self.assertNotIn('private-path', repr(result))
