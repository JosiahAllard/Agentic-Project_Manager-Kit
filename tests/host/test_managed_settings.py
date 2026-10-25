"""The managed-settings template carries the guard hook matcher the adapter builds: the template and
hosts/claude/adapter/host.py MATCHER must name the same tools."""
import json
import os
import unittest

from hosts.claude.adapter import host

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ManagedSettingsMatcher(unittest.TestCase):
    def test_template_guard_matcher_equals_host_matcher(self):
        path = os.path.join(ROOT, 'hosts', 'claude', 'managed-settings.json')
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        matchers = [e['matcher'] for e in data['hooks']['PreToolUse']]
        self.assertEqual([host.MATCHER], matchers)


if __name__ == '__main__':
    unittest.main()
