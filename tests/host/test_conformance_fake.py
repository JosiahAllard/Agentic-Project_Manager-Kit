"""A host that is not Claude passes the same conformance suite (tests/host/conformance.py): the
fake host in tests/host/fakehost, registered only in a test copy of hosts.json. It has no ask, no
subagent identity, no MCP and no skills, so each degradation path runs. Nothing in kitlib/,
content/ or hosts/ knows it (docs/new-host.md, the worked example)."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import content as kcontent
from kitlib.host import Decision, header, registry
from tests.host import conformance
from tests.host.fakehost import adapter as fake

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, 'fakehost')
ROOT = conformance.ROOT


def registry_file(folder):
    """A hosts.json in `folder`: the kit's, plus the fake host's entry (fakehost/host.json)."""
    with open(registry.PATH, encoding='utf-8') as f:
        data = json.load(f)
    with open(os.path.join(FAKE, 'host.json'), encoding='utf-8') as f:
        data.update(json.load(f))
    path = os.path.join(folder, 'hosts.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    return path


@contextlib.contextmanager
def registered():
    folder = tempfile.mkdtemp(prefix='fakehost-')
    try:
        with mock.patch.object(registry, 'PATH', registry_file(folder)):
            yield registry.PATH
    finally:
        shutil.rmtree(folder, True)


def write_shell():
    """Re-render the fake host's shell (its agents and plugin.json) after a role change."""
    with registered():
        return kcontent.write(ROOT, [fake.renderer()])


class FakeHostConformance(conformance.HostConformance, unittest.TestCase):
    HOST = fake.NAME
    FIXTURES = os.path.join(FAKE, 'payloads.json')

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.folder = tempfile.mkdtemp(prefix='fakehost-')
        cls.REGISTRY = registry_file(cls.folder)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, True)
        super().tearDownClass()

    def read_output(self, stdout, code):
        if not stdout:
            self.assertEqual(code, 0)
            return 'allow', None
        self.assertEqual(code, 2, 'a block exits 2 on this host')
        out = json.loads(stdout)
        self.assertIs(out['block'], True)
        return 'deny', out['message']

    def read_context(self, stdout):
        return json.loads(stdout)['context']

    def hook_scripts(self, manifests):
        hooks = json.loads(manifests[fake.MANIFEST_REL])['hooks']
        root = fake.PLUGIN_ROOT + '/'
        return [(event, cmd['args'][0][len(root):]) for event, cmd in hooks.items()
                if cmd['args'][0].startswith(root)]

    def session_host(self):
        return fake.FakeSessions(), {'ids': ['s-1', 's-2', 's-3'], 'cwd': ['C:/r', ['s-1', 's-3']],
                                     'loaded': ['s-1']}


class FakeHostSlot(unittest.TestCase):
    """The slot takes the fake host with nothing outside it changed, and its entry works."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(registered())
        stack.enter_context(mock.patch.dict(os.environ))
        for name in ('PM_KIT_ROOT', 'PM_KIT_RELEASE_ID'):
            os.environ.pop(name, None)
        spec = importlib.util.spec_from_file_location(
            'fakehost_run', os.path.join(ROOT, *fake.ENTRY_REL.split('/')))
        self.entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.entry)

    def test_the_kit_registry_does_not_name_it(self):
        with open(os.path.join(ROOT, 'hosts.json'), encoding='utf-8') as f:
            self.assertNotIn(fake.NAME, json.load(f))
        self.assertIn(fake.NAME, registry.names())        # the test copy does
        self.assertEqual(registry.default(), 'claude')    # and leaves the default alone

    def test_pm_kit_host_selects_it(self):
        self.assertEqual(registry.selected({registry.ENV: fake.NAME}), fake.NAME)
        self.assertEqual(registry.host(fake.NAME).profile.worktrees_directory, '.fakehost/worktrees')

    def run_entry(self, event, payload, decide):
        out = io.StringIO()
        with mock.patch('kitlib.guard.decide', decide):
            code = self.entry.main([event], io.StringIO(json.dumps(payload)), out)
        return code, out.getvalue()

    def test_the_entry_judges_a_tool_call(self):
        payload = {'cwd': 'C:/r', 'call': {'name': 'write_file', 'input': {'path': 'C:/r/a.py'}}}
        seen = []

        def decide(action, ctx=None, environ=None, profile=None):
            seen.append((action, profile()))
            return Decision('ask', 'r', 'owner')
        code, out = self.run_entry('before_tool', payload, decide)
        self.assertEqual((code, json.loads(out)), (2, {'block': True, 'message': 'owner'}))
        self.assertEqual(seen[0][0].kind, 'edit')
        self.assertEqual(seen[0][1], registry.host(fake.NAME).profile)

    def test_the_entry_adds_the_kit_header_at_session_begin(self):
        code, out = self.run_entry('session_begin', {'cwd': 'C:/r'}, None)
        self.assertEqual((code, json.loads(out)), (0, {'context': header.text(ROOT)}))

    def test_the_kit_render_tool_is_untouched_by_it(self):
        """render_content.py renders the kit's registry only; the fake renders under the test copy."""
        self.assertEqual(kcontent.drift(ROOT, [fake.renderer()]), [])


if __name__ == '__main__':
    unittest.main()
