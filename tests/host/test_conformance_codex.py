"""Codex passes the host conformance suite (tests/host/conformance.py) on its payloads
(tests/host/codex/payloads.json) and its app-server reads (tests/host/codex/sessions.json). Both are
in the shapes the Codex branch's tests recorded, not captures from an installed client
(hosts/codex/README.md, "Not verified")."""
import json
import os
import unittest

from tests.host import bridge_conformance, conformance
from tests.host.test_leaks import ALLOWED

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = '${PLUGIN_ROOT}/'
SHELL = ''                      # the plugin root is the export root, so ${PLUGIN_ROOT}/ maps to the kit root


def recorded_request(path=os.path.join(HERE, 'codex', 'sessions.json')):
    """A read-only app-server request callable over the recorded responses: thread/list pages by
    cursor, filtered by cwd as the server does; the other reads as recorded."""
    with open(path, encoding='utf-8') as f:
        data = json.load(f)

    def request(method, params):
        if method != 'thread/list':
            return data[method]
        pages = data['thread/list']
        index = 0 if not params.get('cursor') else 1 + [p['nextCursor'] for p in pages].index(params['cursor'])
        page = dict(pages[index])
        if params.get('cwd') is not None:
            page['data'] = [t for t in page['data'] if t.get('cwd') == params['cwd']]
        return page
    return request


class CodexConformance(conformance.HostConformance, bridge_conformance.mixin('codex'),
                       unittest.TestCase):
    HOST = 'codex'
    FIXTURES = os.path.join(HERE, 'codex', 'payloads.json')
    # Upper bounds on 'codex' where section 2.7 forbids it: test_leaks counts every host's name file by
    # file, exactly; these are its Codex-only lines. Shrink only.
    LEAK_ALLOWED = {
        'kitlib/commit/__init__.py': 1,          # an example family in an error message
        'kitlib/kitrelease/bootstrap.py': 1,     # the release's excluded host folders, before kitlib runs
        'kitlib/launcher/usage.py': 9,           # the launcher wave: Codex's usage line
        'kitlib/pmwatch/__init__.py': 1,         # the usage line's codex account
    }

    def read_output(self, stdout, code):
        self.assertEqual(code, 0, 'Codex reads the verdict from the JSON')
        if not stdout:
            return 'allow', None
        out = json.loads(stdout)['hookSpecificOutput']
        self.assertNotEqual(out['permissionDecision'], 'ask', 'Codex has no ask')
        return out['permissionDecision'], out['permissionDecisionReason']

    def read_context(self, stdout):
        return json.loads(stdout)['hookSpecificOutput']['additionalContext']

    def hook_scripts(self, manifests):
        """hooks.json's hooks: every command runs ${PLUGIN_ROOT}/boot/hook.py codex <event>, the
        kit's one entry, in the export Codex installs."""
        from hosts.codex.adapter import content
        out = []
        for event, groups in json.loads(manifests[content.HOOKS_REL])['hooks'].items():
            for group in groups:
                for hook in group['hooks']:
                    words = hook['command'].split(' ')
                    script = words[1].strip('"')
                    self.assertTrue(script.startswith(PLUGIN_ROOT), script)
                    out.append((event, SHELL + script[len(PLUGIN_ROOT):], words[2:]))
        return out

    def session_host(self):
        from hosts.codex.adapter import sessions
        return sessions.SessionHost(recorded_request()), {
            'ids': ['t-1', 't-2', 't-3'], 'cwd': ['C:/r', ['t-1']], 'loaded': ['t-1']}


class LeakCounts(unittest.TestCase):
    def test_the_allowances_match_test_leaks(self):
        for path, n in CodexConformance.LEAK_ALLOWED.items():
            if path.startswith('kitlib/'):
                self.assertLessEqual(n, ALLOWED.get(path, 0), path)


if __name__ == '__main__':
    unittest.main()
