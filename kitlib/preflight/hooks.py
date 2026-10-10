"""A repository's report hooks (`.kit.json` "preflight".report_hooks): `module:function`, the
module a file in the repo's tools dir, the function returning the lines to print (for example
daily_full:summary_lines). Whatever goes wrong - no file, an import error, a raise, a SystemExit
(argparse on import) - is one `hook NOT CHECKED` line: preflight reports, it never dies on one
reading. A KeyboardInterrupt is not caught.

While a hook loads and runs, the repo's tools dir is first on sys.path, so `import pieces` in a
hook gets the repo's own pieces.py even when the kit has a tool of that name. Afterwards sys.path
is put back, the sys.modules entries the hook added from the tools dir are dropped, and any module
the repo's sibling displaced for the hook is restored, so kit code never sees the repo's module.
Modules the hook pulled in from elsewhere (the stdlib, kitlib) stay loaded. No bytecode is written
while a hook loads and runs (kitlib.config.policy.no_bytecode), so no `__pycache__` appears in the
repo's tools dir.

Single thread only: sys.path and sys.modules are process-global, so while a hook runs no other
thread may import, or it sees the repo's tools dir first and the kit's modules displaced.
"""
import contextlib
import importlib.util
import os
import sys

from kitlib.config.policy import no_bytecode

_MODULE_SUFFIXES = ('.py', '.pyw', '.pyd')


def _siblings(tools_dir):
    """The importable names in the tools dir: `x.py`, `x.pyw`, `x.<tag>.pyd` and folders."""
    names = set()
    for entry in os.listdir(tools_dir):
        if os.path.isdir(os.path.join(tools_dir, entry)):
            names.add(entry)
        elif entry.lower().endswith(_MODULE_SUFFIXES):
            names.add(entry.split('.')[0])
    return names


def _locations(module):
    """Where a loaded module came from: its __file__, else its package __path__; [] for a
    builtin or anything without either."""
    try:
        where = getattr(module, '__file__', None)
        if where:
            return [where]
        return [p for p in (getattr(module, '__path__', None) or []) if p]
    except Exception:  # noqa: BLE001  (sys.modules may hold any object)
        return []


def _under(path, root):
    try:
        path = os.path.normcase(os.path.abspath(path))
        return os.path.commonpath([path, root]) == root
    except ValueError:                       # another drive
        return False


def _from_tools(module, root):
    where = _locations(module)
    return bool(where) and all(_under(p, root) for p in where)


@contextlib.contextmanager
def _repo_first(tools_dir):
    """The repo's tools dir first on sys.path and its sibling names free in sys.modules; on exit
    sys.path is exactly as it was, the modules loaded from the tools dir are gone and the ones
    displaced are back.

    A sibling name (and its `name.` submodules) is displaced only when the loaded module came
    from outside the tools dir - a kit or stdlib copy; a builtin has no file and stays. Of the
    modules first imported during the hook, only those from the tools dir are dropped.

    Precondition: one thread. The changes are process-global, and an import on another thread
    while the hook runs would resolve against them."""
    root = os.path.normcase(os.path.abspath(tools_dir))
    saved_path = list(sys.path)
    saved_modules = dict(sys.modules)
    try:
        names = _siblings(tools_dir)
        for key, module in list(sys.modules.items()):
            top = key.split('.')[0]
            if top in names and _locations(module) and not _from_tools(module, root):
                del sys.modules[key]        # a kit or stdlib copy must not answer
        sys.path.insert(0, tools_dir)
        yield
    finally:
        sys.path[:] = saved_path
        for key, module in list(sys.modules.items()):
            if key not in saved_modules and _from_tools(module, root):
                del sys.modules[key]
        sys.modules.update(saved_modules)


def lines(tools_dir, hooks):
    """Every hook's lines in order, or its NOT CHECKED line."""
    out = []
    for hook in hooks:
        name, func = hook.split(':')
        path = os.path.join(tools_dir, name + '.py')
        try:
            if not os.path.isfile(path):
                raise FileNotFoundError('no %s' % path)
            with _repo_first(tools_dir), no_bytecode():
                spec = importlib.util.spec_from_file_location('preflight_hook_' + name, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                out.extend(str(l) for l in getattr(module, func)())
        except (Exception, SystemExit) as e:  # noqa: BLE001  (a hook's failure is a line)
            out.append('hook      NOT CHECKED - %s: %s: %s' % (hook, type(e).__name__, e))
    return out
