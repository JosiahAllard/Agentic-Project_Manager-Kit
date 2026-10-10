"""A repository's report hooks (`.kit.json` "preflight".report_hooks): `module:function`, the
module a file in the repo's tools dir, the function returning the lines to print (for example
daily_full:summary_lines). Whatever goes wrong - no file, an import error, a raise - is one
`hook NOT CHECKED` line: preflight reports, it never dies on one reading.

While a hook loads and runs, the repo's tools dir is first on sys.path, so `import pieces` in a
hook gets the repo's own pieces.py even when the kit has a tool of that name. Afterwards sys.path
is put back, the sys.modules entries the hook added are dropped, and any kit module the repo's
sibling displaced for the hook is restored, so kit code never sees the repo's module. No bytecode
is written while a hook loads and runs (kitlib.config.policy.no_bytecode), so no `__pycache__`
appears in the repo's tools dir.
"""
import contextlib
import importlib.util
import os
import sys

from kitlib.config.policy import no_bytecode


@contextlib.contextmanager
def _repo_first(tools_dir):
    """The repo's tools dir first on sys.path and its sibling names free in sys.modules; on exit
    both are exactly as they were."""
    saved_path = list(sys.path)
    saved_modules = dict(sys.modules)
    try:
        for entry in os.listdir(tools_dir):
            sibling = entry[:-3] if entry.endswith('.py') else entry
            sys.modules.pop(sibling, None)       # a kit module of that name must not answer
        sys.path.insert(0, tools_dir)
        yield
    finally:
        sys.path[:] = saved_path
        for name in [n for n in sys.modules if n not in saved_modules]:
            del sys.modules[name]
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
        except Exception as e:  # noqa: BLE001  (a hook's failure is a line, never the run's)
            out.append('hook      NOT CHECKED - %s: %s: %s' % (hook, type(e).__name__, e))
    return out
