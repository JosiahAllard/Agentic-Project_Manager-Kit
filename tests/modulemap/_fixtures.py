"""Fixture trees for the module map tests: files written into a fresh temp folder."""
import json
import os
import shutil
import tempfile

from kitlib.modulemap import check as C
from kitlib.modulemap.load import ModuleMap

MAP = {
    'modules': {
        'brain': {'paths': ['brain']},
        'node': {'paths': ['node']},
        'protocol': {'paths': ['shared/protocol', 'shared/Fleet.Protocol*']},
        'worker': {'paths': ['worker']},
    },
    'messaging': ['protocol'],
}


class Tree(object):
    def __init__(self, files=None):
        self.root = tempfile.mkdtemp(prefix='kit-modmap-')
        for rel, text in (files or {}).items():
            self.write(rel, text)

    def write(self, rel, text):
        path = os.path.join(self.root, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text if isinstance(text, str) else json.dumps(text))
        return path

    def files(self):
        out = []
        for d, _, names in os.walk(self.root):
            for n in names:
                out.append(os.path.relpath(os.path.join(d, n), self.root).replace(os.sep, '/'))
        return sorted(out)

    def read(self):
        return C.reader(self.root)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def mapped(extra=None):
    data = json.loads(json.dumps(MAP))
    data.update(extra or {})
    return ModuleMap(data)


def csproj(*items):
    return '<Project Sdk="Microsoft.NET.Sdk">\n  <ItemGroup>\n%s\n  </ItemGroup>\n</Project>\n' % (
        '\n'.join('    ' + i for i in items))
