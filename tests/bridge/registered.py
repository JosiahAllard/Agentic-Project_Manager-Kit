"""Test peers.json files (without Codex, without refused pairs) and a writer. Not a test module.

The bridge serves a peer only once optional/bridge/peers.json lists it (optional/bridge/registry.py).
The kit's peers.json lists Codex, so the Codex half is served for real. To keep the refusal of an
unlisted peer covered, `with codex_unlisted():` points registry.PEERS at a copy of the kit's
peers.json without Codex (and without any refused pair naming it). `with no_refused_pairs():`
keeps every peer and refuses no pair, for a fixture that needs a queued send between two
threads of one host (Codex to Codex) to test something else. A subprocess (the CLI, the MCP
frontend) reads the kit's real peers.json and is not covered by it.
"""
import contextlib
import json
import os
import tempfile
from unittest import mock

from optional.bridge import registry

CODEX = 'codex'


def write_peers(path, peers, refused=()):
    """Write a peers.json at `path` and check it reads back as a valid rule."""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'peers': list(peers), 'refused': [list(pair) for pair in refused]}, f)
    registry.read_peers(path)
    return path


@contextlib.contextmanager
def codex_unlisted():
    """The kit's peers.json with Codex left out."""
    peers, refused = registry.read_peers()
    with tempfile.TemporaryDirectory(prefix='bridge-peers-') as folder:
        path = write_peers(os.path.join(folder, 'peers.json'), [p for p in peers if p != CODEX],
                           [pair for pair in refused if CODEX not in pair])
        with mock.patch.object(registry, 'PEERS', path):
            yield path


@contextlib.contextmanager
def no_refused_pairs():
    """The kit's peers.json with every peer and no refused pair."""
    peers, _ = registry.read_peers()
    with tempfile.TemporaryDirectory(prefix='bridge-peers-') as folder:
        path = write_peers(os.path.join(folder, 'peers.json'), peers)
        with mock.patch.object(registry, 'PEERS', path):
            yield path
