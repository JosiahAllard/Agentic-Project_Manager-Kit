"""A repository's report hooks (`.kit.json` "preflight".report_hooks): `module:function`, the
module a file in the repo's tools dir, the function returning the lines to print (for example
daily_full:summary_lines). Whatever goes wrong - no file, an import error, a raise - is one
`hook NOT CHECKED` line: preflight reports, it never dies on one reading.

The repo's tools dir goes on sys.path (appended, so the kit stays first) for the hook's own
sibling imports.
"""
import importlib.util
import os
import sys


def lines(tools_dir, hooks):
    """Every hook's lines in order, or its NOT CHECKED line."""
    out = []
    for hook in hooks:
        name, func = hook.split(':')
        path = os.path.join(tools_dir, name + '.py')
        try:
            if not os.path.isfile(path):
                raise FileNotFoundError('no %s' % path)
            if tools_dir not in sys.path:
                sys.path.append(tools_dir)
            spec = importlib.util.spec_from_file_location('preflight_hook_' + name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            out.extend(str(l) for l in getattr(module, func)())
        except Exception as e:  # noqa: BLE001  (a hook's failure is a line, never the run's)
            out.append('hook      NOT CHECKED - %s: %s: %s' % (hook, type(e).__name__, e))
    return out
