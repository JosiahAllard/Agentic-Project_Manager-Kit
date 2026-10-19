"""The kit plugin, enabled per account in the default mode (docs/machine-setup.md "Default mode").
survey is the default host's (Claude) step; survey_hosts runs it and notes every other registered
host that machine setup does not install yet (installer).

The plugin's source is the export of the kit's pushed main (docs/plugin-source.md), never the kit
checkout. For each config folder, enabledPlugins["pm-kit@pm-kit"] in its settings.json and the
export's state.json decide: true and recorded as migrated is nothing to do; false (the owner turned
it off) or any other value is a note and is left alone; absent, or true but installed some other
way (from the kit checkout, as before the export), is one change.

Those changes follow tools/plugin_refresh.py --migrate --apply: first one change that makes the
export (kitlib.pluginsource.ensure_export: it runs no claude, and refuses when the kit has not
fetched its remote's main), then per folder kitlib.pluginsource.refresh with migrate, which runs
`claude plugin marketplace remove pm-kit` (its failure tolerated), `marketplace add <export root>`
and `install pm-kit@pm-kit -s user` through kitlib.pluginsource.claude.Runner (CLAUDE_CONFIG_DIR set
to the folder, removed for ~/.claude) and records the folder as migrated. The CLI may rewrite other
keys of settings.json, so each folder's change copies settings.json to a .bak first, and then checks
enabledPlugins is true. With no `claude` on PATH each folder's change becomes a note giving the
migrate command.

Only folders in the refresh set (kitlib.pluginsource.claude.account_folders) are installed: a
--config-dir folder outside it would never be updated by tools/plugin_refresh.py or pm_start, so it
is a note giving the hand commands, and the export change is still made for them.
"""
import json
import os

from .common import free_name

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
    from kitlib import pluginsource as ps
    from kitlib.config import ConfigError
    pid = ps.PLUGIN_ID
    candidates = []
    for d in ctx.config_dirs:
        path = os.path.join(d, 'settings.json')
        have = enabled(path, pid)
        if have is None:
            continue
        if have is not ABSENT and have is not True:
            plan.note('plugin', '%s: enabledPlugins["%s"] is %s; left as is' % (path, pid, json.dumps(have)))
            continue
        candidates.append((d, path, have))
    if not candidates:
        return
    try:
        s = ps.load(ctx.home, kit=ctx.kit)
        state = ps.read_state(s.export_dir)
    except (ps.RefreshError, ps.StateError, ConfigError, OSError) as e:
        plan.refuse('the plugin export (docs/plugin-source.md): %s' % e)
        return
    candidates, outside = _split(candidates, ctx.home)
    for d in outside:
        plan.note('plugin', _outside_note(d, s.export_dir))
    todo = []
    for d, path, have in candidates:
        if have is True and ps.migrated(state, d):
            continue
        if have is True:
            plan.note('plugin', '%s: the kit plugin is enabled but was not installed from the export '
                                '(installed from the kit checkout, as before it); it is installed again '
                                'from the export. The old copy in its plugins cache is left: '
                                'docs/plugin-source.md "Migrating"' % d)
        todo.append((d, path))
    runner = ctx.host.plugin_runner(ctx.home) if todo else None
    if todo and (runner is None or not runner.available()):
        for d, _path in todo:
            plan.note('plugin', '%s: the kit plugin is not enabled and no claude CLI is on PATH; once it '
                                'is, run %s' % (d, ps.migrate_cmd(d)))
        todo = []
    if not todo and not outside:                 # an outside folder's hand steps need the export
        return
    sha, _why = ps.tracking_main(ctx.kit, s.remote)
    at = sha[:12] if sha else 'not fetched yet'
    plan.add('plugin', 'export %s/%s (%s here; the remote is read when it runs) into %s and point its '
                       'marketplace at it (kitlib.pluginsource.ensure_export; runs no claude; refused when '
                       'the kit checkout has not fetched that commit)' % (s.remote, ps.MAIN, at, s.export_dir),
             lambda: ps.ensure_export(ctx.kit, s),
             undo='nothing to undo by hand: the export folder under %s is what tools/plugin_refresh.py '
                  'makes and prunes' % s.export_dir)
    for d, path in todo:
        bak = free_name('%s.machine-setup-%s-plugin.bak' % (path, ctx.today))
        env = 'CLAUDE_CONFIG_DIR unset' if _is_default(d, ctx.home) else 'CLAUDE_CONFIG_DIR=%s' % d
        steps = '; '.join('claude %s' % ' '.join(st) for st in ps.migrate_steps(s.export_dir))
        plan.add('plugin', '%s: enable the kit plugin %s from the export (%s, the remove\'s failure '
                           'tolerated; %s; settings.json backed up first: %s)'
                           % (d, pid, steps, env, os.path.basename(bak)),
                 lambda d=d, path=path, bak=bak: _install(ctx, s, runner, d, path, pid, bak),
                 undo='copy "%s" over "%s"; to run it again: %s (it removes the marketplace first, its '
                      'failure tolerated)' % (bak, path, ps.migrate_cmd(d)))


def _split(candidates, home):
    """(the candidates in the refresh set, the folders of the rest). The refresh set is
    kitlib.pluginsource.claude.account_folders: the folders tools/plugin_refresh.py and pm_start
    keep up to date."""
    from kitlib import pluginsource as ps
    from kitlib.pluginsource import claude as pclaude
    keys = set(ps.folder_key(f) for f in pclaude.account_folders(home))
    inside = [c for c in candidates if ps.folder_key(c[0]) in keys]
    return inside, [c[0] for c in candidates if ps.folder_key(c[0]) not in keys]


def _outside_note(d, root):
    from kitlib import pluginsource as ps
    steps = '; '.join('claude %s' % ' '.join(st) for st in ps.migrate_steps(root))
    update = '; '.join('claude %s' % ' '.join(st) for st in ps.UPDATE_STEPS)
    return ('%s is outside the plugin refresh set (kitlib.pluginsource.claude.account_folders: ~/.claude, '
            '~/.claude-* and the launcher\'s account and org folders), so tools/plugin_refresh.py and '
            'pm_start never install or update the kit plugin there and this step does not install it. '
            'By hand, after this run\'s export change, with CLAUDE_CONFIG_DIR=%s: %s (the remove may '
            'fail); after each kit update: %s' % (d, d, steps, update))


def _is_default(d, home):
    from kitlib.pluginsource import claude as pclaude
    return pclaude.is_default(d, home)


def _install(ctx, s, runner, d, path, pid, bak):
    from kitlib import pluginsource as ps
    if os.path.isfile(path):
        with open(path, 'rb') as f:
            raw = f.read()
        with open(bak, 'xb') as f:
            f.write(raw)
    lines = []
    rc = ps.refresh(ctx.kit, s, [d], lines.append, apply=True, migrate=True, runner=runner)
    if rc != 0:
        bad = [l for l in lines if l.startswith(('REFUSED', 'FAILED'))] or lines[-1:]
        raise RuntimeError('; '.join(bad) or 'the migrate exited %d' % rc)
    if enabled(path, pid) is not True:
        raise RuntimeError('claude exited 0, but enabledPlugins["%s"] in %s is not true' % (pid, path))


def installer(name):
    """The plugin install step for the hosts.json host `name`, or None. The default host's (the
    host whose account settings machine setup edits) is survey; no other host has one yet: Codex's
    plugin is a review template, not installed on any machine (its README, "Not verified": plugin
    loading, hook trust, cached paths). A host gains one here once a live install is verified."""
    from kitlib.host import registry
    return survey if name == registry.default() else None


def survey_hosts(plan, ctx):
    """The plugin step for every registered host (kitlib.host.registry), in hosts.json order; a
    host with no installer is a note naming its README."""
    from kitlib.host import registry
    for name in registry.names():
        install = installer(name)
        if install is not None:
            install(plan, ctx)
            continue
        plan.note('plugin', '%s: not installed. Machine setup installs no plugin for this host yet: '
                            'its files are review templates, not installed (hosts/%s/README.md)'
                  % (name, name))
