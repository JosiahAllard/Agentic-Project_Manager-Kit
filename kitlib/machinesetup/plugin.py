"""The kit plugin, enabled per account in fresh mode (docs/machine-setup.md "Fresh machine (--fresh)").

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
    if not todo:
        return
    runner = ctx.host.plugin_runner(ctx.home)
    if runner is None or not runner.available():
        for d, _path in todo:
            plan.note('plugin', '%s: the kit plugin is not enabled and no claude CLI is on PATH; once it '
                                'is, run %s' % (d, ps.migrate_cmd(d)))
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
