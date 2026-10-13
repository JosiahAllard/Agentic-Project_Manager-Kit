"""The auto-detected gate: with no "gate" section in `.kit.json` (inline or `tools/gate.json`),
the gate's steps come from the checkout's own files (docs/gate.md "Auto-detected gate";
docs/multi-host-plan.md decision 24).

`effective(cfg, root)` is the one call the gate makes: the configured section when there is one
(source `gate.json`: it always wins, and no detected step is added to it), else that section's
defaults with `steps` set to the detected ones (source `auto`). Every gate built on it (the
branch gate, the merged-tree gate of a landing, the daily full run) runs the same steps.

The branch gate and a landing also detect over their base commit (the merge base; the landing's
--expect-main) and keep every step the base has that the tree under test lacks (`owed`): a branch
that deletes its test files, its `test` script or its manifest still runs those tests, which then
fail. A kept lint step only warns. Dropping a check on purpose takes a "gate" section.

Detection is pure: each detector reads only the checkout's files (`paths`: repo-relative posix
paths; `read(rel)`: a file's text or None). Nothing is installed, fetched or run, and no tool is
looked up on PATH: a step names what it needs in `requires`, and the gate checks that when it
runs. A detected step is `always` (it runs whatever changed). A test step is `requires_strict`
(a missing runner FAILs it: a gate never passes without the tests it found); a lint step is not
(a missing linter is SKIP, named in the verdict).

  python  test files (test*.py or *_test.py) -> python-tests: `{python} -m pytest` when the
          repository uses pytest (pytest.ini, a conftest.py, a [tool.pytest...] / [tool:pytest] /
          [pytest] section, pytest or a pytest-* plugin named in pyproject.toml, setup.cfg,
          setup.py, tox.ini, Pipfile or requirements*.txt, or a test file importing it), else
          `{python} -m unittest discover` (`-s tests` or `-s test` when that folder holds the
          tests).
          python-ruff `ruff check .` when ruff.toml, .ruff.toml or [tool.ruff] is there;
          python-flake8 `flake8` when .flake8 or a [flake8] section is there.
  node    package.json at the root -> node-test, node-lint, node-typecheck: `<pm> run <script>`
          for each of the scripts test, lint, typecheck it has (npm init's placeholder test
          script is not a test). <pm> by lockfile: pnpm-lock.yaml pnpm, yarn.lock yarn,
          bun.lockb / bun.lock bun, else npm.
  rust    Cargo.toml at the root -> rust-test `cargo test --workspace`; rust-fmt
          `cargo fmt --all --check` when rustfmt.toml or .rustfmt.toml is there.
  go      go.mod at the root -> go-test `go test ./...`, go-vet `go vet ./...`.
  dotnet  a *.sln / *.slnx at the root -> dotnet-test `dotnet test <sln>` (one step per
          solution); with none, each test project (*.csproj, *.fsproj, *.vbproj named *test* or
          referencing Microsoft.NET.Test.Sdk) -> `dotnet test <project>`; a path starting
          with '-' or holding '{' is never an argument. With
          -nodeReuse:false -p:UseSharedCompilation=false, as the .NET stack template.
Several stacks in one checkout: every one's steps, in the order above.
"""
import collections
import json
import os
import re

from kitlib.git import batch, cmd

AUTO = 'auto'
CONFIG = 'gate.json'        # the configured "gate" section, inline in .kit.json or in its file
SOURCES = (AUTO, CONFIG)
STACKS = ('python', 'node', 'rust', 'go', 'dotnet')
PRUNE = frozenset(['.git', 'node_modules', '.venv', 'venv', '__pycache__', 'target', 'bin', 'obj',
                   'dist', 'build', '.tox', '.mypy_cache', '.pytest_cache', '.ruff_cache'])
MAX_READ = 50               # test files read for an `import pytest`
NO_REUSE = ['-nodeReuse:false', '-p:UseSharedCompilation=false']
NPM_PLACEHOLDER = re.compile(r'no test specified')
LINT = frozenset(['python-ruff', 'python-flake8', 'node-lint', 'node-typecheck', 'rust-fmt',
                  'go-vet'])     # the detected steps that check, not test

Effective = collections.namedtuple('Effective', 'gate source stacks kept')
Effective.__new__.__defaults__ = ((),)


class DetectError(Exception):
    """The base commit's files could not be read, so the gate cannot tell which steps it owes."""


def configured(cfg):
    """True when `.kit.json` has a "gate" section (inline, or in the file it names)."""
    return cfg.has('gate')


def effective(cfg, root, files=None, base=None):
    """The gate a run uses: Effective(gate, source, stacks, kept). `gate` is the "gate" section
    over its defaults; with source `auto` its `steps` are the detected ones (maybe none).
    `files`: `(paths, read)` to detect from instead of the checkout at `root` (tests).

    `base` (a commit: the branch gate's merge base, a landing's --expect-main): the steps
    detected in that commit's tree are owed too. A step the base has and the checkout does not
    (its test files, script or manifest deleted or renamed) is kept, with the base's argv, and
    named in `kept`, so a branch cannot drop a check by deleting what detects it. Raises
    DetectError when git cannot list or read the base."""
    gate = cfg.section('gate')
    if configured(cfg):
        return Effective(gate, CONFIG, [], ())
    paths, read = files if files is not None else checkout_files(root)
    found = detect(paths, read)
    kept = []
    if base:
        found, kept = owed(found, commit_detect(root, base))
    gate['steps'] = [s for _, steps in found for s in steps]
    return Effective(gate, AUTO, [name for name, _ in found], tuple(kept))


def owed(found, at_base):
    """`(found, plus each step of at_base of a family found lacks, in STACKS order; the names
    added)`. A step's family is its name, except that every `dotnet-test...` step is one family
    (a solution or test project renamed or added is not a check dropped). An added step that is
    a check, not a test (LINT), is `warn`, so its failure is a WARN named
    in the verdict, not a FAIL; an added test step fails as it would have."""
    mine, theirs = dict(found), dict(at_base)
    out, kept = [], []
    for name in STACKS:
        steps = list(mine.get(name, []))
        families = set(_family(s['name']) for s in steps)
        for s in theirs.get(name, []):
            if _family(s['name']) not in families:
                steps.append(dict(s, warn=True) if s['name'] in LINT else s)
                kept.append(s['name'])
        if steps:
            out.append((name, steps))
    return out, kept


def _family(name):
    return 'dotnet-test' if name.startswith('dotnet-test') else name


def nothing_detected(eff):
    return eff.source == AUTO and not eff.gate.get('steps')


def label(eff):
    """The report's source line."""
    if eff.source != AUTO:
        return 'source: %s' % CONFIG
    if not eff.stacks:
        return ('source: auto (auto-detected gate: no stack found, so it ran NO checks; write a '
                '"gate" section in .kit.json or tools/gate.json to give it steps)')
    kept = ('; kept from the base, which detects them and this tree does not: %s'
            % ', '.join(eff.kept)) if eff.kept else ''
    return ('source: auto (auto-detected gate: %s%s; a "gate" section in .kit.json or '
            'tools/gate.json replaces it)' % (', '.join(eff.stacks), kept))


# ---------------------------------------------------------------- the checkout's files

def checkout_files(root):
    """`(paths, read)` of the checkout at `root`: the files git tracks, plus the untracked ones it
    does not ignore that are under no PRUNE folder (an unignored virtualenv or node_modules is not
    the project's, and can be large); where git cannot list them, a walk that skips PRUNE folders
    and any folder holding a `.git` of its own (a nested repository or worktree)."""
    tracked, _ = cmd.run_text(['ls-files', '-z', '--cached'], root, strip=False)
    others, _ = cmd.run_text(['ls-files', '-z', '--others', '--exclude-standard'], root,
                             strip=False)
    if tracked is None or others is None:
        return walk_files(root), reader(root)
    paths = set(p for p in tracked.split('\0') if p)
    paths.update(p for p in others.split('\0') if p and not _pruned(p))
    return sorted(paths), reader(root)


def _pruned(rel):
    return any(part in PRUNE for part in rel.split('/')[:-1])


def commit_detect(root, rev):
    """detect() over the tree of commit `rev` (one git ls-tree, one cat-file process). Raises
    DetectError."""
    out, why = cmd.run_text(['ls-tree', '-r', '-z', '--name-only', '--full-tree', rev], root,
                            strip=False)
    if out is None:
        raise DetectError('git cannot list the files of %s: %s' % (rev[:10], why))
    paths = sorted(p for p in out.split('\0') if p)
    with batch.CatFile(root) as cat:
        def read(rel):
            data = cat.read(rev, rel)
            return None if data is None else cmd.text(data)
        found = detect(paths, read)
        if cat.dead:
            raise DetectError('git cannot read the files of %s: %s' % (rev[:10], cat.dead_reason))
    return found


def walk_files(root):
    paths = []
    for here, dirs, files in os.walk(root):
        rel = os.path.relpath(here, root)
        dirs[:] = sorted(d for d in dirs if d not in PRUNE
                         and not os.path.exists(os.path.join(here, d, '.git')))
        for f in files:
            paths.append(f if rel == '.' else '%s/%s' % (rel.replace('\\', '/'), f))
    return sorted(paths)


def reader(root):
    def read(rel):
        try:
            with open(os.path.join(root, rel), encoding='utf-8', errors='replace') as f:
                return f.read()
        except OSError:
            return None
    return read


# ---------------------------------------------------------------- detectors

def detect(paths, read):
    """[(stack, [step objects])] for each stack found, in STACKS order."""
    paths = [p.replace('\\', '/') for p in paths]
    out = []
    for name in STACKS:
        steps = DETECTORS[name](paths, read)
        if steps:
            out.append((name, steps))
    return out


def _step(name, argv, requires, strict):
    s = {'name': name, 'always': True, 'cmd': list(argv)}
    if requires:
        s['requires'] = list(requires)
    if strict:
        s['requires_strict'] = True
    return s


def _base(p):
    return p.rsplit('/', 1)[-1]


def _top(paths):
    """The root-level file names, lower-cased -> as written."""
    return {p.lower(): p for p in paths if '/' not in p}


def _has(read, rel, rx):
    text = read(rel) if rel else None
    return bool(text) and re.search(rx, text, re.M | re.I) is not None


def _shim(name):
    """A package manager's command: npm, pnpm and yarn are .cmd shims on Windows, which
    CreateProcess runs only when named with the extension."""
    return name + '.cmd' if os.name == 'nt' else name


def _is_py_test(p):
    b = _base(p).lower()
    return b.endswith('.py') and (b.startswith('test') or b.endswith('_test.py'))


def _uses_pytest(paths, top, tests, read):
    if 'pytest.ini' in top or any(_base(p) == 'conftest.py' for p in paths):
        return True
    sections = (('pyproject.toml', r'^\s*\[tool\.pytest'), ('setup.cfg', r'^\s*\[tool:pytest\]'),
                ('tox.ini', r'^\s*\[pytest\]'))
    if any(_has(read, top.get(f), rx) for f, rx in sections):
        return True
    named = ['pyproject.toml', 'setup.cfg', 'setup.py', 'tox.ini', 'pipfile']
    deps = [top[f] for f in named if f in top]
    deps += [p for p in paths if re.match(r'(?i)^(requirements[^/]*\.txt|requirements/[^/]+\.txt)$', p)]
    if any(_has(read, p, r'(?<![\w.-])pytest(?!\w)') for p in deps):
        return True
    return any(_has(read, p, r'^\s*(import pytest|from pytest\b)') for p in tests[:MAX_READ])


def python(paths, read):
    top = _top(paths)
    tests = [p for p in paths if _is_py_test(p)]
    steps = []
    if tests:
        if _uses_pytest(paths, top, tests, read):
            steps.append(_step('python-tests', ['{python}', '-m', 'pytest'],
                               [['{python}', '-c', 'import pytest']], True))
        else:
            argv = ['{python}', '-m', 'unittest', 'discover']
            for d in ('tests', 'test'):
                if any(p.startswith(d + '/') for p in tests):
                    argv += ['-s', d]
                    break
            steps.append(_step('python-tests', argv, [], False))
    if ('ruff.toml' in top or '.ruff.toml' in top
            or _has(read, top.get('pyproject.toml'), r'^\s*\[tool\.ruff')):
        steps.append(_step('python-ruff', ['ruff', 'check', '.'], ['ruff'], False))
    if '.flake8' in top or any(_has(read, top.get(f), r'^\s*\[flake8\]')
                               for f in ('setup.cfg', 'tox.ini')):
        steps.append(_step('python-flake8', ['flake8'], ['flake8'], False))
    return steps


def package_manager(top):
    for lock, pm in (('pnpm-lock.yaml', 'pnpm'), ('yarn.lock', 'yarn'), ('bun.lockb', 'bun'),
                     ('bun.lock', 'bun'), ('package-lock.json', 'npm'),
                     ('npm-shrinkwrap.json', 'npm')):
        if lock in top:
            return pm
    return 'npm'


def node(paths, read):
    top = _top(paths)
    if 'package.json' not in top:
        return []
    try:
        pkg = json.loads(read(top['package.json']) or '')
    except ValueError:
        return []
    scripts = pkg.get('scripts') if isinstance(pkg, dict) else None
    if not isinstance(scripts, dict):
        return []
    pm = package_manager(top)
    exe = pm if pm == 'bun' else _shim(pm)
    steps = []
    for script in ('test', 'lint', 'typecheck'):
        body = scripts.get(script)
        if not isinstance(body, str) or not body.strip():
            continue
        if script == 'test' and NPM_PLACEHOLDER.search(body):
            continue
        steps.append(_step('node-' + script, [exe, 'run', script], [pm], script == 'test'))
    return steps


def rust(paths, read):
    top = _top(paths)
    if 'cargo.toml' not in top:
        return []
    # --workspace / --all: a root package with workspace members tests and checks them all, not
    # the root package alone (a virtual manifest does that anyway).
    steps = [_step('rust-test', ['cargo', 'test', '--workspace'], ['cargo'], True)]
    if 'rustfmt.toml' in top or '.rustfmt.toml' in top:
        steps.append(_step('rust-fmt', ['cargo', 'fmt', '--all', '--check'], ['cargo'], False))
    return steps


def go(paths, read):
    if 'go.mod' not in _top(paths):
        return []
    return [_step('go-test', ['go', 'test', './...'], ['go'], True),
            _step('go-vet', ['go', 'vet', './...'], ['go'], False)]


def _step_name(prefix, stem, several):
    if not several:
        return prefix
    return '%s-%s' % (prefix, re.sub(r'[^A-Za-z0-9._-]+', '-', stem).strip('-') or 'x')


def dotnet(paths, read):
    slns = sorted(p for p in paths if '/' not in p and p.lower().endswith(('.sln', '.slnx')))
    targets = slns
    if not targets:
        targets = sorted(p for p in paths if p.lower().endswith(('.csproj', '.fsproj', '.vbproj'))
                         and ('test' in _base(p).lower()
                              or _has(read, p, r'Microsoft\.NET\.Test\.Sdk')))
    # A path is an argument: one starting with '-' would be read as an option, and one holding
    # a '{' could be rewritten by the gate's {placeholder} substitution. Neither is tested.
    targets = [t for t in targets if not t.startswith('-') and '{' not in t]
    several = len(targets) > 1
    steps, names = [], set()
    for t in targets:
        name = _step_name('dotnet-test', os.path.splitext(_base(t))[0], several)
        n = 2
        while name in names:            # two test projects of one name in different folders
            name = '%s-%d' % (_step_name('dotnet-test', os.path.splitext(_base(t))[0], True), n)
            n += 1
        names.add(name)
        steps.append(_step(name, ['dotnet', 'test', t] + NO_REUSE, ['dotnet'], True))
    return steps


DETECTORS = {'python': python, 'node': node, 'rust': rust, 'go': go, 'dotnet': dotnet}
