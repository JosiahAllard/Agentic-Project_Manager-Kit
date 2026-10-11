"""Tests for kitlib.pluginsource and tools/plugin_refresh.py: synthetic fixtures only (a temp git
repository with a bare remote, temp homes and export roots, a fake claude)."""
import json
import os
import subprocess
import tempfile

from tests import gitfixture

MANIFEST = b'\xef\xbb\xbf{\n  "name": "pm-kit",\n  "version": "0.1.0",\n  "agents": "./claude/agents"\n}\n'
MARKETPLACE = ('{\n  "name": "pm-kit",\n  "owner": {"name": "x"},\n'
               '  "plugins": [{"name": "pm-kit", "source": "./"}]\n}\n')


def _prepare(repo):
    """The kit's shape, as far as the export reads it: plugin.json (with a BOM), marketplace.json
    pointing at the checkout, a tool; committed and pushed."""
    path = os.path.join(repo.root, '.claude-plugin', 'plugin.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(MANIFEST)
    repo.write('.claude-plugin/marketplace.json', MARKETPLACE)
    repo.write('tools/x.py', 'print("x")\n')
    repo.commit('kit shape', '.claude-plugin/plugin.json', '.claude-plugin/marketplace.json', 'tools/x.py')
    repo.git('push', '-q', 'origin', 'main')
    return None


class Kit(object):
    """A kit checkout (TempRepo with a bare remote, pushed), a temp home and a temp export root."""

    def __init__(self, case):
        self.repo = gitfixture.TempRepo(bare_remote=True, prepare=_prepare,
                                        prepare_key='pluginsource-kit-v1')
        case.addCleanup(self.repo.cleanup)
        d = tempfile.mkdtemp(prefix='kit-plugin-')
        case.addCleanup(gitfixture.rmtree, d)
        self.base = d
        self.home = os.path.join(d, 'home')
        self.root = os.path.join(d, 'export')
        os.makedirs(self.home)
        self.kit = self.repo.root

    def folders(self, *names):
        out = []
        for n in names:
            p = os.path.join(self.home, n)
            os.makedirs(p, exist_ok=True)
            out.append(p)
        return out

    def seed_migrated(self, folders):
        """A state.json that records each of `folders` as migrated, and no export (as after a
        migrate whose export the next refresh replaces)."""
        from kitlib import pluginsource
        os.makedirs(self.root, exist_ok=True)
        pluginsource.write_state(self.root, {'folders': {f: {'migrated': True} for f in folders}})

    def push_from_clone(self, name='elsewhere.txt'):
        """A commit pushed to the remote from another clone: the kit has not fetched it."""
        other = os.path.join(self.base, 'other')
        self.repo.git('clone', '-q', '-b', 'main', self.repo.remote, other, cwd=self.base)
        with open(os.path.join(other, '.git', 'config'), 'a', encoding='utf-8', newline='\n') as fh:
            fh.write('[user]\n\tname = "%s"\n\temail = "%s"\n' % (gitfixture.NAME, gitfixture.EMAIL))
        self.repo.write(name, 'y\n', root=other)
        self.repo.git('add', '--', name, cwd=other)
        self.repo.git('commit', '-q', '-m', 'elsewhere', cwd=other)
        self.repo.git('push', '-q', 'origin', 'main', cwd=other)
        return self.repo.git('rev-parse', 'refs/heads/main', cwd=other)


class FakeProc(object):
    """subprocess.run for claude.Runner: records (args, CLAUDE_CONFIG_DIR or None), answers rc."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}          # (folder basename, subcommand word) -> rc

    def __call__(self, cmd, env=None, **_kw):
        args = list(cmd[1:])
        self.calls.append((args, env.get('CLAUDE_CONFIG_DIR')))
        folder = env.get('CLAUDE_CONFIG_DIR') or '<default>'
        rc = self.fail.get((os.path.basename(folder), args[1] if len(args) > 1 else ''), 0)
        return subprocess.CompletedProcess(cmd, rc, b'fake output\n')


def read_json(path):
    with open(path, encoding='utf-8-sig') as fh:
        return json.load(fh)
