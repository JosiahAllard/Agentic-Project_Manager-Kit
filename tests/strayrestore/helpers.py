"""A TempRepo whose branches are committed in linked worktrees outside the main checkout, so the
main checkout's own status holds only the strays a test writes."""
import json
import os
import unittest

from tests import gitfixture

ENV = {}        # an unmarked session: the owner's


class Fixture(unittest.TestCase):

    def setUp(self):
        self.r = gitfixture.TempRepo()
        self.addCleanup(self.r.cleanup)
        self.top = self.r.root
        self.n = 0

    def branch(self, name, files, detach=False):
        """Commit `files` ({path: bytes}) on a new branch `name` (detach: on no branch) in a
        linked worktree outside the main checkout; returns (commit, worktree path)."""
        self.n += 1
        wt = os.path.join(self.r.base, 'wt%d' % self.n)
        if detach:
            self.r.git('worktree', 'add', '-q', '--detach', wt)
        else:
            self.r.git('worktree', 'add', '-q', '-b', name, wt)
        for rel, data in files.items():
            self.put(rel, data, wt)
        self.r.git('add', '--', *files, cwd=wt)
        self.r.git('commit', '-q', '-m', 'on %s' % name, cwd=wt)
        return self.r.git('rev-parse', 'HEAD', cwd=wt), wt

    def put(self, rel, data, root=None):
        full = os.path.join(root or self.top, *rel.split('/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as fh:
            fh.write(data)
        return full

    def get(self, rel):
        with open(os.path.join(self.top, *rel.split('/')), 'rb') as fh:
            return fh.read()

    def blob(self, spec):
        return self.r.git('rev-parse', spec)

    def log_lines(self):
        p = os.path.join(self.r.common_dir(), 'kit-stray-restore.jsonl')
        if not os.path.exists(p):
            return []
        with open(p, encoding='utf-8') as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def snapshot(self):
        """Every file under the main checkout but .git/index (git status may refresh it), with
        its bytes, plus the kit's log and quarantine under the common dir."""
        out = {}
        for root, _dirs, files in os.walk(self.top):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, self.top).replace(os.sep, '/')
                if rel == '.git/index' or rel.endswith('.lock'):
                    continue
                with open(full, 'rb') as fh:
                    out[rel] = fh.read()
        return out
