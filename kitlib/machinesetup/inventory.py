"""How far each Claude account folder has drifted from the shared setup (docs/machine-setup.md
"Account inventory"; tools/account_inventory.py). Read only: it writes nothing into any account
folder, the export or the machine config, and removes nothing. The owner runs the removals it
names by hand.

inventory(home, kit, export_dir=None) -> a dict: for each account folder (kitlib.pluginsource
account_folders), its launcher label, whether a credentials file exists (existence only, never read),
the pm-kit plugin's state (migrated to the export, refreshed: kitlib.pluginsource state.json),
the settings.json hooks that the managed settings template also carries (they run twice), and the
leftover links (kitlib.machinesetup.links, each with the command that removes the link only). A
folder other than the default one that no launcher account or org lists is flagged "not an account".

Every path shown has the home folder written as %USERPROFILE%, so the text carries no user name.
lines(result) is the plain text; the caller prints it. Nothing here prints or exits.
"""
import os
import types

from kitlib import pluginsource as ps
from kitlib.config import user
from kitlib.launcher import accounts as laccounts
from kitlib.launcher import settings as lsettings

from . import links, managed, settings
from .common import Plan, kit_file, norm

HOME_TEXT = '%USERPROFILE%'
NOT_ACCOUNT = 'not an account; check before retiring'
_IGNORED_LINK_NOTE = 'a real folder, left as it is'      # links.survey's note that asks nothing of the owner


def _labels(home, ls):
    """{normalised folder: [label]} from the launcher: account n is "n", an org n is "org n"."""
    out = {}
    if ls is None:
        return out
    for n in ls.account_numbers():
        for d in ls.account_dirs(n):
            out.setdefault(norm(d), []).append(n)
    for n in sorted(ls.orgs, key=int):
        out.setdefault(norm(ls.org_dir(n)), []).append('org %s' % n)
    return out


def _launcher(home):
    try:
        return lsettings.load(home), None
    except Exception as e:                        # a missing or bad launcher config: report it, go on
        return None, 'the launcher config cannot be read (%s); no folder has a label' % type(e).__name__


def _template_hooks(kit, home, export_dir):
    """(the managed settings template with {kit} and {plugin_export} filled in, why not). The
    template is read from the main checkout a plugin export stands for (managed.stable_kit)."""
    try:
        tkit = managed.stable_kit(kit)
        path = kit_file(tkit, managed.source())
        if not os.path.isfile(path):
            return None, 'the kit has no managed settings template; no duplicate hook check'
        with open(path, 'rb') as f:
            _, data = settings.read(f.read())
        if not isinstance(data, dict):
            return None, 'the managed settings template is not a JSON object'
        names = {'kit': tkit.replace('\\', '/'), 'pythonw': 'pythonw', 'plugin_export': export_dir or ''}
        return managed._substitute(data, lambda k: names[k]), None
    except Exception as e:
        return None, 'the managed settings template cannot be read (%s); no duplicate hook check' % e


def _duplicates(folder, template, home):
    """The hooks of `folder`'s settings.json that the template also has: [text], or [] ."""
    path = os.path.join(folder, 'settings.json')
    if template is None or not os.path.isfile(path):
        return []
    try:
        with open(path, 'rb') as f:
            _, data = settings.read(f.read())
    except (OSError, ValueError) as e:
        return ['settings.json cannot be read: %s' % e]
    if not isinstance(data, dict):
        return ['settings.json is not a JSON object']
    hooks = template.get('hooks')
    out = []
    for event in sorted(hooks) if isinstance(hooks, dict) else []:
        have = set(settings._signatures(template, event, home))
        seen = set()
        for sig in settings._signatures(data, event, home):
            if sig in have and sig not in seen:
                seen.add(sig)
                out.append('the %s hook %s is also in the managed settings; remove it from %s by hand'
                           % (event, settings.describe(sig), path))
    return out


def _links(folder, kit):
    plan = Plan(sections=('links',))
    links.survey(plan, folder, types.SimpleNamespace(kit=kit, old_kit=None))
    return [n for n in plan.notes['links'] if _IGNORED_LINK_NOTE not in n]


def _plugin(folder, state, state_why, export_dir):
    """{'migrated', 'refreshed', 'problems': [text]} for one folder."""
    if state is None:
        return {'migrated': False, 'refreshed': False, 'problems': [state_why]}
    migrated = ps.migrated(state, folder)
    rec = ps._record(state, folder)
    refreshed = migrated and rec.get('rc') == 0
    problems = []
    if not migrated:
        problems.append('pm-kit plugin not migrated to the export: %s' % ps.migrate_cmd(folder))
    elif not refreshed:
        problems.append('pm-kit plugin not refreshed to the current export: %s' % ps.refresh_cmd())
    if migrated:
        problems.extend(ps.marketplace_mismatches(export_dir, [folder]))
    return {'migrated': migrated, 'refreshed': refreshed, 'problems': problems}


def inventory(home, kit, export_dir=None):
    """The drift of each account folder; see the module doc. `export_dir` defaults to the machine's
    plugin export (kitlib.pluginsource load)."""
    home = os.path.abspath(home)
    notes = []
    if export_dir is None:
        try:
            export_dir = ps.load(home, kit=kit).export_dir
        except Exception as e:
            notes.append('the plugin export cannot be told (%s)' % e)
    state, state_why = None, None
    if export_dir is None:
        state_why = 'no plugin export known'
    else:
        try:
            state = ps.read_state(export_dir)
            if state is None:
                state_why = 'pm-kit plugin: no export state at %s yet: %s' % (export_dir, ps.refresh_cmd(True))
        except ps.StateError as e:
            state_why = 'pm-kit plugin: %s' % e
    ls, why = _launcher(home)
    if why:
        notes.append(why)
    labels = _labels(home, ls)
    template, why = _template_hooks(kit, home, export_dir)
    if why:
        notes.append(why)
    folders = []
    for f in ps.account_folders(home):
        label = labels.get(norm(f))
        unlisted = not label and os.path.basename(f) != lsettings.DEFAULT_FOLDER
        if not label:
            label = [] if unlisted else [laccounts.DEFAULT_LABEL]
        plug = _plugin(f, state, state_why, export_dir)
        issues = list(plug['problems'])
        dups = _duplicates(f, template, home)
        lnk = _links(f, kit)
        issues.extend(dups)
        issues.extend(lnk)
        if unlisted:
            issues.append(NOT_ACCOUNT)
        folders.append({'folder': f, 'label': ', '.join(label) or None,
                        'signed_in': laccounts.signed_in(f), 'plugin': plug,
                        'duplicates': dups, 'links': lnk, 'not_account': unlisted, 'issues': issues})
    res = {'folders': folders, 'notes': notes,
           'summary': {'folders': len(folders),
                       'with_findings': sum(1 for x in folders if x['issues']),
                       'findings': sum(len(x['issues']) for x in folders)}}
    return _hide(res, home)


def _hide(obj, home):
    """`obj` with the home folder written as HOME_TEXT in every string (either slash)."""
    if isinstance(obj, str):
        for h in (home, home.replace('\\', '/')):
            i = obj.lower().find(h.lower())
            while i >= 0:
                obj = obj[:i] + HOME_TEXT + obj[i + len(h):]
                i = obj.lower().find(h.lower())
        return obj
    if isinstance(obj, list):
        return [_hide(x, home) for x in obj]
    if isinstance(obj, dict):
        return {k: _hide(v, home) for k, v in obj.items()}
    return obj


def lines(res):
    """The plain-text report of an inventory() result."""
    out = []
    for f in res['folders']:
        out.append('%s  account: %s  signed in: %s' % (f['folder'], f['label'] or '(none)',
                                                       'yes' if f['signed_in'] else 'no'))
        p = f['plugin']
        out.append('  pm-kit plugin: migrated %s, refreshed %s'
                   % ('yes' if p['migrated'] else 'no', 'yes' if p['refreshed'] else 'no'))
        out.extend('  ' + i for i in f['issues'])
        if not f['issues']:
            out.append('  nothing to do')
    out.extend('note: ' + n for n in res['notes'])
    s = res['summary']
    if s['findings']:
        out.append('summary: %d folder(s), %d need attention (%d finding(s)); nothing was changed, '
                   'run the removals above by hand' % (s['folders'], s['with_findings'], s['findings']))
    else:
        out.append('summary: %d folder(s), nothing to do' % s['folders'])
    return out
