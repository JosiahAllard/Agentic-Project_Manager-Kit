"""A test hosts.json without Codex. Not a test module.

The bridge serves a host only once hosts.json registers it (optional/bridge/registry.py). The kit's
hosts.json registers Codex (plan step 9), so the Codex half is served for real. To keep the
refusal of an unregistered host covered, `with codex_unregistered():` points
kitlib.host.registry.PATH at a copy of the kit's hosts.json without Codex, as tests/host does for
its fake host. A subprocess (the CLI, the MCP frontend) reads the kit's real hosts.json and is not
covered by it.
"""
import contextlib
import json
import os
import tempfile
from unittest import mock

from kitlib.host import registry

CODEX = 'codex'


@contextlib.contextmanager
def codex_unregistered():
    """The kit's hosts.json with the Codex entry left out."""
    with open(registry.PATH, encoding='utf-8') as f:
        data = json.load(f)
    data.pop(CODEX, None)
    with tempfile.TemporaryDirectory(prefix='bridge-hosts-') as folder:
        path = os.path.join(folder, 'hosts.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        registry.load(path)                      # the fixture itself must be a valid registry
        with mock.patch.object(registry, 'PATH', path):
            yield path
