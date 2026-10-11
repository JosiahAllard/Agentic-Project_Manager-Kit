"""The kit plugin, enabled per account in fresh mode (docs/machine-setup.md "Fresh machine (--fresh)").

The plugin id is `<plugin>@<marketplace>` from the kit's .claude-plugin/marketplace.json. For each
config folder, enabledPlugins[<id>] in its settings.json decides: true is nothing to do; false (the
owner turned it off) or any other value is a note and is left alone; absent is one change, which runs
docs/install.md "Plugin install (local marketplace)" through Host.plugin_install with
CLAUDE_CONFIG_DIR set to that folder. That CLI also writes extraKnownMarketplaces and the folder's
plugins/ files, and may rewrite other keys of settings.json, so the change copies settings.json to a
.bak first and then checks enabledPlugins[<id>] is true. With no `claude` on PATH the change becomes a
note giving the commands.
"""
import json
import os

from .common import free_name

MARKETPLACE = '.claude-plugin/marketplace.json'


def plugin_id(kit):
    """'<plugin>@<marketplace>' from the kit's marketplace manifest; raises OSError or ValueError."""
    with open(os.path.join(kit, *MARKETPLACE.split('/')), encoding='utf-8-sig') as f:
        m = json.load(f)
    market = m.get('name') if isinstance(m, dict) else None
    plugins = m.get('plugins') if isinstance(m, dict) else None
    name = plugins[0].get('name') if isinstance(plugins, list) and plugins and isinstance(plugins[0], dict) else None
    if not isinstance(market, str) or not market or not isinstance(name, str) or not name:
        raise ValueError('it names no marketplace and first plugin')
    return '%s@%s' % (name, market)


ABSENT = object()


def enabled(path, pid):
    """enabledPlugins[pid] in the settings.json at `path`: ABSENT when the file or the key is not
    there, None when the file cannot be read as a JSON object (the settings step refuses it)."""
    if not os.path.isfile(path):
        return ABSENT
    try:
        with open(path, 'rb') as f:
            data = json.loads(f.read().decode('utf-8-sig'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ep = data.get('enabledPlugins')
    return ep.get(pid, ABSENT) if isinstance(ep, dict) else ABSENT


def survey(plan, ctx):
    try:
        pid = plugin_id(ctx.kit)
    except (OSError, ValueError) as e:
        plan.refuse('cannot read the kit plugin id from %s: %s' % (MARKETPLACE, e))
        return
    try:
        exe = ctx.host.claude()
    except (OSError, RuntimeError):
        exe = None
    for d in ctx.config_dirs:
        path = os.path.join(d, 'settings.json')
        have = enabled(path, pid)
        if have is True or have is None:
            continue
        if have is not ABSENT:
            plan.note('plugin', '%s: enabledPlugins["%s"] is %s; left as is' % (path, pid, json.dumps(have)))
            continue
        cmds = 'claude plugin marketplace add "%s", then claude plugin install %s' % (ctx.kit, pid)
        if not exe:
            plan.note('plugin', '%s: the kit plugin is not enabled and no claude CLI is on PATH; with '
                                'CLAUDE_CONFIG_DIR=%s run %s' % (d, d, cmds))
            continue
        bak = free_name('%s.machine-setup-%s-plugin.bak' % (path, ctx.today))
        plan.add('plugin', '%s: enable the kit plugin %s (%s, with CLAUDE_CONFIG_DIR=%s; settings.json '
                           'backed up first: %s)' % (d, pid, cmds, d, os.path.basename(bak)),
                 lambda d=d, path=path, bak=bak: _install(ctx, d, path, pid, bak),
                 undo='with CLAUDE_CONFIG_DIR=%s run claude plugin uninstall %s, then copy "%s" over "%s"'
                      % (d, pid, bak, path))


def _install(ctx, d, path, pid, bak):
    if os.path.isfile(path):
        with open(path, 'rb') as f:
            raw = f.read()
        with open(bak, 'xb') as f:
            f.write(raw)
    ctx.host.plugin_install(d, ctx.kit, pid)
    if enabled(path, pid) is not True:
        raise RuntimeError('the claude CLI exited 0, but enabledPlugins["%s"] in %s is not true' % (pid, path))
