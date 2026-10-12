"""A fake machine for the guard's rule tests: real folders in a temp dir (so .kit.json loads
through kitlib.config), a fake git that answers classify, refs and gate-OK reads from tables,
and PreToolUse payloads shaped as Claude Code sends them. Nothing here touches a real
repository, the real lock store or the real machine config."""
import json
import os
import shutil
import tempfile

from kitlib.config import user
from kitlib.git import repo
from kitlib.guard import context

SHA_HEAD = '1' * 40
SHA_WT = '2' * 40
SHA_REMOTE_MAIN = '3' * 40
SHA_GATED = '4' * 40


def fwd(p):
    return p.replace('\\', '/')


class FakeGit(object):
    def __init__(self, world):
        self.w = world
        self.clean = {}                       # path -> bool
        self.ancestors = set()                # (sha, branch)

    def where(self, cwd):
        return 'wt' if cwd and os.path.normcase(os.path.abspath(cwd)).startswith(os.path.normcase(self.w.wt)) else 'main'

    def resolve(self, rev, cwd, peel='commit'):
        table = {'main': {'HEAD': SHA_HEAD, 'main': SHA_HEAD, 'refs/heads/main': SHA_HEAD, 'kit/wt': SHA_WT,
                          'gated': SHA_GATED},
                 'wt': {'HEAD': SHA_WT, 'main': SHA_HEAD, 'kit/wt': SHA_WT, 'gated': SHA_GATED}}
        sha = table[self.where(cwd)].get(rev)
        return (sha, None) if sha else (None, '%s does not name a commit' % rev)

    def current_branch(self, cwd):
        return ('kit/wt' if self.where(cwd) == 'wt' else 'main'), None

    def remote_tip(self, remote, name, cwd):
        return (SHA_REMOTE_MAIN, None) if name == 'main' else (None, 'no such ref')

    def is_ancestor(self, a, b, cwd):
        return (a, b) in self.ancestors, None

    def is_clean(self, cwd):
        return self.clean.get(os.path.normcase(os.path.abspath(cwd)), False), None


class World(object):
    """base/repo is a main checkout (push_block on), base/repo/.claude/worktrees/wt its linked
    worktree, base/pmdata another linked worktree of it (the PM data worktree when the machine
    config or a kit-pm-data.json record says so; its own .kit.json names repo "data"),
    base/plain no checkout, base/unknown a place git cannot tell about. base/kit (kit_dir) stands
    for the kit checkout the guard runs from (Ctx.kit): the folder its advised lock tool is under."""

    def __init__(self, kit=None):
        self.base = tempfile.mkdtemp(prefix='kit-guard-')
        self.main = os.path.join(self.base, 'repo')
        self.wt = os.path.join(self.main, '.claude', 'worktrees', 'wt')
        self.data = os.path.join(self.base, 'pmdata')
        self.data_git_dir = os.path.join(self.main, '.git', 'worktrees', 'pmdata')
        self.plain = os.path.join(self.base, 'plain')
        self.unknown = os.path.join(self.base, 'unknown')
        self.home = os.path.join(self.base, 'home')
        self.temp = os.path.join(self.base, 'tmp')
        self.kit_dir = os.path.join(self.base, 'kit')
        for d in (self.wt, self.data, self.plain, self.unknown, self.home, self.temp, self.kit_dir):
            os.makedirs(d)
        with open(os.path.join(self.main, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump(kit if kit is not None else {'repo': {'name': 'demo', 'push_block': True}}, f)
        with open(os.path.join(self.data, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump({'repo': {'name': 'data', 'main_branch': 'pmdata'}}, f)
        self.ledger = os.path.join(self.base, 'pm', 'rulings', 'LEDGER.md')
        self.git = FakeGit(self)
        self.records = {SHA_GATED}
        self.held = False
        self.environ = {}

    def cleanup(self):
        shutil.rmtree(self.base, True)

    def user_config(self, data):
        d = user.config_dir(self.home)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def classify(self, path):
        p = os.path.normcase(os.path.abspath(path))
        nc = os.path.normcase
        if p.startswith(nc(self.wt)):
            return repo.Place(repo.LINKED, self.wt, '', os.path.join(self.main, '.git', 'worktrees', 'wt'),
                              os.path.join(self.main, '.git'))
        if p.startswith(nc(self.data)):
            return repo.Place(repo.LINKED, self.data, '', self.data_git_dir, os.path.join(self.main, '.git'))
        if p.startswith(nc(self.main)):
            git = os.path.join(self.main, '.git')
            return repo.Place(repo.MAIN, self.main, '', git, git)
        if p.startswith(nc(self.unknown)):
            return repo.Place(repo.UNKNOWN, None, 'git did not answer: timed out', None, None)
        return repo.Place(repo.NONE, None, 'not a git repository', None, None)

    def gate_ok(self, root, sha):
        return ({'commit': sha}, None) if sha in self.records else (None, 'no gate-OK record for %s' % sha)

    def ctx(self, **kw):
        args = dict(environ=self.environ, home=self.home, classify=self.classify, gate_ok=self.gate_ok,
                    git=self.git, lock_held=lambda: (self.held, None if self.held else 'MACHINE_LOCK_RUN is not set'),
                    tempdir=self.temp, kit=self.kit_dir)
        args.update(kw)
        return context.Ctx(**args)


# test_prefix_parity.py sets this to a function that renames every subagent's agent_type, so the
# whole guard suite runs again with the plugin-prefixed names; None leaves agent_type as given.
AGENT_TYPE = None


def event(tool, tool_input, cwd, sub=False, agent_type='opus-worker'):
    """A PreToolUse payload as Claude Code sends it; a subagent's carries agent_id and agent_type."""
    ev = {'session_id': '8f5c9cf4-0000-4000-8000-000000000000',
          'transcript_path': 'C:/u/x/.claude/projects/p/8f5c9cf4.jsonl',
          'cwd': cwd, 'permission_mode': 'default', 'hook_event_name': 'PreToolUse',
          'tool_name': tool, 'tool_input': tool_input}
    if sub:
        ev['agent_id'] = 'a0335183081908f5a'
        ev['agent_type'] = AGENT_TYPE(agent_type) if AGENT_TYPE else agent_type
    return ev


def bash(command, cwd, sub=False):
    return event('Bash', {'command': command, 'description': 'test'}, cwd, sub)


def ps(command, cwd, sub=False):
    return event('PowerShell', {'command': command, 'description': 'test'}, cwd, sub)
