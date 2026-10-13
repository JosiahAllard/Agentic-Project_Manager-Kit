"""The MCP tool prefix and the qualified agent name come from hosts.json (profile mcp_tool_format
and agent_name_format), filled with the kit plugin's name: on the kit's registry they are what
kitlib spelled before, and another host's formats give that host's names."""
import copy
import json
import os
import shutil
import tempfile
import unittest

from kitlib import agentname, lockmcp

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Names(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='names-hosts-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        with open(os.path.join(ROOT, 'hosts.json'), encoding='utf-8') as f:
            data = json.load(f)
        other = copy.deepcopy(data['claude'])
        other.pop('default')
        other['profile']['mcp_tool_format'] = '{server}.{tool}'
        other['profile']['agent_name_format'] = '{plugin}/{name}'
        data['other'] = other
        self.path = os.path.join(self.dir, 'hosts.json')
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def test_the_kit_registry_gives_todays_names(self):
        self.assertEqual(lockmcp.tool_prefix(), 'mcp__plugin_pm-kit_machine-lock__')
        self.assertEqual(lockmcp.claude_tool_prefix(), lockmcp.tool_prefix())
        self.assertEqual(lockmcp.CLAUDE_PLUGIN_NAME, lockmcp.PLUGIN_NAME)
        self.assertEqual(agentname.PREFIX, 'pm-kit:')
        self.assertEqual(agentname.prefix(), agentname.PREFIX)

    def test_another_hosts_formats_give_its_names(self):
        self.assertEqual(lockmcp.tool_prefix('other', path=self.path), 'machine-lock.')
        self.assertEqual(lockmcp.tool_prefix('claude', 'p', 's', path=self.path), 'mcp__plugin_p_s__')
        self.assertEqual(agentname.prefix('other', path=self.path), 'pm-kit/')

    def test_session_processes_are_every_hosts(self):
        from kitlib import pmstart
        self.assertEqual(pmstart.session_images(), frozenset({'claude.exe'}))
        with open(self.path, encoding='utf-8') as f:
            data = json.load(f)
        data['other']['profile']['session_process'] = 'other.exe'
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        self.assertEqual(pmstart.session_images(self.path), frozenset({'claude.exe', 'other.exe'}))
        procs = [{'Name': 'other.exe', 'CommandLine': 'other.exe --remote-control x'}]
        self.assertEqual(pmstart.live_session_names(procs), set())   # not registered in the kit's


if __name__ == '__main__':
    unittest.main()
