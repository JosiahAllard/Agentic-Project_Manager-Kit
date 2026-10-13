"""The auto-detected gate: with no "gate" section in `.kit.json` (inline or `tools/gate.json`),
the gate's steps come from the checkout's own files (docs/gate.md "Auto-detected gate";
docs/multi-host-plan.md decision 24).

`effective(cfg, root)` is the one call the gate makes: the configured section when there is one
(source `gate.json`: it always wins, and no detected step is added to it), else that section's
defaults with `steps` set to the detected ones (source `auto`). Every gate built on it (the
branch gate, the merged-tree gate of a landing, the daily full run) runs the same steps.

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
  rust    Cargo.toml at the root -> rust-test `cargo test`; rust-fmt `cargo fmt --check` when
          rustfmt.toml or .rustfmt.toml is there.
  go      go.mod at the root -> go-test `go test ./...`, go-vet `go vet ./...`.
  dotnet  a *.sln / *.slnx at the root -> dotnet-test `dotnet test <sln>` (one step per
          solution); with none, each test project (*.csproj, *.fsproj, *.vbproj named *test* or
          referencing Microsoft.NET.Test.Sdk) -> `dotnet test <project>`. With
          -nodeReuse:false -p:UseSharedCompilation=false, as the .NET stack template.
Several stacks in one checkout: every one's steps, in the order above.
"""
import collections
import json
import os
import re

from kitlib.git import cmd

AUTO = 'auto'
CONFIG = 'gate.json'        # the configured "gate" section, inline in .kit.json or in its file
SOURCES = (AUTO, CONFIG)
STACKS = ('python', 'node', 'rust', 'go', 'dotnet')
PRUNE = frozenset(['.git', 'node_modules', '.venv', 'venv', '__pycache__', 'target', 'bin', 'obj',
                   'dist', 'build', '.tox', '.mypy_cache', '.pytest_cache', '.ruff_cache'])
MAX_READ = 50               # test files read for an `import pytest`
NO_REUSE = ['-nodeReuse:false', '-p:UseSharedCompilation=false']
NPM_PLACEHOLDER = re.compile(r'no test specified')

Effective = collections.namedtuple('Effective', 'gate source stacks')


def configured(cfg):
    """True when `.kit.json` has a "gate" section (inline, or in the file it names)."""
    return cfg.has('gate')


def effective(cfg, root, files=None):
    """The gate a run uses: Effective(gate, source, stacks). `gate` is the "gate" section over
    its defaults; with source `auto` its `steps` are the detected ones (maybe none). `files`:
    `(paths, read)` to detect from instead of the checkout at `root` (tests)."""
    gate = cfg.section('gate')
    if configured(cfg):
        return Effective(gate, CONFIG, [])
    paths, read = files if files is not None else checkout_files(root)
    found = detect(paths, read)
    gate['steps'] = [s for _, steps in found for s in steps]
    return Effective(gate, AUTO, [name for name, _ in found])


def nothing_detected(eff):
    return eff.source == AUTO and not eff.gate.get('steps')


def label(eff):
    """The report's source line."""
    if eff.source != AUTO:
        return 'source: %s' % CONFIG
    if not eff.stacks:
        return ('source: auto (auto-detected gate: no stack found, so it ran NO checks; write a '
                '"gate" section in .kit.json or tools/gate.json to give it steps)')
    return ('source: auto (auto-detected gate: %s; a "gate" section in .kit.json or '
            'tools/gate.json replaces it)' % ', '.join(eff.stacks))


# ---------------------------------------------------------------- the checkout's files

def checkout_files(root):
    """`(paths, read)` of the checkout at `root`: the files git tracks plus the untracked ones it
    does not ignore; where git cannot list them, a walk that skips PRUNE folders and any folder
    holding a `.git` of its own (a nested repository or worktree)."""
    out, _ = cmd.run_text(['ls-files', '-z', '--cached', '--others', '--exclude-standard'], root,
                          strip=False)
    if out is not None:
        paths = sorted(set(p for p in out.split('\0') if p))
    else:
        paths = walk_files(root)
    return paths, reader(root)


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
    steps = [_step('rust-test', ['cargo', 'test'], ['cargo'], True)]
    if 'rustfmt.toml' in top or '.rustfmt.toml' in top:
        steps.append(_step('rust-fmt', ['cargo', 'fmt', '--check'], ['cargo'], False))
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
    several = len(targets) > 1
    return [_step(_step_name('dotnet-test', os.path.splitext(_base(t))[0], several),
                  ['dotnet', 'test', t] + NO_REUSE, ['dotnet'], True) for t in targets]


DETECTORS = {'python': python, 'node': node, 'rust': rust, 'go': go, 'dotnet': dotnet}
