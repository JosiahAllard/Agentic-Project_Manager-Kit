"""Function-level kit imports that name a module not yet loaded (a late first load of kit code).

`late_kit_imports(names, loaded)` walks the source of each module in `names` and returns one line
per import statement inside a function that names a `kitlib` module absent from `loaded`. `loaded`
is a set of module names taken from a process that has done its start-up work; tests get it from a
child interpreter (child_loaded), because this suite's own sys.modules holds whatever other tests
imported."""
import ast
import importlib.util
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def child_loaded(setup):
    """Run `setup` (python source) in a fresh interpreter, cwd the repo, on this process's
    environment unchanged, and return the set of module names it holds afterwards."""
    code = setup + "\nimport json, sys\nprint(json.dumps(sorted(sys.modules)))\n"
    r = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT, capture_output=True,
                       text=True, timeout=120)
    if r.returncode != 0:
        raise AssertionError('child failed (%d): %s' % (r.returncode, r.stderr))
    return set(json.loads(r.stdout.splitlines()[-1]))


def _spec(name):
    try:
        return importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None


def _imported_modules(node, package):
    """Module names a function-level import statement may load."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    base = node.module or ''
    if node.level:
        parts = package.split('.')
        parts = parts[:len(parts) - (node.level - 1)]
        base = '.'.join(parts + ([base] if base else []))
    out = [base]
    for a in node.names:
        sub = base + '.' + a.name
        if _spec(sub) is not None:
            out.append(sub)
    return out


def late_kit_imports(names, loaded):
    found = []
    for name in names:
        spec = _spec(name)
        package = name if spec.submodule_search_locations else name.rpartition('.')[0]
        with open(spec.origin, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        seen = set()
        for fn in funcs:
            for node in ast.walk(fn):
                if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in seen:
                    seen.add(id(node))
                    for mod in _imported_modules(node, package):
                        if mod.split('.')[0] == 'kitlib' and mod not in loaded:
                            found.append('%s:%d imports %s' % (name, node.lineno, mod))
    return sorted(set(found))
