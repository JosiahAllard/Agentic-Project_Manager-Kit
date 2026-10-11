"""A stand-in pieces table for the tool_deps tests: the PM kit's _pieces_fixtures.py ran them on
two real tables of a repository's (OLD, before its folder move, and NEW, after it) built by its
pieces library, which is not part of this area. This holds only the pieces those tests name and
only the table methods kitlib.tooldeps and the tests call (path, solution, get, gradle_pieces,
pieces), with the same OLD and NEW folders."""
import copy
import sys

OLD_PIECES = [
    {'id': 'core-api', 'kind': 'gradle', 'gradle': ':core-api', 'dir': 'core-api'},
    {'id': 'Mesh.Protocol.Tests', 'kind': 'dotnet', 'dir': 'mesh/Mesh.Protocol.Tests', 'dotnet': True},
    {'id': 'link', 'kind': 'gradle', 'gradle': ':link', 'dir': 'link'},
    {'id': 'runtime', 'kind': 'gradle', 'gradle': ':runtime', 'dir': 'runtime'},
    {'id': 'Mesh.Control', 'kind': 'dotnet', 'dir': 'mesh/Mesh.Control', 'dotnet': True},
    {'id': 'Mesh.Control.Tests', 'kind': 'dotnet', 'dir': 'mesh/Mesh.Control.Tests', 'dotnet': True},
    {'id': 'control-ops', 'kind': 'asset', 'dir': 'mesh/ops', 'dotnet': True},
    {'id': 'Mesh.Node', 'kind': 'dotnet', 'dir': 'mesh/Mesh.Node', 'dotnet': True},
    {'id': 'Mesh.Node.Tests', 'kind': 'dotnet', 'dir': 'mesh/Mesh.Node.Tests', 'dotnet': True},
    {'id': 'aux-scripts', 'kind': 'gradle', 'gradle': ':aux-scripts', 'dir': 'aux-scripts'},
    {'id': 'trainer', 'kind': 'builder', 'dir': 'trainer'},
    {'id': 'model-sums', 'kind': 'file', 'dir': 'aux-scripts/models/SHA256SUMS'},
]
MOVES = {
    'core-api': 'shared/core-api',
    'Mesh.Protocol.Tests': 'shared/Mesh.Protocol.Tests',
    'link': 'worker/link',
    'runtime': 'worker/runtime',
    'Mesh.Control': 'control/Mesh.Control',
    'Mesh.Control.Tests': 'control/Mesh.Control.Tests',
    'control-ops': 'control/ops',
    'Mesh.Node': 'node/Mesh.Node',
    'Mesh.Node.Tests': 'node/Mesh.Node.Tests',
    'aux-scripts': 'artifacts/scripts/aux-scripts',
    'trainer': 'artifacts/models',
    'model-sums': 'artifacts/models/SHA256SUMS',
}


class Table(object):
    def __init__(self, pieces, solution):
        self.pieces = pieces
        self._by_id = {p['id']: p for p in pieces}
        self._solution = solution

    def get(self, pid):
        return self._by_id[pid]

    def path(self, pid, rel=''):
        """`<dir of pid>/<rel>`; `rel` keeps its trailing `/` (as the pieces library's)."""
        d = self.get(pid)['dir']
        return d + '/' + rel.lstrip('/') if rel else d

    def solution(self):
        return self._solution

    def gradle_pieces(self):
        return [(p['gradle'].lstrip(':'), p['dir']) for p in self.pieces if p['kind'] == 'gradle']


def _moved():
    out = copy.deepcopy(OLD_PIECES)
    for p in out:
        p['was'], p['dir'] = p['dir'], MOVES[p['id']]
    return out


OLD = Table(OLD_PIECES, 'mesh/Mesh.slnx')
NEW = Table(_moved(), 'Mesh.slnx')


def both_tables(cls):
    """Class decorator: `cls` runs under OLD and a `<Name>New` subclass, put into the same module,
    runs every test again under NEW. `self.table` is the table, `self.P(id, rel)` a path in it."""
    cls.table = OLD
    if 'P' not in cls.__dict__:
        cls.P = lambda self, pid, rel='': self.table.path(pid, rel)
    new = type(cls.__name__ + 'New', (cls,), {'table': NEW, '__module__': cls.__module__,
                                               '__doc__': cls.__doc__})
    setattr(sys.modules[cls.__module__], new.__name__, new)
    return cls
