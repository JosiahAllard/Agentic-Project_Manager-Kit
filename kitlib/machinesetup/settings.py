"""Per-account settings.json (docs/machine-setup.md "Settings"): drop the old kit's PreToolUse hooks,
add the guard's entry as `boot/hook.py <host> --settings-entry` prints it, re-point the old kit's
SessionStart hooks and statusline scripts to this kit, write a SessionStart hook that runs the kit's
hook entry in shell form (`python <kit>/boot/hook.py <host> SessionStart <part>`) in exec form (the
guard entry's interpreter, `args: [<script>, <words>...]`), and note a settings hook that the kit's
plugin also carries (it runs twice once the plugin is enabled). statusLine stays in shell form:
Claude Code gives it no `args` field. <host> is the default host (hosts.json), whose settings these
are.

In fresh mode (ctx.fresh, no older kit clone) none of that rewriting runs: the guard's entry is
appended where it is absent (a missing settings.json is created), and survey_pm appends the PM
SessionStart hook to ctx.pm_settings alone.

Every path in a hook's `command` and `args` and in statusLine.command is read; one that resolves
into the old kit clone is either in REMOVE (its hook goes; a group or event left empty goes too),
in REPOINT (rewritten to this kit's file, forward slashes, quotes kept; a retired hook script to
ENTRY and its words) or refused, named. The JSON is edited as data and written back in the layout
it was read in (indent, ASCII escaping, line ends, BOM, final newline), so a dry run's diff shows
only the changed lines.

Paths through a tools link (repoint_links): machine setup makes no link (kitlib.machinesetup.links
reports the ones left), so a hook or statusLine path through `<config>/tools/<name>/...`, a name in
links.TOOL_LINKS, is re-pointed to the kit file that link reached: `.../tools/usage/x.ps1` becomes
`<kit>/optional/usage/x.ps1`. `<config>` is any config folder of the run or of the home. It runs in
every mode and in the PM settings, so a status line keeps working once the link goes; a kit with no
such file is refused, named.

Moved hook paths (docs/machine-setup.md "Moved hook paths"): a hook path to one of this kit's
RETIRED hook scripts, in HOOKS (`<kit>/hosts/claude/shell/hooks/`, plan steps 7 to 13) or in
MOVED_HOOKS (`<kit>/claude/hooks/`, before the hosts/ move), is re-pointed to the kit's one hook
entry, ENTRY, followed by the host and the script's words (RETIRED): `guard.py` becomes
`boot/hook.py <host> PreToolUse`. This runs in every mode and in the PM settings too, so a settings
file written before plan step 14 keeps its one guard entry and its PM hook. Any other name in those
folders, or a kit with no ENTRY, is refused.
"""
import difflib
import json
import os
import re

from kitlib.host import registry

from .common import MOVED_FROM, SHELL, free_name, kit_file, norm, under
from . import links

ENTRY = 'boot/hook.py'                                               # every host's one hook entry
HOOKS = SHELL + '/hooks'
MOVED_HOOKS = MOVED_FROM + '/hooks'
# The hook scripts ENTRY replaced (plan step 14), each with the words ENTRY now takes after the host.
RETIRED = {
    'guard.py': ('PreToolUse',),
    'pm_session_start.py': ('SessionStart', 'pm'),
    'coordinator_session_start.py': ('SessionStart', 'coordinator'),
}
PM_WORDS = RETIRED['pm_session_start.py']
REMOVE = ('no_root_search.py', 'main_checkout_redirect_guard.py')
REPOINT = {
    'pm_session_start.py': ENTRY,
    'coordinator_session_start.py': ENTRY,
    'statusline-usage.ps1': 'optional/usage/statusline-usage.ps1',
    'write-usage-status.ps1': 'optional/usage/write-usage-status.ps1',
}
GUARD = ENTRY
PYTHONS = ('python', 'python.exe', 'pythonw', 'pythonw.exe')
PLUGIN_MANIFEST = '.claude-plugin/plugin.json'
PLUGIN_ROOT = re.compile(r'\$\{CLAUDE_PLUGIN_ROOT\}|\$CLAUDE_PLUGIN_ROOT\b')
TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|[^\s"\']+')
WORD = re.compile(r'[A-Za-z0-9_.-]+')        # a word ENTRY takes: a host, an event, a part


def survey(plan, d, ctx):
    path = os.path.join(d, 'settings.json')
    if ctx.fresh:
        _survey_fresh(plan, path, ctx)
        return
    if not os.path.isfile(path):
        plan.note('settings', '%s: no settings.json; nothing re-pointed and no guard entry added' % d)
        return
    with open(path, 'rb') as f:
        raw = f.read()
    try:
        layout, data = read(raw)
    except ValueError as e:
        plan.refuse('%s is not JSON this step can edit: %s' % (path, e))
        return
    if not isinstance(data, dict):
        plan.refuse('%s does not hold a JSON object' % path)
        return
    ed = _Editor(ctx, d, path, plan)
    hooks = data.get('hooks')
    if hooks is not None and not isinstance(hooks, dict):
        plan.refuse('%s: "hooks" is not a JSON object; this step cannot read it' % path)
        return
    try:
        ed.did.extend(repoint_moved(data, ctx))
        ed.did.extend(repoint_links(data, ctx))
    except ValueError as e:
        plan.refuse('%s: %s' % (path, e))
        return
    for event in list(hooks or {}):
        groups = hooks[event]
        if not isinstance(groups, list):
            plan.refuse('%s: hooks.%s is not a list; this step cannot read it' % (path, event))
            return
        emptied = 0
        for g in list(groups):
            hs = g.get('hooks') if isinstance(g, dict) else None
            if not isinstance(hs, list):
                plan.refuse('%s: a hooks.%s group has no "hooks" list; this step cannot read it' % (path, event))
                return
            dropped = 0
            for h in list(hs):
                if isinstance(h, dict) and ed.hook(event, h):
                    hs.remove(h)
                    dropped += 1
            if dropped and not hs:                                   # only a group this step emptied
                groups.remove(g)
                emptied += 1
        if emptied and not groups:                                   # only an event this step emptied
            del hooks[event]
    sl = data.get('statusLine')
    if isinstance(sl, dict) and isinstance(sl.get('command'), str):
        sl['command'] = ed.rewrite(sl['command'], 'statusLine.command', removable=False)[0]
    _note_plugin_twins(plan, path, data, ctx)
    if _wants_guard(plan, path, data, ctx):
        try:
            entry = ed.guard_entry()
        except (OSError, RuntimeError, ValueError) as e:
            plan.refuse('cannot read boot/hook.py --settings-entry: %s' % e)
            return
        if not isinstance(data.get('hooks'), dict):
            data['hooks'] = {}
        for event, groups in entry.items():
            data['hooks'].setdefault(event, []).extend(groups)
        ed.did.append('add the guard\'s PreToolUse entry')
    if not ed.did:
        return
    _plan_write(plan, ctx, path, raw, layout, data, ed.did)


def _wants_guard(plan, path, data, ctx):
    """True when the settings.json at `path` (`data`) should gain the guard's entry: it has none,
    and the managed settings file (ctx.managed_data, read by managed.survey_live) runs no kit's
    guard. One that has it while the managed file does too is noted: the guard runs twice."""
    from . import managed
    guard = norm(os.path.join(ctx.kit, *GUARD.split('/')))
    here = any(norm(p) == guard for p in _hook_paths(data, 'PreToolUse', ctx.home))
    data_m = getattr(ctx, 'managed_data', None)
    where = managed.guard_where(data_m, ctx.kit, ctx.home) if data_m is not None else []
    if here and where == 'kit':
        plan.note('settings', '%s: the guard\'s entry is also in the managed settings file %s, so it '
                              'runs twice; remove it from this file by hand' % (path, ctx.managed_file))
    return not here and not where


def _note_plugin_twins(plan, path, data, ctx, skip=()):
    rel, plugin = plugin_hooks(ctx.kit, ctx.home)
    for event in sorted(e for e in plugin if e not in skip):
        seen = set()
        for sig in _signatures(data, event, ctx.home):
            if sig in plugin[event] and sig not in seen:
                seen.add(sig)
                plan.note('settings', '%s: the %s hook %s is also the kit plugin\'s hook (%s); with the plugin '
                                      'enabled it runs twice, so remove this entry in the same step as the '
                                      'plugin install' % (path, event, describe(sig), rel))


def _plan_write(plan, ctx, path, raw, layout, data, did):
    """Plan the write of `data` to `path` in `layout`: a .bak and a replace when the file is there
    (`raw`), a new file when it is not (`raw` None, fresh mode only)."""
    new = write(layout, data)
    if new == raw:
        return
    if raw is None:
        diff = [l.rstrip('\r\n') for l in difflib.unified_diff(
            [], new.decode('utf-8').splitlines(True), path + ' (none)', path + ' (after)', n=1)]
        plan.add('settings', '%s: create it; %s' % (path, '; '.join(did)),
                 lambda: _create(path, new), undo='delete "%s"' % path, detail=diff)
        return
    shared = shared_link(path)
    if shared:
        plan.refuse('%s is %s; replacing it would split it from the file it shares (edit it by hand: %s)'
                    % (path, shared, '; '.join(did)))
        return
    if not layout['exact']:
        plan.note('settings', '%s: its layout is not one json.dumps writes; the whole file is '
                              'rewritten (indent %r)' % (path, layout['indent']))
    old_text, new_text = raw.decode('utf-8-sig'), new.decode('utf-8-sig')
    diff = [l.rstrip('\r\n') for l in difflib.unified_diff(
        old_text.splitlines(True), new_text.splitlines(True), path, path + ' (after)', n=1)]
    bak = free_name('%s.machine-setup-%s.bak' % (path, ctx.today))
    plan.add('settings', '%s: %s (backup: %s)' % (path, '; '.join(did), os.path.basename(bak)),
             lambda: _write(path, raw, new, bak), undo='copy "%s" over "%s"' % (bak, path),
             detail=diff)


# Fresh mode (--fresh): append only. No entry is removed, re-pointed or reshaped, except a hook path
# the hosts/ move changed (repoint_moved); a missing file is created in NEW_LAYOUT; the PM
# SessionStart hook goes to --pm-settings alone. The kit SessionStart entries the kit plugin also
# runs are the plugin step's (plugin.py, plugin_edit), not this one's: in a folder that step owns
# they get no twin note here.
NEW_LAYOUT = {'bom': False, 'crlf': False, 'final': True, 'indent': 2, 'ascii': False, 'exact': True}
PM_HOOK = ENTRY
PM_MATCHER = 'startup|resume|compact'


def _survey_fresh(plan, path, ctx):
    got = _load(plan, path)
    if got is None:
        return
    raw, layout, data = got
    try:
        did = repoint_moved(data, ctx) + repoint_links(data, ctx)
    except ValueError as e:
        plan.refuse('%s: %s' % (path, e))
        return
    from . import plugin                                             # it reads this module
    owned = plugin.owns_settings(os.path.dirname(path), path, ctx)
    _note_plugin_twins(plan, path, data, ctx, skip=(REPLACED_EVENT,) if owned else ())
    if _wants_guard(plan, path, data, ctx):
        try:
            entry = ctx.host.guard_entry(ctx.kit)
        except (OSError, RuntimeError, ValueError) as e:
            plan.refuse('cannot read boot/hook.py --settings-entry: %s' % e)
            return
        if not _append(plan, path, data, entry):
            return
        did.append('add the guard\'s PreToolUse entry')
    if did:
        _plan_write(plan, ctx, path, raw, layout, data, did)


def host_name():
    """The host whose account settings these are: hosts.json's default host."""
    return registry.default()


def entry_words(base):
    """The words after ENTRY for the RETIRED hook script `base`: the host, then its own."""
    return [host_name()] + list(RETIRED[base])


def _moved(tok, ctx):
    """(the folder, the script's name) when `tok` is a path to a file in this kit's HOOKS or
    MOVED_HOOKS folder, other than the hooks.json manifest; else None."""
    p = expand(tok, ctx.home)
    if not p:
        return None
    for folder in (HOOKS, MOVED_HOOKS):
        old = kit_file(ctx.kit, folder)
        if under(p, old) and norm(p) != norm(old) and os.path.basename(p).lower() != 'hooks.json':
            return folder, os.path.relpath(norm(p), norm(old))
    return None


def _sub_moved(s, ctx, event, did, whole):
    """(`s` with each retired hook script path of this kit (_moved) replaced by ENTRY, [the words a
    whole argument adds after it]). In a command line the words follow the path; for a whole
    argument (`args` item) they are returned, for the caller to insert after it."""
    spans = [(0, len(s), s)] if whole else [(m.start(), m.end(), m.group()) for m in TOKEN.finditer(s)]
    out, last, words = [], 0, []
    for a, b, tok in spans:
        q = tok[0] if tok[:1] in '"\'' and len(tok) > 1 and tok[-1] == tok[0] else ''
        bare = tok[1:-1] if q else tok
        moved = _moved(bare, ctx)
        if moved is None:
            continue
        folder, name = moved
        if name.lower() not in RETIRED:
            raise ValueError('a %s hook runs %s, a hook script the kit no longer has; %s runs every hook '
                             '(re-point it by hand)' % (event, bare, ENTRY))
        new = kit_file(ctx.kit, ENTRY)
        if not os.path.isfile(new):
            raise ValueError('a %s hook runs %s, now %s, which the kit lacks' % (event, bare, new))
        added = entry_words(name.lower())
        out.append(s[last:a] + q + new.replace('\\', '/') + q + ('' if whole else ' ' + ' '.join(added)))
        words = added if whole else words
        last = b
        did.append('re-point the %s hook %s from %s/ to %s %s' % (event, name, folder, ENTRY, ' '.join(added)))
    out.append(s[last:])
    return ''.join(out), words


def repoint_moved(data, ctx):
    """Re-point, in place, every hook path in `data` (any event; `command` and `args`) that names a
    RETIRED hook script in this kit's HOOKS or MOVED_HOOKS folder to ENTRY and its words; [what it
    did, one line per path]. ValueError for another name there, or a kit with no ENTRY. Runs in
    every mode: a settings file written before plan step 14 otherwise gets a second guard entry, or
    keeps a hook path the kit no longer has (a missing guard script blocks every tool call)."""
    did = []
    hooks = data.get('hooks') if isinstance(data, dict) else None
    for event in sorted(hooks) if isinstance(hooks, dict) else []:
        for h in _hooks(data, event):
            if isinstance(h.get('command'), str):
                h['command'] = _sub_moved(h['command'], ctx, event, did, False)[0]
            if isinstance(h.get('args'), list):
                args = []
                for a in h['args']:
                    if isinstance(a, str):
                        a, words = _sub_moved(a, ctx, event, did, True)
                        args += [a] + words
                    else:
                        args.append(a)
                h['args'] = args
    return did


def _link_dirs(ctx):
    """The config folders whose `tools` links a settings path may run through: the run's, then
    every one under the home (a status line may name another account's folder)."""
    from . import default_config_dirs                                # the package imports this module
    out, seen = [], set()
    for d in list(ctx.config_dirs) + default_config_dirs(ctx.home):
        if norm(d) not in seen:
            seen.add(norm(d))
            out.append(d)
    return out


def _through_link(p, dirs, kit):
    """(the TOOL_LINKS name, the kit file) when the absolute path `p` runs through
    `<dir>/tools/<name>` for one of `dirs`; else None. The rest of the path keeps its spelling."""
    for d in dirs:
        for name, rel in links.TOOL_LINKS:
            link = os.path.join(d, 'tools', name)
            if under(p, link):
                rest = os.path.relpath(os.path.normpath(p), os.path.normpath(link))
                after = kit_file(kit, rel)
                return name, (after if rest == '.' else os.path.join(after, rest))
    return None


def _sub_links(s, ctx, dirs, where, did, whole):
    """`s` with each path through a tools link (_through_link) replaced by the kit file, forward
    slashes, its quotes kept; an unquoted path that gains a space is quoted (a whole argument is
    never quoted). ValueError when the kit has no such file."""
    spans = [(0, len(s), s)] if whole else [(m.start(), m.end(), m.group()) for m in TOKEN.finditer(s)]
    out, last = [], 0
    for a, b, tok in spans:
        q = tok[0] if tok[:1] in '"\'' and len(tok) > 1 and tok[-1] == tok[0] else ''
        bare = tok[1:-1] if q else tok
        p = expand(bare, ctx.home)
        hit = _through_link(p, dirs, ctx.kit) if p else None
        if hit is None:
            continue
        name, new = hit
        if not os.path.exists(new):
            raise ValueError('%s runs %s through the tools link %s, and the kit has no %s for it to reach'
                             % (where, bare, name, new))
        fwd = new.replace('\\', '/')
        if not q and not whole and ' ' in fwd:
            q = '"'
        out.append(s[last:a] + q + fwd + q)
        last = b
        did.append('re-point the %s path %s (through the tools link %s) to %s' % (where, bare, name, fwd))
    out.append(s[last:])
    return ''.join(out)


def repoint_links(data, ctx):
    """Re-point, in place, every hook path in `data` (any event; `command` and `args`) and the
    statusLine.command path that runs through `<config>/tools/<name>` (links.TOOL_LINKS) to the kit
    file that link reached; [what it did, one line per path]. ValueError when the kit has no such
    file. Runs in every mode: machine setup makes no link, so the path must name the kit's file
    before the owner removes the leftover link."""
    if not isinstance(data, dict):
        return []
    did, dirs = [], _link_dirs(ctx)
    hooks = data.get('hooks')
    for event in sorted(hooks) if isinstance(hooks, dict) else []:
        where = '%s hook' % event
        for h in _hooks(data, event):
            if isinstance(h.get('command'), str):
                h['command'] = _sub_links(h['command'], ctx, dirs, where, did, False)
            if isinstance(h.get('args'), list):
                h['args'] = [_sub_links(a, ctx, dirs, where, did, True) if isinstance(a, str) else a
                             for a in h['args']]
    sl = data.get('statusLine')
    if isinstance(sl, dict) and isinstance(sl.get('command'), str):
        sl['command'] = _sub_links(sl['command'], ctx, dirs, 'statusLine.command', did, False)
    return did


def survey_pm(plan, ctx):
    """The PM SessionStart hook (docs/pm-start.md "Hook (settings.json)"), written to
    ctx.pm_settings and nowhere else; a note when there is none."""
    path = ctx.pm_settings
    if not path:
        plan.note('settings', 'no --pm-settings: the PM SessionStart hook is not written (add it by hand '
                              'to the settings the PM session loads: docs/pm-first-run.md step 7)')
        return
    if not os.path.isdir(os.path.dirname(path)):
        plan.refuse('--pm-settings %s: its folder does not exist' % path)
        return
    for d in ctx.config_dirs:
        acct = os.path.join(d, 'settings.json')
        if norm(acct) == norm(path) or norm(os.path.realpath(acct)) == norm(os.path.realpath(path)):
            plan.refuse('--pm-settings %s is an account settings.json; the PM hook goes only in settings '
                        'the PM session loads' % path)
            return
    if not _pm_hook_elsewhere(plan, path, ctx):
        return
    got = _load(plan, path)
    if got is None:
        return
    raw, layout, data = got
    try:
        did = repoint_moved(data, ctx) + repoint_links(data, ctx)
    except ValueError as e:
        plan.refuse('%s: %s' % (path, e))
        return
    want = os.path.join(ctx.kit, *PM_HOOK.split('/'))
    ours = (norm(want), tuple([host_name()] + list(PM_WORDS)))
    found = False
    for h in _hooks(data, 'SessionStart'):
        tok = _pm_call(h)
        if tok is None:
            continue
        if _signature(h, ctx.home) == ours:
            found = True
            continue
        plan.refuse('%s: a SessionStart hook already runs %s, not this kit\'s %s %s; two PM checklists '
                    'would run (remove or re-point it by hand)' % (path, tok, PM_HOOK, ' '.join(ours[1])))
        return
    if found:
        if did:
            _plan_write(plan, ctx, path, raw, layout, data, did)
        return
    try:
        exe = ctx.host.guard_entry(ctx.kit)['PreToolUse'][0]['hooks'][0]['command']
        if not isinstance(exe, str) or not exe.strip():
            raise ValueError('its command is %r' % (exe,))
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError) as e:
        plan.refuse('%s: cannot write the PM SessionStart hook: boot/hook.py --settings-entry gave no '
                    'interpreter (%s)' % (path, e))
        return
    entry = {'SessionStart': [{'matcher': PM_MATCHER, 'hooks': [
        {'type': 'command', 'command': exe, 'args': [want.replace('\\', '/')] + list(ours[1])}]}]}
    if _append(plan, path, data, entry):
        _plan_write(plan, ctx, path, raw, layout, data,
                    did + ['add the PM SessionStart hook %s %s' % (PM_HOOK, ' '.join(ours[1]))])


PM_SETTINGS_PAIR = ('settings.json', 'settings.local.json')


def _pm_hook_elsewhere(plan, path, ctx):
    """False after a refusal: a hook of any event already runs a pm_session_start.py in the other
    file of path's .claude/settings.json and settings.local.json pair (the PM session loads both)
    or in an account settings.json (every session on that account loads it). The PM checklist
    would then run twice in a PM session. An account settings.json that cannot be read is skipped
    (the settings step refuses it); a sibling that cannot be read is refused."""
    found = []
    for name in PM_SETTINGS_PAIR:
        other = os.path.join(os.path.dirname(path), name)
        if norm(other) == norm(path) or not os.path.lexists(other):
            continue
        try:
            with open(other, 'rb') as f:
                _layout, data = read(f.read())
        except (OSError, ValueError) as e:
            plan.refuse('--pm-settings %s: cannot read %s to check it runs no PM hook already: %s'
                        % (path, other, e))
            return False
        found += [(other, t) for t in _pm_tokens(data)]
    for d in ctx.config_dirs:
        acct = os.path.join(d, 'settings.json')
        try:
            with open(acct, 'rb') as f:
                _layout, data = read(f.read())
        except (OSError, ValueError):
            continue
        found += [(acct, t) for t in _pm_tokens(data)]
    if not found:
        return True
    for where, tok in found:
        plan.refuse('%s already runs %s; with --pm-settings the PM checklist would run twice in a PM '
                    'session (remove it there by hand, or leave out --pm-settings)' % (where, tok))
    return False


def _pm_call(h):
    """What the hook `h` runs when it runs a PM checklist: a pm_session_start.py (any kit's), or a
    boot/hook.py with the PM words after its host (PM_WORDS); else None."""
    toks = _tokens(h)
    for i, t in enumerate(toks):
        name = t.replace('\\', '/').rstrip('/').split('/')
        if name[-1].lower() == 'pm_session_start.py':
            return t
        if name[-2:] == ['boot', 'hook.py'] and tuple(toks[i + 2:i + 2 + len(PM_WORDS)]) == PM_WORDS:
            return ' '.join(toks[i:i + 2 + len(PM_WORDS)])
    return None


def _pm_tokens(data):
    """What every hook of any event in `data` that runs a PM checklist runs (_pm_call)."""
    hooks = data.get('hooks') if isinstance(data, dict) else None
    out = []
    for event in sorted(hooks) if isinstance(hooks, dict) else []:
        for h in _hooks(data, event):
            tok = _pm_call(h)
            if tok is not None:
                out.append(tok)
    return out


def _load(plan, path):
    """(raw, layout, data) for the JSON object in `path`; (None, NEW_LAYOUT, {}) when there is no
    file; None after a refusal."""
    if not os.path.lexists(path):
        return None, dict(NEW_LAYOUT), {}
    if not os.path.isfile(path):
        plan.refuse('%s is not a file' % path)
        return None
    with open(path, 'rb') as f:
        raw = f.read()
    try:
        layout, data = read(raw)
    except ValueError as e:
        plan.refuse('%s is not JSON this step can edit: %s' % (path, e))
        return None
    if not isinstance(data, dict):
        plan.refuse('%s does not hold a JSON object' % path)
        return None
    if data.get('hooks') is not None and not isinstance(data.get('hooks'), dict):
        plan.refuse('%s: "hooks" is not a JSON object; this step cannot read it' % path)
        return None
    return raw, layout, data


def _append(plan, path, data, entry):
    """Append `entry`'s groups to data's hooks, each event's list kept; False after a refusal."""
    hooks = data.get('hooks')
    for event in entry:
        if hooks is not None and event in hooks and not isinstance(hooks[event], list):
            plan.refuse('%s: hooks.%s is not a list; this step cannot append to it' % (path, event))
            return False
    if hooks is None:
        hooks = data['hooks'] = {}
    for event, groups in entry.items():
        hooks.setdefault(event, []).extend(groups)
    return True


def _hooks(data, event):
    hooks = data.get('hooks')
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    for g in groups if isinstance(groups, list) else []:
        for h in (g.get('hooks') or []) if isinstance(g, dict) else []:
            if isinstance(h, dict):
                yield h


def _tokens(h):
    items = [m.group().strip('"\'') for m in TOKEN.finditer(h.get('command'))] \
        if isinstance(h.get('command'), str) else []
    return items + [a for a in h.get('args') or [] if isinstance(a, str)]


def _create(path, new):
    """A new settings.json: written to a .tmp, then renamed onto `path`, which must not exist. The
    .tmp is removed on any failure."""
    tmp = path + '.machine-setup.tmp'
    try:
        with open(tmp, 'wb') as f:
            f.write(new)
        try:
            os.rename(tmp, path)                                     # Windows: refuses an existing file
        except FileExistsError:
            raise RuntimeError('%s appeared since the survey; not replaced (run again)' % path)
    except BaseException:
        _drop(tmp)
        raise


class _Editor(object):
    def __init__(self, ctx, d, path, plan):
        self.ctx, self.d, self.path, self.plan = ctx, d, path, plan
        self.did = []
        self._entry = None

    def guard_entry(self):
        """The guard's settings entry (boot/hook.py --settings-entry), read once per file; raises what
        the host raises."""
        if self._entry is None:
            self._entry = self.ctx.host.guard_entry(self.ctx.kit)
        return self._entry

    def hook(self, event, h):
        """True when `h` is an old kit hook to drop; otherwise its paths are re-pointed in place and a
        shell-form SessionStart hook running the kit's hook entry is written in exec form."""
        where = '%s hook' % event
        if isinstance(h.get('command'), str):
            h['command'], drop, _ = self.rewrite(h['command'], where, True)
            if drop:
                return True
        args = h.get('args')
        if isinstance(args, list):
            out = []
            for a in args:
                if isinstance(a, str):
                    a, drop, words = self.rewrite(a, where, True, whole=True)
                    if drop:
                        return True
                    out += [a] + words
                else:
                    out.append(a)
            args[:] = out
        if event == 'SessionStart':
            self.exec_form(h, where)
        return False

    def exec_form(self, h, where):
        """Rewrite `python <kit>/boot/hook.py <words>...` (shell form) as the guard entry's
        interpreter with `args: [<script>, <words>...]`, so no shell starts; any other command is
        left as it is."""
        cmd = h.get('command')
        if not isinstance(cmd, str) or 'args' in h:
            return
        toks = [m.group() for m in TOKEN.finditer(cmd)]
        if len(toks) < 2:
            return
        interp, script, *words = [t[1:-1] if len(t) > 1 and t[0] in '"\'' and t[-1] == t[0] else t for t in toks]
        if os.path.basename(interp.replace('\\', '/')).lower() not in PYTHONS:
            return
        if len(words) > 3 or not all(WORD.fullmatch(w) for w in words):
            return                              # a redirect or a pipe is the shell's: leave it in shell form
        p = expand(script, self.ctx.home)
        if not p or norm(p) != norm(kit_file(self.ctx.kit, ENTRY)) or not os.path.isfile(p):
            return
        try:
            exe = self.guard_entry()['PreToolUse'][0]['hooks'][0]['command']
            if not isinstance(exe, str) or not exe.strip():
                raise ValueError('its command is %r' % (exe,))
        except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError) as e:
            self.plan.refuse('%s: cannot write the %s %s in exec form: boot/hook.py --settings-entry gave no '
                             'interpreter (%s)' % (self.path, where, ' '.join([ENTRY] + words), e))
            return
        h['command'] = exe
        h['args'] = [p.replace('\\', '/')] + words
        self.did.append('write the %s %s in exec form' % (where, ' '.join([ENTRY] + words)))

    def rewrite(self, s, where, removable, whole=False):
        """(`s` with its old kit paths re-pointed, whether its hook is one to drop, [the words a whole
        argument re-pointed to ENTRY adds after it]); in a command line those words follow the
        path."""
        spans = [(0, len(s), s)] if whole else [(m.start(), m.end(), m.group()) for m in TOKEN.finditer(s)]
        out, last, words = [], 0, []
        for a, b, tok in spans:
            q = tok[0] if tok[:1] in '"\'' and len(tok) > 1 and tok[-1] == tok[0] else ''
            bare = tok[1:-1] if q else tok
            p = expand(bare, self.ctx.home)
            if not p:
                if unresolved_path(bare):
                    self.plan.refuse('%s: %s names %s, a path this step cannot resolve, so it cannot tell '
                                     'whether it is the old kit\'s (write it as an absolute path)'
                                     % (self.path, where, bare))
                continue
            verdict = self.classify(p)
            if verdict is None:
                continue
            base = os.path.basename(p).lower()
            if verdict == 'remove':
                if removable:
                    self.did.append('drop the %s %s' % (where, os.path.basename(p)))
                    return s, True, []
                self.plan.refuse('%s: %s runs %s, which this step only removes from hooks' % (self.path, where, bare))
                continue
            rel = REPOINT[base]
            target = os.path.join(self.ctx.kit, *rel.split('/'))
            if not os.path.isfile(target):
                self.plan.refuse('%s: %s would re-point %s to %s, which the kit lacks'
                                 % (self.path, where, bare, rel))
                continue
            fwd = target.replace('\\', '/')
            added = entry_words(base) if rel == ENTRY else []
            out.append(s[last:a] + q + fwd + q + ('' if whole or not added else ' ' + ' '.join(added)))
            words = added if whole else words
            last = b
            self.did.append('re-point the %s %s%s' % (where, os.path.basename(p),
                                                    ' to %s' % ' '.join([rel] + added) if added else ''))
        out.append(s[last:])
        return ''.join(out), False, words

    def classify(self, p):
        """None (not the old kit's, or kept as is), 'remove' or 'repoint'; refuses the rest. A path
        through a tools link is the kit's file by now (repoint_links ran first)."""
        ctx = self.ctx
        real = os.path.realpath(p)
        old = under(p, ctx.old_kit) or under(real, ctx.old_kit) \
            or under(real, os.path.join(ctx.kit, 'hooks')) \
            or norm(real) == norm(os.path.join(ctx.kit, 'tools', 'guard.py'))
        if not old:
            return None
        base = os.path.basename(p).lower()
        if base in REMOVE:
            return 'remove'
        if base in REPOINT:
            return 'repoint'
        self.plan.refuse('%s: %s is an old kit path with no kit counterpart in this step' % (self.path, p))
        return None


HOME_VARS = re.compile(
    r'(?i)%HOMEDRIVE%%HOMEPATH%|\$env:HOMEDRIVE\$env:HOMEPATH|\$\{(?:HOME|USERPROFILE)\}'
    r'|\$env:(?:USERPROFILE|HOMEPATH|HOME)\b|%(?:USERPROFILE|HOMEPATH)%|\$HOME\b')
PROJECT_ROOT = re.compile(r'(?i)^(?:\$\{?(?:CLAUDE_PROJECT_DIR|CLAUDE_PLUGIN_ROOT)\}?'
                          r'|%(?:CLAUDE_PROJECT_DIR|CLAUDE_PLUGIN_ROOT)%)(?:[\\/]|$)')
SWITCH = re.compile(r'^/[A-Za-z?][\w:]*$')                          # cmd /c, icacls /T
SCRIPT = re.compile(r'(?i)\.(?:py|pyw|ps1|psm1|sh|bat|cmd|js|vbs|exe)$')


def expand(tok, home):
    """`tok` as an absolute path (~, %USERPROFILE%, $env:USERPROFILE, $HOME, ${HOME},
    %HOMEDRIVE%%HOMEPATH% and %HOMEPATH% read as `home`), or '' when it is not one."""
    t = tok
    if t.startswith('~/') or t.startswith('~\\') or t == '~':
        t = home + t[1:]
    t = HOME_VARS.sub(lambda m: home.replace('\\', '/'), t)
    if not re.match(r'^[A-Za-z]:[\\/]|^\\\\|^//', t):
        return ''
    return os.path.normpath(t)


def unresolved_path(tok):
    """True when `tok` names a file this step cannot resolve: a relative path, an unknown variable,
    a script name. A word, a switch, a URL or a per-project path (${CLAUDE_PROJECT_DIR}/...) is not."""
    if PROJECT_ROOT.match(tok) or SWITCH.match(tok) or re.match(r'^[A-Za-z][\w+.-]*://', tok):
        return False
    return bool('/' in tok or '\\' in tok or tok.startswith('~') or SCRIPT.search(tok)
                or re.search(r'%\w+%|\$env:|\$\{|\$\w', tok))


def _signature(h, home):
    """(the normalised path of the first file the hook `h` runs that is not a Python interpreter,
    (the words after it)), or None when it runs none: what tells two hooks apart when every kit hook
    runs one script, ENTRY, told its work by its words."""
    toks = _tokens(h)
    for i, t in enumerate(toks):
        p = expand(t, home)
        if p and os.path.basename(p).lower() not in PYTHONS:
            return norm(p), tuple(toks[i + 1:])
    return None


def describe(sig):
    """A _signature as a short name: the script's name and its words."""
    return ' '.join([os.path.basename(sig[0])] + list(sig[1]))


def _signatures(data, event, home):
    for h in _hooks(data, event):
        sig = _signature(h, home)
        if sig is not None:
            yield sig


def _hook_paths(data, event, home):
    hooks = data.get('hooks')
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    for g in groups if isinstance(groups, list) else []:
        for h in (g.get('hooks') or []) if isinstance(g, dict) else []:
            if not isinstance(h, dict):
                continue
            items = [m.group().strip('"\'') for m in TOKEN.finditer(h.get('command') or '')] \
                if isinstance(h.get('command'), str) else []
            items += [a for a in h.get('args') or [] if isinstance(a, str)]
            for it in items:
                p = expand(it, home)
                if p:
                    yield p


def plugin_hooks(kit, home):
    """(the hooks file the kit's plugin manifest names, {event: the _signature of each of its hooks}),
    with ${CLAUDE_PLUGIN_ROOT} read as `kit`; ('', {}) when the manifest names no hooks file this can
    read."""
    try:
        with open(os.path.join(kit, *PLUGIN_MANIFEST.split('/')), encoding='utf-8-sig') as f:
            manifest = json.load(f)
        rel = manifest.get('hooks') if isinstance(manifest, dict) else None
        if not isinstance(rel, str):
            return '', {}
        with open(os.path.join(kit, os.path.normpath(rel)), encoding='utf-8-sig') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return '', {}
    if not isinstance(data, dict):
        return '', {}
    root = kit.replace('\\', '/')

    def sub(v):
        if isinstance(v, str):
            return PLUGIN_ROOT.sub(lambda m: root, v)
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, dict):
            return dict((k, sub(x)) for k, x in v.items())
        return v
    data = sub(data)
    hooks = data.get('hooks')
    out = {}
    for event in hooks if isinstance(hooks, dict) else []:
        sigs = set(_signatures(data, event, home))
        if sigs:
            out[event] = sigs
    return (rel[2:] if rel.startswith('./') else rel), out


# The kit plugin's machine-lock tools an account's settings allow once the plugin is enabled
# (plugin.edit_settings): exactly these three; lock_wait_change and lock_stats stay out. Only the owner
# widens a permissions grant.
LOCK_ALLOW = ('mcp__plugin_pm-kit_machine-lock__lock_snapshot', 'mcp__plugin_pm-kit_machine-lock__lock_run',
              'mcp__plugin_pm-kit_machine-lock__lock_hold')
REPLACED_EVENT = 'SessionStart'


def plugin_edit(data, kit, home):
    """(a copy of the settings `data` with the plugin's changes, [the allow rules added], [the hooks
    removed, each as describe() names it]); `data` itself is not changed.

    Allow rules: each LOCK_ALLOW rule not already an exact string in permissions.allow is appended
    (permissions and allow are made when absent). ValueError when permissions is not an object or
    allow not a list.

    Hooks: only under hooks.SessionStart, only a `type: command` entry whose _signature (the script
    it runs, normalised, and the words after it) is the signature of one of the kit plugin's own
    SessionStart hooks (plugin_hooks: ${CLAUDE_PLUGIN_ROOT} read as `kit`) and whose script is this
    kit's ENTRY. Today that is `boot/hook.py claude SessionStart` and `... SessionStart coordinator`;
    the PM hook (`... SessionStart pm`, not in the plugin), another kit's hook and every other event
    stay. A group's hooks list emptied by this step goes, and SessionStart when this step emptied it;
    `hooks` stays."""
    new = json.loads(json.dumps(data))
    added = []
    perms = new.get('permissions')
    if perms is None:
        perms = new['permissions'] = {}
    if not isinstance(perms, dict):
        raise ValueError('"permissions" is not a JSON object')
    allow = perms.get('allow')
    if allow is None:
        allow = perms['allow'] = []
    if not isinstance(allow, list):
        raise ValueError('"permissions.allow" is not a list')
    for rule in LOCK_ALLOW:
        if rule not in allow:
            allow.append(rule)
            added.append(rule)
    dropped = []
    entry = norm(kit_file(kit, ENTRY))
    sigs = set(s for s in plugin_hooks(kit, home)[1].get(REPLACED_EVENT, ()) if s[0] == entry)
    hooks = new.get('hooks')
    groups = hooks.get(REPLACED_EVENT) if isinstance(hooks, dict) else None
    if not sigs or not isinstance(groups, list):
        return new, added, dropped
    emptied = 0
    for g in list(groups):
        hs = g.get('hooks') if isinstance(g, dict) else None
        if not isinstance(hs, list):
            continue
        gone = 0
        for h in list(hs):
            sig = _signature(h, home) if isinstance(h, dict) and h.get('type') == 'command' else None
            if sig is not None and sig in sigs:
                hs.remove(h)
                dropped.append(describe(sig))
                gone += 1
        if gone and not hs:                                          # only a group this step emptied
            groups.remove(g)
            emptied += 1
    if emptied and not groups:                                       # only an event this step emptied
        del hooks[REPLACED_EVENT]
    return new, added, dropped


def read(raw):
    """(layout, data): how the bytes were laid out, and the parsed JSON."""
    bom = raw.startswith(b'\xef\xbb\xbf')
    text = raw.decode('utf-8-sig')
    crlf = '\r\n' in text
    body = text.replace('\r\n', '\n')
    data = json.loads(body)
    final = body.endswith('\n')
    core = body[:-1] if final else body
    layout = {'bom': bom, 'crlf': crlf, 'final': final, 'indent': 2, 'ascii': False, 'exact': False}
    for indent in (2, 4, '\t', 1, 3):
        for ascii_ in (False, True):
            if json.dumps(data, indent=indent, ensure_ascii=ascii_) == core:
                layout.update(indent=indent, ascii=ascii_, exact=True)
                return layout, data
    return layout, data


def write(layout, data):
    text = json.dumps(data, indent=layout['indent'], ensure_ascii=layout['ascii'])
    if layout['final']:
        text += '\n'
    if layout['crlf']:
        text = text.replace('\n', '\r\n')
    raw = text.encode('utf-8')
    return (b'\xef\xbb\xbf' + raw) if layout['bom'] else raw


def shared_link(path):
    """'a symbolic link' / 'a hard link (N names)' when os.replace would split `path` from a file it
    shares, else ''."""
    if os.path.islink(path):
        return 'a symbolic link'
    try:
        n = os.stat(path).st_nlink
    except OSError:
        return ''
    return 'a hard link (%d names)' % n if n > 1 else ''


def _write(path, raw, new, bak):
    shared = shared_link(path)
    if shared:
        raise RuntimeError('%s is %s; not replaced' % (path, shared))
    with open(path, 'rb') as f:
        if f.read() != raw:
            raise RuntimeError('%s changed since the survey; run again' % path)
    with open(bak, 'xb') as f:
        f.write(raw)
    tmp = path + '.machine-setup.tmp'
    try:
        with open(tmp, 'wb') as f:
            f.write(new)
        os.replace(tmp, path)
    except BaseException:
        _drop(tmp)
        raise


def _drop(tmp):
    """Remove a .tmp this step wrote, if it is there; a failure to remove it is ignored."""
    if os.path.lexists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
