"""Per-account settings.json (docs/machine-setup.md "Settings"): drop the old kit's PreToolUse hooks,
add guard.py's entry as `guard.py --settings-entry` prints it, re-point the old kit's SessionStart
hooks and statusline scripts to this kit.

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
    'conductor_session_start.py': 'claude/hooks/coordinator_session_start.py',
    'statusline-usage.ps1': 'optional/usage/statusline-usage.ps1',
    'write-usage-status.ps1': 'optional/usage/write-usage-status.ps1',
}
GUARD = 'claude/hooks/guard.py'
TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|[^\s"\']+')


def survey(plan, d, ctx):
    path = os.path.join(d, 'settings.json')
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
    guard = norm(os.path.join(ctx.kit, *GUARD.split('/')))
    if not any(norm(p) == guard for p in _hook_paths(data, 'PreToolUse', ctx.home)):
        try:
            entry = ctx.host.guard_entry(ctx.kit)
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
    new = write(layout, data)
    if new == raw:
        return
    shared = _shared(path)
    if shared:
        plan.refuse('%s is %s; replacing it would split it from the file it shares (edit it by hand: %s)'
                    % (path, shared, '; '.join(ed.did)))
        return
    if not layout['exact']:
        plan.note('settings', '%s: its layout is not one json.dumps writes; the whole file is '
                              'rewritten (indent %r)' % (path, layout['indent']))
    old_text, new_text = raw.decode('utf-8-sig'), new.decode('utf-8-sig')
    diff = [l.rstrip('\r\n') for l in difflib.unified_diff(
        old_text.splitlines(True), new_text.splitlines(True), path, path + ' (after)', n=1)]
    bak = free_name('%s.machine-setup-%s.bak' % (path, ctx.today))
    plan.add('settings', '%s: %s (backup: %s)' % (path, '; '.join(ed.did), os.path.basename(bak)),
             lambda: _write(path, raw, new, bak), undo='copy "%s" over "%s"' % (bak, path),
             detail=diff)


class _Editor(object):
    def __init__(self, ctx, d, path, plan):
        self.ctx, self.d, self.path, self.plan = ctx, d, path, plan
        self.did = []

    def hook(self, event, h):
        """True when `h` is an old kit hook to drop; otherwise its paths are re-pointed in place."""
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
        return False

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
    with open(tmp, 'wb') as f:
        f.write(new)
    os.replace(tmp, path)
