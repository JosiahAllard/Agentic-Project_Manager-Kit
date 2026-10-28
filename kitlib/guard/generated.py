"""The files of a kit checkout that tools/render_content.py writes, and what each is rendered from
(the guard's generated-file rule; docs/guard.md "Generated files"; decision 19 in
docs/multi-host-plan.md section 7).

  source(top, rel)   for `rel` (a path relative to the checkout `top`, either separator) that the
                     renderer writes: the source a person edits instead; None for any other path,
                     and for every path when `top` is not a kit checkout (kitlib.content.oncommit.is_kit)

What is rendered, by path (case folded, as Windows reads it):
  hosts/<host>/shell/agents/**     a role's agent file: content/roles/<role>.md (the host's agent
                                   name prefix, when its files carry one, dropped from the file name)
  hosts/<host>/shell/skills/**     a skill's copy: the same file under content/skills/
  hosts/<host>/shell/boot/**       the plugin's copy of a boot file: the same file under boot/
  hosts/<host>/shell/hosts.json    the plugin's copy of hosts.json's entry: hosts.json
  hosts/<host>/shell/plugin.json, mcp.json, hooks/hooks.json, hooks/hooks.linux.json, and
  .<host>-plugin/plugin.json and
  marketplace.json at the root     a plugin manifest: kit.json, hosts.json and the host's renderer
A path matched here is refused even when the renderer no longer writes it (a retired folder): the
render removes it. So is the agents, skills or boot folder itself (a shell `rm -r` of it), whose
source is the folder of sources. The renderer itself runs as a process, which the guard does not
judge.
"""
import os

from kitlib import agentname
from kitlib.content import oncommit

ROLES = 'content/roles/'
SKILLS = 'content/skills/'
MANIFESTS = {('plugin.json',), ('mcp.json',), ('hooks', 'hooks.json'), ('hooks', 'hooks.linux.json')}
ROOT_MANIFESTS = ('plugin.json', 'marketplace.json')    # in a host's .<host>-plugin folder at the root


def _manifest_source(host):
    return 'kit.json, hosts.json and hosts/%s/adapter/content.py' % host


def _role_source(top, host, stem):
    names = [stem]
    try:
        pre = agentname.prefix(host)
    except Exception:                           # a host hosts.json does not register: no prefix
        pre = ''
    if pre and stem.startswith(pre):
        names.append(stem[len(pre):])
    for n in names:
        if os.path.isfile(os.path.join(top, 'content', 'roles', n + '.md')):
            return ROLES + n + '.md'
    return ROLES + names[-1] + '.md'


def source(top, rel):
    """The source of the rendered path `rel` in the kit checkout `top`, or None (module docstring)."""
    parts = [p for p in rel.replace('\\', '/').split('/') if p not in ('', '.')]
    low = tuple(p.lower() for p in parts)
    if '..' in low or not top or not oncommit.is_kit(top):
        return None
    if len(low) == 2 and low[0].startswith('.') and low[0].endswith('-plugin') and low[1] in ROOT_MANIFESTS:
        return _manifest_source(low[0][1:-len('-plugin')])
    if len(low) < 4 or low[0] != 'hosts' or low[2] != 'shell':
        return None
    host, kind, rest = parts[1], low[3], parts[4:]
    # the folder itself too (`rm -rf hosts/<host>/shell/agents`): its source is the sources' folder
    if kind == 'agents':
        return _role_source(top, host, os.path.splitext(rest[-1])[0]) if len(rest) == 1 else ROLES
    if kind == 'skills':
        return SKILLS + '/'.join(rest)
    if kind == 'boot':
        return 'boot/' + '/'.join(rest)
    if low[3:] == ('hosts.json',):
        return 'hosts.json'
    if low[3:] in MANIFESTS:
        return _manifest_source(host)
    return None
