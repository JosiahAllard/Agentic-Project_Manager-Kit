"""A test hosts.json that registers Codex beside the kit's own hosts. Not a test module.

The bridge serves a host only once hosts.json registers it (optional/bridge/registry.py), and on
this branch only Claude is registered until plan step 9 adds Codex. The Codex half's wiring is
still tested end to end in process: `with codex_registered():` points kitlib.host.registry.PATH
at a copy of the kit's hosts.json plus a Codex entry, as tests/host does for its fake host. A
subprocess (the CLI, the MCP frontend) reads the kit's real hosts.json and is not covered by it.
"""
import contextlib
import copy
import json
import os
import tempfile
from unittest import mock

from kitlib.host import registry

CODEX = 'codex'


def codex_entry(base):
    """A valid Codex entry for the registry, derived from another host's entry (test data only;
    step 9's real entry is the one that counts)."""
    entry = copy.deepcopy(base)
    entry.pop('default', None)
    entry['adapter'] = 'hosts.codex.adapter'
    entry['shell'] = 'hosts/codex/shell'
    profile = entry['profile']
    profile.update({'owner_paths': ['AGENTS.md', '.codex/'], 'worktrees_directory': '.codex/worktrees',
                    'state_directory': '.codex', 'process_names': ['codex.exe'],
                    'session_env': 'CODEX_SESSION_ID', 'skills_directory': '.codex/skills',
                    'session_process': 'codex.exe'})
    profile['attribution'] = {'name': 'Codex', 'email': 'noreply@openai.com', 'model_env': None,
                              'model_pattern': None}
    return entry


@contextlib.contextmanager
def codex_registered():
    """The kit's hosts.json with Codex added (unchanged if it already registers Codex)."""
    with open(registry.PATH, encoding='utf-8') as f:
        data = json.load(f)
    if CODEX not in data:
        data[CODEX] = codex_entry(next(iter(data.values())))
    with tempfile.TemporaryDirectory(prefix='bridge-hosts-') as folder:
        path = os.path.join(folder, 'hosts.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        registry.load(path)                      # the fixture itself must be a valid registry
        with mock.patch.object(registry, 'PATH', path):
            yield path
