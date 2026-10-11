"""Two path-to-piece tables for tests: OLD, the layout before the restructure, and NEW,
the layout after it. A pure test of a tool that reads a pieces table runs under BOTH (`TABLES`,
`both_tables`), naming paths as `self.P('aux-scripts', 'src/test/...')`; so moving a folder
edits the real json and no test.

OLD is frozen: it is a pieces table as phase 1 wrote it, and must never follow the real file.
NEW is OLD with MOVES and then SPLITS applied - the one place the target layout is written.
The ids and folders are invented; the shape is a repository with gradle, dotnet and spec pieces.
"""
import copy
import sys

from kitlib import pieces

OLD_DATA = {
    'schema': 1,
    'root': ['settings.gradle.kts', 'gradle.properties', 'gradlew', 'gradlew.bat', 'gradle',
             'buildSrc', '.gitattributes', '.gitignore', 'CLAUDE.md', '.claude', 'docs', 'tools'],
    'dotnet': {'solution': 'mesh/Mesh.slnx',
               'root_files': ['mesh/Mesh.slnx', 'mesh/global.json', 'mesh/Directory.Build.props',
                              'mesh/.gitignore']},
    'pieces': [
        {'id': 'core-api', 'group': 'shared', 'kind': 'gradle', 'gradle': ':core-api',
         'dir': 'core-api'},
        {'id': 'protocol', 'group': 'shared', 'kind': 'spec', 'dir': 'protocol', 'wire': True,
         'forces': ['worker']},
        {'id': 'Mesh.Protocol', 'group': 'shared', 'kind': 'dotnet', 'dir': 'mesh/Mesh.Protocol', 'dotnet': True},
        {'id': 'Mesh.Protocol.Tests', 'group': 'shared', 'kind': 'dotnet', 'dir': 'mesh/Mesh.Protocol.Tests',
         'dotnet': True},
        {'id': 'worker', 'group': 'worker', 'kind': 'gradle', 'gradle': ':worker', 'dir': 'worker',
         'outputs': {'install': 'build/install/worker/bin/worker.bat'}},
        {'id': 'link', 'group': 'worker', 'kind': 'gradle', 'gradle': ':link', 'dir': 'link'},
        {'id': 'runtime', 'group': 'worker', 'kind': 'gradle', 'gradle': ':runtime', 'dir': 'runtime'},
        {'id': 'worker-image', 'group': 'worker', 'kind': 'docker', 'dir': 'deploy/worker-image'},
        {'id': 'Mesh.Control', 'group': 'control', 'kind': 'dotnet', 'dir': 'mesh/Mesh.Control', 'dotnet': True},
        {'id': 'Mesh.Control.Tests', 'group': 'control', 'kind': 'dotnet', 'dir': 'mesh/Mesh.Control.Tests',
         'dotnet': True},
        {'id': 'ext-link', 'group': 'control', 'kind': 'asset', 'dir': 'mesh/ext-link', 'dotnet': True},
        {'id': 'ext-companion', 'group': 'control', 'kind': 'asset', 'dir': 'mesh/ext-companion',
         'dotnet': True},
        {'id': 'control-ops', 'group': 'control', 'kind': 'asset', 'dir': 'mesh/ops', 'dotnet': True},
        {'id': 'control-readme', 'group': 'control', 'kind': 'docs', 'dir': 'mesh/README.md', 'dotnet': True},
        {'id': 'control-docs', 'group': 'control', 'kind': 'docs', 'dir': 'mesh/docs', 'dotnet': True},
        {'id': 'control-live', 'group': 'control', 'kind': 'docs', 'dir': 'mesh/LIVE.md', 'dotnet': True,
         'owner_only': True},
        {'id': 'Mesh.Node', 'group': 'node', 'kind': 'dotnet', 'dir': 'mesh/Mesh.Node', 'dotnet': True},
        {'id': 'Mesh.Node.Core', 'group': 'node', 'kind': 'dotnet', 'dir': 'mesh/Mesh.Node.Core', 'dotnet': True},
        {'id': 'Mesh.Node.Tests', 'group': 'node', 'kind': 'dotnet', 'dir': 'mesh/Mesh.Node.Tests',
         'dotnet': True},
        {'id': 'Mesh.StubNode', 'group': 'node', 'kind': 'dotnet', 'dir': 'mesh/Mesh.StubNode', 'dotnet': True},
        {'id': 'Mesh.StubWorker', 'group': 'node', 'kind': 'dotnet', 'dir': 'mesh/Mesh.StubWorker',
         'dotnet': True},
        {'id': 'Mesh.FakeDocker', 'group': 'node', 'kind': 'dotnet', 'dir': 'mesh/Mesh.FakeDocker',
         'dotnet': True},
        {'id': 'node-docs', 'group': 'node', 'kind': 'docs', 'dir': 'mesh/docs/node.md', 'dotnet': True},
        {'id': 'routegraph', 'group': 'artifacts', 'artifact': 'routemap', 'kind': 'gradle',
         'gradle': ':routegraph', 'dir': 'routegraph'},
        {'id': 'scripts', 'group': 'artifacts', 'artifact': 'scripts', 'kind': 'gradle', 'gradle': ':scripts',
         'dir': 'scripts', 'outputs': {'libs': 'build/libs'}},
        {'id': 'aux-scripts', 'group': 'artifacts', 'artifact': 'scripts', 'kind': 'gradle',
         'gradle': ':aux-scripts', 'dir': 'aux-scripts', 'outputs': {'libs': 'build/libs'}},
        {'id': 'trainer', 'group': 'artifacts', 'artifact': 'models', 'kind': 'builder', 'dir': 'trainer'},
        {'id': 'model-sums', 'group': 'artifacts', 'artifact': 'models', 'kind': 'file',
         'dir': 'aux-scripts/models/SHA256SUMS'},
        {'id': 'plans', 'group': 'artifacts', 'artifact': 'plans', 'kind': 'asset', 'dir': 'artifacts/plans', 'dotnet': True},
    ],
}

# The placements the move applied. Change a line here and every both_tables test follows.
MOVES = {
    'core-api': 'shared/core-api',
    'protocol': 'shared/protocol',
    'Mesh.Protocol': 'shared/Mesh.Protocol',
    'Mesh.Protocol.Tests': 'shared/Mesh.Protocol.Tests',
    'worker': 'worker/worker',
    'link': 'worker/link',
    'runtime': 'worker/runtime',
    'worker-image': 'worker/image',
    'Mesh.Control': 'control/Mesh.Control',
    'Mesh.Control.Tests': 'control/Mesh.Control.Tests',
    'ext-link': 'control/ext-link',
    'ext-companion': 'control/ext-companion',
    'control-ops': 'control/ops',
    'control-readme': 'control/README.md',
    'control-docs': 'control/docs',
    'control-live': 'control/LIVE.md',
    'Mesh.Node': 'node/Mesh.Node',
    'Mesh.Node.Core': 'node/Mesh.Node.Core',
    'Mesh.Node.Tests': 'node/Mesh.Node.Tests',
    'Mesh.StubNode': 'node/Mesh.StubNode',
    'Mesh.StubWorker': 'node/Mesh.StubWorker',
    'Mesh.FakeDocker': 'node/Mesh.FakeDocker',
    'node-docs': 'node/docs/node.md',
    'routegraph': 'artifacts/routemap',
    'scripts': 'artifacts/scripts/scripts',
    'aux-scripts': 'artifacts/scripts/aux-scripts',
    'trainer': 'artifacts/models',
    'model-sums': 'artifacts/models/SHA256SUMS',
}
# Split case: one old piece became two. The moved folder keeps its `was` under
# the new id (the map builder, artifacts/routemap); the routing library took the old id to a new
# folder and has no `was` - one old folder cannot repath to two. OLD_ID maps a new id back.
SPLITS = {'routegraph': {'id': 'routemap', 'gradle': ':routemap',
                         'outputs': {'install': 'build/install/routemap/bin/routemap.bat'}}}
ADDED = (('runtime', {'id': 'routegraph', 'group': 'worker', 'kind': 'gradle', 'gradle': ':routegraph',
                      'dir': 'worker/routegraph'}),)    # (inserted after, piece)
OLD_ID = {v['id']: k for k, v in SPLITS.items()}

DOTNET_ROOT_MOVES = {                                # the solution files go to the repo root
    'mesh/Mesh.slnx': 'Mesh.slnx',
    'mesh/global.json': 'global.json',
    'mesh/Directory.Build.props': 'Directory.Build.props',
    'mesh/.gitignore': '.gitignore',                 # merged into the root .gitignore: not a dotnet file
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
