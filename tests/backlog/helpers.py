"""Shared fixtures for the backlog tests: a record builder in the per-repository shape and a store
on a real temporary repository with a bare remote (tests/gitfixture.py)."""
import json
import os
import threading
import time
import unittest
from unittest import mock

from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from tests.gitfixture import TempRepo, fixture_env
from tests.home import temp_home

BASE = [
    ('id', 'FOO'),
    ('area', 'xx'),
    ('priority', '-'),
    ('priority_by', '-'),
    ('effort', '-'),
    ('state', 'open'),
    ('blocked_on', '-'),
    ('scope', 'routegraph/**'),
    ('summary', 'a thing that is wrong and should be fixed'),
]
BRANCH = 'tools/some-branch'
TODAY = '2026-10-07'
# What every `add` must now say about where its finding came from (docs/backlog.md, "The fold-in
# guard"): a fixture's record is found on no branch.
ADD_WHY = 'a test fixture, found on no branch'
ADD_NONE = dict(found_on='none', why_not_folded=ADD_WHY)
ADD_FLAGS = ('--found-on', 'none', '--why-not-folded', ADD_WHY)


def kit_json_ref(repo, data=None, root=None, commit=True):
    """Write `.kit.json` in `root` (default the repo's) as `data` with `"backlog".store` named
    `ref`, and commit it unless `commit` is false. A fixture that writes the ref store names it:
    the default store alone is read-only (docs/backlog.md "Stores")."""
    data = json.loads(json.dumps(data or {}))
    data.setdefault('backlog', {}).setdefault('store', 'ref')
    root = root or repo.root
    repo.write('.kit.json', json.dumps(data) + '\n', root=root)
    if commit:
        repo.commit('kit config: the ref store', '.kit.json', cwd=root)
    return data


def record_text(drop=(), body='', **over):
    """A record's text: the minimal valid header, keys dropped or overridden; a list value writes
    one line per member."""
    pairs = []
    for key, value in BASE:
        if key in drop:
            over.pop(key, None)
            continue
        if key in over:
            value = over.pop(key)
        pairs.append((key, value))
    pairs.extend(over.items())
    lines = []
    for key, value in pairs:
        for one in (value if isinstance(value, (list, tuple)) else [value]):
            lines.append('%s: %s' % (key, one))
    text = '\n'.join(lines) + '\n'
    if body:
        text += '\n' + body + '\n'
    return text


def rec(rid, **over):
    """{stem: text} for one record with `id` set and the scope defaulted per id (no collisions)."""
    over.setdefault('scope', 'area/%s/**' % rid.lower())
    return {rid: record_text(id=rid, **over)}


def priced(rid, priority='2', **over):
    over.setdefault('priority_by', 'agent')
    return rec(rid, priority=priority, **over)


def parse(stem='FOO', **kw):
    return rmod.parse('backlog/%s.md' % stem, record_text(**kw), stem=stem)


def messages(stem='FOO', rules=None, **kw):
    r = parse(stem=stem, **kw)
    return [f.message for f in r.structural + rmod.validate(r, rules or rmod.Rules())]


def race(case, writers, write, hook=None, deadline=120):
    """Run `write(store, name)` for every `(name, store)` in `writers`, each on its own thread,
    every one planning its first attempt against the same store (a barrier in before_publish,
    after the plan) before any publishes. `hook(name, attempt)`, when given, runs after it.

    Returns (errors, attempts): `errors` names each writer that raised anything, BaseException
    included (the fake git's Unsupported is one: a writer it kills must not pass for a write the
    store lost); `attempts` {name: [attempt, ...]}. Every thread shares one `deadline` (seconds),
    and a writer still running when it passes fails the test: it has not written yet, which a
    later read would otherwise report as a lost update."""
    names = [n for n, _ in writers]
    errors, attempts, passed = [], {n: [] for n in names}, set()
    gate = threading.Barrier(len(writers), timeout=deadline)

    def run(store, name):
        def planned(attempt):
            attempts[name].append(attempt)
            if attempt == 0:
                gate.wait()
                passed.add(name)
            if hook is not None:
                hook(name, attempt)
        store.before_publish = planned
        try:
            write(store, name)
        except BaseException as exc:          # reported by the caller; a thread cannot fail a test
            errors.append('%s: %r' % (name, exc))
            if name not in passed:            # one that died before the barrier frees the rest
                gate.abort()
    # daemon: a writer that hangs must not keep the unit's interpreter alive past the failed assert
    threads = [threading.Thread(target=run, args=(s, n), name='race-%s' % n, daemon=True)
               for n, s in writers]
    end = time.monotonic() + deadline
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    alive = [n for n, t in zip(names, threads) if t.is_alive()]
    case.assertEqual(alive, [], 'writers still running %ss after the start: %r'
                     % (deadline, attempts))
    return errors, attempts


def _branch(repo):
    """The template's prepare: BRANCH at main (the plan context's branch)."""
    repo.git('branch', BRANCH)


class StoreCase(unittest.TestCase):
    """A ref store on a temp repository with a bare remote (`remote = False`: none), under a temp
    home (self.home): the free-text machine-path refusal reads the machine config's
    machine_path_folders (kitlib.backlog.plan.refuse_machine_paths)."""
    remote = True
    object_format = None

    def setUp(self):
        # The library's own git calls inherit os.environ: no global config, no auto-gc/maintenance.
        patch = mock.patch.dict(os.environ, fixture_env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.home = temp_home(self)
        self.repo = TempRepo(bare_remote=self.remote, object_format=self.object_format,
                             prepare=_branch, prepare_key='backlog.helpers.StoreCase')
        self.addCleanup(self.repo.cleanup)
        self.rules = rmod.Rules('upper', ('L0', 'L1'))
        self.backend = self.make_backend()
        self.store = smod.Store(self.backend, self.rules)
        self.ctx = pmod.Context(self.store, self.repo.root, branch=BRANCH, today=TODAY)

    def make_backend(self, root=None):
        return smod.RefBackend(root or self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)

    def seed(self, *records):
        writes = {}
        for r in records:
            writes.update(r)
        snap = self.store.load()
        return self.backend.publish(snap, writes, 'seed\n')

    def verb(self, name, *args, **kw):
        """Run plan_<name> through Store.write; returns the Change."""
        plan = getattr(pmod, 'plan_' + name)
        change, _tip = self.store.write(lambda s: plan(s, self.ctx, *args, **kw))
        return change

    def get(self, rid):
        return self.store.load().get(rid)

    def remote_tip(self):
        if not self.remote:
            return self.repo.git('rev-parse', '--verify', '--quiet', smod.STORE_REF, check=False)
        out = self.repo.git('ls-remote', 'origin', smod.STORE_REF)
        return out.split('\t')[0] if out else ''
