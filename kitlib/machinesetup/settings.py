"""Per-account settings.json (docs/machine-setup.md "Settings"): drop the old kit's PreToolUse hooks,
add guard.py's entry as `guard.py --settings-entry` prints it, re-point the old kit's SessionStart
hooks and statusline scripts to this kit, write a SessionStart hook that runs a kit hook script in
shell form (`python <script>`) in exec form (the guard entry's interpreter, `args: [<script>]`), and
note a settings hook that the kit's plugin also carries (it runs twice once the plugin is enabled).
statusLine stays in shell form: Claude Code gives it no `args` field.

In fresh mode (ctx.fresh, no older kit clone) none of that rewriting runs: guard.py's entry is appended
where it is absent (a missing settings.json is created), and survey_pm appends the PM SessionStart
hook to ctx.pm_settings alone.

Every path in a hook's `command` and `args` and in statusLine.command is read; one that resolves
into the old kit clone is either in REMOVE (its hook goes; a group or event left empty goes too),
in REPOINT (rewritten to this kit's file, forward slashes, quotes kept) or refused, named. A path
through `<config>/tools/<link>` is left: the links step points it at this kit, and it is refused
only if this kit has no such file. The JSON is edited as data and written back in the layout it
was read in (indent, ASCII escaping, line ends, BOM, final newline), so a dry run's diff shows
only the changed lines.
"""
import difflib
import json
import os
import re

from .common import free_name, norm, under
from . import links

REMOVE = ('no_root_search.py', 'main_checkout_redirect_guard.py')
REPOINT = {
    'pm_session_start.py': 'claude/hooks/pm_session_start.py',
    'coordinator_session_start.py': 'claude/hooks/coordinator_session_start.py',
    'statusline-usage.ps1': 'optional/usage/statusline-usage.ps1',
    'write-usage-status.ps1': 'optional/usage/write-usage-status.ps1',
}
GUARD = 'claude/hooks/guard.py'
PYTHONS = ('python', 'python.exe', 'pythonw', 'pythonw.exe')
PLUGIN_MANIFEST = '.claude-plugin/plugin.json'
PLUGIN_ROOT = re.compile(r'\$\{CLAUDE_PLUGIN_ROOT\}|\$CLAUDE_PLUGIN_ROOT\b')
TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|[^\s"\']+')


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
        sl['command'], _ = ed.rewrite(sl['command'], 'statusLine.command', removable=False)
    _note_plugin_twins(plan, path, data, ctx)
    guard = norm(os.path.join(ctx.kit, *GUARD.split('/')))
    if not any(norm(p) == guard for p in _hook_paths(data, 'PreToolUse', ctx.home)):
        try:
            entry = ed.guard_entry()
        except (OSError, RuntimeError, ValueError) as e:
            plan.refuse('cannot read guard.py --settings-entry: %s' % e)
            return
        if not isinstance(data.get('hooks'), dict):
            data['hooks'] = {}
        for event, groups in entry.items():
            data['hooks'].setdefault(event, []).extend(groups)
        ed.did.append('add guard.py\'s PreToolUse entry')
    if not ed.did:
        return
    _plan_write(plan, ctx, path, raw, layout, data, ed.did)


def _note_plugin_twins(plan, path, data, ctx):
    rel, plugin = plugin_hooks(ctx.kit, ctx.home)
    for event in sorted(plugin):
        seen = set()
        for p in _hook_paths(data, event, ctx.home):
            if norm(p) in plugin[event] and norm(p) not in seen:
                seen.add(norm(p))
                plan.note('settings', '%s: the %s hook %s is also the kit plugin\'s hook (%s); with the plugin '
                                      'enabled it runs twice, so remove this entry in the same step as the '
                                      'plugin install' % (path, event, os.path.basename(p), rel))


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
    shared = _shared(path)
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


# Fresh mode (--fresh): append only. No entry is removed, re-pointed or reshaped; a missing file is
# created in NEW_LAYOUT; the PM SessionStart hook goes to --pm-settings alone.
NEW_LAYOUT = {'bom': False, 'crlf': False, 'final': True, 'indent': 2, 'ascii': False, 'exact': True}
PM_HOOK = 'claude/hooks/pm_session_start.py'
PM_MATCHER = 'startup|resume|compact'


def _survey_fresh(plan, path, ctx):
    got = _load(plan, path)
    if got is None:
        return
    raw, layout, data = got
    _note_plugin_twins(plan, path, data, ctx)
    guard = norm(os.path.join(ctx.kit, *GUARD.split('/')))
    if any(norm(p) == guard for p in _hook_paths(data, 'PreToolUse', ctx.home)):
        return
    try:
        entry = ctx.host.guard_entry(ctx.kit)
    except (OSError, RuntimeError, ValueError) as e:
        plan.refuse('cannot read guard.py --settings-entry: %s' % e)
        return
    if _append(plan, path, data, entry):
        _plan_write(plan, ctx, path, raw, layout, data, ['add guard.py\'s PreToolUse entry'])


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
    want = os.path.join(ctx.kit, *PM_HOOK.split('/'))
    found = False
    for h in _hooks(data, 'SessionStart'):
        for tok in _tokens(h):
            if os.path.basename(tok.replace('\\', '/')).lower() != 'pm_session_start.py':
                continue
            p = expand(tok, ctx.home)
            if p and norm(p) == norm(want):
                found = True
                continue
            plan.refuse('%s: a SessionStart hook already runs %s, not this kit\'s %s; two PM checklists '
                        'would run (remove or re-point it by hand)' % (path, tok, PM_HOOK))
            return
    if found:
        return
    try:
        exe = ctx.host.guard_entry(ctx.kit)['PreToolUse'][0]['hooks'][0]['command']
        if not isinstance(exe, str) or not exe.strip():
            raise ValueError('its command is %r' % (exe,))
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError) as e:
        plan.refuse('%s: cannot write the PM SessionStart hook: guard.py --settings-entry gave no '
                    'interpreter (%s)' % (path, e))
        return
    entry = {'SessionStart': [{'matcher': PM_MATCHER, 'hooks': [
        {'type': 'command', 'command': exe, 'args': [want.replace('\\', '/')]}]}]}
    if _append(plan, path, data, entry):
        _plan_write(plan, ctx, path, raw, layout, data, ['add the PM SessionStart hook pm_session_start.py'])


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


def _pm_tokens(data):
    """Every pm_session_start.py a hook of any event in `data` runs."""
    hooks = data.get('hooks') if isinstance(data, dict) else None
    out = []
    for event in sorted(hooks) if isinstance(hooks, dict) else []:
        for h in _hooks(data, event):
            out += [t for t in _tokens(h)
                    if os.path.basename(t.replace('\\', '/')).lower() == 'pm_session_start.py']
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
        """guard.py --settings-entry, read once per file; raises what the host raises."""
        if self._entry is None:
            self._entry = self.ctx.host.guard_entry(self.ctx.kit)
        return self._entry

    def hook(self, event, h):
        """True when `h` is an old kit hook to drop; otherwise its paths are re-pointed in place and a
        shell-form SessionStart hook running a kit hook script is written in exec form."""
        where = '%s hook' % event
        if isinstance(h.get('command'), str):
            h['command'], drop = self.rewrite(h['command'], where, True)
            if drop:
                return True
        args = h.get('args')
        if isinstance(args, list):
            for i, a in enumerate(args):
                if isinstance(a, str):
                    args[i], drop = self.rewrite(a, where, True, whole=True)
                    if drop:
                        return True
        if event == 'SessionStart':
            self.exec_form(h, where)
        return False

    def exec_form(self, h, where):
        """Rewrite `python <kit>/claude/hooks/<x>.py` (shell form) as the guard entry's interpreter
        with `args: [<script>]`, so no shell starts; any other command is left as it is."""
        cmd = h.get('command')
        if not isinstance(cmd, str) or 'args' in h:
            return
        toks = [m.group() for m in TOKEN.finditer(cmd)]
        if len(toks) != 2:
            return
        interp, script = [t[1:-1] if len(t) > 1 and t[0] in '"\'' and t[-1] == t[0] else t for t in toks]
        if os.path.basename(interp.replace('\\', '/')).lower() not in PYTHONS:
            return
        p = expand(script, self.ctx.home)
        hooks_dir = os.path.join(self.ctx.kit, 'claude', 'hooks')
        if not p or not p.lower().endswith('.py') or not under(p, hooks_dir) or not os.path.isfile(p):
            return
        try:
            exe = self.guard_entry()['PreToolUse'][0]['hooks'][0]['command']
            if not isinstance(exe, str) or not exe.strip():
                raise ValueError('its command is %r' % (exe,))
        except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError) as e:
            self.plan.refuse('%s: cannot write the %s %s in exec form: guard.py --settings-entry gave no '
                             'interpreter (%s)' % (self.path, where, os.path.basename(p), e))
            return
        h['command'] = exe
        h['args'] = [p.replace('\\', '/')]
        self.did.append('write the %s %s in exec form' % (where, os.path.basename(p)))

    def rewrite(self, s, where, removable, whole=False):
        """(`s` with its old kit paths re-pointed, whether its hook is one to drop)."""
        spans = [(0, len(s), s)] if whole else [(m.start(), m.end(), m.group()) for m in TOKEN.finditer(s)]
        out, last = [], 0
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
                    return s, True
                self.plan.refuse('%s: %s runs %s, which this step only removes from hooks' % (self.path, where, bare))
                continue
            rel = REPOINT[base]
            target = os.path.join(self.ctx.kit, *rel.split('/'))
            if not os.path.isfile(target):
                self.plan.refuse('%s: %s would re-point %s to %s, which the kit lacks'
                                 % (self.path, where, bare, rel))
                continue
            fwd = target.replace('\\', '/')
            out.append(s[last:a] + q + fwd + q)
            last = b
            self.did.append('re-point the %s %s' % (where, os.path.basename(p)))
        out.append(s[last:])
        return ''.join(out), False

    def classify(self, p):
        """None (not the old kit's, or kept as is), 'remove' or 'repoint'; refuses the rest."""
        ctx = self.ctx
        for d in ctx.config_dirs:
            for name, rel in links.TOOL_LINKS:
                link = os.path.join(d, 'tools', name)
                if under(p, link):
                    rest = os.path.relpath(norm(p), norm(link))
                    after = os.path.join(ctx.kit, *rel.split('/'))
                    if rest != '.':
                        after = os.path.join(after, rest)
                    if not os.path.exists(after):
                        self.plan.refuse('%s: %s goes through the tools link %s, and the kit has '
                                         'no %s for it to reach' % (self.path, p, name, after))
                    return None
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
    """(the hooks file the kit's plugin manifest names, {event: normalised paths its hooks run}), with
    ${CLAUDE_PLUGIN_ROOT} read as `kit`; ('', {}) when the manifest names no hooks file this can read."""
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
        paths = set(norm(p) for p in _hook_paths(data, event, home))
        if paths:
            out[event] = paths
    return (rel[2:] if rel.startswith('./') else rel), out


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


def _shared(path):
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
    shared = _shared(path)
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
