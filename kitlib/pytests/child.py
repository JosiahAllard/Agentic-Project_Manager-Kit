"""What runs inside one unit's process (kitlib.pytests.runner starts it as `tools/pytests.py
--child`): the unittest run of one module, class or shard, the `PY SKIP` lines, and the --audit
hook. Ported from the PM kit's tools/pytests.py; the checkout root and its tools folder are passed
in, and the log goes to the stream given.

--audit: the child records every file its tests open or list under the checkout, outside
<tools>/__pycache__, .git and each host's agent worktrees folder (hosts.json, .claude/worktrees on
Claude: other checkouts, when a merged gate runs this in the main one), and FAILs (`[FAIL] audit:` lines) any read that kitlib.tooldeps does not map to the
unit's module: a tools .py in its closure (any tools .py when the closure holds tool_deps, whose
scan reads them all), an `extra` file its closure reads, a `pinned`/`patterns` entry naming it, or
any read by a `whole_tree` class - its tests, setUpClass and tearDownClass. A read at import time
is labelled `<module> (import)` and needs a table entry like any other. Named limits: reads made by
a subprocess (git, a copied tool run as a child) are not seen; a `pinned` piece entry is a
ConfigError here (kitlib.tooldeps is given no pieces table).
"""
import os
import sys
import threading
import unittest

from kitlib.git.batch import fixture_env
from kitlib.host import registry
from kitlib.tooldeps import deps as tool_deps

SKIP_LINE = 'PY SKIP %s %s'


def shard(suite, i, n):
    """The TestCase classes of a module's suite, round-robin: shard i (1-based) of n."""
    groups = [g for g in suite]          # loadTestsFromModule: one suite per TestCase class
    return unittest.TestSuite(groups[i - 1::n])


class Audit(object):
    """Records real-tree reads the unit's module is not mapped to (see the module docstring)."""

    def __init__(self, module, root, tools=None, cfg=None):
        self.td = tool_deps
        self.module = module
        self.root = os.path.normcase(os.path.normpath(root))
        self.cfg = tool_deps.config(root) if cfg is None else cfg
        graph = tool_deps.scan(tools, self.cfg, root)
        self.tools_rel = graph.tools_rel
        self.pins = tool_deps.pinned(self.cfg)
        self.reach = graph.closure(module) | {module}
        self.worktrees = registry.union('worktrees_directory')
        self.current = None
        self.findings = {}
        self.local = threading.local()      # per thread: a test's own pool must not mute others
        self.stopped = False

    def allowed(self, rel):
        if rel == '.git' or rel.startswith('.git/'):
            return True
        if rel.startswith('<'):
            return True         # a pseudo file name (`<unknown>`, `<string>`), resolved against cwd
        if any(rel == w or rel.startswith(w + '/') for w in self.worktrees):
            return True         # other checkouts, not this tree (a merged gate runs in main)
        prefix = self.tools_rel + '/'
        if rel.startswith(prefix):
            name = rel[len(prefix):]
            if 'tool_deps' in self.reach and '/' not in name and name.endswith('.py'):
                return True     # tool_deps.scan reads every tools .py by design (gate --plan)
            if name == '__pycache__' or name.startswith('__pycache__/'):
                return True
            if '/' not in name and name.endswith('.py'):
                return name[:-3] in self.reach
            return bool(set(self.cfg['extra'].get(name, ())) & self.reach) or \
                self.module in self.td.pinned_modules(rel, cfg=self.cfg, pins=self.pins)
        if rel == self.tools_rel:
            return True
        return self.module in self.td.pinned_modules(rel, cfg=self.cfg, pins=self.pins)

    def whole_tree(self):
        cur = self.current or ''
        return any(cur.startswith(c + '.') for c in self.cfg['whole_tree'])

    def hook(self, event, args):
        if self.stopped or event not in ('open', 'os.listdir', 'os.scandir') \
                or getattr(self.local, 'busy', False):
            return
        self.local.busy = True
        try:
            path = args[0] if args else None
            if isinstance(path, int) or path is None:
                return
            try:
                real = os.path.abspath(os.fsdecode(path))
            except (TypeError, ValueError):
                return
            full = os.path.normcase(real)
            if full != self.root and not full.startswith(self.root + os.sep):
                return
            # The path as the test spelled it (normcase lowercases on Windows; the tables do not).
            rel = real[len(self.root):].lstrip(os.sep).replace(os.sep, '/')
            if self.allowed(rel) or self.whole_tree():
                return
            self.findings.setdefault(rel, self.current or '(outside any test)')
        finally:
            self.local.busy = False


def _cases(suite):
    if isinstance(suite, unittest.TestSuite):
        for s in suite:
            yield from _cases(s)
    else:
        yield suite


def label_class_fixtures(suite, auditor):
    """Wrap each TestCase class's setUpClass/tearDownClass so a read they make is attributed to
    `<module>.<Class>.setUpClass` (a whole_tree class's fixtures read the tree too), not to no
    test at all. The label names the class the fixture RUNS for (`cls`), so a base fixture reached
    through a subclass's `super().setUpClass()`, or simply inherited, is credited to the subclass.

    Only a classmethod a class defines in its own __dict__ is replaced, by a classmethod calling
    the original function with the same `cls` - so `super()` still reaches the base's fixture with
    the subclass as `cls`, exactly as unwrapped. Every class in a collected class's MRO is covered
    (a base that is never collected itself can still define the fixture)."""
    seen = set()
    for case in _cases(suite):
        for cls in type(case).__mro__:
            if cls in seen or cls in (unittest.TestCase, object):
                continue
            seen.add(cls)
            for name in ('setUpClass', 'tearDownClass'):
                real = cls.__dict__.get(name)
                if not isinstance(real, classmethod):
                    continue

                def wrapped(klass, func=real.__func__, name=name):
                    prev = auditor.current
                    auditor.current = '%s.%s.%s' % (klass.__module__, klass.__name__, name)
                    try:
                        return func(klass)
                    finally:
                        auditor.current = prev
                setattr(cls, name, classmethod(wrapped))


def set_path(tools, path=None, drop=()):
    """sys.path for a unit, as discover had it: `path` (a test's own folder of fake modules), then
    the checkout's tools folder, first; `drop` (the entry point's own folder, which Python put
    first) removed. The kit root stays where the entry point put it, after them."""
    gone = {os.path.normcase(os.path.abspath(d)) for d in list(drop) + [tools] if d}
    sys.path[:] = [d for d in sys.path if not d or os.path.normcase(os.path.abspath(d)) not in gone]
    sys.path[:0] = [d for d in (path, tools) if d]


def run_child(name, shard_spec, audit, root, tools, out, path=None):
    """Run one unit in this process; return 0 if every test passed and the audit found nothing,
    else 1. `tools` is the checkout's tools folder, already first on sys.path (set_path)."""
    # No auto gc or auto maintenance in any git this unit starts (TOOLS-GIT-SPAWN-DIET): every
    # fixture commit would otherwise start a second git process.
    os.environ.update(fixture_env())
    module = name.split('.')[0]
    auditor = None
    if audit:
        auditor = Audit(module, root, tools=tools)
        sys.addaudithook(auditor.hook)
    loader = unittest.TestLoader()
    if auditor:
        auditor.current = '%s (import)' % module
    suite = loader.loadTestsFromName(name)
    if auditor:
        auditor.current = None
    if shard_spec:
        i, n = (int(x) for x in shard_spec.split('/'))
        suite = shard(suite, i, n)
    if auditor:
        label_class_fixtures(suite, auditor)

    class Result(unittest.TextTestResult):
        def startTest(self, test):
            if auditor:
                auditor.current = test.id()
            super().startTest(test)

        def stopTest(self, test):
            super().stopTest(test)
            if auditor:
                auditor.current = None

    result = unittest.TextTestRunner(stream=out, resultclass=Result).run(suite)
    for test, reason in result.skipped:
        # One token for the id: a class or module fixture's id has a space (`setUpClass (test_x.C)`),
        # and a gate reads the id as the first word after `PY SKIP ` (a repo gate's skip_id).
        out.write(SKIP_LINE % (test.id().replace(' ', '_'),
                               ' '.join(str(reason).split()) or '(no reason)') + '\n')
    out.flush()
    bad = 0
    if auditor:
        auditor.stopped = True  # the hook cannot be removed; stop it recording the report's reads
        for rel, test in sorted(auditor.findings.items()):
            out.write('[FAIL] audit: %s read %s, which tool_deps does not map to %s (add a .kit.json '
                      'tool_tests pinned / extra entry naming it, or a whole_tree class)\n'
                      % (test, rel, module))
            bad += 1
        out.write('pytests: audit %s: %d unmapped real-tree read(s)\n' % (module, bad))
        out.flush()
    return 0 if result.wasSuccessful() and not bad else 1
