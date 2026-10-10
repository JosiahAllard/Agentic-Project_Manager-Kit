"""A stand-in pieces table for the tool_deps tests: the PM kit's _pieces_fixtures.py ran them on
two real tables of Retribution's (OLD, before its folder move, and NEW, after it) built by its
pieces library, which is not part of this area. This holds only the pieces those tests name and
only the table methods kitlib.tooldeps and the tests call (path, solution, get, gradle_pieces,
pieces), with the same OLD and NEW folders."""
import copy
import sys

OLD_PIECES = [
    {'id': 'retribution-api', 'kind': 'gradle', 'gradle': ':retribution-api', 'dir': 'retribution-api'},
    {'id': 'Fleet.Protocol.Tests', 'kind': 'dotnet', 'dir': 'fleet/Fleet.Protocol.Tests', 'dotnet': True},
    {'id': 'binding', 'kind': 'gradle', 'gradle': ':binding', 'dir': 'binding'},
    {'id': 'runtime', 'kind': 'gradle', 'gradle': ':runtime', 'dir': 'runtime'},
    {'id': 'Fleet.Brain', 'kind': 'dotnet', 'dir': 'fleet/Fleet.Brain', 'dotnet': True},
    {'id': 'Fleet.Brain.Tests', 'kind': 'dotnet', 'dir': 'fleet/Fleet.Brain.Tests', 'dotnet': True},
    {'id': 'brain-ops', 'kind': 'asset', 'dir': 'fleet/ops', 'dotnet': True},
    {'id': 'Fleet.Node', 'kind': 'dotnet', 'dir': 'fleet/Fleet.Node', 'dotnet': True},
    {'id': 'Fleet.Node.Tests', 'kind': 'dotnet', 'dir': 'fleet/Fleet.Node.Tests', 'dotnet': True},
    {'id': 'combat-scripts', 'kind': 'gradle', 'gradle': ':combat-scripts', 'dir': 'combat-scripts'},
    {'id': 'pvp', 'kind': 'builder', 'dir': 'pvp'},
    {'id': 'model-sums', 'kind': 'file', 'dir': 'combat-scripts/models/SHA256SUMS'},
]
MOVES = {
    'retribution-api': 'shared/retribution-api',
    'Fleet.Protocol.Tests': 'shared/Fleet.Protocol.Tests',
    'binding': 'worker/binding',
    'runtime': 'worker/runtime',
    'Fleet.Brain': 'brain/Fleet.Brain',
    'Fleet.Brain.Tests': 'brain/Fleet.Brain.Tests',
    'brain-ops': 'brain/ops',
    'Fleet.Node': 'node/Fleet.Node',
    'Fleet.Node.Tests': 'node/Fleet.Node.Tests',
    'combat-scripts': 'artifacts/scripts/combat-scripts',
    'pvp': 'artifacts/models',
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


OLD = Table(OLD_PIECES, 'fleet/Fleet.slnx')
NEW = Table(_moved(), 'Fleet.slnx')


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
