"""Two path-to-piece tables for tests: OLD, the layout before the restructure, and NEW,
the layout after it. A pure test of a tool that reads a pieces table runs under BOTH (`TABLES`,
`both_tables`), naming paths as `self.P('combat-scripts', 'src/test/...')`; so moving a folder
edits the real json and no test.

OLD is frozen: it is a pieces table as phase 1 wrote it, and must never follow the real file.
NEW is OLD with MOVES and then SPLITS applied - the one place the target layout is written.
"""
import copy
import sys

from kitlib import pieces

OLD_DATA = {
    'schema': 1,
    'root': ['settings.gradle.kts', 'gradle.properties', 'gradlew', 'gradlew.bat', 'gradle',
             'buildSrc', '.gitattributes', '.gitignore', 'CLAUDE.md', '.claude', 'docs', 'tools'],
    'dotnet': {'solution': 'fleet/Fleet.slnx',
               'root_files': ['fleet/Fleet.slnx', 'fleet/global.json', 'fleet/Directory.Build.props',
                              'fleet/.gitignore']},
    'pieces': [
        {'id': 'beta-api', 'group': 'shared', 'kind': 'gradle', 'gradle': ':beta-api',
         'dir': 'beta-api'},
        {'id': 'protocol', 'group': 'shared', 'kind': 'spec', 'dir': 'protocol', 'wire': True,
         'forces': ['worker']},
        {'id': 'Fleet.Protocol', 'group': 'shared', 'kind': 'dotnet', 'dir': 'fleet/Fleet.Protocol', 'dotnet': True},
        {'id': 'Fleet.Protocol.Tests', 'group': 'shared', 'kind': 'dotnet', 'dir': 'fleet/Fleet.Protocol.Tests',
         'dotnet': True},
        {'id': 'worker', 'group': 'worker', 'kind': 'gradle', 'gradle': ':worker', 'dir': 'worker',
         'outputs': {'install': 'build/install/worker/bin/worker.bat'}},
        {'id': 'binding', 'group': 'worker', 'kind': 'gradle', 'gradle': ':binding', 'dir': 'binding'},
        {'id': 'runtime', 'group': 'worker', 'kind': 'gradle', 'gradle': ':runtime', 'dir': 'runtime'},
        {'id': 'worker-image', 'group': 'worker', 'kind': 'docker', 'dir': 'deploy/worker-image'},
        {'id': 'Fleet.Brain', 'group': 'brain', 'kind': 'dotnet', 'dir': 'fleet/Fleet.Brain', 'dotnet': True},
        {'id': 'Fleet.Brain.Tests', 'group': 'brain', 'kind': 'dotnet', 'dir': 'fleet/Fleet.Brain.Tests',
         'dotnet': True},
        {'id': 'firefox-link', 'group': 'brain', 'kind': 'asset', 'dir': 'fleet/firefox-link', 'dotnet': True},
        {'id': 'browser-companion', 'group': 'brain', 'kind': 'asset', 'dir': 'fleet/browser-companion',
         'dotnet': True},
        {'id': 'brain-ops', 'group': 'brain', 'kind': 'asset', 'dir': 'fleet/ops', 'dotnet': True},
        {'id': 'brain-readme', 'group': 'brain', 'kind': 'docs', 'dir': 'fleet/README.md', 'dotnet': True},
        {'id': 'brain-docs', 'group': 'brain', 'kind': 'docs', 'dir': 'fleet/docs', 'dotnet': True},
        {'id': 'brain-live', 'group': 'brain', 'kind': 'docs', 'dir': 'fleet/LIVE.md', 'dotnet': True,
         'owner_only': True},
        {'id': 'Fleet.Node', 'group': 'node', 'kind': 'dotnet', 'dir': 'fleet/Fleet.Node', 'dotnet': True},
        {'id': 'Fleet.Node.Core', 'group': 'node', 'kind': 'dotnet', 'dir': 'fleet/Fleet.Node.Core', 'dotnet': True},
        {'id': 'Fleet.Node.Tests', 'group': 'node', 'kind': 'dotnet', 'dir': 'fleet/Fleet.Node.Tests',
         'dotnet': True},
        {'id': 'Fleet.StubNode', 'group': 'node', 'kind': 'dotnet', 'dir': 'fleet/Fleet.StubNode', 'dotnet': True},
        {'id': 'Fleet.StubWorker', 'group': 'node', 'kind': 'dotnet', 'dir': 'fleet/Fleet.StubWorker',
         'dotnet': True},
        {'id': 'Fleet.FakeDocker', 'group': 'node', 'kind': 'dotnet', 'dir': 'fleet/Fleet.FakeDocker',
         'dotnet': True},
        {'id': 'node-docs', 'group': 'node', 'kind': 'docs', 'dir': 'fleet/docs/node.md', 'dotnet': True},
        {'id': 'worldgraph', 'group': 'artifacts', 'artifact': 'worldmap', 'kind': 'gradle',
         'gradle': ':worldgraph', 'dir': 'worldgraph'},
        {'id': 'scripts', 'group': 'artifacts', 'artifact': 'scripts', 'kind': 'gradle', 'gradle': ':scripts',
         'dir': 'scripts', 'outputs': {'libs': 'build/libs'}},
        {'id': 'combat-scripts', 'group': 'artifacts', 'artifact': 'scripts', 'kind': 'gradle',
         'gradle': ':combat-scripts', 'dir': 'combat-scripts', 'outputs': {'libs': 'build/libs'}},
        {'id': 'pvp', 'group': 'artifacts', 'artifact': 'models', 'kind': 'builder', 'dir': 'pvp'},
        {'id': 'model-sums', 'group': 'artifacts', 'artifact': 'models', 'kind': 'file',
         'dir': 'combat-scripts/models/SHA256SUMS'},
        {'id': 'plans', 'group': 'artifacts', 'artifact': 'plans', 'kind': 'asset', 'dir': 'artifacts/plans', 'dotnet': True},
    ],
}

# The owner's placements (answering the move plan's questions 1-7); the move
# applied them. Change a line here and every both_tables test follows.
MOVES = {
    'beta-api': 'shared/beta-api',
    'protocol': 'shared/protocol',
    'Fleet.Protocol': 'shared/Fleet.Protocol',
    'Fleet.Protocol.Tests': 'shared/Fleet.Protocol.Tests',
    'worker': 'worker/worker',                       # Q1
    'binding': 'worker/binding',
    'runtime': 'worker/runtime',
    'worker-image': 'worker/image',                  # Q1
    'Fleet.Brain': 'brain/Fleet.Brain',
    'Fleet.Brain.Tests': 'brain/Fleet.Brain.Tests',
    'firefox-link': 'brain/firefox-link',
    'browser-companion': 'brain/browser-companion',
    'brain-ops': 'brain/ops',
    'brain-readme': 'brain/README.md',               # Q6
    'brain-docs': 'brain/docs',                      # Q6
    'brain-live': 'brain/LIVE.md',                   # Q8
    'Fleet.Node': 'node/Fleet.Node',
    'Fleet.Node.Core': 'node/Fleet.Node.Core',
    'Fleet.Node.Tests': 'node/Fleet.Node.Tests',
    'Fleet.StubNode': 'node/Fleet.StubNode',
    'Fleet.StubWorker': 'node/Fleet.StubWorker',     # Q7
    'Fleet.FakeDocker': 'node/Fleet.FakeDocker',     # Q7
    'node-docs': 'node/docs/node.md',                # Q6
    'worldgraph': 'artifacts/worldmap',              # Q2
    'scripts': 'artifacts/scripts/scripts',          # Q4
    'combat-scripts': 'artifacts/scripts/combat-scripts',   # Q4
    'pvp': 'artifacts/models',                       # Q3
    'model-sums': 'artifacts/models/SHA256SUMS',     # Q3
}
# Move phase 5b (WG-SPLIT): one old piece became two. The moved folder keeps its `was` under
# the new id (the map builder, artifacts/worldmap); the routing library took the old id to a new
# folder and has no `was` - one old folder cannot repath to two. OLD_ID maps a new id back.
SPLITS = {'worldgraph': {'id': 'worldmap', 'gradle': ':worldmap',
                         'outputs': {'install': 'build/install/worldmap/bin/worldmap.bat'}}}
ADDED = (('runtime', {'id': 'worldgraph', 'group': 'worker', 'kind': 'gradle', 'gradle': ':worldgraph',
                      'dir': 'worker/worldgraph'}),)    # (inserted after, piece)
OLD_ID = {v['id']: k for k, v in SPLITS.items()}

DOTNET_ROOT_MOVES = {                                # Q5: the solution files go to the repo root
    'fleet/Fleet.slnx': 'Fleet.slnx',
    'fleet/global.json': 'global.json',
    'fleet/Directory.Build.props': 'Directory.Build.props',
    'fleet/.gitignore': '.gitignore',                # merged into the root .gitignore: not a dotnet file
}


def _new_data():
    data = copy.deepcopy(OLD_DATA)
    for p in data['pieces']:
        if p['id'] in MOVES:
            p['was'], p['dir'] = p['dir'], MOVES[p['id']]
    files = [DOTNET_ROOT_MOVES[f] for f in data['dotnet']['root_files']]
    data['dotnet'] = {'solution': DOTNET_ROOT_MOVES[data['dotnet']['solution']],
                      'root_files': [f for f in files if f not in data['root']],
                      'was': dict(DOTNET_ROOT_MOVES)}
    data['root'] = data['root'] + ['buildscripts']   # phase 4: the per-piece build entry points
    for p in data['pieces']:                         # phase 5b
        if p['id'] in SPLITS:
            p.update(SPLITS[p['id']])
    for after, piece in ADDED:
        at = next(i for i, p in enumerate(data['pieces']) if p['id'] == after)
        data['pieces'].insert(at + 1, copy.deepcopy(piece))
    return data


NEW_DATA = _new_data()
OLD = pieces.load(OLD_DATA)
NEW = pieces.load(NEW_DATA)
TABLES = (('old', OLD), ('new', NEW))


def both_tables(cls):
    """Class decorator for a pure test class: `cls` itself runs under OLD, and a `<Name>New`
    subclass, put into the same module (so the loader collects it), runs every test again under
    NEW. In a test, `self.table` is the table and `self.P(id, rel)` a path in it; pass
    `pieces=self.table` to the tool under test."""
    cls.table = OLD
    if 'P' not in cls.__dict__:
        cls.P = lambda self, pid, rel='': self.table.path(pid, rel)
    new = type(cls.__name__ + 'New', (cls,), {'table': NEW, '__module__': cls.__module__,
                                               '__doc__': cls.__doc__})
    setattr(sys.modules[cls.__module__], new.__name__, new)
    return cls
