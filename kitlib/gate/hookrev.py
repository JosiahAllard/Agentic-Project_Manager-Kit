"""The repo hook module as one commit has it: a landing's merged-tree gate runs the GATE_CHECKS
of --expect-main's tree, the same commit its config is read from, so a branch cannot loosen a
check by changing the hook module it lands (docs/gate.md "Landing").

Only the module's own file is taken from the commit. An import inside it resolves as Python
resolves it in the gating process (the merged checkout's files are not on sys.path), so a hook
module that imports sibling files from its tools folder is not supported here. Its __file__ is
the path the module has in the gated checkout (`root`), so paths derived from it are real; its
tracebacks name `<rev10>:<rel>`, the commit its code came from.
"""
import hashlib
import os
import sys
import types

from kitlib.config import policy
from kitlib.config.types import ConfigError
from kitlib.git import cmd


def module_path(cfg):
    """`(repo-relative posix path of the hook module, its options)`, or None when none is named."""
    h = cfg.hook()
    if not h:
        return None
    tools = cfg.tools_dir.replace('\\', '/').strip('/')
    rel = '%s/%s.py' % (tools, h['module']) if tools else h['module'] + '.py'
    return rel, h['options']


def load_module_at(cfg, root, rev):
    """`(module, options)` of the hook module in commit `rev`, or None when `.kit.json` names none.
    ConfigError when it is named but not in that commit, or fails to load (a named hook never
    silently becomes no hook)."""
    found = module_path(cfg)
    if not found:
        return None
    rel, options = found
    where = '%s:%s' % (rev[:10], rel)
    data, why = cmd.run(['cat-file', 'blob', '%s:%s' % (rev, rel)], root)
    if data is None:
        raise ConfigError('.kit.json names the hook module %s, which commit %s does not have (%s)'
                          % (rel, rev[:10], why))
    name = 'kit_repo_hook_rev_%s' % hashlib.sha256(
        ('%s\0%s\0' % (rev, rel)).encode('utf-8') + data).hexdigest()[:12]
    module = types.ModuleType(name)
    # The code is the commit's; __file__ is the work-tree path, so a hook that finds files from
    # os.path.dirname(__file__) gets a real directory. Tracebacks and compile errors still name
    # the commit: `where` is the compile filename.
    module.__file__ = os.path.join(root, *rel.split('/'))
    sys.modules[name] = module
    try:
        with policy.no_bytecode():    # as the work-tree loader: an import writes no __pycache__
            exec(compile(data, where, 'exec'), module.__dict__)
    except BaseException as e:    # the repo's code: any failure is a config failure
        if sys.modules.get(name) is module:
            del sys.modules[name]
        if not isinstance(e, (Exception, SystemExit)):
            raise                     # Ctrl+C stops the caller; no half-loaded module is left
        raise ConfigError('the hook module %s failed to load: %s: %s' % (where, type(e).__name__, e))
    return module, options


def load_gate_checks_at(cfg, root, rev):
    """`(checks, options)` as kitlib.config.policy.load_gate_checks gives them, from commit `rev`."""
    found = load_module_at(cfg, root, rev)
    module, options = found if found else (None, {})
    checks = policy.gate_checks(module) if module is not None else {}
    wanted = [(s['name'], s['check']) for s in cfg.section('gate')['steps'] or [] if s.get('check')]
    missing = ['%s (step %s)' % (c, n) for n, c in wanted if c not in checks]
    if missing:
        raise ConfigError('the hook module provides no gate check %s' % ', '.join(missing))
    return checks, options
